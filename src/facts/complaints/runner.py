"""Complaints pipeline runner — generates complaints.parquet using
accumulated (CustomerKey, OrderNumber, OrderLineNumber) triples
from the sales pipeline.

Runs AFTER sales generation.  A configurable fraction of customers file
complaints, with most complaints linked to specific order lines and the
remainder being general service complaints.
"""
from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import cpu_count
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa

from src.defaults import NS_PER_DAY as _NS_PER_DAY, FACT_MAX_PARALLEL_CHUNKS
from src.facts.complaints.accumulator import ComplaintsAccumulator
from src.facts.shared.writers import write_fact_table
from src.utils.logging_utils import info, skip
from src.utils.config_helpers import parse_global_dates as _parse_global_dates_shared


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMPLAINT_TYPES_ORDER = [
    "Product Defect",
    "Wrong Item",
    "Late Delivery",
    "Damaged In Transit",
    "Billing Error",
    "Missing Parts",
]

_COMPLAINT_TYPES_GENERAL = [
    "Service",
    "Billing Error",
]

_COMPLAINT_DETAILS: Dict[str, list] = {
    "Product Defect": [
        "Item stopped working after a few uses",
        "Missing components in the package",
        "Product does not match the description",
        "Visible damage on arrival, packaging intact",
        "Color/size differs from what was ordered",
        "Battery drains significantly faster than advertised",
        "Strong chemical smell from the product",
    ],
    "Wrong Item": [
        "Received a completely different product",
        "Correct product but wrong variant/color",
        "Order contained someone else's items",
        "Received duplicate items instead of separate products",
    ],
    "Late Delivery": [
        "Package arrived well past the estimated date",
        "Tracking showed delivered but not received for days",
        "Delivery was rescheduled without notification",
        "Only partial order delivered, rest still pending",
    ],
    "Damaged In Transit": [
        "Box was crushed, item broken inside",
        "Water damage to product and packaging",
        "Item arrived with scratches and dents",
        "Screen cracked during shipping",
    ],
    "Billing Error": [
        "Charged twice for the same order",
        "Discount code was not applied at checkout",
        "Final charge higher than the displayed price",
        "Refund from previous return still not received",
        "Charged for items that were cancelled",
    ],
    "Service": [
        "Staff was unhelpful and dismissive",
        "Long wait time with no resolution",
        "Received conflicting information from support",
        "Store would not honor the posted return policy",
        "No follow-up after filing a complaint",
    ],
    "Missing Parts": [
        "Assembly hardware not included",
        "Accessories listed on the box were missing",
        "Manual/documentation not in the package",
    ],
}

_SEVERITY_VALUES = np.array(["Low", "Medium", "High", "Critical"], dtype=object)
_SEVERITY_WEIGHTS = np.array([0.25, 0.40, 0.25, 0.10])
_SEVERITY_CDF = np.cumsum(_SEVERITY_WEIGHTS); _SEVERITY_CDF[-1] = 1.0

_CHANNEL_VALUES = np.array(["Email", "Phone", "In-Store", "Website", "Chat"], dtype=object)
_CHANNEL_WEIGHTS = np.array([0.30, 0.25, 0.15, 0.15, 0.15])
_CHANNEL_CDF = np.cumsum(_CHANNEL_WEIGHTS); _CHANNEL_CDF[-1] = 1.0

_STATUS_VALUES = np.array(["Resolved", "Closed", "Open", "Escalated"], dtype=object)

_RESOLUTION_TYPES = np.array(
    ["Replacement", "Refund", "Discount", "Apology", "Store Credit"], dtype=object
)
_RESOLUTION_WEIGHTS = np.array([0.25, 0.30, 0.20, 0.10, 0.15])
_RESOLUTION_CDF = np.cumsum(_RESOLUTION_WEIGHTS); _RESOLUTION_CDF[-1] = 1.0

# Fraction of complaints that are order-linked vs general
_ORDER_LINKED_RATE = 0.75

# Below this complainer count, use the serial path (spawning overhead not worth it)
_PARALLEL_THRESHOLD = 50_000

