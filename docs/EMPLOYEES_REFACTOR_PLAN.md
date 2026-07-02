# Store Cluster Refactor Plan (Stores + Warehouses + Employees)

Complete refactor of the coupled cluster:

- `src/dimensions/stores/` (generator, 1,365 lines)
- `src/dimensions/warehouses/` (generator, 442 lines)
- `src/dimensions/employees/` (generator, store-assignments bridge, transfer engine)

Originally scoped to employees only (2026-07 review); expanded to the full
cluster because the seams are shared: employees reads seven-plus columns off
stores.parquet (hierarchy strings, manager names, EmployeeCount, lifecycle
dates), warehouses *writes into* stores.parquet, the transfer/renovation engines
consume store lifecycle state, and the online/physical key boundary is spelled
four different ways across the three packages. Several employees-plan items that
were deferred "because the stores schema is frozen" are now properly fixable.

The plan is ordered so that **behavior-preserving refactors come first** (proven
by byte-identity), **behavior-changing fixes come after** (each with a version
bump and its own regression test). Never mix the two in one phase.

The one deliberate exception is **Phase 1.5**: a small set of surgical,
self-contained employee bug fixes pulled ahead of the decomposition so the core
defects ship even if the XL Phase 3 slips. It is still a clean behavior phase
(its own schema bump + regression tests) — just sequenced early, before the
`(identical)` Phase 2/3 whose byte-identity is then proven against the post-1.5
baseline.

---

## Ground rules

1. **Byte-identity gate.** Phases marked *(identical)* must produce byte-identical
   `stores.parquet` (both pre- and post-warehouse-enrichment states),
   `warehouses.parquet`, `employees.parquet`, and
   `employee_store_assignments.parquet` for a fixed seed, verified by the
   Phase 0 golden-snapshot harness. Phases marked *(behavior)* bump the relevant
   `schema_version` and update the snapshots once at phase end.
2. **One concern per commit.** Bugs found mid-phase go to the Standing TODO.
3. **No phase labels in code comments or commit messages.** Phases exist only in
   this document.
4. **The output contract is frozen** except the schema additions explicitly
   gated in Phase 4 (structured manager-name columns) and the parked decisions.
   Column names, order, and dtypes of all four parquet files match
   `static_schemas.py` before and after every phase (BULK INSERT is positional).
5. **The key encoding is frozen.** Physical stores 1..N, online `10_000+i`;
   employees: CEO=1, VP=2, region `10_000+rid`, district `20_000+did`, manager
   `30M+sk`, staff `40M + sk*1000 + idx`, online rep `50M+sk`;
   `ONLINE_WAREHOUSE_KEY` for the online FC. The refactor centralizes the
   encoding; it does not change it.

### Consumers (blast radius — retest after every phase)

| Consumer | What it reads | Coupling |
|---|---|---|
| `src/facts/sales/prep/dimension_loaders.py` + `worker_cfg_builder.py` | stores arrays (keys, geography, demand weight), bridge `RoleAtStore`/`FTE`/dates | column set + role filter |
| `src/facts/sales/prep/coverage_preflight.py` | stores lifecycle + bridge coverage per store/month | coverage semantics |
| `src/facts/inventory/*` (runner, engine, worker, accumulator, micro_agg) | `stores.WarehouseKey`, warehouses.parquet | **the cross-write consumer** |
| `src/facts/budget/lookups.py` | stores.parquet | column set |
| `src/engine/runners/dimensions_runner.py` | cascade: stores → warehouses (auto-forced) → employees → ESA | regen triggers |
| `scripts/verify_employee_store_sales.py` | employee key-band arithmetic | key encoding |
| `tests/`: test_dimensions, test_transfers, test_warehouses, test_store_demand_weight, test_salesperson_performance, test_coverage_preflight | direct function calls | signatures |
| SQL DDL + Power BI TMDL (generated) | column order/dtypes, all four tables | output contract |

---

## Phase 0 — Guardrails (no production code changes)

**Goal:** make every later phase falsifiable before touching anything.

