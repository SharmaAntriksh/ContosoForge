"""Date logic: formatting, hashing, and delivery-date computation."""

from __future__ import annotations

import numpy as np

# ``fmt`` (datetime64 -> YYYYMMDD string) is defined once in ``..globals`` and
# re-exported here so existing import paths keep working; it was previously
# duplicated in this module.
from ..globals import fmt  # noqa: F401  (re-export for core + tests)


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
# SplitMix64 hashing (vectorized, deterministic)
# ----------------------------------------------------------------

_C1 = np.uint64(0x9E3779B97F4A7C15)
_C2 = np.uint64(0xBF58476D1CE4E5B9)
_C3 = np.uint64(0x94D049BB133111EB)
_MASK63 = np.uint64(0x7FFFFFFFFFFFFFFF)


def _mix_u64(x: np.ndarray) -> np.ndarray:
    # SplitMix64-style mixing (vectorized)
    x ^= (x >> np.uint64(30))
    x *= _C2
    x ^= (x >> np.uint64(27))
    x *= _C3
    x ^= (x >> np.uint64(31))
    return x


def _stable_row_hash(order_dates: np.ndarray, product_keys: np.ndarray) -> np.ndarray:
    """
    Deterministic row hash used when skip_order_cols=True (order_ids_int is None).
    IMPORTANT: uses uint64 ops to avoid numpy float/object promotion and overflows.
    """
    d = np.asarray(order_dates).astype("datetime64[D]").astype("int64", copy=False).astype(np.uint64, copy=False)
    p = np.asarray(product_keys).astype(np.uint64, copy=False)

    x = d * _C1
    x ^= (p + _C2)
    x = _mix_u64(x)

    # Return signed int64 in [0, 2^63-1]
    return (x & _MASK63).astype(np.int64, copy=False)


# ----------------------------------------------------------------
# compute_dates
# ----------------------------------------------------------------

def compute_dates(rng, n, product_keys, order_ids_int, order_dates,
                   *, channel_keys=None, channel_fulfillment_days=None,
                   unique_orders=None, inv_idx=None):
    """
    Compute due dates, delivery dates, delivery status, and order delay flag.

    Supports:
    - order_ids_int present  → order-level coherent behavior
    - order_ids_int is None → row-level fallback (skip_order_cols=True)
    - channel_keys + channel_fulfillment_days → channel-aware due date offsets

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
        mixed = _mix_u64(unique_orders.astype(np.uint64).copy())
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
    # Seeds (vectorized) - reuse modular reductions
    # ------------------------------------------------------------
    # Keep semantics equivalent to original:
    # order_seed = hash % 100
    # product_seed = (hash + product_keys) % 100
    # line_seed = (product_keys + (hash % 100)) % 100
    # Note: product_seed == line_seed under mod 100; compute once.
    hs = hash_vals % 100
    pk = product_keys % 100
    order_seed = hs
    product_seed = (hs + pk) % 100
    line_seed = product_seed  # same under mod 100

    # ------------------------------------------------------------
    # Base delivery offset (relative to due date)
    # ------------------------------------------------------------
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

    # ------------------------------------------------------------
    # Early deliveries (line-item mixed even when has order ids)
    # NOTE: keep RNG draw shapes consistent (still draw per-order),
    # but only a subset of lines in an "early" order become early.
    # ------------------------------------------------------------
    if has_orders:
        n_orders = len(unique_orders)

        # One early flag per order (10%)
        early_order = rng.random(n_orders) < 0.10
        # Early days per order: 1..2
        early_days_per_order = rng.integers(1, 3, size=n_orders, dtype=np.int64)

        # Only some lines in an early order are early (~35%).
        # Use an independent RNG draw so early-delivery is not coupled to
        # product key (line_seed depends on product via hash).
        early_mask = early_order[inv_idx] & (rng.random(n) < 0.35)

        if early_mask.any():
            early_days_rows = early_days_per_order[inv_idx]
            # Early overrides delay for those *lines* only
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
