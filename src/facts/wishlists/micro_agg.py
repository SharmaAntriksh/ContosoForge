"""Micro-aggregate (CustomerKey, ProductKey) pairs from sales detail chunks."""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pyarrow as pa

from src.facts.shared.micro_agg_helpers import (
    validate_required_columns,
    extract_column,
)


def micro_aggregate_wishlists(detail_table: pa.Table) -> Optional[Dict[str, np.ndarray]]:
    """Extract each pair's first purchase date from a single sales chunk.

    Returns a compact dict of two int64 arrays, or None if columns are missing.
    """
    if not validate_required_columns(
        detail_table, {"CustomerKey", "ProductKey", "OrderDate"},
    ):
        return None

    ck = extract_column(detail_table, "CustomerKey", np.int64)
    pk = extract_column(detail_table, "ProductKey", np.int64)
    purchase_ns = (
        detail_table.column("OrderDate").to_numpy(zero_copy_only=False)
        .astype("datetime64[ns]").view(np.int64)
    )

    import pandas as pd

    pairs = pd.DataFrame({
        "customer_key": ck,
        "product_key": pk,
        "first_purchase_ns": purchase_ns,
    })
    pairs = pairs.groupby(
        ["customer_key", "product_key"], as_index=False, sort=True,
    )["first_purchase_ns"].min()
    return {name: pairs[name].to_numpy() for name in pairs.columns}
