"""argparse entrypoint for the Postgres importer.

Wraps ``src.tools.sql.postgres_import.import_postgres`` so it can be
invoked from a PowerShell wrapper (or any shell). Mirrors
``scripts/sql/run_sql_server_import.py`` in shape — validates inputs,
resolves the run directory, then delegates.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.exceptions import PostgresImportError
from src.tools.sql.postgres_import import import_postgres


def _resolve_run_dir(run_path: str) -> Path:
    run_dir = Path(run_path).expanduser().resolve()
    if not run_dir.exists():
        raise ValueError(f"Run path does not exist: {run_dir}")
    schema_dir = run_dir / "postgres" / "schema"
    if not schema_dir.is_dir():
        raise ValueError(
            f"Missing folder (Postgres CSV runs only): {schema_dir}. "
            "Re-generate with --format csv."
        )
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a Postgres database and import generated CREATE TABLE + COPY scripts.",
    )
    parser.add_argument("--host", default="localhost", help="Postgres host (default: localhost)")
    parser.add_argument("--port", type=int, default=5432, help="Postgres port (default: 5432)")
    parser.add_argument("--database", required=True, help="Target database name (must not yet exist)")
    parser.add_argument("--user", default="postgres", help="Postgres role (default: postgres)")
    parser.add_argument(
        "--password",
        default=None,
        help="Postgres password. Falls back to $PGPASSWORD if unset.",
    )
    parser.add_argument("--run-path", required=True, help="Generated run folder (contains postgres/schema/ and postgres/load/)")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the per-table row-count summary at the end.",
    )

    args = parser.parse_args()

    try:
        run_dir = _resolve_run_dir(args.run_path)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    password = args.password or os.environ.get("PGPASSWORD", "") or ""

    try:
        import_postgres(
            host=args.host,
            port=args.port,
            database=args.database,
            user=args.user,
            password=password,
            run_dir=run_dir,
            verify=not args.no_verify,
        )
    except PostgresImportError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
