"""Postgres importer: apply generated CREATE TABLE + COPY scripts to a Postgres DB.

Sibling to ``sql_server_import.py``. Much simpler because Postgres has no
equivalent of the SQL Server-only complications (parallel BULK INSERT
contention, columnstore indexes, ``[admin].[ManagePrimaryKeys]`` proc).
Server-side ``COPY`` is fast enough that we don't need parallel workers.

Run-directory layout (CSV mode):
    <run>/postgres/schema/01_create_dimensions.sql
    <run>/postgres/schema/02_create_facts.sql
    <run>/postgres/load/01_copy_dims.sql
    <run>/postgres/load/02_copy_facts.sql

``import_postgres()`` connects via psycopg, optionally creates the target
database, applies the schema scripts, then the load scripts, then verifies
row counts. Connection details mirror libpq env vars
(``host``/``port``/``dbname``/``user``/``password``).
"""
from __future__ import annotations

import time as _time
from pathlib import Path

from src.exceptions import PostgresImportError
from src.tools.sql._import_common import (
    _extract_tables_from_create_sql,
    _log,
    _short_path,
    find_create_sql,
    list_sql_files,
    ordered_load_files,
)

try:  # psycopg 3
    import psycopg  # type: ignore
except ImportError:
    psycopg = None  # type: ignore[assignment]


def _require_psycopg():
    if psycopg is None:
        raise PostgresImportError(
            "psycopg is required for Postgres import. "
            "Install it with: pip install 'psycopg[binary]'"
        )
    return psycopg


def _read_sql_text(sql_file: Path) -> str:
    raw = sql_file.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _execute_script(conn, sql_file: Path) -> None:
    """Execute every statement in a multi-statement SQL file.

    Psycopg sends the entire text in one round-trip; the server parses
    and runs the ``;``-separated statements. Driver errors are wrapped
    with the originating filename for log readability.
    """
    sql_text = _read_sql_text(sql_file)
    if not sql_text.strip():
        return
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)  # type: ignore[arg-type]
    except psycopg.Error as exc:  # type: ignore[union-attr]
        raise PostgresImportError(
            f"Error executing '{sql_file.name}': {exc}"
        ) from exc


def _database_exists(admin_conn, database: str) -> bool:
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (database,))
        return cur.fetchone() is not None


def _create_database(admin_conn, database: str) -> None:
    # CREATE DATABASE cannot run inside a transaction block.
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        ident = '"' + database.replace('"', '""') + '"'
        cur.execute(f"CREATE DATABASE {ident};")


def _verify_row_counts(conn, *, dim_tables: list[str], fact_tables: list[str]) -> None:
    """Log a row-count summary per table. Best-effort — never raises.

    Uses ``pg_stat_user_tables.n_live_tup`` (O(1) per table, no full scan)
    rather than ``SELECT count(*)`` — matches the SQL Server importer,
    which reads from ``sys.dm_db_partition_stats``. The stats collector
    keeps ``n_live_tup`` close to real after COPY; an exact count would
    require a full scan and is rarely worth it for a load-time sanity check.
    """
    sections = (("Dimensions", dim_tables), ("Facts", fact_tables))
    for title, tables in sections:
        if not tables:
            continue
        _log("INFO", f"  {title} row counts")
        total = 0
        for t in tables:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT schemaname, n_live_tup "
                        "FROM pg_stat_user_tables "
                        "WHERE relname = %s "
                        "ORDER BY (schemaname = 'dbo') DESC "
                        "LIMIT 1;",
                        (t,),
                    )
                    row = cur.fetchone()
                    if not row:
                        _log("WARN", f"    - {t}: [MISSING]")
                        continue
                    schema, n = row[0], int(row[1] or 0)
                    _log("INFO", f"    - {schema}.{t}: {n:,}")
                    total += n
            except psycopg.Error as exc:  # type: ignore[union-attr]
                _log("WARN", f"    - {t}: [SKIP] {exc}")
        _log("INFO", f"    TOTAL: {total:,}")


def import_postgres(
    *,
    host: str = "localhost",
    port: int = 5432,
    database: str,
    user: str = "postgres",
    password: str = "",
    run_dir: Path,
    verify: bool = True,
) -> None:
    """Apply generated Postgres DDL and COPY scripts to a target database.

    Connects to the ``postgres`` maintenance DB to create ``database`` if it
    does not exist, then connects to ``database`` and applies all schema
    scripts followed by all load scripts in order. ``verify=True`` prints
    a per-table row-count summary at the end.
    """
    # Validate inputs before requiring the driver — fail fast on bad layout
    # without forcing psycopg to be installed just to see the error message.
    run_dir = Path(run_dir)
    postgres_dir = run_dir / "postgres"
    schema_dir = postgres_dir / "schema"
    load_dir = postgres_dir / "load"

    if not schema_dir.is_dir():
        raise PostgresImportError(
            f"Postgres schema folder not found: {schema_dir}. "
            "Postgres import is supported only for CSV runs."
        )

    schema_files = list_sql_files(schema_dir)
    if not schema_files:
        raise PostgresImportError(f"No schema scripts found under {schema_dir}.")

    load_files = ordered_load_files(load_dir)

    pg = _require_psycopg()

    _t_total = _time.time()
    _log("INFO", "Postgres Import")
    _log("INFO", f"  Host: {host}:{port}")
    _log("INFO", f"  Database: {database}")

    # Step 1: create the database (connect to 'postgres' maintenance DB).
    admin_dsn = dict(host=host, port=port, dbname="postgres", user=user, password=password)
    try:
        with pg.connect(**admin_dsn) as admin_conn:
            if _database_exists(admin_conn, database):
                raise PostgresImportError(
                    f"Database '{database}' already exists. "
                    "Import aborted to avoid partial state. "
                    "Use a new database name or drop the database first."
                )
            _create_database(admin_conn, database)
            _log("INFO", f"  Database: {database} (created)")
    except pg.Error as exc:
        raise PostgresImportError(
            f"Failed connecting to Postgres at {host}:{port}: {exc}"
        ) from exc

    # Step 2: apply schema + load scripts against the target database.
    target_dsn = dict(admin_dsn, dbname=database)
    try:
        with pg.connect(**target_dsn) as conn:
            conn.autocommit = False

            _t_schema = _time.time()
            _log("INFO", "  Creating Schema")
            for f in schema_files:
                _log("WORK", f"    {_short_path(f, base=run_dir)}")
                _execute_script(conn, f)
            conn.commit()
            _log("DONE", f"  Creating Schema completed in {_time.time() - _t_schema:.1f}s")

            for load_file in load_files:
                is_dims = "dim" in load_file.name.lower()
                section = "Dimensions" if is_dims else "Facts"
                _t_load = _time.time()
                _log("INFO", f"  Loading {section}")
                _log("WORK", f"    {_short_path(load_file, base=run_dir)}")
                _execute_script(conn, load_file)
                conn.commit()
                _log("DONE", f"  Loading {section} completed in {_time.time() - _t_load:.1f}s")

            if verify:
                dim_create = find_create_sql(schema_files, "create_dimensions.sql")
                fact_create = find_create_sql(schema_files, "create_facts.sql")
                dim_tables = _extract_tables_from_create_sql(dim_create) if dim_create else []
                fact_tables = _extract_tables_from_create_sql(fact_create) if fact_create else []
                _verify_row_counts(conn, dim_tables=dim_tables, fact_tables=fact_tables)
    except pg.Error as exc:
        raise PostgresImportError(
            f"Postgres import failed for database '{database}': {exc}"
        ) from exc

    _log("DONE", f"Postgres import complete in {_time.time() - _t_total:.1f}s")
