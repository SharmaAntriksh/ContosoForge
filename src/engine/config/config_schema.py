"""Pydantic schema models for config.yaml and models.yaml.

Typed config models with attribute access::

    cfg = AppConfig.from_raw_dict(raw)   # validated Pydantic model
    cfg.sales.total_rows                 # attribute access
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date as _date
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# =========================================================================
# Mutation mixin (write-side helpers for pipeline_runner / web overrides)
# =========================================================================

class _MutationMixin:
    """Mixin for controlled mutation of Pydantic config models.

    Provides dict-like write/mutation helpers needed by pipeline_runner
    (override injection) and the web layer (preset application).
    Read access uses standard attribute syntax: ``cfg.sales.total_rows``.
    """

    def _all_fields(self) -> Dict[str, Any]:
        """Merge __dict__ (declared fields) + __pydantic_extra__ (unknown keys)."""
        d = dict(self.__dict__)
        extra = getattr(self, "__pydantic_extra__", None)
        if extra:
            d.update(extra)
        return d

    # -- read (dict-like access for code that does cfg["key"] or cfg.get()) --

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            return default

    def __contains__(self, key: str) -> bool:
        return key in self._all_fields()

    # -- write (mutation support for pipeline_runner overrides) --

    def __setitem__(self, key: str, value: Any) -> None:
        object.__setattr__(self, key, value)

    def __delitem__(self, key: str) -> None:
        try:
            object.__delattr__(self, key)
        except AttributeError:
            raise KeyError(key)

    # -- iteration (for dict(model), model_dump fallback) --

    def keys(self):
        return self._all_fields().keys()

    def items(self):
        return self._all_fields().items()

    def values(self):  # noqa: A003
        return self._all_fields().values()

    def __iter__(self):
        return iter(self._all_fields())

    def __len__(self):
        return len(self._all_fields())

    # -- dict-like mutation helpers --

    def pop(self, key: str, *args):
        """Return value for *key* and remove it from the model."""
        try:
            val = getattr(self, key)
        except AttributeError:
            if args:
                return args[0]
            raise KeyError(key)
        try:
            object.__delattr__(self, key)
        except AttributeError:
            pass
        return val

    def setdefault(self, key: str, default: Any = None) -> Any:
        try:
            val = getattr(self, key)
            if val is not None:
                return val
        except AttributeError:
            pass
        object.__setattr__(self, key, default)
        return default

    def update(self, other: Any = None, **kwargs: Any) -> None:
        if other:
            items = other.items() if hasattr(other, "items") else other
            for k, v in items:
                object.__setattr__(self, k, v)
        for k, v in kwargs.items():
            object.__setattr__(self, k, v)

    def clear(self) -> None:
        """Remove all fields (set to None or delete extras)."""
        for key in list(self._all_fields().keys()):
            try:
                object.__delattr__(self, key)
            except AttributeError:
                pass

    def copy(self):
        """Shallow copy — delegates to Pydantic's model_copy to avoid deprecation."""
        if hasattr(self, "model_copy"):
            return self.model_copy()
        return self


# =========================================================================
# Base model with dict compat + permissive extras
# =========================================================================

class _Base(_MutationMixin, BaseModel):
    model_config = ConfigDict(
        extra="forbid",         # catch typos in config keys
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )


# Register _Base as a virtual Mapping so isinstance(cfg, Mapping) is True.
# This lets us replace isinstance(x, dict) guards with isinstance(x, Mapping)
# during the migration — both plain dicts and Pydantic models pass.
Mapping.register(_Base)


# =========================================================================
# config.yaml — sub-models (alphabetical by section)
# =========================================================================

# -- Budget --

class BudgetScenariosConfig(_Base):
    Low: float = -0.03
    Medium: float = 0.00
    High: float = 0.05


class BudgetGrowthCapsConfig(_Base):
    high: float = 0.30
    low: float = -0.20


class BudgetWeightsConfig(_Base):
    local: float = 0.60
    category: float = 0.30
    global_: float = 0.10

    # "global" is a Python keyword; accept it from YAML via alias.
    # extra="allow" needed because model_validator renames "global" → "global_"
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _rename_global(cls, data: Any) -> Any:
        if isinstance(data, dict) and "global" in data:
            data = dict(data)
            data["global_"] = data.pop("global")
        return data


