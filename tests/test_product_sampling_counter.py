"""Counter-based product sampling: a line's sampled product is a pure function
of its ``(OrderNumber, OrderLineNumber)`` entity key.

The three chunk-builder product samplers draw off the line entity key via
``hash_u01`` instead of the chunk RNG, so a line's product is independent of the
order in which lines are grouped/processed (by store, by brand) and of any other
stage's draw count. These tests pin that property with the discriminating check
the old RNG path could not pass: sampling a *permuted* set of the same keys
reproduces each key's product exactly. Under the old per-group ``rng.random`` /
``rng.integers`` draws, permuting the lines changed which draw each line
consumed, so the mapping was position-dependent.

They also assert the RNG stream is untouched when keys are present (proof the
pick is genuinely counter-based), and that the skip_order_cols fallback (no keys)
does consume the RNG.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.facts.sales.sales_logic import chunk_builder as cb
from src.facts.sales.sales_logic.globals import State


@pytest.fixture(autouse=True)
def _clean_state():
    State.reset()
    cb.reset_worker_caches()
    yield
    State.reset()
    cb.reset_worker_caches()


def _keys(n):
    orders = (10_000 + np.arange(n)).astype(np.int64)
    lines = (np.arange(n) % 4 + 1).astype(np.int64)   # 1..4 line numbers
    return orders, lines


def _rng_state(rng):
    return rng.bit_generator.state


# ---------------------------------------------------------------------------
# _sample_product_row_indices — global (non-assortment) path
# ---------------------------------------------------------------------------
def test_row_indices_pick_is_pure_function_of_key():
    n, n_products = 300, 50
    product_np = np.zeros((n_products, 3), dtype=np.float64)
    weight = np.abs(np.random.default_rng(1).random(n_products)) + 0.1
    orders, lines = _keys(n)
    rng = np.random.default_rng(0)

    before = dict(_rng_state(rng))
    out1 = cb._sample_product_row_indices(
        rng, n, product_np, m_offset=0, enabled=False,
        product_weight=weight, order_numbers=orders, line_numbers=lines,
    )
    # Counter-based: the RNG stream must be untouched when keys are present.
    assert _rng_state(rng) == before

    perm = np.random.default_rng(2).permutation(n)
    out2 = cb._sample_product_row_indices(
        rng, n, product_np, m_offset=0, enabled=False,
        product_weight=weight, order_numbers=orders[perm], line_numbers=lines[perm],
    )
    assert np.array_equal(out1[perm], out2)
    assert out1.dtype == np.int32


def test_row_indices_skip_cols_uses_rng():
    n, n_products = 100, 30
    product_np = np.zeros((n_products, 3), dtype=np.float64)
    weight = np.abs(np.random.default_rng(1).random(n_products)) + 0.1
    rng = np.random.default_rng(0)

    before = dict(_rng_state(rng))
    out = cb._sample_product_row_indices(
        rng, n, product_np, m_offset=0, enabled=False,
        product_weight=weight, order_numbers=None, line_numbers=None,
    )
    # No entity key -> falls back to the chunk RNG, which advances.
    assert _rng_state(rng) != before
    assert out.shape == (n,)
    # And it is still deterministic for a fixed seed.
    out_again = cb._sample_product_row_indices(
        np.random.default_rng(0), n, product_np, m_offset=0, enabled=False,
        product_weight=weight, order_numbers=None, line_numbers=None,
    )
    assert np.array_equal(out, out_again)


def test_row_indices_brand_two_stage_pick_is_pure_function_of_key():
    n, n_products, B = 400, 45, 3
    product_np = np.zeros((n_products, 3), dtype=np.float64)
    # Partition products into B brand buckets.
    buckets = np.array_split(np.arange(n_products, dtype=np.int32), B)
    State.brand_to_row_idx = [np.asarray(b, dtype=np.int32) for b in buckets]
    State.brand_prob_by_month = np.array([[0.5, 0.3, 0.2]], dtype=np.float64)
    weight = np.abs(np.random.default_rng(3).random(n_products)) + 0.1
    orders, lines = _keys(n)
    rng = np.random.default_rng(0)

    before = dict(_rng_state(rng))
    out1 = cb._sample_product_row_indices(
        rng, n, product_np, m_offset=0, enabled=True,
        product_weight=weight, order_numbers=orders, line_numbers=lines,
    )
    assert _rng_state(rng) == before  # brand + within-brand draws are both hashed

    perm = np.random.default_rng(4).permutation(n)
    out2 = cb._sample_product_row_indices(
        rng, n, product_np, m_offset=0, enabled=True,
        product_weight=weight, order_numbers=orders[perm], line_numbers=lines[perm],
    )
    assert np.array_equal(out1[perm], out2)


# ---------------------------------------------------------------------------
# _sample_products_per_store — store-grouped path (the position-sensitive one)
# ---------------------------------------------------------------------------
def test_per_store_pick_is_pure_function_of_key_across_grouping():
    n_products = 40
    product_np = np.zeros((n_products, 3), dtype=np.float64)
    weight = np.abs(np.random.default_rng(1).random(n_products)) + 0.1
    # Store 0 has no pool (exercises the global fallback); 1..3 have pools.
    store_to_product_rows = [
        None,
        np.arange(0, 20, dtype=np.int32),
        np.arange(10, 40, dtype=np.int32),
        np.arange(5, 25, dtype=np.int32),
    ]
    n = 500
    store_key_arr = np.random.default_rng(5).integers(0, 4, size=n).astype(np.int64)
    orders, lines = _keys(n)
    rng = np.random.default_rng(0)

    before = dict(_rng_state(rng))
    out1 = cb._sample_products_per_store(
        rng, store_key_arr, store_to_product_rows, product_np,
        product_weight=weight, _cdf_cache={}, use_brand_popularity=False,
        order_numbers=orders, line_numbers=lines,
    )
    assert _rng_state(rng) == before

    # Permuting the lines re-groups them differently by store; each line's
    # product must still follow its key. (This is exactly what the old
    # per-store rng.random draws could NOT guarantee.)
    perm = np.random.default_rng(6).permutation(n)
    out2 = cb._sample_products_per_store(
        rng, store_key_arr[perm], store_to_product_rows, product_np,
        product_weight=weight, _cdf_cache={}, use_brand_popularity=False,
        order_numbers=orders[perm], line_numbers=lines[perm],
    )
    assert np.array_equal(out1[perm], out2)

    # Every sampled product respects its store's assortment pool.
    for sk in (1, 2, 3):
        pool = set(store_to_product_rows[sk].tolist())
        mask = store_key_arr == sk
        assert set(out1[mask].tolist()) <= pool


def test_per_store_brand_aware_pick_is_pure_function_of_key():
    n_products, B = 40, 3
    product_np = np.zeros((n_products, 3), dtype=np.float64)
    weight = np.abs(np.random.default_rng(1).random(n_products)) + 0.1
    buckets = np.array_split(np.arange(n_products, dtype=np.int32), B)
    State.brand_to_row_idx = [np.asarray(b, dtype=np.int32) for b in buckets]
    State.brand_prob_by_month = np.array([[0.4, 0.35, 0.25]], dtype=np.float64)
    State.product_brand_key = np.concatenate(
        [np.full(len(b), i, dtype=np.int64) for i, b in enumerate(buckets)]
    )
    store_to_product_rows = [
        np.arange(0, 25, dtype=np.int32),
        np.arange(8, 40, dtype=np.int32),
    ]
    n = 500
    store_key_arr = np.random.default_rng(7).integers(0, 2, size=n).astype(np.int64)
    orders, lines = _keys(n)
    rng = np.random.default_rng(0)

    before = dict(_rng_state(rng))
    out1 = cb._sample_products_per_store(
        rng, store_key_arr, store_to_product_rows, product_np,
        product_weight=weight, _cdf_cache={}, use_brand_popularity=True,
        m_offset=0, order_numbers=orders, line_numbers=lines,
    )
    assert _rng_state(rng) == before

    perm = np.random.default_rng(8).permutation(n)
    out2 = cb._sample_products_per_store(
        rng, store_key_arr[perm], store_to_product_rows, product_np,
        product_weight=weight, _cdf_cache={}, use_brand_popularity=True,
        m_offset=0, order_numbers=orders[perm], line_numbers=lines[perm],
    )
    assert np.array_equal(out1[perm], out2)
