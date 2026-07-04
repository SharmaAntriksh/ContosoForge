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
| `src/engine/runners/dimensions_runner.py` | cascade: stores (co-produces warehouses) → employees → ESA | regen triggers |
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
   822/827). Replace `fillna(0)` on the identity keys with a raise
   (`DimensionError`) — a NaN key silently coerced to 0 corrupts every key-band
   decode and the sales/bridge joins. Implemented as a testable
   `_assert_identity_keys(df)` guard called before the cast: `EmployeeKey` raises
   unconditionally (never legitimately NaN); `StoreKey` raises **only for
   store-level rows** (`OrgUnitType == "Store"`), because corporate/region/district
   rows legitimately have a NaN `StoreKey` that the intentional 0-fill maps to 0 —
   a blanket `StoreKey` raise would break every run. Byte-identical for valid
   inputs; changes behavior only on already-corrupt data. New regression test.
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
  **(Superseded — `keys.py` and the entire EmployeeKey encoding were removed in
  v2; `EmployeeKey` is now an opaque sequential surrogate and home store / tier
  are read from columns. See
  [STORE_CLUSTER_V2_DESIGN_NOTES.md](STORE_CLUSTER_V2_DESIGN_NOTES.md) Item 1.)**
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

- **Mid-window store openings.** ✅ IMPLEMENTED (option (a), on a flag). Was:
  the stores generator shifted the opening window entirely before `dataset_start`
  so every store opened before the data began, and the opened-mid-window machinery
  (`store_opening_dates` hire clamps, `_store_is_open` checks, the coverage budget
  `opened` mask) was pipeline-dead, exercised only by tests. Now gated by the
  `stores.allow_mid_window_openings` config flag (default **false** → old shifted
  behaviour, byte-identical). When **true**, the opening-window shift is skipped so
  stores may open inside the data window, and the bridge builder
  (`generate_employee_store_assignments`) clamps each assignment's `StartDate` up to
  its store's `OpeningDate` (a `StoreKey → OpeningDate` map threaded from the runner)
  so no one is assigned before the store exists — the clamp is a no-op when the store
  opened before `global_start`, which is why flag-off stays byte-identical. The
  `StartDate >= OpeningDate` invariant is pinned by
  `tests/test_mid_window_openings.py` (the existing 20-seed bridge property tests
  stayed green even with pre-opening assignments, so they would not have caught a
  regression). The *consuming* machinery (coverage invariant, coverage preflight,
  `store_open_on`, transfer destinations, renovation) was already opening-aware.
  **Staggered ramp — ✅ DONE.** A mid-window store's roster now ramps up around its
  opening `O` instead of pre-existing: the manager is hired `O - Uniform[14, 45]`
  days ahead (store setup), the sales role lands on `O` (so salesperson coverage
  holds from the store's first month), and the remaining staff trickle in over
  `[O, O + 60]` days (`MID_WINDOW_*` constants in `defaults.py`). The bridge clamp
  turns this into staggered assignment StartDates (managers/sales at `O`, other
  staff spread `O..O+ramp`). The whole ramp is inert (draws no RNG) when no store
  opens mid-window, so flag-off stays byte-identical. Pinned by
  `tests/test_mid_window_openings.py::test_mid_window_roster_ramps_in`.
  **End-to-end validation (default config, 80 stores, flag on).** A full pipeline
  run confirmed the consuming side holds under mid-window: 51/80 stores opened
  inside the window; **0** bridge assignments started before their store's
  OpeningDate across all 1,195 rows — including the `Renovation Reassignment`,
  `Renovation Return`, `New Store Opening`, `Backfill`, and `Seasonal Support`
  transfer reasons, so the transfer + renovation passes are already opening-aware
  (`apply_transfers` only staffs open destinations); **0** of 100k sales lines were
  dated before their store opened; salesperson coverage held (0 `EmployeeKey=-1`,
  0 orphan FKs; coverage pre-flight reported 0 fully-uncovered, 0 avoidable). One
  caveat: this is one seed/config, not a proof for every opening window.
  **Minimum operating window (STORE_MIN_OPERATING_DAYS = 365, defaults.py).** A
  store must be *visibly* operating for at least a year — between
  max(OpeningDate, dataset_start) and min(ClosingDate, dataset_end) — or it isn't
  worth capturing. Two enforcement points in `stores/lifecycle.py`
  `assign_opening_closing_dates`: (1) with mid-window on, the opening window is
  capped at dataset_end - 1yr so no store opens in the last weeks of the data;
  (2) a closing store's ClosingDate is floored at max(OpeningDate, dataset_start)
  + 1yr, and a store with no room for a full-year window before the close cutoff
  simply stays Open (reverts). This changes the default (flag-off) output too —
  closed stores that used to close early in the window now run ≥1yr or stay open
  — so the stores schema bumped 11→12 and the golden fingerprint regenerated.
  Guarded by `TestMidWindowCluster` (a second, mid-window snapshot fixture) and
  the `verify_employee_store_sales.py` "operates >= ~1 year" check.
- **Integer `StoreDistrictId`/`StoreRegionId` columns on stores.**  ✅ DONE in v2.
  Stores used to build "District 12" strings from integers and employees
  regex-extracted them back (`str.extract(r"(\d+)")`) — a destroy-then-reparse
  seam. Stores now emits the integer columns alongside the display strings and
  employees reads the ints directly (regex kept only as a legacy fallback). See
  [STORE_CLUSTER_V2_DESIGN_NOTES.md](STORE_CLUSTER_V2_DESIGN_NOTES.md) Item 2.
- **`Status` column retirement to a derived label.**  ✅ RESOLVED in v2 (keep +
  document). Dates are the truth (Phase 4.2 doctrine), but BI consumers expect a
  `Status` column, so it stays; `static_schemas.py` now documents the as-of-end
  semantics on both the Stores and Employees `Status` columns so nobody "fixes"
  the duplication ad hoc. See
  [STORE_CLUSTER_V2_DESIGN_NOTES.md](STORE_CLUSTER_V2_DESIGN_NOTES.md) Item 4.

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
- **Re-home `_check_coverage_invariant` — done (Phase 5).** Moved to
  `employees/assignment_core.py` as `check_coverage_invariant` and wired as an
  unconditional post-condition of `run_employee_store_assignments` (warns on any
  constrained store-month left without a salesperson after renovation +
  transfers).
- **Bridge key dtype normalised to int32 (Phase 5 byproduct).** `EmployeeKey` /
  `StoreKey` were `int64` only when renovation split rows (the `iterrows` →
  `to_dict` → python-int path) and `int32` otherwise. The renovation rewrite now
  emits `int32` unconditionally, matching `static_schemas` (`INT`) and the
  transfer engine. Recorded here so a future reviewer doesn't read the change as
  an accident.
- **Config & versioning hygiene — done (Phase 8).** Generated data byte-identical
  to Phase 7 (the golden fingerprint was never regenerated; full suite green);
  only version-key inputs and a couple of internal seams changed. Four items:
  (1) one shared `resolve_salesperson_roles` in `sales_helpers.py`, called by both
  the coverage preflight and the sales dimension loader (they resolved it
  independently before); byte-identical since `sales.salesperson_roles` isn't a
  schema field so both always fell back to `primary_sales_role` + the online rep.
  (2) grep-gate test (`test_gotchas_and_guards.py::TestOnlineBoundaryPredicate`)
  tokenizes every `src` module and asserts only `defaults.py` (the predicate home)
  and the stores key allocator reference the raw `ONLINE_STORE_KEY_BASE`. (3) new
  `frame_content_hash` versioning primitive (order-independent, vectorized); the
  employees→stores signature (was rows + StoreKey min/max, and deliberately
  excluded EmployeeCount) and the ESA→employees signature (was
  `_emp_key_min/max/sum` + `_hire_min/max`) now content-hash the exact consumed
  columns, so any change to a consumed value forces a regen — a one-time forced
  regen. (4) deleted the ESA legacy `a_cfg` merge (top-level
  `employee_store_assignments` is a vestigial no-op; read
  `employees.store_assignments` directly), byte-identical including the version
  key. Left the one-config-idiom sweep across the three runners alone — they were
  already consistent (attribute reads + `as_dict` + `int_or`), the ESA merge being
  the only real outlier.
- **Employee realism — done (Phase 7, employees schema v14).** Three fixes,
  all confined to the isolated HR RNG stream / a deterministic name op, so
  stores/warehouses/bridge stay byte-identical (only the employees fingerprint
  regenerated): (1) `BaseRate` scales with org level — non-overlapping per-level
  salary bands in `defaults.py` (`EMPLOYEE_SALARY_BAND_BY_LEVEL` +
  `EMPLOYEE_STAFF_HOURLY_BAND`, validated monotonic at import), replacing the
  single salaried distribution that let a CEO earn a store manager's wage; (2)
  vacation accrual stops at `TerminationDate` (a closed-store worker no longer
  banks vacation to the dataset end); (3) `EmployeeName` derived once from
  First/Last after every name mutation (byte-identical, kills the two-writer
  drift risk). Distribution guards (CEO out-earns every level-6; each level's
  min BaseRate > the level below) + vacation + name-consistency regression tests
  added to the cluster harness / `test_dimensions.py`.
- **Single-writer `stores.parquet` — done (Phase 6, stores schema v10).**
  Warehouse allocation moved into `run_stores`: it allocates on the in-memory
  store frame, stamps `WarehouseKey` inline, and writes both `stores.parquet`
  and `warehouses.parquet` once with the stores-configured writer settings. The
  old two-step rewrite (hardcoded snappy + `cast_all_datetime`, ignoring
  `stores.parquet_compression`/`force_date32`) is gone, flipping bug-pin #6.
  `run_warehouses` is now a thin guard that skips when fresh and re-invokes the
  stores flow when stale (`--regen-dimensions warehouses`); warehouse-shaping
  config folds into the stores version key. `dimensions_runner` marks
  `warehouses` regenerated whenever `stores` regenerates (co-production), so the
  propagation no longer re-runs the guard or the obsolete stores-rewrite cascade
  — CLAUDE.md gotcha #7 loses one of its two cross-writes. Byte diff: only
  `stores.WarehouseKey` `Int32 → int32` (values identical; now matches its
  `INT_NN` sibling `StoreKey`); warehouses/employees/ESA byte-identical, stores
  fingerprint regenerated.
