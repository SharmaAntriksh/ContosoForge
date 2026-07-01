"""Customer sampling: eligibility, participation targets, and discovery."""

from __future__ import annotations

import warnings
from typing import Optional

import numpy as np

from .allocation import _stable_seed


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
# Legacy per-chunk sampling entry point
# ----------------------------------------------------------------
# DEAD: the live path no longer samples customers per chunk. The
# per-month distinct-customer pool is built once (build_month_customer_pool) and
# each chunk maps its global order band to customers (assign_orders_to_customers),
# which is what makes the per-month distinct curve chunk/worker invariant. This
# function (and _concat_and_shuffle) is retained only for its unit tests and is
# slated for removal during the State/architecture cleanup — do NOT re-wire it
# into the chunk builder, as its chunk-local sampling is exactly the chunk-size
# dependence removed when the per-month plan became global.

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


# ================================================================
# Global per-month plan ("plan globally, shard the index space")
# ================================================================
# The chunk-dependent bug (review Finding #4/#14): the per-month distinct-customer
# target was evaluated against *per-chunk* rows and repeats were drawn from a
# *chunk-local* pool, so splitting the same rows into more chunks redistributed
# which customers transact in which month — ``base_distinct_ratio`` silently
# depended on ``chunk_size``. The fix computes the per-month distinct target and
# the actual distinct-customer *pool* ONCE from the run seed against GLOBAL month
# totals, then treats each month's orders as a contiguous global index space that
# chunks slice. Because a given global order index always maps to the same
# customer (``assign_orders_to_customers``), the union of distinct customers per
# month is exactly the month pool regardless of how the orders are chunked.


def compute_month_distinct_targets(
    *,
    seed: int,
    T: int,
    eligible_counts: np.ndarray,
    orders_per_month: np.ndarray,
    month_cal_index: np.ndarray,
    distinct_ratio: float,
    cycle_amplitude: float,
    participation_noise: float,
    seasonal_spike_map: dict,
    max_distinct_ratio: float,
    min_distinct_customers: int,
) -> np.ndarray:
    """Global per-month distinct-customer target ``D[m]`` (Finding #4/#14/#17).

    Single source of truth for "how many distinct customers transact in month m",
    evaluated against the GLOBAL month totals (``eligible_counts``,
    ``orders_per_month``) and a seed-derived RNG — so it is independent of
    ``chunk_size`` / worker count. This is the single source of truth for the
    per-month distinct target (it replaced the per-chunk inline target in
    chunk_builder and the removed ``_participation_distinct_target`` duplicate).
    Returns an int64 array of length ``T`` with ``D[m] <= orders_per_month[m]``.

    ``distinct_ratio <= 0`` means "no participation throttle" → maximum diversity
    (``D[m] = min(orders, eligible)``), matching the pre-Phase-2 organic behavior
    where a non-positive ratio meant *no distinct target*. It must NOT collapse to
    zero, which would leave every month with an empty pool and silently drop all
    rows (the chunk skips months whose pool is empty).
    """
    T = int(T)
    out = np.zeros(T, dtype=np.int64)
    if T <= 0:
        return out

    ec = np.asarray(eligible_counts, dtype=np.int64)
    om = np.asarray(orders_per_month, dtype=np.int64)
    cal = np.asarray(month_cal_index, dtype=np.int64)
    ratio = float(distinct_ratio)
    # One RNG for the whole plan; drawing T normals in order keeps D[m]
    # deterministic and chunk/worker-invariant (never the per-chunk rng).
    rng = np.random.default_rng(_stable_seed(seed, "distinct_target", T))

    max_ratio = float(max_distinct_ratio)
    min_k = float(min_distinct_customers)
    for m in range(T):
        e = int(ec[m])
        o = int(om[m])
        if e <= 0 or o <= 0:
            continue
        if ratio <= 0.0:
            # No throttle → every order can be a fresh customer (organic).
            out[m] = max(1, min(o, e))
            continue
        k = ratio * e
        if float(cycle_amplitude) > 0.0:
            k *= 1.0 + float(cycle_amplitude) * float(np.sin(2.0 * np.pi * m / 24.0))
        if seasonal_spike_map:
            boost = seasonal_spike_map.get(int(cal[m]))
            if boost:
                k *= 1.0 + float(boost)
        if float(participation_noise) > 0.0:
            k *= 1.0 + float(participation_noise) * float(rng.standard_normal())
        k = max(k, min_k)
        k = min(k, e * max_ratio, float(e), float(o))
        out[m] = max(1, int(round(k)))
    return out


