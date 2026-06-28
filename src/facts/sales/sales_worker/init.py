from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Optional

import numpy as np

from ..output_paths import (
    OutputPaths,
    TABLE_SALES,
    TABLE_SALES_ORDER_DETAIL,
    TABLE_SALES_ORDER_HEADER,
    TABLE_SALES_RETURN,
)
from ..sales_logic import bind_globals, State
from ..sales_logic.chunk_builder import reset_worker_cdf_cache
from .schemas import build_worker_schemas
from src.exceptions import SalesError
from src.utils.config_helpers import int_or, float_or, str_or
from src.utils.logging_utils import warn as _warn
from src.utils.shared_arrays import resolve_array
from ..worker_cfg_schema import SalesWorkerCfg


EMPLOYEE_KEY_MIN_NON_MANAGER = 40_000_000

# Default assortment coverage ratios by StoreType
_DEFAULT_ASSORTMENT_COVERAGE = {
    "Online": 1.0,
    "Hypermarket": 0.85,
    "Supermarket": 0.50,
    "Convenience": 0.25,
}

# Knuth multiplicative hash constant (2^32 * phi)
_HASH_MULT = np.int64(2654435761)


def _build_store_subcat_matrix(
    store_keys: np.ndarray,
    store_type_arr: np.ndarray,
    product_subcat_key: np.ndarray,
    coverage_cfg: dict,
    seed: int = 42,
) -> tuple:
    """Build a compact store × subcategory boolean inclusion matrix.

    Returns ``(unique_subcats, matrix)`` where *matrix* is a bool array of
    shape ``(max_store_key + 1, n_subcats)`` indicating which subcategories
    each store stocks.  Workers expand this to product row indices on-the-fly,
    avoiding the huge materialised jagged array.
    """
    unique_subcats = np.unique(product_subcat_key)
    n_subcats = len(unique_subcats)

    max_sk = int(store_keys.max()) + 1
    matrix = np.zeros((max_sk, n_subcats), dtype=np.bool_)

    seed_offset = np.int64(seed * 31 + 7)
    sc_hash_term = np.abs(unique_subcats.astype(np.int64) * np.int64(40503))

    for sk, st in zip(store_keys, store_type_arr):
        sk_int = int(sk)
        coverage = float(coverage_cfg.get(str(st), coverage_cfg.get("default", 0.50)))

        if coverage >= 1.0:
            matrix[sk_int, :] = True
            continue

        threshold = int(coverage * 10000)
        hashes = np.abs((np.int64(sk_int) * _HASH_MULT + sc_hash_term + seed_offset) % np.int64(10000))
        included_mask = hashes < threshold

        if included_mask.any():
            matrix[sk_int] = included_mask
        else:
            best_idx = int(np.argmin(hashes))
            matrix[sk_int, best_idx] = True

    return unique_subcats, matrix


class _LazyStoreAssortment:
    """Lazy store-product assortment that expands rows on first access per store.

    Avoids expanding all 200 stores × 1M products at init (4+ GB).  Each store's
    product row indices are built on first lookup and cached thereafter.
    Supports ``len()`` and ``[]`` access like a list.
    """
    __slots__ = ("_cache", "_subcat_matrix", "_unique_subcats",
                 "_subcat_to_rows", "_all_rows", "_n_products", "_max_sk")

    def __init__(
        self,
        unique_subcats: np.ndarray,
        subcat_matrix: np.ndarray,
        product_subcat_key: np.ndarray,
        n_products: int,
    ):
        self._subcat_matrix = subcat_matrix
        self._unique_subcats = unique_subcats
        self._n_products = n_products
        self._max_sk = subcat_matrix.shape[0]
        self._cache: dict = {}
        self._all_rows: np.ndarray | None = None

        # Build subcat → row indices once (shared across all stores)
        self._subcat_to_rows: dict[int, np.ndarray] = {}
        for sc in unique_subcats:
            self._subcat_to_rows[int(sc)] = np.flatnonzero(product_subcat_key == sc)

    def __len__(self):
        return self._max_sk

    def __getitem__(self, sk_int: int):
        if sk_int < 0 or sk_int >= self._max_sk:
            return None
        cached = self._cache.get(sk_int)
        if cached is not None:
            return cached

        row_mask = self._subcat_matrix[sk_int]
        if not row_mask.any():
            return None

        if row_mask.all():
            if self._all_rows is None:
                self._all_rows = np.arange(self._n_products, dtype=np.int32)
            result = self._all_rows
        else:
            included = [self._subcat_to_rows[int(sc)]
                        for sc in self._unique_subcats[row_mask]]
            result = np.concatenate(included).astype(np.int32)

        self._cache[sk_int] = result
        return result


def _expand_store_assortment_from_matrix(
    store_keys: np.ndarray,
    unique_subcats: np.ndarray,
    subcat_matrix: np.ndarray,
    product_subcat_key: np.ndarray,
    n_products: int,
) -> "_LazyStoreAssortment":
    """Return a lazy assortment that expands stores on first access.

    Only the ``subcat_to_rows`` index is built eagerly (~40 scans of
    ``product_subcat_key``).  Per-store row arrays are materialized on
    demand, keeping memory proportional to actual stores accessed.
    """
    return _LazyStoreAssortment(
        unique_subcats, subcat_matrix, product_subcat_key, n_products,
    )


# Legacy wrapper: used by the fallback path in init_sales_worker when
# neither the compact subcat matrix nor the pre-built jagged array is available.
def _build_store_assortment(
    store_keys: np.ndarray,
    store_type_arr: np.ndarray,
    product_np: np.ndarray,
    product_subcat_key: np.ndarray,
    coverage_cfg: dict,
    seed: int = 42,
) -> list:
    """Build per-store product row index arrays (backward-compatible wrapper).

    For small product counts, materialises directly.  For large catalogs,
    prefer :func:`_build_store_subcat_matrix` + per-worker expansion.
    """
    unique_subcats, matrix = _build_store_subcat_matrix(
        store_keys, store_type_arr, product_subcat_key, coverage_cfg, seed,
    )
    return _expand_store_assortment_from_matrix(
        store_keys, unique_subcats, matrix, product_subcat_key, len(product_np),
    )


# ---------------------------------------------------------------------
# Small helpers (kept close to monolith behavior for compatibility)
# ---------------------------------------------------------------------


