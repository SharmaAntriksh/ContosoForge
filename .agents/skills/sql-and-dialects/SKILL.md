---
name: sql-and-dialects
description: >-
  Edit or debug ContosoForge SQL generation and database imports: dialects, schema rosters, identifier casing, re-emission, CSV loading, Python entrypoints, and PowerShell launchers. Use for generated SQL or SQL Server/PostgreSQL import failures.
---

# Multi-Dialect SQL Generation & Import

CSV output ships with auto-generated SQL scripts (DDL, bulk-load, constraints,
views) for **both** SQL Server and PostgreSQL, plus live-import runners. The dialect
layer is pluggable so the two targets share one generator and differ only in a small
dialect class.

## Layout

```
src/tools/sql/
  dialect/
    base.py              # abstract dialect interface (shared generation logic)
    sqlserver.py         # SQL Server dialect (T-SQL, BULK INSERT, OBJECT_ID guards)
    postgres.py          # PostgreSQL dialect (COPY, information_schema)
  generate_create_table_scripts.py   # DDL generation
  generate_bulk_insert_sql.py        # bulk-load script generation
  schema_model.py        # declarative constraint + index roster (single source of truth)
  constraint_sql.py      # render constraints + indexes per dialect from the roster
  view_sql.py            # render pass-through + fact-abstraction views per dialect
  identifier_case.py     # pure pascal/snake identifier transform
  name_map.py            # original→cased identifier map (collision + reserved-word checks)
  reserved_words.py      # per-dialect reserved keyword sets
  sql_server_import.py               # live SQL Server import runner
  postgres_import.py                 # live PostgreSQL import runner
  sql_helpers.py, _import_common.py  # shared escaping / import utilities
```

The two dialect subclasses (`sqlserver.py`, `postgres.py`) implement the interface in
`base.py`. When adding a third dialect, subclass `base.py` and implement its abstract
methods — do **not** fork the generators.

## Constraints / indexes / views are generated from one roster

Constraints, Postgres indexes, and model views are **generated per dialect from a
single declarative source**, not maintained as hand-written `.sql` files:

- **`schema_model.py`** holds the roster: `PrimaryKey` / `Unique` / `UniqueIndex` /
  `ForeignKey` / `Check` (with semantic CHECK shapes `Flag`/`Min`/`Range`/`IntSet`/
  `Enum`) grouped into config-gated sections, plus the `Index` (btree/BRIN) roster.
- **`constraint_sql.py`** renders that roster to guarded, idempotent DDL — a
  `_SqlServerWriter` (`IF … BEGIN … END; GO`, `sys.*` catalogs, `NONCLUSTERED`,
  `WITH CHECK`, `N'…'`, type-compat FK guards) and a `_PostgresWriter` (`DO $$ … $$`,
  `pg_constraint`/`to_regclass`, BOOLEAN-flag CHECK drop).
- **`view_sql.py`** renders pass-through dimension views and the fact views. `vw_Sales`
  is *statically specialized from config* (sales_output mode picks the flat-Sales vs
  OrderHeader⋈OrderDetail variant; `skip_order_cols` picks the order-key prefix) rather
  than doing runtime catalog introspection.

**To change a constraint, index, or view, edit the roster — never a generated file.**
The packaging composers (`src/engine/packaging/sql_scripts.py`) call these renderers.
The columnstore helper (`create_drop_cci.sql`) stays a static asset (it discovers tables
dynamically, so it needs no casing), as do the SQL-Server-only verification diagnostics.

## Identifier casing (chosen at import, not generation)

Generation always emits **PascalCase** SQL. Identifier casing is a per-import concern
(SQL Server casing is cosmetic; PostgreSQL wants `snake` to avoid quoting), so both
importers take `--identifier-case pascal|snake` and, for `snake`, **re-render the whole
SQL bundle** from the roster before executing — they do *not* text-rewrite the packaged
PascalCase scripts (the identifiers in guard string literals like
`OBJECT_ID('dbo.Customers')` / `to_regclass('"public"."Customers"')` aren't wrapped, so
only re-rendering is safe).

- **`src/engine/packaging/reemit_sql.py`** — `reemit_run_sql(run_dir, out_sql_root, case)`
  reads the run's own `config/config.yaml` + data files and drives the same packaging
  composers with a snake name map into a scratch dir. Pure re-render, data untouched.
  The effective run shape (CLI overrides like `--sales-rows` / `--skip-order-cols` and
  the authoritative OrderNumber width) is read from `config/sql_generation.json`, written
  by packaging for CSV runs; runs predating that manifest fall back to `config.yaml` with
  a warning (the packaged `config.yaml` is a snapshot taken before overrides).
- **`name_map.py`** builds the validated original→cased map (hard error on a collision,
  soft warning when a cased name hits a dialect reserved word); threaded through the DDL,
  load, constraints, indexes, and views so they all agree.
