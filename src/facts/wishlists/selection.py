"""Unified product selection loop for wishlists.

Used by both the serial path (runner.py) and the parallel path (worker.py).
Both paths pre-build the same flat-array subcategory pool format, then call
``generate_wishlist_items()`` which handles batch random generation and the
per-customer product selection loop.
"""
from __future__ import annotations

from bisect import bisect_left
from typing import Any, Dict, List, NamedTuple, Tuple

import numpy as np

from .constants import NS_PER_DAY, PRIORITY_VALUES, PRIORITY_WEIGHTS


# Per-item product de-dup retry bounds. The misc-RNG pool headroom
# (_MISC_PER_ITEM_MAX in generate_wishlist_items) is derived from these, so they
# are the single source — changing a loop bound updates the headroom too (WISH-1).
_AFFINITY_RETRY_LIMIT = 50
_DEDUP_RETRY_LIMIT = 200


# ---------------------------------------------------------------------------
# Subcategory pool — flat-array format for fast, picklable access
# ---------------------------------------------------------------------------

class SubcatPool(NamedTuple):
    """Flat-array representation of per-subcategory product pools and CDFs."""
    sc_idx_map: Dict[int, int]      # subcategory_id → index in arrays below
    subcat_starts: np.ndarray       # pool_indices start per subcategory
    subcat_ends: np.ndarray         # pool_indices end (exclusive) per subcategory
    pool_indices: np.ndarray        # concatenated product indices per subcategory
    subcat_cdf_starts: np.ndarray   # cdf_data start per subcategory
    subcat_cdf_ends: np.ndarray     # cdf_data end (exclusive) per subcategory
    cdf_data: np.ndarray            # concatenated per-subcategory CDFs


def build_subcategory_pool(
    prod_subcat: np.ndarray,
    product_weights: np.ndarray,
) -> Tuple[SubcatPool, np.ndarray]:
    """Build flat-array subcategory pool and global CDF from product arrays.

    Returns:
        (pool, global_cdf) — pool for selection loop, global_cdf for fallback picks.
    """
    if product_weights.size == 0:
        raise ValueError("No products available for wishlist generation")
    global_cdf = np.cumsum(product_weights)
    global_cdf[-1] = 1.0

    unique_subcats = np.unique(prod_subcat)

    sc_idx_map: Dict[int, int] = {}
    subcat_starts_list: List[int] = []
    subcat_ends_list: List[int] = []
    pool_indices_list: List[int] = []
    subcat_cdf_starts_list: List[int] = []
    subcat_cdf_ends_list: List[int] = []
    cdf_data_list: List[float] = []

    pool_cursor = 0
    cdf_cursor = 0
    for i, sc in enumerate(unique_subcats):
        sc_idx_map[int(sc)] = i
        idx = np.where(prod_subcat == sc)[0]
        w = product_weights[idx]
        ws = float(w.sum())

        subcat_starts_list.append(pool_cursor)
        subcat_ends_list.append(pool_cursor + len(idx))
        pool_indices_list.extend(idx.tolist())

        subcat_cdf_starts_list.append(cdf_cursor)
        if ws > 0:
            sc_cdf = np.cumsum(w / ws)
            sc_cdf[-1] = 1.0
            cdf_data_list.extend(sc_cdf.tolist())
            subcat_cdf_ends_list.append(cdf_cursor + len(sc_cdf))
            cdf_cursor += len(sc_cdf)
        else:
            subcat_cdf_ends_list.append(cdf_cursor)

        pool_cursor += len(idx)

    pool = SubcatPool(
        sc_idx_map=sc_idx_map,
        subcat_starts=np.array(subcat_starts_list, dtype=np.int64),
        subcat_ends=np.array(subcat_ends_list, dtype=np.int64),
        pool_indices=np.array(pool_indices_list, dtype=np.int64),
        subcat_cdf_starts=np.array(subcat_cdf_starts_list, dtype=np.int64),
        subcat_cdf_ends=np.array(subcat_cdf_ends_list, dtype=np.int64),
        cdf_data=np.array(cdf_data_list, dtype=np.float64),
    )
    return pool, global_cdf


def subcategory_pool_from_dict(product_data: Dict[str, Any]) -> SubcatPool:
    """Reconstruct a SubcatPool from a picklable dict (worker path)."""
    subcat_keys = np.asarray(product_data["subcat_keys"], dtype=np.int64)
    sc_idx_map = {int(sc): i for i, sc in enumerate(subcat_keys)}
    return SubcatPool(
        sc_idx_map=sc_idx_map,
        subcat_starts=np.asarray(product_data["subcat_starts"], dtype=np.int64),
        subcat_ends=np.asarray(product_data["subcat_ends"], dtype=np.int64),
        pool_indices=np.asarray(product_data["pool_indices"], dtype=np.int64),
        subcat_cdf_starts=np.asarray(product_data["subcat_cdf_starts"], dtype=np.int64),
        subcat_cdf_ends=np.asarray(product_data["subcat_cdf_ends"], dtype=np.int64),
        cdf_data=np.asarray(product_data["cdf_data"], dtype=np.float64),
    )


