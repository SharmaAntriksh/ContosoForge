"""Tests for the model-view generator (``src/tools/sql/view_sql.py``).

Covers both dialects, the pass-through vs fact-view split, the ``vw_Sales``
flat-vs-join specialization, order-col gating, money/int casts, custom view
schema, and identifier-case (snake) transformation.
"""
from __future__ import annotations

from src.tools.sql.dialect import REGISTRY
from src.tools.sql.identifier_case import IdentifierCase
from src.tools.sql.name_map import build_name_map
from src.tools.sql.view_sql import (
    PASS_THROUGH_VIEWS,
    PUBLISHED_DIMENSION_VIEWS,
    PROJECTION_VIEWS,
    WORKFORCE_VIEWS,
    render_views_sql,
)

SS = REGISTRY["sqlserver"]
PG = REGISTRY["postgres"]


def _r(dialect, *, mode="both", skip_order_cols=False, view_schema=None, name_map=None):
    if view_schema is None:
        view_schema = "dbo" if dialect.name == "sqlserver" else "public"
    return render_views_sql(
        dialect, mode=mode, skip_order_cols=skip_order_cols,
        view_schema=view_schema, name_map=name_map,
    )


class TestRosterCoverage:
    def test_pass_through_and_projection_identifiers_in_name_map(self) -> None:
        nm = build_name_map(IdentifierCase.SNAKE)
        for t in PASS_THROUGH_VIEWS:
            assert t in nm.tables, f"{t} not in name map"
        for v in PROJECTION_VIEWS:
            assert v.source_table in nm.tables
            for c in v.columns:
                assert c.column in nm.columns, f"{c.column} not in name map"
        for view in PUBLISHED_DIMENSION_VIEWS:
            assert view.source_table in nm.tables
            for column in view.columns:
                assert column.column in nm.columns, f"{column.column} not in name map"


class TestPassThrough:
    def test_sqlserver_pass_through_shape(self) -> None:
        out = _r(SS, mode="sales")
        assert "IF OBJECT_ID(N'dbo.Currency', N'U') IS NOT NULL" in out
        assert "EXEC('CREATE OR ALTER VIEW [dbo].[vw_Currency] AS SELECT * FROM [dbo].[Currency];');" in out

    def test_postgres_pass_through_shape(self) -> None:
        out = _r(PG, mode="sales")
        assert "to_regclass('\"public\".\"currency\"')" not in out  # pascal, not snake
        assert 'CREATE OR REPLACE VIEW "public"."vw_Currency" AS SELECT * FROM "public"."Currency"' in out


class TestFactViews:
    def test_both_mode_emits_order_split_views(self) -> None:
        out = _r(SS, mode="both")
        for v in ("vw_OrderHeader", "vw_OrderDetail", "vw_Returns", "vw_Sales", "vw_InventorySnapshot"):
            assert v in out, f"missing {v}"

    def test_sales_mode_omits_order_split_views(self) -> None:
        out = _r(SS, mode="sales")
        assert "vw_OrderHeader" not in out
        assert "vw_OrderDetail" not in out
        # ...but vw_Returns and vw_Sales are still present.
        assert "vw_Returns" in out and "vw_Sales" in out

    def test_money_cast_per_dialect(self) -> None:
        assert "CAST([NetPrice] AS MONEY) AS [NetPrice]" in _r(SS)
        assert 'CAST("NetPrice" AS NUMERIC(19, 4)) AS "NetPrice"' in _r(PG)

    def test_channelkey_int_cast(self) -> None:
        assert "CAST([ChannelKey] AS INT) AS [ChannelKey]" in _r(SS, mode="sales")
        assert 'CAST("ChannelKey" AS INTEGER) AS "ChannelKey"' in _r(PG, mode="sales")


