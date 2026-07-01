"""Worker-config and accumulator setup for the sales fact.

``_build_worker_cfg`` assembles the flat, picklable ``SalesWorkerCfg`` dict the
pool workers receive. ``_setup_accumulators`` wires up the optional streamed facts
(budget / inventory / wishlists / complaints) when enabled and available.

The optional-accumulator imports are guarded (``try/except ImportError``) so the
sales fact still runs when a secondary-fact package is absent.
``_compute_promo_salience`` is imported from ``.dimension_loaders`` (not
``sales.py``) so the import graph stays acyclic.
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from src.utils.config_helpers import (
    bool_or as _bool_or,
    int_or as _int_or,
    str_or as _str_or,
)
from src.utils.logging_utils import info

from .dimension_loaders import _compute_promo_salience
from ..sales_logic import State
from .worker_cfg_schema import SalesWorkerCfg

# Budget streaming aggregation (lazy import to avoid hard dependency)
try:
    from src.facts.budget.lookups import build_budget_lookups
    from src.facts.budget.accumulator import BudgetAccumulator
    _BUDGET_AVAILABLE = True
except ImportError:
    _BUDGET_AVAILABLE = False

try:
    from src.facts.inventory.accumulator import InventoryAccumulator
    _INVENTORY_AVAILABLE = True
except ImportError:
    _INVENTORY_AVAILABLE = False

try:
    from src.facts.wishlists.accumulator import WishlistAccumulator
    _WISHLISTS_AVAILABLE = True
except ImportError:
    _WISHLISTS_AVAILABLE = False

try:
    from src.facts.complaints.accumulator import ComplaintsAccumulator
    _COMPLAINTS_AVAILABLE = True
except ImportError:
    _COMPLAINTS_AVAILABLE = False


def _build_worker_cfg(
    cust, prod, stores, emps, corr, promos,
    cfg, sales_cfg, output_paths, out_folder_p,
    file_format, chunk_size, total_rows, seed, order_id_run_id,
    sales_output, skip_order_cols, write_delta, delta_output_folder,
    partition_enabled, partition_cols, row_group_size, compression,
    returns_enabled_effective, returns_rate,
    returns_min_lag_days, returns_max_lag_days,
    returns_reason_keys, returns_reason_probs,
    returns_full_line_prob, returns_split_rate,
    returns_max_splits, returns_split_min_gap, returns_split_max_gap,
    returns_lag_distribution, returns_lag_mode,
    returns_logistics_keys, returns_event_key_capacity,
    month_stride=0, per_chunk_alloc=0, order_id_int64=False,
    total_chunks=1, returns_reconcile_cents=False,
):
    """Build the worker_cfg dict from typed containers."""
    # Salesperson performance spread (Part 2): resolved once here so both the
    # main-process prebuild and the worker-side fallback use identical values.
    # Seed is derived from the run seed so it tracks defaults.seed / random mode.
    _sp_perf = getattr(sales_cfg, "salesperson_performance", None)
    _perf_on = bool(getattr(_sp_perf, "enabled", False)) if _sp_perf is not None else False
    _perf_spread = float(getattr(_sp_perf, "spread", 0.0)) if (_sp_perf is not None and _perf_on) else 0.0
    _perf_seed = int(seed) ^ 0x5A1E5

    worker_cfg: SalesWorkerCfg = SalesWorkerCfg(
        product_np=prod["product_np"],
        product_brand_key=prod["product_brand_key"],
        brand_names=prod["brand_names"],
        store_keys=stores["store_keys"],
        store_open_month=stores["store_open_month"],
        store_close_month=stores["store_close_month"],
        store_open_day=stores["store_open_day"],
        store_close_day=stores["store_close_day"],
        store_reno_start_day=stores["store_reno_start_day"],
        store_reno_end_day=stores["store_reno_end_day"],
        store_demand_weight=stores["store_demand_weight"],
        salesperson_perf_spread=_perf_spread,
        salesperson_perf_seed=_perf_seed,
        promo_keys_all=promos["promo_keys_all"],
        promo_start_all=promos["promo_start_all"],
        promo_end_all=promos["promo_end_all"],
        # Per-promo salience weights (None => uniform promo draw).
        promo_salience_all=_compute_promo_salience(
            promos["promo_df"], promos["promo_keys_all"],
            getattr(State, "models_cfg", None),
        ),
        new_customer_promo_keys=promos["new_customer_promo_keys"],
        new_customer_window_months=int((getattr(cfg, "promotions", None) or {}).get("new_customer_window_months", 3)),

        customers=cust["customer_keys"],
        customer_keys=cust["customer_keys"],
        customer_is_active_in_sales=cust["is_active_in_sales"],
        customer_start_month=cust["customer_start_month"],
        customer_end_month=cust["customer_end_month"],
        customer_base_weight=cust["customer_base_weight"],
        customer_discovery_month=cust.get("customer_discovery_month"),
        customer_first_eff_start_by_key=cust["customer_first_eff_start_by_key"],

        # Global per-month plan: tiny length-T arrays + scalars.
        sales_rows_per_month=cust.get("sales_rows_per_month"),
        sales_orders_per_month=cust.get("sales_orders_per_month"),
        sales_distinct_target=cust.get("sales_distinct_target"),
        sales_plan_seed=int(seed),
        total_chunks=int(total_chunks),

        store_to_geo=stores["store_to_geo"],
        geo_to_currency=stores["geo_to_currency"],
        date_pool=prod["date_pool"],
        date_prob=prod["date_prob"],

        output_paths={
            "file_format": output_paths.file_format,
            "out_folder": output_paths.out_folder,
            "merged_file": output_paths.merged_file,
            "delta_output_folder": output_paths.delta_output_folder,
        },
        file_format=file_format,
        out_folder=str(out_folder_p),
        row_group_size=_int_or(row_group_size, 2_000_000),
        compression=_str_or(compression, "snappy"),

        chunk_size=int(chunk_size),
        order_id_stride_orders=int(chunk_size),
        total_rows=int(total_rows),
        order_id_run_id=int(order_id_run_id),
        month_stride=int(month_stride),
        per_chunk_alloc=int(per_chunk_alloc),
        order_id_int64=bool(order_id_int64),
        max_lines_per_order=int(getattr(sales_cfg, "max_lines_per_order", 5) or 5),

        sales_output=sales_output,
        no_discount_key=1,

        validate_header_invariants=_bool_or(
            getattr(sales_cfg, "validate_header_invariants", None), False
        ),

        delta_output_folder=delta_output_folder,
        write_delta=write_delta,
        skip_order_cols=bool(skip_order_cols),
        skip_order_cols_requested=bool(skip_order_cols),

        partition_enabled=partition_enabled,
        partition_cols=partition_cols,

        models_cfg=State.models_cfg,

        returns_enabled=bool(returns_enabled_effective),
        returns_rate=float(returns_rate),
        returns_min_lag_days=int(returns_min_lag_days),
        returns_max_lag_days=int(returns_max_lag_days),
        returns_reason_keys=returns_reason_keys,
        returns_reason_probs=returns_reason_probs,
        returns_full_line_probability=float(returns_full_line_prob),
        returns_split_return_rate=float(returns_split_rate),
        returns_max_splits=int(returns_max_splits),
        returns_reconcile_cents=bool(returns_reconcile_cents),
        returns_split_min_gap=int(returns_split_min_gap),
        returns_split_max_gap=int(returns_split_max_gap),
        returns_lag_distribution=str(returns_lag_distribution),
        returns_lag_mode=int(returns_lag_mode),
        returns_logistics_keys=returns_logistics_keys,
        returns_event_key_capacity=int(returns_event_key_capacity),

        seed_master=int(seed),
        employee_salesperson_seed=int(seed) + 99173,
        employee_primary_boost=2.0,

        employee_assign_store_key=emps["employee_assign_store_key"],
        employee_assign_employee_key=emps["employee_assign_employee_key"],
        employee_assign_start_date=emps["employee_assign_start_date"],
        employee_assign_end_date=emps["employee_assign_end_date"],
        employee_assign_fte=emps["employee_assign_fte"],
        employee_assign_is_primary=emps["employee_assign_is_primary"],
        employee_assign_role=emps["employee_assign_role"],
        salesperson_roles=emps["salesperson_roles"],

        product_popularity=prod["product_popularity"],
        product_seasonality=prod["product_seasonality"],

        customer_geo_key=cust["customer_geo_key"],
        geo_to_country_id=corr["geo_to_country_id"],
        store_to_country_id=corr["store_to_country_id"],
        country_to_store_keys=corr["country_to_store_keys"],
        store_channel_keys=corr["store_channel_keys"],
        channel_prob_by_store=corr["channel_prob_by_store"],
        product_channel_eligible=corr["product_channel_eligible"],
        promo_channel_group=corr["promo_channel_group"],
        channel_fulfillment_days=corr["channel_fulfillment_days"],
        _channel_to_elig_group=corr["_channel_to_elig_group"],

        product_scd2_active=prod["product_scd2_active"],
        product_scd2_starts=prod["product_scd2_starts"],
        product_scd2_data=prod["product_scd2_data"],
        customer_scd2_active=cust["customer_scd2_active"],
        customer_scd2_starts=cust["customer_scd2_starts"],
        customer_scd2_keys=cust["customer_scd2_keys"],
        cust_key_to_pool_idx=cust["cust_key_to_pool_idx"],
    )

    # Store-product assortment (optional)
    assortment_cfg = prod["assortment_cfg"]
    product_subcat_key = prod["product_subcat_key"]
    # Publish SubcategoryKey whenever available — the basket-theme
    # correlation needs it even when store assortment is disabled.
    if product_subcat_key is not None:
        worker_cfg["product_subcat_key"] = product_subcat_key
    if assortment_cfg.get("enabled") and product_subcat_key is not None and stores["store_type_map"] is not None:
        worker_cfg["assortment"] = dict(assortment_cfg)
        worker_cfg["store_type_map"] = stores["store_type_map"]
        info("Store-product assortment: enabled")

    return worker_cfg


def _setup_accumulators(cfg, worker_cfg, parquet_folder_p):
    """Set up streaming accumulators for budget, inventory, wishlists, complaints."""
    budget_acc = None
    inventory_acc = None
    wishlists_acc = None
    complaints_acc = None

    # Budget
    _budget_obj = getattr(cfg, "budget", None)
    budget_cfg = _budget_obj if isinstance(_budget_obj, Mapping) else {}
    budget_enabled = _BUDGET_AVAILABLE and bool(budget_cfg.get("enabled", False))

    if budget_enabled:
        try:
            budget_lookups = build_budget_lookups(parquet_folder_p)
            worker_cfg["budget_enabled"] = True
            worker_cfg["budget_store_to_country"] = budget_lookups["budget_store_to_country"]
            worker_cfg["budget_product_to_cat"] = budget_lookups["budget_product_to_cat"]
            worker_cfg["parquet_folder"] = str(parquet_folder_p)

            budget_acc = BudgetAccumulator(
                country_labels=budget_lookups["budget_country_labels"],
                category_labels=budget_lookups["budget_category_labels"],
            )
            info("Budget streaming aggregation: enabled")
        except (KeyError, ValueError, TypeError) as exc:
            info(f"Budget streaming aggregation: disabled ({type(exc).__name__}: {exc})")
            budget_enabled = False
            budget_acc = None
            worker_cfg["budget_enabled"] = False
    else:
        worker_cfg["budget_enabled"] = False

    # Inventory
    _inv_obj = getattr(cfg, "inventory", None)
    inv_cfg = _inv_obj if isinstance(_inv_obj, Mapping) else {}
    inventory_enabled = _INVENTORY_AVAILABLE and bool(inv_cfg.get("enabled", False))

    if inventory_enabled:
        inventory_acc = InventoryAccumulator()
        worker_cfg["inventory_enabled"] = True
        _wh_stores_path = parquet_folder_p / "stores.parquet"
        if _wh_stores_path.exists():
            _wh_st = pd.read_parquet(str(_wh_stores_path), columns=["StoreKey", "WarehouseKey"])
            if "WarehouseKey" in _wh_st.columns:
                _sk = _wh_st["StoreKey"].astype(np.int32).to_numpy()
                _wk = _wh_st["WarehouseKey"].astype(np.int32).to_numpy()
                _max_sk = int(_sk.max()) + 1
                _sk_to_wk = np.full(_max_sk, -1, dtype=np.int32)
                _sk_to_wk[_sk] = _wk
                worker_cfg["inventory_store_to_warehouse"] = _sk_to_wk
        info("Inventory streaming aggregation: enabled")
    else:
        worker_cfg["inventory_enabled"] = False

    # Wishlists
    _wl_obj = getattr(cfg, "wishlists", None)
    wishlists_enabled = _WISHLISTS_AVAILABLE and bool(getattr(_wl_obj, "enabled", False))

    if wishlists_enabled:
        wishlists_acc = WishlistAccumulator()
        worker_cfg["wishlists_enabled"] = True
        info("Wishlists streaming aggregation: enabled")
    else:
        worker_cfg["wishlists_enabled"] = False

    # Complaints
    _cc_obj = getattr(cfg, "complaints", None)
    complaints_enabled = _COMPLAINTS_AVAILABLE and bool(getattr(_cc_obj, "enabled", False))

    if complaints_enabled:
        complaints_acc = ComplaintsAccumulator()
        worker_cfg["complaints_enabled"] = True
        info("Complaints streaming aggregation: enabled")
    else:
        worker_cfg["complaints_enabled"] = False

    return budget_acc, inventory_acc, wishlists_acc, complaints_acc
