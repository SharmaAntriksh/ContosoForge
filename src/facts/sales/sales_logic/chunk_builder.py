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
from src.utils.hashing import hash_u01
from src.utils.logging_utils import warn
from .globals import PA_AVAILABLE, State
from .core import (
    _normalize_cdf,
    _normalize_end_month,
    assign_orders_to_customers,
    build_month_customer_pool,
    effective_customer_weights_for_month,
    apply_promotions,
    build_orders,
    compute_dates,
    compute_prices,
)
from .core.basket import apply_basket_theme, reset_basket_cache
from .columns import build_extra_columns, SALES_CHANNEL_CORE_KEYS, _CHANNEL_SALT
from .eligibility import EligibilityWindow
from .plan_math import chunk_month_band, month_offsets_int
from .weights import cdf_from_probs, normalize_prob, pick_from_cdf, weighted_pick_indices


def _drop_orphan_employee_key_rows(cols: dict, n_rows: int, m_offset: int) -> int:
    """Drop whole-order ``EmployeeKey == -1`` lines; return the new row count.

    A -1 means the sampled (store, month) had no salesperson — only possible on a
    dimension coverage gap that the coordinator's coverage pre-flight has already
    adjudicated: it aborts before generation on avoidable (fully-open) gaps under
    ``sales.coverage_policy: abort``, repairs what it can under ``repair``, and
    otherwise proceeds, documenting that residual boundary-month / unrepairable
    gaps are dropped here. So a -1 that reaches row assembly is always an accepted
    residual, not a precondition violation — drop the whole orders (salesperson is
    per-order, broadcast to lines) to keep FK / order-line integrity, and warn
    loudly that the month is now short of its plan target (``sum n_lines == R[m]``).
    Normal, fully-covered configs never hit a -1 and this is a no-op.
    """
    ek = cols.get("EmployeeKey")
    if ek is None or np.isscalar(ek):
        return n_rows
    ek_arr = np.asarray(ek)
    if not ek_arr.size or not (ek_arr == -1).any():
        return n_rows

    keep = ek_arr != -1
    new_n = int(keep.sum())
    dropped = int(ek_arr.size - new_n)

    warn(
        f"chunk_builder dropped {dropped} EmployeeKey=-1 line(s) at month offset "
        f"{m_offset} (of {n_rows} rows): the store-month had no salesperson, so this "
        "month is now short of its plan target. The coverage pre-flight gates "
        "avoidable gaps before generation (sales.coverage_policy)."
    )
    for cname, cval in list(cols.items()):
        if isinstance(cval, np.ndarray) and cval.shape[:1] == (n_rows,):
            cols[cname] = cval[keep]
    return new_n

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


def _clamp_order_dates_to_customer_lifecycle(
    order_dates: np.ndarray,
    customer_keys: np.ndarray,
) -> np.ndarray:
    """Clamp OrderDate to the customer's exact inclusive lifecycle bounds."""
    if order_dates is None or customer_keys.size == 0:
        return order_dates

    start_lut = getattr(State, "customer_first_eff_start_by_key", None)
    end_lut = getattr(State, "customer_last_eff_end_by_key", None)
    if start_lut is None and end_lut is None:
        return order_dates

    od_days = np.asarray(order_dates, dtype="datetime64[D]").astype(np.int64)
    if start_lut is not None:
        np.maximum(od_days, start_lut[customer_keys], out=od_days)
    if end_lut is not None:
        np.minimum(od_days, end_lut[customer_keys], out=od_days)
    return od_days.view("datetime64[D]")


