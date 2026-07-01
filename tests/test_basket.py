"""Tests for Phase 3.3 order-level basket-theme correlation."""
from __future__ import annotations

import numpy as np

from src.facts.sales.sales_logic.core.basket import (
    apply_basket_theme,
    basket_setup,
    reset_basket_cache,
    _theme_of,
)


def _orders(n_orders=400, lines_per=3, n_products=300, n_subcats=30):
    reset_basket_cache()
    subcat = (np.arange(n_products) % n_subcats).astype(np.int32)
    order_ids = np.repeat(np.arange(1, n_orders + 1), lines_per).astype(np.int64)
    line_num = np.tile(np.arange(1, lines_per + 1), n_orders).astype(np.int32)
    prod_idx = (np.arange(order_ids.size) % n_products).astype(np.int64)
    return subcat, order_ids, line_num, prod_idx


class TestBasketTheme:
    def test_biases_lines_toward_order_theme(self):
        subcat, order_ids, line_num, prod_idx = _orders()
        K, strength = 6, 0.8
        out = apply_basket_theme(prod_idx, order_ids, line_num, subcat,
                                 num_themes=K, strength=strength)
        prod_group, _ = basket_setup(subcat, K)
        theme = _theme_of(order_ids.astype(np.uint64), K)

        base_rate = float((prod_group[prod_idx] == theme).mean())   # ~1/K
        themed_rate = float((prod_group[out] == theme).mean())
        assert base_rate < 0.25
        assert themed_rate > base_rate + 0.3
        assert themed_rate > 0.5

    def test_all_lines_of_an_order_share_a_theme(self):
        subcat, order_ids, line_num, prod_idx = _orders()
        K = 6
        theme = _theme_of(order_ids.astype(np.uint64), K)
        # theme depends only on OrderNumber, so it is constant within an order.
        for oid in np.unique(order_ids)[:20]:
            t = theme[order_ids == oid]
            assert (t == t[0]).all()

    def test_deterministic_pure_function(self):
        subcat, order_ids, line_num, prod_idx = _orders()
        a = apply_basket_theme(prod_idx, order_ids, line_num, subcat,
                               num_themes=6, strength=0.5)
        b = apply_basket_theme(prod_idx, order_ids, line_num, subcat,
                               num_themes=6, strength=0.5)
        np.testing.assert_array_equal(a, b)  # no RNG — keyed on order/line ids

    def test_stronger_strength_biases_more(self):
        subcat, order_ids, line_num, prod_idx = _orders()
        prod_group, _ = basket_setup(subcat, 6)
        theme = _theme_of(order_ids.astype(np.uint64), 6)
        weak = apply_basket_theme(prod_idx, order_ids, line_num, subcat,
                                  num_themes=6, strength=0.2)
        strong = apply_basket_theme(prod_idx, order_ids, line_num, subcat,
                                    num_themes=6, strength=0.9)
        assert (prod_group[strong] == theme).mean() > (prod_group[weak] == theme).mean()

    def test_noop_paths(self):
        subcat, order_ids, line_num, prod_idx = _orders()
        # strength 0 -> unchanged
        np.testing.assert_array_equal(
            apply_basket_theme(prod_idx, order_ids, line_num, subcat,
                               num_themes=6, strength=0.0),
            prod_idx)
        # num_themes < 2 -> unchanged
        np.testing.assert_array_equal(
            apply_basket_theme(prod_idx, order_ids, line_num, subcat,
                               num_themes=1, strength=0.5),
            prod_idx)
        # no subcategory data -> unchanged
        np.testing.assert_array_equal(
            apply_basket_theme(prod_idx, order_ids, line_num, None,
                               num_themes=6, strength=0.5),
            prod_idx)

    def test_output_indices_stay_in_range(self):
        subcat, order_ids, line_num, prod_idx = _orders(n_products=300)
        out = apply_basket_theme(prod_idx, order_ids, line_num, subcat,
                                 num_themes=6, strength=1.0)
        assert out.min() >= 0 and out.max() < 300
