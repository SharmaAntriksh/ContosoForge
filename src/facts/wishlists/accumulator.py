"""Wishlist accumulator — collects (CustomerKey, ProductKey) purchase pairs
streamed from sales worker chunks."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.facts.shared.base_accumulator import BaseAccumulator


class WishlistAccumulator(BaseAccumulator):
    """Collects customer-product purchase pairs across all sales chunks."""

    def __init__(self) -> None:
        super().__init__(validator_key="customer_key")

    def finalize(self) -> pd.DataFrame:
        """Return pairs with their deterministic first purchase date."""
        if not self._parts:
            return pd.DataFrame(
                columns=["CustomerKey", "ProductKey", "FirstPurchaseDate"]
            )

        all_ck = np.concatenate([p["customer_key"] for p in self._parts])
        all_pk = np.concatenate([p["product_key"] for p in self._parts])
        all_purchase_ns = np.concatenate([
            p["first_purchase_ns"] for p in self._parts
        ])

        # Dedup required: same (customer, product) pair can appear in
        # multiple sales chunks when a customer buys the same product
        # in different months.
        df = pd.DataFrame({
            "CustomerKey": all_ck,
            "ProductKey": all_pk,
            "FirstPurchaseNs": all_purchase_ns,
        })
        df = df.groupby(
            ["CustomerKey", "ProductKey"], as_index=False, sort=True,
        )["FirstPurchaseNs"].min()
        df["FirstPurchaseDate"] = df.pop("FirstPurchaseNs").astype(
            "datetime64[ns]"
        )
        return df
