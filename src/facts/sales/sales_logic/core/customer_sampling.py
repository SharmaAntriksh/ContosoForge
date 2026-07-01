"""Customer sampling: eligibility, participation targets, and discovery."""

from __future__ import annotations

import math
import warnings
from typing import Optional

import numpy as np


# ----------------------------------------------------------------
# End-month normalization
# ----------------------------------------------------------------

def _normalize_end_month(end_month_arr, n_customers: int) -> np.ndarray:
    """
    Convert nullable end-month representations into an int64 array with -1 meaning "no end inside window".
    """
    n_customers = int(n_customers)
    if end_month_arr is None:
        return np.full(n_customers, -1, dtype="int64")

    a = np.asarray(end_month_arr)

    if a.shape[0] != n_customers:
        warnings.warn(
            f"end_month_arr length ({a.shape[0]}) != n_customers ({n_customers}). "
            f"Excess elements will be ignored; missing entries default to -1 (no end).",
            stacklevel=2,
        )
        # Truncate or pad to match expected length
        if a.shape[0] > n_customers:
            a = a[:n_customers]
        else:
            deficit = n_customers - a.shape[0]
            if a.dtype == object:
                pad = np.array([None] * deficit, dtype=object)
            elif np.issubdtype(a.dtype, np.floating):
                pad = np.full(deficit, np.nan, dtype=a.dtype)
            else:
                pad = np.full(deficit, -1, dtype=a.dtype)
            a = np.concatenate([a, pad])

    if np.issubdtype(a.dtype, np.integer):
        out = a.astype("int64", copy=False)
        out = np.where(out < 0, -1, out)
        return out

    if np.issubdtype(a.dtype, np.floating):
        out = np.where(np.isnan(a), -1, a).astype("int64")
        out[out < 0] = -1
        return out

    if a.dtype == object:
        try:
            import pandas as pd

            s = pd.Series(a, copy=False)
            num = pd.to_numeric(s, errors="coerce")
            out = num.fillna(-1).astype("int64").to_numpy()
            out[out < 0] = -1
            return out
        except (ValueError, TypeError):
            warnings.warn(
                "end_month_arr contains non-numeric object values; "
                "falling back to per-element conversion (may be slow for large arrays)",
                stacklevel=2,
            )
            out = np.full(n_customers, -1, dtype="int64")
            for i in range(min(n_customers, a.shape[0])):
                v = a[i]
                if v is None:
                    continue
                try:
                    iv = int(v)
                    out[i] = iv if iv >= 0 else -1
                except (ValueError, TypeError):
                    pass
            return out

    try:
        out = a.astype("int64", copy=False)
        out[out < 0] = -1
        return out
    except (ValueError, TypeError):
        return np.full(n_customers, -1, dtype="int64")


# ----------------------------------------------------------------
# Eligibility
# ----------------------------------------------------------------

def _eligible_customer_mask_for_month(
    m_offset: int,
    is_active_in_sales: np.ndarray,
    start_month: np.ndarray,
    end_month_norm: np.ndarray,
) -> np.ndarray:
    """
    Eligibility:
      active == 1
      start_month <= m_offset
      end_month == -1 OR m_offset <= end_month
    """
    m = int(m_offset)
    is_active_in_sales = np.asarray(is_active_in_sales, dtype="int64", order="C")
    start_month = np.asarray(start_month, dtype="int64", order="C")
    end_month_norm = np.asarray(end_month_norm, dtype="int64", order="C")

    return (
        (is_active_in_sales == 1)
        & (start_month <= m)
        & ((end_month_norm < 0) | (m <= end_month_norm))
    )


# ----------------------------------------------------------------
# Participation target
# ----------------------------------------------------------------

