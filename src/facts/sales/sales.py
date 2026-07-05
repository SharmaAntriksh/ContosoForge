from __future__ import annotations

import os
import zlib
from collections.abc import Mapping
from math import ceil
from multiprocessing import cpu_count
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

from src.exceptions import SalesError
from src.utils.config_helpers import int_or as _int_or, float_or as _float_or, bool_or as _bool_or, str_or as _str_or
from src.utils.logging_utils import debug, info, skip, warn
from src.utils.shared_arrays import SharedArrayGroup
from .sales_logic import State
from .sales_logic.core import (
    compute_discovery_months,
    compute_month_distinct_targets,
    build_rows_per_month,
    _normalize_end_month,
)
from .sales_logic.core.allocation import _stable_seed
from .sales_logic.chunk_builder import _eligible_counts_fast
from .sales_logic.plan_math import (
    DISCOVERY_SLOTS_AVG_LINES,
    month_offsets_int,
    plan_avg_lines,
)
from .sales_worker import PoolRunSpec, iter_imap_unordered, _worker_task, init_sales_worker
from .sales_worker.schemas import build_worker_schemas_from_cfg
from .output_paths import (
    TABLE_SALES,
    TABLE_SALES_ORDER_DETAIL,
    TABLE_SALES_ORDER_HEADER,
    TABLE_SALES_RETURN,
    build_output_paths_from_sales_cfg,
)

from .prep.sales_helpers import (
    _apply_cfg_default,
    _cfg_get,
    _normalize_seasonal_spikes,
)

from .prep.memory_model import (
    _available_phys_bytes,
    _cap_chunk_size_by_ram,
    _projected_peak_chunk_bytes,
)

from .prep.dimension_loaders import (
    _load_customers,
    _load_employees,
    _load_products,
    _load_promotions,
    _load_stores,
)

from .prep.correlation_lookups import (
    _build_correlation_lookups,
    _prebuild_shared_structures,
)

from .prep.worker_cfg_builder import (
    _build_worker_cfg,
    _setup_accumulators,
    build_returns_policy,
)
from .prep.run_config import MonthPlan, ReturnsResolved, SalesPools, SalesRunConfig
from .prep.worker_cfg_schema import SHARED_FIELDS


# =====================================================================
# Helpers
# =====================================================================


def _resolve_date_range(cfg: dict, start_date: Optional[str], end_date: Optional[str]) -> Tuple[str, str]:
    """
    Resolve the *Sales* fact date window (raw, unbuffered).

    Priority:
      explicit args
      cfg.sales.override.dates.{start,end}
      cfg.dates.override.dates.{start,end}          # global override
      cfg.defaults.dates.{start,end} (or cfg._defaults.dates)
    """
    if start_date is not None and end_date is not None:
        return str(start_date), str(end_date)

    defaults_section = getattr(cfg, "defaults", None) or getattr(cfg, "_defaults", None)
    defaults_dates = getattr(defaults_section, "dates", None) if defaults_section is not None else None
    if not isinstance(defaults_dates, Mapping):
        raise SalesError("Missing defaults.dates in config")

    ov_sales_dates = _cfg_get(cfg, ["sales", "override", "dates"], default={})
    ov_sales_dates = ov_sales_dates if isinstance(ov_sales_dates, Mapping) else {}

    ov_global_dates = _cfg_get(cfg, ["dates", "override", "dates"], default={})
    ov_global_dates = ov_global_dates if isinstance(ov_global_dates, Mapping) else {}

    if start_date is None:
        start_date = (
            ov_sales_dates.get("start")
            or ov_global_dates.get("start")
            or getattr(defaults_dates, "start", None)
        )
    if end_date is None:
        end_date = (
            ov_sales_dates.get("end")
            or ov_global_dates.get("end")
            or getattr(defaults_dates, "end", None)
        )

    if not start_date or not end_date:
        raise SalesError("Could not resolve start/end dates from config")

    return str(start_date), str(end_date)


def _resolve_seed(cfg: Any, seed: Any, default_seed: int = 42) -> int:
    """Resolve sales seed via unified resolver.

    If *seed* is explicitly provided (not None), it takes priority.
    Otherwise delegates to config_precedence.resolve_seed.
    """
    from src.utils.config_precedence import resolve_seed as _resolve
    if seed is not None:
        return _int_or(seed, default_seed)
    sales_cfg = getattr(cfg, "sales", None) if not isinstance(cfg, dict) else cfg.get("sales")
    return _resolve(cfg, sales_cfg, fallback=default_seed)


def suggest_chunk_size(total_rows: int, target_workers: Optional[int] = None, preferred_chunks_per_worker: int = 2) -> int:
    if target_workers is None:
        target_workers = max(1, cpu_count() - 1)
    desired_chunks = max(1, int(target_workers) * int(preferred_chunks_per_worker))
    return max(1_000, int(ceil(int(total_rows) / desired_chunks)))


def batch_tasks(tasks: List[Tuple[int, int, int]], batch_size: int) -> List[List[Tuple[int, int, int]]]:
    if batch_size <= 1:
        return [[t] for t in tasks]
    return [tasks[i:i + batch_size] for i in range(0, len(tasks), batch_size)]


