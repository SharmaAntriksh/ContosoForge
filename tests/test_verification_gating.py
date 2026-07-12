"""The verification diagnostics are PascalCase-only and skipped for snake mode."""
from __future__ import annotations

from pathlib import Path

from src.engine.packaging.sql_scripts import compose_verification_sql
from src.tools.sql.identifier_case import IdentifierCase
from src.tools.sql.name_map import build_name_map


def _out(run: Path) -> Path:
    return run / "sql" / "sqlserver" / "schema" / "05_create_verify_schema.sql"


def test_pascal_default_composes_verification(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True)
    compose_verification_sql(sql_root=run / "sql", name_map=None)
    assert _out(run).is_file()


def test_snake_skips_verification(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True)
    compose_verification_sql(
        sql_root=run / "sql", name_map=build_name_map(IdentifierCase.SNAKE)
    )
    assert not _out(run).exists()
