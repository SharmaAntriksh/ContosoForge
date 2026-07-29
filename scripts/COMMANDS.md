# Command Reference

Quick-copy syntax for terminal. Replace paths with your actual dataset folder.

---

## Generate Data

```bash
# Full run (default config)
python main.py --format parquet --sales-rows 1000000

# Custom scale
python main.py --sales-rows 1083285 --products 8167 --customers 49834 --stores 100

# With SCD2 versioning
python main.py --sales-rows 1083285 --products 8167 --customers 49834 --stores 100 --products-scd2 --customers-scd2

# CSV output with fewer rows
python main.py --format csv --customers 5000 --sales-rows 100000

# Delta Lake output
python main.py --format deltaparquet --workers 8

# Dimensions only (no sales)
python main.py --only dimensions

# Sales only (dims must already exist)
python main.py --only sales

# Force-rebuild specific dimensions
python main.py --regen-dimensions products
python main.py --regen-dimensions stores,employees,employee_store_assignments
python main.py --regen-dimensions all

# Validate config without generating
python main.py --dry-run

# Custom config file
python main.py --config config_extreme.yaml --regen-dimensions all

# Custom date range
python main.py --start-date 2024-01-01 --end-date 2024-12-31

# Refresh FX rates from Yahoo Finance (no generation)
python main.py --refresh-fx-master

# Quiet mode (top-level stages, warnings, and failures only)
python main.py -q --sales-rows 1000000

# Skip quality report
python main.py --no-report

# Delete old output folders before running
python main.py --clean --format parquet --sales-rows 1000000

# Override chunk size and row group size
python main.py --chunk-size 500000 --row-group-size 500000

# Skip order-level columns (line-level only)
python main.py --skip-order-cols true
```

### All main.py flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--format` | str | config | csv, parquet, delta, deltaparquet |
| `--sales-rows` | int | config | Total sales rows |
| `--customers` | int | config | Customer count |
| `--stores` | int | config | Store count |
| `--products` | int | config | Product count |
| `--promotions` | int | config | Total promotions |
| `--workers` | int | config | Parallel worker count |
| `--chunk-size` | int | config | Rows per sales chunk |
| `--row-group-size` | int | config | Parquet row group size |
| `--start-date` | YYYY-MM-DD | config | Global start date |
| `--end-date` | YYYY-MM-DD | config | Global end date |
| `--products-scd2` | bool | config | Enable product SCD2 versioning |
| `--customers-scd2` | bool | config | Enable customer SCD2 versioning |
| `--skip-order-cols` | bool | config | Omit order-level columns |
| `--only` | str | - | Run only `dimensions` or `sales` |
| `--regen-dimensions` | list | - | Force rebuild (csv list or `all`) |
| `--config` | path | config.yaml | Config file path |
| `--models-config` | path | models.yaml | Models config file path |
| `--dry-run` | flag | - | Print resolved config, no generation |
| `--clean` | flag | - | Delete output folders before running |
| `--refresh-fx-master` | flag | - | Top up FX rates and exit |
| `--no-report` | flag | - | Skip data quality report |
| `-q` / `--quiet` | flag | - | Reduce log output |
| `--version` | flag | - | Show version |

---

## Verify Data Quality

```bash
# Employee/store/sales alignment (29 checks, ~4s on 1M rows)
python scripts/verify_employee_store_sales.py "generated_datasets\2026-04-05 05_59_27 PM Customers 43K Sales 1M PARQUET"
```

Exit code 0 = all passed, 1 = failures.

### Checks performed (29 total)

| Category | Checks |
|---|---|
| Stores (6) | Counts, renovation date sanity, closing consistency |
| Employees (5) | Hire/termination dates, IsActive consistency, hierarchy, no manager salespeople |
| ESA Bridge (12) | StartDate <= EndDate, renovation IsPrimary rules, segment completeness, no overlaps, FK integrity, assignment within employment, no rows past closure, salesperson coverage per store-month |
| Sales (6) | No manager keys, 100% ESA match, no sales during renovation, no sales after closure, no post-transfer leakage |

---

## Optimize Parquet

