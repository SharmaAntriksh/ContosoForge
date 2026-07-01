# ContosoForge

Analytics-ready retail data generator (dimensional model, inspired by ContosoRetailDW). Produces deterministic, idempotent datasets for BI, analytics, and data engineering.

## Tech Stack

- **Language:** Python 3.13+ (`requires-python = ">=3.13"` in `pyproject.toml`)
- **Web UI:** FastAPI backend (`web/api.py` + `web/routes/`) + React SPA (`web/frontend/`)
- **Data:** pandas, numpy, pyarrow, deltalake
- **Config:** PyYAML, pydantic
- **Other:** yfinance (FX rates), multiprocessing (parallel sales)

## Commands

```bash
# Run generation (CLI)
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

## Dual Config System

### config.yaml -- Shape & Scale
Controls row counts, entity counts, date ranges, output format, parallelism, feature toggles.
- Key sections: `scale`, `defaults`, `sales`, `returns`, `products`, `customers`, `stores`, `employees`, `dates`, `exchange_rates`, `budget`, `inventory`, `subscriptions`, `wishlists`, `complaints`
- Validated by strict normalizer registry in `src/engine/config/config.py`

### models.yaml -- Sales Behavior & Business Shape
Controls demand curves, pricing dynamics, quantity distribution, markdown rules, return reasons, and the overall business shape via trend presets.
- Key sections: `models.macro_demand`, `models.quantity`, `models.pricing`, `models.brand_popularity`, `models.returns`
- Validated through Pydantic at pipeline startup: `ModelsConfig.from_raw_dict()` → `ModelsInnerConfig` (typed sub-models for each section)
- `State.models_cfg` holds the validated `ModelsInnerConfig` instance (not a raw dict)
- `resolve_trend_preset()` injects macro demand, customer lifecycle, and customer demand from the trend preset into both `cfg` and `models_cfg`. After resolution, `models_cfg.macro_demand` and `models_cfg.customers` are proper Pydantic models (not plain dicts)
- Not overridable via CLI; edit directly or use web UI

### Trend Presets (`models.macro_demand.trend`)
The trend preset is the **single source of business shape**. Each preset defines a coherent story across revenue curve, customer lifecycle, and demand behavior. Set via `macro_demand.trend` in `models.yaml`.

**Directional presets (extend over time):**
| Preset | Revenue Shape | Customer Curve |
|---|---|---|
| `steady-growth` | Gentle 5%/yr upward line | Stable base, gradual acquisition |
| `strong-growth` | Exponential acceleration | Continuously growing |
| `gradual-growth` | S-curve with dips | Ramp then level off |
| `hockey-stick` | Explosive years 4-6 | Rapid ramp |
| `decline` | Steady erosion | Shrinking (high churn) |
| `new-market-entry` | Near-zero then accelerating | Very slow then ramping |
| `slow-decline` | Gentle ~10%/yr drop | Gradual erosion |

**Cyclical presets (repeat every 10 years):**
| Preset | Revenue Shape | Customer Curve |
|---|---|---|
| `boom-and-bust` | Rapid rise then collapse | Rise then crash |
| `recession-recovery` | U-shape dip | Stable (dip from orders) |
| `seasonal-dominant` | Flat trend, strong seasonal | Flat with seasonal waves |
| `seasonal-with-growth` | Growth + retail seasonality | Growing with seasonal waves |
| `plateau` | Growth then flatline | Growth then stable |
| `volatile` | Wild year-to-year swings | Flat with noise |
| `double-dip` | Two downturns | Gradual decline |
| `stagnation` | Perfectly flat | Perfectly flat (no churn) |

Each preset controls: `year_level_factors`, `row_share_of_growth`, `noise_std`, `shock_probability`, `monthly_seasonality`, `bootstrap_months`, `early_month_cap`, and embedded `lifecycle` (acquisition curve, churn, initial customer spread) + `customers` (participation ratios, discovery, seasonal spikes) sub-dicts.

Resolved by `resolve_trend_preset()` in `src/utils/trend_presets.py`, called from `pipeline_runner.py`. The old `customers.profile` config key is deprecated — it maps to trend presets via `_PROFILE_TO_TREND` for backward compatibility.

### Scaling Guidelines
Chart visual quality depends on the ratio of customers to sales rows and the date range:
- **Target ~1.5 orders per customer per month** for spiky, interesting charts
- **Formula:** `customers ≈ sales_rows / months / 1.5`
- Longer date ranges need fewer customers for the same row count (rows spread over more months)
- Too few customers relative to rows → overly smooth charts (each customer averages many orders, variance averages out)
- Too many customers relative to rows → sparse purchase history per customer

| Sales Rows | 3 years (36 mo) | 6 years (72 mo) | 10 years (120 mo) | 20 years (240 mo) |
|---|---|---|---|---|
| 2M | 37K | 19K | 11K | 6K |
| 10M | 185K | 93K | 56K | 28K |
| 20M | 370K | 185K | 111K | 56K |
| 100M | 1.85M | 925K | 555K | 278K |

### Product Catalog Selection (`scale.products.catalog`)
Controls which product source to use. Set via `scale.products` in `config.yaml`:

```yaml
scale:
  products:
    catalog: all          # "contoso" | "synthetic" | "all"
    rows: 7930            # target product count (trims or expands to fit)
