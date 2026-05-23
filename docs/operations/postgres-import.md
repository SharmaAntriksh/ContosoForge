# PostgreSQL Import

Load a CSV-format generated dataset into PostgreSQL in one step. The import script reads the SQL scripts generated alongside the CSVs (`postgres/schema/`, `postgres/load/`, `postgres/admin/`, `postgres/indexes/`) and applies them in the right order: schema → admin tools → COPY load → constraints → indexes → statistics.

> Available only for `--format csv` runs. Parquet and Delta outputs do not generate SQL bootstrap scripts.

> If the target database already exists, the import aborts. Use a fresh database name per run, or drop the database first.

---

## Prerequisites

- **PostgreSQL 12+** reachable from the import host (tested against Postgres 16 and 18 on Windows, Linux, and Docker).
- **psycopg 3** installed in the Python environment:
  ```bash
  pip install "psycopg[binary]"
  ```
  The PS1 wrapper will fail fast if it's missing.
- A role that can `CREATE DATABASE` on the target server (typically `postgres` or another superuser).

---

## Quick recipes

### Local Postgres with interactive password prompt

```powershell
.\scripts\run_postgres_import.ps1 `
  -RunPath ".\generated_datasets\2026-05-22 06_34_32 PM Customers 43K Sales 1M CSV" `
  -Database Sales1M `
  -PgHost localhost `
  -Port 5432 `
  -Username postgres
```

The wrapper prompts for the password since `-Password` isn't supplied.

### Non-interactive via environment variable

```powershell
$env:PGPASSWORD = "your-password"
.\scripts\run_postgres_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Database Sales1M `
  -Username postgres
```

`PGPASSWORD` is the standard libpq env var; the wrapper reads it as the second-priority password source.

### Non-interactive via `SecureString`

```powershell
$sec = Read-Host -AsSecureString "Postgres password"
.\scripts\run_postgres_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Database Sales1M `
  -Username postgres `
  -Password $sec
```

`-Password` takes precedence over `PGPASSWORD` and the prompt.

### Remote Postgres on a non-default port

```powershell
.\scripts\run_postgres_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Database Sales1M `
  -PgHost db.internal.example.com `
  -Port 6432 `
  -Username analytics_admin
```

### Skip the post-import row-count summary

```powershell
.\scripts\run_postgres_import.ps1 `
  -RunPath ".\generated_datasets\<your-run-folder>" `
  -Database Sales1M `
  -Username postgres `
  -NoVerify
