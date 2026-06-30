# ContosoForge

Generate a complete, **analytics-ready retail dataset** inspired by the **ContosoRetailDW** schema - with configurable dimensions, realistic sales behavior, budget forecasts, inventory snapshots, wishlists, and complaints. Designed for BI, analytics, data engineering, and data modeling scenarios.

Every run is **deterministic**, **schema-stable**, and **idempotent**, making the generator ideal for repeatable demos, training environments, and benchmarking.

---

## What Gets Generated

The generator produces a full dimensional model across dimension and fact tables (lightly snowflaked, with multiple fact tables, like ContosoRetailDW).

**Dimension tables:**

| Group | Tables |
|---|---|
| Customers & accounts | Customers, CustomerProfile, OrganizationProfile, Plans, CustomerSubscriptions, LoyaltyTiers, CustomerAcquisitionChannels |
| Products & catalog | Products, ProductProfile, ProductCategory, ProductSubcategory, Suppliers, Promotions |
| Locations & org | Stores, Employees, EmployeeStoreAssignments, Geography |
| Time | Dates (calendar + fiscal + weekly fiscal), Time |
| Currency | Currency, ExchangeRates |
| Other lookups | Channels, ReturnReason |

**Fact tables:** Sales (flat or split into OrderHeader + OrderDetail), Returns, BudgetYearly, BudgetMonthly, InventorySnapshot, CustomerWishlists, Complaints

### Output formats

| Format | Description |
|---|---|
| `csv` | CSV files + auto-generated SQL Server and PostgreSQL bootstrap scripts (CREATE TABLE, load, constraints, views, indexes) |
| `parquet` | Merged Apache Parquet with configurable compression, row groups, and dictionary encoding |
| `deltaparquet` | Delta Lake tables partitioned by Year/Month |

Each run produces a self-contained output folder under `generated_datasets/` with all tables, SQL scripts (for CSV), and a Power BI Project template ready to open.

---

## Prerequisites

- **Python 3.13** (pinned via `.python-version` for reproducible builds; uv will auto-install it if missing)
- Git

