"""Micro-aggregate (CustomerKey, OrderNumber, OrderLineNumber)
from sales detail chunks for the complaints pipeline."""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pyarrow as pa

from src.facts.shared.micro_agg_helpers import (
    validate_required_columns,
    extract_column,
    deduplicate_tuples,
)


def micro_aggregate_complaints(detail_table: pa.Table) -> Optional[Dict[str, np.ndarray]]:
    """Extract unique order-line identities plus their causal date anchors.

    Returns a compact dict of three arrays, or None if required columns
    are missing.
    """
    if not validate_required_columns(
        detail_table,
        {
            "CustomerKey", "OrderNumber", "OrderLineNumber", "OrderDate",
            "DeliveryDate",
        },
    ):
        return None

    ck = extract_column(detail_table, "CustomerKey", np.int64)
    so = extract_column(detail_table, "OrderNumber", np.int64)
    ln = extract_column(detail_table, "OrderLineNumber", np.int64)
    order_ns = (
        detail_table.column("OrderDate").to_numpy(zero_copy_only=False)
        .astype("datetime64[ns]").view(np.int64)
    )
    delivery_ns = (
        detail_table.column("DeliveryDate").to_numpy(zero_copy_only=False)
        .astype("datetime64[ns]").view(np.int64)
    )

    return deduplicate_tuples(
        [ck, so, ln, order_ns, delivery_ns],
        [
            "customer_key", "sales_order_number", "line_number",
            "order_date_ns", "delivery_date_ns",
        ],
    )
