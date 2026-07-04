# Sales Fact Architecture Refactor Plan

Refactor of `src/facts/sales/` (~14.5k lines: coordinator + prep, sales_logic,
sales_worker + sales_models, sales_writer + output assembly). Driven by the
2026-07 architecture review — the fourth in the series after employees,
customers, and dates.

**Context:** this is a different animal from the previous three. The hard
problems are genuinely *solved* — chunk/worker-invariant determinism, the
static per-month plan, hash-seeded latents, SHM broadcast, bounded-memory
merges, single-commit delta. Nothing here is broken in the "produces wrong
data" sense (the 48a86cb overhaul fixed that). What's wrong is that the
solution is carried by the wrong structure: three god functions
(`generate_sales_fact` ~650 lines, `init_sales_worker` ~680 lines,
`build_chunk_table` ~875 lines) communicating through a stringly relay
(loader dicts → a 41-parameter builder → a ~126-key TypedDict mutated
post-construction by three modules → an unchecked `setattr` loop onto a
78-attribute untyped `State` with ~12 phantom attributes → `getattr(..., None)`
silent fallbacks). Correctness lives in CLAUDE.md gotcha paragraphs, "must
match exactly" comments, and regression tests — not in types or dataflow.
The measure of the debt is that safe operation requires six dense CLAUDE.md
gotchas, one of which (#28's `_sample_customers` reference) has already
drifted stale.

**Layer ratings from the review:** coordinator/prep **5/10**, sales_logic
**4/10**, sales_worker/models **4/10**, sales_writer/output **4.5/10**.
**Overall: 4.5/10** — execution within the design is 8/10 (vectorized,
tested, exhaustively commented); the architecture is 3/10 (accretion shell,
no unifying abstractions, contract-by-convention).

Same discipline as the other plans: behavior-preserving phases proven by
byte-identity, behavior-changing phases behind explicit gates with golden
regeneration. Never mixed.

---

## Ground rules

1. **Byte-identity gate.** Phases marked *(identical)* must produce
   byte-identical sales/returns/header parquet across the Phase 0 fixture
   matrix, at ≥2 worker counts and ≥2 chunk sizes. The existing
   `test_sales_determinism_workers.py` / `test_sales_determinism_chunksize.py`
   invariance tests are the floor, not the ceiling — Phase 0 adds golden
   checksums so *any* byte drift is caught, not just plan-shape drift.
2. **One concern per commit.** Mid-phase discoveries go to the Standing TODO.
3. **No phase labels in code comments or commit messages.**
4. **The output contract is frozen** through Phase 5. Column adds/renames,
   RNG-stream retirements, and schema-variant collapses are parked in Phase 6
   behind explicit decision gates.
5. **The determinism design constraints are inviolable in every phase:**
   static schedule computed in the coordinator and broadcast read-only;
   bind-once worker state; hash-seeded cross-pass latents; `chunk_size` and
   `--workers` as pure performance knobs. Refactors restructure *how* these
   are expressed, never *whether* they hold.

### Consumers (blast radius)

| Consumer | What it uses | Coupling |
|---|---|---|
| `src/engine/runners/sales_runner.py` | `generate_sales_fact(...)` kwargs + manifest (`order_id_int64` only) | public entry signature |
| `src/engine/packaging/*` | scratch layout **by path-probing**, not the manifest | Phase 5 replaces probing with the manifest |
| Streamed facts (budget/inventory/wishlists/complaints) | accumulator wiring in `worker_cfg`, micro-agg hooks in `task.py`, lookups on `State` | Phase 2/4 must keep their contract |
| Returns pass | detail-table columns + recomputed `line_friction` latent | Phase 4 hands latents forward explicitly |
| SQL DDL / BULK INSERT / PBIP generators | `static_schemas.py` (single source — genuinely good) | unchanged |
| Tests (~12 sales test files) | direct imports of `_`-private helpers, `State.reset()` | signatures churn in Phases 3–4 |

---

## Phase 0 — Guardrails (no production code changes)

- **Golden checksums** over a fixture matrix: (a) default config, parquet;
  (b) returns + header mode (`sales_output: both`), CSV; (c) delta with
  partitioning; (d) SCD2 products+customers on; (e) `skip_order_cols=true`
  (returns/complaints auto-disabled path). Each at `workers ∈ {1, 4}` ×
  `chunk_size ∈ {50k, 200k}`. SHA-256 of sorted-normalized frames per table.
- **Contract-pinning tests** for the seams the refactor will move:
  - every key consumed via `worker_cfg[...]`/`.get` in `init_sales_worker` is
    produced by `_build_worker_cfg` (+ its three post-construction mutators) —
    mechanically diff the two key sets;
  - every `getattr(State, name, ...)` read anywhere in `sales_logic/` /
    `sales_worker/` names an attribute that `bind_globals` actually bound for
    the default config — this catches the ~12 phantom attributes and pins
    them before Phase 2 formalizes them;
  - the coordinator's month-offset origin and `avg_lines` equal
    `chunk_builder`'s (turn gotcha #28's "must match exactly" prose into an
    executable assert).