class TestWorkforceViews:
    def test_workforce_view_roster(self) -> None:
        assert WORKFORCE_VIEWS == (
            "EmployeeAssignmentHistory",
            "CurrentEmployeesByStore",
            "EffectiveSalespersonAssignment",
            "CurrentStoreManager",
            "StoreStaffingActualVsPlan",
        )

    def test_sqlserver_workforce_views_are_guarded_and_filtered(self) -> None:
        out = _r(SS, mode="sales")

        for view in WORKFORCE_VIEWS:
            assert f"[dbo].[vw_{view}]" in out
        assert "JOIN [dbo].[Employees] AS e" in out
        assert "JOIN [dbo].[Stores] AS s" in out
        assert "a.[IsCurrentAssignment] = 1" in out
        assert "e.[IsSalesperson] = 1" in out

    def test_store_staffing_view_compares_current_assignments_to_plan(self) -> None:
        out = _r(SS, mode="sales")

        assert "[dbo].[vw_StoreStaffingActualVsPlan]" in out
        assert "LEFT JOIN [dbo].[EmployeeStoreAssignments] AS a" in out
        assert "s.[EmployeeCount] AS [PlannedEmployeeCount]" in out
        assert "COUNT(a.[EmployeeKey]) AS [CurrentEmployeeCount]" in out
        assert "COALESCE(SUM(a.[FTE]), 0.0) AS [CurrentFTE]" in out
        assert "COUNT(a.[EmployeeKey]) - s.[EmployeeCount] AS [HeadcountVariance]" in out

    def test_current_manager_is_an_employee_relationship(self) -> None:
        out = _r(SS, mode="sales")

        assert "[dbo].[vw_CurrentStoreManager]" in out
        assert "a.[EmployeeKey] AS [ManagerEmployeeKey]" in out
        assert "a.[RoleAtStore] = N''Store Manager''" in out
        assert "a.[IsCurrentAssignment] = 1" in out
        assert "a.[AssignmentStatus] = N''Active''" in out

    def test_postgres_workforce_views_use_boolean_literals(self) -> None:
        out = _r(PG, mode="sales")

        assert '"public"."vw_CurrentEmployeesByStore"' in out
        assert 'a."IsCurrentAssignment" = TRUE' in out
        assert 'e."IsSalesperson" = TRUE' in out
        assert 'a."AssignmentStatus" = \'Active\'' in out

    def test_workforce_views_honor_snake_identifier_case(self) -> None:
        out = _r(PG, mode="sales", name_map=build_name_map(IdentifierCase.SNAKE))

        assert '"public"."vw_current_employees_by_store"' in out
        assert '"public"."vw_store_staffing_actual_vs_plan"' in out
        assert '"public"."employee_store_assignments" AS a' in out
        assert 'a."employee_key" = e."employee_key"' in out
        assert 's."employee_count" AS "planned_employee_count"' in out
        assert 'a."employee_key" AS "manager_employee_key"' in out


class TestVwSalesSpecialization:
    def test_flat_variant_from_sales(self) -> None:
        out = _r(PG, mode="sales")
        assert 'FROM "public"."Sales"' in out
        unified_sales = out.split('"public"."vw_Sales"', 1)[1].split(
            "-- Fact-derived store actuals",
            1,
        )[0]
        assert "JOIN" not in unified_sales

    def test_join_variant_in_sales_order(self) -> None:
        out = _r(PG, mode="sales_order")
        tail = out.split('"public"."vw_Sales"')[-1]
        assert 'FROM "public"."OrderDetail" AS d' in tail
        assert 'JOIN "public"."OrderHeader" AS h' in tail

    def test_order_cols_gated_by_skip_order_cols(self) -> None:
        with_cols = _r(PG, mode="sales", skip_order_cols=False)
        without = _r(PG, mode="sales", skip_order_cols=True)

        def sales_block(sql):
            return sql[sql.rfind('"public"."vw_Sales"'):].split(
                "-- Fact-derived store actuals",
                1,
            )[0]

        assert '"OrderNumber"' in sales_block(with_cols)
        assert '"OrderNumber"' not in sales_block(without)

    def test_join_variant_always_has_order_cols(self) -> None:
        # skip_order_cols is irrelevant to the join model (cols come from detail).
        out = _r(PG, mode="sales_order", skip_order_cols=True)
        tail = out.split('"public"."vw_Sales"')[-1]
        assert 'd."OrderNumber"' in tail


