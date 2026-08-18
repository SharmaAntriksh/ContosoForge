"""Sales runtime state + schema binding.

This module is imported by worker processes; keep it lightweight and deterministic.

The ``State`` class is the canonical process-local singleton for the sales
workers: it is populated once per worker by ``bind_globals`` and then read (never
reassigned) during chunk processing. Each worker process has its own ``State``.
"""
from __future__ import annotations


import numpy as np

try:
    import pyarrow as pa  # type: ignore
except ImportError:  # pragma: no cover
    pa = None

from src.exceptions import SalesError
from src.tools.sql.dialect import ColumnSpec, SqlType
from src.utils.static_schemas import get_sales_schema

PA_AVAILABLE = pa is not None


def _add_calendar_months_epoch_days(
    start_days: np.ndarray,
    months: int,
) -> np.ndarray:
    """Add whole calendar months to epoch-day values, clamping month-end.

    ``datetime64[M]`` supplies a vectorized Gregorian month grid. Unknown keys
    use INT64_MIN and remain unknown in the returned dense lookup.
    """
    values = np.asarray(start_days, dtype=np.int64)
    unknown = np.iinfo(np.int64).min
    result = np.full(values.shape, unknown, dtype=np.int64)
    valid = values != unknown
    if not valid.any():
        return result

    start_date = values[valid].astype("datetime64[D]")
    start_month = start_date.astype("datetime64[M]")
    start_month_day = start_month.astype("datetime64[D]")
    day_of_month = (start_date - start_month_day).astype(np.int64) + 1

    target_month_int = start_month.astype(np.int64) + max(0, int(months))
    target_month = target_month_int.astype("datetime64[M]")
    target_month_day = target_month.astype("datetime64[D]")
    next_month_day = (
        (target_month_int + 1).astype("datetime64[M]").astype("datetime64[D]")
    )
    days_in_target_month = (next_month_day - target_month_day).astype(np.int64)
    target_day = np.minimum(day_of_month, days_in_target_month)
    result[valid] = (
        target_month_day + (target_day - 1).astype("timedelta64[D]")
    ).astype(np.int64)
    return result


# ===============================================================
# Schema helpers
# ===============================================================

# DATETIME/DATETIME2 collapse to date32 to match legacy chunk_builder dtype.
# TIME has no temporal counterpart in chunk_builder output, so it falls
# through to pa.string() like the original substring-based implementation.
def _build_sql_to_pa_map():
    return {
        SqlType.BIGINT: pa.int64(),
        SqlType.INT: pa.int32(),
        SqlType.SMALLINT: pa.int16(),
        SqlType.TINYINT: pa.int8(),
        SqlType.FLOAT: pa.float64(),
        SqlType.DECIMAL: pa.float64(),
        SqlType.DATE: pa.date32(),
        SqlType.DATETIME: pa.date32(),
        SqlType.DATETIME2: pa.date32(),
    }


def _spec_to_pa_type(spec: ColumnSpec):
    if not isinstance(spec, ColumnSpec):
        raise TypeError(f"Expected ColumnSpec, got {type(spec).__name__}: {spec!r}")
    return _SQL_TO_PA.get(spec.sql_type, pa.string())


def _logical_to_arrow_schema(logical_schema):
    """Convert logical (name, ColumnSpec) schema into a PyArrow schema."""
    if not PA_AVAILABLE:
        raise SalesError("pyarrow is required to build Arrow schema")

    return pa.schema(
        [pa.field(str(name), _spec_to_pa_type(spec)) for name, spec in logical_schema]
    )


_SQL_TO_PA = _build_sql_to_pa_map() if PA_AVAILABLE else {}



# ===============================================================
# Global Sales runtime state (process-local)
# ===============================================================