- The importers (`sql_server_import.py` / `postgres_import.py`) take a `sql_root_override`;
  the entrypoints (`scripts/sql/run_*_import.py`) re-emit to a `TemporaryDirectory` for
  `snake` and point the importer at it. `pascal` uses the packaged scripts unchanged.

Constraint/index names stay verbatim (internal); view names (`vw_*`) transform
(query-facing). In `snake` the PascalCase-only verification diagnostics self-skip.

## Escaping rules (get these wrong and the generated SQL silently breaks)

- **File paths in `BULK INSERT FROM` must use the `N'...'` Unicode prefix.** Without
  it, non-ASCII paths break the generated SQL. (Gotcha #17.)
- **Escape single quotes in object names passed to `OBJECT_ID()`.** A store or table
  name with an apostrophe otherwise produces invalid T-SQL.
- PostgreSQL uses `COPY ... FROM` and `information_schema` where SQL Server uses
  `BULK INSERT` and `OBJECT_ID()` — keep those differences inside the dialect classes,
  not in the shared generator.

Shared escaping lives in `sql_helpers.py` / `_import_common.py`. Prefer those helpers
over inlining quote-handling in the dialect classes, so both dialects stay consistent.

## Import runners

`sql_server_import.py` and `postgres_import.py` are the live-DB load paths (create
schema, bulk-load the CSVs, apply constraints/indexes/views). They're driven from the
web layer's `/api/import` endpoints (`web/routes/import_routes.py`) as well as
standalone. The PowerShell wrappers are `scripts/run_sql_server_import.ps1` and
`scripts/run_postgres_import.ps1`; the Python CLI entrypoints are under
`scripts/sql/`. Operator references live in `docs/operations/sql-server-import.md`
and `docs/operations/postgres-import.md`.

Both `pyodbc` and `psycopg[binary]` are **default** project dependencies; a plain
`uv sync` installs them. The `sql`/`postgres` extras are compatibility aliases.
For tests use `uv sync --locked --extra dev`. SQL Server additionally needs a
system Microsoft ODBC driver; the Python package does not install it.

PowerShell preflight/repair is shared in `scripts/_common.ps1`. Windows
PowerShell 5.1 can promote native stderr to a terminating error under
`$ErrorActionPreference = "Stop"`, even with `2>$null`. Scope native error
preferences locally and inspect exit codes so a missing-driver probe reaches
the repair path; do not suppress installer failures. Regression coverage:
`tests/test_powershell_driver_preflight.py` runs both shells when available.

The packaged PostgreSQL SQL uses server-file COPY, but the live Python importer
streams local CSV bytes with `COPY ... FROM STDIN`; PostgreSQL need not share the
client filesystem. SQL Server BULK INSERT reads from the SQL Server service's
filesystem/security context. Path/access failures have different remedies.

Both live importers reject an existing target database. Use a fresh name for a
new import. An interrupted import may leave a partial database, and SQL Server
may need its recovery model restored. Keep graceful stop/finally cleanup intact;
`web/process_control.py` owns web-child lifecycle. A passing fake-connection unit
test does not establish a successful import against a live server.

## Tests

The dialect and import paths are well covered — run these when you change anything here:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_dialect.py tests/test_dialect_postgres.py tests/test_sql_tools.py
.venv/Scripts/python.exe -m pytest tests/test_postgres_import.py tests/test_postgres_constraints.py tests/test_postgres_indexes.py tests/test_postgres_views_and_admin.py
# Roster rendering + identifier casing + import-time re-emit:
.venv/Scripts/python.exe -m pytest tests/test_constraint_generation.py tests/test_view_generation.py tests/test_identifier_case.py tests/test_name_map.py tests/test_identifier_case_wiring.py tests/test_identifier_case_e2e.py tests/test_verification_gating.py tests/test_reemit_sql.py
```

For importer or wrapper changes, also select `test_sql_import_hardening.py`,
`test_import_entrypoints.py`, `test_powershell_driver_preflight.py`, and
`test_web_import_routes.py` from `tests/` as appropriate.

The Postgres tests exercise the PostgreSQL dialect/import path specifically; keep them
green when touching `postgres.py` or `postgres_import.py`.
`test_identifier_case_e2e.py` is the cross-artifact guard: it renders every generated
script in snake mode and asserts no PascalCase identifier leaks anywhere.
`test_reemit_sql.py` covers `reemit_run_sql` — the import-time re-render both importers
call for a non-pascal case.

## Related: Power BI TMDL escaping

Not SQL, but the same class of bug — the `.pbip` generator embeds file paths in Power BI
M expressions, and **`"` in those paths must be escaped to `\"`** or the generated
`.tmdl` files break (gotcha #18). That lives in `src/engine/powerbi_packaging.py`, not
here, but if you're touching path-escaping in generated output, keep it in mind.
