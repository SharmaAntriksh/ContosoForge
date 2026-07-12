"""Generate the packaged model views (``04_create_views.sql``) per dialect.

Replaces the hand-written ``00_model_views.sql`` / ``10_budget_views.sql`` (and
their Postgres hand-translations) with a single generated source so the views
honour the chosen identifier case (``NameMap``).

Two kinds of view:

* **Pass-through** — ``vw_<Table> AS SELECT * FROM <Table>`` for every dimension
  (and the budget / inventory fact tables). Table-existence guarded so a minimal
  config that omits a table simply skips its view.
* **Fact abstraction** — ``vw_OrderHeader`` / ``vw_OrderDetail`` / ``vw_Returns``
  project an explicit, money-cast column set; ``vw_Sales`` presents one flat
  order-line grain over either the flat ``Sales`` table or an
  ``OrderHeader``⋈``OrderDetail`` join.

The old ``vw_Sales`` used runtime catalog introspection (``sys.tables`` schema
lookup + ``COL_LENGTH`` probing) to pick its shape. The generator already knows
the shape from config, so it emits a *static specialized* view instead: the
sales_output ``mode`` selects the flat-vs-join variant and ``skip_order_cols``
selects whether the order-key columns are present. A plain table-existence guard
(``OBJECT_ID`` / ``to_regclass``) remains for deployment resilience, exactly as
the pass-through views use.

Identifier casing: table/column identifiers and the view's own name route
through the run ``name_map`` (``vw_<table>`` transforms because a query-facing
view name benefits from snake_case, unlike internal constraint/index names). A
non-default ``defaults.view_schema`` places views in that schema without the
``vw_`` prefix (matching the previous composer's rewrite).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.tools.sql.sql_helpers import sql_escape_literal

if TYPE_CHECKING:
    from src.tools.sql.dialect.base import Dialect
    from src.tools.sql.name_map import NameMap


# ---------------------------------------------------------------------------
# View roster
# ---------------------------------------------------------------------------

# Pass-through ``SELECT *`` views, in emission order. Every one is table-existence
# guarded, so listing an optional table here is safe.
PASS_THROUGH_VIEWS: tuple[str, ...] = (
    "Currency", "Customers", "CustomerProfile", "OrganizationProfile",
    "CustomerAcquisitionChannels", "CustomerWishlists", "Complaints",
    "CustomerSubscriptions", "Plans", "Dates", "Time", "Employees",
    "EmployeeStoreAssignments", "ExchangeRates", "ExchangeRatesMonthly",
    "Geography", "LoyaltyTiers", "ProductCategory", "Products", "ProductProfile",
    "ProductSubcategory", "Promotions", "ReturnReason", "Channels", "Stores",
    "Warehouses", "Suppliers",
    # Budget + inventory fact tables are also plain pass-throughs.
    "BudgetYearly", "BudgetMonthly", "InventorySnapshot",
)


@dataclass(frozen=True)
class ViewCol:
    """One projected column. ``cast`` is None | "money" | "int"; ``alias`` is the
    join-source table alias (``"h"``/``"d"``) or None for single-source views."""

    column: str
    cast: str | None = None
    alias: str | None = None
    order_col: bool = False  # part of the (OrderNumber, OrderLineNumber) key prefix


@dataclass(frozen=True)
class ProjectionView:
    """A fact view with an explicit column projection over one source table."""

    view_base: str          # view is vw_<view_base>
    source_table: str
    columns: tuple[ViewCol, ...]


_ORDER_HEADER_COLS = (
    ViewCol("OrderNumber"), ViewCol("CustomerKey"), ViewCol("StoreKey"),
    ViewCol("EmployeeKey"), ViewCol("PromotionKey"), ViewCol("CurrencyKey"),
    ViewCol("ChannelKey"), ViewCol("OrderDate"), ViewCol("TimeKey"),
    ViewCol("IsOrderDelayed"),
)

_ORDER_DETAIL_COLS = (
    ViewCol("OrderNumber"), ViewCol("OrderLineNumber"), ViewCol("ProductKey"),
    ViewCol("DueDate"), ViewCol("DeliveryDate"), ViewCol("Quantity"),
    ViewCol("NetPrice", "money"), ViewCol("UnitCost", "money"),
    ViewCol("UnitPrice", "money"), ViewCol("DiscountAmount", "money"),
    ViewCol("DeliveryStatus"),
)

_RETURNS_COLS = (
    ViewCol("ReturnEventKey"), ViewCol("OrderNumber"), ViewCol("OrderLineNumber"),
    ViewCol("ReturnSequence"), ViewCol("CustomerKey"), ViewCol("ProductKey"),
    ViewCol("StoreKey"), ViewCol("EmployeeKey"), ViewCol("ReturnDate"),
    ViewCol("ReturnReasonKey"), ViewCol("ReturnQuantity"),
    ViewCol("ReturnNetPrice", "money"),
)

PROJECTION_VIEWS: tuple[ProjectionView, ...] = (
    ProjectionView("OrderHeader", "OrderHeader", _ORDER_HEADER_COLS),
    ProjectionView("OrderDetail", "OrderDetail", _ORDER_DETAIL_COLS),
    ProjectionView("Returns", "Returns", _RETURNS_COLS),
)

# vw_Sales unified column list. Join aliases: header cols "h", detail cols "d".
# The two order-key columns are conditional in the flat-Sales variant
# (skip_order_cols) but always present (from detail) in the join variant.
_SALES_COLS = (
    ViewCol("OrderNumber", None, "d", order_col=True),
    ViewCol("OrderLineNumber", None, "d", order_col=True),
    ViewCol("CustomerKey", None, "h"),
    ViewCol("ProductKey", None, "d"),
    ViewCol("StoreKey", None, "h"),
    ViewCol("EmployeeKey", None, "h"),
    ViewCol("PromotionKey", None, "h"),
    ViewCol("CurrencyKey", None, "h"),
    ViewCol("ChannelKey", "int", "h"),
    ViewCol("TimeKey", "int", "h"),
    ViewCol("OrderDate", None, "h"),
    ViewCol("DueDate", None, "d"),
    ViewCol("DeliveryDate", None, "d"),
    ViewCol("Quantity", None, "d"),
    ViewCol("NetPrice", "money", "d"),
    ViewCol("UnitCost", "money", "d"),
    ViewCol("UnitPrice", "money", "d"),
    ViewCol("DiscountAmount", "money", "d"),
    ViewCol("DeliveryStatus", None, "d"),
    ViewCol("IsOrderDelayed", None, "h"),
)


# ---------------------------------------------------------------------------
# Dialect specifics
# ---------------------------------------------------------------------------

_DIALECT = {
    "sqlserver": {
        "money": "MONEY",
        "int": "INT",
        "create": "CREATE OR ALTER VIEW",
        "default_schema": "dbo",
    },
    "postgres": {
        "money": "NUMERIC(19, 4)",
        "int": "INTEGER",
        "create": "CREATE OR REPLACE VIEW",
        "default_schema": "public",
    },
}


class _ViewWriter:
    def __init__(self, dialect: "Dialect", resolve, view_schema: str) -> None:
        self.d = dialect
        self.resolve = resolve
        self.cfg = _DIALECT[dialect.name]
        self.table_schema = self.cfg["default_schema"]
        self.view_schema = view_schema
        self.custom_schema = view_schema != self.table_schema

    # -- identifiers -------------------------------------------------------
    def q(self, ident: str) -> str:
        return self.d.quote_ident(ident)

    def table_ref(self, table: str) -> str:
        return self.d.qualify(self.table_schema, self.resolve(table))

    def view_ref(self, base: str) -> str:
        name = self.resolve(base)
        view_name = name if self.custom_schema else f"vw_{name}"
        return self.d.qualify(self.view_schema, view_name)

    def _proj(self, c: ViewCol) -> str:
        ref = (f"{c.alias}." if c.alias else "") + self.q(self.resolve(c.column))
        if c.cast == "money":
            return f"CAST({ref} AS {self.cfg['money']}) AS {self.q(self.resolve(c.column))}"
        if c.cast == "int":
            return f"CAST({ref} AS {self.cfg['int']}) AS {self.q(self.resolve(c.column))}"
        return ref

    # -- guards / block wrapping ------------------------------------------
    def _table_exists(self, table: str) -> str:
        if self.d.name == "postgres":
            return (
                f"to_regclass('{self.q(self.table_schema)}.{self.q(self.resolve(table))}')"
                " IS NOT NULL"
            )
        dotted = sql_escape_literal(f"{self.table_schema}.{self.resolve(table)}")
        return f"OBJECT_ID(N'{dotted}', N'U') IS NOT NULL"

    def schema_preamble(self) -> str | None:
        """``CREATE SCHEMA`` for a non-default view schema, else None."""
        if not self.custom_schema:
            return None
        if self.d.name == "postgres":
            return f'CREATE SCHEMA IF NOT EXISTS {self.q(self.view_schema)};'
        safe = sql_escape_literal(self.view_schema)
        return (
            f"IF SCHEMA_ID('{safe}') IS NULL\n"
            f"    EXEC('CREATE SCHEMA {self.q(self.view_schema)} AUTHORIZATION [dbo];');\nGO"
        )

    def _block(self, guard_tables: list[str], body: str) -> str:
        cond = " AND ".join(self._table_exists(t) for t in guard_tables)
        if self.d.name == "postgres":
            return (
                "DO $$\nBEGIN\n"
                f"    IF {cond} THEN\n"
                f"        EXECUTE $sql$\n{body}\n$sql$;\n"
                "    END IF;\nEND $$;"
            )
        # SQL Server: EXEC('<create view>;'), guarded, GO-terminated.
        exec_body = sql_escape_literal(body + ";")
        return f"IF {cond}\n    EXEC('{exec_body}');\nGO"

    # -- view bodies -------------------------------------------------------
    def _select(self, exprs: list[str], from_clause: str) -> str:
        cols = ",\n    ".join(exprs)
        return f"{self.cfg['create']} {self.view_ref(self._current_base)} AS\nSELECT\n    {cols}\nFROM {from_clause}"

    def pass_through(self, table: str) -> str:
        self._current_base = table
        body = (
            f"{self.cfg['create']} {self.view_ref(table)} AS "
            f"SELECT * FROM {self.table_ref(table)}"
        )
        return self._block([table], body)

    def projection(self, v: ProjectionView) -> str:
        self._current_base = v.view_base
        exprs = [self._proj(c) for c in v.columns]
        body = self._select(exprs, self.table_ref(v.source_table))
        return self._block([v.source_table], body)

    def sales_from_flat(self, *, include_order_cols: bool) -> str:
        self._current_base = "Sales"
        cols = [c for c in _SALES_COLS if include_order_cols or not c.order_col]
        exprs = [self._proj(ViewCol(c.column, c.cast, None)) for c in cols]
        body = self._select(exprs, self.table_ref("Sales"))
        return self._block(["Sales"], body)

    def sales_from_join(self) -> str:
        self._current_base = "Sales"
        exprs = [self._proj(c) for c in _SALES_COLS]  # keep h/d aliases
        d_ref = f"{self.table_ref('OrderDetail')} AS d"
        h_ref = f"{self.table_ref('OrderHeader')} AS h"
        on = f"h.{self.q(self.resolve('OrderNumber'))} = d.{self.q(self.resolve('OrderNumber'))}"
        from_clause = f"{d_ref}\nJOIN {h_ref}\n  ON {on}"
        body = self._select(exprs, from_clause)
        return self._block(["OrderHeader", "OrderDetail"], body)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_views_sql(
    dialect: "Dialect",
    *,
    mode: str,
    skip_order_cols: bool,
    view_schema: str,
    name_map: "NameMap | None" = None,
) -> str:
    """Render ``04_create_views.sql`` for ``dialect``.

    ``mode`` is the sales_output mode; ``skip_order_cols`` gates the order-key
    columns of the flat ``vw_Sales``; ``view_schema`` is the resolved target
    schema for the views (default ``dbo``/``public`` uses the ``vw_`` prefix, a
    custom schema drops it). ``name_map`` transforms identifiers (None = default).
    """
    resolve = name_map.resolve if name_map is not None else (lambda s: s)
    w = _ViewWriter(dialect, resolve, view_schema)
    mode = (mode or "sales").strip().lower()

    parts: list[str] = [
        f"-- Auto-generated by packaging: generated model views ({dialect.name})",
        f"-- view_schema: {view_schema}",
    ]

    preamble = w.schema_preamble()
    if preamble:
        parts.append(preamble)

    parts.append("-- Pass-through views")
    parts.extend(w.pass_through(t) for t in PASS_THROUGH_VIEWS)

    # Order-split projection views only when that model is emitted.
    if mode in {"sales_order", "both"}:
        parts.append("-- Order-split fact views")
        parts.extend(w.projection(v) for v in PROJECTION_VIEWS if v.view_base != "Returns")

    # vw_Returns is independent of the order-split vs flat choice.
    parts.append("-- Returns view")
    parts.append(w.projection(next(v for v in PROJECTION_VIEWS if v.view_base == "Returns")))

    parts.append("-- Unified sales grain (vw_Sales)")
    if mode == "sales_order":
        parts.append(w.sales_from_join())
    else:
        parts.append(w.sales_from_flat(include_order_cols=not skip_order_cols))

    return "\n\n".join(parts) + "\n"
