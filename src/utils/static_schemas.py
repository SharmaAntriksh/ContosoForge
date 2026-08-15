# src/utils/static_schemas.py

from __future__ import annotations

from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.tools.sql.dialect import ColumnSpec, SqlType
from src.utils.config_helpers import bool_or as _bool_or

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
# DATES COLUMN REGISTRY — single source of truth
# ============================================================================
# One ordered table of (name, group, spaced_name, sql_type) per Dates column.
# Row order defines the output column order. Everything downstream derives from
# it: the Dates entry in STATIC_SCHEMAS (name -> sql_type), the per-group
# internal column lists + DATE_COLUMN_GROUPS, and columns.py's
# resolve_date_columns group lists + spaced-name rename map. To add a Dates
# column, append one row here (plus the generator that emits it) — the sync test
# and the runner's unregistered-column guard enforce that the sides stay aligned.
_DatesRegistryRow = Tuple[str, str, str, ColumnSpec]

_DATES_REGISTRY: Tuple[_DatesRegistryRow, ...] = (
    # --- base ---
    ('Date', 'base', 'Date', DATE_NN),
    ('DateKey', 'base', 'Date Key', INT_NN),
    ('DateSerialNumber', 'base', 'Date Serial Number', INT_NN),
    ('Year', 'base', 'Year', INT_NN),
    ('Quarter', 'base', 'Quarter', INT_NN),
    ('QuarterStartDate', 'base', 'Quarter Start Date', DATE_NN),
    ('QuarterEndDate', 'base', 'Quarter End Date', DATE_NN),
    ('QuarterYear', 'base', 'Quarter Year', VARCHAR(10, not_null=True)),
    ('Month', 'base', 'Month', INT_NN),
    ('MonthName', 'base', 'Month Name', VARCHAR(10, not_null=True)),
    ('MonthShort', 'base', 'Month Short', VARCHAR(10, not_null=True)),
    ('MonthStartDate', 'base', 'Month Start Date', DATE_NN),
    ('MonthEndDate', 'base', 'Month End Date', DATE_NN),
    ('MonthYear', 'base', 'Month Year', VARCHAR(20, not_null=True)),
    ('MonthYearKey', 'base', 'Month Year Key', INT_NN),
    ('YearQuarterKey', 'base', 'Year Quarter Key', INT_NN),
    ('CalendarMonthIndex', 'base', 'Calendar Month Index', INT_NN),
    ('CalendarQuarterIndex', 'base', 'Calendar Quarter Index', INT_NN),
    ('WeekOfMonth', 'base', 'Week Of Month', INT_NN),
    ('CalendarWeekNumber', 'base', 'Calendar Week Number', INT_NN),
    ('CalendarWeekStartDate', 'base', 'Calendar Week Start Date', DATE_NN),
    ('CalendarWeekEndDate', 'base', 'Calendar Week End Date', DATE_NN),
    ('CalendarWeekDateRange', 'base', 'Calendar Week Date Range', VARCHAR(30, not_null=True)),
    ('CalendarWeekIndex', 'base', 'Calendar Week Index', INT_NN),
    ('Day', 'base', 'Day', INT_NN),
    ('DayName', 'base', 'Day Name', VARCHAR(10, not_null=True)),
    ('DayShort', 'base', 'Day Short', VARCHAR(10, not_null=True)),
    ('DayOfYear', 'base', 'Day Of Year', INT_NN),
    ('DayOfQuarter', 'base', 'Day Of Quarter', INT_NN),
    ('DayOfWeek', 'base', 'Day Of Week', INT_NN),
    ('IsWeekend', 'base', 'Is Weekend', BIT(not_null=True)),
    ('IsBusinessDay', 'base', 'Is Business Day', BIT(not_null=True)),
    ('NextBusinessDay', 'base', 'Next Business Day', DATE_NN),
    ('PreviousBusinessDay', 'base', 'Previous Business Day', DATE_NN),
    ('YearStartDate', 'base', 'Year Start Date', DATE_NN),
    ('YearEndDate', 'base', 'Year End Date', DATE_NN),
    ('MonthDays', 'base', 'Month Days', INT_NN),
    ('QuarterDays', 'base', 'Quarter Days', INT_NN),
    ('YearDays', 'base', 'Year Days', INT_NN),
    ('DatePreviousWeek', 'base', 'Date Previous Week', DATE_NN),
    ('DatePreviousMonth', 'base', 'Date Previous Month', DATE_NN),
    ('DatePreviousQuarter', 'base', 'Date Previous Quarter', DATE_NN),
    ('DatePreviousYear', 'base', 'Date Previous Year', DATE_NN),
    # --- calendar ---
    ('IsYearStart', 'calendar', 'Is Year Start', BIT(not_null=True)),
    ('IsYearEnd', 'calendar', 'Is Year End', BIT(not_null=True)),
    ('IsQuarterStart', 'calendar', 'Is Quarter Start', BIT(not_null=True)),
    ('IsQuarterEnd', 'calendar', 'Is Quarter End', BIT(not_null=True)),
    ('IsMonthStart', 'calendar', 'Is Month Start', BIT(not_null=True)),
    ('IsMonthEnd', 'calendar', 'Is Month End', BIT(not_null=True)),
    ('IsToday', 'calendar', 'Is Today', BIT(not_null=True)),
    ('IsCurrentYear', 'calendar', 'Is Current Year', BIT(not_null=True)),
    ('IsCurrentMonth', 'calendar', 'Is Current Month', BIT(not_null=True)),
    ('IsCurrentQuarter', 'calendar', 'Is Current Quarter', BIT(not_null=True)),
    ('CurrentDayOffset', 'calendar', 'Current Day Offset', INT_NN),
    ('YearOffset', 'calendar', 'Year Offset', SMALLINT_NN),
    ('CalendarMonthOffset', 'calendar', 'Calendar Month Offset', INT_NN),
    ('CalendarQuarterOffset', 'calendar', 'Calendar Quarter Offset', INT_NN),
    ('CalendarWeekOffset', 'calendar', 'Calendar Week Offset', INT_NN),
    # --- iso ---
    ('ISOWeekNumber', 'iso', 'ISO Week Number', INT_NN),
    ('ISOYear', 'iso', 'ISO Year', INT_NN),
    ('ISOYearWeekIndex', 'iso', 'ISO Year Week Index', INT_NN),
    ('ISOWeekOffset', 'iso', 'ISO Week Offset', INT_NN),
    ('ISOWeekStartDate', 'iso', 'ISO Week Start Date', DATE_NN),
    ('ISOWeekEndDate', 'iso', 'ISO Week End Date', DATE_NN),
    ('ISOWeekDateRange', 'iso', 'ISO Week Date Range', VARCHAR(30, not_null=True)),
    # --- fiscal ---
    ('FiscalYearStartYear', 'fiscal', 'Fiscal Year Start Year', INT_NN),
    ('FiscalMonthNumber', 'fiscal', 'Fiscal Month Number', INT_NN),
    ('FiscalQuarterNumber', 'fiscal', 'Fiscal Quarter Number', INT_NN),
    ('FiscalMonthIndex', 'fiscal', 'Fiscal Month Index', INT_NN),
    ('FiscalQuarterIndex', 'fiscal', 'Fiscal Quarter Index', INT_NN),
    ('FiscalMonthOffset', 'fiscal', 'Fiscal Month Offset', INT_NN),
    ('FiscalQuarterOffset', 'fiscal', 'Fiscal Quarter Offset', INT_NN),
    ('FiscalQuarterLabel', 'fiscal', 'Fiscal Quarter Label', VARCHAR(20, not_null=True)),
    ('FiscalMonthName', 'fiscal', 'Fiscal Month Name', VARCHAR(10, not_null=True)),
    ('FiscalMonthShort', 'fiscal', 'Fiscal Month Short', VARCHAR(10, not_null=True)),
    ('FiscalYearRange', 'fiscal', 'Fiscal Year Range', VARCHAR(20, not_null=True)),
    ('FiscalYearStartDate', 'fiscal', 'Fiscal Year Start Date', DATE_NN),
    ('FiscalYearEndDate', 'fiscal', 'Fiscal Year End Date', DATE_NN),
    ('FiscalQuarterStartDate', 'fiscal', 'Fiscal Quarter Start Date', DATE_NN),
    ('FiscalQuarterEndDate', 'fiscal', 'Fiscal Quarter End Date', DATE_NN),
    ('IsFiscalYearStart', 'fiscal', 'Is Fiscal Year Start', BIT(not_null=True)),
    ('IsFiscalYearEnd', 'fiscal', 'Is Fiscal Year End', BIT(not_null=True)),
    ('IsFiscalQuarterStart', 'fiscal', 'Is Fiscal Quarter Start', BIT(not_null=True)),
    ('IsFiscalQuarterEnd', 'fiscal', 'Is Fiscal Quarter End', BIT(not_null=True)),
    ('FiscalYear', 'fiscal', 'Fiscal Year', INT_NN),
    ('FiscalYearLabel', 'fiscal', 'Fiscal Year Label', VARCHAR(10, not_null=True)),
    ('FiscalQuarterDays', 'fiscal', 'Fiscal Quarter Days', INT_NN),
    ('FiscalYearDays', 'fiscal', 'Fiscal Year Days', INT_NN),
    ('FiscalDayOfQuarter', 'fiscal', 'Fiscal Day Of Quarter', INT_NN),
    ('FiscalDayOfYear', 'fiscal', 'Fiscal Day Of Year', INT_NN),
    ('FiscalYearOffset', 'fiscal', 'Fiscal Year Offset', INT_NN),
    # --- weekly_fiscal ---
    ('FWYearNumber', 'weekly_fiscal', 'FW Year Number', INT_NN),
    ('FWYearLabel', 'weekly_fiscal', 'FW Year Label', VARCHAR(10, not_null=True)),
    ('FWQuarterNumber', 'weekly_fiscal', 'FW Quarter Number', INT_NN),
    ('FWQuarterLabel', 'weekly_fiscal', 'FW Quarter Label', VARCHAR(20, not_null=True)),
    ('FWQuarterIndex', 'weekly_fiscal', 'FW Quarter Index', INT_NN),
    ('FWQuarterOffset', 'weekly_fiscal', 'FW Quarter Offset', INT_NN),
    ('FWMonthNumber', 'weekly_fiscal', 'FW Month Number', INT_NN),
    ('FWMonthLabel', 'weekly_fiscal', 'FW Month Label', VARCHAR(20, not_null=True)),
    ('FWMonthIndex', 'weekly_fiscal', 'FW Month Index', INT_NN),
    ('FWMonthOffset', 'weekly_fiscal', 'FW Month Offset', INT_NN),
    ('FWWeekNumber', 'weekly_fiscal', 'FW Week Number', INT_NN),
    ('FWWeekLabel', 'weekly_fiscal', 'FW Week Label', VARCHAR(20, not_null=True)),
    ('FWWeekDateRange', 'weekly_fiscal', 'FW Week Date Range', VARCHAR(30, not_null=True)),
    ('FWWeekIndex', 'weekly_fiscal', 'FW Week Index', INT_NN),
    ('FWWeekOffset', 'weekly_fiscal', 'FW Week Offset', INT_NN),
    ('FWPeriodNumber', 'weekly_fiscal', 'FW Period Number', INT_NN),
    ('FWPeriodLabel', 'weekly_fiscal', 'FW Period Label', VARCHAR(20, not_null=True)),
    ('FWStartOfYear', 'weekly_fiscal', 'FW Start Of Year', DATE_NN),
    ('FWEndOfYear', 'weekly_fiscal', 'FW End Of Year', DATE_NN),
    ('FWStartOfQuarter', 'weekly_fiscal', 'FW Start Of Quarter', DATE_NN),
    ('FWEndOfQuarter', 'weekly_fiscal', 'FW End Of Quarter', DATE_NN),
    ('FWStartOfMonth', 'weekly_fiscal', 'FW Start Of Month', DATE_NN),
    ('FWEndOfMonth', 'weekly_fiscal', 'FW End Of Month', DATE_NN),
    ('FWStartOfWeek', 'weekly_fiscal', 'FW Start Of Week', DATE_NN),
    ('FWEndOfWeek', 'weekly_fiscal', 'FW End Of Week', DATE_NN),
    ('FWWeekDayNumber', 'weekly_fiscal', 'FW Week Day Number', INT_NN),
    ('FWWeekDayNameShort', 'weekly_fiscal', 'FW Week Day Name Short', VARCHAR(10, not_null=True)),
    ('FWDayOfYear', 'weekly_fiscal', 'FW Day Of Year', INT_NN),
    ('FWDayOfQuarter', 'weekly_fiscal', 'FW Day Of Quarter', INT_NN),
    ('FWDayOfMonth', 'weekly_fiscal', 'FW Day Of Month', INT_NN),
    ('FWMonthDays', 'weekly_fiscal', 'FW Month Days', INT_NN),
    ('FWQuarterDays', 'weekly_fiscal', 'FW Quarter Days', INT_NN),
    ('FWYearDays', 'weekly_fiscal', 'FW Year Days', INT_NN),
    ('FWDatePreviousMonth', 'weekly_fiscal', 'FW Date Previous Month', DATE_NN),
    ('FWDatePreviousQuarter', 'weekly_fiscal', 'FW Date Previous Quarter', DATE_NN),
    ('FWIsWorkingDay', 'weekly_fiscal', 'FW Is Working Day', BIT(not_null=True)),
    ('FWDayType', 'weekly_fiscal', 'FW Day Type', VARCHAR(20, not_null=True)),
    ('FWWeekInQuarterNumber', 'weekly_fiscal', 'FW Week In Quarter Number', INT_NN),
    ('FWYearMonthLabel', 'weekly_fiscal', 'FW Year Month Label', VARCHAR(20, not_null=True)),
    ('WeeklyFiscalSystem', 'weekly_fiscal', 'Weekly Fiscal System', VARCHAR(40, not_null=True)),
)