class State:
    """
    Shared global state for Sales runtime only.

    Holds cached dimension data, promotion context, and output configuration.

    Notes:
    - Process-local (safe with multiprocessing): each worker process has its own
      ``State``, populated once by ``bind_globals`` and treated as read-only
      afterward by convention. The bound dimension/config fields must not be
      reassigned after binding; per-worker scratch (lazy caches) is the only thing
      that mutates during chunk processing.
    """

    # --------------------------------------------------------------
    # Core runtime flags / data
    # --------------------------------------------------------------
    skip_order_cols = None

    product_np = None
    active_product_np = None

    # Lifecycle-aware customer dimension arrays (aligned by row index)
    customer_keys = None
    customer_is_active_in_sales = None
    customer_start_month = None
    customer_end_month = None  # int64 with -1 meaning "no end"
    # Normalized end-month broadcast (int64, -1 == "no end"): a pure function of
    # customer_end_month, derived ONCE in bind_globals and read read-only by the
    # chunk hot path so it isn't reallocated every chunk.
    customer_end_month_norm = None
    customer_base_weight = None  # optional float64
    customer_country_id_by_pool = None  # int32 pool row -> temporal country id
    customer_gender_id_by_pool = None   # int32 pool row -> temporal gender id
    customer_country_factors = None     # float64[T, country]
    customer_gender_factors = None      # float64[T, gender]
    geo_to_customer_country_id = None   # dense GeographyKey -> temporal country id
    customer_first_eff_start_by_key = None  # dense key -> exact start epoch day
    customer_last_eff_end_by_key = None     # dense key -> exact end epoch day; max=open
    customer_new_promo_end_by_key = None    # dense key -> exact promotion deadline
    customer_loyalty_tier_by_key = None     # dense key -> LoyaltyTierKey (0=unknown)
    customer_income_score_by_key = None      # dense key -> income percentile score [-1, 1]
    customer_is_organization_by_key = None  # dense key -> 1 for organization customers
    customer_org_scale_score_by_key = None  # dense key -> company-size score [0, 1]
    customer_org_procurement_score_by_key = None  # dense key -> cadence score [0, 1]
    customer_org_preferred_category_mask_by_key = None  # dense key -> CategoryKey bitmask

    # Closed-form discovery schedule (optional): int64 array aligned with
    # customer_keys giving the month each customer first enters the sales
    # population. Built once per run and broadcast read-only; replaces the old
    # mutable per-worker ``seen_customers`` accumulator.
    customer_discovery_month = None

    # Global per-month plan. Computed once in the coordinator against
    # the GLOBAL month totals and broadcast read-only; each chunk slices a
    # contiguous band of every month's order-id space, so the per-month row curve
    # and distinct-customer curve no longer depend on chunk_size / worker count
    # (review Finding #4/#14). See chunk_builder.build_chunk_table.
    sales_rows_per_month = None        # int64[T]: rows per month
    sales_orders_per_month = None      # int64[T]: orders per month (<= rows)
    sales_distinct_target = None       # int64[T]: distinct-customer target per month
    sales_plan_seed = None             # run seed for month-pool + repeat draws
    total_chunks = None                # chunk count (index-space sharding divisor)

    date_pool = None
    date_prob = None
    store_keys = None
    store_eligible_by_month = None  # list[np.ndarray[int32]]: eligible store keys per month offset
    store_open_day = None   # np.ndarray[datetime64[D]] dense by StoreKey
    store_close_day = None  # np.ndarray[datetime64[D]] dense by StoreKey
    store_reno_start_day = None  # dense by StoreKey; far-future sentinel where no renovation
    store_reno_end_day = None    # dense by StoreKey; far-past sentinel where no renovation
    store_demand_weight = None   # dense float by StoreKey (all-ones = uniform); bound at worker init

    models_cfg = None

    # Returns policy (a ReturnsConfig, or None when returns are disabled), built
    # once in the coordinator. Replaces the old 16 loose returns_* attrs.
    returns_policy = None

    # --------------------------------------------------------------
    # Promotions
    # --------------------------------------------------------------
    promo_keys_all = None
    promo_start_all = None
    promo_end_all = None
    promo_discount_pct_all = None
    promo_discount_pct_by_key = None
    promo_audience_group = None
    new_customer_promo_keys = None
    new_customer_window_months = 3

    # --------------------------------------------------------------
    # Mappings
    # --------------------------------------------------------------
    store_to_product_rows = None  # assortment: list[StoreKey] -> np.ndarray of product row indices

    # --------------------------------------------------------------
    # Column correlation data (worker-side lookups)
    # --------------------------------------------------------------
    # Customer geography (for store geo-bias)
    customer_geo_key = None          # dense int32: customer pool index -> GeographyKey
    geo_to_country_id = None         # dense int32: GeographyKey -> country_id
    store_to_country_id = None       # dense int32: StoreKey -> country_id
    country_to_store_keys = None     # list[np.ndarray]: country_id -> store keys

    # Store type -> channel constraint
    store_channel_keys = None        # list[np.ndarray]: StoreKey -> valid ChannelKey[]
    channel_prob_by_store = None     # list[np.ndarray]: StoreKey -> probability[] (aligned with store_channel_keys)

    # Product channel eligibility (aligned with product_np rows)
    product_channel_eligible = None  # int8 2-D: (n_products, n_channel_groups) — 4 groups: store/online/marketplace/b2b
    product_price_score = None       # product-row-aligned price percentile [-1, 1]
    product_category_key = None      # product-row-aligned CategoryKey

    # Promotion channel category
    promo_channel_group = None       # int8: per promo — 0=any, 1=physical, 2=digital

    # Channel-aware delivery
    channel_fulfillment_days = None  # dense int32: ChannelKey -> typical fulfillment days

    # --------------------------------------------------------------
    # Budget streaming aggregation (worker-side lookups)
    # --------------------------------------------------------------
    budget_enabled = None
    budget_store_to_country = None   # dense int32 array: StoreKey -> country_id
    budget_product_to_cat = None     # dense int32 array: ProductKey -> category_id

    store_to_geo_arr = None
    geo_to_currency_arr = None

    # (kept for compatibility; may be passed as dicts too)
    store_to_geo = None
    geo_to_currency = None

    # --------------------------------------------------------------
    # SCD2 version lookup tables (per-entity, per-row resolution)
    # --------------------------------------------------------------
    product_scd2_active = None      # bool
    product_scd2_starts = None      # np.ndarray (N_pool, max_ver) — version start epoch days
    product_scd2_data = None        # np.ndarray (N_pool, max_ver, 3) — ProductKey/ListPrice/UnitCost
    customer_scd2_active = None     # bool
    customer_scd2_starts = None     # np.ndarray (N_pool, max_ver) — version start epoch days
    customer_scd2_keys = None       # np.ndarray (N_pool, max_ver) — CustomerKey per version
    customer_scd2_geography = None  # np.ndarray (N_pool, max_ver) — GeographyKey per version
    cust_key_to_pool_idx = None     # np.ndarray (max_key+1,) — IsCurrent CustomerKey → pool index

    # --------------------------------------------------------------
    # Output configuration
    # --------------------------------------------------------------
    file_format = None
    out_folder = None

    # CRITICAL: constant per-run stride for chunk order-id ranges.
    # Also controls output chunking (row count per chunk file).
    # (task.py validates this; chunk_builder uses it to avoid overlaps)
    chunk_size = None

    row_group_size = None
    compression = None

    # Day-based order ID ranges (ensures OrderNumber ~ OrderDate)
    order_id_day_stride = None         # total ID space per day (num_chunks * per_chunk_alloc)
    per_chunk_alloc = None             # ID slots each chunk owns within a day
    order_id_int64 = False             # emit OrderNumber as int64 (large runs)

    # used by task.py when deciding to drop order cols in Sales output
    skip_order_cols_requested = None
    
    max_lines_per_order = 6

    # parquet tuning
    parquet_dict_exclude = None

    # --------------------------------------------------------------
    # Delta options
    # --------------------------------------------------------------
    no_discount_key = None
    delta_output_folder = None

    # --------------------------------------------------------------
    # Partitioning
    # --------------------------------------------------------------
    partition_enabled = None
    partition_cols = None

    # --------------------------------------------------------------
    # Schema (bound once per run)
    # --------------------------------------------------------------
    sales_schema = None

    # These may be injected by worker init for debugging/inspection.
    schema_no_order = None
    schema_with_order = None
    schema_no_order_delta = None
    schema_with_order_delta = None

    # --------------------------------------------------------------
    # Attributes bind_globals sets from the worker init gdict. These were bound
    # dynamically but never declared, so they read as "phantom" attributes. They
    # are declared here so the bind_globals allowlist (below) accepts them and
    # the class body is the single source of the bound surface. Every one is set
    # unconditionally by init's gdict before any chunk read, so the None default
    # is a pre-bind placeholder only (the getattr(..., <default>) reads that
    # remain are post-bind defensive fallbacks).
    # --------------------------------------------------------------
    brand_to_row_idx = None
    brand_prob_by_month = None
    product_brand_key = None
    product_category_id_by_pool = None
    product_brand_id_by_pool = None
    product_color_id_by_pool = None
    product_category_factors = None
    product_brand_factors = None
    product_color_factors = None
    product_popularity = None
    product_seasonality = None
    product_return_risk_by_key = None
    product_defect_risk_by_key = None
    product_subcat_key = None
    promo_salience_all = None
    _channel_to_elig_group = None
    sales_output = None
    output_paths = None
    parquet_folder = None
    validate_header_invariants = None
    salesperson_effective_by_store = None
    salesperson_global_pool = None
    inventory_enabled = None
    inventory_store_to_warehouse = None
    wishlists_enabled = None
    complaints_enabled = None
    # Output-schema / dict-encoding descriptors carried from the schema bundle.
    date_cols_by_table = None
    schema_by_table = None
    parquet_dict_cols = None
    parquet_dict_cols_by_table = None

    # --------------------------------------------------------------
    # Lifecycle helpers
    # --------------------------------------------------------------
    @staticmethod
    def reset():
        """
        Reset all State fields.
        Intended for tests / development only.
        """
        for key in list(vars(State).keys()):
            if key.startswith("__"):
                continue
            attr = getattr(State, key)
            if callable(attr):
                continue
            setattr(State, key, None)

        # Also clear the module-level scratch cache derived from State.models_cfg
        # (the lines-per-order month-demand curve), so a between-run reset in the
        # same process starts clean. Function-local import breaks the
        # globals <-> core.orders import cycle.
        from .core.orders import _reset_month_demand
        _reset_month_demand()