- **Bug-pinning tests** (strict xfail, fixing phase in reason) for the Phase 1
  defects below — each reproduces the failure before any fix lands.
- **Docs hygiene:** delete the stale `_sample_customers` sentence from
  CLAUDE.md gotcha #28 (the symbol no longer exists anywhere).

---

## Phase 1 — Latent defects *(behavior-visible fixes, individually gated)*

Found during the review; none require the big refactor, all should land first
so later byte-identity phases aren't rebased over bug fixes.

1. **ReturnEventKey silent collision.** `task.py:657` offsets by
   `idx * returns_event_key_capacity` (default 100k) and
   `returns_builder.py:498` assigns `offset + arange(1..total_events)` with
   **no guard** that `total_events <= capacity`. A chunk with more return
   events than capacity silently collides with the next chunk's key range.
   Fix: hard assert with a `SalesError` naming the config knob (cheap), or
   derive capacity from `chunk_size × max_splits` (better).
2. **CSV merge data-loss window.** `_merge_fact_csv_chunks`
   (`output_assembler.py:171–265`) deletes source chunks *before* the
   tmp→final move and ignores its own `delete_chunks` parameter; a failed
   move loses the data. Fix: honor the parameter; delete only after
   `os.replace` succeeds; remove the tempdir on exception.
3. **Parquet merge writes directly to the final path.**
   `_merge_parquet_files_common` (`parquet_merge.py:349–376`) leaves a
   truncated-but-plausible `sales.parquet` on mid-merge crash, which the
   path-probing packager will happily ship. Fix: write to `*.tmp` +
   `os.replace` (the pattern `optimize_parquet` already uses).
4. **`str(None)` path.** `build_output_paths_from_sales_cfg`
   (`output_paths.py:218/231`) turns a `None` `merged_file` into a file
   literally named `"None"`. One-line guard.
5. **`_MONTH_DEMAND` cache never reset in production.** `orders.py:35` caches
   a function of `State.models_cfg`; `_reset_month_demand` exists but has no
   production caller, so an in-process rebind serves the previous run's
   lines-per-order curve. Fix: chain it into `init_sales_worker`'s reset
   block (and into `State.reset()` for tests).
6. **`optimize_parquet` encoding drift.** `parquet_merge.py:599–603` computes
   its own dict-column list ignoring `DICT_EXCLUDE`, so the "optimized" file
   dictionary-encodes exactly the columns the merge deliberately excluded.
   Fix: reuse the merge's policy.
7. **dict-config attribute misses.** `sales.py:140–141` and `206–208` call
   `getattr(...)` on values that may be plain dicts (guarded by
   `isinstance(x, Mapping)` *for the other branch*), so partitioning and
   `merge_parquet` config are silently ignored for dict-shaped cfgs. Fix:
   route through `_cfg_get` consistently (Phase 2 deletes the ambiguity
   wholesale).
8. **EmployeeKey=-1 row-drop breaks the plan invariant.**
   `chunk_builder.py:2099–2108` filters all columns by a
   shape-matching heuristic *after* assembly, quietly invalidating
   `sum n_lines == R[m]`. Near-term fix: assert the drop count is zero when
   the coverage preflight passed (it should be), log loudly otherwise;
   real fix in Phase 4 (resolve coverage before materialization).