def _within_day_cursor(d_off: np.ndarray) -> np.ndarray:
    """0-based rank of each order within its OrderDate (day-offset) group.

    Robust to unsorted input. ``build_orders`` returns orders sorted by date,
    but ``_clamp_order_dates_to_customer_lifecycle`` can push individual orders onto
    a later day *after* that sort, breaking the contiguity a naive
    ``arange - first_index`` cursor relies on. That naive cursor would then
    over-count within a day and spill the resulting OrderNumber past the
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


class _WorkerCaches:
    """Worker-lifetime scratch for the chunk builder, grouped under one lifecycle.

    Every entry is a deterministic function of worker-bound constants (the
    lifecycle arrays, store assortment, brand buckets, product weights, run seed),
    so a worker builds each at most once and reuses it across all the chunks it
    processes. ``reset()`` clears every field and runs once per worker init — now
    including ``brand_flat`` and ``delta_pcols``, which previously had no reset
    path and could serve stale state to a reused (in-process / web-server) worker.

    Fields:
    - ``cdf``: weighted product-sampling CDFs, keyed by (pool, brand_or_merged,
      month). Intersections + CDFs depend only on store assortment / brand
      buckets / product weights.
    - ``eligible``: per-month eligible customer row indices, keyed by month count
      ``T``. The eligibility predicate depends only on the (worker-invariant)
      lifecycle arrays; the discovery schedule does NOT enter it, so caching by
      ``T`` alone is safe.
    - ``prodweight``: per-relative-month product weights. Temporal category,
      brand, and color factors make calendar month alone an incomplete key.
    - ``month_pool``: per-month distinct-customer ``(pool, cdf)``, keyed by month
      offset alone (per CLAUDE.md gotcha #27, ``m`` fully determines the value
      given the worker-constant inputs).
    - ``brand_flat_ref`` / ``brand_flat_data``: flat brand-index + offsets, guarded
      by an identity check against the source ``brand_to_rows``.
    - ``delta_pcols``: the Delta partition-column set read once from State.
    """

    __slots__ = (
        "cdf", "eligible", "prodweight", "month_pool",
        "brand_flat_ref", "brand_flat_data", "delta_pcols",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.cdf: dict = {}
        self.eligible: dict = {}
        self.prodweight: dict = {}
        self.month_pool: dict = {}
        self.brand_flat_ref = None
        self.brand_flat_data = None
        self.delta_pcols = None


_CACHES = _WorkerCaches()


def reset_worker_caches() -> None:
    """Clear every worker-lifetime chunk-builder cache (called once per worker init)."""
    _CACHES.reset()
    reset_basket_cache()


# Backwards-compatible alias for the previous (narrower) name.
reset_worker_cdf_cache = reset_worker_caches


def _delta_partition_cols() -> set[str]:
    if _CACHES.delta_pcols is None:
        raw = getattr(State, "partition_cols", None)
        _CACHES.delta_pcols = set(raw) if isinstance(raw, (list, tuple)) and raw else {"Year", "Month"}
    return _CACHES.delta_pcols


def _eligible_idx_by_month(T, is_active, start_month, end_month_norm) -> list:
    """Per-month eligible customer row indices, cached for the worker's lifetime.

    Returns the flatnonzero indices directly and memoizes them.  The lifecycle
    arrays are bound once per worker (and this cache is cleared at worker init),
    so the result is invariant across the chunks a worker processes — keyed on
    the month count T alone.
    """
    T = int(T)
    cached = _CACHES.eligible.get(T)
    if cached is not None:
        return cached

    idx_by_month = EligibilityWindow.from_lifecycle(
        is_active, start_month, end_month_norm
    ).indices_by_month(T)
    _CACHES.eligible[T] = idx_by_month
    return idx_by_month


def _get_month_pool(
    m_offset, distinct_target, eligible_idx, customer_keys,
    discovery_month, base_weight, end_month_norm, plan_seed, month_start_day,
):
    """Return the cached ``(pool, cdf)`` for month ``m`` (build once per worker).

    The pool is deterministic in ``(m, plan_seed, static customer arrays)`` so it
    is identical for every chunk and worker — caching by month offset alone is
    correct (CLAUDE.md gotcha #27) and makes per-chunk customer assignment cheap.
    """
    m = int(m_offset)
    cached = _CACHES.month_pool.get(m)
    if cached is not None:
        return cached
    effective_weight = effective_customer_weights_for_month(
        m_offset=m,
        month_start_day=int(month_start_day),
        eligible_idx=eligible_idx,
        base_weight=base_weight,
        country_id_by_pool=State.customer_country_id_by_pool,
        gender_id_by_pool=State.customer_gender_id_by_pool,
        country_factors=State.customer_country_factors,
        gender_factors=State.customer_gender_factors,
        customer_scd2_starts=(
            State.customer_scd2_starts if State.customer_scd2_active else None
        ),
        customer_scd2_geography=(
            State.customer_scd2_geography if State.customer_scd2_active else None
        ),
        geo_to_country_id=(
            State.geo_to_customer_country_id if State.customer_scd2_active else None
        ),
    )
    pool, cdf = build_month_customer_pool(
        m_offset=m,
        distinct_target=int(distinct_target),
        eligible_idx=eligible_idx,
        customer_keys=customer_keys,
        discovery_month=discovery_month,
        base_weight=effective_weight,
        end_month_norm=end_month_norm,
        seed=int(plan_seed),
        key_to_pool_idx=State.cust_key_to_pool_idx,
    )
    _CACHES.month_pool[m] = (pool, cdf)
    return pool, cdf


def _get_brand_flat_cache(brand_to_rows: list, B: int) -> tuple:
    """Return (flat_idx, offsets) for brand_to_rows, caching across calls."""
    if _CACHES.brand_flat_ref is brand_to_rows and _CACHES.brand_flat_data is not None:
        return _CACHES.brand_flat_data

    flat_parts = []
    lengths = np.zeros(B, dtype=np.int64)
    for b, bucket in enumerate(brand_to_rows):
        if bucket is not None and len(bucket) > 0:
            flat_parts.append(np.asarray(bucket, dtype=np.int32))
            lengths[b] = len(bucket)
    offsets = np.zeros(B + 1, dtype="int64")
    np.cumsum(lengths, out=offsets[1:])
    flat_idx = np.concatenate(flat_parts) if flat_parts else np.empty(0, dtype=np.int32)
    _CACHES.brand_flat_ref = brand_to_rows
    _CACHES.brand_flat_data = (flat_idx, offsets)
    return flat_idx, offsets


def _get_brand_probs(m_offset: int, B: int) -> np.ndarray | None:
    """Return normalized brand probability vector for month *m_offset*, or None."""
    bp = getattr(State, "brand_prob_by_month", None)
    if bp is not None:
        bp = np.asarray(bp, dtype="float64")
        cand = bp[int(m_offset) % bp.shape[0]] if bp.ndim > 1 else bp
        probs = normalize_prob(cand, on_zero="none")
        if probs is not None and int(probs.size) == B:
            return probs
    return None


# Counter-based product-sampling salts. Each per-line product draw is a pure
# hash of the line's (OrderNumber, OrderLineNumber) entity key instead of a
# sequential chunk-RNG draw, so a line's sampled product is a function of its
# identity alone -- independent of chunk_size, worker count, group-iteration
# order, or any other stage's draw count. Three distinct salts give the brand
# choice, the within-brand / within-pool product pick, and the uniform-catalog
# fallback three independent uniforms off the same key.
_PRODUCT_BRAND_SALT = np.uint64(0x9D0DC0DE00000001)
_PRODUCT_PICK_SALT = np.uint64(0x9D0DC0DE00000002)
_PRODUCT_FALLBACK_SALT = np.uint64(0x9D0DC0DE00000003)
_SEGMENT_CHANNEL_ROUTE_SALT = np.uint64(0x5E6D3E1700000001)
_SEGMENT_CHANNEL_SPLIT_SALT = np.uint64(0x5E6D3E1700000002)
_SEGMENT_PRODUCT_CANDIDATE_SALT = np.uint64(0x5E6D3E1700000003)
_SEGMENT_PRODUCT_SWITCH_SALT = np.uint64(0x5E6D3E1700000004)
_PRODUCT_ELIGIBILITY_SALT = np.uint64(0x5E6D3E1700000100)


def _line_uniform(rng, salt, order_numbers, line_numbers, count, idx=None):
    """Per-line uniform in ``[0, 1)`` for one product-sampling draw.

    Counter-based off the line entity key ``(OrderNumber, OrderLineNumber)`` when
    order identifiers exist, so the value is a pure function of the line's
    identity (independent of chunk_size, worker count, and group-iteration
    order). Under ``skip_order_cols`` there is no per-line key, so it falls back
    to a sequential chunk-RNG draw. ``idx`` selects a subset of the line
    positions (for group-wise sampling); ``count`` is the number of draws and
    must equal ``len(idx)`` when ``idx`` is given.
    """
    if order_numbers is not None and line_numbers is not None:
        if idx is None:
            return hash_u01(salt, order_numbers, line_numbers)
        return hash_u01(salt, order_numbers[idx], line_numbers[idx])
    return rng.random(count)


def _uniform_index(u, k):
    """Map uniforms ``u`` in ``[0, 1)`` to integer indices in ``[0, k)``.

    The counter-based counterpart of ``rng.integers(0, k)``. ``k`` may be a
    scalar (uniform over a fixed range) or a per-element array of bucket sizes.
    """
    idx = (np.asarray(u, dtype=np.float64) * k).astype(np.int64)
    return np.minimum(idx, np.asarray(k, dtype=np.int64) - 1)


def _dense_lookup(lut, keys, default=0.0):
    """Safely index a dense CustomerKey lookup without allowing wraparound."""
    if lut is None:
        return np.full(len(keys), default)
    _lut = np.asarray(lut)
    _keys = np.asarray(keys, dtype=np.int64)
    _out = np.full(len(_keys), default, dtype=_lut.dtype)
    _valid = (_keys >= 0) & (_keys < len(_lut))
    _out[_valid] = _lut[_keys[_valid]]
    return _out


def _apply_organization_channel_routing(
    rng,
    channel_keys,
    customer_keys,
    *,
    order_numbers,
    order_starts,
    order_idx,
    business_channel_share,
    b2b_channel_share,
):
    """Route a configured share of organization orders to channels 4/9.

    The choice is order-grain and counter-based when OrderNumber exists. Store
    correlation lookups declare these account-level channels valid everywhere
    with zero consumer weight, so this explicit opt-in is their only path outside
    Warehouse stores.
    """
    _is_org_lut = getattr(State, "customer_is_organization_by_key", None)
    if _is_org_lut is None or business_channel_share <= 0.0:
        return channel_keys

    _channel = np.asarray(channel_keys, dtype=np.int16).copy()
    if order_starts is not None and order_idx is not None:
        _cust = np.asarray(customer_keys)[order_starts]
        _keys = np.asarray(order_numbers)[order_starts] if order_numbers is not None else None
        _is_org = _dense_lookup(_is_org_lut, _cust, 0).astype(bool)
        _scale = _dense_lookup(
            getattr(State, "customer_org_scale_score_by_key", None), _cust, 0.5,
        ).astype(np.float64)
        _proc = _dense_lookup(
            getattr(State, "customer_org_procurement_score_by_key", None), _cust, 0.5,
        ).astype(np.float64)
        _propensity = np.clip(1.0 + 0.30 * (_scale - 0.5) + 0.20 * (_proc - 0.5), 0.75, 1.25)
        _route_p = np.clip(float(business_channel_share) * _propensity, 0.0, 1.0)
        _route_u = (
            hash_u01(_SEGMENT_CHANNEL_ROUTE_SALT, _keys)
            if _keys is not None else rng.random(len(_cust))
        )
        _split_u = (
            hash_u01(_SEGMENT_CHANNEL_SPLIT_SALT, _keys)
            if _keys is not None else rng.random(len(_cust))
        )
        _route = _is_org & (_route_u < _route_p)
        _order_channel = _channel[order_starts].copy()
        _order_channel[_route] = np.where(
            _split_u[_route] < float(b2b_channel_share), 4, 9,
        ).astype(np.int16)
        return _order_channel[order_idx]

    _cust = np.asarray(customer_keys)
    _is_org = _dense_lookup(_is_org_lut, _cust, 0).astype(bool)
    _route = _is_org & (rng.random(len(_cust)) < float(business_channel_share))
    _channel[_route] = np.where(
        rng.random(int(_route.sum())) < float(b2b_channel_share), 4, 9,
    ).astype(np.int16)
    return _channel


def _apply_segment_product_preferences(
    rng,
    prod_idx,
    customer_keys,
    product_weight,
    *,
    order_numbers,
    line_numbers,
    income_strength,
    organization_category_strength,
):
    """Use a deterministic candidate tournament for segment preferences.

    Income pulls individuals modestly toward similarly ranked catalog prices.
    Organization industries use explicit, interpretable product-category
    affinities. Only a better candidate can replace the baseline choice, and the
    switch probability rises smoothly with score advantage, preserving
    substantial segment overlap.
    """
    _price_score = getattr(State, "product_price_score", None)
    if _price_score is None or len(prod_idx) == 0:
        return prod_idx
    _income_lut = getattr(State, "customer_income_score_by_key", None)
    _org_lut = getattr(State, "customer_is_organization_by_key", None)
    _category_mask_lut = getattr(
        State, "customer_org_preferred_category_mask_by_key", None,
    )
    _product_category = getattr(State, "product_category_key", None)
    if income_strength <= 0.0 and (
        organization_category_strength <= 0.0 or _product_category is None
    ):
        return prod_idx

    _n = len(prod_idx)
    if product_weight is not None and float(np.asarray(product_weight).sum()) > 1e-12:
        _candidate_cdf = cdf_from_probs(
            np.asarray(product_weight, dtype=np.float64)
            / float(np.asarray(product_weight, dtype=np.float64).sum())
        )
        _candidate_u = _line_uniform(
            rng, _SEGMENT_PRODUCT_CANDIDATE_SALT,
            order_numbers, line_numbers, _n,
        )
        _candidate = pick_from_cdf(_candidate_cdf, _candidate_u).astype(np.int32)
    else:
        _candidate_u = _line_uniform(
            rng, _SEGMENT_PRODUCT_CANDIDATE_SALT,
            order_numbers, line_numbers, _n,
        )
        _candidate = _uniform_index(_candidate_u, len(_price_score)).astype(np.int32)

    _cust = np.asarray(customer_keys, dtype=np.int32)
    _income = _dense_lookup(_income_lut, _cust, 0.0).astype(np.float64)
    _is_org = _dense_lookup(_org_lut, _cust, 0).astype(bool)
    _price_score = np.asarray(_price_score, dtype=np.float64)
    _current_score = float(income_strength) * _income * _price_score[prod_idx]
    _candidate_score = float(income_strength) * _income * _price_score[_candidate]

    if (
        organization_category_strength > 0.0
        and _product_category is not None
        and _category_mask_lut is not None
    ):
        _preferred_mask = _dense_lookup(
            _category_mask_lut, _cust, 0,
        ).astype(np.uint32)
        _has_preference = _is_org & (_preferred_mask != 0)
        _product_category = np.asarray(_product_category, dtype=np.int64)

        def _category_matches(_row_idx):
            _category = _product_category[_row_idx]
            _valid = (_category >= 1) & (_category <= 32)
            _bit = np.zeros(len(_category), dtype=np.uint32)
            _bit[_valid] = np.left_shift(
                np.uint32(1), (_category[_valid] - 1).astype(np.uint32),
            )
            return (_preferred_mask & _bit) != 0

        _current_match = _category_matches(prod_idx)
        _candidate_match = _category_matches(_candidate)
        _current_score += (
            float(organization_category_strength) * _has_preference * _current_match
        )
        _candidate_score += (
            float(organization_category_strength) * _has_preference * _candidate_match
        )

    _advantage = np.maximum(_candidate_score - _current_score, 0.0)
    _switch_p = np.tanh(_advantage)
    _switch_u = _line_uniform(
        rng, _SEGMENT_PRODUCT_SWITCH_SALT,
        order_numbers, line_numbers, _n,
    )
    _switch = _switch_u < _switch_p
    if _switch.any():
        _out = np.asarray(prod_idx, dtype=np.int32).copy()
        _out[_switch] = _candidate[_switch]
        return _out
    return prod_idx


def _organization_quantity_multiplier(customer_keys, organization_cfg):
    """Build a deterministic per-line organization quantity multiplier."""
    _out = np.ones(len(customer_keys), dtype=np.float64)
    if not organization_cfg or not organization_cfg.get("enabled", False):
        return _out
    _cust = np.asarray(customer_keys, dtype=np.int32)
    _is_org = _dense_lookup(
        getattr(State, "customer_is_organization_by_key", None), _cust, 0,
    ).astype(bool)
    if not _is_org.any():
        return _out
    _scale = _dense_lookup(
        getattr(State, "customer_org_scale_score_by_key", None), _cust, 0.5,
    ).astype(np.float64)
    _proc = _dense_lookup(
        getattr(State, "customer_org_procurement_score_by_key", None), _cust, 0.5,
    ).astype(np.float64)
    _mult = np.full(
        len(_cust), float(organization_cfg.get("quantity_multiplier", 1.0)),
        dtype=np.float64,
    )
    _mult *= 1.0 + float(organization_cfg.get("company_scale_strength", 0.0)) * (_scale - 0.5)
    _mult *= 1.0 + float(organization_cfg.get("procurement_strength", 0.0)) * (_proc - 0.5)
    _out[_is_org] = np.clip(_mult[_is_org], 1.0, 4.0)
    return _out


def _sample_product_row_indices(
    rng: np.random.Generator,
    n: int,
    product_np: np.ndarray,
    *,
    m_offset: int,
    enabled: bool,
    product_weight: np.ndarray | None = None,
    order_numbers: np.ndarray | None = None,
    line_numbers: np.ndarray | None = None,
) -> np.ndarray:
    """
    Return row indices into `product_np` for n orders.

    Brand-first sampling is used iff:
      - enabled == True (controlled by models_cfg.brand_popularity presence), AND
      - State provides brand buckets and optionally per-month brand probabilities.

    When product_weight is provided, within-brand selection is weighted by
    popularity+seasonality instead of uniform.  If brand sampling is disabled,
    product_weight is used directly for weighted global sampling.

    Draws are counter-based: each line's brand choice, within-brand pick, and
    uniform fallback are hashed on its ``(OrderNumber, OrderLineNumber)`` entity
    key (``order_numbers``/``line_numbers``), so the sampled product is a pure
    function of the line's identity. Under ``skip_order_cols`` those keys are
    ``None`` and the draws fall back to the chunk RNG.

    Safe fallback:
      - If anything is missing/invalid -> uniform sampling over product_np.
    """
    n_int = int(n)
    n_products = len(product_np)

    if not enabled:
        if product_weight is not None:
            ws = float(product_weight.sum())
            if ws > 1e-12:
                u = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, n_int)
                return pick_from_cdf(cdf_from_probs(product_weight / ws), u).astype("int32", copy=False)
        u = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, n_int)
        return _uniform_index(u, n_products).astype("int32", copy=False)

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
        u = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, n_int)
        return _uniform_index(u, n_products).astype("int32", copy=False)

    B = int(len(brand_to_rows))
    p = _get_brand_probs(m_offset, B)

    # Fallback: equal probability across brands with at least 1 SKU
    if p is None:
        avail = np.asarray([len(x) > 0 for x in brand_to_rows], dtype="float64")
        if float(avail.sum()) <= 0:
            u = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, n_int)
            return _uniform_index(u, n_products).astype("int32", copy=False)
        p = avail / float(avail.sum())

    u_brand = _line_uniform(rng, _PRODUCT_BRAND_SALT, order_numbers, line_numbers, n_int)
    brand_ids = pick_from_cdf(cdf_from_probs(p), u_brand)

    # Build flat index array + offsets once and cache at module level (deterministic
    # per worker).  Avoids rebuilding from brand_to_rows (up to 300K brands) per call.
    flat_idx, offsets = _get_brand_flat_cache(brand_to_rows, B)

    if offsets[-1] == 0:
        u = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, n_int)
        return _uniform_index(u, n_products).astype("int32", copy=False)

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
                u = _line_uniform(rng, _PRODUCT_FALLBACK_SALT, order_numbers, line_numbers, cnt, idx=orig_idx)
                out[orig_idx] = _uniform_index(u, n_products).astype("int32", copy=False)
                continue
            rows = flat_idx[start:end]
            w = product_weight[rows]
            ws = float(w.sum())
            u = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, cnt, idx=orig_idx)
            if ws > 1e-12:
                picks = pick_from_cdf(cdf_from_probs(w / ws), u)
            else:
                picks = _uniform_index(u, len(rows))
            out[orig_idx] = rows[picks]
        return out

    u_within = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, n_int)
    rand_within = _uniform_index(u_within, sizes_per_order)

    abs_idx = offsets[brand_ids] + rand_within
    out = flat_idx[abs_idx]

    # Fix up any rows that landed on empty buckets with uniform fallback
    if empty_mask.any():
        _em_idx = np.flatnonzero(empty_mask)
        u = _line_uniform(rng, _PRODUCT_FALLBACK_SALT, order_numbers, line_numbers, _em_idx.size, idx=_em_idx)
        out[empty_mask] = _uniform_index(u, n_products).astype("int32", copy=False)

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
    order_numbers: np.ndarray | None = None,
    line_numbers: np.ndarray | None = None,
) -> np.ndarray:
    """
    Sample product row indices from each store's assortment pool.

    When brand popularity is enabled, orders first pick a brand via
    ``brand_prob_by_month``, then sample within the intersection of
    store pool and brand bucket.  Otherwise sampling is weighted by
    ``product_weight`` (popularity) within each store's full pool.

    Performance: per-store CDFs are cached by relative month because temporal
    factors differ across absolute months. The store key is semantic (the
    assortment pool is worker-constant), avoiding ``id(pool)`` reuse hazards.

    The per-line product pick is counter-based, hashed on the line entity key
    ``(order_numbers, line_numbers)``; the store grouping only reorders which
    lines a pick is computed for, never the value. Under ``skip_order_cols`` the
    keys are ``None`` and the draws fall back to the chunk RNG.
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
        brand_to_rows = getattr(State, "brand_to_row_idx", None)
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
                    brand_probs, B, product_weight, _cdf_cache, m_offset,
                    int(_sk_list[i]),
                    order_numbers, line_numbers,
                )
            elif _has_weight and pool_sz > 1:
                cache_key = (int(_sk_list[i]), -1, int(m_offset))
                cached = _cache_get(cache_key) if _cache_get is not None else None
                if cached is not None:
                    _, cdf = cached
                else:
                    w = product_weight[pool]
                    cdf = _normalize_cdf(w)
                    if _cache_get is not None:
                        _cdf_cache[cache_key] = (pool, cdf)

                total = float(cdf[-1]) if cdf.size > 0 else 0.0
                u = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, count, idx=orig_idx)
                if total > 1e-12:
                    out[orig_idx] = pool[pick_from_cdf(cdf, u)]
                else:
                    out[orig_idx] = pool[_uniform_index(u, pool_sz)]
            else:
                u = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, count, idx=orig_idx)
                out[orig_idx] = pool[_uniform_index(u, pool_sz)]
        else:
            u = _line_uniform(rng, _PRODUCT_FALLBACK_SALT, order_numbers, line_numbers, count, idx=orig_idx)
            if _global_p is not None:
                out[orig_idx] = pick_from_cdf(cdf_from_probs(_global_p), u)
            else:
                out[orig_idx] = _uniform_index(u, n_products)

    return out


