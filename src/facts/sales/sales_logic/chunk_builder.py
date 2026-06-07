"""Sales chunk builder (Arrow table).

This is the ONLY place that materializes the Sales row-level table.

Adding columns:
- Add schema field(s) in src/utils/static_schemas.get_sales_schema(...)
- Implement the values in sales_logic/columns.py (single extension point)
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

from src.defaults import PHYSICAL_CHANNELS, DIGITAL_CHANNELS
from src.exceptions import SalesError
from src.utils.config_helpers import int_or as _int_or, float_or as _float_or
from .globals import PA_AVAILABLE, State
from .core import (
    _eligible_customer_mask_for_month,
    _make_seen_lookup,
    _normalize_cdf,
    _normalize_end_month,
    _sample_customers,
    _update_seen_lookup,
    apply_promotions,
    build_orders,
    build_rows_per_month,
    compute_dates,
    compute_prices,
)
from .columns import build_extra_columns, SALES_CHANNEL_CORE_KEYS

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

_EPOCH_D = np.datetime64("1970-01-01", "D")

def _as_datetime64_D(x):
    """Normalize array to datetime64[D]. Handles datetime64, integer, and fallback."""
    x = np.asarray(x)

    # Already a datetime64 -> normalize to day resolution
    if np.issubdtype(x.dtype, np.datetime64):
        return x.astype("datetime64[D]", copy=False)

    # Integer -> interpret as epoch-based; handle both day-scale and ns-scale
    if np.issubdtype(x.dtype, np.integer):
        if x.size == 0:
            return x.astype("datetime64[D]")
        mx = int(np.max(np.abs(x)))
        # days for 2020s are ~ 18k–22k; ns timestamps are ~ 1e18
        if mx > 10_000_000:  # too large to be "days"
            return x.astype("datetime64[ns]").astype("datetime64[D]")
        return (_EPOCH_D + x.astype("timedelta64[D]")).astype("datetime64[D]")

    # Fallback
    return np.asarray(x, dtype="datetime64[D]")

def _clamp_order_dates_to_customer_start(
    order_dates: np.ndarray,
    customer_keys: np.ndarray,
) -> np.ndarray:
    """Push OrderDate forward to the customer's first EffectiveStartDate.

    Eligibility is month-granular (CustomerStartMonth) but EffectiveStartDate
    is day-granular (random 0-27 day offset within the start month), so a
    customer who joined Dec 28 can otherwise be sampled for a Dec 20 order.
    The lookup table fills unknown-key slots with INT64_MIN so np.maximum is
    a no-op for those rows.
    """
    lut = getattr(State, "customer_first_eff_start_by_key", None)
    if lut is None or order_dates is None or customer_keys.size == 0:
        return order_dates
    od_days = np.asarray(order_dates, dtype="datetime64[D]").astype(np.int64)
    np.maximum(od_days, lut[customer_keys], out=od_days)
    return od_days.view("datetime64[D]")


def _within_day_cursor(d_off: np.ndarray) -> np.ndarray:
    """0-based rank of each order within its OrderDate (day-offset) group.

    Robust to unsorted input. ``build_orders`` returns orders sorted by date,
    but ``_clamp_order_dates_to_customer_start`` can push individual orders onto
    a later day *after* that sort, breaking the contiguity a naive
    ``arange - first_index`` cursor relies on. That naive cursor would then
    over-count within a day and spill the resulting SalesOrderNumber past the
    per-chunk allocation band into the next chunk's band → duplicate order
    numbers across chunks (CHUNK-1). Computing the rank from a stable sort by
    day yields the true 0-based within-day position regardless of ordering.
    """
    n = d_off.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.int64)
    order = np.argsort(d_off, kind="stable")
    sorted_days = d_off[order]
    unique_days, first_idx = np.unique(sorted_days, return_index=True)
    group_start = first_idx[np.searchsorted(unique_days, sorted_days)]
    cursor_sorted = np.arange(n, dtype=np.int64) - group_start
    cursor = np.empty(n, dtype=np.int64)
    cursor[order] = cursor_sorted
    return cursor


# Cached partition cols (read once from the per-worker State; avoids per-chunk getattr).
_cached_delta_pcols: set[str] | None = None


def _delta_partition_cols() -> set[str]:
    global _cached_delta_pcols
    if _cached_delta_pcols is None:
        raw = getattr(State, "partition_cols", None)
        _cached_delta_pcols = set(raw) if isinstance(raw, (list, tuple)) and raw else {"Year", "Month"}
    return _cached_delta_pcols


# Module-level cache for brand flat index + offsets (rebuilt once per worker).
_brand_flat_cache_ref: list | None = None
_brand_flat_cache_data: tuple | None = None  # (flat_idx, offsets)

# Worker-lifetime CDF cache for weighted product sampling.
# Keyed by (pool_id, brand_or_merged, cal_month).  Contents are deterministic
# (intersections + CDFs depend only on store assortment, brand buckets,
# and product weights — all identical across chunks in the same worker).
_worker_cdf_cache: dict = {}


def reset_worker_cdf_cache() -> None:
    """Clear the worker-lifetime CDF cache (called once per worker init)."""
    _worker_cdf_cache.clear()


def _get_brand_flat_cache(brand_to_rows: list, B: int) -> tuple:
    """Return (flat_idx, offsets) for brand_to_rows, caching across calls."""
    global _brand_flat_cache_ref, _brand_flat_cache_data
    if _brand_flat_cache_ref is brand_to_rows and _brand_flat_cache_data is not None:
        return _brand_flat_cache_data

    flat_parts = []
    lengths = np.zeros(B, dtype=np.int64)
    for b, bucket in enumerate(brand_to_rows):
        if bucket is not None and len(bucket) > 0:
            flat_parts.append(np.asarray(bucket, dtype=np.int32))
            lengths[b] = len(bucket)
    offsets = np.zeros(B + 1, dtype="int64")
    np.cumsum(lengths, out=offsets[1:])
    flat_idx = np.concatenate(flat_parts) if flat_parts else np.empty(0, dtype=np.int32)
    _brand_flat_cache_ref = brand_to_rows
    _brand_flat_cache_data = (flat_idx, offsets)
    return flat_idx, offsets


def _normalize_prob(p: np.ndarray) -> np.ndarray | None:
    p = np.asarray(p, dtype="float64")
    p = np.where(np.isfinite(p) & (p > 0), p, 0.0)
    s = float(p.sum())
    if s <= 1e-12:
        return None
    return p / s


def _get_brand_probs(m_offset: int, B: int) -> np.ndarray | None:
    """Return normalized brand probability vector for month *m_offset*, or None."""
    bp = _get_state_attr("brand_prob_by_month", default=None)
    if bp is not None:
        bp = np.asarray(bp, dtype="float64")
        cand = bp[int(m_offset) % bp.shape[0]] if bp.ndim > 1 else bp
        probs = _normalize_prob(cand)
        if probs is not None and int(probs.size) == B:
            return probs
    return None


def _sample_product_row_indices(
    rng: np.random.Generator,
    n: int,
    product_np: np.ndarray,
    *,
    m_offset: int,
    enabled: bool,
    product_weight: np.ndarray | None = None,
) -> np.ndarray:
    """
    Return row indices into `product_np` for n orders.

    Brand-first sampling is used iff:
      - enabled == True (controlled by models_cfg.brand_popularity presence), AND
      - State provides brand buckets and optionally per-month brand probabilities.

    When product_weight is provided, within-brand selection is weighted by
    popularity+seasonality instead of uniform.  If brand sampling is disabled,
    product_weight is used directly for weighted global sampling.

    Safe fallback:
      - If anything is missing/invalid -> uniform sampling over product_np (old behavior).
    """
    n_int = int(n)
    n_products = len(product_np)

    if not enabled:
        if product_weight is not None:
            ws = float(product_weight.sum())
            if ws > 1e-12:
                return rng.choice(n_products, size=n_int, p=product_weight / ws).astype("int32", copy=False)
        return rng.integers(0, n_products, size=n_int).astype("int32", copy=False)

    # Prefer "active" buckets if we're using active_product_np upstream.
    if getattr(State, "active_product_np", None) is not None and product_np is State.active_product_np:
        brand_to_rows = getattr(State, "active_brand_to_row_idx", None)
        if brand_to_rows is None:
            brand_to_rows = getattr(State, "brand_to_row_idx", None)
    else:
        brand_to_rows = getattr(State, "brand_to_row_idx", None)

    if brand_to_rows is not None:
        mx = -1
        for b in brand_to_rows:
            if b is not None and len(b) > 0:
                mx = max(mx, int(np.max(b)))
        if mx >= n_products:
            import logging
            logging.getLogger(__name__).warning(
                "brand_to_row_idx max index (%d) >= n_products (%d); "
                "falling back to uniform product sampling", mx, n_products,
            )
            brand_to_rows = None  # force uniform fallback

    if brand_to_rows is None or len(brand_to_rows) == 0:
        return rng.integers(0, n_products, size=n_int).astype("int32", copy=False)

    B = int(len(brand_to_rows))
    p = _get_brand_probs(m_offset, B)

    # Fallback: equal probability across brands with at least 1 SKU
    if p is None:
        avail = np.asarray([len(x) > 0 for x in brand_to_rows], dtype="float64")
        if float(avail.sum()) <= 0:
            return rng.integers(0, n_products, size=n_int).astype("int32", copy=False)
        p = avail / float(avail.sum())

    brand_ids = rng.choice(B, size=n_int, p=p)

    # Build flat index array + offsets once and cache at module level (deterministic
    # per worker).  Avoids rebuilding from brand_to_rows (up to 300K brands) per call.
    flat_idx, offsets = _get_brand_flat_cache(brand_to_rows, B)

    if offsets[-1] == 0:
        return rng.integers(0, n_products, size=n_int).astype("int32", copy=False)

    # Vectorized sampling: for each order, pick a random offset within its brand's bucket
    bucket_sizes = offsets[1:] - offsets[:-1]
    sizes_per_order = bucket_sizes[brand_ids]

    # Handle empty buckets (shouldn't happen given avail filter, but be safe)
    empty_mask = sizes_per_order == 0
    sizes_per_order[empty_mask] = 1  # temporary to avoid division by zero

    # Within-brand weighted sampling when product_weight is available
    if product_weight is not None:
        out = np.empty(n_int, dtype=np.int32)
        # Group orders by brand via argsort (avoids O(B×n) per-brand mask scans)
        _brand_order = np.argsort(brand_ids, kind="stable")
        _brand_counts = np.bincount(brand_ids, minlength=B)
        _brand_starts = np.zeros(B + 1, dtype=np.int64)
        np.cumsum(_brand_counts, out=_brand_starts[1:])

        for b in range(B):
            s, e = int(_brand_starts[b]), int(_brand_starts[b + 1])
            cnt = e - s
            if cnt == 0:
                continue
            orig_idx = _brand_order[s:e]
            start, end = int(offsets[b]), int(offsets[b + 1])
            if start == end:
                out[orig_idx] = rng.integers(0, n_products, size=cnt).astype("int32", copy=False)
                continue
            rows = flat_idx[start:end]
            w = product_weight[rows]
            ws = float(w.sum())
            if ws > 1e-12:
                picks = rng.choice(len(rows), size=cnt, p=w / ws)
            else:
                picks = rng.integers(0, len(rows), size=cnt)
            out[orig_idx] = rows[picks]
        return out

    rand_within = rng.integers(0, np.iinfo(np.int64).max, size=n_int, dtype="int64")
    rand_within = np.abs(rand_within) % sizes_per_order

    abs_idx = offsets[brand_ids] + rand_within
    out = flat_idx[abs_idx]

    # Fix up any rows that landed on empty buckets with uniform fallback
    if empty_mask.any():
        n_empty = int(empty_mask.sum())
        out[empty_mask] = rng.integers(0, n_products, size=n_empty).astype("int32", copy=False)

    return out


def _sample_products_per_store(
    rng: np.random.Generator,
    store_key_arr: np.ndarray,
    store_to_product_rows: list,
    product_np: np.ndarray,
    *,
    product_weight: np.ndarray | None = None,
    _cdf_cache: dict | None = None,
    _cal_month: int = 0,
    m_offset: int = 0,
    use_brand_popularity: bool = False,
) -> np.ndarray:
    """
    Sample product row indices from each store's assortment pool.

    When brand popularity is enabled, orders first pick a brand via
    ``brand_prob_by_month``, then sample within the intersection of
    store pool and brand bucket.  Otherwise sampling is weighted by
    ``product_weight`` (popularity) within each store's full pool.

    Performance: per-(store, brand) intersection arrays and CDFs are
    cached in ``_cdf_cache`` keyed by ``(pool_key, brand, cal_month)``.
    """
    n = len(store_key_arr)
    n_products = len(product_np)
    max_sk = len(store_to_product_rows)
    out = np.empty(n, dtype=np.int32)

    # Brand popularity state
    brand_to_rows = None
    brand_probs = None
    B = 0
    if use_brand_popularity:
        brand_to_rows = _get_state_attr("brand_to_row_idx", default=None)
        if brand_to_rows is not None and len(brand_to_rows) > 0:
            B = len(brand_to_rows)
            brand_probs = _get_brand_probs(m_offset, B)
        if brand_probs is None:
            brand_to_rows = None  # disable brand path

    # Pre-compute normalized global weights once (used by fallback path)
    _global_p = None
    if product_weight is not None:
        ws = float(product_weight.sum())
        if ws > 1e-12:
            _global_p = product_weight / ws

    # Group lines by store via argsort (avoids O(stores×n) per-store mask scans)
    unique_stores, inverse = np.unique(store_key_arr, return_inverse=True)
    _store_order = np.argsort(inverse, kind="stable")
    _store_counts = np.bincount(inverse, minlength=len(unique_stores))
    _store_starts = np.zeros(len(unique_stores) + 1, dtype=np.int64)
    np.cumsum(_store_counts, out=_store_starts[1:])

    n_stores = len(unique_stores)
    _sk_list = unique_stores.tolist()
    _pools = [
        pool if pool is not None and pool.size > 0 else None
        for pool in (
            store_to_product_rows[sk] if sk < max_sk and store_to_product_rows[sk] is not None else None
            for sk in _sk_list
        )
    ]
    _has_brand = brand_to_rows is not None
    _has_weight = product_weight is not None
    _cache_get = _cdf_cache.get if _cdf_cache is not None else None

    for i in range(n_stores):
        s = int(_store_starts[i])
        e = int(_store_starts[i + 1])
        count = e - s
        orig_idx = _store_order[s:e]
        pool = _pools[i]

        if pool is not None:
            pool_sz = pool.size

            if _has_brand:
                _sample_brand_aware(
                    rng, out, orig_idx, count, pool,
                    brand_probs, B, product_weight, _cdf_cache, _cal_month,
                )
            elif _has_weight and pool_sz > 1:
                cache_key = (id(pool), -1, _cal_month)
                cached = _cache_get(cache_key) if _cache_get is not None else None
                if cached is not None:
                    _, cdf = cached
                else:
                    w = product_weight[pool]
                    cdf = _normalize_cdf(w)
                    if _cache_get is not None:
                        _cdf_cache[cache_key] = (pool, cdf)

                total = float(cdf[-1]) if cdf.size > 0 else 0.0
                if total > 1e-12:
                    u = rng.random(count)
                    picks = np.searchsorted(cdf, u, side="right")
                    np.minimum(picks, pool_sz - 1, out=picks)
                    out[orig_idx] = pool[picks]
                else:
                    out[orig_idx] = pool[rng.integers(0, pool_sz, size=count)]
            else:
                out[orig_idx] = pool[rng.integers(0, pool_sz, size=count)]
        else:
            if _global_p is not None:
                out[orig_idx] = rng.choice(n_products, size=count, p=_global_p)
            else:
                out[orig_idx] = rng.integers(0, n_products, size=count)

    return out


def _sample_brand_aware(
    rng, out, orig_idx, count, pool, brand_probs, B,
    product_weight, _cdf_cache, _cal_month,
):
    """Sample products using merged brand × popularity CDF (no brand loop).

    Builds a single per-store CDF where each product's weight incorporates
    its brand probability: w(p) = brand_prob[brand_of_p] * product_weight[p].
    One vectorized RNG + searchsorted call per store instead of looping over
    ~200 brands.  Produces the same marginal distribution as the two-stage
    "pick brand, then pick within brand" approach.
    """
    pool_set_key = id(pool)
    merged_key = (pool_set_key, "_merged_", _cal_month)

    if _cdf_cache is not None and merged_key in _cdf_cache:
        merged_pool, merged_cdf = _cdf_cache[merged_key]
    else:
        # Build combined weight: brand_prob[brand] * product_weight[product]
        product_brand_key = _get_state_attr("product_brand_key", default=None)
        w = np.ones(len(pool), dtype=np.float64)
        if product_weight is not None:
            w *= product_weight[pool]
        if product_brand_key is not None:
            pool_brands = product_brand_key[pool].astype(np.intp)
            valid = (pool_brands >= 0) & (pool_brands < B)
            w[valid] *= brand_probs[pool_brands[valid]]
            w[~valid] *= 1e-6
        merged_cdf = _normalize_cdf(w)
        merged_pool = pool
        if _cdf_cache is not None:
            _cdf_cache[merged_key] = (merged_pool, merged_cdf)

    if merged_cdf is not None and merged_cdf.size > 0 and float(merged_cdf[-1]) > 1e-12:
        u = rng.random(count)
        picks = np.searchsorted(merged_cdf, u, side="right")
        np.minimum(picks, len(merged_pool) - 1, out=picks)
        out[orig_idx] = merged_pool[picks]
    else:
        out[orig_idx] = pool[rng.integers(0, len(pool), size=count)]


def _get_state_attr(*names, default=None):
    """Return the first non-None attribute from State among names."""
    for n in names:
        if hasattr(State, n):
            v = getattr(State, n)
            if v is not None:
                return v
    return default


# Seasonality boost: which calendar months each profile peaks in
_SEASON_SALES_BOOST: dict[str, dict[int, float]] = {
    "Holiday":      {11: 0.8, 12: 1.0, 1: 0.3, 10: 0.4},
    "Winter":       {11: 0.5, 12: 0.5, 1: 0.5, 2: 0.4},
    "Summer":       {6: 0.5, 7: 0.5, 8: 0.4, 5: 0.3},
    "BackToSchool": {7: 0.4, 8: 0.7, 9: 0.4},
    "Spring":       {3: 0.4, 4: 0.5, 5: 0.4},
}
# Numeric encoding matching sales.py _SEASON_ENCODE
_SEASON_CODE = {"Holiday": 1, "Winter": 2, "Summer": 3, "BackToSchool": 4, "Spring": 5}


def _build_product_weight_for_month(
    month_date_pool: np.ndarray,
    m_offset: int,
    cal_month: int = 0,
) -> np.ndarray | None:
    """
    Build a per-product-row weight array combining PopularityScore and
    SeasonalityProfile for the current calendar month.

    Returns None if no profile data is available (uniform sampling fallback).
    """
    pop = getattr(State, "product_popularity", None)
    sea = getattr(State, "product_seasonality", None)

    if pop is None:
        return None

    # Base weight: popularity (clamp to avoid zeros)
    # np.maximum already returns a new array — no .copy() needed
    w = np.maximum(pop, 1.0)

    # Seasonal boost for this calendar month
    if sea is not None and cal_month > 0:
        for season, boosts in _SEASON_SALES_BOOST.items():
            if cal_month in boosts:
                code = _SEASON_CODE[season]
                mask = sea == code
                if mask.any():
                    w[mask] *= 1.0 + boosts[cal_month]

    return w


def _build_month_slices(date_pool: np.ndarray):
    """
    Build a list mapping month_offset -> slice/indices in date_pool belonging to that month.
    month_offset is 0..T-1 where 0 is min month in date_pool (datetime64[M] int).

    Optimized fast-path for sorted date_pool:
      - returns `slice(start, end)` per month (view slicing; low alloc)
    Fallback for unsorted pools:
      - returns index arrays per month
    """
    if not isinstance(date_pool, np.ndarray):
        date_pool = np.asarray(date_pool)

    if date_pool.size == 0:
        return []

    months_int = date_pool.astype("datetime64[M]").astype("int64")
    min_m = int(months_int.min())
    max_m = int(months_int.max())
    T = int(max_m - min_m + 1)

    # Fast-path: non-decreasing months (typical for date_range-derived pools)
    if months_int.size <= 1 or np.all(months_int[:-1] <= months_int[1:]):
        cuts = np.flatnonzero(months_int[1:] != months_int[:-1]) + 1
        starts = np.concatenate(([0], cuts))
        ends = np.concatenate((cuts, [months_int.size]))

        out = [slice(0, 0)] * T
        for s, e in zip(starts, ends):
            mo = int(months_int[int(s)] - min_m)
            out[mo] = slice(int(s), int(e))
        return out

    # Fallback: non-sorted pools
    out = [slice(0, 0)] * T
    for m_int in range(min_m, max_m + 1):
        idx = np.nonzero(months_int == m_int)[0]
        out[int(m_int - min_m)] = idx
    return out


def _eligible_counts_fast(
    T: int,
    is_active_in_sales: np.ndarray,
    start_month: np.ndarray,
    end_month_norm: np.ndarray,
) -> np.ndarray:
    """
    Compute eligible customer counts per month in O(N + T) using a delta/cumsum approach.

    Eligibility rule matches _eligible_customer_mask_for_month:
      active == 1 AND start_month <= m AND (end_month == -1 OR end_month >= m)
    """
    if T <= 0:
        return np.zeros(0, dtype="float64")

    is_active_in_sales = np.asarray(is_active_in_sales, dtype=np.int32, order="C")
    start_month = np.asarray(start_month, dtype="int64", order="C")
    end_month_norm = np.asarray(end_month_norm, dtype="int64", order="C")

    if start_month.size == 0:
        return np.zeros(T, dtype="float64")

    active = is_active_in_sales == 1
    if not active.any():
        return np.zeros(T, dtype="float64")

    s = start_month[active]
    e = end_month_norm[active]

    # keep only sane starts
    valid_start = (s >= 0) & (s < T)
    s = s[valid_start]
    e = e[valid_start]
    if s.size == 0:
        return np.zeros(T, dtype="float64")

    # discard invalid ranges where end < start (and end != -1)
    valid_range = (e < 0) | (e >= s)
    s = s[valid_range]
    e = e[valid_range]
    if s.size == 0:
        return np.zeros(T, dtype="float64")

    delta = np.zeros(T + 1, dtype="int64")
    np.add.at(delta, s, 1)

    finite = e >= 0
    if finite.any():
        endp1 = e[finite] + 1
        endp1 = endp1[(endp1 > 0) & (endp1 < T)]
        if endp1.size:
            np.add.at(delta, endp1, -1)

    counts = np.cumsum(delta[:-1])
    counts = np.maximum(counts, 0)
    return counts.astype("int64", copy=False)


def _to_pa_array(name: str, data: object, n_rows: int, schema_types: dict) -> pa.array:
    """Convert a column's numpy/scalar data to a typed pa.Array."""
    t = schema_types[name]

    if data is None:
        return pa.nulls(n_rows, type=t)

    # Broadcast scalars
    if np.isscalar(data):
        data = np.full(n_rows, data)

    # date32: build as timestamp first (inference), then cast to date32
    if pa.types.is_date32(t):
        dt = _as_datetime64_D(data)
        arr = pa.array(np.asarray(dt).astype("datetime64[ns]"))
        if arr.type != t:
            arr = arr.cast(t, safe=False)
        return arr

    return pa.array(data, type=t, safe=False)