# Canonical group order (also the output section order).
_DATES_GROUP_ORDER: Tuple[str, ...] = ("base", "calendar", "iso", "fiscal", "weekly_fiscal")

# include.<group> fallback defaults — the single shared default table read by
# BOTH resolvers (this module's _dates_internal_columns and columns.py's
# resolve_date_columns), so a missing include key can never diverge between them.
# iso defaults OFF to match the pipeline (DatesIncludeConfig.iso = False).
DATE_GROUP_DEFAULTS: Dict[str, bool] = {"calendar": True, "iso": False, "fiscal": True}


def _dates_cols_by_group(group: str) -> List[str]:
    return [name for name, grp, _spaced, _t in _DATES_REGISTRY if grp == group]


_DATES_BASE_INTERNAL = _dates_cols_by_group("base")
_DATES_CALENDAR_INTERNAL = _dates_cols_by_group("calendar")
_DATES_ISO_INTERNAL = _dates_cols_by_group("iso")
_DATES_FISCAL_INTERNAL = _dates_cols_by_group("fiscal")
_WF_INTERNAL_COLS = _dates_cols_by_group("weekly_fiscal")

# name -> spaced display name (authority for columns.py's rename map).
_DATES_SPACED_NAMES: Dict[str, str] = {name: spaced for name, _g, spaced, _t in _DATES_REGISTRY}