class BudgetConfig(_Base):
    enabled: bool = True
    report_currency: str = "USD"
    scenarios: BudgetScenariosConfig = BudgetScenariosConfig()
    growth_caps: BudgetGrowthCapsConfig = BudgetGrowthCapsConfig()
    weights: BudgetWeightsConfig = BudgetWeightsConfig()
    default_backcast_growth: float = 0.05
    return_rate_cap: float = 0.30


# -- Customers --

class CustomersConfig(_Base):
    total_customers: Optional[int] = None
    active_ratio: float = 0.98
    profile: Optional[str] = None  # DEPRECATED: use models.macro_demand.trend
    first_year_pct: Optional[float] = None
    # Flattened from region_mix by _expand_region_mix
    pct_us: float = 0.0
    pct_eu: float = 0.0
    pct_india: float = 0.0
    pct_asia: float = 0.0
    pct_org: float = 0.0
    # Original region_mix kept for reference
    region_mix: Optional[Dict[str, float]] = None
    org_pct: Optional[float] = None
    # Injected by resolve_trend_preset
    lifecycle: Optional[Dict[str, Any]] = None
    # Customer enrichment (loyalty tiers, acquisition channels)
    enrichment: Optional[Dict[str, Any]] = None
    # Override block (seed, dates)
    override: Optional[Dict[str, Any]] = None
    # Injected by dimensions_runner
    global_dates: Optional[Any] = None
    # Household grouping: fraction of individual customers in multi-person households
    household_pct: Optional[float] = None
    # SCD Type 2 settings (nested block)
    scd2: Optional["CustomersSCD2Config"] = None



# -- Dates (dimension table config) --

class WeeklyFiscalConfig(_Base):
    enabled: bool = False
    first_day_of_week: int = 0
    weekly_type: str = "Last"
    quarter_week_type: str = "445"
    type_start_fiscal_year: int = 1


class DatesIncludeConfig(_Base):
    calendar: bool = True
    iso: bool = False
    fiscal: bool = True
    weekly_fiscal: Optional[WeeklyFiscalConfig] = None


class DatesTableConfig(_Base):
    fiscal_start_month: int = 5
    as_of_date: Optional[str] = None
    spaced_column_names: bool = False

    @model_validator(mode="before")
    @classmethod
    def _migrate_fiscal_offset(cls, data: Any) -> Any:
        """Migrate removed ``fiscal_month_offset`` to ``fiscal_start_month``."""
        if isinstance(data, dict) and "fiscal_month_offset" in data:
            data = dict(data)
            if "fiscal_start_month" not in data:
                data["fiscal_start_month"] = data.pop("fiscal_month_offset")
            else:
                data.pop("fiscal_month_offset")
        return data
    buffer_years: int = 1
    include: DatesIncludeConfig = DatesIncludeConfig()
    override: Optional[Dict[str, Any]] = None
    # Parquet output knobs
    parquet_compression: str = "snappy"
    parquet_compression_level: Optional[int] = None
    force_date32: bool = True
    # Injected by dimensions_runner
    global_dates: Optional[Any] = None


# -- Defaults --

class GlobalDatesConfig(_Base):
    start: str = "2021-01-01"
    end: str = "2025-12-31"


class DefaultsConfig(_Base):
    seed: int = 42
    random: bool = False
    dates: GlobalDatesConfig = GlobalDatesConfig()
    final_output: str = "./generated_datasets"
    view_schema: str = "dbo"


# -- Employees --

class HRConfig(_Base):
    email_domain: str = "contoso.com"


class StoreAssignmentsConfig(_Base):
    # Used by employee generator (not ESA) to guarantee min SAs per store
    primary_sales_role: str = "Sales Associate"
    min_primary_sales_per_store: int = 1


class TransfersConfig(_Base):
    enabled: bool = False
    annual_rate: float = 0.05
    min_tenure_months: int = 6
    same_region_pref: float = 0.7


