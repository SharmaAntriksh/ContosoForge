"""Tests for the quantity / basket-size model."""
from __future__ import annotations

import numpy as np
import pytest

from src.exceptions import SalesError
from src.facts.sales.sales_models.quantity_model import (
    _DEFAULTS,
    _load_cfg,
    _reset_cache,
    build_quantity,
)
from src.facts.sales.sales_logic.globals import State


# ===================================================================
# Default constants
# ===================================================================

class TestDefaults:
    def test_monthly_factors_length(self):
        assert len(_DEFAULTS["monthly_factors"]) == 12

    def test_min_qty_at_least_one(self):
        assert _DEFAULTS["min_qty"] >= 1

    def test_max_gte_min(self):
        assert _DEFAULTS["max_qty"] >= _DEFAULTS["min_qty"]

    def test_lambda_positive(self):
        assert _DEFAULTS["base_poisson_lambda"] > 0

    def test_noise_sigma_non_negative(self):
        assert _DEFAULTS["noise_sigma"] >= 0


# ===================================================================
# _load_cfg with mocked State
# ===================================================================

class TestLoadCfg:
    def setup_method(self):
        State.reset()
        _reset_cache()

    def teardown_method(self):
        State.reset()
        _reset_cache()

    def test_loads_defaults_when_no_models(self):
        State.models_cfg = {}

        cfg = _load_cfg()

        assert cfg["base_poisson_lambda"] == _DEFAULTS["base_poisson_lambda"]
        assert cfg["min_qty"] == _DEFAULTS["min_qty"]
        assert cfg["max_qty"] == _DEFAULTS["max_qty"]

    def test_custom_lambda(self):
        State.models_cfg = {"quantity": {"base_poisson_lambda": 3.0}}

        cfg = _load_cfg()

        assert cfg["base_poisson_lambda"] == 3.0

    def test_negative_lambda_raises(self):
        State.models_cfg = {"quantity": {"base_poisson_lambda": -1.0}}

        with pytest.raises(SalesError, match="must be >= 0"):
            _load_cfg()

    def test_wrong_monthly_factors_length_raises(self):
        State.models_cfg = {"quantity": {"monthly_factors": [1.0, 1.0]}}

        with pytest.raises(SalesError, match="12 floats"):
            _load_cfg()

    def test_min_max_swap_if_inverted(self):
        State.models_cfg = {"quantity": {"min_qty": 10, "max_qty": 2}}

        cfg = _load_cfg()

        assert cfg["min_qty"] == 2
        assert cfg["max_qty"] == 10

    def test_legacy_noise_sd_key(self):
        State.models_cfg = {"quantity": {"noise_sd": 0.25}}

        cfg = _load_cfg()

        assert cfg["noise_sigma"] == 0.25


# ===================================================================
# build_quantity
# ===================================================================

