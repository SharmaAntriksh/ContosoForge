from __future__ import annotations

import glob
import logging
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
from .sales_worker import PoolRunSpec, iter_imap_unordered, _worker_task, init_sales_worker
from .sales_writer import merge_parquet_files, optimize_parquet
from .output_paths import (
    OutputPaths,
    TABLE_SALES,
    TABLE_SALES_ORDER_DETAIL,
    TABLE_SALES_ORDER_HEADER,
    TABLE_SALES_RETURN,
)

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

@dataclass(frozen=True)
class TableOutputs:
    table: str
    file_format: str
    chunks: list[Any]                 # list[str] for csv/parquet; list[{"part":..,"rows":..}] for delta
    merged_path: Optional[str] = None # parquet only
    delta_table_dir: Optional[str] = None  # delta only
    delta_parts_dir: Optional[str] = None  # delta only

@dataclass(frozen=True)
class SalesRunManifest:
    sales_output: str
    file_format: str
    out_folder: str
    tables: dict[str, TableOutputs]


@dataclass
class SalesFactResult:
    """Structured return from generate_sales_fact()."""
    chunk_files: List[str]
    manifest: SalesRunManifest
    budget_acc: Any = None
    inventory_acc: Any = None
    wishlists_acc: Any = None
    complaints_acc: Any = None


class ChunkResultCollector:
    """Collects per-chunk results from the multiprocessing pool.

    Replaces the _record_chunk_result closure with explicit state.
    """

    _TABLE_SHORT = {
        TABLE_SALES: "sales",
        TABLE_SALES_ORDER_DETAIL: "detail",
        TABLE_SALES_ORDER_HEADER: "header",
        TABLE_SALES_RETURN: "return",
    }

    def __init__(
        self,
        tables: list[str],
        budget_acc,
        inventory_acc,
        wishlists_acc,
        complaints_acc,
    ):
        self.tables = tables
        self.budget_acc = budget_acc
        self.inventory_acc = inventory_acc
        self.wishlists_acc = wishlists_acc
        self.complaints_acc = complaints_acc
        self.created_by_table: Dict[str, List[Any]] = {t: [] for t in tables}
        self.created_files: List[str] = []

    @staticmethod
    def _chunk_tag(path_like: str) -> str:
        b = os.path.basename(path_like)
        i = b.find("chunk")
        if i < 0:
            return b
        j = i + 5
        while j < len(b) and b[j].isdigit():
            j += 1
        return b[i:j]

    def record(self, r: Any, completed_units: int, total_units: int) -> None:
        # Extract streaming micro-aggregates (if present)
        if self.budget_acc is not None and isinstance(r, Mapping):
            self.budget_acc.add_sales(r.pop("_budget_agg", None))

        if self.inventory_acc is not None and isinstance(r, Mapping):
            self.inventory_acc.add(r.pop("_inventory_agg", None))

        if self.wishlists_acc is not None and isinstance(r, Mapping):
            self.wishlists_acc.add(r.pop("_wishlists_agg", None))

        if self.complaints_acc is not None and isinstance(r, Mapping):
            self.complaints_acc.add(r.pop("_complaints_agg", None))

        if isinstance(r, str):
            self.created_by_table.setdefault(TABLE_SALES, []).append(r)
            self.created_files.append(r)
            work(f"[{completed_units}/{total_units}] {self._chunk_tag(r)} -> sales")
            return

        if isinstance(r, Mapping):
            ordered_keys = (
                [t for t in self.tables if t in r]
                + [k for k in r.keys() if k not in set(self.tables)]
            )

            tag = None
            for k in ordered_keys:
                v = r.get(k)
                if isinstance(v, str):
                    tag = self._chunk_tag(v)
                    break

            produced: list[str] = []
            for table_name in ordered_keys:
                val = r.get(table_name)
                self.created_by_table.setdefault(table_name, []).append(val)
                if isinstance(val, str):
                    self.created_files.append(val)
                    produced.append(self._TABLE_SHORT.get(table_name, table_name))
                elif isinstance(val, Mapping) and "part" in val:
                    produced.append(self._TABLE_SHORT.get(table_name, table_name))

            if produced:
                if tag is None:
                    tag = "chunk"
                work(f"[{completed_units}/{total_units}] {tag} -> " + ", ".join(produced))
            return

        info(f"[{completed_units}/{total_units}] Worker returned unsupported result type: {type(r).__name__}")


# =====================================================================
# Helpers
# =====================================================================

def _as_np(x, dtype=None) -> np.ndarray:
    """Works for pandas Series/Index AND for already-materialized numpy arrays."""
    return np.asarray(x, dtype=dtype)


def _bool_mask(x) -> np.ndarray:
    """Ensure we always have a numpy bool mask."""
    return np.asarray(x, dtype=bool)


def ensure_dir(path: Union[str, Path]) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def load_parquet_column(path: Union[str, Path], col: str) -> np.ndarray:
    """
    Load a single parquet column as a numpy array.
    """
    s = pd.read_parquet(str(path), columns=[col])[col]
    return _as_np(s)


def load_parquet_df(path: Union[str, Path], cols: Optional[Sequence[str]] = None) -> pd.DataFrame:
    return pd.read_parquet(str(path), columns=list(cols) if cols is not None else None)


def _cfg_get(cfg: Any, path: Sequence[str], default: Any = None) -> Any:
    cur = cfg
    for k in path:
        if not isinstance(cur, Mapping):
            return default
        # Prefer attribute access (Pydantic models) over dict access
        if hasattr(cur, k):
            cur = getattr(cur, k)
        elif isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _apply_cfg_default(current: Any, default: Any, cfg_value: Any) -> Any:
    """
    Treat cfg as source-of-truth defaults when call-site leaves args at their defaults.
    """
    if cfg_value is None:
        return current
    return cfg_value if current == default else current


