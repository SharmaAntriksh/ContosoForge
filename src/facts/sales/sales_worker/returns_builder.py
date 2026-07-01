from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pyarrow as pa

from src.defaults import RETURN_REASON_KEYS, RETURN_REASON_DEFAULT_WEIGHTS
from src.exceptions import SalesError
from src.facts.sales.sales_logic.core.delivery import line_friction


# Columns required from OrderDetail to produce a returns fact table.
RETURNS_REQUIRED_DETAIL_COLS: tuple[str, ...] = (
    "OrderNumber",
    "OrderLineNumber",
    "DeliveryDate",
    "Quantity",
    "NetPrice",
    "IsOrderDelayed",
)

# Output schema for the Returns fact table.
#
# Notes on semantics:
# - ReturnNetPrice is a *line amount* prorated by returned quantity.
RETURNS_SCHEMA = pa.schema(
    [
        ("OrderNumber", pa.int32()),
        ("OrderLineNumber", pa.int32()),
        ("ReturnDate", pa.date32()),
        ("ReturnReasonKey", pa.int32()),
        ("ReturnQuantity", pa.int32()),
        ("ReturnNetPrice", pa.float64()),
        ("ReturnSequence", pa.int32()),
        ("ReturnEventKey", pa.int64()),
    ]
)

@dataclass(frozen=True)
class ReturnsConfig:
    enabled: bool = False
    return_rate: float = 0.0
    min_lag_days: int = 0
    max_lag_days: int = 60
    # Lag shape between delivery and return. Source: models.yaml ->
    # returns.lag_days.{distribution,mode}. Default "uniform" preserves the
    # historical behavior for direct construction (e.g. tests); the pipeline
    # passes the configured value (models.yaml ships "triangular").
    lag_distribution: str = "uniform"
    lag_mode: int = 7
    reason_keys: Sequence[int] = RETURN_REASON_KEYS
    reason_probs: Sequence[float] = tuple(RETURN_REASON_DEFAULT_WEIGHTS[k] for k in RETURN_REASON_KEYS)
    full_line_probability: float = 0.85
    split_return_rate: float = 0.0
    max_splits: int = 3
    split_min_gap: int = 3
    split_max_gap: int = 20
    # Integer-cent (largest-remainder) proration for multi-event returns. Default
    # off reproduces the legacy per-event rounding byte-for-byte.
    reconcile_cents: bool = False
    event_key_offset: int = 0
    logistics_keys: frozenset = frozenset()
    # Fulfillment-friction coupling (recomputed per line from
    # OrderNumber+OrderLineNumber). Defaults 0.0 => no effect (byte-identical to
    # the pre-3.4 behavior for direct/unit construction).
    friction_return_boost: float = 0.0   # p_return_i = rate * (1 + boost*friction_i)
    friction_lag_shorten: float = 0.0    # lag_i *= (1 - shorten*friction_i)


def _returns_schema_for(so_type: pa.DataType) -> pa.Schema:
    """RETURNS_SCHEMA with OrderNumber set to *so_type*.

    Returns mirror the dtype the detail table carries for OrderNumber, so a
    large run with int64 order numbers produces int64 here too (rather than
    silently truncating to the int32 default).
    """
    if so_type == pa.int32():
        return RETURNS_SCHEMA
    return pa.schema([
        pa.field(f.name, so_type) if f.name == "OrderNumber" else f
        for f in RETURNS_SCHEMA
    ])


def _empty_returns_table(so_type: pa.DataType = pa.int32()) -> pa.Table:
    schema = _returns_schema_for(so_type)
    arrays = [pa.array([], type=f.type) for f in schema]
    return pa.Table.from_arrays(arrays, schema=schema)


def _ontime_reason_probs(reason_probs: np.ndarray, logistics_mask: np.ndarray) -> np.ndarray:
    """Reason distribution for on-time orders: logistics reasons are invalid, so
    their weight is zeroed and redistributed.

    The renormalization residual is placed on the largest NON-logistics slot so
    logistics reasons stay at exactly 0 (RETURNS-2). Forcing the last slot (as the
    old CDF boundary guard did) could revive a zeroed logistics reason there — or
    push it negative and break ``rng.choice``. Falls back to the last slot only in
    the degenerate case where every reason is a logistics reason.
    """
    p = np.asarray(reason_probs, dtype=np.float64).copy()
    p[logistics_mask] = 0.0
    s = float(p.sum())
    if s > 0:
        p /= s
    else:
        p = np.full_like(p, 1.0 / max(1, p.size))
    non_log = np.where(logistics_mask, -np.inf, p)
    fix = int(np.argmax(non_log)) if np.isfinite(non_log).any() else p.size - 1
    p[fix] += 1.0 - float(p.sum())
    return p


