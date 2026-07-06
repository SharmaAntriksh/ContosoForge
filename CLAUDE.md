# ContosoForge

Analytics-ready retail data generator (dimensional model, inspired by ContosoRetailDW). Produces deterministic, idempotent datasets for BI, analytics, and data engineering.

## Skills (load the relevant one before working in that area)

Detailed, area-specific knowledge lives in on-demand skills under `.claude/skills/` so this file stays a lean map. Load the skill that matches your task — its body carries the deep detail; the "danger index" below keeps only the one-line tripwire.

| Skill | Load it when you're… | Holds the detail for |
|---|---|---|
| **generate-dataset** | running/tuning generation: rows, customers, dates, trend preset, catalog, output format | CLI flags, dual-config, presets, scaling, catalog |
| **sales-internals** | editing anything under `src/facts/sales/` | the determinism/parallelism contract + numeric tripwires |
| **config-schema** | adding/changing a config field, or reading `cfg`/`models_cfg` | Pydantic spine, normalizer, coercion helpers |
| **web-layer** | editing anything under `web/` | thread safety, route versioning, CORS/security/limits |
| **sql-and-dialects** | editing `src/tools/sql/` or generated SQL | dialect classes, escaping, import runners |

## Tech Stack

- **Language:** Python 3.13+ (`requires-python = ">=3.13"` in `pyproject.toml`)
- **Web UI:** FastAPI backend (`web/api.py` + `web/routes/`) + React SPA (`web/frontend/`)
- **Data:** pandas, numpy, pyarrow, deltalake
- **Config:** PyYAML, pydantic
- **Other:** yfinance (FX rates), multiprocessing (parallel sales)

## Commands