def _build_scd2_product_versions(
    products_path: Path,
    pool_product_ids: np.ndarray,
    pool_product_np: np.ndarray,
) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Build per-entity version lookup tables for SCD2 product resolution.

    Returns (starts, data):
      - starts: shape (N_pool, max_ver) int64 — EffectiveStartDate as epoch days,
        sorted ascending per entity, padded with INT64_MAX.
      - data: shape (N_pool, max_ver, 3) float64 — [ProductKey, ListPrice, UnitCost]
        per version slot, padded with IsCurrent=1 values.

    At lookup time: for a sale with epoch-day D on product pool index P,
    ``ver = searchsorted(starts[P], D, side='right') - 1`` gives the correct
    version slot.
    """
    try:
        all_df = pd.read_parquet(
            str(products_path),
            columns=["ProductID", "ProductKey", "ListPrice", "UnitCost",
                     "EffectiveStartDate", "EffectiveEndDate"],
        )
    except (KeyError, ValueError):
        return None

    all_df["eff_start_days"] = pd.to_datetime(all_df["EffectiveStartDate"]).values.astype("datetime64[D]").astype(np.int64)

    N_pool = len(pool_product_ids)

    # Vectorized pool index mapping via dense lookup array
    max_pid = max(int(pool_product_ids.max()), int(all_df["ProductID"].max())) + 1
    pid_lookup = np.full(max_pid, -1, dtype=np.int32)
    pid_lookup[pool_product_ids] = np.arange(N_pool, dtype=np.int32)

    pool_idx = pid_lookup[all_df["ProductID"].to_numpy()]
    mask = pool_idx >= 0
    pool_idx = pool_idx[mask]
    eff_start = all_df["eff_start_days"].to_numpy()[mask]
    pkey_arr = all_df["ProductKey"].to_numpy(dtype=np.float64)[mask]
    lprice_arr = all_df["ListPrice"].to_numpy(dtype=np.float64)[mask]
    ucost_arr = all_df["UnitCost"].to_numpy(dtype=np.float64)[mask]

    # Sort by (pool_idx, eff_start_days) via lexsort (secondary key first)
    order = np.lexsort((eff_start, pool_idx))
    pool_idx = pool_idx[order]
    eff_start = eff_start[order]
    pkey_arr = pkey_arr[order]
    lprice_arr = lprice_arr[order]
    ucost_arr = ucost_arr[order]

    # Compute per-entity version slot indices using group boundaries
    breaks = np.empty(len(pool_idx), dtype=np.int32)
    breaks[0] = 0
    breaks[1:] = np.cumsum(pool_idx[1:] != pool_idx[:-1])
    group_starts = np.concatenate([[0], np.where(pool_idx[1:] != pool_idx[:-1])[0] + 1])
    slot = np.arange(len(pool_idx), dtype=np.int32)
    slot -= np.repeat(group_starts, np.diff(np.append(group_starts, len(pool_idx))))

    max_ver = int(slot.max()) + 1 if len(slot) > 0 else 1

    # Initialize with IsCurrent=1 defaults
    starts = np.full((N_pool, max_ver), np.iinfo(np.int64).max, dtype=np.int64)
    data = np.empty((N_pool, max_ver, 3), dtype=np.float64)
    data[:, :, 0] = pool_product_np[:, 0:1]  # ProductKey broadcast
    data[:, :, 1] = pool_product_np[:, 1:2]  # ListPrice broadcast
    data[:, :, 2] = pool_product_np[:, 2:3]  # UnitCost broadcast

    # Cap slot indices at max_ver - 1
    valid = slot < max_ver
    pi = pool_idx[valid]
    si = slot[valid]

    # Vectorized scatter into output arrays
    starts[pi, si] = eff_start[valid]
    data[pi, si, 0] = pkey_arr[valid]
    data[pi, si, 1] = lprice_arr[valid]
    data[pi, si, 2] = ucost_arr[valid]

    # Clamp first version start to 0 (covers all time before second version)
    starts[pi, 0] = 0

    return starts, data


def _build_scd2_customer_versions(
    customers_path: Path,
    pool_customer_keys: np.ndarray,
    pool_customer_ids: np.ndarray,
) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Build per-entity version lookup tables for SCD2 customer resolution.

    Returns (starts, keys, key_to_pool_idx):
      - starts: shape (N_pool, max_ver) int64 — EffectiveStartDate as epoch days,
        sorted ascending per entity, padded with INT64_MAX.
      - keys: shape (N_pool, max_ver) int32 — CustomerKey per version slot,
        padded with IsCurrent=1 key.
      - key_to_pool_idx: dense int32 array mapping IsCurrent CustomerKey → pool index.

    At lookup time: for a sale with epoch-day D on customer pool index P,
    ``ver = searchsorted(starts[P], D, side='right') - 1`` gives the correct
    version slot.
    """
    try:
        all_df = pd.read_parquet(
            str(customers_path),
            columns=["CustomerID", "CustomerKey",
                     "EffectiveStartDate", "EffectiveEndDate"],
        )
    except (KeyError, ValueError):
        return None

    all_df["eff_start_days"] = pd.to_datetime(all_df["EffectiveStartDate"]).values.astype("datetime64[D]").astype(np.int64)

    N_pool = len(pool_customer_keys)

    # Vectorized reverse lookup: IsCurrent CustomerKey → pool index
    max_key = int(pool_customer_keys.max()) + 1
    key_to_pool_idx = np.full(max_key, -1, dtype=np.int32)
    key_to_pool_idx[pool_customer_keys] = np.arange(N_pool, dtype=np.int32)

    # Vectorized pool index mapping via dense lookup array
    max_cid = max(int(pool_customer_ids.max()), int(all_df["CustomerID"].max())) + 1
    cid_lookup = np.full(max_cid, -1, dtype=np.int32)
    cid_lookup[pool_customer_ids] = np.arange(N_pool, dtype=np.int32)

    all_cids = all_df["CustomerID"].to_numpy()
    pool_idx = cid_lookup[all_cids]
    mask = pool_idx >= 0
    pool_idx = pool_idx[mask]
    eff_start = all_df["eff_start_days"].to_numpy()[mask]
    ckey_arr = all_df["CustomerKey"].to_numpy(dtype=np.int32)[mask]

    # Sort by (pool_idx, eff_start_days) via lexsort (secondary key first)
    order = np.lexsort((eff_start, pool_idx))
    pool_idx = pool_idx[order]
    eff_start = eff_start[order]
    ckey_arr = ckey_arr[order]

    # Compute per-entity version slot indices using group boundaries
    group_starts = np.concatenate([[0], np.where(pool_idx[1:] != pool_idx[:-1])[0] + 1])
    slot = np.arange(len(pool_idx), dtype=np.int32)
    slot -= np.repeat(group_starts, np.diff(np.append(group_starts, len(pool_idx))))

    max_ver = int(slot.max()) + 1 if len(slot) > 0 else 1

    # Initialize: all slots default to IsCurrent=1 key, starts padded with MAX
    starts = np.full((N_pool, max_ver), np.iinfo(np.int64).max, dtype=np.int64)
    # Broadcast fill instead of np.tile (avoids full copy + intermediate array)
    keys = np.empty((N_pool, max_ver), dtype=np.int32)
    keys[:] = pool_customer_keys.astype(np.int32)[:, np.newaxis]

    # Cap slot indices at max_ver - 1
    valid = slot < max_ver
    pi = pool_idx[valid]
    si = slot[valid]

    # Vectorized scatter into output arrays
    starts[pi, si] = eff_start[valid]
    keys[pi, si] = ckey_arr[valid]

    # Clamp first version start to 0 (covers all time before second version)
    starts[pi, 0] = 0

    return starts, keys, key_to_pool_idx


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


def _normalize_dt_any(x) -> Union[pd.Series, pd.DatetimeIndex]:
    """
    Normalize date-like inputs to midnight.
    Handles Series (has .dt) and DatetimeIndex (has .normalize()).
    """
    dt = pd.to_datetime(x, errors="coerce")
    return dt.dt.normalize() if hasattr(dt, "dt") else dt.normalize()


def build_weighted_date_pool(start: str, end: str, seed: int = 42) -> Tuple[np.ndarray, np.ndarray]:
    """
    Build a weighted daily date pool with realistic seasonality.
    Returns:
      date_pool: datetime64[D] array
      date_prob: normalized probabilities
    """
    rng = np.random.default_rng(_int_or(seed, 42))

    dates = pd.date_range(start, end, freq="D")
    n = len(dates)
    if n <= 0:
        raise SalesError("Date range produced an empty pool")

    weekdays = _as_np(dates.weekday)

    # Weekday effect (0=Mon..6=Sun) — within-month date distribution only.
    # Retail-typical pattern: midweek soft, Friday strong, weekend peaks.
    # Year growth, monthly seasonality, promotional spikes, and one-off trends
    # are controlled by macro_demand settings in models.yaml.
    weekday_w = np.array([0.85, 0.85, 0.90, 0.95, 1.10, 1.20, 1.15], dtype=np.float64)
    wdw = weekday_w[weekdays]

    noise = rng.uniform(0.98, 1.02, size=n).astype(np.float64)

    weights = wdw * noise

    # Occasional zero-sales days (outages / closures); kept low so day-level
    # charts don't show a dead day every week.
    blackout_rate = rng.uniform(0.01, 0.03)
    blackout = rng.random(n) < blackout_rate
    weights[_bool_mask(blackout)] = 0.0

    total = float(weights.sum())
    if total <= 0:
        weights[:] = 1.0 / n
    else:
        weights /= total
        # Clamp last element to prevent FP rounding from leaving sum != 1.0,
        # which causes searchsorted out-of-bounds (CLAUDE.md gotcha #16).
        # max(0, ...) guards against FP overshoot making it negative.
        weights[-1] = max(0.0, 1.0 - weights[:-1].sum())

    return dates.to_numpy("datetime64[D]"), weights



def suggest_chunk_size(total_rows: int, target_workers: Optional[int] = None, preferred_chunks_per_worker: int = 2) -> int:
    if target_workers is None:
        target_workers = max(1, cpu_count() - 1)
    desired_chunks = max(1, int(target_workers) * int(preferred_chunks_per_worker))
    return max(1_000, int(ceil(int(total_rows) / desired_chunks)))


def batch_tasks(tasks: List[Tuple[int, int, int]], batch_size: int) -> List[List[Tuple[int, int, int]]]:
    if batch_size <= 1:
        return [[t] for t in tasks]
    return [tasks[i:i + batch_size] for i in range(0, len(tasks), batch_size)]


def _normalize_nullable_int_month(arr: Any, n: int) -> np.ndarray:
    """
    Normalize CustomerEndMonth into int64 with -1 meaning "no end".
    """
    if arr is None:
        return np.full(n, -1, dtype=np.int64)

    s = pd.Series(arr)
    v = pd.to_numeric(s, errors="coerce").fillna(-1).astype("int64").to_numpy(copy=True)
    v[v < 0] = -1
    if v.shape[0] != n:
        v = np.resize(v, n)
    return v


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


_CSV_COPY_BUF = 1 << 22  # 4 MiB block size for byte-level CSV concatenation


def _available_phys_bytes() -> int | None:
    """Best-effort available physical RAM in bytes (cross-platform).

    Returns None if it cannot be queried, in which case callers skip the cap.
    """
    try:
        if os.name == "nt":
            import ctypes

            class _MEMSTATEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = _MEMSTATEX(dwLength=ctypes.sizeof(_MEMSTATEX))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullAvailPhys)
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, AttributeError, ValueError):
        return None


def _inflight_bytes_per_order(bytes_per_order: int = 512, safety: float = 3.0) -> float:
    """Per-order resident bytes assumed in flight per worker.

    ``bytes_per_order`` ≈ avg_lines_per_order × row_width; ``safety`` covers the
    derived-column + group-by + returns transients; the ``2×`` is the ``2×
    processes`` submission window (up to two chunks per worker in flight).
    Shared by the projection and the cap so they stay exact inverses.
    """
    return float(bytes_per_order) * safety * 2.0


def _projected_peak_chunk_bytes(chunk_size: int, n_workers: int,
                                *, bytes_per_order: int = 512,
                                safety: float = 3.0) -> int:
    """Estimate peak resident bytes from in-flight chunks across all workers."""
    per_worker = _inflight_bytes_per_order(bytes_per_order, safety)
    return int(chunk_size * per_worker * max(1, n_workers))