# ===============================================================
# Binding
# ===============================================================

# The attributes bind_globals is permitted to set: exactly the data attributes
# declared on the State class body. Computed once at import (before any bind, so
# no dynamic setattr can widen it) — the class body is the single source of the
# bindable surface. A gdict key outside this set means someone added a bound
# value without declaring it, which would otherwise surface only as a phantom
# read deep in a worker.
_ALLOWED_STATE_ATTRS = frozenset(
    k for k in vars(State)
    if not k.startswith("__") and not callable(getattr(State, k))
)


def bind_globals(gdict: dict):
    """
    Bind values into State and finalize the Sales Arrow schema.

    Must be called before workers start (per-process).
    """
    if not isinstance(gdict, dict):
        raise TypeError("bind_globals expects a dict")

    # Bind raw values, rejecting any key not declared on the State class.
    for k, v in gdict.items():
        if k not in _ALLOWED_STATE_ATTRS:
            raise SalesError(
                f"bind_globals received an undeclared State attribute: {k!r}. "
                "Declare it on the State class — the bind allowlist is the "
                "declared class surface."
            )
        setattr(State, k, v)

    # --------------------------------------------------------------
    # Derive the normalized end-month broadcast ONCE per (re)bind so the chunk
    # hot path reads it instead of reallocating it every chunk. It is a pure
    # function of customer_end_month (already int64/-1 upstream), so this is
    # byte-identical to the old per-chunk _normalize_end_month call. Only
    # (re)compute when this bind actually touches the customer arrays; the
    # import is function-local to avoid the core -> ..globals.fmt import cycle.
    # --------------------------------------------------------------
    if ("customer_end_month" in gdict or "customer_keys" in gdict) and State.customer_keys is not None:
        from .core.customer_sampling import _normalize_end_month
        _n_cust = int(np.asarray(State.customer_keys).shape[0])
        State.customer_end_month_norm = _normalize_end_month(State.customer_end_month, _n_cust)

    # Derive exact New Customer deadlines once at worker bind. This is a pure
    # function of the read-only start-date lookup and configured calendar-month
    # window, so workers and chunks see identical boundaries.
    if (
        "customer_first_eff_start_by_key" in gdict
        or "new_customer_window_months" in gdict
    ):
        if State.customer_first_eff_start_by_key is None:
            State.customer_new_promo_end_by_key = None
        else:
            State.customer_new_promo_end_by_key = _add_calendar_months_epoch_days(
                State.customer_first_eff_start_by_key,
                int(State.new_customer_window_months or 0),
            )

    # --------------------------------------------------------------
    # Bind Sales schema ONCE, respecting skip_order_cols
    # (worker may pass an explicit sales_schema; if so, don't override)
    # --------------------------------------------------------------
    if PA_AVAILABLE and State.sales_schema is None:
        if State.skip_order_cols is None:
            raise SalesError("skip_order_cols must be bound before Sales schema initialization")

        logical_schema = get_sales_schema(bool(State.skip_order_cols))
        State.sales_schema = _logical_to_arrow_schema(logical_schema)


# ===============================================================
# Date formatting
# ===============================================================

def fmt(dt):
    """
    Format datetime64[D] as YYYYMMDD string array (fast path).

    Accepts scalar or array-like.
    """
    d = np.asarray(dt).astype("datetime64[D]", copy=False)

    # Extract Y/M/D in a vectorized way
    y = d.astype("datetime64[Y]").astype("int64") + 1970
    m = (
        d.astype("datetime64[M]").astype("int64")
        - d.astype("datetime64[Y]").astype("datetime64[M]").astype("int64")
        + 1
    )
    day = (d - d.astype("datetime64[M]")).astype("timedelta64[D]").astype("int64") + 1

    yyyymmdd = (y * 10000 + m * 100 + day).astype("int64")
    return yyyymmdd.astype(str)


__all__ = [
    "State",
    "bind_globals",
    "fmt",
    "PA_AVAILABLE",
]