def pool_to_dict(pool: SubcatPool) -> Dict[str, Any]:
    """Serialize a SubcatPool into a picklable dict for workers."""
    # Reconstruct subcat_keys from sc_idx_map (index → key)
    inv = {v: k for k, v in pool.sc_idx_map.items()}
    subcat_keys = [inv[i] for i in range(len(inv))]
    return {
        "subcat_keys": subcat_keys,
        "subcat_starts": pool.subcat_starts.tolist(),
        "subcat_ends": pool.subcat_ends.tolist(),
        "pool_indices": pool.pool_indices.tolist(),
        "subcat_cdf_starts": pool.subcat_cdf_starts.tolist(),
        "subcat_cdf_ends": pool.subcat_cdf_ends.tolist(),
        "cdf_data": pool.cdf_data.tolist(),
    }


# ---------------------------------------------------------------------------
# Unified selection loop
# ---------------------------------------------------------------------------

def generate_wishlist_items(
    rng: np.random.Generator,
    *,
    cust_keys: np.ndarray,
    earliest_ns: np.ndarray,
    latest_ns: np.ndarray,
    items_per: np.ndarray,
    purchased_map: Dict[int, List[int]],
    prod_subcat: np.ndarray,
    n_products: int,
    global_cdf: np.ndarray,
    pool: SubcatPool,
    conversion_rate: float,
    affinity_strength: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Batch random generation + per-customer product selection.

    All arrays are indexed directly by participant index (0..n_participants-1).
    The caller is responsible for pre-filtering to participants.

    Args:
        rng: numpy Generator (already seeded).
        cust_keys: customer keys for each participant.
        earliest_ns: earliest wishlist date (nanoseconds) per participant.
        latest_ns: latest wishlist date (nanoseconds) per participant.
        items_per: number of wishlist items per participant.
        purchased_map: {customer_key: [product_indices]} from sales data.
        prod_subcat: subcategory id per product (full product array).
        n_products: total number of products.
        global_cdf: cumulative probability distribution over all products.
        pool: flat-array subcategory pool for affinity lookups.
        conversion_rate: probability of picking from purchases.
        affinity_strength: probability of picking from same subcategory.

    Returns:
        (out_prod_idx, out_ckey, out_date_ns, out_priority, out_quantity)
    """
    n_participants = len(cust_keys)
    total_rows = int(items_per.sum())

    if total_rows == 0 or n_participants == 0:
        empty_i = np.empty(0, dtype=np.int64)
        empty_s = np.empty(0, dtype=object)
        empty_q = np.empty(0, dtype=np.int32)
        return empty_i.copy(), empty_i.copy(), empty_i.copy(), empty_s, empty_q

    # ------------------------------------------------------------------
    # BATCH ALL RANDOM NUMBER GENERATION UPFRONT
    # ------------------------------------------------------------------
    _base_prod_idx = np.searchsorted(global_cdf, rng.random(total_rows))
    np.clip(_base_prod_idx, 0, n_products - 1, out=_base_prod_idx)

    _conv_rolls = rng.random(total_rows)
    _aff_rolls = rng.random(total_rows)

    # Worst-case misc draws a single item (one `j`) can consume before the
    # end-of-item refill: anchor (1) + affinity-CDF retry loop + default dedup retry
    # loop. Refilling with at least this much headroom keeps any one item from
    # exhausting the pool mid-iteration -> IndexError (WISH-1).
    _MISC_PER_ITEM_MAX = 1 + _AFFINITY_RETRY_LIMIT + _DEDUP_RETRY_LIMIT
    _MISC_SIZE = max(total_rows * 6, 1024)
    _misc = rng.random(_MISC_SIZE)
    _mi = 0

    _date_rands = rng.random(total_rows)

    _prio_cdf = np.cumsum(PRIORITY_WEIGHTS)
    _prio_cdf[-1] = 1.0
    _prio_idx = np.searchsorted(_prio_cdf, rng.random(total_rows))
    np.clip(_prio_idx, 0, len(PRIORITY_VALUES) - 1, out=_prio_idx)
    out_priority = PRIORITY_VALUES[_prio_idx]

    _qty_options = np.array([1, 1, 1, 1, 2, 2, 3], dtype=np.int32)
    out_quantity = _qty_options[rng.integers(0, 7, size=total_rows)]

    # ------------------------------------------------------------------
    # OUTPUT ARRAYS
    # ------------------------------------------------------------------
    out_prod_idx = _base_prod_idx.copy()

    offsets = np.zeros(n_participants + 1, dtype=np.int64)
    np.cumsum(items_per, out=offsets[1:])

    out_ckey = np.repeat(cust_keys, items_per)

    _span = (latest_ns - earliest_ns).astype(np.int64)
    _span = np.where(_span <= 0, NS_PER_DAY, _span)
    _e_rep = np.repeat(earliest_ns, items_per).astype(np.int64)
    _s_rep = np.repeat(_span, items_per)
    out_date_ns = _e_rep + (_date_rands * _s_rep).astype(np.int64)

    # Per-customer product selection with bisect for scalar CDF lookups
    # (avoids numpy call overhead that dominates at single-element scale)
    _prod_subcat = prod_subcat
    _n_products = n_products

    sc_idx_map = pool.sc_idx_map
    subcat_starts = pool.subcat_starts
    subcat_ends = pool.subcat_ends
    _pool_indices_arr = pool.pool_indices
    subcat_cdf_starts = pool.subcat_cdf_starts
    subcat_cdf_ends = pool.subcat_cdf_ends

    _global_cdf_list = global_cdf.tolist()
    _cdf_data_list = pool.cdf_data.tolist()

    for i in range(n_participants):
        start = int(offsets[i])
        end = int(offsets[i + 1])
        n_items = end - start
        if n_items == 0:
            continue
        ck = int(cust_keys[i])

        purch_list: List[int] = purchased_map.get(ck) or []
        has_purch = len(purch_list) > 0

        chosen_set: set = set()
        for j in range(n_items):
            pos = start + j
            picked = False

            # --- Conversion: pick from actual purchases ---
            if has_purch and _conv_rolls[pos] < conversion_rate:
                avail = [p for p in purch_list if p not in chosen_set]
                if avail:
                    r = float(_misc[_mi]); _mi += 1
                    out_prod_idx[pos] = avail[min(int(r * len(avail)), len(avail) - 1)]
                    chosen_set.add(int(out_prod_idx[pos]))
                    picked = True

            # --- Affinity: pick from same subcategory ---
            if not picked and j > 0 and _aff_rolls[pos] < affinity_strength:
                r = float(_misc[_mi]); _mi += 1
                anchor_j = min(int(r * j), j - 1)
                anchor = int(out_prod_idx[start + anchor_j])
                sc = int(_prod_subcat[anchor])
                sc_i = sc_idx_map.get(sc)
                if sc_i is not None:
                    ps = int(subcat_starts[sc_i])
                    pe = int(subcat_ends[sc_i])
                    pool_len = pe - ps
                    cs = int(subcat_cdf_starts[sc_i])
                    ce = int(subcat_cdf_ends[sc_i])
                    has_cdf = ce > cs
                    if has_cdf and pool_len > 0:
                        sc_cdf_slice = _cdf_data_list[cs:ce]
                        for _ in range(_AFFINITY_RETRY_LIMIT):
                            r = float(_misc[_mi]); _mi += 1
                            local_idx = bisect_left(sc_cdf_slice, r)
                            if local_idx >= pool_len:
                                local_idx = pool_len - 1
                            pick = int(_pool_indices_arr[ps + local_idx])
                            if pick not in chosen_set:
                                out_prod_idx[pos] = pick
                                chosen_set.add(pick)
                                picked = True
                                break
                    elif pool_len > 0:
                        r = float(_misc[_mi]); _mi += 1
                        pick = int(_pool_indices_arr[ps + min(int(r * pool_len), pool_len - 1)])
                        if pick not in chosen_set:
                            out_prod_idx[pos] = pick
                            chosen_set.add(pick)
                            picked = True

            # --- Default: use pre-generated global pick, dedup ---
            if not picked:
                pick = int(out_prod_idx[pos])
                if pick in chosen_set:
                    for _ in range(_DEDUP_RETRY_LIMIT):
                        r = float(_misc[_mi]); _mi += 1
                        pick = bisect_left(_global_cdf_list, r)
                        if pick >= _n_products:
                            pick = _n_products - 1
                        if pick not in chosen_set:
                            out_prod_idx[pos] = pick
                            break
                    else:
                        for p in range(_n_products):
                            if p not in chosen_set:
                                out_prod_idx[pos] = p
                                break
                chosen_set.add(int(out_prod_idx[pos]))

            # Refill misc pool if it could be exhausted by the next item.
            if _mi + _MISC_PER_ITEM_MAX > len(_misc):
                _misc = rng.random(_MISC_SIZE)
                _mi = 0

    return out_prod_idx, out_ckey, out_date_ns, out_priority, out_quantity