def _sample_brand_aware(
    rng, out, orig_idx, count, pool, brand_probs, B,
    product_weight, _cdf_cache, m_offset, store_key,
    order_numbers=None, line_numbers=None,
):
    """Sample products using merged brand × popularity CDF (no brand loop).

    Builds a single per-store CDF where each product's weight incorporates
    its brand probability: w(p) = brand_prob[brand_of_p] * product_weight[p].
    One vectorized inverse-CDF pick per store instead of looping over ~200
    brands.  Produces the same marginal distribution as the two-stage
    "pick brand, then pick within brand" approach.

    The pick is counter-based, hashed on the line entity key
    ``(order_numbers, line_numbers)`` (``None`` under skip_order_cols -> chunk
    RNG); grouping lines by store never changes a line's value.

    The merged CDF depends on ``brand_probs``, which is the *absolute*-month
    brand mix (``brand_prob_by_month[m_offset]``), so the worker-lifetime cache
    MUST be keyed by ``m_offset`` — not the calendar month. Keying by calendar
    month lets the first chunk a worker happens to run fix the CDF for every
    later same-calendar-month absolute month (e.g. Jan-2023 reusing Jan-2022's
    brand mix), which made product sampling depend on chunk→worker scheduling.
    """
    # Semantic key: the store's assortment pool is worker-constant, so keying by
    # store_key (not id(pool)) is stable and cannot collide on a reused address.
    merged_key = (int(store_key), "_merged_", int(m_offset))

    if _cdf_cache is not None and merged_key in _cdf_cache:
        merged_pool, merged_cdf = _cdf_cache[merged_key]
    else:
        # Build combined weight: brand_prob[brand] * product_weight[product]
        product_brand_key = getattr(State, "product_brand_key", None)
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
        u = _line_uniform(rng, _PRODUCT_PICK_SALT, order_numbers, line_numbers, count, idx=orig_idx)
        out[orig_idx] = merged_pool[pick_from_cdf(merged_cdf, u)]
    else:
        u = _line_uniform(rng, _PRODUCT_FALLBACK_SALT, order_numbers, line_numbers, count, idx=orig_idx)
        out[orig_idx] = pool[_uniform_index(u, len(pool))]




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
    """Build popularity, seasonality, and hierarchical temporal weights.

    Category is the top-level layer; category-qualified brand and color are
    residual layers centered within category by the coordinator. The combined
    temporal factor is centered again over this month's base catalog weight.
    """
    pop = getattr(State, "product_popularity", None)
    sea = getattr(State, "product_seasonality", None)
    category_ids = np.asarray(State.product_category_id_by_pool, dtype=np.int32)
    brand_ids = np.asarray(State.product_brand_id_by_pool, dtype=np.int32)
    color_ids = np.asarray(State.product_color_id_by_pool, dtype=np.int32)
    month = int(m_offset)

    # Every dependency other than relative month is immutable worker State, so
    # m_offset is the complete cache key. January in different years must not
    # share a product vector.
    cached = _CACHES.prodweight.get(month)
    if cached is not None:
        return cached

    if pop is None:
        w = np.ones(category_ids.size, dtype=np.float64)
    else:
        w = np.maximum(np.asarray(pop, dtype=np.float64), 1.0)

    # Seasonal boost for this calendar month
    if sea is not None and cal_month > 0:
        for season, boosts in _SEASON_SALES_BOOST.items():
            if cal_month in boosts:
                code = _SEASON_CODE[season]
                mask = sea == code
                if mask.any():
                    w[mask] *= 1.0 + boosts[cal_month]

    category_factors = np.asarray(State.product_category_factors, dtype=np.float64)
    brand_factors = np.asarray(State.product_brand_factors, dtype=np.float64)
    color_factors = np.asarray(State.product_color_factors, dtype=np.float64)
    combined = (
        category_factors[month, category_ids]
        * brand_factors[month, brand_ids]
        * color_factors[month, color_ids]
    )
    weighted_mean = float(np.dot(w, combined) / w.sum())
    if not np.isfinite(weighted_mean) or weighted_mean <= 0.0:
        raise SalesError("Product temporal factor normalization is invalid")
    w *= combined / weighted_mean
    if not np.all(np.isfinite(w)) or np.any(w <= 0.0):
        raise SalesError("Effective product weights must be finite and positive")

    _CACHES.prodweight[month] = w
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

    months_int = month_offsets_int(date_pool)
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

    Thin view over :class:`EligibilityWindow` (the single-source predicate).
    """
    return EligibilityWindow.from_lifecycle(
        is_active_in_sales, start_month, end_month_norm
    ).counts(T)


def _to_pa_array(name: str, data: object, n_rows: int, schema_types: dict) -> pa.array:
    """Convert a column's numpy/scalar data to a typed pa.Array."""
    t = schema_types[name]

    if data is None:
        return pa.nulls(n_rows, type=t)

    # Broadcast scalars
    if np.isscalar(data):
        data = np.full(n_rows, data)

    # date32: build directly from days-since-epoch (datetime64[D] stores exactly
    # that), skipping the datetime64[ns] multiply + downcast round-trip.  This is
    # also robust to dates beyond the ns-representable range (post-2262).
    if pa.types.is_date32(t):
        dt = np.asarray(_as_datetime64_D(data), dtype="datetime64[D]")
        isnat = np.isnat(dt)
        days = dt.astype("int64")
        if isnat.any():
            days = np.where(isnat, 0, days)  # null-masked; value irrelevant
            return pa.array(days.astype("int32"), type=t, mask=isnat)
        return pa.array(days.astype("int32"), type=t)

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


