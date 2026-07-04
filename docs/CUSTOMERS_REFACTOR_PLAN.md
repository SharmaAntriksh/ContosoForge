# Customers Package Refactor Plan

Complete refactor of `src/dimensions/customers/` (generator, helpers, households,
org_profile, scd2, worker, subscriptions/). Driven by the 2026-07 review. The
package is the oldest and most-patched dimension: 1,742-line generator module, a
serial and a parallel path that re-implement every post-merge phase in two
dialects, four different seed-derivation idioms, and a set of `CUST-AN-*` patch
markers that record symptoms being fixed while the underlying diseases survived.

**Headline finding (empirically verified):** the parallel customer path derives
every chunk's seed as `int(SeedSequence(seed).spawn(n_chunks)[chunk_idx].entropy)`
(`worker.py:30`) — and `.entropy` returns the *parent's* entropy, so **every chunk
runs on the identical seed**. Equal-sized chunks generate byte-identical customer
populations: with 500K customers (10 × 50K chunks), the same 50K people — names,
DOBs, incomes, demographics — repeat 10 times; only global-key-suffixed fields
differ. Any dataset ≥ 200K customers (`CUSTOMER_PARALLEL_THRESHOLD`) on a ≥3-core
machine is affected. The CUST-AN-9 email-collision fix ("millions of customers
shared an address") patched the visible symptom of exactly this bug and left the
cause in place.

The plan is ordered like the employees plan: **behavior-preserving refactors
proven by byte-identity**, then **behavior-changing fixes each with a version
bump and a regression test**. Never mix the two in one phase.

---

## Ground rules

1. **Byte-identity gate.** Phases marked *(identical)* must produce byte-identical
   `customers.parquet`, `customer_profile.parquet`, `organization_profile.parquet`,
   `plans.parquet`, and `customer_subscriptions.parquet` for a fixed seed, on BOTH
   the serial and parallel fixtures pinned in Phase 0. Phases marked *(behavior)*
   bump `_schema_version` (customers) / `_schema_version` (subscriptions) and
   regenerate the snapshots once at phase end.
2. **One concern per commit.** Bugs discovered mid-phase go to the Standing TODO,
   not into the current diff.
3. **No phase labels in code comments or commit messages.** Phases exist only in
   this document. The existing `CUST-AN-*` / `CUST-SCD2-*` markers are grandfathered
   (they reference review findings, not phases) but new code gets plain rationale
   comments.
4. **Output contract is frozen except one gated addition.** Column names, order,
   and dtypes match `static_schemas.py` before and after every phase. The single
   deliberate schema change is `IsActiveInSales` (Phase 2e, decision-gated) —
   the sales loader already contains the consuming branch for it.
5. **Downstream re-verification.** Any byte change to customers.parquet changes
   the sales fact (CustomerKey drives everything). The sales guardrail suite
   (worker-count invariance, chunk-size invariance) is *invariance*-based, not
   golden-bytes — it must stay green after every behavior phase.

### Consumers (blast radius — retest after every phase)

| Consumer | What it reads | Coupling |
|---|---|---|
| `src/facts/sales/prep/dimension_loaders.py` | customers.parquet: keys, StartDate/EndDate/EndMonth, `CustomerBaseWeight`, GeographyKey, SCD2 IsCurrent, (`IsActiveInSales` branch, currently dead) | column set + SCD2 semantics |
| `src/engine/runners/sales_runner.py` | customers.parquet presence/paths | pipeline wiring |
| `src/dimensions/customers/subscriptions/runner.py` | customers.parquet (IsCurrent dedup, Start/End dates) | lifecycle windows |
| `src/engine/runners/dimensions_runner.py` | deps + `inject_global_dates` for customers and subscriptions | regen cascade |
| `src/engine/quality_report.py` | FK checks (GeographyKey, LoyaltyTierKey, acquisition channel) | key validity |
| Wishlists / complaints / budget facts | customer arrays via the sales prep bundle | indirect |
| SQL DDL + Power BI TMDL (generated) | column order/dtypes of all five outputs | output contract |
| `tests/test_customer_profiles.py`, `test_subscriptions.py`, `test_dimensions.py` | direct function calls | signatures |

---

## Phase 0 — Guardrails (no production code changes)

**Goal:** pin current behavior on both code paths and make every claimed bug
falsifiable.

- **Golden snapshots, two fixtures** (`tests/test_customers_snapshot.py`):
  - *Serial*: small N (< threshold), SCD2 on, churn on, orgs > 0, households on.
  - *Parallel*: same config forced through `_generate_parallel` (patch
    `CUSTOMER_PARALLEL_THRESHOLD` / pass workers explicitly — add a small test
    seam if needed rather than generating 200K rows in CI).
  SHA-256 over parquet-normalized frames for all five outputs.
- **Characterization tests** for behavior to keep:
  - SCD2: per `CustomerID`, versions numbered 1..k, exactly one `IsCurrent`,
    intervals contiguous (`EffectiveEndDate = next EffectiveStartDate − 1d`),
    `end ≥ start` on every row (the CUST-SCD2-1 guarantee);
  - households: exactly one Head per multi-member household, matched spouses are
    `Married`, dependents ≤ income cap, members share GeographyKey/address;
  - profiles: `CustomerProfile.CustomerKey` ⊆ current-version person keys;
    `OrganizationProfile.CustomerKey` ⊆ current-version org keys; profile row
    count == person count (the positional-alignment contract, asserted explicitly);
  - subscriptions: `SubscriptionKey` unique, billing periods ordered per
    subscription, exactly one `IsFirstPeriod`, `IsChurnPeriod` only on last;
  - email uniqueness across the full population (parallel fixture).
- **Bug-pinning tests** (`xfail(strict=True)`, fixing phase named in reason):
  1. two equal-size parallel chunks produce disjoint FirstName/DOB populations
     (currently identical — the chunk-seed bug);
  2. `default_rng(spawn[n+2])` email stream ≠ SCD2 selection stream (currently
     the same child);
  3. changing `sales.workers` (or the worker fallback) does not change
     customers.parquet bytes (currently flips serial↔parallel);
  4. a customer's LoyaltyTierKey is identical whether generated serially or in
     the parallel path / any chunk split (currently data-derived per-chunk
     quantile cuts);
  5. no subscription billing period starts after the customer's
     `CustomerEndDate` (currently non-churned subs bill to dataset end);
  6. changing `defaults.seed` alone regenerates customers (currently the version
     key omits the resolved seed).

**Acceptance:** suite green (or strict-xfail) on `main`, zero production diffs.

---

## Phase 1 — Truth restoration & dead weight *(identical)*

- Fix annotations: `run_customers(cfg: Dict)` / `generate_synthetic_customers(cfg: Dict)`
  → `AppConfig`; same sweep in subscriptions.
- Delete the unused `write_chunk_rows` field from `SubscriptionsCfg` and its
  `read_cfg` line (parsed, threaded, consumed by neither bridge writer; the
  config schema keeps accepting the key so user configs don't break).
- Delete `CANCELLATION_REASONS` (exported, consumed by nothing).
- Correct docstrings that oversell ("pure function" on RNG-consuming builders;
  "Vectorized bulk expansion" sections that contain per-customer Python loops —
  keep the loops, fix the prose).
- Document (do not yet change) the discarded `active_customer_set` return and
  the dead `IsActiveInSales` loader branch — both are Phase 2e.
- Leave the seven hardcoded `_has_*` version-key flags for Phase 7 (touching the
  key forces a pointless regen for every user; batch it with the other key changes).

**Acceptance:** both snapshots unchanged; full suite green.

---

## Phase 2 — Determinism & seed architecture *(behavior — customers `_schema_version` → 9)*

The centerpiece. Everything here changes bytes once, together, behind one bump.

- **2a. Fix the chunk-seed bug.** Chunk workers receive the spawned
  `SeedSequence` child (or an integer derived from `(entropy, spawn_key)`), never
  `.entropy`. Every chunk gets a distinct stream; chunk populations become
  disjoint. Pin with Phase 0 test #1. Note `scd2_chunk_worker` and the
  subscriptions worker already pass the child object correctly — only
  `customer_chunk_worker` is broken.
- **2b. Named substream registry.** Replace the ad-hoc zoo (`.spawn(n)[i]`,
  `.spawn(n)[i].entropy`, `seed + 9999`, re-spawning with different counts) with
  one table: `streams = SeedSequence(seed).spawn(K)` with named indices
  (`CHUNKS_BASE`, `HOUSEHOLDS`, `ORG_PROFILE`, `EMAIL_REBUILD`, `SCD2_SELECT`,
  `SCD2_CHUNKS_BASE`, …). This fixes the verified email/SCD2 collision (both
  currently `spawn(...)[n_chunks+2]`) and makes every subsystem's stream
  independent of insertions elsewhere — the property that makes Phase 3's
  decomposition provable.
- **2c. One canonical data path.** Delete the serial/parallel fork. The chunked
  plan (chunk count a pure function of N, per CUST-AN-7) becomes the only data
  definition; `workers` (and `cpu_count`) only size the pool that executes the
  chunks — 1 worker executes them in-process, sequentially, same bytes. This
  removes: the `sales.workers`-changes-dimension-data bug, the
  hardware-dependent path switch, and the entire duplicated post-merge block in
  `_generate_parallel` (household move-copy, dependent cap, title reconcile,
  org profile, SCD2 selection, GenderCode — each currently written twice in two
  dialects). `generate_synthetic_customers(_skip_post_phases=True)` becomes the
  chunk kernel; the post-merge phases exist exactly once.
- **2d. Fixed tier cuts.** `assign_tier_by_score` currently cuts on
  `np.quantile(score, …)` — data-derived, therefore chunk-membership-dependent
  (the same disease `_robust_unit_norm` was built to cure for spend buckets,
  CUST-AN-10). Replace with fixed calibrated score edges in `defaults.py`
  (same doctrine as `SPEND_BUCKET_EDGES`); keep the configured
  `probs_low_to_high` semantics by calibrating edges against the analytical
  score distribution once, offline, with the derivation recorded in a comment.
- **2e. Active-set coherence** *(decision-gated schema addition)*. Today the
  generator draws an active set, uses it only to bias warm-start months, returns
  it, and `run_customers` throws it away — while sales re-derives a completely
  different active set from `default_rng(seed + 7)`. "Early adopters" can be
  permanently inactive in sales. Fix: persist `IsActiveInSales` (bool) on
  customers.parquet; the generator's warm-start bias and the sales gate then
  share one set, and the loader's existing preferred branch
  (`dimension_loaders.py:145`) finally executes. Requires: `static_schemas.py`,
  SQL DDL, TMDL template updates. **Decision point for the user at phase start**
  — recommended yes (the consuming code was clearly written for it); fallback
  option is aligning sales' derivation with the generator's draw, which fixes
  coherence without the schema change but keeps the value invisible to BI.
- **2f. Version-key completeness.** Fold the resolved seed into the customers
  version key (the exact hole employees patched as its v11); add people-pool /
  org-name-file signatures (name file changes currently don't regenerate).

**Acceptance:** Phase 0 xfails #1 #2 #3 #4 #6 flip to pass; both snapshots
regenerate once (and are now *identical to each other* for the same config,
proving 2c); household/SCD2/subscription characterization tests still green;
sales invariance guardrails green against the new dimension.

---

## Phase 3 — Decompose the generator *(identical)*

**Goal:** break the 1,742-line module / ~630-line god function into the standard
package layout (`dates/`, `employees/` post-refactor: one module per subsystem,
thin orchestrator).

```
src/dimensions/customers/
  __init__.py        # same public API
  runner.py          # run_customers(): config, versioning, IO, pool sizing
  generator.py       # thin orchestrator (chunk kernel + phase sequence)
  seeds.py           # the Phase 2b substream registry
  identity.py        # region/org split, gender, names, org names, email build
  demographics.py    # _build_demographics + income/credit helpers
  lifecycle.py       # start/end months, churn sim, acquisition weights, weights/temp/bias
  engagement.py      # _build_engagement_profile (profile-only columns)
  geo.py             # region pools, country→city pools, lat/lon, postal codes
  post_merge.py      # households apply, dependent cap, title reconcile, GenderCode
  org_profile.py     # unchanged this phase
  households.py      # unchanged this phase
  scd2.py            # unchanged this phase
  worker.py          # chunk kernel entry (thin)
  subscriptions/     # unchanged this phase
```

- Submodules take `(rng_or_stream, arrays…)` and return dicts of arrays; no
  config access below the runner; frames assembled only in the orchestrator.
- Thanks to 2b, moving a subsystem cannot reshuffle another subsystem's draws —
  byte-identity is the proof the split preserved stream order *within* each.
- `helpers.py` dissolves into the new modules (it is currently a junk drawer of
  five unrelated concerns); `parse_cfg_dates` survives until Phase 7 kills it.

**Acceptance:** snapshots unchanged; no module > ~350 lines; import-cycle check;
`test_customer_profiles.py` untouched and green.

---

## Phase 4 — SCD2 engine rewrite *(behavior — `_schema_version` → 10)*

**Goal:** the life-event engine is a per-row Python state machine with dict-copy
churn and a hand-rolled list-preallocation scheme — slow enough that it needed
its own multiprocessing pathway (chunk tasks that pickle a 7K-entry geo cache
each). Products SCD2 is vectorized; customers should be too.

- Rewrite as a vectorized event grid: draw per-customer event counts and
  strictly-increasing event dates as arrays (preserving the CUST-SCD2-1
  spacing/uniqueness guarantee); resolve event *types* per step with masked
  vector ops over the state columns (the state space is small: marital ×
  ownership × children × tier); apply column updates by mask. Target: the SCD2
  multiprocessing path (`scd2_chunk_worker`, task pickling, scratch parquets)
  is **deleted** because serial vectorized is fast enough at 1M+ changed rows.
- **Single change-selection implementation**: `generate_scd2_versions` becomes
  the only place that selects changed customers; the duplicated inline block in
  the (now unified) orchestrator is deleted.
- Reconcile `Title` at SCD2 marriage/divorce events (today household assignment
  carefully flips Ms→Mrs but SCD2 events leave the salutation stale — one
  doctrine, both places).
- Keep event semantics (weights, income bumps, relocation behavior,
  region-pooled destination sampling) unchanged unless the vectorization forces
  a documented equivalent.

**Acceptance:** property tests (contiguity, numbering, IsCurrent uniqueness,
relocation stays in-region, income within [MIN, MAX]) across 20 seeds;
SCD2-at-scale timing recorded in the PR; snapshots regenerated.

---

## Phase 5 — Subscriptions correctness *(behavior — subscriptions `_schema_version` → 6)*

- **Billing periods respect the customer lifecycle.** Clamp every subscription's
  end reference to the customer's `CustomerEndDate` (currently only churn-coin
  subs stop; everyone else bills to dataset end even if the customer churned
  years earlier). Phase 0 xfail #5 flips.
- **Remove the fossil RNG draw** (`rng.choice(len(PAYMENT_METHODS), …)` with a
  discarded result, kept only for stream compatibility with a removed column)
  and the now-orphaned `PAYMENT_METHODS` / `_PAYMENT_WEIGHTS` constants.
- **`save_version` on both branches** — with `generate_bridge: false` the version
  is never saved, so plans.parquet regenerates every run.
- **Content-based upstream signature** — replace the mtime+size customers
  signature with row count + key min/max/sum (byte-identical regeneration of
  customers should not force a subscriptions rebuild).
- Adopt the shared date-resolution helper (pre-work for Phase 7 is fine here).

**Acceptance:** no billing period outside `[CustomerStartDate, min(CustomerEndDate,
g_end)]`; version-skip works with bridge disabled; snapshots regenerated.

---

## Phase 6 — Profile & org realism *(behavior — `_schema_version` → 11)*

- **Churn-aware engagement:** `LastWebVisitDate` drawn within the customer's own
  activity window (≤ `CustomerEndDate`), `MemberSinceDate` ≤ end date; NPS /
  RewardPoints / AvgOrderFrequency conditioned on active-vs-churned so a
  ChurnRisk table stops describing dead customers as browsing last week.
- **org_profile fixed-reference normalization:** `churn_norm_org = churn /
  churn.max()` is the data-derived anti-pattern (SatisfactionTier changes when
  unrelated customers are added); use `_robust_unit_norm` with
  `lognormal_p95_ref(bias_sigma)` exactly as the person path does.
- **org_profile constants → `defaults.py`** (ten module-local tables with local
  validation, contra gotcha #11).
- Household nits, fix-or-document: moved spouses keep the income drawn under
  their pre-move region multiplier; `YearsAsCustomer` measured to dataset end
  for churned orgs.

**Acceptance:** distribution tests (visit dates within lifecycle; org tier stable
under population growth in a two-run comparison); quality report clean;
snapshots regenerated.

---

## Phase 7 — Config, versioning & idiom hygiene *(identical data; version-key churn only)*

- **One date-resolution helper.** `customers/helpers.parse_cfg_dates` and
  `subscriptions/helpers.parse_global_dates` are the second and third
  implementations of what `config_helpers.parse_global_dates` (used by
  employees) already does. Route both through it; delete the copies.
- **Collapse the seven `_has_*` flags** into the schema version (one bump, noted
  in release notes as a forced one-time regen).
- **Idiom sweep:** `dict(cfg_section)` vs `as_dict` consistency; attribute access
  on Pydantic models; `str_or`-style coercions only at dict boundaries.
- **Grep-gate test:** no `.entropy`, no `seed + <int>` arithmetic, no
  `spawn(` outside `seeds.py` in the package (locks in the Phase 2b architecture).

**Acceptance:** generated data byte-identical to Phase 6 output (only version
JSON files differ); the grep-gate test green.

---

## Explicit non-goals

- **No column renames/removals.** The only schema change is the gated
  `IsActiveInSales` addition (Phase 2e).
- **No preservation of the legacy serial-path bytes.** Phase 2c deletes the fork;
  there is deliberately no compatibility mode that reproduces pre-refactor data
  (the pre-refactor parallel data is *wrong* — duplicated populations).
- **No subscriptions catalog changes** (plan names, prices, cycles stay).
- **No household↔SCD2 cross-coherence** (spouses relocating together, household
  dissolution on divorce). Real modeling work, not refactoring — documented as a
  known simplification.
- **No customer attrition-model changes** (churn hazard math stays as-is).

## Sequencing & effort

| Phase | Type | Size | Depends on |
|---|---|---|---|
| 0 Guardrails | tests only | M–L | — |
| 1 Truth & dead weight | identical | S | 0 |
| 2 Determinism & seeds | behavior | **XL** | 0, 1 |
| 3 Decomposition | identical | L | 2 |
| 4 SCD2 rewrite | behavior | L | 3 |
| 5 Subscriptions | behavior | M | 0 (independent of 2–4) |
| 6 Realism | behavior | M | 2 |
| 7 Hygiene | identical | S–M | 4, 5, 6 |

Phase 5 can run in parallel with 2–4 (different files, different version key).
**Phases 0–2 are the "stop the bleeding" milestone**: after them, large datasets
stop containing N/50K copies of the same 50K people, the dimension stops changing
with `sales.workers` and CPU count, and a global seed change actually regenerates
customers. Ship that even if 3–7 slip.

## Standing TODO (bugs found later go here, not into unrelated phases)

- (empty)