class EmployeesConfig(_Base):
    hr: HRConfig = HRConfig()
    store_assignments: StoreAssignmentsConfig = StoreAssignmentsConfig()
    transfers: TransfersConfig = TransfersConfig()
    # Parquet output knobs
    parquet_compression: str = "snappy"
    parquet_compression_level: Optional[int] = None
    # Injected by dimensions_runner
    global_dates: Optional[Any] = None


# -- Currency --

class CurrencyConfig(_Base):
    currencies: Optional[List[str]] = None  # if None, derived from union of exchange_rates from/to
    parquet_compression: str = "snappy"
    parquet_compression_level: Optional[int] = None
    force_date32: bool = True


# -- Exchange Rates --

class ExchangeRatesConfig(_Base):
    from_currencies: List[str] = ["USD"]
    to_currencies: List[str] = ["CAD", "GBP", "EUR", "INR", "AUD", "CNY", "JPY"]
    base_currency: str = "USD"
    future_annual_drift: float = 0.02
    include_monthly: bool = True
    master_file: str = "./data/exchange_rates_master/fx_master.parquet"
    # Injected by dimensions_runner
    global_dates: Optional[Any] = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_currencies_key(cls, data: Any) -> Any:
        """Backward compat: rename old ``currencies`` key to ``to_currencies``."""
        if isinstance(data, dict) and "currencies" in data and "to_currencies" not in data:
            data = dict(data)
            data["to_currencies"] = data.pop("currencies")
        # Strip removed keys so extra="forbid" on _Base doesn't reject them
        if isinstance(data, dict):
            for removed in ("volatility", "use_global_dates", "dates"):
                data.pop(removed, None)
        return data


# -- Geography --

class GeographyConfig(_Base):
    override: Optional[Dict[str, Any]] = None  # override.seed, override.dates, etc.


# -- Inventory --

class ABCStockMultiplierConfig(_Base):
    A: float = 1.20
    B: float = 1.00
    C: float = 0.60


class ShrinkageConfig(_Base):
    enabled: bool = True
    rate: float = 0.02


class InventoryConfig(_Base):
    enabled: bool = True
    seed: int = 42
    grain: str = "monthly"
    partition_by: Optional[List[str]] = ["Year"]
    abc_filter: Optional[List[str]] = None
    min_demand_months: int = 6
    initial_stock_multiplier: float = 1.5
    reorder_compliance: float = 0.65
    lead_time_variance: float = 0.40
    overstock_bias: float = 1.0
    abc_stock_multiplier: ABCStockMultiplierConfig = ABCStockMultiplierConfig()
    shrinkage: ShrinkageConfig = ShrinkageConfig()
    write_chunk_rows: int = 2_000_000


# -- Packaging --

class PackagingConfig(_Base):
    reset_scratch_fact_out: bool = True
    clean_scratch_fact_out: bool = True
    dim_parquet_compression: str = "snappy"
    dim_parquet_compression_level: Optional[int] = None
    dim_force_date32: bool = True


# -- SCD Type 2 sub-models --

class CustomersSCD2Config(_Base):
    enabled: bool = False
    change_rate: float = 0.15
    max_versions: int = 4


class ProductsSCD2Config(_Base):
    enabled: bool = False
    revision_frequency: int = Field(default=12, ge=1)   # months between price revisions
    price_drift: float = 0.05                            # ~5% price change per revision
    max_versions: int = Field(default=4, ge=1)           # max version rows per product
    # Probability a product accrues an additional revision (geometric draw).
    # Default 0.40 yields roughly 60/24/10/6 across versions 1..4 — most
    # products keep V1 only and a tail accumulates history, mirroring real
    # repricing patterns. Lower → more concentrated at V1; higher → more revisions.
    revision_probability: float = Field(default=0.40, ge=0.0, le=0.99)


# -- Products --

class ProductsConfig(_Base):
    catalog: Literal["contoso", "synthetic", "all"] = "all"
    num_products: Optional[int] = None
    active_ratio: float = 0.98
    value_scale: float = 1.0
    price_range: List[float] = [10.0, 3000.0]
    margin_range: List[float] = [0.20, 0.35]
    brand_normalize: bool = False
    brand_normalize_alpha: float = 0.35
    # Expanded pricing dict (populated by _expand_products_pricing)
    pricing: Optional[Dict[str, Any]] = None
    # SCD Type 2 settings (nested block)
    scd2: Optional["ProductsSCD2Config"] = None


