"""Tests for the Postgres constraints composer.

Constraints are now *generated* from the declarative roster in
``src/tools/sql/schema_model.py`` (rendered by ``constraint_sql.py``) rather than
composed from hand-written .sql files. These tests pin the composer wiring:
output location, mode/feature gating (via representative constraints, since
there are no longer per-file banners), and Postgres-native idioms. The
dialect-agnostic rendering and identifier-case behaviour live in
``test_constraint_generation.py``.
"""
from __future__ import annotations

from pathlib import Path

from src.engine.config.config_schema import AppConfig
from src.engine.packaging.sql_scripts import compose_postgres_constraints_sql


def _run_layout(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True, exist_ok=True)
    return run


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class _SalesCfg:
    """Attribute-access shim: ``_sales_mode`` uses ``getattr(sales_cfg, ...)``."""

    def __init__(self, mode: str) -> None:
        self.sales_output = mode


def _compose(run: Path, *, mode: str, cfg=None) -> str:
    compose_postgres_constraints_sql(
        sql_root=run / "sql", sales_cfg=_SalesCfg(mode), cfg=cfg
    )
    return _read(run / "sql" / "postgres" / "schema" / "03_create_constraints.sql")


class TestPostgresComposerOutputLocation:
    def test_writes_under_postgres_schema(self, tmp_path: Path) -> None:
        run = _run_layout(tmp_path)
        _compose(run, mode="sales")
        assert (run / "sql" / "postgres" / "schema" / "03_create_constraints.sql").is_file()

    def test_does_not_touch_sqlserver_folder(self, tmp_path: Path) -> None:
        run = _run_layout(tmp_path)
        _compose(run, mode="sales")
        assert not (run / "sql" / "sqlserver" / "schema" / "03_create_constraints.sql").exists()


class TestPostgresComposerModeGating:
    def test_sales_mode_includes_flat_sales_not_order_split(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="sales")
        assert "PK_Sales" in out  # flat sales section
        assert "FK_OrderDetail_OrderHeader" not in out  # order-split relations

    def test_sales_order_mode_includes_order_split_not_flat(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="sales_order")
        assert "UQ_OrderHeader_OrderNumber" in out
        assert "UQ_OrderDetail_OrderLine" in out
        assert "FK_OrderDetail_OrderHeader" in out
        assert "PK_Sales" not in out

    def test_both_mode_includes_everything(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="both")
        for name in ("PK_Sales", "UQ_OrderHeader_OrderNumber", "FK_OrderDetail_OrderHeader"):
            assert name in out, f"Missing {name} in 'both' mode"

    def test_dimensions_always_present(self, tmp_path: Path) -> None:
        for mode in ("sales", "sales_order", "both"):
            out = _compose(_run_layout(tmp_path), mode=mode)
            assert "PK_Customers" in out
            # Returns→snapshot dimension FKs live in the always-on section.
            assert "FK_Returns_Customers" in out


class TestPostgresComposerFeatureGating:
    def _cfg(self, *, budget: bool, inventory: bool) -> AppConfig:
        return AppConfig.model_validate(
            {
                "sales": {"sales_output": "sales", "total_rows": 1000},
                "budget": {"enabled": budget},
                "inventory": {"enabled": inventory},
            }
        )

    def test_budget_off_inventory_off(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="sales", cfg=self._cfg(budget=False, inventory=False))
        assert "PK_BudgetYearly" not in out
        assert "PK_InventorySnapshot" not in out

    def test_budget_on(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="sales", cfg=self._cfg(budget=True, inventory=False))
        assert "PK_BudgetYearly" in out and "PK_BudgetMonthly" in out
        assert "PK_InventorySnapshot" not in out

    def test_inventory_on(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="sales", cfg=self._cfg(budget=False, inventory=True))
        assert "PK_InventorySnapshot" in out
        assert "PK_BudgetYearly" not in out

    def test_both_features_on(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="sales", cfg=self._cfg(budget=True, inventory=True))
        assert "PK_BudgetYearly" in out and "PK_InventorySnapshot" in out


class TestPostgresIdioms:
    def test_no_sql_server_idioms(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="both")
        for bad in ("[dbo]", "OBJECT_ID(", "COL_LENGTH(", "sys.key_constraints",
                    "sys.foreign_keys", "sys.check_constraints", "NONCLUSTERED",
                    "\nGO\n", "SET NOCOUNT", "WITH CHECK"):
            assert bad not in out, f"T-SQL idiom leaked into Postgres output: {bad!r}"

    def test_postgres_idioms_present(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="both")
        assert "DO $$" in out
        assert "to_regclass(" in out
        assert '"public".' in out

    def test_constraint_names_double_quoted(self, tmp_path: Path) -> None:
        import re
        out = _compose(_run_layout(tmp_path), mode="both")
        for m in re.finditer(r"ADD CONSTRAINT\s+(\S+)", out):
            assert m.group(1).startswith('"'), f"constraint name not quoted: {m.group(1)!r}"

    def test_bit_flag_checks_dropped(self, tmp_path: Path) -> None:
        out = _compose(_run_layout(tmp_path), mode="both")
        for name in ("CK_Customers_IsCurrent", "CK_Products_IsCurrent",
                     "CK_Sales_IsOrderDelayed_01", "CK_CustomerSubscriptions_IsTrialPeriod"):
            assert name not in out, f"BIT-flag CHECK should be dropped on Postgres: {name}"

    def test_type_compat_guard_dropped(self, tmp_path: Path) -> None:
        # The SQL-Server-only type-compatibility EXISTS block must not appear.
        out = _compose(_run_layout(tmp_path), mode="both")
        assert "system_type_id" not in out
        # ...but the FK itself is still emitted.
        assert "FK_Sales_Employees" in out
