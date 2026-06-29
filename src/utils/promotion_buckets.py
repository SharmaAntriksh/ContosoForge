"""Shared promotion-bucket helpers.

Single source of truth for the eight configurable promotion bucket keys and
the algorithm that distributes a requested total across them. Used by both the
CLI override path (``engine.runners.pipeline_runner``) and the web UI
(``web.shared_state``) so the two cannot drift apart.

Holiday promotions are generated separately (one per holiday per year) and are
not part of this distribution.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

# The eight configurable promotion buckets, in canonical order.
PROMOTION_BUCKET_KEYS: Tuple[str, ...] = (
    "num_seasonal",
    "num_clearance",
    "num_limited",
    "num_flash",
    "num_volume",
    "num_loyalty",
    "num_bundle",
    "num_new_customer",
)


def distribute_total(weights: Sequence[int], total: int) -> List[int]:
    """Distribute ``total`` across ``len(weights)`` buckets proportionally to
    ``weights``.

    A bucket with zero weight receives zero, unless *every* weight is zero, in
    which case ``total`` is split as evenly as possible. The returned counts
    are non-negative and always sum to exactly ``max(0, int(total))``.
    """
    total = max(0, int(total))
    n = len(weights)
    if n == 0:
        return []

    base = [int(w) for w in weights]
    current = sum(base)
    if current <= 0:
        base = [1] * n
        current = n

    scaled = [b * total / current for b in base]
    floors = [int(x) for x in scaled]
    remainder = total - sum(floors)

    # Hand the rounding remainder to the buckets with the largest fractional
    # parts; each bucket can gain at most +1, so cap the loop to n.
    order = sorted(range(n), key=lambda i: scaled[i] - floors[i], reverse=True)
    for i in range(min(remainder, n)):
        floors[order[i]] += 1

    return floors