Each ships with its Phase 0 xfail flipped to a passing regression test.
Items 2/3/4/6 change failure-path or auxiliary-file behavior only; items
1/5/7/8 can change output bytes for configs that hit them — golden checksums
regenerate per item with the diff explained in the commit.

---

## Phase 2 — The typed config spine *(identical)*

Kill the stringly relay end-to-end. This is the highest-leverage phase: it is
pure re-plumbing (same values, same order of computation, same bytes) and
every later phase gets cheaper because of it.

- **`SalesRunConfig`** (frozen dataclass) — one
  `resolve_run_config(cfg, models_cfg, **cli) → SalesRunConfig` pass owns ALL
  precedence logic with explicit `None` sentinels. This deletes
  `_apply_cfg_default`'s value-equality trap (an explicit
  `chunk_size=2_000_000` is currently indistinguishable from an omitted arg)
  and the triple-paradigm `getattr`/`hasattr`/`_cfg_get` zoo. Nothing below
  the resolve line touches `cfg` again. Sub-objects: `OutputSpec`,
  `ChunkingSpec`, **`ReturnsPolicy`** (the existing `ReturnsConfig` dataclass,
  constructed ONCE here instead of being exploded into 16 scalars → 16 State
  attrs → reassembled per task), `OrderIdSpace` (stride math, per-chunk
  alloc, int32→int64 promotion, the 8.0 safety factor, and
  `band(chunk_idx, month)` as a method — one tested class instead of inline
  coordinator math mirrored in `chunk_builder`).