```bash
# Run generation (CLI) — full flag reference lives in the generate-dataset skill
python main.py --format parquet --sales-rows 1000000
python main.py --format csv --customers 5000 --sales-rows 100000
python main.py --format deltaparquet --workers 8

# Useful flags
python main.py --dry-run                        # validate config, no generation
python main.py --only dimensions                # generate dims only
python main.py --only sales                     # generate sales only (dims must exist)
python main.py --regen-dimensions products      # force-rebuild specific dim
python main.py --regen-dimensions all           # force-rebuild all dims
python main.py --refresh-fx-master              # top up FX rates via Yahoo Finance

# Post-generation tools
python scripts/optimize_delta.py "generated_datasets/..."   # compact small delta files
python scripts/optimize_parquet.py "generated_datasets/..."  # re-compress parquet

# Web UI
python -m uvicorn web.api:app --port 8502
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

## Directory Map

```
main.py                          # Entry point, calls src.cli.main()
src/
  cli.py                         # Argument parser, CLI overrides
  defaults.py                    # Centralized hardcoded constants (stores, customers, promos, etc.)
  exceptions.py                  # Custom exception hierarchy (PipelineError, ConfigError, etc.)
  dimensions/                    # One generator per dimension table
    customers/                   # Customer dimension package
      __init__.py                # Re-exports run_customers, run_subscriptions
      generator.py               # Main orchestrator: generate_synthetic_customers(), run_customers()
      helpers.py                 # Date math, acquisition curves, income/phone/credit/address generation
      org_profile.py             # OrganizationProfile constants + generate_org_profile()
      households.py              # Household assignment (spouse/dependent matching)
      scd2.py                    # SCD2 life event engine (career, marriage, relocation, etc.)
      subscriptions/             # Plans + CustomerSubscriptions (many-to-many bridge) package
        __init__.py              # Re-exports run_subscriptions
        runner.py                # run_subscriptions() entry point
        catalog.py               # Plan catalog definitions
        helpers.py               # Subscription assignment helpers
        bridge_serial.py         # Serial bridge builder
        bridge_parallel.py       # Parallel (multiprocessing) bridge builder
      worker.py                  # Multiprocessing chunk worker for parallel customer generation
    dates/                       # Date dimension package — one module per calendar system
      __init__.py                # Re-exports run_dates, generate_date_table, resolve_date_columns, WeeklyFiscalConfig, run_time_table
      runner.py                  # run_dates() — config parsing, versioning, parquet write
      generator.py               # Orchestrator: calls add_*_columns() subsystems in sequence
      calendar.py                # add_calendar_columns() — base calendar + as-of relative offsets
      iso.py                     # add_iso_columns() — ISO week fields + offsets
      fiscal.py                  # add_fiscal_columns() — monthly fiscal system + offsets
      weekly_fiscal.py           # WeeklyFiscalConfig + add_weekly_fiscal_columns() — 4-4-5 system
      columns.py                 # resolve_date_columns() — builds ordered output column list per config
      helpers.py                 # Shared constants (_EXCEL_EPOCH, _ISO_WEEK_REF) and utility functions
      time.py                    # Time-of-day dimension (minute grain, 1440 rows)
    employees/                   # Employee dimension package
      __init__.py                # Re-exports run_employees, run_employee_store_assignments
      generator.py               # Employee generation with role profiles, HR fields
      employee_store_assignments.py  # Multi-store assignment with transfer reasons
      transfers.py               # Transfer scheduling logic
    exchange_rates/              # Currency + exchange rate dimensions
      __init__.py                # Re-exports run_currency, run_exchange_rates
      currency.py                # Currency dimension from defaults
      exchange_rates.py          # Daily exchange rates (Yahoo Finance or synthetic)
      monthly_rates.py           # Monthly aggregated rates
      helpers.py                 # Triangulation, date alignment utilities
    products/                    # Product dimension package
      __init__.py                # Re-exports run_suppliers, generate_product_dimension, load_static_dimension
      products.py                # Entry point: generate_product_dimension() (category + subcategory + products)
      generator.py               # Orchestrator: load → expand → price → enrich → SCD2 → write
      scd2.py                    # SCD2 price revision versioning (vectorized)
      product_profile.py         # ProductProfile enrichment (analytical attributes)
      pricing.py                 # Price grid: bands, margin, snap, brand normalization
      contoso_loader.py          # Load product catalog (catalog: contoso | synthetic | all)
      contoso_expander.py        # Stratified trim/expand of products
      catalog_builder.py         # Offline script: generates synthetic products, writes split parquets
      static_loader.py           # Generic static dimension loader (category, subcategory)
      suppliers.py               # Supplier dimension generation
      worker.py                  # Multiprocessing chunk worker for parallel product enrichment
    stores/                      # Store dimension package
      __init__.py                # Re-exports run_stores, generate_store_table, GeoContext
      generator.py               # Store generation with geography, staffing, assortment
    warehouses/                  # Warehouse dimension package
      generator.py               # Warehouse generation (capacity, location, type)
    geography.py                 # Geography dimension (countries, states, cities, population weighting)
    promotions.py                # Promotions dimension (seasonal, clearance, BOGO, etc.)
    lookups.py                   # Lookup dimensions (sales channels, loyalty tiers, acquisition channels)
    return_reasons.py            # Return reason dimension
  facts/
    sales/                       # Core sales fact generation
      sales.py                   # Entry point, orchestrates worker pool
      prep/                      # Coordinator-side setup (main process, before the worker pool spawns)
        sales_helpers.py         # Leaf helpers (stdlib/numpy/pandas; importable without importing sales.py)
        dimension_loaders.py     # Load dimension parquet + promo-salience precompute
        correlation_lookups.py   # Correlation lookups + _prebuild_shared_structures (SHM broadcast)
        scd2_grid.py             # Shared SCD2 version-index grid helper
        memory_model.py          # Chunk-size RAM budgeting (cap by available physical bytes)
        worker_cfg_builder.py    # Assemble worker_cfg dict + streamed-fact accumulators
        worker_cfg_schema.py     # Worker config dict schema (SalesWorkerCfg)
        coverage_preflight.py    # Pre-run salesperson-coverage check (abort/skip/repair policy)
      sales_logic/               # Business logic (orders, pricing, promos, allocation)
        globals.py               # State class (per-worker; bound once, read-only thereafter) + schema bridge
        chunk_builder.py         # Per-chunk row assembly (CDF sampling, ID arithmetic)
        columns.py               # Sales column derivation (channel cache, etc.)
        core/                    # customer_sampling, orders, promotions, allocation, pricing, delivery
      sales_models/              # quantity model (Poisson), pricing pipeline (inflation+markdown+snap)
      sales_worker/              # Multiprocessing pool, task definitions, schemas
      sales_writer/              # Output writers (parquet merge, delta, CSV), encoding
      output_paths.py            # Scratch/output path resolution
      output_assembler.py        # Final output assembly (parquet merge / delta / csv dispatch)
    budget/                      # Budget fact (Low/Medium/High scenarios): accumulator, engine, lookups, micro_agg, runner
    inventory/                   # Inventory snapshots (monthly grain, ABC class, shrinkage): accumulator, engine, micro_agg, runner, worker
    wishlists/                   # Customer wishlists (streamed): accumulator, constants, micro_agg, runner, scd2, selection, worker
    complaints/                  # Customer complaints (streamed): accumulator, micro_agg, runner
    shared/                      # Shared fact utilities
      base_accumulator.py        # Base class for streamed fact accumulators
      micro_agg_helpers.py       # Micro-aggregation helpers for fact generation
      writers.py                 # Shared output writers for fact tables
  engine/
    config/                      # config_loader.py, config.py (normalizer registry), config_schema.py (Pydantic models)
    packaging/                   # package_output (orchestrator), csv/parquet/delta packagers, paths, SQL script gen
    runners/
      pipeline_runner.py         # Main orchestrator (override precedence, config injection)
      dimensions_runner.py       # Dependency-aware dim generation with version tracking
      sales_runner.py            # Sales pipeline coordinator
    quality_report.py            # Post-generation quality report (FK checks, stats, warnings)
    dimension_loader.py          # Load dimension parquet files for pipeline use
    powerbi_packaging.py         # Auto-generates .pbip project
  integrations/
    fx_yahoo.py                  # Yahoo Finance FX rate downloader
  tools/sql/                     # Multi-dialect SQL script generators (CREATE TABLE, BULK INSERT/COPY)
    dialect/                     # Pluggable SQL dialects: base.py, sqlserver.py, postgres.py
    generate_create_table_scripts.py, generate_bulk_insert_sql.py  # DDL + bulk-load script gen
    sql_server_import.py, postgres_import.py  # Live DB import runners (SQL Server + PostgreSQL)
    sql_helpers.py, _import_common.py          # Shared escaping / import utilities
  utils/
    shared_arrays.py             # Numpy shared memory for dimension broadcasting to workers
    static_schemas.py            # Column definitions for all output tables
    trend_presets.py             # Trend presets: business shape (revenue, lifecycle, demand)
    config_helpers.py            # Canonical type coercion helpers (bool_or, int_or, float_or, etc.)
    config_precedence.py         # Standardized config resolution (resolve_seed, resolve_dates)
    pool.py                      # Generic multiprocessing pool (PoolRunSpec, iter_imap_unordered)
    config_merge.py              # Deep-merge helpers for config layering
    promotion_buckets.py         # Promotion bucketing helpers
    name_pools.py, output_utils.py, logging_utils.py
  versioning/
    version_store.py             # Hash-based dimension version tracking (writes data/versioning/<name>.version.json)