def _weighted_choice_idx(
    rng: np.random.Generator,
    weights: np.ndarray,
    size: int,
) -> np.ndarray:
    """Sample `size` indices into `weights` with replacement, proportional to weights.

    Inverse-CDF via searchsorted — substantially faster than
    ``rng.choice(n, size=size, p=...)`` for repeated weighted sampling. Statistically
    identical to ``rng.choice`` with replacement; only the consumed RNG stream differs.
    Callers must ensure the weights sum is positive (see ``_normalize_cdf``).
    """
    cdf = _normalize_cdf(weights)
    return np.searchsorted(cdf, rng.random(size), side="right")


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
                    all_picked = emp2[_weighted_choice_idx(rng, w2, total_count)]

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
                    picked = emp2[_weighted_choice_idx(rng, w2, count)]
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

def _weighted_pick(rng, pool, store_weight, k):
    """Pick *k* stores from *pool* with probability ∝ store_weight[StoreKey]."""
    if k <= 0:
        return np.empty(0, dtype=np.int32)
    pool = np.asarray(pool, dtype=np.int32)
    if pool.size == 0:
        return np.empty(0, dtype=np.int32)
    w = store_weight[np.clip(pool, 0, len(store_weight) - 1)].astype(np.float64)
    idx = weighted_pick_indices(rng, w, k, pool.size)
    return pool[idx].astype(np.int32)


