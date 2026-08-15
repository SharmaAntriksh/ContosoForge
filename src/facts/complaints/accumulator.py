"""Complaints accumulator — collects (CustomerKey, OrderNumber,
OrderLineNumber) triples streamed from sales worker chunks."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.facts.shared.base_accumulator import BaseAccumulator


class ComplaintsAccumulator(BaseAccumulator):
    """Collects customer-order-line triples across all sales chunks."""

    def __init__(self) -> None:
        super().__init__(validator_key="customer_key")

    def finalize(self) -> pd.DataFrame:
        """Return deduplicated (CustomerKey, OrderNumber,
        OrderLineNumber) identities and their sale/delivery dates."""
        if not self._parts:
            return pd.DataFrame(
                columns=[
                    "CustomerKey", "OrderNumber", "OrderLineNumber",
                    "OrderDate", "DeliveryDate",
                ]
            )

        all_ck = np.concatenate([p["customer_key"] for p in self._parts])
        all_so = np.concatenate([p["sales_order_number"] for p in self._parts])
        all_ln = np.concatenate([p["line_number"] for p in self._parts])
        all_order_ns = np.concatenate([p["order_date_ns"] for p in self._parts])
        all_delivery_ns = np.concatenate([p["delivery_date_ns"] for p in self._parts])

        # Each chunk already deduplicates per-chunk via micro_agg, and
        # OrderNumbers are unique across chunks (chunk_idx × stride),
        # so cross-chunk duplicates cannot occur.  Skip the expensive
        # drop_duplicates on the full concatenated array.
        return pd.DataFrame({
            "CustomerKey": all_ck,
            "OrderNumber": all_so,
            "OrderLineNumber": all_ln,
            "OrderDate": all_order_ns.astype("datetime64[ns]"),
            "DeliveryDate": all_delivery_ns.astype("datetime64[ns]"),
        })