def build_buckets_from_key(
    key: Any,
    *,
    max_key: Optional[int] = None,
    max_buckets: int = 1_000_000,
) -> list[np.ndarray]:
    """
    Build index buckets for non-negative integer keys.

    Returns: buckets[k] = row indices where key == k.

    Safety:
      - By default, refuses to allocate more than `max_buckets` buckets to avoid OOM
        if called with raw, high-magnitude surrogate keys (e.g., EmployeeKey ~ 40,000,000+).
      - Override via `max_key=` or `max_buckets=` if you *intend* to allocate a large table.
    """
    key_np = np.asarray(key, dtype=np.int32)
    if key_np.size == 0:
        return []
    if key_np.min() < 0:
        raise SalesError("Key values must be non-negative integers")

    max_k = int(key_np.max()) if max_key is None else int(max_key)
    K = max_k + 1
    if K > int(max_buckets):
        raise SalesError(
            f"Refusing to allocate {K:,} buckets (max_key={max_k:,}); "
            f"set max_key/max_buckets explicitly if this is intended."
        )

    order = np.argsort(key_np, kind="mergesort")
    k_sorted = key_np[order]
    starts = np.flatnonzero(np.r_[True, k_sorted[1:] != k_sorted[:-1]])
    ends = np.r_[starts[1:], k_sorted.size]

    buckets: list[np.ndarray] = [np.empty(0, dtype=np.int32) for _ in range(K)]
    for s, e in zip(starts, ends):
        k = int(k_sorted[int(s)])
        buckets[k] = order[int(s) : int(e)].astype(np.int32, copy=False)
    return buckets


def as_int64(x: Any) -> np.ndarray:
    return np.asarray(x, dtype=np.int64)


def as_int32(x: Any) -> np.ndarray:
    """Convert to int32 — use for dimension/surrogate key columns."""
    return np.asarray(x, dtype=np.int32)


def as_f64(x: Any) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def dense_map(mapping: Optional[dict]) -> Optional[np.ndarray]:
    if not mapping:
        return None
    keys = np.fromiter((int(k) for k in mapping.keys()), dtype=np.int32)
    vals = np.fromiter((int(v) for v in mapping.values()), dtype=np.int32)
    if keys.size == 0:
        return None
    max_key = int(keys.max())
    if max_key < 0:
        return None
    arr = np.full(max_key + 1, -1, dtype=np.int32)
    arr[keys] = vals
    return arr


def infer_T_from_date_pool(date_pool: Any) -> int:
    dp = np.asarray(date_pool, dtype="datetime64[D]")
    return int(np.unique(dp.astype("datetime64[M]")).size)


# back-compat alias (keep older imports stable)
def _build_buckets_from_brand_key(brand_key) -> list:
    """Back-compat alias for build_buckets_from_key. Prefer direct call in new code."""
    arr = np.asarray(brand_key, dtype=np.int32)
    max_key = int(arr.max()) if arr.size > 0 else None
    return build_buckets_from_key(brand_key, max_key=max_key)


# ---------------------------------------------------------------------
# Employee salesperson assignment

# Canonical: day-accurate effective-dated bridge index
# ---------------------------------------------------------------------


def _normalize_assignment_arrays(
    *,
    store_keys: np.ndarray,
    assign_store: Any,
    assign_emp: Any,
    assign_start: Any,
    assign_end: Any,
    assign_fte: Any = None,
    assign_is_primary: Any = None,
) -> Optional[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]]:
    """
    Normalize and validate employee assignment arrays.

    Returns:
      (assign_store_i64, assign_emp_i32, start_D, end_D, fte_f64, is_primary_bool, max_store_key)

    Notes:
      - NaT start -> FAR_PAST
      - NaT end   -> FAR_FUTURE
      - Filters invalid store keys outside [0, max_store_key]
      - Filters invalid windows where start > end
      - Keeps fte/is_primary aligned with filtered rows
    """
    if assign_store is None or assign_emp is None or assign_start is None or assign_end is None:
        return None

    store_keys = np.asarray(store_keys, dtype=np.int32)
    if store_keys.size == 0:
        return None
    max_store_key = int(store_keys.max())

    a_store = np.asarray(assign_store, dtype=np.int32)
    a_emp = np.asarray(assign_emp, dtype=np.int32)
    if a_store.size == 0 or a_emp.size == 0:
        return None
    if a_store.shape[0] != a_emp.shape[0]:
        raise SalesError("employee assignment arrays must align (StoreKey vs EmployeeKey)")

    start_raw = np.asarray(assign_start, dtype="datetime64[D]")
    end_raw = np.asarray(assign_end, dtype="datetime64[D]")
    if start_raw.shape[0] != a_store.shape[0] or end_raw.shape[0] != a_store.shape[0]:
        raise SalesError("employee assignment arrays must align (dates vs keys)")

    # Optional aligned arrays
    fte = np.ones(a_store.shape[0], dtype=np.float64) if assign_fte is None else np.asarray(assign_fte, dtype=np.float64)
    if fte.shape[0] != a_store.shape[0]:
        raise SalesError("employee_assign_fte must align with employee assignments")

    is_primary = (
        np.zeros(a_store.shape[0], dtype=bool)
        if assign_is_primary is None
        else np.asarray(assign_is_primary, dtype=bool)
    )
    if is_primary.shape[0] != a_store.shape[0]:
        raise SalesError("employee_assign_is_primary must align with employee assignments")

    FAR_FUTURE = np.datetime64("2262-04-11", "D")
    FAR_PAST = np.datetime64("1900-01-01", "D")

    start_fixed = start_raw.copy()
    end_fixed = end_raw.copy()

    nat_s = np.isnat(start_fixed)
    if nat_s.any():
        start_fixed[nat_s] = FAR_PAST

    nat_e = np.isnat(end_fixed)
    if nat_e.any():
        end_fixed[nat_e] = FAR_FUTURE

    # Filter store key validity
    valid_store = (a_store >= 0) & (a_store <= max_store_key)
    if not valid_store.all():
        a_store = a_store[valid_store]
        a_emp = a_emp[valid_store]
        start_fixed = start_fixed[valid_store]
        end_fixed = end_fixed[valid_store]
        fte = fte[valid_store]
        is_primary = is_primary[valid_store]
        if a_store.size == 0:
            return None

    # Filter window validity
    ok_window = start_fixed <= end_fixed
    if not ok_window.all():
        a_store = a_store[ok_window]
        a_emp = a_emp[ok_window]
        start_fixed = start_fixed[ok_window]
        end_fixed = end_fixed[ok_window]
        fte = fte[ok_window]
        is_primary = is_primary[ok_window]
        if a_store.size == 0:
            return None

    return a_store, a_emp, start_fixed, end_fixed, fte, is_primary, max_store_key


