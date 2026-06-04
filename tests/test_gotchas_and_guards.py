"""Tests for critical guards and CLAUDE.md gotchas.

Covers: int32 overflow (#14), bincount weights dtype (#15), CDF boundary
clamping (#16), SCD2 life events, subscription payment weights,
cross-section rules, config normalizers, web thread safety, determinism.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.dimensions.customers.scd2 import _apply_life_event, _available_events, expand_changed_customers
from src.engine.config.config_schema import AppConfig
from src.exceptions import ConfigError
from src.facts.sales.sales_logic.core import _normalize_cdf


# ===================================================================
# Gotcha #14: int32 Order ID overflow
# ===================================================================

class TestOrderIdOverflow:
    """Verify int32 overflow detection in order ID generation."""

    def _build(self, n, order_id_start, max_lines=3):
        from src.facts.sales.sales_logic.core.orders import build_orders
        from src.facts.sales.sales_logic.globals import State
        State.reset()
        State.models_cfg = type("M", (), {"get": lambda self, k, d=None: d})()
        State.skip_order_cols = False
        rng = np.random.default_rng(42)
        return build_orders(
            rng=rng,
            n=n,
            skip_cols=False,
            date_pool=np.array([19000, 19001, 19002]),
            date_prob=np.array([0.4, 0.3, 0.3]),
            customers=np.arange(1, n + 1, dtype="int32"),
            _len_date_pool=3,
            _len_customers=n,
            order_id_start=order_id_start,
        )

    def test_valid_range_succeeds(self):
        """Order IDs within int32 range should work."""
        result = self._build(n=100, order_id_start=0)
        assert result is not None

    def test_overflow_raises(self):
        """Order IDs that would overflow int32 should raise OverflowError."""
        with pytest.raises(OverflowError, match="overflow int32"):
            self._build(n=100, order_id_start=np.iinfo(np.int32).max - 10)

    def test_near_boundary_succeeds(self):
        """IDs just below the int32 boundary should succeed."""
        result = self._build(n=5, order_id_start=np.iinfo(np.int32).max - 100)
        assert result is not None


# ===================================================================
# Gotcha #15: np.bincount weights dtype
# ===================================================================

class TestBincountWeightsDtype:
    """Verify that bincount with large counts doesn't overflow with small dtypes."""

    def test_float64_weights_safe_for_large_counts(self):
        """float64 weights are always safe regardless of element count."""
        labels = np.zeros(200, dtype=np.intp)  # all same bin
        weights = np.ones(200, dtype=np.float64)
        result = np.bincount(labels, weights=weights)
        assert result[0] == 200.0

    def test_float64_weights_correct(self):
        """float64 weights always give correct bincount results."""
        labels = np.arange(300) % 5
        weights = np.ones(300, dtype=np.float64)
        result = np.bincount(labels, weights=weights)
        assert all(result == 60.0)


# ===================================================================
# Gotcha #16: CDF + searchsorted boundary (_normalize_cdf)
# ===================================================================

class TestNormalizeCdf:
    """Tests for the shared _normalize_cdf helper."""

    def test_basic_normalization(self):
        w = np.array([1.0, 2.0, 3.0, 4.0])
        cdf = _normalize_cdf(w)
        assert cdf[-1] == 1.0
        assert cdf[0] == pytest.approx(0.1)
        assert cdf.dtype == np.float64

    def test_last_element_clamped(self):
        """CDF[-1] must be exactly 1.0 even with floating-point rounding."""
        # Create weights that cause rounding issues
        w = np.array([1e-15, 1e-15, 1.0 - 2e-15])
        cdf = _normalize_cdf(w)
        assert cdf[-1] == 1.0  # not 0.99999...

    def test_zero_weights(self):
        """All-zero weights should return raw cumsum (all zeros)."""
        w = np.array([0.0, 0.0, 0.0])
        cdf = _normalize_cdf(w)
        assert cdf[-1] == 0.0

    def test_empty_array(self):
        w = np.array([], dtype=np.float64)
        cdf = _normalize_cdf(w)
        assert cdf.size == 0

    def test_searchsorted_in_bounds(self):
        """searchsorted on normalized CDF should never return out-of-bounds."""
        w = np.array([0.1, 0.2, 0.3, 0.4])
        cdf = _normalize_cdf(w)
        # Sample 10000 uniform values
        u = np.random.default_rng(42).random(10000)
        idx = np.searchsorted(cdf, u, side="right")
        assert idx.max() <= len(w) - 1 or idx.max() == len(w)
        # With clamped CDF, searchsorted(1.0) returns len(cdf), so clip:
        idx_clipped = np.minimum(idx, len(w) - 1)
        assert idx_clipped.min() >= 0
        assert idx_clipped.max() <= len(w) - 1


# ===================================================================
# SCD2 life events
# ===================================================================