def _as_np_i64(x) -> np.ndarray:
    arr = np.asarray(x)
    return arr if arr.dtype == np.int64 else arr.astype(np.int64, copy=False)


def _as_np_i32(x) -> np.ndarray:
    arr = np.asarray(x)
    return arr if arr.dtype == np.int32 else arr.astype(np.int32, copy=False)


def _as_np_f64(x) -> np.ndarray:
    arr = np.asarray(x)
    return arr if arr.dtype == np.float64 else arr.astype(np.float64, copy=False)


def _ensure_required_columns(detail: pa.Table) -> None:
    missing = [c for c in RETURNS_REQUIRED_DETAIL_COLS if c not in detail.column_names]
    if missing:
        raise RuntimeError(f"Returns builder: missing required column(s): {missing!r}")


def _col_np(table: pa.Table, name: str) -> np.ndarray:
    """
    Convert a table column to a NumPy array (materializing if required).
    """
    if name not in table.column_names:
        raise RuntimeError(f"Returns builder: missing required column {name!r}")

    col = table[name]
    # table[name] is typically a ChunkedArray.
    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()
        # combine_chunks() returns an Array (or a ChunkedArray with 1 chunk depending on version).
        if isinstance(col, pa.ChunkedArray):
            col = col.chunk(0)

    return col.to_numpy(zero_copy_only=False)


def _to_np_date_days(x: np.ndarray) -> np.ndarray:
    """
    Normalize input to numpy datetime64[D].

    Handles:
    - numpy datetime64 (any unit)
    - object arrays of python date/datetime
    - strings parsable by numpy
    """
    if np.issubdtype(x.dtype, np.datetime64):
        return x.astype("datetime64[D]", copy=False)

    # object/strings -> let numpy parse; this may raise if unparseable.
    return np.asarray(x, dtype="datetime64[D]")


def _validate_cfg(cfg: ReturnsConfig) -> tuple[float, int, int, np.ndarray, np.ndarray]:
    """
    Returns:
      (return_rate, min_lag_days, max_lag_days, reason_keys_i64, reason_probs_f64_normalized)
    """
    if not isinstance(cfg.enabled, bool):
        raise SalesError("ReturnsConfig.enabled must be a bool.")

    # return_rate validation
    rr = float(cfg.return_rate)
    if not np.isfinite(rr):
        raise SalesError("ReturnsConfig.return_rate must be finite.")
    if rr < 0.0 or rr > 1.0:
        raise SalesError("ReturnsConfig.return_rate must be in [0, 1].")

    # min_lag_days validation (allow 0)
    min_lag = int(getattr(cfg, 'min_lag_days', 0))
    if min_lag < 0:
        raise SalesError('ReturnsConfig.min_lag_days must be >= 0.')

    # max_lag_days validation (allow 0)
    max_lag = int(cfg.max_lag_days)
    if max_lag < 0:
        raise SalesError("ReturnsConfig.max_lag_days must be >= 0.")

    if min_lag > max_lag:
        raise SalesError('ReturnsConfig.min_lag_days must be <= max_lag_days.')

    # reasons
    rk = _as_np_i32(list(cfg.reason_keys))
    rp = _as_np_f64(list(cfg.reason_probs))

    if rk.size == 0:
        rk = np.array([1], dtype=np.int32)
        rp = np.array([1.0], dtype=np.float64)

    if rp.size != rk.size:
        raise SalesError("ReturnsConfig.reason_probs must match reason_keys length.")

    if not np.all(np.isfinite(rp)):
        raise SalesError("ReturnsConfig.reason_probs must be finite.")
    if np.any(rp < 0):
        raise SalesError("ReturnsConfig.reason_probs must be >= 0.")

    s = float(rp.sum())
    if s <= 0.0:
        rp = np.full_like(rp, 1.0 / rp.size, dtype=np.float64)
    else:
        rp = rp / s

    return rr, min_lag, max_lag, rk, rp


