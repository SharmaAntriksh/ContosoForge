"""Date logic: formatting, hashing, and delivery-date computation."""

from __future__ import annotations

import numpy as np

# ``fmt`` (datetime64 -> YYYYMMDD string) is defined once in ``..globals`` and
# re-exported here so existing import paths keep working; it was previously
# duplicated in this module.
from ..globals import fmt  # noqa: F401  (re-export for core + tests)
from src.utils.hashing import GOLDEN, MIX_A, splitmix64, u01_from_u64


# ----------------------------------------------------------------
# Date formatting
# ----------------------------------------------------------------

def _yyyymmdd_from_days(days: np.ndarray) -> np.ndarray:
    """
    Convert days-since-epoch (int64) to YYYYMMDD (int64) WITHOUT string ops.
    Uses numpy datetime64 conversions (fast, vectorized).
    """
    d = days.astype("datetime64[D]")
    y = d.astype("datetime64[Y]").astype(np.int64) + 1970  # years since 1970
    m = (d.astype("datetime64[M]").astype(np.int64) % 12) + 1
    day = (d - d.astype("datetime64[M]")).astype(np.int64) + 1
    return (y * 10000 + m * 100 + day).astype(np.int64, copy=False)


# ----------------------------------------------------------------
# Row hashing (SplitMix64 finalizer imported from src.utils.hashing)
# ----------------------------------------------------------------

_MASK63 = np.uint64(0x7FFFFFFFFFFFFFFF)


def _stable_row_hash(order_dates: np.ndarray, product_keys: np.ndarray) -> np.ndarray:
    """
    Deterministic row hash used when skip_order_cols=True (order_ids_int is None).
    IMPORTANT: uses uint64 ops to avoid numpy float/object promotion and overflows.
    """
    d = np.asarray(order_dates).astype("datetime64[D]").astype("int64", copy=False).astype(np.uint64, copy=False)
    p = np.asarray(product_keys).astype(np.uint64, copy=False)

    x = d * GOLDEN
    x ^= (p + MIX_A)
    x = splitmix64(x)

    # Return signed int64 in [0, 2^63-1]
    return (x & _MASK63).astype(np.int64, copy=False)


# ----------------------------------------------------------------
# Fulfillment friction latent
# ----------------------------------------------------------------

_FRICTION_C = np.uint64(0xD1B54A32D192ED03)


def line_friction(order_numbers: np.ndarray, line_numbers: np.ndarray) -> np.ndarray:
    """Per-line fulfillment-friction latent in [0, 1).

    A pure, stateless SplitMix64 hash of the globally-unique
    ``(OrderNumber, OrderLineNumber)`` pair, so the *same* line gets the *same*
    friction whether it is computed in the delivery pass (chunk builder) or
    recomputed later in the separate returns pass — independent of chunk_size,
    worker count, or RNG state. High friction => late delivery + more/faster
    returns.
    """
    o = np.asarray(order_numbers).astype(np.int64, copy=False).astype(np.uint64, copy=False)
    ln = np.asarray(line_numbers).astype(np.int64, copy=False).astype(np.uint64, copy=False)
    x = o * GOLDEN
    x ^= (ln + _FRICTION_C)
    x = splitmix64(x)
    # Top 53 bits -> uniform double in [0, 1)
    return u01_from_u64(x)


def _delay_from_quantile(t: np.ndarray, dmin: int, dmax: int, mode: int,
                         distribution: str) -> np.ndarray:
    """Map a per-line quantile ``t`` in [0,1) to an integer delay in [dmin, dmax].

    ``distribution`` is the inverse-CDF shape (``"uniform"`` or ``"triangular"``
    peaked at ``mode``), so the delay magnitude is a deterministic function of the
    friction latent — no RNG consumed.
    """
    dmin = int(dmin)
    dmax = int(dmax)
    if dmax <= dmin:
        return np.full(t.shape, dmin, dtype=np.int64)
    if str(distribution).strip().lower() == "triangular":
        c = float(min(max(int(mode), dmin), dmax))
        span = float(dmax - dmin)
        fc = (c - dmin) / span if span > 0 else 0.0
        left = t < fc
        val = np.empty(t.shape, dtype=np.float64)
        # inverse CDF of the triangular distribution
        if fc > 0:
            val[left] = dmin + np.sqrt(t[left] * span * (c - dmin))
        else:
            val[left] = dmin
        if fc < 1:
            val[~left] = dmax - np.sqrt((1.0 - t[~left]) * span * (dmax - c))
        else:
            val[~left] = dmax
        return np.clip(np.rint(val), dmin, dmax).astype(np.int64)
    # uniform
    return np.clip(dmin + np.floor(t * (dmax - dmin + 1)), dmin, dmax).astype(np.int64)


