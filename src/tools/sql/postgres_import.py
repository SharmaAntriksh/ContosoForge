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
    ``NOT VALID`` step and a separate ``VALIDATE CONSTRAINT`` step. Constraint
    work is grouped by parent table because PostgreSQL's validation lock is
    self-conflicting; this prevents workers from idling behind another
    validation of the same table while unrelated tables are ready to run.
    PK/UQ index builds use at most four external builders and share a bounded
    1 GiB maintenance-memory budget so concurrent sorts do not swamp storage
    or multiply memory with the COPY worker count.
  * Secondary indexes use the same bounded build policy: at most four
    external builders share 1 GiB, while each CREATE INDEX may use at most
    two Postgres parallel-maintenance workers internally.

A post-load ``VACUUM (FREEZE, ANALYZE, PARALLEL N)`` bakes in the
visibility map + frozen xmin so the loaded rows skip the first-read
hint-bit tax and qualify for index-only scans immediately. ``COPY ...
FREEZE`` can't be used inline with parallel chunked workers (the FREEZE
precondition is that the table was created in the same transaction —
only satisfiable by one session), so table-level maintenance workers pay
the freeze cost once per relation after the load.

Run-directory layout (CSV mode):
    <run>/sql/postgres/schema/01_create_dimensions.sql
    <run>/sql/postgres/schema/02_create_facts.sql
    <run>/sql/postgres/load/01_copy_dims.sql
    <run>/sql/postgres/load/02_copy_facts.sql

