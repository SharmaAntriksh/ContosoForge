"""Per-chunk micro-aggregation for inventory.

Called inside each worker's _worker_task() after the Arrow table is built.
Collapses ~1-2M sales rows to ~(products × stores × months-in-chunk) rows
grouped by (ProductKey, StoreKey, Year, Month).

Returns a small dict of numpy arrays (trivial to pickle across IPC).

Uses flat composite int64 key + argsort + np.add.reduceat for O(n log n) groupby.
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pyarrow as pa

from src.facts.shared.micro_agg_helpers import (
    validate_required_columns,
    extract_column,
    decompose_dates,
)


def micro_aggregate_inventory(
    table: pa.Table,
) -> Optional[Dict[str, np.ndarray]]:
    """Collapse a sales chunk to inventory-grain demand aggregates.

    Input:  Arrow table with ProductKey, StoreKey, OrderDate, Quantity columns.

    Output: dict of aligned arrays.  When a store→warehouse mapping is
    available on State, aggregates at warehouse grain (much smaller),
    eliminating the expensive post-hoc rollup in the inventory runner.
    Otherwise falls back to store-grain aggregation.

    Keys returned: product_key, location_key, year, month, quantity_sold,
    plus a 'grain' flag ('warehouse' or 'store').
    """
    if not validate_required_columns(table, {"ProductKey", "StoreKey", "OrderDate", "Quantity"}):
        return None

    product_keys = extract_column(table, "ProductKey", np.int64)
    store_keys = extract_column(table, "StoreKey", np.int64)
    order_dates = table.column("OrderDate").to_numpy(zero_copy_only=False)
    qty = extract_column(table, "Quantity", np.float64)

    year, month = decompose_dates(order_dates)

    n = len(product_keys)
    if n == 0:
        return None

    # Map StoreKey → WarehouseKey if available (avoids expensive post-hoc rollup)
    from src.facts.sales.sales_logic.globals import State
    sk_to_wk = getattr(State, "inventory_store_to_warehouse", None)

    if sk_to_wk is not None:
        sk_i32 = store_keys.astype(np.int32)
        valid = (sk_i32 >= 0) & (sk_i32 < len(sk_to_wk))
        location_keys = np.where(valid, sk_to_wk[sk_i32], np.int32(-1)).astype(np.int64)
        # Drop rows with unmapped stores (should not happen if FK integrity holds)
        keep = location_keys >= 0
        if not keep.all():
            from src.utils.logging_utils import warn as _warn
            _warn(f"inventory micro_agg: {int((~keep).sum())} rows dropped (unmapped StoreKey)")
            product_keys = product_keys[keep]
            location_keys = location_keys[keep]
            year = year[keep]
            month = month[keep]
            qty = qty[keep]
            n = int(keep.sum())
            if n == 0:
                return None
        grain = "warehouse"
    else:
        location_keys = store_keys
        grain = "store"

    # Groupby via flat composite int64 key + argsort + reduceat.
    max_loc = int(location_keys.max()) + 1
    max_month = 13  # months 1..12
    year_min = int(year.min())
    n_years = int(year.max()) - year_min + 1
    stride_s = n_years * max_month
    stride_y = max_month

    flat_key = (
        product_keys * (max_loc * stride_s)
        + location_keys * stride_s
        + (year - year_min).astype(np.int64) * stride_y
        + month.astype(np.int64)
    )

    order = np.argsort(flat_key)
    s_fk = flat_key[order]
    s_q = qty[order]

    diff = np.empty(n, dtype=bool)
    diff[0] = True
    diff[1:] = s_fk[1:] != s_fk[:-1]
    group_starts = np.flatnonzero(diff)
    group_qty = np.add.reduceat(s_q, group_starts)
    fk_groups = s_fk[group_starts]

    # Decode flat keys back to (product, location, year, month)
    remainder = fk_groups
    g_product = remainder // (max_loc * stride_s)
    remainder = remainder % (max_loc * stride_s)
    g_location = remainder // stride_s
    remainder = remainder % stride_s
    g_year = remainder // stride_y + year_min
    g_month = remainder % stride_y

    return {
        "product_key": g_product,
        "location_key": g_location,
        "year": g_year.astype(np.int16),
        "month": g_month.astype(np.int8),
        "quantity_sold": group_qty.astype(np.int32),
        "grain": grain,
    }
