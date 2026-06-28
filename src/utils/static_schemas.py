# src/utils/static_schemas.py

from __future__ import annotations

from types import MappingProxyType
from typing import Dict, List, Mapping, Sequence, Tuple
import re

from src.tools.sql.dialect import ColumnSpec, SqlType

# Type aliases (lightweight, no runtime overhead)
SchemaCol = Tuple[str, ColumnSpec]
Schema = Tuple[SchemaCol, ...]


# ============================================================================
# Dialect-neutral type helpers. Concrete SQL is rendered at emit time by a
# Dialect (default: SqlServerDialect).
# ============================================================================
def INT(*, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.INT, nullable=not not_null)


def BIGINT(*, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.BIGINT, nullable=not not_null)


def SMALLINT(*, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.SMALLINT, nullable=not not_null)


def TINYINT(*, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.TINYINT, nullable=not not_null)


def BIT(*, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.BIT, nullable=not not_null)


def FLOAT(*, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.FLOAT, nullable=not not_null)


def DATE(*, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.DATE, nullable=not not_null)


def DATETIME(*, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.DATETIME, nullable=not not_null)


def DATETIME2(p: int = 7, *, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.DATETIME2, nullable=not not_null, args=(p,))


def TIME(p: int = 0, *, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.TIME, nullable=not not_null, args=(p,))


def VARCHAR(n: int | str, *, not_null: bool = False) -> ColumnSpec:
    return ColumnSpec(SqlType.VARCHAR, nullable=not not_null, args=(n,))


def CHAR(n: int, *, not_null: bool = False) -> ColumnSpec:
    return ColumnSpec(SqlType.CHAR, nullable=not not_null, args=(n,))


def DECIMAL(p: int, s: int, *, not_null: bool = True) -> ColumnSpec:
    return ColumnSpec(SqlType.DECIMAL, nullable=not not_null, args=(p, s))


# Common aliases (readability)
INT_NN = INT(not_null=True)
INT_NULL = INT(not_null=False)
BIGINT_NN = BIGINT(not_null=True)
BIGINT_NULL = BIGINT(not_null=False)
SMALLINT_NN = SMALLINT(not_null=True)
TINYINT_NN = TINYINT(not_null=True)
DATE_NN = DATE(not_null=True)
DATE_NULL = DATE(not_null=False)


# ============================================================================
# Schema derivation + validation
# ============================================================================
def _derive_schema_from_base(base: Schema, cols: Sequence[str], *, name: str) -> Schema:
    """
    Build a schema by selecting columns from a base schema, preserving the provided column order.

    This keeps datatypes consistent across derived tables (e.g., OrderHeader/OrderDetail)
    while making the column list explicit and easy to review.
    """
    base_map = {c: t for c, t in base}
    missing = [c for c in cols if c not in base_map]
    if missing:
        raise KeyError(
            f"{name}: missing columns not found in base schema: {missing}. "
            f"Update {name} column list or base schema."
        )
    return tuple((c, base_map[c]) for c in cols)


def _validate_schema(name: str, schema: Schema) -> None:
    if not isinstance(schema, tuple) or len(schema) == 0:
        raise ValueError(f"{name}: schema must be a non-empty tuple of (col, type) pairs")

    seen = set()
    for item in schema:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], ColumnSpec)
        ):
            raise TypeError(
                f"{name}: invalid schema entry {item!r} (expected (str, ColumnSpec))"
            )

        col = item[0]
        if not col.strip():
            raise ValueError(f"{name}: empty column name")
        if col in seen:
            raise ValueError(f"{name}: duplicate column {col!r}")
        seen.add(col)


def _validate_schema_map(schema_map: Mapping[str, Schema]) -> None:
    for table, schema in schema_map.items():
        if not isinstance(table, str) or not table.strip():
            raise ValueError(f"Invalid table key: {table!r}")
        _validate_schema(table, schema)


# ============================================================================
# FACT BASE SCHEMAS (single source of truth for derivations)
# ============================================================================
_SALES_SCHEMA: Schema = (
    ("OrderNumber", INT_NN),
    ("OrderLineNumber", INT_NN),
    ("CustomerKey", INT_NN),
    ("ProductKey", INT_NN),
    ("StoreKey", INT_NN),
    ("EmployeeKey", INT_NN),
    ("PromotionKey", INT_NN),
    ("CurrencyKey", INT_NN),
    ("ChannelKey", SMALLINT_NN),
    ("TimeKey", SMALLINT_NN),
    ("OrderDate", DATE_NN),
    ("DueDate", DATE_NN),
    ("DeliveryDate", DATE_NN),
    ("Quantity", INT_NN),
    ("UnitPrice", DECIMAL(8, 2, not_null=True)),
    ("NetPrice", DECIMAL(8, 2, not_null=True)),
    ("UnitCost", DECIMAL(8, 2, not_null=True)),
    ("DiscountAmount", DECIMAL(8, 2, not_null=True)),
    ("DeliveryStatus", VARCHAR(20, not_null=True)),
    ("IsOrderDelayed", BIT(not_null=True)),
)


_SALES_SCHEMA_NO_ORDER: Schema = tuple(
    (name, dtype) for (name, dtype) in _SALES_SCHEMA
    if name not in ("OrderNumber", "OrderLineNumber")
)

# Derived fact tables (PascalCase table names for SQL)
_SALES_ORDER_HEADER_COLS: Tuple[str, ...] = (
    "OrderNumber",
    "CustomerKey",
    "StoreKey",
    "EmployeeKey",
    "PromotionKey",
    "CurrencyKey",
    "ChannelKey",
    "OrderDate",
    "TimeKey",
    "IsOrderDelayed",
)

_SALES_ORDER_DETAIL_COLS: Tuple[str, ...] = (
    "OrderNumber",
    "OrderLineNumber",
    "ProductKey",
    "DueDate",
    "DeliveryDate",
    "Quantity",
    "UnitPrice",
    "NetPrice",
    "UnitCost",
    "DiscountAmount",
    "DeliveryStatus",
)

_SALES_ORDER_HEADER_SCHEMA: Schema = _derive_schema_from_base(
    _SALES_SCHEMA, _SALES_ORDER_HEADER_COLS, name="OrderHeader"
)
_SALES_ORDER_DETAIL_SCHEMA: Schema = _derive_schema_from_base(
    _SALES_SCHEMA, _SALES_ORDER_DETAIL_COLS, name="OrderDetail"
)


