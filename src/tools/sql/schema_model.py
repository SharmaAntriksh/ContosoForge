"""Declarative constraint roster for the packaged bootstrap SQL.

This module is the single source of truth for the primary keys, unique keys,
candidate-key indexes, foreign keys, and check constraints that the packaging
layer emits for every SQL dialect. The per-dialect *rendering* lives in
``constraint_sql.py``; this module only describes *what* the constraints are,
in a form both dialects (and the identifier-case name map) can consume.

Why declarative
───────────────
The constraints were previously hand-written twice — once as T-SQL under
``scripts/sql/bootstrap/constraints/`` and once, hand-translated, as PL/pgSQL
under ``scripts/sql/postgres/constraints/``. That duplication drifted and could
not be re-cased (the identifiers were baked into the text). Modelling each
constraint once here lets a single generator emit correct, guarded, idempotent
DDL for either dialect, with every table/column identifier routed through the
shared name map so importing in ``snake`` mode Just Works.

Sections mirror the old file layout so the mode/feature gating is unchanged:

  ``DIMENSIONS``      – always composed (dimension PKs/UKs/FKs/CHECKs, plus the
                        Returns→snapshot-dimension FKs that are valid in every
                        sales_output mode).
  ``SALES``           – flat Sales + Returns; mode ∈ {sales, both}.
  ``ORDER_HEADER`` /
  ``ORDER_DETAIL`` /
  ``ORDER_RELATIONS`` – normalised order split; mode ∈ {sales_order, both}.
  ``BUDGET``          – when budget generation is enabled.
  ``INVENTORY``       – when inventory generation is enabled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "CheckExpr",
    "Flag",
    "Min",
    "Range",
    "IntSet",
    "Enum",
    "PrimaryKey",
    "Unique",
    "UniqueIndex",
    "ForeignKey",
    "Check",
    "Constraint",
    "SECTIONS",
    "select_sections",
    "Index",
    "INDEXES",
]


# ---------------------------------------------------------------------------
# CHECK-constraint semantics
#
# A CHECK is described by the column it constrains plus one of a small set of
# expression shapes. The renderer turns each shape into dialect SQL (adding the
# ``N'...'`` prefix for SQL Server string literals, etc.). ``Flag`` (a 0/1 BIT
# flag) is special: it is *dropped* on Postgres, where the column is BOOLEAN and
# already restricted to true/false.
# ---------------------------------------------------------------------------

class CheckExpr:
    """Base marker for a CHECK expression shape."""


@dataclass(frozen=True)
class Flag(CheckExpr):
    """``col IN (0, 1)`` — a BIT flag. Dropped on dialects with a native BOOLEAN."""


@dataclass(frozen=True)
class Min(CheckExpr):
    """``col >= value``."""

    value: int


@dataclass(frozen=True)
class Range(CheckExpr):
    """``col >= lo AND col < hi`` (half-open)."""

    lo: int
    hi: int


@dataclass(frozen=True)
class IntSet(CheckExpr):
    """``col IN (v1, v2, ...)`` for integer members."""

    values: tuple[int, ...]


@dataclass(frozen=True)
class Enum(CheckExpr):
    """``col IN ('a', 'b', ...)`` for string members (quoted per dialect)."""

    values: tuple[str, ...]


# ---------------------------------------------------------------------------
# Constraint kinds
#
# ``guard_columns`` lists the columns whose existence gates the constraint
# (SQL Server ``COL_LENGTH`` / Postgres ``information_schema.columns``). Core
# tables with guaranteed schemas leave it empty; config-optional columns list
# themselves so the constraint is silently skipped when the column is absent.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrimaryKey:
    name: str
    table: str
    columns: tuple[str, ...]
    guard_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unique:
    """A named UNIQUE constraint (candidate key enforced as a constraint)."""

    name: str
    table: str
    columns: tuple[str, ...]
    guard_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class UniqueIndex:
    """A candidate key enforced as a standalone ``CREATE UNIQUE INDEX``.

    ``dialects`` restricts which dialects emit it: the SQL Server side creates a
    unique index on FK-target columns even when the PK is elsewhere, while
    Postgres only needs the one on ``Currency(CurrencyCode)`` (the rest are
    already covered by the PK's implicit unique index).
    """

    name: str
    table: str
    columns: tuple[str, ...]
    guard_columns: tuple[str, ...] = ()
    dialects: frozenset[str] = frozenset({"sqlserver", "postgres"})


@dataclass(frozen=True)
class ForeignKey:
    name: str
    table: str
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]
    guard_columns: tuple[str, ...] = ()
    # SQL Server emits an extra type-compatibility EXISTS guard on these (the
    # INT/BIGINT EmployeeKey / ReturnReasonKey drift). Dropped on Postgres,
    # where a fresh generator-built DB always matches by construction.
    type_compat: bool = False


@dataclass(frozen=True)
class Check:
    name: str
    table: str
    column: str
    expr: CheckExpr
    guard_columns: tuple[str, ...] = ()

    def guards(self) -> tuple[str, ...]:
        """Columns gating this CHECK — defaults to the constrained column."""
        return self.guard_columns or (self.column,)


Constraint = PrimaryKey | Unique | UniqueIndex | ForeignKey | Check


# ---------------------------------------------------------------------------
# The roster, grouped by section. Order within a section follows the original
# hand-written files so review diffs stay legible.
# ---------------------------------------------------------------------------

_DIMENSIONS: tuple[Constraint, ...] = (
    # --- Primary keys + SCD2 checks --------------------------------------
    PrimaryKey("PK_Customers", "Customers", ("CustomerKey",)),
    Check("CK_Customers_IsCurrent", "Customers", "IsCurrent", Flag()),
    Check("CK_Customers_VersionNumber", "Customers", "VersionNumber", Min(1)),
    PrimaryKey("PK_CustomerProfile", "CustomerProfile", ("CustomerKey",)),
    PrimaryKey("PK_OrganizationProfile", "OrganizationProfile", ("CustomerKey",)),
    PrimaryKey("PK_Products", "Products", ("ProductKey",)),
    Check("CK_Products_IsCurrent", "Products", "IsCurrent", Flag()),
    Check("CK_Products_VersionNumber", "Products", "VersionNumber", Min(1)),
    PrimaryKey("PK_ProductProfile", "ProductProfile", ("ProductKey",)),
    PrimaryKey("PK_ProductSubcategory", "ProductSubcategory", ("SubcategoryKey",)),
    PrimaryKey("PK_ProductCategory", "ProductCategory", ("CategoryKey",)),
    PrimaryKey("PK_Geography", "Geography", ("GeographyKey",)),
    PrimaryKey("PK_Currency", "Currency", ("CurrencyKey",)),
    PrimaryKey("PK_Dates", "Dates", ("Date",)),
    PrimaryKey("PK_ExchangeRates", "ExchangeRates", ("Date", "FromCurrency", "ToCurrency")),
    PrimaryKey("PK_Stores", "Stores", ("StoreKey",), guard_columns=("StoreKey",)),
    PrimaryKey("PK_Warehouses", "Warehouses", ("WarehouseKey",)),
    ForeignKey(
        "FK_Warehouses_Geography", "Warehouses", ("GeographyKey",),
        "Geography", ("GeographyKey",), guard_columns=("GeographyKey",),
    ),
    PrimaryKey("PK_Promotions", "Promotions", ("PromotionKey",), guard_columns=("PromotionKey",)),
    PrimaryKey("PK_Employees", "Employees", ("EmployeeKey",), guard_columns=("EmployeeKey",)),
    PrimaryKey(
        "PK_EmployeeStoreAssignments", "EmployeeStoreAssignments", ("AssignmentKey",),
        guard_columns=("AssignmentKey",),
    ),
    ForeignKey(
        "FK_EmployeeStoreAssignments_Employees", "EmployeeStoreAssignments", ("EmployeeKey",),
        "Employees", ("EmployeeKey",), guard_columns=("EmployeeKey",),
    ),
    ForeignKey(
        "FK_EmployeeStoreAssignments_Stores", "EmployeeStoreAssignments", ("StoreKey",),
        "Stores", ("StoreKey",), guard_columns=("StoreKey",),
    ),
    PrimaryKey("PK_Suppliers", "Suppliers", ("SupplierKey",), guard_columns=("SupplierKey",)),
    PrimaryKey("PK_Channels", "Channels", ("ChannelKey",), guard_columns=("ChannelKey",)),
    PrimaryKey("PK_Time", "Time", ("TimeKey",), guard_columns=("TimeKey",)),
    PrimaryKey("PK_ReturnReason", "ReturnReason", ("ReturnReasonKey",), guard_columns=("ReturnReasonKey",)),
    # --- Candidate keys (unique indexes for FK targets) ------------------
    UniqueIndex(
        "UX_Employees_EmployeeKey", "Employees", ("EmployeeKey",),
        guard_columns=("EmployeeKey",), dialects=frozenset({"sqlserver"}),
    ),
    UniqueIndex(
        "UX_Currency_CurrencyCode", "Currency", ("CurrencyCode",),
        guard_columns=("CurrencyCode",),
    ),
    UniqueIndex(
        "UX_Channels_ChannelKey", "Channels", ("ChannelKey",),
        guard_columns=("ChannelKey",), dialects=frozenset({"sqlserver"}),
    ),
    UniqueIndex(
        "UX_Time_TimeKey", "Time", ("TimeKey",),
        guard_columns=("TimeKey",), dialects=frozenset({"sqlserver"}),
    ),
    UniqueIndex(
        "UX_ReturnReason_ReturnReasonKey", "ReturnReason", ("ReturnReasonKey",),
        guard_columns=("ReturnReasonKey",), dialects=frozenset({"sqlserver"}),
    ),
    # --- Foreign keys ----------------------------------------------------
    ForeignKey(
        "FK_Products_ProductSubcategory", "Products", ("SubcategoryKey",),
        "ProductSubcategory", ("SubcategoryKey",),
    ),
    ForeignKey(
        "FK_ProductSubcategory_ProductCategory", "ProductSubcategory", ("CategoryKey",),
        "ProductCategory", ("CategoryKey",),
    ),
    ForeignKey(
        "FK_ProductProfile_Products", "ProductProfile", ("ProductKey",),
        "Products", ("ProductKey",),
    ),
    ForeignKey(
        "FK_ProductProfile_Suppliers", "ProductProfile", ("SupplierKey",),
        "Suppliers", ("SupplierKey",), guard_columns=("SupplierKey",),
    ),
    ForeignKey(
        "FK_Customers_Geography", "Customers", ("GeographyKey",),
        "Geography", ("GeographyKey",),
    ),
    ForeignKey(
        "FK_CustomerProfile_Customers", "CustomerProfile", ("CustomerKey",),
        "Customers", ("CustomerKey",),
    ),
    ForeignKey(
        "FK_OrganizationProfile_Customers", "OrganizationProfile", ("CustomerKey",),
        "Customers", ("CustomerKey",),
    ),
    ForeignKey(
        "FK_Stores_Geography", "Stores", ("GeographyKey",),
        "Geography", ("GeographyKey",), guard_columns=("GeographyKey",),
    ),
    ForeignKey(
        "FK_Stores_Warehouses", "Stores", ("WarehouseKey",),
        "Warehouses", ("WarehouseKey",), guard_columns=("WarehouseKey",),
    ),
    ForeignKey(
        "FK_ExchangeRates_FromCurrency", "ExchangeRates", ("FromCurrencyKey",),
        "Currency", ("CurrencyKey",),
    ),
    ForeignKey(
        "FK_ExchangeRates_ToCurrency", "ExchangeRates", ("ToCurrencyKey",),
        "Currency", ("CurrencyKey",),
    ),
    ForeignKey(
        "FK_ExchangeRates_Dates", "ExchangeRates", ("Date",),
        "Dates", ("Date",),
    ),
    # --- ExchangeRatesMonthly (conditional) ------------------------------
    PrimaryKey(
        "PK_ExchangeRatesMonthly", "ExchangeRatesMonthly",
        ("Date", "FromCurrencyKey", "ToCurrencyKey"),
    ),
    ForeignKey(
        "FK_ExchangeRatesMonthly_FromCurrency", "ExchangeRatesMonthly", ("FromCurrencyKey",),
        "Currency", ("CurrencyKey",),
    ),
    ForeignKey(
        "FK_ExchangeRatesMonthly_ToCurrency", "ExchangeRatesMonthly", ("ToCurrencyKey",),
        "Currency", ("CurrencyKey",),
    ),
    # --- Loyalty tiers ---------------------------------------------------
    PrimaryKey("PK_LoyaltyTiers", "LoyaltyTiers", ("LoyaltyTierKey",), guard_columns=("LoyaltyTierKey",)),
    ForeignKey(
        "FK_Customers_LoyaltyTiers", "Customers", ("LoyaltyTierKey",),
        "LoyaltyTiers", ("LoyaltyTierKey",), guard_columns=("LoyaltyTierKey",),
    ),
    # --- Customer acquisition channels -----------------------------------
    PrimaryKey(
        "PK_CustomerAcquisitionChannels", "CustomerAcquisitionChannels",
        ("CustomerAcquisitionChannelKey",), guard_columns=("CustomerAcquisitionChannelKey",),
    ),
    ForeignKey(
        "FK_Customers_AcquisitionChannels", "Customers", ("CustomerAcquisitionChannelKey",),
        "CustomerAcquisitionChannels", ("CustomerAcquisitionChannelKey",),
        guard_columns=("CustomerAcquisitionChannelKey",),
    ),
    # --- Plans + subscriptions bridge ------------------------------------
    PrimaryKey("PK_Plans", "Plans", ("PlanKey",), guard_columns=("PlanKey",)),
    PrimaryKey(
        "PK_CustomerSubscriptions", "CustomerSubscriptions",
        ("SubscriptionKey", "BillingCycleNumber"),
        guard_columns=("SubscriptionKey", "BillingCycleNumber"),
    ),
    ForeignKey(
        "FK_CustomerSubscriptions_Customers", "CustomerSubscriptions", ("CustomerKey",),
        "Customers", ("CustomerKey",), guard_columns=("CustomerKey",),
    ),
    ForeignKey(
        "FK_CustomerSubscriptions_Plans", "CustomerSubscriptions", ("PlanKey",),
        "Plans", ("PlanKey",), guard_columns=("PlanKey",),
    ),
    Check(
        "CK_Plans_BillingCycle", "Plans", "BillingCycle",
        Enum(("Monthly", "Quarterly", "Half-Yearly", "Annual")),
    ),
    Check("CK_Plans_Discount", "Plans", "Discount", Range(0, 1)),
    Check("CK_Plans_BaseMonthlyPrice", "Plans", "BaseMonthlyPrice", Min(0)),
    Check("CK_Plans_CycleMonths", "Plans", "CycleMonths", IntSet((1, 3, 6, 12))),
    Check("CK_CustomerSubscriptions_PeriodPrice", "CustomerSubscriptions", "PeriodPrice", Min(0)),
    Check(
        "CK_CustomerSubscriptions_BillingCycleNumber", "CustomerSubscriptions",
        "BillingCycleNumber", Min(1),
    ),
    Check("CK_CustomerSubscriptions_IsFirstPeriod", "CustomerSubscriptions", "IsFirstPeriod", Flag()),
    Check("CK_CustomerSubscriptions_IsChurnPeriod", "CustomerSubscriptions", "IsChurnPeriod", Flag()),
    Check("CK_CustomerSubscriptions_IsTrialPeriod", "CustomerSubscriptions", "IsTrialPeriod", Flag()),
    # --- Customer wishlists bridge ---------------------------------------
    PrimaryKey("PK_CustomerWishlists", "CustomerWishlists", ("WishlistKey",), guard_columns=("WishlistKey",)),
    ForeignKey(
        "FK_CustomerWishlists_Customers", "CustomerWishlists", ("CustomerKey",),
        "Customers", ("CustomerKey",), guard_columns=("CustomerKey",),
    ),
    ForeignKey(
        "FK_CustomerWishlists_Products", "CustomerWishlists", ("ProductKey",),
        "Products", ("ProductKey",), guard_columns=("ProductKey",),
    ),
    Check("CK_CustomerWishlists_Quantity", "CustomerWishlists", "Quantity", Min(1)),
    Check("CK_CustomerWishlists_Priority", "CustomerWishlists", "Priority", Enum(("High", "Medium", "Low"))),
    Check("CK_CustomerWishlists_NetPrice", "CustomerWishlists", "NetPrice", Min(0)),
    # --- Complaints (optional) -------------------------------------------
    PrimaryKey("PK_Complaints", "Complaints", ("ComplaintKey",), guard_columns=("ComplaintKey",)),
    ForeignKey(
        "FK_Complaints_Customers", "Complaints", ("CustomerKey",),
        "Customers", ("CustomerKey",), guard_columns=("CustomerKey",),
    ),
    Check("CK_Complaints_Severity", "Complaints", "Severity", Enum(("Low", "Medium", "High", "Critical"))),
    Check("CK_Complaints_Status", "Complaints", "Status", Enum(("Open", "Resolved", "Escalated", "Closed"))),
    Check("CK_Complaints_Channel", "Complaints", "Channel", Enum(("Email", "Phone", "In-Store", "Website", "Chat"))),
    Check("CK_Complaints_ResponseDays", "Complaints", "ResponseDays", Min(0)),
    # --- Returns → snapshot-at-sale dimension FKs ------------------------
    # Valid in every sales_output mode (keys copied verbatim from the sold
    # line), so composed with the always-on dimensions section.
    ForeignKey(
        "FK_Returns_Customers", "Returns", ("CustomerKey",),
        "Customers", ("CustomerKey",), guard_columns=("CustomerKey",),
    ),
    ForeignKey(
        "FK_Returns_Products", "Returns", ("ProductKey",),
        "Products", ("ProductKey",), guard_columns=("ProductKey",),
    ),
    ForeignKey(
        "FK_Returns_Stores", "Returns", ("StoreKey",),
        "Stores", ("StoreKey",), guard_columns=("StoreKey",),
    ),
    ForeignKey(
        "FK_Returns_Employees", "Returns", ("EmployeeKey",),
        "Employees", ("EmployeeKey",), guard_columns=("EmployeeKey",), type_compat=True,
    ),
)

_SALES: tuple[Constraint, ...] = (
    PrimaryKey(
        "PK_Sales", "Sales", ("OrderNumber", "OrderLineNumber"),
        guard_columns=("OrderNumber", "OrderLineNumber"),
    ),
    ForeignKey("FK_Sales_Customers", "Sales", ("CustomerKey",), "Customers", ("CustomerKey",)),
    ForeignKey("FK_Sales_Products", "Sales", ("ProductKey",), "Products", ("ProductKey",)),
    ForeignKey("FK_Sales_Stores", "Sales", ("StoreKey",), "Stores", ("StoreKey",)),
    ForeignKey(
        "FK_Sales_Employees", "Sales", ("EmployeeKey",), "Employees", ("EmployeeKey",),
        guard_columns=("EmployeeKey",), type_compat=True,
    ),
    ForeignKey("FK_Sales_Promotions", "Sales", ("PromotionKey",), "Promotions", ("PromotionKey",)),
    ForeignKey("FK_Sales_Currency", "Sales", ("CurrencyKey",), "Currency", ("CurrencyKey",)),
    ForeignKey(
        "FK_Sales_Channels", "Sales", ("ChannelKey",), "Channels", ("ChannelKey",),
        guard_columns=("ChannelKey",),
    ),
    ForeignKey(
        "FK_Sales_Time", "Sales", ("TimeKey",), "Time", ("TimeKey",),
        guard_columns=("TimeKey",),
    ),
    ForeignKey("FK_Sales_Dates_OrderDate", "Sales", ("OrderDate",), "Dates", ("Date",)),
    ForeignKey("FK_Sales_Dates_DueDate", "Sales", ("DueDate",), "Dates", ("Date",)),
    ForeignKey("FK_Sales_Dates_DeliveryDate", "Sales", ("DeliveryDate",), "Dates", ("Date",)),
    Check("CK_Sales_IsOrderDelayed_01", "Sales", "IsOrderDelayed", Flag()),
    PrimaryKey(
        "PK_Returns", "Returns", ("OrderNumber", "OrderLineNumber", "ReturnSequence"),
        guard_columns=("OrderNumber", "OrderLineNumber", "ReturnSequence"),
    ),
    ForeignKey(
        "FK_Returns_Sales", "Returns", ("OrderNumber", "OrderLineNumber"),
        "Sales", ("OrderNumber", "OrderLineNumber"),
        guard_columns=("OrderNumber", "OrderLineNumber"),
    ),
    ForeignKey(
        "FK_Returns_Dates_ReturnDate", "Returns", ("ReturnDate",), "Dates", ("Date",),
        guard_columns=("ReturnDate",),
    ),
    ForeignKey(
        "FK_Returns_ReturnReason", "Returns", ("ReturnReasonKey",), "ReturnReason", ("ReturnReasonKey",),
        guard_columns=("ReturnReasonKey",), type_compat=True,
    ),
)

_ORDER_HEADER: tuple[Constraint, ...] = (
    Unique("UQ_OrderHeader_OrderNumber", "OrderHeader", ("OrderNumber",), guard_columns=("OrderNumber",)),
    ForeignKey(
        "FK_OrderHeader_Customers", "OrderHeader", ("CustomerKey",), "Customers", ("CustomerKey",),
        guard_columns=("CustomerKey",),
    ),
    ForeignKey(
        "FK_OrderHeader_Stores", "OrderHeader", ("StoreKey",), "Stores", ("StoreKey",),
        guard_columns=("StoreKey",),
    ),
    ForeignKey(
        "FK_OrderHeader_Employees_EmployeeKey", "OrderHeader", ("EmployeeKey",), "Employees", ("EmployeeKey",),
        guard_columns=("EmployeeKey",), type_compat=True,
    ),
    ForeignKey(
        "FK_OrderHeader_Dates_OrderDate", "OrderHeader", ("OrderDate",), "Dates", ("Date",),
        guard_columns=("OrderDate",),
    ),
    ForeignKey(
        "FK_OrderHeader_Time", "OrderHeader", ("TimeKey",), "Time", ("TimeKey",),
        guard_columns=("TimeKey",),
    ),
    ForeignKey(
        "FK_OrderHeader_Channels", "OrderHeader", ("ChannelKey",), "Channels", ("ChannelKey",),
        guard_columns=("ChannelKey",),
    ),
    Check("CK_OrderHeader_IsOrderDelayed_01", "OrderHeader", "IsOrderDelayed", Flag()),
)

_ORDER_DETAIL: tuple[Constraint, ...] = (
    Unique(
        "UQ_OrderDetail_OrderLine", "OrderDetail", ("OrderNumber", "OrderLineNumber"),
        guard_columns=("OrderNumber", "OrderLineNumber"),
    ),
    ForeignKey(
        "FK_OrderDetail_Products", "OrderDetail", ("ProductKey",), "Products", ("ProductKey",),
        guard_columns=("ProductKey",),
    ),
    ForeignKey(
        "FK_OrderDetail_Promotions", "OrderDetail", ("PromotionKey",), "Promotions", ("PromotionKey",),
        guard_columns=("PromotionKey",),
    ),
    ForeignKey(
        "FK_OrderDetail_Currency", "OrderDetail", ("CurrencyKey",), "Currency", ("CurrencyKey",),
        guard_columns=("CurrencyKey",),
    ),
    ForeignKey(
        "FK_OrderDetail_Dates_DueDate", "OrderDetail", ("DueDate",), "Dates", ("Date",),
        guard_columns=("DueDate",),
    ),
    ForeignKey(
        "FK_OrderDetail_Dates_DeliveryDate", "OrderDetail", ("DeliveryDate",), "Dates", ("Date",),
        guard_columns=("DeliveryDate",),
    ),
)

_ORDER_RELATIONS: tuple[Constraint, ...] = (
    ForeignKey(
        "FK_OrderDetail_OrderHeader", "OrderDetail", ("OrderNumber",), "OrderHeader", ("OrderNumber",),
        guard_columns=("OrderNumber",),
    ),
    ForeignKey(
        "FK_Returns_OrderDetail", "Returns", ("OrderNumber", "OrderLineNumber"),
        "OrderDetail", ("OrderNumber", "OrderLineNumber"),
        guard_columns=("OrderNumber", "OrderLineNumber"),
    ),
)

_BUDGET: tuple[Constraint, ...] = (
    PrimaryKey("PK_BudgetYearly", "BudgetYearly", ("Country", "Category", "BudgetYear", "Scenario")),
    PrimaryKey(
        "PK_BudgetMonthly", "BudgetMonthly",
        ("Country", "Category", "BudgetYear", "BudgetMonthStart", "Scenario"),
    ),
)

_INVENTORY: tuple[Constraint, ...] = (
    PrimaryKey(
        "PK_InventorySnapshot", "InventorySnapshot",
        ("ProductKey", "WarehouseKey", "SnapshotDate"),
    ),
    ForeignKey(
        "FK_InventorySnapshot_Products", "InventorySnapshot", ("ProductKey",), "Products", ("ProductKey",),
    ),
    ForeignKey(
        "FK_InventorySnapshot_Warehouses", "InventorySnapshot", ("WarehouseKey",), "Warehouses", ("WarehouseKey",),
    ),
)


SECTIONS: dict[str, tuple[Constraint, ...]] = {
    "dimensions": _DIMENSIONS,
    "sales": _SALES,
    "order_header": _ORDER_HEADER,
    "order_detail": _ORDER_DETAIL,
    "order_relations": _ORDER_RELATIONS,
    "budget": _BUDGET,
    "inventory": _INVENTORY,
}


def select_sections(
    mode: str, *, budget_enabled: bool, inventory_enabled: bool
) -> list[tuple[str, tuple[Constraint, ...]]]:
    """Return the ordered ``(section_name, constraints)`` list for this run.

    Mirrors ``_gated_constraint_parts`` in ``sql_scripts.py``: the flat-Sales
    section is included for ``sales``/``both`` modes, the normalised order-split
    sections for ``sales_order``/``both``, and budget/inventory by feature flag.
    """
    mode = (mode or "sales").strip().lower()
    out: list[tuple[str, tuple[Constraint, ...]]] = [("dimensions", _DIMENSIONS)]
    if mode in {"sales", "both"}:
        out.append(("sales", _SALES))
    if mode in {"sales_order", "both"}:
        out.append(("order_header", _ORDER_HEADER))
        out.append(("order_detail", _ORDER_DETAIL))
        out.append(("order_relations", _ORDER_RELATIONS))
    if budget_enabled:
        out.append(("budget", _BUDGET))
    if inventory_enabled:
        out.append(("inventory", _INVENTORY))
    return out


def iter_all_constraints() -> Iterable[Constraint]:
    """Every constraint across all sections (for validation/tests)."""
    for items in SECTIONS.values():
        yield from items


# ---------------------------------------------------------------------------
# Secondary indexes (Postgres only)
#
# SQL Server relies on the clustered columnstore index (applied separately by
# create_drop_cci.sql, which discovers tables dynamically and needs no
# identifier casing). Postgres has no columnstore equivalent, so joining a fact
# to a dimension without an index on the FK column forces a sequential scan.
# These are applied *after* the load phase (building them during COPY would
# force per-row updates); each is guarded by table (and, when config-optional,
# column) existence, and uses ``CREATE INDEX IF NOT EXISTS`` for idempotency.
#
# Names (``IX_<Table>_<Column>`` / ``BRIN_<Table>_<Column>``) are internal
# metadata and stay verbatim under the identifier-case flag, like constraint
# names; only the table/column identifiers in the DDL and guards transform.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Index:
    """A secondary index. ``kind`` is ``"btree"`` or ``"brin"``.

    ``guarded`` adds a column-existence guard (for config-optional columns);
    core columns guaranteed present whenever the table exists leave it False.
    """

    kind: str
    table: str
    column: str
    guarded: bool = False

    @property
    def name(self) -> str:
        prefix = "IX" if self.kind == "btree" else "BRIN"
        return f"{prefix}_{self.table}_{self.column}"


def _btree(table: str, column: str, *, guarded: bool = False) -> Index:
    return Index("btree", table, column, guarded)


def _brin(table: str, column: str, *, guarded: bool = False) -> Index:
    return Index("brin", table, column, guarded)


INDEXES: tuple[Index, ...] = (
    # --- dimension-to-dimension FK columns -------------------------------
    _btree("Warehouses", "GeographyKey", guarded=True),
    _btree("EmployeeStoreAssignments", "EmployeeKey", guarded=True),
    _btree("EmployeeStoreAssignments", "StoreKey", guarded=True),
    _btree("Products", "SubcategoryKey"),
    _btree("ProductSubcategory", "CategoryKey"),
    _btree("ProductProfile", "SupplierKey", guarded=True),
    _btree("Customers", "GeographyKey"),
    _btree("Customers", "LoyaltyTierKey", guarded=True),
    _btree("Customers", "CustomerAcquisitionChannelKey", guarded=True),
    _btree("Stores", "GeographyKey", guarded=True),
    _btree("Stores", "WarehouseKey", guarded=True),
    _btree("ExchangeRates", "FromCurrencyKey"),
    _btree("ExchangeRates", "ToCurrencyKey"),
    _btree("CustomerSubscriptions", "CustomerKey", guarded=True),
    _btree("CustomerSubscriptions", "PlanKey", guarded=True),
    _btree("CustomerWishlists", "CustomerKey", guarded=True),
    _btree("CustomerWishlists", "ProductKey", guarded=True),
    _btree("Complaints", "CustomerKey", guarded=True),
    # --- Sales (flat) fact FK columns ------------------------------------
    _btree("Sales", "CustomerKey"),
    _btree("Sales", "ProductKey"),
    _btree("Sales", "StoreKey"),
    _btree("Sales", "PromotionKey"),
    _btree("Sales", "CurrencyKey"),
    _btree("Sales", "OrderDate"),
    _btree("Sales", "DueDate"),
    _btree("Sales", "DeliveryDate"),
    _btree("Sales", "EmployeeKey", guarded=True),
    _btree("Sales", "ChannelKey", guarded=True),
    _btree("Sales", "TimeKey", guarded=True),
    # --- Returns fact FK columns (OrderNumber/OrderLineNumber lead PK) ----
    _btree("Returns", "ReturnDate", guarded=True),
    _btree("Returns", "ReturnReasonKey", guarded=True),
    _btree("Returns", "CustomerKey", guarded=True),
    _btree("Returns", "ProductKey", guarded=True),
    _btree("Returns", "StoreKey", guarded=True),
    _btree("Returns", "EmployeeKey", guarded=True),
    # --- OrderHeader FK columns (OrderNumber has UQ) ---------------------
    _btree("OrderHeader", "CustomerKey"),
    _btree("OrderHeader", "StoreKey"),
    _btree("OrderHeader", "OrderDate"),
    _btree("OrderHeader", "EmployeeKey", guarded=True),
    _btree("OrderHeader", "TimeKey", guarded=True),
    _btree("OrderHeader", "ChannelKey", guarded=True),
    # --- OrderDetail FK columns (OrderNumber leads UQ) -------------------
    _btree("OrderDetail", "ProductKey"),
    _btree("OrderDetail", "PromotionKey"),
    _btree("OrderDetail", "CurrencyKey"),
    _btree("OrderDetail", "DueDate"),
    _btree("OrderDetail", "DeliveryDate"),
    # --- InventorySnapshot (ProductKey leads composite PK) ---------------
    _btree("InventorySnapshot", "WarehouseKey"),
    # --- BRIN on naturally-ordered date columns --------------------------
    _brin("Sales", "OrderDate"),
    _brin("Sales", "DueDate"),
    _brin("Sales", "DeliveryDate"),
    _brin("Returns", "ReturnDate", guarded=True),
    _brin("OrderHeader", "OrderDate", guarded=True),
    _brin("OrderDetail", "DueDate", guarded=True),
    _brin("OrderDetail", "DeliveryDate", guarded=True),
    _brin("InventorySnapshot", "SnapshotDate"),
)