def _apply_geo_bias_store_sampling(
    rng, skip_cols, n_unique_orders, n_lines,
    customer_keys_for_orders, customer_keys_out,
    month_stores, order_idx,
    cust_geo, geo2c, c2sk, store_weight,
):
    """Sample stores with geographic bias toward the customer's home country,
    weighted by per-store demand (``store_weight``).

    70% of orders draw from the customer's country pool, 30% globally; each pick
    is proportional to ``store_weight``, so bigger / higher-revenue stores sell
    more. ``store_weight`` is always present — all-ones means uniform (when
    sales.store_demand_weight is unconfigured), so there is no separate uniform
    code path. Geo-bias needs the cust->geo (``cust_geo``), geo->country
    (``geo2c``) and country->store-key (``c2sk``) maps; without them it falls
    back to a single weighted pool over ``month_stores``.
    """
    geo_bias = (cust_geo is not None and geo2c is not None and c2sk is not None)
    if not skip_cols:
        n = n_unique_orders
        cust = customer_keys_for_orders
    else:
        n = n_lines
        cust = customer_keys_out

    if not geo_bias:
        order_store = _weighted_pick(rng, month_stores, store_weight, n)
    else:
        _ck = np.clip(np.asarray(cust, dtype=np.int32), 0, len(cust_geo) - 1)
        cust_countries = geo2c[np.clip(cust_geo[_ck], 0, len(geo2c) - 1)]
        use_local = rng.random(n) < 0.70
        month_set = set(month_stores.tolist())
        order_store = np.empty(n, dtype=np.int32)
        # global component (30%)
        gmask = ~use_local
        order_store[gmask] = _weighted_pick(rng, month_stores, store_weight, int(gmask.sum()))
        # local component (70%): weighted within each customer country's open pool
        lidx = np.where(use_local)[0]
        lc = cust_countries[lidx]
        for _cid in np.unique(lc):
            cid = int(_cid)
            csk = c2sk[cid] if cid < len(c2sk) else np.array([], dtype=np.int32)
            pool = csk[np.fromiter((int(s) in month_set for s in csk), dtype=bool, count=len(csk))] if csk.size else csk
            if pool.size == 0:
                pool = month_stores  # no local store open -> fall back to global pool
            sel = lidx[lc == _cid]
            order_store[sel] = _weighted_pick(rng, pool, store_weight, sel.size)

    if not skip_cols:
        return order_store[order_idx]
    return order_store


def _resample_stores_for_open_close(
    rng, store_key_arr, order_dates,
    store_open_day, store_close_day,
    store_keys, order_idx,
    store_weight,
    *,
    store_reno_start_day=None, store_reno_end_day=None,
):
    """Resample stores whose order date falls outside their open/close window
    OR inside their renovation window. Replacement candidates are stores that
    are open AND not currently renovating on the order date.

    Replacements are drawn in proportion to per-store demand (``store_weight``,
    all-ones = uniform), matching the initial sampler so boundary resampling
    doesn't dilute the demand weighting."""
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
            _repls = _weighted_pick(rng, _day_stores, store_weight, len(_uniq_oids))
            store_key_arr[_date_rows] = _repls[_oid_inv]
        else:
            store_key_arr[_date_rows] = _weighted_pick(rng, _day_stores, store_weight, len(_date_rows))
    return store_key_arr


def _apply_store_channel_correlation(
    rng, store_key_arr, skip_cols,
    order_idx, order_starts, n_unique_orders, n_lines,
    store_ch_keys, ch_prob_by_store,
    order_numbers=None,
):
    """Sample ChannelKey per order/line based on store type channel affinity.

    The per-order channel draw is counter-based, hashed on the order entity key
    (``order_numbers``); under skip_order_cols there is no per-order key and the
    per-line draw falls back to the chunk RNG.
    """
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
            _ch_cdf_2d[_sk_i, :_nc] = cdf_from_probs(_cp)
        else:
            _ch_cdf_2d[_sk_i, :_nc] = np.arange(1, _nc + 1, dtype=np.float64) / _nc
        if _nc < _max_n_ch:
            _ch_cdf_2d[_sk_i, _nc:] = 1.0

    def _sample_channels_vectorized(_store_arr, _n, _keys):
        # Counter-based per-order uniform when an entity key is available; else the
        # chunk RNG (skip_order_cols per-line path).
        _r = hash_u01(_CHANNEL_SALT, _keys) if _keys is not None else rng.random(_n)
        _sk_clp = np.clip(_store_arr.astype(np.int64), 0, _max_sk_ch - 1)
        _row_cdf = _ch_cdf_2d[_sk_clp]
        _ch_idx = np.argmax(_r[:, None] < _row_cdf, axis=1)
        return _ch_keys_2d[_sk_clp, _ch_idx]

    if not skip_cols and order_idx is not None:
        _order_stores_for_ch = store_key_arr[order_starts] if order_starts is not None else store_key_arr[:n_unique_orders]
        _order_keys = (
            order_numbers[order_starts] if (order_numbers is not None and order_starts is not None)
            else None
        )
        _ch_per_order = _sample_channels_vectorized(_order_stores_for_ch, n_unique_orders, _order_keys)
        return _ch_per_order[order_idx]
    return _sample_channels_vectorized(store_key_arr, n_lines, None)


