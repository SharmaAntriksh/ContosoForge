import argparse
import os
import sys
from pathlib import Path

# Ensure repo root is importable so "import src...." works when running this file directly
REPO_ROOT = Path(__file__).resolve().parents[2]  # scripts/sql/ -> scripts -> repo root
sys.path.insert(0, str(REPO_ROOT))

from src.tools.sql.sql_server_import import (
    import_sql_server,
    SqlServerImportError,
    TABULAR_LOGIN_DEFAULT,
)


def build_connection_string(args) -> str:
    """
    Build a SQL Server ODBC connection string from CLI arguments.
    """
    driver = args.odbc_driver or "ODBC Driver 17 for SQL Server"

    # Sanitize driver name to prevent connection string injection
    if ";" in driver or "{" in driver or "}" in driver:
        raise ValueError(f"Invalid ODBC driver name (contains illegal characters): {driver}")

    if args.trusted:
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={args.server};"
            "Trusted_Connection=yes;"
        )

    if not args.user or not args.user.strip():
        raise ValueError("Username must be a non-empty string when not using --trusted")
    password = args.password
    if getattr(args, "password_env", False):
        password = os.environ.get("SYNDATA_DB_PASSWORD", password)
    if not password:
        raise ValueError("Password must be provided when not using --trusted")

    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={args.server};"
        f"UID={args.user};PWD={password};"
    )