_TWO_POW_63 = np.float64(9223372036854775808.0)  # 2**63


def _friction_delivery_offset(n, has_orders, order_ids_int, line_numbers,
                              hash_vals, cfg):
    """Friction-driven delivery offset, relative to the due date.

    Deterministic (no RNG): the per-line friction latent buckets a line into
    early (offset < 0), on-time (0), or delayed (offset > 0) via (p_early,
    p_delayed), and the delay magnitude is the inverse-CDF of the configured
    distribution at the friction sub-quantile.
    """
    if has_orders and line_numbers is not None:
        friction = line_friction(order_ids_int, np.asarray(line_numbers))
    else:
        # skip_order_cols: no OrderNumber/line, so reuse the per-row stable hash.
        # Returns are disabled in this mode, so cross-pass consistency is moot.
        friction = np.asarray(hash_vals, dtype=np.float64) / _TWO_POW_63

    p_early = min(max(float(cfg.get("p_early", 0.10)), 0.0), 1.0)
    p_delayed = min(max(float(cfg.get("p_delayed", 0.20)), 0.0), 1.0 - p_early)
    dmin = int(cfg.get("delay_min", 1))
    dmax = int(cfg.get("delay_max", 10))
    mode = int(cfg.get("delay_mode", 3))
    dist = str(cfg.get("delay_distribution", "triangular"))

    offset = np.zeros(n, dtype=np.int64)
    if p_early > 0.0:
        early = friction < p_early
        te = np.clip(friction / p_early, 0.0, 1.0)
        early_days = np.where(te < 0.5, 1, 2).astype(np.int64)
        offset[early] = -early_days[early]
    if p_delayed > 0.0:
        delayed = friction >= (1.0 - p_delayed)
        td = np.clip((friction - (1.0 - p_delayed)) / p_delayed, 0.0, 1.0)
        delay_days = _delay_from_quantile(td, dmin, dmax, mode, dist)
        offset[delayed] = delay_days[delayed]
    return offset


# ----------------------------------------------------------------
# compute_dates
# ----------------------------------------------------------------