# -- Promotions --

class PromotionsConfig(_Base):
    new_customer_window_months: int = 3
    num_seasonal: Optional[int] = None
    num_clearance: Optional[int] = None
    num_limited: Optional[int] = None
    num_flash: Optional[int] = None
    num_volume: Optional[int] = None
    num_loyalty: Optional[int] = None
    num_bundle: Optional[int] = None
    num_new_customer: Optional[int] = None
    total_promotions: Optional[int] = None
    seed: Optional[int] = None
    override: Optional[Dict[str, Any]] = None
    # Parquet output knobs
    parquet_compression: str = "snappy"
    parquet_compression_level: Optional[int] = None
    force_date32: bool = True
    # Injected by dimensions_runner
    global_dates: Optional[Any] = None


# -- Returns --

class ReturnsConfig(_Base):
    enabled: bool = False
    return_rate: float = 0.03
    min_days_after_sale: int = 1
    max_days_after_sale: int = 60
    min_lag_days: int = 0


# -- Sales --

class SalesConfig(_Base):
    total_rows: int = 1_000_000
    max_lines_per_order: int = 5
    file_format: str = "parquet"
    sales_output: str = "sales"
    skip_order_cols: bool = False

    # Merge (flattened from sales.merge block)
    merge_parquet: bool = True
    merged_file: str = "sales.parquet"
    delete_chunks: bool = True

    # Partitioning (flattened from sales.partitioning block)
    partition_enabled: bool = False
    partition_cols: Optional[List[str]] = None
    partitioning: Optional[Dict[str, Any]] = None

    # Parquet: sort the merged parquet file for better downstream query perf
    # (predicate pushdown, row group skipping). Adds O(N log N) post-merge overhead.
    sort_merged_parquet: bool = False

    # Delta Lake: sort each partition part before writing. Improves downstream
    # query performance (predicate pushdown, row group skipping) but adds
    # O(N log N) overhead per part during generation. Disable for faster
    # generation when the consuming tool (Power BI, SQL Server, etc.) applies
    # its own indexes or columnstore compression.
    sort_delta_parts: bool = False

    # Performance (promoted from sales.advanced)
    chunk_size: int = 1_000_000
    workers: Optional[int] = None
    row_group_size: int = 1_000_000
    compression: str = "snappy"
    quality_report: bool = False

    # Order ID run identifier (0..999)
    order_id_run_id: Optional[int] = None
    # Advanced chunk size tuning
    tune_chunk: bool = False

    # Derived paths
    parquet_folder: str = "./data/parquet_dims"
    out_folder: str = "./data/fact_out"
    delta_output_folder: str = "./data/fact_out/delta"

    @model_validator(mode="before")
    @classmethod
    def _migrate_optimize(cls, data: Any) -> Any:
        """Migrate removed ``optimize`` to ``sort_merged_parquet``."""
        if isinstance(data, dict) and "optimize" in data:
            data = dict(data)
            if "sort_merged_parquet" not in data:
                data["sort_merged_parquet"] = data.pop("optimize")
            else:
                data.pop("optimize")
        return data


# -- Scale --

class ScalePromotionsConfig(_Base):
    seasonal: Optional[int] = None
    clearance: Optional[int] = None
    limited: Optional[int] = None
    flash: Optional[int] = None
    volume: Optional[int] = None
    loyalty: Optional[int] = None
    bundle: Optional[int] = None
    new_customer: Optional[int] = None


class ScaleProductsConfig(_Base):
    catalog: Literal["contoso", "synthetic", "all"] = "all"
    rows: Optional[int] = None


class ScaleConfig(_Base):
    sales_rows: Optional[int] = None
    products: Optional[Union[ScaleProductsConfig, int]] = None
    customers: Optional[int] = None
    stores: Optional[int] = None
    promotions: Optional[Union[ScalePromotionsConfig, Dict[str, int]]] = None


# -- Stores --

class StoreOpeningConfig(_Base):
    start: str = "2018-01-01"
    end: str = "2025-12-31"


class AssortmentCoverageConfig(_Base):
    Online: float = 1.00
    Hypermarket: float = 0.85
    Supermarket: float = 0.50
    Convenience: float = 0.25


