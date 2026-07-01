from __future__ import annotations

import glob
import os
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil
from multiprocessing import cpu_count
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
import pyarrow.parquet as _pq

from src.defaults import (
    ONLINE_SALES_REP_ROLE,
    ALL_CHANNELS,
    STORE_TYPE_CHANNEL_MAP,
    DEFAULT_CHANNEL_MAP,
    DEFAULT_CHANNEL_FULFILLMENT_DAYS,
    CHANNEL_TO_ELIG_GROUP,
)
from src.exceptions import PackagingError, SalesError
from .worker_cfg_schema import SalesWorkerCfg
from src.utils.config_helpers import int_or as _int_or, float_or as _float_or, bool_or as _bool_or, str_or as _str_or
from src.utils.logging_utils import debug, info, skip, warn, work
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
from .sales_worker import PoolRunSpec, iter_imap_unordered, _worker_task, init_sales_worker
from .sales_writer import merge_parquet_files, optimize_parquet
from .output_paths import (
    OutputPaths,
    TABLE_SALES,
    TABLE_SALES_ORDER_DETAIL,
    TABLE_SALES_ORDER_HEADER,
    TABLE_SALES_RETURN,
)

from .sales_helpers import (
    _CSV_COPY_BUF,
    _apply_cfg_default,
    _as_np,
    _bool_mask,
    _cfg_get,
    _normalize_dt_any,
    _normalize_nullable_int_month,
    build_weighted_date_pool,
    ensure_dir,
    load_parquet_column,
    load_parquet_df,
)

from .memory_model import (
    _available_phys_bytes,
    _cap_chunk_size_by_ram,
    _projected_peak_chunk_bytes,
)

from .dimension_loaders import (
    _compute_promo_salience,
    _load_customers,
    _load_employees,
    _load_products,
    _load_promotions,
    _load_stores,
)

from .correlation_lookups import (
    _build_correlation_lookups,
    _prebuild_shared_structures,
)

from .worker_cfg_builder import _build_worker_cfg, _setup_accumulators


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
    sales_cfg = getattr(cfg, "sales", None) if isinstance(cfg, Mapping) else None
    sales_cfg = sales_cfg if isinstance(sales_cfg, Mapping) else {}

    cfg_enabled = None
    if hasattr(sales_cfg, "partition_enabled"):
        cfg_enabled = getattr(sales_cfg, "partition_enabled", None)
    elif hasattr(sales_cfg, "partitioning"):
        _part = getattr(sales_cfg, "partitioning", None)
        if isinstance(_part, Mapping):
            cfg_enabled = getattr(_part, "enabled", None)

    cfg_cols = None
    if hasattr(sales_cfg, "partition_cols"):
        cfg_cols = getattr(sales_cfg, "partition_cols", None)
    elif hasattr(sales_cfg, "partitioning"):
        _part = getattr(sales_cfg, "partitioning", None)
        if isinstance(_part, Mapping):
            cfg_cols = getattr(_part, "columns", None)

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


# =====================================================================
# Dimension Loading Helpers
# =====================================================================