```bash
# Default (zstd compression, 1M row groups, writes to optimized/ subfolder)
python scripts/optimize_parquet.py "generated_datasets\...\PARQUET"

# Preview without writing
python scripts/optimize_parquet.py "generated_datasets\...\PARQUET" --dry-run

# Snappy (faster write, larger files -- Power Query default)
python scripts/optimize_parquet.py "generated_datasets\...\PARQUET" -c snappy

# Max zstd compression (slower write, smallest files)
python scripts/optimize_parquet.py "generated_datasets\...\PARQUET" -c zstd -l 19

# Smaller row groups (better for column pruning)
python scripts/optimize_parquet.py "generated_datasets\...\PARQUET" -r 500000

# Overwrite originals
python scripts/optimize_parquet.py "generated_datasets\...\PARQUET" --in-place

# Snappy + custom row groups + overwrite
python scripts/optimize_parquet.py "generated_datasets\...\PARQUET" -c snappy -r 1_000_000 --in-place

# No compression (fastest write, largest files)
python scripts/optimize_parquet.py "generated_datasets\...\PARQUET" -c none
```

### All optimize_parquet.py flags

| Flag | Short | Type | Default | Description |
|---|---|---|---|---|
| `dataset_dir` | | path | (required) | Dataset folder path |
| `--compression` | `-c` | str | zstd | snappy, zstd, gzip, brotli, lz4, none |
| `--level` | `-l` | int | auto | Compression level (codec-dependent, e.g. zstd 1-22) |
| `--row-group-size` | `-r` | int | 1000000 | Target rows per row group |
| `--in-place` | | flag | - | Overwrite originals (default: writes to optimized/) |
| `--dry-run` | | flag | - | List files without writing |

---

## Optimize Delta Lake

Compacts small Delta files into fewer, larger ones. Safe to run multiple times.

```bash
# Default (256 MB target, compact + vacuum)
python scripts/optimize_delta.py "generated_datasets\...\DELTAPARQUET"

# Larger target file size
python scripts/optimize_delta.py "generated_datasets\...\DELTAPARQUET" --target-size 512

# Recompress with ZSTD during compaction
python scripts/optimize_delta.py "generated_datasets\...\DELTAPARQUET" --compression zstd

# Limit concurrency
python scripts/optimize_delta.py "generated_datasets\...\DELTAPARQUET" --max-tasks 4

# Skip small tables (default: skip tables with 5 or fewer files)
python scripts/optimize_delta.py "generated_datasets\...\DELTAPARQUET" --min-files 10

# Compact tables that have as few as 2 files
python scripts/optimize_delta.py "generated_datasets\...\DELTAPARQUET" --min-files 1
```

### All optimize_delta.py flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `folder` | path | (required) | Dataset folder to scan for Delta tables |
| `--target-size` | int | 256 | Target file size in MB |
| `--max-tasks` | int | CPU count | Max concurrent compaction tasks |
| `--min-files` | int | 5 | Skip tables with fewer files than this |
| `--compression` | str | keep existing | Recompress during compaction (case-insensitive: snappy, zstd, gzip, brotli, lz4, lz4_raw, uncompressed) |

---

## Repartition Delta Lake

Changes the partition layout of existing Delta tables. Streams per-partition to keep memory low. Only processes partitioned fact tables (dimensions are skipped automatically).

```bash
# Demote Year+Month -> Year (fewer, larger files)
python scripts/repartition_delta.py "generated_datasets\...\DELTAPARQUET" --partition-by year

# Promote Year -> Year+Month (finer partition pruning)
python scripts/repartition_delta.py "generated_datasets\...\DELTAPARQUET" --partition-by year-month

# Remove partitions entirely (single consolidated Delta table per fact)
python scripts/repartition_delta.py "generated_datasets\...\DELTAPARQUET" --partition-by none

# Recompress while repartitioning
python scripts/repartition_delta.py "generated_datasets\...\DELTAPARQUET" --partition-by year --compression zstd
```

### Typical workflows

```bash
# Generated with Year+Month but want fewer files for small datasets
python scripts/repartition_delta.py "generated_datasets\...\DELTAPARQUET" --partition-by year

# Generated with Year only but want finer pruning for large datasets
python scripts/repartition_delta.py "generated_datasets\...\DELTAPARQUET" --partition-by year-month

# Remove partitions and compact into minimal files
python scripts/repartition_delta.py "generated_datasets\...\DELTAPARQUET" --partition-by none
python scripts/optimize_delta.py "generated_datasets\...\DELTAPARQUET" --min-files 1
```