class AssortmentConfig(_Base):
    enabled: bool = True
    seed: int = 42
    coverage: AssortmentCoverageConfig = AssortmentCoverageConfig()


class StoreClosingConfig(_Base):
    enabled: bool = True
    close_share: float = 0.10


class WarehousesConfig(_Base):
    seed: int = 42
    min_stores_per_warehouse: int = 15   # split countries above this by state
    min_stores_for_own_warehouse: int = 5  # merge countries below this into zone hubs


class StoresConfig(_Base):
    num_stores: Optional[int] = None
    total_stores: Optional[int] = None  # back-compat alias
    ensure_iso_coverage: bool = True
    district_size: int = 10
    districts_per_region: int = 8
    opening: StoreOpeningConfig = StoreOpeningConfig()
    closing_end: str = "2028-12-31"
    closing: StoreClosingConfig = StoreClosingConfig()
    assortment: AssortmentConfig = AssortmentConfig()
    # Store attribute config
    square_footage: Optional[Dict[str, Any]] = None
    staffing_ranges: Optional[Dict[str, Any]] = None
    region_weights: Optional[Dict[str, float]] = None  # currency code → fraction of stores
    use_name_pools: bool = True
    # Online store settings
    online_stores: Optional[int] = None      # explicit count carved from num_stores
    # Parquet output knobs
    parquet_compression: str = "snappy"
    parquet_compression_level: Optional[int] = None
    force_date32: bool = True
    # Injected by dimensions_runner
    global_dates: Optional[Any] = None


# -- Wishlists --

class WishlistsConfig(_Base):
    enabled: bool = False
    participation_rate: float = 0.35
    avg_items: float = 3.5
    max_items: int = 20
    pre_browse_days: int = 90
    affinity_strength: float = 0.6
    conversion_rate: float = 0.30
    seed: Optional[int] = 500
    write_chunk_rows: int = 250_000


# -- Complaints --

class ComplaintsConfig(_Base):
    enabled: bool = False
    complaint_rate: float = 0.03
    repeat_complaint_rate: float = 0.15
    max_complaints: int = 5
    resolution_rate: float = 0.85
    escalation_rate: float = 0.10
    avg_response_days: int = 5
    max_response_days: int = 30
    seed: Optional[int] = 600
    write_chunk_rows: int = 250_000


# -- Subscriptions --

class SubscriptionsConfig(_Base):
    enabled: bool = False
    generate_bridge: bool = False
    participation_rate: float = 0.65
    avg_subscriptions_per_customer: float = 1.5
    max_subscriptions: int = 5
    churn_rate: float = 0.25
    trial_rate: float = 0.30
    trial_conversion_rate: float = 0.85
    trial_days: int = 14
    seed: Optional[int] = 700
    write_chunk_rows: int = 250_000
    # Injected by dimensions_runner
    global_dates: Optional[Any] = None


# =========================================================================
# config.yaml — root model
# =========================================================================

