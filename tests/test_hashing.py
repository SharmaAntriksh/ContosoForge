"""Unit tests for the shared counter-based hashing primitive.

``hash_u01`` is the single definition of the sales fact's counter-based draw:
``u01(salt, entity_key…)``. It generalizes the hand-rolled hashers it replaces
(``line_friction`` keyed on ``(OrderNumber, OrderLineNumber)`` and the posted
price snap keyed on ``(ProductID, month)``), so these tests pin both its
statistical properties and that it reproduces the legacy inline formula
bit-for-bit.
"""
from __future__ import annotations

import numpy as np

from src.utils.hashing import (
    GOLDEN,
    hash_i64,
    hash_u01,
    splitmix64,
    u01_from_u64,
)


def _legacy_two_key(salt, k1, k2):
    """The inline formula hash_u01 replaces, written out explicitly."""
    a = np.asarray(k1).astype(np.int64, copy=False).astype(np.uint64, copy=False)
    b = np.asarray(k2).astype(np.int64, copy=False).astype(np.uint64, copy=False)
    x = splitmix64(a * GOLDEN ^ (b + np.uint64(salt)))
    return u01_from_u64(x)


class TestHashU01:
    def test_deterministic(self):
        k1 = np.arange(1, 1000, dtype=np.int64)
        k2 = np.full(k1.size, 7, dtype=np.int64)
        a = hash_u01(np.uint64(0x1234), k1, k2)
        b = hash_u01(np.uint64(0x1234), k1, k2)
        np.testing.assert_array_equal(a, b)

    def test_in_unit_interval(self):
        k1 = np.arange(0, 50_000, dtype=np.int64)
        u = hash_u01(np.uint64(0xABCD), k1, k1 % 13)
        assert u.min() >= 0.0
        assert u.max() < 1.0

    def test_roughly_uniform(self):
        k1 = np.arange(0, 200_000, dtype=np.int64)
        u = hash_u01(np.uint64(0x99), k1, np.ones_like(k1))
        assert 0.49 < float(u.mean()) < 0.51

    def test_salt_gives_independent_draws(self):
        k1 = np.arange(1, 5000, dtype=np.int64)
        k2 = np.full(k1.size, 3, dtype=np.int64)
        u1 = hash_u01(np.uint64(0x1), k1, k2)
        u2 = hash_u01(np.uint64(0x2), k1, k2)
        assert not np.allclose(u1, u2)
        # independent draws are essentially uncorrelated
        assert abs(float(np.corrcoef(u1, u2)[0, 1])) < 0.05

    def test_each_key_component_matters(self):
        n = 4000
        base = hash_u01(np.uint64(0x55), np.arange(n), np.full(n, 2))
        diff_k1 = hash_u01(np.uint64(0x55), np.arange(1, n + 1), np.full(n, 2))
        diff_k2 = hash_u01(np.uint64(0x55), np.arange(n), np.full(n, 3))
        assert not np.allclose(base, diff_k1)
        assert not np.allclose(base, diff_k2)

    def test_reproduces_legacy_two_key_formula(self):
        # The exact bytes the old line_friction / posted-price snap produced.
        salt = np.uint64(0xD1B54A32D192ED03)
        k1 = np.arange(1, 10_000, dtype=np.int64)
        k2 = (k1 * 3 + 1).astype(np.int64)
        np.testing.assert_array_equal(
            hash_u01(salt, k1, k2), _legacy_two_key(salt, k1, k2)
        )

    def test_single_key_matches_zero_second_key(self):
        # The one-key form folds only the salt; equivalent to k2 == 0.
        salt = np.uint64(0x50A1C0DE00000001)
        k1 = np.arange(1, 8000, dtype=np.int64)
        one = hash_u01(salt, k1)
        two = _legacy_two_key(salt, k1, np.zeros_like(k1))
        np.testing.assert_array_equal(one, two)


class TestHashI64:
    """The counter-based int64 surrogate-key hash (ReturnEventKey)."""

    _SALT = np.uint64(0x8E7040DE00000001)

    def test_nonnegative_int64(self):
        keys = hash_i64(self._SALT, np.arange(1, 100_000), np.ones(99_999, dtype=np.int64))
        assert keys.dtype == np.int64
        assert (keys >= 0).all()

    def test_deterministic(self):
        a = hash_i64(self._SALT, np.arange(50_000), np.arange(50_000) % 7, np.arange(50_000) % 3)
        b = hash_i64(self._SALT, np.arange(50_000), np.arange(50_000) % 7, np.arange(50_000) % 3)
        np.testing.assert_array_equal(a, b)

    def test_unique_over_distinct_triples(self):
        # (order, line, seq) triples that are all distinct -> no collisions.
        order = np.repeat(np.arange(1, 20_001), 3)
        line = np.ones(order.size, dtype=np.int64)
        seq = np.tile(np.array([1, 2, 3]), 20_000)
        keys = hash_i64(self._SALT, order, line, seq)
        assert len(np.unique(keys)) == keys.size

    def test_each_key_component_matters(self):
        n = 5000
        base = hash_i64(self._SALT, np.arange(n), np.full(n, 1), np.full(n, 1))
        d_order = hash_i64(self._SALT, np.arange(1, n + 1), np.full(n, 1), np.full(n, 1))
        d_line = hash_i64(self._SALT, np.arange(n), np.full(n, 2), np.full(n, 1))
        d_seq = hash_i64(self._SALT, np.arange(n), np.full(n, 1), np.full(n, 2))
        assert not np.array_equal(base, d_order)
        assert not np.array_equal(base, d_line)
        assert not np.array_equal(base, d_seq)

    def test_pure_function_of_keys_not_order(self):
        order = np.array([5, 9, 2, 7, 1], dtype=np.int64)
        line = np.array([1, 1, 2, 1, 3], dtype=np.int64)
        seq = np.array([1, 2, 1, 1, 1], dtype=np.int64)
        keys = hash_i64(self._SALT, order, line, seq)
        perm = np.array([3, 0, 4, 1, 2])
        keys_perm = hash_i64(self._SALT, order[perm], line[perm], seq[perm])
        np.testing.assert_array_equal(keys[perm], keys_perm)
