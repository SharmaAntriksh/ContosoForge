"""Tests for the declarative constraint roster + per-dialect renderer.

Covers the dialect-agnostic generator (``src/tools/sql/constraint_sql.py``) and
the roster (``schema_model.py``): roster integrity, SQL Server vs Postgres
idioms, identifier-case (snake) transformation of both DDL and guards, and
idempotency. The composer *wiring* (output paths, gating) is in
``test_postgres_constraints.py``.
"""
from __future__ import annotations

import pytest

from src.tools.sql.dialect import REGISTRY
from src.tools.sql.constraint_sql import render_constraints_sql
from src.tools.sql.identifier_case import IdentifierCase
from src.tools.sql.name_map import build_name_map
from src.tools.sql.schema_model import (
    Check,
    ForeignKey,
    PrimaryKey,
    Unique,
    UniqueIndex,
    iter_all_constraints,
)

SS = REGISTRY["sqlserver"]
PG = REGISTRY["postgres"]

_MODES = ("sales", "sales_order", "both")


def _render(dialect, *, mode="both", budget=True, inventory=True, name_map=None):
    return render_constraints_sql(
        dialect, mode=mode, budget_enabled=budget, inventory_enabled=inventory,
        name_map=name_map,
    )


# ---------------------------------------------------------------------------
# Roster integrity
# ---------------------------------------------------------------------------

class TestRosterIntegrity:
    def test_constraint_names_are_unique(self) -> None:
        names = [c.name for c in iter_all_constraints()]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"Duplicate constraint names: {sorted(dupes)}"

    def test_every_identifier_is_in_the_snake_name_map(self) -> None:
        """Snake mode only transforms identifiers the name map knows.

        If a roster table/column is missing from the map it would silently stay
        PascalCase in snake mode — a correctness gap. This asserts full coverage,
        which also keeps the constraints consistent with the CREATE TABLE / load
        scripts (they share the same map).
        """
        nm = build_name_map(IdentifierCase.SNAKE)
        missing_tables: set[str] = set()
        missing_cols: set[str] = set()
        for c in iter_all_constraints():
            if c.table not in nm.tables:
                missing_tables.add(c.table)
            if isinstance(c, ForeignKey) and c.ref_table not in nm.tables:
                missing_tables.add(c.ref_table)
            cols = set(getattr(c, "columns", ()) or ())
            cols |= set(getattr(c, "ref_columns", ()) or ())
            cols |= set(getattr(c, "guard_columns", ()) or ())
            if isinstance(c, Check):
                cols.add(c.column)
            missing_cols |= {x for x in cols if x not in nm.columns}
        assert not missing_tables, f"tables not in name map: {sorted(missing_tables)}"
        assert not missing_cols, f"columns not in name map: {sorted(missing_cols)}"

    def test_fk_references_a_keyed_target(self) -> None:
        """Every FK's (ref_table, ref_columns) is backed by a PK/UNIQUE/UX."""
        keyed: set[tuple[str, tuple[str, ...]]] = set()
        for c in iter_all_constraints():
            if isinstance(c, (PrimaryKey, Unique, UniqueIndex)):
                keyed.add((c.table, tuple(c.columns)))
        for c in iter_all_constraints():
            if isinstance(c, ForeignKey):
                key = (c.ref_table, tuple(c.ref_columns))
                assert key in keyed, f"{c.name}: FK target {key} has no PK/UQ/UX"

    def test_employee_home_store_fk_uses_explicit_nullable_column(self) -> None:
        fk = next(
            c for c in iter_all_constraints()
            if c.name == "FK_Employees_HomeStore"
        )
        assert isinstance(fk, ForeignKey)
        assert fk.table == "Employees"
        assert fk.columns == ("HomeStoreKey",)
        assert fk.ref_table == "Stores"
        assert fk.ref_columns == ("StoreKey",)


# ---------------------------------------------------------------------------
# Rendering does not crash for any config combination
# ---------------------------------------------------------------------------

class TestRenderMatrix:
    @pytest.mark.parametrize("dialect", [SS, PG])
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("budget", [True, False])
    @pytest.mark.parametrize("inventory", [True, False])
    def test_renders_nonempty_and_terminated(self, dialect, mode, budget, inventory) -> None:
        out = _render(dialect, mode=mode, budget=budget, inventory=inventory)
        assert out.strip()
        assert out.endswith("\n")
        assert f"-- mode: {mode}" in out


# ---------------------------------------------------------------------------
# SQL Server dialect specifics
# ---------------------------------------------------------------------------