def _assemble_column(name, bufs, month_row_counts, total_rows, schema_types) -> pa.array:
    """Concatenate one column's per-month buffers into a single pa.Array.

    *bufs* holds one entry per appended month: a numpy array, or ``None`` for a
    month where the column produced no data. *month_row_counts* is aligned 1:1
    with *bufs*. Null months are padded with typed nulls so the column stays the
    full ``total_rows`` length and positionally aligned with the other columns
    (CHUNK-3 — the old mixed path skipped null months, yielding a short, misaligned
    column that crashed ``pa.Table.from_arrays``).
    """
    t = schema_types[name]
    has_data = any(b is not None for b in bufs)
    if not has_data:
        return pa.nulls(total_rows, type=t)
    if all(b is not None for b in bufs):
        combined = np.concatenate(bufs) if len(bufs) > 1 else bufs[0]
        return _to_pa_array(name, combined, total_rows, schema_types)
    # Mixed: some months produced the column, others did not.
    month_arrays = [
        pa.nulls(int(n), type=t) if b is None
        else _to_pa_array(name, b, len(b), schema_types)
        for b, n in zip(bufs, month_row_counts)
    ]
    return pa.concat_arrays(month_arrays)


_FAR_PAST = np.datetime64("1900-01-01", "D")
_FAR_FUTURE = np.datetime64("2262-04-11", "D")