``import_postgres()`` connects via psycopg, optionally creates the target
database, applies the schema scripts, then the load scripts, then verifies
row counts. Connection details mirror libpq env vars
(``host``/``port``/``dbname``/``user``/``password``).
"""
from __future__ import annotations

import re
import shutil
import threading
import time as _time
from pathlib import Path

from src.exceptions import PostgresImportError
from src.tools.sql._import_common import (
    _extract_tables_from_create_sql,
    _log,
    _run_with_spinner,
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


def _orphan_db_hint(database: str) -> str:
    """Guidance appended to any failure that occurs after CREATE DATABASE.

    The importer aborts if the target DB already exists, so a database left
    half-populated by a mid-import failure blocks every retry until dropped.
    PG13+ supports ``DROP DATABASE ... WITH (FORCE)`` to evict stragglers.
    """
    return (
        f" NOTE: database '{database}' was created by this run and is now "
        f"partially populated; drop it (DROP DATABASE ... WITH (FORCE) on "
        f"PG13+) before retrying, or the 'already exists' guard will block "
        f"the next attempt."
    )


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
            _run_with_spinner(
                f"Executing {sql_file.name}",
                lambda: cur.execute(sql_text),  # type: ignore[arg-type]
            )
    except psycopg.Error as exc:  # type: ignore[union-attr]
        raise PostgresImportError(
            f"Error executing '{sql_file.name}': {exc}"
        ) from exc


# Pulls ``(target, path)`` pairs out of the generated COPY script so the
# load can be executed client-side via ``COPY ... FROM STDIN``. The WITH
# clause is intentionally ignored: every CSV the generator emits uses the
# same options, so we just hard-code them in the STDIN statement below.
# The path is a SQL single-quoted literal, so an embedded apostrophe is
# written doubled (``''``); the body matches non-quotes OR escaped pairs,
# and the caller un-doubles them via ``_unescape_sql_literal``.
_COPY_STATEMENT_RE = re.compile(
    r'COPY\s+(?P<target>(?:"[^"]+"\."[^"]+"|"[^"]+"|[\w.]+))\s+'
    r"FROM\s+'(?P<path>(?:[^']|'')*)'",
    flags=re.IGNORECASE,
)


def _unescape_sql_literal(value: str) -> str:
    """Reverse ``sql_escape_literal``: collapse doubled single quotes."""
    return value.replace("''", "'")

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
    on_item_done=None,
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
                        if on_item_done is not None:
                            on_item_done()
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
_MAINTENANCE_MEMORY_MB = 1024
_MAX_AUTO_MAINTENANCE_WORKERS = 2
_MAX_AUTO_INDEX_BUILD_WORKERS = 4
_MAX_INDEX_PARALLEL_WORKERS = 2


def _resolve_maintenance_workers(load_workers: int, requested: int) -> int:
    """Resolve bounded table-level V/F/A concurrency.

    COPY and VACUUM stress storage differently, so the COPY worker count is
    not reused wholesale. Auto mode uses at most two table workers; callers
    can force the old sequential behavior with ``requested=1``.
    """
    if requested < 0 or requested > 64:
        raise PostgresImportError(
            f"maintenance_workers must be between 0 and 64 (got {requested})."
        )
    if requested:
        return requested
    return min(max(1, load_workers), _MAX_AUTO_MAINTENANCE_WORKERS)


def _resolve_index_build_workers(load_workers: int) -> int:
    """Bound concurrent index builds independently from COPY concurrency."""
    return min(max(1, load_workers), _MAX_AUTO_INDEX_BUILD_WORKERS)


def _index_build_tuning(n_workers: int) -> list[str]:
    """Allocate a fixed total memory budget across index-builder sessions."""
    memory_mb = max(1, _MAINTENANCE_MEMORY_MB // max(1, n_workers))
    return _DDL_BASE_TUNING + [
        f"SET maintenance_work_mem = '{memory_mb}MB';",
        f"SET max_parallel_maintenance_workers = {_MAX_INDEX_PARALLEL_WORKERS};",
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
_ALTER_TABLE_TARGET_RE = re.compile(
    r'ALTER\s+TABLE\s+("[^"]+"\."[^"]+")',
    re.IGNORECASE,
)
_CREATE_INDEX_TARGET_RE = re.compile(
    r'CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+'
    r'(?P<index>"[^"]+")\s+ON\s+'
    r'(?P<table>"[^"]+"\."[^"]+")',
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
    item_label=None,
) -> None:
    """Dispatch independent DO blocks and optionally report slow items."""
    if not blocks:
        return
    n_active = min(n_workers, len(blocks))
    _log("WORK", f"    {phase_label} — {len(blocks)} statements across {n_active} workers")

    timings: list[tuple[str, float]] = []
    timings_lock = threading.Lock()

    def _run_block(cur, block: str) -> None:
        started = _time.time()
        cur.execute(block)
        if item_label is not None:
            with timings_lock:
                timings.append((item_label(block), _time.time() - started))

    _run_with_spinner(
        phase_label,
        lambda: _run_parallel_in_workers(
            blocks,
            _run_block,
            dsn=target_dsn,
            n_workers=n_active,
            setup_sql=setup_sql,
        ),
    )

    for label, elapsed in sorted(timings, key=lambda item: item[1], reverse=True):
        if elapsed >= 0.2:
            _log("WORK", f"    {label} ({elapsed:.1f}s)")


def _index_block_label(block: str) -> str:
    """Return a readable table/index label for one generated index block."""
    match = _CREATE_INDEX_TARGET_RE.search(block)
    if match is None:
        return "unrecognized index statement"
    return f"{match.group('table')} {match.group('index')}"


def _group_ddl_blocks_by_table(
    blocks: list[str],
) -> list[tuple[str, list[str]]]:
    """Group DDL by ALTER TABLE target, largest groups first.

    PostgreSQL's locks for PK/UQ creation, FK addition, and FK validation are
    self-conflicting. Dispatching two statements for one table to different
    workers therefore consumes a worker just to wait for a lock. A table group
    is the scheduler's unit of work so statements for that table stay serial
    while different tables still run concurrently.
    """
    groups: dict[str, list[str]] = {}
    for ordinal, block in enumerate(blocks, start=1):
        match = _ALTER_TABLE_TARGET_RE.search(block)
        # Generated constraint blocks always have an ALTER TABLE target. Keep
        # an unrecognized future shape isolated rather than accidentally
        # serializing it with another unrelated statement.
        target = match.group(1) if match else f"statement {ordinal}"
        groups.setdefault(target, []).append(block)
    # Stable sorting preserves generated order for ties while starting tables
    # with the most serial work first. This avoids a large table group becoming
    # the tail after workers have already drained all smaller groups.
    return sorted(groups.items(), key=lambda item: len(item[1]), reverse=True)


def _run_table_affine_ddl_phase(
    blocks: list[str],
    phase_label: str,
    *,
    target_dsn: dict,
    n_workers: int,
    setup_sql: list[str],
) -> None:
    """Run constraint DDL without same-table lock convoys."""
    if not blocks:
        return

    groups = _group_ddl_blocks_by_table(blocks)
    n_active = min(n_workers, len(groups))
    _log(
        "WORK",
        f"    {phase_label} — {len(blocks)} statements on "
        f"{len(groups)} tables across {n_active} workers",
    )

    timings: list[tuple[str, int, float]] = []
    timings_lock = threading.Lock()

    def _run_table_group(cur, group: tuple[str, list[str]]) -> None:
        table, statements = group
        started = _time.time()
        for statement in statements:
            cur.execute(statement)
        with timings_lock:
            timings.append((table, len(statements), _time.time() - started))

    _run_with_spinner(
        phase_label,
        lambda: _run_parallel_in_workers(
            groups,
            _run_table_group,
            dsn=target_dsn,
            n_workers=n_active,
            setup_sql=setup_sql,
        ),
    )

    for table, statement_count, elapsed in sorted(
        timings,
        key=lambda item: item[2],
        reverse=True,
    ):
        if elapsed >= 0.2:
            noun = "statement" if statement_count == 1 else "statements"
            _log(
                "WORK",
                f"    {table} ({elapsed:.1f}s, {statement_count} {noun})",
            )


def _parallel_copy_chunks(
    target: str,
    csv_paths: list[Path],
    *,
    dsn: dict,
    n_workers: int,
    run_dir: Path,
    on_chunk_done=None,
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
        on_item_done=on_chunk_done,
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
        csv_path = Path(_unescape_sql_literal(m.group("path")))
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
            run_parallel = parallel_ok and n_chunks > 1
            n_active = min(load_workers, n_chunks) if run_parallel else 1
            completed = [0]
            completed_lock = threading.Lock()

            def _on_chunk_done() -> None:
                with completed_lock:
                    completed[0] += 1

            def _detail() -> str:
                if n_chunks <= 1:
                    return ""
                worker_note = f", {n_active} workers" if run_parallel else ""
                with completed_lock:
                    completed_count = completed[0]
                return f" ({completed_count}/{n_chunks} chunks{worker_note})"

            def _load_table() -> None:
                if run_parallel:
                    _parallel_copy_chunks(
                        target, csv_paths,
                        dsn=parallel_dsn,  # type: ignore[arg-type]
                        n_workers=n_active,
                        run_dir=run_dir,
                        on_chunk_done=_on_chunk_done,
                    )
                    return
                for csv_path in csv_paths:
                    _copy_one_chunk(cur, target, csv_path, run_dir=run_dir)
                    _on_chunk_done()

            t0 = _time.time()
            _run_with_spinner(f"Loading {target}", _load_table, detail=_detail)
            elapsed = _time.time() - t0
            if elapsed >= 0.2:
                chunk_note = f", {n_chunks} chunks" if n_chunks > 1 else ""
                if run_parallel:
                    chunk_note += f", {n_active} workers"
                _log("WORK", f"    {target} ({elapsed:.1f}s{chunk_note})")


def _apply_constraints_parallel(
    files: list[Path],
    *,
    target_dsn: dict,
    n_workers: int,
    run_dir: Path,
) -> None:
    """Apply constraints in dependency order with table-affine workers.

    The generator emits PKs and FKs intermixed in one file; parallelizing
    blindly would race FKs against their referenced PKs. Splitting by
    classification preserves the dependency order. Within each phase, all
    statements for one parent table stay on one worker because the relevant
    PostgreSQL table locks self-conflict; different tables still run in
    parallel without blocked workers forming a same-table lock convoy.
    """
    pk_blocks: list[str] = []
    add_blocks: list[str] = []      # FK NOT VALID adds + CHECKs + others (fast metadata)
    validate_blocks: list[str] = [] # FK VALIDATE; one worker per parent table

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

    pk_workers = _resolve_index_build_workers(n_workers)
    _run_table_affine_ddl_phase(
        pk_blocks,
        "PK/UQ phase",
        target_dsn=target_dsn,
        n_workers=pk_workers,
        setup_sql=_index_build_tuning(pk_workers),
    )

    for blocks, label in (
        (add_blocks,      "FK NOT VALID + CHECK phase"),
        (validate_blocks, "VALIDATE CONSTRAINT phase"),
    ):
        _run_table_affine_ddl_phase(
            blocks,
            label,
            target_dsn=target_dsn,
            n_workers=n_workers,
            setup_sql=_DDL_BASE_TUNING,
        )

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
    The external builder count is independent from COPY concurrency. Builder
    sessions share a fixed maintenance-memory budget and retain a small amount
    of Postgres-internal parallelism for each CREATE INDEX.
    """
    blocks = [b for f in files for b in _split_do_blocks(_read_sql_text(f))]
    if not blocks:
        return

    t0 = _time.time()
    _log("INFO", "  Creating Indexes")
    for f in files:
        _log("WORK", f"    {_short_path(f, base=run_dir)}")

    index_workers = _resolve_index_build_workers(n_workers)
    _run_ddl_phase(
        blocks,
        "Index phase",
        target_dsn=target_dsn,
        n_workers=index_workers,
        setup_sql=_index_build_tuning(index_workers),
        item_label=_index_block_label,
    )

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


