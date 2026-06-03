from __future__ import annotations

import sys
import time as _time
import threading as _threading
from datetime import datetime
import re
from pathlib import Path
from typing import Iterable, List, Tuple

from src.exceptions import SqlServerImportError
from src.tools.sql._import_common import (
    _extract_tables_from_create_sql,
    _log,
    _short_path,
    find_create_sql as _find_create_sql,
    list_sql_files,
    ordered_load_files,
)
from src.tools.sql.sql_helpers import sql_escape_literal

try:
    import pyodbc
except ImportError:
    pyodbc = None  # type: ignore[assignment]

def _find_project_root() -> Path:
    """Walk up from this file to find the repo root (contains main.py)."""
    d = Path(__file__).resolve().parent
    for _ in range(10):
        if (d / "main.py").exists():
            return d
        d = d.parent
    return Path(__file__).resolve().parents[3]  # fallback


PROJECT_ROOT = _find_project_root()
_GO_SPLIT_RE = re.compile(r"^\s*GO\s*$", flags=re.MULTILINE | re.IGNORECASE)

# sys.key_constraints.type / _PK_Backup.constraint_type literals.
_CT_PK = "PK"
_CT_UQ = "UQ"
_CT_FK = "FK"



# Importer logging helpers (_ts, _log, _COLORS), _extract_tables_from_create_sql,
# and _short_path are imported from _import_common above.


def _find_table_schema(cursor: "pyodbc.Cursor", table_name: str) -> str:
    cursor.execute(
        "SELECT s.name "
        "FROM sys.tables t "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE t.name = ? "
        "ORDER BY CASE WHEN s.name = 'dbo' THEN 0 ELSE 1 END, s.name;",
        table_name,
    )
    row = cursor.fetchone()
    if not row:
        raise SqlServerImportError(f"Table not found in database: {table_name}")
    return str(row[0])


def _fast_rowcount(cursor: "pyodbc.Cursor", schema: str, table: str) -> int:
    cursor.execute(
        "SELECT COALESCE(SUM(row_count),0) "
        "FROM sys.dm_db_partition_stats "
        "WHERE object_id = OBJECT_ID(?) AND index_id IN (0,1);",
        f"[{schema}].[{table}]",
    )
    return int(cursor.fetchone()[0])


def _print_table_counts(cursor: "pyodbc.Cursor", *, tables: list[str], title: str) -> None:
    _log("INFO", title)
    for t in tables:
        try:
            schema = _find_table_schema(cursor, t)
            n = _fast_rowcount(cursor, schema, t)
            print(f"  - {schema}.{t}: {n:,}")
        except (ValueError, KeyError, OSError) as exc:
            print(f"  - {t}: [SKIP] {exc}")


def _cci_count(cursor: "pyodbc.Cursor", schema: str, table: str) -> int:
    cursor.execute(
        "SELECT COUNT(*) "
        "FROM sys.indexes i "
        "JOIN sys.tables t ON t.object_id = i.object_id "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE s.name = ? AND t.name = ? AND i.type_desc = 'CLUSTERED COLUMNSTORE';",
        schema,
        table,
    )
    return int(cursor.fetchone()[0])


def _count_user_constraints(cursor: "pyodbc.Cursor") -> tuple[int, int, int]:
    """Return (pk_count, uq_count, fk_count) on user tables in a single round-trip."""
    cursor.execute(
        "SELECT "
        "  SUM(CASE WHEN kc.type = 'PK' THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN kc.type = 'UQ' THEN 1 ELSE 0 END), "
        "  (SELECT COUNT(*) FROM sys.foreign_keys fk "
        "   JOIN sys.tables t ON t.object_id = fk.parent_object_id "
        "   JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "   WHERE t.is_ms_shipped = 0 "
        "     AND s.name NOT IN ('sys','INFORMATION_SCHEMA','admin')) "
        "FROM sys.key_constraints kc "
        "JOIN sys.tables t ON t.object_id = kc.parent_object_id "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE t.is_ms_shipped = 0 "
        "  AND s.name NOT IN ('sys','INFORMATION_SCHEMA','admin');"
    )
    row = cursor.fetchone()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def _print_cci_summary(cursor: "pyodbc.Cursor", *, tables: list[str]) -> None:
    cursor.execute("SELECT COUNT(*) FROM sys.indexes WHERE type_desc = 'CLUSTERED COLUMNSTORE';")
    total_ccis = int(cursor.fetchone()[0])

    _log("INFO", f"CCI summary: total CCIs in DB = {total_ccis}")
    for t in tables:
        try:
            schema = _find_table_schema(cursor, t)
            c = _cci_count(cursor, schema, t)
            print(f"  - {schema}.{t}: CCI={c}")
        except (ValueError, KeyError, OSError) as exc:
            print(f"  - {t}: [SKIP] {exc}")


# -------------------------
# SQL file execution helpers
# -------------------------
def _read_sql_text(sql_file: Path) -> str:
    raw = sql_file.read_bytes()

    # BOM detection
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
    else:
        # Heuristic: NULs early usually mean UTF-16
        if b"\x00" in raw[:4096]:
            try:
                text = raw.decode("utf-16")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-16")

    # Strip NULs that break identifiers (dbo -> d\0b\0o\0)
    if "\x00" in text:
        text = text.replace("\x00", "")

    return text


def _drain_results(cursor) -> None:
    """
    Consume all pending result sets on *cursor* so that deferred errors
    (e.g. a THROW after an earlier SELECT in the same batch) are surfaced
    as pyodbc.Error instead of being silently swallowed.
    """
    while True:
        try:
            cursor.fetchall()
        except pyodbc.ProgrammingError:
            # No result set open — expected for DDL / DML-only batches.
            pass
        if not cursor.nextset():
            break


