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
MASK63 = np.uint64(0x7FFFFFFFFFFFFFFF)        # drop the sign bit -> non-negative int64


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


def hash_u01(salt, key1, key2=None) -> np.ndarray:
    """Per-element uniform in ``[0, 1)`` keyed by ``salt`` and one or two integer
    keys.

    This is the single counter-based draw primitive: a stage draws
    ``hash_u01(stage_salt, entity_key…)`` instead of consuming a sequential RNG,
    so its value is a pure function of the (globally-unique) entity key and is
    independent of chunk_size, worker count, or any other stage's draw count.
    Two lines that differ in key get independent uniforms; the same key always
    reproduces the same uniform (e.g. recomputed in a later pass).

    The mix is ``splitmix64(key1 * GOLDEN ^ (key2 + salt))`` — identical to the
    hand-rolled hashers this generalizes (``line_friction`` keyed on
    ``(OrderNumber, OrderLineNumber)``; the posted-price snap keyed on
    ``(ProductID, month)``). ``key2`` is optional for per-order/single-key draws.
    Distinct ``salt`` values give independent draws off the same key(s).
    """
    k1 = np.asarray(key1).astype(np.int64, copy=False).astype(np.uint64, copy=False)
    x = k1 * GOLDEN
    s = np.uint64(salt)
    if key2 is None:
        x = x ^ s
    else:
        k2 = np.asarray(key2).astype(np.int64, copy=False).astype(np.uint64, copy=False)
        x = x ^ (k2 + s)
    return u01_from_u64(splitmix64(x))


def hash_i64(salt, key1, *more_keys) -> np.ndarray:
    """Per-element non-negative ``int64`` hash of one or more integer keys.

    A counter-based surrogate key: the value is a pure function of the key tuple,
    so the same ``(key1, key2, …)`` always maps to the same id regardless of
    chunk_size, worker count, or emission order — no sequential counter or
    cross-chunk offset needed. Each key is folded through the SplitMix64 finalizer
    (the first as ``key1 * GOLDEN ^ salt``, matching :func:`hash_u01`), then masked
    to 63 bits so the result is a valid non-negative ``int64``. Collisions are
    astronomically unlikely at realistic row counts (63-bit space).
    """
    k1 = np.asarray(key1).astype(np.int64, copy=False).astype(np.uint64, copy=False)
    x = splitmix64(k1 * GOLDEN ^ np.uint64(salt))
    for k in more_keys:
        ki = np.asarray(k).astype(np.int64, copy=False).astype(np.uint64, copy=False)
        x = splitmix64(x ^ (ki * GOLDEN))
    return (x & MASK63).astype(np.int64)