def build_month_customer_pool(
    *,
    m_offset: int,
    distinct_target: int,
    eligible_idx: np.ndarray,
    customer_keys: np.ndarray,
    discovery_month,
    base_weight,
    end_month_norm,
    seed: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic distinct-customer pool ``P`` and weighted CDF for month ``m``.

    ``P`` is the exact set of distinct customers that transact in month ``m``
    (``len(P) <= distinct_target``). Built once per ``(month, run)`` from a
    month-seeded RNG — never the chunk RNG — so it is identical for every chunk
    and every worker. Semantics mirror the old discovery/participation path:

    - force-include the discovery *debut* cohort (``discovery_month == m``),
      nearest-expiry first when it must be truncated to fit ``distinct_target``;
    - fill the remaining distinct slots from previously-introduced customers
      (``discovery_month < m``), weighted by ``base_weight``;
    - if nobody has been introduced yet this month, fall back to an organic draw
      from all eligible customers so planned orders are never lost.

    The returned CDF (over ``P``, weighted by ``base_weight``) drives repeat-order
    customer selection in :func:`assign_orders_to_customers`.
    """
    m = int(m_offset)
    D = int(distinct_target)
    ei = np.asarray(eligible_idx)
    ck = np.asarray(customer_keys)
    empty = (np.empty(0, dtype=ck.dtype), np.empty(0, dtype=np.float64))
    if D <= 0 or ei.size == 0:
        return empty

    rng = np.random.default_rng(_stable_seed(seed, "month_pool", m))
    elig_keys = ck[ei]

    if discovery_month is not None:
        disc = np.asarray(discovery_month)[ei]
        introduced = disc <= m
        debut = disc == m
    else:
        introduced = np.ones(ei.size, dtype=bool)
        debut = np.zeros(ei.size, dtype=bool)
    prior = introduced & ~debut

    debut_keys = elig_keys[debut]
    debut_idx = ei[debut]
    prior_keys = elig_keys[prior]
    prior_idx = ei[prior]

    # Force the debut cohort in, capped at the distinct target (nearest-expiry
    # first so churning customers aren't dropped when the cohort is truncated).
    if debut_keys.size > D:
        pool = _urgency_pick(rng, debut_keys, debut_idx, end_month_norm, m, D)
    else:
        pool = debut_keys

    need = D - int(pool.size)
    if need > 0 and prior_keys.size > 0:
        p_prior = _weights_for_indices(prior_idx, base_weight)
        take = min(need, int(prior_keys.size))
        extra = _choice(rng, prior_keys, take, replace=False, p=p_prior)
        pool = extra if pool.size == 0 else np.concatenate([pool, extra])

    if pool.size == 0:
        # Nobody introduced yet this month → organic draw so orders aren't lost.
        take = min(D, int(elig_keys.size))
        p_elig = _weights_for_indices(ei, base_weight)
        pool = _choice(rng, elig_keys, take, replace=False, p=p_elig)

    if pool.size == 0:
        return empty

    pool = np.asarray(pool, dtype=ck.dtype)
    p_pool = _weights_for_keys(pool, base_weight)
    if p_pool is None:
        p_pool = np.full(pool.size, 1.0 / pool.size, dtype=np.float64)
    cdf = np.cumsum(p_pool, dtype=np.float64)
    cdf[-1] = 1.0  # CLAUDE.md gotcha #16: clamp so searchsorted stays in bounds
    return pool, cdf


def _hash_uniform_positions(m_offset: int, positions: np.ndarray, seed: int) -> np.ndarray:
    """Deterministic uniforms in ``[0, 1)`` keyed by ``(m_offset, position, seed)``.

    Used to pick repeat-order customers by *global order index* so a given index
    always maps to the same customer regardless of how the month is chunked.
    Vectorized splitmix64-style mix (same family as :func:`_hash_uniform`).
    """
    pos = np.asarray(positions).astype(np.uint64)
    s_val = (int(seed) * 0x2545F4914F6CDD1D + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    m_val = ((int(m_offset) + 1) * 0xD1B54A32D192ED03) & 0xFFFFFFFFFFFFFFFF
    s = np.uint64(s_val ^ m_val)
    with np.errstate(over="ignore"):
        z = (pos * np.uint64(0x9E3779B97F4A7C15)) ^ s
        z ^= (z >> np.uint64(30))
        z = z * np.uint64(0xBF58476D1CE4E5B9)
        z ^= (z >> np.uint64(27))
        z = z * np.uint64(0x94D049BB133111EB)
        z ^= (z >> np.uint64(31))
    return (z >> np.uint64(11)).astype(np.float64) * (1.0 / float(1 << 53))


def assign_orders_to_customers(
    *,
    m_offset: int,
    order_start: int,
    n_orders: int,
    pool: np.ndarray,
    cdf: np.ndarray,
    seed: int,
) -> np.ndarray:
    """Map a contiguous band of global order indices to CustomerKeys.

    Global order index ``j`` (in ``[0, orders_this_month)``) maps to:

    - ``pool[j]`` when ``j < len(pool)`` — each distinct customer appears once, at
      the front of the month's order space;
    - ``pool[searchsorted(cdf, hash(m, j))]`` otherwise — a weighted repeat.

    Because every chunk that owns index ``j`` computes the same customer, the
    union of distinct customers across all chunks for month ``m`` is exactly
    ``pool`` — independent of how the month's orders are split into chunks
    (the whole point of the global per-month plan). Returns a CustomerKey array of length
    ``n_orders``.
    """
    n = int(n_orders)
    if n <= 0 or pool.size == 0:
        return np.empty(0, dtype=(pool.dtype if pool.size else np.int32))
    P = int(pool.size)
    gj = np.arange(int(order_start), int(order_start) + n, dtype=np.int64)
    out = np.empty(n, dtype=pool.dtype)

    distinct_mask = gj < P
    if distinct_mask.any():
        out[distinct_mask] = pool[gj[distinct_mask]]
    rep_mask = ~distinct_mask
    if rep_mask.any():
        u = _hash_uniform_positions(m_offset, gj[rep_mask], seed)
        idx = np.searchsorted(cdf, u, side="right")
        np.clip(idx, 0, P - 1, out=idx)
        out[rep_mask] = pool[idx]
    return out