def _draw_base_lag(
    rng: np.random.Generator,
    m: int,
    lo: int,
    hi: int,
    distribution: str,
    mode: int,
) -> np.ndarray:
    """Draw ``m`` integer return lags (days), each in ``[lo, hi]``.

    The shape is set by ``distribution`` (from models.yaml ->
    returns.lag_days.distribution):

      - "uniform"     : flat over [lo, hi] (historical behavior)
      - "triangular"  : peaked at ``mode`` (clamped into [lo, hi])
      - "normal"      : centered at ``mode``, sigma ≈ (hi-lo)/6 so ~99.7% of
                        mass falls within [lo, hi] before clamping

    Unknown names fall back to uniform. For a degenerate range (hi <= lo) every
    lag is ``lo`` and no RNG is consumed. Note: the pre-refactor code only
    skipped the draw when ``hi == 0``; for ``min_lag == max_lag > 0`` it still
    drew a (constant) value from ``rng``, so the return-RNG stream for that edge
    config differs from older versions (the lags themselves are unchanged).
    """
    if hi <= lo:
        return np.full(m, lo, dtype=np.int32)

    dist = str(distribution or "uniform").strip().lower()
    if dist in ("triangular", "normal"):
        peak = float(min(max(int(mode), lo), hi))  # clamp mode into [lo, hi]
        if dist == "triangular":
            vals = rng.triangular(lo, peak, hi, size=m)
        else:  # normal
            vals = rng.normal(peak, max(1.0, (hi - lo) / 6.0), size=m)
        return np.clip(np.rint(vals), lo, hi).astype(np.int32)

    # uniform (default / unrecognized) — identical RNG draw to the legacy path
    return rng.integers(lo, hi + 1, size=m, dtype=np.int32)


