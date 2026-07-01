"""Sales core generation logic.

Pure(ish) numpy routines shared by the sales chunk builder.

This package was split from a single core.py module. All public symbols
are re-exported here so existing imports continue to work unchanged:

    from .core import compute_dates, build_orders, ...
"""

from __future__ import annotations

# Re-export: date logic (delivery re-exports fmt from ..globals — single source)
from .delivery import (
    fmt,
    _yyyymmdd_from_days,
    _stable_row_hash,
    compute_dates,
)

# Re-export: order logic
from .orders import (
    build_month_demand,
    build_orders,
)

# Re-export: customer sampling
from .customer_sampling import (
    _normalize_end_month,
    _eligible_customer_mask_for_month,
    _participation_distinct_target,
    _sample_customers,
    compute_discovery_months,
    compute_month_distinct_targets,
    build_month_customer_pool,
    assign_orders_to_customers,
)

# Re-export: promotions
from .promotions import (
    apply_promotions,
)

# Re-export: pricing
from .pricing import (
    compute_prices,
)

# Re-export: month planning / allocation
from .allocation import (
    macro_month_weights,
    build_rows_per_month,
)

# Shared CDF utility (CLAUDE.md gotcha #16: clamp cdf[-1] = 1.0)
import numpy as np

def _normalize_cdf(weights: np.ndarray) -> np.ndarray:
    """Build a normalized CDF from *weights*, clamping the last element to 1.0.

    Returns a float64 array suitable for ``np.searchsorted(..., side='right')``.
    If all weights are zero or the array is empty, returns the raw cumsum
    (caller should check ``cdf[-1] > 0`` before using).
    """
    cdf = np.cumsum(weights, dtype=np.float64)
    if cdf.size > 0 and cdf[-1] > 0:
        cdf /= cdf[-1]
        cdf[-1] = 1.0
    return cdf


# Backward compatibility: PA_AVAILABLE was defined in the original core.py
try:
    import pyarrow as pa  # type: ignore
except ImportError:
    pa = None

PA_AVAILABLE = pa is not None

__all__ = [
    # date logic
    "compute_dates",

    # order logic
    "build_month_demand",
    "build_orders",

    # promotions
    "apply_promotions",

    # pricing
    "compute_prices",

    # month planning
    "macro_month_weights",
    "build_rows_per_month",

    # customer discovery schedule
    "compute_discovery_months",

    # global per-month plan (Phase 2)
    "compute_month_distinct_targets",
    "build_month_customer_pool",
    "assign_orders_to_customers",
]

# Internal symbols re-exported for chunk_builder and tests (not part of the
# public API, but importable from this package for backward compatibility):
#   _normalize_end_month, _eligible_customer_mask_for_month,
#   _participation_distinct_target, _sample_customers, _stable_row_hash
# (_yyyymmdd_from_days is retained for test coverage only — no prod caller.)