# ============================================================================
# STATIC SCHEMAS (organized, validated, then frozen)
# ============================================================================
DIM_SCHEMAS: Dict[str, Schema] = {
    "Customers": (
        ("CustomerKey", INT_NN),
        ("CustomerID", INT_NN),
        ("VersionNumber", TINYINT(not_null=True)),
        ("EffectiveStartDate", DATE_NN),
        ("EffectiveEndDate", DATE_NN),
        ("IsCurrent", BIT(not_null=True)),
        ("Title", VARCHAR(10, not_null=False)),
        ("FirstName", VARCHAR(50, not_null=False)),
        ("MiddleName", VARCHAR(50, not_null=False)),
        ("LastName", VARCHAR(50, not_null=False)),
        ("FullName", VARCHAR(100, not_null=True)),
        ("DOB", DATE_NULL),
        ("Gender", VARCHAR(10, not_null=True)),
        ("GenderCode", CHAR(1, not_null=True)),
        ("EmailAddress", VARCHAR(100, not_null=False)),
        ("PhoneNumber", VARCHAR(15, not_null=False)),
        ("HomeAddress", VARCHAR(200, not_null=False)),
        ("WorkAddress", VARCHAR(200, not_null=False)),
        ("Latitude", FLOAT(not_null=False)),
        ("Longitude", FLOAT(not_null=False)),
        ("PostalCode", VARCHAR(20, not_null=False)),
        ("CustomerType", VARCHAR(20, not_null=True)),
        ("CompanyName", VARCHAR(200, not_null=False)),
        ("GeographyKey", INT_NN),
        ("HouseholdKey", INT_NN),
        ("HouseholdRole", VARCHAR(20, not_null=False)),
        ("LoyaltyTierKey", INT_NN),
        ("CustomerAcquisitionChannelKey", INT_NN),
        ("CustomerBaseWeight", FLOAT(not_null=True)),
        ("YearlyIncome", FLOAT(not_null=False)),
        ("IncomeGroup", VARCHAR(10, not_null=False)),
        ("MaritalStatus", VARCHAR(20, not_null=False)),
        ("HomeOwnership", VARCHAR(10, not_null=False)),
        ("NumberOfChildren", INT(not_null=False)),
        ("CustomerStartDate", DATE_NN),
        ("CustomerEndDate", DATE_NULL),
    ),
    "CustomerProfile": (
        ("CustomerKey", INT_NN),
        ("AgeGroup", VARCHAR(10, not_null=False)),
        ("Education", VARCHAR(20, not_null=False)),
        ("Occupation", VARCHAR(20, not_null=False)),
        ("NumberOfCars", INT(not_null=False)),
        ("CreditScore", INT(not_null=False)),
        ("UrbanRural", VARCHAR(10, not_null=False)),
        ("TimeZone", VARCHAR(30, not_null=False)),
        ("BirthCity", VARCHAR(100, not_null=False)),
        ("CurrentCity", VARCHAR(100, not_null=False)),
        ("DistanceToNearestStoreKm", FLOAT(not_null=False)),
        ("PreferredLanguage", VARCHAR(20, not_null=False)),
        ("HasOnlineAccount", BIT(not_null=True)),
        ("ConsentEmail", BIT(not_null=True)),
        ("ConsentSMS", BIT(not_null=True)),
        ("ConsentCall", BIT(not_null=True)),
        ("SocialMediaFollower", BIT(not_null=True)),
        ("AppInstalled", BIT(not_null=True)),
        ("NewsletterFrequency", VARCHAR(10, not_null=True)),
        ("DevicePreference", VARCHAR(10, not_null=True)),
        ("LastWebVisitDate", DATE_NULL),
        ("PreferredPaymentMethod", VARCHAR(20, not_null=True)),
        ("PreferredContactMethod", VARCHAR(10, not_null=True)),
        ("ReferralSource", VARCHAR(20, not_null=True)),
        ("MemberSinceDate", DATE_NULL),
        ("IsEmployee", BIT(not_null=True)),
        ("AnnualSpendBucket", VARCHAR(10, not_null=True)),
        ("HasGiftCardBalance", BIT(not_null=True)),
        ("RewardPointsBalance", INT(not_null=False)),
        ("AvgOrderFrequencyDays", INT(not_null=False)),
        ("CustomerSatisfactionScore", INT(not_null=False)),
        ("NPS", INT(not_null=False)),
        ("CustomerLifetimeValue", FLOAT(not_null=False)),
        ("ChurnRisk", VARCHAR(10, not_null=True)),
    ),
    "OrganizationProfile": (
        ("CustomerKey", INT_NN),
        ("Industry", VARCHAR(30, not_null=True)),
        ("CompanySize", VARCHAR(20, not_null=True)),
        ("AnnualRevenue", FLOAT(not_null=True)),
        ("FoundedYear", INT(not_null=True)),
        ("IsPubliclyTraded", BIT(not_null=True)),
        ("HeadquarterCountry", VARCHAR(50, not_null=True)),
        ("Website", VARCHAR(200, not_null=True)),
        ("OrgDomain", VARCHAR(100, not_null=True)),
        ("OrgAddress", VARCHAR(300, not_null=True)),
        ("PrimaryContactName", VARCHAR(100, not_null=True)),
        ("PrimaryContactRole", VARCHAR(50, not_null=True)),
        ("NumberOfEmployees", INT(not_null=True)),
        ("NumberOfLocations", INT(not_null=True)),
        ("ProcurementCycle", VARCHAR(20, not_null=True)),
        ("ContractType", VARCHAR(20, not_null=True)),
        ("CreditRating", VARCHAR(5, not_null=True)),
        ("PaymentTerms", VARCHAR(20, not_null=True)),
        ("PreferredShippingMethod", VARCHAR(20, not_null=True)),
        ("HasDedicatedAccountTeam", BIT(not_null=True)),
        ("AvgOrderValueUSD", FLOAT(not_null=True)),
        ("IsStrategicAccount", BIT(not_null=True)),
        ("YearsAsCustomer", FLOAT(not_null=True)),
        ("SatisfactionTier", VARCHAR(10, not_null=True)),
        ("HasExclusiveDeal", BIT(not_null=True)),
        ("ESGRating", VARCHAR(10, not_null=True)),
    ),
    "CustomerAcquisitionChannels": (
        ("CustomerAcquisitionChannelKey", INT_NN),
        ("AcquisitionChannel", VARCHAR(50, not_null=False)),
        ("ChannelGroup", VARCHAR(50, not_null=False)),
    ),
    "Geography": (
        ("GeographyKey", INT_NN),
        ("City", VARCHAR(100, not_null=True)),
        ("State", VARCHAR(100, not_null=True)),
        ("Country", VARCHAR(100, not_null=True)),
        ("Continent", VARCHAR(100, not_null=True)),
        ("ISOCode", VARCHAR(10, not_null=True)),
        ("Latitude", FLOAT(not_null=True)),
        ("Longitude", FLOAT(not_null=True)),
        ("Timezone", VARCHAR(50, not_null=True)),
        ("Population", BIGINT(not_null=True)),
    ),
    "Products": (
        ("ProductKey", INT_NN),
        ("ProductID", INT_NN),
        ("VersionNumber", TINYINT(not_null=True)),
        ("EffectiveStartDate", DATE_NN),
        ("EffectiveEndDate", DATE_NN),
        ("IsCurrent", BIT(not_null=True)),
        ("ProductCode", VARCHAR(200, not_null=True)),
        ("ProductName", VARCHAR(200, not_null=True)),
        ("ProductDescription", VARCHAR(500, not_null=True)),
        ("SubcategoryKey", INT_NN),
        ("Brand", VARCHAR(30, not_null=True)),
        ("Class", VARCHAR(30, not_null=True)),
        ("Color", VARCHAR(30, not_null=True)),
        ("StockTypeCode", INT_NN),
        ("StockType", VARCHAR(20, not_null=True)),
        ("UnitCost", DECIMAL(10, 2, not_null=True)),
        ("ListPrice", DECIMAL(10, 2, not_null=True)),
        ("BaseProductID", INT_NN),
        ("VariantIndex", INT_NN),
        ("Source", VARCHAR(20, not_null=True)),
    ),
    "ProductProfile": (
        ("ProductKey", INT_NN),
        ("ProductID", INT_NN),
        ("ColorFamily", VARCHAR(20, not_null=True)),
        ("Material", VARCHAR(30, not_null=True)),
        ("Style", VARCHAR(20, not_null=True)),
        ("AgeGroup", VARCHAR(20, not_null=True)),
        ("ProductLine", VARCHAR(30, not_null=True)),
        ("SeasonalityProfile", VARCHAR(30, not_null=True)),
        ("WeightKg", FLOAT(not_null=True)),
        ("LengthCm", FLOAT(not_null=True)),
        ("WidthCm", FLOAT(not_null=True)),
        ("HeightCm", FLOAT(not_null=True)),
        ("VolumeCm3", FLOAT(not_null=True)),
        ("ShippingClass", VARCHAR(20, not_null=True)),
        ("IsFragile", BIT(not_null=True)),
        ("IsHazmat", BIT(not_null=True)),
        ("LeadTimeDays", INT_NN),
        ("CasePackQty", INT_NN),
        ("FulfillmentType", VARCHAR(20, not_null=True)),
        ("BrandTier", VARCHAR(20, not_null=True)),
        ("EligibleStore", BIT(not_null=True)),
        ("EligibleOnline", BIT(not_null=True)),
        ("EligibleMarketplace", BIT(not_null=True)),
        ("EligibleB2B", BIT(not_null=True)),
        ("WarrantyMonths", INT_NN),
        ("ReturnRateBase", FLOAT(not_null=True)),
        ("DefectRateBase", FLOAT(not_null=True)),
        ("ReturnWindowDays", INT_NN),
        ("CountryOfOrigin", VARCHAR(20, not_null=True)),
        ("SustainabilityScore", INT_NN),
        ("CertificationType", VARCHAR(10, not_null=True)),
        ("AssemblyRequired", VARCHAR(5, not_null=True)),
        ("PopularityScore", INT_NN),
        ("ABCClassification", VARCHAR(5, not_null=True)),
        ("ReorderPointUnits", INT_NN),
        ("SafetyStockUnits", INT_NN),
        ("PackagingType", VARCHAR(10, not_null=True)),
        ("AvgCustomerRating", FLOAT(not_null=True)),
        ("ReviewCount", INT_NN),
        ("CompetitorPriceIndex", FLOAT(not_null=True)),
        ("MarginCategory", VARCHAR(10, not_null=True)),
        ("IsGiftEligible", VARCHAR(5, not_null=True)),
        ("IsBestseller", VARCHAR(5, not_null=True)),
        ("IsEcoFriendly", VARCHAR(5, not_null=True)),
        ("TargetGender", VARCHAR(10, not_null=True)),
        ("SupplierKey", INT_NN),
    ),
    "ProductCategory": (
        ("CategoryKey", INT_NN),
        ("Category", VARCHAR(100, not_null=True)),
        ("CategoryLabel", VARCHAR(10, not_null=True)),
    ),
    "ProductSubcategory": (
        ("SubcategoryKey", INT_NN),
        ("SubcategoryLabel", VARCHAR(10, not_null=True)),
        ("Subcategory", VARCHAR(100, not_null=True)),
        ("CategoryKey", INT(not_null=False)),
    ),
    "Promotions": (
        ("PromotionKey", INT_NN),
        ("PromotionLabel", VARCHAR(20, not_null=True)),
        ("PromotionName", VARCHAR(100, not_null=True)),
        ("PromotionDescription", VARCHAR(100, not_null=True)),
        ("DiscountPct", DECIMAL(6, 2, not_null=False)),
        ("PromotionType", VARCHAR(20, not_null=True)),
        ("PromotionCategory", VARCHAR(20, not_null=True)),
        ("PromotionYear", INT_NN),
        ("PromotionSequence", INT_NN),
        ("StartDate", DATE_NULL),
        ("EndDate", DATE_NULL),
    ),
    "Warehouses": (
        ("WarehouseKey", INT_NN),
        ("WarehouseName", VARCHAR(100, not_null=True)),
        ("WarehouseType", VARCHAR(30, not_null=True)),
        ("Zone", VARCHAR(20, not_null=True)),
        ("Country", VARCHAR(100, not_null=True)),
        ("Territory", VARCHAR(200, not_null=True)),
        ("GeographyKey", INT_NN),
        ("Capacity", INT(not_null=False)),
        ("SquareFootage", INT(not_null=False)),
    ),
    "Stores": (
        ("StoreKey", INT_NN),
        ("StoreNumber", VARCHAR(10, not_null=True)),
        ("StoreName", VARCHAR(200, not_null=True)),
        ("StoreType", VARCHAR(20, not_null=True)),
        ("StoreFormat", VARCHAR(20, not_null=True)),
        ("OwnershipType", VARCHAR(20, not_null=True)),
        ("RevenueClass", CHAR(1, not_null=True)),
        ("Status", VARCHAR(12, not_null=False)),
        ("GeographyKey", INT_NN),
        ("StoreZone", VARCHAR(20, not_null=True)),
        ("StoreDistrict", VARCHAR(20, not_null=True)),
        ("StoreRegion", VARCHAR(20, not_null=True)),
        ("OpeningDate", DATE(not_null=False)),
        ("ClosingDate", DATE(not_null=False)),
        ("RenovationStartDate", DATE(not_null=False)),
        ("RenovationEndDate", DATE(not_null=False)),
        ("OpenFlag", BIT(not_null=False)),
        ("SquareFootage", INT(not_null=False)),
        ("EmployeeCount", INT(not_null=False)),
        ("StoreManager", VARCHAR(200, not_null=False)),
        ("Phone", VARCHAR(24, not_null=False)),
        ("StoreEmail", VARCHAR(120, not_null=False)),
        ("StoreDescription", VARCHAR("MAX", not_null=False)),
        ("CloseReason", VARCHAR("MAX", not_null=False)),
        ("AvgTransactionValue", FLOAT(not_null=False)),
        ("CustomerSatisfactionScore", FLOAT(not_null=False)),
        ("InventoryTurnoverTarget", FLOAT(not_null=False)),
        ("LastAuditScore", INT(not_null=False)),
        ("ShrinkageRatePct", FLOAT(not_null=False)),
        ("WarehouseKey", INT_NN),
    ),
    "Dates": (
        ("Date", DATE_NN),
        ("DateKey", INT_NN),
        ("DateSerialNumber", INT_NN),

        ("Year", INT_NN),
        ("IsYearStart", BIT(not_null=True)),
        ("IsYearEnd", BIT(not_null=True)),

        ("Quarter", INT_NN),
        ("QuarterStartDate", DATE_NN),
        ("QuarterEndDate", DATE_NN),
        ("IsQuarterStart", BIT(not_null=True)),
        ("IsQuarterEnd", BIT(not_null=True)),
        ("QuarterYear", VARCHAR(10, not_null=True)),

        ("Month", INT_NN),
        ("MonthName", VARCHAR(10, not_null=True)),
        ("MonthShort", VARCHAR(10, not_null=True)),
        ("MonthStartDate", DATE_NN),
        ("MonthEndDate", DATE_NN),
        ("MonthYear", VARCHAR(20, not_null=True)),
        ("MonthYearKey", INT_NN),
        ("YearQuarterKey", INT_NN),
        ("CalendarMonthIndex", INT_NN),
        ("CalendarQuarterIndex", INT_NN),
        ("IsMonthStart", BIT(not_null=True)),
        ("IsMonthEnd", BIT(not_null=True)),

        ("WeekOfMonth", INT_NN),
        ("CalendarWeekNumber", INT_NN),
        ("CalendarWeekStartDate", DATE_NN),
        ("CalendarWeekEndDate", DATE_NN),
        ("CalendarWeekDateRange", VARCHAR(30, not_null=True)),
        ("CalendarWeekIndex", INT_NN),

        ("Day", INT_NN),
        ("DayName", VARCHAR(10, not_null=True)),
        ("DayShort", VARCHAR(10, not_null=True)),
        ("DayOfYear", INT_NN),
        ("DayOfWeek", INT_NN),
        ("IsWeekend", BIT(not_null=True)),
        ("IsBusinessDay", BIT(not_null=True)),
        ("NextBusinessDay", DATE_NN),
        ("PreviousBusinessDay", DATE_NN),
        ("YearStartDate", DATE_NN),
        ("YearEndDate", DATE_NN),
        ("MonthDays", INT_NN),
        ("QuarterDays", INT_NN),
        ("YearDays", INT_NN),
        ("DayOfQuarter", INT_NN),
        ("DatePreviousWeek", DATE_NN),
        ("DatePreviousMonth", DATE_NN),
        ("DatePreviousQuarter", DATE_NN),
        ("DatePreviousYear", DATE_NN),

        ("IsToday", BIT(not_null=True)),
        ("IsCurrentYear", BIT(not_null=True)),
        ("IsCurrentMonth", BIT(not_null=True)),
        ("IsCurrentQuarter", BIT(not_null=True)),
        ("CurrentDayOffset", INT_NN),
        ("YearOffset", SMALLINT(not_null=True)),
        ("CalendarMonthOffset", INT_NN),
        ("CalendarQuarterOffset", INT_NN),
        ("CalendarWeekOffset", INT_NN),

        ("ISOWeekNumber", INT_NN),
        ("ISOYear", INT_NN),
        ("ISOYearWeekIndex", INT_NN),
        ("ISOWeekOffset", INT_NN),
        ("ISOWeekStartDate", DATE_NN),
        ("ISOWeekEndDate", DATE_NN),
        ("ISOWeekDateRange", VARCHAR(30, not_null=True)),

        # Month-based fiscal calendar (kept as "Fiscal ..." to distinguish from Weekly Fiscal)
        ("FiscalYearStartYear", INT_NN),
        ("FiscalMonthNumber", INT_NN),
        ("FiscalQuarterNumber", INT_NN),
        ("FiscalMonthIndex", INT_NN),
        ("FiscalQuarterIndex", INT_NN),
        ("FiscalMonthOffset", INT_NN),
        ("FiscalQuarterOffset", INT_NN),
        ("FiscalQuarterLabel", VARCHAR(20, not_null=True)),
        ("FiscalMonthName", VARCHAR(10, not_null=True)),
        ("FiscalMonthShort", VARCHAR(10, not_null=True)),
        ("FiscalYearRange", VARCHAR(20, not_null=True)),
        ("FiscalYearStartDate", DATE_NN),
        ("FiscalYearEndDate", DATE_NN),
        ("FiscalQuarterStartDate", DATE_NN),
        ("FiscalQuarterEndDate", DATE_NN),
        ("IsFiscalYearStart", BIT(not_null=True)),
        ("IsFiscalYearEnd", BIT(not_null=True)),
        ("IsFiscalQuarterStart", BIT(not_null=True)),
        ("IsFiscalQuarterEnd", BIT(not_null=True)),
        ("FiscalYear", INT_NN),
        ("FiscalYearLabel", VARCHAR(10, not_null=True)),
        ("FiscalQuarterDays", INT_NN),
        ("FiscalYearDays", INT_NN),
        ("FiscalDayOfQuarter", INT_NN),
        ("FiscalDayOfYear", INT_NN),
        ("FiscalYearOffset", INT_NN),
        # Weekly fiscal calendar (DAX weekly logic), disambiguated with "Weekly Fiscal ..." prefix.
        ("FWYearNumber", INT_NN),
        ("FWYearLabel", VARCHAR(10, not_null=True)),
        ("FWQuarterNumber", INT_NN),
        ("FWQuarterLabel", VARCHAR(20, not_null=True)),
        ("FWQuarterIndex", INT_NN),
        ("FWQuarterOffset", INT_NN),
        ("FWMonthNumber", INT_NN),
        ("FWMonthLabel", VARCHAR(20, not_null=True)),
        ("FWMonthIndex", INT_NN),
        ("FWMonthOffset", INT_NN),
        ("FWWeekNumber", INT_NN),
        ("FWWeekLabel", VARCHAR(20, not_null=True)),
        ("FWWeekDateRange", VARCHAR(30, not_null=True)),
        ("FWWeekIndex", INT_NN),
        ("FWWeekOffset", INT_NN),
        ("FWPeriodNumber", INT_NN),
        ("FWPeriodLabel", VARCHAR(20, not_null=True)),
        ("FWStartOfYear", DATE_NN),
        ("FWEndOfYear", DATE_NN),
        ("FWStartOfQuarter", DATE_NN),
        ("FWEndOfQuarter", DATE_NN),
        ("FWStartOfMonth", DATE_NN),
        ("FWEndOfMonth", DATE_NN),
        ("FWStartOfWeek", DATE_NN),
        ("FWEndOfWeek", DATE_NN),
        ("FWWeekDayNumber", INT_NN),
        ("FWWeekDayNameShort", VARCHAR(10, not_null=True)),
        ("FWDayOfYear", INT_NN),
        ("FWDayOfQuarter", INT_NN),
        ("FWDayOfMonth", INT_NN),
        ("FWMonthDays", INT_NN),
        ("FWQuarterDays", INT_NN),
        ("FWYearDays", INT_NN),
        ("FWDatePreviousMonth", DATE_NN),
        ("FWDatePreviousQuarter", DATE_NN),
        ("FWIsWorkingDay", BIT(not_null=True)),
        ("FWDayType", VARCHAR(20, not_null=True)),
        ("FWWeekInQuarterNumber", INT_NN),
        ("FWYearMonthLabel", VARCHAR(20, not_null=True)),
        ("WeeklyFiscalSystem", VARCHAR(40, not_null=True)),
    ),
    "Time": (
        ("TimeKey", SMALLINT_NN),       # 0..1439 minute-of-day
        ("Hour24", INT_NN),             # 0..23
        ("Hour12", INT_NN),             # 1..12
        ("Minute", INT_NN),             # 0..59
        ("AmPm", VARCHAR(2, not_null=True)),
        ("Hour12Text", VARCHAR(5, not_null=True)),  # "3 PM"
        ("TimeText", VARCHAR(5, not_null=True)),     # "HH:MM"

        ("PeriodOfDay", VARCHAR(15, not_null=True)),  # Midnight/Early Morning/Morning/Afternoon/Evening/Night
        ("PeriodOfDaySort", INT_NN),

        ("Bin15mKey", INT_NN),
        ("Bin30mKey", INT_NN),
        ("Bin1hKey", INT_NN),
        ("Bin6hKey", INT_NN),
        ("Bin12hKey", INT_NN),

        ("Bin15mLabel", VARCHAR(11, not_null=True)),
        ("Bin30mLabel", VARCHAR(11, not_null=True)),
        ("Bin1hLabel", VARCHAR(11, not_null=True)),
        ("Bin6hLabel", VARCHAR(11, not_null=True)),
        ("Bin12hLabel", VARCHAR(11, not_null=True)),
        ("Bin12hName", VARCHAR(12, not_null=True)),  # Before Noon / After Noon

        ("TimeSeconds", INT_NN),
        ("TimeOfDay", TIME(0, not_null=True)),
    ),
    "Currency": (
        ("CurrencyKey", INT_NN),
        ("CurrencyCode", VARCHAR(10, not_null=True)),
        ("CurrencyName", VARCHAR(50, not_null=True)),
        ("CurrencySymbol", VARCHAR(10, not_null=True)),
        ("DecimalPlaces", INT_NN),
    ),
    "Employees": (
        ("EmployeeKey", INT_NN),
        ("ParentEmployeeKey", INT_NULL),
        ("EmployeeName", VARCHAR(200, not_null=True)),
        ("Title", VARCHAR(100, not_null=True)),
        ("OrgLevel", INT_NN),
        ("OrgUnitType", VARCHAR(20, not_null=True)),
        ("RegionId", INT_NULL),
        ("DistrictId", INT_NULL),
        ("StoreKey", INT_NULL),
        ("GeographyKey", INT_NULL),
        ("HireDate", DATE_NN),
        ("TerminationDate", DATE_NULL),
        ("TerminationReason", VARCHAR(20, not_null=False)),
        ("IsActive", BIT(not_null=True)),
        ("EmploymentType", VARCHAR(20, not_null=True)),
        ("FTE", DECIMAL(3, 2, not_null=True)),
        ("Gender", VARCHAR(10, not_null=False)),
        ("FirstName", VARCHAR(100, not_null=True)),
        ("LastName", VARCHAR(100, not_null=True)),
        ("MiddleName", VARCHAR(50, not_null=False)),
        ("BirthDate", DATE_NN),
        ("MaritalStatus", CHAR(1, not_null=True)),
        ("EmailAddress", VARCHAR(200, not_null=True)),
        ("Phone", VARCHAR(20, not_null=True)),
        ("EmergencyContactName", VARCHAR(200, not_null=True)),
        ("EmergencyContactPhone", VARCHAR(20, not_null=True)),
        ("SalariedFlag", BIT(not_null=True)),
        ("PayFrequency", INT_NN),
        ("BaseRate", DECIMAL(10, 2, not_null=True)),
        ("VacationHours", INT_NN),
        ("Status", VARCHAR(20, not_null=True)),
        ("IsSalesperson", BIT(not_null=True)),
        ("DepartmentName", VARCHAR(50, not_null=True)),
    ),
    "EmployeeStoreAssignments": (
        ("AssignmentKey", INT_NN),
        ("EmployeeKey", INT_NN),
        ("AssignmentSequence", SMALLINT(not_null=True)),
        ("StoreKey", INT_NN),
        ("StartDate", DATE_NN),
        ("EndDate", DATE_NN),
        ("FTE", DECIMAL(18, 2, not_null=True)),
        ("RoleAtStore", VARCHAR(100, not_null=True)),
        ("IsPrimary", BIT(not_null=True)),
        ("TransferReason", VARCHAR(50, not_null=True)),
        ("Status", VARCHAR(20, not_null=True)),
    ),
    "Suppliers": (
        ("SupplierKey", INT_NN),
        ("SupplierName", VARCHAR(200, not_null=True)),
        ("SupplierType", VARCHAR(50, not_null=True)),
        ("Country", VARCHAR(100, not_null=False)),
        ("ReliabilityScore", DECIMAL(4, 3, not_null=False)),
    ),
    "Plans": (
        ("PlanKey", INT_NN),
        ("PlanName", VARCHAR(100, not_null=True)),
        ("PlanType", VARCHAR(50, not_null=True)),
        ("Category", VARCHAR(50, not_null=True)),
        ("BillingCycle", VARCHAR(20, not_null=True)),
        ("CycleMonths", INT(not_null=True)),
        ("BaseMonthlyPrice", DECIMAL(10, 2, not_null=False)),
        ("Discount", DECIMAL(5, 2, not_null=True)),
        ("CyclePrice", DECIMAL(10, 2, not_null=True)),
        ("AnnualPrice", DECIMAL(10, 2, not_null=True)),
        ("Tier", VARCHAR(20, not_null=True)),
        ("MaxUsers", INT(not_null=True)),
        ("HasFreeTrial", BIT(not_null=True)),
        ("LaunchDate", DATE(not_null=True)),
        ("IsActiveFlag", BIT(not_null=True)),
    ),
    "CustomerSubscriptions": (
        ("SubscriptionKey", INT_NN),
        ("CustomerKey", INT_NN),
        ("PlanKey", INT_NN),
        ("PeriodStartDate", DATE(not_null=True)),
        ("PeriodEndDate", DATE(not_null=True)),
        ("PeriodPrice", DECIMAL(10, 2, not_null=True)),
        ("IsFirstPeriod", BIT(not_null=True)),
        ("IsChurnPeriod", BIT(not_null=True)),
        ("IsTrialPeriod", BIT(not_null=True)),
        ("BillingCycleNumber", INT(not_null=True)),
    ),
    "Complaints": (
        ("ComplaintKey", INT_NN),
        ("CustomerKey", INT_NN),
        ("OrderNumber", INT(not_null=False)),
        ("LineNumber", INT(not_null=False)),
        ("ComplaintDate", DATE_NN),
        ("ResolutionDate", DATE(not_null=False)),
        ("ComplaintType", VARCHAR(30, not_null=True)),
        ("ComplaintDetail", VARCHAR(200, not_null=True)),
        ("Severity", VARCHAR(10, not_null=True)),
        ("Channel", VARCHAR(10, not_null=True)),
        ("Status", VARCHAR(10, not_null=True)),
        ("ResolutionType", VARCHAR(20, not_null=False)),
        ("ResponseDays", INT(not_null=False)),
    ),
    "LoyaltyTiers": (
        ("LoyaltyTierKey", INT_NN),
        ("LoyaltyTier", VARCHAR(50, not_null=True)),
        ("TierRank", TINYINT(not_null=True)),
        ("PointsMultiplier", DECIMAL(4, 2, not_null=True)),
    ),
    "Channels": (
        ("ChannelKey", SMALLINT_NN),
        ("Channel", VARCHAR(50, not_null=False)),
        ("ChannelDescription", VARCHAR(200, not_null=False)),
        ("ChannelGroup", VARCHAR(50, not_null=False)),
        ("ChannelGroupDescription", VARCHAR(200, not_null=False)),
        ("ChannelCode", VARCHAR(30, not_null=False)),
        ("SortOrder", SMALLINT(not_null=False)),
        ("IsDigital", BIT(not_null=False)),
        ("IsPhysical", BIT(not_null=False)),
        ("IsThirdParty", BIT(not_null=False)),
        ("IsB2B", BIT(not_null=False)),
        ("IsAssisted", BIT(not_null=False)),
        ("IsOwnedChannel", BIT(not_null=False)),
        ("Is24x7", BIT(not_null=False)),
        ("OpenTime", VARCHAR(5, not_null=False)),
        ("CloseTime", VARCHAR(5, not_null=False)),
        ("CommissionRate", DECIMAL(5, 2, not_null=False)),
        ("TypicalFulfillmentDays", SMALLINT(not_null=False)),
    ),
    "ReturnReason": (
        ("ReturnReasonKey", INT_NN),
        ("ReturnReason", VARCHAR(200, not_null=True)),
        ("ReturnReasonCategory", VARCHAR(100, not_null=True)),
    ),
}