def build_sales_returns_from_detail(
    detail: pa.Table,
    *,
    chunk_seed: int,
    cfg: ReturnsConfig,
) -> pa.Table:
    """
    Build Returns event rows from OrderDetail lines.

    Supports multi-event returns: a single line item can produce multiple
    return events on different dates (e.g., return 5 of 10 on day 3, then
    2 more on day 8, keeping 3).

    Determinism: seeded from chunk_seed.
    """
    # Mirror the detail table's OrderNumber dtype (int32 or, for large runs,
    # int64) so returns never truncate the order number.
    _so_type = (
        detail.schema.field("OrderNumber").type
        if "OrderNumber" in detail.schema.names
        else pa.int32()
    )

    if not cfg.enabled:
        return _empty_returns_table(_so_type)

    rr, min_lag, max_lag, reason_keys, reason_probs = _validate_cfg(cfg)
    if rr <= 0.0:
        return _empty_returns_table(_so_type)

    n = int(detail.num_rows)
    if n <= 0:
        return _empty_returns_table(_so_type)

    _ensure_required_columns(detail)
    detail_cc = detail.combine_chunks()

    rng = np.random.default_rng(int(chunk_seed) ^ 0xA5A5_F00D)

    qty_all = _as_np_i32(_col_np(detail_cc, "Quantity"))
    if qty_all.size != n:
        raise RuntimeError("Returns builder: unexpected Quantity length mismatch.")

    # OrderNumber/line for all rows — the friction latent keys on them, and the
    # masked subset feeds the returns output. Read as int64 so large order
    # numbers aren't truncated; the output is built back to _so_type below.
    so_all = _as_np_i64(_col_np(detail_cc, "OrderNumber"))
    line_all = _as_np_i32(_col_np(detail_cc, "OrderLineNumber"))

    # Shared fulfillment friction couples returns to delivery — the
    # SAME per-line friction the delivery pass used (recomputed here from
    # OrderNumber+line) makes late-delivered lines both more likely to be
    # returned and returned sooner.
    use_friction = (cfg.friction_return_boost > 0.0) or (cfg.friction_lag_shorten > 0.0)
    friction_all = line_friction(so_all, line_all) if use_friction else None

    # Select candidate return lines (only positive-quantity lines can be returned).
    if cfg.friction_return_boost > 0.0 and friction_all is not None:
        p_return = np.clip(rr * (1.0 + cfg.friction_return_boost * friction_all), 0.0, 1.0)
    else:
        p_return = rr
    mask = (rng.random(n) < p_return) & (qty_all > 0)
    if not bool(mask.any()):
        return _empty_returns_table(_so_type)

    so = so_all[mask]
    line = line_all[mask]
    delivery_raw = _col_np(detail_cc, "DeliveryDate")[mask]
    delivery = _to_np_date_days(delivery_raw)
    qty = qty_all[mask]
    net_price = _as_np_f64(_col_np(detail_cc, "NetPrice"))[mask]
    is_delayed = _as_np_i32(_col_np(detail_cc, "IsOrderDelayed"))[mask]
    friction = friction_all[mask] if friction_all is not None else None

    m = int(qty.size)
    if m == 0:
        return _empty_returns_table(_so_type)

    # --- Determine return quantity per line ---
    full_line = rng.random(m) < cfg.full_line_probability
    u = rng.random(m)
    ret_qty = np.where(
        full_line,
        qty,
        (np.floor(u * qty).astype(np.int32) + 1).clip(1, qty),
    ).astype(np.int32)

    # --- Determine number of events per line (vectorized) ---
    max_splits = max(1, cfg.max_splits)
    num_events = np.ones(m, dtype=np.int32)
    if cfg.split_return_rate > 0:
        is_split = rng.random(m) < cfg.split_return_rate
        can_split = is_split & (ret_qty >= 2)
        n_can = int(can_split.sum())
        if n_can > 0:
            k_max = np.minimum(ret_qty[can_split], max_splits)
            # Draw uniform in [2, k_max] for each splittable line
            u = rng.random(n_can)
            num_events[can_split] = np.where(
                k_max >= 2,
                (2 + np.floor(u * (k_max - 1))).astype(np.int32),
                1,
            )

    total_events = int(num_events.sum())

    # --- Expand parent arrays by num_events ---
    so_exp = np.repeat(so, num_events)
    line_exp = np.repeat(line, num_events)
    delivery_exp = np.repeat(delivery, num_events)
    qty_exp = np.repeat(qty, num_events)  # original line qty (for pro-rating)
    net_price_exp = np.repeat(net_price, num_events)

    # --- ReturnSequence: 1, 2, 3... per group ---
    if total_events == m:
        seq = np.ones(total_events, dtype=np.int32)
    else:
        seq = np.ones(total_events, dtype=np.int32)
        group_starts = np.cumsum(num_events)[:-1]
        seq[group_starts] -= num_events[:-1]
        np.cumsum(seq, out=seq)

    # --- Partition return quantities across events ---
    # Single-event lines: event_qty = ret_qty (vectorized assignment)
    # Multi-event lines: random partition via cut points (loop only over splits)
    event_qty = np.zeros(total_events, dtype=np.int32)
    offsets = np.zeros(m + 1, dtype=np.int64)
    np.cumsum(num_events, out=offsets[1:])

    single_mask = num_events == 1
    if single_mask.any():
        event_qty[offsets[:-1][single_mask]] = ret_qty[single_mask]

    multi_idx = np.flatnonzero(~single_mask)
    for i in multi_idx:
        k = int(num_events[i])
        rq = int(ret_qty[i])
        p = int(offsets[i])
        cuts = np.sort(rng.choice(np.arange(1, rq), size=k - 1, replace=False))
        parts = np.diff(np.concatenate([[0], cuts, [rq]]))
        event_qty[p:p + k] = parts.astype(np.int32)

    # --- Compute dates ---
    lo = max(0, int(min_lag))
    hi = max(lo, int(max_lag))
    # RETURNS-1: one base lag per PARENT LINE, broadcast to its events — not an
    # independent draw per event. Sharing the base means the cumulative split
    # gaps below make ReturnDate non-decreasing by ReturnSequence; a fresh base
    # per event could let a later event land earlier than an earlier one. The lag
    # shape (uniform/triangular/normal) + peak come from models.yaml ->
    # returns.lag_days.{distribution,mode}.
    per_line_lag = _draw_base_lag(rng, m, lo, hi, cfg.lag_distribution, cfg.lag_mode)
    # Shorten the lag for high-friction (late-delivered) lines so they
    # are returned sooner. Deterministic scale, no extra RNG draw.
    if cfg.friction_lag_shorten > 0.0 and friction is not None:
        _scale = 1.0 - cfg.friction_lag_shorten * friction
        per_line_lag = np.clip(np.rint(per_line_lag * _scale), lo, hi).astype(np.int32)
    base_lag = np.repeat(per_line_lag, num_events)

    # For split events (seq > 1), add cumulative incremental gaps
    # Each subsequent event adds an independent gap, accumulated via cumsum
    # within each group to guarantee non-decreasing dates.
    if total_events > m:
        per_event_gap = np.where(
            seq > 1,
            rng.integers(
                max(1, cfg.split_min_gap), max(2, cfg.split_max_gap + 1),
                size=total_events, dtype=np.int32,
            ),
            0,
        ).astype(np.int32)
        # Segmented cumsum within groups (vectorized reset trick)
        # Full cumsum minus the cumsum value at each group start gives within-group cumsum
        extra_lag = np.cumsum(per_event_gap)
        group_starts = offsets[:-1]  # reuse offsets from partition step
        correction = np.repeat(
            np.concatenate([[0], extra_lag[group_starts[1:] - 1]]),
            num_events,
        )
        extra_lag = (extra_lag - correction).astype(np.int32)
    else:
        extra_lag = np.zeros(total_events, dtype=np.int32)

    lag = (base_lag + extra_lag).astype("timedelta64[D]")
    ret_date = (delivery_exp + lag).astype("datetime64[D]", copy=False)

    # --- Reason keys (category-aware per event) ---
    # Logistics reasons (e.g., "Late delivery", "Damaged in shipping") are only
    # valid for delayed orders. On-time orders get those weights redistributed.
    reason = np.empty(total_events, dtype=np.int32)
    if cfg.logistics_keys:
        logistics_mask = np.array([k in cfg.logistics_keys for k in reason_keys], dtype=bool)
        probs_ontime = _ontime_reason_probs(reason_probs, logistics_mask)

        is_delayed_exp = np.repeat(is_delayed, num_events) > 0
        n_delayed = int(is_delayed_exp.sum())
        n_ontime = total_events - n_delayed
        if n_delayed > 0:
            reason[is_delayed_exp] = rng.choice(reason_keys, size=n_delayed, p=reason_probs).astype(np.int32)
        if n_ontime > 0:
            reason[~is_delayed_exp] = rng.choice(reason_keys, size=n_ontime, p=probs_ontime).astype(np.int32)
    else:
        reason[:] = rng.choice(reason_keys, size=total_events, p=reason_probs).astype(np.int32)

    # --- ReturnNetPrice: pro-rated by event quantity ---
    unit_price = net_price_exp / np.maximum(qty_exp.astype(np.float64), 1.0)
    return_net_price = np.round(unit_price * event_qty.astype(np.float64), 2)
    if cfg.reconcile_cents and multi_idx.size:
        # Integer-cent largest-remainder apportionment: a line's multi-event
        # ReturnNetPrice values sum EXACTLY to its single-event equivalent
        # (round(unit_price * ret_qty, 2)) instead of drifting by per-event
        # rounding. Pure arithmetic — no RNG draw — so the flag-on path leaves the
        # rest of the returns table (dates, reasons, qty, keys) byte-identical to
        # the flag-off path; only multi-event ReturnNetPrice values change.
        for i in multi_idx:
            k = int(num_events[i])
            p = int(offsets[i])
            ev = event_qty[p:p + k].astype(np.int64)
            rq_line = int(ev.sum())                      # == ret_qty[i]
            if rq_line <= 0:
                continue
            up_line = float(net_price[i]) / max(float(qty[i]), 1.0)
            total_cents = int(np.round(up_line * rq_line * 100.0))
            ideal = total_cents * ev
            base = ideal // rq_line                       # floor cents per event
            leftover = int(total_cents - int(base.sum()))
            if leftover > 0:
                rem = ideal - base * rq_line              # Hamilton remainders
                # +1 cent to the largest remainders; ties broken by event order.
                order = np.lexsort((np.arange(k), -rem))
                base[order[:leftover]] += 1
            return_net_price[p:p + k] = base.astype(np.float64) / 100.0

    # --- ReturnEventKey: sequential, globally unique via offset ---
    return_event_key = cfg.event_key_offset + np.arange(1, total_events + 1, dtype=np.int64)

    return pa.table(
        {
            "OrderNumber": pa.array(so_exp, type=_so_type),
            "OrderLineNumber": pa.array(line_exp, type=pa.int32()),
            "ReturnDate": pa.array(ret_date, type=pa.date32()),
            "ReturnReasonKey": pa.array(reason, type=pa.int32()),
            "ReturnQuantity": pa.array(event_qty, type=pa.int32()),
            "ReturnNetPrice": pa.array(return_net_price, type=pa.float64()),
            "ReturnSequence": pa.array(seq, type=pa.int32()),
            "ReturnEventKey": pa.array(return_event_key, type=pa.int64()),
        },
        schema=_returns_schema_for(_so_type),
    )