def _hash_u64(x: np.ndarray) -> np.ndarray:
    """Vectorized splitmix64 finalizer (uint64 in -> well-mixed uint64 out)."""
    x = x.astype(np.uint64)
    x ^= x >> np.uint64(30)
    x *= np.uint64(0xBF58476D1CE4E5B9)
    x ^= x >> np.uint64(27)
    x *= np.uint64(0x94D049BB133111EB)
    x ^= x >> np.uint64(31)
    return x


def _salesperson_perf_multiplier(
    emp_keys: Any, spread: float, seed: int,
    lo: float = 0.25, hi: float = 4.0,
) -> np.ndarray:
    """Stable per-employee sales-performance multiplier (lognormal, median 1.0).

    A deterministic function of EmployeeKey + seed only — independent of worker,
    chunk, order, or array position — so the same employee carries the same
    multiplier in every store and every chunk (transfers included). ``spread``
    is the lognormal sigma (<=0 disables -> all ones); the result is clipped to
    ``[lo, hi]`` so a handful of top performers stand out without runaway
    outliers. Used to weight salesperson selection so within a store some reps
    sell more than others (Part 1 evens load *across* stores; this varies it
    *within* a store)."""
    emp = np.asarray(emp_keys, dtype=np.int64)
    if spread <= 0.0 or emp.size == 0:
        return np.ones(emp.size, dtype=np.float64)
    s = np.uint64(int(seed) & 0x7FFFFFFF)
    base = (emp.astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15)) ^ s
    h1 = _hash_u64(base + np.uint64(0x1))
    h2 = _hash_u64(base + np.uint64(0x9E3779B9))
    # Box-Muller: two uniforms -> standard normal. u1 kept strictly in (0,1).
    u1 = (h1.astype(np.float64) + 1.0) / (18446744073709551616.0 + 1.0)  # 2**64 + 1
    u2 = h2.astype(np.float64) / 18446744073709551616.0                  # 2**64
    z = np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)
    perf = np.exp(float(spread) * z)
    return np.clip(perf, lo, hi)


def _build_salesperson_effective_by_store(
    *,
    store_keys: np.ndarray,
    assign_store: Any,
    assign_emp: Any,
    assign_start: Any,
    assign_end: Any,
    assign_fte: Any = None,
    assign_is_primary: Any = None,
    primary_boost: float = 2.0,
    perf_spread: float = 0.0,
    perf_seed: int = 0,
) -> Optional[dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]]:
    """
    Canonical structure for Sales employee assignment.

    Returns:
      { store_key: (emp_keys, start_dates[D], end_dates[D], weights[f64]) }

    - Enforces StartDate/EndDate at DAY granularity.
    - Treats missing EndDate as open-ended.
    - Treats missing StartDate as FAR_PAST (always eligible until EndDate).
    """
    norm = _normalize_assignment_arrays(
        store_keys=store_keys,
        assign_store=assign_store,
        assign_emp=assign_emp,
        assign_start=assign_start,
        assign_end=assign_end,
        assign_fte=assign_fte,
        assign_is_primary=assign_is_primary,
    )
    if norm is None:
        return None

    a_store, a_emp, start_fixed, end_fixed, fte, is_primary, max_store_key = norm

    # Validate FTE bounds — negative or extreme values indicate data corruption
    bad_fte = (fte < 0.0) | (fte > 2.0)
    if np.any(bad_fte):
        fte = np.clip(fte, 0.0, 2.0)

    weights = fte * np.where(is_primary, float(primary_boost), 1.0)
    # Per-employee performance spread: some reps sell more than others within a
    # store. Keyed on EmployeeKey so it's stable across stores/chunks/workers.
    if perf_spread and float(perf_spread) > 0.0:
        weights = weights * _salesperson_perf_multiplier(a_emp, float(perf_spread), int(perf_seed))

    order = np.argsort(a_store, kind="mergesort")
    s_sorted = a_store[order]
    starts = np.flatnonzero(np.r_[True, s_sorted[1:] != s_sorted[:-1]])
    ends = np.r_[starts[1:], s_sorted.size]

    out: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    for s, e in zip(starts, ends):
        idx = order[int(s) : int(e)]
        store = int(s_sorted[int(s)])
        if store < 0 or store > max_store_key:
            continue
        out[store] = (
            a_emp[idx].astype(np.int32, copy=False),
            start_fixed[idx].astype("datetime64[D]", copy=False),
            end_fixed[idx].astype("datetime64[D]", copy=False),
            weights[idx].astype(np.float64, copy=False),
        )

    return out


# ---------------------------------------------------------------------
# Brand popularity model helper (unchanged)
# ---------------------------------------------------------------------


