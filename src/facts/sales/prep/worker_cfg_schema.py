"""Type contract for worker_cfg: the serialization boundary between
generate_sales_fact (producer) and init_sales_worker (consumer).

TypedDict is a plain dict at runtime — zero performance overhead, full
pickle compatibility.  Static analysis tools (pyright, mypy) catch key
typos at construction time.

After SharedArrayGroup.publish_dict(), some ndarray values become SHM
descriptor dicts on the wire.  resolve_array() in init_sales_worker
converts them back.  Types here reflect the *logical* contract
(post-resolution), not the wire format.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Required, TypedDict, Union

import numpy as np

from src.exceptions import ConfigError


class SalesWorkerCfg(TypedDict, total=False):
    """Configuration dict passed from generate_sales_fact to init_sales_worker.

    Keys marked Required are accessed via bare worker_cfg["key"] and must
    be present.  All other keys use worker_cfg.get("key", default).
    """

    # -- Product dimension ---------------------------------------------------
    product_np: Required[np.ndarray]
    product_brand_key: Optional[np.ndarray]
    brand_names: Optional[np.ndarray]
    product_popularity: Optional[np.ndarray]
    product_seasonality: Optional[np.ndarray]
    product_subcat_key: Optional[np.ndarray]
    product_channel_eligible: Optional[np.ndarray]

    # -- Store dimension -----------------------------------------------------
    store_keys: Required[np.ndarray]
    store_open_month: Optional[np.ndarray]
    store_close_month: Optional[np.ndarray]
    store_open_day: Optional[np.ndarray]
    store_close_day: Optional[np.ndarray]
    store_reno_start_day: Optional[np.ndarray]
    store_reno_end_day: Optional[np.ndarray]
    store_demand_weight: Optional[np.ndarray]
    store_to_geo: Optional[Dict[int, int]]
    store_type_map: Optional[Dict[int, str]]

    # -- Promotion dimension -------------------------------------------------
    promo_keys_all: Required[np.ndarray]
    promo_start_all: Required[np.ndarray]
    promo_end_all: Required[np.ndarray]
    promo_salience_all: Optional[np.ndarray]
    new_customer_promo_keys: Optional[np.ndarray]
    new_customer_window_months: int

    # -- Customer dimension --------------------------------------------------
    customer_keys: Required[np.ndarray]
    customer_is_active_in_sales: Optional[np.ndarray]
    customer_start_month: Optional[np.ndarray]
    customer_end_month: Optional[np.ndarray]
    customer_base_weight: Optional[np.ndarray]
    customer_discovery_month: Optional[np.ndarray]
    customer_geo_key: Optional[np.ndarray]
    customer_first_eff_start_by_key: Optional[np.ndarray]

    # -- Global per-month plan -----------------------------------------------
    sales_rows_per_month: Optional[np.ndarray]     # int64[T]: rows per month
    sales_orders_per_month: Optional[np.ndarray]   # int64[T]: orders per month
    sales_distinct_target: Optional[np.ndarray]    # int64[T]: distinct target
    sales_plan_seed: int                           # run seed for month pools
    total_chunks: int                              # index-space sharding divisor

    # -- Geography / currency ------------------------------------------------
    geo_to_currency: Optional[Dict[int, int]]
    geo_to_country_id: Optional[np.ndarray]
    store_to_country_id: Optional[np.ndarray]
    country_to_store_keys: Optional[List[np.ndarray]]

    # -- Date / time ---------------------------------------------------------
    date_pool: Required[np.ndarray]
    date_prob: Required[np.ndarray]

    # -- Output configuration ------------------------------------------------
    output_paths: Required[Dict[str, Any]]
    file_format: str
    out_folder: str
    row_group_size: int
    compression: str
    delta_output_folder: Optional[str]
    sales_output: str
    parquet_folder: Optional[str]
    parquet_dict_exclude: Optional[Union[set, list]]

    # -- Chunking / order ID -------------------------------------------------
    chunk_size: Required[int]
    total_rows: int
    order_id_run_id: int
    max_lines_per_order: int
    order_id_day_stride: int
    per_chunk_alloc: int
    order_id_int64: bool               # emit OrderNumber as int64 (large runs)

    # -- Column control ------------------------------------------------------
    no_discount_key: Required[int]
    skip_order_cols: Required[bool]
    skip_order_cols_requested: bool
    validate_header_invariants: bool

    # -- Partitioning --------------------------------------------------------
    partition_enabled: bool
    partition_cols: List[str]

    # -- Models --------------------------------------------------------------
    models_cfg: Any

    # -- Returns -------------------------------------------------------------
    # The base ReturnsConfig, built once in the coordinator (None when returns
    # are disabled). The per-chunk event_key_offset/capacity is applied in the
    # worker task; the capacity stride is kept as its own scalar because it is
    # chunking arithmetic, not returns policy.
    returns_policy: Any
    returns_event_key_capacity: int

    # -- Employee / salesperson ----------------------------------------------
    seed_master: int
    employee_salesperson_seed: int
    employee_primary_boost: float
    employee_assign_store_key: Optional[np.ndarray]
    employee_assign_employee_key: Optional[np.ndarray]
    employee_assign_start_date: Optional[np.ndarray]
    employee_assign_end_date: Optional[np.ndarray]
    employee_assign_fte: Optional[np.ndarray]
    employee_assign_is_primary: Optional[np.ndarray]
    employee_assign_role: Optional[np.ndarray]
    salesperson_roles: List[str]
    salesperson_perf_spread: float    # lognormal sigma for per-rep performance; 0 = off
    salesperson_perf_seed: int        # stable seed for the per-employee multiplier

    # -- Channel correlation -------------------------------------------------
    store_channel_keys: Optional[list]
    channel_prob_by_store: Optional[list]
    promo_channel_group: Optional[np.ndarray]
    channel_fulfillment_days: Optional[np.ndarray]
    _channel_to_elig_group: Optional[np.ndarray]

    # -- SCD2 versioning -----------------------------------------------------
    product_scd2_active: bool
    product_scd2_starts: Optional[np.ndarray]
    product_scd2_data: Optional[np.ndarray]
    customer_scd2_active: bool
    customer_scd2_starts: Optional[np.ndarray]
    customer_scd2_keys: Optional[np.ndarray]
    cust_key_to_pool_idx: Optional[np.ndarray]

    # -- Assortment (post-construction) --------------------------------------
    assortment: Optional[dict]
    _assortment_subcat_matrix: Any   # ndarray or SHM descriptor
    _assortment_unique_subcats: Any  # ndarray or SHM descriptor

    # -- Budget (post-construction) ------------------------------------------
    budget_enabled: bool
    budget_store_to_country: Optional[np.ndarray]
    budget_product_to_cat: Optional[np.ndarray]

    # -- Inventory (post-construction) ---------------------------------------
    inventory_enabled: bool
    inventory_store_to_warehouse: Optional[np.ndarray]

    # -- Wishlists (post-construction) ---------------------------------------
    wishlists_enabled: bool

    # -- Complaints (post-construction) --------------------------------------
    complaints_enabled: bool

    # -- Pre-built derived structures (post-construction, SHM descriptors) ---
    _brand_flat_idx: Any
    _brand_flat_offsets: Any
    _prebuilt_salesperson_effective_by_store: Optional[dict]
    _prebuilt_salesperson_global_pool: Optional[np.ndarray]
    _prebuilt_brand_prob_by_month: Any


# Fields the coordinator moves into OS shared memory before spawning the pool
# (SharedArrayGroup.publish_dict). Each large ndarray value is swapped in-place
# for a pickle-safe descriptor; workers resolve it back to a zero-copy view via
# resolve_array. Colocated with the schema so this is the single source of truth
# for "which worker_cfg fields are broadcast" rather than a list hand-maintained
# in generate_sales_fact. Small arrays / None pass through untouched, and key
# order is irrelevant (each field maps to its own uniquely-named block). This is
# the BASE broadcast only; the derived prebuilds (_brand_flat_*, _assortment_*,
# _prebuilt_*) publish their own blocks inside _prebuild_shared_structures.
SHARED_FIELDS: List[str] = [
    "product_np",
    "product_brand_key",
    "customer_keys",
    "customer_is_active_in_sales",
    "customer_start_month",
    "customer_end_month",
    "customer_base_weight",
    "customer_discovery_month",
    "customer_first_eff_start_by_key",
    "product_subcat_key",
    "product_popularity",
    "date_pool",
    "date_prob",
    "promo_keys_all",
    "promo_start_all",
    "promo_end_all",
    "employee_assign_store_key",
    "employee_assign_employee_key",
    "employee_assign_start_date",
    "employee_assign_end_date",
    "employee_assign_fte",
    "employee_assign_is_primary",
    "product_seasonality",
    "customer_geo_key",
    "geo_to_country_id",
    "store_to_country_id",
    "product_channel_eligible",
    "promo_channel_group",
    "channel_fulfillment_days",
    "_channel_to_elig_group",
    "product_scd2_starts",
    "product_scd2_data",
    "customer_scd2_starts",
    "customer_scd2_keys",
    "cust_key_to_pool_idx",
]


# The Required[...] fields above, as an explicit list. `from __future__ import
# annotations` stringizes the annotations, so TypedDict's runtime
# __required_keys__ comes back empty (total=False makes every stringized field
# look optional); mirror the Required[...] markers here and keep the two in
# sync. These are the fields init_sales_worker reads via bare worker_cfg["key"];
# a missing one would otherwise surface as a KeyError (or a silent None bound
# onto State) deep inside chunk processing.
REQUIRED_FIELDS: List[str] = [
    "product_np",
    "store_keys",
    "promo_keys_all",
    "promo_start_all",
    "promo_end_all",
    "customer_keys",
    "date_pool",
    "date_prob",
    "output_paths",
    "chunk_size",
    "no_discount_key",
    "skip_order_cols",
]


def validate_worker_cfg(worker_cfg: Mapping[str, Any]) -> None:
    """Fail loud, naming the field, if a Required worker_cfg field is missing.

    This is the from-wire completeness check: init_sales_worker calls it once
    after the wire hop so a typo'd or dropped field aborts at pool spawn instead
    of surfacing as ``State.x is None`` deep inside chunk processing. SHM
    publication swaps Required ndarray *values* for descriptors but never removes
    the key, so a plain membership test is valid before or after resolution.
    """
    missing = [k for k in REQUIRED_FIELDS if k not in worker_cfg]
    if missing:
        raise ConfigError(
            "worker_cfg is missing required field(s): " + ", ".join(missing)
        )