def _cap_chunk_size_by_ram(chunk_size: int, n_workers: int,
                           *, bytes_per_order: int = 512,
                           safety: float = 3.0) -> int:
    """Shrink chunk_size so n_workers can each hold their in-flight chunks.

    Only used on the auto-tune path (``tune_chunk=True``), where chunk_size is
    already derived from the machine's worker count; pinned chunk_size values
    are never silently changed (determinism), only warned about.
    """
    avail = _available_phys_bytes()
    if not avail:
        return int(chunk_size)
    budget = avail * 0.75  # leave headroom for OS + page cache
    per_worker = budget / max(1, n_workers)
    max_orders = int(per_worker / _inflight_bytes_per_order(bytes_per_order, safety))
    return max(50_000, min(int(chunk_size), max_orders))


def _merge_parquet_job(job: tuple) -> None:
    """Pool task: merge one table's parquet chunks (top-level for spawn pickling)."""
    t, chunks, out, delete_after, compression = job
    merge_parquet_files(
        chunks, out, delete_after=delete_after,
        compression=compression, table_name=t, log=False,
    )


def _merge_fact_csv_chunks(
    csv_chunks: list,
    out_dir: Path,
    chunk_prefix: str,
    chunk_size: int,
    delete_chunks: bool,
) -> None:
    """Re-chunk CSV files for a sales fact table to respect chunk_size.

    Output files keep the existing chunk_prefix naming convention
    (e.g. ``sales_chunk0000.csv``, ``sales_return_chunk0000.csv``).

    Concatenation is done at the **byte** level (raw block copy with inline
    newline counting) rather than the old per-row Python loop, which decoded and
    re-encoded UTF-8 for every one of up to 10⁹ rows on a single GIL-bound core.
    A new output file is started at whole-source-chunk boundaries once the
    current file has reached ``chunk_size`` rows, so each output file holds
    approximately ``chunk_size`` rows (source chunks are never split mid-file).
    Exact per-file row counts are not preserved — no consumer depends on them
    (``BULK INSERT``/``COPY`` ignore per-file row counts, and the SQL generators
    enumerate whatever ``.csv`` files exist) — but the row *data* is identical.

    Files are written in binary mode so LF terminators pass through verbatim
    (no CRLF translation on Windows).

    Writes to a temporary directory first, then moves files into out_dir
    to avoid overwriting source chunks that share the same filename.
    """
    if not csv_chunks:
        return

    with open(csv_chunks[0], "rb") as f:
        header = f.readline()  # raw header bytes, including the line terminator

    # Write to a temp directory so we never clobber source chunks.
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(dir=out_dir, prefix=".merge_"))

    tmp_files: list[Path] = []
    out_f = None
    rows_in_current = 0
    file_idx = 0

    def _open_next():
        nonlocal out_f, rows_in_current, file_idx
        if out_f is not None:
            out_f.close()
        path = tmp_dir / f"{chunk_prefix}{file_idx:04d}.csv"
        tmp_files.append(path)
        out_f = open(path, "wb")
        out_f.write(header)
        rows_in_current = 0
        file_idx += 1

    try:
        _open_next()
        for chunk_path in csv_chunks:
            # Roll to a fresh output file at the chunk boundary once the current
            # file has met its row target (keeps whole source chunks intact).
            if rows_in_current >= chunk_size:
                _open_next()
            with open(chunk_path, "rb") as in_f:
                in_f.readline()  # skip this chunk's header
                while True:
                    buf = in_f.read(_CSV_COPY_BUF)
                    if not buf:
                        break
                    out_f.write(buf)
                    rows_in_current += buf.count(b"\n")  # data rows (header skipped)
    finally:
        if out_f is not None:
            out_f.close()

    # Remove original chunks
    for f in csv_chunks:
        try:
            f.unlink()
        except OSError:
            pass

    # Move merged files from temp into out_dir
    out_files: list[Path] = []
    for tmp_f in tmp_files:
        dest = out_dir / tmp_f.name
        try:
            tmp_f.replace(dest)
        except OSError as exc:
            raise PackagingError(f"Failed to move merged chunk {tmp_f.name} to {dest}: {exc}") from exc
        out_files.append(dest)

    # Clean up temp directory
    try:
        tmp_dir.rmdir()
    except OSError:
        pass


# =====================================================================
# Dimension Loading Helpers
# =====================================================================

def _load_customers(
    parquet_folder_p: Path,
    cfg,
    start_date,
    seed: int,
) -> dict:
    """Load customer dimension arrays for the sales pool.

    Returns a dict with all customer-related arrays that the caller
    needs for worker_cfg and correlation lookups.
    """
    customers_path = parquet_folder_p / "customers.parquet"

    # Single parquet read: discover available columns via schema, then
    # load ALL needed columns in one I/O call instead of 3-5 separate opens.
    _cust_schema_names = set(_pq.read_schema(str(customers_path)).names)

    _cust_load_cols = ["CustomerKey", "CustomerStartDate", "CustomerEndDate"]
    # Backward compat: also load legacy columns if they still exist
    _cust_legacy = ["IsActiveInSales", "CustomerStartMonth", "CustomerEndMonth"]
    # SCD2 columns (previously a separate parquet open)
    _cust_scd2_cols_wanted = ["CustomerID", "IsCurrent"]
    # Weight columns (previously a separate parquet open)
    _cust_weight_cols = ["CustomerBaseWeight", "CustomerWeight"]
    # Geo column (previously a separate parquet open)
    _cust_geo_cols = ["GeographyKey"]

    _cust_all_cols = list(dict.fromkeys(
        _cust_load_cols
        + [c for c in _cust_legacy if c in _cust_schema_names]
        + [c for c in _cust_scd2_cols_wanted if c in _cust_schema_names]
        + [c for c in _cust_weight_cols if c in _cust_schema_names]
        + [c for c in _cust_geo_cols if c in _cust_schema_names]
    ))

    cust_df_full = load_parquet_df(customers_path, _cust_all_cols)

    if cust_df_full.empty:
        raise SalesError("customers.parquet is empty; cannot generate sales")

    # --- SCD2 customer deduplication ---
    _cust_scd2_detected = False
    _cust_pool_ids = None   # CustomerID array (parallel to pool) — set if SCD2 active
    # Save pre-filter geo column before SCD2 filtering (needed later for geo mapping)
    _cust_geo_full = _as_np(cust_df_full["GeographyKey"], np.int32) if "GeographyKey" in cust_df_full.columns else None
    _cust_is_current_full = cust_df_full["IsCurrent"].values if "IsCurrent" in cust_df_full.columns else None

    if ("CustomerID" in cust_df_full.columns and "IsCurrent" in cust_df_full.columns
            and (cust_df_full["IsCurrent"] == 0).any()):
        _cust_scd2_detected = True
        _n_before = len(cust_df_full)
        # Keep only IsCurrent=1 rows for the sampling pool
        _is_current_mask = cust_df_full["IsCurrent"] == 1
        cust_df = cust_df_full[_is_current_mask].reset_index(drop=True)
        cust_df = cust_df.drop(columns=["IsCurrent"], errors="ignore")
        _cust_pool_ids = _as_np(cust_df["CustomerID"], np.int32)
        info(f"Customer SCD2: dedup {_n_before:,} -> {len(cust_df):,} rows (IsCurrent=1 pool)")
    else:
        cust_df = cust_df_full.drop(columns=["IsCurrent"], errors="ignore")

    # Free the full DataFrame — cust_df now holds the filtered pool
    del cust_df_full

    customer_keys = _as_np(cust_df["CustomerKey"], np.int32)

    # CustomerKey -> first EffectiveStartDate (epoch days) lookup. Eligibility
    # is month-granular but EffectiveStartDate is day-granular (random 0-27 day
    # offset within CustomerStartMonth), so without a per-row clamp the chunk
    # builder can place orders earlier in the start month than the customer's
    # actual join date. INT64_MIN fills unknown-key slots so np.maximum against
    # OrderDate is a natural no-op there. Used by chunk_builder.py.
    if "CustomerStartDate" in cust_df.columns:
        _cust_start_days = pd.to_datetime(
            cust_df["CustomerStartDate"], errors="coerce"
        ).values.astype("datetime64[D]").astype(np.int64)
        customer_first_eff_start_by_key = np.full(
            int(customer_keys.max()) + 1, np.iinfo(np.int64).min, dtype=np.int64,
        )
        customer_first_eff_start_by_key[customer_keys] = _cust_start_days
    else:
        customer_first_eff_start_by_key = None

    # --- Derive month indices from dates ---
    config_start = pd.to_datetime(start_date).to_period("M")

    if "CustomerStartMonth" in cust_df.columns:
        # Legacy path: use stored month indices
        customer_start_month = _as_np(cust_df["CustomerStartMonth"], np.int64)
    elif "CustomerStartDate" in cust_df.columns:
        cust_start_ts = pd.to_datetime(cust_df["CustomerStartDate"], errors="coerce")
        cust_start_period = cust_start_ts.dt.to_period("M")
        customer_start_month = (cust_start_period.apply(lambda p: p.ordinal) - config_start.ordinal).to_numpy(dtype=np.int64)
        customer_start_month = np.clip(customer_start_month, 0, None)
    else:
        customer_start_month = np.zeros(len(customer_keys), dtype=np.int64)

    if "CustomerEndMonth" in cust_df.columns:
        customer_end_month = _normalize_nullable_int_month(_as_np(cust_df["CustomerEndMonth"]), len(customer_keys))
    elif "CustomerEndDate" in cust_df.columns:
        cust_end_ts = pd.to_datetime(cust_df["CustomerEndDate"], errors="coerce")
        customer_end_month = np.full(len(customer_keys), -1, dtype=np.int64)
        valid_end = cust_end_ts.notna()
        if valid_end.any():
            end_periods = cust_end_ts[valid_end].dt.to_period("M")
            customer_end_month[valid_end.to_numpy()] = (end_periods.apply(lambda p: p.ordinal) - config_start.ordinal).to_numpy(dtype=np.int64)
    else:
        customer_end_month = np.full(len(customer_keys), -1, dtype=np.int64)

    # --- Derive is_active_in_sales ---
    # active_ratio marks a permanent inactive fraction (never transact).
    # Derived from seed for reproducibility without a persisted column.
    if "IsActiveInSales" in cust_df.columns:
        is_active_in_sales = _as_np(cust_df["IsActiveInSales"], np.int32)
    else:
        _cust_active_ratio = _float_or(
            _cfg_get(cfg, ["customers", "active_ratio"], 1.0), 1.0
        )
        N_cust = len(customer_keys)
        active_count = int(np.floor(N_cust * _cust_active_ratio))
        if 0 < active_count < N_cust:
            _ar_rng = np.random.default_rng(seed + 7)
            active_idx = _ar_rng.choice(N_cust, size=active_count, replace=False)
            is_active_in_sales = np.zeros(N_cust, dtype=np.int32)
            is_active_in_sales[active_idx] = 1
        else:
            is_active_in_sales = np.ones(N_cust, dtype=np.int32)

    # Extract customer weight from the already-loaded DataFrame (no extra parquet open)
    customer_base_weight = None
    for wcol in ("CustomerBaseWeight", "CustomerWeight"):
        if wcol in cust_df.columns:
            customer_base_weight = _as_np(cust_df[wcol], np.float64)
            break

    # --- Resolve customer_geo_key (for correlation lookups) ---
    # Dense array indexed by CustomerKey (not pool position) so that
    # geo-bias store sampling works even when keys are sparse (SCD2).
    customer_geo_key = None
    _pool_geo = None
    if _cust_geo_full is not None:
        if _cust_scd2_detected and _cust_is_current_full is not None:
            _pool_geo = _cust_geo_full[_cust_is_current_full == 1]
        else:
            _pool_geo = _cust_geo_full
    elif "GeographyKey" in cust_df.columns:
        _pool_geo = _as_np(cust_df["GeographyKey"], np.int32)

    if _pool_geo is not None:
        _max_ck = int(customer_keys.max()) + 1
        customer_geo_key = np.zeros(_max_ck, dtype=np.int32)
        customer_geo_key[customer_keys] = _pool_geo
    del _cust_geo_full, _cust_is_current_full, _pool_geo

    # --- Build customer SCD2 version tables ---
    _customer_scd2_active = False
    _customer_scd2_starts = None
    _customer_scd2_keys = None
    _cust_key_to_pool_idx = None

    if _cust_scd2_detected and _cust_pool_ids is not None:
        _cust_result = _build_scd2_customer_versions(
            customers_path, customer_keys, _cust_pool_ids,
        )
        if _cust_result is not None:
            _customer_scd2_starts, _customer_scd2_keys, _cust_key_to_pool_idx = _cust_result
            _customer_scd2_active = True
            info(f"Customer SCD2: {_customer_scd2_starts.shape[1]} max versions × "
                 f"{_customer_scd2_starts.shape[0]:,} customers")

    return {
        "customer_keys": customer_keys,
        "customer_start_month": customer_start_month,
        "customer_end_month": customer_end_month,
        "is_active_in_sales": is_active_in_sales,
        "customer_base_weight": customer_base_weight,
        "customer_geo_key": customer_geo_key,
        "customer_first_eff_start_by_key": customer_first_eff_start_by_key,
        "customer_scd2_active": _customer_scd2_active,
        "customer_scd2_starts": _customer_scd2_starts,
        "customer_scd2_keys": _customer_scd2_keys,
        "cust_key_to_pool_idx": _cust_key_to_pool_idx,
    }