def _sample_salesperson_vectorized(
    store_ids: np.ndarray,
    dates_D: np.ndarray,
    eff: dict,
    rng: np.random.Generator,
) -> np.ndarray:
    """Sample one salesperson per (store, date) row using composite-key vectorization.

    Groups by store first, then vectorizes date-eligibility across all dates
    for that store using 2-D broadcasting, reducing Python loop iterations
    from O(stores × dates) to O(stores × eligibility_patterns).

    STRICT: never assigns a salesperson outside an effective-dated assignment window.
    If no eligible salesperson exists for a given (StoreKey, Date), emits -1.
    """
    n = store_ids.shape[0]
    out = np.full(n, -1, dtype=np.int32)

    if not isinstance(eff, dict) or not eff:
        return out

    # Composite key: encode (store, date) pairs
    day_i64 = dates_D.astype("datetime64[D]").astype("int64")
    day_min = int(day_i64.min())
    day_range = int(day_i64.max()) - day_min + 1

    sd_key = store_ids.astype(np.int64) * day_range + (day_i64 - day_min)
    sd_uniq, sd_inv = np.unique(sd_key, return_inverse=True)
    n_pairs = sd_uniq.size

    # Decode back to store/date
    pair_store = (sd_uniq // day_range).astype(np.int64)
    pair_day64 = sd_uniq % day_range + day_min

    # Sort by pair index for contiguous-slice access
    pair_counts = np.bincount(sd_inv, minlength=n_pairs).astype(np.int64)
    pair_order = np.argsort(sd_inv, kind="mergesort")
    pair_starts = np.zeros(n_pairs + 1, dtype=np.int64)
    np.cumsum(pair_counts, out=pair_starts[1:])

    # Group pairs by store for batch processing
    store_uniq_vals, store_inv = np.unique(pair_store, return_inverse=True)

    for si in range(store_uniq_vals.size):
        sk = int(store_uniq_vals[si])
        entry = eff.get(sk)
        if entry is None:
            continue

        emp_keys, start_d, end_d, weights = entry
        emp_keys = np.asarray(emp_keys, dtype=np.int32)
        start_d = np.asarray(start_d, dtype="datetime64[D]")
        end_d = np.asarray(end_d, dtype="datetime64[D]")
        weights = np.asarray(weights, dtype=np.float64)

        # Never allow Store Manager keys (30M..40M)
        non_mgr = (emp_keys < 30_000_000) | (emp_keys >= 40_000_000)
        emp_keys = emp_keys[non_mgr]
        start_d = start_d[non_mgr]
        end_d = end_d[non_mgr]
        weights = weights[non_mgr]

        if emp_keys.size == 0:
            continue

        if np.isnat(start_d).any():
            start_d = start_d.copy()
            start_d[np.isnat(start_d)] = _FAR_PAST
        if np.isnat(end_d).any():
            end_d = end_d.copy()
            end_d[np.isnat(end_d)] = _FAR_FUTURE

        # All pair indices belonging to this store
        pair_idxs = np.where(store_inv == si)[0]
        store_days = pair_day64[pair_idxs]  # int64 epoch days

        # 2-D eligibility: active[emp, date] — broadcast (k,1) vs (1,d)
        start_i64 = start_d.astype("int64")
        end_i64 = end_d.astype("int64")
        active = (start_i64[:, None] <= store_days[None, :]) & (store_days[None, :] <= end_i64[:, None])
        # active shape: (n_employees, n_dates_for_store)

        # Find unique eligibility patterns to avoid redundant sampling
        # Pack each column's active bool pattern into a hashable key
        # For small k (<64), pack into a single int; otherwise hash the bytes
        k = emp_keys.size
        d = store_days.size

        if k <= 63:
            # Pack each date's active pattern into a uint64 fingerprint
            powers = np.uint64(1) << np.arange(k, dtype=np.uint64)
            pattern_keys = active.astype(np.uint64).T @ powers  # shape (d,)

            # Group dates by pattern — batch-sample all rows per pattern
            pat_uniq, pat_inv = np.unique(pattern_keys, return_inverse=True)
            for pi in range(pat_uniq.size):
                p_mask = active[:, pat_inv == pi][:, 0]  # which employees are active
                if not p_mask.any():
                    continue
                emp2 = emp_keys[p_mask]
                w2 = weights[p_mask]
                sw = float(w2.sum())

                # Total rows across all dates with this eligibility pattern
                date_positions = np.where(pat_inv == pi)[0]
                gi_arr = pair_idxs[date_positions]
                total_count = int(pair_counts[gi_arr].sum())
                if total_count == 0:
                    continue

                # Single batch sample for all rows in this pattern
                if sw <= 1e-12:
                    all_picked = emp2[rng.integers(0, emp2.size, size=total_count)]
                else:
                    p = w2 / sw
                    all_picked = emp2[rng.choice(emp2.size, size=total_count, p=p)]

                _gi_s = pair_starts[gi_arr]
                _gi_e = pair_starts[gi_arr + 1]
                _slot_idx = np.concatenate([pair_order[s:e] for s, e in zip(_gi_s, _gi_e)])
                out[_slot_idx] = all_picked
        else:
            # Fallback for many employees: iterate per date (rare)
            for j in range(d):
                ok = active[:, j]
                if not ok.any():
                    continue
                emp2 = emp_keys[ok]
                w2 = weights[ok]
                gi = pair_idxs[j]
                count = int(pair_counts[gi])
                sw = float(w2.sum())
                if sw <= 1e-12:
                    picked = emp2[rng.integers(0, emp2.size, size=count)]
                else:
                    p = w2 / sw
                    picked = emp2[rng.choice(emp2.size, size=count, p=p)]
                s, e = int(pair_starts[gi]), int(pair_starts[gi + 1])
                out[pair_order[s:e]] = picked

    return out


def _empty_table(schema: pa.Schema) -> pa.Table:
    """Return a zero-row table with the exact schema types."""
    arrays = [pa.array([], type=f.type, safe=False) for f in schema]
    return pa.Table.from_arrays(arrays, schema=schema)


# ------------------------------------------------------------
# Correlation helpers
# ------------------------------------------------------------

def _apply_geo_bias_store_sampling(
    rng, skip_cols, n_unique_orders, n_lines,
    customer_keys_for_orders, customer_keys_out,
    month_stores, order_idx,
    cust_geo, geo2c, st2c, c2sk,
):
    """Sample stores with geographic bias toward customer's home country."""
    geo_bias = (cust_geo is not None and geo2c is not None
                and st2c is not None and c2sk is not None)

    if not skip_cols:
        _n_to_sample = n_unique_orders
        _cust_for_store = customer_keys_for_orders
    else:
        _n_to_sample = n_lines
        _cust_for_store = customer_keys_out

    if geo_bias:
        _ck_arr = np.clip(np.asarray(_cust_for_store, dtype=np.int32), 0, len(cust_geo) - 1)
        _cust_countries = geo2c[np.clip(cust_geo[_ck_arr], 0, len(geo2c) - 1)]
        _use_local = rng.random(_n_to_sample) < 0.70

        _month_set = set(month_stores.tolist())
        _unique_countries = np.unique(_cust_countries)
        _max_cid = int(_unique_countries.max()) + 1

        _countries_no_local = np.zeros(_max_cid, dtype=bool)

        _local_pools = [None] * _max_cid
        _max_pool_len = 0
        for _cid_v in _unique_countries:
            _cid_int = int(_cid_v)
            _country_sk = c2sk[_cid_int] if _cid_int < len(c2sk) else np.array([], dtype=np.int32)
            if _country_sk.size:
                _lp = _country_sk[np.array([s in _month_set for s in _country_sk.tolist()], dtype=bool)]
            else:
                _lp = np.array([], dtype=np.int32)
            if _lp.size == 0:
                _lp = month_stores
                _countries_no_local[_cid_int] = True
            _local_pools[_cid_int] = _lp
            if _lp.size > _max_pool_len:
                _max_pool_len = _lp.size

        _pool_2d = np.empty((_max_cid, _max_pool_len), dtype=np.int32)
        _pool_lens = np.ones(_max_cid, dtype=np.int64)
        for _cid_v in _unique_countries:
            _cid_int = int(_cid_v)
            _lp = _local_pools[_cid_int]
            _pool_lens[_cid_int] = _lp.size
            _pool_2d[_cid_int, :_lp.size] = _lp
            if _lp.size < _max_pool_len:
                _pool_2d[_cid_int, _lp.size:] = _lp[0]

        _rand_idx = rng.integers(0, np.iinfo(np.int64).max, size=_n_to_sample).astype(np.int64)
        _local_pick = _pool_2d[_cust_countries, _rand_idx % _pool_lens[_cust_countries]]
        _global_pick = month_stores[rng.integers(0, len(month_stores), size=_n_to_sample)]

        # Without local stores, the 70/30 local-vs-global split is meaningless
        # (both pools are identical) — turn off the split for those customers.
        if _countries_no_local.any():
            _use_local[_countries_no_local[_cust_countries]] = False

        order_store = np.where(_use_local, _local_pick, _global_pick).astype(np.int32)
    else:
        order_store = month_stores[rng.integers(0, len(month_stores), size=_n_to_sample)]

    if not skip_cols:
        return order_store[order_idx]
    return order_store


def _resample_stores_for_open_close(
    rng, store_key_arr, order_dates,
    store_open_day, store_close_day,
    store_keys, order_idx,
    *,
    store_reno_start_day=None, store_reno_end_day=None,
):
    """Resample stores whose order date falls outside their open/close window
    OR inside their renovation window. Replacement candidates are stores that
    are open AND not currently renovating on the order date."""
    if store_open_day is None:
        return store_key_arr

    _max_sk_d = len(store_open_day)
    _line_dates_d = np.asarray(order_dates, dtype="datetime64[D]")
    _sk_i = store_key_arr.astype(np.int32)
    _sk_clipped = np.clip(_sk_i, 0, _max_sk_d - 1)
    _open_for_row = store_open_day[_sk_clipped]
    _bad = _line_dates_d < _open_for_row
    if store_close_day is not None:
        _max_sk_c = len(store_close_day)
        _close_for_row = store_close_day[np.clip(_sk_i, 0, _max_sk_c - 1)]
        _bad |= _line_dates_d >= _close_for_row

    if store_reno_start_day is not None and store_reno_end_day is not None:
        _max_sk_r = len(store_reno_start_day)
        _sk_clipped_r = np.clip(_sk_i, 0, _max_sk_r - 1)
        # Renovation window is [start, end) — store reopens on end day.
        _bad |= (
            (_line_dates_d >= store_reno_start_day[_sk_clipped_r])
            & (_line_dates_d < store_reno_end_day[_sk_clipped_r])
        )

    _n_bad = int(_bad.sum())
    if _n_bad == 0:
        return store_key_arr

    _bad_idx = np.where(_bad)[0]
    _bad_dates = _line_dates_d[_bad_idx]
    _unique_bad_dates = np.unique(_bad_dates)
    _sk_i32 = store_keys.astype(np.int32)
    _all_open = store_open_day[np.clip(_sk_i32, 0, _max_sk_d - 1)]
    _all_close_arr = (
        store_close_day[np.clip(_sk_i32, 0, len(store_close_day) - 1)]
        if store_close_day is not None else None
    )
    _all_rs = None
    _all_re = None
    if store_reno_start_day is not None and store_reno_end_day is not None:
        _sk_clipped_all_r = np.clip(_sk_i32, 0, len(store_reno_start_day) - 1)
        _all_rs = store_reno_start_day[_sk_clipped_all_r]
        _all_re = store_reno_end_day[_sk_clipped_all_r]
    for _bd in _unique_bad_dates:
        _date_mask = _bad_dates == _bd
        _date_rows = _bad_idx[_date_mask]
        _day_ok = _all_open <= _bd
        if _all_close_arr is not None:
            _day_ok = _day_ok & (_all_close_arr > _bd)
        if _all_rs is not None and _all_re is not None:
            _day_ok = _day_ok & ~((_all_rs <= _bd) & (_all_re > _bd))
        _day_stores = store_keys[_day_ok]
        if _day_stores.size == 0:
            _day_stores = store_keys
        if order_idx is not None:
            _bad_order_ids = order_idx[_date_rows]
            _uniq_oids, _oid_inv = np.unique(_bad_order_ids, return_inverse=True)
            _repls = _day_stores[rng.integers(0, len(_day_stores), size=len(_uniq_oids))]
            store_key_arr[_date_rows] = _repls[_oid_inv]
        else:
            store_key_arr[_date_rows] = _day_stores[
                rng.integers(0, len(_day_stores), size=len(_date_rows))
            ]
    return store_key_arr


def _apply_store_channel_correlation(
    rng, store_key_arr, skip_cols,
    order_idx, order_starts, n_unique_orders, n_lines,
    store_ch_keys, ch_prob_by_store,
):
    """Sample SalesChannelKey per order/line based on store type channel affinity."""
    if store_ch_keys is None or ch_prob_by_store is None:
        return None

    _max_sk_ch = max(len(store_ch_keys), len(ch_prob_by_store))
    _unique_sk_all = np.unique(store_key_arr)
    _max_n_ch = max(
        (len(store_ch_keys[int(s)]) for s in _unique_sk_all
         if int(s) < len(store_ch_keys) and store_ch_keys[int(s)] is not None),
        default=len(SALES_CHANNEL_CORE_KEYS),
    )
    _ch_keys_2d = np.full((_max_sk_ch, _max_n_ch), SALES_CHANNEL_CORE_KEYS[0], dtype=np.int32)
    _ch_cdf_2d = np.ones((_max_sk_ch, _max_n_ch), dtype=np.float64)

    for _sk_v in _unique_sk_all:
        _sk_i = int(_sk_v)
        if _sk_i >= _max_sk_ch:
            continue
        _ck = store_ch_keys[_sk_i] if _sk_i < len(store_ch_keys) and store_ch_keys[_sk_i] is not None else SALES_CHANNEL_CORE_KEYS
        _cp = ch_prob_by_store[_sk_i] if _sk_i < len(ch_prob_by_store) and ch_prob_by_store[_sk_i] is not None else None
        _nc = len(_ck)
        _ch_keys_2d[_sk_i, :_nc] = _ck
        if _nc < _max_n_ch:
            _ch_keys_2d[_sk_i, _nc:] = _ck[-1]
        if _cp is not None and len(_cp) == _nc:
            _cdf = np.cumsum(_cp)
            _cdf[-1] = 1.0
            _ch_cdf_2d[_sk_i, :_nc] = _cdf
        else:
            _ch_cdf_2d[_sk_i, :_nc] = np.arange(1, _nc + 1, dtype=np.float64) / _nc
        if _nc < _max_n_ch:
            _ch_cdf_2d[_sk_i, _nc:] = 1.0

    def _sample_channels_vectorized(_store_arr, _n):
        _r = rng.random(_n)
        _sk_clp = np.clip(_store_arr.astype(np.int64), 0, _max_sk_ch - 1)
        _row_cdf = _ch_cdf_2d[_sk_clp]
        _ch_idx = np.argmax(_r[:, None] < _row_cdf, axis=1)
        return _ch_keys_2d[_sk_clp, _ch_idx]

    if not skip_cols and order_idx is not None:
        _order_stores_for_ch = store_key_arr[order_starts] if order_starts is not None else store_key_arr[:n_unique_orders]
        _ch_per_order = _sample_channels_vectorized(_order_stores_for_ch, n_unique_orders)
        return _ch_per_order[order_idx]
    return _sample_channels_vectorized(store_key_arr, n_lines)


def _enforce_channel_product_eligibility(
    rng, prod_idx, sales_channel_key_arr,
    pce, ch2eg, product_weight,
):
    """Resample products that are ineligible for their assigned sales channel."""
    if (pce is None or ch2eg is None
            or sales_channel_key_arr is None
            or product_weight is None):
        return prod_idx

    _MAX_RESAMPLE_PASSES = 3
    for _pass in range(_MAX_RESAMPLE_PASSES):
        _line_ch = sales_channel_key_arr.astype(np.int32)
        _line_eg = ch2eg[np.clip(_line_ch, 0, len(ch2eg) - 1)]
        _line_elig = pce[prod_idx, _line_eg]
        _bad = _line_elig == 0
        _n_bad = int(_bad.sum())
        if _n_bad == 0:
            break
        _bad_idx = np.flatnonzero(_bad)
        for _eg in np.unique(_line_eg[_bad_idx]):
            _eg_mask = _bad & (_line_eg == _eg)
            _n_eg = int(_eg_mask.sum())
            if _n_eg == 0:
                continue
            _eligible_rows = np.flatnonzero(pce[:, _eg] == 1)
            if _eligible_rows.size == 0:
                continue
            _ew = product_weight[_eligible_rows]
            _ews = float(_ew.sum())
            if _ews > 1e-12:
                _picks = rng.choice(len(_eligible_rows), size=_n_eg, p=_ew / _ews)
            else:
                _picks = rng.integers(0, len(_eligible_rows), size=_n_eg)
            prod_idx[_eg_mask] = _eligible_rows[_picks]
    return prod_idx


def _apply_new_customer_promo(
    promo_keys, order_dates, customer_keys_out,
    sales_channel_key_arr,
    ckey_to_start_month, nc_min_month,
    nc_keys, nc_starts, nc_ends,
    nc_promo_set, nc_window_months,
    no_discount_key,
    promo_channel_group, promo_keys_all,
):
    """Strip invalid NC promo assignments and force-assign to genuinely new customers."""
    if ckey_to_start_month is None or nc_keys is None or nc_keys.size == 0:
        return promo_keys

    order_dates_D = order_dates.astype("datetime64[D]", copy=False)
    order_month_offset = order_dates.astype("datetime64[M]").astype("int64") - nc_min_month
    cust_start = ckey_to_start_month[customer_keys_out]
    months_since = order_month_offset - cust_start
    is_new = (cust_start >= 0) & (months_since >= 0) & (months_since <= nc_window_months)

    # Step 1: remove NC promo from non-new customers (invalid random assignments)
    has_nc = np.isin(promo_keys, list(nc_promo_set))
    invalid_nc = has_nc & ~is_new
    if invalid_nc.any():
        promo_keys[invalid_nc] = int(no_discount_key)

    # Step 2: force-assign NC promo to new customers that have no other promo.
    eligible = is_new & (promo_keys == int(no_discount_key))
    if not eligible.any():
        return promo_keys

    new_indices = np.flatnonzero(eligible)
    new_dates = order_dates_D[new_indices]

    _nc_ch_group = promo_channel_group[np.isin(promo_keys_all, list(nc_promo_set))] if promo_channel_group is not None else None
    _new_ch = sales_channel_key_arr[new_indices] if sales_channel_key_arr is not None else None
    _PHYS = frozenset(PHYSICAL_CHANNELS.tolist())
    _DIGI = frozenset(DIGITAL_CHANNELS.tolist())

    for pi in range(len(nc_keys)):
        active = (new_dates >= nc_starts[pi]) & (new_dates <= nc_ends[pi])
        if not active.any():
            continue
        if _nc_ch_group is not None and _new_ch is not None and pi < len(_nc_ch_group):
            _pg = int(_nc_ch_group[pi])
            if _pg == 1:
                _ch_ok = np.array([int(c) in _PHYS for c in _new_ch[active]], dtype=bool)
                active_idx = np.flatnonzero(active)
                active[active_idx[~_ch_ok]] = False
            elif _pg == 2:
                _ch_ok = np.array([int(c) in _DIGI for c in _new_ch[active]], dtype=bool)
                active_idx = np.flatnonzero(active)
                active[active_idx[~_ch_ok]] = False
        if active.any():
            promo_keys[new_indices[active]] = int(nc_keys[pi])

    return promo_keys


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def build_chunk_table(
    n: int,
    seed: int,
    no_discount_key: int = 1,
    *,
    chunk_idx: int,
    chunk_capacity_orders: int,
) -> pa.Table:
    """
    Build a chunk of synthetic sales data.

    Guarantees:
    - Customer lifecycle controls eligibility (IsActiveInSales + Start/End month)
    - Month loop generates orders within that month (date_pool sliced)
    - Optional discovery forcing (persistable across chunks via State.seen_customers)
      - IMPORTANT: discovery is OFF if customer_discovery block is absent
    - All per-order arrays remain aligned
    """

    # Lazy import to avoid circular import: sales_models imports State from sales_logic
    from ..sales_models import build_quantity, build_prices

    if not PA_AVAILABLE:
        raise RuntimeError("pyarrow is required")

    rng = np.random.default_rng(int(seed))
    skip_cols = bool(State.skip_order_cols)
    chunk_idx = int(chunk_idx)
    cap = int(chunk_capacity_orders)

    if chunk_idx < 0:
        raise ValueError(f"chunk_idx must be >= 0, got {chunk_idx}")
    if cap <= 0:
        raise ValueError(f"chunk_capacity_orders must be > 0, got {cap}")

    INT32_MAX = np.int64(np.iinfo(np.int32).max)

    # Day-based order ID ranges: each calendar day gets a disjoint ID band,
    # and within each day each chunk gets its own non-overlapping slot.
    # This guarantees SalesOrderNumber increases monotonically with OrderDate
    # even across parallel chunks.
    _day_stride = np.int64(State.month_stride or 0)
    _per_chunk_alloc = np.int64(State.per_chunk_alloc or 0)
    _use_day_ids = _day_stride > 0 and _per_chunk_alloc > 0
    _order_id_int64 = bool(getattr(State, "order_id_int64", False))

    # ------------------------------------------------------------
    # STATIC STATE
    # ------------------------------------------------------------
    product_np = (
        State.active_product_np
        if getattr(State, "active_product_np", None) is not None
        else State.product_np
    )

    # Customer dimension arrays (new contract)
    customer_keys = _get_state_attr("customer_keys", "customers")
    if customer_keys is None:
        raise SalesError("State must provide customer_keys/customers")
    customer_keys = np.asarray(customer_keys, dtype=np.int32)

    # is_active_in_sales (new contract)
    is_active_in_sales = _get_state_attr("customer_is_active_in_sales", "is_active_in_sales")
    if is_active_in_sales is None:
        # backward compat: if State.active_customer_keys exists, treat those as active
        active_keys = getattr(State, "active_customer_keys", None)
        if active_keys is not None:
            is_active_in_sales = np.zeros(customer_keys.shape[0], dtype=np.int32)
            idx = (np.asarray(active_keys, dtype=np.int32) - 1)
            idx = idx[(idx >= 0) & (idx < customer_keys.shape[0])]
            is_active_in_sales[idx] = 1
        else:
            # assume all active
            is_active_in_sales = np.ones(customer_keys.shape[0], dtype=np.int32)
    else:
        is_active_in_sales = np.asarray(is_active_in_sales, dtype=np.int32)

    start_month = _get_state_attr("customer_start_month")
    if start_month is None:
        # If not yet wired, fall back: everyone starts at 0 (old behavior)
        start_month = np.zeros(customer_keys.shape[0], dtype="int64")
    else:
        start_month = np.asarray(start_month, dtype="int64")

    end_month = _get_state_attr("customer_end_month")
    end_month_norm = _normalize_end_month(end_month, customer_keys.shape[0])

    base_weight = _get_state_attr("customer_base_weight")
    if base_weight is not None:
        base_weight = np.asarray(base_weight, dtype="float64")

    date_pool = State.date_pool
    date_prob = State.date_prob

    if _use_day_ids:
        _date_pool_min_day = np.int64(
            date_pool[0].astype("datetime64[D]").astype(np.int64)
        )
    store_keys = State.store_keys
    store_eligible_by_month = State.store_eligible_by_month
    store_open_day = State.store_open_day      # dense array: store_key -> datetime64[D]
    store_close_day = State.store_close_day    # dense array: store_key -> datetime64[D]
    store_reno_start_day = getattr(State, "store_reno_start_day", None)
    store_reno_end_day = getattr(State, "store_reno_end_day", None)

    promo_keys_all = State.promo_keys_all
    promo_start_all = State.promo_start_all
    promo_end_all = State.promo_end_all

    nc_promo_keys = State.new_customer_promo_keys
    nc_promo_set = frozenset(nc_promo_keys.tolist()) if nc_promo_keys is not None and nc_promo_keys.size > 0 else None
    nc_window_months = int(State.new_customer_window_months or 3)

    # Precompute New Customer promo date windows for force-assignment
    if nc_promo_set is not None and customer_keys.size > 0 and start_month is not None:
        _max_ckey = int(customer_keys.max())
        _ckey_to_start_month = np.full(_max_ckey + 1, -1, dtype=np.int64)
        _ckey_to_start_month[customer_keys] = start_month

        _nc_min_month = int(date_pool.astype("datetime64[M]").astype("int64").min())

        nc_idx = np.isin(promo_keys_all, list(nc_promo_set))
        _nc_keys = promo_keys_all[nc_idx]
        _nc_starts = promo_start_all[nc_idx]
        _nc_ends = promo_end_all[nc_idx]
    else:
        _ckey_to_start_month = None
        _nc_min_month = 0
        _nc_keys = None
        _nc_starts = None
        _nc_ends = None

    st2g_arr = State.store_to_geo_arr
    g2c_arr = State.geo_to_currency_arr
    if st2g_arr is None or g2c_arr is None:
        raise SalesError("State must provide store_to_geo_arr and geo_to_currency_arr")

    file_format = State.file_format

    # IMPORTANT:
    # - State.sales_schema reflects the *Sales output* schema (driven by skip_order_cols_requested)
    # - chunk_builder must use the *generation* schema (driven by skip_order_cols effective)
    if file_format == "deltaparquet":
        schema = State.schema_no_order_delta if skip_cols else State.schema_with_order_delta
    else:
        schema = State.schema_no_order if skip_cols else State.schema_with_order

    schema_types = {f.name: f.type for f in schema}

    # ------------------------------------------------------------
    # MONTH SLICES (date_pool indices per month)
    # ------------------------------------------------------------
    month_slices = _build_month_slices(date_pool)
    T = len(month_slices)
    if T == 0:
        return _empty_table(schema)

    # ------------------------------------------------------------
    # ROW BUDGET PER MONTH (BASE DEMAND NORMALIZATION)
    # ------------------------------------------------------------
    macro_cfg = State.models_cfg.get("macro_demand", {}) or {}

    eligible_counts = _eligible_counts_fast(
        T=T,
        is_active_in_sales=is_active_in_sales,
        start_month=start_month,
        end_month_norm=end_month_norm,
    )
    if eligible_counts.sum() <= 0:
        return _empty_table(schema)

    rows_per_month = build_rows_per_month(
        rng=rng,
        total_rows=int(n),
        eligible_counts=eligible_counts,
        macro_cfg=macro_cfg,
    )

    # ------------------------------------------------------------
    # CUSTOMER MIX CONFIG
    # ------------------------------------------------------------
    cust_cfg = State.models_cfg.get("customers", {}) or {}
    new_customer_share = float(np.clip(
        cust_cfg.get("new_customer_share", 0.10), 0.0, 1.0))
    distinct_ratio = float(np.clip(
        cust_cfg.get("distinct_ratio", 0.55), 0.0, 1.0))
    cycle_amplitude = float(np.clip(
        cust_cfg.get("cycle_amplitude", 0.35), 0.0, 1.0))
    discovery_shape = float(np.clip(
        cust_cfg.get("discovery_shape", 0.0), -1.0, 1.0))

    participation_noise = float(np.clip(
        cust_cfg.get("participation_noise", 0.10), 0.0, 1.0))

    _DEFAULT_SEASONAL_SPIKES = [
        {"month": 3,  "boost": 0.15},
        {"month": 7,  "boost": 0.12},
        {"month": 9,  "boost": 0.10},
        {"month": 11, "boost": 0.40},
        {"month": 12, "boost": 0.25},
    ]
    seasonal_spikes_raw = cust_cfg.get("seasonal_spikes", None)
    if seasonal_spikes_raw is None:
        seasonal_spikes_raw = _DEFAULT_SEASONAL_SPIKES
    seasonal_spike_map: dict[int, float] = {}
    for entry in seasonal_spikes_raw:
        # Support both dicts (fallback defaults) and Pydantic models (trend presets)
        month = entry.get("month") if isinstance(entry, dict) else getattr(entry, "month", None)
        boost = entry.get("boost") if isinstance(entry, dict) else getattr(entry, "boost", None)
        if month is not None and boost is not None:
            cal_month = int(month)
            if 1 <= cal_month <= 12:
                seasonal_spike_map[cal_month] = float(boost)

    use_discovery = new_customer_share > 0.0

    # Precompute per-month discovery shape multipliers (normalized to mean=1)
    if use_discovery and T > 1 and discovery_shape != 0.0:
        progress = np.linspace(0.0, 1.0, T)
        shape_mult = 1.0 + discovery_shape * (2.0 * progress - 1.0)
        shape_mult = np.maximum(shape_mult, 0.1)
        shape_mult /= shape_mult.mean()
    else:
        shape_mult = np.ones(T, dtype=np.float64)

    _BOOTSTRAP_MONTHS = _int_or(macro_cfg.get("bootstrap_months"), 6)
    _MAX_FRAC_PER_MONTH = _float_or(cust_cfg.get("max_new_fraction_per_month"), 0.015)
    _MAX_DISTINCT_RATIO = _float_or(macro_cfg.get("max_distinct_ratio"), 0.70)
    _MIN_DISTINCT_CUSTOMERS = _int_or(macro_cfg.get("min_distinct_customers"), 250)

    # Brand popularity is OFF if block absent. If present without enabled, default-on.
    brand_cfg = State.models_cfg.get("brand_popularity", None)
    use_brand_popularity = bool(brand_cfg) and bool(brand_cfg.get("enabled", True))

    if use_discovery:
        seen_customers = getattr(State, "seen_customers", None)
        if seen_customers is None:
            seen_customers = _make_seen_lookup(customer_keys)
        elif isinstance(seen_customers, set):
            seen_customers = _make_seen_lookup(customer_keys, existing_set=seen_customers)
        # else: already a numpy boolean lookup from a previous chunk
    else:
        seen_customers = None

    # ------------------------------------------------------------
    # Generate month-by-month
    # ------------------------------------------------------------
    # Accumulate raw numpy buffers per column; build ONE Arrow table at the end.
    # This eliminates T × C Arrow array constructions + pa.concat_tables overhead.
    col_buffers: dict[str, list] = {f.name: [] for f in schema}
    # Rows produced per appended month, aligned 1:1 with each col_buffers list, so
    # the final assembly can pad null months with typed nulls (CHUNK-3).
    month_row_counts: list[int] = []
    total_rows = 0

    # Salesperson effective-date map + fallback (constant across months)
    eff = getattr(State, "salesperson_effective_by_store", None)
    sp_map_fallback = getattr(State, 'salesperson_by_store_month', None)

    # Expected average lines per order — used to estimate order count from row target.
    # When max_lines > 1, build_orders expands each order into 1..max_lines line rows,
    # so we sample fewer customers (≈ m_rows / avg_lines) and let build_orders expand.
    max_lines = int(getattr(State, "max_lines_per_order", 6) or 6)
    avg_lines_est = 1.0 if max_lines == 1 else 1.8

    # CDF cache: use worker-lifetime cache so subsequent chunks skip
    # intersection + CDF recomputation entirely (contents are deterministic).
    _product_cdf_cache = _worker_cdf_cache

    for m_offset in range(T):
        m_rows = int(rows_per_month[m_offset])
        if m_rows <= 0:
            continue

        date_idx = month_slices[m_offset]
        month_date_pool = date_pool[date_idx]
        if month_date_pool.size == 0:
            continue

        if date_prob is not None:
            # dtype conversion creates a new array (no explicit .copy() needed)
            month_date_prob = np.asarray(date_prob[date_idx], dtype="float64")
            s = float(month_date_prob.sum())
            if s > 1e-12:
                month_date_prob = month_date_prob / s  # new array, avoids mutating shared view
            else:
                month_date_prob = None
        else:
            month_date_prob = None

        # --------------------------------------------------------
        # Eligibility mask (compute only for months that generate rows)
        # --------------------------------------------------------
        eligible_mask = _eligible_customer_mask_for_month(
            m_offset=int(m_offset),
            is_active_in_sales=is_active_in_sales,
            start_month=start_month,
            end_month_norm=end_month_norm,
        )
        if not eligible_mask.any():
            continue

        # --------------------------------------------------------
        # DISCOVERY TARGET
        # --------------------------------------------------------
        disc_cfg_local = {}
        if use_discovery:
            target_new = int(round(
                new_customer_share * m_rows * float(shape_mult[m_offset])))

            # Bootstrap: smooth ramp over first months
            if m_offset < _BOOTSTRAP_MONTHS:
                target_new = int(round(
                    target_new * (m_offset + 1) / _BOOTSTRAP_MONTHS))

            disc_cfg_local["_target_new_customers"] = max(0, target_new)
            disc_cfg_local["max_fraction_per_month"] = _MAX_FRAC_PER_MONTH

        # --------------------------------------------------------
        # DETERMINE SAMPLING COUNT
        # --------------------------------------------------------
        # In order mode (skip_cols=False, max_lines > 1): sample fewer
        # customers (one per order), then let build_orders expand each
        # order into 1..max_lines line rows to reach m_rows total.
        if not skip_cols and max_lines > 1:
            n_sample = max(1, int(round(m_rows / avg_lines_est)))
        else:
            n_sample = m_rows

        # --------------------------------------------------------
        # PARTICIPATION DISTINCT TARGET
        # --------------------------------------------------------
        target_distinct = None
        if distinct_ratio > 0.0:
            eligible_count = int(eligible_mask.sum())
            k = distinct_ratio * eligible_count

            if cycle_amplitude > 0.0:
                cyc = float(np.sin(2.0 * np.pi * m_offset / 24.0))
                k *= 1.0 + cycle_amplitude * cyc

            if seasonal_spike_map and month_date_pool.size > 0:
                cal_month = int(month_date_pool[0].astype("datetime64[M]").astype(int) % 12) + 1
                k *= 1.0 + seasonal_spike_map.get(cal_month, 0.0)

            if participation_noise > 0.0:
                k *= 1.0 + participation_noise * float(rng.standard_normal())

            k = max(k, float(_MIN_DISTINCT_CUSTOMERS))
            k = min(k, eligible_count * _MAX_DISTINCT_RATIO,
                    float(eligible_count), float(n_sample))
            target_distinct = max(1, int(round(k)))

        # --------------------------------------------------------
        # CUSTOMER SAMPLING (discovery/participation aware)
        # --------------------------------------------------------
        customer_keys_for_orders = _sample_customers(
            rng=rng,
            customer_keys=customer_keys,
            eligible_mask=eligible_mask,
            seen_set=seen_customers,
            n=int(n_sample),
            use_discovery=use_discovery,
            discovery_cfg=disc_cfg_local,
            base_weight=base_weight,
            target_distinct=target_distinct,
            end_month_norm=end_month_norm,
            m_offset=int(m_offset),
        )


        if customer_keys_for_orders.size == 0:
            continue

        n_orders = int(customer_keys_for_orders.size)

        # --------------------------------------------------------
        # ORDERS (use month-specific date pool so month loop is real)
        # --------------------------------------------------------
        if not skip_cols:
            orders = build_orders(
                rng=rng,
                n=m_rows,
                skip_cols=False,
                date_pool=month_date_pool,
                date_prob=month_date_prob,
                customers=customer_keys_for_orders,
                _len_date_pool=len(month_date_pool),
                _len_customers=n_orders,
                order_id_start=0,
            )

            customer_keys_out = orders["customer_keys"]
            order_dates = orders["order_dates"]
            line_num = orders["line_num"]

            order_dates = _clamp_order_dates_to_customer_start(order_dates, customer_keys_out)

            if _use_day_ids:
                _oc = orders["_order_count"]
                _reps = orders["_repeats"]

                _od_D = order_dates[line_num == 1].astype("datetime64[D]")
                _d_off = _od_D.astype(np.int64) - _date_pool_min_day

                # CHUNK-1: the customer-start clamp above runs AFTER build_orders
                # sorted by date, so _d_off is no longer guaranteed sorted. Derive
                # each order's within-day rank from a stable sort (see helper) so
                # the cursor is correct regardless of clamp-induced reordering.
                _cursor = _within_day_cursor(_d_off)

                # Guard on cursor magnitude (the value that actually overflows the
                # per-chunk band). max(cursor)+1 is the largest per-day order count.
                _max_day_count = int(_cursor.max(initial=-1)) + 1
                if _max_day_count > int(_per_chunk_alloc):
                    raise SalesError(
                        f"per_chunk_alloc too small: {_max_day_count} orders on one "
                        f"day but alloc={int(_per_chunk_alloc)} per chunk per day."
                    )

                _new_ids = (
                    _d_off * _day_stride
                    + np.int64(chunk_idx) * _per_chunk_alloc
                    + _cursor
                    + np.int64(1)
                )

                # Emit the dtype the schema expects (single-sourced from sales.py's
                # _order_id_int64). int64 for large runs; int32 otherwise, with a
                # loud overflow guard as a backstop since the int32 cast (and the
                # schema's safe=False conversion) would otherwise wrap silently.
                if _order_id_int64:
                    order_ids_int = np.repeat(_new_ids, _reps)
                else:
                    if _new_ids.size > 0 and int(_new_ids.max()) >= int(INT32_MAX):
                        raise SalesError(
                            f"Day-based SalesOrderNumber would overflow int32: "
                            f"max_id={int(_new_ids.max())} >= {int(INT32_MAX)} "
                            "but int64 promotion was not enabled for this run."
                        )
                    order_ids_int = np.repeat(_new_ids.astype(np.int32), _reps)
            else:
                order_ids_int = orders["order_ids_int"]

        else:
            customer_keys_out = customer_keys_for_orders
            order_dates = month_date_pool[rng.integers(0, len(month_date_pool), size=n_orders)]
            order_dates = _clamp_order_dates_to_customer_start(order_dates, customer_keys_out)
            order_ids_int = None
            line_num = None

        n_lines = int(np.asarray(customer_keys_out).shape[0])

        # --------------------------------------------------------
        # STORE (sampled first — needed for assortment filtering)
        #   Agreement: 1 Store per Order (when order ids exist)
        # --------------------------------------------------------
        if not skip_cols and line_num is not None:
            order_starts = (np.asarray(line_num) == 1)
            order_idx = np.cumsum(order_starts.astype(np.int64)) - 1
            n_unique_orders = int(order_idx.max() + 1) if order_idx.size else 0
        else:
            order_starts = None
            order_idx = None
            n_unique_orders = 0

        # Use per-month eligible stores if available, else fall back to all stores
        _month_stores = store_keys
        if store_eligible_by_month is not None and m_offset < len(store_eligible_by_month):
            _eligible = store_eligible_by_month[m_offset]
            if _eligible is not None and len(_eligible) > 0:
                _month_stores = _eligible

        # CORRELATION #2: Customer → Store geographic bias
        # build_orders sorts by date, so customer_keys_for_orders (pre-sort)
        # is misaligned with order_idx (post-sort). Use sorted order-level
        # keys derived from the line-level output instead.
        _sorted_order_cust = customer_keys_out[order_starts] if order_starts is not None else customer_keys_for_orders
        store_key_arr = _apply_geo_bias_store_sampling(
            rng, skip_cols, n_unique_orders, n_lines,
            _sorted_order_cust, customer_keys_out,
            _month_stores, order_idx,
            getattr(State, "customer_geo_key", None),
            getattr(State, "geo_to_country_id", None),
            getattr(State, "store_to_country_id", None),
            getattr(State, "country_to_store_keys", None),
        )

        # DAY-LEVEL STORE ELIGIBILITY: resample for open/close + renovation.
        # Replacement candidates are drawn from the per-month eligible set
        # so a store filtered out by the month set isn't reintroduced here.
        store_key_arr = _resample_stores_for_open_close(
            rng, store_key_arr, order_dates,
            store_open_day, store_close_day,
            _month_stores, order_idx,
            store_reno_start_day=store_reno_start_day,
            store_reno_end_day=store_reno_end_day,
        )

        # CORRELATION #1: Store → SalesChannelKey
        sales_channel_key_arr = _apply_store_channel_correlation(
            rng, store_key_arr, skip_cols,
            order_idx, order_starts, n_unique_orders, n_lines,
            getattr(State, "store_channel_keys", None),
            getattr(State, "channel_prob_by_store", None),
        )

        # --------------------------------------------------------
        # PRODUCTS (PER LINE) — each line gets its own product
        # so multi-line orders contain distinct items, not repeats.
        # When assortment is active, products are sampled from
        # the store's available pool instead of the full catalog.
        #
        # Product sampling is weighted by PopularityScore and
        # boosted by SeasonalityProfile for the current calendar month.
        #
        # CORRELATION #4: SalesChannelKey → ProductKey
        # Filter product pool by channel eligibility flags.
        # --------------------------------------------------------
        # Compute calendar month once (reused by product weight + store assortment CDF)
        _cal_month = int(month_date_pool[0].astype("datetime64[M]").astype(int) % 12) + 1 if month_date_pool.size > 0 else 0

        _product_weight = _build_product_weight_for_month(month_date_pool, m_offset, cal_month=_cal_month)

        _pce = getattr(State, "product_channel_eligible", None)
        _ch2eg = getattr(State, "_channel_to_elig_group", None)

        _store_product_rows = getattr(State, "store_to_product_rows", None)
        if _store_product_rows is not None:
            prod_idx = _sample_products_per_store(
                rng=rng,
                store_key_arr=store_key_arr,
                store_to_product_rows=_store_product_rows,
                product_np=product_np,
                product_weight=_product_weight,
                _cdf_cache=_product_cdf_cache,
                _cal_month=_cal_month,
                m_offset=int(m_offset),
                use_brand_popularity=use_brand_popularity,
            )
        else:
            prod_idx = _sample_product_row_indices(
                rng=rng,
                n=n_lines,
                product_np=product_np,
                m_offset=int(m_offset),
                enabled=use_brand_popularity,
                product_weight=_product_weight,
            )

        # CORRELATION #4: Channel → product eligibility enforcement
        prod_idx = _enforce_channel_product_eligibility(
            rng, prod_idx, sales_channel_key_arr,
            _pce, _ch2eg, _product_weight,
        )

        customer_keys_out = np.asarray(customer_keys_out, dtype=np.int32)
        order_dates = np.asarray(order_dates, dtype="datetime64[D]")

        # Convert order dates to epoch days for SCD2 per-row version lookups
        _order_epoch_days = order_dates.astype(np.int64)  # datetime64[D] → epoch days

        # SCD2: resolve per-row product version using actual OrderDate
        _pscd2_starts = getattr(State, "product_scd2_starts", None)
        _pscd2_data = getattr(State, "product_scd2_data", None)
        if getattr(State, "product_scd2_active", False) and _pscd2_starts is not None and _pscd2_data is not None:
            # For each sale: find the last version where start <= order_day
            _p_starts = _pscd2_starts[prod_idx]         # (n_lines, max_ver)
            _ver_idx = np.sum(_p_starts <= _order_epoch_days[:, np.newaxis], axis=1) - 1
            _ver_idx = np.clip(_ver_idx, 0, _p_starts.shape[1] - 1)
            _p_data = _pscd2_data[prod_idx]             # (n_lines, max_ver, 3)
            _row_range = np.arange(len(prod_idx))
            product_keys = _p_data[_row_range, _ver_idx, 0].astype(np.int32)
            unit_price   = _p_data[_row_range, _ver_idx, 1].astype(np.float64)
            unit_cost    = _p_data[_row_range, _ver_idx, 2].astype(np.float64)
        else:
            product_keys = product_np[prod_idx, 0].astype(np.int32, copy=False)
            unit_price   = product_np[prod_idx, 1].astype(np.float64, copy=False)
            unit_cost    = product_np[prod_idx, 2].astype(np.float64, copy=False)

        geo_arr = st2g_arr[store_key_arr]
        if np.any(geo_arr < 0):
            raise SalesError("store_to_geo_arr missing mapping for sampled StoreKey(s)")
        currency_arr = g2c_arr[geo_arr]
        if np.any(currency_arr < 0):
            raise SalesError("geo_to_currency_arr missing mapping for sampled GeographyKey(s)")

        # --------------------------------------------------------
        # DATE LOGIC (CORRELATION #3: channel-aware delivery)
        # --------------------------------------------------------
        _ch_fulfill = getattr(State, "channel_fulfillment_days", None)
        dates = compute_dates(
            rng=rng,
            n=n_lines,
            product_keys=product_keys,
            order_ids_int=order_ids_int,
            order_dates=order_dates,
            channel_keys=sales_channel_key_arr,
            channel_fulfillment_days=_ch_fulfill,
        )
        
        # --------------------------------------------------------
        # PROMOTIONS (CORRELATION #5: channel-filtered)
        # --------------------------------------------------------
        _pcg = getattr(State, "promo_channel_group", None)
        if (not skip_cols) and (order_starts is not None):
            # use order-level dates (first line per order)
            order_dates_order = np.asarray(order_dates, dtype="datetime64[D]")[order_starts]
            _order_ch = sales_channel_key_arr[order_starts] if sales_channel_key_arr is not None else None

            promo_order_keys = apply_promotions(
                rng=rng,
                n=n_unique_orders,
                order_dates=order_dates_order,
                promo_keys_all=promo_keys_all,
                promo_start_all=promo_start_all,
                promo_end_all=promo_end_all,
                no_discount_key=no_discount_key,
                channel_keys=_order_ch,
                promo_channel_group=_pcg,
            )

            promo_keys = np.asarray(promo_order_keys, dtype=np.int32)[order_idx]
        else:
            promo_keys = apply_promotions(
                rng=rng,
                n=n_lines,
                order_dates=order_dates,
                promo_keys_all=promo_keys_all,
                promo_start_all=promo_start_all,
                promo_end_all=promo_end_all,
                no_discount_key=no_discount_key,
                channel_keys=sales_channel_key_arr,
                promo_channel_group=_pcg,
            )
        promo_keys = np.asarray(promo_keys, dtype=np.int32)

        # New Customer promo: strip invalid + force-assign to new customers
        promo_keys = _apply_new_customer_promo(
            promo_keys, order_dates, customer_keys_out,
            sales_channel_key_arr,
            _ckey_to_start_month, _nc_min_month,
            _nc_keys, _nc_starts, _nc_ends,
            nc_promo_set, nc_window_months,
            no_discount_key,
            _pcg, promo_keys_all,
        )

        # --------------------------------------------------------
        # EMPLOYEE (EmployeeKey)
        #   Agreement:
        #     - If order identifiers exist (skip_cols == False): 1 salesperson per order (broadcast to all lines),
        #       still respecting effective-dated store assignments by (StoreKey, OrderDate).
        #     - If order identifiers do not exist: keep line-level sampling (old behavior).
        # - Prefer DAY-accurate effective-dated bridge:
        #     State.salesperson_effective_by_store[store] = (emp_keys[int32], start_dates[D], end_dates[D], weights[f64])
        # - Fallback: State.salesperson_by_store_month (values may be -1)
        # IMPORTANT: Never emit Store Manager keys (30_000_000 + StoreKey).
        # --------------------------------------------------------

        # STRICT: no cross-store salesperson fallback. If no eligible salesperson exists, we emit -1.

        # --- Sampling mode ---
        if not skip_cols and order_ids_int is not None:
            # Order-level salesperson: sample per unique order, then broadcast back to lines
            uniq_orders, first_idx, inv_idx = np.unique(order_ids_int, return_index=True, return_inverse=True)
            order_store_sp = store_key_arr[first_idx]
            order_date_sp = order_dates[first_idx].astype("datetime64[D]", copy=False)

            if isinstance(eff, dict) and eff:
                salesperson_order = _sample_salesperson_vectorized(
                    order_store_sp, order_date_sp, eff, rng)
            else:
                # Fallback: month map
                sp_map = sp_map_fallback
                if sp_map is not None:
                    salesperson_order = sp_map[order_store_sp, int(m_offset)].astype(np.int32, copy=False)
                else:
                    salesperson_order = np.full(uniq_orders.size, -1, dtype=np.int32)

            salesperson_key_arr = salesperson_order[inv_idx]
        else:
            # Line-level fallback (when order ids do not exist)
            s_ids = np.asarray(store_key_arr, dtype=np.int32)
            d_ids = np.asarray(order_dates, dtype="datetime64[D]")
            if isinstance(eff, dict) and eff:
                salesperson_key_arr = _sample_salesperson_vectorized(s_ids, d_ids, eff, rng)
            else:
                sp_map = sp_map_fallback
                if sp_map is not None:
                    salesperson_key_arr = sp_map[s_ids, int(m_offset)].astype(np.int32, copy=False)
                else:
                    salesperson_key_arr = np.full(s_ids.size, -1, dtype=np.int32)

        # UPDATE DISCOVERY STATE (persist)
        # --------------------------------------------------------
        if use_discovery:
            # track customers that actually appeared in this month
            _update_seen_lookup(seen_customers, np.unique(customer_keys_out))
            State.seen_customers = seen_customers

        # --------------------------------------------------------
        # QUANTITY + PRICING
        # --------------------------------------------------------
        qty = build_quantity(rng, order_dates)

        price = compute_prices(
            rng=rng,
            n=len(customer_keys_out),
            unit_price=unit_price,
            unit_cost=unit_cost,
        )

        price = build_prices(
            rng=rng,
            order_dates=order_dates,
            qty=qty,
            price=price,
        )

        # --------------------------------------------------------
        # BUILD ARROW TABLE (schema-driven)
        #
        # Base columns are produced here.
        # Extra columns are produced ONLY via sales_logic/columns.py (single extension point).
        #
        # Behavior:
        # - If a schema field isn't produced, it is filled with typed nulls.
        # - If columns.py returns a column not present in the schema, we raise (forces schema update).
        # --------------------------------------------------------
        n_rows = int(customer_keys_out.shape[0])

        # Base columns
        cols: dict[str, object] = {}

        if not skip_cols:
            cols["SalesOrderNumber"] = order_ids_int
            cols["SalesOrderLineNumber"] = line_num

        # SCD2: remap IsCurrent CustomerKey → version-specific CustomerKey using actual OrderDate
        _cscd2_starts = getattr(State, "customer_scd2_starts", None)
        _cscd2_keys = getattr(State, "customer_scd2_keys", None)
        _ckey_to_pidx = getattr(State, "cust_key_to_pool_idx", None)
        if (getattr(State, "customer_scd2_active", False)
                and _cscd2_starts is not None and _cscd2_keys is not None
                and _ckey_to_pidx is not None):
            _ckeys_i32 = np.asarray(customer_keys_out, dtype=np.int32)
            # Bounds check: keys outside the lookup array cannot be remapped
            _in_bounds = (_ckeys_i32 >= 0) & (_ckeys_i32 < len(_ckey_to_pidx))
            _pool_idx = np.full(len(_ckeys_i32), -1, dtype=np.int32)
            _pool_idx[_in_bounds] = _ckey_to_pidx[_ckeys_i32[_in_bounds]]
            _valid = _pool_idx >= 0
            if _valid.any():
                _v_pool = _pool_idx[_valid]
                _v_days = _order_epoch_days[_valid]
                _c_starts = _cscd2_starts[_v_pool]           # (N_valid, max_ver)
                _ver_idx = np.sum(_c_starts <= _v_days[:, np.newaxis], axis=1) - 1
                _ver_idx = np.clip(_ver_idx, 0, _c_starts.shape[1] - 1)
                _c_keys = _cscd2_keys[_v_pool]               # (N_valid, max_ver)
                _row_range = np.arange(len(_v_pool))
                _remapped = np.array(customer_keys_out, dtype=np.int32, copy=True)
                _remapped[_valid] = _c_keys[_row_range, _ver_idx]
                customer_keys_out = _remapped

        cols["CustomerKey"] = customer_keys_out
        cols["ProductKey"] = product_keys
        cols["StoreKey"] = store_key_arr
        cols["EmployeeKey"] = salesperson_key_arr
        cols["PromotionKey"] = promo_keys
        cols["CurrencyKey"] = currency_arr

        # SalesChannelKey produced by store-channel correlation (above)
        if sales_channel_key_arr is not None:
            cols["SalesChannelKey"] = sales_channel_key_arr

        cols["OrderDate"] = _as_datetime64_D(order_dates)
        cols["DueDate"] = _as_datetime64_D(dates["due_date"])
        cols["DeliveryDate"] = _as_datetime64_D(dates["delivery_date"])

        cols["Quantity"] = qty
        cols["NetPrice"] = price["final_net_price"]
        cols["UnitCost"] = price["final_unit_cost"]
        cols["UnitPrice"] = price["final_unit_price"]
        cols["DiscountAmount"] = price["discount_amt"]

        cols["DeliveryStatus"] = dates["delivery_status"]
        cols["IsOrderDelayed"] = dates["is_order_delayed"]

        if file_format == "deltaparquet":
            _pcols = _delta_partition_cols()
            if _pcols & {"Year", "Month"}:
                m_int = order_dates.astype("datetime64[M]").astype("int64")
                if "Year" in _pcols:
                    cols["Year"] = (m_int // 12 + 1970).astype("int16")
                if "Month" in _pcols:
                    cols["Month"] = (m_int % 12 + 1).astype("int16")

        # Extra columns (single extension point)
        extra = build_extra_columns(
            {
                "State": State,
                "rng": rng,
                "n": n_rows,
                "skip_cols": skip_cols,
                "chunk_idx": chunk_idx,
                "seed": seed,
                "schema_types": schema_types,

                # primary arrays
                "customer_keys": customer_keys_out,
                "product_keys": product_keys,
                "store_keys": store_key_arr,
                "salesperson_keys": salesperson_key_arr,
                "promo_keys": promo_keys,
                "currency_keys": currency_arr,
                "order_dates": order_dates,

                # order cols (may be None if skip_cols=True)
                "order_ids_int": order_ids_int,
                "line_num": line_num,

                # derived / measures
                "qty": qty,
                "price": price,
                "due_date": dates["due_date"],
                "delivery_date": dates["delivery_date"],
                "delivery_status": dates["delivery_status"],
                "is_order_delayed": dates["is_order_delayed"],

                # current base columns (so extras can reference already-built cols)
                "cols": cols,
            }
        )

        if extra:
            unknown = [k for k in extra.keys() if k not in schema_types]
            if unknown:
                raise SalesError(
                    f"Extra columns not in Sales schema: {unknown}. "
                    "Add them to src/utils/static_schemas.get_sales_schema(...) first."
                )
            cols.update(extra)

        # Accumulate per-column numpy buffers (defer Arrow conversion to end)
        for f in schema:
            data = cols.get(f.name)
            if data is None:
                # Typed null placeholder — will be converted at final table build
                col_buffers[f.name].append(None)
            else:
                # Ensure numpy array
                if np.isscalar(data):
                    data = np.full(n_rows, data)
                elif not isinstance(data, np.ndarray):
                    data = np.asarray(data)
                col_buffers[f.name].append(data)
        month_row_counts.append(n_rows)
        total_rows += n_rows

    # ------------------------------------------------------------------
    # FINAL: Build ONE Arrow table from all accumulated month buffers
    # ------------------------------------------------------------------
    if total_rows == 0:
        return _empty_table(schema)

    arrays = [
        _assemble_column(f.name, col_buffers[f.name], month_row_counts, total_rows, schema_types)
        for f in schema
    ]
    return pa.Table.from_arrays(arrays, schema=schema)
