# Pipeline Flowchart

End-to-end execution flow of the synthetic data generator, from CLI invocation through final output packaging.

---

## High-Level Overview

```
CLI (main.py)
  |
  v
Parse Arguments (cli.py)
  |
  |-- --refresh-fx-master? --> Refresh FX rates via Yahoo Finance --> EXIT
  |
  v
Load & Validate Configs
  |  config.yaml  --> load_config()  --> AppConfig (Pydantic)
  |  models.yaml  --> ModelsConfig.from_raw_dict() --> ModelsInnerConfig (Pydantic)
  |
  v
Apply CLI Overrides
  |  CLI flags > config.yaml values (not persisted)
  |  Force FX dates to match global date range
  |
  |-- --dry-run? --> Print resolved config --> EXIT
  |
  v
Resolve Trend Preset
  |  macro_demand.trend --> injects MacroDemandConfig + CustomersDemandConfig
  |
  v
Inject Pricing Appearance
  |  models.yaml pricing rules --> config product pricing (shared grid)
  |
  v
+-----------------------------------------------------+
|              STAGE 1: DIMENSIONS                    |
|              (sequential, dependency-aware)         |
|                                                     |
|  Topological order, version-checked, force-cascade  |
|  Output: ~24 .parquet files in dims folder          |
+---------------------+-------------------------------+
                      |
                      v
+-----------------------------------------------------+
|              STAGE 2: FACTS                         |
|              (parallel, multi-worker)               |
|                                                     |
|  Sales --> Budget --> Inventory --> Wishlists/      |
|                                     Complaints      |
|  Output: fact tables (csv/parquet/delta)            |
+---------------------+-------------------------------+
                      |
                      v
+-----------------------------------------------------+
|              STAGE 3: PACKAGING                     |
|                                                     |
|  Copy dims + facts to timestamped output folder     |
|  Generate SQL scripts (CSV only)                    |
|  Attach Power BI project (CSV/Parquet only)         |
|  Optional quality report                            |
+-----------------------------------------------------+
```

---

## Stage 0: Entry Point & Config Resolution

```
main.py
  |  multiprocessing.freeze_support()
  v
cli.py :: main()
  |
  |-- Parse ~20 CLI arguments
  |     --format, --sales-rows, --workers, --chunk-size,
  |     --start-date, --end-date, --customers, --stores,
  |     --products, --promotions, --skip-order-cols,
  |     --products-scd2, --customers-scd2, --only,
  |     --clean, --dry-run, --regen-dimensions, --report, ...
  |
  |-- Group into PipelineOverrides dataclass
  |
  v
pipeline_runner.py :: run_pipeline()
  |
  |-- load_config(config.yaml)
  |     |-- YAML parse
  |     |-- Normalizer registry (per-section validation & coercion)
  |     |-- apply_cross_section_rules() (e.g., returns auto-disable)
  |     +-- AppConfig.from_raw_dict() --> Pydantic validation
  |
  |-- load_config_file(models.yaml)
  |     +-- ModelsConfig.from_raw_dict() --> Pydantic validation
  |
  |-- Deep-copy config (model_copy)
  |-- Attach metadata (config_yaml_path, model_yaml_path)
  |
  |-- _apply_overrides()
  |     CLI flags mutate cfg.sales, cfg.defaults.dates, cfg.products, etc.
  |
  |-- _force_fx_to_global_dates()
  |     exchange_rates date range := defaults.dates range
  |
  |-- resolve_trend_preset(models_cfg, cfg)
  |     macro_demand.trend --> MacroDemandConfig (Pydantic)
  |     lifecycle --> cfg.customers.lifecycle
  |     customers demand --> CustomersDemandConfig (Pydantic)
  |
  +-- _inject_models_appearance()
        models.yaml pricing.appearance --> cfg.products.pricing
        (shared price grid for both dimension and sales-time pricing)
```

---

## Stage 1: Dimension Generation

```
dimensions_runner.py :: generate_dimensions()
  |
  |-- Determine force set
  |     |-- --regen-dimensions flag (specific dims or "all")
  |     |-- Random mode? --> force ALL dims
  |     |-- Expand via force_also (e.g., products --> suppliers)
  |     |-- Expand date-dependent group (any forced --> all forced)
  |     +-- Cascade downstream (forced dim --> all dependents forced)
  |
  |-- Clean .version files for forced dims
  |     force all? --> also clean parquet_dims folder
  |
  v
  For each dim spec in topological order:
  |
  |   +---------------------------------------------+
  |   |  Check: enabled? (config toggle)            |
  |   |  Check: forced? (--regen-dimensions)        |
  |   |  Check: version hash changed?               |
  |   |                                             |
  |   |  Skip if: not enabled AND not forced        |
  |   |  Skip if: version hash unchanged            |
  |   +------------------+--------------------------+
  |                      |
  |                      v
  |              Call spec.run_fn(cfg, parquet_dims)
  |              Detect file changes (mtime + size)
  |              Track: regenerated[dim_name] = bool
  |
  v

  Dimension Dependency Graph (18 specs, ~24 parquet files):

  geography <-------------- stores <----- warehouses
       |                      |  ^             |
       |                      |  +-------------+
       |                      |
       |                      |---- employees
       |                      |        |
       |                      |        v
       |                      |  employee_store_assignments
       |                      |
       v                      v
  (independent)           dates <---- currency <---- exchange_rates

  sales_channels         promotions
  loyalty_tiers
  customer_acq_channels
  time

  customers <---- subscriptions (conditional)
  suppliers <---- products
  return_reason (conditional: returns.enabled)

  Note: warehouses feeds WarehouseKey back into stores.parquet.
  If stores regenerates, warehouses is auto-forced to re-add
  the column (cross-dimension write cascade).
```

