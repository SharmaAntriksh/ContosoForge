# Dates Package Refactor Plan

Refactor of `src/dimensions/dates/` (runner, generator, calendar, iso, fiscal,
weekly_fiscal, columns, helpers, time). Driven by the 2026-07 review — the third
in the series after employees and customers.

**Context:** this is the healthiest of the three packages. It was already split
into one-module-per-calendar-system, the `DATES-2`/`DATES-3` fixes carry
excellent rationale comments, and the weekly-fiscal math (arithmetic period
boundaries instead of groupby clipping, vectorized year assignment) is genuinely
good. The refactor is therefore smaller and different in character: the problems
are **duplicated sources of truth and contradictory defaults at the seams**, not
god-functions or broken randomness (the package consumes no RNG at all — which
also makes byte-identity verification trivial).

Same discipline as the other plans: behavior-preserving phases proven by
byte-identity, behavior-changing phases behind a version bump with regression
tests. Never mixed.

---

## Ground rules

1. **Byte-identity gate.** Phases marked *(identical)* must produce byte-identical
   `dates.parquet` and `time.parquet` across the Phase 0 fixture matrix (default
   config + exotic config). No RNG in this package — any diff is a real diff.
2. **One concern per commit.** Mid-phase discoveries go to the Standing TODO.
3. **No phase labels in code comments or commit messages.** Existing `DATES-N`
   markers are grandfathered.
4. **The output contract is frozen.** No column adds/renames/removals in refactor
   phases. The two decision-gated exceptions (holiday support, the
   `FiscalMonthName` duplicate) are explicitly parked in Phase 5.
5. **Schema-generation coupling.** `static_schemas.py` builds the dates SQL/TMDL
   column list *independently* of `columns.py`. Until Phase 2 unifies them, any
   change to either must update both — and Phase 0 adds the test that finally
   enforces this mechanically.

### Consumers (blast radius)

Uniquely for this repo, **no fact or dimension reads `dates.parquet`** — the
facts do their own month arithmetic from config dates. The blast radius is the
packaging layer:

| Consumer | What it uses | Coupling |
|---|---|---|
| `src/utils/static_schemas.py` | its own parallel dates column resolver + `_WF_INTERNAL_COLS` | duplicate source of truth |
| SQL DDL + BULK INSERT generators | static_schemas column list (positional loads for CSV) | parquet↔DDL column-set match |
| Power BI TMDL packaging | static_schemas + `get_date_rename_map` (spaced names) | rename-map completeness |
| Sales fact `TimeKey` | `time.parquet` minute grain (int16 SMALLINT FK, CLAUDE.md 5.5) | key range 0..1439 |
| `src/engine/runners/dimensions_runner.py` | registration + `inject_global_dates` | regen triggers |
| `tests/test_dates.py` (686 lines) | direct function calls | signatures |

---

## Phase 0 — Guardrails (no production code changes)

- **Golden snapshots** (`tests/test_dates_snapshot.py`) over a fixture matrix:
  1. default config (calendar+fiscal on, iso off, weekly fiscal default);
  2. exotic config (`fiscal_start_month: 7`, weekly `454 Nearest`,
     `type_start_fiscal_year: 0`, `include.iso: true`, `spaced_column_names: true`,
     `buffer_years: 0`);
  3. a 53-week-year window (weekly type `Last`, range chosen so a 53-week FW year
     falls inside).
  SHA-256 of parquet-normalized frames for `dates.parquet` + `time.parquet`.
- **The sync test** (the important one): for each fixture config, assert
  `resolve_date_columns(cfg)` == the static_schemas dates column list == the
  actual parquet columns, and that every resolved column has a `_SPACED_NAMES`
  entry. This mechanically enforces what the `NOTE:` comment in
  static_schemas.py:1027 asks humans to remember — and it will immediately
  document the `include.iso` fallback divergence (pipeline-defused, see Phase 2).
- **Property tests**: `DateKey` unique and contiguous with the daily range;
  `CalendarWeekIndex`/`ISOYearWeekIndex`/`FWWeekIndex` increase by exactly 1 at
  week boundaries; every offset column is 0 on the `as_of` row; FW year bounds
  partition the timeline with no gaps/overlaps (successive `FWStartOfYear` =
  previous `FWEndOfYear` + 1d); 53-week years get `FWQuarterDays == 98` in Q4;
  `time.parquet` has exactly 1440 rows, `TimeKey` 0..1439.
