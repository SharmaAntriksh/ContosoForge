---
name: sales-internals
description: >-
  The determinism, parallelism, and numeric-safety contract for the sales fact
  generator (src/facts/sales/). Load this BEFORE editing anything under
  src/facts/sales/ — chunk building, the worker pool, customer sampling, pricing,
  promotions, allocation, delivery, or the State object. It explains why the sales
  fact must be byte-identical regardless of --workers and --chunk-size, the rules
  for worker-lifetime caches and hash-seeded draws, the ID-arithmetic and numpy
  pitfalls that silently corrupt output, and the golden-snapshot test workflow.
  Use it whenever you touch sales generation, see a determinism/snapshot test fail,
  add a correlated-fact feature, or need to regenerate the sales fingerprint baseline.
---

# Sales-Fact Internals: the Determinism Contract

The sales fact is the crown jewel of ContosoForge's "deterministic & idempotent"
promise. Its output must be **byte-identical** no matter how many workers run or
how the rows are chunked. `chunk_size` and `--workers` are **pure performance
knobs** — they must never change the shape *or the bytes* of the data. Everything
below exists to protect that invariant. Violate it and you break the product's
core guarantee, usually silently.

If you are editing under `src/facts/sales/`, read this first.

## The three invariants and why they hold

1. **Worker-invariance** — `--workers 1` and `--workers 4` at the same chunk size
   produce identical bytes. `imap_unordered` scheduling must not leak into output.
2. **Chunk-shape invariance** — the *row multiset* and the per-month distinct-customer
   set are identical across chunk sizes. (Surrogate `OrderNumber` labels legitimately
   differ across chunk sizes, since IDs are sharded by chunk index — chunk size changes
   the *labels*, not the *shape*.)
3. **Absolute byte-identity** — a behavior-preserving change reproduces the recorded
   golden fingerprint exactly.

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
  chunk/worker. Every `models.*` feature is default-on with a zero/off path that
  reproduces the pre-feature bytes. Applies to: elasticity (`models.quantity.elasticity`),
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

```bash
REGEN_SALES_SNAPSHOT=1 python -m pytest tests/test_sales_snapshot.py -q
```

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