FACT_SCHEMAS: Dict[str, Schema] = {
    "CustomerWishlists": (
        ("WishlistKey", INT_NN),
        ("CustomerKey", INT_NN),
        ("ProductKey", INT_NN),
        ("AddedDate", DATE_NN),
        ("Priority", VARCHAR(10, not_null=True)),
        ("Quantity", INT_NN),
        ("NetPrice", DECIMAL(10, 2, not_null=True)),
    ),
    "Sales": _SALES_SCHEMA,
    "OrderHeader": _SALES_ORDER_HEADER_SCHEMA,
    "OrderDetail": _SALES_ORDER_DETAIL_SCHEMA,
    "Returns": (
        ("OrderNumber", INT_NN),
        ("OrderLineNumber", INT_NN),
        ("ReturnDate", DATE_NN),
        ("ReturnReasonKey", INT_NN),
        ("ReturnQuantity", INT_NN),
        ("ReturnNetPrice", DECIMAL(8, 2, not_null=True)),
        ("ReturnSequence", INT_NN),
        ("ReturnEventKey", BIGINT_NN),
    ),
    "ExchangeRates": (
        ("Date", DATE_NN),
        ("FromCurrencyKey", INT_NN),
        ("ToCurrencyKey", INT_NN),
        ("FromCurrency", VARCHAR(10, not_null=True)),
        ("ToCurrency", VARCHAR(10, not_null=True)),
        ("Rate", DECIMAL(10, 6, not_null=True)),
    ),
    "ExchangeRatesMonthly": (
        ("Date", DATE_NN),
        ("FromCurrencyKey", INT_NN),
        ("ToCurrencyKey", INT_NN),
        ("FromCurrency", VARCHAR(10, not_null=True)),
        ("ToCurrency", VARCHAR(10, not_null=True)),
        ("AvgRate", DECIMAL(10, 6, not_null=True)),
        ("MinRate", DECIMAL(10, 6, not_null=True)),
        ("MaxRate", DECIMAL(10, 6, not_null=True)),
        ("EndOfMonthRate", DECIMAL(10, 6, not_null=True)),
    ),
    "BudgetYearly": (
        ("Country", VARCHAR(100, not_null=True)),
        ("Category", VARCHAR(100, not_null=True)),
        ("BudgetYear", INT_NN),
        ("Scenario", VARCHAR(20, not_null=True)),
        ("BudgetGrowthPct", DECIMAL(9, 6, not_null=True)),
        ("BudgetAmount", DECIMAL(19, 2, not_null=True)),
        ("BudgetQuantity", DECIMAL(19, 2, not_null=True)),
        ("BudgetMethod", VARCHAR(140, not_null=True)),
    ),
    "BudgetMonthly": (
        ("Country", VARCHAR(100, not_null=True)),
        ("Category", VARCHAR(100, not_null=True)),
        ("BudgetYear", INT_NN),
        ("BudgetMonthStart", DATE_NN),
        ("Scenario", VARCHAR(20, not_null=True)),
        ("BudgetAmount", DECIMAL(19, 2, not_null=True)),
        ("BudgetQuantity", DECIMAL(19, 2, not_null=True)),
        ("BudgetMethod", VARCHAR(140, not_null=True)),
    ),
    "InventorySnapshot": (
        ("ProductKey", INT_NN),
        ("WarehouseKey", INT_NN),
        ("SnapshotDate", DATE_NN),
        ("QuantityOnHand", INT_NN),
        ("QuantityOnOrder", INT_NN),
        ("QuantitySold", INT_NN),
        ("QuantityReceived", INT_NN),
        ("ReorderFlag", TINYINT(not_null=True)),
        ("StockoutFlag", TINYINT(not_null=True)),
        ("DaysOutOfStock", TINYINT(not_null=True)),
    ),
}

