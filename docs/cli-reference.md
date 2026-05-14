# CLI Reference

Complete flag reference for `python main.py`. All CLI flags override their corresponding `config.yaml` values for the current run only — they are not persisted back to the file.

```
python main.py [OPTIONS]
```

---

## Quick reference

| Flag | Description |
|---|---|
| `--version` | Print version and exit |
| `--format` | Output format: `csv`, `parquet`, `delta`, `deltaparquet` |
| `--sales-rows N` | Number of sales rows to generate |
| `--customers N` | Number of customers |
| `--stores N` | Number of stores |
| `--products N` | Number of products |
| `--promotions N` | Total promotions (distributed across types) |
| `--start-date YYYY-MM-DD` | Override global start date |
| `--end-date YYYY-MM-DD` | Override global end date |
| `--workers N` | Parallel worker count (default: auto-detect from CPU count) |
| `--chunk-size N` | Rows per processing chunk (default 1,000,000) |
| `--row-group-size N` | Parquet row group size |
| `--skip-order-cols` | Omit `SalesOrderNumber` / `LineNumber` columns |
| `--only dimensions\|sales` | Run only one pipeline stage |
| `--regen-dimensions [names]` | Force regeneration of specific dimensions (or `all`) |
| `--refresh-fx-master` | Top up FX rates via Yahoo Finance and exit (no pipeline run) |
| `--clean` | Delete the final output folder before running |
| `--dry-run` | Print resolved config and exit without generating |
| `--config PATH` | Path to config file (default: `config.yaml`) |
| `--models-config PATH` | Path to models config file (default: `models.yaml`) |
| `--products-scd2 [true\|false]` | Override `products.scd2.enabled` |
| `--customers-scd2 [true\|false]` | Override `customers.scd2.enabled` |
| `--no-report` | Skip the post-generation data quality report |
| `-q`, `--quiet` | Reduce log output to top-level stages, warnings, and failures |

---

## Output

### `--format`
Output format. One of `csv`, `parquet`, `delta`, `deltaparquet` (delta and deltaparquet are aliases). Overrides `sales.file_format`.

```bash
python main.py --format parquet
python main.py --format csv
python main.py --format deltaparquet
```

CSV mode additionally generates SQL Server bootstrap scripts. Parquet mode produces a single merged file per table by default.

### `--row-group-size N`
Target rows per Parquet row group. Larger values give better compression and faster scans but higher memory pressure when reading. Default is 1,000,000.

```bash
python main.py --format parquet --row-group-size 500000
```

### `--skip-order-cols`
Omit `SalesOrderNumber` and `LineNumber` columns from the sales fact. Saves space when those columns aren't needed. Note: returns are auto-disabled when this flag is on with `sales_output='sales'`, because returns need `SalesOrderNumber` to link back.

---

## Scale

### `--sales-rows N`
Primary driver of dataset size and run time. Overrides `scale.sales_rows`.

### `--customers N` / `--stores N` / `--products N` / `--promotions N`
Override entity counts in `scale.*`. For products, this trims or expands the catalog selected by `scale.products.catalog`.

```bash
python main.py --sales-rows 100000 --customers 5000 --stores 50 --products 500
```

---

## Dates

### `--start-date` / `--end-date`
Override `defaults.dates.start` and `defaults.dates.end`. Use `YYYY-MM-DD` format. The exchange-rates date range is automatically clamped to this window — you cannot set FX dates independently.

```bash
python main.py --start-date 2022-01-01 --end-date 2025-12-31
```

---

## Parallelism

### `--workers N`
Number of parallel worker processes. Defaults to CPU count. Don't exceed CPU count — diminishing returns past that point.

### `--chunk-size N`
Rows per processing chunk inside each worker. Too small = scheduling overhead dominates. Too large = high peak memory. Default 1,000,000 is a good baseline.

---

## Pipeline control

### `--only dimensions`
Generate dimensions only. Useful when iterating on dimension config (e.g. tuning customer profiles) before committing to a long sales run.

### `--only sales`
Generate sales only. Requires dimensions to already exist on disk (run `--only dimensions` first, or any prior full run). Lets you iterate on `models.yaml` without rebuilding dims.

