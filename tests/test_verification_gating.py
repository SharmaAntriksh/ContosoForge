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


def test_employee_verification_uses_home_store_key(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True)
    compose_verification_sql(sql_root=run / "sql", name_map=None)

    sql = _out(run).read_text(encoding="utf-8")
    assert "e.HomeStoreKey = s.StoreKey" in sql
    assert "e.IsActive = 1 AND e.HomeStoreKey > 0" in sql
    assert "JOIN dbo.Employees e ON e.StoreKey" not in sql
    assert "e.IsActive = 1 AND e.StoreKey > 0" not in sql
    assert "SELECT StoreKey, COUNT(*) AS Cnt FROM dbo.Employees" not in sql


def test_employee_lifecycle_verification_uses_assignment_history(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True)
    compose_verification_sql(sql_root=run / "sql", name_map=None)

    sql = _out(run).read_text(encoding="utf-8")
    assert "esa.IsCurrentAssignment = 1" in sql
    assert "esa.AssignmentStatus = 'Active'" in sql
    assert "MAX(AssignmentSequence) AS MaxSequence" in sql
    assert "ExpectedHomeRenovation" in sql
    assert "malformed return:" in sql
    assert "SUM(CASE WHEN NOT EXISTS" not in sql
    assert "LEFT JOIN (\n                SELECT DISTINCT EmployeeKey, StartDate" in sql
    assert "@bad_perm_ip" not in sql
    assert "@unexpected_return" not in sql
    assert "JOIN dbo.Employees e ON e.HomeStoreKey" not in sql


def test_snake_skips_verification(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True)
    compose_verification_sql(
        sql_root=run / "sql", name_map=build_name_map(IdentifierCase.SNAKE)
    )
    assert not _out(run).exists()