class AppConfig(_Base):
    """Root Pydantic model for config.yaml.

    All sections are optional with sensible defaults so the model
    can validate partial configs and configs at various stages of the
    normalization pipeline.
    """

    model_config = ConfigDict(
        extra="allow",          # tolerate runtime-injected keys
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    scale: Optional[ScaleConfig] = None
    defaults: DefaultsConfig = DefaultsConfig()

    sales: SalesConfig = SalesConfig()
    returns: ReturnsConfig = ReturnsConfig()

    products: ProductsConfig = ProductsConfig()
    customers: CustomersConfig = CustomersConfig()
    subscriptions: SubscriptionsConfig = SubscriptionsConfig()
    wishlists: WishlistsConfig = WishlistsConfig()
    complaints: ComplaintsConfig = ComplaintsConfig()

    geography: Optional[GeographyConfig] = None
    promotions: PromotionsConfig = PromotionsConfig()
    stores: StoresConfig = StoresConfig()
    warehouses: WarehousesConfig = WarehousesConfig()
    employees: EmployeesConfig = EmployeesConfig()
    dates: DatesTableConfig = DatesTableConfig()

    exchange_rates: ExchangeRatesConfig = ExchangeRatesConfig()
    budget: BudgetConfig = BudgetConfig()
    inventory: InventoryConfig = InventoryConfig()
    packaging: PackagingConfig = PackagingConfig()

    # Dimension configs with minimal models
    currency: Optional[CurrencyConfig] = None
    employee_store_assignments: Optional[Dict[str, Any]] = None
    suppliers: Optional[Dict[str, Any]] = None
    time: Optional[Dict[str, Any]] = None
    sales_channels: Optional[Dict[str, Any]] = None
    loyalty_tiers: Optional[Dict[str, Any]] = None
    customer_acquisition_channels: Optional[Dict[str, Any]] = None
    return_reason: Optional[Dict[str, Any]] = None

    # Top-level keys set by pipeline_runner
    config_yaml_path: Optional[str] = None
    model_yaml_path: Optional[str] = None

    @classmethod
    def from_raw_dict(cls, raw: Dict[str, Any]) -> "AppConfig":
        """Build an AppConfig from a raw (pre-normalized) dict.

        This does NOT run the transformation pipeline (scale distribution,
        flattening, etc.) — that stays in config.py for now.  This method
        validates a dict that has ALREADY been through the transform pipeline.
        """
        # Strip internal normalizer metadata keys (prefixed with _)
        cleaned = _strip_internal_keys(raw)
        return cls.model_validate(cleaned)


def _strip_internal_keys(d: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively strip keys starting with '_' from nested dicts.

    These are normalizer metadata (e.g. ``_ignored_keys``) that should
    not be passed to Pydantic validation.
    """
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            out[k] = _strip_internal_keys(v)
        else:
            out[k] = v
    return out


# =========================================================================
# models.yaml — sub-models
# =========================================================================

# -- Macro Demand --

class YearLevelFactorsConfig(_Base):
    mode: str = "once"
    factor_values: List[float] = [1.0]

    @model_validator(mode="before")
    @classmethod
    def _rename_values(cls, data: Any) -> Any:
        if isinstance(data, dict) and "values" in data:
            data = dict(data)
            if "factor_values" not in data:
                data["factor_values"] = data.pop("values")
            else:
                # Deep merge can produce both keys; factor_values wins
                del data["values"]
        return data


class EarlyMonthCapConfig(_Base):
    enabled: bool = True
    max_rows_per_customer: int = 12
    redistribute_excess: bool = True


class MacroDemandConfig(_Base):
    base_level: float = 1.0
    yearly_growth: float = 0.0
    seasonality_amplitude: float = 0.0
    seasonality_phase: float = 0.0
    noise_std: float = 0.0
    row_share_of_growth: float = 1.0
    shock_probability: float = 0.0
    shock_impact: List[float] = [-0.25, -0.08]
    yoy_growth_schedule: Optional[YearLevelFactorsConfig] = None
    year_level_factors: Optional[YearLevelFactorsConfig] = None
    early_month_cap: Optional[EarlyMonthCapConfig] = None
    eligible_blend: float = 0.0
    # Trend preset system
    trend: Optional[str] = None
    monthly_seasonality: Optional[List[float]] = None
    seasonality: Optional[Any] = None
    # Derived constants (set by trend resolver, read by chunk_builder)
    bootstrap_months: Optional[int] = None
    max_distinct_ratio: Optional[float] = None
    min_distinct_customers: Optional[int] = None


# -- Quantity --

class QuantityModelConfig(_Base):
    base_poisson_lambda: float = 1.7
    min_qty: int = 1
    max_qty: int = 8
    monthly_factors: List[float] = [1.0] * 12
    noise_sigma: float = 0.12


# -- Pricing --

class InflationConfig(_Base):
    annual_rate: float = 0.05
    month_volatility_sigma: float = 0.012
    factor_clip: List[float] = [1.00, 1.30]
    volatility_seed: int = 123
    apply_with_scd2: bool = True


class MarkdownLadderEntry(_Base):
    kind: str = "none"
    value: float = 0.0
    weight: float = 1.0


class MarkdownConfig(_Base):
    enabled: bool = True
    max_pct_of_price: float = 0.50
    min_net_price: float = 0.01
    allow_negative_margin: bool = False
    ladder: List[MarkdownLadderEntry] = []


class PriceBandEntry(_Base):
    max: float
    step: float


class EndingEntry(_Base):
    value: float
    weight: float = 1.0


class PriceAppearanceUnitConfig(_Base):
    rounding: str = "floor"
    endings: List[EndingEntry] = []
    bands: List[PriceBandEntry] = []


class DiscountAppearanceConfig(_Base):
    rounding: str = "floor"
    bands: List[PriceBandEntry] = []


class AppearanceConfig(_Base):
    enabled: bool = True
    unit_price: PriceAppearanceUnitConfig = PriceAppearanceUnitConfig()
    unit_cost: PriceAppearanceUnitConfig = PriceAppearanceUnitConfig()
    discount: DiscountAppearanceConfig = DiscountAppearanceConfig()


class PricingModelsConfig(_Base):
    inflation: InflationConfig = InflationConfig()
    markdown: MarkdownConfig = MarkdownConfig()
    appearance: AppearanceConfig = AppearanceConfig()


# -- Brand Popularity --

class BrandPopularityConfig(_Base):
    enabled: bool = True
    seed: int = 123
    winner_boost: float = 2.5
    noise_sd: float = 0.15
    min_share: float = 0.02
    year_len_months: int = 12
    count_exponent: float = Field(0.25, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def _strip_removed_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            data.pop("brand_weights", None)
        return data


# -- Returns (models.yaml) --

class ReturnReasonEntry(_Base):
    key: int
    label: Optional[str] = None
    weight: float


class LagDaysConfig(_Base):
    distribution: str = "triangular"
    mode: int = 7
    split_min_gap: int = 3
    split_max_gap: int = 20


class ReturnQuantityConfig(_Base):
    full_line_probability: float = 0.85
    split_return_rate: float = 0.0
    max_splits: int = 3


class ReturnsModelsConfig(_Base):
    enabled: bool = True
    reasons: List[ReturnReasonEntry] = []
    lag_days: LagDaysConfig = LagDaysConfig()
    quantity: ReturnQuantityConfig = ReturnQuantityConfig()

    @model_validator(mode="after")
    def _validate_reason_keys(self) -> "ReturnsModelsConfig":
        if self.reasons:
            from src.defaults import RETURN_REASON_KEYS
            valid = set(RETURN_REASON_KEYS)
            invalid = [r.key for r in self.reasons if r.key not in valid]
            if invalid:
                raise ValueError(
                    f"models.yaml returns.reasons contains keys not in "
                    f"defaults.RETURN_REASONS: {invalid}. Valid: {sorted(valid)}"
                )
        return self


# -- Customers (models.yaml: injected by resolve_trend_preset) --

class SeasonalSpikeConfig(_Base):
    month: int
    boost: float


class CustomersDemandConfig(_Base):
    distinct_ratio: float = 0.55
    new_customer_share: float = 0.10
    max_new_fraction_per_month: float = 0.015
    cycle_amplitude: float = 0.35
    discovery_shape: float = 0.0
    participation_noise: float = 0.10
    seasonal_spikes: Optional[List[SeasonalSpikeConfig]] = None


# =========================================================================
# models.yaml — root model
# =========================================================================

class ModelsInnerConfig(_Base):
    """The ``models`` section inside models.yaml."""
    macro_demand: MacroDemandConfig = MacroDemandConfig()
    quantity: QuantityModelConfig = QuantityModelConfig()
    pricing: PricingModelsConfig = PricingModelsConfig()
    brand_popularity: BrandPopularityConfig = BrandPopularityConfig()
    returns: ReturnsModelsConfig = ReturnsModelsConfig()
    # Injected at runtime by resolve_trend_preset
    customers: Optional[CustomersDemandConfig] = None


class ModelsConfig(_Base):
    """Root model for models.yaml (has a single ``models`` key).

    Uses ``extra="allow"`` because the config normalizer injects keys
    like ``packaging`` into all loaded files.
    """
    model_config = ConfigDict(extra="allow")
    models: ModelsInnerConfig = ModelsInnerConfig()

    @classmethod
    def from_raw_dict(cls, raw: Dict[str, Any]) -> "ModelsConfig":
        return cls.model_validate(raw)
