"""Tests for wishlists pipeline modules: accumulator, micro_agg, selection, scd2, constants."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from src.facts.wishlists.accumulator import WishlistAccumulator
from src.facts.wishlists.micro_agg import micro_aggregate_wishlists
from src.facts.wishlists.constants import (
    NS_PER_DAY,
    PRIORITY_VALUES,
    PRIORITY_WEIGHTS,
    WishlistsCfg,
    bridge_schema,
    read_cfg,
)
from src.facts.wishlists.selection import (
    SubcatPool,
    build_subcategory_pool,
    generate_wishlist_items,
    pool_to_dict,
    subcategory_pool_from_dict,
)
from src.facts.wishlists.scd2 import build_scd2_price_lookup, resolve_scd2_prices


# ===================================================================
# WishlistAccumulator
# ===================================================================


class TestWishlistAccumulator:
    def test_empty_finalize(self):
        acc = WishlistAccumulator()
        df = acc.finalize()
        assert df.empty
        assert "CustomerKey" in df.columns
        assert "ProductKey" in df.columns

    def test_has_data_false_initially(self):
        acc = WishlistAccumulator()
        assert acc.has_data is False

    def test_add_and_finalize(self):
        acc = WishlistAccumulator()
        micro = {
            "customer_key": np.array([1, 2, 3], dtype=np.int64),
            "product_key": np.array([10, 20, 30], dtype=np.int64),
        }
        acc.add(micro)
        assert acc.has_data is True
        df = acc.finalize()
        assert len(df) == 3
        assert set(df["CustomerKey"]) == {1, 2, 3}

    def test_add_none_ignored(self):
        acc = WishlistAccumulator()
        acc.add(None)
        assert acc.has_data is False

    def test_add_empty_ignored(self):
        acc = WishlistAccumulator()
        acc.add({"customer_key": np.array([], dtype=np.int64)})
        assert acc.has_data is False

    def test_deduplication(self):
        acc = WishlistAccumulator()
        acc.add({
            "customer_key": np.array([1, 1, 2], dtype=np.int64),
            "product_key": np.array([10, 10, 20], dtype=np.int64),
        })
        acc.add({
            "customer_key": np.array([1, 3], dtype=np.int64),
            "product_key": np.array([10, 30], dtype=np.int64),
        })
        df = acc.finalize()
        assert len(df) == 3  # (1,10), (2,20), (3,30) — duplicate (1,10) removed

    def test_multiple_parts_concatenated(self):
        acc = WishlistAccumulator()
        acc.add({
            "customer_key": np.array([1], dtype=np.int64),
            "product_key": np.array([10], dtype=np.int64),
        })
        acc.add({
            "customer_key": np.array([2], dtype=np.int64),
            "product_key": np.array([20], dtype=np.int64),
        })
        df = acc.finalize()
        assert len(df) == 2


# ===================================================================
# Wishlists micro-aggregation
# ===================================================================


class TestMicroAggregateWishlists:
    def _make_table(self, ck, pk):
        return pa.table({
            "CustomerKey": pa.array(ck, type=pa.int64()),
            "ProductKey": pa.array(pk, type=pa.int64()),
        })

    def test_basic_extraction(self):
        tbl = self._make_table([1, 2, 3], [10, 20, 30])
        result = micro_aggregate_wishlists(tbl)
        assert result is not None
        assert len(result["customer_key"]) == 3

    def test_deduplication(self):
        tbl = self._make_table([1, 1, 2], [10, 10, 20])
        result = micro_aggregate_wishlists(tbl)
        assert result is not None
        assert len(result["customer_key"]) == 2

    def test_missing_columns(self):
        tbl = pa.table({"CustomerKey": pa.array([1], type=pa.int64())})
        result = micro_aggregate_wishlists(tbl)
        assert result is None

    def test_empty_table(self):
        tbl = pa.table({
            "CustomerKey": pa.array([], type=pa.int64()),
            "ProductKey": pa.array([], type=pa.int64()),
        })
        result = micro_aggregate_wishlists(tbl)
        assert result is None


# ===================================================================
# Constants and config
# ===================================================================


class TestWishlistsConstants:
    def test_priority_weights_sum_to_one(self):
        assert PRIORITY_WEIGHTS.sum() == pytest.approx(1.0)

    def test_priority_values_length(self):
        assert len(PRIORITY_VALUES) == len(PRIORITY_WEIGHTS)

    def test_ns_per_day(self):
        assert NS_PER_DAY == 86_400 * 10**9

    def test_bridge_schema_fields(self):
        schema = bridge_schema()
        names = schema.names
        assert "WishlistKey" in names
        assert "CustomerKey" in names
        assert "ProductKey" in names
        assert "AddedDate" in names
        assert "Priority" in names
        assert "Quantity" in names
        assert "NetPrice" in names

    def test_read_cfg_defaults(self):
        cfg = type("Cfg", (), {"wishlists": None})()
        wc = read_cfg(cfg)
        assert wc.enabled is False
        assert wc.participation_rate == 0.35
        assert wc.seed == 500

    def test_read_cfg_custom(self):
        wl_section = type("WL", (), {
            "enabled": True,
            "participation_rate": 0.5,
            "avg_items": 5.0,
            "max_items": 15,
            "pre_browse_days": 60,
            "affinity_strength": 0.8,
            "conversion_rate": 0.4,
            "seed": 42,
            "write_chunk_rows": 100_000,
        })()
        cfg = type("Cfg", (), {"wishlists": wl_section})()
        wc = read_cfg(cfg)
        assert wc.enabled is True
        assert wc.participation_rate == 0.5
        assert wc.seed == 42
        assert wc.max_items == 15


# ===================================================================
# SubcatPool and selection
# ===================================================================


class TestSubcatPool:
    def _make_pool_data(self):
        """Create minimal product arrays: 6 products in 2 subcategories."""
        prod_subcat = np.array([1, 1, 1, 2, 2, 2], dtype=np.int64)
        product_weights = np.array([0.1, 0.2, 0.2, 0.15, 0.15, 0.2])
        product_weights /= product_weights.sum()
        return prod_subcat, product_weights

    def test_build_subcategory_pool(self):
        prod_subcat, product_weights = self._make_pool_data()
        pool, global_cdf = build_subcategory_pool(prod_subcat, product_weights)

        assert isinstance(pool, SubcatPool)
        assert len(pool.sc_idx_map) == 2  # 2 subcategories
        assert 1 in pool.sc_idx_map
        assert 2 in pool.sc_idx_map
        assert global_cdf[-1] == pytest.approx(1.0)

    def test_pool_roundtrip(self):
        prod_subcat, product_weights = self._make_pool_data()
        pool, _ = build_subcategory_pool(prod_subcat, product_weights)

        d = pool_to_dict(pool)
        pool2 = subcategory_pool_from_dict(d)

        assert pool2.sc_idx_map == pool.sc_idx_map
        np.testing.assert_array_equal(pool2.subcat_starts, pool.subcat_starts)
        np.testing.assert_array_equal(pool2.subcat_ends, pool.subcat_ends)
        np.testing.assert_array_equal(pool2.pool_indices, pool.pool_indices)
        np.testing.assert_allclose(pool2.cdf_data, pool.cdf_data)

    def test_pool_cdf_normalized(self):
        prod_subcat, product_weights = self._make_pool_data()
        pool, _ = build_subcategory_pool(prod_subcat, product_weights)

        for sc_id, sc_i in pool.sc_idx_map.items():
            cs = int(pool.subcat_cdf_starts[sc_i])
            ce = int(pool.subcat_cdf_ends[sc_i])
            if ce > cs:
                assert pool.cdf_data[ce - 1] == pytest.approx(1.0)


class TestGenerateWishlistItems:
    def _setup(self):
        """Build minimal test data for generate_wishlist_items."""
        prod_subcat = np.array([1, 1, 2, 2, 3], dtype=np.int64)
        product_weights = np.ones(5) / 5.0
        pool, global_cdf = build_subcategory_pool(prod_subcat, product_weights)
        return prod_subcat, product_weights, pool, global_cdf

    def test_basic_generation(self):
        prod_subcat, _, pool, global_cdf = self._setup()
        rng = np.random.default_rng(42)

        n = 3
        cust_keys = np.array([100, 200, 300], dtype=np.int64)
        earliest_ns = np.full(n, 0, dtype=np.int64)
        latest_ns = np.full(n, 10 * NS_PER_DAY, dtype=np.int64)
        items_per = np.array([2, 3, 1], dtype=np.int64)

        out_prod_idx, out_ckey, out_date_ns, out_priority, out_quantity = (
            generate_wishlist_items(
                rng,
                cust_keys=cust_keys,
                earliest_ns=earliest_ns,
                latest_ns=latest_ns,
                items_per=items_per,
                purchased_map={},
                prod_subcat=prod_subcat,
                n_products=5,
                global_cdf=global_cdf,
                pool=pool,
                conversion_rate=0.3,
                affinity_strength=0.6,
            )
        )

        total = items_per.sum()
        assert len(out_prod_idx) == total
        assert len(out_ckey) == total
        assert len(out_date_ns) == total
        assert len(out_priority) == total
        assert len(out_quantity) == total

        # Product indices in valid range
        assert np.all(out_prod_idx >= 0)
        assert np.all(out_prod_idx < 5)

        # Customer keys match
        assert set(out_ckey) == {100, 200, 300}

        # Priorities are valid values
        valid_prios = set(PRIORITY_VALUES)
        assert all(p in valid_prios for p in out_priority)

        # Quantities are positive
        assert np.all(out_quantity >= 1)

    def test_empty_participants(self):
        prod_subcat, _, pool, global_cdf = self._setup()
        rng = np.random.default_rng(42)

        out = generate_wishlist_items(
            rng,
            cust_keys=np.array([], dtype=np.int64),
            earliest_ns=np.array([], dtype=np.int64),
            latest_ns=np.array([], dtype=np.int64),
            items_per=np.array([], dtype=np.int64),
            purchased_map={},
            prod_subcat=prod_subcat,
            n_products=5,
            global_cdf=global_cdf,
            pool=pool,
            conversion_rate=0.3,
            affinity_strength=0.6,
        )
        assert all(len(a) == 0 for a in out)

    def test_conversion_from_purchases(self):
        """With high conversion_rate, items should often come from purchases."""
        prod_subcat, _, pool, global_cdf = self._setup()
        rng = np.random.default_rng(42)

        purchased_map = {100: [0, 1, 2, 3, 4]}
        cust_keys = np.array([100], dtype=np.int64)
        earliest_ns = np.array([0], dtype=np.int64)
        latest_ns = np.array([10 * NS_PER_DAY], dtype=np.int64)
        items_per = np.array([5], dtype=np.int64)

        out_prod_idx, *_ = generate_wishlist_items(
            rng,
            cust_keys=cust_keys,
            earliest_ns=earliest_ns,
            latest_ns=latest_ns,
            items_per=items_per,
            purchased_map=purchased_map,
            prod_subcat=prod_subcat,
            n_products=5,
            global_cdf=global_cdf,
            pool=pool,
            conversion_rate=1.0,
            affinity_strength=0.0,
        )
        # All 5 products are purchased — with conversion_rate=1.0 all should come from purchases
        assert set(out_prod_idx).issubset({0, 1, 2, 3, 4})

    def test_deterministic(self):
        """Same seed produces same output."""
        prod_subcat, _, pool, global_cdf = self._setup()

        kwargs = dict(
            cust_keys=np.array([1, 2], dtype=np.int64),
            earliest_ns=np.array([0, 0], dtype=np.int64),
            latest_ns=np.array([10 * NS_PER_DAY, 10 * NS_PER_DAY], dtype=np.int64),
            items_per=np.array([3, 3], dtype=np.int64),
            purchased_map={},
            prod_subcat=prod_subcat,
            n_products=5,
            global_cdf=global_cdf,
            pool=pool,
            conversion_rate=0.3,
            affinity_strength=0.6,
        )

        rng1 = np.random.default_rng(99)
        out1 = generate_wishlist_items(rng1, **kwargs)

        rng2 = np.random.default_rng(99)
        out2 = generate_wishlist_items(rng2, **kwargs)

        for a, b in zip(out1, out2):
            np.testing.assert_array_equal(a, b)

    def test_no_duplicates_per_customer(self):
        """Each customer should not get the same product twice."""
        prod_subcat = np.arange(20, dtype=np.int64) % 4
        product_weights = np.ones(20) / 20.0
        pool, global_cdf = build_subcategory_pool(prod_subcat, product_weights)
        rng = np.random.default_rng(42)

        cust_keys = np.array([1], dtype=np.int64)
        items_per = np.array([10], dtype=np.int64)

        out_prod_idx, *_ = generate_wishlist_items(
            rng,
            cust_keys=cust_keys,
            earliest_ns=np.array([0], dtype=np.int64),
            latest_ns=np.array([10 * NS_PER_DAY], dtype=np.int64),
            items_per=items_per,
            purchased_map={},
            prod_subcat=prod_subcat,
            n_products=20,
            global_cdf=global_cdf,
            pool=pool,
            conversion_rate=0.0,
            affinity_strength=0.0,
        )
        assert len(set(out_prod_idx)) == 10  # all unique

    def test_high_collision_does_not_overflow_misc_pool(self):
        """WISH-1: heavy dedup collisions (tiny catalog, max items/customer, full
        affinity) drive items toward the 50+200 retry paths. The misc RNG pool must
        be refilled with enough headroom that no item exhausts it mid-iteration
        (IndexError with the old headroom of 60)."""
        prod_subcat = np.array([1, 1, 2, 2, 3], dtype=np.int64)
        product_weights = np.ones(5) / 5.0
        pool, global_cdf = build_subcategory_pool(prod_subcat, product_weights)
        rng = np.random.default_rng(123)

        n_cust = 500
        cust_keys = np.arange(1, n_cust + 1, dtype=np.int64)
        items_per = np.full(n_cust, 5, dtype=np.int64)  # == n_products -> max dedup
        total = int(items_per.sum())

        out_prod_idx, *_ = generate_wishlist_items(
            rng,
            cust_keys=cust_keys,
            earliest_ns=np.zeros(n_cust, dtype=np.int64),
            latest_ns=np.full(n_cust, 10 * NS_PER_DAY, dtype=np.int64),
            items_per=items_per,
            purchased_map={},
            prod_subcat=prod_subcat,
            n_products=5,
            global_cdf=global_cdf,
            pool=pool,
            conversion_rate=0.0,    # never short-circuit via purchases
            affinity_strength=1.0,  # always enter the affinity retry path
        )
        assert len(out_prod_idx) == total
        assert np.all((out_prod_idx >= 0) & (out_prod_idx < 5))


# ===================================================================
# SCD2 price resolution
# ===================================================================


class TestSCD2PriceLookup:
    def test_no_scd2_column(self):
        df = pd.DataFrame({"ProductKey": [1, 2], "ListPrice": [10.0, 20.0]})
        result = build_scd2_price_lookup(
            df,
            prod_keys_current=np.array([1, 2], dtype=np.int64),
            prod_prices_current=np.array([10.0, 20.0]),
        )
        assert result is None

    def test_single_version_per_product(self):
        df = pd.DataFrame({
            "ProductKey": [1, 2],
            "ListPrice": [10.0, 20.0],
            "EffectiveStartDate": ["2023-01-01", "2023-01-01"],
        })
        result = build_scd2_price_lookup(
            df,
            prod_keys_current=np.array([1, 2], dtype=np.int64),
            prod_prices_current=np.array([10.0, 20.0]),
        )
        assert result is None  # no multi-version products

    def test_multi_version_lookup(self):
        df = pd.DataFrame({
            "ProductKey": [1, 1, 2],
            "ListPrice": [8.0, 10.0, 20.0],
            "EffectiveStartDate": ["2023-01-01", "2023-07-01", "2023-01-01"],
        })
        starts, prices = build_scd2_price_lookup(
            df,
            prod_keys_current=np.array([1, 2], dtype=np.int64),
            prod_prices_current=np.array([10.0, 20.0]),
        )
        assert starts.shape[0] == 2  # 2 products
        assert starts.shape[1] >= 2  # at least 2 version slots
        # Product 1 (index 0) should have 2 versions
        assert prices[0, 0] == 8.0
        assert prices[0, 1] == 10.0


class TestResolveSCD2Prices:
    def test_resolve_prices(self):
        # 2 products, 2 version slots
        # Product 0: version 0 from day 0, version 1 from day 180
        # Product 1: version 0 from day 0, no second version (max sentinel)
        scd2_starts = np.array([
            [0, 180],
            [0, np.iinfo(np.int64).max],
        ], dtype=np.int64)
        scd2_prices = np.array([
            [8.0, 10.0],
            [20.0, 20.0],
        ], dtype=np.float64)

        prod_idx = np.array([0, 0, 1], dtype=np.int64)
        # Day 90 (before version 1), Day 200 (after version 1), Day 100
        ns_per_day = 24 * 3600 * 10**9
        date_ns = np.array([90 * ns_per_day, 200 * ns_per_day, 100 * ns_per_day], dtype=np.int64)

        prices = resolve_scd2_prices(prod_idx, date_ns, scd2_starts, scd2_prices)
        assert prices[0] == 8.0   # product 0, day 90 → version 0
        assert prices[1] == 10.0  # product 0, day 200 → version 1
        assert prices[2] == 20.0  # product 1, day 100 → version 0
