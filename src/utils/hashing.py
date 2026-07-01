"""Vectorized SplitMix64 hashing shared across the sales fact.

Several correlated-fact features derive a deterministic per-row uniform in
``[0, 1)`` by hashing a globally-unique integer key (e.g. ``(OrderNumber,
OrderLineNumber)`` or ``(ProductID, month)``) with a SplitMix64 finalizer,
never the chunk RNG -- so the value is identical regardless of ``chunk_size``
or worker count and can be recomputed byte-for-byte in a later pass. This
module is the single definition of that primitive; every sales hashing site
imports from here instead of re-declaring the constants and the mix.
"""
from __future__ import annotations

import numpy as np

# ``GOLDEN`` is the SplitMix64 increment/gamma; callers fold a key into the
# stream as ``key * GOLDEN ^ salt``. ``MIX_A`` / ``MIX_B`` are the finalizer
# multipliers. A couple of callers also reuse ``MIX_A`` as an additive salt in
# their key derivation, so it is exported to keep that value defined once.
GOLDEN = np.uint64(0x9E3779B97F4A7C15)
MIX_A = np.uint64(0xBF58476D1CE4E5B9)
MIX_B = np.uint64(0x94D049BB133111EB)
TWO_POW_53 = np.float64(9007199254740992.0)  # 2**53


def splitmix64(x: np.ndarray) -> np.ndarray:
    """SplitMix64 finalizer (vectorized). Returns a new array; ``x`` is untouched."""
    x = x ^ (x >> np.uint64(30))
    x = x * MIX_A
    x = x ^ (x >> np.uint64(27))
    x = x * MIX_B
    x = x ^ (x >> np.uint64(31))
    return x


def u01_from_u64(x: np.ndarray) -> np.ndarray:
    """Top 53 bits of a mixed uint64 -> uniform double in ``[0, 1)``."""
    return (x >> np.uint64(11)).astype(np.float64) / TWO_POW_53