```

| Catalog | Source Files | Products | Brands | Description |
|---|---|---|---|---|
| `contoso` | `contoso_products.parquet` | 2,517 | 11 | Original ContosoRetailDW products only |
| `synthetic` | `synthetic_products.parquet` | 5,633 | 115 | Retail + electronics + audio/media brands (Nike, Samsung, IKEA, Sennheiser, etc.) |
| `all` | both files combined | ~7,930 | 126 | Combined with Contoso house brand trimmed to 490 |

- Source files live in `data/contoso_products/` and are generated by `catalog_builder.py`
- `rows` controls expansion (variants) when > base count, or stratified trim when < base count
- `scale.products` also accepts a plain integer for backward compatibility (defaults to `catalog: all`)
- The house brand trim (`_MAX_HOUSE_BRAND` in `contoso_loader.py`) only applies in `all` mode to prevent the Contoso house brand (710 products) from dominating
- Changing `catalog` triggers product dimension regeneration (included in version key)

### Override Precedence
CLI flags > config.yaml values (one-time, not persisted).

## Critical Gotchas

1. **Pricing injection:** At pipeline startup, `models.yaml` pricing appearance rules are injected into config's product section. Both product dimension generation and sales-time pricing use the same price grid. If you edit pricing bands in models.yaml, run `--regen-dimensions products` to sync.

2. **Dimension versioning:** Each dim has a `data/versioning/<name>.version.json` file storing a SHA-256 `config_hash` of its config section (`version_store.py`). Dims only regenerate if that hash changes, the parquet/version file is missing, or `--regen-dimensions` forces it (force deletes the version file). Stale outputs usually mean the version hash didn't change.

3. **State is per-worker, bound-once then read-only (by construction + convention):** `State` in `sales_logic/globals.py` is a module-global class populated once per worker process by `bind_globals()`. After the Phase 5.1 cleanup, **all** worker-lifetime scratch (the CDF / month-pool / dir / task / sales-channels caches) lives in **module-level globals reset at worker init** (`init_sales_worker`), NOT on `State` — so within a worker, `State` is written only inside `bind_globals` (never reassigned during chunk processing). There is **no runtime immutability enforcement**: the old `_SealableMeta`/`seal()` machinery was removed (never called; couldn't be, since scratch used to live on `State`), and a replacement runtime freeze was **deliberately not added** — the determinism guarantee is enforced by the Phase-0 guardrail tests (worker- and chunk-invariance) plus the static-schedule design (discovery/plan/pools are computed once in the coordinator and broadcast read-only), and a seal would be fragile because the coordinator (`generate_sales_fact`, main process) and in-process workers share the same `State`. Immutability rests on (a) convention (bind once, never reassign), (b) process isolation (each worker has its own `State`), and (c) scratch being fully externalized. **Do not reintroduce cross-chunk mutable state on `State`** (e.g. a lazy cache) — add it as a module-global with a reset in `init_sales_worker` instead. In tests, call `State.reset()` between cases. (The `_get_state_attr` dual-name resolver and the dead `SalesContext` dataclass were also removed.)

4. **Returns/complaints conditional skip:** Returns *and* complaints are auto-disabled at config load time if `enabled=true` AND `sales_output='sales'` AND `skip_order_cols=true` (both need OrderNumber to link back). `apply_cross_section_rules()` in `config.py` loops over `("returns", "complaints")` and logs a warning for each section it disables.

5. **FX dates are coupled:** exchange_rates date range is overridden at runtime to match `defaults.dates.start/end`. You cannot set FX dates independently.

6. **Shared arrays:** Dimensions are loaded into numpy shared memory for zero-copy worker access. If adding new dimension columns, update `SharedArrayGroup` initialization.

7. **Cross-dimension file writes:** Two places modify another dimension's output: (a) `warehouses/generator.py` adds `WarehouseKey` to `stores.parquet` — the dimension runner auto-forces warehouses when stores regenerates to prevent a missing-column crash in inventory; (b) `inventory/runner.py` overwrites `product_profile.parquet` to update ABCClassification from actual sales volume (fact-to-dimension write, safe due to sequential execution). Do not add more cross-writes without updating the cascade logic in `dimensions_runner.py`.

8. **Scratch vs final output:** Workers write to scratch (`data/`), then packaging copies to final timestamped folder (`generated_datasets/`). Interrupted runs may leave scratch behind.

9. **Exception hierarchy & specificity:** Use exceptions from `src/exceptions.py` (`ConfigError`, `DimensionError`, `SalesError`, `PackagingError`, `ValidationError`) instead of generic `RuntimeError`/`ValueError`. All inherit from `PipelineError`. Avoid bare `except Exception`— always catch specific types (e.g., `except (KeyError, ValueError, OSError)`).

10. **Type coercion helpers:** Import `bool_or`, `int_or`, `float_or`, `str_or` from `src.utils.config_helpers`. Do not define local variants. These are still required for plain dict accesses (worker configs, nested profile dicts) and function parameters that might be None. For Pydantic model attributes with typed defaults, they're redundant but harmless — new code accessing Pydantic fields can skip them.

11. **Hardcoded constants:** Domain constants (store types, customer demographics, promo categories, currency maps, etc.) live in `src/defaults.py`. Dimension generators import from there — do not inline large constant dicts. Probability arrays are validated at import time to sum to 1.0.

12. **API versioning:** All web API endpoints are available at both `/api/...` (backward-compatible) and `/v1/api/...` (versioned). Both routes hit the same router handlers.

13. **Web layer thread safety:** Shared mutable state in `web/shared_state.py` (`_cfg`, `_models_cfg`, etc.) is guarded by `_cfg_lock`. Always acquire the lock when reading or mutating these globals from route handlers. Reads must `copy.deepcopy()` under the lock to avoid races.

14. **int32 overflow in ID arithmetic:** Order ID math (add, multiply) must use `int64` intermediates before casting back to `int32`. Silent wraparound at 2^31 produces negative/duplicate IDs.

15. **numpy bincount weights dtype:** `np.bincount(..., weights=...)` requires `float64` weights. Passing `int8` silently overflows at >127 elements, producing wrong counts.

16. **CDF + searchsorted boundary:** After computing a CDF via `np.cumsum(w) / total`, always clamp `cdf[-1] = 1.0`. Floating-point rounding can leave the last element slightly below 1.0, causing `searchsorted` to return out-of-bounds indices.

17. **SQL output escaping:** Use `N'...'` (Unicode prefix) for file paths in `BULK INSERT FROM` statements. Escape single quotes in names passed to `OBJECT_ID()`. Without this, non-ASCII paths or names with apostrophes break generated SQL.

18. **TMDL expression injection:** File paths embedded in Power BI M expressions must have `"` escaped to `\"`. Unescaped quotes in paths break the generated `.tmdl` files.

