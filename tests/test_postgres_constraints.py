"""Tests for the Postgres constraints composer.

Mirrors compose_constraints_sql's mode/budget/inventory gating but reads
from scripts/sql/postgres/constraints/ and lands the result at
<run>/postgres/schema/03_create_constraints.sql.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.engine.config.config_schema import AppConfig
from src.engine.packaging.sql_scripts import compose_postgres_constraints_sql


@pytest.fixture
def run_layout(tmp_path: Path) -> Path:
    """A minimal run-folder layout: <run>/sql/ exists, <run>/postgres/ does not yet."""
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True)
    return run


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class _SalesCfg:
    """Attribute-access shim: ``_sales_mode`` uses ``getattr(sales_cfg, ...)``."""
    def __init__(self, mode: str) -> None:
        self.sales_output = mode


def _sales_cfg(mode: str) -> _SalesCfg:
    return _SalesCfg(mode)


class TestPostgresComposerOutputLocation:
    def test_writes_under_postgres_schema(self, run_layout: Path) -> None:
        compose_postgres_constraints_sql(
            sql_root=run_layout / "sql",
            sales_cfg=_sales_cfg("sales"),
            cfg=None,
        )
        out = run_layout / "postgres" / "schema" / "03_create_constraints.sql"
        assert out.is_file(), f"Expected output at {out}"

    def test_does_not_touch_sql_root(self, run_layout: Path) -> None:
        compose_postgres_constraints_sql(
            sql_root=run_layout / "sql",
            sales_cfg=_sales_cfg("sales"),
            cfg=None,
        )
        # No file should land under sql/schema/ from this call.
        assert not (run_layout / "sql" / "schema" / "03_create_constraints.sql").exists()


class TestPostgresComposerModeGating:
    def test_sales_mode_includes_only_sales(self, run_layout: Path) -> None:
        compose_postgres_constraints_sql(
            sql_root=run_layout / "sql",
            sales_cfg=_sales_cfg("sales"),
            cfg=None,
        )
        out = _read(run_layout / "postgres" / "schema" / "03_create_constraints.sql")
        assert "00_dimensions.sql" in out
        assert "10_sales.sql" in out
        assert "20_sales_order_header.sql" not in out
        assert "21_sales_order_detail.sql" not in out
        assert "22_sales_order_relations.sql" not in out

    def test_sales_order_mode_includes_normalised_parts(self, run_layout: Path) -> None:
        compose_postgres_constraints_sql(
            sql_root=run_layout / "sql",
            sales_cfg=_sales_cfg("sales_order"),
            cfg=None,
        )
        out = _read(run_layout / "postgres" / "schema" / "03_create_constraints.sql")
        assert "00_dimensions.sql" in out
        assert "10_sales.sql" not in out
        assert "20_sales_order_header.sql" in out
        assert "21_sales_order_detail.sql" in out
        assert "22_sales_order_relations.sql" in out

    def test_both_mode_includes_everything(self, run_layout: Path) -> None:
        compose_postgres_constraints_sql(
            sql_root=run_layout / "sql",
            sales_cfg=_sales_cfg("both"),
            cfg=None,
        )
        out = _read(run_layout / "postgres" / "schema" / "03_create_constraints.sql")
        for name in (
            "00_dimensions.sql",
            "10_sales.sql",
            "20_sales_order_header.sql",
            "21_sales_order_detail.sql",
            "22_sales_order_relations.sql",
        ):
            assert name in out, f"Missing {name} in 'both' mode"


class TestPostgresComposerFeatureGating:
    def _cfg(self, *, budget: bool, inventory: bool) -> AppConfig:
        return AppConfig.model_validate(
            {
                "sales": {"sales_output": "sales", "total_rows": 1000},
                "budget": {"enabled": budget},
                "inventory": {"enabled": inventory},
            }
        )

    def test_budget_off_excludes_30(self, run_layout: Path) -> None:
        compose_postgres_constraints_sql(
            sql_root=run_layout / "sql",
            sales_cfg=_sales_cfg("sales"),
            cfg=self._cfg(budget=False, inventory=False),
        )
        out = _read(run_layout / "postgres" / "schema" / "03_create_constraints.sql")
        assert "30_budget.sql" not in out
        assert "40_inventory.sql" not in out

    def test_budget_on_includes_30(self, run_layout: Path) -> None:
        compose_postgres_constraints_sql(
            sql_root=run_layout / "sql",
            sales_cfg=_sales_cfg("sales"),
            cfg=self._cfg(budget=True, inventory=False),
        )
        out = _read(run_layout / "postgres" / "schema" / "03_create_constraints.sql")
        assert "30_budget.sql" in out
        assert "40_inventory.sql" not in out

    def test_inventory_on_includes_40(self, run_layout: Path) -> None:
        compose_postgres_constraints_sql(
            sql_root=run_layout / "sql",
            sales_cfg=_sales_cfg("sales"),
            cfg=self._cfg(budget=False, inventory=True),
        )
        out = _read(run_layout / "postgres" / "schema" / "03_create_constraints.sql")
        assert "30_budget.sql" not in out
        assert "40_inventory.sql" in out

    def test_both_features_on(self, run_layout: Path) -> None:
        compose_postgres_constraints_sql(
            sql_root=run_layout / "sql",
            sales_cfg=_sales_cfg("sales"),
            cfg=self._cfg(budget=True, inventory=True),
        )
        out = _read(run_layout / "postgres" / "schema" / "03_create_constraints.sql")
        assert "30_budget.sql" in out
        assert "40_inventory.sql" in out


class TestPostgresConstraintFilesContent:
    """Sanity checks against the hand-translated files themselves."""

    def _all_files(self) -> list[Path]:
        # Resolve relative to the test file's location instead of cwd.
        repo_root = Path(__file__).resolve().parents[1]
        pg_dir = repo_root / "scripts" / "sql" / "postgres" / "constraints"
        return sorted(pg_dir.glob("*.sql"))

    def test_all_seven_files_present(self) -> None:
        names = {p.name for p in self._all_files()}
        expected = {
            "00_dimensions.sql",
            "10_sales.sql",
            "20_sales_order_header.sql",
            "21_sales_order_detail.sql",
            "22_sales_order_relations.sql",
            "30_budget.sql",
            "40_inventory.sql",
        }
        assert expected.issubset(names), f"Missing: {expected - names}"

    @staticmethod
    def _strip_comments(text: str) -> str:
        """Remove ``-- ...`` line comments and ``/* ... */`` block comments.

        Cheap one-pass approach: drop block comments first (they don't nest in
        these files), then drop line comments. Good enough for content-check
        assertions; not a full SQL parser.
        """
        import re
        text = re.sub(r"/\*[\s\S]*?\*/", "", text)
        text = re.sub(r"--[^\n]*", "", text)
        return text

    def test_no_sql_server_idioms(self) -> None:
        """No T-SQL leakage in the Postgres constraint files (excluding comments)."""
        for p in self._all_files():
            text = self._strip_comments(p.read_text(encoding="utf-8"))
            assert "[dbo]" not in text, f"{p.name}: [dbo] schema bracket leaked"
            assert "OBJECT_ID(" not in text, f"{p.name}: OBJECT_ID() leaked"
            assert "COL_LENGTH(" not in text, f"{p.name}: COL_LENGTH() leaked"
            assert "sys.key_constraints" not in text, f"{p.name}: sys.key_constraints leaked"
            assert "sys.foreign_keys" not in text, f"{p.name}: sys.foreign_keys leaked"
            assert "sys.check_constraints" not in text, f"{p.name}: sys.check_constraints leaked"
            assert "NONCLUSTERED" not in text, f"{p.name}: NONCLUSTERED leaked"
            assert "\nGO\n" not in text, f"{p.name}: GO separator leaked"
            assert "SET NOCOUNT" not in text, f"{p.name}: SET NOCOUNT leaked"
            assert "WITH CHECK" not in text, f"{p.name}: WITH CHECK leaked"

    def test_postgres_idioms_present(self) -> None:
        """Every file should use Postgres-native constructs."""
        for p in self._all_files():
            text = p.read_text(encoding="utf-8")
            # Each file must contain at least one DO $$ block.
            assert "DO $$" in text, f"{p.name}: no DO $$ block"
            assert 'to_regclass(' in text, f"{p.name}: no to_regclass()"
            assert '"public".' in text, f"{p.name}: schema not double-quoted"

    def test_constraint_names_quoted(self) -> None:
        """ADD CONSTRAINT names should be double-quoted in Postgres."""
        import re
        for p in self._all_files():
            text = p.read_text(encoding="utf-8")
            # Find every ADD CONSTRAINT <name> and assert <name> starts with ".
            for m in re.finditer(r"ADD CONSTRAINT\s+(\S+)", text):
                name = m.group(1)
                assert name.startswith('"'), (
                    f'{p.name}: constraint name {name!r} is not double-quoted'
                )

    def test_bit_check_constraints_dropped(self) -> None:
        """CHECK ((...) IN (0, 1)) on BIT->BOOLEAN columns should be gone."""
        dims = (
            Path(__file__).resolve().parents[1]
            / "scripts" / "sql" / "postgres" / "constraints" / "00_dimensions.sql"
        )
        text = dims.read_text(encoding="utf-8")
        # No IsCurrent/IsFirstPeriod/IsChurnPeriod/IsTrialPeriod CHECKs
        for col in ("IsCurrent", "IsFirstPeriod", "IsChurnPeriod", "IsTrialPeriod"):
            assert f'CK_Customers_{col}' not in text and f'CK_CustomerSubscriptions_{col}' not in text, (
                f"Bit-flag CHECK for {col} should have been dropped"
            )