# Validate before freezing (fail fast at import time)
_ALL_SCHEMAS_MUT: Dict[str, Schema] = {**DIM_SCHEMAS, **FACT_SCHEMAS}
_validate_schema_map(_ALL_SCHEMAS_MUT)

# Freeze: prevent accidental runtime mutation by callers
STATIC_SCHEMAS: Mapping[str, Schema] = MappingProxyType(_ALL_SCHEMAS_MUT)


# ============================================================================
# DATE COLUMN GROUPS (logical, superset)
# ============================================================================

# ---------------------------------------------------------------------
# Dates schema helpers
# ---------------------------------------------------------------------

_WF_INTERNAL_COLS: list[str] = [
    "FWYearNumber",
    "FWYearLabel",
    "FWQuarterNumber",
    "FWQuarterLabel",
    "FWQuarterIndex",
    "FWQuarterOffset",
    "FWMonthNumber",
    "FWMonthLabel",
    "FWMonthIndex",
    "FWMonthOffset",
    "FWWeekNumber",
    "FWWeekLabel",
    "FWWeekDateRange",
    "FWWeekIndex",
    "FWWeekOffset",
    "FWPeriodNumber",
    "FWPeriodLabel",
    "FWStartOfYear",
    "FWEndOfYear",
    "FWStartOfQuarter",
    "FWEndOfQuarter",
    "FWStartOfMonth",
    "FWEndOfMonth",
    "FWStartOfWeek",
    "FWEndOfWeek",
    "FWWeekDayNumber",
    "FWWeekDayNameShort",
    "FWDayOfYear",
    "FWDayOfQuarter",
    "FWDayOfMonth",
    "FWMonthDays",
    "FWQuarterDays",
    "FWYearDays",
    "FWDatePreviousMonth",
    "FWDatePreviousQuarter",
    "FWIsWorkingDay",
    "FWDayType",
    "FWWeekInQuarterNumber",
    "FWYearMonthLabel",
    "WeeklyFiscalSystem",
]