19. **CORS allowlist:** `web/api.py` restricts origins to `localhost`/`127.0.0.1` on ports `8502` and `3000` by default (not `*`); the allowlist is overridable via the `CORS_ORIGINS` env var. A `SecurityHeadersMiddleware` adds `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, and `Referrer-Policy` to all responses.

20. **Web payload size limits:** YAML and models endpoints reject bodies > 1 MB (HTTP 413). This prevents memory exhaustion from oversized payloads.

21. **Weekly fiscal column guard:** `dates/weekly_fiscal.py`'s `add_weekly_fiscal_columns()` early-returns (`if not cfg.enabled: return df`) before computing any 4-4-5 columns, including the `FWWeekOffset` / `FWMonthOffset` / `FWQuarterOffset` offsets and `FWWeekNumber` / `FWWeekIndex`. Without this guard, accessing those columns when weekly fiscal is disabled caused a `KeyError`.

22. **Nested probability validation:** `defaults.py` now validates probability arrays inside nested dicts (`CUSTOMER_HOME_OWNERSHIP_PROBS_BY_INCOME`, `CUSTOMER_OCCUPATION_PROBS_BY_EDUCATION`) and lists (`CUSTOMER_MARITAL_PROBS_BY_AGE`, `CUSTOMER_EDUCATION_PROBS_BY_AGE`) at import time, not just top-level arrays.

23. **Log file descriptor thread safety:** `_ensure_log_file_open()` in `logging_utils.py` is guarded by `_LOG_FD_LOCK` to prevent races when multiple threads open the log file simultaneously.

24. **Both configs are Pydantic, not dicts:** `cfg` is `AppConfig` and `models_cfg` (aka `State.models_cfg`) is `ModelsInnerConfig` — both Pydantic models from `src/engine/config/config_schema.py`. Access fields via attribute syntax, not dict syntax. Key rules:
    - **Read config:** `cfg.sales.file_format` or `models_cfg.pricing.inflation` — attribute access, not `cfg["field"]`
    - **Write config:** `cfg.sales.file_format = "csv"` (attribute assignment) — bracket write (`cfg["key"] = val`) works via `_MutationMixin.__setitem__`
    - **`.get()` shim:** `_MutationMixin.get(key, default)` delegates to `getattr()`, so existing `.get()` chains work on Pydantic models. This is intentional for backward compatibility with code that handles both dicts and models
    - **`resolve_trend_preset` caveat:** After trend resolution, `models_cfg.macro_demand` and `models_cfg.customers` are replaced with freshly validated Pydantic models (`MacroDemandConfig` and `CustomersDemandConfig`). The old `resolve_customer_profile` is deprecated
    - **`_`-prefixed keys are stripped:** Pydantic v2 treats `_`-prefixed keys as private fields. `_strip_internal_keys()` in `config_schema.py` removes normalizer metadata before validation
    - **`extra="forbid"` on sub-models:** All config sub-models (via `_Base`) reject unknown keys. `AppConfig` and `ModelsConfig` use `extra="allow"` for runtime-injected keys. Add new fields to the schema class if needed
    - **Worker configs are plain dicts:** `worker_cfg`, `acfg`, and sales worker internals remain plain dicts for multiprocessing pickling — these correctly use dict access
    - **Copying configs:** Use `cfg.model_copy(deep=True)` instead of `dict(cfg)` or `copy.deepcopy(cfg)`
    - **Hash functions:** Use `_to_dict(obj)` helper (calls `model_dump()` on Pydantic, pass-through for dicts) before `json.dumps()` for config hashing
    - **Schema location:** All Pydantic models live in `src/engine/config/config_schema.py`. `AppConfig.from_raw_dict(d)` and `ModelsConfig.from_raw_dict(d)` are the canonical constructors

25. **`Products.BaseProductID` references `ProductID`, not `ProductKey`, under SCD2:** The column is set during `expand_contoso_products()` to the post-expansion `ProductKey` of each variant's `VariantIndex=0` row. After SCD2 expansion (`scd2.generate_scd2_versions`) reassigns `ProductKey` to a fresh `1..total_rows` sequence, the values stored in `BaseProductID` no longer match any current `ProductKey` — but they DO still match `ProductID` (which `generator.py` sets to the pre-SCD2 `ProductKey` and SCD2 preserves). Practical implications:
    - To group all SCD2 versions of a product family: use `ProductID`, not `ProductKey` or `BaseProductID`.
    - To find variants of a base product: filter `Products` where `BaseProductID == <some ProductID>`.
    - A self-join `Products.BaseProductID = Products.ProductKey` only works when SCD2 is disabled. With SCD2 on, use `Products.BaseProductID = Products.ProductID`.

26. **Sales posted `UnitPrice`/`UnitCost` is deterministic per (product, month); only `DiscountAmount` varies per line (Phase 4.1):** The sales pricing pipeline (`sales_models/pricing_pipeline.py`) applies per-month inflation (consistent within a month) and then snaps to the retail grid. The stochastic snap (the floor/round coin and the price ending) is now **hash-seeded on `(ProductID, month)`** (`_price_hash_u01`), so every sales line for the same product-month carries the **same** posted `UnitPrice`/`UnitCost` — a `(product, month)` self-join on the *fact* recovers a single list price. Per-line realized variation lives only in `DiscountAmount`/`NetPrice`. Gated by `models.pricing.appearance.deterministic` (default **true**); when false (or when `build_prices` gets `product_ids=None`, e.g. a direct unit-test call) the legacy **per-row** stochastic snap is used, so two same-(product, month) lines can differ — that OFF path is preserved byte-for-byte for callers that opt out. With product SCD2 *on*, the pipeline preserves catalog `ListPrice` per version anyway (its own branch, snapping skipped), so both modes give a deterministic posted price. `ProductID == ProductKey` under non-SCD2, which is the only mode where the snap runs, so keying on the per-line `product_keys` is correct. Regression guard: `tests/test_pricing_pipeline.py::TestDeterministicPostedPrice` + end-to-end `test_sales_phase3_fidelity.py::TestDeterministicPostedPrice` (single `UnitPrice` per `(ProductKey, month)`).

27. **Customer discovery is a static schedule; worker-lifetime cache keys must fully determine their value:** Customer discovery (which month each customer first transacts) is a static `State.customer_discovery_month` array — computed once in the coordinator from `hash(CustomerKey, seed)` + the eligibility window, broadcast read-only — so the parallel sales fact is byte-identical regardless of `--workers`. Two corollaries for the sales hot path (`sales_logic/chunk_builder.py`): (a) don't reintroduce cross-chunk mutable discovery state; (b) **any worker-lifetime cache must be keyed by every variable its value depends on.** Workers are reused across chunks and the month loop can skip months (`rows_per_month == 0`), so a cache whose key omits a dimension its value depends on becomes first-writer-wins and therefore chunk→worker-order dependent. This bit `_sample_brand_aware`'s product CDF cache: it was keyed by *calendar* month but the brand mix (`brand_prob_by_month[m_offset]`) is an *absolute*-month quantity, so Jan-2023 chunks could reuse Jan-2022's mix depending on scheduling. It is now keyed by `m_offset`. Regression guard: `tests/test_sales_determinism_workers.py` (seed 20250701 specifically exercises this).

28. **The sales per-month plan is global; chunks shard the order-id index space (do not reintroduce per-chunk allocation):** `chunk_size` is a pure performance knob — it must not change the shape of the data. The coordinator computes three length-`T` arrays ONCE from the seed against the GLOBAL month totals and broadcasts them read-only (like `customer_discovery_month`): `State.sales_rows_per_month` (R[m]), `State.sales_orders_per_month` (O[m] = R[m] / avg-lines-per-order, ≤ R[m]), and `State.sales_distinct_target` (D[m], ≤ O[m]). Each chunk slices a contiguous band of every month's order-id space via `_chunk_month_band(R, O, chunk_idx, total_chunks)` → `(order_start, n_orders, n_lines)`: orders **floor-tile `[0, O[m])` with no gaps** (`sum n_orders == O[m]`) and lines start at one-per-order + a proportional share of the extra (`n_lines ≥ n_orders`, `sum n_lines == R[m]` exactly, so `total_rows` is preserved for any `chunk_size`). The month's distinct-customer **pool** `P` (size ≤ D[m]) is built once per worker (`build_month_customer_pool`, cached in `_worker_month_pool_cache` keyed by month offset — the pool depends only on `(m, State.sales_plan_seed, static customer arrays)`), and `assign_orders_to_customers` maps global order index `j` → `pool[j]` if `j < len(P)` else a hash-weighted repeat from `P`. Because a given global order index always maps to the same customer and the distinct prefix `[0, len(P))` is always materialized by exactly one chunk, the **per-month distinct-customer set is exactly `P` regardless of chunking** (repeats add nobody new). Consequences for the sales hot path: (a) customer sampling uses the plan/month RNG (`State.sales_plan_seed`), **not** the per-chunk `rng` — don't move it back onto the chunk stream; (b) the coordinator's `avg_lines` and month-offset indexing must match `chunk_builder` exactly (both index months from `min(date_pool month)`); (c) don't re-add a per-chunk `build_rows_per_month` or a per-chunk distinct target — that is precisely the `chunk_size` dependence this removed. `compute_month_distinct_targets` is the single source of truth for D[m]. The legacy `_sample_customers` (chunk-local sampling) is retained but **dead** — do not re-wire it. Regression guard: `tests/test_sales_determinism_chunksize.py` (per-month distinct curve, hard assert).

29. **Phase 3 correlated-fact features change only per-line measures/identity, and are hash-seeded on stable global keys (never the chunk RNG):** The joint-model features add correlation to the sales fact without breaking the Phase 0–2 guarantees. The rule they all obey: a feature may change a per-line *measure* (quantity, discount) or *product identity*, but **never row counts or customer assignment** — so per-month rows and the distinct-customer curve stay chunk-invariant. New randomness is either drawn off the per-chunk `rng` (fine for worker byte-identity, since chunks own fixed bands seeded by `chunk_idx`) or, when a value must be recomputed in a *different* pass, **hash-seeded on a globally-unique key** so it is identical regardless of chunk/worker. Concretely, all `models.*` toggles are default-on:
    - **3.1 elasticity** (`models.quantity.elasticity`): `build_quantity` scales the float qty by `(unit_price/ref)^(-ε)` × propensity(`PopularityScore`), where `ref` = median catalog ListPrice from `State.product_np` (lazy, cached). **No RNG** — pure arithmetic before the round/clamp; only active when `unit_price` is passed (the pipeline path).
    - **3.2 promo salience** (`models.promotions`): `apply_promotions` does inverse-CDF selection weighted by `promo_salience_all` (precomputed in the coordinator = `exp(β·DiscountPct)·type_weight`, broadcast). `None` ⇒ uniform.
    - **3.3 basket theme** (`models.basket`): `sales_logic/core/basket.py` redirects a share of off-theme lines toward each order's theme (a subcategory group). Theme + redirect are **SplitMix64 hashes of (OrderNumber, OrderLineNumber)** — applied *before* channel-eligibility enforcement so eligibility keeps the final say. Needs `State.product_subcat_key` (now loaded whenever `SubcategoryKey` exists, not just under assortment).
    - **3.4 fulfillment friction** (`models.fulfillment`): `delivery.line_friction(OrderNumber, OrderLineNumber)` is a stateless SplitMix64 hash in [0,1). The delivery pass buckets a line early/on-time/delayed via `(p_early, p_delayed)` + a named delay-magnitude distribution (replaces the mod-100 ladder, **no RNG**); the **separate returns pass recomputes the identical friction** from the detail table's OrderNumber+line to raise return probability (`base·(1+boost·f)`) and shorten lag (`·(1−shorten·f)`). Both passes read `models.fulfillment` from `State.models_cfg` (no broadcast). Distinct salts keep basket (3.3) and friction (3.4) independent.
    - **3.5 markdown reconcile** (`models.pricing.markdown.reconcile_promotions`): discount is drawn only for promoted lines, from the *nonzero* ladder; consumes the same one `rng.choice(size=n)` as the legacy lottery so the RNG stream position is unchanged.
    Every feature has an off/zero path that reproduces the pre-Phase-3 bytes (so unit tests that don't opt in are unchanged), plus an end-to-end acceptance in `tests/test_sales_phase3_fidelity.py`. When adding another correlated feature: hash-seed cross-pass latents on an emitted key, keep it off the row/customer plan, and give it a default-on `models.*` toggle with a zero/off reproduction path.

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