web/
  api.py                         # FastAPI app entry point (includes routers, serves frontend)
  shared_state.py                # Shared mutable state and helpers for web layer
  routes/                        # FastAPI routers (split from monolithic api.py)
    config_routes.py             # /api/config endpoints
    models_routes.py             # /api/models endpoints
    generation_routes.py         # /api/generate, /api/validate endpoints
    presets_routes.py            # /api/presets endpoints
    data_routes.py               # /api/datasets endpoints (browse, preview generated data)
    import_routes.py             # /api/import endpoints (SQL Server import management)
  frontend/                      # React SPA (YAML editor, presets, log viewer)
```

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
- **Both `cfg` and `models_cfg` are Pydantic, not dicts** — attribute access; copy with `model_copy(deep=True)`. → config-schema
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

### Homeless tripwires (no skill — full detail lives here)
- **Cross-dimension file writes:** `inventory/runner.py` overwrites `product_profile.parquet` to update ABCClassification from actual sales volume (fact-to-dimension write, safe due to sequential execution). Don't add more cross-writes without updating the cascade logic in `dimensions_runner.py`. (Warehouses no longer cross-writes `stores.parquet`: `run_stores` allocates warehouses on the in-memory store frame, stamps `WarehouseKey` inline, and writes both `stores.parquet` and `warehouses.parquet` once. `run_warehouses` is a thin guard that re-invokes the stores flow when `warehouses.parquet` is stale.)
- **Exception hierarchy & specificity:** use `ConfigError`/`DimensionError`/`SalesError`/`PackagingError`/`ValidationError` from `src/exceptions.py` (all inherit `PipelineError`), not generic `RuntimeError`/`ValueError`. Avoid bare `except Exception` — catch specific types.
- **Hardcoded constants:** domain constants live in `src/defaults.py`; import them, don't inline large constant dicts. Probability arrays are validated at import time to sum to 1.0.
- **Weekly fiscal column guard:** `dates/weekly_fiscal.py`'s `add_weekly_fiscal_columns()` early-returns `if not cfg.enabled` before computing any 4-4-5 columns (`FW*Offset`, `FWWeekNumber`, `FWWeekIndex`); without the guard, accessing those columns when weekly fiscal is disabled raised `KeyError`.
- **Nested probability validation:** `defaults.py` validates probability arrays inside nested dicts (`CUSTOMER_HOME_OWNERSHIP_PROBS_BY_INCOME`, `CUSTOMER_OCCUPATION_PROBS_BY_EDUCATION`) and lists (`CUSTOMER_MARITAL_PROBS_BY_AGE`, `CUSTOMER_EDUCATION_PROBS_BY_AGE`) at import time, not just top-level arrays.
- **Log file descriptor thread safety:** `_ensure_log_file_open()` in `logging_utils.py` is guarded by `_LOG_FD_LOCK` to prevent races when multiple threads open the log file simultaneously.
- **`Products.BaseProductID` references `ProductID`, not `ProductKey`, under SCD2:** SCD2 reassigns `ProductKey` to a fresh `1..total_rows` sequence, so `BaseProductID` no longer matches any current `ProductKey` — but it still matches `ProductID` (which SCD2 preserves). To group all SCD2 versions of a family use `ProductID`; to find variants filter `BaseProductID == <ProductID>`. A `BaseProductID = ProductKey` self-join only works with SCD2 off; with SCD2 on use `BaseProductID = ProductID`.

## Testing

```bash
# Run all tests (uses pyproject.toml defaults: -v --tb=short)
pytest

