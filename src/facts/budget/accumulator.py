"""Budget accumulator (main-process side).

Collects micro-aggregate dicts as they arrive from workers via IPC,
then produces the final consolidated actuals DataFrame for the budget engine.

Memory: holds ~50K-200K summary rows in memory (a few MB even at 100M sales).
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd


class BudgetAccumulator:
    """
    Thread-safe (GIL) accumulator for worker micro-aggregates.

    Usage:
        acc = BudgetAccumulator(country_labels, category_labels)

        # In _record_chunk_result:
        acc.add_sales(result["_budget_agg"])

        # After all chunks:
        actuals = acc.finalize_sales()
    """

    def __init__(
        self,
        country_labels: np.ndarray,
        category_labels: np.ndarray,
    ):
        self._country_labels = country_labels
        self._category_labels = category_labels
        self._sales_parts: List[Dict[str, np.ndarray]] = []

    def add_sales(self, micro: Optional[Dict[str, np.ndarray]]) -> None:
        """Append a micro-aggregate from one worker chunk."""
        if micro is not None and len(micro.get("sales_amount", [])) > 0:
            self._sales_parts.append(micro)

    # ------------------------------------------------------------------
    # Public finalize methods
    # ------------------------------------------------------------------

    _SALES_OUTPUT_COLS = [
        "Country", "Category", "Year", "Month",
        "SalesChannelKey", "SalesAmount", "SalesQuantity",
    ]

    def finalize_sales(self) -> pd.DataFrame:
        """Merge all sales micro-aggregates into a monthly-grain DataFrame."""
        parts = self._sales_parts
        if not parts:
            return pd.DataFrame(columns=self._SALES_OUTPUT_COLS)

        df = pd.DataFrame({
            "country_id": np.concatenate([p["country_id"] for p in parts]),
            "category_id": np.concatenate([p["category_id"] for p in parts]),
            "year": np.concatenate([p["year"] for p in parts]),
            "month": np.concatenate([p["month"] for p in parts]),
            "channel_key": np.concatenate([p["channel_key"] for p in parts]),
            "sales_amount": np.concatenate([p["sales_amount"] for p in parts]),
            "sales_qty": np.concatenate([p["sales_qty"] for p in parts]),
        })

        # Re-aggregate (workers may produce overlapping month groups in rare edge
        # cases if chunk boundaries split a month — this final groupby is the safety net)
        monthly = df.groupby(
            ["country_id", "category_id", "year", "month", "channel_key"],
            as_index=False,
        ).agg({"sales_amount": "sum", "sales_qty": "sum"})

        # Map ids back to labels (with bounds check)
        country_ids = monthly["country_id"].to_numpy()
        category_ids = monthly["category_id"].to_numpy()
        if country_ids.size > 0 and int(country_ids.max()) >= len(self._country_labels):
            raise IndexError(
                f"country_id {int(country_ids.max())} >= country_labels length {len(self._country_labels)}"
            )
        if category_ids.size > 0 and int(category_ids.max()) >= len(self._category_labels):
            raise IndexError(
                f"category_id {int(category_ids.max())} >= category_labels length {len(self._category_labels)}"
            )
        monthly["Country"] = self._country_labels[country_ids]
        monthly["Category"] = self._category_labels[category_ids]
        monthly.rename(columns={
            "year": "Year",
            "month": "Month",
            "channel_key": "SalesChannelKey",
            "sales_amount": "SalesAmount",
            "sales_qty": "SalesQuantity",
        }, inplace=True)

        return monthly[self._SALES_OUTPUT_COLS]

    @property
    def has_data(self) -> bool:
        return len(self._sales_parts) > 0