- **Bug-pinning tests** (strict xfail, fixing phase in reason):
  1. dict-shaped config without `include.iso` → `resolve_date_columns` and the
     static_schemas resolver return the same column set (currently: `False` vs
     `True` fallbacks diverge);
  2. `add_weekly_fiscal_columns` with an `as_of` not present in the frame →
     offsets are arithmetically correct, not all-zero (currently the zeros
     fallback — the exact pattern DATES-2 fixed in `fiscal.py`);
  3. changing dates *code output* regenerates existing outputs (currently the
     version key has no schema version at all — pin as an assertion on the
     version-key contents).

**Acceptance:** suite green (or strict-xfail) on `main`, zero production diffs.

---

## Phase 1 — Truth & trivia *(identical)*

- Remove the unused `warn` import in `runner.py`.
- Fix docstrings that drifted (e.g. `iso.py`'s "ISOWeekStartDate is NOT yet
  present" phrasing; `columns.py` module comment referencing "dates.py" which no
  longer exists).
- Annotate `run_dates(cfg: Dict)` → `AppConfig`; same for `run_time_table`.
- Document the `_override` backdoor in `_normalize_override_dates` at its
  *callers* (who injects it and why), or delete it if a grep shows nothing sets
  it — verify first; deletion only if provably dead.

**Acceptance:** all snapshots unchanged.

---

## Phase 2 — One source of column truth *(identical for pipeline configs)*

**Goal:** adding a dates column currently requires touching five places
(generator module, `resolve_date_columns` group lists, `_SPACED_NAMES`,
static_schemas group lists + SQL types, TMDL via static_schemas) — and a column
generated but not listed silently vanishes at `df = df[cols]`. The two resolvers
have already drifted (`include.iso` fallback: `False` in columns.py, `True` in
static_schemas.py — defused in the pipeline only because
`DatesIncludeConfig.iso = False` always materializes the key).

- Build a **single column registry**: one table of
  `(name, group, spaced_name, sql_type)` per dates column. Location:
  `static_schemas.py` (already the cross-package schema authority, and dates →
  static_schemas is the existing import direction via `_WF_INTERNAL_COLS`; the
  reverse would create a util→dimension dependency).