def execute_sql_batches(cursor, sql_file: Path) -> None:
    """
    Execute a .sql file split on line-only GO statements (case-insensitive).
    """
    sql_text = _read_sql_text(sql_file)
    batches = _GO_SPLIT_RE.split(sql_text)

    for idx, batch in enumerate(batches, start=1):
        batch = batch.strip()
        if not batch:
            continue
        try:
            cursor.execute(batch)
            _drain_results(cursor)
        except pyodbc.Error as exc:
            raise SqlServerImportError(
                f"Error executing batch {idx} in file '{sql_file.name}'. Details: {exc.args}"
            ) from exc


def execute_sql_files(cursor, files: Iterable[Path]) -> None:
    for f in files:
        execute_sql_batches(cursor, f)


# Anchored at line start so matches inside ``-- ...`` comments are ignored.
_BULK_INSERT_LINE_PREFIX = r"^[ \t]*BULK\s+INSERT\s+"
_TABLE_REF_TAIL = r"(?:\[?\w+\]?\.)?\[?(\w+)\]?"


def _extract_table_from_batch(batch_text: str) -> str:
    """Extract the target table name from a BULK INSERT/INSERT batch."""
    m = re.search(
        _BULK_INSERT_LINE_PREFIX + _TABLE_REF_TAIL,
        batch_text, flags=re.IGNORECASE | re.MULTILINE,
    )
    if m:
        return m.group(1)
    m = re.search(
        r"^[ \t]*INSERT\s+(?:INTO\s+)?" + _TABLE_REF_TAIL,
        batch_text, flags=re.IGNORECASE | re.MULTILINE,
    )
    if m:
        return m.group(1)
    return ""


def _execute_cci_with_progress(cursor, sql_file: Path, target_tables: list[str]) -> None:
    """Execute a CCI script with a spinner showing elapsed time.

    Splits on GO and executes each batch with a spinner. After all
    batches complete, verifies which tables got CCI indexes.
    No second cursor needed — avoids MARS conflict.
    """
    time = _time
    threading = _threading

    _FRAMES = ["-", "\\", "|", "/"]

    sql_text = _read_sql_text(sql_file)
    batches = _GO_SPLIT_RE.split(sql_text)
    non_empty = [b.strip() for b in batches if b.strip()]

    if not non_empty:
        execute_sql_batches(cursor, sql_file)
        return

    n_tables = len(target_tables)

    for batch in non_empty:
        stop_event = threading.Event()
        t0 = time.time()

        def _spin(_t0=t0, _stop=stop_event, _n=n_tables):
            i = 0
            while not _stop.is_set():
                elapsed = time.time() - _t0
                frame = _FRAMES[i % len(_FRAMES)]
                sys.stdout.write(f"\r    [{frame}] Building indexes ({_n} tables)  {elapsed:.0f}s  ")
                sys.stdout.flush()
                i += 1
                _stop.wait(0.15)

        spinner_thread = threading.Thread(target=_spin, daemon=True)
        spinner_thread.start()

        error = None
        try:
            cursor.execute(batch)
            _drain_results(cursor)
        except pyodbc.Error as exc:
            error = exc
        finally:
            stop_event.set()
            spinner_thread.join(timeout=1)

        # Clear spinner line
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()

        if error is not None:
            _log("FAIL", f"    CCI batch failed")
            raise SqlServerImportError(
                f"Error in CCI script '{sql_file.name}'. Details: {error.args}"
            ) from error

    if not target_tables:
        return

    placeholders = ",".join("?" * len(target_tables))
    cursor.execute(
        f"SELECT t.name, "
        f"  SUM(CASE WHEN i.type_desc = 'CLUSTERED COLUMNSTORE' THEN 1 ELSE 0 END) "
        f"FROM sys.tables t "
        f"JOIN sys.schemas s ON s.schema_id = t.schema_id "
        f"LEFT JOIN sys.indexes i ON i.object_id = t.object_id "
        f"WHERE t.name IN ({placeholders}) "
        f"  AND s.name NOT IN ('sys','INFORMATION_SCHEMA','admin') "
        f"GROUP BY t.name;",
        *target_tables,
    )
    cci_count_by_table = {row[0]: int(row[1] or 0) for row in cursor.fetchall()}

    missing = [t for t in target_tables if cci_count_by_table.get(t, 0) == 0]
    n_with_cci = len(target_tables) - len(missing)

    _log("WORK", f"    {n_with_cci}/{len(target_tables)} tables now have CCI")
    for t in missing:
        _log("WARN", f"    {t}: no CCI")


_BULK_INSERT_SPLIT_RE = re.compile(
    r"(?=" + _BULK_INSERT_LINE_PREFIX + r")",
    flags=re.MULTILINE | re.IGNORECASE,
)