class TestStoreSalesActuals:
    def test_flat_sales_derives_actual_order_value(self) -> None:
        out = _r(SS, mode="sales", skip_order_cols=False)

        assert "[dbo].[vw_StoreSalesActuals]" in out
        assert "FROM [dbo].[Sales] AS f" in out
        assert "SUM(f.[NetPrice] * f.[Quantity]) AS [TransactionAmount]" in out
        assert "AVG(o.[TransactionAmount]) AS [AvgTransactionValue]" in out

    def test_flat_sales_without_order_keys_omits_transaction_view(self) -> None:
        out = _r(SS, mode="sales", skip_order_cols=True)
        assert "vw_StoreSalesActuals" not in out

    def test_order_split_uses_header_and_detail(self) -> None:
        out = _r(PG, mode="sales_order", skip_order_cols=True)

        assert '"public"."vw_StoreSalesActuals"' in out
        assert 'FROM "public"."OrderHeader" AS h' in out
        assert 'JOIN "public"."OrderDetail" AS d' in out

    def test_actual_view_honors_snake_identifier_case(self) -> None:
        out = _r(
            PG,
            mode="sales",
            skip_order_cols=False,
            name_map=build_name_map(IdentifierCase.SNAKE),
        )

        assert '"public"."vw_store_sales_actuals"' in out
        assert 'AVG(o."transaction_amount") AS "avg_transaction_value"' in out


class TestCustomSchema:
    def test_sqlserver_custom_schema(self) -> None:
        out = _r(SS, mode="sales", view_schema="bi")
        assert "SCHEMA_ID('bi')" in out
        assert "[bi].[Sales]" in out       # view relocated, no vw_ prefix
        assert "[dbo].[Sales]" in out       # source table still dbo
        assert "[dbo].[vw_" not in out

    def test_postgres_custom_schema(self) -> None:
        out = _r(PG, mode="sales", view_schema="bi")
        assert 'CREATE SCHEMA IF NOT EXISTS "bi";' in out
        assert '"bi"."Sales"' in out
        assert '"public"."Sales"' in out
        assert '"public"."vw_' not in out


class TestSnakeCase:
    def _nm(self):
        return build_name_map(IdentifierCase.SNAKE)

    def test_sqlserver_snake(self) -> None:
        out = _r(SS, mode="sales", name_map=self._nm())
        # View name transforms (query-facing), guards + refs transform.
        assert "[dbo].[vw_customers]" in out
        assert "OBJECT_ID(N'dbo.customers', N'U')" in out
        assert "[dbo].[Customers]" not in out

    def test_postgres_snake(self) -> None:
        out = _r(PG, mode="sales", name_map=self._nm())
        assert '"public"."vw_customers"' in out
        assert "to_regclass('\"public\".\"customers\"')" in out
        assert '"public"."Customers"' not in out

    def test_pascal_default_unchanged(self) -> None:
        out = _r(PG, mode="sales", name_map=None)
        assert '"public"."vw_Customers"' in out


class TestIdempotencyGuards:
    def test_postgres_blocks_balanced(self) -> None:
        out = _r(PG, mode="both")
        assert out.count("DO $$") == out.count("END $$;")

    def test_sqlserver_every_view_guarded(self) -> None:
        out = _r(SS, mode="both")
        # Every EXEC create-view is preceded by an IF OBJECT_ID guard.
        assert out.count("EXEC('CREATE OR ALTER VIEW") == out.count("IF OBJECT_ID(")