- `columns.py` derives `resolve_date_columns` group lists and `_SPACED_NAMES`
  from the registry; static_schemas derives its dates schema and
  `DATE_COLUMN_GROUPS` from the same registry. Both `include.*` fallbacks read
  one shared default table (which also kills the divergence structurally —
  Phase 0 xfail #1 flips).
- Add the inverse guard the runner lacks: after generation, warn (or fail in
  tests) on columns *generated but not exposed* — today's check only catches
  requested-but-missing.
- Delete the `NOTE: must be updated to match` comment; the sync test replaces it.

**Acceptance:** snapshots unchanged; sync test now derives both sides from one
registry; a deliberate "add a fake column to the registry" test shows exactly
one place needed the edit.

---

## Phase 3 — One WeeklyFiscalConfig, one boundary *(identical)*

**Goal:** two classes named `WeeklyFiscalConfig` exist — the frozen dataclass in
`weekly_fiscal.py` (default `enabled=False`) and the Pydantic model in
`config_schema.py` — and the runner hand-copies fields between them through a
*third* set of inline defaults (`enabled=True`), in two nearly identical
branches (dict-shaped vs model-shaped). `_wf_is_enabled` carries a fourth
default path (bool | dict | Mapping). Whether weekly fiscal is on by default
depends on which layer you ask.

- **One normalizer at the runner boundary**: a single
  `resolve_weekly_fiscal_cfg(include_block) -> WeeklyFiscalConfig` that accepts
  bool / dict / Pydantic model / None and returns the frozen dataclass. All
  shape-shimming lives there; everything below the runner takes the dataclass
  only. `_wf_is_enabled` becomes a trivial `cfg.enabled` (its bool/dict
  tolerance moves into the normalizer, where `columns.py` also gets its answer).
- **One default source**: the Pydantic `WeeklyFiscalConfig` in `config_schema.py`
  is the authority; the dataclass mirrors it via a construction-time assert in
  tests (field names + defaults equal), or better, the dataclass defaults are
  *removed* (all fields required) so the normalizer is the only place defaults
  are applied. Pipeline default stays **enabled** (current runner behavior —
  byte-preserving); the dataclass's misleading `enabled=False` default dies.
- Collapse the runner's duplicated dict/model construction branches into one
  call to the normalizer.

**Acceptance:** snapshots unchanged (pipeline default was already
enabled-by-runner); grep-gate: no `getattr(wf_block, ...)`/`wf_block.get(...)`
outside the normalizer; `generate_date_table(weekly_cfg=None)` behavior
documented explicitly (it is the *API* default and may stay disabled — state it,
test it).

---

## Phase 4 — Correctness & policy fixes *(behavior — introduce `schema_version: 1` in the version key)*

Each item = one commit + one test (Phase 0 xfails flip here).

1. **Add `schema_version` to the dates version key.** Dates is the only
   dimension whose version key contains *no code-version component* — every
   shipped output change (the alias-removal wave, DATES-2, DATES-3) silently
   never reached existing outputs until an unrelated config edit. With CSV
   output this is not cosmetic: a stale parquet + freshly generated DDL from
   static_schemas mismatches positional BULK INSERT. Adding the key forces a
   one-time regen for all users (release-note it) and gives future fixes a
   landing mechanism. Do the same for `time` if `_run_lookup_dim`'s key lacks
   one (verify at implementation).
2. **Arithmetic weekly-fiscal offsets.** Replace the locate-`as_of`-row +
   all-zeros fallback (weekly_fiscal.py:393-409) with direct arithmetic from
   `as_of` (FWWeekIndex from the fixed 1900 reference; month/quarter indices via
   the already-built year-bounds map) — the same fix DATES-2 applied to
   `fiscal.py`, finishing the job. Unreachable-in-pipeline, so pipeline
   snapshots should not change — but it is a behavior change for direct API
   callers, so it lands in this phase, not Phase 3.
3. **Unify the inverted-dates policy.** `_require_start_end` silently swaps
   start/end with a warn; `generate_date_table` raises for the same condition;
   customers and employees raise. Align on **raise** (`DimensionError` with the
   offending values). Anyone relying on the swap was hiding a config bug.
4. **`CalendarWeekNumber` ≥ 54 documentation-or-fix decision**: the Sunday-based
   partial-first-week convention can yield week 54 in a leap year starting
   Saturday. It is arithmetically consistent — document the convention in the
   registry description rather than change it (changing would be a data-visible
   break for zero benefit). Test the 2000-01-01 case explicitly.

**Acceptance:** Phase 0 xfails #2 #3 flip; snapshots regenerated once; SQL
import smoke test (CSV path) green.

---

## Phase 5 — Parked decisions & backlog *(each decision-gated, separately sized)*

Not refactoring — real feature/product decisions surfaced by the review. Do not
bundle into the phases above.

- **Holiday calendar support.** `IsBusinessDay`/`NextBusinessDay`/
  `PreviousBusinessDay` treat only weekends as non-business. A configurable
  holiday list (per-country or fixed corporate calendar) would make the
  business-day columns honest. Schema addition (`IsHoliday`, `HolidayName`) —
  needs the registry (Phase 2) done first, plus static_schemas/TMDL updates.
- **`FiscalMonthName`/`FiscalMonthShort` are byte-identical duplicates** of
  `MonthName`/`MonthShort`. Keep (BI convenience when only fiscal columns are
  shown) or drop (schema removal, breaks consumers). Recommendation: keep,
  document in the registry as intentional duplicates.
- **Batch the column assembly in `calendar.py`/`fiscal.py`/`iso.py`** the way
  `weekly_fiscal.py` already does (single concat) — kills the pandas
  fragmentation warning its own sibling module documents avoiding. Cosmetic;
  byte-identical; do it opportunistically with Phase 2's registry rewiring.
- **`time.py`'s import of `_run_lookup_dim`** (private cross-module) — either
  promote the helper to public in `lookups.py` or leave; note only.

## Explicit non-goals

- No changes to week/fiscal conventions (Sunday-based calendar weeks,
  partial-week-1 numbering, DAX-compatible weekly fiscal boundary logic).
- No column renames or removals; spaced-names feature stays as-is.
- No holiday support inside the refactor phases (Phase 5 decision only).
- `time.py` logic untouched (it is fine).

## Sequencing & effort

| Phase | Type | Size | Depends on |
|---|---|---|---|
| 0 Guardrails | tests only | M | — |
| 1 Truth & trivia | identical | XS | 0 |
| 2 Column registry | identical | M | 0 |
| 3 One WF config | identical | S–M | 0 |
| 4 Correctness & policy | behavior | S | 2, 3 |
| 5 Parked decisions | per-item | — | 2 (for schema items) |

Total effort is roughly one-third of the employees plan and one-sixth of the
customers plan. Phases 0–4 are the complete refactor; Phase 5 is a decision
backlog. Phases 2 and 3 are independent and can be done in either order.

## Standing TODO (bugs found later go here, not into unrelated phases)

- (empty)