# ---------------------------------------------------------------------
# Dates column naming
# ---------------------------------------------------------------------
# The generator emits SQL-friendly internal column names (no spaces).
# Any "pretty" renaming should be done downstream (e.g., Power BI display folders or SQL views).


# Internal Dates column groups (must match dates.py resolve_date_columns)
#
# _DATES_BASE_INTERNAL: Always emitted regardless of include flags.
# Contains the primary key columns plus fundamental date-part attributes
# that every downstream consumer needs (Year, Month, Day, names, etc.).
#
# _DATES_CALENDAR_INTERNAL: Emitted only when include.calendar is true.
# Contains calendar-specific flags (IsYearStart, IsMonthEnd, IsToday, etc.)
# and relative offsets (CurrentDayOffset, YearOffset, etc.).

_DATES_BASE_INTERNAL = [
    "Date", "DateKey", "DateSerialNumber",
    "Year",
    "Quarter", "QuarterStartDate", "QuarterEndDate",
    "QuarterYear",
    "Month", "MonthName", "MonthShort",
    "MonthStartDate", "MonthEndDate",
    "MonthYear", "MonthYearKey",
    "YearQuarterKey",
    "CalendarMonthIndex", "CalendarQuarterIndex",
    "WeekOfMonth",
    "CalendarWeekNumber",
    "CalendarWeekStartDate", "CalendarWeekEndDate",
    "CalendarWeekDateRange",
    "CalendarWeekIndex",
    "Day", "DayName", "DayShort", "DayOfYear", "DayOfQuarter", "DayOfWeek",
    "IsWeekend", "IsBusinessDay",
    "NextBusinessDay", "PreviousBusinessDay",
    "YearStartDate", "YearEndDate",
    "MonthDays", "QuarterDays", "YearDays",
    "DatePreviousWeek", "DatePreviousMonth", "DatePreviousQuarter", "DatePreviousYear",
]

