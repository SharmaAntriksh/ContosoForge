"""Sales runtime state + schema binding.

This module is imported by worker processes; keep it lightweight and deterministic.

The ``State`` class remains the canonical process-local singleton for
multiprocessing workers.  ``SalesContext`` is the new dependency-injection
friendly dataclass that makes dependencies explicit.  Use
``SalesContext.from_state()`` to snapshot the current ``State`` into a
context object, which can then be passed through function parameters
instead of relying on the global.

Migration path:
    1. New/refactored functions accept ``ctx: SalesContext`` as their
       first parameter.
    2. Legacy code continues to read from ``State`` directly.
    3. Over time, functions are converted to use ``ctx`` and ``State``
       access is phased out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

try:
    import pyarrow as pa  # type: ignore
except ImportError:  # pragma: no cover
    pa = None

from src.tools.sql.dialect import ColumnSpec, SqlType
from src.utils.static_schemas import get_sales_schema

PA_AVAILABLE = pa is not None


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
        raise RuntimeError("pyarrow is required to build Arrow schema")

    return pa.schema(
        [pa.field(str(name), _spec_to_pa_type(spec)) for name, spec in logical_schema]
    )


_SQL_TO_PA = _build_sql_to_pa_map() if PA_AVAILABLE else {}


# ===============================================================
# Dependency-injection context (explicit alternative to State)
# ===============================================================

@dataclass
class SalesContext:
    """Explicit, testable container for all sales worker dependencies.

    Every field that ``State`` exposes as a class variable is represented
    here as a typed dataclass field.  Use ``SalesContext.from_state()``
    to snapshot the current global ``State`` into a portable context.
    """

    # -- Dimension data --
    product_np: Any = None
    active_product_np: Any = None
    customer_keys: Any = None
    customer_is_active_in_sales: Any = None
    customer_start_month: Any = None
    customer_end_month: Any = None
    customer_base_weight: Any = None
    customer_discovery_month: Any = None   # int64 pool-aligned: month each customer first enters sales
    # -- Global per-month plan: computed once in the coordinator,
    #    broadcast read-only, and sliced per chunk so the per-month row curve and
    #    distinct-customer curve are independent of chunk_size / worker count. --
    sales_rows_per_month: Any = None       # int64[T]: global rows per month
    sales_orders_per_month: Any = None     # int64[T]: global orders per month
    sales_distinct_target: Any = None      # int64[T]: distinct-customer target per month
    sales_plan_seed: Optional[int] = None  # run seed for month-pool + repeat draws
    total_chunks: Optional[int] = None     # chunk count (for index-space sharding)
    date_pool: Any = None
    date_prob: Any = None
    store_keys: Any = None
    store_eligible_by_month: Any = None
    store_open_day: Any = None   # np.ndarray[datetime64[D]] dense by StoreKey
    store_close_day: Any = None  # np.ndarray[datetime64[D]] dense by StoreKey
    store_reno_start_day: Any = None  # dense by StoreKey; far-future sentinel where no renovation
    store_reno_end_day: Any = None    # dense by StoreKey; far-past sentinel where no renovation
    store_demand_weight: Any = None   # dense float by StoreKey (all-ones = uniform); bound at worker init

    # -- Promotions --
    promo_keys_all: Any = None
    promo_start_all: Any = None
    promo_end_all: Any = None
    new_customer_promo_keys: Any = None
    new_customer_window_months: int = 3

    # -- Mappings --
    store_to_product_rows: Any = None
    store_to_geo_arr: Any = None
    geo_to_currency_arr: Any = None
    models_cfg: Optional[Dict[str, Any]] = None

    # -- Column correlations --
    customer_geo_key: Any = None
    geo_to_country_id: Any = None
    store_to_country_id: Any = None
    country_to_store_keys: Any = None
    store_channel_keys: Any = None
    channel_prob_by_store: Any = None
    product_channel_eligible: Any = None
    promo_channel_group: Any = None
    channel_fulfillment_days: Any = None

    # -- SCD2 version lookup tables (per-entity, per-row resolution) --
    product_scd2_active: bool = False
    product_scd2_starts: Any = None     # (N_pool, max_ver) int64: version start epoch days
    product_scd2_data: Any = None       # (N_pool, max_ver, 3) float64: ProductKey/ListPrice/UnitCost
    customer_scd2_active: bool = False
    customer_scd2_starts: Any = None    # (N_pool, max_ver) int64: version start epoch days
    customer_scd2_keys: Any = None      # (N_pool, max_ver) int32: CustomerKey per version
    cust_key_to_pool_idx: Any = None    # dense int32: IsCurrent CustomerKey → pool index
    customer_first_eff_start_by_key: Any = None  # dense int64: CustomerKey → first EffectiveStartDate epoch days; INT64_MIN for unknown keys

    # -- Output config --
    file_format: Optional[str] = None
    out_folder: Optional[str] = None
    chunk_size: Optional[int] = None
    row_group_size: Optional[int] = None
    compression: Optional[str] = None
    order_id_stride_orders: Optional[int] = None
    skip_order_cols: Optional[bool] = None
    skip_order_cols_requested: Optional[bool] = None
    max_lines_per_order: int = 6

    # -- Delta / partitioning --
    no_discount_key: Any = None
    delta_output_folder: Optional[str] = None
    write_delta: Optional[bool] = None
    partition_enabled: Optional[bool] = None
    partition_cols: Optional[List[str]] = None

    # -- Budget --
    budget_enabled: Optional[bool] = None
    budget_store_to_country: Any = None
    budget_product_to_cat: Any = None

    # -- Schema --
    sales_schema: Any = None

    @classmethod
    def from_state(cls) -> "SalesContext":
        """Snapshot the current ``State`` singleton into a ``SalesContext``."""
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {}
        for name in fields:
            val = getattr(State, name, None)
            if val is not None:
                kwargs[name] = val
        return cls(**kwargs)


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

    # Backward-compat customer key pool
    customers = None

    # New lifecycle-aware customer dimension arrays (aligned by row index)
    customer_keys = None
    customer_is_active_in_sales = None
    customer_start_month = None
    customer_end_month = None  # int64 with -1 meaning "no end"
    customer_base_weight = None  # optional float64

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
    # --------------------------------------------------------------
    # Promotions
    # --------------------------------------------------------------
    promo_keys_all = None
    promo_start_all = None
    promo_end_all = None
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
    cust_key_to_pool_idx = None     # np.ndarray (max_key+1,) — IsCurrent CustomerKey → pool index
    customer_first_eff_start_by_key = None  # np.ndarray (max_key+1,) — CustomerKey → first EffectiveStartDate epoch days; INT64_MIN for unknown keys

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

    # Forward-compat aliases for OrderNumber generation
    order_id_stride_orders = None      # usually == chunk_size

    # Day-based order ID ranges (ensures OrderNumber ~ OrderDate)
    month_stride = None                # total ID space per day (num_chunks * per_chunk_alloc)
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
    write_delta = None

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


# ===============================================================
# Binding
# ===============================================================

def bind_globals(gdict: dict):
    """
    Bind values into State and finalize the Sales Arrow schema.

    Must be called before workers start (per-process).
    """
    if not isinstance(gdict, dict):
        raise TypeError("bind_globals expects a dict")

    # Bind raw values (allow injecting additional attrs for debugging)
    for k, v in gdict.items():
        setattr(State, k, v)

    # --------------------------------------------------------------
    # Bind Sales schema ONCE, respecting skip_order_cols
    # (worker may pass an explicit sales_schema; if so, don't override)
    # --------------------------------------------------------------
    if PA_AVAILABLE and State.sales_schema is None:
        if State.skip_order_cols is None:
            raise RuntimeError("skip_order_cols must be bound before Sales schema initialization")

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
    "SalesContext",
    "State",
    "bind_globals",
    "fmt",
    "PA_AVAILABLE",
]
