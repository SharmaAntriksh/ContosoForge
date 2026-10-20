# ContosoForge

Analytics-ready retail data generator (dimensional model, inspired by ContosoRetailDW). Produces deterministic, idempotent datasets for BI, analytics, and data engineering.

## Skills (load the relevant one before working in that area)

Detailed, area-specific knowledge lives in on-demand skills under `.agents/skills/` so this file stays a lean map. Load the skill that matches your task — its body carries the deep detail; the "danger index" below keeps only the one-line tripwire.

| Skill | Load it when you're… | Holds the detail for |
|---|---|---|
| **generate-dataset** | running/tuning generation: rows, customers, dates, trend preset, catalog, output format | CLI flags, dual-config, presets, scaling, catalog |
| **sales-internals** | editing anything under `src/facts/sales/` | the determinism/parallelism contract + numeric tripwires |
| **config-schema** | adding/changing a config field, or reading `cfg`/`models_cfg` | Pydantic spine, normalizer, coercion helpers |
| **web-layer** | editing anything under `web/` | thread safety, route versioning, CORS/security/limits |
| **sql-and-dialects** | editing `src/tools/sql/` or generated SQL | dialect classes, escaping, import runners |

The `.agents/skills/` files are the canonical repo skills. `.claude/skills/` mirrors
those files for Claude discovery; update both copies when changing a skill.
`CLAUDE.md` imports this file so the shared repo guidance has one source.

## Tech Stack

- **Language:** Python 3.13+ (`requires-python = ">=3.13"` in `pyproject.toml`)
- **Web UI:** FastAPI backend (`web/api.py` + `web/routes/`) + React SPA (`web/frontend/`)
- **Data:** pandas, numpy, pyarrow, deltalake
- **Config:** PyYAML, pydantic
- **Other:** yfinance (FX rates), multiprocessing (parallel sales)

## Running Python (agents: read this first)