def _load_products(
    parquet_folder_p: Path,
    cfg,
    seed: int,
    start_date,
    end_date,
    active_product_np=None,
) -> dict:
    """Load product dimension arrays, profile, date pool, and SCD2 versions.

    Returns a dict with all product-related arrays plus date_pool/date_prob.
    """
    product_brand_key = None
    brand_names = None
    product_subcat_key = None
    products_path = parquet_folder_p / "products.parquet"
    assortment_cfg = (getattr(cfg, "stores", None) or {}).get("assortment") or {}

    def _brand_codes_from_series(s: pd.Series) -> tuple[np.ndarray, np.ndarray]:
        # Guarantee no NA => no -1 codes
        s2 = s.fillna("Unknown").astype(str)
        codes, uniques = pd.factorize(s2, sort=True)
        return np.asarray(codes, dtype=np.int32), np.asarray(uniques, dtype=object)

    # Read schema once (metadata only) to discover available columns
    _prod_schema = set(_pq.read_schema(str(products_path)).names)
    _has_brand_col = "Brand" in _prod_schema
    _has_subcat_col = "SubcategoryKey" in _prod_schema
    _need_subcat = bool(assortment_cfg.get("enabled")) and _has_subcat_col

    # Determine SCD2 capability upfront so we include required columns in
    # the single product load below (avoids extra parquet opens later).
    _prod_has_scd2 = (
        "ProductID" in _prod_schema
        and "IsCurrent" in _prod_schema
        and "EffectiveStartDate" in _prod_schema
    )

    # Cached full product DataFrame — reused for SCD2 version table building
    _prod_df_full = None

    if active_product_np is not None:
        product_np = active_product_np
        active_keys = np.asarray(product_np[:, 0], dtype=np.int32)

        # Single load: Brand + optional SubcategoryKey + SCD2 columns (one parquet open)
        _prod_cols = ["ProductKey"]
        if _has_brand_col:
            _prod_cols.append("Brand")
        if _need_subcat:
            _prod_cols.append("SubcategoryKey")
        # Include SCD2 columns so we can reuse this DataFrame later
        if _prod_has_scd2:
            for _sc in ("ProductID", "IsCurrent"):
                if _sc not in _prod_cols:
                    _prod_cols.append(_sc)

        try:
            _prod_df = load_parquet_df(products_path, _prod_cols)
            # Keep the full DF for SCD2 use before dedup
            if _prod_has_scd2:
                _prod_df_full = _prod_df
            _prod_df_dedup = _prod_df.drop_duplicates("ProductKey", keep="first")
            _prod_keys = _prod_df_dedup["ProductKey"].to_numpy(dtype=np.int32)

            # Sorted-key lookup (avoids pandas reindex float64 promotion)
            _sort_idx = np.argsort(_prod_keys)
            _sorted_keys = _prod_keys[_sort_idx]
            _pos = np.clip(np.searchsorted(_sorted_keys, active_keys), 0, max(len(_sorted_keys) - 1, 0))
            _found = _sorted_keys[_pos] == active_keys

            if _has_brand_col:
                codes, brand_names = _brand_codes_from_series(_prod_df_dedup["Brand"])
                bk = np.full(len(active_keys), -1, dtype=np.int32)
                bk[_found] = codes[_sort_idx][_pos[_found]]
                if np.any(bk < 0):
                    info("Brand mapping missing/invalid for some ProductKeys; disabling brand_popularity for this run.")
                else:
                    product_brand_key = bk

            if _need_subcat and "SubcategoryKey" in _prod_df_dedup.columns:
                subcat_vals = _prod_df_dedup["SubcategoryKey"].to_numpy(dtype=np.int32)
                sc = np.zeros(len(active_keys), dtype=np.int32)
                sc[_found] = subcat_vals[_sort_idx][_pos[_found]]
                product_subcat_key = sc

            del _prod_df_dedup

        except (KeyError, ValueError, TypeError, OSError) as exc:
            info(f"Could not load/derive Brand from products.parquet ({type(exc).__name__}: {exc}); "
                 "disabling brand_popularity for this run.")
            product_brand_key = None

    else:
        # Full product path — single load with all needed columns
        _prod_cols = ["ProductKey", "ListPrice", "UnitCost"]
        if _has_brand_col:
            _prod_cols.append("Brand")
        if _need_subcat:
            _prod_cols.append("SubcategoryKey")
        # SCD2: load IsCurrent + ProductID to reuse for version table building
        if "IsCurrent" in _prod_schema:
            _prod_cols.append("IsCurrent")
        if _prod_has_scd2 and "ProductID" in _prod_schema:
            _prod_cols.append("ProductID")

        prod_df = load_parquet_df(products_path, _prod_cols)
        # Keep the full DF for SCD2 use before filtering
        if _prod_has_scd2:
            _prod_df_full = prod_df
        # SCD2: only use current version rows for the product pool
        if "IsCurrent" in prod_df.columns:
            prod_df = prod_df[prod_df["IsCurrent"] == 1].copy()
            prod_df = prod_df.drop(columns=["IsCurrent"], errors="ignore")
        if "ProductID" in prod_df.columns:
            prod_df = prod_df.drop(columns=["ProductID"], errors="ignore")
        prod_df["ProductKey"] = pd.to_numeric(prod_df["ProductKey"], errors="coerce")
        prod_df["ListPrice"] = pd.to_numeric(prod_df["ListPrice"], errors="coerce")
        prod_df["UnitCost"] = pd.to_numeric(prod_df["UnitCost"], errors="coerce")
        prod_df = prod_df.dropna(subset=["ProductKey", "ListPrice", "UnitCost"])
        prod_df["ProductKey"] = prod_df["ProductKey"].astype("int32", copy=False)

        product_np = np.column_stack([
            prod_df["ProductKey"].to_numpy(dtype=np.int32, copy=False),
            prod_df["ListPrice"].to_numpy(dtype=np.float64, copy=False),
            prod_df["UnitCost"].to_numpy(dtype=np.float64, copy=False),
        ])

        if _has_brand_col:
            codes, brand_names = _brand_codes_from_series(prod_df["Brand"])
            product_brand_key = codes if not np.any(codes < 0) else None

        if _need_subcat and "SubcategoryKey" in prod_df.columns:
            product_subcat_key = prod_df["SubcategoryKey"].to_numpy(dtype=np.int32)

    # PopularityScore + SeasonalityProfile from ProductProfile (for weighted sampling)
    product_popularity = None
    product_seasonality = None
    _profile_path = parquet_folder_p / "product_profile.parquet"
    if _profile_path.exists():
        try:
            _pp_df = load_parquet_df(_profile_path, ["ProductKey", "PopularityScore", "SeasonalityProfile"])
            _pp_df = _pp_df.drop_duplicates("ProductKey", keep="first")
            _pp_df["ProductKey"] = _pp_df["ProductKey"].astype("int32")
            active_keys = np.asarray(product_np[:, 0], dtype=np.int32)
            _pp_map_pop = pd.Series(
                _pp_df["PopularityScore"].to_numpy(dtype=np.float64),
                index=_pp_df["ProductKey"].to_numpy(dtype=np.int32),
            )
            product_popularity = _pp_map_pop.reindex(active_keys).fillna(50.0).to_numpy(dtype=np.float64)
            _pp_map_sea = pd.Series(
                _pp_df["SeasonalityProfile"].to_numpy().astype(str),
                index=_pp_df["ProductKey"].to_numpy(dtype=np.int32),
            )
            _sea_str = _pp_map_sea.reindex(active_keys).fillna("None").to_numpy().astype(str)
            # Encode as int8 so it can be shared via shared memory (avoids 8x pickle of object array)
            _SEASON_ENCODE = {"Holiday": 1, "Winter": 2, "Summer": 3, "BackToSchool": 4, "Spring": 5}
            product_seasonality = np.zeros(len(_sea_str), dtype=np.int8)
            for _sname, _scode in _SEASON_ENCODE.items():
                product_seasonality[_sea_str == _sname] = _scode
            # Warn on unmapped profiles so typos / new categories don't get
            # silently bucketed as "no seasonal boost".
            _known = set(_SEASON_ENCODE) | {"None", "nan"}
            _unknown = [s for s in np.unique(_sea_str).tolist() if s not in _known]
            if _unknown:
                warn(
                    f"Unknown SeasonalityProfile values in product_profile.parquet "
                    f"(no boost applied): {_unknown}. "
                    f"Expected one of: {sorted(_SEASON_ENCODE)}."
                )
        except (KeyError, ValueError, TypeError, OSError) as exc:
            info(f"Could not load product profile ({type(exc).__name__}: {exc}); using uniform product sampling.")
            product_popularity = None
            product_seasonality = None

    # Weighted date pool (deterministic) — needed by SCD2 grid builders below
    date_pool, date_prob = build_weighted_date_pool(start_date, end_date, seed)

    # --- SCD2 product version lookup tables ---
    _product_scd2_active = False
    _product_scd2_starts = None   # (N_pool, max_ver) int64
    _product_scd2_data = None     # (N_pool, max_ver, 3) float64

    # Detect product SCD2 and build version tables (reusing _prod_df_full
    # from the single product load above — no extra parquet opens).
    if _prod_has_scd2 and _prod_df_full is not None:
        try:
            _has_history = (_prod_df_full["IsCurrent"] == 0).any()
        except (KeyError, ValueError):
            _has_history = False

        if _has_history:
            try:
                _pid_current = _prod_df_full[_prod_df_full["IsCurrent"] == 1].drop_duplicates("ProductKey", keep="first")
                _pid_map = pd.Series(
                    _pid_current["ProductID"].to_numpy(dtype=np.int32),
                    index=_pid_current["ProductKey"].to_numpy(dtype=np.int32),
                )
                _pool_keys = np.asarray(product_np[:, 0], dtype=np.int32)
                _reindexed = _pid_map.reindex(_pool_keys)
                _unmapped = _reindexed.isna()
                if _unmapped.any():
                    info(f"Product SCD2: {int(_unmapped.sum())} pool keys have no "
                         f"IsCurrent=1 ProductID mapping; they will use current-version prices.")
                _pool_product_ids = _reindexed.fillna(-1).to_numpy(dtype=np.int32)
                del _pid_current, _pid_map, _reindexed

                _prod_result = _build_scd2_product_versions(
                    products_path, _pool_product_ids, product_np,
                )
                if _prod_result is not None:
                    _product_scd2_starts, _product_scd2_data = _prod_result
                    _product_scd2_active = True
                    info(f"Product SCD2: {_product_scd2_starts.shape[1]} max versions × "
                         f"{_product_scd2_starts.shape[0]:,} products")
            except (KeyError, ValueError, TypeError, OSError) as exc:
                info(f"Product SCD2 build failed ({type(exc).__name__}: {exc}); "
                     "using current-version prices for all months.")

    # Free the full product DataFrame now that SCD2 is built
    del _prod_df_full

    return {
        "product_np": product_np,
        "product_brand_key": product_brand_key,
        "brand_names": brand_names,
        "product_subcat_key": product_subcat_key,
        "product_popularity": product_popularity,
        "product_seasonality": product_seasonality,
        "date_pool": date_pool,
        "date_prob": date_prob,
        "product_scd2_active": _product_scd2_active,
        "product_scd2_starts": _product_scd2_starts,
        "product_scd2_data": _product_scd2_data,
        "assortment_cfg": assortment_cfg,
    }


