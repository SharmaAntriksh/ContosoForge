---
name: sql-and-dialects
description: >-
  How ContosoForge generates multi-dialect SQL (SQL Server + PostgreSQL) and runs
  live database imports. Load this when editing anything under src/tools/sql/ —
  the pluggable dialect classes, CREATE TABLE / BULK INSERT / COPY / constraint /
  view generators, or the sql_server_import.py / postgres_import.py runners. Also
  use it when adding a new SQL dialect, changing generated DDL, debugging escaping
  of paths or names in generated scripts, or working on the CSV-to-database load
  path. Reach for it on any "the generated SQL is broken / wrong quoting / import
  fails" question.
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
  sql_server_import.py               # live SQL Server import runner
  postgres_import.py                 # live PostgreSQL import runner
  sql_helpers.py, _import_common.py  # shared escaping / import utilities
```

The two dialect subclasses (`sqlserver.py`, `postgres.py`) implement the interface in
`base.py`. When adding a third dialect, subclass `base.py` and implement its abstract
methods — do **not** fork the generators.

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
standalone. The recent hardening pass (`3e18ebd`) tightened the CSV→SQL Server/Postgres
import path — see `docs/SQL_IMPORT_REVIEW.md` if present for the review notes.

## Tests

The dialect and import paths are well covered — run these when you change anything here:

```bash
pytest tests/test_dialect.py tests/test_dialect_postgres.py tests/test_sql_tools.py
pytest tests/test_postgres_import.py tests/test_postgres_constraints.py \
       tests/test_postgres_indexes.py tests/test_postgres_views_and_admin.py
```

The Postgres tests exercise the PostgreSQL dialect/import path specifically; keep them
green when touching `postgres.py` or `postgres_import.py`.

## Related: Power BI TMDL escaping

Not SQL, but the same class of bug — the `.pbip` generator embeds file paths in Power BI
M expressions, and **`"` in those paths must be escaped to `\"`** or the generated
`.tmdl` files break (gotcha #18). That lives in `src/engine/powerbi_packaging.py`, not
here, but if you're touching path-escaping in generated output, keep it in mind.