### `--regen-dimensions [names]`
Force regeneration of specific dimensions even if their version hash is unchanged. Use this after editing pricing bands in `models.yaml` (which don't bump the products dim hash automatically).

```bash
# Force-rebuild products dimension only
python main.py --regen-dimensions products

# Force-rebuild multiple dimensions
python main.py --regen-dimensions customers products

# Force-rebuild everything
python main.py --regen-dimensions all
```

### `--refresh-fx-master`
Re-download FX rates from Yahoo Finance, update the master cache file, and **exit**. This does not run the pipeline — it's a maintenance command. Run it separately before a multi-year generation when extending the date range past previously-fetched data, then run the pipeline normally.

```bash
# 1. Top up the FX master
python main.py --refresh-fx-master

# 2. Then run the pipeline
python main.py --start-date 2020-01-01 --end-date 2025-12-31
```

Other CLI flags (`--start-date`, `--end-date`, `--sales-rows`, etc.) are ignored when `--refresh-fx-master` is set.

### `--clean`
Delete the final output folder (whatever `defaults.final_output` points to, `generated_datasets/` by default) before running. Be careful — this wipes prior runs.

### `--dry-run`
Print the fully resolved config (after merging CLI overrides) and exit without generating data. Use this to validate a complex invocation before committing to a long run.

```bash
python main.py --sales-rows 100000000 --workers 16 --dry-run
```

---

## Config files

### `--config PATH`
Path to the `config.yaml` file. Default is `config.yaml` in the project root.

### `--models-config PATH`
Path to the `models.yaml` file. Default is `models.yaml` in the project root.

```bash
python main.py --config configs/big-run.yaml --models-config configs/seasonal.yaml
```

---

## SCD2 toggles

### `--products-scd2 [true|false]`
Override `products.scd2.enabled`. Controls whether the product dimension emits SCD2 version rows (price revisions over time) or only the latest snapshot. Accepts `true`/`false`/`1`/`0`/`yes`/`no`. Passing the flag without a value implies `true`.

```bash
python main.py --products-scd2 false       # snapshot products dim
python main.py --products-scd2             # equivalent to --products-scd2 true
```

### `--customers-scd2 [true|false]`
Override `customers.scd2.enabled`. Controls whether the customer dimension emits SCD2 life-event rows (marriage, relocation, career change) or only the current state.

```bash
python main.py --customers-scd2 false
```

---

## Logging & reporting

### `--no-report`
Skip the post-generation data quality report (FK checks, distribution stats, warnings). The report is generated by default.

### `-q`, `--quiet`
Reduce log output to top-level stages, warnings, and failures only. Useful for batch / CI runs where per-chunk progress lines aren't wanted.

```bash
python main.py --quiet --sales-rows 50000000
```

---

## Common invocation patterns

### Iterating on dimension config
```bash
# First pass — generate dims, don't waste time on sales yet
python main.py --only dimensions --customers 50000

# Tweak config, force regen, repeat
python main.py --only dimensions --regen-dimensions customers
```

### Iterating on sales behavior (`models.yaml`)
```bash
# One-time dim generation
python main.py --only dimensions

# Iterate sales-only runs against the same dims
python main.py --only sales --sales-rows 1000000
python main.py --only sales --sales-rows 1000000   # edit models.yaml between runs
```

### Validating a large run before committing
```bash
python main.py --sales-rows 200000000 --workers 16 --format parquet --dry-run
```

### After editing pricing bands in `models.yaml`
```bash
# Force product dim rebuild so dim and sales-time pricing stay in sync
python main.py --regen-dimensions products
```

### Force full dimension rebuild
```bash
# Rebuild every dimension from scratch, then run the full pipeline
python main.py --regen-dimensions all --format parquet
```
Use after large config changes (seed, date range, region mix) where multiple dimensions are affected and you'd rather wipe all version hashes than track which ones changed.

### Reproducible benchmark runs
```bash
# Same seed (in config.yaml) + same flags = byte-identical output
python main.py --clean --sales-rows 1000000 --format parquet
```

### Tiny smoke-test run (fast feedback during development)
```bash
python main.py \
  --format parquet \
  --sales-rows 10000 \
  --customers 500 \
  --stores 10 \
  --products 100 \
  --workers 2 \
  --clean
```

### Large production-scale run
```bash
python main.py \
  --format parquet \
  --sales-rows 100000000 \
  --customers 555000 \
  --stores 1000 \
  --products 10000 \
  --start-date 2015-01-01 \
  --end-date 2025-12-31 \
  --workers 16 \
  --chunk-size 2000000 \
  --row-group-size 1000000 \
  --clean
```

### CSV run for SQL Server import
```bash
# Generate CSV with auto-generated SQL Server bootstrap scripts
python main.py \
  --format csv \
  --sales-rows 20000000 \
  --customers 200000 \
  --workers 8 \
  --clean
```
Then load with `scripts/run_sql_server_import.ps1` — see [sql-server-import](operations/sql-server-import.md).

### Delta Lake run with partitioned facts
```bash
python main.py \
  --format deltaparquet \
  --sales-rows 50000000 \
  --customers 280000 \
  --workers 12 \
  --clean
```
Then compact with `scripts/optimize_delta.py` — see [delta-optimization](operations/delta-optimization.md).

### Trying a new business shape (edit `models.yaml` first)
```bash
# 1. Generate dims once (independent of macro_demand.trend)
python main.py --only dimensions

# 2. Edit models.yaml — set macro_demand.trend to e.g. "boom-and-bust"

# 3. Run sales only against the existing dims, multiple times if needed
python main.py --only sales --sales-rows 2000000
```

### Refreshing FX rates before a multi-year run
```bash
# 1. Top up the FX master file (this exits immediately)
python main.py --refresh-fx-master

# 2. Then run the pipeline
python main.py --start-date 2020-01-01 --end-date 2025-12-31
```

### Using alternate config files (e.g. multiple presets)
```bash
python main.py \
  --config configs/seasonal-retail.yaml \
  --models-config configs/seasonal-retail-models.yaml \
  --clean
```

### Capturing the resolved config for review
```bash
# Print fully merged config to a file, don't generate
python main.py --sales-rows 100000000 --workers 16 --dry-run > resolved-config.txt
```

---

## Override precedence

```
CLI flags  >  config.yaml values  >  built-in defaults
```

CLI overrides apply to the **current run only**. They are not written back to `config.yaml`. To persist, edit the file directly or use the web UI.
