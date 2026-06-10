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
# Seen-set masking
# ----------------------------------------------------------------

# Threshold for switching from lookup-table to sorted-intersection.
# For dense 1-based keys a boolean array is O(max_key) memory and O(n) time.
# Fall back to sorted intersection only when keys are extremely sparse.
_SPARSE_KEY_RATIO = 64


def _build_seen_mask(eligible_keys: np.ndarray, seen_set) -> np.ndarray:
    """
    Vectorized seen-membership test.

    Accepts either a Python set or a numpy boolean lookup array (see
    ``_make_seen_lookup`` / ``_update_seen_lookup``).

    Strategy:
      - If *seen_set* is a numpy array: direct O(n_eligible) indexed lookup.
      - If *seen_set* is a Python set (legacy): convert to lookup on the fly.
    """
    # Fast path: numpy boolean lookup array (new contract)
    if isinstance(seen_set, np.ndarray):
        if seen_set.size == 0:
            return np.zeros(eligible_keys.size, dtype=bool)
        # Eligible keys that exceed the lookup size are unseen by definition:
        # out-of-range keys default to False (unseen), which is correct
        # semantics since they have not been added to the seen_set yet.
        max_idx = seen_set.size - 1
        keys = np.asarray(eligible_keys, dtype=np.int32)
        in_range = keys <= max_idx
        out = np.zeros(keys.size, dtype=bool)
        out[in_range] = seen_set[keys[in_range]]
        return out

    # Legacy path: Python set
    if not seen_set or len(seen_set) == 0:
        return np.zeros(eligible_keys.size, dtype=bool)

    max_key = int(eligible_keys.max())
    n_keys = eligible_keys.size

    # Dense path: boolean lookup table
    if max_key < n_keys * _SPARSE_KEY_RATIO and max_key < 50_000_000:
        lookup = np.zeros(max_key + 1, dtype=bool)
        seen_arr = np.fromiter(seen_set, dtype=np.int32, count=len(seen_set))
        # Clip to valid range (keys outside the eligible range are irrelevant)
        valid = seen_arr[(seen_arr >= 0) & (seen_arr <= max_key)]
        lookup[valid] = True
        return lookup[eligible_keys]

    # Sparse path: sorted intersection via searchsorted (no Python loop)
    seen_sorted = np.sort(np.fromiter(seen_set, dtype=np.int32, count=len(seen_set)))
    pos = np.searchsorted(seen_sorted, eligible_keys)
    pos = np.clip(pos, 0, seen_sorted.size - 1)
    return seen_sorted[pos] == eligible_keys


def _make_seen_lookup(customer_keys: np.ndarray, existing_set=None) -> np.ndarray:
    """Create a boolean lookup array for seen-customer tracking.

    Much faster than a Python set for repeated vectorized membership tests.
    """
    max_key = int(np.asarray(customer_keys, dtype=np.int32).max())
    lookup = np.zeros(max_key + 1, dtype=bool)
    if existing_set:
        if isinstance(existing_set, np.ndarray):
            # Copy existing lookup
            copy_len = min(lookup.size, existing_set.size)
            lookup[:copy_len] = existing_set[:copy_len]
        elif isinstance(existing_set, set) and len(existing_set) > 0:
            seen_arr = np.fromiter(existing_set, dtype="int64", count=len(existing_set))
            valid = seen_arr[(seen_arr >= 0) & (seen_arr <= max_key)]
            lookup[valid] = True
    return lookup