class TestSqlServerIdioms:
    def test_nonclustered_and_go_and_set(self) -> None:
        out = _render(SS)
        assert "PRIMARY KEY NONCLUSTERED" in out
        assert "\nGO\n" in out
        assert "SET NOCOUNT ON;" in out
        assert "SET XACT_ABORT ON;" in out

    def test_enum_uses_n_prefix(self) -> None:
        out = _render(SS)
        assert "CHECK ([BillingCycle] IN (N'Monthly', N'Quarterly', N'Half-Yearly', N'Annual'))" in out

    def test_flag_checks_present(self) -> None:
        out = _render(SS)
        assert "CK_Customers_IsCurrent" in out
        assert "CHECK ([IsCurrent] IN (0, 1))" in out

    def test_type_compat_guard_present(self) -> None:
        out = _render(SS)
        assert "system_type_id = rc.system_type_id" in out
        assert out.count("FROM sys.columns pc") == 4  # 4 type-compat FKs

    def test_unique_index_detection_present(self) -> None:
        out = _render(SS)
        assert "CREATE UNIQUE INDEX [UX_Employees_EmployeeKey]" in out
        assert "HAVING COUNT(*) = 1" in out

    def test_range_check(self) -> None:
        out = _render(SS)
        assert "CHECK ([Discount] >= 0 AND [Discount] < 1)" in out

    def test_composite_pk(self) -> None:
        out = _render(SS)
        assert "ADD CONSTRAINT PK_ExchangeRates PRIMARY KEY NONCLUSTERED ([Date], [FromCurrency], [ToCurrency])" in out


class TestPostgresDialectDeltas:
    def test_currency_ux_only(self) -> None:
        """Postgres keeps only the Currency candidate-key index (PK covers the rest)."""
        out = _render(PG)
        assert "UX_Currency_CurrencyCode" in out
        for name in ("UX_Employees_EmployeeKey", "UX_Channels_ChannelKey",
                     "UX_Time_TimeKey", "UX_ReturnReason_ReturnReasonKey"):
            assert name not in out, f"{name} should be SQL-Server-only"

    def test_enum_plain_quotes(self) -> None:
        # Postgres string literals have no N'' prefix.
        out = _render(PG, name_map=None)
        assert """CHECK ("BillingCycle" IN ('Monthly', 'Quarterly', 'Half-Yearly', 'Annual'))""" in out
        assert "N'Monthly'" not in out


# ---------------------------------------------------------------------------
# Identifier-case (snake) behaviour
# ---------------------------------------------------------------------------

class TestSnakeCase:
    def _nm(self):
        return build_name_map(IdentifierCase.SNAKE)

    def test_sqlserver_snake_lowercases_ddl_and_guards(self) -> None:
        out = _render(SS, name_map=self._nm())
        # DDL identifiers
        assert "ALTER TABLE [dbo].[customers]" in out
        assert "ADD CONSTRAINT PK_Customers PRIMARY KEY NONCLUSTERED ([customer_key])" in out
        # Guard string literals are transformed too.
        assert "OBJECT_ID(N'dbo.customers', N'U')" in out
        assert "COL_LENGTH(N'dbo.stores', N'store_key')" in out
        # No PascalCase identifiers leak into the DDL brackets.
        assert "[CustomerKey]" not in out
        assert "[Customers]" not in out

    def test_postgres_snake_lowercases_ddl_and_guards(self) -> None:
        out = _render(PG, name_map=self._nm())
        assert 'ALTER TABLE "public"."customers"' in out
        assert 'to_regclass(\'"public"."customers"\')' in out
        assert "table_name='customers'" in out
        assert "column_name='customer_key'" in out
        assert '"CustomerKey"' not in out

    def test_constraint_names_stay_verbatim_in_snake(self) -> None:
        """Constraint names are internal metadata; not transformed by the flag."""
        out = _render(PG, name_map=self._nm())
        assert '"PK_Customers"' in out
        assert '"pk_customers"' not in out

    def test_pascal_default_keeps_pascalcase(self) -> None:
        out = _render(SS, name_map=None)
        assert "[CustomerKey]" in out
        assert "[customer_key]" not in out


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_every_sqlserver_block_is_guarded(self) -> None:
        out = _render(SS)
        # Every ALTER TABLE / CREATE UNIQUE INDEX sits inside an IF ... BEGIN block.
        assert out.count("BEGIN\n") == out.count("\nEND;")
        assert "ADD CONSTRAINT" in out and "IF OBJECT_ID(" in out

    def test_every_postgres_block_is_guarded(self) -> None:
        out = _render(PG)
        assert out.count("DO $$") == out.count("END $$;")
        assert out.count("DO $$") == out.count("    IF ")