- **Golden snapshot harness** (`tests/test_store_cluster_snapshot.py`): one fixture
  (≈30 physical + 2 online stores, 3-year window, seed 42, transfers on, at least
  one closing store, one renovating store, one renovation-closed store) run
  through the full chain `stores → warehouses → employees → ESA`. SHA-256 of
  parquet-normalized frames for all four outputs, capturing stores.parquet
  **after** the warehouse enrichment (the state consumers actually see).
- **Characterization tests** for behavior to keep:
  - staffing contract: per physical store, employee roster size ==
    `EmployeeCount` (1 manager + N−1 staff); online stores exactly 1 rep;
  - every store has a `WarehouseKey` after enrichment; warehouse names unique;
    every warehouse's `GeographyKey` is FK-valid;
  - one row per employee in the initial bridge; exactly one `IsPrimary=True`
    per employee after renovation and after transfers; no assignment-date gaps
    or overlaps; `StartDate <= EndDate` everywhere;
  - coverage invariant: every constrained (open, non-renovating) store-month has
    ≥ 1 salesperson;
  - key-band integrity: every EmployeeKey decodes to its own StoreKey column;
  - store lifecycle sanity: `ClosingDate > OpeningDate`, renovation windows
    inside the dataset window, `Status` consistent with the dates as-of
    dataset end.
- **Bug-pinning tests** (`xfail(strict=True)`, fixing phase in reason):
  1. non-default `primary_sales_role` → `IsSalesperson` all-False (Phase 1.5);
  2. renovation temp store chosen before its `OpeningDate` / while closed
     (Phase 4);
  3. legacy stores frame without renovation columns → `KeyError` in the ESA
     renovation path (Phase 4);
  4. legacy geography frame without `Country`/`State` → `KeyError` in
     warehouses' `_enrich_with_geography` (Phase 4);
  5. a StoreKey equal to exactly `ONLINE_STORE_KEY_BASE` classifies identically
     in stores, warehouses, employees, and transfers (Phase 2 — today `>`, `>=`,
     `<`, `<=` disagree);
  6. stores.parquet's compression / date32 treatment is identical before and
     after the warehouse enrichment rewrite (Phase 6 — today the rewrite uses
     `cast_all_datetime=True` + hardcoded snappy, ignoring
     `stores.parquet_compression` and `force_date32`).

**Acceptance:** new tests pass (or strict-xfail) on `main` with zero diffs.

---

## Phase 1 — Truth restoration *(identical)*

Zero behavior change, zero logic change, all three packages.

- Employees (from the original plan): rewrite the `generate_employee_dimension`
  docstring (attrition story is gone — document the static model); delete the
  `"Voluntary"`/`"Involuntary"` comment; fix `_stores_signature`'s stale
  write-back justification; fix `run_employees(cfg: Dict)` + the
  `cfg = cfg or {}` guard; correct the "Vectorised cov build" and "single
  consolidated pass" comments; delete the duplicate cast pass and the redundant
  `~online_mask` band term; re-home `_check_coverage_invariant` (final wiring
  decided in Phase 5).
- Stores: fix `run_stores(cfg: Dict)` / `_require_cfg(cfg: Dict)` annotations
  (they take `AppConfig`); remove hardcoded date fallbacks in the runner that
  duplicate config-schema defaults (verify equality first).
- Warehouses: fix `run_warehouses(cfg, ...)` annotation; document (do not yet
  change) that the stores.parquet rewrite is the Phase 6 target.

**Acceptance:** all four snapshots unchanged; full suite green.

---

## Phase 1.5 — Surgical correctness fixes *(behavior — employees schema v12)*

Pulled ahead of the decomposition: the highest-value, lowest-risk employee bug
fixes that are fully independent of the Phase 2 key codec and the Phase 3 module
split. Shipping them here means the real defects are fixed even if the XL
decomposition slips. Each is local to the employees generator (a few lines),
each gets its own commit + regression test, and none touches stores,
warehouses, or the bridge. These three items are lifted out of Phase 4 (which
retains their numbers as `moved to Phase 1.5` pointers so its `4.x`
cross-references stay valid).