---

## Stage 2: Facts Generation

```
sales_runner.py :: run_sales_pipeline()
  |
  |-- Resolve output paths (format-specific)
  |     csv --> fact_out/csv/
  |     parquet --> fact_out/ or fact_out/parquet/
  |     deltaparquet --> fact_out/sales/
  |
  |-- Check returns eligibility
  |     returns.enabled AND sales_output='sales' AND skip_order_cols?
  |     --> warn + disable returns (need OrderNumber to link)
  |
  |-- Load active products
  |     products.parquet --> filter by active_ratio
  |     SCD2? --> filter IsCurrent=1 first
  |     --> numpy array (ProductKey, ListPrice, UnitCost)
  |
  |-- Bind runner globals (State class, sealed after bind)
  |
  v
  +------------------------------------------------------------------+
  |  STEP 1: Sales Generation (multiprocessing pool)                 |
  |                                                                  |
  |  sales.py :: generate_sales_fact()                               |
  |    |                                                             |
  |    |-- Build worker config (plain dict for pickling)             |
  |    |-- Build SCD2 product version lookups (if applicable)        |
  |    |-- Spawn multiprocessing.Pool(workers)                       |
  |    |     Each worker: init_sales_worker()                        |
  |    |       +-- Load shared dimension arrays (zero-copy)          |
  |    |                                                             |
  |    |-- Distribute chunks via imap_unordered                      |
  |    |     Per chunk:                                              |
  |    |       |-- Sample customers (weighted by demand)             |
  |    |       |-- Generate orders (date, store, product, qty)       |
  |    |       |-- Apply pricing pipeline                            |
  |    |       |     inflation --> markdown --> appearance snap      |
  |    |       |-- Apply promotions (random assignment)              |
  |    |       |-- Calculate delivery dates                          |
  |    |       |-- Generate returns (conditional)                    |
  |    |       |-- Stream to accumulators                            |
  |    |       |     budget, inventory, wishlists, complaints        |
  |    |       +-- Write chunk (csv/parquet/delta)                   |
  |    |                                                             |
  |    +-- Return: (chunk_files, row_count, accumulators...)         |
  +----------------------+-------------------------------------------+
                        |
                        v
  +------------------------------------------------------------------+
  |  STEP 2: Secondary Facts (conditional, from accumulators)        |
  |                                                                  |
  |  +-- Budget (if budget.enabled)                                  |
  |  |     budget/ :: run_budget_pipeline()                          |
  |  |     --> budget_yearly.parquet, budget_monthly.parquet         |
  |  |     Low / Medium / High scenarios                             |
  |  |                                                               |
  |  |-- Inventory (if inventory.enabled)                            |
  |  |     inventory/ :: run_inventory_pipeline()                    |
  |  |     --> inventory_snapshot.parquet                            |
  |  |     Monthly grain, ABC classification, shrinkage              |
  |  |                                                               |
  |  |-- Wishlists (if wishlists.enabled)          +                 |
  |  |     wishlists/ :: run_wishlists_pipeline()   | Concurrent     |
  |  |     --> customer_wishlists.parquet           | via Thread-    |
  |  |                                              | PoolExecutor   |
  |  +-- Complaints (if complaints.enabled)        +                 |
  |       complaints/ :: run_complaints_pipeline()                   |
  |       --> customer_complaints.parquet                            |
  +----------------------+-------------------------------------------+
                        |
                        v
```

---

## Stage 3: Packaging & Output

