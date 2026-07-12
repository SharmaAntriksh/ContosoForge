"""End-to-end identifier-case consistency across every generated SQL artifact.

Phases 4a-4d made constraints, indexes, and views generated from one roster
through the shared name map. This asserts the whole bundle agrees in snake mode:
no table/column identifier ever leaks as a quoted/bracketed PascalCase name, and
the snake forms are actually present. Constraint/index names stay PascalCase by
design (internal metadata) and are excluded from the leak check by construction
(they carry PK_/FK_/CK_/UX_/IX_/BRIN_ prefixes, so a bare ``[Customers]`` /
``"Customers"`` never matches them).
"""
from __future__ import annotations

import pytest

from src.tools.sql.constraint_sql import render_constraints_sql, render_indexes_sql
from src.tools.sql.dialect import REGISTRY
from src.tools.sql.identifier_case import IdentifierCase
from src.tools.sql.name_map import build_name_map
from src.tools.sql.view_sql import render_views_sql

SS = REGISTRY["sqlserver"]
PG = REGISTRY["postgres"]

# (PascalCase, snake_case) identifier samples spanning tables and columns.
_SAMPLES = [
    ("Customers", "customers"),
    ("CustomerKey", "customer_key"),
    ("OrderHeader", "order_header"),
    ("ProductKey", "product_key"),
    ("IsOrderDelayed", "is_order_delayed"),
    ("BillingCycle", "billing_cycle"),
    ("ExchangeRatesMonthly", "exchange_rates_monthly"),
    ("SnapshotDate", "snapshot_date"),
]


def _all_snake_sql() -> str:
    nm = build_name_map(IdentifierCase.SNAKE)
    kw = dict(mode="both", budget_enabled=True, inventory_enabled=True, name_map=nm)
    chunks = [
        render_constraints_sql(SS, **kw),
        render_constraints_sql(PG, **kw),
        render_indexes_sql(PG, name_map=nm),
        render_views_sql(SS, mode="both", skip_order_cols=False, view_schema="dbo", name_map=nm),
        render_views_sql(PG, mode="both", skip_order_cols=False, view_schema="public", name_map=nm),
    ]
    return "\n".join(chunks)


@pytest.mark.parametrize("pascal,snake", _SAMPLES)
def test_no_pascal_identifier_leaks_in_snake(pascal: str, snake: str) -> None:
    sql = _all_snake_sql()
    # As a bracketed (SQL Server) or double-quoted (Postgres) identifier, the
    # PascalCase form must never appear — everything routed through the map.
    assert f"[{pascal}]" not in sql, f"[{pascal}] leaked un-transformed"
    assert f'"{pascal}"' not in sql, f'"{pascal}" leaked un-transformed'


@pytest.mark.parametrize("pascal,snake", _SAMPLES)
def test_snake_identifier_present(pascal: str, snake: str) -> None:
    sql = _all_snake_sql()
    assert (f"[{snake}]" in sql) or (f'"{snake}"' in sql), f"{snake} not emitted"


def test_pascal_default_keeps_pascalcase_everywhere() -> None:
    """The default case is byte-identical PascalCase: no snake leakage."""
    kw = dict(mode="both", budget_enabled=True, inventory_enabled=True, name_map=None)
    sql = "\n".join([
        render_constraints_sql(SS, **kw),
        render_constraints_sql(PG, **kw),
        render_indexes_sql(PG, name_map=None),
        render_views_sql(SS, mode="both", skip_order_cols=False, view_schema="dbo", name_map=None),
        render_views_sql(PG, mode="both", skip_order_cols=False, view_schema="public", name_map=None),
    ])
    assert "[CustomerKey]" in sql or '"CustomerKey"' in sql
    assert "[customer_key]" not in sql and '"customer_key"' not in sql


def test_view_source_tables_match_constraint_tables_in_snake() -> None:
    """A cross-check: the snaked table name a view selects from is the same
    name the constraints reference (both come from the shared map)."""
    nm = build_name_map(IdentifierCase.SNAKE)
    cons = render_constraints_sql(
        SS, mode="both", budget_enabled=True, inventory_enabled=True, name_map=nm
    )
    views = render_views_sql(
        SS, mode="both", skip_order_cols=False, view_schema="dbo", name_map=nm
    )
    # e.g. both reference [dbo].[order_header]
    assert "[dbo].[order_header]" in cons
    assert "[dbo].[order_header]" in views