def _resolve_run_dir(run_path: str) -> Path:
    run_dir = Path(run_path).expanduser().resolve()

    sql_dir = run_dir / "sql"
    schema_dir = sql_dir / "schema"
    load_dir = sql_dir / "load"

    if not run_dir.exists():
        raise ValueError(f"Run path does not exist: {run_dir}")
    if not sql_dir.is_dir():
        raise ValueError(f"Missing folder: {sql_dir}")
    if not schema_dir.is_dir():
        raise ValueError(f"Missing folder (CSV runs only): {schema_dir}")
    if not load_dir.is_dir():
        raise ValueError(f"Missing folder (CSV runs only): {load_dir}")

    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create SQL Server database and import generated SQL scripts"
    )

    parser.add_argument(
        "--server",
        required=True,
        help="SQL Server hostname or instance name",
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Target database name",
    )
    parser.add_argument(
        "--run-path",
        required=True,
        help="Path to generator run output folder containing SQL scripts",
    )

    parser.add_argument("--user", help="SQL Server username")
    parser.add_argument("--password", help="SQL Server password")
    parser.add_argument(
        "--password-env",
        action="store_true",
        help="Read password from SYNDATA_DB_PASSWORD environment variable",
    )

    parser.add_argument(
        "--trusted",
        action="store_true",
        help="Use Windows Integrated Authentication",
    )

    parser.add_argument(
        "--apply-cci",
        action="store_true",
        help=(
            "Optionally execute CCI/columnstore apply scripts (if present). "
            "All other steps run every time."
        ),
    )

    parser.add_argument(
        "--drop-pk",
        action="store_true",
        help=(
            "Drop primary key and foreign key constraints after import. "
            "Reduces database size significantly for analytics workloads."
        ),
    )

    parser.add_argument(
        "--drop-pk-before-load",
        action="store_true",
        help=(
            "Drop primary key and foreign key constraints BEFORE the data load "
            "so BULK INSERT runs into pure heaps. Removes per-row PK maintenance "
            "and FK validation, which is the main bottleneck for parallel loads "
            "on fact tables (especially Sales with 11+ FKs). Definitions are "
            "saved to [admin].[_PK_Backup]. Pair with --restore-pk-after-load "
            "to re-add them automatically once the load is done."
        ),
    )

    parser.add_argument(
        "--restore-pk-after-load",
        action="store_true",
        help=(
            "Restore primary keys and foreign keys from [admin].[_PK_Backup] "
            "after the data load (and after CCI apply if --apply-cci). Use "
            "together with --drop-pk-before-load for the canonical pattern: "
            "fast parallel BULK INSERT into heaps, then re-add constraints. "
            "Cannot be combined with --drop-pk (conflicting end-state)."
        ),
    )

    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Run data verification after import. "
            "Executes verify.RunAll scorecard and reports PASS/FAIL results."
        ),
    )

    parser.add_argument(
        "--odbc-driver",
        default=None,
        help="Override ODBC driver name (default: ODBC Driver 17 for SQL Server).",
    )

    parser.add_argument(
        "--load-workers",
        type=int,
        default=4,
        help=(
            "Number of parallel BULK INSERT workers for multi-chunk fact tables. "
            "Each worker holds its own connection and loads chunks concurrently "
            "into the same heap (TABLOCK allows this). 1 disables parallelism. "
            "Default: 4. Diminishing returns past ~8 on a single NVMe."
        ),
    )

    parser.add_argument(
        "--no-recovery-management",
        action="store_true",
        help=(
            "Disable automatic recovery-model tuning. By default, if the target "
            "database is in FULL recovery the importer switches it to BULK_LOGGED "
            "for the load (faster, smaller transaction log) and restores it after "
            "(databases already in SIMPLE/BULK_LOGGED are left untouched). Pass "
            "this to leave the recovery model alone — e.g. on a production "
            "database whose log-backup chain must not be broken."
        ),
    )

    parser.add_argument(
        "--provision-tabular-user",
        action="store_true",
        help=(
            "After import, ensure the SQL login exists, create a matching user "
            "in the imported database, and grant DB_OWNER. Password must be "
            "provided via SYNDATA_TABULAR_PASSWORD env var."
        ),
    )

    parser.add_argument(
        "--tabular-login",
        default=TABULAR_LOGIN_DEFAULT,
        help=(
            f"Login name for the tabular user "
            f"(default: {TABULAR_LOGIN_DEFAULT}). "
            "Used as both the SQL login and the per-DB user."
        ),
    )

    args = parser.parse_args()

    if args.load_workers < 1:
        print("VALIDATION ERROR: --load-workers must be >= 1", file=sys.stderr)
        return 2

    if args.restore_pk_after_load and args.drop_pk:
        print(
            "VALIDATION ERROR: --restore-pk-after-load and --drop-pk are mutually "
            "exclusive (one re-adds PKs/FKs, the other drops them — pick one).",
            file=sys.stderr,
        )
        return 2

    if args.restore_pk_after_load and not args.drop_pk_before_load:
        print(
            "VALIDATION ERROR: --restore-pk-after-load requires --drop-pk-before-load "
            "(nothing to restore otherwise).",
            file=sys.stderr,
        )
        return 2

    try:
        run_dir = _resolve_run_dir(args.run_path)
        connection_string = build_connection_string(args)

        tabular_password = None
        if args.provision_tabular_user:
            tabular_password = os.environ.get("SYNDATA_TABULAR_PASSWORD")
            if not tabular_password:
                raise ValueError(
                    "--provision-tabular-user requires SYNDATA_TABULAR_PASSWORD env var to be set."
                )

        import_sql_server(
            server=args.server,
            database=args.database,
            run_dir=run_dir,
            connection_string=connection_string,
            apply_cci=bool(args.apply_cci),
            drop_pk=bool(args.drop_pk),
            drop_pk_before_load=bool(args.drop_pk_before_load),
            restore_pk_after_load=bool(args.restore_pk_after_load),
            verify=bool(args.verify),
            provision_tabular_user=bool(args.provision_tabular_user),
            tabular_login=args.tabular_login,
            tabular_password=tabular_password,
            load_workers=int(args.load_workers),
            manage_recovery=not bool(args.no_recovery_management),
        )

    except ValueError as exc:
        print(f"VALIDATION ERROR: {exc}", file=sys.stderr)
        return 2
    except SqlServerImportError as exc:
        print(f"IMPORT ERROR: {exc}", file=sys.stderr)
        return 1
    except ConnectionError as exc:
        print(f"CONNECTION ERROR: {exc}", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())