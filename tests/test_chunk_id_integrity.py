"""CHUNK-1 regression tests: day-based OrderNumber uniqueness.

The day-based ID scheme assigns each order
``id = day_offset * day_stride + chunk_idx * per_chunk_alloc + cursor + 1``
where ``cursor`` is the order's 0-based rank within its OrderDate. The cursor
must be a true within-day rank even when the customer-start clamp reorders a
day's orders *after* build_orders sorted them — otherwise the cursor overruns
the per-chunk band and IDs collide across chunks.
"""
from __future__ import annotations

import numpy as np

from src.facts.sales.sales_logic.chunk_builder import _within_day_cursor


def _naive_cursor(d_off: np.ndarray) -> np.ndarray:
    """The old (buggy) cursor: arange - first_index, assumes sorted input."""
    u_days, fi = np.unique(d_off, return_index=True)
    gi = np.searchsorted(u_days, d_off)
    return np.arange(d_off.shape[0], dtype=np.int64) - fi[gi]


def _ids(d_off, cursor, chunk_idx, day_stride, per_chunk_alloc):
    return d_off * day_stride + chunk_idx * per_chunk_alloc + cursor + 1


class TestWithinDayCursor:
    def test_empty(self):
        assert _within_day_cursor(np.empty(0, dtype=np.int64)).tolist() == []

    def test_sorted_input_matches_naive(self):
        d_off = np.array([0, 0, 0, 1, 1, 2], dtype=np.int64)
        assert _within_day_cursor(d_off).tolist() == [0, 1, 2, 0, 1, 0]

    def test_is_true_within_day_rank_when_unsorted(self):
        # Day 5 appears interleaved with day 7 (clamp pushed an order forward).
        d_off = np.array([5, 7, 5, 7, 7, 5], dtype=np.int64)
        cursor = _within_day_cursor(d_off)
        # Each day's cursors must be exactly 0..count-1, assigned in appearance order.
        assert cursor.tolist() == [0, 0, 1, 1, 2, 2]
        # Per day, cursors are a contiguous 0-based range.
        for day in np.unique(d_off):
            day_cursors = sorted(cursor[d_off == day].tolist())
            assert day_cursors == list(range(len(day_cursors)))

    def test_max_cursor_is_max_day_count_minus_one(self):
        d_off = np.array([3, 3, 3, 3, 9, 9], dtype=np.int64)
        cursor = _within_day_cursor(d_off)
        assert int(cursor.max()) + 1 == 4  # day 3 has 4 orders


class TestCrossChunkUniqueness:
    """The property CHUNK-1 was breaking: IDs unique across chunks."""

    def test_unsorted_days_produce_unique_ids_across_chunks(self):
        rng = np.random.default_rng(0)
        day_stride = np.int64(1000)
        per_chunk_alloc = np.int64(100)
        n_days = 20

        all_ids = []
        for chunk_idx in range(5):
            # Simulate post-clamp order dates: sorted, then a fraction of orders
            # pushed forward to a later day (breaking the sort within the chunk).
            counts = rng.integers(1, 40, size=n_days)
            d_off = np.repeat(np.arange(n_days, dtype=np.int64), counts)
            # Push ~15% of orders forward by 1-3 days (the clamp behavior).
            mask = rng.random(d_off.shape[0]) < 0.15
            d_off[mask] = np.minimum(d_off[mask] + rng.integers(1, 4, size=mask.sum()),
                                     n_days - 1)
            rng.shuffle(d_off)  # build_orders' sort is undone by the clamp anyway

            cursor = _within_day_cursor(d_off)
            assert int(cursor.max(initial=-1)) + 1 <= per_chunk_alloc
            ids = _ids(d_off, cursor, np.int64(chunk_idx), day_stride, per_chunk_alloc)
            all_ids.append(ids)

        merged = np.concatenate(all_ids)
        assert len(np.unique(merged)) == merged.shape[0], "duplicate OrderNumber"

    def test_naive_cursor_collides_when_clamp_reorders(self):
        # A concrete reordering where the old cursor over-counts and the fixed
        # one does not. Day 0 has 3 orders; an order is interleaved out of order.
        d_off = np.array([0, 1, 0, 0], dtype=np.int64)
        naive = _naive_cursor(d_off)
        fixed = _within_day_cursor(d_off)
        # Naive cursor for the last day-0 order: index 3 - first_index(0)=0 -> 3,
        # exceeding the true count (3 orders -> max rank should be 2).
        assert int(naive.max()) == 3
        assert int(fixed.max()) == 2
        # The fixed cursor yields a clean per-day 0..n-1 set; naive does not.
        day0_fixed = sorted(fixed[d_off == 0].tolist())
        assert day0_fixed == [0, 1, 2]
