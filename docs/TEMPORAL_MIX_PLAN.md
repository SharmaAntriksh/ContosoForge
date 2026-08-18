# Temporal Mix Dynamics Implementation Plan

## Status

Phases 1 through 5 completed on 2026-08-18. The pure factor engine, reusable rank
diagnostics, deterministic analytical baselines, customer-side country/gender
integration, and hierarchical product-side category/brand/color integration are
implemented. The factors use relative months, the resolved trend preset, and the
run seed; no temporal-mix controls are exposed to users. Global monthly plans and
output schemas remain unchanged. Calibration now covers representative trend
families, the reported 43K-customer/2M-sales dataset, and 20-year large-pool
performance. The intentional sales-output change has been reviewed across all 15
golden fixture/grid cells, and the absolute fingerprint baseline is now updated.

## Problem statement

The generated sales fact has a realistic global time curve, but many entity
rankings remain nearly stationary from year to year. In the reference run
`2026-08-18 01_20_39 PM Customers 43K Sales 2M PARQUET`, the observed top-rank
consistency over five years was:

| Analytical axis | Observed behavior |
|---|---|
| Product category | Every top-10 rank was unchanged |
| Product color | Every top-10 rank was unchanged |
| Product brand | Some movement, but top-10 rank consistency remained about 74% |
| Customer gender | The leading gender changed only once |
| Customer country | Most ranks were occupied by the same country in four or five years |

This is not a country-specific defect. It is a general consequence of applying a
global demand curve to entity populations whose relative weights are largely
static:

- customers retain a fixed `CustomerBaseWeight`;
- country and gender have no time-varying group propensity;
- category and color have no time-varying demand component;
- product popularity and most product structure remain persistent;
- calendar seasonality repeats every year;
- the current rotating-brand winner is too narrow to overcome catalog size,
  price, popularity, and category composition consistently.

The global trend changes the size of the business, but it does not sufficiently
change the mix of the business.

## Product decision

Temporal mix dynamics will be internal generator behavior. No new end-user
configuration knobs will be added.

The existing trend selection remains the only user-facing business-shape choice:

```yaml
models:
  macro_demand:
    trend: gradual-growth
```

The resolved trend supplies the intensity and character of temporal mix movement.
Users should not need to understand volatility, persistence, drift, shock decay,
factor bounds, hierarchy weights, or normalization rules.

## Goals

1. Produce gradual and credible rank movement across multiple analytical axes.
2. Work for any configured date range without calendar-year-specific values.
3. Keep persistent structural differences: large markets and strong categories
   should remain more stable than closely matched peers.
4. Make stable trends calmer and volatile trends more dynamic automatically.
5. Preserve exact global monthly row, order, and distinct-customer plans.
6. Preserve worker-count and chunk-size determinism.
7. Preserve product/store eligibility, customer lifecycle rules, pricing rules,
   and output schemas.
8. Add no required dimension regeneration when only sales are rerun.
9. Keep runtime memory proportional to entities plus months times groups, not
   months times customers or products.

## Non-goals

- Randomly reshuffling every rank every year.
- Guaranteeing that the top entity changes in every period.
- Altering `UnitPrice`, `NetPrice`, `UnitCost`, or `Quantity` to manufacture rank
  movement.
- Changing total sales rows or allocating independent country/category row totals.
- Adding temporal-factor columns to output tables.
- Exposing an advanced market-model configuration surface.
- Applying dynamics automatically to every low-cardinality dimension attribute.
  Only analytically meaningful axes will be wired deliberately.

## Proposed architecture

Introduce an internal `TemporalMixPlan`, built once from resolved run inputs and
broadcast read-only to sales workers.

```text
Resolved trend + run seed + date pool + dimension attributes
                              |
                              v
                    TemporalMixPlan builder
                              |
              +---------------+----------------+
              |                                |
              v                                v
     Customer-side factors             Product-side factors
     country, gender                    category, brand, color
              |                                |
              v                                v
     Monthly customer pool              Monthly product weights
              |                                |
              +---------------+----------------+
                              |
                              v
                    Existing sales pipeline
```

