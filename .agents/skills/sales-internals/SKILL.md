---
name: sales-internals
description: >-
  Read before editing src/facts/sales/. Covers worker invariance, chunk-shape guarantees, temporal mix, counter sampling, State/shared-memory boundaries, numeric safety, and golden fingerprint updates. Use for sales determinism failures and correlated-fact changes.
---

# Sales-Fact Internals: the Determinism Contract

Preserve the guarantees actually tested by the sales suite:

1. **Worker invariance:** at fixed config, seed, and chunk size, changing worker
   count must preserve the canonical key-sorted output fingerprint. Physical
   parquet bytes or file ordering are not the fingerprint contract.
2. **Chunk-shape invariance:** changing chunk size preserves requested row count,
   per-month row totals, and per-month distinct-customer coverage. The tests do
   not promise an identical full row multiset across chunk sizes. Order labels
   are chunk-sharded, and some line draws consume chunk-local RNG streams.
3. **Golden fingerprints:** behavior-preserving changes must match the stored
   baseline for each fixture/worker/chunk-size cell. Intentional output changes
   need an explained baseline update, not weakened assertions.

These are enforced by **design**, not runtime locks:

- **State is per-worker, bound once, then read-only by convention.** `State` in
  `sales_logic/globals.py` is populated once per worker by `bind_globals()` and never
  reassigned during chunk processing. There is **no runtime immutability enforcement**
  (the old `_SealableMeta`/`seal()` machinery was removed and deliberately not
  replaced). **Never reintroduce cross-chunk mutable state on `State`** (e.g. a lazy
  cache). Worker-lifetime scratch lives as **module-level globals reset in
  `init_sales_worker`**, not on `State`. In tests, call `State.reset()` between cases.
- **The schedule is static and computed in the coordinator, broadcast read-only.**
  Discovery, the per-month plan, and customer pools are computed once in the main
  process and shared, so every worker sees the same inputs.

## Rules you must follow when changing the hot path

These come straight from the gotchas the refactor hard-won. Regression guards named
in each.

- **Worker-lifetime caches must be keyed by EVERY variable their value depends on.**
  Workers are reused across chunks and the month loop can skip months, so a cache whose
  key omits a dimension becomes first-writer-wins → chunk/worker-order dependent. This
  bit `_sample_brand_aware`'s product-CDF cache: keyed by *calendar* month but the brand
  mix is an *absolute*-month quantity, so it now keys on `m_offset`. Guard:
  `tests/test_sales_determinism_workers.py` (seed 20250701).
- **The per-month plan is global; chunks only shard the order-id index space.** The
  coordinator computes `State.sales_rows_per_month` (R[m]), `sales_orders_per_month`
  (O[m]), and `sales_distinct_target` (D[m]) ONCE against global month totals and
  broadcasts them. Each chunk slices a contiguous band via
  `_chunk_month_band(R, O, chunk_idx, total_chunks)`; orders floor-tile `[0, O[m])`
  with no gaps and `sum n_lines == R[m]` exactly. **Do not reintroduce a per-chunk
  `build_rows_per_month` or per-chunk distinct target** — that is exactly the
  `chunk_size` dependence this removed. Customer sampling uses the plan/month RNG
  (`State.sales_plan_seed`), **not** the per-chunk `rng`. Guard:
  `tests/test_sales_determinism_chunksize.py`.
- **Correlated-fact features may change per-line measures or product identity, never
  row counts or customer assignment.** New randomness is either drawn off the per-chunk
  `rng` (fine — chunks own fixed bands seeded by `chunk_idx`) OR, when a value is
  recomputed in a different pass, **hash-seeded on a globally-unique emitted key**
  (e.g. SplitMix64 of `(OrderNumber, OrderLineNumber)`) so it's identical regardless of
  chunk/worker. The following features have zero/off compatibility paths; check their tests
  rather than assuming every new model has a legacy toggle: elasticity (`models.quantity.elasticity`),
  promo salience (`models.promotions`), basket theme (`models.basket`), fulfillment
  friction (`models.fulfillment`), markdown reconcile
  (`models.pricing.markdown.reconcile_promotions`). The friction hash is recomputed
  identically in the separate returns pass — distinct salts keep basket and friction
  independent. Guard: `tests/test_sales_phase3_fidelity.py`.
- **Deterministic posted price:** `UnitPrice`/`UnitCost` are deterministic per
  `(product, month)` — the stochastic snap is hash-seeded on `(ProductID, month)`
  (`_price_hash_u01`), so all lines for a product-month share one list price. Only
  `DiscountAmount`/`NetPrice` vary per line. Gated by
  `models.pricing.appearance.deterministic` (default true); the OFF path preserves the
  legacy per-row snap byte-for-byte. Guards:
  `tests/test_pricing_pipeline.py::TestDeterministicPostedPrice` and
  `tests/test_sales_phase3_fidelity.py`.