This project targets **Python 3.13** and runs from a local **`.venv`** (created by
`scripts/create_venv.ps1`, deps locked via uv). In a non-interactive/agent shell the
venv is never "activated" (shell state doesn't persist between tool calls), and a bare
`python` may resolve to a **different** interpreter that lacks the deps (e.g. a global
3.14 without `pytest` → `No module named pytest`). **Invoke the venv interpreter
directly:**

```bash
.venv/Scripts/python.exe -m pytest        # Windows (this repo's primary platform)
.venv/bin/python -m pytest                # POSIX
```

Run commands from the repo root. For a fresh checkout or missing test tools:

```powershell
uv sync --locked --extra dev
```

Plain `uv sync` installs both database Python drivers (`pyodbc` and
`psycopg[binary]`); `sql` and `postgres` extras are compatibility aliases.
`pytest`, `httpx`, and `ruff` are in the `dev` extra. SQL Server also needs a
separately installed Microsoft ODBC driver. Reuse the venv interpreter after
syncing rather than relying on activation or bare `python`/`pytest`.

Read and write repo text explicitly as UTF-8; Windows Python may otherwise use
cp1252. When testing PowerShell scripts, check both Windows PowerShell 5.1
(`powershell.exe`) and PowerShell 7 (`pwsh`) when available; native stderr handling
differs. See `tests/test_powershell_driver_preflight.py`.

Human contributors can activate the venv — see README.md for full setup.

## Commands

```powershell
# Run generation (CLI) — full flag reference lives in the generate-dataset skill
.venv/Scripts/python.exe main.py --format parquet --sales-rows 1000000
.venv/Scripts/python.exe main.py --format csv --customers 5000 --sales-rows 100000
.venv/Scripts/python.exe main.py --format deltaparquet --workers 8

# Useful flags
.venv/Scripts/python.exe main.py --dry-run                        # validate config, no generation
.venv/Scripts/python.exe main.py --only dimensions                # generate dims only
.venv/Scripts/python.exe main.py --only sales                     # generate sales only (dims must exist)
.venv/Scripts/python.exe main.py --regen-dimensions products      # force-rebuild specific dim
.venv/Scripts/python.exe main.py --regen-dimensions all           # force-rebuild all dims
.venv/Scripts/python.exe main.py --refresh-fx-master              # top up FX rates via Yahoo Finance

# Post-generation tools
.venv/Scripts/python.exe scripts/optimize_delta.py "generated_datasets/..."   # compact small delta files
.venv/Scripts/python.exe scripts/optimize_parquet.py "generated_datasets/..."  # re-compress parquet

# Web UI
.venv/Scripts/python.exe -m uvicorn web.api:app --port 8502
```

## Architecture

Two-stage pipeline: **Dimensions** (sequential, dependency-aware) then **Facts** (parallel, multi-worker).

```
config.yaml + models.yaml --> Pipeline Runner
  |-- Dimensions Stage (sequential)
  |   generates ~22 dimension tables (customers, products, stores, dates, plans, etc.)
  |-- Sales Stage (parallel via multiprocessing pool)
  |   |-- sales transactions (chunked, imap_unordered)
  |   |-- sales returns (optional)
  |   |-- budget forecasts (streamed accumulator)
  |   |-- inventory snapshots (streamed accumulator)
  |   |-- customer wishlists (streamed accumulator)
  |   |-- customer complaints (streamed accumulator)
  |-- Package Output (final timestamped folder with dims, facts, SQL, Power BI project)
```

## Code map

| Area | Entry points / guidance |
|---|---|
| CLI and orchestration | `src/cli.py`, `src/engine/runners/` |
| Config validation | `src/engine/config/`, `config-schema` skill |
| Dimensions | `src/dimensions/`, dependency/version cascade in `dimensions_runner.py` |
| Sales | `src/facts/sales/`, `sales-internals` skill |
| Other facts | `src/facts/{budget,inventory,wishlists,complaints,shared}/` |
| Output and Power BI | `src/engine/packaging/`, `src/engine/powerbi_packaging.py` |
| SQL and live imports | `src/tools/sql/`, `scripts/sql/`, `sql-and-dialects` skill |
| Web backend and SPA | `web/`, `web-layer` skill; frontend build in `web/frontend/README.md` |
| Published schemas | `src/utils/static_schemas.py`, SQL roster `src/tools/sql/schema_model.py` |
| Tests and fixtures | `tests/README.md`, `tests/sales_gen.py`, `tests/_snapshots/`, `tests/golden/` |
| User-facing configuration and semantics | `docs/CONFIG_GUIDE.md`, `docs/MODELS_GUIDE.md`, `docs/ANALYTICAL_SEMANTICS.md` |

For dimension changes, check the published schema and version key as well as the
generator. Dates columns are registry-driven in `static_schemas.py`; the date
resolver, DDL, and Power BI must agree (`tests/test_dates_snapshot.py`). Changes to
store/employee assignments must preserve effective-date coverage and published
workforce contracts (`tests/test_temporal_contracts.py`,
`tests/test_workforce_published_contract.py`, `tests/test_store_warehouse_causality.py`).

## Config & Generation (summary — full detail in the generate-dataset skill)

Two config files drive everything:

- **`config.yaml`** — *shape & scale*: row counts, entity counts, date ranges, output format, parallelism, feature toggles. **Overridable by CLI** (CLI flags > config.yaml, one-time, not persisted). Sections: `scale`, `defaults`, `sales`, `returns`, `products`, `customers`, `stores`, `employees`, `dates`, `exchange_rates`, `budget`, `inventory`, `subscriptions`, `wishlists`, `complaints`. Validated by the strict normalizer registry in `src/engine/config/config.py`.
- **`models.yaml`** — *behavior & business shape*: demand curves, pricing, quantity, markdown, returns, and the **trend preset** (`models.macro_demand.trend`) — the single source of business shape. **Not CLI-overridable**; edit directly or via the web UI. Validated through Pydantic (`ModelsConfig.from_raw_dict()` → `ModelsInnerConfig`).

For the full CLI flag table, the 15 trend presets, the customers↔rows scaling guidance, and product-catalog selection, **load the generate-dataset skill**.

## Critical Gotchas (danger index)

One line per tripwire. Where a skill owns the detail, it's tagged `→ skill`; load that skill before working in the area. The homeless ones (no skill) keep their full text here.

### Sales fact — load **sales-internals** before editing `src/facts/sales/`
- **State is per-worker, bound once, read-only by convention** — never add cross-chunk mutable state to `State`; put worker scratch in module globals reset in `init_sales_worker`. → sales-internals
- **Worker-lifetime caches must be keyed by every variable their value depends on** (the calendar-vs-absolute-month CDF bug). → sales-internals
- **Per-month plan is global; chunks only shard the order-id index space** — no per-chunk allocation; customer sampling uses the plan RNG, not the chunk RNG. → sales-internals
- **Correlated-fact features change per-line measures/identity only**, never row counts or customer assignment; cross-pass latents are hash-seeded on emitted keys. → sales-internals
- **Posted `UnitPrice`/`UnitCost` is deterministic per (product, month)**; only `DiscountAmount`/`NetPrice` vary per line. → sales-internals
- **int32 overflow:** ID math needs `int64` intermediates before casting back. → sales-internals
- **`np.bincount(weights=...)` requires `float64`** (int8 overflows >127 elems). → sales-internals
- **Clamp `cdf[-1] = 1.0`** after `cumsum/total` before `searchsorted`. → sales-internals
- **Shared arrays:** update `SharedArrayGroup` init when adding a dimension column workers need. → sales-internals

### Config & generation — load **config-schema** / **generate-dataset**
- **Pipeline `cfg` and `models_cfg` are Pydantic, not dicts** — attribute access; copy with `model_copy(deep=True)`. → config-schema
- **Coercion helpers** (`bool_or`/`int_or`/`float_or`/`str_or`) come from `src.utils.config_helpers`; don't redefine. → config-schema
- **Dimension versioning:** a SHA-256 of each dim's config section gates regen. If an edit "did nothing," the hash didn't change — force with `--regen-dimensions`. → generate-dataset
- **Pricing injection:** editing `models.yaml` pricing bands requires `--regen-dimensions products` to resync the product dim. → generate-dataset
- **FX dates are coupled:** `exchange_rates` range is overridden to `defaults.dates.start/end`; can't be set independently. → generate-dataset
- **Returns/complaints auto-disable** when `enabled=true` AND `sales_output='sales'` AND `skip_order_cols=true` (both need OrderNumber). → generate-dataset
- **Scratch vs final:** workers write scratch (`data/`), packaging copies to `generated_datasets/`; interrupted runs may leave scratch behind. → generate-dataset

### Web — load **web-layer** before editing `web/`
- **Thread safety:** guard `web/shared_state.py` globals with `_cfg_lock`; reads must `copy.deepcopy()` under the lock. → web-layer
- **Dual mounting:** every endpoint is served at both `/api/...` and `/v1/api/...` (same handlers). → web-layer
- **Security:** CORS is a localhost allowlist (not `*`, override via `CORS_ORIGINS`); `SecurityHeadersMiddleware` adds headers; YAML/models endpoints reject bodies > 1 MB. → web-layer

### SQL & Power BI — load **sql-and-dialects** before editing `src/tools/sql/`
- **SQL escaping:** `N'...'` prefix for paths in `BULK INSERT FROM`; escape single quotes in `OBJECT_ID()` names. → sql-and-dialects
- **TMDL injection:** escape `"` → `\"` in file paths embedded in Power BI M expressions (`powerbi_packaging.py`). → sql-and-dialects
- **Constraints/indexes/views are generated from `src/tools/sql/schema_model.py`** (rendered by `constraint_sql.py`/`view_sql.py`), not hand-written SQL — edit the roster, not the output. → sql-and-dialects
- **Identifier casing is an import-time choice, not generation.** Generation is always PascalCase. The importers (`--identifier-case pascal|snake`) re-render the whole SQL bundle in the chosen case via `src/engine/packaging/reemit_sql.py` (reads the run's config + data, drives the same roster generators, never touches CSV/parquet). The effective run shape (CLI overrides, OrderNumber width) comes from `config/sql_generation.json` written at packaging, since the packaged config.yaml predates overrides. snake is the Postgres quote-free win. → sql-and-dialects

### Homeless tripwires (no skill — full detail lives here)
- **Cross-dimension file writes:** `inventory/runner.py` overwrites `product_profile.parquet` to update ABCClassification from actual sales volume (fact-to-dimension write, safe due to sequential execution). Don't add more cross-writes without updating the cascade logic in `dimensions_runner.py`. (Warehouses no longer cross-writes `stores.parquet`: `run_stores` allocates warehouses on the in-memory store frame, stamps `WarehouseKey` inline, and writes both `stores.parquet` and `warehouses.parquet` once. `run_warehouses` is a thin guard that re-invokes the stores flow when `warehouses.parquet` is stale.)
- **Exception hierarchy & specificity:** use `ConfigError`/`DimensionError`/`SalesError`/`PackagingError`/`ValidationError` from `src/exceptions.py` (all inherit `PipelineError`), not generic `RuntimeError`/`ValueError`. Avoid bare `except Exception` — catch specific types.
- **Hardcoded constants:** domain constants live in `src/defaults.py`; import them, don't inline large constant dicts. Probability arrays are validated at import time to sum to 1.0.
- **Weekly fiscal column guard:** `dates/weekly_fiscal.py`'s `add_weekly_fiscal_columns()` early-returns `if not cfg.enabled` before computing any 4-4-5 columns (`FW*Offset`, `FWWeekNumber`, `FWWeekIndex`); without the guard, accessing those columns when weekly fiscal is disabled raised `KeyError`.
- **Nested probability validation:** `defaults.py` validates probability arrays inside nested dicts (`CUSTOMER_HOME_OWNERSHIP_PROBS_BY_INCOME`, `CUSTOMER_OCCUPATION_PROBS_BY_EDUCATION`) and lists (`CUSTOMER_MARITAL_PROBS_BY_AGE`, `CUSTOMER_EDUCATION_PROBS_BY_AGE`) at import time, not just top-level arrays.
- **Log file descriptor thread safety:** `_ensure_log_file_open()` in `logging_utils.py` is guarded by `_LOG_FD_LOCK` to prevent races when multiple threads open the log file simultaneously.
- **`Products.BaseProductID` references `ProductID`, not `ProductKey`, under SCD2:** SCD2 reassigns `ProductKey` to a fresh `1..total_rows` sequence, so `BaseProductID` no longer matches any current `ProductKey` — but it still matches `ProductID` (which SCD2 preserves). To group all SCD2 versions of a family use `ProductID`; to find variants filter `BaseProductID == <ProductID>`. A `BaseProductID = ProductKey` self-join only works with SCD2 off; with SCD2 on use `BaseProductID = ProductID`.

## Testing and verification

Use the venv interpreter directly; examples below are for Windows:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_config_loader.py tests/test_schema.py
.venv/Scripts/python.exe -m pytest -k "geography and not key"
.venv/Scripts/python.exe -m pytest --collect-only -q
.venv/Scripts/python.exe -m pytest
```

`pyproject.toml` defines pytest defaults (`-v --tb=short`). Choose checks for the
changed area using `tests/README.md` and the relevant skill; avoid maintaining a
fixed test count or exhaustive filename list here. The dev extra supplies `httpx`
so web tests run instead of silently skipping. Importer unit tests use fake
connections; passing them does not establish live SQL Server/PostgreSQL success.

Frontend source changes require rebuilding and committing `web/frontend/dist/`
with the source (`npm ci`, `npm run build`, `npm run build:check` from
`web/frontend/`). See the web skill for offline assets and CSP constraints.

Snapshot tests pin generated output. For an intentional output change, inspect
which fixtures moved, explain why, then regenerate only the affected baseline.
See the sales skill and `tests/README.md`; do not refresh snapshots to hide an
unexplained failure.

`scripts/validate_web_release.py` validates an isolated archive of **committed
HEAD**, not uncommitted changes. `scripts/benchmark_output_modes.py` defaults to
20M rows; supply a small explicit `--rows` when only a smoke run is intended.

## Output Formats

- **CSV:** chunked files + auto-generated SQL scripts (DDL, BULK INSERT/COPY, constraints, views) for SQL Server and PostgreSQL dialects
- **Parquet:** merged single file (configurable) + compression/row_group tuning
- **Delta Lake (`deltaparquet`, also `delta`):** partitioned by Year/Month, ACID transactions via delta-rs
- **Power BI `.pbip` project:** auto-generated for `csv` and `parquet` only — `deltaparquet` **intentionally skips** PBIP generation (`powerbi_packaging.py`)

## Workflow Tips

- Use `--dry-run` before large runs to validate config
- Use `--only dimensions` then `--only sales` to iterate on each stage independently
- `workers` defaults to CPU count; don't exceed it
- `chunk_size` too small = overhead, too large = memory pressure (default 1M is good)
- Check `generated_datasets/` for timestamped output folders