def _participation_distinct_target(
    rng: np.random.Generator,
    m_offset: int,
    eligible_count: int,
    n_orders: int,
    cfg: dict,
) -> int:
    """
    Target number of distinct customers to appear in the month.
    """
    eligible_count = int(eligible_count)
    n_orders = int(n_orders)
    if eligible_count <= 0 or n_orders <= 0:
        return 0

    base_ratio = float(cfg.get("base_distinct_ratio", 0.0))
    min_k = int(cfg.get("min_distinct_customers", 0))
    max_ratio = float(cfg.get("max_distinct_ratio", 1.0))

    k = eligible_count * base_ratio

    cycles_cfg = cfg.get("cycles", {}) or {}
    if bool(cycles_cfg.get("enabled", False)):
        period = int(cycles_cfg.get("period_months", 24))
        amp = float(cycles_cfg.get("amplitude", 0.0))
        phase = float(cycles_cfg.get("phase", 0.0))
        noise_std = float(cycles_cfg.get("noise_std", 0.0))

        cyc = math.sin((2.0 * math.pi * float(m_offset) / max(period, 1)) + phase)
        mult = 1.0 + (amp * cyc)
        if noise_std > 0:
            mult += float(rng.normal(loc=0.0, scale=noise_std))

        mult = max(0.05, min(mult, 3.0))
        k *= mult

    k = max(k, float(min_k))
    k = min(k, eligible_count * max_ratio)
    k = min(k, float(eligible_count), float(n_orders))

    # round() can push k above n_orders at the boundary
    return min(int(max(1, round(k))), n_orders, eligible_count)


# ------------------------------------------------------------
# Shared weight normalization
# ------------------------------------------------------------

def _normalize_weights(w: np.ndarray) -> Optional[np.ndarray]:
    """
    Shared weight normalization used by both _weights_for_indices
    and _weights_for_keys. Returns a valid probability vector, or None if
    all weights are zero/invalid (caller should fall back to uniform).
    """
    w = np.asarray(w, dtype="float64")
    w = np.where(np.isfinite(w), w, 0.0)
    w = np.clip(w, 1e-12, None)
    s = w.sum()
    if s <= 0.0:
        return None
    return w / s


# ------------------------------------------------------------
# Sampling helpers
# ------------------------------------------------------------