def _update_seen_lookup(lookup: np.ndarray, new_keys: np.ndarray) -> None:
    """Mark keys as seen in the boolean lookup array. O(k) for k new keys."""
    keys = np.asarray(new_keys, dtype=np.int32)
    keys = keys[(keys >= 0) & (keys < lookup.size)]
    lookup[keys] = True


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
    seen_set,
    n: int,
    use_discovery: bool,
    discovery_cfg: dict,
    base_weight: np.ndarray | None = None,
    target_distinct: int | None = None,
    end_month_norm: np.ndarray | None = None,
    m_offset: int = 0,
    eligible_idx: np.ndarray | None = None,
) -> np.ndarray:
    """
    Returns array of CustomerKeys of length n, sampled from eligible customers.

    - If use_discovery: forces a slice of newly-eligible-but-unseen customers to appear.
    - If target_distinct is provided: builds a distinct pool then repeats from it.
    - If end_month_norm is provided: undiscovered customers closest to expiry are
      discovered first so they are not lost to churn.

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

    # Accept numpy boolean lookup arrays (fast) or Python sets (legacy)
    if not isinstance(seen_set, (set, np.ndarray)):
        seen_set = set(seen_set) if seen_set else set()

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
    # Discovery mode
    # -----------------------------

    _has_seen = (seen_set.any() if isinstance(seen_set, np.ndarray)
                  else bool(seen_set))
    if _has_seen:
        seen_mask = _build_seen_mask(eligible_keys, seen_set)
        seen_eligible = eligible_keys[seen_mask]
        seen_eligible_idx = eligible_idx[seen_mask]

        undiscovered = eligible_keys[~seen_mask]
        undiscovered_idx = eligible_idx[~seen_mask]
    else:
        seen_eligible = np.empty(0, dtype=eligible_keys.dtype)
        seen_eligible_idx = np.empty(0, dtype="int64")
        undiscovered = eligible_keys
        undiscovered_idx = eligible_idx

    forced = np.empty(0, dtype=eligible_keys.dtype)
    if undiscovered.size > 0:
        discover_n = int(discovery_cfg.get("_target_new_customers", 1))

        # Add variance around the target (seed-deterministic but not "flat").
        # Default True: restores the older jaggedness without requiring config edits.
        if bool(discovery_cfg.get("stochastic_discovery", True)) and discover_n > 0:
            discover_n = int(rng.poisson(lam=float(discover_n)))

        max_frac = discovery_cfg.get("max_fraction_per_month")
        if max_frac is not None:
            try:
                max_frac_f = float(max_frac)
            except (TypeError, ValueError):
                warnings.warn(
                    f"discovery.max_fraction_per_month={max_frac!r} is not numeric; "
                    f"ignoring cap. Fix config to apply the intended limit.",
                    stacklevel=2,
                )
                max_frac_f = None

            if max_frac_f is not None:
                max_new = int(max_frac_f * int(eligible_keys.size))
                discover_n = min(discover_n, max_new)

        discover_n = max(0, min(discover_n, int(undiscovered.size)))
        if discover_n > 0:
            forced = _urgency_pick(
                rng, undiscovered, undiscovered_idx,
                end_month_norm, m_offset, discover_n,
            )

    # ------------------------------------------------------------
    # Discovery without participation target
    # ------------------------------------------------------------
    if k is None:
        remaining = n - forced.size
        if remaining <= 0:
            return _concat_and_shuffle(rng, forced)

        # Repeat from ALL eligible this month (seen + undiscovered),
        # so undiscovered customers can appear organically beyond the
        # forced set.
        repeat_pool = eligible_keys
        p_repeat = p_eligible

        repeat = _choice(rng, repeat_pool, remaining, replace=True, p=p_repeat)
        return _concat_and_shuffle(rng, forced, repeat)

    # ------------------------------------------------------------
    # Participation-controlled discovery
    # ------------------------------------------------------------
    if forced.size > k:
        # _urgency_pick returns in urgency order (nearest-expiry first),
        # so taking the first k preserves the most urgent customers.
        forced = forced[:k]

    distinct_pool = forced
    need = k - distinct_pool.size

    if need > 0:
        other = undiscovered
        other_idx = undiscovered_idx
        if distinct_pool.size > 0 and other.size > 0:
            keep = ~np.isin(other, distinct_pool)
            other = other[keep]
            other_idx = other_idx[keep]

        # Take as many undiscovered as we can, nearest-expiry first
        if other.size > 0:
            take_new = min(need, int(other.size))
            new_extra = _urgency_pick(
                rng, other, other_idx,
                end_month_norm, m_offset, take_new,
            )
            distinct_pool = np.concatenate([distinct_pool, new_extra])
            need = k - distinct_pool.size

        # Fill remaining slots with seen eligible
        if need > 0 and seen_eligible.size > 0:
            take_seen = min(need, int(seen_eligible.size))
            seen_extra = rng.choice(seen_eligible, size=take_seen, replace=False)
            distinct_pool = np.concatenate([distinct_pool, seen_extra])

    if distinct_pool.size == 0:
        return _choice(rng, eligible_keys, n, replace=True, p=p_eligible)

    remaining = n - distinct_pool.size
    if remaining <= 0:
        return _concat_and_shuffle(rng, distinct_pool)

    p_distinct = _weights_for_keys(distinct_pool, base_weight)
    repeats = _choice(rng, distinct_pool, remaining, replace=True, p=p_distinct)
    return _concat_and_shuffle(rng, distinct_pool, repeats)