The plan should contain compact group mappings and factor matrices rather than
materialized customer-by-month or product-by-month matrices.

### Initial analytical axes

Customer side:

- Country
- Gender

Product side:

- Category
- Brand residual within category
- Color residual within category

The engine should be generic enough to support store, channel, subcategory, or
other axes later, but those axes should not be enabled without separate analysis.

## Generic temporal factor engine

Add a pure internal module:

```text
src/facts/sales/sales_models/temporal_mix.py
```

Its core operation is conceptually:

```python
build_temporal_factors(
    axis_name,
    group_names,
    baseline_mass,
    month_count,
    resolved_trend,
    run_seed,
    parent_group=None,
)
```

The function returns a positive factor matrix with shape `(T, G)`, where `T` is
the number of relative months in the run and `G` is the number of groups on the
axis.

### Time model

Factors evolve on relative month offsets `0..T-1`, not named calendar years.
The same algorithm therefore works for 18-month, 5-year, 20-year, or longer runs.

Each group receives a deterministic, persistent, mean-reverting log-space state:

```text
state[group, month] =
    persistence * state[group, month - 1]
    + trend-derived drift
    + trend-derived innovation
    + active trend-derived shocks

factor[group, month] = exp(state[group, month])
```

Random inputs must be stable functions of:

```text
(run_seed, axis_name, group_name, relative_month, purpose_salt)
```

Do not use Python's process-randomized `hash()`. Use the repository's stable hash
utilities or a new explicitly specified integer hash implementation.

The first `N` months must remain identical when a run with the same start and seed
is extended from `N` to `N + K` months.

### Trend-derived behavior

The temporal engine reads the already resolved trend rather than new YAML fields.
At minimum, it derives behavior from:

- macro `noise_std`;
- `shock_probability` and `shock_impact`;
- the slope and variation of the resolved year-level curve;
- customer acquisition/churn intensity where relevant;
- whether the preset represents stable, directional, cyclical, or volatile demand.

Internal calibration constants may exist in code, but there must be no hard-coded
country, gender, category, brand, color, or calendar-year factor values.

Expected qualitative behavior:

| Existing trend family | Temporal mix behavior |
|---|---|
| Stable growth | Small, persistent movements and occasional adjacent swaps |
| Gradual/strong growth | Unequal capture of growth across groups |
| Decline | Unequal erosion rather than every group shrinking proportionally |
| Recession/recovery | Groups enter and leave the downturn at different strengths |
| Boom/bust or volatile | Larger idiosyncratic shocks with eventual mean reversion |
| Stagnation | Flat global demand with modest mix movement |

### Normalization

Every factor layer must be centered by its baseline mass so it changes relative mix
without replacing the global macro curve.

For an axis with baseline mass `mass[g]`:

```text
weighted_mean[m] = sum(mass[g] * factor[m, g]) / sum(mass[g])
factor[m, g] = factor[m, g] / weighted_mean[m]
```

All factors must be finite and strictly positive. Final CDFs must remain monotonic,
and `cdf[-1]` must be clamped to exactly `1.0`.

## Hierarchical factor composition

Naively multiplying independent category, brand, and color processes would double
count structural effects and create excessive variance. Product factors must be
hierarchical and log-additive.

### Product hierarchy

```text
log ProductTemporalFactor =
    CategoryEffect
    + BrandWithinCategoryResidual
    + ColorWithinCategoryResidual
```

- Category effects are normalized across the full catalog baseline mass.
- Brand effects are centered within category.
- Color effects are centered within category.
- A brand occurring in multiple categories is represented internally by the
  compound group `(CategoryKey, Brand)`.
- Reporting still aggregates normally by the existing `Brand` column.

The total temporal variance budget must be split across layers internally so that
three moderate effects do not multiply into an extreme result.

### Customer composition

```text
log CustomerTemporalFactor =
    CountryEffect
    + GenderEffect
```

Country effects are centered using aggregated `CustomerBaseWeight`. Gender effects
are also centered by customer baseline mass. The combined customer factor is
normalized once more across the eligible customer population for the month.