def _enforce_channel_product_eligibility(
    rng, prod_idx, sales_channel_key_arr,
    pce, ch2eg, product_weight,
    order_numbers=None, line_numbers=None,
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
            _bad_positions = np.flatnonzero(_eg_mask)
            _salt = np.uint64(
                (int(_PRODUCT_ELIGIBILITY_SALT) + _pass * 17 + int(_eg))
                & 0xFFFFFFFFFFFFFFFF
            )
            _u = _line_uniform(
                rng, _salt, order_numbers, line_numbers, _n_eg,
                idx=_bad_positions,
            )
            if _ews > 1e-12:
                _picks = pick_from_cdf(cdf_from_probs(_ew / _ews), _u)
            else:
                _picks = _uniform_index(_u, len(_eligible_rows))
            prod_idx[_eg_mask] = _eligible_rows[_picks]
    return prod_idx


def _apply_new_customer_promo(
    promo_keys, order_dates, customer_keys_out,
    sales_channel_key_arr,
    ckey_to_start_day, ckey_to_deadline_day,
    nc_keys, nc_starts, nc_ends,
    nc_promo_set,
    no_discount_key,
    promo_channel_group, promo_keys_all,
):
    """Strip invalid NC assignments and use exact calendar-date eligibility."""
    if (
        ckey_to_start_day is None
        or ckey_to_deadline_day is None
        or nc_keys is None
        or nc_keys.size == 0
    ):
        return promo_keys

    order_dates_D = order_dates.astype("datetime64[D]", copy=False)
    order_day = order_dates_D.astype(np.int64)
    cust_start_day = ckey_to_start_day[customer_keys_out]
    cust_deadline_day = ckey_to_deadline_day[customer_keys_out]
    known_start = cust_start_day != np.iinfo(np.int64).min
    is_new = (
        known_start
        & (order_day >= cust_start_day)
        & (order_day <= cust_deadline_day)
    )

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
def _compute_order_grouping(skip_cols, line_num):
    """Group lines into orders.

    Returns ``(order_starts, order_idx, n_unique_orders)`` — the line-1 mask, the
    0-based order index per line, and the unique-order count — or ``(None, None,
    0)`` when there are no order identifiers (skip_order_cols). Pure, no RNG.
    """
    if not skip_cols and line_num is not None:
        order_starts = (np.asarray(line_num) == 1)
        order_idx = np.cumsum(order_starts.astype(np.int64)) - 1
        n_unique_orders = int(order_idx.max() + 1) if order_idx.size else 0
    else:
        order_starts = None
        order_idx = None
        n_unique_orders = 0
    return order_starts, order_idx, n_unique_orders


def _resolve_row_prices(prod_idx, order_epoch_days, product_np):
    """Resolve per-line ProductKey / UnitPrice / UnitCost.

    With product SCD2 active, pick the version effective on each line's OrderDate
    (last version whose start <= order day); otherwise read straight from the
    current-version ``product_np``. Pure lookup, no RNG.
    """
    _pscd2_starts = getattr(State, "product_scd2_starts", None)
    _pscd2_data = getattr(State, "product_scd2_data", None)
    if getattr(State, "product_scd2_active", False) and _pscd2_starts is not None and _pscd2_data is not None:
        # For each sale: find the last version where start <= order_day
        _p_starts = _pscd2_starts[prod_idx]         # (n_lines, max_ver)
        _ver_idx = np.sum(_p_starts <= order_epoch_days[:, np.newaxis], axis=1) - 1
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
    return product_keys, unit_price, unit_cost


def _remap_customer_scd2(customer_keys_out, order_epoch_days):
    """SCD2: remap the IsCurrent CustomerKey to the version-specific key
    effective on each line's OrderDate.

    Returns ``customer_keys_out`` unchanged when customer SCD2 is inactive or no
    key resolves to a pool index. Pure lookup, no RNG.
    """
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
            _v_days = order_epoch_days[_valid]
            _c_starts = _cscd2_starts[_v_pool]           # (N_valid, max_ver)
            _ver_idx = np.sum(_c_starts <= _v_days[:, np.newaxis], axis=1) - 1
            _ver_idx = np.clip(_ver_idx, 0, _c_starts.shape[1] - 1)
            _c_keys = _cscd2_keys[_v_pool]               # (N_valid, max_ver)
            _row_range = np.arange(len(_v_pool))
            _remapped = np.array(customer_keys_out, dtype=np.int32, copy=True)
            _remapped[_valid] = _c_keys[_row_range, _ver_idx]
            customer_keys_out = _remapped
    return customer_keys_out


def _compute_day_based_order_ids(
    order_dates, line_num, reps, date_pool_min_day,
    day_stride, per_chunk_alloc, chunk_idx, order_id_int64, int32_max,
):
    """Assign day-banded OrderNumbers (line-level, via repeat).

    Each calendar day gets a disjoint id band and each chunk a non-overlapping
    slot within it, so OrderNumber increases monotonically with OrderDate even
    across parallel chunks. Pure id arithmetic, no RNG. Emits the schema dtype
    (int64 for large runs; int32 otherwise, with a loud overflow guard).
    """
    _od_D = order_dates[line_num == 1].astype("datetime64[D]")
    _d_off = _od_D.astype(np.int64) - date_pool_min_day

    # CHUNK-1: the customer-start clamp runs AFTER build_orders sorted by date,
    # so _d_off is no longer guaranteed sorted. Derive each order's within-day
    # rank from a stable sort (see helper) so the cursor is correct regardless of
    # clamp-induced reordering.
    _cursor = _within_day_cursor(_d_off)

    # Guard on cursor magnitude (the value that actually overflows the per-chunk
    # band). max(cursor)+1 is the largest per-day order count.
    _max_day_count = int(_cursor.max(initial=-1)) + 1
    if _max_day_count > int(per_chunk_alloc):
        raise SalesError(
            f"per_chunk_alloc too small: {_max_day_count} orders on one "
            f"day but alloc={int(per_chunk_alloc)} per chunk per day."
        )

    _new_ids = (
        _d_off * day_stride
        + np.int64(chunk_idx) * per_chunk_alloc
        + _cursor
        + np.int64(1)
    )

    # int32 cast (and the schema's safe=False conversion) would wrap silently, so
    # backstop with a loud overflow guard when int64 promotion wasn't enabled.
    if order_id_int64:
        return np.repeat(_new_ids, reps)
    if _new_ids.size > 0 and int(_new_ids.max()) >= int(int32_max):
        raise SalesError(
            f"Day-based OrderNumber would overflow int32: "
            f"max_id={int(_new_ids.max())} >= {int(int32_max)} "
            "but int64 promotion was not enabled for this run."
        )
    return np.repeat(_new_ids.astype(np.int32), reps)


def _assign_salespersons(
    skip_cols, order_ids_int, order_starts, order_idx,
    store_key_arr, order_dates, eff, rng,
):
    """Assign EmployeeKey (salesperson) per line, drawing off ``rng``.

    When order identifiers exist, sample one salesperson per unique order via the
    day-accurate effective-dated store bridge and broadcast to the order's lines;
    otherwise sample per line. No eligible salesperson for a (store, date) yields
    -1 (dropped downstream by the HARD GUARANTEE; the coverage preflight
    reports/aborts/repairs this upstream). Never emits Store Manager keys.

    The order-level path reuses the chunk's precomputed grouping
    (order_starts/order_idx) when order ids are ascending — equal to np.unique's
    return_index/return_inverse in O(n) — but falls back to np.unique when the
    day-based ids are reordered, which would otherwise change both the unique
    ordering and the per-order draw sequence.
    """
    if not skip_cols and order_ids_int is not None:
        _ids_ascending = (
            order_starts is not None and order_idx is not None
            and (order_ids_int.size < 2
                 or bool((order_ids_int[1:] >= order_ids_int[:-1]).all()))
        )
        if _ids_ascending:
            first_idx = np.flatnonzero(order_starts)
            uniq_orders = order_ids_int[first_idx]
            inv_idx = order_idx
        else:
            uniq_orders, first_idx, inv_idx = np.unique(
                order_ids_int, return_index=True, return_inverse=True)
        order_store_sp = store_key_arr[first_idx]
        order_date_sp = order_dates[first_idx].astype("datetime64[D]", copy=False)

        if isinstance(eff, dict) and eff:
            salesperson_order = _sample_salesperson_vectorized(
                order_store_sp, order_date_sp, eff, rng)
        else:
            salesperson_order = np.full(uniq_orders.size, -1, dtype=np.int32)

        return salesperson_order[inv_idx]

    # Line-level sampling (when order ids do not exist)
    s_ids = np.asarray(store_key_arr, dtype=np.int32)
    d_ids = np.asarray(order_dates, dtype="datetime64[D]")
    if isinstance(eff, dict) and eff:
        return _sample_salesperson_vectorized(s_ids, d_ids, eff, rng)
    return np.full(s_ids.size, -1, dtype=np.int32)


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
    - Discovery forcing follows a static per-run schedule
      (State.customer_discovery_month); it is worker/chunk-order independent
      - IMPORTANT: discovery is OFF if new_customer_share == 0
    - All per-order arrays remain aligned
    """

    # Lazy import to avoid circular import: sales_models imports State from sales_logic
    from ..sales_models import build_quantity, build_prices

    if not PA_AVAILABLE:
        raise SalesError("pyarrow is required")

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
    # This guarantees OrderNumber increases monotonically with OrderDate
    # even across parallel chunks.
    _day_stride = np.int64(State.order_id_day_stride or 0)
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
    customer_keys = getattr(State, "customer_keys", None)
    if customer_keys is None:
        raise SalesError("State must provide customer_keys/customers")
    customer_keys = np.asarray(customer_keys, dtype=np.int32)

    # is_active_in_sales (new contract)
    is_active_in_sales = getattr(State, "customer_is_active_in_sales", None)
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

    start_month = getattr(State, "customer_start_month", None)
    if start_month is None:
        # If not yet wired, fall back: everyone starts at 0 (old behavior)
        start_month = np.zeros(customer_keys.shape[0], dtype="int64")
    else:
        start_month = np.asarray(start_month, dtype="int64")

    # Prefer the once-per-bind normalized broadcast (bind_globals); fall back to
    # computing it here for binders that didn't precompute it (e.g. partial-dict
    # test binds). Same pure function + inputs, so both paths are byte-identical.
    end_month_norm = getattr(State, "customer_end_month_norm", None)
    if end_month_norm is None:
        end_month = getattr(State, "customer_end_month", None)
        end_month_norm = _normalize_end_month(end_month, customer_keys.shape[0])

    base_weight = getattr(State, "customer_base_weight", None)
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
    promo_salience_all = getattr(State, "promo_salience_all", None)
    promo_audience_group = getattr(State, "promo_audience_group", None)
    promo_discount_pct_by_key = getattr(State, "promo_discount_pct_by_key", None)
    # Fulfillment-friction config (drives delivery bucketing here and
    # return prob/lag in the separate returns pass). Pydantic model or None; both
    # compute_dates and the returns builder read it via .get().
    _mdl_cfg = getattr(State, "models_cfg", None)
    fulfillment_cfg = _mdl_cfg.get("fulfillment", None) if _mdl_cfg is not None else None
    # Basket-theme correlation config (biases per-line product choice
    # toward each order's theme). product_subcat_key is bound read-only on State.
    _basket_cfg = _mdl_cfg.get("basket", None) if _mdl_cfg is not None else None
    _basket_on = bool(_basket_cfg) and bool(_basket_cfg.get("enabled", False))
    _basket_num_themes = int(_basket_cfg.get("num_themes", 6)) if _basket_on else 0
    _basket_strength = float(_basket_cfg.get("strength", 0.0)) if _basket_on else 0.0
    _basket_subcat = getattr(State, "product_subcat_key", None) if _basket_on else None
    _segment_cfg = _mdl_cfg.get("segments", {}) if _mdl_cfg is not None else {}
    _segments_on = bool(_segment_cfg) and bool(_segment_cfg.get("enabled", False))
    _income_cfg = (
        _segment_cfg.get("income_product_preference", {}) if _segments_on else {}
    )
    _income_strength = (
        float(_income_cfg.get("strength", 0.0))
        if _income_cfg.get("enabled", False) else 0.0
    )
    _organization_cfg = _segment_cfg.get("organization", {}) if _segments_on else {}
    _organization_on = bool(_organization_cfg) and bool(
        _organization_cfg.get("enabled", False)
    )
    promo_end_all = State.promo_end_all

    nc_promo_keys = State.new_customer_promo_keys
    nc_promo_set = frozenset(nc_promo_keys.tolist()) if nc_promo_keys is not None and nc_promo_keys.size > 0 else None
    # Exact CustomerStartDate and start+N-calendar-month deadline are derived
    # once at worker bind and shared read-only across every chunk.
    if nc_promo_set is not None and customer_keys.size > 0:
        _ckey_to_start_day = getattr(
            State, "customer_first_eff_start_by_key", None)
        _ckey_to_deadline_day = getattr(
            State, "customer_new_promo_end_by_key", None)
        nc_idx = np.isin(promo_keys_all, list(nc_promo_set))
        _nc_keys = promo_keys_all[nc_idx]
        _nc_starts = promo_start_all[nc_idx]
        _nc_ends = promo_end_all[nc_idx]
    else:
        _ckey_to_start_day = None
        _ckey_to_deadline_day = None
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
    # GLOBAL PER-MONTH PLAN — rows/month, orders/month, and the
    # distinct-customer target are computed ONCE in the coordinator against the
    # GLOBAL month totals and broadcast read-only. This chunk slices a contiguous
    # band of every month's order-id space (see chunk_month_band), so the
    # per-month row curve and distinct-customer curve are independent of
    # chunk_size / worker count (review Finding #4/#14). The per-chunk
    # build_rows_per_month + inline distinct target that made both chunk-size
    # dependent are gone.
    # ------------------------------------------------------------
    rows_per_month = getattr(State, "sales_rows_per_month", None)
    orders_per_month = getattr(State, "sales_orders_per_month", None)
    distinct_target_by_month = getattr(State, "sales_distinct_target", None)
    plan_seed = int(getattr(State, "sales_plan_seed", None) or 0)
    total_chunks = int(getattr(State, "total_chunks", None) or 1)
    if rows_per_month is None or orders_per_month is None:
        raise SalesError(
            "Sales global month plan missing (sales_rows_per_month / "
            "sales_orders_per_month). The coordinator must broadcast it."
        )
    rows_per_month = np.asarray(rows_per_month, dtype=np.int64)
    orders_per_month = np.asarray(orders_per_month, dtype=np.int64)
    if distinct_target_by_month is not None:
        distinct_target_by_month = np.asarray(distinct_target_by_month, dtype=np.int64)
    if int(rows_per_month.sum()) <= 0:
        return _empty_table(schema)

    # ------------------------------------------------------------
    # CUSTOMER MIX CONFIG (only what the chunk still needs; the distinct-customer
    # target + participation shaping now live in the global plan / month pool)
    # ------------------------------------------------------------
    cust_cfg = State.models_cfg.get("customers", {}) or {}
    new_customer_share = float(np.clip(
        cust_cfg.get("new_customer_share", 0.10), 0.0, 1.0))

    # Discovery is on whenever new customers are expected. Its per-month shape is
    # baked into the static customer_discovery_month schedule (built once in the
    # coordinator); here it only gates whether the month pool honors the schedule.
    use_discovery = new_customer_share > 0.0

    # Legacy rotating-winner brand probabilities are intentionally not stacked
    # on the unified category-qualified brand residual.
    use_brand_popularity = False

    # Closed-form discovery schedule: pool-aligned month each customer first
    # enters sales, static and broadcast read-only (Finding #5/#6).
    if use_discovery:
        discovery_month = getattr(State, "customer_discovery_month", None)
    else:
        discovery_month = None

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

    # Salesperson effective-date map (day-accurate bridge, constant across months)
    eff = getattr(State, "salesperson_effective_by_store", None)

    # CDF cache: use worker-lifetime cache so subsequent chunks skip
    # intersection + CDF recomputation entirely (contents are deterministic).
    _product_cdf_cache = _CACHES.cdf

    # Per-month eligible row indices, computed once per worker (chunk-invariant).
    eligible_idx_all = _eligible_idx_by_month(
        T, is_active_in_sales, start_month, end_month_norm)

    for m_offset in range(T):
        R_m = int(rows_per_month[m_offset])
        if R_m <= 0:
            continue
        O_m = int(orders_per_month[m_offset])
        if O_m <= 0:
            continue

        # --------------------------------------------------------
        # INDEX-SPACE SHARDING: this chunk's contiguous band of month m's global
        # order/line space. order_start/n_orders_band tile [0, O_m) across chunks
        # with no gaps (so the distinct pool is always fully covered); m_lines is
        # the exact line target (sum over chunks == R_m).
        # --------------------------------------------------------
        order_start, n_orders_band, m_lines = chunk_month_band(
            R_m, O_m, chunk_idx, total_chunks)
        if n_orders_band <= 0 or m_lines <= 0:
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
        # Eligibility (precomputed per-month indices, chunk-invariant)
        # --------------------------------------------------------
        eligible_idx = eligible_idx_all[m_offset]
        if eligible_idx.size == 0:
            continue

        # --------------------------------------------------------
        # CUSTOMER ASSIGNMENT (global plan; chunk-invariant by construction)
        # --------------------------------------------------------
        # Build (once per worker) the month's distinct-customer pool, then map this
        # chunk's contiguous order band to customers. A given global order index
        # always maps to the same customer, so the union of distinct customers per
        # month is exactly the pool regardless of how orders are split into chunks.
        D_m = int(distinct_target_by_month[m_offset]) if distinct_target_by_month is not None else 0
        pool, pool_cdf = _get_month_pool(
            m_offset, D_m, eligible_idx, customer_keys,
            discovery_month, base_weight, end_month_norm, plan_seed,
            np.asarray(month_date_pool)
            .astype("datetime64[M]")
            .astype("datetime64[D]")
            .astype(np.int64)
            .min())
        if pool.size == 0:
            continue

        customer_keys_for_orders = assign_orders_to_customers(
            m_offset=int(m_offset),
            order_start=int(order_start),
            n_orders=int(n_orders_band),
            pool=pool,
            cdf=pool_cdf,
            seed=plan_seed,
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
                n=m_lines,
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

            order_dates = _clamp_order_dates_to_customer_lifecycle(
                order_dates, customer_keys_out)

            if _use_day_ids:
                order_ids_int = _compute_day_based_order_ids(
                    order_dates, line_num, orders["_repeats"],
                    _date_pool_min_day, _day_stride, _per_chunk_alloc,
                    chunk_idx, _order_id_int64, INT32_MAX,
                )
            else:
                order_ids_int = orders["order_ids_int"]

        else:
            customer_keys_out = customer_keys_for_orders
            # Honor per-day demand weights (month_date_prob) here too, matching
            # the order path (build_orders); previously this sampled dates
            # uniformly, flattening intra-month seasonality when skip_order_cols.
            if month_date_prob is not None:
                _od_idx = rng.choice(len(month_date_pool), size=n_orders, p=month_date_prob)
            else:
                _od_idx = rng.integers(0, len(month_date_pool), size=n_orders)
            order_dates = month_date_pool[_od_idx]
            order_dates = _clamp_order_dates_to_customer_lifecycle(
                order_dates, customer_keys_out)
            order_ids_int = None
            line_num = None

        n_lines = int(np.asarray(customer_keys_out).shape[0])

        # --------------------------------------------------------
        # STORE (sampled first — needed for assortment filtering)
        #   Agreement: 1 Store per Order (when order ids exist)
        # --------------------------------------------------------
        order_starts, order_idx, n_unique_orders = _compute_order_grouping(skip_cols, line_num)

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
        _store_weight = getattr(State, "store_demand_weight", None)
        store_key_arr = _apply_geo_bias_store_sampling(
            rng, skip_cols, n_unique_orders, n_lines,
            _sorted_order_cust, customer_keys_out,
            _month_stores, order_idx,
            getattr(State, "customer_geo_key", None),
            getattr(State, "geo_to_country_id", None),
            getattr(State, "country_to_store_keys", None),
            store_weight=_store_weight,
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
            store_weight=_store_weight,
        )

        # CORRELATION #1: Store → ChannelKey
        sales_channel_key_arr = _apply_store_channel_correlation(
            rng, store_key_arr, skip_cols,
            order_idx, order_starts, n_unique_orders, n_lines,
            getattr(State, "store_channel_keys", None),
            getattr(State, "channel_prob_by_store", None),
            order_numbers=order_ids_int,
        )
        if _organization_on:
            sales_channel_key_arr = _apply_organization_channel_routing(
                rng,
                sales_channel_key_arr,
                customer_keys_out,
                order_numbers=order_ids_int,
                order_starts=order_starts,
                order_idx=order_idx,
                business_channel_share=float(
                    _organization_cfg.get("business_channel_share", 0.0)
                ),
                b2b_channel_share=float(
                    _organization_cfg.get("b2b_channel_share", 0.70)
                ),
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
        # CORRELATION #4: ChannelKey → ProductKey
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
                order_numbers=order_ids_int,
                line_numbers=line_num,
            )
        else:
            prod_idx = _sample_product_row_indices(
                rng=rng,
                n=n_lines,
                product_np=product_np,
                m_offset=int(m_offset),
                enabled=use_brand_popularity,
                product_weight=_product_weight,
                order_numbers=order_ids_int,
                line_numbers=line_num,
            )

        # CORRELATION #6: bias off-theme lines toward each order's
        # basket theme. Applied before channel enforcement so eligibility still
        # has the final say. Hash-seeded on order/line ids (not the chunk RNG).
        if _basket_on and _basket_subcat is not None and order_ids_int is not None:
            prod_idx = apply_basket_theme(
                prod_idx, order_ids_int, line_num, _basket_subcat,
                num_themes=_basket_num_themes, strength=_basket_strength,
            )

        if _segments_on:
            prod_idx = _apply_segment_product_preferences(
                rng,
                prod_idx,
                customer_keys_out,
                _product_weight,
                order_numbers=order_ids_int,
                line_numbers=line_num,
                income_strength=_income_strength,
                organization_category_strength=(
                    float(_organization_cfg.get("category_strength", 0.0))
                    if _organization_on else 0.0
                ),
            )

        # CORRELATION #4: Channel → product eligibility enforcement
        prod_idx = _enforce_channel_product_eligibility(
            rng, prod_idx, sales_channel_key_arr,
            _pce, _ch2eg, _product_weight,
            order_numbers=order_ids_int,
            line_numbers=line_num,
        )

        customer_keys_out = np.asarray(customer_keys_out, dtype=np.int32)
        order_dates = np.asarray(order_dates, dtype="datetime64[D]")

        # Convert order dates to epoch days for SCD2 per-row version lookups
        _order_epoch_days = order_dates.astype(np.int64)  # datetime64[D] → epoch days

        # SCD2: resolve per-row product version + price using actual OrderDate
        product_keys, unit_price, unit_cost = _resolve_row_prices(
            prod_idx, _order_epoch_days, product_np)

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
        # Pass the precomputed order grouping (order_starts/order_idx) so
        # compute_dates skips a redundant np.unique sort.  Safe regardless of
        # whether order_ids_int is ascending: the due-date hash is a pure
        # function of the id value re-indexed by inv_idx, so ordering is
        # irrelevant to the result.
        _uniq_for_dates = order_ids_int[order_starts] if (
            order_ids_int is not None and order_starts is not None) else None
        _inv_for_dates = order_idx if (
            order_ids_int is not None and order_idx is not None) else None
        dates = compute_dates(
            n=n_lines,
            product_keys=product_keys,
            order_ids_int=order_ids_int,
            order_dates=order_dates,
            channel_keys=sales_channel_key_arr,
            channel_fulfillment_days=_ch_fulfill,
            unique_orders=_uniq_for_dates,
            inv_idx=_inv_for_dates,
            line_numbers=line_num,
            fulfillment_cfg=fulfillment_cfg,
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
                promo_salience_all=promo_salience_all,
                customer_keys=customer_keys_out[order_starts],
                customer_loyalty_tier_by_key=getattr(
                    State, "customer_loyalty_tier_by_key", None),
                customer_start_day_by_key=_ckey_to_start_day,
                customer_new_promo_end_by_key=_ckey_to_deadline_day,
                promo_audience_group=promo_audience_group,
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
                promo_salience_all=promo_salience_all,
                customer_keys=customer_keys_out,
                customer_loyalty_tier_by_key=getattr(
                    State, "customer_loyalty_tier_by_key", None),
                customer_start_day_by_key=_ckey_to_start_day,
                customer_new_promo_end_by_key=_ckey_to_deadline_day,
                promo_audience_group=promo_audience_group,
            )
        promo_keys = np.asarray(promo_keys, dtype=np.int32)

        # New Customer promo: strip invalid + force-assign to new customers
        promo_keys = _apply_new_customer_promo(
            promo_keys, order_dates, customer_keys_out,
            sales_channel_key_arr,
            _ckey_to_start_day, _ckey_to_deadline_day,
            _nc_keys, _nc_starts, _nc_ends,
            nc_promo_set,
            no_discount_key,
            _pcg, promo_keys_all,
        )

        # --------------------------------------------------------
        # EMPLOYEE (EmployeeKey)
        #   Agreement:
        #     - If order identifiers exist (skip_cols == False): 1 salesperson per order (broadcast to all lines),
        #       still respecting effective-dated store assignments by (StoreKey, OrderDate).
        #     - If order identifiers do not exist: keep line-level sampling (old behavior).
        # - DAY-accurate effective-dated bridge:
        #     State.salesperson_effective_by_store[store] = (emp_keys[int32], start_dates[D], end_dates[D], weights[f64])
        # - No eligible salesperson for a (store, date) => -1 (dropped downstream by the
        #   HARD GUARANTEE; coverage pre-flight reports/aborts/repairs this upstream).
        # IMPORTANT: Never emit Store Manager keys (30_000_000 + StoreKey).
        # --------------------------------------------------------

        # STRICT: no cross-store salesperson fallback. If no eligible salesperson exists, we emit -1.
        salesperson_key_arr = _assign_salespersons(
            skip_cols, order_ids_int, order_starts, order_idx,
            store_key_arr, order_dates, eff, rng,
        )

        # Discovery is a static closed-form schedule (State.customer_discovery_month),
        # so there is no per-chunk "seen" state to persist here anymore.

        # --------------------------------------------------------
        # QUANTITY + PRICING
        # --------------------------------------------------------
        qty = build_quantity(
            rng, order_dates,
            product_row_idx=prod_idx,
            unit_price=unit_price,
            order_numbers=order_ids_int,
            line_numbers=line_num,
            segment_multiplier=_organization_quantity_multiplier(
                customer_keys_out, _organization_cfg,
            ) if _organization_on else None,
        )

        price = compute_prices(
            n=len(customer_keys_out),
            unit_price=unit_price,
            unit_cost=unit_cost,
        )

        price = build_prices(
            order_dates=order_dates,
            qty=qty,
            price=price,
            promo_keys=promo_keys,
            promo_discount_pct=(
                promo_discount_pct_by_key[promo_keys]
                if promo_discount_pct_by_key is not None else None
            ),
            no_discount_key=no_discount_key,
            product_ids=product_keys,
            order_numbers=order_ids_int,
            line_numbers=line_num,
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
            cols["OrderNumber"] = order_ids_int
            cols["OrderLineNumber"] = line_num

        # SCD2: remap IsCurrent CustomerKey → version-specific CustomerKey using actual OrderDate
        customer_keys_out = _remap_customer_scd2(customer_keys_out, _order_epoch_days)

        cols["CustomerKey"] = customer_keys_out
        cols["ProductKey"] = product_keys
        cols["StoreKey"] = store_key_arr
        cols["EmployeeKey"] = salesperson_key_arr
        cols["PromotionKey"] = promo_keys
        cols["CurrencyKey"] = currency_arr

        # ChannelKey produced by store-channel correlation (above)
        if sales_channel_key_arr is not None:
            cols["ChannelKey"] = sales_channel_key_arr

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

        # HARD GUARANTEE: never emit EmployeeKey=-1 (orphan FK). Drop such whole
        # orders, but loudly — a drop means a coverage gap slipped past the
        # preflight and shortened this month below its plan target.
        n_rows = _drop_orphan_employee_key_rows(cols, n_rows, m_offset)

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