```

Verification runs by default; use `-NoVerify` to skip it on extremely large datasets where the per-table `n_live_tup` lookups add noticeable time.

### Calling the Python entrypoint directly

```bash
python -c "
from pathlib import Path
from src.tools.sql.postgres_import import import_postgres
import_postgres(
    host='localhost', port=5432,
    database='Sales1M',
    user='postgres', password='your-password',
    run_dir=Path('generated_datasets/<your-run-folder>'),
)
"
```

Useful for cross-platform automation (the PS1 wrapper is Windows-only).

---

## Full flag reference

| Flag | Description |
|---|---|
| `-RunPath` | Path to the generated dataset folder (the one containing `postgres/`). |
| `-Database` | Target database name. **Must not exist** — the importer aborts if it does. |
| `-PgHost` | Postgres host. Default `localhost`. Named `-PgHost` because `-Host` is reserved in PowerShell. |
| `-Port` | TCP port. Default `5432`. |
| `-Username` | Postgres role used for both database creation and import. Default `postgres`. Named `-Username` to avoid collision with the SQL Server wrapper's `-User`. |
| `-Password` | `SecureString` password. Resolution order: `-Password` > `$env:PGPASSWORD` > interactive prompt. |
| `-Verify` | No-op (accepted for symmetry with `run_sql_server_import.ps1`). Verification is on by default. |
| `-NoVerify` | Skip the per-table row-count summary at the end. |
| `-PythonExe` | Python interpreter to invoke. Default `python`. |

---

## What the importer actually does

The flow runs as a sequence of timed, logged phases against a single Postgres connection:

| Phase | What it applies |
|---|---|
| **Create database** | `CREATE DATABASE <name>` against the `postgres` maintenance DB (autocommit, since CREATE DATABASE can't run in a transaction). Aborts if the DB already exists. |
| **Creating Schema** | All `postgres/schema/*.sql` files except `*_create_constraints.sql`. Currently: `01_create_dimensions.sql`, `02_create_facts.sql`, `04_create_views.sql`. |
| **Installing Admin Tools** | All `postgres/admin/*.sql`. Currently: `create_pk_proc.sql`, which installs the `admin.manage_primary_keys` procedure (see below). |
| **Loading Dimensions** | `postgres/load/01_copy_dims.sql`. Streamed via `COPY ... FROM STDIN` from this Python process (bypasses server-side filesystem permissions). |
| **Loading Facts** | `postgres/load/02_copy_facts.sql`. Same client-side STDIN streaming. |
| **Applying Constraints** | `postgres/schema/03_create_constraints.sql`. Adds PKs, FKs, CHECKs. Deferred until **after** the load — adding FKs first would force per-row validation during COPY. |
| **Creating Indexes** | `postgres/indexes/01_create_indexes.sql`. Btree on FK source columns + BRIN on naturally-ordered date columns. Also post-load to avoid per-row index maintenance during COPY. |
| **Updating Statistics** | Runs `ANALYZE;` so the query planner has accurate stats from the first user query, rather than waiting on autovacuum. |
| **Row count verification** | Per-table `n_live_tup` summary via `pg_stat_user_tables` (cheap, no full scans). Skipped with `-NoVerify`. |

Typical end-to-end timing on local Postgres 18, NVMe, 1M sales rows / 3.26M total fact rows: **~20–22 seconds**.

---

## Generated folder layout

```
<run-folder>/
└── postgres/
    ├── schema/
    │   ├── 01_create_dimensions.sql      ← dim CREATE TABLEs
    │   ├── 02_create_facts.sql           ← fact CREATE TABLEs
    │   ├── 03_create_constraints.sql     ← PKs + FKs + CHECKs (deferred post-load)
    │   └── 04_create_views.sql           ← vw_* pass-through + projected fact views
    ├── load/
    │   ├── 01_copy_dims.sql              ← COPY for every dim
    │   └── 02_copy_facts.sql             ← COPY for every fact
    ├── admin/
    │   └── create_pk_proc.sql            ← admin.manage_primary_keys('DROP' / 'RESTORE')
    └── indexes/
        └── 01_create_indexes.sql         ← btree on FK columns + BRIN on date columns
```

---

## `admin.manage_primary_keys` — dev-tooling procedure

Installed automatically during the **Installing Admin Tools** phase. Lets you drop every PK and FK at once for bulk-edit experimentation, then restore them after.

```sql
-- Drop every PRIMARY KEY and FOREIGN KEY on user tables; save definitions
-- into admin._pk_backup.
CALL admin.manage_primary_keys('DROP');

-- ... bulk edits, experiments, etc. ...

-- Restore everything from admin._pk_backup; clear the backup table.
CALL admin.manage_primary_keys('RESTORE');
```

Both actions cover **PKs and FKs together** because Postgres refuses to drop a PK that's referenced by an FK without `CASCADE` (which would silently destroy the FKs you wanted to keep). The procedure drops FKs first, then PKs; restore goes in reverse order. Excludes `pg_catalog`, `information_schema`, and `admin` itself, so the proc can't disable system state.

---

## View schema configuration

By default, views are created in the `public` schema with a `vw_` prefix matching the SQL Server convention:

```
public.vw_Sales
public.vw_Customers
...
```

To land views in a separate schema (e.g. `bi`) with the prefix dropped, set `defaults.view_schema` in `config.yaml`:

```yaml
defaults:
  view_schema: bi
```

The composer creates the schema if needed and rewrites view targets:

```sql
CREATE SCHEMA IF NOT EXISTS "bi";
CREATE OR REPLACE VIEW "bi"."Sales" AS SELECT * FROM "public"."Sales";
```

Tables stay in `public`; only the view layer relocates. This mirrors `run_sql_server_import.ps1`'s `view_schema` flow, so the same config value works on both dialects.

---

## SQL Server vs PostgreSQL: key differences

| Concept | SQL Server | PostgreSQL |
|---|---|---|
| Default table schema | `dbo` | `public` |
| Identifier quoting | `[Table]` | `"Table"` |
| Bulk-load primitive | `BULK INSERT FROM N'<path>'` (server-side) | `COPY ... FROM STDIN` (client-side, no filesystem perms needed) |
| Clustered storage | Clustered Columnstore Index (CCI) on every table | No equivalent in core Postgres; heaps + btree/BRIN indexes |
| `BIT` type | `BIT` (0/1) | `BOOLEAN` (true/false) — `CHECK (col IN (0,1))` constraints dropped accordingly |
| `MONEY` type in views | `CAST(x AS MONEY)` | `CAST(x AS NUMERIC(19, 4))` — Postgres MONEY is locale-dependent and avoided |
| FK index strategy | CCI handles analytical scans; FK columns largely uncovered | Explicit btree on every FK column (sequential scans without it) |
| Pre-load constraint drop | `[admin].[ManagePrimaryKeys]` proc; needed because BULK INSERT contends on FK validation | Not needed by default — constraints are deferred to post-load via composer ordering |
| Idempotency guard syntax | `IF OBJECT_ID(N'dbo.X', N'U') IS NOT NULL` + `COL_LENGTH(...)` | `to_regclass('"public"."X"') IS NOT NULL` + `information_schema.columns` lookup, wrapped in `DO $$ ... END $$` |
| Statistics refresh after load | `UPDATE STATISTICS` (optional) | `ANALYZE;` (built into the importer's post-index phase) |

For the in-depth translation rules, see the constraint/view source files under [scripts/sql/postgres/](../../scripts/sql/postgres/).

---

## Performance notes

- **`COPY ... FROM STDIN`** streams CSV bytes from this Python process over the existing libpq connection. No server-side file permissions needed, which means the importer works against Postgres in Docker, on remote hosts, and on Windows installs where the postgres service runs under a constrained account.
- **Btree indexes on FK source columns** (Step 5) compensate for Postgres's lack of a columnstore equivalent. Without them, `WHERE CustomerKey = X` on `Sales` would seq-scan the whole fact table.
- **BRIN indexes on date columns** are ~24 KB each regardless of table size. The pipeline loads `Sales` in chronological order, so `OrderDate`, `DueDate`, and `DeliveryDate` are naturally clustered in the heap — exactly what BRIN needs. At 1M–10M rows the planner usually still picks the btree on the same column; BRIN earns its keep at larger scales where btree maintenance becomes burdensome, or as a near-free safety net if the btree is later dropped.
- **`ANALYZE;` post-load** prevents bad initial plans. Without it, autovacuum eventually catches up, but the first few queries hit the planner with default selectivity estimates and can produce dramatically wrong plans.

---

## Troubleshooting

**"Database 'X' already exists. Import aborted to avoid partial state."**
Unlike the SQL Server importer (which silently skips), the Postgres importer refuses to import into an existing DB to avoid mixing old and new state. Drop the database first:
```powershell
$env:PGPASSWORD = "your-password"
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U postgres -d postgres `
  -c "DROP DATABASE IF EXISTS Sales1M;"
```
Then rerun the importer.

**"psycopg is required for Postgres import."**
The PS1 wrapper validates psycopg is importable before doing anything else. Install with:
```bash
pip install "psycopg[binary]"
```
The `[binary]` extra avoids the C-compiler dependency.

**`COPY into "public"."X" from <path> failed: ...`**
The CSV is missing or unreadable. The importer reports the run-relative path; check the file exists under `<run>/dimensions/` or `<run>/facts/`. The most common cause is the run folder being moved or partially deleted between generation and import.

**Connection refused / timeout**
Postgres isn't reachable at the supplied `-PgHost` / `-Port`. Verify with `psql` directly:
```bash
psql -h <host> -p <port> -U <user> -d postgres -c "SELECT 1;"
```
If `psql` works but the importer doesn't, double-check `-Username` (Postgres role name, not Windows login).

**View creation fails with "relation does not exist"**
The view layer runs in the **Creating Schema** phase right after `CREATE TABLE`. If you've manually edited the generated `04_create_views.sql` to reference a non-existent table, the importer will fail there. Reset by regenerating the run.

**Import is slow**
On localhost NVMe Postgres, expect ~20s for 3M rows. Slowdowns usually come from:
- Remote host on high-latency network → `COPY` is chatty; consider `ssh -L` tunneling or running the importer on the database host
- HDD storage → unavoidable; constraints + indexes are the dominant cost

**`Updating Statistics` phase takes much longer than 2-3 seconds**
For very large databases (100M+ rows), full `ANALYZE` can take much longer. The importer runs it without `(VERBOSE)`, so check via `pg_stat_progress_analyze` in a separate session if you want to watch progress.