## Temporal mix and counter-based sampling

`prep/temporal_mix_plan.py` builds compact customer country/gender and product
category/brand/color plans from the existing macro trend. The pure factor engine
is `sales_models/temporal_mix.py`; calibration constants are code-owned, not new
YAML knobs. Preserve factor stability across group ordering, worker scheduling,
and an extended end month; factor draws use relative month offsets and stable
hash/counter inputs. Never use Python's process-randomized `hash()` for seeds.

Customer, product, and channel/time sampling have dedicated counter guards:
`test_customer_temporal_mix.py`, `test_product_temporal_mix.py`,
`test_temporal_mix.py`, `test_product_sampling_counter.py`, and
`test_channel_time_counter.py`. Use those alongside the worker/chunk/snapshot
checks when changing sampling. Do not replace counter draws with chunk RNG draws
merely because a worker-invariance test still passes.

The worker boundary is typed: coordinator holders in `prep/run_config.py`,
`SalesWorkerCfg` in `prep/worker_cfg_schema.py`, then a dict on the wire and
`State` binding in each worker. New worker inputs must traverse the builder,
schema/validation, shared-memory setup where applicable, and State binding.
`test_worker_cfg_validation.py` and `test_sales_contract.py` cover those seams.

## Numeric tripwires (silent corruption)

These produce *wrong data*, not crashes — no test will save you unless you know them:

- **int32 overflow in ID arithmetic:** Order-ID math (add, multiply) must use `int64`
  intermediates before casting back to `int32`. Silent wraparound at 2^31 produces
  negative/duplicate IDs.
- **`np.bincount(..., weights=...)` requires `float64` weights.** Passing `int8`
  silently overflows at >127 elements → wrong counts.
- **CDF + `searchsorted` boundary:** after `cdf = np.cumsum(w) / total`, always clamp
  `cdf[-1] = 1.0`. FP rounding can leave the last element below 1.0 → out-of-bounds
  `searchsorted` indices.

## Adjacent: shared-memory broadcast

Dimensions are loaded into numpy shared memory for zero-copy worker access
(`src/utils/shared_arrays.py`). **If you add a new dimension column the workers need,
update `SharedArrayGroup` initialization** — otherwise workers won't see it.

## When your change is SUPPOSED to alter output

Some changes intentionally move the bytes (a tuning change, a new default). Then the
golden fingerprint test `tests/test_sales_snapshot.py::test_golden_fingerprint` will
fail across the affected cells — that's correct. To regenerate the baseline:

```powershell
$env:REGEN_SALES_SNAPSHOT = "1"
try { .venv/Scripts/python.exe -m pytest tests/test_sales_snapshot.py -q }
finally { Remove-Item Env:REGEN_SALES_SNAPSHOT }
```

POSIX equivalent: `REGEN_SALES_SNAPSHOT=1 .venv/bin/python -m pytest tests/test_sales_snapshot.py -q`.
If the variable was already set by the caller, restore its previous value instead
of removing it.

Before you regenerate, **confirm the change was intentional**: check that the set of
failing cells matches the surface you changed, and that the cause traces to a
deliberate edit. Regenerating a stale baseline over an *accidental* regression launders
the bug into the baseline. Commit the regenerated
`tests/_snapshots/sales_fact_fingerprints.json` with the diff explained.

The determinism tests (`test_sales_determinism_workers.py`,
`test_sales_determinism_chunksize.py`) are only *relative* (they compare sibling runs);
the snapshot is the *absolute* anchor. Both must pass.

## Code map (where things live)

```
src/facts/sales/
  sales.py                     # entry point, orchestrates the worker pool
  prep/                        # coordinator-side setup (main process, pre-pool)
    correlation_lookups.py     # _prebuild_shared_structures (SHM broadcast)
    scd2_grid.py, memory_model.py, worker_cfg_builder.py, coverage_preflight.py
  sales_logic/
    globals.py                 # State class + bind_globals()
    chunk_builder.py           # per-chunk row assembly (CDF sampling, ID math)
    core/                      # customer_sampling, orders, promotions, allocation,
                               #   pricing, delivery, basket
  sales_models/                # quantity model (Poisson), pricing pipeline
  sales_worker/                # pool, task defs, init_sales_worker
  sales_writer/                # parquet merge / delta / csv writers
```

## Focused validation

Use the repo venv (install test tools with `uv sync --locked --extra dev`).
For sales behavior changes, run the relevant units plus worker invariance,
chunk-shape invariance, and golden fingerprints:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_sales_determinism_workers.py tests/test_sales_determinism_chunksize.py tests/test_sales_snapshot.py
```

See `tests/README.md` for schema, temporal, and output-writer test groups. Shared
small-pipeline fixtures live in `tests/sales_gen.py`; reuse them instead of
running the developer's full dataset or overwriting its scratch/version files.