def _resolve_partitioning(cfg: dict, partition_enabled: bool, partition_cols: Optional[Sequence[str]]) -> Tuple[bool, Optional[List[str]]]:
    # Read partition config via _cfg_get, which resolves both attribute-shaped
    # (AppConfig) and plain-dict cfgs. A bare getattr(cfg, "sales") silently
    # returns None for a dict cfg — dicts have no ".sales" attribute — which used
    # to drop the partition config entirely. Support both the flat
    # sales.partition_enabled/partition_cols spelling and the nested
    # sales.partitioning.enabled/columns block, flat taking precedence.
    cfg_enabled = _cfg_get(cfg, ["sales", "partition_enabled"], None)
    if cfg_enabled is None:
        cfg_enabled = _cfg_get(cfg, ["sales", "partitioning", "enabled"], None)

    cfg_cols = _cfg_get(cfg, ["sales", "partition_cols"], None)
    if cfg_cols is None:
        cfg_cols = _cfg_get(cfg, ["sales", "partitioning", "columns"], None)

    partition_enabled = _apply_cfg_default(
        partition_enabled,
        False,
        _bool_or(cfg_enabled, False) if cfg_enabled is not None else None
    )

    if partition_cols is None:
        if isinstance(cfg_cols, list) and cfg_cols:
            partition_cols = list(cfg_cols)
        else:
            partition_cols = None
    else:
        partition_cols = list(partition_cols)

    return bool(partition_enabled), partition_cols


def _resolve_returns_config(cfg, sales_output, skip_order_cols, models_cfg) -> ReturnsResolved:
    """Resolve all returns scalars for the run into one holder.

    Merges the ``config.yaml`` returns section (rate + lag window, gated by
    ``facts.enabled``) with the ``models.yaml`` multi-event quantity/lag shape and
    the ``defaults.py`` reason weights. Keeps ``requested`` vs ``effective``
    enabled separate: returns are switched off (effective only) when
    ``sales_output='sales'`` with ``skip_order_cols``, which removes the order IDs
    a return needs to reference its sale.
    """
    facts_enabled = _cfg_get(cfg, ["facts", "enabled"], default=[])
    facts_enabled = facts_enabled if isinstance(facts_enabled, list) else []

    _returns_obj = getattr(cfg, "returns", None)
    returns_cfg = _returns_obj if isinstance(_returns_obj, Mapping) else {}
    returns_enabled = _bool_or(returns_cfg.get("enabled"), False)

    # If facts.enabled is used, treat it as an additional "feature gate"
    if facts_enabled:
        returns_enabled = bool(returns_enabled and ("returns" in {str(x).lower() for x in facts_enabled}))

    returns_rate = _float_or(returns_cfg.get("return_rate", 0.0), 0.0)
    if not np.isfinite(returns_rate):
        returns_rate = 0.0
    # keep within [0, 1]
    returns_rate = max(0.0, min(1.0, returns_rate))

    returns_min_lag_days = _int_or(
        returns_cfg.get("min_days_after_sale", returns_cfg.get("returns_min_lag_days", 0)),
        0,
    )
    returns_min_lag_days = max(0, returns_min_lag_days)

    returns_max_lag_days = _int_or(
        returns_cfg.get("max_days_after_sale", returns_cfg.get("returns_max_lag_days", 60)),
        60,
    )
    returns_max_lag_days = max(0, returns_max_lag_days)

    # Ensure min <= max (swap/clamp defensively)
    if returns_min_lag_days > returns_max_lag_days:
        returns_min_lag_days = returns_max_lag_days

    # Extract multi-event returns config from models.yaml
    _models_returns = getattr(models_cfg, "returns", None)
    _ret_qty_cfg = getattr(_models_returns, "quantity", None)
    _ret_lag_cfg = getattr(_models_returns, "lag_days", None)

    returns_full_line_prob = _float_or(getattr(_ret_qty_cfg, "full_line_probability", 0.85), 0.85)
    returns_split_rate = _float_or(getattr(_ret_qty_cfg, "split_return_rate", 0.0), 0.0)
    returns_max_splits = _int_or(getattr(_ret_qty_cfg, "max_splits", 3), 3)
    returns_reconcile_cents = _bool_or(getattr(_ret_qty_cfg, "reconcile_cents", False), False)
    returns_split_min_gap = _int_or(getattr(_ret_lag_cfg, "split_min_gap", 3), 3)
    returns_split_max_gap = _int_or(getattr(_ret_lag_cfg, "split_max_gap", 20), 20)
    returns_lag_distribution = _str_or(getattr(_ret_lag_cfg, "distribution", "triangular"), "triangular")
    returns_lag_mode = _int_or(getattr(_ret_lag_cfg, "mode", 7), 7)

    # Merge models.yaml weight overrides with defaults.py canonical reasons
    from src.defaults import (
        RETURN_REASON_KEYS as _RR_KEYS,
        RETURN_REASON_DEFAULT_WEIGHTS as _RR_DEFAULTS,
        RETURN_REASON_LOGISTICS_KEYS as _RR_LOGISTICS,
    )
    _models_reasons = getattr(_models_returns, "reasons", None)
    if _models_reasons and len(_models_reasons) > 0:
        _weight_overrides = {int(r.key): float(r.weight) for r in _models_reasons}
    else:
        _weight_overrides = {}
    returns_reason_keys = list(_RR_KEYS)
    returns_reason_probs = [_weight_overrides.get(k, _RR_DEFAULTS[k]) for k in _RR_KEYS]
    returns_logistics_keys = list(_RR_LOGISTICS)

    # Keep "requested" vs "effective" separate so we can warn+continue.
    returns_enabled_requested = bool(returns_enabled)
    returns_enabled_effective = bool(returns_enabled)

    # Allow returns for ALL modes (sales / sales_order / both),
    # but if sales_output == "sales" and skip_order_cols == True, we cannot derive returns
    # (no order identifiers), so disable returns and warn.
    if returns_enabled_requested and sales_output == "sales" and bool(skip_order_cols):
        info("Disabling returns: skip_order_cols removes order IDs needed by returns")
        returns_enabled_effective = False

    return ReturnsResolved(
        returns_enabled_requested=returns_enabled_requested,
        returns_enabled_effective=returns_enabled_effective,
        returns_rate=returns_rate,
        returns_min_lag_days=returns_min_lag_days,
        returns_max_lag_days=returns_max_lag_days,
        returns_full_line_prob=returns_full_line_prob,
        returns_split_rate=returns_split_rate,
        returns_max_splits=returns_max_splits,
        returns_reconcile_cents=returns_reconcile_cents,
        returns_split_min_gap=returns_split_min_gap,
        returns_split_max_gap=returns_split_max_gap,
        returns_lag_distribution=returns_lag_distribution,
        returns_lag_mode=returns_lag_mode,
        returns_reason_keys=returns_reason_keys,
        returns_reason_probs=returns_reason_probs,
        returns_logistics_keys=returns_logistics_keys,
    )