def _weights_for_indices(indices: np.ndarray, base_weight: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """
    Build probability vector p aligned with a subset of dimension indices.
    This path is correct even if CustomerKey isn't dense/sequential.
    """
    if base_weight is None:
        return None
    try:
        idx = np.asarray(indices, dtype=np.int32)
        if idx.size > 0 and (idx.max() >= base_weight.shape[0] or idx.min() < 0):
            return None  # Out-of-range indices; fall back to uniform
        w = base_weight[idx]
        return _normalize_weights(w)
    except (IndexError, ValueError, TypeError):
        return None


def _weights_for_keys(keys: np.ndarray, base_weight: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """
    Map CustomerKey (1-based) to base_weight indices and return a probability vector.

    Contract: CustomerKey values are 1-based (key 1 -> base_weight[0]).
    If mapping fails or keys are out of range, returns None (uniform sampling).
    """
    if base_weight is None:
        return None
    try:
        keys_i32 = np.asarray(keys, dtype=np.int32)

        idx = keys_i32 - 1
        if idx.size == 0:
            return None
        if idx.min() < 0 or idx.max() >= base_weight.shape[0]:
            return None
        w = base_weight[idx]
        return _normalize_weights(w)
    except (IndexError, ValueError, TypeError):
        return None


def _choice(
    rng: np.random.Generator,
    keys: np.ndarray,
    size: int,
    *,
    replace: bool,
    p: Optional[np.ndarray],
) -> np.ndarray:
    if size <= 0:
        return np.empty(0, dtype=keys.dtype)
    if p is None:
        return rng.choice(keys, size=size, replace=replace)
    return rng.choice(keys, size=size, replace=replace, p=p)


def _concat_and_shuffle(rng: np.random.Generator, *arrays: np.ndarray) -> np.ndarray:
    """Concatenate non-empty arrays and shuffle the result in-place."""
    parts = [a for a in arrays if a.size > 0]
    if len(parts) == 0:
        dtype = arrays[0].dtype if arrays else "int64"
        return np.empty(0, dtype=dtype)
    out = np.concatenate(parts) if len(parts) > 1 else parts[0].copy()
    rng.shuffle(out)
    return out


# ----------------------------------------------------------------
# Closed-form customer discovery schedule
# ----------------------------------------------------------------
# The month each customer first enters the sales population ("discovery") is a
# pure function of ``(CustomerKey, run_seed)`` and the customer's eligibility
# window — computed ONCE per run and broadcast read-only to every worker. This
# replaces the old mutable, per-worker ``seen_customers`` accumulator whose
# contents depended on which chunks a worker happened to process, which made the
# output depend on ``--workers`` (review Finding #5/#6). With a static schedule
# every chunk is a pure function of its own inputs, so worker count no longer
# affects the generated sales fact.

_SPLITMIX_MASK = np.uint64(0xFFFFFFFFFFFFFFFF)


def _hash_uniform(keys: np.ndarray, seed: int) -> np.ndarray:
    """Deterministic per-key uniform draw in ``[0, 1)`` from ``(key, seed)``.

    Vectorized splitmix64-style mix; stable across runs, platforms, and worker
    counts. ``seed`` is folded in so different run seeds reshuffle discovery
    timing even for an unchanged customer dimension.
    """
    k = np.asarray(keys).astype(np.uint64)
    # Pre-mix the scalar seed into a 64-bit constant (non-zero even for seed 0).
    s_val = (int(seed) * 0x2545F4914F6CDD1D + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    s = np.uint64(s_val)
    with np.errstate(over="ignore"):
        z = (k * np.uint64(0x9E3779B97F4A7C15)) ^ s
        z ^= (z >> np.uint64(30))
        z = z * np.uint64(0xBF58476D1CE4E5B9)
        z ^= (z >> np.uint64(27))
        z = z * np.uint64(0x94D049BB133111EB)
        z ^= (z >> np.uint64(31))
    # Top 53 bits → double in [0, 1).
    return (z >> np.uint64(11)).astype(np.float64) * (1.0 / float(1 << 53))


def compute_discovery_months(
    customer_keys: np.ndarray,
    is_active_in_sales: np.ndarray,
    start_month: np.ndarray,
    end_month,
    T: int,
    run_seed: int,
    *,
    lag_scale: float = 1.0,
) -> np.ndarray:
    """Assign every customer the month they are first introduced into sales.

    Returns an int64 array aligned with ``customer_keys``. Discoverable
    customers get a value in ``[0, T-1]``; inactive customers and those whose
    join month falls after the window get the sentinel ``T`` ("never", which is
    strictly greater than any real month offset).

    The month is anchored at the customer's eligibility start and pushed forward
    by a small, deterministic, hash-seeded lag (mean ``lag_scale`` months) so
    that discovery is spread realistically past the join month rather than every
    customer transacting the instant they become eligible. The lag is clamped to
    the customer's end month so a churning customer is never scheduled past their
    window. Warm-start (pre-existing, ``start_month < 0``) customers are treated
    as already known and get no lag.
    """
    n = int(np.asarray(customer_keys).shape[0])
    T = int(T)
    never = np.int64(max(T, 0))
    if n == 0 or T <= 0:
        return np.full(n, never, dtype=np.int64)

    keys = np.asarray(customer_keys).astype(np.int64)
    active = np.asarray(is_active_in_sales).astype(np.int64) == 1
    sm = np.asarray(start_month).astype(np.int64)
    em = _normalize_end_month(end_month, n)   # -1 => no end within window

    # Earliest possible discovery = the customer's first eligible month.
    s = np.clip(sm, 0, T - 1)
    # Latest possible discovery = last eligible month within the window.
    e = np.where(em < 0, np.int64(T - 1), np.minimum(em, np.int64(T - 1)))
    e = np.maximum(e, s)

    # Deterministic forward lag ~ Exponential(mean=lag_scale), floored to months.
    u = _hash_uniform(keys, run_seed)
    scale = max(0.0, float(lag_scale))
    if scale > 0.0:
        lag = np.floor(-np.log1p(-u) * scale).astype(np.int64)
    else:
        lag = np.zeros(n, dtype=np.int64)
    lag = np.where(sm < 0, np.int64(0), lag)   # warm start: no lag

    disc = np.clip(s + lag, s, e)

    out = np.full(n, never, dtype=np.int64)
    discoverable = active & (sm < T)
    out[discoverable] = disc[discoverable]
    return out


# ----------------------------------------------------------------
# Urgency-based selection for discovery
# ----------------------------------------------------------------

def _urgency_pick(
    rng: np.random.Generator,
    keys: np.ndarray,
    indices: np.ndarray,
    end_month_norm: np.ndarray | None,
    m_offset: int,
    size: int,
) -> np.ndarray:
    """Pick `size` keys from undiscovered, prioritizing nearest expiry.

    Customers with a finite end_month closest to the current month are
    selected first so they aren't lost to churn before discovery.
    Ties (including all open-ended customers) are broken randomly.
    """
    if size <= 0:
        return np.empty(0, dtype=keys.dtype)

    if end_month_norm is None:
        # No expiry info to order by.
        if size >= keys.size:
            return keys.copy()
        return rng.choice(keys, size=size, replace=False)

    # Order by urgency (nearest-expiry first) so a downstream ``[:k]`` slice keeps
    # the most urgent customers — including when every key is forced
    # (size >= keys.size), where the old code returned original key order and a
    # later slice could drop near-expiry customers (CORE-1).
    # end_month == -1 means open-ended → treat as infinite remaining.
    em = end_month_norm[indices]
    remaining_months = np.where(em < 0, np.int64(999_999), em - np.int64(m_offset))

    # Add a tiny random jitter to break ties without full sort stability overhead
    jitter = rng.random(keys.size) * 0.5
    sort_key = remaining_months.astype(np.float64) + jitter

    order = np.argsort(sort_key, kind="quicksort")
    return keys[order[:min(size, keys.size)]]


# ----------------------------------------------------------------
# Main sampling entry point
# ----------------------------------------------------------------

def _sample_customers(
    rng: np.random.Generator,
    customer_keys: np.ndarray,
    eligible_mask: np.ndarray | None,
    discovery_month,
    n: int,
    use_discovery: bool,
    base_weight: np.ndarray | None = None,
    target_distinct: int | None = None,
    end_month_norm: np.ndarray | None = None,
    m_offset: int = 0,
    eligible_idx: np.ndarray | None = None,
) -> np.ndarray:
    """
    Returns array of CustomerKeys of length n, sampled from eligible customers.

    Discovery is closed-form and independent of worker count / chunk order.
    ``discovery_month`` is a pool-aligned int64 array (see
    ``compute_discovery_months``) giving the month each customer is first
    introduced into the sales population. In month ``m_offset``:

    - customers with ``discovery_month == m_offset`` are the debut cohort and are
      force-introduced (nearest-expiry first when the cohort must be truncated);
    - customers with ``discovery_month <= m_offset`` form the repeat pool;
    - customers with ``discovery_month > m_offset`` are not yet introduced and do
      not transact this month.

    - If target_distinct is provided: builds a distinct pool then repeats from it.
    - If end_month_norm is provided: the debut cohort closest to expiry is kept
      first when the cohort must be truncated to fit.

    ``eligible_idx`` may be passed precomputed (the per-month eligible row indices)
    to skip the ``flatnonzero(mask)`` derivation; otherwise it is derived from
    ``eligible_mask``.
    """
    n = int(n)
    if n <= 0:
        return np.empty(0, dtype=np.asarray(customer_keys).dtype)

    customer_keys = np.asarray(customer_keys)

    if eligible_idx is None:
        eligible_mask = np.asarray(eligible_mask, dtype=bool)
        eligible_idx = np.flatnonzero(eligible_mask)
    else:
        eligible_idx = np.asarray(eligible_idx)
    if eligible_idx.size == 0:
        return np.empty(0, dtype=customer_keys.dtype)

    eligible_keys = customer_keys[eligible_idx]

    k = None
    if target_distinct is not None:
        try:
            k0 = int(target_distinct)
            k = max(1, min(k0, int(eligible_keys.size), n))
        except (TypeError, ValueError):
            warnings.warn(
                f"target_distinct={target_distinct!r} is not a valid integer; "
                f"falling back to unlimited distinct customers.",
                stacklevel=2,
            )
            k = None

    # Precompute eligible weights (dimension-aligned)
    p_eligible = _weights_for_indices(eligible_idx, base_weight)

    # -----------------------------
    # No discovery
    # -----------------------------
    if not use_discovery:
        if k is None:
            return _choice(rng, eligible_keys, n, replace=True, p=p_eligible)

        distinct_pool = _choice(rng, eligible_keys, k, replace=False, p=p_eligible)
        remaining = n - distinct_pool.size
        if remaining <= 0:
            return _concat_and_shuffle(rng, distinct_pool)

        p_distinct = _weights_for_keys(distinct_pool, base_weight)
        repeats = _choice(rng, distinct_pool, remaining, replace=True, p=p_distinct)
        return _concat_and_shuffle(rng, distinct_pool, repeats)

    # -----------------------------
    # Discovery mode (closed-form schedule)
    # -----------------------------
    if discovery_month is not None:
        disc_elig = np.asarray(discovery_month)[eligible_idx]
        introduced_mask = disc_elig <= m_offset     # known on/before this month
        debut_mask = disc_elig == m_offset          # scheduled to debut this month
    else:
        # No schedule available: treat everyone eligible as already introduced.
        introduced_mask = np.ones(eligible_keys.size, dtype=bool)
        debut_mask = np.zeros(eligible_keys.size, dtype=bool)

    prior_mask = introduced_mask & ~debut_mask
    seen_eligible = eligible_keys[prior_mask]           # repeat pool (introduced earlier)
    introduced_keys = eligible_keys[introduced_mask]    # everyone allowed to transact now
    p_introduced = _weights_for_indices(eligible_idx[introduced_mask], base_weight)

    debut_keys = eligible_keys[debut_mask]
    debut_idx = eligible_idx[debut_mask]

    # Force the debut cohort in, capped to the slots available (n, and the
    # distinct target k when set). Nearest-expiry first so churning customers
    # are not dropped when the cohort must be truncated.
    cap = n if k is None else k
    if debut_keys.size > cap:
        forced = _urgency_pick(
            rng, debut_keys, debut_idx, end_month_norm, m_offset, cap)
    else:
        forced = debut_keys

    # ------------------------------------------------------------
    # Discovery without participation target
    # ------------------------------------------------------------
    if k is None:
        remaining = n - forced.size
        if remaining <= 0:
            return _concat_and_shuffle(rng, forced)

        # Repeats come only from introduced customers (never the not-yet-
        # introduced future pool). Fall back to all eligible if nobody has been
        # introduced yet this month, so rows are never lost.
        if introduced_keys.size > 0:
            repeat_pool, p_repeat = introduced_keys, p_introduced
        else:
            repeat_pool, p_repeat = eligible_keys, p_eligible

        repeat = _choice(rng, repeat_pool, remaining, replace=True, p=p_repeat)
        return _concat_and_shuffle(rng, forced, repeat)

    # ------------------------------------------------------------
    # Participation-controlled discovery
    # ------------------------------------------------------------
    distinct_pool = forced
    need = k - distinct_pool.size

    # Fill remaining distinct slots from previously-introduced customers
    # (never the not-yet-introduced future pool).
    if need > 0 and seen_eligible.size > 0:
        take_seen = min(need, int(seen_eligible.size))
        seen_extra = rng.choice(seen_eligible, size=take_seen, replace=False)
        distinct_pool = np.concatenate([distinct_pool, seen_extra])

    if distinct_pool.size == 0:
        # Nobody introduced yet this month → organic draw so rows aren't lost.
        return _choice(rng, eligible_keys, n, replace=True, p=p_eligible)

    remaining = n - distinct_pool.size
    if remaining <= 0:
        return _concat_and_shuffle(rng, distinct_pool)

    p_distinct = _weights_for_keys(distinct_pool, base_weight)
    repeats = _choice(rng, distinct_pool, remaining, replace=True, p=p_distinct)
    return _concat_and_shuffle(rng, distinct_pool, repeats)