# All Dates columns in output order — used by the runner's unregistered-column guard.
DATE_ALL_COLUMNS: Tuple[str, ...] = tuple(name for name, _g, _s, _t in _DATES_REGISTRY)

# Each column belongs to exactly one group; assert it once at build time so the
# resolvers can skip a per-call dedupe pass and rely on the invariant.
assert len(set(DATE_ALL_COLUMNS)) == len(DATE_ALL_COLUMNS), "duplicate name in _DATES_REGISTRY"

# The Dates schema (name -> sql_type), derived from the registry and injected
# into STATIC_SCHEMAS below.
_DATES_SCHEMA: Schema = tuple((name, sql_type) for name, _g, _s, sql_type in _DATES_REGISTRY)


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
    # Canonical fact-grain width for these two keys is SMALLINT (int16): the
    # values (channel id; minute-of-day 0..1439) fit int16, and the SQL foreign
    # keys reference SMALLINT dimension keys (e.g. Time.TimeKey), so the fact
    # columns must be SMALLINT for FK creation to succeed. The Arrow schema is
    # projected from this (SMALLINT -> int16); the writer casts the int32 arrays
    # the chunk builder/injection produce down to int16 at write time, so the
    # parquet dtype matches the SQL DDL (was int32 in parquet vs SMALLINT in SQL).
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
        ("TargetInventoryTurnover", FLOAT(not_null=False)),
        ("LastAuditScore", INT(not_null=False)),
        ("TargetShrinkageRatePct", FLOAT(not_null=False)),
    ),
    "Stores": (
        ("StoreKey", INT_NN),
        ("StoreNumber", VARCHAR(10, not_null=True)),
        ("StoreName", VARCHAR(200, not_null=True)),
        ("StoreType", VARCHAR(20, not_null=True)),
        ("StoreFormat", VARCHAR(20, not_null=True)),
        ("OwnershipType", VARCHAR(20, not_null=True)),
        ("RevenueClass", CHAR(1, not_null=True)),
        # Status is an as-of-dataset-end snapshot (Open/Renovating/Closed). It is
        # NOT valid for point-in-time queries — use the lifecycle date columns
        # (OpeningDate, ClosingDate, RenovationStartDate/RenovationEndDate), which
        # are the source of truth for a store's state on any given date.
        ("Status", VARCHAR(12, not_null=False)),
        ("GeographyKey", INT_NN),
        ("StoreZone", VARCHAR(20, not_null=True)),
        ("StoreDistrict", VARCHAR(20, not_null=True)),
        ("StoreRegion", VARCHAR(20, not_null=True)),
        ("StoreDistrictId", INT(not_null=True)),
        ("StoreRegionId", INT(not_null=True)),
        ("OpeningDate", DATE(not_null=False)),
        ("ClosingDate", DATE(not_null=False)),
        ("RenovationStartDate", DATE(not_null=False)),
        ("RenovationEndDate", DATE(not_null=False)),
        ("SquareFootage", INT(not_null=False)),
        ("EmployeeCount", INT(not_null=False)),
        ("StoreManager", VARCHAR(200, not_null=False)),
        ("StoreManagerFirstName", VARCHAR(100, not_null=False)),
        ("StoreManagerLastName", VARCHAR(100, not_null=False)),
        ("Phone", VARCHAR(24, not_null=False)),
        ("StoreEmail", VARCHAR(120, not_null=False)),
        ("StoreDescription", VARCHAR("MAX", not_null=False)),
        ("CloseReason", VARCHAR("MAX", not_null=False)),
        ("SuccessorStoreKey", INT(not_null=False)),
        ("WarehouseKey", INT_NN),
    ),
    # Derived from _DATES_REGISTRY (the single source of truth above).
    "Dates": _DATES_SCHEMA,
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
        ("HomeStoreKey", INT_NULL),
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
        ("PayFrequencyLabel", VARCHAR(20, not_null=True)),
        ("BaseRate", DECIMAL(10, 2, not_null=True)),
        ("VacationHours", INT_NN),
        # Status is an as-of-dataset-end snapshot (Active/Terminated). It is NOT
        # valid for point-in-time queries — use HireDate/TerminationDate, which
        # are the source of truth for whether an employee was active on a date.
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
        ("FTE", DECIMAL(3, 2, not_null=True)),
        ("RoleAtStore", VARCHAR(100, not_null=True)),
        ("IsCurrentAssignment", BIT(not_null=True)),
        ("TransferReason", VARCHAR(50, not_null=True)),
        ("AssignmentStatus", VARCHAR(20, not_null=True)),
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
        ("TerminationReason", VARCHAR(40, not_null=False)),
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
        # Snapshot-at-sale dimension keys (carry-through from the sold line) — make
        # Returns a first-class star fact with direct dim relationships. Order must
        # match RETURNS_SCHEMA in returns_builder.py so CSV columns line up with the
        # generated DDL / BULK INSERT.
        ("CustomerKey", INT_NN),
        ("ProductKey", INT_NN),
        ("StoreKey", INT_NN),
        ("EmployeeKey", INT_NN),
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
        # Wide enough for high-value currencies (e.g. USD->X above 9999) and for
        # small inverse/cross rates (e.g. KRW->USD ~ 0.00075) without precision loss.
        ("Rate", DECIMAL(18, 8, not_null=True)),
    ),
    "ExchangeRatesMonthly": (
        ("Date", DATE_NN),
        ("FromCurrencyKey", INT_NN),
        ("ToCurrencyKey", INT_NN),
        ("FromCurrency", VARCHAR(10, not_null=True)),
        ("ToCurrency", VARCHAR(10, not_null=True)),
        ("AvgRate", DECIMAL(18, 8, not_null=True)),
        ("MinRate", DECIMAL(18, 8, not_null=True)),
        ("MaxRate", DECIMAL(18, 8, not_null=True)),
        ("EndOfMonthRate", DECIMAL(18, 8, not_null=True)),
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
        ("ShrinkageQuantity", INT_NN),
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
# The per-group internal column lists (_DATES_BASE_INTERNAL, _WF_INTERNAL_COLS,
# etc.) and _DATES_SPACED_NAMES are all derived from _DATES_REGISTRY above — the
# single source of truth. The generator emits SQL-friendly internal names (no
# spaces); spaced display names are applied downstream via columns.py.


def _cfg_get(obj, key, default=None):
    """Get a config value from either a dict or a Pydantic model."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def weekly_fiscal_enabled(wf_block) -> bool:
    """Single weekly-fiscal enablement predicate.

    Shared by the parquet-side normalizer (``resolve_weekly_fiscal_cfg``) and the
    SQL-side column resolver (``_dates_internal_columns``) so the two can never
    disagree — the exact parquet↔DDL divergence class the registry closes.

    Rules: a bare ``bool`` is honoured first (so ``weekly_fiscal: false`` is not
    erased by the ``or {}`` coalescing); absence / ``None`` / empty ⇒ **enabled**
    (the pipeline default); a present block reports its own ``enabled`` via the
    canonical ``bool_or`` coercion, so ``"false"`` / null / ``0`` parse the same
    on both sides.
    """
    if isinstance(wf_block, bool):
        wf_block = {"enabled": wf_block}
    wf_block = wf_block or {}
    return _bool_or(_cfg_get(wf_block, "enabled", True), True)


def _dates_internal_columns(dates_cfg: Mapping) -> list[str]:
    """Resolve the internal (PascalCase) Dates output column list.

    Single registry-driven resolver for both the parquet output (via
    ``columns.resolve_date_columns``, which only layers spaced-name renaming on
    top) and the SQL/DDL schema (``get_dates_schema``). Inclusion logic:
      - Base columns are always included (Date, DateKey, DateSerialNumber, plus
        fundamental date-part attributes: Year, Month, MonthName, Day, etc.).
      - include.calendar (default True) adds calendar flags/offsets.
      - include.iso (default False) adds ISO week columns.
      - include.fiscal (default True) adds monthly fiscal columns.
      - include.weekly_fiscal.enabled adds weekly fiscal (4-4-5) columns; the same
        block holds the algorithm parameters (first_day_of_week, weekly_type, ...).

    The include.* fallbacks read DATE_GROUP_DEFAULTS and weekly fiscal routes
    through ``weekly_fiscal_enabled`` — the same tables/predicate both output
    paths consume — so a missing/edge-cased key cannot diverge between them. The
    registry assigns each column to exactly one group (asserted at build time),
    so no dedupe pass is needed.
    """
    dates_cfg = dates_cfg or {}
    include = _cfg_get(dates_cfg, "include", {}) or {}

    cols: list[str] = list(_DATES_BASE_INTERNAL)

    if _cfg_get(include, "calendar", DATE_GROUP_DEFAULTS["calendar"]):
        cols += _DATES_CALENDAR_INTERNAL
    if _cfg_get(include, "iso", DATE_GROUP_DEFAULTS["iso"]):
        cols += _DATES_ISO_INTERNAL
    if _cfg_get(include, "fiscal", DATE_GROUP_DEFAULTS["fiscal"]):
        cols += _DATES_FISCAL_INTERNAL

    if weekly_fiscal_enabled(_cfg_get(include, "weekly_fiscal", None)):
        cols += _WF_INTERNAL_COLS

    return cols

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

# The emitted OrderNumber is a *day-banded* id, not a 1..total_rows counter: each
# calendar day gets a disjoint band and every chunk a slot within it, so the
# worst-case id is ~8x total_rows (an 8x safety factor in the per-chunk
# allocation). See sales.generate_sales_fact's ``_max_order_id``. The SQL DDL
# must promote at the same point the parquet writer does, i.e. against the id
# ceiling — NOT against total_rows alone (which would keep INT up to ~1.07B rows
# while parquet already emits BIGINT past ~134M rows → import overflow).
_ORDER_ID_SAFETY = 8


def order_id_int64_for_rows(total_rows: int) -> bool:
    """ID-space-aware estimate of the int64 ``OrderNumber`` decision from row
    count alone. Mirrors the parquet side's ``_max_order_id`` threshold so the
    two agree whenever the authoritative per-run flag is unavailable.
    """
    return int(total_rows) * _ORDER_ID_SAFETY > _INT32_HALF


def promote_order_number(
    cols: Sequence[SchemaCol],
    total_rows: int,
    *,
    force_int64: Optional[bool] = None,
) -> List[SchemaCol]:
    """Widen ``OrderNumber`` to BIGINT when the run needs 64-bit order ids,
    preserving the column's nullability.

    Single source of the int64 ``OrderNumber`` decision for generated SQL:
    used by ``get_sales_schema`` (Sales fact) and by the CREATE TABLE generator
    for the other fact tables that carry the column (OrderHeader/OrderDetail/
    Returns/Complaints).

    ``force_int64`` is the authoritative decision computed once per run from the
    real emitted id ceiling (stored on the sales run manifest, threaded here by
    the CREATE TABLE generator). When it is ``None`` — e.g. generating DDL with
    no run to consult — the decision falls back to a raw ``total_rows`` threshold
    (callers that want the id-space-aware estimate pass
    ``force_int64=order_id_int64_for_rows(total_rows)``).
    """
    use_bigint = force_int64 if force_int64 is not None else (total_rows > _INT32_HALF)
    if not use_bigint:
        return list(cols)
    return [
        (name, BIGINT(not_null=not dtype.nullable)) if name == "OrderNumber" else (name, dtype)
        for name, dtype in cols
    ]


def get_sales_schema(
    skip_order_cols: bool,
    total_rows: int = 0,
    *,
    force_int64: Optional[bool] = None,
) -> List[SchemaCol]:
    """Return the Sales schema with or without order number columns.

    ``OrderNumber`` is promoted to BIGINT per the run's ``force_int64`` decision
    when supplied, otherwise per the raw *total_rows* threshold. See
    ``promote_order_number``.
    """
    cols = list(_SALES_SCHEMA_NO_ORDER if skip_order_cols else _SALES_SCHEMA)
    return promote_order_number(cols, total_rows, force_int64=force_int64)


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
      - include.iso (default False) adds ISO week columns.
      - include.fiscal (default True) adds monthly fiscal columns.
      - include.weekly_fiscal.enabled adds weekly fiscal (4-4-5) columns.

    Defaults come from DATE_GROUP_DEFAULTS (shared with the parquet resolver).
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
    "DATE_GROUP_DEFAULTS",
    "DATE_ALL_COLUMNS",
    "promote_order_number",
    "get_sales_schema",
    "get_sales_order_header_schema",
    "get_sales_order_detail_schema",
    "get_dates_schema",
]
