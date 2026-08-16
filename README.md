# ContosoForge

Create realistic, analytics-ready retail data for Power BI, SQL, notebooks,
lakehouses, demos, training, and benchmarking.

ContosoForge generates a complete dimensional model inspired by
ContosoRetailDW: customers, products, stores, employees, dates, sales, returns,
budgets, inventory, wishlists, complaints, and more. Every run is deterministic,
schema-stable, and repeatable.

You can start with the web interface and never touch YAML, or use the full CLI
and configuration system when you need precise control.

![ContosoForge generator web interface](docs/assets/web-interface.png)

## Choose how you want to use it

| I want to... | Recommended path |
|---|---|
| Generate a dataset without editing files | [Use the web interface](#option-a-use-the-web-interface-recommended-for-new-users) |
| Run a ready-made dataset from PowerShell | [Use the one-command generator](#option-b-generate-from-powershell) |
| Automate or precisely tune generation | [Use the CLI and YAML configuration](#customize-your-dataset) |
| Import the result into a database | Generate CSV, then use the [SQL import tools](#database-imports) |

If this is your first visit, use the web interface. It exposes the same
generator through a friendlier workflow and includes configuration editors,
presets, validation, progress, and logs.

## Quick start on Windows

Windows PowerShell is the project's primary and best-supported environment.

### 1. Install Git and uv

You need:

- [Git](https://git-scm.com/downloads)
- [uv](https://docs.astral.sh/uv/), which installs the correct Python version
  and the project's locked dependencies

Install uv from PowerShell:

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

If uv was just installed, fully close and reopen Windows Terminal, PowerShell,
or VS Code so the updated `PATH` is available.

### 2. Download and set up ContosoForge

```powershell
git clone https://github.com/SharmaAntriksh/ContosoForge.git
cd ContosoForge
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.\scripts\create_venv.ps1
```

The setup script creates `.venv`, provisions Python 3.13, and installs the
locked dependencies. Administrator rights are not required.

If you prefer not to change your PowerShell execution policy, run scripts with
a one-time bypass:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\create_venv.ps1
```

### 3. Generate your first dataset

Choose either option below.

### Option A: Use the web interface (recommended for new users)

```powershell
.\scripts\run_web.ps1
```

Your browser opens to `http://127.0.0.1:8502` in **Quick Start**. Work through
the six short steps:

1. Pick a dataset size. Start with **Quick sample**.
2. Choose the date range.
3. Choose the business story you want the charts to show.
4. Choose Parquet, CSV, or Delta Lake and a product catalog.
5. Choose how many extra business tables you want.
6. Review the estimate, select **Apply these settings**, then
   **Generate dataset**.

The choices are kept in server memory for this browser session; Quick Start
does not overwrite `config.yaml` or `models.yaml`. Progress and bounded live
logs appear below the review while generation is running.

![ContosoForge pipeline run status](docs/assets/web-pipeline-run.png)

Press `Ctrl+C` in PowerShell when you want to stop the web server.

The web interface is self-contained: its production JavaScript and styles are
included in the repository, so launching it does not require Node.js or access
to public CDNs.

Use **Advanced Setup** when you need every scale, model, YAML, or performance
control. After a run, open **Data Preview** to browse any generated CSV,
Parquet, or Delta table without leaving the web interface. CSV runs also expose
the SQL Server and PostgreSQL wizard under **Database Import**.

### Option B: Generate from PowerShell

Run the checked-in `config.yaml` and `models.yaml`:

```powershell
.\scripts\run_generator.ps1
```

The checked-in defaults showcase most of the available facts and dimensions.
For a smaller, easier-to-explore first dataset:

```powershell
.\scripts\run_generator.ps1 `
  -Format parquet `
  -SalesRows 100000 `
  -Customers 1500 `
  -Stores 25 `
  -Products 500
```

CLI overrides affect only that run; they do not modify `config.yaml`.

Before a large or unfamiliar run, validate the resolved configuration without
generating data:

```powershell
.\scripts\run_generator.ps1 -DryRun
```

Runtime and disk usage depend on row count, date range, enabled facts, output
format, and hardware. Start small before generating millions of sales rows.

## Find and open the result

Completed runs appear in a timestamped folder under `generated_datasets/`.

```text
generated_datasets/
`-- <timestamped run>/
    |-- Sales PBIP/            # CSV and Parquet only
    |   `-- Sales.pbip
    |-- config/
    |   |-- config.yaml
    |   `-- models.yaml
    |-- dimensions/
    |-- facts/
    `-- sql/                  # CSV output only
```

For CSV and Parquet output, open `Sales PBIP/Sales.pbip` in Power BI Desktop.
The generated project already points to the files in its dataset folder.

Delta Lake output intentionally does not include a Power BI Project.

![Generated ContosoForge dataset folder](docs/assets/output.png)

## What gets generated

Each run can produce a complete retail analytics model rather than an isolated
sales table.

| Area | Examples |
|---|---|
| Customers | Customers, profiles, organizations, loyalty tiers, acquisition channels, subscriptions |
| Products | Products, product profiles, categories, subcategories, suppliers, promotions |
| Retail operations | Stores, employees, store assignments, warehouses, geography |
| Time and currency | Calendar, fiscal and weekly-fiscal dates, time, currencies, exchange rates |
| Business facts | Sales, returns, budgets, inventory, wishlists, complaints |

Optional facts can be enabled or disabled independently. The full table list is
available in [Complete generated model](#complete-generated-model).

## Pick an output format

If you are unsure, choose **Parquet**.

| Format | Choose it when... | Included extras |
|---|---|---|
| `parquet` | You use Power BI, Python, notebooks, DuckDB, or general analytics | Merged Parquet files and a Power BI Project |
| `csv` | You want plain-text files or plan to import into SQL Server/PostgreSQL | CSV chunks, database bootstrap scripts, and a Power BI Project |
| `deltaparquet` | You are testing Delta Lake or lakehouse workflows | Partitioned Delta tables; no Power BI Project |

`delta` is accepted as an alias for `deltaparquet`.

## Customize your dataset

There are three levels of control. Start at the top and go deeper only when you
need to.

### Level 1: One-run CLI overrides

These are the most useful controls for everyday runs:

```powershell
.\scripts\run_generator.ps1 `
  -Format parquet `
  -SalesRows 1000000 `
  -Customers 12000 `
  -Stores 100 `
  -Products 2000 `
  -StartDate 2022-01-01 `
  -EndDate 2025-12-31 `
  -Workers 8
```

The equivalent direct Python command is useful for automation and non-Windows
systems:

```bash
uv run python main.py \
  --format parquet \
  --sales-rows 1000000 \
  --customers 12000 \
  --stores 100 \
  --products 2000 \
  --start-date 2022-01-01 \
  --end-date 2025-12-31 \
  --workers 8
```

See the [CLI reference](docs/cli-reference.md) for every flag and precedence
rule.

> `--clean` deletes previous final output folders before generation. Use it only
> when you intentionally want to remove those generated datasets.

### Level 2: Shape and scale in `config.yaml`

`config.yaml` controls what is generated and how large it is:

- Sales rows, customers, products, stores, and promotions
- Date range and output location
- CSV, Parquet, or Delta output
- Optional facts such as returns, budgets, inventory, wishlists, and complaints
- Product/customer history tracking
- Fiscal calendars, exchange rates, staffing, warehouses, and subscriptions
- Performance settings such as workers, chunk size, compression, and partitions

The first `scale` block is the safest place to begin. Most users should leave
the lower-level performance and modeling settings unchanged until they have run
the defaults successfully.

See the complete [`config.yaml` guide](docs/CONFIG_GUIDE.md).

### Level 3: Business behavior in `models.yaml`

`models.yaml` controls how the generated business behaves:

- Overall growth or decline
- Seasonality, shocks, and volatility
- Customer acquisition, participation, and churn
- Product demand and brand popularity
- Basket quantity and pricing behavior
- Discounts, markdowns, and returns

The most important setting is:

```yaml
models:
  macro_demand:
    trend: gradual-growth
```

Useful trend presets include:

| Desired story | Preset |
|---|---|
| Stable retail growth | `steady-growth` |
| Strong accelerating growth | `strong-growth` |
| Growth that eventually levels off | `gradual-growth` |
| Explosive late growth | `hockey-stick` |
| Strong retail seasonality plus growth | `seasonal-with-growth` |
| Mostly seasonal with little long-term growth | `seasonal-dominant` |
| Recession followed by recovery | `recession-recovery` |
| Rapid rise followed by a crash | `boom-and-bust` |
| Unpredictable year-to-year movement | `volatile` |
| Long-term erosion | `decline` or `slow-decline` |

The trend preset is not currently a CLI override. Change it in `models.yaml` or
through the web interface. See the [models and trend preset guide](docs/MODELS_GUIDE.md).

### Keep customers, rows, and dates in balance

The ratio between customers, sales rows, and months affects how convincing the
result feels.

- Too many customers for the number of sales rows creates sparse purchase
  histories.
- Too few customers creates frequent repeat purchases and can make aggregate
  patterns appear overly smooth.
- Longer date ranges spread the same number of sales rows across more months.

A useful starting target for detailed customer analysis is approximately:

```text
customers = sales rows / months / 1.5
```

This is guidance, not a validation rule. A broad customer dimension with sparse
sales can be appropriate for acquisition or segmentation scenarios. See
[Scaling tips](docs/CONFIG_GUIDE.md#scaling-tips) for examples.

### Choose a product catalog

Set the catalog in `config.yaml`:

```yaml
scale:
  products:
    catalog: all
    rows: 7930
```

| Catalog | Base products | Description |
|---|---:|---|
| `contoso` | 2,517 | Original ContosoRetailDW products |
| `synthetic` | 5,633 | Expanded retail, electronics, audio, and media catalog |
| `all` | About 7,930 | Combined catalog |

The generator can stratified-trim a catalog or create variants to reach the
requested row count.

## Advanced generation workflows

### Generate dimensions and sales separately

Dimensions are generated sequentially; sales facts use parallel workers. When
iterating, you can build dimensions once and reuse them:

```powershell
.\scripts\run_generator.ps1 -Only dimensions
.\scripts\run_generator.ps1 -Only sales
```

Sales-only generation requires existing dimensions.

### Force a dimension to regenerate

Dimensions use configuration hashes to avoid unnecessary work. If you need to
force a rebuild:

```powershell
.\scripts\run_generator.ps1 -RegenDimensions products
.\scripts\run_generator.ps1 -RegenDimensions customers,products,stores
.\scripts\run_generator.ps1 -RegenDimensions all
```

If you change pricing bands in `models.yaml`, regenerate the product dimension
so dimension pricing and sales-time pricing remain synchronized.

### Refresh foreign-exchange history

```powershell
uv run python main.py --refresh-fx-master
```

Exchange-rate dates follow the global dataset date range.

### Performance controls

- `workers` controls parallel sales workers. Do not set it higher than your
  available CPU count.
- `chunk_size` is a performance and memory setting; it does not change the
  generated business shape.
- Small chunks add overhead. Very large chunks increase memory pressure.
- The default chunk size of one million rows is a sensible starting point.

## Complete generated model

The model is lightly snowflaked and designed for BI, analytics engineering,
data modeling, and database exercises.

### Dimension and bridge tables

| Group | Tables |
|---|---|
| Customers and accounts | Customers, CustomerProfile, OrganizationProfile, Plans, CustomerSubscriptions, LoyaltyTiers, CustomerAcquisitionChannels |
| Products and catalog | Products, ProductProfile, ProductCategory, ProductSubcategory, Suppliers, Promotions |
| Locations and organization | Stores, Warehouses, Employees, EmployeeStoreAssignments, Geography |
| Time | Dates with calendar/fiscal/weekly-fiscal attributes, Time |
| Currency | Currency, ExchangeRates, monthly exchange rates |
| Other lookups | Channels, ReturnReason |

### Fact tables

- Sales, or split OrderHeader and OrderDetail output
- Returns
- BudgetYearly and BudgetMonthly
- InventorySnapshot
- CustomerWishlists
- Complaints

### Output folder structure

```text
generated_datasets/
`-- <timestamped run>/
    |-- Sales PBIP/                 # CSV and Parquet only
    |-- config/                     # effective config.yaml and models.yaml
    |-- dimensions/
    |-- facts/
    |   |-- sales/
    |   |-- returns/
    |   |-- budget/
    |   |-- inventory/
    |   |-- customer_wishlists/
    |   `-- complaints/
    `-- sql/                        # CSV only
        |-- sqlserver/
        `-- postgres/
```

The packaged configuration records the settings used for that run.

## Database imports

CSV output includes generated SQL Server and PostgreSQL bundles with table DDL,
load scripts, constraints, views, verification procedures, and indexes.

| Task | Command or documentation |
|---|---|
| Import from the web UI | Open **Database Import**, choose SQL Server or PostgreSQL, then select a CSV run |
| Import into SQL Server | [`scripts/run_sql_server_import.ps1`](docs/operations/sql-server-import.md) |
| Import into PostgreSQL | [`scripts/run_postgres_import.ps1`](docs/operations/postgres-import.md) |
| Provision a SQL login for SSAS/Power BI | [Tabular user guide](docs/operations/tabular-user.md) |
| Manage keys and verify an import | [Post-import procedures](docs/operations/post-import-procedures.md) |
| Interpret measures, events, promotions, and lifecycle rules | [Analytical semantics](docs/ANALYTICAL_SEMANTICS.md) |

Generated identifiers are PascalCase. At import time you can choose
`pascal` or `snake`; the import tool regenerates the SQL bundle without changing
the CSV or Parquet data. The PostgreSQL wizard recommends `snake` so ordinary
queries do not need quoted identifiers.

![Database import wizard showing SQL Server settings](docs/assets/web-sqlserver-import.png)

## Post-generation utilities

| Task | Script | Guide |
|---|---|---|
| Recompress or change row groups in Parquet | `scripts/optimize_parquet.py` | [Parquet optimization](docs/operations/parquet-optimization.md) |
| Compact small Delta Lake files | `scripts/optimize_delta.py` | [Delta optimization](docs/operations/delta-optimization.md) |
| Change Delta Lake partition layout | `scripts/repartition_delta.py` | [Delta repartitioning](docs/operations/delta-repartitioning.md) |

## macOS and Linux setup

Install [uv](https://docs.astral.sh/uv/), clone the repository, and synchronize
the locked environment:

```bash
git clone https://github.com/SharmaAntriksh/ContosoForge.git
cd ContosoForge
uv sync --extra sql --extra postgres
```

Generate data:

```bash
uv run python main.py --format parquet --sales-rows 100000
```

Launch the web interface:

```bash
uv run python -m uvicorn web.api:app --port 8502
```

Then open `http://127.0.0.1:8502`.

The database drivers are optional. For a smaller core-only environment, use:

```bash
uv sync
```

## Troubleshooting

### `uv` was installed but is not found

Fully close and reopen the terminal application. A new tab may inherit the old
environment from Windows Terminal or VS Code.

For the current PowerShell session, you can also use:

```powershell
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
```

### PowerShell blocks the project scripts

Allow locally created scripts for your user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Or use a one-time bypass:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_generator.ps1
```

### Python reports a missing package

Use `.\scripts\run_generator.ps1` or `uv run python main.py` so the project
environment is selected. A bare `python main.py` may use an unrelated system
Python.

### The first command appears slow

The first Python start on Windows can take approximately 10–20 seconds while
Windows Defender scans new package files, Python creates bytecode caches, and
the operating system warms its file cache. Later starts are usually faster.

### A configuration edit did not rebuild a dimension

Dimension files are versioned by configuration hash. Force regeneration:

```powershell
.\scripts\run_generator.ps1 -RegenDimensions products
```

### An interrupted run left files behind

Workers use `data/` for scratch output. Completed, self-contained datasets are
the timestamped folders under `generated_datasets/`.

## Power BI model

CSV and Parquet runs include a Power BI Project with preconfigured file paths.
Open the `.pbip` file directly in Power BI Desktop.

![Power BI model diagram](docs/assets/data-model-diagram-view.png)

## Testing and contributing

The project includes tests for configuration, dimensions, facts, determinism,
pricing, SQL generation, PostgreSQL behavior, packaging, the web API, and
end-to-end integration.

On Windows, use the project interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest tests/test_geography.py
.\.venv\Scripts\python.exe -m pytest -k "deterministic"
```

On macOS or Linux:

```bash
uv run pytest
```

### Frontend development

Node.js is needed only when changing files under `web/frontend/`. The checked-in
production assets are already used by `run_web.ps1`.

```powershell
cd web\frontend
npm ci
npm run build
npm run build:check
```

The build produces content-hashed assets under `web/frontend/dist/`. Commit
those generated files with the frontend source change. Use `npm run dev` to
rebuild automatically while working; see the
[frontend development guide](web/frontend/README.md).

## Documentation

| Topic | Guide |
|---|---|
| Full CLI flag reference | [CLI reference](docs/cli-reference.md) |
| Every `config.yaml` setting | [Configuration guide](docs/CONFIG_GUIDE.md) |
| Business models and trend presets | [Models guide](docs/MODELS_GUIDE.md) |
| Pipeline architecture | [Pipeline flowchart](docs/PIPELINE_FLOWCHART.md) |
| Parquet, Delta, and database operations | [Operations guides](docs/operations/) |

## License

ContosoForge is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE.md).

You may use, modify, and share it for noncommercial purposes. Commercial use,
including selling it, is not permitted. Keep the attribution notice
`Copyright (c) 2026 Antriksh Sharma`. See [LICENSE.md](LICENSE.md) for the full
terms.