def _load_sales_pools(parquet_folder_p, cfg, start_date, end_date, seed, active_product_np) -> SalesPools:
    """Load every sales dimension pool + correlation lookups for the run.

    Reads customers / products / stores / promotions / employee-bridge parquet
    into their typed pools, then builds the geo/channel correlation lookups from
    the store + product + promo arrays. Pure IO + seed-based derivation; no State
    reads and no shared-memory publication happen here.
    """
    _cust = _load_customers(parquet_folder_p, cfg, start_date, seed)
    _prod = _load_products(
        parquet_folder_p, cfg, seed, start_date, end_date,
        active_product_np=active_product_np,
    )
    _sdw_cfg = getattr(getattr(cfg, "sales", None), "store_demand_weight", None)
    _stores = _load_stores(parquet_folder_p, end_date, weight_cfg=_sdw_cfg)
    _promos = _load_promotions(parquet_folder_p)
    _emps = _load_employees(parquet_folder_p, cfg, end_date)

    _corr = _build_correlation_lookups(
        parquet_folder_p, _stores.store_keys, _stores.store_to_geo, _stores.store_type_map,
        _prod.product_np, _promos.promo_keys_all, _promos.promo_df,
    )

    return SalesPools(
        customers=_cust, products=_prod, stores=_stores,
        promos=_promos, employees=_emps, correlations=_corr,
    )