def _build_brand_prob_by_month_rotate_winner(
    rng: np.random.Generator,
    *,
    T: int,
    B: int,
    winner_boost: float = 2.5,
    noise_sd: float = 0.15,
    min_share: float = 0.02,
    year_len_months: int = 12,
    brand_product_counts: np.ndarray | None = None,
    count_exponent: float = 0.25,
) -> np.ndarray:
    if T <= 0 or B <= 0:
        raise SalesError(f"Invalid T/B for brand_prob_by_month: T={T}, B={B}")

    year_len = max(1, int(year_len_months))

    if brand_product_counts is not None and len(brand_product_counts) == B:
        counts = np.maximum(brand_product_counts.astype(np.float64), 1.0)
        exp = float(np.clip(count_exponent, 0.0, 1.0))
        base = counts ** exp if exp > 0.0 else np.ones_like(counts)
        base = base / base.sum()
    else:
        base = np.ones(B, dtype=np.float64) / float(B)

    out = np.empty((T, B), dtype=np.float64)

    n_years = max(1, (T + year_len - 1) // year_len)
    full_cycles = (n_years + B - 1) // B
    winner_sequence = np.tile(np.arange(B, dtype=np.int64), full_cycles)
    for c in range(full_cycles):
        chunk = winner_sequence[c * B : (c + 1) * B]
        rng.shuffle(chunk)
    winner_sequence = winner_sequence[:n_years]

    # Tapered co-winners ensure short runs (where year_idx never reaches most
    # brands, e.g. B=70 with n_years=3) still show popularity variation across
    # a meaningful slice of the catalog.
    boost = float(winner_boost)
    last = len(winner_sequence) - 1
    tapers = (1.0, 0.5, 0.25)
    for t in range(T):
        year_idx = t // year_len
        v = base.copy()
        seen: set[int] = set()
        for offset, taper in enumerate(tapers):
            idx = int(winner_sequence[min(year_idx + offset, last)])
            if idx in seen:
                continue
            seen.add(idx)
            v[idx] *= 1.0 + taper * (boost - 1.0)

        if noise_sd > 0:
            v *= np.exp(rng.normal(loc=0.0, scale=float(noise_sd), size=B))
        if min_share > 0:
            v = np.maximum(v, float(min_share))
        s = float(v.sum())
        out[t] = (v / s) if s > 0 else base
    return out


# ---------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------


def init_sales_worker(worker_cfg: SalesWorkerCfg) -> None:
    reset_worker_cdf_cache()
    # Resolve any shared-memory descriptors back into numpy array views.
    # This is a no-op for values that are already plain arrays/None.
    _REQUIRED_ARRAYS = {"product_np", "store_keys", "customer_keys", "date_pool", "date_prob"}
    for _k in list(worker_cfg):
        worker_cfg[_k] = resolve_array(worker_cfg[_k])
    # Validate that required arrays resolved successfully
    for _rk in _REQUIRED_ARRAYS:
        if _rk in worker_cfg and worker_cfg[_rk] is None:
            raise RuntimeError(
                f"Failed to resolve shared array for required key '{_rk}'. "
                "Shared memory descriptor may be corrupted."
            )

    try:
        product_np = worker_cfg["product_np"]
        product_brand_key = worker_cfg.get("product_brand_key")
        store_keys = worker_cfg["store_keys"]

        store_to_geo = worker_cfg.get("store_to_geo")
        geo_to_currency = worker_cfg.get("geo_to_currency")

        promo_keys_all = worker_cfg["promo_keys_all"]
        promo_start_all = worker_cfg["promo_start_all"]
        promo_end_all = worker_cfg["promo_end_all"]
        new_customer_promo_keys = worker_cfg.get("new_customer_promo_keys")
        new_customer_window_months = int(worker_cfg.get("new_customer_window_months", 3))

        customer_keys = worker_cfg["customer_keys"]
        customer_is_active_in_sales = worker_cfg.get("customer_is_active_in_sales")
        customer_start_month = worker_cfg.get("customer_start_month")
        customer_end_month = worker_cfg.get("customer_end_month")
        customer_base_weight = worker_cfg.get("customer_base_weight")

        date_pool = worker_cfg["date_pool"]
        date_prob = worker_cfg["date_prob"]

        # employee store assignments (bridge)
        employee_assign_store_key = worker_cfg.get("employee_assign_store_key")
        employee_assign_employee_key = worker_cfg.get("employee_assign_employee_key")
        employee_assign_start_date = worker_cfg.get("employee_assign_start_date")
        employee_assign_end_date = worker_cfg.get("employee_assign_end_date")
        employee_assign_fte = worker_cfg.get("employee_assign_fte")
        employee_assign_is_primary = worker_cfg.get("employee_assign_is_primary")
        employee_primary_boost = float(worker_cfg.get("employee_primary_boost", 2.0))
        salesperson_perf_spread = float_or(worker_cfg.get("salesperson_perf_spread"), 0.0)
        salesperson_perf_seed = int_or(worker_cfg.get("salesperson_perf_seed"), 0)
        employee_assign_role = worker_cfg.get("employee_assign_role")
        salesperson_roles = worker_cfg.get("salesperson_roles", ["Sales Associate"])

        op = worker_cfg["output_paths"]
        if not isinstance(op, Mapping):
            raise RuntimeError("output_paths must be a dict")

        file_format = worker_cfg.get("file_format") or op.get("file_format")
        out_folder = worker_cfg.get("out_folder") or op.get("out_folder")
        if not file_format:
            raise RuntimeError("file_format is required (worker_cfg.file_format or output_paths.file_format)")
        if not out_folder:
            raise RuntimeError("out_folder is required (worker_cfg.out_folder or output_paths.out_folder)")

        row_group_size = int(worker_cfg.get("row_group_size", 2_000_000))
        compression = str(worker_cfg.get("compression", "snappy"))

        # ------------------------------------------------------------
        # CRITICAL: OrderNumber uniqueness depends on a CONSTANT
        # per-run stride for chunk order-id ranges (NOT per-task batch size).
        # Prefer 'order_id_stride_orders'; fall back to 'chunk_size'.
        # ------------------------------------------------------------
        stride_raw = worker_cfg.get("order_id_stride_orders", None)
        if stride_raw is None:
            stride_raw = worker_cfg.get("chunk_size", None)

        chunk_size = int(stride_raw or 0)
        if chunk_size <= 0:
            raise RuntimeError(
                "Missing/invalid order-id stride. Set worker_cfg.order_id_stride_orders (preferred) "
                "or worker_cfg.chunk_size to a positive int. This value partitions OrderNumber "
                "space across chunks and must be constant across the run."
            )

        month_stride = int(worker_cfg.get("month_stride", 0) or 0)
        per_chunk_alloc = int(worker_cfg.get("per_chunk_alloc", 0) or 0)
        order_id_int64 = bool(worker_cfg.get("order_id_int64", False))

        no_discount_key = worker_cfg["no_discount_key"]
        delta_output_folder = worker_cfg.get("delta_output_folder") or op.get("delta_output_folder")
        merged_file = worker_cfg.get("merged_file") or op.get("merged_file")
        write_delta = worker_cfg.get("write_delta", False)

        skip_order_cols = worker_cfg["skip_order_cols"]
        sales_output = str_or(worker_cfg.get("sales_output"), "sales").lower()
        if sales_output not in {"sales", "sales_order", "both"}:
            raise RuntimeError(f"Invalid sales_output: {sales_output}")

        skip_order_cols_requested = bool(worker_cfg.get("skip_order_cols_requested", skip_order_cols))

        returns_enabled = bool(worker_cfg.get("returns_enabled", False))
        returns_rate = float(worker_cfg.get("returns_rate", 0.0))
        returns_min_lag_days = int(worker_cfg.get("returns_min_lag_days", 0))
        returns_max_lag_days = int(worker_cfg.get("returns_max_lag_days", 60))
        if returns_min_lag_days < 0:
            returns_min_lag_days = 0
        if returns_max_lag_days < 0:
            returns_max_lag_days = 0
        if returns_min_lag_days > returns_max_lag_days:
            # Clamp defensively to avoid crashing the pool on bad configs.
            returns_min_lag_days = returns_max_lag_days
        returns_reason_keys = worker_cfg.get("returns_reason_keys")
        returns_reason_probs = worker_cfg.get("returns_reason_probs")
        returns_full_line_probability = float(worker_cfg.get("returns_full_line_probability", 0.85))
        returns_split_return_rate = float(worker_cfg.get("returns_split_return_rate", 0.0))
        returns_max_splits = int(worker_cfg.get("returns_max_splits", 3))
        returns_split_min_gap = int(worker_cfg.get("returns_split_min_gap", 3))
        returns_split_max_gap = int(worker_cfg.get("returns_split_max_gap", 20))
        returns_lag_distribution = str(worker_cfg.get("returns_lag_distribution", "uniform") or "uniform")
        returns_lag_mode = int(worker_cfg.get("returns_lag_mode", 7))
        returns_event_key_capacity = int(worker_cfg.get("returns_event_key_capacity", 100000))
        returns_logistics_keys = worker_cfg.get("returns_logistics_keys", [])

        if sales_output in {"sales_order", "both"}:
            skip_order_cols = False

        partition_enabled = bool(worker_cfg.get("partition_enabled", False))
        partition_cols = worker_cfg.get("partition_cols") or []
        models_cfg = worker_cfg.get("models_cfg")

        parquet_dict_exclude = worker_cfg.get("parquet_dict_exclude")

        # NEW: configurable cap for OrderLineNumber per OrderNumber
        max_lines_per_order = int_or(worker_cfg.get("max_lines_per_order"), 5)
        if max_lines_per_order < 1:
            raise RuntimeError(f"max_lines_per_order must be >= 1, got {max_lines_per_order}")

    except KeyError as e:
        raise RuntimeError(f"Missing worker_cfg key: {e}") from e

    product_np = np.asarray(product_np)

    # Brand buckets: reconstruct from pre-computed flat index + offsets
    # (shared memory, no per-worker argsort).
    _brand_flat_idx = worker_cfg.get("_brand_flat_idx")
    _brand_flat_offsets = worker_cfg.get("_brand_flat_offsets")
    if _brand_flat_idx is not None and _brand_flat_offsets is not None:
        _brand_flat_idx = resolve_array(_brand_flat_idx)
        _brand_flat_offsets = resolve_array(_brand_flat_offsets)
        B = len(_brand_flat_offsets) - 1
        brand_to_row_idx = [None] * B
        for b in range(B):
            s, e = int(_brand_flat_offsets[b]), int(_brand_flat_offsets[b + 1])
            if e > s:
                brand_to_row_idx[b] = _brand_flat_idx[s:e]
            else:
                brand_to_row_idx[b] = np.empty(0, dtype=np.int32)
        if product_brand_key is not None:
            product_brand_key = as_int32(product_brand_key)
    elif worker_cfg.get("_prebuilt_brand_to_row_idx") is not None:
        # Legacy path: pre-built jagged shared memory
        from src.utils.shared_arrays import resolve_jagged
        brand_to_row_idx = resolve_jagged(worker_cfg["_prebuilt_brand_to_row_idx"])
        if product_brand_key is not None:
            product_brand_key = as_int32(product_brand_key)
    elif product_brand_key is not None:
        product_brand_key = as_int32(product_brand_key)
        if product_brand_key.shape[0] != product_np.shape[0]:
            raise RuntimeError("product_brand_key must align with product_np row count")
        brand_to_row_idx = _build_buckets_from_brand_key(product_brand_key)
    else:
        brand_to_row_idx = None

    store_keys = as_int32(store_keys)

    # ------------------------------------------------------------
    # Per-month eligible stores (based on OpeningDate / ClosingDate)
    # ------------------------------------------------------------
    store_open_month = worker_cfg.get("store_open_month")
    store_close_month = worker_cfg.get("store_close_month")
    store_eligible_by_month = None
    if store_open_month is not None:
        store_open_month = np.asarray(store_open_month, dtype=np.int64)
        _INT64_MAX = np.iinfo(np.int64).max
        if store_close_month is not None:
            store_close_month = np.asarray(store_close_month, dtype=np.int64)
        else:
            store_close_month = np.full(store_keys.shape[0], _INT64_MAX, dtype=np.int64)
        # Build month_ints from date_pool (unique months as int64)
        dp = np.asarray(date_pool, dtype="datetime64[D]")
        _unique_months = np.unique(dp.astype("datetime64[M]"))
        _month_ints = _unique_months.astype("int64")
        store_eligible_by_month = []
        for mi in _month_ints:
            # Store is eligible if it opened on or before this month
            # and has not closed before this month
            eligible_mask = (store_open_month <= mi) & (store_close_month >= mi)
            store_eligible_by_month.append(store_keys[eligible_mask])
        # Warn if any month has zero eligible stores
        _n_fallback = 0
        for idx, arr in enumerate(store_eligible_by_month):
            if arr.size == 0:
                store_eligible_by_month[idx] = store_keys  # fallback: all stores
                _n_fallback += 1
        if _n_fallback > 0:
            _warn(
                f"{_n_fallback} month(s) had zero eligible stores based on "
                "opening/closing dates; falling back to all stores for those months."
            )

    # ------------------------------------------------------------
    # Day-level store open/close for exact eligibility filtering
    # Build dense lookup: store_key -> opening/closing/renovation day arrays.
    # ------------------------------------------------------------
    _FAR_PAST = np.datetime64("1900-01-01", "D")
    _FAR_FUTURE = np.datetime64("2262-04-11", "D")

    def _dense_by_store_key(values, sentinel):
        """Build a dense array indexed by StoreKey from per-position values."""
        if values is None:
            return None
        vals = np.asarray(values, dtype="datetime64[D]")
        out = np.full(int(store_keys.max()) + 1, sentinel, dtype="datetime64[D]")
        out[store_keys.astype(np.intp)] = vals
        return out

    _store_open_day_dense = _dense_by_store_key(worker_cfg.get("store_open_day"), _FAR_PAST)
    _store_close_day_dense = _dense_by_store_key(worker_cfg.get("store_close_day"), _FAR_FUTURE)

    def _dense_float_by_store_key(values, fill=1.0):
        """Dense float array indexed by StoreKey from per-position values.

        When ``values`` is None (store_demand_weight unconfigured) the result is
        all-``fill`` (all-ones), so the single weighted store sampler produces
        uniform draws — there is no separate uniform code path."""
        out = np.full(int(store_keys.max()) + 1, float(fill), dtype=np.float64)
        if values is not None:
            out[store_keys.astype(np.intp)] = np.asarray(values, dtype=np.float64)
        return out

    _store_demand_weight_dense = _dense_float_by_store_key(
        worker_cfg.get("store_demand_weight"), 1.0,
    )

    # Renovation windows. Sentinels chosen so a non-renovating store's
    # window [far_future, far_past] never overlaps any month/date.
    _store_reno_start_day_dense = _dense_by_store_key(
        worker_cfg.get("store_reno_start_day"), _FAR_FUTURE,
    )
    _store_reno_end_day_dense = _dense_by_store_key(
        worker_cfg.get("store_reno_end_day"), _FAR_PAST,
    )

    # ------------------------------------------------------------
    # Store-product assortment (optional)
    # ------------------------------------------------------------
    store_to_product_rows = None

    # New compact path: expand subcat matrix to row indices per-worker
    _assort_matrix_desc = worker_cfg.get("_assortment_subcat_matrix")
    _assort_subcats_desc = worker_cfg.get("_assortment_unique_subcats")
    if _assort_matrix_desc is not None and _assort_subcats_desc is not None:
        _subcat_matrix = resolve_array(_assort_matrix_desc)
        _unique_subcats = resolve_array(_assort_subcats_desc)
        product_subcat_key_arr = worker_cfg.get("product_subcat_key")
        if product_subcat_key_arr is not None:
            product_subcat_key_arr = np.asarray(product_subcat_key_arr, dtype=np.int32)
            store_to_product_rows = _expand_store_assortment_from_matrix(
                store_keys=store_keys,
                unique_subcats=_unique_subcats,
                subcat_matrix=_subcat_matrix,
                product_subcat_key=product_subcat_key_arr,
                n_products=len(product_np),
            )

    # Legacy fallback: pre-built jagged shared memory (backward compat)
    if store_to_product_rows is None:
        _prebuilt_assortment = worker_cfg.get("_prebuilt_store_to_product_rows")
        if _prebuilt_assortment is not None:
            from src.utils.shared_arrays import resolve_jagged
            store_to_product_rows = resolve_jagged(_prebuilt_assortment)

    # Final fallback: build from scratch (no shared memory at all)
    if store_to_product_rows is None:
        assortment_cfg = worker_cfg.get("assortment")
        if isinstance(assortment_cfg, Mapping) and assortment_cfg.get("enabled"):
            product_subcat_key = worker_cfg.get("product_subcat_key")
            store_type_map = worker_cfg.get("store_type_map")
            if product_subcat_key is not None and store_type_map is not None:
                product_subcat_key = np.asarray(product_subcat_key, dtype=np.int32)
                store_type_arr = np.array(
                    [str(store_type_map.get(int(sk), "Supermarket")) for sk in store_keys],
                    dtype=object,
                )
                coverage = assortment_cfg.get("coverage", _DEFAULT_ASSORTMENT_COVERAGE)
                assort_seed = int(assortment_cfg.get("seed", worker_cfg.get("seed_master", 42)))
                store_to_product_rows = _build_store_assortment(
                    store_keys=store_keys,
                    store_type_arr=store_type_arr,
                    product_np=product_np,
                    product_subcat_key=product_subcat_key,
                    coverage_cfg=coverage,
                    seed=assort_seed,
                )

    # ------------------------------------------------------------
    # Filter employee assignment rows to sales-eligible roles
    # ------------------------------------------------------------
    _prebuilt_sp = worker_cfg.get("_prebuilt_salesperson_effective_by_store")
    if _prebuilt_sp is not None:
        # Pre-built in main process — skip redundant filtering & construction
        salesperson_effective_by_store = _prebuilt_sp
        salesperson_global_pool = worker_cfg.get("_prebuilt_salesperson_global_pool")
    else:
        if employee_assign_employee_key is not None and employee_assign_store_key is not None:
            emp_key = np.asarray(employee_assign_employee_key, dtype=np.int32)

            if employee_assign_role is None:
                raise RuntimeError(
                    "RoleAtStore is required in employee_store_assignments.parquet. "
                    "Regenerate the bridge table."
                )
            role_arr = np.asarray(employee_assign_role).astype(str)
            allowed = np.asarray(list(salesperson_roles), dtype=str)
            mask = np.isin(role_arr, allowed)

            if not mask.any():
                raise RuntimeError(
                    f"No employees with role in {salesperson_roles} found in bridge. "
                    f"Roles present: {np.unique(role_arr).tolist()}"
                )

            employee_assign_store_key = np.asarray(employee_assign_store_key, dtype=np.int32)[mask]
            employee_assign_employee_key = emp_key[mask]
            employee_assign_start_date = np.asarray(employee_assign_start_date, dtype="datetime64[D]")[mask]
            employee_assign_end_date = np.asarray(employee_assign_end_date, dtype="datetime64[D]")[mask]

            if employee_assign_fte is not None:
                employee_assign_fte = np.asarray(employee_assign_fte, dtype=np.float64)[mask]
            if employee_assign_is_primary is not None:
                employee_assign_is_primary = np.asarray(employee_assign_is_primary, dtype=bool)[mask]

            salesperson_global_pool = np.unique(employee_assign_employee_key).astype(np.int32, copy=False)
        else:
            salesperson_global_pool = None

        salesperson_effective_by_store = _build_salesperson_effective_by_store(
            store_keys=store_keys,
            assign_store=employee_assign_store_key,
            assign_emp=employee_assign_employee_key,
            assign_start=employee_assign_start_date,
            assign_end=employee_assign_end_date,
            assign_fte=employee_assign_fte,
            assign_is_primary=employee_assign_is_primary,
            primary_boost=employee_primary_boost,
            perf_spread=salesperson_perf_spread,
            perf_seed=salesperson_perf_seed,
        )

    # Refine store eligibility from the bridge table: a store is excluded
    # from a month if it has no salesperson coverage on the first or last
    # day of that month, OR if its renovation window overlaps that month
    # (renovations entirely inside one month escape the staff-coverage
    # check because pre-reno covers first day and post-reno covers last
    # day, even though the middle of the month has no coverage).
    if store_eligible_by_month is not None and salesperson_effective_by_store:
        _pool_first_day = dp[0] if dp.size > 0 else None
        _pool_last_day = dp[-1] if dp.size > 0 else None
        # Renovation overlap is checked against the dense per-StoreKey arrays
        # (sentinels never overlap, so non-renovating stores pass through).
        _reno_active = _store_reno_start_day_dense is not None and _store_reno_end_day_dense is not None

        for midx, month_M in enumerate(_unique_months):
            first_day = month_M.astype("datetime64[D]")
            last_day = (month_M + np.timedelta64(1, "M")).astype("datetime64[D]") - np.timedelta64(1, "D")
            if _pool_first_day is not None and first_day < _pool_first_day:
                first_day = _pool_first_day
            if _pool_last_day is not None and last_day > _pool_last_day:
                last_day = _pool_last_day
            eligible_arr = store_eligible_by_month[midx]
            staffed_mask = np.ones(eligible_arr.shape[0], dtype=bool)
            for sidx, sk in enumerate(eligible_arr):
                sk_int = int(sk)
                if _reno_active:
                    rs = _store_reno_start_day_dense[sk_int]
                    re = _store_reno_end_day_dense[sk_int]
                    # Renovation window is [rs, re) — store reopens on re.
                    if rs <= last_day and re > first_day:
                        staffed_mask[sidx] = False
                        continue
                sp_data = salesperson_effective_by_store.get(sk_int)
                if sp_data is None:
                    staffed_mask[sidx] = False
                    continue
                _, starts, ends, _ = sp_data
                if not (np.any((starts <= first_day) & (ends >= first_day))
                        and np.any((starts <= last_day) & (ends >= last_day))):
                    staffed_mask[sidx] = False
            if not staffed_mask.all():
                store_eligible_by_month[midx] = eligible_arr[staffed_mask]

        _n_fallback_staffed = sum(1 for arr in store_eligible_by_month if arr.size == 0)
        for midx, arr in enumerate(store_eligible_by_month):
            if arr.size == 0:
                # Restore the open/close-only subset (recomputed from store_open_month /
                # store_close_month). Per-day renovation filtering still happens at chunk
                # time, so any renovating store sampled here gets resampled away. Lines
                # that still land on an unstaffed (store, date) are dropped at the end of
                # the chunk builder, so this never emits EmployeeKey=-1.
                _mi = _month_ints[midx]
                _restored = store_keys[(store_open_month <= _mi) & (store_close_month >= _mi)]
                store_eligible_by_month[midx] = _restored if _restored.size > 0 else store_keys
        if _n_fallback_staffed > 0:
            _warn(
                f"{_n_fallback_staffed} month(s) had zero staffed stores; "
                "falling back to open-this-month stores (staffing requirement relaxed)."
            )

    promo_keys_all = as_int32(promo_keys_all)
    promo_start_all = np.asarray(promo_start_all, dtype="datetime64[D]")
    promo_end_all = np.asarray(promo_end_all, dtype="datetime64[D]")
    if new_customer_promo_keys is not None and len(new_customer_promo_keys) > 0:
        new_customer_promo_keys = as_int32(new_customer_promo_keys)
    else:
        new_customer_promo_keys = None

    customer_keys = as_int32(customer_keys)

    if customer_is_active_in_sales is not None:
        customer_is_active_in_sales = as_int32(customer_is_active_in_sales)
        if customer_is_active_in_sales.shape[0] != customer_keys.shape[0]:
            raise RuntimeError("customer_is_active_in_sales must align with customer_keys length")

    if customer_start_month is not None:
        customer_start_month = as_int64(customer_start_month)
        if customer_start_month.shape[0] != customer_keys.shape[0]:
            raise RuntimeError("customer_start_month must align with customer_keys length")

    if customer_end_month is not None:
        customer_end_month = as_int64(customer_end_month)
        if customer_end_month.shape[0] != customer_keys.shape[0]:
            raise RuntimeError("customer_end_month must align with customer_keys length")

    if customer_base_weight is not None:
        customer_base_weight = np.asarray(customer_base_weight, dtype=np.float64)
        if customer_base_weight.shape[0] != customer_keys.shape[0]:
            raise RuntimeError("customer_base_weight must align with customer_keys length")

    # Use pre-built brand_prob_by_month from shared memory if available
    _prebuilt_bp = worker_cfg.get("_prebuilt_brand_prob_by_month")
    if _prebuilt_bp is not None:
        brand_prob_by_month = resolve_array(_prebuilt_bp)
    else:
        brand_prob_by_month = None
        if isinstance(models_cfg, Mapping):
            brand_cfg = models_cfg.get("brand_popularity") if isinstance(models_cfg, Mapping) else None
            if brand_cfg:
                T = infer_T_from_date_pool(date_pool)
                B = int(product_brand_key.max()) + 1 if product_brand_key is not None and product_brand_key.size else 0
                rng_bp = np.random.default_rng(int(int_or(brand_cfg.get("seed"), 1234)))

                bp_counts = None
                if brand_to_row_idx is not None and len(brand_to_row_idx) == B:
                    bp_counts = np.array(
                        [len(b) if b is not None else 0 for b in brand_to_row_idx],
                        dtype=np.float64,
                    )

                brand_prob_by_month = _build_brand_prob_by_month_rotate_winner(
                    rng_bp,
                    T=T,
                    B=B,
                    winner_boost=float_or(brand_cfg.get("winner_boost"), 2.5),
                    noise_sd=float_or(brand_cfg.get("noise_sd"), 0.15),
                    min_share=float_or(brand_cfg.get("min_share"), 0.02),
                    year_len_months=int_or(brand_cfg.get("year_len_months"), 12),
                    brand_product_counts=bp_counts,
                    count_exponent=float_or(brand_cfg.get("count_exponent"), 0.25),
                )

    store_to_geo_arr = dense_map(store_to_geo) if isinstance(store_to_geo, Mapping) else None
    geo_to_currency_arr = dense_map(geo_to_currency) if isinstance(geo_to_currency, Mapping) else None

    output_paths = OutputPaths(
        out_folder=out_folder,
        delta_output_folder=delta_output_folder,
        file_format=file_format,
        merged_file=merged_file,
    )

    tables: list[str] = []
    if sales_output in {"sales", "both"}:
        tables.append(TABLE_SALES)
    if sales_output in {"sales_order", "both"}:
        tables += [TABLE_SALES_ORDER_DETAIL, TABLE_SALES_ORDER_HEADER]
    if returns_enabled:
        tables.append(TABLE_SALES_RETURN)
    for t in tables:
        output_paths.ensure_dirs(t)

    bundle = build_worker_schemas(
        file_format=file_format,
        skip_order_cols=bool(skip_order_cols),
        skip_order_cols_requested=bool(skip_order_cols_requested),
        returns_enabled=bool(returns_enabled),
        parquet_dict_exclude=set(parquet_dict_exclude) if parquet_dict_exclude else None,
        models_cfg=models_cfg if isinstance(models_cfg, Mapping) else None,
        order_id_int64=order_id_int64,
        partition_cols=partition_cols if partition_cols else None,
    )

    # ---- Budget lookups (already built in main process, passed as flat keys) ----
    budget_enabled = bool(worker_cfg.get("budget_enabled", False))
    budget_store_to_country = worker_cfg.get("budget_store_to_country")
    budget_product_to_cat = worker_cfg.get("budget_product_to_cat")

    bind_globals(
        {
            "product_np": product_np,
            "brand_to_row_idx": brand_to_row_idx,
            "product_brand_key": product_brand_key,
            "brand_prob_by_month": brand_prob_by_month,
            "store_keys": store_keys,
            "store_eligible_by_month": store_eligible_by_month,
            "store_open_day": _store_open_day_dense,
            "store_close_day": _store_close_day_dense,
            "store_reno_start_day": _store_reno_start_day_dense,
            "store_reno_end_day": _store_reno_end_day_dense,
            "store_demand_weight": _store_demand_weight_dense,
            "promo_keys_all": promo_keys_all,
            "promo_start_all": promo_start_all,
            "promo_end_all": promo_end_all,
            "new_customer_promo_keys": new_customer_promo_keys,
            "new_customer_window_months": new_customer_window_months,
            "customer_keys": customer_keys,
            "customer_is_active_in_sales": customer_is_active_in_sales,
            "customer_start_month": customer_start_month,
            "customer_end_month": customer_end_month,
            "customer_base_weight": customer_base_weight,
            "store_to_geo_arr": store_to_geo_arr,
            "geo_to_currency_arr": geo_to_currency_arr,
            "date_pool": date_pool,
            "date_prob": date_prob,
            "file_format": file_format,
            "out_folder": out_folder,

            # CRITICAL: constant per-run stride used to partition OrderNumber ranges
            "chunk_size": int(max(1, chunk_size)),
            "order_id_stride_orders": int(max(1, chunk_size)),
            "month_stride": month_stride,
            "per_chunk_alloc": per_chunk_alloc,
            "order_id_int64": order_id_int64,
            "max_lines_per_order": int(max_lines_per_order),
            
            "row_group_size": int(max(1, row_group_size)),
            "compression": compression,
            "output_paths": output_paths,
            "sales_output": sales_output,
            "skip_order_cols_requested": bool(skip_order_cols_requested),
            "skip_order_cols": bool(skip_order_cols),
            "delta_output_folder": os.path.normpath(delta_output_folder) if delta_output_folder else None,
            "write_delta": bool(write_delta),
            "no_discount_key": no_discount_key,
            "partition_enabled": bool(partition_enabled),
            "partition_cols": list(partition_cols),
            "models_cfg": models_cfg,
            "date_cols_by_table": bundle.date_cols_by_table,
            "schema_no_order": bundle.schema_no_order,
            "schema_with_order": bundle.schema_with_order,
            "schema_no_order_delta": bundle.schema_no_order_delta,
            "schema_with_order_delta": bundle.schema_with_order_delta,
            "sales_schema": bundle.sales_schema_gen,
            "sales_schema_out": bundle.sales_schema_out,
            "schema_by_table": bundle.schema_by_table,
            "parquet_dict_cols_by_table": bundle.parquet_dict_cols_by_table,
            "parquet_dict_exclude": bundle.parquet_dict_exclude,
            "parquet_dict_cols": bundle.parquet_dict_cols,
            "returns_enabled": bool(returns_enabled),
            "returns_rate": float(returns_rate),
            "returns_min_lag_days": int(max(0, returns_min_lag_days)),
            "returns_max_lag_days": int(returns_max_lag_days),
            "returns_reason_keys": returns_reason_keys,
            "returns_reason_probs": returns_reason_probs,
            "returns_full_line_probability": returns_full_line_probability,
            "returns_split_return_rate": returns_split_return_rate,
            "returns_max_splits": returns_max_splits,
            "returns_split_min_gap": returns_split_min_gap,
            "returns_split_max_gap": returns_split_max_gap,
            "returns_lag_distribution": returns_lag_distribution,
            "returns_lag_mode": returns_lag_mode,
            "returns_event_key_capacity": returns_event_key_capacity,
            "returns_logistics_keys": returns_logistics_keys,
            # EMPLOYEE assignment (canonical day-accurate bridge)
            "salesperson_effective_by_store": salesperson_effective_by_store,
            "salesperson_global_pool": salesperson_global_pool,

            "parquet_folder": worker_cfg.get("parquet_folder"),

            "budget_enabled": budget_enabled,
            "budget_store_to_country": budget_store_to_country,
            "budget_product_to_cat": budget_product_to_cat,

            "inventory_enabled": bool(worker_cfg.get("inventory_enabled", False)),
            "inventory_store_to_warehouse": worker_cfg.get("inventory_store_to_warehouse"),
            "wishlists_enabled": bool(worker_cfg.get("wishlists_enabled", False)),
            "complaints_enabled": bool(worker_cfg.get("complaints_enabled", False)),

            "store_to_product_rows": store_to_product_rows,

            # Product profile attributes for weighted sampling
            "product_popularity": worker_cfg.get("product_popularity"),
            "product_seasonality": worker_cfg.get("product_seasonality"),

            # Column correlation data
            "customer_geo_key": worker_cfg.get("customer_geo_key"),
            "geo_to_country_id": worker_cfg.get("geo_to_country_id"),
            "store_to_country_id": worker_cfg.get("store_to_country_id"),
            "country_to_store_keys": worker_cfg.get("country_to_store_keys"),
            "store_channel_keys": worker_cfg.get("store_channel_keys"),
            "channel_prob_by_store": worker_cfg.get("channel_prob_by_store"),
            "product_channel_eligible": worker_cfg.get("product_channel_eligible"),
            "promo_channel_group": worker_cfg.get("promo_channel_group"),
            "channel_fulfillment_days": worker_cfg.get("channel_fulfillment_days"),
            "_channel_to_elig_group": worker_cfg.get("_channel_to_elig_group"),

            # SCD2 version grids
            "product_scd2_active": bool(worker_cfg.get("product_scd2_active", False)),
            "product_scd2_starts": worker_cfg.get("product_scd2_starts"),
            "product_scd2_data": worker_cfg.get("product_scd2_data"),
            "customer_scd2_active": bool(worker_cfg.get("customer_scd2_active", False)),
            "customer_scd2_starts": worker_cfg.get("customer_scd2_starts"),
            "customer_scd2_keys": worker_cfg.get("customer_scd2_keys"),
            "cust_key_to_pool_idx": worker_cfg.get("cust_key_to_pool_idx"),

            # Header invariant validation toggle (default False for perf)
            "validate_header_invariants": bool(worker_cfg.get("validate_header_invariants", False)),
        }
    )

    # Validate critical State attributes once at worker init instead of per-task
    State.validate(["chunk_size"])

