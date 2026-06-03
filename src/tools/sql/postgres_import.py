"""Postgres importer: apply generated CREATE TABLE + COPY scripts to a Postgres DB.

Sibling to ``sql_server_import.py``. Postgres lacks the SQL Server
lock-escalation problem that motivates the ``[admin].[ManagePrimaryKeys]``
proc (concurrent ``COPY`` sessions take compatible RowExclusiveLock), and
constraints are deferred to post-load via composer ordering — so by default
a single connection is fast enough. Pass ``load_workers > 1`` when one
table dominates the run (e.g. Sales emits 200 chunked CSVs); chunks of the
same table are then dispatched across N dedicated psycopg connections.

Beyond chunked COPY, ``load_workers > 1`` also unlocks:

  * Per-DB tuning via ``ALTER DATABASE … SET …`` at creation time
    (``wal_compression``, SSD-friendly I/O cost, work_mem) so every
    subsequent session inherits the right defaults.
  * Three-phase parallel constraint application: PK/UQ → FK NOT VALID
    + CHECK → VALIDATE CONSTRAINT. The FK ADD blocks are rewritten in
    Python from one block per FK into two — a microsecond metadata-only
    ``NOT VALID`` step and a separate ``VALIDATE CONSTRAINT`` step. The
    latter uses ``ShareUpdateExclusiveLock``, which is self-compatible,
    so multiple FKs on the same parent (e.g. Sales' six FKs) validate
    truly in parallel instead of serializing on AccessExclusiveLock.
  * Parallel index creation with ``maintenance_work_mem = 1GB`` and
    ``max_parallel_maintenance_workers = load_workers`` boosted per
    session so each CREATE INDEX uses parallel sort internally.

A post-load ``VACUUM (FREEZE, ANALYZE, PARALLEL N)`` bakes in the
visibility map + frozen xmin so the loaded rows skip the first-read
hint-bit tax and qualify for index-only scans immediately. ``COPY ...
FREEZE`` can't be used inline with parallel chunked workers (the FREEZE
precondition is that the table was created in the same transaction —
only satisfiable by one session), so we pay the freeze cost once here in
a single sequential pass.

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
    run_script_phase,
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
            "psycopg is required for Postgres import but is not installed.\n"
            "Install the Postgres extra:  uv sync --extra postgres"
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


def _copy_one_chunk(cur, target: str, csv_path: Path, *, run_dir: Path) -> None:
    """Stream a single CSV chunk into ``target`` via ``COPY ... FROM STDIN``."""
    stmt = f"COPY {target} FROM STDIN WITH ({_STDIN_COPY_OPTS})"
    try:
        with cur.copy(stmt) as copy, csv_path.open("rb") as fh:
            shutil.copyfileobj(fh, copy, length=_COPY_CHUNK_BYTES)
    except psycopg.Error as exc:  # type: ignore[union-attr]
        raise PostgresImportError(
            f"COPY into {target} from {_short_path(csv_path, base=run_dir)} "
            f"failed: {exc}"
        ) from exc


def _run_parallel_in_workers(
    items: list,
    work_fn,
    *,
    dsn: dict,
    n_workers: int,
    setup_sql: list[str] | None = None,
) -> None:
    """Run ``work_fn(cur, item)`` for each item across ``n_workers`` connections.

    Each worker opens its own autocommit psycopg connection, runs any
    ``setup_sql`` statements (e.g. ``SET synchronous_commit = off;``), then
    pulls items off a shared queue until drained or until any worker reports
    an error. First worker error stops the rest; remaining queue items are
    abandoned.
    """
    import queue
    import threading

    task_q: "queue.Queue" = queue.Queue()
    for item in items:
        task_q.put(item)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    stop_event = threading.Event()

    def _worker() -> None:
        try:
            conn = psycopg.connect(**dsn, autocommit=True)  # type: ignore[union-attr]
        except psycopg.Error as exc:  # type: ignore[union-attr]
            with errors_lock:
                errors.append(exc)
            stop_event.set()
            return
        try:
            with conn.cursor() as cur:
                if setup_sql:
                    for stmt in setup_sql:
                        cur.execute(stmt)
                while not stop_event.is_set():
                    try:
                        item = task_q.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        work_fn(cur, item)
                    except Exception as exc:
                        with errors_lock:
                            errors.append(exc)
                        stop_event.set()
                        return
        finally:
            try:
                conn.close()
            except psycopg.Error:  # type: ignore[union-attr]
                pass

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        raise errors[0]


# Per-session GUCs applied at the start of every DDL worker connection.
# synchronous_commit=off matters for the constraint phases — each ALTER
# auto-commits, and with 80+ constraints serialized fsync-per-commit was a
# real chunk of the runtime. For the COPY load phase it's a no-op (chunks
# are large, commits rare) and slightly noisy, so the load workers don't
# get this tuning.
_DDL_BASE_TUNING = ["SET synchronous_commit = off;"]


# Adds index-build memory + parallel-degree caps. Big sorts during CREATE
# INDEX (and the implicit ones inside VACUUM FREEZE) benefit from more
# work_mem; PG13+ uses max_parallel_maintenance_workers as the cap for
# both CREATE INDEX and VACUUM parallel index processing.
def _ddl_full_tuning(load_workers: int) -> list[str]:
    return _DDL_BASE_TUNING + [
        "SET maintenance_work_mem = '1GB';",
        f"SET max_parallel_maintenance_workers = {load_workers};",
    ]


# Database-level tunings (key, value) that auto-apply to every connection
# to this DB. Worth pinning at ALTER DATABASE time so we don't have to
# repeat them in every session's setup. wal_compression cuts WAL volume
# ~50% with small CPU cost — biggest single win for WAL-bound bulk loads.
_DB_PERSISTENT_TUNING: list[tuple[str, str]] = [
    ("wal_compression",          "on"),
    ("effective_io_concurrency", "200"),
    ("random_page_cost",         "1.1"),
    ("work_mem",                 "'64MB'"),
]


# `DO $$ … END $$;` blocks are the unit of work in the generated constraint
# and index files. Each block is independently idempotent (guarded by
# to_regclass / pg_constraint EXISTS checks), so we can dispatch them
# across workers without coordination — modulo the PK-before-FK split done
# by _is_pk_or_uq below.
_DO_BLOCK_RE = re.compile(r"DO\s*\$\$.*?END\s*\$\$\s*;", re.DOTALL | re.IGNORECASE)

# Comment stripping is required because the generated headers contain
# literal "DO $$ … END $$;" prose describing the convention. Without this
# the splitter would extract garbage statements from inside /* … */.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# FK constraints need their referenced PK/UQ to exist first, so we split
# blocks on these predicates. The FK ADD path also gets rewritten into a
# fast metadata-only "NOT VALID" step plus a separate VALIDATE step that
# uses ShareUpdateExclusiveLock — self-compatible, so multiple FKs on the
# same parent table validate in parallel.
_PK_OR_UQ_RE = re.compile(
    r"ADD\s+CONSTRAINT[^;]*?(?:PRIMARY\s+KEY|UNIQUE\s*\()",
    re.IGNORECASE | re.DOTALL,
)
_FK_CONSTRAINT_NAME_RE = re.compile(
    r'ADD\s+CONSTRAINT\s+"([^"]+)"\s+FOREIGN\s+KEY',
    re.IGNORECASE | re.DOTALL,
)
_FK_PARENT_TABLE_RE = re.compile(
    r'ALTER\s+TABLE\s+("[^"]+"\."[^"]+")\s*[\r\n]+\s*ADD\s+CONSTRAINT',
    re.IGNORECASE | re.DOTALL,
)
# Matches the trailing `REFERENCES "schema"."table" ("col");` — we insert
# ` NOT VALID` before the `;`.
_FK_END_RE = re.compile(
    r'(REFERENCES\s+"[^"]+"\."[^"]+"\s*\([^)]+\))\s*;',
    re.IGNORECASE | re.DOTALL,
)


def _split_do_blocks(sql_text: str) -> list[str]:
    return _DO_BLOCK_RE.findall(_BLOCK_COMMENT_RE.sub("", sql_text))


def _is_pk_or_uq(block: str) -> bool:
    return _PK_OR_UQ_RE.search(block) is not None


def _rewrite_fk_block(block: str) -> tuple[str, str] | None:
    """Split an FK DO block into (ADD NOT VALID, VALIDATE) blocks.

    Returns None if the block isn't an FK or doesn't match the expected
    shape (caller should fall back to running it unchanged).
    """
    cname = _FK_CONSTRAINT_NAME_RE.search(block)
    if not cname:
        return None
    parent = _FK_PARENT_TABLE_RE.search(block)
    if not parent:
        return None

    constraint_name = cname.group(1)
    parent_table = parent.group(1)

    modified, n_subs = _FK_END_RE.subn(r"\1 NOT VALID;", block, count=1)
    if n_subs == 0:
        return None

    validate = (
        "DO $$\nBEGIN\n"
        f"    IF EXISTS (SELECT 1 FROM pg_constraint "
        f"WHERE conname = '{constraint_name}' AND convalidated = false)\n"
        "    THEN\n"
        f"        ALTER TABLE {parent_table} VALIDATE CONSTRAINT \"{constraint_name}\";\n"
        "    END IF;\nEND $$;"
    )
    return modified, validate


def _run_ddl_phase(
    blocks: list[str],
    phase_label: str,
    *,
    target_dsn: dict,
    n_workers: int,
    setup_sql: list[str],
) -> None:
    """Dispatch a list of DO blocks across workers under one phase label."""
    if not blocks:
        return
    n_active = min(n_workers, len(blocks))
    _log("WORK", f"    {phase_label} — {len(blocks)} statements across {n_active} workers")
    _run_parallel_in_workers(
        blocks,
        lambda cur, stmt: cur.execute(stmt),
        dsn=target_dsn,
        n_workers=n_active,
        setup_sql=setup_sql,
    )


def _parallel_copy_chunks(
    target: str,
    csv_paths: list[Path],
    *,
    dsn: dict,
    n_workers: int,
    run_dir: Path,
) -> None:
    """Load chunks for one table across ``n_workers`` dedicated connections.

    Safe because concurrent ``COPY`` sessions take compatible RowExclusiveLock
    on the heap; each backend writes to different pages via FSM coordination.
    """
    def _do_copy(cur, csv_path: Path) -> None:
        _copy_one_chunk(cur, target, csv_path, run_dir=run_dir)

    _run_parallel_in_workers(
        csv_paths, _do_copy,
        dsn=dsn,
        n_workers=n_workers,
    )


def _apply_copy_script_via_stdin(
    conn,
    sql_file: Path,
    *,
    run_dir: Path,
    load_workers: int = 1,
    parallel_dsn: dict | None = None,
) -> None:
    """Apply a generated COPY script using client-side STDIN streaming.

    The generated load scripts use server-side ``COPY ... FROM '<path>'``,
    which requires the Postgres server process to read the host filesystem
    directly. On native Windows installs the service account often lacks
    read permission on user-home directories; in Docker the host filesystem
    isn't visible at all without a bind mount. To sidestep both, we parse
    each ``COPY`` statement out of the script and execute it as
    ``COPY ... FROM STDIN``, streaming the CSV bytes from this Python
    process over the existing connection.

    When ``load_workers > 1`` and ``parallel_dsn`` is set, consecutive COPY
    statements for the same target table (one per chunk file) are dispatched
    across a pool of dedicated worker connections. Single-chunk groups still
    run on the main connection.
    """
    sql_text = _read_sql_text(sql_file)
    matches = list(_COPY_STATEMENT_RE.finditer(sql_text))
    if not matches:
        return

    # Group consecutive (target, csv_path) pairs by target. The generator
    # emits all chunks for a table contiguously, so consecutive grouping
    # captures the full chunk set without reordering work.
    groups: list[tuple[str, list[Path]]] = []
    for m in matches:
        target = m.group("target")
        csv_path = Path(m.group("path"))
        if not csv_path.is_file():
            raise PostgresImportError(
                f"CSV not found for {target}: {csv_path}. "
                "Did the generated run move or get deleted?"
            )
        if groups and groups[-1][0] == target:
            groups[-1][1].append(csv_path)
        else:
            groups.append((target, [csv_path]))

    parallel_ok = load_workers > 1 and parallel_dsn is not None

    with conn.cursor() as cur:
        for target, csv_paths in groups:
            n_chunks = len(csv_paths)
            if parallel_ok and n_chunks > 1:
                n_active = min(load_workers, n_chunks)
                t0 = _time.time()
                _log("WORK", f"    {target} — {n_chunks} chunks across {n_active} workers")
                _parallel_copy_chunks(
                    target, csv_paths,
                    dsn=parallel_dsn,  # type: ignore[arg-type]
                    n_workers=n_active,
                    run_dir=run_dir,
                )
                _log("DONE", f"    {target} — loaded in {_time.time() - t0:.1f}s")
            else:
                for csv_path in csv_paths:
                    _copy_one_chunk(cur, target, csv_path, run_dir=run_dir)


def _apply_constraints_parallel(
    files: list[Path],
    *,
    target_dsn: dict,
    n_workers: int,
    run_dir: Path,
) -> None:
    """Apply constraint files in two ordered phases (PK/UQ, then FK + other).

    The generator emits PKs and FKs intermixed in one file; parallelizing
    blindly would race FKs against their referenced PKs. Splitting by
    classification preserves the dependency order while still parallelizing
    within each phase. Multiple constraints on the same parent table
    serialize on AccessExclusiveLock; constraints on different tables run
    truly concurrently.
    """
    pk_blocks: list[str] = []
    add_blocks: list[str] = []      # FK NOT VALID adds + CHECKs + others (fast metadata)
    validate_blocks: list[str] = [] # FK VALIDATE — parallel even on same parent

    for f in files:
        for block in _split_do_blocks(_read_sql_text(f)):
            if _is_pk_or_uq(block):
                pk_blocks.append(block)
                continue
            rewritten = _rewrite_fk_block(block)
            if rewritten is None:
                # Non-FK (CHECK etc.) or unrecognized FK shape — runs in
                # the metadata-only phase under AccessExclusiveLock.
                add_blocks.append(block)
            else:
                add, validate = rewritten
                add_blocks.append(add)
                validate_blocks.append(validate)

    if not (pk_blocks or add_blocks or validate_blocks):
        return

    t0 = _time.time()
    _log("INFO", "  Applying Constraints")
    for f in files:
        _log("WORK", f"    {_short_path(f, base=run_dir)}")

    for blocks, label in (
        (pk_blocks,       "PK/UQ phase"),
        (add_blocks,      "FK NOT VALID + CHECK phase"),
        (validate_blocks, "VALIDATE CONSTRAINT phase"),
    ):
        _run_ddl_phase(blocks, label,
                       target_dsn=target_dsn, n_workers=n_workers,
                       setup_sql=_DDL_BASE_TUNING)

    _log("DONE", f"  Applying Constraints completed in {_time.time() - t0:.1f}s")


def _apply_indexes_parallel(
    files: list[Path],
    *,
    target_dsn: dict,
    n_workers: int,
    run_dir: Path,
) -> None:
    """Apply index DO blocks across workers.

    CREATE INDEX (non-CONCURRENT) takes a ShareLock that's compatible with
    itself, so multiple indexes on the same table can build concurrently.
    Each worker session gets maintenance_work_mem + max_parallel_maintenance_workers
    bumped — both speed up individual builds, and the latter unlocks
    Postgres' built-in parallel sort inside each CREATE INDEX.
    """
    blocks = [b for f in files for b in _split_do_blocks(_read_sql_text(f))]
    if not blocks:
        return

    t0 = _time.time()
    _log("INFO", "  Creating Indexes")
    for f in files:
        _log("WORK", f"    {_short_path(f, base=run_dir)}")

    _run_ddl_phase(blocks, "Index phase",
                   target_dsn=target_dsn, n_workers=n_workers,
                   setup_sql=_ddl_full_tuning(n_workers))

    _log("DONE", f"  Creating Indexes completed in {_time.time() - t0:.1f}s")


def _database_exists(admin_conn, database: str) -> bool:
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (database,))
        return cur.fetchone() is not None


def _create_database(admin_conn, database: str, dialect) -> None:
    # CREATE DATABASE cannot run inside a transaction block; the caller is
    # responsible for opening admin_conn with autocommit=True.
    with admin_conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE {dialect.quote_ident(database)};")


def _tune_database_for_speed(admin_conn, database: str, dialect) -> None:
    """Apply ALTER DATABASE GUCs that auto-attach to every future session.

    These are session-default settings we'd otherwise have to SET per
    worker — locking them into the DB once is cheaper and self-documents
    the import's tuning. ``wal_compression`` is the biggest single lever
    for WAL-bound bulk loads; the rest are SSD/modern-hardware defaults
    that the stock postgresql.conf still hasn't caught up to.
    """
    quoted = dialect.quote_ident(database)
    with admin_conn.cursor() as cur:
        for key, value in _DB_PERSISTENT_TUNING:
            cur.execute(f"ALTER DATABASE {quoted} SET {key} = {value};")


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
    load_workers: int = 1,
) -> None:
    """Apply generated Postgres DDL and COPY scripts to a target database.

    Connects to the ``postgres`` maintenance DB to create ``database`` if it
    does not exist, then connects to ``database`` and applies all schema
    scripts followed by all load scripts in order. ``verify=True`` prints
    a per-table row-count summary at the end.

    ``load_workers > 1`` dispatches chunks of the same table (e.g. the 200
    Sales CSV parts) across N psycopg connections. Single-chunk tables and
    the dimension load still run on the main connection.
    """
    if load_workers < 1:
        raise PostgresImportError(f"load_workers must be >= 1 (got {load_workers}).")
    # Validate inputs before requiring the driver — fail fast on bad layout
    # without forcing psycopg to be installed just to see the error message.
    run_dir = Path(run_dir)
    postgres_dir = run_dir / "postgres"
    schema_dir = postgres_dir / "schema"
    load_dir = postgres_dir / "load"
    admin_dir = postgres_dir / "admin"
    indexes_dir = postgres_dir / "indexes"

    if not schema_dir.is_dir():
        raise PostgresImportError(
            f"Postgres schema folder not found: {schema_dir}. "
            "Postgres import is supported only for CSV runs."
        )

    schema_files = list_sql_files(schema_dir)
    if not schema_files:
        raise PostgresImportError(f"No schema scripts found under {schema_dir}.")

    admin_files = list_sql_files(admin_dir)
    index_files = list_sql_files(indexes_dir)

    # Constraints are applied AFTER the load — adding FKs before the COPY
    # would force per-row validation and crush throughput.
    constraint_files = [
        f for f in schema_files if f.name.lower().endswith("_create_constraints.sql")
    ]
    schema_files = [f for f in schema_files if f not in constraint_files]

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
            _tune_database_for_speed(admin_conn, database, _DIALECT)
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

            run_script_phase(conn, "Creating Schema", schema_files,
                             run_dir=run_dir, execute=_execute_script)
            run_script_phase(conn, "Installing Admin Tools", admin_files,
                             run_dir=run_dir, execute=_execute_script)

            for load_file in load_files:
                is_dims = "dim" in load_file.name.lower()
                section = "Dimensions" if is_dims else "Facts"
                # Dim tables are single-chunk and tiny; parallel workers
                # would just spin up connections for nothing.
                file_workers = 1 if is_dims else load_workers
                _t_load = _time.time()
                _log("INFO", f"  Loading {section}")
                _log("WORK", f"    {_short_path(load_file, base=run_dir)}")
                _apply_copy_script_via_stdin(
                    conn, load_file,
                    run_dir=run_dir,
                    load_workers=file_workers,
                    parallel_dsn=target_dsn if file_workers > 1 else None,
                )
                conn.commit()
                _log("DONE", f"  Loading {section} completed in {_time.time() - _t_load:.1f}s")

            if load_workers > 1 and constraint_files:
                _apply_constraints_parallel(
                    constraint_files,
                    target_dsn=target_dsn,
                    n_workers=load_workers,
                    run_dir=run_dir,
                )
            else:
                run_script_phase(conn, "Applying Constraints", constraint_files,
                                 run_dir=run_dir, execute=_execute_script)

            if load_workers > 1 and index_files:
                _apply_indexes_parallel(
                    index_files,
                    target_dsn=target_dsn,
                    n_workers=load_workers,
                    run_dir=run_dir,
                )
            else:
                run_script_phase(conn, "Creating Indexes", index_files,
                                 run_dir=run_dir, execute=_execute_script)
    except pg.Error as exc:
        raise PostgresImportError(
            f"Postgres import failed for database '{database}': {exc}"
        ) from exc

    # VACUUM (FREEZE, ANALYZE) needs autocommit — VACUUM can't run inside a
    # transaction. FREEZE sets the visibility map + frozen xmin in one
    # sequential pass, so subsequent queries skip the first-read hint-bit
    # tax and can use index-only scans immediately. Also subsumes the
    # planner-statistics refresh the old ANALYZE phase did. PARALLEL N
    # (PG13+) parallelizes index processing — capped server-side by
    # max_parallel_maintenance_workers, so passing a larger N is harmless.
    try:
        with pg.connect(**target_dsn, autocommit=True) as final_conn:
            _t_vac = _time.time()
            _log("INFO", "  Vacuum + Freeze + Analyze")
            vacuum_opts = "FREEZE, ANALYZE"
            if load_workers > 1:
                vacuum_opts += f", PARALLEL {load_workers}"
            with final_conn.cursor() as cur:
                # Bump maintenance settings before VACUUM so the parallel
                # index workers actually have memory + worker slots to use.
                for stmt in _ddl_full_tuning(load_workers):
                    cur.execute(stmt)
                cur.execute(f"VACUUM ({vacuum_opts});")
            _log("DONE", f"  Vacuum + Freeze + Analyze completed in {_time.time() - _t_vac:.1f}s")

            if verify:
                dim_create = find_create_sql(schema_files, "create_dimensions.sql")
                fact_create = find_create_sql(schema_files, "create_facts.sql")
                dim_tables = _extract_tables_from_create_sql(dim_create) if dim_create else []
                fact_tables = _extract_tables_from_create_sql(fact_create) if fact_create else []
                _verify_row_counts(final_conn, dim_tables=dim_tables, fact_tables=fact_tables)
    except pg.Error as exc:
        raise PostgresImportError(
            f"Post-load step failed for database '{database}': {exc}"
        ) from exc

    _log("DONE", f"Postgres import complete in {_time.time() - _t_total:.1f}s")
