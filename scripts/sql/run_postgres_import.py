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

from src.exceptions import PipelineError, PostgresImportError
from src.tools.sql.postgres_import import import_postgres


def _resolve_run_dir(run_path: str) -> Path:
    run_dir = Path(run_path).expanduser().resolve()
    if not run_dir.exists():
        raise ValueError(f"Run path does not exist: {run_dir}")
    schema_dir = run_dir / "sql" / "postgres" / "schema"
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
    parser.add_argument("--run-path", required=True, help="Generated run folder (contains sql/postgres/schema/ and sql/postgres/load/)")
    parser.add_argument(
        "--identifier-case",
        choices=["pascal", "snake"],
        default="pascal",
        help=(
            "Identifier casing to import under. 'pascal' (default) runs the "
            "PascalCase scripts packaged with the run. 'snake' re-renders the "
            "DDL/constraints/views/indexes/load in lower_snake_case from the "
            "run's config before importing (data files are never touched), so "
            "you can query the loaded tables without double-quoting."
        ),
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the per-table row-count summary at the end.",
    )
    parser.add_argument(
        "--load-workers",
        type=int,
        default=1,
        help=(
            "Parallelism for chunked tables (e.g. Sales with 200 CSV parts). "
            "Default 1 = single-connection sequential load. Set to your "
            "CPU count for large fact tables. Dim tables always load on "
            "the main connection."
        ),
    )
    parser.add_argument(
        "--maintenance-workers",
        type=int,
        default=0,
        help=(
            "Concurrent table-level VACUUM FREEZE ANALYZE workers. Default 0 "
            "uses a conservative auto value (1 for sequential loads, otherwise 2). "
            "Set 1 to force the lowest-I/O sequential maintenance path."
        ),
    )

    args = parser.parse_args()

    try:
        run_dir = _resolve_run_dir(args.run_path)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    password = args.password or os.environ.get("PGPASSWORD", "") or ""

    # Identifier casing is chosen at import. 'snake' re-renders the SQL from the
    # run's config into a temp dir; 'pascal' uses the packaged scripts.
    import contextlib
    import tempfile

    case = str(args.identifier_case or "pascal").strip().lower()
    try:
        with contextlib.ExitStack() as stack:
            sql_root_override = None
            if case != "pascal":
                from src.engine.packaging.reemit_sql import reemit_run_sql

                tmp = stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="cf_pg_import_")
                )
                sql_root_override = Path(tmp) / "sql"
                reemit_run_sql(run_dir=run_dir, out_sql_root=sql_root_override, case=case)

            import_postgres(
                host=args.host,
                port=args.port,
                database=args.database,
                user=args.user,
                password=password,
                run_dir=run_dir,
                verify=not args.no_verify,
                load_workers=args.load_workers,
                maintenance_workers=args.maintenance_workers,
                sql_root_override=sql_root_override,
            )
    except PostgresImportError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, PipelineError) as exc:
        # e.g. re-emit could not find/load the run's packaged config for a
        # non-pascal --identifier-case. PostgresImportError (a PipelineError
        # subclass) is handled above, so it never reaches here.
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