Optional:
- [uv](https://docs.astral.sh/uv/) - recommended for fast, locked dependency installs (`pip install uv`)
- Power BI Desktop (to explore the included `.pbip` project template)

Verify Python:

```bash
python --version
```

---

## Getting Started

### 1. Clone and set up

```bash
git clone https://github.com/SharmaAntriksh/ContosoForge.git
cd ContosoForge
```

This project uses [uv](https://docs.astral.sh/uv/) for locked, reproducible
installs. Install it once if you don't have it:

- **With Python:** `pip install uv`
- **Without Python (Windows):** `irm https://astral.sh/uv/install.ps1 | iex`
- **Without Python (macOS / Linux):** `curl -LsSf https://astral.sh/uv/install.sh | sh`

uv then provisions the correct Python itself - no separate Python install needed.

> **Just installed uv and still see "uv is required but was not found on PATH"?**
> The installer adds uv to your PATH, but already-running shells don't pick that
> up. Opening a *new tab* isn't enough; Windows Terminal / VS Code give new tabs
> the old environment. Fully quit and reopen the terminal app, or just set it in
> the current session and re-run:
>
> ```powershell
> $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
> .\scripts\create_venv.ps1
> ```

**Windows (PowerShell), first-time setup:**

**Step 1: allow local scripts (one time only).** Windows blocks unsigned `.ps1`
files by default, so the setup scripts won't run until you permit them for your
user account. This needs no admin rights and you only ever do it once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

(If you'd rather not change the policy, skip this step and prefix each script
with a one-off bypass, e.g. `powershell -ExecutionPolicy Bypass -File .\scripts\create_venv.ps1`.)

**Step 2: create the environment and activate it:**

```powershell
.\scripts\create_venv.ps1        # creates .venv + installs locked deps via uv
. .\scripts\activate_venv.ps1    # activate it
```

To update dependencies later: `.\scripts\sync_venv.ps1`

**macOS / Linux:**

```bash
# Locked, reproducible - includes SQL Server + Postgres import drivers
uv sync --extra sql --extra postgres

# Core only (no database import drivers)
uv sync
```

> **Importing CSV output into SQL Server / Postgres?** The `pyodbc` and `psycopg`
> drivers are optional extras. The `create_venv.ps1` / `sync_venv.ps1` scripts
> install them by default; with bare `uv sync` add `--extra sql --extra postgres`.
> A plain `uv sync` (no extras) will *remove* them from an existing venv.

### 2. Generate data

The fastest way to get started - run with default settings from `config.yaml`:

```powershell
.\scripts\run_generator.ps1
```

Or run directly via CLI with overrides:

```bash
python main.py \
  --format parquet \
  --sales-rows 100000 \
  --customers 5000 \
  --stores 50 \
  --products 500 \
  --start-date 2022-01-01 \
  --end-date 2025-12-31 \
  --workers 8 \
  --clean
```

The 5 most-used flags are `--format`, `--sales-rows`, `--customers`, `--workers`, and `--clean`. For the full CLI surface (every flag, common patterns, override precedence), see the [CLI reference](docs/cli-reference.md).

### 3. Explore the output

Generated datasets land in `generated_datasets/` with a timestamped folder name like:

```
generated_datasets/
  └── 2026-03-07 02_30_45 PM Customers 5K Sales 100K CSV/
      ├── Sales PBIP/
      │   └── Sales.pbip    ← open directly in Power BI
      ├── config/
      │   ├── config.yaml
      │   └── models.yaml
      ├── dimensions/
      │   ├── customers.csv
      │   ├── products.csv
      │   ├── stores.csv
      │   ├── dates.csv
      │   └── ... (22 dimension tables total)
      ├── facts/
      │   ├── sales/
      │   ├── returns/                         ← if returns enabled
      │   ├── budget/                          ← if budget enabled
      │   ├── inventory/                       ← if inventory enabled
      │   ├── customer_wishlists/              ← if wishlists enabled
      │   └── complaints/                      ← if complaints enabled
      ├── sql/                                 ← CSV mode only - SQL Server bootstrap
      │   ├── schema/
      │   ├── load/
      │   └── indexes/
      └── postgres/                            ← CSV mode only - PostgreSQL bootstrap
          ├── schema/                          ← CREATE TABLE, views, constraints (DDL)
          ├── load/                            ← COPY scripts
          ├── admin/                           ← manage_primary_keys procedure
          └── indexes/                         ← btree + BRIN indexes
```

---

## Configuration

The generator is driven by two YAML files at the project root.

- **`config.yaml`** - controls the **shape and scale** of the dataset: row counts, entity counts, date ranges, output format, parallelism, and feature toggles. Full reference: [CONFIG_GUIDE](docs/CONFIG_GUIDE.md).

- **`models.yaml`** - controls **how sales behave**: demand curves, pricing dynamics, basket sizes, brand popularity, return patterns, and the overall business shape via [trend presets](docs/MODELS_GUIDE.md#available-presets). Not overridable via CLI - edit directly or via the web UI. Full reference: [MODELS_GUIDE](docs/MODELS_GUIDE.md).

CLI flags override `config.yaml` values for the current run only - they are not persisted.

For tuning the customer/row/date balance to get visually interesting charts, see [Scaling tips](docs/CONFIG_GUIDE.md#scaling-tips).

---

## Operations

Post-generation utilities for tuning, repartitioning, and importing generated datasets. Each script has its own reference doc with full flag coverage, recipes, and troubleshooting.

| Task | Script | Docs |
|---|---|---|
| Re-compress / re-row-group Parquet output | `scripts/optimize_parquet.py` | [parquet-optimization](docs/operations/parquet-optimization.md) |
| Compact small Delta Lake files | `scripts/optimize_delta.py` | [delta-optimization](docs/operations/delta-optimization.md) |
| Change Delta Lake partition layout | `scripts/repartition_delta.py` | [delta-repartitioning](docs/operations/delta-repartitioning.md) |
| Import CSV output to SQL Server | `scripts/run_sql_server_import.ps1` | [sql-server-import](docs/operations/sql-server-import.md) |
| Import CSV output to PostgreSQL | `scripts/run_postgres_import.ps1` | [postgres-import](docs/operations/postgres-import.md) |
| Provision a SQL login for SSAS / Power BI | (same import script) | [tabular-user](docs/operations/tabular-user.md) |
| Post-import admin & verify procedures | (generated SQL) | [post-import-procedures](docs/operations/post-import-procedures.md) |

---

## Web Interface

A web UI (FastAPI + React) is also available for interactive generation:

```powershell
.\scripts\run_web.ps1
```

### Generator Web UI

<img src="docs/assets/web-interface.png" alt="Generator Web UI" width="700" />

### Pipeline Run Status

<img src="docs/assets/web-pipeline-run.png" alt="Pipeline run status" width="700" />

### SQL Server Import

<img src="docs/assets/web-sqlserver-import.png" alt="SQL Server import UI" width="700" />

---

## Generated Dataset Folder

<img src="docs/assets/output.png" alt="Output folder structure" width="700" />

---

## Power BI Data Model

Each output includes a Power BI Project (`.pbip`) template with pre-configured folder paths. Open the `.pbip` file directly in Power BI Desktop - no manual path setup required.

<img src="docs/assets/data-model-diagram-view.png" alt="Power BI model collapsed" width="600" />

---

## Testing

The project includes an extensive test suite covering config validation, pricing pipeline, quantity model, geography, trend presets, version store, state management, determinism guarantees, edge-case guards, web API, packaging, sales logic, schema validation, product dimensions, sales writer, SQL tools, and date dimension edge cases.

```bash
# Run all tests
pytest

# Run a specific file or class
pytest tests/test_geography.py
pytest tests/test_config_loader.py::TestParseDate

# Stop on first failure
pytest -x

# Rerun only previously failed tests
pytest --lf
```

---

## Documentation map

| Topic | Doc |
|---|---|
| Full CLI flag reference | [cli-reference](docs/cli-reference.md) |
| `config.yaml` reference | [CONFIG_GUIDE](docs/CONFIG_GUIDE.md) |
| `models.yaml` reference + trend presets | [MODELS_GUIDE](docs/MODELS_GUIDE.md) |
| Pipeline architecture | [PIPELINE_FLOWCHART](docs/PIPELINE_FLOWCHART.md) |
| Operations (parquet, delta, SQL Server / PostgreSQL import) | [operations/](docs/operations/) |

---

## License

This project is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE.md).

You are free to use, modify, and share it for any **noncommercial** purpose. Commercial use, including selling it, is not permitted. You must keep the attribution notice (`Copyright (c) 2026 Antriksh Sharma`). See [LICENSE.md](LICENSE.md) for the full terms.