### All repartition_delta.py flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `folder` | path | (required) | Dataset folder to scan for Delta tables |
| `--partition-by` | str | (required) | Target layout: `none`, `year`, or `year-month` |
| `--compression` | str | keep existing | Recompress during repartition (case-insensitive: snappy, zstd, gzip, brotli, lz4, lz4_raw, uncompressed) |

---

## SQL Server Import (CSV datasets)

```powershell
# Windows auth + columnstore + verification
.\scripts\run_sql_server_import.ps1 `
  -RunPath "generated_datasets\2026-04-05 03_10_42 PM Customers 43K Sales 1M CSV" `
  -Server "SUMMER\SQL2022" `
  -Database Orders_1M `
  -TrustedConnection `
  -ApplyCCI $true `
  -DropPK $true `
  -Verify

# SQL auth
.\scripts\run_sql_server_import.ps1 `
  -RunPath "generated_datasets\...\CSV" `
  -Server "localhost\SQLEXPRESS" `
  -Database SalesData `
  -User sa `
  -Password "YourPassword"

# Custom ODBC driver
.\scripts\run_sql_server_import.ps1 `
  -RunPath "generated_datasets\...\CSV" `
  -Server "localhost" `
  -Database SalesData `
  -TrustedConnection `
  -OdbcDriver "ODBC Driver 18 for SQL Server"

# Show full paths in log output
.\scripts\run_sql_server_import.ps1 `
  -RunPath "generated_datasets\...\CSV" `
  -Server "SUMMER\SQL2022" `
  -Database Orders_1M `
  -TrustedConnection `
  -ShowFullPaths
```

### All run_sql_server_import.ps1 parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `-RunPath` | string | (required) | Path to generated CSV dataset folder |
| `-Server` | string | (required) | SQL Server instance name |
| `-Database` | string | (required) | Target database name |
| `-TrustedConnection` | switch | - | Use Windows authentication |
| `-User` | string | - | SQL auth username (alternative to TrustedConnection) |
| `-Password` | string | - | SQL auth password |
| `-ApplyCCI` | bool | $false | Apply clustered columnstore indexes after import |
| `-DropPK` | bool | $false | Drop PK/FK constraints after import |
| `-Verify` | switch | - | Run verification procs after import |
| `-OdbcDriver` | string | auto | Override ODBC driver name |
| `-PythonExe` | string | python | Override Python executable |
| `-ShowFullPaths` | switch | - | Show full paths in log output |

---

## Profiling

```bash
# Profile full sales pipeline (cProfile)
python scripts/profile_sales.py --rows 500000

# Profile with specific format and multiple workers
python scripts/profile_sales.py --rows 1000000 --format csv --workers 4

# Validate config only (no profiling)
python scripts/profile_sales.py --dry-run

# Profile single worker in-process
python scripts/profile_worker.py --rows 250000
```

Output: `scripts/profile_output/*.prof` (open with `snakeviz`) + `*_top50.txt` summaries.

### All profile_sales.py flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--rows` | int | 1000000 | Total sales rows |
| `--format` | str | parquet | Output format |
| `--workers` | int | 1 | Worker count (1 for clean profile) |
| `--dry-run` | flag | - | Validate config only |

### All profile_worker.py flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--rows` | int | 500000 | Sales rows to generate |

---

## Utilities

```bash
# Rebuild geography master data (one-time, no arguments)
python scripts/build_geography_master.py

# Print project tree (optional: specify root path)
python scripts/print_project_tree.py
python scripts/print_project_tree.py generated_datasets/2026-01-15_run

# Print config summary as JSON (used by run_generator.ps1)
python scripts/helper_print_config_summary.py config.yaml
```

---

## Web UI

```bash
python -m uvicorn web.api:app --port 8502
```

The server uses committed, self-contained assets from `web/frontend/dist/`.
Only frontend contributors need Node.js:

```powershell
cd web\frontend
npm ci
npm run build
npm run build:check
```