1. **`IsSalesperson`/`DepartmentName` honor the configured role** (employees
   generator, `IsSalesperson` derivation ~line 264). Today the flag hardcodes
   `title.isin(["Sales Associate", ONLINE_SALES_REP_ROLE])` while the title
   itself is driven by `primary_sales_role`; a non-default role yields an
   all-False `IsSalesperson` (except online reps), silently breaking every
   downstream salesperson filter. Derive both flag and department from the
   configured role. Flips Phase 0 xfail #1.
2. **Key columns crash on corruption** (employees final cast pass ~lines
   822/827). Replace `fillna(0)` on `EmployeeKey`/`StoreKey` with a raise
   (`DimensionError`) — a NaN key silently coerced to 0 corrupts every key-band
   decode and the sales/bridge joins. Byte-identical for valid inputs; changes
   behavior only on already-corrupt data. New regression test.
3. **Manager BirthDate ≥ 18-at-hire clamp** (employees hr logic). Clamp so no
   employee is born fewer than 18 years before their hire date. New regression
   test (a seed that today produces an under-age hire).

**Why behavior, not identical:** the BirthDate clamp can move bytes for the
default config/seed, so the phase bumps `employees` `schema_version` 11 → 12
(forced regen — release-note it). Items 1 and 2 are byte-neutral for the default
config (they only bite non-default roles / corrupt input) but ride the same bump.

**Acceptance:** Phase 0 xfail #1 flips; new tests for items 2–3 pass; stores and
warehouses snapshots byte-identical; the employees snapshot regenerated once (and
the ESA snapshot only if its output shifts); full `pytest` green.

---

## Phase 2 — Boundary predicate & `EmployeeKeyCodec` *(identical)*

**Goal:** one named home for the online/physical boundary and the employee key
encoding. The boundary is now spelled **four ways** across the cluster:
`sk > BASE` (employees generator), `sk <= BASE` physical (transfers/renovation),
`sk < BASE` (ESA min-stores gate), `sk >= BASE` online / `< BASE` physical
(warehouses). Key 10 000 exactly classifies differently in three packages.

- `is_online_store_key(sk)` / `is_physical_store_key(sk)` live **next to the
  key allocator** — with `ONLINE_STORE_KEY_BASE` in `src/defaults.py` (a pure
  function of the constant; stores allocates keys, everyone else asks). The
  canonical rule: online ⇔ `sk > ONLINE_STORE_KEY_BASE` (matches the allocator:
  online keys start at `BASE + 1`, and the stores generator already raises if
  physical count reaches `BASE`). Route all seven call sites through it,
  including both warehouse spellings.
- `src/dimensions/employees/keys.py`: the `EmployeeKeyCodec` exactly as in the
  original plan (encode/decode/band enum, `MAX_STAFF_PER_STORE` guard), built on
  the shared boundary predicate. `_infer_home_store_key` becomes a thin wrapper;
  the bridge runner adds `StoreKey` to its `columns=` read so the authoritative
  column is primary and arithmetic is the cross-check.
- Add a guard in warehouses: raise if the running `wk` counter would reach
  `ONLINE_WAREHOUSE_KEY` (today a large-enough store estate silently collides
  with the reserved online FC key).
- Re-export old constant names unchanged (scripts and tests import them).

**Acceptance:** snapshots unchanged; Phase 0 xfail #5 flips;
`tests/test_employee_keys.py` covers every boundary value.

---

## Phase 3 — Decompose the generators *(identical)*

**Employees** — as in the original plan: `runner.py` / thin `generator.py` /
`hierarchy.py` / `staffing.py` / `lifecycle.py` / `names.py` / `hr.py` /
`assignments.py` / `transfers.py` / `keys.py`, RNG streams owned by the
orchestrator, `load_store_context()` dataclass replacing the three map-building
blocks. One addition now that stores is in scope: **delete the legacy
no-hierarchy fallback** (generator.py:390–423) instead of isolating it — stores
has emitted `StoreDistrict`/`StoreRegion` since its schema v8 and the runner
cascade guarantees fresh inputs; hard-require the columns with a clear error.

**Stores** — the 1,365-line module (≈580-line `generate_store_table`) gets the
same treatment:

```
src/dimensions/stores/
  __init__.py        # same re-exports (run_stores, generate_store_table, GeoContext)
  runner.py          # config, versioning, geography loading, parquet write
  generator.py       # thin orchestrator
  geo_sampling.py    # _sample_geography_keys + region-weight / iso-coverage modes
  hierarchy.py       # _build_hierarchy (zone → country → district/region)
  lifecycle.py       # Status assignment, opening/closing/renovation dates
  enrich.py          # names, manager names, phones, emails, descriptions
  analytical.py      # ATV / CSAT / turnover / audit / shrinkage
```

Same RNG discipline as employees: the orchestrator owns the single `Generator`
and passes it down; byte-identity proves the draw order survived. The
hash-indexed fallbacks (`sk64 * 5 + seed` brand/area/manager picks) consume no
RNG and move freely.

**Warehouses** — already reasonably sized; split only the runner from the
allocator (`runner.py` + `generator.py`), no further decomposition.

**Acceptance:** all four snapshots unchanged; no module > ~350 lines; existing
tests untouched and green.

---

## Phase 4 — Correctness fixes *(behavior — employees schema v13, bridge v20, stores v9, warehouses v4)*

Each item = one commit + one regression test flipped from Phase 0 xfail or new.

1. **`IsSalesperson`/`DepartmentName` honor the configured role** — *moved to
   Phase 1.5.*
2. **Renovation temp-store selection checks real openness** (assignments.py).
   Replace the `Status == "Open"` filter with date-window logic
   (`_store_is_open` semantics): candidate open for the *entire* temp window.
   Root cause is now documented at the source: stores' `Status` is an
   **as-of-dataset-end snapshot** (renovations that finish reopen to "Open";
   closures flip to "Closed"), so consuming the label for point-in-time
   decisions is always wrong — the dates are the truth. Add that rule to the
   stores module docstring.
3. **Legacy-column guards**: ESA renovation warns-and-skips when renovation
   columns are absent; warehouses' `_enrich_with_geography` degrades gracefully
   (or raises a `DimensionError` naming the missing columns) instead of a raw
   `KeyError` when geography lacks `Country`/`State`.
4. **Key columns crash on corruption** (employees) — *moved to Phase 1.5.*
5. **Assert the transfer-date invariant** (transfers.py): reject candidates
   with `transfer_date > original_end` explicitly; document the
   "Active ⇒ EndDate == global_end" assumption where the eligibility mask is
   built.
6. **Close the date-override asymmetry** (ESA runner → `allow_override=False`,
   matching employees).
7. **Structured manager-name columns** *(the schema addition — now in scope)*.
   The stores generator already holds `first, last` arrays from
   `assign_person_names` and **concatenates them, discarding the structure**
   (stores generator ~line 857–871); employees then token-splits the string back
   apart (employees generator ~line 799–819). Fix at the source: stores emits
   `StoreManagerFirstName` / `StoreManagerLastName` alongside the existing
   `StoreManager` display column (both name-pool and hash-fallback paths);
   employees consumes the structured columns and **deletes the reverse-parser**.
   Requires `static_schemas.py`, SQL DDL, and TMDL updates — this is the one
   deliberate schema addition of the plan.
8. **Inverted-date policy**: stores' opening-window swap-and-warn
   (generator ~line 910) aligns with the cluster policy — raise
   (`DimensionError`), consistent with the fix planned for dates and the
   behavior of everything else.
9. **Manager BirthDate ≥ 18-at-hire clamp** (employees hr.py) — *moved to
   Phase 1.5.*

**Acceptance:** Phase 0 xfails #2–#4 flip; snapshots regenerated once at phase
end; full `pytest` green including sales preflight and inventory tests.

---

## Phase 5 — Unify renovation with the transfer engine *(behavior — bridge schema v21)*

Unchanged from the original plan: extract the assignment-mutation core
(`split_assignment` + `CoverageBudget` bookkeeping), renovation becomes a forced
transfer through the same machinery, fix the rollback chain bug
(`_TransferRecord` chain links; only the last un-rolled-back transfer of a chain
is eligible), wire `_check_coverage_invariant` as an unconditional runner
post-condition, kill the renovation `iterrows`. One addition: the openness
checks all route through a single store-lifecycle helper whose semantics are
owned by the stores package (dates are truth; `Status` is a display label) —
the rule Phase 4.2 establishes.

