"""Shared helpers used by the SQL Server and Postgres importers.

The two importers have driver-specific bodies (pyodbc vs psycopg) but share
small bits: terminal logging, CREATE TABLE name extraction (used to verify
row counts), and a path-shortening utility for log lines.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path


# ANSI color codes matching the pipeline's logging_utils.py
_COLORS = {
    "INFO": "\033[94m",   # Blue
    "WORK": "\033[93m",   # Yellow
    "DONE": "\033[92m",   # Green
    "PASS": "\033[92m",   # Green (verification checks)
    "SKIP": "\033[90m",   # Grey
    "WARN": "\033[95m",   # Magenta
    "FAIL": "\033[91m",   # Red
    "LOAD": "\033[93m",   # Yellow (same as WORK)
    "RESET": "\033[0m",
}

_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")


def _log(level: str, msg: str) -> None:
    if _USE_COLOR:
        c = _COLORS.get(level, "")
        r = _COLORS["RESET"]
        print(f"{_ts()} | {c}{level:<4}{r} | {msg}")
    else:
        print(f"{_ts()} | {level:<4} | {msg}")


# Identifier in either [brackets], "double quotes", or bare form. Each
# variant gets its own named group; the extractor picks whichever matched.
# Bracketed/quoted forms allow embedded spaces (e.g. [My Table]) to match the
# identifier charset accepted by _validate_sql_identifier; bare names can't.
_IDENT = (
    r"(?:\[(?P<bracket>[\w ]+?)\]"
    r"|\"(?P<quoted>[\w ]+?)\""
    r"|(?P<bare>\w+))"
)
# Schema-qualified form: "schema.table" — capture only the table half;
# the schema's identifier is consumed but not named.
_QUALIFIED_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:\[\s*\w+\s*\]|\"\s*\w+\s*\"|\w+)\s*\.\s*" + _IDENT,
    flags=re.IGNORECASE,
)
_UNQUALIFIED_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _IDENT,
    flags=re.IGNORECASE,
)


def _extract_tables_from_create_sql(sql_file: Path) -> list[str]:
    """Extract table names from a CREATE TABLE script in execution order.

    Handles SQL Server ``[brackets]``, Postgres ``"double quotes"``, and
    bare identifiers, with an optional schema-qualified prefix. ``IF NOT
    EXISTS`` between CREATE TABLE and the identifier is tolerated.
    """
    try:
        txt = sql_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    out: list[str] = []
    seen: set[str] = set()

    for raw in txt.splitlines():
        line = re.sub(r"--.*$", "", raw).strip()
        if not line:
            continue
        if "CREATE" not in line.upper() or "TABLE" not in line.upper():
            continue

        m = _QUALIFIED_RE.search(line) or _UNQUALIFIED_RE.search(line)
        if not m:
            continue

        table = (m.group("bracket") or m.group("quoted") or m.group("bare") or "").strip()
        if not table:
            continue

        if table not in seen:
            seen.add(table)
            out.append(table)

    return out


def _short_path(p: Path, *, base: Path | None = None) -> str:
    """Prefer paths relative to ``base`` (if possible), else just the filename."""
    try:
        if base is not None:
            return p.resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, TypeError):
        pass
    return p.name


def list_sql_files(folder: Path) -> list[Path]:
    """Return sorted ``*.sql`` files in ``folder`` (empty list if folder missing)."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.sql") if p.is_file())


def find_create_sql(files, suffix: str) -> Path | None:
    """Return the first file in ``files`` whose name ends with ``suffix`` (case-insensitive)."""
    return next((p for p in files if p.name.lower().endswith(suffix)), None)


def run_timed_phase(conn, label: str, work) -> None:
    """Time + log around ``work(conn)``; commit at the end.

    Wraps any unit of importer work in the same INFO header / DONE
    elapsed-time format. Callers that drive a file list go through
    :func:`run_script_phase`; one-shot statements (e.g. ``ANALYZE;``)
    call this directly with their own closure.
    """
    import time as _time

    t0 = _time.time()
    _log("INFO", f"  {label}")
    work(conn)
    conn.commit()
    _log("DONE", f"  {label} completed in {_time.time() - t0:.1f}s")


def run_script_phase(
    conn,
    label: str,
    files: list[Path],
    *,
    run_dir: Path,
    execute,
) -> None:
    """Apply a list of SQL scripts as a single timed, committed phase.

    Shared by sequential importer phases (schema, admin tools, constraints,
    indexes). The Load phase doesn't use this — its per-file section
    labels and streaming COPY semantics differ enough to keep separate.

    ``execute`` is passed in so SQL Server (pyodbc) and Postgres (psycopg)
    importers can share the orchestration while keeping their driver-
    specific statement runners.
    """
    if not files:
        return

    def _body(c):
        for f in files:
            _log("WORK", f"    {_short_path(f, base=run_dir)}")
            execute(c, f)

    run_timed_phase(conn, label, _body)


def ordered_load_files(load_dir: Path) -> list[Path]:
    """Return load scripts in dims-then-facts order; warn about extras.

    Matches filenames by substring (``"dims"`` / ``"facts"``) so the same
    helper serves SQL Server (``01_bulk_insert_dims.sql``) and Postgres
    (``01_copy_dims.sql``).
    """
    load_files = list_sql_files(load_dir)
    dims = next((p for p in load_files if "dims" in p.name.lower()), None)
    facts = next((p for p in load_files if "facts" in p.name.lower()), None)

    ordered: list[Path] = []
    if dims is not None:
        ordered.append(dims)
    if facts is not None:
        ordered.append(facts)
    if not ordered:
        return load_files

    # The prepare/finish recovery-window scripts are expected and handled
    # separately by the importer (smart recovery management), so they are not
    # "stray" extras — only warn about genuinely unexpected files. Match the
    # documented ``*_prepare_load`` / ``*_finish_load`` suffixes (not bare
    # "prepare"/"finish"), so an unrelated file like ``03_facts_prepare_index``
    # is neither silenced here nor mistaken for a recovery script below.
    extras = [
        p for p in load_files
        if p not in ordered
        and "prepare_load" not in p.name.lower()
        and "finish_load" not in p.name.lower()
    ]
    if extras:
        _log("WARN", "Extra load scripts present; skipping by default:")
        for f in extras:
            _log("WARN", f"    {f.name}")
    return ordered


def find_prepare_load_script(load_dir: Path) -> Path | None:
    """Locate the optional pre-load recovery script (``00_*_prepare_load.sql``)."""
    return next(
        (p for p in list_sql_files(load_dir) if "prepare_load" in p.name.lower()), None
    )


def find_finish_load_script(load_dir: Path) -> Path | None:
    """Locate the optional post-load recovery script (``99_*_finish_load.sql``)."""
    return next(
        (p for p in list_sql_files(load_dir) if "finish_load" in p.name.lower()), None
    )
