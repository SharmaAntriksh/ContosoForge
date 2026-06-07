"""Per-chunk micro-aggregation for budget.

Called inside each worker's _worker_task() after the Arrow table is built.
Collapses ~2M rows to ~1K rows grouped by (country_id, category_id, year, month, channel).

Returns a small dict of numpy arrays (trivial to pickle across IPC).

Performance: ~5-10ms per 2M-row chunk (numpy bincount, single O(n) pass).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pyarrow as pa


# ----------------------------------------------------------------
# Shared column extraction
# ----------------------------------------------------------------

def _build_dimension_arrays(
    store_keys: np.ndarray,
    product_keys: np.ndarray,
    channel_raw: np.ndarray,
    order_dates: np.ndarray,
    *,
    store_to_country: np.ndarray,
    product_to_cat: np.ndarray,
) -> Dict[str, np.ndarray]:
    """Derive dimension arrays and flat composite key for bincount aggregation.

    Accepts raw numpy arrays (extracted from either an Arrow table or a join
    result) so both sales and returns micro-aggregation can share this logic.

    Returns a dict with: country_id, category_id, year, month, channel_idx,
    channel_uniq, and the per-dimension cardinalities / strides needed for
    the flat composite key.
    """
    # Bounds-safe lookup: keys beyond the lookup array map to 0 (first bucket)
    # rather than raising IndexError or leaving -1 (which breaks np.bincount).
    _sk_safe = np.clip(store_keys, 0, len(store_to_country) - 1)
    _pk_safe = np.clip(product_keys, 0, len(product_to_cat) - 1)
    country_id = store_to_country[_sk_safe]
    category_id = product_to_cat[_pk_safe]

    # Replace any -1 sentinel values (unmapped keys) with 0 so flat_key
    # stays non-negative — np.bincount requires non-negative indices.
    country_id = np.where(country_id < 0, 0, country_id)
    category_id = np.where(category_id < 0, 0, category_id)

    m_int = order_dates.astype("datetime64[M]").astype(np.int64)
    year = (m_int // 12 + 1970).astype(np.int32)
    month = (m_int % 12 + 1).astype(np.int32)

    # Channels: remap to 0-based dense index (with bounds check)
    channel_uniq = np.unique(channel_raw)
    n_ch = int(channel_uniq.size)
    if n_ch == 0:
        raise ValueError("Budget micro-agg: no channel keys found in chunk")
    ch_min = int(channel_uniq.min())
    if ch_min < 0:
        raise ValueError(f"Budget micro-agg: negative channel key {ch_min}")
    ch_max = int(channel_uniq.max()) + 1
    ch_remap = np.zeros(ch_max, dtype=np.int32)
    ch_remap[channel_uniq] = np.arange(n_ch, dtype=np.int32)
    channel_idx = ch_remap[channel_raw.astype(np.int64, copy=False)]

    n_country = int(store_to_country.max()) + 1
    n_cat = int(product_to_cat.max()) + 1
    min_year = int(year.min())
    n_year = int(year.max()) - min_year + 1

    stride_ch = n_ch
    stride_m = 12 * stride_ch
    stride_y = n_year * stride_m
    stride_cat = n_cat * stride_y
    total_cells = n_country * stride_cat

    if total_cells > 2**62:
        raise OverflowError(f"Budget micro-aggregation stride would overflow: {total_cells} cells")

    flat_key = (
        country_id.astype(np.int64) * stride_cat
        + category_id.astype(np.int64) * stride_y
        + (year - min_year).astype(np.int64) * stride_m
        + (month - 1).astype(np.int64) * stride_ch
        + channel_idx.astype(np.int64)
    )

    return {
        "country_id": country_id,
        "category_id": category_id,
        "year": year,
        "month": month,
        "channel_idx": channel_idx,
        "channel_uniq": channel_uniq,
        "min_year": min_year,
        "flat_key": flat_key,
        "total_cells": total_cells,
        "stride_cat": stride_cat,
        "stride_y": stride_y,
        "stride_m": stride_m,
        "stride_ch": stride_ch,
    }


def _extract_columns_from_table(
    table: pa.Table,
    *,
    store_to_country: np.ndarray,
    product_to_cat: np.ndarray,
) -> Dict[str, np.ndarray]:
    """Extract dimension columns from an Arrow table and build flat key."""
    return _build_dimension_arrays(
        store_keys=table.column("StoreKey").to_numpy(zero_copy_only=False).astype(np.int64),
        product_keys=table.column("ProductKey").to_numpy(zero_copy_only=False).astype(np.int64),
        channel_raw=table.column("SalesChannelKey").to_numpy(zero_copy_only=False),
        order_dates=table.column("OrderDate").to_numpy(zero_copy_only=False),
        store_to_country=store_to_country,
        product_to_cat=product_to_cat,
    )


def _decode_flat_key(
    indices: np.ndarray,
    *,
    stride_cat: int,
    stride_y: int,
    stride_m: int,
    stride_ch: int,
    min_year: int,
    channel_uniq: np.ndarray,
) -> Dict[str, np.ndarray]:
    """Decode composite flat keys back into dimension arrays."""
    out_country, rem = np.divmod(indices, stride_cat)
    out_cat, rem = np.divmod(rem, stride_y)
    out_year_idx, rem = np.divmod(rem, stride_m)
    out_month_idx, out_ch_idx = np.divmod(rem, stride_ch)

    return {
        "country_id": out_country.astype(np.int32),
        "category_id": out_cat.astype(np.int32),
        "year": (out_year_idx + min_year).astype(np.int16),
        "month": (out_month_idx + 1).astype(np.int8),
        "channel_key": channel_uniq[out_ch_idx.astype(np.intp)].astype(np.int32),
    }


# ----------------------------------------------------------------
# Shared bincount + decode pipeline
# ----------------------------------------------------------------

def _aggregate_via_bincount(
    dims: Dict[str, np.ndarray],
    weight_pairs: Dict[str, np.ndarray],
) -> Optional[Dict[str, np.ndarray]]:
    """Bincount aggregation over flat composite key, then decode.

    Args:
        dims: output of ``_build_dimension_arrays()``.
        weight_pairs: {output_name: weights_array} — each is bincount'd
            over the same flat_key.

    Returns:
        dict with decoded dimension columns + aggregated weight columns,
        or None if all bins are zero.
    """
    flat_key = dims["flat_key"]
    total_cells = dims["total_cells"]

    sums = {
        name: np.bincount(flat_key, weights=w, minlength=total_cells)
        for name, w in weight_pairs.items()
    }

    # Build non-zero mask directly via functools.reduce (avoids pre-allocating zeros)
    sum_arrays = list(sums.values())
    mask = sum_arrays[0] != 0
    for arr in sum_arrays[1:]:
        mask |= arr != 0
    indices = np.flatnonzero(mask)

    if indices.size == 0:
        return None

    decoded = _decode_flat_key(
        indices,
        stride_cat=dims["stride_cat"],
        stride_y=dims["stride_y"],
        stride_m=dims["stride_m"],
        stride_ch=dims["stride_ch"],
        min_year=dims["min_year"],
        channel_uniq=dims["channel_uniq"],
    )
    for name, arr in sums.items():
        decoded[name] = arr[indices]
    return decoded


# ----------------------------------------------------------------
# Sales micro-aggregation
# ----------------------------------------------------------------

def micro_aggregate_sales(
    table: pa.Table,
    *,
    store_to_country: np.ndarray,   # dense: StoreKey -> country_id (int32)
    product_to_cat: np.ndarray,     # dense: ProductKey -> category_id (int32)
) -> Dict[str, np.ndarray]:
    """
    Collapse a sales chunk to budget-grain aggregates.

    Input:  ~2M row Arrow table with StoreKey, ProductKey, OrderDate,
            Quantity, NetPrice, SalesChannelKey columns.

    Output: dict of aligned arrays (each ~500-2000 rows):
        country_id, category_id, year, month, channel_key,
        sales_amount (Quantity * NetPrice), sales_qty

    Uses np.bincount with a flat composite key — O(n) single pass,
    no sorting, no structured arrays.
    """
    dims = _extract_columns_from_table(
        table,
        store_to_country=store_to_country,
        product_to_cat=product_to_cat,
    )

    qty = table.column("Quantity").to_numpy(zero_copy_only=False).astype(np.float64, copy=False)
    net_price = table.column("NetPrice").to_numpy(zero_copy_only=False).astype(np.float64, copy=False)

    result = _aggregate_via_bincount(dims, {
        "sales_amount": qty * net_price,
        "sales_qty": qty,
    })
    # Sales micro-agg always returns a dict (even if empty — caller checks length)
    return result or {
        "country_id": np.empty(0, dtype=np.int32),
        "category_id": np.empty(0, dtype=np.int32),
        "year": np.empty(0, dtype=np.int16),
        "month": np.empty(0, dtype=np.int8),
        "channel_key": np.empty(0, dtype=np.int32),
        "sales_amount": np.empty(0, dtype=np.float64),
        "sales_qty": np.empty(0, dtype=np.float64),
    }