def _adjust_customer_demand(models_cfg, cust, total_rows, date_pool):
    """Auto-adjust ``new_customer_share`` / ``max_new_fraction_per_month`` so all
    customers can be discovered within the available rows.

    Low row-to-customer ratios otherwise silently leave thousands of customers
    with no sales. Returns the (possibly updated) ``models_cfg`` — the same
    object unchanged when no adjustment is needed, a fresh copy otherwise.
    """
    if not isinstance(models_cfg, Mapping):
        return models_cfg
    from src.engine.config.config_schema import CustomersDemandConfig

    _cust_mdl = models_cfg.customers
    if _cust_mdl is None:
        _cust_mdl = CustomersDemandConfig()
    configured_share = float(_cust_mdl.new_customer_share)
    configured_max_frac = float(_cust_mdl.max_new_fraction_per_month)

    is_active_in_sales = cust.is_active_in_sales
    total_active = int((is_active_in_sales == 1).sum())
    # Customers with negative start_month are backdated (pre-existing): the
    # discovery schedule debuts them in month 0 (no lag). Only customers with
    # start_month >= 0 need to be discovered within the window.
    warm_start = int(((is_active_in_sales == 1) & (cust.customer_start_month < 0)).sum())
    undiscovered = max(0, total_active - warm_start)
    if undiscovered <= 0 or total_rows <= 0:
        return models_cfg

    headroom = 1.5
    # Customer sampling operates at order granularity (avg ~2 lines per order),
    # so the effective number of customer slots is total_rows / avg_lines.
    effective_slots = total_rows / DISCOVERY_SLOTS_AVG_LINES
    needed_share = (undiscovered * headroom) / effective_slots
    needed_share = float(np.clip(needed_share, 0.0, 0.50))

    # Per-month fraction cap: with T months and avg_eligible customers, each
    # month must discover undiscovered / T customers.
    months_int = month_offsets_int(date_pool)
    T_est = max(1, int(months_int.max() - months_int.min() + 1))
    avg_eligible = max(1, total_active // 2)
    needed_frac = (undiscovered * headroom) / (avg_eligible * T_est)
    needed_frac = float(np.clip(needed_frac, 0.0, 0.50))

    updates = {}
    if needed_share > configured_share:
        updates["new_customer_share"] = needed_share
    if needed_frac > configured_max_frac:
        updates["max_new_fraction_per_month"] = needed_frac
    if not updates:
        return models_cfg

    new_cust = _cust_mdl.model_copy(update=updates)
    debug(
        f"Auto-adjusting customer discovery: new_customer_share "
        f"{configured_share:.3f} -> {new_cust.new_customer_share:.3f}, "
        f"max_new_fraction_per_month "
        f"{configured_max_frac:.4f} -> {new_cust.max_new_fraction_per_month:.4f} "
        f"({undiscovered:,} undiscovered customers across {total_rows:,} rows)."
    )
    return models_cfg.model_copy(update={"customers": new_cust})


def _build_month_plan(
    cust,
    date_pool,
    seed,
    total_rows,
    models_cfg,
    sales_output,
    skip_order_cols,
    max_lines_per_order,
) -> MonthPlan:
    """Build the global per-month plan ONCE against the GLOBAL month totals.

    Produces the closed-form customer discovery schedule plus the per-month row,
    order, and distinct-customer curves — all pure functions of ``seed`` and the
    static customer arrays, so the sales fact is independent of worker count /
    chunk order. Every RNG here is deterministically seeded (no shared stream).
    """
    _dp_months = month_offsets_int(date_pool)
    _T_months = int(_dp_months.max() - _dp_months.min() + 1) if _dp_months.size else 0

    # Closed-form customer discovery schedule (Finding #5/#6): the month each
    # customer first enters the sales population, a pure function of
    # (CustomerKey, seed) and their eligibility window.
    _cust_mdl_final = getattr(models_cfg, "customers", None)
    _discovery_lag_scale = float(getattr(_cust_mdl_final, "discovery_lag_scale", 1.0)) \
        if _cust_mdl_final is not None else 1.0
    _discovery_month_g = compute_discovery_months(
        customer_keys=cust.customer_keys,
        is_active_in_sales=cust.is_active_in_sales,
        start_month=cust.customer_start_month,
        end_month=cust.customer_end_month,
        T=_T_months,
        run_seed=int(seed),
        lag_scale=_discovery_lag_scale,
    )

    # Per-month row curve, order count, and distinct-customer target.
    _end_month_norm_g = _normalize_end_month(
        cust.customer_end_month,
        int(np.asarray(cust.customer_keys).shape[0]),
    )
    _elig_counts_g = _eligible_counts_fast(
        T=_T_months,
        is_active_in_sales=cust.is_active_in_sales,
        start_month=cust.customer_start_month,
        end_month_norm=_end_month_norm_g,
    )
    _macro_cfg_plan = models_cfg.get("macro_demand", {}) or {}
    _plan_rng = np.random.default_rng(_stable_seed(int(seed), "month_rows_plan", _T_months))
    _rows_per_month_g = build_rows_per_month(
        rng=_plan_rng,
        total_rows=int(total_rows),
        eligible_counts=_elig_counts_g,
        macro_cfg=_macro_cfg_plan,
    )

    # Orders per month = lines / avg-lines-per-order, matching chunk_builder's
    # order-vs-line accounting. avg=1 collapses orders==lines (skip_order_cols or
    # max_lines==1). O[m] <= R[m] and O[m] >= 1 wherever R[m] > 0.
    _eff_skip = False if sales_output in {"sales_order", "both"} else bool(skip_order_cols)
    _avg_lines = plan_avg_lines(_eff_skip, max_lines_per_order)
    _orders_per_month_g = np.minimum(
        np.rint(_rows_per_month_g / _avg_lines).astype(np.int64), _rows_per_month_g)
    _orders_per_month_g = np.where(
        _rows_per_month_g > 0, np.maximum(_orders_per_month_g, 1), 0).astype(np.int64)

    # Distinct-customer target per month (canonical single source).
    _cust_dmd = models_cfg.get("customers", {}) or {}
    _distinct_ratio = float(np.clip(_cust_dmd.get("distinct_ratio", 0.55), 0.0, 1.0))
    _cycle_amp = float(np.clip(_cust_dmd.get("cycle_amplitude", 0.35), 0.0, 1.0))
    _part_noise = float(np.clip(_cust_dmd.get("participation_noise", 0.10), 0.0, 1.0))
    _DEFAULT_SEASONAL_SPIKES = [
        {"month": 3, "boost": 0.15}, {"month": 7, "boost": 0.12},
        {"month": 9, "boost": 0.10}, {"month": 11, "boost": 0.40},
        {"month": 12, "boost": 0.25},
    ]
    _spikes_raw = _cust_dmd.get("seasonal_spikes", None)
    if _spikes_raw is None:
        _spikes_raw = _DEFAULT_SEASONAL_SPIKES
    _seasonal_spike_map = _normalize_seasonal_spikes(_spikes_raw)
    _max_distinct_ratio_g = _float_or(_macro_cfg_plan.get("max_distinct_ratio"), 0.70)
    _min_distinct_customers_g = _int_or(_macro_cfg_plan.get("min_distinct_customers"), 250)

    # Calendar month (1-12) per month offset (for the seasonal spike lookup).
    if _dp_months.size:
        _min_abs_month = int(_dp_months.min())
        _month_cal_index = ((np.arange(_T_months, dtype=np.int64) + _min_abs_month) % 12) + 1
    else:
        _month_cal_index = np.zeros(_T_months, dtype=np.int64)

    _distinct_target_g = compute_month_distinct_targets(
        seed=int(seed),
        T=_T_months,
        eligible_counts=_elig_counts_g,
        orders_per_month=_orders_per_month_g,
        month_cal_index=_month_cal_index,
        distinct_ratio=_distinct_ratio,
        cycle_amplitude=_cycle_amp,
        participation_noise=_part_noise,
        seasonal_spike_map=_seasonal_spike_map,
        max_distinct_ratio=_max_distinct_ratio_g,
        min_distinct_customers=_min_distinct_customers_g,
    )

    return MonthPlan(
        customer_discovery_month=_discovery_month_g,
        sales_rows_per_month=_rows_per_month_g,
        sales_orders_per_month=_orders_per_month_g,
        sales_distinct_target=_distinct_target_g,
    )


def _design_order_id_space(date_pool, total_rows, total_chunks, skip_order_cols):
    """Design the day-based OrderNumber ID space.

    Each calendar day gets a disjoint ID band and each chunk a non-overlapping
    slot within it, so OrderNumber increases with OrderDate. Returns
    ``(per_chunk_alloc, day_stride, order_id_int64)``; ``order_id_int64`` promotes
    OrderNumber columns to int64 once the worst-case ID (~8x total_rows, since
    per_chunk_alloc carries an 8x safety factor) would exceed half of int32.
    """
    _dp = np.asarray(date_pool)
    _n_days = int(_dp.size) if _dp.size else 1
    _safety = 8.0
    per_chunk_alloc = int(ceil(total_rows / max(1, _n_days) * _safety / max(1, total_chunks)))
    per_chunk_alloc = max(per_chunk_alloc, 1)
    day_stride = per_chunk_alloc * total_chunks

    _INT32_MAX = 2_147_483_647
    if _dp.size:
        _dp_days = _dp.astype("datetime64[D]")
        _day_span = int((_dp_days.max() - _dp_days.min()).astype("int64"))
    else:
        _day_span = 0
    _max_order_id = (_day_span + 1) * int(day_stride)
    order_id_int64 = _max_order_id > _INT32_MAX // 2
    if order_id_int64 and not skip_order_cols:
        warn(
            f"OrderNumber worst-case value ~{_max_order_id:,} exceeds the int32 "
            "safety margin; emitting int64 for order-number columns."
        )
    return per_chunk_alloc, day_stride, order_id_int64


def _resolve_worker_count(workers, n_tasks, chunk_size, tune_chunk):
    """Resolve the worker-process count.

    The generated fact is worker-count invariant, so this only bounds
    parallelism and RAM. Auto-caps on Windows for spawn-mode memory, and warns
    on projected peak memory when chunk_size is pinned (never silently shrinks a
    pinned chunk_size — that would change output).
    """
    if workers is None:
        n_workers = min(n_tasks, max(1, cpu_count() - 1))
    else:
        n_workers = min(n_tasks, max(1, _int_or(workers, cpu_count() - 1)))

    if os.name == "nt":
        _avail_bytes = _available_phys_bytes()
        if _avail_bytes:
            avail_mb = _avail_bytes / (1024 * 1024)
            from src.defaults import WORKER_OS_RESERVE_MB, WORKER_ESTIMATE_MB
            usable_mb = max(0, avail_mb - WORKER_OS_RESERVE_MB)
            mem_cap = max(1, int(usable_mb / WORKER_ESTIMATE_MB))
            if mem_cap < n_workers:
                info(f"Auto-capping workers {n_workers} -> {mem_cap} (available RAM: {avail_mb:.0f} MB)")
                n_workers = mem_cap

    if not tune_chunk:
        _avail = _available_phys_bytes()
        if _avail:
            _peak = _projected_peak_chunk_bytes(chunk_size, n_workers)
            if _peak > _avail * 0.75:
                warn(
                    f"Memory risk: ~{_peak / (1024 ** 3):.1f} GB projected peak "
                    f"(chunk_size={chunk_size:,} × {n_workers} workers) vs "
                    f"~{_avail / (1024 ** 3):.1f} GB available."
                )
                warn(
                    "Lower sales.chunk_size or set sales.tune_chunk=true "
                    "to avoid possible OOM."
                )
    return n_workers


def _resolve_chunk_size(chunk_size, tune_chunk, workers, total_rows) -> int:
    """Resolve the final rows-per-chunk.

    When ``tune_chunk`` is set, auto-derive chunk_size from total_rows against a
    planned worker count, then RAM-cap the joint workers×chunk_size OOM surface
    (safe to shrink here because an auto-tuned chunk_size is already
    machine-specific). A *pinned* chunk_size is only floored to 1000, never
    shrunk — that would change output; its RAM risk is surfaced as a warning in
    ``_resolve_worker_count`` instead.
    """
    if tune_chunk:
        if workers is None:
            n_workers_planned = max(1, cpu_count() - 1)
        else:
            n_workers_planned = max(1, _int_or(workers, cpu_count() - 1))
        chunk_size = suggest_chunk_size(total_rows, target_workers=n_workers_planned, preferred_chunks_per_worker=2)
        # Joint RAM cap: the OOM surface is workers × chunk_size, not either alone.
        _capped = _cap_chunk_size_by_ram(chunk_size, n_workers_planned)
        if _capped < chunk_size:
            info(f"Auto-capping chunk_size {chunk_size:,} -> {_capped:,} to bound "
                 f"in-flight memory across {n_workers_planned} workers")
            chunk_size = _capped
    return max(1_000, _int_or(chunk_size, 1_000_000))


def _resolve_order_id_run_id(sales_cfg, out_folder_p, seed):
    """Resolve the 0..999 OrderNumber RunId: the configured
    ``sales.order_id_run_id``, or a stable crc32 of (output folder, seed) when
    unset (unique per run folder)."""
    order_id_run_id_raw = getattr(sales_cfg, "order_id_run_id", None)
    if order_id_run_id_raw is None:
        key = f"{out_folder_p.resolve()}|{int(seed)}".encode("utf-8")
        order_id_run_id = int(zlib.crc32(key) % 1000)
    else:
        order_id_run_id = int(order_id_run_id_raw) % 1000
    if order_id_run_id < 0:
        order_id_run_id += 1000
    return order_id_run_id


# =====================================================================
# Main Fact Generation
# =====================================================================

def generate_sales_fact(
    cfg,
    parquet_folder,
    out_folder,
    total_rows,
    chunk_size=2_000_000,
    start_date=None,
    end_date=None,
    row_group_size=2_000_000,
    compression="snappy",
    merge_parquet=False,
    merged_file="sales.parquet",
    delete_chunks=False,
    seed=42,
    file_format="parquet",
    workers=None,
    tune_chunk=False,
    delta_output_folder=None,
    skip_order_cols=False,
    partition_enabled=False,
    partition_cols=None,
) -> SalesFactResult:
    # ------------------------------------------------------------
    # Normalize cfg defaults (cfg is source-of-truth when call-site omits)
    # ------------------------------------------------------------
    cfg = cfg if isinstance(cfg, Mapping) else {}
    # Resolve the sales section dict- and attribute-agnostically. A bare
    # getattr(cfg, "sales") returns None for a plain-dict cfg, so every
    # getattr(sales_cfg, ...) below silently fell back to the call-site arg and
    # the config was ignored. _cfg_get returns the section for both shapes; wrap a
    # plain dict in SimpleNamespace so the attribute reads below work unchanged.
    sales_cfg = _cfg_get(cfg, ["sales"], None)
    if isinstance(sales_cfg, dict):
        sales_cfg = SimpleNamespace(**sales_cfg)
    elif not isinstance(sales_cfg, Mapping):
        sales_cfg = SimpleNamespace()

    file_format_cfg = getattr(sales_cfg, "file_format", None)
    if file_format_cfg is not None:
        file_format = _apply_cfg_default(file_format, "parquet", _str_or(file_format_cfg, "parquet").lower())

    merge_parquet = _apply_cfg_default(
        merge_parquet, False,
        _bool_or(getattr(sales_cfg, "merge_parquet", None), merge_parquet) if hasattr(sales_cfg, "merge_parquet") else None
    )
    merged_file = _apply_cfg_default(merged_file, "sales.parquet", getattr(sales_cfg, "merged_file", None) if hasattr(sales_cfg, "merged_file") else None)

    delete_chunks = _apply_cfg_default(
        delete_chunks, False,
        _bool_or(getattr(sales_cfg, "delete_chunks", None), delete_chunks) if hasattr(sales_cfg, "delete_chunks") else None
    )

    chunk_size = _apply_cfg_default(chunk_size, 2_000_000, _int_or(getattr(sales_cfg, "chunk_size", None), chunk_size) if hasattr(sales_cfg, "chunk_size") else None)
    row_group_size = _apply_cfg_default(row_group_size, 2_000_000, _int_or(getattr(sales_cfg, "row_group_size", None), row_group_size))
    compression = _apply_cfg_default(compression, "snappy", getattr(sales_cfg, "compression", None))
    workers = _apply_cfg_default(workers, None, getattr(sales_cfg, "workers", None))
    tune_chunk = _apply_cfg_default(tune_chunk, False, _bool_or(getattr(sales_cfg, "tune_chunk", None), tune_chunk))
    skip_order_cols = _apply_cfg_default(
        skip_order_cols,
        False,
        _bool_or(getattr(sales_cfg, "skip_order_cols", None), skip_order_cols) if hasattr(sales_cfg, "skip_order_cols") else None,
    )

    start_date, end_date = _resolve_date_range(cfg, start_date, end_date)
    optimize_after_merge = _bool_or(getattr(sales_cfg, "sort_merged_parquet", None), False)

    seed = _resolve_seed(cfg, seed, default_seed=42)
    partition_enabled, partition_cols = _resolve_partitioning(cfg, partition_enabled, partition_cols)

    # ------------------------------------------------------------
    # Paths / folders
    # ------------------------------------------------------------
    parquet_folder_p = Path(str(parquet_folder))
    out_folder_p = Path(str(out_folder))

    # Resolve output paths (delta-folder defaulting + abspath) via the single owner.
    output_paths = build_output_paths_from_sales_cfg(
        SimpleNamespace(
            file_format=file_format,
            out_folder=str(out_folder_p),
            merged_file=merged_file,
            delta_output_folder=delta_output_folder,
        )
    )
    delta_output_folder = output_paths.delta_output_folder

    sales_output = _str_or(getattr(sales_cfg, "sales_output", None), "sales").lower()
    if sales_output not in {"sales", "sales_order", "both"}:
        raise SalesError(f"Invalid sales_output: {sales_output}")

    # The models config is bound onto State by the runner before this call; read
    # it once here and thread it forward as a local (adjusted below) so nothing
    # downstream depends on a mid-function State mutation.
    models_cfg = getattr(State, "models_cfg", None)

    # ------------------------------------------------------------
    # Returns (optional)
    # ------------------------------------------------------------
    _returns = _resolve_returns_config(cfg, sales_output, skip_order_cols, models_cfg)

    tables: list[str] = []
    if sales_output in {"sales", "both"}:
        tables.append(TABLE_SALES)
    if sales_output in {"sales_order", "both"}:
        tables += [TABLE_SALES_ORDER_DETAIL, TABLE_SALES_ORDER_HEADER]

    # Returns table is independent of which sales family tables you output
    # (as long as returns_enabled_effective == True).
    if _returns.returns_enabled_effective:
        tables.append(TABLE_SALES_RETURN)

    for t in tables:
        output_paths.ensure_dirs(t)

    def _empty_manifest() -> SalesRunManifest:
        """Build an empty manifest for early-exit paths."""
        per_table: dict[str, TableOutputs] = {}
        for t in tables:
            per_table[t] = TableOutputs(
                table=t,
                file_format=file_format,
                chunks=[],
                merged_path=(output_paths.merged_path(t) if file_format == "parquet" else None),
                delta_table_dir=(output_paths.delta_table_dir(t) if file_format == "deltaparquet" else None),
                delta_parts_dir=(output_paths.delta_parts_dir(t) if file_format == "deltaparquet" else None),
            )

        return SalesRunManifest(
            sales_output=sales_output,
            file_format=file_format,
            out_folder=str(out_folder_p),
            tables=per_table,
        )

    # ------------------------------------------------------------
    # Optional auto chunk sizing
    # ------------------------------------------------------------
    total_rows = _int_or(total_rows, 0)
    # NOTE: the OrderNumber int32→int64 decision is made *after* day-ID
    # sizing below — the ID space is ~8x total_rows, not total_rows, so it can't
    # be decided from total_rows alone here. See `_order_id_int64`.
    if total_rows <= 0:
        skip("No sales rows to generate (total_rows <= 0).")
        return SalesFactResult(chunk_files=[], manifest=_empty_manifest())

    chunk_size = _resolve_chunk_size(chunk_size, tune_chunk, workers, total_rows)

    # Structural upper bound on returns events per chunk:
    #   chunk_size_orders * max_lines_per_order * max_splits
    # Independent of returns_rate / split_return_rate, so ReturnEventKey
    # ranges stay disjoint across chunks even under chunk auto-tuning.
    _max_lines_per_order = _int_or(getattr(sales_cfg, "max_lines_per_order", 5), 5)
    returns_event_key_capacity = (
        int(chunk_size)
        * max(_max_lines_per_order, 1)
        * max(_returns.returns_max_splits, 1)
        + 1
    )

    # Load dimensions + correlation lookups (one concern).
    _pools = _load_sales_pools(
        parquet_folder_p, cfg, start_date, end_date, seed,
        active_product_np=getattr(State, "active_product_np", None),
    )
    _cust = _pools.customers
    _prod = _pools.products
    _stores = _pools.stores
    _promos = _pools.promos
    _emps = _pools.employees
    _corr = _pools.correlations

    # The date pool drives order-id sizing, demand auto-adjust, and the plan.
    date_pool = _prod.date_pool

    # ------------------------------------------------------------
    # Chunk scheduling
    # ------------------------------------------------------------
    total_chunks = int(ceil(total_rows / chunk_size))

    # Day-based OrderNumber ID space (per-day disjoint bands, per-chunk slots).
    _per_chunk_alloc, _day_stride, _order_id_int64 = _design_order_id_space(
        date_pool, total_rows, total_chunks, skip_order_cols,
    )

    # Per-chunk seeds are derived in the worker via
    # ``SeedSequence(run_seed).spawn(...)[chunk_idx]`` (the repo's house pattern,
    # see task.derive_chunk_seed), so each chunk seed is a pure function of
    # (run_seed, chunk_idx) — independently regenerable and worker-count
    # invariant. The task tuple carries the run seed itself; no materialized
    # per-chunk seed array (which would be a sequential draw off one stream).
    tasks: List[Tuple[int, int, int]] = []
    remaining = total_rows
    for idx in range(total_chunks):
        if remaining <= 0:
            break
        batch = min(chunk_size, remaining)
        tasks.append((idx, int(batch), int(seed)))
        remaining -= batch

    if not tasks:
        skip("No sales rows to generate.")
        return SalesFactResult(chunk_files=[], manifest=_empty_manifest())

    # Worker count (output is worker-count invariant; this only bounds parallelism).
    n_workers = _resolve_worker_count(workers, len(tasks), chunk_size, tune_chunk)
    info(f"Spawning {n_workers} worker processes...")

    order_id_run_id = _resolve_order_id_run_id(sales_cfg, out_folder_p, seed)

    # Auto-adjust customer discovery demand so all customers can be discovered
    # within the available rows (low row-to-customer ratios otherwise leave
    # thousands of customers with no sales). The adjusted models config flows
    # forward as a local into the month plan, returns policy, and worker
    # broadcast — no State mutation, so correctness no longer rides on the order
    # of these statements.
    models_cfg = _adjust_customer_demand(models_cfg, _cust, total_rows, date_pool)

    # Global per-month plan: the closed-form discovery schedule plus the per-month
    # row/order/distinct-customer curves, computed ONCE against the GLOBAL month
    # totals and broadcast read-only (so the fact is worker-count / chunk-order
    # independent). Bundled into one MonthPlan holder.
    _month_plan = _build_month_plan(
        _cust, date_pool, seed, total_rows, models_cfg,
        sales_output, skip_order_cols, _max_lines_per_order,
    )

    # Build the base returns policy once (None when returns are disabled), so the
    # worker-config builder takes one object instead of 15 loose returns scalars.
    _returns_policy = build_returns_policy(
        returns_enabled_effective=_returns.returns_enabled_effective,
        returns_rate=_returns.returns_rate,
        returns_min_lag_days=_returns.returns_min_lag_days,
        returns_max_lag_days=_returns.returns_max_lag_days,
        returns_reason_keys=_returns.returns_reason_keys,
        returns_reason_probs=_returns.returns_reason_probs,
        returns_full_line_prob=_returns.returns_full_line_prob,
        returns_split_rate=_returns.returns_split_rate,
        returns_max_splits=_returns.returns_max_splits,
        returns_reconcile_cents=_returns.returns_reconcile_cents,
        returns_split_min_gap=_returns.returns_split_min_gap,
        returns_split_max_gap=_returns.returns_split_max_gap,
        returns_lag_distribution=_returns.returns_lag_distribution,
        returns_lag_mode=_returns.returns_lag_mode,
        returns_logistics_keys=_returns.returns_logistics_keys,
        models_cfg=models_cfg,
    )

    # Worker configuration: bundle the resolved run-level scalars into one typed
    # holder so the builder takes a handful of containers + config objects
    # instead of a long positional tail.
    _run_cfg = SalesRunConfig(
        file_format=file_format,
        out_folder=str(out_folder_p),
        chunk_size=chunk_size,
        total_rows=total_rows,
        seed=seed,
        order_id_run_id=order_id_run_id,
        sales_output=sales_output,
        skip_order_cols=skip_order_cols,
        delta_output_folder=delta_output_folder,
        partition_enabled=partition_enabled,
        partition_cols=partition_cols,
        row_group_size=row_group_size,
        compression=compression,
        order_id_day_stride=_day_stride,
        per_chunk_alloc=_per_chunk_alloc,
        order_id_int64=_order_id_int64,
        total_chunks=total_chunks,
    )
    worker_cfg = _build_worker_cfg(
        _cust, _month_plan, _prod, _stores, _emps, _corr, _promos,
        cfg, sales_cfg, output_paths, _run_cfg,
        _returns_policy, returns_event_key_capacity, models_cfg,
    )

    # Streaming accumulators (budget, inventory, wishlists, complaints)
    budget_acc, inventory_acc, wishlists_acc, complaints_acc = _setup_accumulators(
        cfg, worker_cfg, parquet_folder_p,
    )

    # ------------------------------------------------------------
    # Shared memory: place large numpy arrays in OS shared memory
    # so worker processes get zero-copy views instead of pickle copies.
    # ------------------------------------------------------------
    _shm = SharedArrayGroup()
    _shm.publish_dict(worker_cfg, SHARED_FIELDS)

    # Pre-build expensive derived structures (brand buckets, assortment, salesperson, brand probs)
    _prebuild_shared_structures(worker_cfg, _shm, _prod, _stores, _emps, seed)

    collector = ChunkResultCollector(tables, budget_acc, inventory_acc, wishlists_acc, complaints_acc)

    # ------------------------------------------------------------
    # Multiprocessing (batched)
    # ------------------------------------------------------------
    CHUNKS_PER_CALL = 2
    batched_tasks = batch_tasks(tasks, CHUNKS_PER_CALL)

    total_units = len(tasks)
    completed_units = 0

    pool_spec = PoolRunSpec(
        processes=n_workers,
        label="sales",
    )

    try:
        for result in iter_imap_unordered(
            tasks=batched_tasks,
            task_fn=_worker_task,
            spec=pool_spec,
            initializer=init_sales_worker,
            initargs=(worker_cfg,),
        ):
            if isinstance(result, list):
                for r in result:
                    completed_units += 1
                    collector.record(r, completed_units, total_units)
            else:
                completed_units += 1
                collector.record(result, completed_units, total_units)
    finally:
        _shm.cleanup()

    # Final assembly (delta writes, CSV re-chunking, or parquet merge).
    # For Delta, hand the assembler the run's authoritative per-table schemas
    # (derived from the same worker_cfg the workers used) so the Delta commit
    # adopts that schema instead of sniffing an arbitrary part file.
    sales_schema_by_table = (
        build_worker_schemas_from_cfg(worker_cfg).schema_by_table
        if file_format == "deltaparquet"
        else None
    )
    return _assemble_output(
        file_format, tables, output_paths, collector,
        partition_cols, sales_cfg, sales_output, out_folder_p,
        chunk_size, delete_chunks, merge_parquet, compression,
        row_group_size, optimize_after_merge,
        order_id_int64=_order_id_int64,
        schema_by_table=sales_schema_by_table,
    )
from .output_assembler import (
    ChunkResultCollector,
    SalesFactResult,
    SalesRunManifest,
    TableOutputs,
    _assemble_output,
)