The initial implementation does not need a country-by-gender interaction. Add one
only if later analytical validation shows that independent marginal effects produce
implausible combinations.

## Customer sampling integration

Customer temporal dynamics are a core allocation policy, not a per-line correlated
fact enrichment. They intentionally change which customers receive orders while
preserving all global plan totals.

Existing base behavior:

```text
CustomerBaseWeight
```

New monthly behavior:

```text
EffectiveCustomerWeight =
    CustomerBaseWeight
    * CountryTemporalFactor[month, country]
    * GenderTemporalFactor[month, gender]
```

Apply the effective weight in both places owned by
`sales_logic/core/customer_sampling.py`:

1. selecting previously introduced customers for the month's distinct pool;
2. building the repeat-order CDF over that pool.

Keep the forced discovery/debut cohort behavior unchanged. Customer coverage and
nearest-expiry rules take precedence over temporal mix. The feature changes the mix
of optional and repeat participation, not lifecycle validity.

Required internal data:

- `customer_country_id_by_pool`;
- `customer_gender_id_by_pool`;
- a dense `CustomerKey -> pool index` lookup for repeat-order CDF construction;
- customer-side factor matrices.

Do not assume `CustomerKey - 1` is the pool position, especially with customer SCD2.

## Product sampling integration

The current monthly product weight combines persistent popularity with repeating
calendar seasonality. Extend it to:

```text
EffectiveProductWeight =
    ExistingPopularityWeight
    * ExistingCalendarSeasonality
    * CategoryTemporalFactor
    * BrandWithinCategoryResidual
    * ColorWithinCategoryResidual
```

Apply this weight before store assortment CDF construction. Store assortments,
channel eligibility, basket constraints, and segment correlations remain hard
constraints and continue to filter the eligible product pool.

Required internal data:

- product-row-to-category mapping;
- product-row-to-category-brand mapping;
- product-row-to-category-color mapping;
- product-side factor matrices.

### Cache-key correction

`_build_product_weight_for_month()` currently memoizes by calendar month because
its inputs repeat annually. Temporal mix makes the result depend on absolute relative
month `m_offset`.

The product-weight cache must therefore be keyed by `m_offset`, or by a complete key
containing every dependency. The store/product CDF cache must likewise use
`m_offset`. January of two different years must never reuse the same temporal CDF.

This is a determinism requirement, not merely a freshness optimization: an incomplete
worker-lifetime cache key can make output depend on which chunk reaches a worker first.

## Existing brand-popularity migration

The current rotating-winner brand model must not be multiplied on top of the unified
temporal brand residual. Doing so would give brand two independent temporal effects.

Migration plan:

1. Move rotating brand behavior into the generic temporal factor engine.
2. Stop exposing `brand_popularity` in the default `models.yaml` documentation.
3. Continue accepting the existing block for backward compatibility during a
   deprecation period.
4. Translate legacy values internally where practical, or issue a clear deprecation
   warning if an exact translation is not meaningful.
5. Do not silently ignore an existing user configuration.

The final default path should have one owner for category, brand, and color temporal
mix.

## SCD2 behavior

### Customers

Customer country may change under SCD2 relocation. A sales row must use the country
effective during its month, not the customer's current/future country.

Extend the customer SCD2 version grid to retain `GeographyKey` per version alongside
the versioned `CustomerKey`. Resolve the effective geography at monthly grain for
customer-pool weighting. Gender is expected to remain invariant but should still be
validated against version history rather than assumed silently.

The exact order date can fall on either side of a mid-month relocation. The temporal
model operates at monthly grain, so define and test a consistent boundary rule, such
as geography effective at month start. The final emitted `CustomerKey` continues to
use the existing exact-day SCD2 resolution.

### Products

Current product SCD2 represents price revisions. Category, brand, and color should be
mapped by stable `ProductID`/current product pool unless product SCD2 later permits
taxonomy changes. Add a validation guard so future taxonomy-changing SCD2 cannot
silently reuse stale mappings.

## Coordinator and worker-state changes