- **Typed pools** — `CustomerPool`, `ProductPool`, `StorePool`, `PromoPool`,
  `EmployeeBridge`, `Calendar` frozen dataclasses of ndarrays. Each loader
  splits into `read_x(path) → raw` (IO) and `build_x_pool(raw, cfg) → Pool`
  (pure, unit-testable). The date pool moves out of `_load_products` (it has
  nothing to do with products); `_compute_promo_salience` moves out of
  `dimension_loaders` (it's a model, placed there only for import topology).
- **Explicit `MonthPlan`** — `rows[T]`, `orders[T]`, `distinct[T]`,
  `discovery[N]`, `plan_seed`, `T`, `month_origin`, `avg_lines` as ONE object
  produced by one pure function. Today the plan — the determinism crown
  jewel — travels as four loose keys injected into the *customer loader's*
  dict (`sales.py:618/692–694`), where a missed injection silently yields
  `None`.
- **`WorkerConfig` dataclass + wire codec** — replaces the ~126-key
  `SalesWorkerCfg`. `to_wire(shm)` substitutes SHM descriptors per a
  declarative `SHARED_FIELDS` table colocated with the class (replacing the
  hand-maintained 42-name string list in `sales.py:737–774`);
  `from_wire(d)` resolves and **validates completeness** — a typo'd field
  fails at pool spawn with the field named, instead of surfacing as
  `State.x is None` deep in a chunk. Construct-once and frozen: the three
  post-construction mutators (assortment, accumulators, prebuilds) become
  constructor inputs. Kill the self-duplication: `customers` aliasing
  `customer_keys`, `order_id_stride_orders` aliasing `chunk_size`,
  `skip_order_cols_requested` twin, flat copies of `output_paths` values,
  the misnamed `month_stride` (it is a per-*day* stride), and the
  legacy-ignored `write_delta`.
- **`bind_globals` validates.** Until Phase 4 replaces `State` wholesale,
  the `setattr` loop gains a strict allowlist (the declared attributes) —
  unknown keys raise. The ~12 phantom attributes pinned in Phase 0 get real
  declarations. `build_worker_schemas_from_cfg`'s documented
  "replicate the extraction exactly" mirror dissolves: it takes the resolved
  `WorkerConfig`.
- **Defaults declared once.** The divergent per-layer defaults
  (`row_group_size` 2M vs 1M vs 1M; `returns_max_lag_days` declared at three
  sites) collapse onto the dataclass definitions; consumer-site
  `getattr(State, k, default)` re-defaulting is deleted.
- **Exception sweep rides along:** the ~40 bare `RuntimeError`s across
  `init.py`/`task.py`/`io.py`/`parquet_merge.py`/`delta.py` become
  `ConfigError` (pre-spawn validation) or `SalesError`/`PackagingError`
  (runtime), per the repo's own gotcha #9.

Gate: full byte-identity across the Phase 0 matrix. `generate_sales_fact`'s
public signature is preserved as a thin adapter over `resolve_run_config`.

---

## Phase 3 — One source of truth for shared arithmetic *(identical)*

Delete the comment-enforced duplication. All pure code motion.

- **`weights.py`** — one `normalize(w, on_zero: 'none'|'uniform')`, one
  `build_cdf(w)` (owning the `cdf[-1] = 1.0` clamp), one weighted-pick.
  Replaces seven clones with *divergent semantics* — `_normalize_prob` in
  `chunk_builder` returns `None` on zero-sum while the same-named function in
  `columns.py` returns uniform — plus five hand-inlined clamp sites and the
  two unrelated `_weighted_pick`s. Gotcha #16 becomes one function's
  docstring.
- **`EligibilityWindow`** — the predicate
  `active & start<=m & (end==-1 | m<=end)` exists in three implementations
  (`customer_sampling:96`, `chunk_builder:162`, `chunk_builder:675`) that must
  agree for the plan to be consistent. One value object, three views
  (mask / index / delta-cumsum counts), used by coordinator and worker.
- **`plan_math.py`** — month-offset origin, `avg_lines`, band slicing —
  imported by both coordinator and `chunk_builder`, so gotcha #28's "must
  match exactly" becomes "is the same code". Also reconciles the coordinator's
  *two* hardcoded avg-lines constants (1.8 at `sales.py:660`, 2.0 at
  `sales.py:573`) into one named value with the discrepancy resolved
  deliberately (behavior gate if the reconciliation changes bytes).
- **`roles.py`** — one `resolve_salesperson_roles`, shared by
  `coverage_preflight`, `dimension_loaders`, and worker init (currently a
  three-way hand-maintained mirror — drift here means the preflight validates
  a *different* condition than the workers enforce).
- **Prebuilds become mandatory.** `init_sales_worker`'s nine alternative
  construction paths (brand buckets ×3, assortment ×3, salesperson bridge ×2,
  brand-prob ×2, plus the always-recomputed store-eligibility staffing loop)
  collapse: the coordinator computes every derived structure exactly once
  (the codebase's own stated philosophy, gotchas #27/#28) and broadcasts;
  worker fallback branches are deleted; tests that exercised the fallbacks
  call the same builder functions in-process. Worker init shrinks to
  resolve-refs → validate → bind.

Gate: byte-identity, plus the Phase 0 contract-pinning tests now assert
single-source (grep-level check that the deleted clones are gone).

---

## Phase 4 — Decompose the god functions *(identical)*

Now cheap, because the objects exist.

- **`generate_sales_fact` → ~40-line driver:** resolve config → load pools →
  plan → preflight → build `WorkerConfig` → publish SHM → run pool →
  assemble. The 10 concerns currently interleaved in one 650-line scroll
  (returns parsing, ID-space design, RAM capping, plan computation, SHM
  publication, …) become named, independently testable functions. The
  bottom-of-file import and the "keep this import function-local" docstring
  lore go away by fixing the dependency direction: worker imports leaves
  only, never the coordinator.
- **Coordinator↔State decoupling:** `generate_sales_fact` currently *reads*
  `State.models_cfg` (hidden precondition on pipeline_runner having bound it)
  and *reassigns* it mid-function (`sales.py:604`) with correctness depending
  on statement order 604-before-709. The discovery auto-adjust becomes
  `adjust_customer_demand(models_cfg, pools, total_rows) → models_cfg'`
  flowing forward explicitly; `State` becomes worker-only, populated solely
  by `init_sales_worker`. Gotcha #3 shrinks from a page to a sentence.
- **`build_chunk_table` → stage pipeline:** the month-loop body becomes ~10
  pure stage functions `(ctx, batch) → batch` over an explicit `ChunkBatch`
  (columns + the order-grouping trio, currently recomputed at three sites):
  `slice_plan → assign_customers → expand_orders → assign_stores →
  assign_channels → sample_products → resolve_scd2 → price → deliver →
  promote → extras → to_arrow`. Stage order is frozen (RNG stream, see
  gate 5). The 13-positional-param walls (`_apply_new_customer_promo`,
  `_apply_geo_bias_store_sampling`, …) become methods over the batch/ctx.
  `skip_order_cols` becomes two thin adapters over one order-granular core
  instead of dual paths inside every helper.
- **Module-global scratch → one `WorkerCaches` object** created at init,
  semantic keys (`store_key`, `m_offset`) instead of `id()`-keyed entries,
  one lifecycle instead of six hand-registered reset functions across five
  modules (four of today's ten cache globals have *no* reset path). The
  gotcha-#27 cache-key bug class becomes structurally impossible to write.
- **Returns pass consumes `ChunkArrays` directly:** the chunk builder hands
  forward its numpy columns *and latents* (fulfillment friction), deleting
  the arrow→numpy re-materialization and the comment-enforced
  "recompute the identical friction" convention. `columns.py`'s per-chunk
  `channels.parquet` disk read (located by probing four guessed State
  attribute names) moves to coordinator prep like every other dimension;
  ChannelKey gets exactly one producer.
- **Coverage as a precondition, not a post-hoc filter:** with the preflight
  guaranteed, the EmployeeKey=-1 shape-heuristic row-drop is replaced by an
  assert (Phase 1 item 8 fixed properly).

Gate: byte-identity across the full matrix, both worker counts, both chunk
sizes, all five fixture configs. This is the phase where the golden
checksums earn their keep — the RNG stream must not move, so stage
boundaries are drawn exactly at existing draw sites.

---

## Phase 5 — Writer/output unification *(identical for success paths)*

- **`FormatWriter` protocol** — `write_chunk / finalize / package` with
  Parquet, Csv, Delta implementations. The format string is parsed once into
  a writer instance; the ≥8 independent `if format ==` ladders
  (`task.py:135`, `output_assembler.py:310/341/352`, `output_paths.py:105+`,
  `package_output.py:38+`, `shared/writers.py:64`, manifest ternaries ×2)
  collapse. `shared/writers.py`'s parallel format policy (its own delta
  import shim, its own CSV dialect that differs from sales' — two dialects
  feeding the same generated BULK INSERT scripts) is rebased onto the same
  core.
- **The manifest becomes the contract.** `SalesRunManifest` is currently
  computed and then discarded; `engine/packaging` re-discovers outputs by
  probing 4–9 candidate paths per table — hundreds of lines that can
  silently ship a stale file from a previous run. Packaging takes the
  manifest; the probing archaeology and the `_empty_manifest` duplicate are
  deleted; scratch layout becomes a private detail (unlocking the removal of
  the Sales-at-root layout special case that six modules branch on).
- **One schema authority.** Chunks are already normalized to the
  `WorkerSchemaBundle` at write time, yet parquet merge re-derives a schema
  by unioning N file footers and re-projects defensively (delta already
  receives `schema_by_table` — parquet just wasn't given the same
  treatment). Merge asserts chunks match the bundle; `projection.py` and
  `normalize_to_schema` collapse into one function; dict-encode policy lives
  on the bundle (deleting the third, drifted copy in `optimize_parquet`).
  The write-time OrderDate value-range sniffing (`io.py:239–270`) dies —
  the producer knows its dtype.
- **One `atomic_output()` helper** (tmp + `os.replace` + cleanup-on-error)
  used by all finalizers — generalizing the Phase 1 spot-fixes into the
  layer's single lifecycle pattern.
- **Typed `ChunkResult`** (`chunk_idx`, `outputs: dict[table, ChunkRef]`,
  `aggs: dict[str, Any]`) replacing the str-vs-Mapping-with-magic-popped-keys
  worker protocol and the filename-parsing `_chunk_tag`; the collector
  iterates registered accumulators instead of four copy-pasted if-blocks in
  two files.
- **Alias/dead-symbol sweep:** the underscore/public dual namespaces in
  `sales_writer` (including the `_project_table_to_schema` alias whose cast
  default *differs* from its public name — a semantic trap), the no-caller
  delta helpers, `__init__.py` re-exports of `_`-private names, and the
  `delete_chunks`-style dead parameters.

Gate: byte-identity of final artifacts (merged parquet, delta table content,
CSV chunk bytes) for success paths; failure-path behavior intentionally
changes (no more corrupt finals) and is covered by new crash-injection tests.

Decision gate parked here: whether `optimize_parquet`'s piecewise
"sort" (which does not globally sort and barely tightens row-group stats,
at the cost of a full decode/encode pass, default-on) is replaced by a k-way
streaming merge-sort or simply dropped. Measure, then decide.

---

## Phase 6 — Behavior-changing consolidations *(gated, golden regeneration)*

Each independently decided; each regenerates goldens with the diff
characterized.

1. **Retire the byte-compat legacy paths.** ~20–25% of
   `pricing_pipeline`'s branch structure, the legacy mod-100 delivery
   ladder, the `deterministic=false` per-row snap, the reconcile path's
   draw-n-use-subset RNG-position choreography — all exist to reproduce
   pre-overhaul bytes for opt-outs that production never uses. Delete the
   OFF paths, regenerate goldens once.
2. **Counter-based RNG everywhere.** Extend the hash-seeded regime
   (`_price_hash_u01`, `line_friction`, basket) to the remaining
   sequential-stream stages (dates, lines-per-order, stores, channels,
   products, salespeople, promos, quantity, markdown): every per-line draw
   becomes `u01(stage_salt, entity_key)`. After this, inserting or
   reordering a stage never perturbs another stage's values — gotchas #27
   and #29 reduce to "use the Rand API", and byte-determinism is a property
   of the design instead of draw-count bookkeeping. This is the single
   biggest payoff in the plan and the reason Phase 4 freezes stage order
   only "for now".
3. **Emit ChannelKey/TimeKey from the chunk builder** (per-order hash draws)
   instead of post-hoc injection in `task.py` — collapsing the GEN/OUT
   schema split, the `_INJECTED` drop-set, and the six sales-schema variants
   bound to State down to one schema per table per run.
4. **Derive `ReturnEventKey`** as `hash(OrderNumber, line, seq)` (or
   cumulative offsets), removing the capacity knob entirely.
5. **Uniform scratch layout** (`<scratch>/<format>/<table>/…` including
   Sales), now free because packaging reads the manifest.

---

## Standing TODO

- `State` hosts budget/inventory lookups (`budget_store_to_country`, …) —
  move to the streamed facts' own contexts when touched.
- `coverage_preflight.repair_bridge` writes back into a dimension parquet
  from the fact stage (another gotcha-#7-class cross-write) — fold into the
  dimension runner's cascade logic.
- Seasonal-spike defaults hardcoded inline (`sales.py:673–677`) → Pydantic
  schema defaults.
- `CHUNKS_PER_CALL = 2` buried at `sales.py:784` → `ChunkingSpec`.
- `_LazyStoreAssortment` vs eager matrix expansion — pick one after Phase 3
  makes prebuilds mandatory.
- Config-hash-versioned lazy caches in `pricing_pipeline`/`quantity_model`
  (SHA-256-over-JSON per miss-check, duplicated `_cfg_hash`) exist only
  because config identity is unknowable through the dict/Pydantic
  ambivalence — parse once into the worker context in Phase 4, delete the
  versioning machinery.
- CLAUDE.md gotchas #3, #16, #26–#29 shrink to one-liners as their
  underlying seams are closed; rewrite them at each phase boundary rather
  than at the end.

---

## Why this order

Phase 1 first so bug fixes never hide inside refactor diffs. Phase 2 before
everything structural because every later phase's cost is dominated by how
many call sites must be re-plumbed — typed objects make each subsequent move
a mechanical signature change instead of a key-string hunt. Phase 3 before 4
because decomposing `build_chunk_table` is only safe when the arithmetic it
shares with the coordinator has a single home. Phase 5 is independent of 2–4
(different seam) and can run in parallel on a separate branch if desired.
Phase 6 last because everything before it is provable by byte-identity, and
mixing provable and behavior-changing work is how refactors lose the trail.