def _run_parallel_chunks(
    db_conn_str: str,
    statements: list[str],
    n_workers: int,
    on_chunk_done=None,
) -> None:
    """Execute BULK INSERT chunks across n_workers dedicated connections.

    Each worker holds one autocommit pyodbc connection and pulls statements
    from a shared queue until drained or until any worker reports an error.
    Safe for HEAP loads with TABLOCK: SQL Server's bulk-update lock allows
    concurrent BULK INSERTs into the same heap.

    on_chunk_done: optional thread-safe callable invoked once per successfully
    completed chunk. Used by callers to drive a live progress display.
    """
    import queue
    import threading as _t

    task_q: "queue.Queue[str]" = queue.Queue()
    for stmt in statements:
        task_q.put(stmt)

    errors: list[BaseException] = []
    errors_lock = _t.Lock()
    stop_event = _t.Event()

    def _worker() -> None:
        try:
            conn = pyodbc.connect(db_conn_str, autocommit=True)
        except pyodbc.Error as exc:
            with errors_lock:
                errors.append(exc)
            stop_event.set()
            return

        try:
            _try_disable_query_timeout(conn)
            cur = conn.cursor()
            while not stop_event.is_set():
                try:
                    stmt = task_q.get_nowait()
                except queue.Empty:
                    return
                try:
                    cur.execute(stmt)
                    _drain_results(cur)
                except pyodbc.Error as exc:
                    with errors_lock:
                        errors.append(exc)
                    stop_event.set()
                    return
                if on_chunk_done is not None:
                    on_chunk_done()
        finally:
            try:
                conn.close()
            except pyodbc.Error:
                pass

    threads = [_t.Thread(target=_worker, daemon=True) for _ in range(n_workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    if errors:
        raise errors[0]


def _split_load_statements(sql_text: str) -> list[tuple[str, str]]:
    """Split a load script into individual BULK INSERT statements.

    Returns list of (table_name, sql_statement) tuples.
    If the script uses GO separators, splits on those instead.
    Preamble (SET NOCOUNT ON, etc.) is attached to the first statement.
    """
    # Try GO-split first
    go_parts = _GO_SPLIT_RE.split(sql_text)
    if len(go_parts) > 1:
        result = []
        for part in go_parts:
            part = part.strip()
            if not part:
                continue
            table = _extract_table_from_batch(part)
            result.append((table or "batch", part))
        return result

    # No GO separators — split on BULK INSERT boundaries
    parts = _BULK_INSERT_SPLIT_RE.split(sql_text)
    result = []
    preamble = ""

    for part in parts:
        stripped = part.strip()
        if not stripped:
            continue
        table = _extract_table_from_batch(stripped)
        if not table:
            # Preamble (SET NOCOUNT ON, comments, etc.)
            preamble = stripped + "\n"
            continue
        sql = preamble + stripped
        preamble = ""  # preamble only prepended to the first statement
        result.append((table, sql))

    return result


def _execute_load_with_progress(
    cursor,
    sql_file: Path,
    *,
    base: Path = None,
    label: str = "Loading",
    db_conn_str: str | None = None,
    load_workers: int = 1,
) -> None:
    """Execute a load script with a live spinner, aggregating per table.

    Splits the script into individual BULK INSERT statements, groups
    chunks of the same table together, and shows one spinner per
    logical table (not per chunk).  Tables under 0.2s are grouped
    into a single "N others" summary line.

    When ``load_workers > 1`` and ``db_conn_str`` is provided, multi-chunk
    table groups are loaded in parallel across dedicated worker connections.
    Single-chunk groups and preamble batches still run on the outer cursor.
    """
    time = _time
    threading = _threading

    sql_text = _read_sql_text(sql_file)
    statements = _split_load_statements(sql_text)

    if not statements:
        execute_sql_batches(cursor, sql_file)
        return

    # Group consecutive statements by table name so chunked tables
    # (e.g. 24 InventorySnapshot chunks) show as one line.
    grouped: list[tuple[str, list[str]]] = []
    for table_name, sql_stmt in statements:
        if not table_name or table_name == "batch":
            # Preamble / non-BULK-INSERT batch — execute silently
            try:
                cursor.execute(sql_stmt)
                _drain_results(cursor)
            except pyodbc.Error:
                pass
            continue
        if grouped and grouped[-1][0] == table_name:
            grouped[-1][1].append(sql_stmt)
        else:
            grouped.append((table_name, [sql_stmt]))

    _FRAMES = ["-", "\\", "|", "/"]
    _SMALL_THRESHOLD = 0.2  # seconds — tables faster than this are grouped

    parallel_enabled = load_workers > 1 and db_conn_str is not None

    # Two-pass: execute all, collect results, then print (so we can group small ones)
    results: list[tuple[str, float, int]] = []  # (table_name, elapsed, n_chunks)

    for table_name, sql_stmts in grouped:
        n_chunks = len(sql_stmts)
        run_parallel = parallel_enabled and n_chunks > 1
        n_active = min(load_workers, n_chunks) if run_parallel else 1

        # Live chunk counter (thread-safe via GIL on int read; lock guards increment).
        done_count = [0]
        done_lock = threading.Lock()

        def _on_chunk_done(_d=done_count, _l=done_lock):
            with _l:
                _d[0] += 1

        def _suffix(_n=n_chunks, _na=n_active, _rp=run_parallel, _d=done_count):
            if _n <= 1:
                return ""
            progress = _d[0]
            if _rp:
                return f" ({progress}/{_n} chunks, {_na} workers)"
            return f" ({progress}/{_n} chunks)"

        stop_event = threading.Event()
        t0 = time.time()

        def _spin(_tbl=table_name, _t0=t0, _stop=stop_event, _suf=_suffix):
            i = 0
            while not _stop.is_set():
                elapsed = time.time() - _t0
                frame = _FRAMES[i % len(_FRAMES)]
                sys.stdout.write(f"\r    [{frame}] {_tbl}{_suf()}  {elapsed:.0f}s  ")
                sys.stdout.flush()
                i += 1
                _stop.wait(0.15)

        spinner_thread = threading.Thread(target=_spin, daemon=True)
        spinner_thread.start()

        error = None
        try:
            if run_parallel:
                _run_parallel_chunks(db_conn_str, sql_stmts, n_active, on_chunk_done=_on_chunk_done)
            else:
                for stmt in sql_stmts:
                    cursor.execute(stmt)
                    _drain_results(cursor)
                    _on_chunk_done()
        except pyodbc.Error as exc:
            error = exc
        finally:
            stop_event.set()
            spinner_thread.join(timeout=1)

        elapsed = time.time() - t0

        # Clear spinner line
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

        if error is not None:
            _log("WORK", f"    FAIL {table_name} ({elapsed:.1f}s)")
            raise SqlServerImportError(
                f"Error loading {table_name} in '{sql_file.name}'. Details: {error.args}"
            ) from error

        results.append((table_name, elapsed, n_chunks))

        # Print immediately for tables that take significant time
        if elapsed >= _SMALL_THRESHOLD:
            chunk_note = ""
            if n_chunks > 1:
                chunk_note = f", {n_chunks} chunks"
                if run_parallel:
                    chunk_note += f", {n_active} workers"
            _log("WORK", f"    {table_name} ({elapsed:.1f}s{chunk_note})")

    # Print grouped summary for small tables
    small = [(t, e) for t, e, _ in results if e < _SMALL_THRESHOLD]
    if small:
        small_total = sum(e for _, e in small)
        _log("WORK", f"    {len(small)} others ({small_total:.1f}s)")




def _try_disable_query_timeout(conn) -> None:
    """
    pyodbc timeout support differs by build/driver.
    Prefer connection-level timeout; do nothing if unsupported.
    """
    try:
        # 0 commonly means "no timeout" for ODBC query timeout.
        conn.timeout = 0
    except (AttributeError, OSError):
        pass


# -------------------------
# DB helpers
# -------------------------
def database_exists(cursor, database: str) -> bool:
    cursor.execute("SELECT 1 FROM sys.databases WHERE name = ?", database)
    return cursor.fetchone() is not None


def _quote_db_name(database: str) -> str:
    # Safe bracket quoting for SQL Server identifiers
    return f"[{database.replace(']', ']]')}]"


def create_database_if_not_exists(cursor, database: str) -> None:
    """
    Create the database if it does not already exist.
    Must be executed in autocommit mode.
    """
    try:
        cursor.execute(f"CREATE DATABASE {_quote_db_name(database)}")
    except pyodbc.Error as exc:
        raise SqlServerImportError(
            f"Failed to create database '{database}'. Details: {exc.args}"
        ) from exc


# -------------------------
# Script discovery / ordering
# -------------------------
def _is_view_file(p: Path) -> bool:
    n = p.name.lower()
    return "view" in n or "views" in n


def _is_constraint_file(p: Path) -> bool:
    n = p.name.lower()
    return (
        "constraint" in n
        or n.startswith("fk")
        or "_fk" in n
        or "foreignkey" in n
        or "foreign_key" in n
        or n.startswith("pk")
        or "_pk" in n
        or "primarykey" in n
        or "primary_key" in n
    )


def _is_cci_file(p: Path) -> bool:
    n = p.name.lower()
    return "cci" in n or "columnstore" in n


def _is_verify_file(p: Path) -> bool:
    n = p.name.lower()
    return "verify" in n


def _collect_phase_scripts(sql_dir: Path) -> Tuple[List[Path], List[Path], List[Path], List[Path], List[Path]]:
    """
    Collect scripts for phases:
      tables, views, constraints, cci_apply, verify

    Supported layouts:

    A) Preferred layout:
      sql/schema/tables/*.sql
      sql/schema/views/*.sql
      sql/schema/constraints/*.sql
      sql/cci/*.sql                   (optional apply scripts)

    B) Alternate layout:
      sql/schema/*.sql                (flat)
      sql/views/*.sql (optional)
      sql/constraints/*.sql (optional)
      sql/cci/*.sql (optional)

    Notes:
    - Any file inferred as CCI (name contains 'cci'/'columnstore') is treated as OPTIONAL apply
      and is excluded from the always-run phases.
    """
    schema_dir = sql_dir / "schema"
    if not schema_dir.is_dir():
        return [], [], [], [], []

    schema_tables_dir = schema_dir / "tables"
    schema_views_dir = schema_dir / "views"
    schema_constraints_dir = schema_dir / "constraints"

    top_views_dir = sql_dir / "views"
    top_constraints_dir = sql_dir / "constraints"

    cci_dir = sql_dir / "cci"
    indexes_dir = sql_dir / "indexes"

    # Structured schema folders (preferred)
    if schema_tables_dir.is_dir() or schema_views_dir.is_dir() or schema_constraints_dir.is_dir():
        tables = list_sql_files(schema_tables_dir)
        views = list_sql_files(schema_views_dir)
        constraints = list_sql_files(schema_constraints_dir)

        cci_apply = list_sql_files(cci_dir)
        cci_apply += [p for p in list_sql_files(indexes_dir) if _is_cci_file(p)]
        return tables, views, constraints, cci_apply, []

    # Flat schema folder with inference
    schema_files = list_sql_files(schema_dir)

    views = list_sql_files(top_views_dir) or [p for p in schema_files if _is_view_file(p)]
    constraints = list_sql_files(top_constraints_dir) or [p for p in schema_files if _is_constraint_file(p)]

    cci_apply = list_sql_files(cci_dir)
    cci_apply += [p for p in list_sql_files(indexes_dir) if _is_cci_file(p)]
    inferred_cci_from_schema = [p for p in schema_files if _is_cci_file(p)]
    verify = [p for p in schema_files if _is_verify_file(p)]

    excluded = set(views) | set(constraints) | set(inferred_cci_from_schema) | set(verify)
    tables = [p for p in schema_files if p not in excluded]

    cci_apply += inferred_cci_from_schema

    # Deduplicate while preserving order
    seen = set()
    cci_apply_unique: List[Path] = []
    for p in cci_apply:
        if p not in seen:
            seen.add(p)
            cci_apply_unique.append(p)

    return tables, views, constraints, cci_apply_unique, verify


# -------------------------
# Tabular user provisioning
# -------------------------
TABULAR_LOGIN_DEFAULT = "tabular_user"
_TABULAR_LOGIN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def validate_tabular_login_name(login: str) -> str:
    """Reject login names that are unsafe to embed in dynamic SQL.

    The name is interpolated into both an identifier (``[name]``) and a
    string literal (``WHERE name = 'name'``); restricting to letters/digits/
    underscores avoids needing to escape either context.
    """
    if not login or not _TABULAR_LOGIN_RE.match(login):
        raise ValueError(
            f"Invalid tabular login name: {login!r}. "
            "Must start with a letter or underscore and contain only "
            "letters, digits, and underscores (max 128 chars)."
        )
    return login


def _provision_tabular_user(
    connection_string: str,
    database: str,
    login: str,
    password: str | None,
) -> None:
    """Create a SQL login + per-DB user + db_owner membership (idempotent).

    Designed to make the imported database immediately usable by SSAS Tabular /
    Power BI without manual user setup. The same login can be reused across
    every imported database — re-running on a new DB just adds a user mapping.
    Logs per-step outcome (created vs. already existed) so the operator can see
    exactly what changed.
    """
    if not password:
        raise SqlServerImportError(
            "Tabular user provisioning requires a password "
            "(set SYNDATA_TABULAR_PASSWORD)."
        )

    login = validate_tabular_login_name(login)
    db_q = _quote_db_name(database)

    with pyodbc.connect(connection_string, autocommit=True) as conn:
        _try_disable_query_timeout(conn)
        cursor = conn.cursor()

        # 1. Server login (master scope)
        cursor.execute(
            "SELECT 1 FROM sys.server_principals "
            "WHERE name = ? AND type_desc = 'SQL_LOGIN'",
            login,
        )
        if cursor.fetchone():
            _log("SKIP", f"    Server login [{login}] already exists")
        else:
            # CREATE LOGIN does not accept its password as a parameter,
            # so embed it as a literal. The login name is regex-validated.
            pw_lit = sql_escape_literal(password)
            cursor.execute(
                f"CREATE LOGIN [{login}] WITH PASSWORD = '{pw_lit}', "
                "CHECK_POLICY = ON, CHECK_EXPIRATION = OFF;"
            )
            _drain_results(cursor)
            _log("DONE", f"    Created server login [{login}]")

        # Switch to target DB for steps 2-3
        cursor.execute(f"USE {db_q};")
        _drain_results(cursor)

        # 2. Database user
        cursor.execute(
            "SELECT 1 FROM sys.database_principals WHERE name = ?", login,
        )
        if cursor.fetchone():
            _log("SKIP", f"    Database user [{login}] already exists in [{database}]")
        else:
            cursor.execute(f"CREATE USER [{login}] FOR LOGIN [{login}];")
            _drain_results(cursor)
            _log("DONE", f"    Created database user [{login}] in [{database}]")

        # 3. db_owner membership
        cursor.execute(
            "SELECT 1 FROM sys.database_role_members drm "
            "JOIN sys.database_principals r ON r.principal_id = drm.role_principal_id "
            "JOIN sys.database_principals m ON m.principal_id = drm.member_principal_id "
            "WHERE r.name = 'db_owner' AND m.name = ?",
            login,
        )
        if cursor.fetchone():
            _log("SKIP", f"    [{login}] already member of db_owner")
        else:
            cursor.execute(f"ALTER ROLE [db_owner] ADD MEMBER [{login}];")
            _drain_results(cursor)
            _log("DONE", f"    Added [{login}] to db_owner")


# -------------------------
# Main import
# -------------------------
def _run_verify(cursor) -> None:
    """
    Execute verify.RunAll if the procedure exists.

    Fetches the detail rows (Suite, Category, Check, Description, Result, ActualValue)
    and the summary row, then logs the results.
    """
    # Check if the proc exists
    cursor.execute(
        "SELECT 1 FROM sys.procedures p "
        "JOIN sys.schemas s ON s.schema_id = p.schema_id "
        "WHERE s.name = 'verify' AND p.name = 'RunAll'"
    )
    if cursor.fetchone() is None:
        _log("SKIP", "  verify.RunAll not found; skipping verification")
        return

    cursor.execute("EXEC verify.RunAll")

    # First result set: detail rows
    # Columns: Suite, Category, Check, Description, Result, ActualValue
    rows = cursor.fetchall()
    passed = 0
    failed = 0
    info = 0
    seen: set = set()
    for row in rows:
        suite, _cat, check, _desc, result, actual = (
            row[0], row[1], row[2], row[3], row[4], row[5],
        )
        key = (suite, check, result)
        if key in seen:
            continue
        seen.add(key)
        if result == "PASS":
            passed += 1
            _log("PASS", f"    [{suite}] {check}  ({actual})")
        elif result == "INFO":
            info += 1
        else:
            failed += 1
            detail = f"  → {_desc}" if _desc and _desc != check else ""
            _log("FAIL", f"    [{suite}] {check}  ({actual}){detail}")

    # Second result set: summary (TotalChecks, Passed, Failed, Info, Verdict)
    if cursor.nextset():
        summary = cursor.fetchone()
        if summary:
            verdict = summary[4] if len(summary) > 4 else "UNKNOWN"
            level = "DONE" if failed == 0 else "WARN"
            _log(level, f"  Verification: {passed} passed, {failed} failed, {info} info - {verdict}")

    _drain_results(cursor)


def import_sql_server(
    *,
    server: str,
    database: str,
    run_dir: Path,
    connection_string: str,
    apply_cci: bool = False,
    drop_pk: bool = False,
    drop_pk_before_load: bool = False,
    restore_pk_after_load: bool = False,
    verify: bool = False,
    provision_tabular_user: bool = False,
    tabular_login: str = TABULAR_LOGIN_DEFAULT,
    tabular_password: str | None = None,
    load_workers: int = 4,
) -> None:
    if pyodbc is None:
        raise SqlServerImportError(
            "pyodbc is required for SQL Server import but is not installed.\n"
            "Install the SQL extra:  uv sync --extra sql"
        )
    import time as _time

    run_dir = Path(run_dir)
    sql_dir = run_dir / "sql"
    schema_dir = sql_dir / "schema"
    load_dir = sql_dir / "load"

    if not schema_dir.is_dir() or not load_dir.is_dir():
        raise SqlServerImportError(
            "SQL Server import is supported only for CSV runs. "
            "Expected 'sql/schema/' and 'sql/load/' folders in run directory."
        )

    bootstrap_dir = PROJECT_ROOT / "scripts" / "sql" / "bootstrap"
    cci_proc_file = bootstrap_dir / "create_cci_proc.sql"
    pk_proc_file = bootstrap_dir / "create_pk_proc.sql"

    tables_files, view_files, constraint_files, cci_apply_files, verify_files = _collect_phase_scripts(sql_dir)

    if not tables_files:
        raise SqlServerImportError(
            f"No table scripts found. Expected files under '{schema_dir}' "
            "or under 'sql/schema/tables/'."
        )

    _t_total = _time.time()

    # --- Header ---
    _log("INFO", "SQL Server Import")
    _log("INFO", f"  Server: {server}")

    # Step 1: ensure DB exists
    try:
        with pyodbc.connect(connection_string, autocommit=True) as conn:
            _try_disable_query_timeout(conn)
            cursor = conn.cursor()

            db_existed = database_exists(cursor, database)
            if db_existed:
                raise SqlServerImportError(
                    f"Database '{database}' already exists. "
                    "Import aborted to avoid partial drops / FK failures. "
                    "Use a new database name or drop the database first."
                )

            create_database_if_not_exists(cursor, database)
            _log("INFO", f"  Database: {database} (created)")

    except pyodbc.Error as exc:
        raise SqlServerImportError(
            f"Failed connecting to SQL Server '{server}'. Details: {exc.args}"
        ) from exc

    # Detect auth mode from connection string
    if "trusted_connection=yes" in connection_string.lower():
        _log("INFO", "  Auth: Windows Integrated")
    else:
        _log("INFO", "  Auth: SQL Authentication")

    db_conn_str = f"{connection_string};DATABASE={database}"

    # Step 2: core import (schema + load)
    try:
        with pyodbc.connect(db_conn_str, autocommit=False) as conn:
            _try_disable_query_timeout(conn)
            cursor = conn.cursor()

            # --- 2.1 Schema creation ---
            _t_schema = _time.time()
            _log("INFO", "  Creating Schema")

            all_schema_files = list(tables_files) + list(constraint_files) + list(view_files) + list(verify_files)
            for f in all_schema_files:
                _log("WORK", f"    {f.name}")
            execute_sql_files(cursor, all_schema_files)

            # Commit so parallel load workers (separate connections) can see the tables.
            conn.commit()

            _log("DONE", f"  Creating Schema completed in {_time.time() - _t_schema:.1f}s")

            # --- 2.1b Pre-load PK/FK drop (parallel-safe loads) ---
            if drop_pk_before_load:
                _t_predrop = _time.time()
                _log("INFO", "  Dropping PKs/UQs/FKs before load (enables parallel BULK INSERT scaling)")
                try:
                    with pyodbc.connect(db_conn_str, autocommit=True) as predrop_conn:
                        _try_disable_query_timeout(predrop_conn)
                        pre_cur = predrop_conn.cursor()

                        # Install the management proc here so we can use it before the
                        # post-import phase that normally installs it.
                        if pk_proc_file.is_file():
                            execute_sql_batches(pre_cur, pk_proc_file)
                        else:
                            raise SqlServerImportError(
                                f"PK proc not found: {pk_proc_file}. "
                                "Cannot honor --drop-pk-before-load."
                            )

                        pk_count_before, uq_count_before, fk_count_before = _count_user_constraints(pre_cur)

                        if pk_count_before == 0 and uq_count_before == 0 and fk_count_before == 0:
                            _log("INFO", "    No PKs, UQs, or FKs found to drop")
                        else:
                            pre_cur.execute("EXEC [admin].[ManagePrimaryKeys] @Action = 'DROP'")
                            _drain_results(pre_cur)
                            _log(
                                "DONE",
                                f"  Dropped {pk_count_before} PKs, {uq_count_before} UQs, "
                                f"{fk_count_before} FKs in {_time.time() - _t_predrop:.1f}s",
                            )
                            _log("INFO", "    Definitions saved to [admin].[_PK_Backup]")
                except pyodbc.Error as exc:
                    raise SqlServerImportError(
                        f"Failed pre-load PK/FK drop in '{database}'. Details: {exc.args}"
                    ) from exc

            # --- 2.2 Data loading ---
            ordered_load = ordered_load_files(load_dir)

            for load_file in ordered_load:
                is_dims = "dim" in load_file.name.lower()
                section = "Dimensions" if is_dims else "Facts"

                # Count tables in this load file
                sql_text = _read_sql_text(load_file)
                stmts = _split_load_statements(sql_text)
                table_names = [t for t, _ in stmts if t and t != "batch"]
                n_tables = len(set(table_names))

                _t_load = _time.time()
                _log("INFO", f"  Loading {section} ({n_tables} tables)")
                _log("WORK", f"    {load_file.name}")
                # Dimensions are tiny and single-chunk; only parallelize facts.
                file_workers = load_workers if not is_dims else 1
                _execute_load_with_progress(
                    cursor,
                    load_file,
                    base=run_dir,
                    label=section,
                    db_conn_str=db_conn_str,
                    load_workers=file_workers,
                )
                # Release any outer-cursor work between load files.
                conn.commit()
                _log("DONE", f"  Loading {section} completed in {_time.time() - _t_load:.1f}s")

            # --- 2.3 Row count verification ---
            try:
                dim_create = _find_create_sql(tables_files, "create_dimensions.sql")
                fact_create = _find_create_sql(tables_files, "create_facts.sql")

                dim_tables = _extract_tables_from_create_sql(dim_create) if dim_create else []
                fact_tables = _extract_tables_from_create_sql(fact_create) if fact_create else []

                _log("INFO", "  Row Counts")
                dim_total = 0
                for t in dim_tables:
                    try:
                        schema = _find_table_schema(cursor, t)
                        n = _fast_rowcount(cursor, schema, t)
                        dim_total += n
                    except (ValueError, KeyError, OSError):
                        pass
                fact_total = 0
                for t in fact_tables:
                    try:
                        schema = _find_table_schema(cursor, t)
                        n = _fast_rowcount(cursor, schema, t)
                        fact_total += n
                    except (ValueError, KeyError, OSError):
                        pass
                _log("INFO", f"    Dimensions: {len(dim_tables)} tables, {dim_total:,} rows")
                _log("INFO", f"    Facts: {len(fact_tables)} tables, {fact_total:,} rows")
            except (ValueError, KeyError, OSError) as _exc:
                _log("WARN", f"  Row count verification skipped: {_exc}")

            conn.commit()

        _log("DONE", f"SQL Server Import completed in {_time.time() - _t_total:.1f}s")

    except pyodbc.Error as exc:
        raise SqlServerImportError(
            f"Failed importing SQL into database '{database}'. Details: {exc.args}"
        ) from exc

    # Step 3: CCI + optional PK/FK drop (share one connection)
    if drop_pk or apply_cci or restore_pk_after_load:
        _log("INFO", "=" * 60)
    try:
        with pyodbc.connect(db_conn_str, autocommit=True) as conn:
            _try_disable_query_timeout(conn)
            cursor = conn.cursor()

            # 3.1 Install CCI management proc (always — ships with the database)
            if cci_proc_file.is_file():
                execute_sql_batches(cursor, cci_proc_file)
            if pk_proc_file.is_file():
                execute_sql_batches(cursor, pk_proc_file)

            # 3.2 Drop PK/FK constraints via stored proc (only when --drop-pk is set)
            #     Must run BEFORE CCI apply — CCI requires no clustered rowstore index.
            if drop_pk:
                _t_drop = _time.time()
                _log("INFO", "  Dropping Primary Keys, Unique Constraints & Foreign Keys")

                # Snapshot PK/UQ sizes before drop
                cursor.execute(
                    "SELECT t.name, kc.type, "
                    "CAST(SUM(ps.used_page_count) * 8.0 / 1024 AS DECIMAL(10,1)) "
                    "FROM sys.key_constraints kc "
                    "JOIN sys.tables t ON t.object_id = kc.parent_object_id "
                    "JOIN sys.schemas sc ON sc.schema_id = t.schema_id "
                    "JOIN sys.indexes i ON i.object_id = kc.parent_object_id AND i.name = kc.name "
                    "JOIN sys.dm_db_partition_stats ps ON ps.object_id = i.object_id AND ps.index_id = i.index_id "
                    "WHERE kc.type IN ('PK','UQ') AND t.is_ms_shipped = 0 "
                    "  AND sc.name NOT IN ('sys','INFORMATION_SCHEMA','admin') "
                    "GROUP BY t.name, kc.type ORDER BY SUM(ps.used_page_count) DESC"
                )
                pk_before = cursor.fetchall()

                if not pk_before:
                    _log("INFO", "    No primary keys or unique constraints found to drop")
                else:
                    cursor.execute(
                        "SELECT COUNT(*) FROM sys.foreign_keys fk "
                        "JOIN sys.tables t ON t.object_id = fk.parent_object_id "
                        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
                        "WHERE t.is_ms_shipped = 0 AND s.name NOT IN ('sys','INFORMATION_SCHEMA','admin')"
                    )
                    fk_count_before = int(cursor.fetchone()[0])
                    pk_count_before = 0
                    uq_count_before = 0
                    total_saved = 0.0
                    for _, ctype, size_mb in pk_before:
                        if ctype == _CT_PK:
                            pk_count_before += 1
                        elif ctype == _CT_UQ:
                            uq_count_before += 1
                        total_saved += float(size_mb)

                    cursor.execute("EXEC [admin].[ManagePrimaryKeys] @Action = 'DROP'")
                    _drain_results(cursor)

                    if fk_count_before:
                        _log("WORK", f"    Dropped {fk_count_before} foreign key constraints")
                    for tbl_name, ctype, size_mb in pk_before:
                        if float(size_mb) >= 1.0:
                            _log("WORK", f"    {tbl_name} [{ctype}] - freed {size_mb} MB")

                    _log("DONE", f"  Dropped {pk_count_before} PKs, {uq_count_before} UQs, "
                         f"{fk_count_before} FKs - "
                         f"freed {total_saved:.1f} MB in {_time.time() - _t_drop:.1f}s")
                    _log("INFO", "    Definitions saved to [admin].[_PK_Backup] for RESTORE")

            # 3.3 Apply CCI scripts (only when --apply-cci is set)
            if apply_cci and drop_pk:
                _log("INFO", "=" * 60)
            if apply_cci:
                if not cci_apply_files:
                    _log("INFO", "  No CCI apply scripts found; skipping.")
                else:
                    # CCI is applied to all user tables (dims + facts) by create_drop_cci.sql.
                    dim_create = _find_create_sql(tables_files, "create_dimensions.sql")
                    fact_create = _find_create_sql(tables_files, "create_facts.sql")
                    dim_tables = _extract_tables_from_create_sql(dim_create) if dim_create else []
                    fact_tables = _extract_tables_from_create_sql(fact_create) if fact_create else []
                    cci_target_tables = dim_tables + fact_tables

                    _t_cci = _time.time()
                    _log("INFO", f"  Applying Columnstore Indexes ({len(cci_target_tables)} tables)")
                    for f in cci_apply_files:
                        _log("WORK", f"    {f.name}")
                        _execute_cci_with_progress(cursor, f, cci_target_tables)
                    _log("DONE", f"  Applying Columnstore Indexes completed in {_time.time() - _t_cci:.1f}s")

                    # Verification
                    cursor.execute("SELECT COUNT(*) FROM sys.indexes WHERE type_desc = 'CLUSTERED COLUMNSTORE';")
                    total_ccis = int(cursor.fetchone()[0])

                    if total_ccis == 0:
                        raise SqlServerImportError(
                            "CCI apply completed without errors, but no CLUSTERED COLUMNSTORE index exists."
                        )

            # 3.4 Restore PKs/FKs from backup (only when --restore-pk-after-load is set)
            if restore_pk_after_load:
                if apply_cci or drop_pk:
                    _log("INFO", "=" * 60)
                _t_restore = _time.time()
                _log("INFO", "  Restoring Primary Keys & Foreign Keys")

                cursor.execute(
                    "SELECT "
                    "  SUM(CASE WHEN constraint_type = 'PK' THEN 1 ELSE 0 END), "
                    "  SUM(CASE WHEN constraint_type = 'UQ' THEN 1 ELSE 0 END), "
                    "  SUM(CASE WHEN constraint_type = 'FK' THEN 1 ELSE 0 END) "
                    "FROM [admin].[_PK_Backup]"
                )
                row = cursor.fetchone()
                pk_backup_count = int(row[0] or 0) if row else 0
                uq_backup_count = int(row[1] or 0) if row else 0
                fk_backup_count = int(row[2] or 0) if row else 0

                if pk_backup_count == 0 and uq_backup_count == 0 and fk_backup_count == 0:
                    _log("INFO", "    No backup definitions found in [admin].[_PK_Backup] — nothing to restore")
                else:
                    cursor.execute("EXEC [admin].[ManagePrimaryKeys] @Action = 'RESTORE'")
                    _drain_results(cursor)
                    _log(
                        "DONE",
                        f"  Restored {pk_backup_count} PKs, {uq_backup_count} UQs, "
                        f"{fk_backup_count} FKs in {_time.time() - _t_restore:.1f}s",
                    )

    except pyodbc.Error as exc:
        raise SqlServerImportError(
            f"Failed running post-import steps in database '{database}'. Details: {exc.args}"
        ) from exc

    # Step 4: optional tabular user provisioning
    if provision_tabular_user:
        _log("INFO", "=" * 60)
        _t_prov = _time.time()
        _log("INFO", f"  Provisioning [{tabular_login}] as DB_OWNER")
        try:
            _provision_tabular_user(
                connection_string, database, tabular_login, tabular_password,
            )
            _log("DONE", f"  Provisioning completed in {_time.time() - _t_prov:.1f}s")
        except (pyodbc.Error, SqlServerImportError, ValueError) as exc:
            # Non-fatal: data is already loaded and usable under the import login.
            _log("WARN", f"  Tabular user provisioning failed: {exc}")

    # Step 5: optional verification (run verify.RunAll scorecard)
    if not verify:
        return

    _log("INFO", "=" * 60)
    try:
        with pyodbc.connect(db_conn_str, autocommit=True) as conn:
            _try_disable_query_timeout(conn)
            cursor = conn.cursor()

            _t_verify = _time.time()
            _log("INFO", "  Running Data Verification")
            _run_verify(cursor)
            _log("DONE", f"  Data Verification completed in {_time.time() - _t_verify:.1f}s")

    except pyodbc.Error as exc:
        _log("WARN", f"  Verification failed: {exc.args}")
        # Non-fatal — data is already loaded

    # Grand total (import + drop-pk + CCI + verify)
    _log("INFO", "=" * 60)
    _log("DONE", f"Total pipeline time: {_time.time() - _t_total:.1f}s")