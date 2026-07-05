"""Shared month / order / line plan arithmetic.

The coordinator (``sales.py``) and the worker chunk builder
(``chunk_builder.py``) must agree on three things, or the parallel sales fact
stops being chunk-invariant (CLAUDE.md gotcha #28): how a date pool maps to
month offsets, how many orders a month's rows imply (average lines per order),
and how a chunk carves the global order-id space. Those rules live here, once,
so "must match exactly" becomes "is the same code".
"""

from __future__ import annotations

import numpy as np


def month_offsets_int(date_pool) -> np.ndarray:
    """Month index per element of ``date_pool`` (``datetime64[M]`` as int64).

    Month offset 0 corresponds to the minimum month present; ``max - min + 1``
    is the month count ``T``. Both the coordinator's global plan and the
    worker's month slicing derive their axis from this call, so the two cannot
    disagree on where month 0 sits.
    """
    return np.asarray(date_pool).astype("datetime64[M]").astype("int64")


def plan_avg_lines(eff_skip: bool, max_lines_per_order: int) -> float:
    """Average lines per order used to turn a month's row count into an order
    count for the global plan.

    1.8 whenever orders are materialized with multi-line capacity; 1.0 when
    every order is a single line (``skip_order_cols``, or
    ``max_lines_per_order == 1``, collapses orders == lines).
    """
    return 1.8 if (not eff_skip and int(max_lines_per_order) > 1) else 1.0


# A deliberately separate, coarser estimate used ONLY by the customer-discovery
# auto-adjust headroom heuristic (how many customer "slots" the row budget
# implies). It is NOT the plan divisor in ``plan_avg_lines``: reconciling
# 2.0 -> 1.8 would change the auto-adjust output, so the discrepancy is left
# resolved-as-is and any change to it is a deliberate behavior gate.
DISCOVERY_SLOTS_AVG_LINES: float = 2.0


def chunk_month_band(R_m: int, O_m: int, chunk_idx: int, total_chunks: int) -> tuple:
    """This chunk's slice of month ``m`` in the global order/line index space.

    Returns ``(order_start, n_orders, n_lines)`` (index-space sharding):

    - Orders are floor-sliced from the global ``O_m`` so the chunks tile
      ``[0, O_m)`` contiguously with no gaps (``sum n_orders == O_m``). This
      guarantees every order index in ``[0, distinct_pool_size)`` is materialized
      by exactly one chunk, so the per-month distinct-customer set is exactly the
      pool regardless of ``chunk_size``.
    - Lines start at one-per-order and distribute the ``E = R_m - O_m`` extra
      lines by order-prefix, so ``n_lines >= n_orders`` (build_orders needs >= 1
      line/order) and ``sum n_lines == R_m`` exactly (preserves total_rows).

    Both allocations are pure functions of ``(chunk_idx, total_chunks)`` — no gaps,
    no clamps, sums exact in both dimensions.
    """
    C = int(total_chunks)
    c = int(chunk_idx)
    O = int(O_m)
    R = int(R_m)
    if C <= 0 or O <= 0 or R <= 0:
        return 0, 0, 0
    lo = (c * O) // C
    hi = ((c + 1) * O) // C
    n_orders = hi - lo
    if n_orders <= 0:
        return lo, 0, 0
    E = R - O                    # extra lines beyond one-per-order (>= 0 since O <= R)
    extra = (E * hi) // O - (E * lo) // O
    return lo, n_orders, n_orders + extra
