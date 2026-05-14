# Contoso Sales Data Generator

Generate a complete, **analytics-ready retail dataset** inspired by the **ContosoRetailDW** schema — with configurable dimensions, realistic sales behavior, budget forecasts, inventory snapshots, wishlists, and complaints. Designed for BI, analytics, data engineering, and data modeling scenarios.

Every run is **deterministic**, **schema-stable**, and **idempotent**, making the generator ideal for repeatable demos, training environments, and benchmarking.

---

## What Gets Generated

The generator produces a full star-schema data model across dimension and fact tables.

**Dimension tables:** Customers, CustomerProfile, OrganizationProfile, Products, ProductProfile, ProductCategory, ProductSubcategory, Stores, Employees, EmployeeStoreAssignments, Dates (calendar + fiscal + weekly fiscal), Time, Geography, Currency, ExchangeRates, Promotions, Suppliers, Plans, CustomerSubscriptions, LoyaltyTiers, SalesChannels, CustomerAcquisitionChannels, ReturnReason

**Fact tables:** Sales (flat or split into SalesOrderHeader + SalesOrderDetail), SalesReturn, BudgetYearly, BudgetMonthly, InventorySnapshot, CustomerWishlists, Complaints

### Output formats

| Format | Description |
|---|---|
| `csv` | CSV files + auto-generated SQL Server bootstrap scripts (CREATE TABLE, BULK INSERT, views, constraints) |
| `parquet` | Merged Apache Parquet with configurable compression, row groups, and dictionary encoding |
| `deltaparquet` | Delta Lake tables partitioned by Year/Month |

Each run produces a self-contained output folder under `generated_datasets/` with all tables, SQL scripts (for CSV), and a Power BI Project template ready to open.

---

## Prerequisites

- **Python 3.11+**
- Git

Optional:
- [uv](https://docs.astral.sh/uv/) — recommended for fast, locked dependency installs (`pip install uv`)
- Power BI Desktop (to explore the included `.pbip` project template)

Verify Python:

```bash
python --version
```

---

## Getting Started

### 1. Clone and set up

```bash
git clone https://github.com/SharmaAntriksh/synthetic-data-generator.git
cd synthetic-data-generator
```

**Windows (PowerShell):**

```powershell
# Create virtual environment and install dependencies
# Uses uv (preferred) if available, falls back to pip
.\scripts\create_venv.ps1

# Activate
. .\scripts\activate_venv.ps1
```

To update dependencies later:

```powershell
.\scripts\sync_venv.ps1
```

**macOS / Linux:**

```bash
# Recommended (locked, reproducible)
pip install uv
uv sync

# Or traditional
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Generate data

The fastest way to get started — run with default settings from `config.yaml`:

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

The 5 most-used flags are `--format`, `--sales-rows`, `--customers`, `--workers`, and `--clean`. For the full CLI surface (every flag, common patterns, override precedence), see [docs/cli-reference.md](docs/cli-reference.md).

### 3. Explore the output

Generated datasets land in `generated_datasets/` with a timestamped folder name like:

```
generated_datasets/
  └── 2026-03-07 02_30_45 PM Customers 5K Sales 100K Parquet/
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
      │   ├── sales_return/                    ← if returns enabled
      │   ├── budget/                          ← if budget enabled
      │   ├── inventory/                       ← if inventory enabled
      │   ├── customer_wishlists/              ← if wishlists enabled
      │   └── complaints/                      ← if complaints enabled
      └── sql/                                 ← CSV mode only
          ├── schema/
          ├── load/
          └── indexes/
```

---

## Configuration

The generator is driven by two YAML files at the project root.

- **`config.yaml`** — controls the **shape and scale** of the dataset: row counts, entity counts, date ranges, output format, parallelism, and feature toggles. Full reference: [docs/CONFIG_GUIDE.md](docs/CONFIG_GUIDE.md).

- **`models.yaml`** — controls **how sales behave**: demand curves, pricing dynamics, basket sizes, brand popularity, return patterns, and the overall business shape via [trend presets](docs/MODELS_GUIDE.md#available-presets). Not overridable via CLI — edit directly or via the web UI. Full reference: [docs/MODELS_GUIDE.md](docs/MODELS_GUIDE.md).

CLI flags override `config.yaml` values for the current run only — they are not persisted.

For tuning the customer/row/date balance to get visually interesting charts, see [Scaling tips](docs/CONFIG_GUIDE.md#scaling-tips).

---

## Operations

Post-generation utilities for tuning, repartitioning, and importing generated datasets. Each script has its own reference doc with full flag coverage, recipes, and troubleshooting.

| Task | Script | Docs |
|---|---|---|
| Re-compress / re-row-group Parquet output | `scripts/optimize_parquet.py` | [docs/operations/parquet-optimization.md](docs/operations/parquet-optimization.md) |
| Compact small Delta Lake files | `scripts/optimize_delta.py` | [docs/operations/delta-optimization.md](docs/operations/delta-optimization.md) |
| Change Delta Lake partition layout | `scripts/repartition_delta.py` | [docs/operations/delta-repartitioning.md](docs/operations/delta-repartitioning.md) |
| Import CSV output to SQL Server | `scripts/run_sql_server_import.ps1` | [docs/operations/sql-server-import.md](docs/operations/sql-server-import.md) |
| Provision a SQL login for SSAS / Power BI | (same import script) | [docs/operations/tabular-user.md](docs/operations/tabular-user.md) |
| Post-import admin & verify procedures | (generated SQL) | [docs/operations/post-import-procedures.md](docs/operations/post-import-procedures.md) |

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

Each output includes a Power BI Project (`.pbip`) template with pre-configured folder paths. Open the `.pbip` file directly in Power BI Desktop — no manual path setup required.

<img src="docs/assets/data-model-diagram-view.png" alt="Power BI model collapsed" width="600" />

---

## Testing

The project includes 1437+ tests covering config validation, pricing pipeline, quantity model, geography, trend presets, version store, state management, determinism guarantees, edge-case guards, web API, packaging, sales logic, schema validation, product dimensions, sales writer, SQL tools, and date dimension edge cases.

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
| Full CLI flag reference | [docs/cli-reference.md](docs/cli-reference.md) |
| `config.yaml` reference | [docs/CONFIG_GUIDE.md](docs/CONFIG_GUIDE.md) |
| `models.yaml` reference + trend presets | [docs/MODELS_GUIDE.md](docs/MODELS_GUIDE.md) |
| Pipeline architecture | [docs/PIPELINE_FLOWCHART.md](docs/PIPELINE_FLOWCHART.md) |
| Operations (parquet, delta, SQL import) | [docs/operations/](docs/operations/) |

---

## License

This project is licensed under the [MIT License](LICENSE).