**Acceptance:** property tests (one-primary, no-gap, no-overlap, coverage)
across 20 seeds; no more cross-module private imports between assignments.py
and transfers.py; snapshots regenerated.

---

## Phase 6 — Single-writer stores.parquet *(behavior — stores schema v10)*

**Goal:** two modules currently write stores.parquet with different writer
settings. The warehouses runner rewrites it with `cast_all_datetime=True` and
hardcoded snappy, ignoring `stores.parquet_compression`,
`parquet_compression_level`, and `force_date32` — so the file's physical schema
depends on which module wrote it last. The dimensions_runner cascade
(auto-forcing warehouses whenever stores regenerates, per CLAUDE.md gotcha #7)
exists purely to compensate for the missing-column crash this split ownership
creates in inventory.

- **Recommended design: warehouse allocation moves inside the stores flow.**
  `run_stores` builds the store frame, calls the warehouse allocator on the
  in-memory frame (`generate_warehouse_table` already takes a DataFrame),
  attaches `WarehouseKey`, and writes **stores.parquet once** with its own
  configured writer settings; warehouses.parquet is written in the same stage.
  `run_warehouses` remains registered for `--regen-dimensions warehouses`
  compatibility but becomes a thin re-invoker. The gotcha-#7 cascade machinery
  in dimensions_runner is then deleted (update CLAUDE.md gotcha #7 accordingly —
  this removes one of the two cross-dimension writes it documents).
- Fallback (if the runner-registration change is judged too invasive): keep the
  two-phase flow but the warehouse runner re-writes stores.parquet **through
  the stores package's writer function with the stores config** — fixes the
  settings drift without moving ownership. Decide at phase start; recommended
  is the first option.
- Warehouses' own parquet write honors `warehouses.parquet_compression` if
  configured (today hardcoded snappy) — align with every other dimension.

**Acceptance:** Phase 0 xfail #6 flips; inventory end-to-end green; a
`--regen-dimensions stores` run no longer leaves a WarehouseKey-less window;
snapshots regenerated.

---

## Phase 7 — Realism *(behavior — employees schema v14)*

- Employees compensation by org level, terminated-employee vacation accrual,
  `EmployeeName` consistency — as in the original plan. The manager-name parse
  item is gone (solved properly in Phase 4.7).
- Stores: none required — `RevenueClass`-correlated analytics are fine. The
  roster-vs-EmployeeCount backstop mismatch (employees can force an extra
  associate for hand-built frames, making the roster exceed `EmployeeCount` and
  the `StoreDescription` headcount lie) becomes unreachable in-pipeline once
  Phase 3 hard-requires fresh stores inputs; keep the warn.

**Acceptance:** distribution tests (CEO BaseRate > every level-6 BaseRate, etc.);
quality report clean; snapshots regenerated.

---

## Phase 8 — Config & versioning hygiene *(identical except version-key churn)*

- One config-access idiom across all three packages; delete the ESA `a_cfg`
  legacy merge if unsupported.
- **Version signatures**: employees' `_stores_signature` (rows+min/max only) and
  warehouses' `_n_stores`+`_zones` are both weak content signatures whose gaps
  are currently papered over by the runner cascade. After Phase 6 the
  stores→warehouses signature disappears entirely (single flow); employees'
  signature folds in a hash of the columns it actually consumes
  (StoreKey, EmployeeCount, OpeningDate, ClosingDate, manager names, hierarchy).
  Replace the ad-hoc `_emp_key_min/max/sum` fingerprint with a content hash.
  One-time forced regen — release-note it.
- Reconcile `resolve_salesperson_roles` (coverage_preflight) with the dimension
  via a single shared helper.
- Grep-gate test: no raw `ONLINE_STORE_KEY_BASE` comparisons outside the
  boundary-predicate module.

**Acceptance:** generated data byte-identical to Phase 7 output (only version
JSON files differ).

---

## Parked decisions (not scheduled — each needs a product call)

- **Mid-window store openings do not exist.** The stores generator shifts the
  opening window entirely before `dataset_start` (generator ~lines 919–932), so
  every store opens before the data begins. Consequence: the opened-mid-window
  machinery maintained across the cluster — `store_opening_dates` hire clamps in
  employees, `_store_is_open` opened-checks, the coverage budget's `opened`
  mask — is **pipeline-dead code**, exercised only by tests. Two coherent
  options: (a) *enable openings as a feature* — stores open mid-window, managers
  and staff hired at opening (the machinery already exists and would finally
  run), which also enriches the `new-market-entry` / growth trend stories; or
  (b) *delete the dead machinery* and document the invariant "all stores open
  before dataset start". Option (a) is more valuable but is modeling work, not
  refactoring. Needs an explicit call before or after this plan.
- **Integer `DistrictId`/`RegionId` columns on stores.** Stores builds
  "District 12" strings from integers; employees regex-extracts the integers
  back (`str.extract(r"(\d+)")`) — the same destroy-then-reparse seam as manager
  names. Emitting the integer columns is the clean fix but is a second schema
  addition; the regex survives Phase 4 otherwise. Recommended: bundle with
  Phase 4.7 if approved.
- **`Status` column retirement to a derived label.** Dates are the truth
  (Phase 4.2 doctrine); `Status`/`OpenFlag` could be derived at write time only.
  Cosmetic; BI consumers expect the columns, so keep — documented here so nobody
  "fixes" the duplication ad hoc.

## Explicit non-goals

- No key-encoding changes (store, employee, and warehouse key schemes frozen).
- No revival of employee attrition; no store opening/closing *model* changes
  beyond the parked decisions.
- No changes to how sales/inventory/budget consume the outputs (beyond the
  structured manager-name columns being additive).
- No warehouse allocation redesign — the three-tier greedy grouping is sound.

## Sequencing & effort

| Phase | Type | Size | Depends on |
|---|---|---|---|
| 0 Guardrails | tests only | M–L | — |
| 1 Truth restoration | identical | S | 0 |
| 1.5 Surgical fixes | behavior | S | 1 |
| 2 Boundary + KeyCodec | identical | S–M | 0 |
| 3 Decomposition (emp + stores) | identical | L–XL | 1, 2 |
| 4 Correctness fixes | behavior | M–L | 3 |
| 5 Transfer unification | behavior | L | 4 |
| 6 Single-writer stores | behavior | M | 4 |
| 7 Realism | behavior | M | 4 |
| 8 Config/versioning | hygiene | S–M | 5, 6, 7 |

Phases 5, 6, and 7 are mutually independent after Phase 4. Everything through
Phase 4 is the "stop the bleeding" milestone and is worth shipping even if 5–8
slip. **Phase 1.5 pulls the three surgical employee bug fixes ahead of the
XL decomposition**, so the core defects land early and are not held hostage to
Phase 3 completing — the "stop the bleeding" value no longer depends on the
largest, riskiest phase.

## Standing TODO (bugs found later go here, not into unrelated phases)

- **The Phase 1 "duplicate cast pass" was a false premise — no action taken.**
  The plan called for deleting the integer casts at the end of
  `generate_employee_dimension` as redundant with `_finalize_employee_integer_cols`.
  Adversarial verification showed they are *not* redundant: those casts are the
  only place the seven key/int columns get integer-typed *inside*
  `generate_employee_dimension`, which is a public, directly-tested API (called in
  `tests/test_dimensions.py` without `_finalize_employee_integer_cols`). Removing
  them flips its return dtype `int32 → object`. The two cast sites live in
  different functions / pipeline stages; the later one operates on the enriched
  frame in `run_employees`. Left as-is; the `# Final integer casts` comment is
  accurate for that function.
- **Re-home `_check_coverage_invariant`.** Defined in `employees/transfers.py`,
  currently called only by tests. Phase 5 wires it as an unconditional runner
  post-condition; the physical move is deferred to that phase to keep Phase 1
  byte-identical.
