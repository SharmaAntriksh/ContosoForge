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

from typing import Any, Dict, List, Optional, Required, Sequence, TypedDict, Union

import numpy as np


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
    store_to_geo: Optional[Dict[int, int]]
    store_type_map: Optional[Dict[int, str]]

    # -- Promotion dimension -------------------------------------------------
    promo_keys_all: Required[np.ndarray]
    promo_start_all: Required[np.ndarray]
    promo_end_all: Required[np.ndarray]
    new_customer_promo_keys: Optional[np.ndarray]
    new_customer_window_months: int

    # -- Customer dimension --------------------------------------------------
    customers: Optional[np.ndarray]
    customer_keys: Required[np.ndarray]
    customer_is_active_in_sales: Optional[np.ndarray]
    customer_start_month: Optional[np.ndarray]
    customer_end_month: Optional[np.ndarray]
    customer_base_weight: Optional[np.ndarray]
    customer_geo_key: Optional[np.ndarray]
    customer_first_eff_start_by_key: Optional[np.ndarray]

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
    write_delta: bool
    sales_output: str
    parquet_folder: Optional[str]
    parquet_dict_exclude: Optional[Union[set, list]]

    # -- Chunking / order ID -------------------------------------------------
    chunk_size: Required[int]
    order_id_stride_orders: int
    total_rows: int
    order_id_run_id: int
    max_lines_per_order: int
    month_stride: int
    per_chunk_alloc: int
    order_id_int64: bool               # emit SalesOrderNumber as int64 (large runs)

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
    returns_enabled: bool
    returns_rate: float
    returns_min_lag_days: int
    returns_max_lag_days: int
    returns_reason_keys: Optional[Union[list, np.ndarray]]
    returns_reason_probs: Optional[Union[list, np.ndarray]]
    returns_full_line_probability: float
    returns_split_return_rate: float
    returns_max_splits: int
    returns_split_min_gap: int
    returns_split_max_gap: int
    returns_logistics_keys: list
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
    legacy_salesperson_by_store_month: bool

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
    _prebuilt_brand_to_row_idx: Any
    _prebuilt_store_to_product_rows: Any
    _prebuilt_salesperson_effective_by_store: Optional[dict]
    _prebuilt_salesperson_global_pool: Optional[np.ndarray]
    _prebuilt_brand_prob_by_month: Any
