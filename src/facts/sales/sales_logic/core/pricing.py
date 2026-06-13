"""Base price packaging for Sales rows.

This module packages product-level UnitPrice/UnitCost into the dict
structure consumed by ``pricing_pipeline.build_prices``, which applies
inflation, appearance snapping, and markdown discounts.

All discount/markdown logic lives exclusively in ``pricing_pipeline.py``.
"""
from __future__ import annotations

import numpy as np


def compute_prices(
    rng,
    n,
    unit_price,
    unit_cost,
):
    """
    Package base product prices for downstream processing.

    Returns the standard price dict with zero discount; all markdown
    logic is handled by ``pricing_pipeline.build_prices``.

    Parameters
    ----------
    rng : numpy.random.Generator
    n : int - number of rows
    unit_price, unit_cost : array-like - per-row base prices from products
    """

    n = int(n)
    if n <= 0:
        z = np.zeros(0, dtype=np.float64)
        return {"final_unit_price": z, "final_unit_cost": z,
                "discount_amt": z, "final_net_price": z}

    up = np.asarray(unit_price, dtype=np.float64)
    up = np.where(np.isfinite(up), up, 0.0)

    uc = np.asarray(unit_cost, dtype=np.float64)
    uc = np.where(np.isfinite(uc), uc, 0.0)

    up = np.maximum(up, 0.0)
    uc = np.minimum(np.maximum(uc, 0.0), up)

    return {
        "final_unit_price": up,
        "final_unit_cost": uc,
        "discount_amt": np.zeros(n, dtype=np.float64),
        # build_prices always overwrites final_net_price (and never reads the
        # passed value), so share the up reference instead of a wasted copy.
        "final_net_price": up,
    }
