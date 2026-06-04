# Code Review Findings

Running log of issues / silent bugs found during the area-by-area review.
The review pass is complete; we are now **fixing holistically** (one change shouldn't break
another). Each finding has an ID, severity, status, and a proposed fix. As findings are
resolved they are checked off in **Completed work** below and their inline `Status:` flips to
`fixed` (with a note on the fix + tests).

Severity scale: **High** (corrupts data in common configs) · **Medium** (corrupts data
in some configs or at edges) · **Low** (fragility / latent / cosmetic).

Status: `open` · `confirmed` · `wontfix` · `fixed`

---

## ✅ Completed work

Branch: `fix/sales-order-id-integrity`. SQL-related findings (TOOLS-1, the SQL-DDL facet of
SCHEMA-1) are deferred to the end per plan.

| ID | Sev | What was fixed | Tests |
|----|-----|----------------|-------|
| **QR-1** | Medium | Quality report now checks `sales_order_header.SalesOrderNumber` uniqueness/nulls + fact-to-fact FK (returns/complaints → header). Shared `_emit_fk_check` helper. This is the **detector** for CHUNK-1. | `tests/test_quality_report.py::TestSalesOrderIntegrity` |
| **CHUNK-1** | **High** | Within-day order cursor derived from a stable sort (`_within_day_cursor`) instead of `arange − first_index`, so SalesOrderNumber stays unique across chunks despite the post-sort customer-start clamp. Overflow guard now keys on cursor magnitude. | `tests/test_chunk_id_integrity.py`; verified end-to-end (multi-chunk header SO# unique) |
| **SCHEMA-1 / ORCH-1** | Medium | `SalesOrderNumber` int32→int64 decision now sized to the real ~8× day-ID space (single `_order_id_int64` threaded to schema + builder + returns); warning re-thresholded. Generation/parquet facet only — SQL DDL `BIGINT` widening deferred to the SQL pass. | `TestBuildWorkerSchemas::test_order_id_int64_*`, `TestBuildSalesReturns::test_int64_*`; verified end-to-end (forced int64 run, all SO# columns int64 + consistent) |

**Remaining next up (suggested order):** DATES-1 (4-4-5 boundary clipping), then the low-severity
cluster (CHUNK-3, RETURNS-1, CUST-SCD2-1/2, EMP-1, INV-1, WISH-1, etc.). SQL findings (TOOLS-1 +
SCHEMA-1's `BIGINT` DDL facet) batched for the end. See the table below for the full list.

---

## Consolidated summary (severity-ranked) — for the holistic fix pass

Areas reviewed so far: **Dates dimension**, all of **Sales facts**
(`sales_models`, `sales_logic/core`, `sales_logic`, `sales_worker`, `sales_writer`, `sales.py`),
**Customers dimension** (generator, scd2, helpers, households, subscriptions),
**Products dimension** (generator, scd2, pricing, expander, loader, profile),
**Stores + Warehouses dimensions** (incl. the gotcha #7 cross-write + runner cascade),
**Employees dimension** (generator, store-assignment bridge, transfers),
**Geography dimension** (master-file loader),
**Exchange Rates + Currency dimensions** (incl. the Yahoo FX integration),
and the **small dimensions** (promotions, lookups/sales-channels/loyalty/acquisition,
return_reasons, suppliers). **All dimensions are now reviewed.**

Facts reviewed: **shared**, **budget**, **inventory**, **wishlists**, **complaints**.
**All facts are now reviewed.** Infrastructure reviewed: **versioning/**.
Engine reviewed: `pipeline_runner` (clean), `config/config` cross-section+scale (clean),
`quality_report` (QR-1), `packaging/package_output` (clean), `config_schema._MutationMixin` (clean,
dict.get-faithful), `powerbi_packaging` TMDL escaping (clean, gotcha #18). Not deep-read (lower-risk
plumbing): `sales_runner` (BUDGET-2 confirmed via grep), csv/parquet/delta packagers (file copy),
`config_schema` validators, `dimension_loader`. Utils reviewed: `shared_arrays`, `trend_presets` (resolve), `config_precedence`, `config_helpers`,
`static_schemas` (spot-check) — all clean. (`web/` skipped per user.)

## Area: tools/sql (`src/tools/sql/`) — rating 7.5/10
Escaping is correct (gotcha #17): `sql_escape_literal` doubles single quotes; `quote_ident` escapes
`]`→`]]`; `OBJECT_ID(N'...')` and `BULK INSERT FROM N'...'` both use the Unicode prefix + escaped
literal. The dialect abstraction (base/sqlserver/postgres) is clean.

### TOOLS-1 — CSV row-terminator mismatch on Windows for batch facts
- **Severity:** Medium (Windows-specific; loud import failure for most tables, silent `\r` for budget)
- **Status:** confirmed (empirically: pandas `to_csv` → CRLF, pyarrow → LF on this Windows host)
- **Where:** `bulk_load_statement` hardcodes `ROWTERMINATOR = '0x0a'`
  ([dialect/sqlserver.py:73-74](src/tools/sql/dialect/sqlserver.py#L73)); batch facts written CRLF via
  pandas `to_csv` ([shared/writers.py:101-106](src/facts/shared/writers.py#L101-L106),
  [wishlists/runner.py:572-575](src/facts/wishlists/runner.py#L572),
  [inventory/worker.py:120](src/facts/inventory/worker.py#L120)).
- **What:** dimensions and the sales/returns/header/detail facts are written with **pyarrow** (LF) and
  match `0x0a`. But the **batch facts** (BudgetYearly/Monthly, InventorySnapshot, Complaints,
  CustomerWishlists — all present in `_FOLDER_TABLE_ALIASES`, so they receive BULK INSERT statements)
  are written with **pandas `to_csv`**, which defaults to **CRLF on Windows**. With `ROWTERMINATOR='0x0a'`
  the trailing `\r` becomes the last character of each row's last field:
  - last col numeric (InventorySnapshot=`DaysOutOfStock`, Complaints=`ResponseDays`, wishlists price) →
    conversion error → **BULK INSERT fails** (loud);
  - last col string (Budget=`BudgetMethod`) → **silent trailing `\r`** in the value.
- **Proposed fix:** pass `lineterminator="\n"` to every pandas `to_csv` for emitted tables (shared
  writer + wishlists + inventory worker), or write batch facts via pyarrow like the others, or set
  `ROWTERMINATOR` from the actual file ending. Forcing LF everywhere is simplest and matches the
  pyarrow path.

Not deep-read: `sql_server_import.py`/`postgres_import.py` (Python DB-loader tooling that *runs* the
import via pyodbc/psycopg — connection/parallelism logic, not generated-data correctness),
`generate_create_table_scripts.py` (DDL spelling).

## Area: utils (`src/utils/`) — clean, no findings
- `shared_arrays.py` (gotcha #6): read-only zero-copy views, RAII cleanup, stale-block recovery,
  keep-alive worker handles, object-dtype → pickle fallback. Windows shared-memory lifecycle correct.
- `trend_presets.resolve_trend_preset`: user YAML overrides preset via `model_fields_set` deep-merge,
  emits proper Pydantic models (gotcha #24), zeros `yearly_growth`/`seasonality_amplitude` to avoid
  double-counting. Correct.
- `config_precedence` / `config_helpers`: documented precedence chains honored; coercions correct;
  `parse_global_dates` swap-guards inverted ranges. (`region_from_iso_code` defaults unmapped
  currencies like ZAR/AED to a fallback name-pool region — cosmetic, not a bug.)
- `static_schemas`: VARCHAR sizes reasonable; the long `StoreDescription` correctly uses
  `VARCHAR("MAX")`. **Confirms SCHEMA-1 in the SQL DDL too**: `SalesOrderNumber` is `INT` (int32), so
  the day-ID overflow (>~268M rows) would also overflow the SQL import column, not just parquet.

Not deep-read (lower-risk plumbing): `output_utils` (writers — the dim-CSV pyarrow/LF path was
confirmed relevant to TOOLS-1), `name_pools`, `pool`, `logging_utils`, `config_merge`,
`products/catalog_builder.py` (offline catalog generator).

---

## Review status: COMPLETE for the data-generation pipeline

Covered end-to-end: all **dimensions**, all **facts**, **engine** (config/runners/packaging/quality),
**versioning**, **tools/sql**, and **utils** (correctness-critical modules). Skipped per user: `web/`.
Lower-risk plumbing noted inline as not-deep-read.

**Severity tally:** 1 High (CHUNK-1) · 5 Medium (SCHEMA-1/ORCH-1, DATES-1, EMP-1, QR-1, TOOLS-1) ·
~15 Low/latent. The two highest-leverage fixes remain **CHUNK-1** (most downstream blast radius) and
**QR-1** (so the quality report can actually detect CHUNK-1).

| ID | Sev | One-liner |
|----|-----|-----------|
| ✅ **CHUNK-1** | **High** | ~~Duplicate `SalesOrderNumber` across chunks: start-date clamp breaks the day-ID cursor's sorted-input assumption (multi-chunk + order cols + acquisition).~~ **FIXED** |
| ✅ **SCHEMA-1 / ORCH-1** | Medium | ~~int64 SO# promotion mis-thresholded; day-IDs hard-cast to int32 → runs >~268M rows crash on overflow.~~ **FIXED** (parquet/generation; SQL DDL `BIGINT` widening deferred to SQL pass) |
| **DATES-1** | Medium | 4-4-5 month/quarter boundaries clipped to data range at partial edge periods (masked by default buffer; exposed at `buffer_years: 0`). |
| **CHUNK-3** | Low-Med | Final-assembly "mixed" path drops null months instead of padding (latent; would error/misalign if a column's presence varies per month). |
| **RETURNS-1** | Low-Med | Split-return event dates not monotonic by sequence (only if `split_return_rate>0`). |
| **DATES-2** | Low | Fiscal/FW as-of offsets via row-lookup with all-zeros fallback (dead today; fragile). |
| **CORE-1..5** | Low | Discovery urgency-order corner; line-adjust loop hard-fail; silent row under-removal; channel-filter silent disable; vestigial pricing params. |
| **CHUNK-2** | Low | `seal()` never called in prod → CLAUDE.md gotcha #3 inaccurate; discovery state is per-worker. |
| **CUST-SCD2-1** | Low-Med | SCD2 life-event offset collapse can emit version rows with end < start (malformed interval). |
| **CUST-SCD2-2** | Low | `customers.max_versions: 1` crashes (`rng.integers(1,1)`). |
| **EMP-1** | Medium | Staff EmployeeKey collision + wrong-store decode when a store has >1000 staff (only int64 overflow guarded, not the ×1000 encoding mult). |
| **FX-CUR-1** | Low | Explicit `currency.currencies` omitting an FX currency → cryptic NaN→int32 crash in the FX key-join. |
| **BUDGET-1** | Low | Monthly budget can exceed yearly total for categories missing months (seasonal shares normalized before the 12-month expand + 1/12 fillna). |
| **BUDGET-2** | Low (perf) | Returns micro-agg computed every chunk (join + bincount) but `finalize_returns()` is never called → wasted compute; several BudgetConfig fields are dead. |
| **INV-1** | Low-Med | ABC recompute ranks demand by per-**version** ProductKey + assigns family ABC by arbitrary last-version-wins → distorted ABC when product SCD2 is enabled (correct when off, the default). |
| **WISH-1** | Low-Med | Wishlist selection misc-RNG pool refill headroom (60) < worst-case per-item retry consumption (~251) → latent `IndexError` for high-collision customers. |
| **WISH-2** | Low | Wishlist SCD2 price lookup always returns None (detects SCD2 by duplicate ProductKey, but scheme uses unique key per version) → wishlist prices always current, never historical (dead code, graceful fallback). |
| ✅ **QR-1** | Medium (meta) | ~~Quality report has no uniqueness check on `SalesOrderHeader.SalesOrderNumber` and no fact-to-fact FK check → CHUNK-1's duplicate order numbers pass undetected (false green).~~ **FIXED** |
| **TOOLS-1** | Medium | Batch facts (budget/inventory/complaints/wishlists) written CRLF by pandas `to_csv` on Windows, but generated BULK INSERT hardcodes `ROWTERMINATOR='0x0a'` (LF) → import failure (numeric last col) or silent trailing-`\r` (string last col). Dims/sales use pyarrow (LF) and are safe. |
| **RETURNS-2** | Low | Logistics return reason can leak to on-time orders via CDF boundary overwrite. |
| **WORKER-1 / WRITER-1 / TASK-2 / DATES-3 / SM-1..3** | Low | CSV unquoted; back-compat unsafe cast; duplicated channel/time logic; label heuristics; pricing nits. |

**Downstream of CHUNK-1 (resolved by fixing it):** duplicate `SalesOrderHeader` rows (TASK-1),
ambiguous returns→sales joins, and earlier int32 overflow.

**Suggested fix order:** CHUNK-1 first (largest blast radius; verify with a small multi-chunk
repro of SO# uniqueness), then SCHEMA-1/ORCH-1 (same ID subsystem), then DATES-1, then the
low-severity cluster opportunistically.

---

## Area: Dates dimension (`src/dimensions/dates/`) — rating 8.5/10

Strong, carefully engineered. Arithmetic DateKey/serial, contiguous month/quarter
indices across year boundaries, leap-safe month-end snapping, correct 4-4-5 engine
(53-week detection, all week-pattern variants, pattern-derived DatePrevious).

### DATES-1 — Weekly-fiscal month/quarter boundaries clipped to data range
- **Severity:** Medium (currently masked by default buffer; exposed at `buffer_years: 0`)
- **Status:** confirmed
- **Where:** [weekly_fiscal.py:215-221](src/dimensions/dates/weekly_fiscal.py#L215-L221);
  derived label at [:316](src/dimensions/dates/weekly_fiscal.py#L316)
- **What:** `FWStartOfMonth`, `FWEndOfMonth`, `FWStartOfQuarter`, `FWEndOfQuarter`
  (and derived `FWDayOfMonth`, `FWDayOfQuarter`, `FWMonthLabel`) use
  `groupby(FWMonthIndex)["Date"].transform("min"/"max")` — min/max over only the dates
  present in the frame. At the first/last *partial* fiscal period of the range these
  clip to the table edge instead of the true fiscal-period boundary, so boundary-month
  `FWStartOfMonth` = table's first date and `FWDayOfMonth` undercounts.
- **Contrast:** `FWStartOfYear`/`FWDayOfYear` are immune — they use the true `bounds`
  map ([:167-170](src/dimensions/dates/weekly_fiscal.py#L167-L170)). The inconsistency
  is the tell.
- **Masking:** [runner.py:48-50](src/dimensions/dates/runner.py#L48-L50) expands to whole
  calendar years with default `buffer_years: 1`, pushing clipped periods into the buffer
  zone outside real data. But `buffer_years: 0` is allowed (`max(0, …)`), and default
  fiscal start is May → Jan 1 is mid-fiscal-year → clipped boundary lands inside real data.
- **Proposed fix:** derive month/quarter boundaries arithmetically from the week pattern
  (same approach already used for `FWMonthDays`), not from `groupby`.

### DATES-2 — Fiscal as-of offsets via row-lookup with all-zeros fallback
- **Severity:** Low (latent / fragility; dead code today)
- **Status:** confirmed
- **Where:** [fiscal.py:84-98](src/dimensions/dates/fiscal.py#L84-L98);
  [weekly_fiscal.py:366-385](src/dimensions/dates/weekly_fiscal.py#L366-L385)
- **What:** offsets are computed by locating the row where `Date == as_of`; if not found,
  **every** offset is set to 0. Dead today (as_of is normalized + clamped in-range), but
  `calendar.py` computes the same offsets purely arithmetically
  ([calendar.py:182-186](src/dimensions/dates/calendar.py#L182-L186)) with no lookup. If
  clamping ever changes, calendar offsets keep working while fiscal/FW offsets silently
  collapse to all-zeros instead of erroring.
- **Proposed fix:** compute fiscal/FW offsets arithmetically, matching calendar.py.

### DATES-3 — `FWMonthLabel` representative-month heuristic
- **Severity:** Low (cosmetic)
- **Status:** open
- **Where:** [weekly_fiscal.py:316](src/dimensions/dates/weekly_fiscal.py#L316),
  [:319](src/dimensions/dates/weekly_fiscal.py#L319)
- **What:** picks the label month via `fw_start_month + 14 days`. For 5-/6-week fiscal
  months that lands in the first third (possible mislabel); also inherits DATES-1 clipping
  at boundaries.
- **Proposed fix:** pick the month containing the period midpoint computed from the
  pattern length, after DATES-1 is fixed.

### Not yet reviewed in this area
- `time.py` (1440-row minute grid) — low risk, skipped.

---

## Area: Customers dimension (`src/dimensions/customers/`) — rating 8.5/10

High quality. `helpers.py` (acquisition curves, tier assignment, income/credit/address) applies
CDF/quantile boundary guards correctly. `generator.py` wires dates coherently: `CustomerStartDate`
(original acquisition, preserved across SCD2 versions) feeds the sales clamp LUT + eligibility,
while `EffectiveStartDate` is version-specific. `households.py` greedy matching is sound.
`subscriptions/` correctly dedups to `IsCurrent==1` before bridging (correct FK/entity semantics),
and the int32→bool schema cast in `from_arrays` is benign (verified empirically).

### CUST-SCD2-1 — SCD2 life-event offsets can collapse → invalid (end < start) version rows
- **Severity:** Low-Medium (rare; produces malformed SCD2 intervals)
- **Status:** confirmed
- **Where:** [scd2.py:368-374](src/dimensions/customers/scd2.py#L368-L374) (offset spacing),
  consumed at [:379-393](src/dimensions/customers/scd2.py#L379-L393)
- **What:** event offsets are drawn with `rng.integers(90, max_offset, size=n_events)` (can return
  duplicates) then spaced ≥60 days via `offsets[i]=min(offsets[i-1]+60, max_offset-1)`. When draws
  cluster near the top, the `max_offset-1` clamp makes consecutive offsets **equal**, yielding two
  versions with the same `EffectiveStartDate`. The version chaining then sets the second version's
  `EffectiveEndDate = event_date - 1 day` < its `EffectiveStartDate = event_date` → a row with
  end < start (an empty/negative interval). Sales SCD2 resolution tolerates it (picks the later
  same-start version), but `customers.parquet` carries a malformed "phantom" version row that never
  matches a point-in-time `[start,end]` join. The n_events cap usually leaves enough room, so this
  is reachable only on unlucky clustered draws.
- **Proposed fix:** deduplicate `event_dates` after spacing (drop/merge offsets that remain equal),
  or draw distinct offsets via `rng.choice(range, replace=False)`, or skip emitting a version whose
  interval would be empty.

### CUST-SCD2-2 — `max_versions=1` raises in `rng.integers(1, max_versions)`
- **Severity:** Low (edge config; loud crash)
- **Status:** confirmed
- **Where:** [scd2.py:342](src/dimensions/customers/scd2.py#L342)
- **What:** `int(rng.integers(1, max_versions))` with `max_versions=1` calls `integers(1, 1)` →
  `ValueError: low >= high`. Default is 4, so only bites if a user sets `customers.max_versions: 1`
  (a reasonable "disable extra versions" intent).
- **Proposed fix:** guard `max_versions <= 1` to mean "no extra versions" (skip expansion) instead
  of calling `integers(1, 1)`. **Reference:** `products/scd2.py` already does exactly this
  ([products/scd2.py:37](src/dimensions/products/scd2.py#L37)) — mirror it.

---

## Area: Products dimension (`src/dimensions/products/`) — rating 9/10

Cleanest dimension reviewed so far. No findings. Notable strengths verified:
- **scd2.py** uses deterministic arithmetic version spacing (strictly increasing dates) — so it
  has **no** offset-collapse / invalid-interval bug (the issue CUST-SCD2-1 has) and handles
  `max_versions<=1` gracefully. This is the reference pattern for fixing the customers SCD2.
- **gotcha #25 linchpin confirmed:** [generator.py:275](src/dimensions/products/generator.py#L275)
  sets `ProductID = ProductKey` before SCD2, so post-SCD2 `BaseProductID == ProductID` holds.
- **pricing.py** is defensive and correct: `_step_for_value` searchsorted bands, NaN/inf handled
  throughout, margins sampled per-BaseProductID (variant consistency), cost ≤ price enforced.
- **contoso_expander.py** Hamilton largest-remainder allocation with a terminating deficit loop;
  variant grouping is safe because the combined catalog has globally-unique ProductKeys
  (verified empirically: Contoso 1–2517, Synthetic 2518–8150, zero overlap).
- **product_profile.py** categorical draws all use normalize→cumsum→searchsorted→**clip**, and use
  deterministic splitmix64 hashing keyed by base product, so **serial and parallel enrichment
  produce identical output** (no worker-count determinism divergence).

Caveat: `catalog_builder.py` (1083-line offline catalog generator) was not reviewed — it runs
once to build the static parquets, not at dataset-generation time. Its output (unique keys) was
validated empirically.

Note on SM-2: products/pricing.py uses a *deterministic* snap while the sales pipeline uses a
*stochastic* snap; they agree only when product SCD2 is on (sales reads catalog prices directly).
This is the mechanism behind SM-2, already logged — not a products bug.

---

## Area: Stores + Warehouses dimensions (`src/dimensions/stores`, `.../warehouses`) — rating 9/10

Clean. No findings. The store generator's date/status logic is careful and internally consistent:
- Opening window is shifted to end **before** `dataset_start` (so physical stores exist before
  sales begin), with width preserved ([generator.py:892-905](src/dimensions/stores/generator.py#L892-L905)).
- Closures constrained to `[dataset_start+30, dataset_end-60]`; opening-before-closing enforced
  with a repair pass; renovation dates floored at OpeningDate; reopened/closed-for-renovation
  cases all produce historically-consistent reno windows that correctly feed sales availability
  filtering (`store_reno_start/end_day`).
- Online vs physical key spaces are separated and bounded (physical < `ONLINE_STORE_KEY_BASE`,
  enforced with a raise).

### Gotcha #7 (warehouse cross-write to stores.parquet) — verified safe
The hazard: `run_warehouses` overwrites `stores.parquet` to add `WarehouseKey`
([warehouses/generator.py:417-424](src/dimensions/warehouses/generator.py#L417-L424)); a fresh
stores.parquet without it would crash inventory. **Both** safeguards are present and correct in
[dimensions_runner.py](src/engine/runners/dimensions_runner.py):
1. Static downstream cascade — forcing `stores` forces dependents incl. `warehouses` (lines 434-445).
2. Runtime check — if `stores` regenerated (hash change) but `warehouses` didn't, it deletes the
   warehouse version and **actually re-runs** `run_warehouses` (lines 491-498).
Spec ordering guarantees warehouses runs after stores, and employees after both, in a single pass.
The cross-write is idempotent (overwrites WarehouseKey if already present).

Minor watch-item (not a bug): the warehouse overwrite mutates `stores.parquet` after its
`.version` is saved. This is safe because `should_regenerate` keys on the **config hash**, not the
output file's content — confirm this assumption still holds when the versioning area is reviewed
(if it ever hashed output bytes, stores would needlessly regen every run, though the runtime check
above would self-heal the WarehouseKey).

---

## Area: Employees dimension (`src/dimensions/employees/`) — rating 8/10

The store-assignment bridge + transfer engine are well-built; one latent encoding bug in the
employee generator.

Strengths verified:
- **Bridge date ranges are contiguous & non-overlapping per employee.** Initial bridge is one row
  per employee `[max(hire,start), min(term,end)]`; renovation splits produce contiguous pre/temp/
  post segments; transfer splits set old `EndDate = transfer_date-1`, new `[transfer_date, new_end]`
  within the original range. So each employee is at exactly one store per date → no double-counting
  in sales salesperson sampling.
- **Online employees excluded from transfers** ([transfers.py:636-637](src/dimensions/employees/transfers.py#L636-L637)),
  and only one eligible (primary/active/staff/tenured) row per employee per year → ≤1 transfer/employee/year.
- New assignment end clamped to dest store close/renovation and to the employee's termination.
- Coverage-budget feasibility checks + **all-or-nothing surgical rollback** (reverts to original
  assignments if violations remain) — a safe failure mode.
- Managers carry `RoleAtStore="Store Manager"` so the sales salesperson pool (filtered to
  `salesperson_roles`) never emits manager keys — consistent with the "never emit Store Manager" rule.

### EMP-1 — Staff EmployeeKey collision when a store has > 1000 staff
- **Severity:** Medium (latent; silent corruption when triggered; needs unusual staffing config)
- **Status:** confirmed
- **Where:** [generator.py:571-574](src/dimensions/employees/generator.py#L571-L574); decode at
  [employee_store_assignments.py:189-193](src/dimensions/employees/employee_store_assignments.py#L189-L193)
- **What:** staff keys are encoded `STAFF_KEY_BASE + StoreKey*STAFF_KEY_STORE_MULT + within_store_idx`
  with `STAFF_KEY_STORE_MULT = 1000`. The only guard is an int64-overflow check (max key ≈ 5e7, so it
  never fires). There is **no** guard that `within_store_idx < 1000`. If any store's `EmployeeCount`
  exceeds ~1000 (so `within_store_idx` reaches 1000+), the key spills into the next StoreKey's band:
  e.g. store 5 / staff #1001 → `40,006,001` == store 6 / staff #1 → **duplicate EmployeeKey** (breaks
  the employee PK and any FK join), and `_infer_home_store_key` decodes it to the **wrong store**
  (`(ek-40M)//1000`). Verified key-base spacing: managers `30M`, staff `[40M, 49,999,999]` (at idx≤999),
  online `≥50,010,001` — all disjoint *only while idx ≤ 999*.
- **Reachability:** default staffing is tens per store; requires `staffing_ranges`/store type configured
  to exceed 1000 staff at a single store. Unusual but unguarded and silent.
- **Proposed fix:** validate `staff_counts.max() < STAFF_KEY_STORE_MULT` (raise a clear error) — or
  size `STAFF_KEY_STORE_MULT` from the actual max staff-per-store — instead of the dead int64 check.

---

## Area: Geography dimension (`src/dimensions/geography.py`) — rating 9/10

Small, clean master-file loader. No findings. Key properties verified:
- `GeographyKey` is assigned sequentially **after** currency filtering
  ([geography.py:231](src/dimensions/geography.py#L231)), so keys depend on the configured
  currencies. Correctly handled: the version key includes the currency lists + master-file content
  hash ([:287-294](src/dimensions/geography.py#L287-L294)), and geography is upstream of
  stores/customers in the runner deps graph → changing currencies regenerates geography and cascades
  to dependents. Row order is the deterministic master-file order (no shuffle).
- Base currency (USD) always included; uncovered configured currencies warn (non-fatal).
- Fallback path (missing master) sets `Population=0`; the population-weighted samplers in stores
  (`_sample_geography_keys`) and customers (`_build_region_pools`) treat that as uniform via
  `np.maximum(pop, 1.0)` — degraded realism, not a bug. (Master file ships with the repo.)

---

## Area: Exchange Rates + Currency dimensions (`src/dimensions/exchange_rates/`, `src/integrations/fx_yahoo.py`) — rating 8.5/10

Solid. Triangulation math is correct (USD→X direct, X→USD inverse, A→B = rate_B/rate_A via
date inner-join), monthly aggregation is correct (`sort_values("Date")` before groupby makes
`"last"` the true end-of-month rate), and the daily output is validated (finite + positive, else
raises). The Yahoo integration caches real data (only gaps downloaded), never persists projected
values, projects future dates with sane compounding (`anchor*(1+drift)^(days/365.25)`, `drift>-1`
guarded), and **raises loudly** if a currency can't be sourced (no silent missing-currency). FX
dates always follow `defaults.dates` (gotcha #5, by design).

### FX-CUR-1 — explicit `currency.currencies` that omits an FX currency → cryptic crash
- **Severity:** Low (loud but unclear crash on misconfig; default path is safe)
- **Status:** confirmed
- **Where:** [currency.py:60-67](src/dimensions/exchange_rates/currency.py#L60-L67) vs FX key-join
  [exchange_rates.py:188-189](src/dimensions/exchange_rates/exchange_rates.py#L188-L189)
- **What:** when `cfg.currency.currencies` is set explicitly, the currency dim uses exactly that list
  (plus base USD). If it doesn't superset `from_currencies ∪ to_currencies`, the FX dim has rows for
  the missing currency but `code_to_key` lacks it → `.map(...).astype("int32")` hits NaN and raises
  `Cannot convert non-finite values to integer` — a confusing error far from the root cause. The
  default path (derive currency list from from∪to) is safe.
- **Proposed fix:** in currency runner, union explicit `currency.currencies` with the FX from/to
  lists (or validate superset with a clear error at config time).

### By-design caveat (not a bug) — FX is the one non-deterministic dimension
- `build_or_update_fx` pulls **live Yahoo data**, so a fresh checkout (no cached master) produces
  different rates depending on the run date, and future-date projections drift as real data
  accumulates and the anchor moves forward. The master parquet caches downloaded data, so once
  populated the dimension is stable. For reproducible datasets, ship/freeze the FX master. This is
  inherent to a real FX feed — flagging so it's a conscious choice, not a latent surprise.

---

## Area: Small dimensions (promotions, lookups, return_reasons, suppliers) — rating 9/10

All four reviewed; **no findings**. All are static/deterministic with correct, unique key
assignment and FK consistency.

- **promotions.py** — keys unique (1 = "No Discount" sentinel, rest 2..N by StartDate); strict
  `start < end` windows enforced via `_valid_window`; year-boundary clamping provably cannot invert
  a window (start.year < end.year ⇒ clamped start ≤ Dec31 < Jan1 ≤ clamped end). Deterministic.
- **lookups.py** (sales_channels / loyalty_tiers / customer_acquisition_channels) — static rows with
  override support; sales-channel keys 1–10 are the stable contract that defaults' `PHYSICAL_CHANNELS`
  /`DIGITAL_CHANNELS` sets and `promo_channel_group` depend on. Consistent in normal use; only custom
  key overrides could desync the hardcoded sets (advanced usage). `OpenTime`/`CloseTime` are "HH:MM"
  strings parsed consistently by sales `columns.py`. Minor: editing a hardcoded value without changing
  row count/columns won't trigger regen (dev-time staleness, not a data bug).
- **return_reasons.py** — deduped keys, sorted, derived from the same `defaults.RETURN_REASONS` that
  the returns fact's reason weights come from → FK-consistent by construction. **Watch-item for the
  facts review:** a custom `returns.reasons` config override must reach *both* the dimension and the
  returns-fact reason sampling (`State.returns_reason_keys`), else returns could reference reason keys
  absent from the dimension — verify the worker_cfg wiring when reviewing the returns fact.
- **suppliers.py** — dense unique `SupplierKey = [start_key, start_key+num)`; products dim references
  via mod-indexing into these keys (valid FK). Deterministic.

---

## Area: Facts — shared + budget

### Shared (`src/facts/shared/`) — clean
`base_accumulator`, `micro_agg_helpers` (correct `datetime64[M]` decompose), and `writers` are clean.
Note: the batch fact CSV writer (`write_fact_table`) uses **pandas default (minimal) quoting**, so
batch facts are properly quoted — unlike the sales path's `quoting_style="none"` (WORKER-1).

### Budget (`src/facts/budget/`) — rating 7.5/10
Micro-agg and accumulator are solid: float64 `bincount` weights (gotcha #15 correct), a bijective
flat-key encode/decode with overflow guard, decoded micro-aggs use **absolute** dim values so the
cross-chunk re-groupby merges correctly, and the returns→sales join is **within-chunk** → immune to
CHUNK-1. The engine's growth math (capped/blended with NaN-weight handling, scenario tiling/jitter
alignment, deterministic md5 jitter) is correct.

#### BUDGET-1 — Monthly budget can exceed the yearly total for categories missing months
- **Severity:** Low (only sparse categories; full-coverage categories are exact)
- **Status:** confirmed
- **Where:** [engine.py:344-360](src/facts/budget/engine.py#L344-L360)
- **What:** seasonal `MonthShare` is normalized to sum 1 per category **over the months present in
  actuals** (line 349-350), then the budget is expanded to all 12 months and absent months are
  `fillna(1/12)` (line 360). For a category missing K months, the 12 shares then sum to `1 + K/12`,
  so `sum(monthly budget) > yearly budget` — breaking the expected "monthly rolls up to yearly"
  invariant. Categories with full 12-month coverage (typical at realistic volumes) are exact.
- **Proposed fix:** re-normalize `MonthShare` **after** the 12-month expand + fillna (per
  Country/Category/BudgetYear/Scenario) so the 12 shares sum to 1.

#### BUDGET-2 — Returns micro-agg computed every chunk but never used; dead config
- **Severity:** Low (perf waste + misleading config; output is correct)
- **Status:** confirmed
- **Where:** `_maybe_returns_agg` runs per chunk
  ([task.py:856-858](src/facts/sales/sales_worker/task.py#L856-L858)), accumulated via
  `add_returns` ([sales.py:140](src/facts/sales/sales.py#L140)), but
  `BudgetAccumulator.finalize_returns()` ([accumulator.py:141](src/facts/budget/accumulator.py#L141))
  is **never called**; `compute_budget` ignores returns and channel entirely.
- **What:** with returns + budget both enabled, every chunk pays for a returns→sales join + bincount
  whose result is discarded. Also `BudgetConfig.digital_shift`, `physical_shift`, `mix_current_weight`,
  `mix_prior_weight`, `return_rate_cap`, and `report_currency` are declared (some read in
  `load_budget_config`) but never applied — `report_currency` implies an FX conversion that doesn't
  happen (budget amounts stay in the sales `NetPrice` denomination).
- **Proposed fix:** either wire returns/channel/report_currency into `compute_budget`, or stop
  computing/accumulating the returns micro-agg and remove the dead config fields.

### Inventory (`src/facts/inventory/`) — rating 8/10
Strong. The micro-agg is clean (bijective flat-key groupby via argsort+reduceat, warehouse-grain
rollup with unmapped-store drop+warn) and the engine is a careful vectorized month-loop:
stockout/days-OOS computed before the QoH clamp, the "lost order beyond buffer" case is unreachable
(buffer sized for max lead+jitter), draws are pre-generated (deterministic), and quarterly rollup
takes a correctly-ordered end-of-quarter `"last"`. The product_profile cross-write (gotcha #7b) is
idempotent, runs after products (sequential), and is config-hash-versioned — same safe pattern as
gotcha #7 (same watch-item: relies on `should_regenerate` keying on config, not output bytes).

#### INV-1 — Volume-based ABC recompute is distorted under product SCD2
- **Severity:** Low-Medium (correctness degradation; only when product SCD2 is enabled — non-default)
- **Status:** confirmed
- **Where:** [runner.py:81-124](src/facts/inventory/runner.py#L81-L124) (rank by `ProductKey`);
  [runner.py:155-160](src/facts/inventory/runner.py#L155-L160) (family ABC = last-version-wins)
- **What:** `_recompute_abc_from_demand` groups demand by `ProductKey`, but under SCD2 that's the
  **version-specific** key (sales emits per-version ProductKey). So a multi-version family's volume is
  **fragmented across its version keys**, so the 20/30/50 volume ranking treats one high-volume family
  as several lower-volume products → wrong A/B/C tiers. Then `_update_product_profile_abc` collapses
  version-keyed ABC to ProductID via `abc_by_pid[pid] = abc` in a loop — **last version processed wins**,
  so the family's broadcast ABC is arbitrary rather than its true total-volume tier. Correct when product
  SCD2 is off (one version ⇒ ProductKey≈ProductID), which is the default.
- **Proposed fix:** map demand `ProductKey → ProductID` (via product_profile's ProductKey/ProductID)
  and aggregate volume **per family** before ranking; assign one ABC per ProductID and broadcast.

#### Minor (not logged separately)
- `_update_product_profile_abc` replaces `ABCClassification` with `pa.large_string()`
  ([runner.py:168](src/facts/inventory/runner.py#L168)); if the original column was `string`, this
  changes the column type on overwrite (harmless for NVARCHAR/Power BI, but a silent schema drift).

### Wishlists (`src/facts/wishlists/`) — rating 7.5/10
Accumulator/micro-agg are trivial and clean (dedup purchased customer-product pairs). Selection
uses correct CDF+bisect sampling (global + per-subcategory), batched RNG, per-customer dedup via
`chosen_set`, and `items_per` is capped at `n_products` so the dedup fallback always finds a free
product. SCD2 `resolve_scd2_prices` uses the correct `sum(starts<=date)-1` pattern.

#### WISH-1 — Selection misc-RNG pool can overflow → latent IndexError
- **Severity:** Low-Medium (loud crash; only under high selection-collision conditions)
- **Status:** confirmed
- **Where:** [selection.py:183-184](src/facts/wishlists/selection.py#L183-L184) (`_misc` pool),
  refill at [:308-311](src/facts/wishlists/selection.py#L308-L311); consumers in the affinity
  50-retry [:270-280](src/facts/wishlists/selection.py#L270-L280) and default 200-retry
  [:293-300](src/facts/wishlists/selection.py#L293-L300) loops.
- **What:** the misc-random pool is refilled only at each item's end when `<60` remain, but a single
  item can consume up to `1 + 50 + 200 = 251` draws (conversion + affinity retries + default retries).
  If an item starts with 60–250 remaining and hits the worst-case retry path, `_misc[_mi]` indexes past
  the array → `IndexError`. Reachable when a customer's `chosen_set` covers most of its affinity
  subcategory pool (small pool / many items in one subcat), so picks keep colliding. Typical configs
  (large catalog, modest `max_items`) almost always pick on the first try (~3 draws/item) and never
  overflow.
- **Proposed fix:** check/refill `_mi` inside the retry loops, size the headroom to ≥251, or guard each
  `_misc[_mi]` access with a refill.

#### WISH-2 — Wishlist SCD2 price resolution is dead (keys by ProductKey, not ProductID)
- **Severity:** Low (price-on-wishlist accuracy only; graceful current-price fallback; SCD2 non-default)
- **Status:** confirmed
- **Where:** [scd2.py:35-37](src/facts/wishlists/scd2.py#L35-L37); fallback at
  [runner.py:225-230](src/facts/wishlists/runner.py#L225-L230)
- **What:** `build_scd2_price_lookup` decides "SCD2 is active" via `len(all_df) <= ProductKey.nunique()`,
  and maps history by `ProductKey`. But products SCD2 assigns a **unique ProductKey per version**
  (versions grouped by ProductID, gotcha #25), so `nunique == len` **always** → returns `None`, and
  wishlist prices always use the **current** ListPrice regardless of wishlist date. The historical-price
  code path never runs. (Sales does this correctly by keying on ProductID — see
  `_build_scd2_product_versions`.)
- **Proposed fix:** detect SCD2 and group versions by `ProductID` (like the sales builder), mapping
  current ProductKey → ProductID → version history.

### Complaints (`src/facts/complaints/`) — rating 9/10
Clean and well-built. Micro-agg dedups triples per chunk; row generation is fully vectorized with the
safe CDF+searchsorted+clip pattern for severity/channel/resolution, order-linking against each
customer's *own* orders, resolution-date clamping to `g_end`, and batched deterministic RNG.
`ComplaintKey` is assigned globally in one pass (`key_offset=0` on the merged arrays) → unique across
serial and parallel paths, with proper null-masking for non-order-linked SO/line and unresolved dates.

#### Downstream of CHUNK-1 (not a new bug) + fragile comment
- The accumulator **skips dedup** ([accumulator.py:29-37](src/facts/complaints/accumulator.py#L29-L37))
  asserting "SalesOrderNumbers are unique across chunks (chunk_idx × stride)". That rationale describes
  the **legacy** order-id stride scheme, not the production **day-based** scheme — and CHUNK-1 can break
  cross-chunk SO# uniqueness. The triples carry CustomerKey + LineNumber so distinct orders won't merge
  into identical triples, but complaints still inherit CHUNK-1's ambiguous-join issue (complaint→sales
  on SalesOrderNumber can fan out). Fixing CHUNK-1 resolves it; also update the comment.

---

## Facts summary

| Fact | Rating | Findings |
|---|---|---|
| Budget | 7.5 | BUDGET-1 (low), BUDGET-2 (low/perf) |
| Inventory | 8 | INV-1 (low-med) |
| Wishlists | 7.5 | WISH-1 (low-med), WISH-2 (low) |
| Complaints | 9 | — (inherits CHUNK-1 downstream) |
| Shared | — | clean |

No High-severity bugs in the facts. Recurring theme: **SCD2 version-key vs ProductID confusion**
(INV-1, WISH-2) — both mis-handle the unique-ProductKey-per-version scheme; products/customers SCD2
generators and the sales resolver get it right (key by ProductID), these two consumers don't. And
**CHUNK-1 keeps surfacing downstream** (returns, header, budget-join, complaints).

---

## Area: Versioning (`src/versioning/`) — rating 9/10. No findings.

`should_regenerate` is purely **config-hash + output-file-existence** based
([version_store.py:74-99](src/versioning/version_store.py#L74-L99)); it never compares parquet
content or mtime (the stored `parquet_mtime` is debug-only and never read). This **resolves both
cross-write watch-items**:
- **Gotcha #7 (stores↩warehouse):** warehouses overwriting `stores.parquet` changes content+mtime,
  but stores' `should_regenerate` sees unchanged config_hash → skip; WarehouseKey preserved, no
  needless regen.
- **Gotcha #7b (inventory↩product_profile):** products' version is keyed to `products.parquet`, so
  the ABC overwrite of `product_profile.parquet` is invisible to it; config_hash unchanged → skip;
  volume-ABC preserved.

Upstream changes propagate via per-dimension version signatures embedded in each `version_cfg`
(`_geography_sig`, `supplier_sig`, `_master_sig`, etc.) **plus** the runner dependency cascade.

---

## Area: Engine (`src/engine/`) — partial

`runners/pipeline_runner.py` and `config/config.py` (cross-section rules + scale distribution) are
clean:
- Trend-preset resolution → `State.models_cfg` → appearance injection ordering is correct; overrides
  are copy-on-write on a deep copy; config snapshots guard mid-run edits; promotions Hamilton scaling
  is correct. (Confirms the SM-2 mechanism: injection shares price *bands* but the product dim uses a
  single ending + deterministic snap while sales uses stochastic multi-ending.)
- Cross-section rules (gotcha #4) correctly disable **both** returns and complaints under
  `skip_order_cols + sales_output='sales'` (wishlists/budget unaffected — they need no order IDs); FX
  date keys stripped to force global-date coupling. `_distribute_scale` uses `setdefault` so
  section-level counts win over the `scale` block (documented precedence).

### QR-1 — Quality report can't detect CHUNK-1 (no SalesOrderNumber uniqueness / fact-to-fact FK check)
- **Severity:** Medium (meta — masks the highest-severity bug; not data corruption itself)
- **Status:** **fixed** — added a duplicate/null-PK check on `sales_order_header.SalesOrderNumber`
  (in `_check_nulls_and_duplicates`) and fact-to-fact FK checks for `sales_return` and `complaints`
  → `sales_order_header.SalesOrderNumber` (in `_check_referential_integrity`, nulls ignored).
  Tests in `tests/test_quality_report.py::TestSalesOrderIntegrity`. This is the detector for CHUNK-1.
- **Where:** [quality_report.py:520-556](src/engine/quality_report.py#L520-L556) (PK dup checks);
  FK check [:260-423](src/engine/quality_report.py#L260-L423) (fact→dim only)
- **What:** the duplicate-PK check covers only **dimension** PKs (customers/products/stores/promotions/
  employees/dates/geography/currency). `SalesOrderHeader.SalesOrderNumber` — the one fact table where
  SO# is a true PK — is **not** in the list, and the referential-integrity check only validates
  fact→dimension keys (not fact-to-fact links like returns/complaints → sales `SalesOrderNumber`). So a
  dataset with CHUNK-1 duplicate order numbers (and the resulting duplicate header rows / ambiguous
  returns joins) **passes the quality report green**. Note the report *would* catch EMP-1 (EmployeeKey
  dup check exists, line 525).
- **Proposed fix:** add a uniqueness check on `SalesOrderHeader.SalesOrderNumber` (when that table is
  emitted), and optionally a returns/complaints → SalesOrderHeader existence check. This both surfaces
  CHUNK-1 and gives a regression guard once it's fixed.

---

## (versioning minor notes)
Minor (not a finding): `_compute_hash` falls back to `str(obj)` (order-sensitive) for
non-JSON-serializable config — harmless since `version_cfg` dicts are JSON-native via `as_dict`/
`model_dump`. `version_checker.ensure_dimension_version_exists` backfills missing version files using
a *simplified* cfg_section whose hash won't match the dimension's real (rich) hash — so it errs toward
regeneration (safe), never toward using a stale parquet.

---

## Area: Sales facts (`src/facts/sales/`)

Reviewing sub-section by sub-section. Status of sub-areas:
- [x] `sales_models/` (quantity, pricing pipeline) — rating 9/10
- [x] `sales_logic/core/` (customer_sampling, orders, promotions, pricing, allocation, delivery) — rating 8.5/10
- [x] `sales_logic/` (globals, chunk_builder, columns) — rating 7/10 (one High-severity bug)
- [x] `sales_worker/` (init, returns_builder, io, task, schemas) — rating 8/10
- [x] `sales_writer/` (parquet_merge, delta, projection; encoding/utils skimmed) — rating 9/10
- [x] `sales.py` (orchestrator) — rating 9/10

### Sub-area: `sales_models/` — rating 9/10

Very solid. `quantity_model.py`: Poisson+1 floor, lognormal multiplicative noise (no
negatives), correct month indexing (`datetime64[M] % 12` → Jan=0), proper clamp.
`pricing_pipeline.py`: per-month inflation (consistent within a month), layered discount
constraints (max_pct → min_net → margin → clip), cents rounding, caches keyed by content
hash with determinism-preserving eviction. Inflation anchor verified consistent across
workers (global `State.date_pool`).

#### SM-1 — Margin re-fix bypasses the appearance grid
- **Severity:** Low (cosmetic)
- **Status:** confirmed
- **Where:** [pricing_pipeline.py:620-628](src/facts/sales/sales_models/pricing_pipeline.py#L620-L628)
- **What:** the final positive-margin safety net recomputes `disc = up - uc - 0.01`
  directly, *after* `_snap_discount`. So the handful of margin-violating rows carry
  discounts that are not on the configured appearance/step grid (e.g. `12.37` instead of
  a snapped `12.50`). No constraint violated; just grid inconsistency on a few rows.
- **Proposed fix:** re-snap `disc` to the grid after the margin fix, then re-clip net,
  or accept as-is (negligible).

#### SM-2 — Non-SCD2 mode: same product+month can have different UnitPrice per row
- **Severity:** Low (likely by-design; document the expectation)
- **Status:** open / needs intent confirmation
- **Where:** [pricing_pipeline.py:342-358](src/facts/sales/sales_models/pricing_pipeline.py#L342-L358)
  (stochastic rounding + per-row `rng.choice` ending)
- **What:** with product SCD2 off, inflation is per-month (consistent), but `_snap_unit_price`
  applies per-row stochastic rounding and a per-row random price ending. So two sales lines
  for the same product in the same month can carry different `UnitPrice`. Realistic, but
  surprising for BI that assumes a deterministic price-by-(product,date). SCD2-on mode
  preserves catalog prices and is immune.
- **Proposed fix:** none if intentional; otherwise snap per (product,month) rather than per
  row. Flagging so we decide deliberately.

#### SM-3 — Defensive fallbacks that would hard-error / drift if assumptions change
- **Severity:** Low (fragility; not live)
- **Status:** noted
- **Where:** quantity_model [_load_cfg:68](src/facts/sales/sales_models/quantity_model.py#L68)
  (raises if `models.quantity` is not a `Mapping`); pricing
  [_global_start_month_int:493-503](src/facts/sales/sales_models/pricing_pipeline.py#L493-L503)
  (per-chunk `min` fallback)
- **What:** both assume worker-side `models_cfg` sections are plain dicts and `date_pool`
  is always bound. Verified true today. If a future change passes Pydantic sub-models to
  workers, quantity hard-errors; if `date_pool` ever isn't bound, inflation anchor drifts
  per-chunk. Low risk, listed for the holistic pass.

### Sub-area: `sales_logic/core/` — rating 8.5/10

Strong. CDF/searchsorted uses the safe clamp pattern (orders.py), int64 ID math with an
explicit overflow guard, `line_num` cumsum-reset is correct, chunk-invariant macro weights
via stable seeds (allocation.py), float64 bincount weights in delivery.py (gotcha #15
avoided), correct searchsorted-membership in customer_sampling. All findings below are low.

#### CORE-1 — `_urgency_pick` loses expiry ordering in the all-undiscovered corner
- **Severity:** Low (narrow corner; weakens discovery prioritization)
- **Status:** confirmed
- **Where:** [customer_sampling.py:357-375](src/facts/sales/sales_logic/core/customer_sampling.py#L357-L375)
  used at [:523-526](src/facts/sales/sales_logic/core/customer_sampling.py#L523-L526)
- **What:** when `discover_n >= undiscovered.size`, `_urgency_pick` returns `keys.copy()`
  in original key order (not expiry-sorted). If that set then exceeds the participation
  cap `k`, `forced[:k]` takes the first `k` in key order — the comment claims urgency order
  is preserved, but in this branch it isn't, so near-expiry customers may be dropped before
  churn instead of prioritized.
- **Proposed fix:** in the `size >= keys.size` branch, still return in urgency order (or
  guard the `forced[:k]` slice to re-rank by remaining months).

#### CORE-2 — Line-count adjust loop can hard-fail with few orders + large `max_lines`
- **Severity:** Low (loud failure, not silent; rare config)
- **Status:** confirmed
- **Where:** [orders.py:281-295](src/facts/sales/sales_logic/core/orders.py#L281-L295)
- **What:** the 8-iteration remainder loop adds at most 1 line per chosen candidate per
  iteration. If a few orders carry large headroom (e.g. `max_lines_per_order` large,
  `order_count` small relative to `n`), 8 iterations may not absorb the remainder and it
  raises `SalesError`. Total capacity exists; only the iteration cap is the limit. Symmetric
  case exists in the `delta < 0` shrink loop (that one falls through to a `RuntimeError`).
- **Proposed fix:** loop until `remaining == 0` (capacity already proven sufficient) or
  distribute the residual in one weighted multinomial pass instead of capped single-adds.

#### CORE-3 — `_remove_rows_stochastic` can silently under-remove (total overshoot)
- **Severity:** Low (latent; abundant candidates make it practically safe)
- **Status:** confirmed
- **Where:** [allocation.py:86-124](src/facts/sales/sales_logic/core/allocation.py#L86-L124),
  caller returns without re-check at [:442](src/facts/sales/sales_logic/core/allocation.py#L442)
- **What:** unlike the additive path (which has loud guards), the negative-diff path calls
  `_remove_rows_stochastic` with an 8-iteration cap and **no post-check**. If it can't remove
  the full `need`, `rows_per_month.sum()` stays above `total_rows` silently. Reachable only
  if diff<0 survives the cap-redistribution (rare) and candidates are starved (very rare,
  since removal candidates hold `total_rows + |diff|` rows). Distinct from CORE-2 because
  this one is *silent*.
- **Proposed fix:** assert `rows.sum() == total_rows` before returning, or loop to
  completion (capacity is guaranteed).

#### CORE-4 — Channel-affinity promo filter silently disabled on length mismatch
- **Severity:** Low
- **Status:** confirmed
- **Where:** [promotions.py:101-102](src/facts/sales/sales_logic/core/promotions.py#L101-L102)
- **What:** `_has_ch_filter` requires `len(promo_channel_group) == P`. On any length
  mismatch it silently falls back to unfiltered uniform promo assignment — the channel
  correlation (#5) just disappears with no warning.
- **Proposed fix:** warn (or raise) when `channel_keys` is provided but
  `promo_channel_group` length ≠ P, rather than silently dropping the feature.

#### CORE-5 — `compute_prices` vestigial parameters
- **Severity:** Low (dead/misleading API; not live)
- **Status:** confirmed
- **Where:** [pricing.py:20-21](src/facts/sales/sales_logic/core/pricing.py#L20-L21)
- **What:** `price_pressure` and `row_price_jitter_pct` are accepted and documented but never
  applied; the sole caller ([chunk_builder.py:1756](src/facts/sales/sales_logic/chunk_builder.py#L1756))
  doesn't pass them. If someone wires config to these expecting an effect, nothing happens.
- **Proposed fix:** either implement them or remove the params + docstring lines.

### Sub-area: `sales_logic/` (globals + chunk_builder + columns) — rating 7/10

`globals.py` is clean. `chunk_builder.py` is the orchestrator and is mostly careful, but it
contains the most serious bug found so far (CHUNK-1).

#### CHUNK-1 — Duplicate `SalesOrderNumber` across chunks (day-based IDs + start-date clamp) ⚠️ HIGH
- **Severity:** **High** — corrupts a key column in large (multi-chunk) runs with order
  columns enabled. Breaks order-grain aggregations, distinct-order counts, and returns
  linkage (returns join on SalesOrderNumber).
- **Status:** **fixed** — within-day cursor is now derived from a stable sort
  (`_within_day_cursor` in `chunk_builder.py`) instead of `arange − first_index`, so it is the
  true 0-based within-day rank regardless of the post-sort customer-start clamp reordering. The
  overflow guard now checks cursor magnitude (the quantity that actually spills the per-chunk
  band), not raw per-day count. Tests: `tests/test_chunk_id_integrity.py` (unit-proves the
  stable-rank cursor + cross-chunk ID uniqueness, and that the old naive cursor over-counts on
  reordered input). Verified end-to-end with a multi-chunk `sales_output: both` run: header
  `SalesOrderNumber` was unique and matched the detail's distinct-order count exactly, and the
  new QR-1 checks passed green. (Empirical overflow remains hard to force under default presets —
  the 8×-rows `per_chunk_alloc` headroom absorbs within-month reordering — consistent with this
  never having been observed in the wild; the fix removes the mechanism unconditionally.)
- **Where:** [chunk_builder.py:1454-1490](src/facts/sales/sales_logic/chunk_builder.py#L1454-L1490);
  clamp at [:61-78](src/facts/sales/sales_logic/chunk_builder.py#L61-L78);
  insufficient guard at [:1469](src/facts/sales/sales_logic/chunk_builder.py#L1469)
- **Trigger conditions (all must hold):**
  1. `_use_day_ids` active — it is the **production default** (sizing at
     [sales.py:2129-2131](src/facts/sales/sales.py#L2129-L2131)).
  2. `skip_order_cols=False` (SalesOrderNumber emitted).
  3. `total_chunks > 1` (run larger than `chunk_size`, e.g. any 100M-row run = ~100 chunks).
  4. Customer lifecycle/discovery introduces customers in their first eligible month whose
     day-granular `EffectiveStartDate` is later than the sampled order date — i.e. essentially
     every realistic acquisition scenario.
- **Mechanism:** `build_orders` returns order dates **sorted ascending**, and the day-ID
  cursor math assumes that sort:
  `_cursor = arange(n_orders) - _fi[group]`, where `_fi` is each day's *first* index. But
  `_clamp_order_dates_to_customer_start` runs **after** the sort (line 1454) and pushes some
  early-day orders forward to later days **within the month**, breaking the sort. A single
  early-indexed order clamped onto a later day drags that day's `_fi` down to a small index,
  so the day's genuine orders get `cursor = their_index − small_fi`, which can vastly exceed
  the per-day order count. When `cursor >= per_chunk_alloc`, the ID
  `_d_off*stride + chunk_idx*alloc + cursor + 1` spills out of chunk `c`'s reserved band into
  chunk `c+1`'s band for the same day → identical SalesOrderNumber in two different chunks
  (different customers/dates).
- **Why the guard misses it:** [:1469](src/facts/sales/sales_logic/chunk_builder.py#L1469)
  checks `_dc.max()` (max **orders per day**) ≤ `per_chunk_alloc`, but the overflow is driven
  by **cursor magnitude** (index spread), not per-day count. `per_chunk_alloc` is only
  `8 × avg_orders_per_day_per_chunk`, so reordering inflation of a few× easily overruns it.
- **Not triggered when:** single-chunk runs (≤ `chunk_size`), or `skip_order_cols=True`.
  Within a single chunk IDs stay unique (per-day cursors are mutually distinct); the collision
  is strictly cross-chunk.
- **Proposed fix (decide in holistic pass):** after clamping, re-sort the order-level arrays
  by clamped OrderDate before computing day IDs (and re-expand to lines), **or** compute the
  within-day cursor by stable rank within each day group (robust to unsorted input) instead of
  `index − first_index`, **or** apply the clamp to OrderDate *before* `build_orders` assigns/
  sorts. Also strengthen the guard to check `max(cursor) < per_chunk_alloc`, not per-day count.

#### CHUNK-2 — `State.seal()` never called in production; CLAUDE.md gotcha #3 inaccurate
- **Severity:** Low (doc/design mismatch; no runtime failure)
- **Status:** confirmed
- **Where:** `seal()` defined at [globals.py:390-400](src/facts/sales/sales_logic/globals.py#L390-L400),
  only ever called in `tests/test_state.py`; `bind_globals` does **not** call it
  ([globals.py:407-443](src/facts/sales/sales_logic/globals.py#L407-L443)). Per-month mutation
  at [chunk_builder.py:1749](src/facts/sales/sales_logic/chunk_builder.py#L1749).
- **What:** CLAUDE.md gotcha #3 states "`bind_globals()` calls `seal()`" and that State is
  immutable after binding. In production it is never sealed, so `State.seen_customers = …`
  inside the month loop is legal (and relied upon). Two implications: (a) the documented
  immutability safety net is not actually active during generation; (b) discovery `seen` state
  persists **per worker process**, not globally — different workers discover independently.
- **Proposed fix:** either call `seal()` in worker init after binding (and stop mutating State
  in the loop — pass `seen` through a context), or update CLAUDE.md to reflect that seal is a
  test-only guard and discovery state is per-worker by design.

#### CHUNK-3 — Final-assembly "mixed" path drops null months instead of padding
- **Severity:** Low-Medium (latent; would hard-error or misalign if triggered)
- **Status:** confirmed (latent — not reachable with today's column set)
- **Where:** [chunk_builder.py:1933-1943](src/facts/sales/sales_logic/chunk_builder.py#L1933-L1943)
- **What:** when a column is produced in some months but `None` in others within one chunk, the
  mixed path concatenates only the non-null months' arrays and **skips** the null months
  (`continue`) rather than emitting `pa.nulls(rows_in_that_month)`. Result: a column shorter
  than `total_rows` and positionally misaligned. `pa.Table.from_arrays` would then raise on
  length mismatch (loud) — unless every other column is also null in those same months. Not
  reachable today because all per-month column presence is constant within a chunk, but it's a
  latent trap for anyone adding a conditionally-emitted extra column.
- **Proposed fix:** in the mixed path, for `b is None` append `pa.nulls(rows_for_that_month)`;
  track per-month row counts alongside the buffers so null months can be padded in order.

### Sub-area: `sales_worker/` — rating 8.5/10

`init.py` (worker setup) and `io.py` (writers) are careful. The clamp LUT
([sales.py:745-748](src/facts/sales/sales.py#L745-L748)) is built correctly (dense, day-
granular, INT64_MIN sentinel) — confirming CHUNK-1's trigger is real. Notably, ReturnEventKey
bands use a **structural** capacity (`chunk_size × max_lines × max_splits`,
[sales.py:2089-2094](src/facts/sales/sales.py#L2089-L2094)) that stays disjoint regardless of
reordering — the correct pattern that CHUNK-1's *statistical* `per_chunk_alloc` should have used.

#### RETURNS-1 — Split-return event dates not guaranteed non-decreasing within a group
- **Severity:** Low-Medium (only when `split_return_rate > 0`; default 0.0)
- **Status:** confirmed
- **Where:** [returns_builder.py:286-319](src/facts/sales/sales_worker/returns_builder.py#L286-L319)
  (comment at [:296](src/facts/sales/sales_worker/returns_builder.py#L296) claims non-decreasing)
- **What:** `base_lag` is drawn **independently per event** (line 292), then split events add a
  cumulative gap. Because each event has its own random base, event 2 (`ReturnSequence=2`) can
  land on an earlier `ReturnDate` than event 1 if `base_lag2 + gap < base_lag1`. The intended
  invariant (later sequence ⇒ later/equal date) is violated.
- **Proposed fix:** share one `base_lag` per parent line (broadcast within the group) and add
  cumulative gaps on top, so dates are monotonic by sequence.

#### RETURNS-2 — Logistics reason can leak to on-time orders via CDF boundary overwrite
- **Severity:** Low (tiny probability; only if last reason key is a logistics key + logistics_keys set)
- **Status:** confirmed
- **Where:** [returns_builder.py:325-334](src/facts/sales/sales_worker/returns_builder.py#L325-L334)
- **What:** after zeroing logistics reasons for on-time orders and renormalizing, the line
  `probs_ontime[-1] = 1.0 - probs_ontime[:-1].sum()` (a CDF boundary guard) **overwrites** the
  last slot. If the last reason key is itself a logistics key, it gets a small nonzero
  probability for on-time orders — the exact thing the block tries to prevent.
- **Proposed fix:** apply the boundary correction to the largest non-zero / non-logistics slot,
  or renormalize without forcing the last element.

#### WORKER-1 — CSV `quoting_style="none"` is unquoted output
- **Severity:** Low (fragility; safe today)
- **Status:** confirmed
- **Where:** [io.py:193-197](src/facts/sales/sales_worker/io.py#L193-L197)
- **What:** sales/returns CSV is written with no quoting, matching the SQL Server BULK INSERT
  contract. Safe today because the only string column is `DeliveryStatus` (controlled vocab,
  no delimiters). But any future string column with an embedded comma/quote/newline would
  silently corrupt row alignment in CSV output.
- **Proposed fix:** if a free-text string column is ever added to a CSV-exported table, switch
  to `quoting_style="needed"` and update the SQL import to expect quoted fields.

### Sub-area: `sales_writer/` — rating 9/10

`parquet_merge.py` is a careful, lossless concatenation (per-row-group writes, layered
schema reconciliation, chunks preserved on failure, `cast_safe=True` so int downcasts raise
rather than truncate). `delta.py` uses `first=(i==0)` overwrite/append correctly and refuses
to delete the parts folder unless `_delta_log` exists. Merge does not dedup — correct, but
note it faithfully propagates CHUNK-1 duplicates into the merged output.

#### WRITER-1 — back-compat `_project_table_to_schema` hardcodes `cast_safe=False` (silent truncation)
- **Severity:** Low (latent; not on the live merge path)
- **Status:** noted
- **Where:** [projection.py:72-74](src/facts/sales/sales_writer/projection.py#L72-L74), used by
  the back-compat [parquet_merge.py:416-433](src/facts/sales/sales_writer/parquet_merge.py#L416-L433)
- **What:** the underscore alias projects with `safe=False`, which silently truncates on
  narrowing casts (e.g. int64→int32). The live merge path uses `cast_safe=True` (safe), so this
  only bites if the back-compat `_read_row_group_projected` is revived — and it would interact
  badly with the int32 SO# question (SCHEMA-1) by silently wrapping oversized order numbers.
- **Proposed fix:** default the alias to `cast_safe=True`, or remove it if no longer imported.

### Sub-area: `sales.py` (orchestrator) — rating 9/10

Solid glue. Lossless per-table parquet merge (every chunk's row groups written), correct chunk
scheduling, and two correctness highlights: `build_weighted_date_pool` applies the CDF last-
element clamp (gotcha #16), and the SCD2 version builders
([_build_scd2_product_versions](src/facts/sales/sales.py#L243),
[_build_scd2_customer_versions](src/facts/sales/sales.py#L328)) lexsort versions, pad with
INT64_MAX (never selected), and clamp the first version start to 0 — so the chunk builder's
`sum(starts <= D) - 1` resolves the right slot. Each chunk emits exactly its batch, so total
row count is preserved end-to-end.

#### ORCH-1 — `total_rows` guard/warning mis-thresholded (see SCHEMA-1)
- **Severity:** Medium (same root issue as SCHEMA-1)
- **Status:** **fixed** (with SCHEMA-1) — the early `total_rows > 1.07B` warning is removed; a
  correctly-sized warning now fires from the real worst-case ID (`(day_span+1)*day_stride`) after
  day-ID sizing in `sales.py`.
- **Where:** [sales.py:2065-2069](src/facts/sales/sales.py#L2065-L2069)
- **What:** warns only when `total_rows > 1.07B` and claims "SalesOrderNumber will use int64",
  but the day-ID space is ~8× total_rows and the builder hard-casts to int32 and raises at
  ~268M rows. So 268M–1.07B-row runs get no warning, no int64, and crash. Fold into the
  SCHEMA-1 fix (size the threshold to the real ID space and actually emit int64 day-IDs).
- **Minor (not logged separately):** `build_weighted_date_pool` line 516 `weights[-1] = 1 -
  sum(rest)` can give the last calendar day a residual probability even if it was a blackout
  day — same class as RETURNS-2, but a single day with negligible weight.

#### Downstream note (not a new bug)
- CHUNK-1's duplicate SalesOrderNumber **amplifies into returns**: returns are built per chunk
  and join back on `(SalesOrderNumber, SalesOrderLineNumber)`. Cross-chunk SO# collisions make
  that join ambiguous (fan-out / double-counted returns). Fixing CHUNK-1 resolves this.

#### SCHEMA-1 — `SalesOrderNumber` int64 promotion ignores the ~8× day-ID multiplier (effective int32 cap)
- **Severity:** Medium (loud crash on very large runs; dead promotion logic; worsened by CHUNK-1)
- **Status:** **fixed (parquet/generation facet; SQL DDL deferred)** — a single `_order_id_int64`
  decision is computed in `sales.py` from the true worst-case ID `(day_span+1)*day_stride` (~8×
  total_rows) and threaded via `worker_cfg` → `build_worker_schemas` (drives `order_num_type` for
  sales/detail/header/returns) and → `State.order_id_int64` (the chunk builder emits an int64 SO#
  array instead of unconditionally casting to int32). `returns_builder` now mirrors the detail's
  SalesOrderNumber dtype (reads int64-safe, builds output + empty tables to match) so returns no
  longer silently truncate. Tests: `TestBuildWorkerSchemas::test_order_id_int64_*`,
  `TestBuildSalesReturns::test_int64_*`. Verified end-to-end (forced int64 run: header/detail/return
  all int64, header SO# unique = detail distinct, returns ⊆ header). **Still deferred to the SQL
  pass:** widening `SalesOrderNumber` from `INT` to `BIGINT` in the generated SQL DDL
  (`static_schemas.py` / SQL packagers) so CSV→SQL import doesn't overflow on >268M-row runs.
- **Where:** [schemas.py:130](src/facts/sales/sales_worker/schemas.py#L130);
  builder hard-casts int32 at [chunk_builder.py:1482-1490](src/facts/sales/sales_logic/chunk_builder.py#L1482-L1490);
  sizing at [sales.py:2129-2131](src/facts/sales/sales.py#L2129-L2131)
- **What:** the schema promotes `SalesOrderNumber` to int64 only when `total_rows > INT32_MAX/2`
  (~1.07B). But the day-based ID scheme produces values up to **~8 × total_rows** (because
  `per_chunk_alloc` carries an 8× safety factor and `day_stride = per_chunk_alloc × total_chunks`).
  So IDs hit the int32 ceiling (2.15B) at **total_rows ≈ 268M**, far below the promotion
  threshold. Worse, the builder hard-casts day IDs to int32 and *raises* on overflow
  ([:1482](src/facts/sales/sales_logic/chunk_builder.py#L1482)), so the int64 promotion never
  takes effect for the production day-ID path. Net: runs above ~268M rows with order columns
  crash with `SalesError: Day-based SalesOrderNumber would overflow int32`. (CHUNK-1's cursor
  inflation pushes this overflow even earlier.)
- **Proposed fix:** size the promotion to the actual ID space (`~8 × total_rows`, e.g. promote
  when `8*total_rows > INT32_MAX/2`), and make the builder emit `int64` day IDs when the schema
  is int64 instead of unconditionally casting to int32.

#### TASK-1 (downstream of CHUNK-1, not a new bug) — duplicate `SalesOrderHeader` rows
- **Severity:** inherits CHUNK-1 (High)
- **Where:** [task.py:870](src/facts/sales/sales_worker/task.py#L870),
  header groupby at [task.py:632-656](src/facts/sales/sales_worker/task.py#L632-L656)
- **What:** `build_header_from_detail` groups by `SalesOrderNumber` **within a chunk**. Cross-chunk
  SO# collisions (CHUNK-1) therefore produce **duplicate SalesOrderHeader rows** (duplicate
  primary key) in the merged header table. The optional `validate_header_invariants` check only
  validates within-chunk consistency, so it does **not** catch the cross-chunk duplication.

#### TASK-2 — Duplicated SalesChannelKey/TimeKey sampling logic (maintainability)
- **Severity:** Low (smell, not a bug)
- **Status:** noted
- **Where:** `columns.py` vs [task.py:304-540](src/facts/sales/sales_worker/task.py#L304-L540)
- **What:** hour-weight/channel/TimeKey sampling is implemented twice (chunk-builder extension
  point and a task.py fallback). They're parallel and currently consistent (task.py path is a
  guarded no-op when chunk_builder already produced the columns), but two copies risk silent
  divergence over time.
- **Proposed fix:** consolidate on one implementation imported by both.