_DATES_CALENDAR_INTERNAL = [
    "IsYearStart", "IsYearEnd",
    "IsQuarterStart", "IsQuarterEnd",
    "IsMonthStart", "IsMonthEnd",
    "IsToday", "IsCurrentYear", "IsCurrentMonth", "IsCurrentQuarter",
    "CurrentDayOffset", "YearOffset",
    "CalendarMonthOffset", "CalendarQuarterOffset", "CalendarWeekOffset",
]

_DATES_ISO_INTERNAL = [
    "ISOWeekNumber",
    "ISOYear",
    "ISOYearWeekIndex",
    "ISOWeekOffset",
    "ISOWeekStartDate",
    "ISOWeekEndDate",
    "ISOWeekDateRange",
]

_DATES_FISCAL_INTERNAL = [
    "FiscalYearStartYear", "FiscalMonthNumber", "FiscalQuarterNumber",
    "FiscalMonthIndex", "FiscalQuarterIndex", "FiscalMonthOffset", "FiscalQuarterOffset",
    "FiscalQuarterLabel", "FiscalMonthName", "FiscalMonthShort", "FiscalYearRange",
    "FiscalYearStartDate", "FiscalYearEndDate",
    "FiscalQuarterStartDate", "FiscalQuarterEndDate",
    "IsFiscalYearStart", "IsFiscalYearEnd",
    "IsFiscalQuarterStart", "IsFiscalQuarterEnd",
    "FiscalYear", "FiscalYearLabel",
    "FiscalQuarterDays", "FiscalYearDays",
    "FiscalDayOfQuarter", "FiscalDayOfYear",
    "FiscalYearOffset",
]


