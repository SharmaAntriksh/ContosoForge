"""Counter-based ChannelKey / TimeKey: order-level draws hashed on the order
entity key (OrderNumber), not the chunk RNG.

ChannelKey and TimeKey are constant within an order, so each is a pure function
of the order's OrderNumber -- independent of chunk_size, worker count, and the
order in which lines/orders are processed. These tests pin that property plus
the channel-aware hour weighting; the skip_order_cols fallback (no per-order key)
keeps using the chunk RNG and is covered by the byte-identical snapshot fixture.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.facts.sales.sales_logic import chunk_builder as cb
from src.facts.sales.sales_logic.columns import _timekey_by_channel_counter


# ---------------------------------------------------------------------------
# _timekey_by_channel_counter
# ---------------------------------------------------------------------------
def _restricted_lut():
    """channel_hour_lut where channel 1 opens 9-12h and channel 2 opens 18-22h."""
    lut = np.zeros((3, 24), dtype=np.float64)
    lut[1, 9:12] = 1.0 / 3
    lut[2, 18:22] = 1.0 / 4
    return lut


def test_timekey_is_pure_function_of_order_key():
    n = 400
    keys = (7000 + np.arange(n)).astype(np.int64)
    channels = np.where(np.arange(n) % 2 == 0, 1, 2).astype(np.int32)
    lut = _restricted_lut()

    t1 = _timekey_by_channel_counter(keys, channels, lut)
    t1b = _timekey_by_channel_counter(keys, channels, lut)
    assert np.array_equal(t1, t1b)              # deterministic

    perm = np.random.default_rng(0).permutation(n)
    t2 = _timekey_by_channel_counter(keys[perm], channels[perm], lut)
    assert np.array_equal(t1[perm], t2)         # pure function of (key, channel)

    assert t1.min() >= 0 and t1.max() < 1440


def test_timekey_respects_channel_operating_hours():
    n = 2000
    keys = (np.arange(n) + 1).astype(np.int64)
    channels = np.where(np.arange(n) % 2 == 0, 1, 2).astype(np.int32)
    lut = _restricted_lut()

    t = _timekey_by_channel_counter(keys, channels, lut)
    hours = t // 60
    assert set(np.unique(hours[channels == 1]).tolist()) <= {9, 10, 11}
    assert set(np.unique(hours[channels == 2]).tolist()) <= {18, 19, 20, 21}


def test_timekey_digital_fallback_when_no_lut():
    n = 1000
    keys = (np.arange(n) + 1).astype(np.int64)
    # No LUT and no channels -> digital hour profile, all 24 hours reachable.
    t = _timekey_by_channel_counter(keys, None, None)
    assert t.min() >= 0 and t.max() < 1440
    assert len(np.unique(t // 60)) > 12   # digital profile spreads across the day


def test_timekey_empty():
    assert _timekey_by_channel_counter(np.empty(0, dtype=np.int64), None, None).shape == (0,)


# ---------------------------------------------------------------------------
# _apply_store_channel_correlation
# ---------------------------------------------------------------------------
def _orders_to_lines(orders, lines_per=2):
    """Expand [(order_number, store), ...] into per-line arrays + grouping."""
    onum, store, line_num = [], [], []
    for oid, sk in orders:
        for j in range(lines_per):
            onum.append(oid)
            store.append(sk)
            line_num.append(j + 1)
    onum = np.asarray(onum, dtype=np.int64)
    store = np.asarray(store, dtype=np.int64)
    line_num = np.asarray(line_num, dtype=np.int64)
    order_starts = line_num == 1
    order_idx = np.cumsum(order_starts.astype(np.int64)) - 1
    n_unique = int(order_idx.max() + 1)
    return onum, store, order_starts, order_idx, n_unique, len(onum)


_STORE_CH_KEYS = [None, np.array([10, 11], dtype=np.int32), np.array([20, 21, 22], dtype=np.int32)]
_CH_PROB = [None, np.array([0.5, 0.5]), np.array([0.3, 0.3, 0.4])]


def _channel_per_order(orders):
    onum, store, order_starts, order_idx, n_unique, n_lines = _orders_to_lines(orders)
    rng = np.random.default_rng(0)
    before = dict(rng.bit_generator.state)
    line_ch = cb._apply_store_channel_correlation(
        rng, store, False, order_idx, order_starts, n_unique, n_lines,
        _STORE_CH_KEYS, _CH_PROB, order_numbers=onum,
    )
    # RNG must be untouched (counter-based) when order keys are present.
    assert rng.bit_generator.state == before
    # Collapse to per-order (channel is order-constant -> first line per order).
    return {int(orders[i][0]): int(line_ch[np.flatnonzero(order_starts)[i]]) for i in range(n_unique)}


def test_channel_is_pure_function_of_order_and_store():
    orders = [(500, 1), (501, 2), (502, 1), (503, 2), (504, 1)]
    m1 = _channel_per_order(orders)

    # Same orders in a different arrangement -> each OrderNumber keeps its channel.
    shuffled = [orders[i] for i in (3, 0, 4, 1, 2)]
    m2 = _channel_per_order(shuffled)
    assert m1 == m2

    # Every channel respects its store's channel set.
    for oid, sk in orders:
        assert m1[oid] in set(_STORE_CH_KEYS[sk].tolist())


def test_channel_skip_path_uses_rng():
    # No order grouping (skip_order_cols): per-line RNG fallback advances the stream.
    store = np.array([1, 2, 1, 2, 1], dtype=np.int64)
    rng = np.random.default_rng(0)
    before = dict(rng.bit_generator.state)
    out = cb._apply_store_channel_correlation(
        rng, store, True, None, None, 0, len(store),
        _STORE_CH_KEYS, _CH_PROB, order_numbers=None,
    )
    assert rng.bit_generator.state != before
    assert out.shape == (len(store),)