# =====================================================================
# Helpers (extracted from generate_sales_fact)
# =====================================================================


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
    write_delta=False,     # legacy (ignored)
    delta_output_folder=None,
    skip_order_cols=False,
    partition_enabled=False,
    partition_cols=None,
) -> SalesFactResult:
    # ------------------------------------------------------------
    # Normalize cfg defaults (cfg is source-of-truth when call-site omits)
    # ------------------------------------------------------------
    cfg = cfg if isinstance(cfg, Mapping) else {}
    sales_cfg = getattr(cfg, "sales", None)
    sales_cfg = sales_cfg if isinstance(sales_cfg, Mapping) else {}

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

    # Resolve delta folder early (so OutputPaths is built with final values)
    if file_format == "deltaparquet":
        if delta_output_folder is None:
            delta_output_folder = str(out_folder_p / "delta")
        delta_output_folder = os.path.abspath(str(delta_output_folder))

    output_paths = OutputPaths(
        file_format=file_format,
        out_folder=str(out_folder_p),
        merged_file=str(merged_file),
        delta_output_folder=(str(delta_output_folder) if file_format == "deltaparquet" else None),
    )

    sales_output = _str_or(getattr(sales_cfg, "sales_output", None), "sales").lower()
    if sales_output not in {"sales", "sales_order", "both"}:
        raise SalesError(f"Invalid sales_output: {sales_output}")

    # ------------------------------------------------------------
    # Returns (optional)
    # ------------------------------------------------------------
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
    _models_returns = getattr(State.models_cfg, "returns", None)
    _ret_qty_cfg = getattr(_models_returns, "quantity", None)
    _ret_lag_cfg = getattr(_models_returns, "lag_days", None)

    returns_full_line_prob = _float_or(getattr(_ret_qty_cfg, "full_line_probability", 0.85), 0.85)
    returns_split_rate = _float_or(getattr(_ret_qty_cfg, "split_return_rate", 0.0), 0.0)
    returns_max_splits = _int_or(getattr(_ret_qty_cfg, "max_splits", 3), 3)
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

    tables: list[str] = []
    if sales_output in {"sales", "both"}:
        tables.append(TABLE_SALES)
    if sales_output in {"sales_order", "both"}:
        tables += [TABLE_SALES_ORDER_DETAIL, TABLE_SALES_ORDER_HEADER]

    # Returns table is independent of which sales family tables you output
    # (as long as returns_enabled_effective == True).
    if returns_enabled_effective:
        tables.append(TABLE_SALES_RETURN)

    for t in tables:
        output_paths.ensure_dirs(t)

    # Normalize delta_output_folder after OutputPaths decides defaults/abspath (if your class does that)
    delta_output_folder = output_paths.delta_output_folder

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

    if workers is None:
        n_workers_planned = max(1, cpu_count() - 1)
    else:
        n_workers_planned = max(1, _int_or(workers, cpu_count() - 1))

    if tune_chunk:
        chunk_size = suggest_chunk_size(total_rows, target_workers=n_workers_planned, preferred_chunks_per_worker=2)
        # Joint RAM cap: the OOM surface is workers × chunk_size, not either
        # alone.  Safe to apply here because the auto-tuned chunk_size is already
        # a function of the machine's worker count (not reproducible across
        # machines); pinned chunk_size is handled by a warning below instead.
        _capped = _cap_chunk_size_by_ram(chunk_size, n_workers_planned)
        if _capped < chunk_size:
            info(f"Auto-capping chunk_size {chunk_size:,} -> {_capped:,} to bound "
                 f"in-flight memory across {n_workers_planned} workers")
            chunk_size = _capped

    chunk_size = max(1_000, _int_or(chunk_size, 1_000_000))

    # Structural upper bound on returns events per chunk:
    #   chunk_size_orders * max_lines_per_order * max_splits
    # Independent of returns_rate / split_return_rate, so ReturnEventKey
    # ranges stay disjoint across chunks even under chunk auto-tuning.
    _max_lines_per_order = _int_or(getattr(sales_cfg, "max_lines_per_order", 5), 5)
    returns_event_key_capacity = (
        int(chunk_size)
        * max(_max_lines_per_order, 1)
        * max(returns_max_splits, 1)
        + 1
    )

    # Load dimensions
    _cust = _load_customers(parquet_folder_p, cfg, start_date, seed)
    _prod = _load_products(
        parquet_folder_p, cfg, seed, start_date, end_date,
        active_product_np=getattr(State, "active_product_np", None),
    )
    _sdw_cfg = getattr(getattr(cfg, "sales", None), "store_demand_weight", None)
    _stores = _load_stores(parquet_folder_p, end_date, weight_cfg=_sdw_cfg)
    _promos = _load_promotions(parquet_folder_p)
    _emps = _load_employees(parquet_folder_p, cfg, end_date)

    # Locals needed by auto-adjust customer discovery and correlation lookups
    store_keys = _stores["store_keys"]
    product_np = _prod["product_np"]
    date_pool = _prod["date_pool"]
    is_active_in_sales = _cust["is_active_in_sales"]
    customer_start_month = _cust["customer_start_month"]

    _corr = _build_correlation_lookups(
        parquet_folder_p, store_keys, _stores["store_to_geo"], _stores["store_type_map"],
        product_np, _promos["promo_keys_all"], _promos["promo_df"],
    )

    # ------------------------------------------------------------
    # Chunk scheduling
    # ------------------------------------------------------------
    total_chunks = int(ceil(total_rows / chunk_size))

    # Day-based order ID ranges: each calendar day gets a disjoint ID band,
    # and within each day each chunk gets its own non-overlapping slot.
    # This guarantees OrderNumber increases with OrderDate.
    _dp = np.asarray(date_pool)
    _n_days = int(_dp.size) if _dp.size else 1
    _safety = 8.0
    _per_chunk_alloc = int(ceil(total_rows / max(1, _n_days) * _safety / max(1, total_chunks)))
    _per_chunk_alloc = max(_per_chunk_alloc, 1)
    _day_stride = _per_chunk_alloc * total_chunks

    # OrderNumber dtype decision. Day IDs reach up to (day_span+1)*day_stride
    # (≈ 8x total_rows, since per_chunk_alloc carries an 8x safety factor). That
    # crosses the int32 ceiling near ~268M rows — far below total_rows itself — so
    # the promotion must be sized to the real ID space, not total_rows. Promote to
    # int64 once the worst-case ID would exceed half of int32 (a 2x safety margin).
    _INT32_MAX = 2_147_483_647
    if _dp.size:
        _dp_days = _dp.astype("datetime64[D]")
        _day_span = int((_dp_days.max() - _dp_days.min()).astype("int64"))
    else:
        _day_span = 0
    _max_order_id = (_day_span + 1) * int(_day_stride)
    _order_id_int64 = _max_order_id > _INT32_MAX // 2
    if _order_id_int64 and not skip_order_cols:
        warn(
            f"OrderNumber worst-case value ~{_max_order_id:,} exceeds the int32 "
            "safety margin; emitting int64 for order-number columns."
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

    # ------------------------------------------------------------
    # Worker count
    # ------------------------------------------------------------
    if workers is None:
        n_workers = min(len(tasks), max(1, cpu_count() - 1))
    else:
        n_workers = min(len(tasks), max(1, _int_or(workers, cpu_count() - 1)))

    # Auto-cap workers on Windows to prevent page-file thrashing during
    # spawn-mode init.  Each worker imports numpy/pandas/pyarrow (~350 MB).
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

    # When chunk_size is pinned (not auto-tuned), we never silently shrink it
    # (that would change output and break reproducibility) — but warn if the
    # projected peak memory across all workers looks likely to exhaust RAM,
    # so the user can lower chunk_size or enable tune_chunk deliberately.
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

    info(f"Spawning {n_workers} worker processes...")

    # OrderNumber RunId:
    # - If configured: sales.order_id_run_id (0..999)
    # - Else derive a stable 0..999 id from output folder + seed (unique per run folder)
    order_id_run_id_raw = getattr(sales_cfg, "order_id_run_id", None)
    if order_id_run_id_raw is None:
        key = f"{out_folder_p.resolve()}|{int(seed)}".encode("utf-8")
        order_id_run_id = int(zlib.crc32(key) % 1000)
    else:
        order_id_run_id = int(order_id_run_id_raw) % 1000
    if order_id_run_id < 0:
        order_id_run_id += 1000

    # ------------------------------------------------------------
    # Auto-adjust new_customer_share so all customers can be
    # discovered within the available rows.  Without this, low
    # row-to-customer ratios silently leave thousands of customers
    # without any sales.
    # ------------------------------------------------------------
    _models = State.models_cfg
    if isinstance(_models, Mapping):
        from src.engine.config.config_schema import CustomersDemandConfig

        _cust_mdl = _models.customers
        if _cust_mdl is None:
            _cust_mdl = CustomersDemandConfig()
        configured_share = float(_cust_mdl.new_customer_share)
        configured_max_frac = float(_cust_mdl.max_new_fraction_per_month)

        total_active = int((is_active_in_sales == 1).sum())
        # Customers with negative start_month are backdated (pre-existing): the
        # discovery schedule debuts them in month 0 (no lag). Only customers with
        # start_month >= 0 need to be discovered within the window.
        warm_start = int(((is_active_in_sales == 1) & (customer_start_month < 0)).sum())
        undiscovered = max(0, total_active - warm_start)

        if undiscovered > 0 and total_rows > 0:
            headroom = 1.5
            # Customer sampling operates at order granularity (avg ~2
            # lines per order), so the effective number of customer
            # slots is roughly total_rows / avg_lines_per_order.
            avg_lines_per_order = 2.0
            effective_slots = total_rows / avg_lines_per_order
            needed_share = (undiscovered * headroom) / effective_slots
            needed_share = float(np.clip(needed_share, 0.0, 0.50))

            # Compute the per-month fraction cap needed to sustain discovery.
            # With T months and avg_eligible customers, each month must discover
            # undiscovered / T customers, i.e. (undiscovered / T) / avg_eligible
            # of the eligible pool.
            months_int = date_pool.astype("datetime64[M]").astype("int64")
            T_est = max(1, int(months_int.max() - months_int.min() + 1))
            avg_eligible = max(1, total_active // 2)
            needed_frac = (undiscovered * headroom) / (avg_eligible * T_est)
            needed_frac = float(np.clip(needed_frac, 0.0, 0.50))

            updates = {}
            if needed_share > configured_share:
                updates["new_customer_share"] = needed_share
            if needed_frac > configured_max_frac:
                updates["max_new_fraction_per_month"] = needed_frac

            if updates:
                new_cust = _cust_mdl.model_copy(update=updates)
                debug(
                    f"Auto-adjusting customer discovery: new_customer_share "
                    f"{configured_share:.3f} -> {new_cust.new_customer_share:.3f}, "
                    f"max_new_fraction_per_month "
                    f"{configured_max_frac:.4f} -> {new_cust.max_new_fraction_per_month:.4f} "
                    f"({undiscovered:,} undiscovered customers across {total_rows:,} rows)."
                )
                _models_copy = _models.model_copy(update={"customers": new_cust})
                State.models_cfg = _models_copy

    # ------------------------------------------------------------
    # Closed-form customer discovery schedule (Finding #5/#6).
    # Assign every customer the month they first enter the sales population as a
    # pure function of (CustomerKey, seed) and their eligibility window. Built
    # ONCE here and broadcast read-only, so the sales fact is independent of
    # worker count / chunk order (replaces the old mutable seen_customers set).
    # ------------------------------------------------------------
    _dp_months = np.asarray(date_pool).astype("datetime64[M]").astype("int64")
    _T_months = int(_dp_months.max() - _dp_months.min() + 1) if _dp_months.size else 0
    _cust_mdl_final = getattr(State.models_cfg, "customers", None)
    _discovery_lag_scale = float(getattr(_cust_mdl_final, "discovery_lag_scale", 1.0)) \
        if _cust_mdl_final is not None else 1.0
    _cust["customer_discovery_month"] = compute_discovery_months(
        customer_keys=_cust["customer_keys"],
        is_active_in_sales=_cust["is_active_in_sales"],
        start_month=_cust["customer_start_month"],
        end_month=_cust["customer_end_month"],
        T=_T_months,
        run_seed=int(seed),
        lag_scale=_discovery_lag_scale,
    )

    # ------------------------------------------------------------
    # Global per-month plan. Compute the per-month row curve,
    # order count, and distinct-customer target ONCE here against the GLOBAL month
    # totals, then broadcast read-only. Each chunk slices a contiguous band of
    # every month's order-id space (see chunk_builder), so both the per-month row
    # curve and the distinct-customer curve are independent of chunk_size / worker
    # count (review Finding #4/#14/#17) — replacing the old per-chunk allocation
    # and per-chunk distinct target that made both depend on chunk_size.
    # ------------------------------------------------------------
    _end_month_norm_g = _normalize_end_month(
        _cust.get("customer_end_month"),
        int(np.asarray(_cust["customer_keys"]).shape[0]),
    )
    _elig_counts_g = _eligible_counts_fast(
        T=_T_months,
        is_active_in_sales=_cust["is_active_in_sales"],
        start_month=_cust["customer_start_month"],
        end_month_norm=_end_month_norm_g,
    )
    _macro_cfg_plan = State.models_cfg.get("macro_demand", {}) or {}
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
    _avg_lines = 1.8 if (not _eff_skip and _max_lines_per_order > 1) else 1.0
    _orders_per_month_g = np.minimum(
        np.rint(_rows_per_month_g / _avg_lines).astype(np.int64), _rows_per_month_g)
    _orders_per_month_g = np.where(
        _rows_per_month_g > 0, np.maximum(_orders_per_month_g, 1), 0).astype(np.int64)

    # Distinct-customer target per month (canonical single source).
    # Mirrors the config reads the chunk builder used inline, but evaluated once
    # against global month totals.
    _cust_dmd = State.models_cfg.get("customers", {}) or {}
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
    _seasonal_spike_map: dict = {}
    for _entry in _spikes_raw:
        _mo = _entry.get("month") if isinstance(_entry, dict) else getattr(_entry, "month", None)
        _bo = _entry.get("boost") if isinstance(_entry, dict) else getattr(_entry, "boost", None)
        if _mo is not None and _bo is not None and 1 <= int(_mo) <= 12:
            _seasonal_spike_map[int(_mo)] = float(_bo)
    _max_distinct_ratio_g = _float_or(_macro_cfg_plan.get("max_distinct_ratio"), 0.70)
    _min_distinct_customers_g = _int_or(_macro_cfg_plan.get("min_distinct_customers"), 250)

    # Calendar month (1-12) per month offset (for the seasonal spike lookup).
    if _dp_months.size:
        _min_abs_month = int(_dp_months.min())
        _month_cal_index = ((np.arange(_T_months, dtype=np.int64) + _min_abs_month) % 12) + 1
    else:
        _month_cal_index = np.zeros(_T_months, dtype=np.int64)

    _cust["sales_rows_per_month"] = _rows_per_month_g
    _cust["sales_orders_per_month"] = _orders_per_month_g
    _cust["sales_distinct_target"] = compute_month_distinct_targets(
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

    # Worker configuration
    worker_cfg = _build_worker_cfg(
        _cust, _prod, _stores, _emps, _corr, _promos,
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
        month_stride=_day_stride, per_chunk_alloc=_per_chunk_alloc,
        order_id_int64=_order_id_int64, total_chunks=total_chunks,
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
    _shm.publish_dict(worker_cfg, [
        "product_np",
        "product_brand_key",
        "customers",              # alias for customer_keys
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
    ])

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

    # Final assembly (delta writes, CSV re-chunking, or parquet merge)
    return _assemble_output(
        file_format, tables, output_paths, collector,
        partition_cols, sales_cfg, sales_output, out_folder_p,
        chunk_size, delete_chunks, merge_parquet, compression,
        row_group_size, optimize_after_merge,
        order_id_int64=_order_id_int64,
    )
from .output_assembler import (
    ChunkResultCollector,
    SalesFactResult,
    SalesRunManifest,
    TableOutputs,
    _assemble_output,
    _merge_fact_csv_chunks,
    _merge_parquet_job,
)