def _cfg_get(obj, key, default=None):
    """Get a config value from either a dict or a Pydantic model."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _wf_is_enabled(wf_cfg) -> bool:
    """Return True if the weekly_fiscal config block is present and enabled.

    Accepts both the new dict form (``{enabled: true, ...}``) and the legacy
    bare bool (``weekly_fiscal: true``).
    """
    if isinstance(wf_cfg, bool):
        return wf_cfg
    if wf_cfg is not None:
        return bool(_cfg_get(wf_cfg, "enabled", True))
    return False


def _dates_internal_columns(dates_cfg: Mapping) -> list[str]:
    """Resolve internal output column list for SQL CREATE TABLE generation.

    Column inclusion logic:
      - Base columns are always included (Date, DateKey, DateSerialNumber,
        plus fundamental date-part attributes: Year, Month, MonthName, Day,
        DayName, Quarter, WeekOfMonth, etc.).
      - include.calendar (default True) adds calendar flags and offsets
        (IsYearStart, IsMonthEnd, IsToday, CurrentDayOffset, etc.).
      - include.iso (default True) adds ISO week columns.
      - include.fiscal (default True) adds monthly fiscal columns.
      - include.weekly_fiscal.enabled adds weekly fiscal (4-4-5) columns;
        the same block holds the algorithm parameters (first_day_of_week,
        weekly_type, quarter_week_type, type_start_fiscal_year).

    NOTE: dates.py resolve_date_columns must be updated to match this split
    so that the parquet output and SQL schema stay in sync.
    """
    dates_cfg = dates_cfg or {}
    include = _cfg_get(dates_cfg, "include", {}) or {}
    wf_cfg = _cfg_get(include, "weekly_fiscal", {}) or {}

    cols: list[str] = list(_DATES_BASE_INTERNAL)

    if _cfg_get(include, "calendar", True):
        cols += _DATES_CALENDAR_INTERNAL
    if _cfg_get(include, "iso", True):
        cols += _DATES_ISO_INTERNAL
    if _cfg_get(include, "fiscal", True):
        cols += _DATES_FISCAL_INTERNAL

    if _wf_is_enabled(wf_cfg):
        cols += _WF_INTERNAL_COLS

    # Dedupe preserve order
    seen = set()
    out = []
    for c in cols:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

DATE_COLUMN_GROUPS = {
    # Base columns are always present in Dates output (includes fundamental date-part attributes).
    "base": frozenset(_DATES_BASE_INTERNAL),

    # Calendar flags and offsets (IsYearStart, IsToday, CurrentDayOffset, etc.).
    "calendar": frozenset(_DATES_CALENDAR_INTERNAL),

    # Optional systems
    "iso": frozenset(_DATES_ISO_INTERNAL),
    "fiscal": frozenset(_DATES_FISCAL_INTERNAL),
    "weekly_fiscal": frozenset(_WF_INTERNAL_COLS),

    # Convenience: base + calendar combined
    "base_calendar": frozenset(_DATES_BASE_INTERNAL + _DATES_CALENDAR_INTERNAL),
}

_INT32_HALF = 1_073_741_823  # int32_max // 2


def promote_order_number(cols: Sequence[SchemaCol], total_rows: int) -> List[SchemaCol]:
    """Widen ``OrderNumber`` to BIGINT when *total_rows* would overflow
    int32, preserving the column's nullability.

    Single source of the int64 ``OrderNumber`` decision for generated SQL:
    used by ``get_sales_schema`` (Sales fact) and by the CREATE TABLE generator
    for the other fact tables that carry the column (OrderHeader/OrderDetail/
    Returns/Complaints) — which pass the static (tuple) schemas. The
    parquet/generation side makes the same int64 call via the ``order_id_int64``
    flag.
    """
    if total_rows <= _INT32_HALF:
        return list(cols)
    return [
        (name, BIGINT(not_null=not dtype.nullable)) if name == "OrderNumber" else (name, dtype)
        for name, dtype in cols
    ]


def get_sales_schema(skip_order_cols: bool, total_rows: int = 0) -> List[SchemaCol]:
    """Return the Sales schema with or without order number columns.

    When *total_rows* exceeds half of int32 max, ``OrderNumber`` is
    promoted to BIGINT to prevent overflow in both parquet and SQL output.
    """
    cols = list(_SALES_SCHEMA_NO_ORDER if skip_order_cols else _SALES_SCHEMA)
    return promote_order_number(cols, total_rows)


def get_sales_order_header_schema() -> List[SchemaCol]:
    """Return the OrderHeader schema."""
    return list(_SALES_ORDER_HEADER_SCHEMA)


def get_sales_order_detail_schema() -> List[SchemaCol]:
    """Return the OrderDetail schema."""
    return list(_SALES_ORDER_DETAIL_SCHEMA)



def get_dates_schema(dates_cfg: Mapping) -> list[SchemaCol]:
    """
    Return the Dates schema for SQL CREATE TABLE / BULK INSERT.

      - Base columns are always included: primary keys (Date, DateKey,
        DateSerialNumber) plus fundamental date-part attributes (Year,
        Month, MonthName, Day, DayName, Quarter, etc.).
      - include.calendar (default True) adds calendar flags/offsets.
      - include.iso (default True) adds ISO week columns.
      - include.fiscal (default True) adds monthly fiscal columns.
      - include.weekly_fiscal.enabled adds weekly fiscal (4-4-5) columns.

    When ``dates.spaced_column_names`` is true, output column names have
    spaces (e.g. ``"Calendar Week Start Date"``).
    """
    dates_cfg = dates_cfg or {}
    internal_cols = _dates_internal_columns(dates_cfg)

    # Import here to avoid circular dependency (columns.py imports from us).
    from src.dimensions.dates.columns import get_date_rename_map
    rename = get_date_rename_map(dates_cfg)

    types = dict(STATIC_SCHEMAS["Dates"])  # internal PascalCase superset
    out: list[SchemaCol] = []
    for col in internal_cols:
        dtype = types.get(col)
        if dtype is None:
            raise KeyError(f"STATIC_SCHEMAS['Dates'] missing expected column: {col}")
        display = rename.get(col, col) if rename else col
        out.append((display, dtype))
    return out



__all__ = [
    "STATIC_SCHEMAS",
    "DATE_COLUMN_GROUPS",
    "promote_order_number",
    "get_sales_schema",
    "get_sales_order_header_schema",
    "get_sales_order_detail_schema",
    "get_dates_schema",
]