# =====================================================================
# Helpers (extracted from generate_sales_fact)
# =====================================================================

def _load_stores(parquet_folder_p, end_date):
    """Load store dimension arrays for the sales pool."""
    _store_cols = ["StoreKey", "GeographyKey"]
    _store_path = parquet_folder_p / "stores.parquet"
    if _store_path.exists():
        _store_schema_names = set(_pq.read_schema(str(_store_path)).names)
        if "StoreType" in _store_schema_names:
            _store_cols.append("StoreType")
        if "OpeningDate" in _store_schema_names:
            _store_cols.append("OpeningDate")
        if "ClosingDate" in _store_schema_names:
            _store_cols.append("ClosingDate")
        if "RenovationStartDate" in _store_schema_names:
            _store_cols.append("RenovationStartDate")
        if "RenovationEndDate" in _store_schema_names:
            _store_cols.append("RenovationEndDate")
    store_df = load_parquet_df(_store_path, _store_cols)
    store_keys = _as_np(store_df["StoreKey"], np.int32)
    store_to_geo = dict(zip(_as_np(store_df["StoreKey"], np.int32), _as_np(store_df["GeographyKey"], np.int32)))

    store_open_month = None
    store_close_month = None
    store_open_day = None
    store_close_day = None
    _FAR_PAST_DAY = np.datetime64("1900-01-01", "D")
    _FAR_FUTURE_DAY = np.datetime64("2262-04-11", "D")
    if "OpeningDate" in store_df.columns:
        _open_dt = pd.to_datetime(store_df["OpeningDate"]).values.astype("datetime64[M]")
        store_open_month = _open_dt.astype("int64").astype(np.int64)
        _open_dt_d = pd.to_datetime(store_df["OpeningDate"]).values.astype("datetime64[D]")
        _open_nat = np.isnat(_open_dt_d)
        _open_dt_d[_open_nat] = _FAR_PAST_DAY
        store_open_day = _open_dt_d
    if "ClosingDate" in store_df.columns:
        _close_dt = pd.to_datetime(store_df["ClosingDate"]).values
        _close_nat_mask = np.isnat(_close_dt)
        _close_m = _close_dt.astype("datetime64[M]").astype("int64").astype(np.int64)
        _close_m[_close_nat_mask] = np.iinfo(np.int64).max
        store_close_month = _close_m
        _close_dt_d = _close_dt.astype("datetime64[D]")
        _close_dt_d[_close_nat_mask] = _FAR_FUTURE_DAY
        store_close_day = _close_dt_d

    store_reno_start_day = None
    store_reno_end_day = None
    if "RenovationStartDate" in store_df.columns and "RenovationEndDate" in store_df.columns:
        _rs_dt = pd.to_datetime(store_df["RenovationStartDate"]).values.astype("datetime64[D]")
        _re_dt = pd.to_datetime(store_df["RenovationEndDate"]).values.astype("datetime64[D]")
        _rs_nat = np.isnat(_rs_dt)
        _re_nat = np.isnat(_re_dt)
        # Use sentinels that never overlap any month: start = far future, end = far past.
        _rs_dt[_rs_nat | _re_nat] = _FAR_FUTURE_DAY
        _re_dt[_rs_nat | _re_nat] = _FAR_PAST_DAY
        store_reno_start_day = _rs_dt
        store_reno_end_day = _re_dt

    store_type_map = None
    if "StoreType" in store_df.columns:
        store_type_map = dict(zip(
            store_df["StoreKey"].astype(int).tolist(),
            store_df["StoreType"].astype(str).tolist(),
        ))

    # Geography + currency mapping
    geo_df = load_parquet_df(parquet_folder_p / "geography.parquet", ["GeographyKey", "ISOCode"])
    currency_df = load_parquet_df(parquet_folder_p / "currency.parquet", ["CurrencyKey", "CurrencyCode"])

    geo_df = geo_df.merge(currency_df, left_on="ISOCode", right_on="CurrencyCode", how="left")
    if geo_df["CurrencyKey"].isna().any():
        n_missing = int(geo_df["CurrencyKey"].isna().sum())
        missing_isos = geo_df.loc[geo_df["CurrencyKey"].isna(), "ISOCode"].unique().tolist()
        default_currency = int(currency_df.iloc[0]["CurrencyKey"])
        geo_df["CurrencyKey"] = geo_df["CurrencyKey"].fillna(default_currency)
        info(f"WARNING: {n_missing} geography row(s) have no currency match (ISOs: {missing_isos}); "
             f"defaulting to CurrencyKey={default_currency}.")

    geo_to_currency = dict(zip(_as_np(geo_df["GeographyKey"], np.int32), _as_np(geo_df["CurrencyKey"], np.int32)))

    return {
        "store_keys": store_keys,
        "store_to_geo": store_to_geo,
        "store_open_month": store_open_month,
        "store_close_month": store_close_month,
        "store_open_day": store_open_day,
        "store_close_day": store_close_day,
        "store_reno_start_day": store_reno_start_day,
        "store_reno_end_day": store_reno_end_day,
        "store_type_map": store_type_map,
        "geo_to_currency": geo_to_currency,
    }