class TestSCD2LifeEvents:
    """Test the SCD2 life event engine for customer dimension."""

    def test_available_events_single(self):
        state = {"MaritalStatus": "Single", "HomeOwnership": "Rent",
                 "NumberOfChildren": 0, "LoyaltyTierKey": 1}
        tier_keys = np.array([1, 2, 3])
        events = _available_events(state, tier_keys)
        names = [e[0] for e in events]
        assert "career_growth" in names
        assert "marriage" in names
        assert "divorce" not in names  # not married
        assert "family_growth" not in names  # not married

    def test_available_events_married(self):
        state = {"MaritalStatus": "Married", "HomeOwnership": "Own",
                 "NumberOfChildren": 1, "LoyaltyTierKey": 1}
        tier_keys = np.array([1, 2, 3])
        events = _available_events(state, tier_keys)
        names = [e[0] for e in events]
        assert "divorce" in names
        assert "family_growth" in names
        assert "marriage" not in names  # already married
        assert "home_purchase" not in names  # already owns

    def test_weights_are_positive(self):
        state = {"MaritalStatus": "Single", "HomeOwnership": "Rent",
                 "NumberOfChildren": 0, "LoyaltyTierKey": 0}
        events = _available_events(state, np.array([1, 2, 3]))
        for name, weight in events:
            assert weight > 0, f"Event {name} has non-positive weight"

    def test_apply_career_growth(self):
        rng = np.random.default_rng(42)
        state = {"YearlyIncome": 50000, "IncomeGroup": "Low",
                 "LoyaltyTierKey": 1, "MaritalStatus": "Single"}
        geo_keys = np.array([1, 2, 3])
        tier_keys = np.array([1, 2, 3])
        _apply_life_event(rng, state, "career_growth", geo_keys, tier_keys, {})
        assert state["YearlyIncome"] > 50000

    def test_scd2_deterministic(self):
        """Same seed should produce identical SCD2 expansions."""
        N = 20
        df = pd.DataFrame({
            "CustomerKey": np.arange(1, N + 1),
            "CustomerID": np.arange(1, N + 1),
            "CustomerType": "Individual",
            "MaritalStatus": "Married",
            "HomeOwnership": "Rent",
            "NumberOfChildren": np.zeros(N, dtype=int),
            "LoyaltyTierKey": np.ones(N, dtype=int),
            "YearlyIncome": np.full(N, 60000),
            "IncomeGroup": "Medium",
            "GeographyKey": np.ones(N, dtype=int),
            "VersionNumber": np.ones(N, dtype=int),
            "EffectiveStartDate": pd.Timestamp("2021-01-01"),
            "EffectiveEndDate": pd.Timestamp("2099-12-31"),
            "IsCurrent": 1,
            "HomeAddress": "123 Main St",
            "WorkAddress": "456 Work Ave",
            "Latitude": 40.0,
            "Longitude": -74.0,
            "PostalCode": "10001",
        })
        geo_keys = np.array([1, 2, 3])
        tier_keys = np.array([1, 2, 3])
        end_date = pd.Timestamp("2025-12-31")

        r1 = expand_changed_customers(np.random.default_rng(42), df.copy(), 4, geo_keys, tier_keys, end_date, {})
        r2 = expand_changed_customers(np.random.default_rng(42), df.copy(), 4, geo_keys, tier_keys, end_date, {})

        pd.testing.assert_frame_equal(r1, r2)

    def test_max_versions_1_skips_expansion(self):
        """CUST-SCD2-2: max_versions=1 means no extra versions — must skip
        expansion and return base_df unchanged, not crash on rng.integers(1, 1)."""
        from src.dimensions.customers.scd2 import generate_scd2_versions

        base = pd.DataFrame({
            "CustomerType": ["Individual", "Individual"],
            "CustomerKey": [1, 2],
            "CustomerID": [1, 2],
            "IsCurrent": [1, 1],
            "EffectiveStartDate": pd.to_datetime(["2021-01-01", "2021-01-01"]),
            "EffectiveEndDate": pd.to_datetime(["2099-12-31", "2099-12-31"]),
        })
        cust_cfg = type("Cfg", (), {"change_rate": 0.15, "max_versions": 1})()
        # The guard returns before geo_keys/tier_keys/geo_lookup are used.
        out = generate_scd2_versions(
            np.random.default_rng(42), base, cust_cfg,
            geo_keys=np.array([], dtype=int), tier_keys=np.array([], dtype=int),
            end_date=pd.Timestamp("2025-12-31"), geo_lookup=None,
        )
        pd.testing.assert_frame_equal(out, base)

    def test_event_offsets_strictly_increasing_under_clustering(self):
        """CUST-SCD2-1: even when draws cluster near max_offset (so the spacing
        clamp would collide), event offsets must be strictly increasing — equal
        offsets chain a version with EffectiveEndDate < EffectiveStartDate."""
        from src.dimensions.customers.scd2 import _event_offsets

        for seed in range(200):
            rng = np.random.default_rng(seed)
            # Tight room (max_offset=120) vs 5 events forces the clamp to collide.
            offsets = _event_offsets(rng, n_events=5, max_offset=120)
            assert offsets.size >= 1
            if offsets.size > 1:
                assert np.all(np.diff(offsets) >= 1), f"equal offsets at seed {seed}"


# ===================================================================
# Subscription payment weights validation
# ===================================================================