# Single file
pytest tests/test_geography.py

# Single class
pytest tests/test_config_loader.py::TestParseDate

# Single test
pytest tests/test_config_loader.py::TestParseDate::test_iso_string

# Keyword filter (supports and/or/not)
pytest -k "deterministic"
pytest -k "geography and not key"

# Useful flags
pytest -x              # stop on first failure
pytest -s              # show print() output
pytest --tb=long       # full tracebacks
pytest --lf            # rerun only last-failed tests
pytest --co            # list tests without running
```

Test files (`tests/`, 40 total): `test_chunk_id_integrity.py`, `test_complaints.py`, `test_config_loader.py`, `test_coverage_preflight.py`, `test_customer_profiles.py`, `test_dates.py`, `test_determinism.py`, `test_dialect.py`, `test_dialect_postgres.py`, `test_dimension_loader.py`, `test_dimensions.py`, `test_fact_writers.py`, `test_geography.py`, `test_gotchas_and_guards.py`, `test_integration.py`, `test_multi_event_returns.py`, `test_packaging.py`, `test_postgres_constraints.py`, `test_postgres_import.py`, `test_postgres_indexes.py`, `test_postgres_views_and_admin.py`, `test_pricing_pipeline.py`, `test_product_scd2.py`, `test_products.py`, `test_quality_report.py`, `test_quantity_model.py`, `test_salesperson_performance.py`, `test_sales_logic.py`, `test_sales_writer.py`, `test_schema.py`, `test_sql_tools.py`, `test_state.py`, `test_store_demand_weight.py`, `test_subscriptions.py`, `test_transfers.py`, `test_utils.py`, `test_version_store.py`, `test_warehouses.py`, `test_web_api.py`, `test_web_routes.py`, `test_wishlists.py`. Web API/route tests require `httpx` and are skipped without it; the Postgres tests (`test_postgres_*`, `test_dialect_postgres`) exercise the PostgreSQL dialect/import path.

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