def _load_promotions(parquet_folder_p, promo_df=None):
    """Load promotion arrays for the sales pool."""
    if promo_df is None:
        promo_df = load_parquet_df(parquet_folder_p / "promotions.parquet")

    if promo_df.empty:
        return {
            "promo_df": promo_df,
            "promo_keys_all": np.array([], dtype=np.int32),
            "promo_start_all": np.array([], dtype="datetime64[D]"),
            "promo_end_all": np.array([], dtype="datetime64[D]"),
            "new_customer_promo_keys": np.array([], dtype=np.int32),
        }

    promo_start = _normalize_dt_any(promo_df["StartDate"])
    promo_end = _normalize_dt_any(promo_df["EndDate"])

    promo_keys_all = _as_np(promo_df["PromotionKey"], np.int32)
    promo_start_all = _as_np(promo_start, "datetime64[D]")
    promo_end_all = _as_np(promo_end, "datetime64[D]")

    if "PromotionType" in promo_df.columns:
        nc_mask = promo_df["PromotionType"].astype(str) == "New Customer"
        new_customer_promo_keys = _as_np(promo_df.loc[nc_mask, "PromotionKey"], np.int32)
    else:
        new_customer_promo_keys = np.array([], dtype=np.int32)

    return {
        "promo_df": promo_df,
        "promo_keys_all": promo_keys_all,
        "promo_start_all": promo_start_all,
        "promo_end_all": promo_end_all,
        "new_customer_promo_keys": new_customer_promo_keys,
    }


