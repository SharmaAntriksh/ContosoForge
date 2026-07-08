"""Shared test fixtures for the Returns fact builder.

Kept in one place so the returns-builder tests in ``test_multi_event_returns.py``
and ``test_sales_logic.py`` share a single source of truth for the synthetic
detail-frame columns (see ``from tests.returns_fixtures import dim_keys``).
"""
from __future__ import annotations

import numpy as np


def dim_keys(n: int) -> dict:
    """Snapshot-at-sale dimension keys the returns builder carries through.

    Deterministic placeholder values keyed off the 0-based row index — the
    returns *logic* ignores them, but the builder now requires the columns to be
    present on the detail frame. Row ``i`` gets ``CustomerKey = i % 1000 + 1``,
    ``ProductKey = i % 500 + 1``, ``StoreKey = i % 50 + 1``,
    ``EmployeeKey = i % 100 + 1`` so carry-through can be asserted against the
    sold line.
    """
    a = np.arange(n, dtype=np.int32)
    return {
        "CustomerKey": (a % 1000 + 1).astype(np.int32),
        "ProductKey": (a % 500 + 1).astype(np.int32),
        "StoreKey": (a % 50 + 1).astype(np.int32),
        "EmployeeKey": (a % 100 + 1).astype(np.int32),
    }
