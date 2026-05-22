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

import re
import shutil
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
from src.tools.sql.dialect import PostgresDialect

_DIALECT = PostgresDialect()

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


# Pulls ``(target, path)`` pairs out of the generated COPY script so the
# load can be executed client-side via ``COPY ... FROM STDIN``. The WITH
# clause is intentionally ignored: every CSV the generator emits uses the
# same options, so we just hard-code them in the STDIN statement below.
_COPY_STATEMENT_RE = re.compile(
    r'COPY\s+(?P<target>(?:"[^"]+"\."[^"]+"|"[^"]+"|[\w.]+))\s+'
    r"FROM\s+'(?P<path>[^']+)'",
    flags=re.IGNORECASE,
)

# psycopg's Copy.write() chunks into libpq's PQputCopyData, which itself
# buffers; 1 MB minimises Python-level loop overhead for multi-GB files
# without growing memory pressure meaningfully.
_COPY_CHUNK_BYTES = 1 << 20

_STDIN_COPY_OPTS = "FORMAT csv, HEADER true, ENCODING 'UTF8'"


def _apply_copy_script_via_stdin(conn, sql_file: Path, *, run_dir: Path) -> None:
    """Apply a generated COPY script using client-side STDIN streaming.

    The generated load scripts use server-side ``COPY ... FROM '<path>'``,
    which requires the Postgres server process to read the host filesystem
    directly. On native Windows installs the service account often lacks
    read permission on user-home directories; in Docker the host filesystem
    isn't visible at all without a bind mount. To sidestep both, we parse
    each ``COPY`` statement out of the script and execute it as
    ``COPY ... FROM STDIN``, streaming the CSV bytes from this Python
    process over the existing connection.
    """
    sql_text = _read_sql_text(sql_file)
    matches = list(_COPY_STATEMENT_RE.finditer(sql_text))
    if not matches:
        return

    with conn.cursor() as cur:
        for m in matches:
            target = m.group("target")
            csv_path = Path(m.group("path"))
            if not csv_path.is_file():
                raise PostgresImportError(
                    f"CSV not found for {target}: {csv_path}. "
                    "Did the generated run move or get deleted?"
                )
            stmt = f"COPY {target} FROM STDIN WITH ({_STDIN_COPY_OPTS})"
            try:
                with cur.copy(stmt) as copy, csv_path.open("rb") as fh:
                    shutil.copyfileobj(fh, copy, length=_COPY_CHUNK_BYTES)
            except psycopg.Error as exc:  # type: ignore[union-attr]
                raise PostgresImportError(
                    f"COPY into {target} from {_short_path(csv_path, base=run_dir)} "
                    f"failed: {exc}"
                ) from exc


def _database_exists(admin_conn, database: str) -> bool:
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (database,))
        return cur.fetchone() is not None


def _create_database(admin_conn, database: str, dialect) -> None:
    # CREATE DATABASE cannot run inside a transaction block; the caller is
    # responsible for opening admin_conn with autocommit=True.
    with admin_conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE {dialect.quote_ident(database)};")


def _verify_row_counts(conn, *, dim_tables: list[str], fact_tables: list[str]) -> None:
    """Log a row-count summary per table. Best-effort — never raises.

    Uses ``pg_stat_user_tables.n_live_tup`` (O(1) per table, no full scan)
    rather than ``SELECT count(*)`` — matches the SQL Server importer,
    which reads from ``sys.dm_db_partition_stats``. The stats collector
    keeps ``n_live_tup`` close to real after COPY; an exact count would
    require a full scan and is rarely worth it for a load-time sanity check.
    """
    default_schema = _DIALECT.default_schema
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
                        "ORDER BY (schemaname = %s) DESC "
                        "LIMIT 1;",
                        (t, default_schema),
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
    # CREATE DATABASE can't run in a transaction, so the admin connection
    # opens in autocommit mode — psycopg won't let us flip it after the
    # SELECT in _database_exists implicitly opens a transaction.
    admin_dsn = dict(host=host, port=port, dbname="postgres", user=user, password=password)
    try:
        with pg.connect(**admin_dsn, autocommit=True) as admin_conn:
            if _database_exists(admin_conn, database):
                raise PostgresImportError(
                    f"Database '{database}' already exists. "
                    "Import aborted to avoid partial state. "
                    "Use a new database name or drop the database first."
                )
            _create_database(admin_conn, database, _DIALECT)
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
                _apply_copy_script_via_stdin(conn, load_file, run_dir=run_dir)
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