def _load_employees(parquet_folder_p, cfg, end_date):
    """Load employee store assignments for salesperson resolution."""
    emp_assign_path = parquet_folder_p / "employee_store_assignments.parquet"

    if not emp_assign_path.exists():
        raise FileNotFoundError(
            f"employee_store_assignments.parquet is required: {emp_assign_path}. "
            f"Run dimension generation first."
        )

    salesperson_roles = _cfg_get(cfg, ["sales", "salesperson_roles"], default=None)
    if not (isinstance(salesperson_roles, list) and salesperson_roles):
        primary = _cfg_get(cfg, ["employees", "store_assignments", "primary_sales_role"], default="Sales Associate")
        salesperson_roles = [str(primary), ONLINE_SALES_REP_ROLE]

    _esa_base_cols = [
        "EmployeeKey", "StoreKey", "StartDate", "EndDate",
        "FTE", "RoleAtStore",
    ]
    try:
        emp_assign_df = load_parquet_df(
            emp_assign_path, cols=_esa_base_cols + ["IsPrimary"],
        )
    except (KeyError, ValueError):
        emp_assign_df = load_parquet_df(emp_assign_path, cols=_esa_base_cols)

    if "RoleAtStore" in emp_assign_df.columns:
        emp_assign_df = emp_assign_df[emp_assign_df["RoleAtStore"].isin(salesperson_roles)].copy()

    if emp_assign_df.empty:
        raise SalesError(
            f"No employee assignments with role in {salesperson_roles} found in "
            f"{emp_assign_path}. Check employees.store_assignments.primary_sales_role "
            f"and ensure the bridge has been regenerated."
        )

    end_dt = pd.to_datetime(end_date, errors="coerce").normalize()

    start_dt = pd.to_datetime(emp_assign_df["StartDate"], errors="coerce").dt.normalize()
    end_dt_col = pd.to_datetime(emp_assign_df["EndDate"], errors="coerce").dt.normalize()
    end_dt_col = end_dt_col.fillna(end_dt)

    result = {
        "employee_assign_store_key": _as_np(emp_assign_df["StoreKey"], np.int32),
        "employee_assign_employee_key": _as_np(emp_assign_df["EmployeeKey"], np.int32),
        "employee_assign_start_date": _as_np(start_dt, "datetime64[D]"),
        "employee_assign_end_date": _as_np(end_dt_col, "datetime64[D]"),
        "employee_assign_role": _as_np(emp_assign_df["RoleAtStore"].astype(str)),
        "employee_assign_fte": None,
        "employee_assign_is_primary": None,
        "salesperson_roles": salesperson_roles,
    }
    if "FTE" in emp_assign_df.columns:
        result["employee_assign_fte"] = _as_np(emp_assign_df["FTE"], np.float64)
    if "IsPrimary" in emp_assign_df.columns:
        result["employee_assign_is_primary"] = _as_np(emp_assign_df["IsPrimary"], bool)
    return result


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
    returns_logistics_keys, returns_event_key_capacity,
    month_stride=0, per_chunk_alloc=0, order_id_int64=False,
):
    """Build the worker_cfg dict from typed containers."""
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
        promo_keys_all=promos["promo_keys_all"],
        promo_start_all=promos["promo_start_all"],
        promo_end_all=promos["promo_end_all"],
        new_customer_promo_keys=promos["new_customer_promo_keys"],
        new_customer_window_months=int((getattr(cfg, "promotions", None) or {}).get("new_customer_window_months", 3)),

        customers=cust["customer_keys"],
        customer_keys=cust["customer_keys"],
        customer_is_active_in_sales=cust["is_active_in_sales"],
        customer_start_month=cust["customer_start_month"],
        customer_end_month=cust["customer_end_month"],
        customer_base_weight=cust["customer_base_weight"],
        customer_first_eff_start_by_key=cust["customer_first_eff_start_by_key"],

        store_to_geo=stores["store_to_geo"],
        geo_to_currency=stores["geo_to_currency"],
        date_pool=prod["date_pool"],
        date_prob=prod["date_prob"],

        output_paths=output_paths.to_dict() if hasattr(output_paths, "to_dict") else {
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
        returns_split_min_gap=int(returns_split_min_gap),
        returns_split_max_gap=int(returns_split_max_gap),
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
    if assortment_cfg.get("enabled") and product_subcat_key is not None and stores["store_type_map"] is not None:
        worker_cfg["assortment"] = dict(assortment_cfg)
        worker_cfg["product_subcat_key"] = product_subcat_key
        worker_cfg["store_type_map"] = stores["store_type_map"]
        info("Store-product assortment: enabled")

    return worker_cfg


def _build_correlation_lookups(
    parquet_folder_p, store_keys, store_to_geo, store_type_map,
    product_np, promo_keys_all, promo_df,
):
    """Build all column-correlation lookup arrays for workers."""
    # 1) Geography: customer -> country, store -> country, country -> stores
    _geo_country_df = load_parquet_df(parquet_folder_p / "geography.parquet", ["GeographyKey", "Country"])
    _unique_countries = _geo_country_df["Country"].fillna("Unknown").unique()
    _country_to_id = {c: i for i, c in enumerate(_unique_countries)}
    _n_countries = len(_unique_countries)

    _max_geo = int(_geo_country_df["GeographyKey"].max()) if len(_geo_country_df) else 0
    geo_to_country_id = np.full(_max_geo + 1, 0, dtype=np.int32)
    _geo_keys = _geo_country_df["GeographyKey"].to_numpy(dtype=np.int32)
    _geo_countries = _geo_country_df["Country"].fillna("Unknown").map(_country_to_id).to_numpy(dtype=np.int32)
    geo_to_country_id[_geo_keys] = _geo_countries

    _max_sk = int(store_keys.max()) if store_keys.size else 0
    store_to_country_id = np.full(_max_sk + 1, 0, dtype=np.int32)
    _sg_sk = np.fromiter(store_to_geo.keys(), dtype=np.int32, count=len(store_to_geo))
    _sg_gk = np.fromiter(store_to_geo.values(), dtype=np.int32, count=len(store_to_geo))
    _sg_valid = (_sg_sk <= _max_sk) & (_sg_gk <= _max_geo)
    store_to_country_id[_sg_sk[_sg_valid]] = geo_to_country_id[_sg_gk[_sg_valid]]

    _sk_country_ids = store_to_country_id[store_keys.astype(np.int32)]
    country_to_store_keys = [
        store_keys[_sk_country_ids == cid].astype(np.int32)
        for cid in range(_n_countries)
    ]

    # 2) Store type -> valid SalesChannelKeys
    store_channel_keys_list = [None] * (_max_sk + 1)
    channel_prob_by_store_list = [None] * (_max_sk + 1)
    if store_type_map is not None:
        for sk in store_keys:
            sk_int = int(sk)
            st = store_type_map.get(sk_int, "")
            keys, probs = STORE_TYPE_CHANNEL_MAP.get(st, DEFAULT_CHANNEL_MAP)
            store_channel_keys_list[sk_int] = keys
            channel_prob_by_store_list[sk_int] = probs / probs.sum()
    else:
        _uniform_p = np.ones(len(ALL_CHANNELS), dtype=np.float64) / len(ALL_CHANNELS)
        for sk in store_keys:
            store_channel_keys_list[int(sk)] = ALL_CHANNELS
            channel_prob_by_store_list[int(sk)] = _uniform_p

    # 3) Product channel eligibility (from ProductProfile)
    product_channel_eligible = None
    _profile_path = parquet_folder_p / "product_profile.parquet"
    if _profile_path.exists():
        try:
            _elig_cols = ["ProductKey", "EligibleStore", "EligibleOnline", "EligibleMarketplace", "EligibleB2B"]
            _elig_df = pd.read_parquet(str(_profile_path), columns=_elig_cols)
            _prod_keys_arr = product_np[:, 0].astype(np.int32)
            _max_pk = int(_prod_keys_arr.max()) if _prod_keys_arr.size else 0
            _pk_to_row = np.full(_max_pk + 1, -1, dtype=np.int32)
            _pk_to_row[_prod_keys_arr] = np.arange(len(_prod_keys_arr), dtype=np.int32)
            product_channel_eligible = np.ones((len(product_np), 4), dtype=np.int8)
            _elig_pks = _elig_df["ProductKey"].to_numpy(dtype=np.int32)
            _elig_mask = (_elig_pks <= _max_pk)
            _elig_pks_valid = _elig_pks[_elig_mask]
            _elig_rows = _pk_to_row[_elig_pks_valid]
            _mapped = _elig_rows >= 0
            _ri = _elig_rows[_mapped]
            _elig_mask_idx = np.where(_elig_mask)[0][_mapped]
            for col_idx, col_name in enumerate(["EligibleStore", "EligibleOnline",
                                                 "EligibleMarketplace", "EligibleB2B"]):
                product_channel_eligible[_ri, col_idx] = (
                    _elig_df[col_name].to_numpy(dtype=np.int8)[_elig_mask_idx]
                )
        except (KeyError, OSError):
            pass

    # 4) Promotion channel group
    promo_channel_group = np.zeros(len(promo_keys_all), dtype=np.int8)
    if not promo_df.empty and "PromotionCategory" in promo_df.columns:
        _cat_series = promo_df["PromotionCategory"].astype(str)
        promo_channel_group[_cat_series.isin({"Store", "Physical"}).to_numpy()] = 1
        promo_channel_group[_cat_series.isin({"Online", "Digital"}).to_numpy()] = 2

    # 5) Channel fulfillment days
    channel_fulfillment_days = DEFAULT_CHANNEL_FULFILLMENT_DAYS.copy()
    _sc_path = parquet_folder_p / "sales_channels.parquet"
    if _sc_path.exists():
        try:
            _sc_df = pd.read_parquet(str(_sc_path))
            if "TypicalFulfillmentDays" in _sc_df.columns and "SalesChannelKey" in _sc_df.columns:
                _sc_keys = _sc_df["SalesChannelKey"].to_numpy(dtype=np.int32)
                _sc_days = _sc_df["TypicalFulfillmentDays"]
                _sc_valid = (_sc_keys >= 0) & (_sc_keys < len(channel_fulfillment_days)) & _sc_days.notna()
                channel_fulfillment_days[_sc_keys[_sc_valid]] = _sc_days.to_numpy(dtype=np.int32)[_sc_valid]
        except (KeyError, OSError):
            pass

    return {
        "geo_to_country_id": geo_to_country_id,
        "store_to_country_id": store_to_country_id,
        "country_to_store_keys": country_to_store_keys,
        "store_channel_keys": store_channel_keys_list,
        "channel_prob_by_store": channel_prob_by_store_list,
        "product_channel_eligible": product_channel_eligible,
        "promo_channel_group": promo_channel_group,
        "channel_fulfillment_days": channel_fulfillment_days,
        "_channel_to_elig_group": CHANNEL_TO_ELIG_GROUP,
    }


def _prebuild_shared_structures(
    worker_cfg, _shm, prod, stores, emps, seed,
):
    """Pre-build expensive derived structures in main process for shared memory."""
    product_brand_key = prod["product_brand_key"]
    store_keys = stores["store_keys"]
    store_type_map = stores["store_type_map"]
    product_subcat_key = prod["product_subcat_key"]
    assortment_cfg = prod["assortment_cfg"]
    date_pool = prod["date_pool"]
    employee_assign_store_key = emps["employee_assign_store_key"]
    employee_assign_employee_key = emps["employee_assign_employee_key"]
    employee_assign_start_date = emps["employee_assign_start_date"]
    employee_assign_end_date = emps["employee_assign_end_date"]
    employee_assign_fte = emps["employee_assign_fte"]
    employee_assign_is_primary = emps["employee_assign_is_primary"]
    from .sales_worker.init import (
        _build_store_subcat_matrix,
        _build_brand_prob_by_month_rotate_winner,
        _build_salesperson_effective_by_store,
        _DEFAULT_ASSORTMENT_COVERAGE,
        infer_T_from_date_pool,
        int_or,
        float_or,
    )

    # 1) brand_to_row_idx — sorted index + offsets for zero-copy brand buckets
    _brand_product_counts = None
    if product_brand_key is not None:
        _bk = np.asarray(product_brand_key, dtype=np.int32)
        _brand_product_counts = np.bincount(_bk).astype(np.float64)
        _brand_order = np.argsort(_bk, kind="mergesort").astype(np.int32)
        _bk_sorted = _bk[_brand_order]
        _brand_starts = np.flatnonzero(np.r_[True, _bk_sorted[1:] != _bk_sorted[:-1]])
        B = int(_bk.max()) + 1
        _brand_offsets = np.zeros(B + 1, dtype=np.int64)
        for s_idx in range(len(_brand_starts)):
            k = int(_bk_sorted[_brand_starts[s_idx]])
            e = int(_brand_starts[s_idx + 1]) if s_idx + 1 < len(_brand_starts) else len(_bk_sorted)
            _brand_offsets[k + 1] = e
        np.maximum.accumulate(_brand_offsets, out=_brand_offsets)
        worker_cfg["_brand_flat_idx"] = _shm.publish("brand_flat_idx", _brand_order)
        worker_cfg["_brand_flat_offsets"] = _shm.publish("brand_flat_off", _brand_offsets)
        del _brand_order, _bk_sorted, _brand_starts, _brand_offsets

    # 2) store-product assortment — compact subcat matrix
    if assortment_cfg.get("enabled") and product_subcat_key is not None and store_type_map is not None:
        store_type_arr = np.array(
            [str(store_type_map.get(int(sk), "Supermarket")) for sk in store_keys],
            dtype=object,
        )
        coverage = assortment_cfg.get("coverage", _DEFAULT_ASSORTMENT_COVERAGE)
        assort_seed = int(assortment_cfg.get("seed", seed))
        _unique_subcats, _subcat_matrix = _build_store_subcat_matrix(
            store_keys=store_keys,
            store_type_arr=store_type_arr,
            product_subcat_key=product_subcat_key,
            coverage_cfg=coverage,
            seed=assort_seed,
        )
        worker_cfg["_assortment_subcat_matrix"] = _shm.publish(
            "assort_matrix", _subcat_matrix,
        )
        worker_cfg["_assortment_unique_subcats"] = _shm.publish(
            "assort_subcats", _unique_subcats,
        )
        del _subcat_matrix, _unique_subcats

    # 3) salesperson_effective_by_store
    if employee_assign_employee_key is not None and employee_assign_store_key is not None:
        _sp_eff = _build_salesperson_effective_by_store(
            store_keys=store_keys,
            assign_store=employee_assign_store_key,
            assign_emp=employee_assign_employee_key,
            assign_start=employee_assign_start_date,
            assign_end=employee_assign_end_date,
            assign_fte=employee_assign_fte,
            assign_is_primary=employee_assign_is_primary,
            primary_boost=2.0,
        )
        worker_cfg["_prebuilt_salesperson_effective_by_store"] = _sp_eff
        if _sp_eff is not None:
            _all_sp = np.concatenate([v[0] for v in _sp_eff.values()])
            worker_cfg["_prebuilt_salesperson_global_pool"] = np.unique(_all_sp).astype(np.int32)
        del _sp_eff

    # 4) brand_prob_by_month
    models_cfg = worker_cfg.get("models_cfg")
    if isinstance(models_cfg, Mapping):
        _brand_cfg = models_cfg.get("brand_popularity") if isinstance(models_cfg, Mapping) else None
        if _brand_cfg and product_brand_key is not None and product_brand_key.size > 0:
            _T = infer_T_from_date_pool(date_pool)
            _B = int(product_brand_key.max()) + 1
            _rng_bp = np.random.default_rng(int(int_or(_brand_cfg.get("seed"), 1234)))

            _bp_counts = _brand_product_counts if (_brand_product_counts is not None and len(_brand_product_counts) == _B) else None

            _brand_prob = _build_brand_prob_by_month_rotate_winner(
                _rng_bp,
                T=_T, B=_B,
                winner_boost=float_or(_brand_cfg.get("winner_boost"), 2.5),
                noise_sd=float_or(_brand_cfg.get("noise_sd"), 0.15),
                min_share=float_or(_brand_cfg.get("min_share"), 0.02),
                year_len_months=int_or(_brand_cfg.get("year_len_months"), 12),
                brand_product_counts=_bp_counts,
                count_exponent=float_or(_brand_cfg.get("count_exponent"), 0.25),
            )
            worker_cfg["_prebuilt_brand_prob_by_month"] = _shm.publish(
                "brand_prob", _brand_prob,
            )
            del _brand_prob


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


def _assemble_output(
    file_format, tables, output_paths, collector,
    partition_cols, sales_cfg, sales_output, out_folder_p,
    chunk_size, delete_chunks, merge_parquet, compression,
    row_group_size, optimize_after_merge,
):
    """Post-pool output assembly: delta writes, CSV re-chunking, or parquet merge."""
    def _build_sales_manifest():
        per_table = {}
        for t in tables:
            per_table[t] = TableOutputs(
                table=t,
                file_format=file_format,
                chunks=list(collector.created_by_table.get(t, [])),
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

    def _make_result():
        return SalesFactResult(
            chunk_files=collector.created_files,
            manifest=_build_sales_manifest(),
            budget_acc=collector.budget_acc,
            inventory_acc=collector.inventory_acc,
            wishlists_acc=collector.wishlists_acc,
            complaints_acc=collector.complaints_acc,
        )

    if file_format == "deltaparquet":
        from .sales_writer import write_delta_partitioned

        missing_parts = []
        wrote = 0

        for t in tables:
            parts_dir = output_paths.delta_parts_dir(t)
            delta_dir = output_paths.delta_table_dir(t)

            part_files = glob.glob(os.path.join(parts_dir, "**", "*.parquet"), recursive=True)
            if not part_files:
                missing_parts.append((t, parts_dir))
                continue

            write_delta_partitioned(
                parts_folder=parts_dir,
                delta_output_folder=delta_dir,
                partition_cols=partition_cols,
                table_name=t,
                sort_small_parts=_bool_or(getattr(sales_cfg, "sort_delta_parts", False), False),
            )
            wrote += 1

        if wrote == 0:
            msg = " | ".join([f"{t} -> {p}" for t, p in missing_parts]) if missing_parts else "no parts found"
            raise SalesError(f"No delta parts found for any table. {msg}")

        return _make_result()

    if file_format == "csv":
        for t in tables:
            csv_dir = Path(output_paths.table_out_dir(t))
            spec = output_paths.spec(t)
            csv_chunks = sorted(csv_dir.glob(f"{spec.chunk_prefix}*.csv"))
            if len(csv_chunks) <= 1:
                continue
            _merge_fact_csv_chunks(csv_chunks, csv_dir, spec.chunk_prefix, chunk_size, delete_chunks)

        return _make_result()

    if file_format == "parquet":
        if merge_parquet:
            merge_jobs = []
            skipped = []

            for t in tables:
                chunks = sorted(
                    f for f in glob.glob(output_paths.chunk_glob(t, "parquet"))
                    if os.path.isfile(f)
                )
                if not chunks:
                    skipped.append(t)
                    continue
                merge_jobs.append((t, chunks, output_paths.merged_path(t)))

            if merge_jobs:
                short = {
                    TABLE_SALES: "sales",
                    TABLE_SALES_ORDER_DETAIL: "detail",
                    TABLE_SALES_ORDER_HEADER: "header",
                    TABLE_SALES_RETURN: "return",
                }

                counts = [(short.get(t, t), len(chunks)) for (t, chunks, _out) in merge_jobs]

                if len({c for _, c in counts}) == 1:
                    n = counts[0][1]
                    info(f"Merge parquet: {n} chunks -> " + ", ".join(name for name, _ in counts))
                else:
                    info("Merge parquet: " + ", ".join(f"{name}={n}" for name, n in counts))

                # The per-table merges are independent (disjoint chunk sets),
                # and each is a full re-decode+re-encode pass that holds the GIL
                # in its Python-level row-group loop — so run them across
                # processes when there is more than one table.  For a single
                # table there is nothing to overlap, so merge inline and skip
                # the process-spawn overhead.
                if len(merge_jobs) > 1:
                    from src.utils.pool import iter_imap_unordered, PoolRunSpec
                    n_merge = min(len(merge_jobs), max(1, (os.cpu_count() or 1) - 1))
                    jobs = [
                        (t, chunks, out, bool(delete_chunks), compression)
                        for (t, chunks, out) in merge_jobs
                    ]
                    # Drain to completion; iter_imap_unordered surfaces any worker
                    # exception and shares the pipeline's Windows CTRL_C spawn guard.
                    for _ in iter_imap_unordered(
                        tasks=jobs, task_fn=_merge_parquet_job,
                        spec=PoolRunSpec(processes=n_merge, label="parquet-merge"),
                    ):
                        pass
                else:
                    t, chunks, out = merge_jobs[0]
                    merge_parquet_files(
                        chunks,
                        out,
                        delete_after=bool(delete_chunks),
                        compression=compression,
                        table_name=t,
                        log=False,
                    )

                if optimize_after_merge:
                    info("Optimize parquet: sorting merged files...")
                    for t, _chunks, out in merge_jobs:
                        result = optimize_parquet(
                            out,
                            table_name=t,
                            compression=compression,
                            row_group_size=row_group_size,
                        )
                        if result:
                            info(f"  Optimized: {os.path.basename(out)}")
            else:
                info("Merge parquet: none")

        return _make_result()

    raise SalesError(f"Unknown file_format: {file_format}")


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
    # NOTE: the SalesOrderNumber int32→int64 decision is made *after* day-ID
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
    _stores = _load_stores(parquet_folder_p, end_date)
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
    # This guarantees SalesOrderNumber increases with OrderDate.
    _dp = np.asarray(date_pool)
    _n_days = int(_dp.size) if _dp.size else 1
    _safety = 8.0
    _per_chunk_alloc = int(ceil(total_rows / max(1, _n_days) * _safety / max(1, total_chunks)))
    _per_chunk_alloc = max(_per_chunk_alloc, 1)
    _day_stride = _per_chunk_alloc * total_chunks

    # SalesOrderNumber dtype decision. Day IDs reach up to (day_span+1)*day_stride
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
            f"SalesOrderNumber worst-case value ~{_max_order_id:,} exceeds the int32 "
            "safety margin; emitting int64 for order-number columns."
        )

    rng_master = np.random.default_rng(seed + 1)
    seeds = rng_master.integers(1, 1 << 30, size=total_chunks, dtype=np.int64)

    tasks: List[Tuple[int, int, int]] = []
    remaining = total_rows
    for idx, s in enumerate(seeds):
        if remaining <= 0:
            break
        batch = min(chunk_size, remaining)
        tasks.append((idx, int(batch), int(s)))
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
    # projected in-flight memory across all workers looks likely to exhaust RAM,
    # so the user can lower chunk_size or enable tune_chunk deliberately.
    if not tune_chunk:
        _avail = _available_phys_bytes()
        if _avail:
            _peak = _projected_peak_chunk_bytes(chunk_size, n_workers)
            if _peak > _avail * 0.75:
                logging.getLogger(__name__).warning(
                    "chunk_size=%s × %d workers projects ~%.1f GB in-flight "
                    "(~%.1f GB available); reduce sales.chunk_size or set "
                    "sales.tune_chunk=true to avoid possible OOM.",
                    f"{chunk_size:,}", n_workers,
                    _peak / (1024 ** 3), _avail / (1024 ** 3),
                )

    info(f"Spawning {n_workers} worker processes...")

    # SalesOrderNumber RunId:
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
        # Customers with negative start_month are backdated (pre-existing)
        # and will be pre-seeded into seen_customers by chunk_builder.
        # Only customers with start_month >= 0 need discovery.
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
        returns_logistics_keys, returns_event_key_capacity,
        month_stride=_day_stride, per_chunk_alloc=_per_chunk_alloc,
        order_id_int64=_order_id_int64,
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
    )