def compute_dates(rng, n, product_keys, order_ids_int, order_dates,
                   *, channel_keys=None, channel_fulfillment_days=None,
                   unique_orders=None, inv_idx=None,
                   line_numbers=None, fulfillment_cfg=None):
    """
    Compute due dates, delivery dates, delivery status, and order delay flag.

    Supports:
    - order_ids_int present  → order-level coherent behavior
    - order_ids_int is None → row-level fallback (skip_order_cols=True)
    - channel_keys + channel_fulfillment_days → channel-aware due date offsets
    - fulfillment_cfg → friction-driven early/on-time/delayed
      bucketing with an explicit named delay-magnitude distribution, replacing
      the legacy mod-100 ladder + RNG early draws. Deterministic (no RNG). When
      None/disabled, the legacy path is used unchanged.

    Returns dict of numpy arrays:
      due_date: datetime64[D]
      delivery_date: datetime64[D]
      delivery_status: fixed-width unicode (U15)
      is_order_delayed: bool
    """
    n = int(n)
    if n <= 0:
        return {
            "due_date": np.empty(0, dtype="datetime64[D]"),
            "delivery_date": np.empty(0, dtype="datetime64[D]"),
            "delivery_status": np.empty(0, dtype="U15"),
            "is_order_delayed": np.empty(0, dtype=bool),
        }

    # Normalize inputs once
    product_keys = np.asarray(product_keys, dtype=np.int32)
    order_dates = np.asarray(order_dates).astype("datetime64[D]", copy=False)

    has_orders = order_ids_int is not None

    if has_orders:
        order_ids_int = np.asarray(order_ids_int, dtype=np.int64)

        # Map rows → order index (order-level coherence).  The caller can pass
        # the precomputed grouping (the chunk builder already derives it from
        # the line-number cumsum), avoiding a redundant O(n log n) np.unique
        # sort.  order_ids_int is np.repeat(sequential_ids, repeats) — already
        # sorted/grouped — so the derived outputs equal np.unique's.
        if inv_idx is None:
            unique_orders, inv_idx = np.unique(order_ids_int, return_inverse=True)
        else:
            inv_idx = np.asarray(inv_idx, dtype=np.int64)

        # Mix sequential order IDs to break visible due-date patterns
        mixed = splitmix64(unique_orders.astype(np.uint64).copy())
        hash_vals = (mixed & _MASK63).astype(np.int64)[inv_idx]
    else:
        # Deterministic per-row hash without consuming RNG
        hash_vals = _stable_row_hash(order_dates, product_keys)

    # ------------------------------------------------------------
    # Due dates: channel-aware fulfillment + jitter
    # CORRELATION #3: ChannelKey → DeliveryDate
    # Physical channels (Store, Kiosk) → 0 days base (immediate)
    # Digital channels → 2-5 days base
    # B2B → 5-10 days base
    # Fallback (no channel data): 3..7 days (original behavior)
    # ------------------------------------------------------------
    if channel_keys is not None and channel_fulfillment_days is not None:
        _ch = np.asarray(channel_keys, dtype=np.int32)
        _ch_clipped = np.clip(_ch, 0, len(channel_fulfillment_days) - 1)
        _base_days = channel_fulfillment_days[_ch_clipped].astype(np.int64)
        # Add hash-based jitter: -1 to +2 days around the base
        _jitter = (hash_vals % 4) - 1  # -1, 0, 1, 2
        due_offset = np.maximum(_base_days + _jitter, 0)
    else:
        # Original behavior: 3..7 days
        due_offset = (hash_vals % 5) + 3
    due_date = order_dates + due_offset.astype("timedelta64[D]")

    # ------------------------------------------------------------
    # Delivery offset (relative to due date)
    # ------------------------------------------------------------
    _ff_on = bool(fulfillment_cfg) and bool(fulfillment_cfg.get("enabled", False))

    if _ff_on:
        # Friction-driven bucketing (deterministic, no RNG).
        delivery_offset = _friction_delivery_offset(
            n, has_orders, order_ids_int, line_numbers, hash_vals, fulfillment_cfg
        )
    else:
        # Legacy mod-100 ladder + per-order RNG early draws.
        # order_seed = hash % 100; product_seed = (hash + product_keys) % 100;
        # line_seed == product_seed under mod 100.
        hs = hash_vals % 100
        pk = product_keys % 100
        order_seed = hs
        product_seed = (hs + pk) % 100
        line_seed = product_seed

        delivery_offset = np.zeros(n, dtype=np.int64)

        # Condition C: small delay (1..4)
        mask_c = (order_seed >= 60) & (order_seed < 85) & (product_seed >= 60)
        if mask_c.any():
            delivery_offset[mask_c] = (line_seed[mask_c] % 4) + 1

        # Condition D: larger delay (2..6) – filter by product_seed so some
        # lines stay non-delayed, allowing all 3 statuses within one order.
        mask_d = (order_seed >= 85) & (product_seed >= 40)
        if mask_d.any():
            delivery_offset[mask_d] = (product_seed[mask_d] % 5) + 2

        # Early deliveries (line-item mixed even when has order ids).
        if has_orders:
            n_orders = len(unique_orders)
            early_order = rng.random(n_orders) < 0.10
            early_days_per_order = rng.integers(1, 3, size=n_orders, dtype=np.int64)
            early_mask = early_order[inv_idx] & (rng.random(n) < 0.35)
            if early_mask.any():
                early_days_rows = early_days_per_order[inv_idx]
                delivery_offset[early_mask] = -early_days_rows[early_mask]
        else:
            early_mask = rng.random(n) < 0.10
            if early_mask.any():
                early_days = rng.integers(1, 3, size=n, dtype=np.int64)
                delivery_offset[early_mask] = -early_days[early_mask]

    # Final delivery date (never before order date)
    delivery_date = due_date + delivery_offset.astype("timedelta64[D]")
    delivery_date = np.maximum(delivery_date, order_dates)

    # Recompute effective offset after clamping so status labels match
    effective_offset = (delivery_date - due_date).astype(np.int64)

    # ------------------------------------------------------------
    # Delivery status (use effective offset after clamping)
    # ------------------------------------------------------------
    # 0 = On Time, 1 = Early, 2 = Delayed
    codes = np.zeros(n, dtype=np.int32)
    codes[effective_offset < 0] = 1
    codes[effective_offset > 0] = 2
    labels = np.array(["On Time", "Early Delivery", "Delayed"], dtype="U15")
    delivery_status = labels[codes]

    # ------------------------------------------------------------
    # Order delayed flag (order-level coherence when has order ids)
    # ------------------------------------------------------------
    delayed_line = effective_offset > 0

    if has_orders:
        # Any delayed line → order delayed
        delayed_any = (
            np.bincount(inv_idx, weights=delayed_line.astype(np.float64), minlength=len(unique_orders)) > 0
        )
        is_order_delayed = delayed_any[inv_idx].astype(bool, copy=False)
    else:
        is_order_delayed = delayed_line.astype(bool, copy=False)

    return {
        "due_date": due_date,
        "delivery_date": delivery_date,
        "delivery_status": delivery_status,
        "is_order_delayed": is_order_delayed,
    }