def _list_vacuum_targets(conn) -> list[tuple[str, str, int]]:
    """Return database-local tables largest-first for table-level V/F/A.

    This mirrors database-wide ``VACUUM`` coverage while excluding shared
    catalogs (which database owners cannot vacuum) and TOAST relations (which
    are processed with their owning table). Partitioned parents are excluded;
    their ordinary child relations are listed and vacuumed exactly once.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, c.relname, pg_total_relation_size(c.oid) "
            "FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'm') "
            "  AND c.relisshared = false "
            "  AND n.nspname NOT LIKE 'pg_toast%' "
            "ORDER BY pg_total_relation_size(c.oid) DESC, n.nspname, c.relname;"
        )
        rows = cur.fetchall()

    targets = []
    for schema, table, size_bytes in rows:
        qualified = (
            f"{_DIALECT.quote_ident(str(schema))}."
            f"{_DIALECT.quote_ident(str(table))}"
        )
        targets.append((qualified, f"{schema}.{table}", int(size_bytes or 0)))
    return targets


def _vacuum_freeze_analyze_tables(
    *,
    target_dsn: dict,
    targets: list[tuple[str, str, int]],
    maintenance_workers: int,
    load_workers: int,
) -> None:
    """Run full V/F/A on every user table with bounded table concurrency.

    PostgreSQL's ``VACUUM ... PARALLEL`` parallelizes index cleanup, not the
    heap scan that dominates a fresh bulk load. Running separate tables on a
    small number of connections parallelizes those heap scans. When multiple
    table workers are active, nested index parallelism is disabled and the
    existing 1 GiB maintenance-memory budget is divided across workers.
    """
    if not targets:
        return

    n_active = min(maintenance_workers, len(targets))
    index_workers = load_workers if n_active == 1 else 0
    memory_mb = max(128, _MAINTENANCE_MEMORY_MB // n_active)
    setup_sql = _DDL_BASE_TUNING + [
        f"SET maintenance_work_mem = '{memory_mb}MB';",
        f"SET max_parallel_maintenance_workers = {index_workers};",
    ]

    completed = [0]
    timings: list[tuple[str, float, int]] = []
    progress_lock = threading.Lock()

    def _vacuum_one(cur, target: tuple[str, str, int]) -> None:
        qualified, display, size_bytes = target
        started = _time.time()
        cur.execute(
            f"VACUUM (FREEZE, ANALYZE, PARALLEL {index_workers}) "
            f"ONLY {qualified};"
        )
        elapsed = _time.time() - started
        with progress_lock:
            timings.append((display, elapsed, size_bytes))
            completed[0] += 1

    def _detail() -> str:
        with progress_lock:
            return f" ({completed[0]}/{len(targets)} tables, {n_active} workers)"

    _log(
        "WORK",
        f"    {len(targets)} tables across {n_active} maintenance workers",
    )
    _run_with_spinner(
        "Vacuum + Freeze + Analyze",
        lambda: _run_parallel_in_workers(
            targets,
            _vacuum_one,
            dsn=target_dsn,
            n_workers=n_active,
            setup_sql=setup_sql,
        ),
        detail=_detail,
    )

    for display, elapsed, size_bytes in sorted(
        timings,
        key=lambda item: item[1],
        reverse=True,
    ):
        if elapsed >= 0.2:
            size_mb = size_bytes / (1024 * 1024)
            _log("WORK", f"    {display} ({elapsed:.1f}s, {size_mb:,.0f} MB)")


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
    maintenance_workers: int = 0,
    sql_root_override: Path | None = None,
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
    maintenance_workers = _resolve_maintenance_workers(
        load_workers,
        maintenance_workers,
    )
    # Validate inputs before requiring the driver — fail fast on bad layout
    # without forcing psycopg to be installed just to see the error message.
    run_dir = Path(run_dir)
    # ``sql_root_override`` points at a re-rendered ``sql/`` tree (e.g. a snake
    # re-emit into a temp dir) so casing can be chosen at import; default uses the
    # PascalCase scripts packaged with the run.
    sql_root = Path(sql_root_override) if sql_root_override is not None else (run_dir / "sql")
    postgres_dir = sql_root / "postgres"
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
    db_created = False
    try:
        with pg.connect(**admin_dsn, autocommit=True) as admin_conn:
            if _database_exists(admin_conn, database):
                raise PostgresImportError(
                    f"Database '{database}' already exists. "
                    "Import aborted to avoid partial state. "
                    "Use a new database name or drop the database first."
                )
            _create_database(admin_conn, database, _DIALECT)
            db_created = True
            _tune_database_for_speed(admin_conn, database, _DIALECT)
            _log("INFO", f"  Database: {database} (created)")
    except pg.Error as exc:
        # Once CREATE DATABASE has run, a later failure (e.g. ALTER DATABASE
        # tuning) orphans the new DB rather than being a connection problem —
        # don't mislabel it "Failed connecting", and tell the user it now
        # blocks retries.
        if db_created:
            raise PostgresImportError(
                f"Failed tuning database '{database}' after creation: {exc}."
                + _orphan_db_hint(database)
            ) from exc
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
            f"Postgres import failed for database '{database}': {exc}."
            + _orphan_db_hint(database)
        ) from exc

    # VACUUM (FREEZE, ANALYZE) needs autocommit — VACUUM can't run inside a
    # transaction. FREEZE sets the visibility map + frozen xmin so subsequent
    # queries skip the first-read hint-bit tax and can use index-only scans.
    # Run generated tables on a bounded number of connections: VACUUM's own
    # PARALLEL option only parallelizes index processing, not table heap scans.
    try:
        with pg.connect(**target_dsn, autocommit=True) as final_conn:
            _t_vac = _time.time()
            _log("INFO", "  Vacuum + Freeze + Analyze")
            vacuum_targets = _list_vacuum_targets(final_conn)
            _vacuum_freeze_analyze_tables(
                target_dsn=target_dsn,
                targets=vacuum_targets,
                maintenance_workers=maintenance_workers,
                load_workers=load_workers,
            )
            _log("DONE", f"  Vacuum + Freeze + Analyze completed in {_time.time() - _t_vac:.1f}s")

            if verify:
                dim_create = find_create_sql(schema_files, "create_dimensions.sql")
                fact_create = find_create_sql(schema_files, "create_facts.sql")
                dim_tables = _extract_tables_from_create_sql(dim_create) if dim_create else []
                fact_tables = _extract_tables_from_create_sql(fact_create) if fact_create else []
                _verify_row_counts(final_conn, dim_tables=dim_tables, fact_tables=fact_tables)
    except pg.Error as exc:
        raise PostgresImportError(
            f"Post-load step failed for database '{database}': {exc}."
            + _orphan_db_hint(database)
        ) from exc

    _log("DONE", f"Postgres import complete in {_time.time() - _t_total:.1f}s")