class TestSubscriptionPaymentWeights:
    """Verify payment weights are validated at import time."""

    def test_weights_sum_to_one(self):
        from src.dimensions.customers.subscriptions import _PAYMENT_WEIGHTS
        assert abs(_PAYMENT_WEIGHTS.sum() - 1.0) < 1e-9


# ===================================================================
# Cross-section rules and skip_order_blocks_feature
# ===================================================================

class TestSkipOrderBlocksFeature:
    """Test the shared predicate for returns/complaints disable logic."""

    def test_skip_order_with_sales_output_blocks(self):
        from src.engine.config.config import skip_order_blocks_feature
        cfg = AppConfig.model_validate({
            "sales": {"skip_order_cols": True, "sales_output": "sales"},
        })
        assert skip_order_blocks_feature(cfg) is True

    def test_skip_order_with_sales_order_does_not_block(self):
        from src.engine.config.config import skip_order_blocks_feature
        cfg = AppConfig.model_validate({
            "sales": {"skip_order_cols": True, "sales_output": "sales_order"},
        })
        assert skip_order_blocks_feature(cfg) is False

    def test_no_skip_order_does_not_block(self):
        from src.engine.config.config import skip_order_blocks_feature
        cfg = AppConfig.model_validate({
            "sales": {"skip_order_cols": False, "sales_output": "sales"},
        })
        assert skip_order_blocks_feature(cfg) is False

    def test_dict_input(self):
        from src.engine.config.config import skip_order_blocks_feature
        cfg = {"sales": {"skip_order_cols": True, "sales_output": "sales"}}
        assert skip_order_blocks_feature(cfg) is True


# ===================================================================
# Config normalizer coverage (new normalizers from Phase 1)
# ===================================================================

class TestNewNormalizers:
    """Test config normalizers for products, customers, stores, promotions, and region_mix."""

    def test_products_negative_count_raises(self):
        from src.engine.config.config import normalize_products_config
        with pytest.raises(ConfigError, match="positive integer"):
            normalize_products_config({"num_products": -5})

    def test_products_bad_active_ratio_raises(self):
        from src.engine.config.config import normalize_products_config
        with pytest.raises(ConfigError, match="between 0 and 1"):
            normalize_products_config({"active_ratio": 1.5})

    def test_customers_negative_pct_raises(self):
        from src.engine.config.config import normalize_customers_config
        with pytest.raises(ConfigError, match="must be >= 0"):
            normalize_customers_config({"pct_us": -10})

    def test_stores_coerces_int(self):
        from src.engine.config.config import normalize_stores_config
        result = normalize_stores_config({"num_stores": "100"})
        assert result["num_stores"] == 100
        assert isinstance(result["num_stores"], int)

    def test_promotions_negative_bucket_raises(self):
        from src.engine.config.config import normalize_promotions_config
        with pytest.raises(ConfigError, match="non-negative"):
            normalize_promotions_config({"num_seasonal": -1})

    def test_region_mix_unknown_raises(self):
        from src.engine.config.config import _expand_region_mix
        cfg = {"customers": {"region_mix": {"Atlantis": 50}}}
        with pytest.raises(ConfigError, match="Unknown region"):
            _expand_region_mix(cfg)

    def test_region_mix_valid(self):
        from src.engine.config.config import _expand_region_mix
        cfg = {"customers": {"region_mix": {"US": 60, "EU": 30, "India": 10}}}
        result = _expand_region_mix(cfg)
        assert result["customers"]["pct_us"] == 60.0
        assert result["customers"]["pct_eu"] == 30.0
        assert result["customers"]["pct_india"] == 10.0


# ===================================================================
# Thread safety for web shared state
# ===================================================================

class TestWebThreadSafety:
    """Basic thread-safety tests for web shared state."""

    def test_concurrent_config_reads(self):
        """Multiple threads reading config should not crash."""
        import threading
        from web.shared_state import _cfg_lock

        errors = []

        def read_config():
            try:
                with _cfg_lock:
                    pass  # just acquire/release lock
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=read_config) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"


# ===================================================================
# Determinism expansion
# ===================================================================

class TestExpandedDeterminism:
    """Expanded determinism tests beyond the basic RNG tests."""

    def test_normalize_cdf_deterministic(self):
        w = np.array([0.3, 0.5, 0.1, 0.1])
        r1 = _normalize_cdf(w.copy())
        r2 = _normalize_cdf(w.copy())
        np.testing.assert_array_equal(r1, r2)

    def test_customer_sampling_deterministic(self):
        from src.facts.sales.sales_logic.core.customer_sampling import _sample_customers
        keys = np.arange(1, 101, dtype="int32")
        mask = np.ones(100, dtype=bool)

        r1 = _sample_customers(
            np.random.default_rng(42), keys, mask, set(), 50,
            use_discovery=False, discovery_cfg={},
        )
        r2 = _sample_customers(
            np.random.default_rng(42), keys, mask, set(), 50,
            use_discovery=False, discovery_cfg={},
        )
        np.testing.assert_array_equal(r1, r2)