Suggested ownership:

| Area | Change |
|---|---|
| `sales_models/temporal_mix.py` | Pure factor generation and normalization |
| `prep/run_config.py` | Frozen `TemporalMixPlan` holder |
| `prep/dimension_loaders.py` | Load/group customer and product axis attributes |
| `prep/correlation_lookups.py` | Stable group-name-to-ID mappings where shared |
| `sales.py` | Build the plan once after dimensions and trend resolution |
| `prep/worker_cfg_schema.py` | Declare logical worker-config fields and shared arrays |
| `prep/worker_cfg_builder.py` | Add plan arrays to the serialization boundary |
| `sales_worker/init.py` | Resolve and validate shared arrays once |
| `sales_logic/globals.py` | Declare read-only State fields |
| `sales_logic/core/customer_sampling.py` | Apply customer factors |
| `sales_logic/chunk_builder.py` | Apply product factors and correct cache keys |

Large mapping arrays and factor matrices must be included in `SHARED_FIELDS` so
Windows worker processes receive zero-copy shared-memory views rather than copies.

Do not place mutable or lazily generated cross-chunk state on `State`.

## Memory and performance model

Allowed memory shape:

```text
O(customers + products + months * total_group_values)
```

Disallowed memory shape:

```text
O(months * customers)
O(months * products)  # for unbounded product catalogs
```

Workers may materialize one length-`N_products` effective product-weight vector per
month and cache it by `m_offset`. They should not receive a full product-by-month
matrix.

Factor matrices can use `float32` if numerical validation shows no meaningful CDF
drift; CDF construction and normalization must use `float64`.

## Determinism contract

The implementation must retain:

1. identical output for repeated runs with the same configuration and seed;
2. identical output for workers 1, 2, 4, and other supported counts;
3. chunk-shape invariance across chunk sizes;
4. a static global per-month row/order/distinct plan;
5. no RNG whose consumption depends on worker scheduling;
6. complete worker-lifetime cache keys;
7. stable factor generation independent of dimension row order.

Group randomness must be keyed by stable group labels or stable business keys, not
transient integer IDs assigned from current file ordering.

## Validation and diagnostics

The plan should emit concise generation diagnostics without changing output schemas:

- factor range by axis;
- number of values per axis;
- baseline and realized share concentration;
- rank-turnover summary by year for generated QA runs;
- warnings for missing/unknown group mappings;
- warnings when a factor layer has no practical effect because only one group is
  eligible;
- confirmation that all factor and CDF arrays are finite.

No user action should be required in normal operation.

## Test plan

### Pure factor-engine tests

- same inputs and seed produce identical factors;
- extending the end date preserves the existing month prefix;
- reordering input groups produces the same factor for each named group;
- all factors are positive and finite;
- weighted mean is `1.0` per month within numerical tolerance;
- parent-centered residuals preserve parent mass;
- stable and volatile trends produce appropriately ordered movement intensity;
- no calendar-year assumptions exist.

### Customer sampling tests

- effective customer weights alter country and gender participation;
- total rows, orders, and monthly distinct-customer counts do not change;
- forced debut and expiry behavior remain intact;
- sparse/SCD2 `CustomerKey` values map to the correct pool positions;
- country relocation uses the defined effective-month rule;
- no customer outside its eligibility interval is sampled.

### Product sampling tests

- category, brand, and color factors change product-selection probabilities;
- store assortment and channel eligibility remain hard constraints;
- brand is not double-weighted by the legacy and unified models;
- January in two absolute years builds different CDFs when temporal factors differ;
- all cached CDFs are keyed by their complete dependencies;
- product SCD2 price resolution remains unchanged.

### End-to-end determinism tests

Run the temporal model through:

- `tests/test_sales_determinism_workers.py`;
- `tests/test_sales_determinism_chunksize.py`;
- `tests/test_sales_snapshot.py`;
- customer and product SCD2 fixtures;
- parquet, CSV, and Delta writer fixtures already covered by the snapshot matrix.

### Analytical quality tests

Use fixed-seed multi-year fixtures and compute, for each axis:

- year-over-year Spearman rank correlation;
- rank persistence by ordinal position;
- number of adjacent-rank swaps;
- top-entity tenure;
- minimum and maximum realized share;
- share concentration over time.

Tests should assert ranges appropriate to the trend rather than require arbitrary
annual winner changes. Desired properties:

- some movement exists on category, brand, color, country, and gender;
- stable trends retain more rank persistence than volatile trends;
- similarly sized entities swap more often than structurally distant entities;
- no group is starved;
- the hierarchy is persistent but not frozen.

## Rollout sequence

### Phase 1: measurements and pure engine

Completed:

1. Added reusable rank-persistence diagnostics in
   `temporal_mix_diagnostics.py`.
2. Added the pure `temporal_mix.py` engine and its unit/analytical tests.
3. Kept the engine disconnected from production sampling.
4. Confirmed the existing worker-count, chunk-size, and golden-snapshot tests
   remain unchanged.

### Phase 2: customer-side integration

Completed:

1. Loaded stable country and gender mappings without adding configuration fields.
2. Built compact relative-month factor matrices once in the coordinator and
   broadcast them through shared memory.
3. Applied the combined, eligible-mass-centered factor to optional monthly-pool
   selection and repeat-order weights; forced debut behavior remains unchanged.
4. Extended the customer SCD2 grid with versioned `GeographyKey`, resolving the
   temporal country at calendar-month start while retaining exact-day
   `CustomerKey` resolution for emitted rows. Gender changes across SCD2 history
   now fail validation.
5. Added dense sparse-key mapping and validation across the worker boundary.
6. Passed worker-count, chunk-size, SCD2, focused integration, lint, and broad
   regression tests. The old golden hashes differ only because this phase is
   designed to alter customer allocation; baseline regeneration remains Phase 5.

### Phase 3: product-side integration

Completed:

1. Loaded pool-aligned category, brand, and color mappings and added validation
   that product taxonomy is invariant across SCD2 versions.
2. Built compact coordinator-side category, category-qualified brand, and
   category-qualified color factor matrices from relative month, the resolved
   trend preset, and the run seed. Brand and color residuals are mass-centered
   within their parent category.
3. Broadcast the mappings and matrices through shared memory and multiplied them
   into the existing popularity and seasonality product weights without changing
   monthly row, order, or distinct-customer plans.
4. Corrected the product-weight and per-store product-CDF cache keys to use
   relative `m_offset`, so the same calendar month in different years can have a
   different mix.
5. Removed the rotating-brand block from the shipped `models.yaml` and disabled
   that legacy path in production. Old configs still parse for compatibility and
   produce a deprecation warning instead of layering two competing mechanisms.
6. Added focused determinism, hierarchy-centering, cache-key, and SCD2 tests.
   Worker-count, chunk-size, snapshot invariance, schema/config compatibility,
   and the full non-golden suite pass: 2692 passed, 2 skipped, 2 deliberately
   deselected. Golden regeneration remains Phase 5.

### Phase 4: calibration and QA

Completed:

1. Added `tests/test_temporal_mix_calibration.py`, covering stable, growth,
   decline, cyclical, and volatile 10-year analytical runs for category, brand,
   color, country, and gender. The tests compare position persistence, adjacent
   Spearman correlation, swaps, tenure, shares, and concentration.
2. Added `scripts/calibrate_temporal_mix.py`, a read-only developer QA tool that
   measures a packaged parquet dataset and projects all five representative trend
   families over the same entity masses and calendar window.
3. Calibrated against the reported `2026-08-18 01_20_39 PM Customers 43K Sales
   2M PARQUET` dataset. Its observed category and color position persistence was
   `1.000`. Under the volatile QA projection, category moved to `0.740` with 13
   swaps, color to `0.700` with 10 swaps, and country from `0.840` to `0.660`
   with 22 swaps. Category concentration remained `0.1187`; movement comes from
   reshuffling shares rather than replacing the macro demand shape.
