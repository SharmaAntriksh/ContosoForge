---
name: generate-dataset
description: >-
  How to generate and tune a ContosoForge dataset — running main.py, choosing
  output format, sizing rows/customers/stores, picking the business shape (trend
  preset), setting the date range, and selecting the product catalog. Use this
  whenever the user wants to produce data, change the scale or row counts, make
  the charts look a certain way (spikier/smoother/growing/declining/seasonal),
  adjust the date range, choose contoso vs synthetic products, or understand the
  config.yaml-vs-models.yaml split. Also use for "why do my charts look flat",
  "how many customers should I use", "--only sales", "--regen-dimensions", or any
  question about tuning generation output. Reach for it even when the user doesn't
  say "generate" explicitly but is clearly trying to shape the output data.
---

# Generating & Tuning a ContosoForge Dataset

ContosoForge produces deterministic, idempotent retail datasets (dimensional
model). This skill covers the *operator's* view: how to run it, how to size it,
and how to shape the output. It does **not** cover the sales-fact internals
(determinism/parallelism contract) — that lives in the sales code and its
gotchas.

## Mental model

Two-stage pipeline, two config files:

- **`config.yaml`** — *shape & scale*: row counts, entity counts, date range,
  output format, parallelism, feature toggles. Overridable by CLI flags.
- **`models.yaml`** — *behavior & business shape*: demand curves, pricing
  dynamics, quantity distribution, and the **trend preset** (the single source
  of business shape). **Not** overridable by CLI — edit directly or via the web UI.

Pipeline runs **Dimensions** (sequential, ~22 tables) then **Sales facts**
(parallel, multi-worker), then packages a timestamped folder under
`generated_datasets/`.

## Running it (authoritative CLI)

Entry point is `python main.py`. The full flag set (from `src/cli.py`):

```bash
# Core run
python main.py --format parquet --sales-rows 1000000
python main.py --format csv --customers 5000 --sales-rows 100000
python main.py --format deltaparquet --workers 8

# Stage control
python main.py --dry-run                 # print resolved config and exit — do this before big runs
python main.py --only dimensions         # dims only
python main.py --only sales              # sales only (dims must already exist)
python main.py --regen-dimensions products stores   # force-rebuild named dims
python main.py --regen-dimensions all               # force-rebuild every dim
python main.py --clean                   # delete FINAL output folders before running
python main.py --refresh-fx-master       # top up FX master to today, then exit

# Format is one of: csv | parquet | delta | deltaparquet  (delta == deltaparquet)
```

**Every override flag** (all one-time, not persisted; CLI > config.yaml):