```
  +------------------------------------------------------------------+
  |  STEP 3: Package Output                                          |
  |                                                                  |
  |  packaging/package_output.py :: package_output()                 |
  |    |                                                             |
  |    |-- Create timestamped final folder                           |
  |    |     generated_datasets/YYYYMMDD_HHMMSS/                     |
  |    |                                                             |
  |    |-- Copy dimension .parquet files                             |
  |    |                                                             |
  |    |-- Copy fact tables (format-specific)                        |
  |    |     |-- CSV:    copy chunks + generate SQL scripts          |
  |    |     |             CREATE TABLE, BULK INSERT,                |
  |    |     |             constraints, views                        |
  |    |     |-- Parquet: merge/copy .parquet files                  |
  |    |     +-- Delta:   copy delta tables                          |
  |    |                                                             |
  |    |-- Copy secondary facts                                      |
  |    |     inventory, budget, wishlists, complaints                |
  |    |                                                             |
  |    +-- Copy config.yaml + models.yaml (for reproducibility)      |
  |                                                                  |
  +----------------------+-------------------------------------------+
                        |
                        v
  +------------------------------------------------------------------+
  |  STEP 4: Power BI Project (CSV/Parquet only)                     |
  |                                                                  |
  |  powerbi_packaging.py :: maybe_attach_pbip_project()             |
  |    |                                                             |
  |    |-- deltaparquet? --> skip (not supported)                    |
  |    |                                                             |
  |    |-- Resolve template:                                         |
  |    |     samples/powerbi/templates/{csv|parquet}/                |
  |    |       {Sales PBIP|Orders PBIP|Both PBIP}/                   |
  |    |                                                             |
  |    |-- Copy template into final folder                           |
  |    |                                                             |
  |    +-- Rewrite TMDL M expression                                 |
  |          ContosoFolder --> final_folder path (escaped)           |
  |                                                                  |
  +----------------------+-------------------------------------------+
                        |
                        v
  +------------------------------------------------------------------+
  |  STEP 5: Quality Report (optional, --report flag)                |
  |                                                                  |
  |  Generates .html quality report from final output                |
  |                                                                  |
  +------------------------------------------------------------------+
```

---

## Data Flow Summary

```
                          +----------------+
                          |  config.yaml   |---- Shape & Scale
                          +-------+--------+     (rows, entities, dates,
                                  |               format, toggles)
                                  v
                          +----------------+
                          |  models.yaml   |---- Sales Behavior
                          +-------+--------+     (demand, pricing, quantity,
                                  |               returns, brand popularity)
                                  v
              +----------------------------------------+
              |         Pipeline Runner                |
              |  (override precedence, config merge)   |
              +-------+------------------------+-------+
                      |                        |
                      v                        v
         +----------------------+   +----------------------+
         |    Dimensions        |   |    Facts             |
         |   (sequential)       |   |   (parallel)         |
         |                      |   |                      |
         |  geography           |   |  sales transactions  |
         |  customers           |   |  sales returns       |
         |  products            |   |  budget forecasts    |
         |  stores              |   |  inventory           |
         |  warehouses          |   |  wishlists           |
         |  employees           |   |  complaints          |
         |  dates               |   |                      |
         |  currency            |   |                      |
         |  exchange_rates      |   |                      |
         |  promotions          |   |                      |
         |  subscriptions       |   |                      |
         |  ...                 |   |                      |
         +----------+-----------+   +-----------+----------+
                    |                           |
                    |    +------------------+   |
                    +--->|    Packaging     |<--+
                         |                  |
                         |  Final folder    |
                         |  + SQL scripts   |
                         |  + Power BI      |
                         |  + configs       |
                         +------------------+
```

---

## Key Decision Points

| Decision | Where | Logic |
|----------|-------|-------|
| Skip entire pipeline | `cli.py` | `--refresh-fx-master` → standalone FX refresh, no generation |
| Validate only | `pipeline_runner.py` | `--dry-run` → print config and exit |
| Which stages run | `pipeline_runner.py` | `--only dimensions` or `--only sales` |
| Force-regenerate dims | `dimensions_runner.py` | `--regen-dimensions` + version hash check + force cascading |
| Warehouses cascade | `dimensions_runner.py` | If stores regenerates, warehouses is auto-forced to re-add WarehouseKey |
| Returns auto-disable | `config.py` | `returns.enabled` + `sales_output=sales` + `skip_order_cols=true` |
| Product active set | `sales_runner.py` | `products.active_ratio` filters product catalog at runtime |
| SCD2 version lookup | `sales.py` | `searchsorted` on EffectiveStartDate per product per sale |
| Output format routing | `sales_runner.py` | `file_format` → csv/parquet/deltaparquet path and writer |
| SQL script generation | `packaging/` | CSV format only |
| Power BI attachment | `powerbi_packaging.py` | CSV/Parquet only (deltaparquet skipped) |
| Quality report | `sales_runner.py` | `--report` flag + final folder exists |
| Concurrent secondary facts | `sales_runner.py` | 2+ lightweight pipelines → ThreadPoolExecutor |

---

## File Reference

| Component | Key Files |
|-----------|-----------|
| Entry point | `main.py`, `src/cli.py` |
| Config loading | `src/engine/config/config.py`, `config_schema.py` |
| Pipeline orchestration | `src/engine/runners/pipeline_runner.py` |
| Dimension generation | `src/engine/runners/dimensions_runner.py`, `src/dimensions/` |
| Sales generation | `src/engine/runners/sales_runner.py`, `src/facts/sales/sales.py` |
| Sales workers | `src/facts/sales/sales_worker/`, `sales_logic/`, `sales_models/` |
| Secondary facts | `src/facts/budget/`, `inventory/`, `wishlists/`, `complaints/` |
| Output packaging | `src/engine/packaging/` |
| Power BI | `src/engine/powerbi_packaging.py` |
| Trend presets | `src/utils/trend_presets.py` |
| Shared state | `src/facts/sales/sales_logic/globals.py` (State class) |
| Shared memory | `src/utils/shared_arrays.py` |