# Pre-flattened (type, detail) pairs — computed once at import time so neither
# the serial path nor the worker rebuild them on every call.

def _build_flat_pools():
    _ot, _od = [], []
    for ct in _COMPLAINT_TYPES_ORDER:
        for detail in _COMPLAINT_DETAILS[ct]:
            _ot.append(ct)
            _od.append(detail)
    _gt, _gd = [], []
    for ct in _COMPLAINT_TYPES_GENERAL:
        for detail in _COMPLAINT_DETAILS[ct]:
            _gt.append(ct)
            _gd.append(detail)
    return (
        np.array(_ot, dtype=object), np.array(_od, dtype=object),
        np.array(_gt, dtype=object), np.array(_gd, dtype=object),
    )

_ORDER_TYPES_FLAT, _ORDER_DETAILS_FLAT, _GENERAL_TYPES_FLAT, _GENERAL_DETAILS_FLAT = _build_flat_pools()


# ---------------------------------------------------------------------------
# Shared generation — batch RNG, used by both serial and parallel paths
# ---------------------------------------------------------------------------

def _generate_rows_batch(
    rng: np.random.Generator,
    *,
    complainer_keys: np.ndarray,
    complaints_per: np.ndarray,
    cust_orders: Dict[int, Tuple[np.ndarray, np.ndarray]],
    g_start_ns: int,
    g_end_ns: int,
    cfg: "_ComplaintsCfg",
) -> Dict[str, np.ndarray]:
    """Fully vectorized complaint generation — no Python row loop.

    Args:
        cust_orders: {customer_key: (so_array, ln_array)} — numpy tuple format.
    """
    n_complainers = len(complainer_keys)
    total_rows = int(complaints_per.sum())

    if total_rows == 0:
        return {k: np.empty(0, dtype=d) for k, d in [
            ("ckey", np.int64), ("so", np.int64), ("ln", np.int64),
            ("date_ns", np.int64), ("res_date_ns", np.int64),
            ("type", object), ("detail", object), ("severity", object),
            ("channel", object), ("status", object), ("res_type", object),
            ("resp_days", np.int32),
        ]}

    # ------------------------------------------------------------------
    # Flatten per-customer orders into contiguous arrays with boundaries
    # (O(n_complainers) Python loop — negligible vs O(total_rows) vectorized work)
    # ------------------------------------------------------------------
    all_so: List[np.ndarray] = []
    all_ln: List[np.ndarray] = []
    order_starts = np.zeros(n_complainers + 1, dtype=np.int64)
    for i in range(n_complainers):
        orders = cust_orders.get(int(complainer_keys[i]))
        if orders is not None:
            all_so.append(orders[0])
            all_ln.append(orders[1])
            order_starts[i + 1] = order_starts[i] + len(orders[0])
        else:
            order_starts[i + 1] = order_starts[i]

    if all_so:
        orders_so_flat = np.concatenate(all_so)
        orders_ln_flat = np.concatenate(all_ln)
    else:
        orders_so_flat = np.empty(0, dtype=np.int64)
        orders_ln_flat = np.empty(0, dtype=np.int64)
    has_any_orders = len(orders_so_flat) > 0

    n_orders_per_cust = np.diff(order_starts)  # shape (n_complainers,)

    # Per-row customer index: which complainer each row belongs to
    cust_idx = np.repeat(np.arange(n_complainers, dtype=np.int64), complaints_per)

    # ------------------------------------------------------------------
    # BATCH RNG — all random numbers generated upfront
    # ------------------------------------------------------------------
    order_linked_rolls = rng.random(total_rows)
    order_select_rolls = rng.random(total_rows)
    type_detail_rolls = rng.random(total_rows)

    span = max(1, g_end_ns - g_start_ns)
    date_offsets = rng.integers(0, span, size=total_rows, dtype=np.int64)

    sev_idx = np.searchsorted(_SEVERITY_CDF, rng.random(total_rows))
    np.clip(sev_idx, 0, len(_SEVERITY_VALUES) - 1, out=sev_idx)

    chan_idx = np.searchsorted(_CHANNEL_CDF, rng.random(total_rows))
    np.clip(chan_idx, 0, len(_CHANNEL_VALUES) - 1, out=chan_idx)

    resolution_rolls = rng.random(total_rows)
    status_rolls = rng.random(total_rows)

    res_type_idx = np.searchsorted(_RESOLUTION_CDF, rng.random(total_rows))
    np.clip(res_type_idx, 0, len(_RESOLUTION_TYPES) - 1, out=res_type_idx)

    resp_days_raw = rng.exponential(cfg.avg_response_days, size=total_rows).astype(np.int32)
    np.clip(resp_days_raw, 0, cfg.max_response_days, out=resp_days_raw)

    escalation_rolls = rng.random(total_rows)

    # ------------------------------------------------------------------
    # VECTORIZED OUTPUT — no Python row loop
    # ------------------------------------------------------------------

    # Customer keys: repeat each complainer's key by their complaint count
    out_ckey = np.repeat(complainer_keys, complaints_per)

    # Dates
    out_date_ns = g_start_ns + date_offsets

    # Severity and channel (fully batch)
    out_severity = _SEVERITY_VALUES[sev_idx]
    out_channel = _CHANNEL_VALUES[chan_idx]

    # --- Order-linked vs general complaint ---
    n_orders_per_row = n_orders_per_cust[cust_idx]
    is_order_linked = (order_linked_rolls < _ORDER_LINKED_RATE) & (n_orders_per_row > 0)

    # Order selection for order-linked rows
    out_so = np.full(total_rows, -1, dtype=np.int64)
    out_ln = np.full(total_rows, -1, dtype=np.int64)
    if has_any_orders:
        order_start_per_row = order_starts[:-1][cust_idx]
        order_offset = np.minimum(
            (order_select_rolls * n_orders_per_row).astype(np.int64),
            np.maximum(n_orders_per_row - 1, 0),
        )
        flat_idx = order_start_per_row + order_offset
        ol_mask = is_order_linked
        # Clip to valid range to avoid IndexError on non-order-linked rows
        safe_flat_idx = np.clip(flat_idx, 0, max(len(orders_so_flat) - 1, 0))
        out_so[ol_mask] = orders_so_flat[safe_flat_idx[ol_mask]]
        out_ln[ol_mask] = orders_ln_flat[safe_flat_idx[ol_mask]]

    # Type and detail selection
    aot, aod = _ORDER_TYPES_FLAT, _ORDER_DETAILS_FLAT
    agt, agd = _GENERAL_TYPES_FLAT, _GENERAL_DETAILS_FLAT

    order_td_idx = np.minimum(
        (type_detail_rolls * len(aot)).astype(np.int64), len(aot) - 1,
    )
    general_td_idx = np.minimum(
        (type_detail_rolls * len(agt)).astype(np.int64), len(agt) - 1,
    )
    out_type = np.where(is_order_linked, aot[order_td_idx], agt[general_td_idx])
    out_detail = np.where(is_order_linked, aod[order_td_idx], agd[general_td_idx])

    # --- Resolution logic ---
    is_resolved = resolution_rolls < cfg.resolution_rate

    out_status = np.where(
        is_resolved,
        np.where(status_rolls < 0.5, _STATUS_VALUES[0], _STATUS_VALUES[1]),
        np.where(escalation_rolls < cfg.escalation_rate, _STATUS_VALUES[3], _STATUS_VALUES[2]),
    )

    out_res_type = np.where(is_resolved, _RESOLUTION_TYPES[res_type_idx], None)

    out_resp_days = np.where(is_resolved, resp_days_raw, np.int32(-1))

    # Resolution dates (only meaningful for resolved rows)
    res_date_raw = out_date_ns + resp_days_raw.astype(np.int64) * _NS_PER_DAY
    needs_clamp = is_resolved & (res_date_raw > g_end_ns)
    clamped_resp_days = np.maximum(
        np.int32(0),
        ((g_end_ns - out_date_ns) // _NS_PER_DAY).astype(np.int32),
    )

    out_res_date_ns = np.where(
        is_resolved,
        np.where(needs_clamp, np.int64(g_end_ns), res_date_raw),
        np.int64(-1),
    )
    out_resp_days = np.where(needs_clamp, clamped_resp_days, out_resp_days)

    return {
        "ckey": out_ckey,
        "so": out_so,
        "ln": out_ln,
        "date_ns": out_date_ns,
        "res_date_ns": out_res_date_ns,
        "type": out_type,
        "detail": out_detail,
        "severity": out_severity,
        "channel": out_channel,
        "status": out_status,
        "res_type": out_res_type,
        "resp_days": out_resp_days,
    }


# ---------------------------------------------------------------------------
# Order lookup helper
# ---------------------------------------------------------------------------

def _build_order_lookup(
    order_arrays: Dict[str, np.ndarray],
) -> Dict[int, Tuple[np.ndarray, np.ndarray]]:
    """Build {customer_key: (so_array, ln_array)} from flat order arrays."""
    ck_arr = order_arrays["CustomerKey"]
    so_arr = order_arrays["OrderNumber"]
    ln_arr = order_arrays["OrderLineNumber"]

    cust_orders: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
    if len(ck_arr) > 0:
        sort_idx = np.argsort(ck_arr, kind="stable")
        sorted_ck = ck_arr[sort_idx]
        sorted_so = so_arr[sort_idx]
        sorted_ln = ln_arr[sort_idx]
        boundaries = np.where(np.diff(sorted_ck))[0] + 1
        ck_groups = np.split(sorted_ck, boundaries)
        so_groups = np.split(sorted_so, boundaries)
        ln_groups = np.split(sorted_ln, boundaries)
        for g_ck, g_so, g_ln in zip(ck_groups, so_groups, ln_groups):
            cust_orders[int(g_ck[0])] = (g_so, g_ln)
    return cust_orders


# ---------------------------------------------------------------------------
# Worker function — must be top-level for Windows spawn pickling
# ---------------------------------------------------------------------------

def _complaints_worker_task(args: Tuple) -> Dict[str, np.ndarray]:
    """Generate complaint rows for a chunk of customers.

    Args (tuple):
        chunk_idx:             int — chunk sequence number (for seeding)
        seed:                  int — base seed from _ComplaintsCfg
        n_chunks:              int — total number of chunks (for SeedSequence.spawn)
        complainer_keys_chunk: np.ndarray[int64] — CustomerKeys for this chunk
        complaints_per_chunk:  np.ndarray[int32] — complaint counts per customer
        order_arrays_chunk:    dict of numpy arrays — order data for this chunk's customers
            Keys: "CustomerKey", "OrderNumber", "OrderLineNumber"
        complaints_cfg:        _ComplaintsCfg — complaint config (frozen dataclass, picklable)
        date_range:            tuple[int, int] — (g_start_ns, g_end_ns)

    Returns:
        dict of numpy arrays for the chunk's complaint rows
    """
    (
        chunk_idx,
        seed,
        n_chunks,
        complainer_keys_chunk,
        complaints_per_chunk,
        order_arrays_chunk,
        complaints_cfg,
        date_range,
    ) = args

    child_rng = np.random.default_rng(
        np.random.SeedSequence(seed).spawn(n_chunks)[chunk_idx]
    )

    g_start_ns, g_end_ns = date_range
    cust_orders = _build_order_lookup(order_arrays_chunk)

    result = _generate_rows_batch(
        child_rng,
        complainer_keys=complainer_keys_chunk,
        complaints_per=complaints_per_chunk,
        cust_orders=cust_orders,
        g_start_ns=g_start_ns,
        g_end_ns=g_end_ns,
        cfg=complaints_cfg,
    )
    # Tag with chunk_idx so the main process can reassemble in deterministic
    # chunk order (iter_imap_unordered yields in arrival order). Without this,
    # ComplaintKey assignment and row order would vary run-to-run.
    result["chunk_idx"] = chunk_idx
    return result


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ComplaintsCfg:
    enabled: bool = False
    complaint_rate: float = 0.03
    repeat_complaint_rate: float = 0.15
    max_complaints: int = 5
    resolution_rate: float = 0.85
    escalation_rate: float = 0.10
    avg_response_days: int = 5
    max_response_days: int = 30
    seed: int = 600
    write_chunk_rows: int = 250_000


def _read_cfg(cfg: Any) -> _ComplaintsCfg:
    cc = getattr(cfg, "complaints", None)
    if cc is None:
        return _ComplaintsCfg()
    return _ComplaintsCfg(
        enabled=bool(getattr(cc, "enabled", False)),
        complaint_rate=float(getattr(cc, "complaint_rate", 0.03)),
        repeat_complaint_rate=float(getattr(cc, "repeat_complaint_rate", 0.15)),
        max_complaints=int(getattr(cc, "max_complaints", 5)),
        resolution_rate=float(getattr(cc, "resolution_rate", 0.85)),
        escalation_rate=float(getattr(cc, "escalation_rate", 0.10)),
        avg_response_days=int(getattr(cc, "avg_response_days", 5)),
        max_response_days=int(getattr(cc, "max_response_days", 30)),
        seed=int(getattr(cc, "seed", None) or 600),
        write_chunk_rows=int(getattr(cc, "write_chunk_rows", 250_000)),
    )


def _parse_global_dates(cfg: Any) -> Tuple[pd.Timestamp, pd.Timestamp]:
    return _parse_global_dates_shared(cfg, {}, dimension_name="complaints")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _complaints_schema() -> pa.Schema:
    return pa.schema([
        pa.field("ComplaintKey", pa.int64()),
        pa.field("CustomerKey", pa.int64()),
        pa.field("OrderNumber", pa.int64(), nullable=True),
        pa.field("LineNumber", pa.int64(), nullable=True),
        pa.field("ComplaintDate", pa.date32()),
        pa.field("ResolutionDate", pa.date32(), nullable=True),
        pa.field("ComplaintType", pa.string()),
        pa.field("ComplaintDetail", pa.string()),
        pa.field("Severity", pa.string()),
        pa.field("Channel", pa.string()),
        pa.field("Status", pa.string()),
        pa.field("ResolutionType", pa.string(), nullable=True),
        pa.field("ResponseDays", pa.int32(), nullable=True),
    ])


# ---------------------------------------------------------------------------
# Helpers: assemble a PyArrow table from raw column arrays
# ---------------------------------------------------------------------------

def _arrays_to_table(
    out_ckey: np.ndarray,
    out_so: np.ndarray,
    out_ln: np.ndarray,
    out_date_ns: np.ndarray,
    out_res_date_ns: np.ndarray,
    out_type: np.ndarray,
    out_detail: np.ndarray,
    out_severity: np.ndarray,
    out_channel: np.ndarray,
    out_status: np.ndarray,
    out_res_type: np.ndarray,
    out_resp_days: np.ndarray,
    key_offset: int,
) -> pa.Table:
    """Convert pre-allocated numpy arrays into a PyArrow table segment."""
    schema = _complaints_schema()
    n_rows = len(out_ckey)

    complaint_keys = np.arange(key_offset + 1, key_offset + n_rows + 1, dtype=np.int64)

    complaint_dates = out_date_ns.view("datetime64[ns]").astype("datetime64[ms]")

    so_mask = out_so == -1
    ln_mask = out_ln == -1

    res_date_mask = out_res_date_ns == -1
    res_dates_copy = out_res_date_ns.copy()
    res_dates_copy[res_date_mask] = 0
    res_dates_dt = res_dates_copy.view("datetime64[ns]").astype("datetime64[ms]")

    resp_days_mask = out_resp_days == -1

    so_pa = pa.array(out_so, type=pa.int64(), mask=so_mask)
    ln_pa = pa.array(out_ln, type=pa.int64(), mask=ln_mask)
    res_date_pa = pa.array(res_dates_dt, type=pa.date32(), mask=res_date_mask)
    resp_days_pa = pa.array(out_resp_days, type=pa.int32(), mask=resp_days_mask)

    return pa.table(
        [
            pa.array(complaint_keys, type=pa.int64()),
            pa.array(out_ckey, type=pa.int64()),
            so_pa,
            ln_pa,
            pa.array(complaint_dates, type=pa.date32()),
            res_date_pa,
            pa.array(out_type, type=pa.string()),
            pa.array(out_detail, type=pa.string()),
            pa.array(out_severity, type=pa.string()),
            pa.array(out_channel, type=pa.string()),
            pa.array(out_status, type=pa.string()),
            pa.array(out_res_type, type=pa.string()),
            resp_days_pa,
        ],
        schema=schema,
    )


# ---------------------------------------------------------------------------
# Complaint generation — serial path (small datasets / fallback)
# ---------------------------------------------------------------------------

def _generate_complaints_serial(
    order_data: pd.DataFrame,
    c: _ComplaintsCfg,
    g_start: pd.Timestamp,
    g_end: pd.Timestamp,
) -> pa.Table:
    rng = np.random.default_rng(c.seed)

    g_start_ns = np.int64(g_start.value)
    g_end_ns = np.int64(g_end.value)

    unique_customers = order_data["CustomerKey"].unique()
    n_customers = len(unique_customers)

    schema = _complaints_schema()
    if n_customers == 0:
        return pa.table({f.name: pa.array([], type=f.type) for f in schema}, schema=schema)

    # Select complaining customers
    n_complainers = max(1, int(round(n_customers * c.complaint_rate)))
    complainer_keys = rng.choice(unique_customers, size=n_complainers, replace=False)

    # Determine number of complaints per customer
    complaints_per = np.ones(n_complainers, dtype=np.int32)
    repeat_mask = rng.random(n_complainers) < c.repeat_complaint_rate
    n_repeaters = repeat_mask.sum()
    if n_repeaters > 0:
        complaints_per[repeat_mask] = rng.integers(
            2, c.max_complaints + 1, size=n_repeaters
        ).astype(np.int32)

    # Build per-customer order lookup as numpy tuples (same format as worker)
    _complainer_set = set(int(k) for k in complainer_keys)
    _complainer_mask = order_data["CustomerKey"].isin(_complainer_set)
    filtered = order_data[_complainer_mask]
    cust_orders = _build_order_lookup({
        "CustomerKey": filtered["CustomerKey"].to_numpy(dtype=np.int64),
        "OrderNumber": filtered["OrderNumber"].to_numpy(dtype=np.int64),
        "OrderLineNumber": filtered["OrderLineNumber"].to_numpy(dtype=np.int64),
    })

    result = _generate_rows_batch(
        rng,
        complainer_keys=complainer_keys,
        complaints_per=complaints_per,
        cust_orders=cust_orders,
        g_start_ns=int(g_start_ns),
        g_end_ns=int(g_end_ns),
        cfg=c,
    )

    return _arrays_to_table(
        result["ckey"], result["so"], result["ln"],
        result["date_ns"], result["res_date_ns"],
        result["type"], result["detail"], result["severity"],
        result["channel"], result["status"], result["res_type"],
        result["resp_days"],
        key_offset=0,
    )


# ---------------------------------------------------------------------------
# Complaint generation — parallel path (large datasets)
# ---------------------------------------------------------------------------

def _generate_complaints_parallel(
    order_data: pd.DataFrame,
    c: _ComplaintsCfg,
    g_start: pd.Timestamp,
    g_end: pd.Timestamp,
    n_workers: int,
) -> pa.Table:
    """Partition complainers across workers and merge results."""
    from src.utils.pool import PoolRunSpec, iter_imap_unordered

    g_start_ns = np.int64(g_start.value)
    g_end_ns = np.int64(g_end.value)

    unique_customers = order_data["CustomerKey"].unique()
    n_customers = len(unique_customers)

    schema = _complaints_schema()
    if n_customers == 0:
        return pa.table({f.name: pa.array([], type=f.type) for f in schema}, schema=schema)

    # --- Setup phase (main process) ---
    # Use a seeded RNG for the setup (complainer selection + complaint counts)
    # so results are deterministic regardless of worker count.
    setup_rng = np.random.default_rng(c.seed)

    n_complainers = max(1, int(round(n_customers * c.complaint_rate)))
    complainer_keys = setup_rng.choice(unique_customers, size=n_complainers, replace=False)

    complaints_per = np.ones(n_complainers, dtype=np.int32)
    repeat_mask = setup_rng.random(n_complainers) < c.repeat_complaint_rate
    n_repeaters = int(repeat_mask.sum())
    if n_repeaters > 0:
        complaints_per[repeat_mask] = setup_rng.integers(
            2, c.max_complaints + 1, size=n_repeaters
        ).astype(np.int32)

    # Build per-customer order lookup filtered to complainers only
    _complainer_set = set(int(k) for k in complainer_keys)
    _complainer_mask = order_data["CustomerKey"].isin(_complainer_set)
    filtered = order_data[_complainer_mask]

    # Convert to numpy arrays for pickle-efficient IPC
    orders_ck = filtered["CustomerKey"].to_numpy(dtype=np.int64, copy=True)
    orders_so = filtered["OrderNumber"].to_numpy(dtype=np.int64, copy=True)
    orders_ln = filtered["OrderLineNumber"].to_numpy(dtype=np.int64, copy=True)

    # --- Partition complainers into chunks ---
    # Chunk COUNT depends only on the data (not the worker count), so per-chunk
    # SeedSequence streams — and thus the generated rows — are identical across
    # machines. The pool SIZE still adapts to the available workers.
    n_chunks = max(2, min(n_complainers, FACT_MAX_PARALLEL_CHUNKS))
    # Actual workers bounded by chunks
    actual_workers = min(n_chunks, n_workers)

    chunk_indices = np.array_split(np.arange(n_complainers), n_chunks)

    date_range = (int(g_start_ns), int(g_end_ns))

    tasks = []
    for chunk_idx, indices in enumerate(chunk_indices):
        if len(indices) == 0:
            continue
        chunk_keys = complainer_keys[indices].astype(np.int64)
        chunk_counts = complaints_per[indices]

        # Filter order arrays to only this chunk's customers (minimize pickle size)
        chunk_key_set = set(int(k) for k in chunk_keys)
        mask = np.isin(orders_ck, list(chunk_key_set))
        order_arrays_chunk = {
            "CustomerKey": orders_ck[mask],
            "OrderNumber": orders_so[mask],
            "OrderLineNumber": orders_ln[mask],
        }

        tasks.append((
            chunk_idx,
            c.seed,
            n_chunks,
            chunk_keys,
            chunk_counts,
            order_arrays_chunk,
            c,
            date_range,
        ))

    info(
        f"Complaints parallel: {len(tasks)} chunks across {actual_workers} workers "
        f"({n_complainers:,} complainers)"
    )

    pool_spec = PoolRunSpec(
        processes=actual_workers,
        label="complaints",
    )

    # Collect results keyed by chunk_idx. iter_imap_unordered yields in worker
    # arrival order, so we reassemble in deterministic chunk order before
    # assigning ComplaintKeys — otherwise keys and row order would vary per run.
    _array_cols = [
        "ckey", "so", "ln", "date_ns", "res_date_ns",
        "type", "detail", "severity", "channel", "status",
        "res_type", "resp_days",
    ]
    results_by_chunk: Dict[int, Dict[str, np.ndarray]] = {}

    for result in iter_imap_unordered(
        tasks=tasks,
        task_fn=_complaints_worker_task,
        spec=pool_spec,
    ):
        results_by_chunk[int(result["chunk_idx"])] = result

    # --- Merge phase ---
    # ComplaintKeys are assigned globally after concatenation in chunk order.

    ordered = [results_by_chunk[i] for i in sorted(results_by_chunk)]
    if not ordered or all(len(r["ckey"]) == 0 for r in ordered):
        return pa.table({f.name: pa.array([], type=f.type) for f in schema}, schema=schema)

    merged: Dict[str, np.ndarray] = {
        col: np.concatenate([r[col] for r in ordered]) for col in _array_cols
    }

    return _arrays_to_table(
        merged["ckey"],
        merged["so"],
        merged["ln"],
        merged["date_ns"],
        merged["res_date_ns"],
        merged["type"],
        merged["detail"],
        merged["severity"],
        merged["channel"],
        merged["status"],
        merged["res_type"],
        merged["resp_days"],
        key_offset=0,
    )


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

def _generate_complaints(
    order_data: pd.DataFrame,
    c: _ComplaintsCfg,
    g_start: pd.Timestamp,
    g_end: pd.Timestamp,
    workers: Optional[int] = None,
) -> pa.Table:
    """Generate the complaints table; dispatches to parallel or serial path."""
    unique_customers = order_data["CustomerKey"].unique()
    n_customers = len(unique_customers)
    n_complainers = max(1, int(round(n_customers * c.complaint_rate))) if n_customers > 0 else 0

    n_cpus = max(1, cpu_count() - 1)
    if workers is not None and workers >= 1:
        n_cpus = min(n_cpus, workers)

    # Path selection depends only on the data, not the worker count, so the same
    # dataset always takes the same path (and produces the same output) on any
    # machine. The pool may run with a single worker on a 1-core host.
    use_parallel = n_complainers >= _PARALLEL_THRESHOLD

    if use_parallel:
        return _generate_complaints_parallel(
            order_data=order_data,
            c=c,
            g_start=g_start,
            g_end=g_end,
            n_workers=n_cpus,
        )
    else:
        return _generate_complaints_serial(
            order_data=order_data,
            c=c,
            g_start=g_start,
            g_end=g_end,
        )


# ---------------------------------------------------------------------------
# Format-aware writer
# ---------------------------------------------------------------------------

_COMPLAINTS_CSV_COLUMNS = [
    "ComplaintKey", "CustomerKey", "OrderNumber", "LineNumber",
    "ComplaintDate", "ResolutionDate", "ComplaintType", "ComplaintDetail",
    "Severity", "Channel", "Status", "ResolutionType", "ResponseDays",
]

_COMPLAINTS_CSV_INT_COLS = ("ComplaintKey", "CustomerKey", "OrderNumber", "LineNumber", "ResponseDays")


def _prepare_complaints_csv(df: pd.DataFrame) -> pd.DataFrame:
    """Select columns and cast integers for clean CSV output."""
    out = df.copy()
    for col in _COMPLAINTS_CSV_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out = out[_COMPLAINTS_CSV_COLUMNS]

    for col in _COMPLAINTS_CSV_INT_COLS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")

    return out


def _write_complaints(table: pa.Table, complaints_dir: Path, file_format: str,
                      csv_chunk_size: Optional[int] = None) -> None:
    """Write complaints table in the requested format (parquet, csv, or delta)."""
    write_fact_table(table, complaints_dir, "complaints", file_format,
                     csv_prep_fn=_prepare_complaints_csv,
                     csv_chunk_size=csv_chunk_size)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run_complaints_pipeline(
    *,
    accumulator: ComplaintsAccumulator,
    parquet_dims: Path,
    fact_out: Path,
    cfg: Any,
    file_format: str = "parquet",
) -> Optional[Dict[str, Any]]:
    """Generate complaints output using accumulated sales data."""
    c = _read_cfg(cfg)
    if not c.enabled:
        return None

    if not accumulator.has_data:
        skip("Complaints: no sales data accumulated; skipping.")
        return None

    g_start, g_end = _parse_global_dates(cfg)
    order_data = accumulator.finalize()

    complaints_dir = Path(fact_out) / "complaints"
    complaints_dir.mkdir(parents=True, exist_ok=True)

    # Resolve worker count from config if available
    sales_cfg = getattr(cfg, "sales", None)
    workers: Optional[int] = int(getattr(sales_cfg, "workers", 0) or 0) or None

    table = _generate_complaints(
        order_data=order_data,
        c=c,
        g_start=g_start,
        g_end=g_end,
        workers=workers,
    )

    n_rows = table.num_rows
    if n_rows == 0:
        skip("Complaints: generated 0 rows; skipping write.")
        return None

    _csv_chunk = int(getattr(sales_cfg, "chunk_size", 0) or 0) if sales_cfg else 0
    _write_complaints(table, complaints_dir, file_format, csv_chunk_size=_csv_chunk)

    # For deltaparquet the delta table is written directly at complaints_dir;
    # the directory structure is the delta table itself — no cleanup needed.

    return {
        "complaints": str(complaints_dir),
        "complaints_rows": n_rows,
    }