| Flag | Overrides |
|---|---|
| `--format` | `sales.file_format` (csv/parquet/delta/deltaparquet) |
| `--sales-rows N` | `sales.total_rows` |
| `--workers N` | `sales.workers` (defaults to CPU count; don't exceed it) |
| `--chunk-size N` | `sales.chunk_size` (perf knob only — never changes data shape) |
| `--row-group-size N` | `sales.row_group_size` (parquet/deltaparquet only) |
| `--skip-order-cols {true,false}` | `sales.skip_order_cols` |
| `--start-date YYYY-MM-DD` | global start date |
| `--end-date YYYY-MM-DD` | global end date |
| `--customers N` | `customers.total_customers` |
| `--stores N` | store count (`stores.num_stores`; legacy `stores.total_stores`) |
| `--products N` | `products.num_products` |
| `--promotions N` | total promotions (distributed across buckets) |
| `--products-scd2 {true,false}` | `products.scd2.enabled` |
| `--customers-scd2 {true,false}` | `customers.scd2.enabled` |
| `--config PATH` / `--models-config PATH` | config file locations |
| `--only {dimensions,sales}` / `--regen-dimensions ...` | stage control |
| `--clean` / `--dry-run` / `--no-report` / `-q`/`--quiet` | run control |

## Choosing the business shape (trend presets)

The trend preset is the **single source of business shape** — it drives the
revenue curve, customer lifecycle, and demand behavior as one coherent story.
Set it in `models.yaml` under `models.macro_demand.trend`. This is the main lever
for "make the charts look like X".

**Directional presets (extend over time):**

| Preset | Revenue shape | Customer curve |
|---|---|---|
| `steady-growth` | Gentle ~5%/yr upward line | Stable base, gradual acquisition |
| `strong-growth` | Exponential acceleration | Continuously growing |
| `gradual-growth` | S-curve with dips | Ramp then level off |
| `hockey-stick` | Explosive years 4–6 | Rapid ramp |
| `decline` | Steady erosion | Shrinking (high churn) |
| `new-market-entry` | Near-zero then accelerating | Very slow then ramping |
| `slow-decline` | Gentle ~10%/yr drop | Gradual erosion |

**Cyclical presets (repeat every 10 years):**

| Preset | Revenue shape | Customer curve |
|---|---|---|
| `boom-and-bust` | Rapid rise then collapse | Rise then crash |
| `recession-recovery` | U-shape dip | Stable (dip from orders) |
| `seasonal-dominant` | Flat trend, strong seasonal | Flat with seasonal waves |
| `seasonal-with-growth` | Growth + retail seasonality | Growing with seasonal waves |
| `plateau` | Growth then flatline | Growth then stable |
| `volatile` | Wild year-to-year swings | Flat with noise |
| `double-dip` | Two downturns | Gradual decline |
| `stagnation` | Perfectly flat | Perfectly flat (no churn) |

Each preset bundles `year_level_factors`, `row_share_of_growth`, `noise_std`,
`shock_probability`, `monthly_seasonality`, plus embedded `lifecycle` (acquisition,
churn) and `customers` (participation, discovery, seasonal spikes) sub-configs.
Resolved by `resolve_trend_preset()` in `src/utils/trend_presets.py`. The old
`customers.profile` key is deprecated but still maps to a preset via
`_PROFILE_TO_TREND` for backward compatibility.

## Sizing: the customers ↔ rows ratio (this is what makes charts look good)

Chart quality depends on the ratio of customers to sales rows over the date range.
**Target ~1.5 orders per customer per month** for spiky, interesting charts.

- **Formula:** `customers ≈ sales_rows / months / 1.5`
- **Too few customers** vs rows → each customer averages many orders, variance
  averages out → overly **smooth** charts.
- **Too many customers** vs rows → sparse purchase history per customer.
- Longer date ranges spread rows over more months, so they need **fewer**
  customers for the same row count.

| Sales rows | 3 yr (36 mo) | 6 yr (72 mo) | 10 yr (120 mo) | 20 yr (240 mo) |
|---|---|---|---|---|
| 2M | 37K | 19K | 11K | 6K |
| 10M | 185K | 93K | 56K | 28K |
| 20M | 370K | 185K | 111K | 56K |
| 100M | 1.85M | 925K | 555K | 278K |

If the user complains charts are "too flat/smooth," the usual fixes are: (1) reduce
customer count toward the table above, or (2) pick a more dynamic trend preset
(`volatile`, `seasonal-dominant`, `boom-and-bust`).

## Product catalog selection

Set under `scale.products` in `config.yaml`:

```yaml
scale:
  products:
    catalog: all          # "contoso" | "synthetic" | "all"
    rows: 7930            # target product count (trims or expands to fit)
```

| Catalog | Products | Brands | Description |
|---|---|---|---|
| `contoso` | 2,517 | 11 | Original ContosoRetailDW products only |
| `synthetic` | 5,633 | 115 | Retail + electronics + audio/media (Nike, Samsung, IKEA, …) |
| `all` | ~7,930 | 126 | Both combined, Contoso house brand trimmed to 490 |

- Source parquets live in `data/contoso_products/` (built by `catalog_builder.py`).
- `rows` expands (variants) when above the base count, or stratified-trims below it.
- `scale.products` also accepts a plain integer (defaults to `catalog: all`).
- Changing `catalog` triggers product-dimension regeneration (it's in the version key).

## Output formats

- **csv** — chunked files + auto-generated SQL scripts (DDL, BULK INSERT/COPY,
  constraints, views) for SQL Server and PostgreSQL. Gets a Power BI `.pbip`.
- **parquet** — merged single file (configurable) + compression/row-group tuning.
  Gets a Power BI `.pbip`.
- **deltaparquet** (`delta`) — partitioned by Year/Month, ACID via delta-rs.
  **Intentionally skips** PBIP generation.

Post-generation compaction: `python scripts/optimize_delta.py <path>` /
`python scripts/optimize_parquet.py <path>`.

## Iteration workflow

- `--dry-run` first to validate config on any large or unfamiliar run.
- Split the loop: `--only dimensions` once, then iterate sales with `--only sales`
  (dims are reused, so you're not rebuilding ~22 tables each time).
- `chunk_size` is a pure performance knob (default 1M is good): too small = overhead,
  too large = memory pressure. It **never** changes the data.
- Output lands in a timestamped folder under `generated_datasets/`.

## Gotchas that bite during generation

- **Dimension versioning:** each dim has `data/versioning/<name>.version.json`
  storing a SHA-256 of its config section. Dims only regenerate when that hash
  changes, the file/parquet is missing, or `--regen-dimensions` forces it. **If a
  config edit "did nothing," the version hash probably didn't change** — force with
  `--regen-dimensions`.
- **Pricing injection:** `models.yaml` pricing appearance rules are injected into the
  product section at startup; product-dim generation and sales-time pricing share one
  price grid. **If you edit pricing bands in `models.yaml`, run
  `--regen-dimensions products`** to resync the dimension.
- **FX dates are coupled:** `exchange_rates` date range is overridden at runtime to
  match `defaults.dates.start/end`. You can't set FX dates independently.
- **Returns/complaints auto-disable:** both are silently disabled at config load if
  `enabled=true` AND `sales_output='sales'` AND `skip_order_cols=true` (both need
  OrderNumber to link back). Watch for the warning in the log.
- **Interrupted runs** may leave scratch behind in `data/`; final output is only the
  packaged `generated_datasets/` folder.

## Web UI alternative

`python -m uvicorn web.api:app --port 8502` — React SPA with YAML editors,
presets, and a log viewer. This is the only way to edit `models.yaml` (including the
trend preset) without hand-editing the file, since it isn't CLI-overridable.