class TestBuildQuantity:
    def setup_method(self):
        State.reset()
        _reset_cache()

    def teardown_method(self):
        State.reset()
        _reset_cache()

    def _setup_state(self):
        State.models_cfg = {
            "quantity": {
                "base_poisson_lambda": 1.7,
                "monthly_factors": _DEFAULTS["monthly_factors"],
                "noise_sigma": 0.12,
                "min_qty": 1,
                "max_qty": 8,
            },
        }

    def test_deterministic_with_same_seed(self):
        self._setup_state()
        dates = np.array(["2023-01-15", "2023-06-20", "2023-12-01"] * 100,
                         dtype="datetime64[D]")

        qty1 = build_quantity(np.random.default_rng(42), dates)
        qty2 = build_quantity(np.random.default_rng(42), dates)

        np.testing.assert_array_equal(qty1, qty2)

    def test_different_seed_differs(self):
        self._setup_state()
        dates = np.array(["2023-01-15"] * 1000, dtype="datetime64[D]")

        qty1 = build_quantity(np.random.default_rng(42), dates)
        qty2 = build_quantity(np.random.default_rng(99), dates)

        # Extremely unlikely to be identical with 1000 draws
        assert not np.array_equal(qty1, qty2)

    def test_output_within_bounds(self):
        self._setup_state()
        dates = np.array(["2023-03-15"] * 5000, dtype="datetime64[D]")

        qty = build_quantity(np.random.default_rng(42), dates)

        assert qty.min() >= 1
        assert qty.max() <= 8

    def test_output_dtype_is_int(self):
        self._setup_state()
        dates = np.array(["2023-03-15"] * 100, dtype="datetime64[D]")

        qty = build_quantity(np.random.default_rng(42), dates)

        assert np.issubdtype(qty.dtype, np.integer)

    def test_empty_dates(self):
        self._setup_state()
        dates = np.array([], dtype="datetime64[D]")

        qty = build_quantity(np.random.default_rng(42), dates)

        assert qty.shape == (0,)

    def test_zero_noise(self):
        """With noise_sigma=0, output should still be valid."""
        State.models_cfg = {
            "quantity": {
                "base_poisson_lambda": 2.0,
                "monthly_factors": [1.0] * 12,
                "noise_sigma": 0.0,
                "min_qty": 1,
                "max_qty": 10,
            },
        }
        dates = np.array(["2023-06-15"] * 500, dtype="datetime64[D]")

        qty = build_quantity(np.random.default_rng(42), dates)

        assert qty.min() >= 1
        assert qty.max() <= 10

    def test_seasonality_affects_mean(self):
        """Months with higher seasonal factors should produce higher mean qty."""
        # Factor for June (index 5) = 1.02, January (index 0) = 0.99
        State.models_cfg = {
            "quantity": {
                "base_poisson_lambda": 3.0,
                "monthly_factors": [0.5, 0.5, 0.5, 0.5, 0.5, 2.0,
                                    0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
                "noise_sigma": 0.0,
                "min_qty": 1,
                "max_qty": 20,
            },
        }
        n = 5000
        jan_dates = np.array(["2023-01-15"] * n, dtype="datetime64[D]")
        jun_dates = np.array(["2023-06-15"] * n, dtype="datetime64[D]")

        rng1 = np.random.default_rng(42)
        jan_qty = build_quantity(rng1, jan_dates)
        rng2 = np.random.default_rng(42)
        jun_qty = build_quantity(rng2, jun_dates)

        # June factor (2.0) is 4x January factor (0.5) → mean should be notably higher
        assert jun_qty.mean() > jan_qty.mean() * 1.5

    def test_noise_increases_variance(self):
        """Positive noise_sigma should increase output variance vs zero noise."""
        base_cfg = {
            "base_poisson_lambda": 3.0,
            "monthly_factors": [1.0] * 12,
            "min_qty": 1,
            "max_qty": 20,
        }
        dates = np.array(["2023-03-15"] * 5000, dtype="datetime64[D]")

        State.models_cfg = {"quantity": {**base_cfg, "noise_sigma": 0.0}}
        _reset_cache()
        qty_no_noise = build_quantity(np.random.default_rng(42), dates)

        State.models_cfg = {"quantity": {**base_cfg, "noise_sigma": 0.5}}
        _reset_cache()
        qty_noisy = build_quantity(np.random.default_rng(42), dates)

        assert qty_noisy.std() > qty_no_noise.std()


# ===================================================================
# Phase 3.1 — quantity elasticity (price + propensity)
# ===================================================================

class TestQuantityElasticity:
    def setup_method(self):
        State.reset()
        _reset_cache()

    def teardown_method(self):
        State.reset()
        _reset_cache()

    def _bind(self, *, enabled=True, eps=1.0, ref=100.0,
              prop_strength=0.0, popularity=None):
        State.models_cfg = {
            "quantity": {
                "base_poisson_lambda": 3.0,
                "monthly_factors": [1.0] * 12,
                "noise_sigma": 0.0,
                "min_qty": 1,
                "max_qty": 20,
                "elasticity": {
                    "enabled": enabled,
                    "price_elasticity": eps,
                    "reference_price": ref,
                    "factor_clip": [0.1, 5.0],
                    "propensity_strength": prop_strength,
                    "propensity_clip": [0.1, 5.0],
                },
            },
        }
        if popularity is not None:
            State.product_popularity = np.asarray(popularity, dtype=np.float64)

    def test_cheap_products_get_higher_quantity(self):
        """(price/ref)^(-ε) makes cheap items sell in larger quantities."""
        self._bind(eps=1.0, ref=100.0)
        n = 4000
        dates = np.array(["2023-06-15"] * n, dtype="datetime64[D]")
        cheap = np.full(n, 10.0)     # 1/10 of reference
        pricey = np.full(n, 1000.0)  # 10x reference

        q_cheap = build_quantity(np.random.default_rng(7), dates, unit_price=cheap)
        q_pricey = build_quantity(np.random.default_rng(7), dates, unit_price=pricey)

        assert q_cheap.mean() > q_pricey.mean() * 1.5

    def test_disabled_matches_no_unit_price(self):
        """enabled=False ⇒ identical to the legacy product-agnostic draw."""
        self._bind(enabled=False, eps=1.0, ref=100.0)
        n = 2000
        dates = np.array(["2023-06-15"] * n, dtype="datetime64[D]")
        price = np.full(n, 10.0)

        q_with = build_quantity(np.random.default_rng(11), dates, unit_price=price)
        _reset_cache()
        q_base = build_quantity(np.random.default_rng(11), dates)
        np.testing.assert_array_equal(q_with, q_base)

    def test_propensity_lifts_popular_products(self):
        """Higher PopularityScore ⇒ larger quantities (propensity term)."""
        # Two products: row 0 unpopular (pop 10), row 1 popular (pop 90).
        self._bind(eps=0.0, prop_strength=1.0, popularity=[10.0, 90.0])
        n = 4000
        dates = np.array(["2023-06-15"] * n, dtype="datetime64[D]")
        price = np.full(n, 100.0)
        idx_unpop = np.zeros(n, dtype=np.int64)
        idx_pop = np.ones(n, dtype=np.int64)

        q_unpop = build_quantity(np.random.default_rng(3), dates,
                                 product_row_idx=idx_unpop, unit_price=price)
        q_pop = build_quantity(np.random.default_rng(3), dates,
                               product_row_idx=idx_pop, unit_price=price)

        assert q_pop.mean() > q_unpop.mean() * 1.3

    def test_reference_price_auto_from_product_pool(self):
        """reference_price=None resolves to the median catalog ListPrice."""
        from src.facts.sales.sales_models.quantity_model import _reference_price
        # product_np columns: [ProductKey, ListPrice, UnitCost]
        State.product_np = np.array(
            [[1, 10.0, 4.0], [2, 100.0, 40.0], [3, 1000.0, 400.0]],
            dtype=np.float64,
        )
        assert _reference_price(None) == 100.0
        # explicit config value overrides the auto median
        assert _reference_price(250.0) == 250.0