4. Consolidated the code-owned layer strengths in
   `TEMPORAL_MIX_LAYER_INTENSITY`. Calibration increased category from `0.85` to
   `1.10`, color from `0.45` to `0.60`, and country from `0.80` to `0.95`.
   Brand remains `0.60` because it already showed strong movement. Gender remains
   `0.55` because higher values widened shares without improving rank turnover.
   These are internal constants, not configuration fields or user-facing knobs.
5. Added a 240-month production-plan stress test with 250,000 customers and
   100,000 products. The compact mappings and group matrices remain below one
   twentieth of the corresponding entity-by-month matrices and complete well
   inside the 15-second regression ceiling.
6. Re-ran worker-count, chunk-size, snapshot-invariance, worker-state, and focused
   temporal calibration tests. The full non-golden suite passes with 2725 tests,
   2 environment-dependent skips, and 2 deliberately deselected golden tests.
   The intentional absolute fingerprint update remains isolated to Phase 5.

### Phase 5: intentional baseline update

Completed:

1. Ran the old absolute fingerprint before regeneration. Exactly the expected 15
   existing cells changed across default, CSV returns, Delta, SCD2, and
   skip-order-column fixtures; no cells were missing or added.
2. Confirmed the changed surface is explained by the new customer and product
   assignment weights, while schemas, fixture structure, and global allocation
   plans remain unchanged.
3. Regenerated `tests/_snapshots/sales_fact_fingerprints.json` deliberately. The
   diff contains 15 hash replacements and no key changes; within every fixture,
   the one-worker and four-worker small-chunk fingerprints remain identical.
4. Re-ran the absolute snapshot suite (6 passed), the explicit worker-count and
   chunk-size determinism suite (7 passed), and the complete repository suite
   (2727 passed, 2 environment-dependent skips).
5. Documented the behavior and compatibility impact in `docs/RELEASE_NOTES.md`.
   The baseline was updated only after the mismatch scope was independently
   observed and explained.
6. Generated a fresh five-year validation package with 42,851 customers and
   2,003,295 sales rows. Compared with the original reported run, observed
   top-rank position persistence moved from `1.000` to `0.880` for category,
   `1.000` to `0.900` for color, approximately `0.740` to `0.700` for brand, and
   `0.840` to `0.760` for country. Gender's leading-entity tenure was three of
   five years. The run completed with the normal five-year allocation and all
   downstream facts enabled.

## Acceptance criteria

Implementation is complete only when all of the following hold:

- no new end-user temporal-mix knobs exist;
- the existing trend preset is the sole user-facing controller;
- arbitrary date ranges work without hard-coded years;
- category, brand, color, country, and gender exhibit calibrated temporal movement;
- global monthly rows, orders, and distinct targets remain exact;
- output schemas and relationships are unchanged;
- customer and product SCD2 tests pass;
- worker-count and chunk-size determinism pass with the temporal model active;
- memory use follows the bounded plan described above;
- existing structural dominance remains visible;
- the old brand winner does not double-count temporal brand demand;
- golden snapshot changes are reviewed and intentional.

## Primary risks and mitigations

| Risk | Mitigation |
|---|---|
| Multiplicative factors create extreme demand | Log-additive hierarchy, variance budgets, centering, and bounded factors |
| Rank changes are still invisible in revenue | Calibrate using baseline revenue mass, not only entity counts |
| Too much motion makes data look random | Persistent mean reversion and trend-specific analytical quality ranges |
| Worker output diverges | Hash-seeded immutable factors and complete `m_offset` cache keys |
| SCD2 uses future geography | Carry versioned geography and resolve at defined monthly grain |
| Brand is weighted twice | Replace/absorb the rotating-winner path instead of stacking it |
| Large runs consume excessive memory | Store group factors plus entity mappings, never entity-by-month matrices |
| Existing configs break | Accept legacy brand configuration during a documented deprecation period |

## Summary

The implementation adds a unified temporal mix layer, not separate country, brand,
category, color, or gender hacks. It derives persistent group-level movement
automatically from the existing trend and seed, applies that movement at the
customer and product sampling boundaries, and preserves every global allocation and
determinism contract around those boundaries.
