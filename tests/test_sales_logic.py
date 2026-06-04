"""Comprehensive tests for sales logic, budget, and inventory modules.

Covers:
  - Order generation (orders.py)
  - Pricing (pricing.py)
  - Promotions (promotions.py)
  - Delivery/dates (delivery.py)
  - Allocation (allocation.py)
  - Customer sampling (customer_sampling.py)
  - Budget micro-aggregation, accumulator, engine
  - Inventory micro-aggregation, accumulator, engine
  - Returns builder
  - Worker schemas
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from src.exceptions import SalesError
from src.facts.sales.sales_logic.core.orders import (
    _reset_month_demand,
    _safe_normalized_prob,
    build_month_demand,
    build_orders,
)
from src.facts.sales.sales_logic.core.pricing import compute_prices
from src.facts.sales.sales_logic.core.promotions import apply_promotions
from src.facts.sales.sales_logic.core.delivery import (
    _yyyymmdd_from_days,
    compute_dates,
    fmt,
)
from src.facts.sales.sales_logic.core.allocation import (
    _safe_prob,
    _sched_mode_and_values,
    build_rows_per_month,
    macro_month_weights,
)
from src.facts.sales.sales_logic.core.customer_sampling import (
    _build_seen_mask,
    _eligible_customer_mask_for_month,
    _make_seen_lookup,
    _normalize_end_month,
    _normalize_weights,
    _participation_distinct_target,
    _sample_customers,
    _update_seen_lookup,
)
from src.facts.sales.sales_logic.globals import State
from src.facts.budget.accumulator import BudgetAccumulator
from src.engine.config.config_schema import AppConfig
from src.facts.budget.engine import (
    BudgetConfig,
    _jitter_pct,
    compute_budget,
    load_budget_config,
)
from src.facts.budget.micro_agg import (
    _decode_flat_key,
    _extract_columns_from_table,
    micro_aggregate_sales,
)
from src.facts.inventory.accumulator import InventoryAccumulator
from src.facts.inventory.engine import (
    InventoryConfig,
    compute_inventory_snapshots,
    load_inventory_config,
)
from src.facts.inventory.micro_agg import micro_aggregate_inventory
from src.facts.inventory.runner import _recompute_abc_from_demand
from src.facts.sales.sales_worker.returns_builder import (
    RETURNS_SCHEMA,
    ReturnsConfig,
    _empty_returns_table,
    build_sales_returns_from_detail,
)
from src.facts.sales.sales_worker.schemas import (
    build_worker_schemas,
    schema_dict_cols,
)


# ===================================================================
# Helpers
# ===================================================================

def _rng(seed: int = 42) -> np.random.Generator:
    return np.random.default_rng(seed)


def _date_pool(start: str = "2023-01-01", days: int = 31) -> np.ndarray:
    base = np.datetime64(start)
    return np.array([base + np.timedelta64(i, "D") for i in range(days)],
                    dtype="datetime64[D]")


# ===================================================================
# 1. Order generation
# ===================================================================

class TestBuildMonthDemand:
    def test_returns_12_elements(self):
        demand = build_month_demand()
        assert demand.shape == (12,)

    def test_dtype_float64(self):
        demand = build_month_demand()
        assert demand.dtype == np.float64

    def test_q4_boost_increases_oct_dec(self):
        no_boost = build_month_demand(q4_boost=0.0)
        with_boost = build_month_demand(q4_boost=0.60)
        # Oct (9), Nov (10), Dec (11) should be higher with boost
        for m in [9, 10, 11]:
            assert with_boost[m] >= no_boost[m]

    def test_zero_amplitude_flat(self):
        demand = build_month_demand(amplitude=0.0, q4_boost=0.0)
        np.testing.assert_allclose(demand, 1.0)


class TestSafeNormalizedProb:
    def test_none_returns_none(self):
        assert _safe_normalized_prob(None) is None

    def test_empty_returns_none(self):
        assert _safe_normalized_prob([]) is None

    def test_all_zeros_returns_none(self):
        assert _safe_normalized_prob([0, 0, 0]) is None

    def test_normalizes_correctly(self):
        result = _safe_normalized_prob([1.0, 3.0])
        np.testing.assert_allclose(result, [0.25, 0.75])

    def test_nan_treated_as_zero(self):
        result = _safe_normalized_prob([float("nan"), 4.0])
        np.testing.assert_allclose(result, [0.0, 1.0])

    def test_sums_to_one(self):
        result = _safe_normalized_prob([1, 2, 3, 4, 5])
        assert abs(result.sum() - 1.0) < 1e-12


class TestBuildOrders:
    def setup_method(self):
        State.reset()
        State.max_lines_per_order = 5
        _reset_month_demand()

    def teardown_method(self):
        State.reset()
        _reset_month_demand()

    def test_zero_n_returns_empty(self):
        rng = _rng()
        result = build_orders(
            rng, 0, False, _date_pool(), None,
            np.array([1], dtype=np.int32), 31, 1,
            order_id_start=0,
        )
        assert result["customer_keys"].shape == (0,)
        assert result["order_dates"].shape == (0,)

    def test_output_length_matches_n(self):
        rng = _rng()
        n = 50
        customers = np.arange(1, 21, dtype=np.int32)
        dp = _date_pool()
        result = build_orders(
            rng, n, False, dp, None,
            customers, len(dp), len(customers),
            order_id_start=0,
        )
        assert result["customer_keys"].shape == (n,)
        assert result["order_dates"].shape == (n,)

    def test_order_ids_unique_when_not_skip(self):
        rng = _rng()
        n = 30
        customers = np.arange(1, 16, dtype=np.int32)
        dp = _date_pool()
        result = build_orders(
            rng, n, False, dp, None,
            customers, len(dp), len(customers),
            order_id_start=0,
        )
        order_ids = result["order_ids_int"]
        assert len(np.unique(order_ids)) <= len(order_ids)
        # All positive (1-based)
        assert order_ids.min() >= 1

    def test_order_ids_dtype_int32(self):
        rng = _rng()
        customers = np.arange(1, 6, dtype=np.int32)
        dp = _date_pool()
        result = build_orders(
            rng, 10, False, dp, None,
            customers, len(dp), len(customers),
            order_id_start=0,
        )
        assert result["order_ids_int"].dtype == np.int32

    def test_overflow_raises(self):
        rng = _rng()
        customers = np.arange(1, 6, dtype=np.int32)
        dp = _date_pool()
        with pytest.raises(OverflowError, match="overflow int32"):
            build_orders(
                rng, 10, False, dp, None,
                customers, len(dp), len(customers),
                order_id_start=np.iinfo(np.int32).max - 1,
            )

    def test_skip_cols_omits_order_fields(self):
        rng = _rng()
        customers = np.arange(1, 11, dtype=np.int32)
        dp = _date_pool()
        result = build_orders(
            rng, 20, True, dp, None,
            customers, len(dp), len(customers),
            order_id_start=0,
        )
        assert "order_ids_int" not in result
        assert "line_num" not in result

    def test_empty_date_pool_raises(self):
        rng = _rng()
        with pytest.raises(RuntimeError, match="date_pool is empty"):
            build_orders(
                rng, 10, False,
                np.array([], dtype="datetime64[D]"), None,
                np.array([1], dtype=np.int32), 0, 1,
                order_id_start=0,
            )

    def test_empty_customers_raises(self):
        rng = _rng()
        dp = _date_pool()
        with pytest.raises(RuntimeError, match="customers array is empty"):
            build_orders(
                rng, 10, False, dp, None,
                np.array([], dtype=np.int32), len(dp), 0,
                order_id_start=0,
            )

    def test_missing_order_id_start_raises(self):
        rng = _rng()
        dp = _date_pool()
        customers = np.array([1, 2, 3], dtype=np.int32)
        with pytest.raises(RuntimeError, match="order_id_start is required"):
            build_orders(
                rng, 10, False, dp, None,
                customers, len(dp), len(customers),
            )

    def test_line_numbers_reset_per_order(self):
        rng = _rng()
        State.max_lines_per_order = 3
        customers = np.arange(1, 11, dtype=np.int32)
        dp = _date_pool()
        result = build_orders(
            rng, 20, False, dp, None,
            customers, len(dp), len(customers),
            order_id_start=100,
        )
        line_nums = result["line_num"]
        order_ids = result["order_ids_int"]
        # For each unique order, line numbers should start at 1
        for oid in np.unique(order_ids):
            lines = line_nums[order_ids == oid]
            assert lines[0] == 1
            np.testing.assert_array_equal(lines, np.arange(1, len(lines) + 1))


# ===================================================================
# 2. Pricing
# ===================================================================

class TestComputePrices:
    def test_zero_n_returns_empty(self):
        result = compute_prices(_rng(), 0, [], [])
        assert result["final_unit_price"].shape == (0,)

    def test_positive_prices(self):
        up = np.array([10.0, 20.0, 30.0])
        uc = np.array([5.0, 10.0, 15.0])
        result = compute_prices(_rng(), 3, up, uc)
        assert np.all(result["final_unit_price"] >= 0)
        assert np.all(result["final_unit_cost"] >= 0)

    def test_cost_not_exceeding_price(self):
        up = np.array([10.0, 5.0, 20.0])
        uc = np.array([15.0, 3.0, 25.0])  # some costs > prices
        result = compute_prices(_rng(), 3, up, uc)
        assert np.all(result["final_unit_cost"] <= result["final_unit_price"])

    def test_zero_discount(self):
        up = np.array([10.0, 20.0])
        uc = np.array([5.0, 10.0])
        result = compute_prices(_rng(), 2, up, uc)
        np.testing.assert_array_equal(result["discount_amt"], [0.0, 0.0])

    def test_net_price_equals_unit_price(self):
        up = np.array([10.0, 20.0])
        uc = np.array([5.0, 10.0])
        result = compute_prices(_rng(), 2, up, uc)
        np.testing.assert_array_equal(result["final_net_price"], result["final_unit_price"])

    def test_nan_prices_treated_as_zero(self):
        up = np.array([float("nan"), 20.0])
        uc = np.array([5.0, float("nan")])
        result = compute_prices(_rng(), 2, up, uc)
        assert result["final_unit_price"][0] == 0.0
        assert result["final_unit_cost"][1] == 0.0

    def test_negative_prices_clipped_to_zero(self):
        up = np.array([-10.0, 20.0])
        uc = np.array([-5.0, 10.0])
        result = compute_prices(_rng(), 2, up, uc)
        assert result["final_unit_price"][0] == 0.0
        assert result["final_unit_cost"][0] == 0.0

    def test_single_element(self):
        result = compute_prices(_rng(), 1, [99.99], [49.99])
        assert result["final_unit_price"].shape == (1,)
        assert result["final_unit_price"][0] == pytest.approx(99.99)


# ===================================================================
# 3. Promotions
# ===================================================================

class TestApplyPromotions:
    def test_zero_n(self):
        result = apply_promotions(_rng(), 0, [], None, None, None)
        assert result.shape == (0,)

    def test_no_promos_returns_no_discount_key(self):
        dates = _date_pool(days=10)
        result = apply_promotions(_rng(), 10, dates, None, None, None, no_discount_key=1)
        np.testing.assert_array_equal(result, np.ones(10, dtype=np.int32))

    def test_empty_promo_keys_returns_no_discount(self):
        dates = _date_pool(days=5)
        result = apply_promotions(
            _rng(), 5, dates,
            np.array([], dtype=np.int32),
            np.array([], dtype="datetime64[D]"),
            np.array([], dtype="datetime64[D]"),
        )
        np.testing.assert_array_equal(result, np.ones(5, dtype=np.int32))

    def test_active_promo_assigned(self):
        dates = np.array(["2023-01-15"] * 20, dtype="datetime64[D]")
        promo_keys = np.array([1, 10, 20], dtype=np.int32)
        promo_start = np.array(["2023-01-01", "2023-01-10", "2023-02-01"], dtype="datetime64[D]")
        promo_end = np.array(["2023-01-31", "2023-01-20", "2023-02-28"], dtype="datetime64[D]")

        result = apply_promotions(
            _rng(), 20, dates, promo_keys, promo_start, promo_end,
            no_discount_key=1,
        )
        # Promo 10 and promo 1 (no_discount_key) are both active;
        # since no_discount_key is excluded from valid, only promo 10 should appear
        # where there's an active promo
        assert np.any(result == 10)

    def test_no_active_promo_gets_default(self):
        dates = np.array(["2023-06-15"] * 10, dtype="datetime64[D]")
        promo_keys = np.array([1, 10], dtype=np.int32)
        promo_start = np.array(["2023-01-01", "2023-01-01"], dtype="datetime64[D]")
        promo_end = np.array(["2023-01-31", "2023-01-31"], dtype="datetime64[D]")

        result = apply_promotions(
            _rng(), 10, dates, promo_keys, promo_start, promo_end,
            no_discount_key=1,
        )
        np.testing.assert_array_equal(result, np.ones(10, dtype=np.int32))

    def test_length_mismatch_raises(self):
        dates = _date_pool(days=5)
        promo_keys = np.array([1, 10], dtype=np.int32)
        promo_start = np.array(["2023-01-01"], dtype="datetime64[D]")  # wrong length
        promo_end = np.array(["2023-01-31", "2023-01-31"], dtype="datetime64[D]")

        with pytest.raises(SalesError, match="must align"):
            apply_promotions(_rng(), 5, dates, promo_keys, promo_start, promo_end)


# ===================================================================
# 4. Delivery
# ===================================================================

class TestFmt:
    def test_single_date(self):
        assert fmt(np.datetime64("2023-06-15")) == "20230615"

    def test_array(self):
        dates = np.array(["2023-01-01", "2023-12-31"], dtype="datetime64[D]")
        np.testing.assert_array_equal(fmt(dates), ["20230101", "20231231"])


class TestYYYYMMDDFromDays:
    def test_known_date(self):
        d = np.array([np.datetime64("2023-06-15").astype("int64")], dtype=np.int64)
        result = _yyyymmdd_from_days(d)
        assert result[0] == 20230615


class TestComputeDates:
    def test_zero_n_returns_empty(self):
        result = compute_dates(_rng(), 0, [], None, [])
        assert result["due_date"].shape == (0,)
        assert result["delivery_date"].shape == (0,)
        assert result["delivery_status"].shape == (0,)
        assert result["is_order_delayed"].shape == (0,)

    def test_output_lengths_match(self):
        n = 50
        rng = _rng()
        pk = np.arange(1, n + 1, dtype=np.int32)
        oids = np.arange(1, n + 1, dtype=np.int64)
        dates = np.array(["2023-03-15"] * n, dtype="datetime64[D]")
        result = compute_dates(rng, n, pk, oids, dates)
        for key in ("due_date", "delivery_date", "delivery_status", "is_order_delayed"):
            assert result[key].shape == (n,), f"{key} wrong shape"

    def test_due_date_after_order_date(self):
        n = 100
        rng = _rng()
        pk = np.ones(n, dtype=np.int32)
        oids = np.arange(1, n + 1, dtype=np.int64)
        dates = np.array(["2023-06-01"] * n, dtype="datetime64[D]")
        result = compute_dates(rng, n, pk, oids, dates)
        # Due date is 3-7 days after order
        offsets = (result["due_date"] - dates).astype(int)
        assert np.all(offsets >= 3)
        assert np.all(offsets <= 7)

    def test_delivery_status_values(self):
        n = 200
        rng = _rng()
        pk = np.arange(1, n + 1, dtype=np.int32)
        oids = np.arange(1, n + 1, dtype=np.int64)
        dates = np.array(["2023-06-01"] * n, dtype="datetime64[D]")
        result = compute_dates(rng, n, pk, oids, dates)
        valid_statuses = {"On Time", "Early Delivery", "Delayed"}
        actual_statuses = set(result["delivery_status"])
        assert actual_statuses.issubset(valid_statuses)

    def test_no_order_ids_fallback(self):
        """order_ids_int=None triggers row-level hash fallback."""
        n = 30
        rng = _rng()
        pk = np.arange(1, n + 1, dtype=np.int32)
        dates = np.array(["2023-06-01"] * n, dtype="datetime64[D]")
        result = compute_dates(rng, n, pk, None, dates)
        assert result["due_date"].shape == (n,)

    def test_is_order_delayed_dtype(self):
        n = 20
        rng = _rng()
        pk = np.ones(n, dtype=np.int32)
        oids = np.arange(1, n + 1, dtype=np.int64)
        dates = np.array(["2023-03-15"] * n, dtype="datetime64[D]")
        result = compute_dates(rng, n, pk, oids, dates)
        assert result["is_order_delayed"].dtype == bool


# ===================================================================
# 5. Allocation
# ===================================================================

class TestSafeProb:
    def test_normalizes(self):
        result = _safe_prob(np.array([2.0, 3.0]))
        assert abs(result.sum() - 1.0) < 1e-12

    def test_all_zeros_uniform(self):
        result = _safe_prob(np.array([0.0, 0.0, 0.0]))
        np.testing.assert_allclose(result, [1/3, 1/3, 1/3])

    def test_negative_clipped(self):
        result = _safe_prob(np.array([-1.0, 2.0]))
        assert result[0] == 0.0
        assert abs(result.sum() - 1.0) < 1e-12

    def test_nan_treated_as_zero(self):
        result = _safe_prob(np.array([float("nan"), 3.0]))
        assert abs(result.sum() - 1.0) < 1e-12


class TestSchedModeAndValues:
    def test_valid_repeat(self):
        mode, vals = _sched_mode_and_values(
            {"mode": "repeat", "values": [1.0, 2.0]}, "test"
        )
        assert mode == "repeat"
        assert vals == [1.0, 2.0]

    def test_valid_once(self):
        mode, vals = _sched_mode_and_values(
            {"mode": "once", "values": [0.5]}, "test"
        )
        assert mode == "once"

    def test_invalid_mode_raises(self):
        with pytest.raises(SalesError, match="mode"):
            _sched_mode_and_values({"mode": "bad", "values": [1]}, "test")

    def test_empty_values_raises(self):
        with pytest.raises(SalesError, match="non-empty"):
            _sched_mode_and_values({"mode": "repeat", "values": []}, "test")

    def test_non_dict_raises(self):
        with pytest.raises(SalesError, match="mapping"):
            _sched_mode_and_values("not a dict", "test")


class TestMacroMonthWeights:
    def test_zero_months(self):
        result = macro_month_weights(_rng(), 0, {})
        assert result.shape == (0,)

    def test_sums_to_one(self):
        result = macro_month_weights(_rng(), 24, {"base_level": 1.0})
        assert abs(result.sum() - 1.0) < 1e-9

    def test_all_positive(self):
        result = macro_month_weights(_rng(), 12, {"base_level": 1.0})
        assert np.all(result > 0)

    def test_seasonality_creates_variation(self):
        flat = macro_month_weights(_rng(), 12, {"base_level": 1.0, "seasonality_amplitude": 0.0})
        seasonal = macro_month_weights(_rng(), 12, {"base_level": 1.0, "seasonality_amplitude": 0.3})
        # Seasonal should have more variation
        assert seasonal.std() > flat.std()

    def test_both_schedules_raises(self):
        with pytest.raises(SalesError, match="only one"):
            macro_month_weights(_rng(), 12, {
                "yoy_growth_schedule": {"mode": "repeat", "values": [0.05]},
                "year_level_factors": {"mode": "repeat", "values": [1.0]},
            })


class TestBuildRowsPerMonth:
    def test_zero_months(self):
        result = build_rows_per_month(
            rng=_rng(), total_rows=100,
            eligible_counts=np.array([], dtype=np.int64),
            macro_cfg=None,
        )
        assert result.shape == (0,)

    def test_zero_rows(self):
        result = build_rows_per_month(
            rng=_rng(), total_rows=0,
            eligible_counts=np.array([10, 20, 30], dtype=np.int64),
            macro_cfg=None,
        )
        np.testing.assert_array_equal(result, [0, 0, 0])

    def test_sum_equals_total(self):
        eligible = np.array([100, 200, 150, 300], dtype=np.int64)
        result = build_rows_per_month(
            rng=_rng(), total_rows=1000,
            eligible_counts=eligible,
            macro_cfg=None,
        )
        assert result.sum() == 1000

    def test_no_rows_to_empty_months(self):
        eligible = np.array([0, 100, 0, 200], dtype=np.int64)
        result = build_rows_per_month(
            rng=_rng(), total_rows=500,
            eligible_counts=eligible,
            macro_cfg=None,
        )
        assert result[0] == 0
        assert result[2] == 0
        assert result.sum() == 500

    def test_macro_cfg_sum_equals_total(self):
        eligible = np.array([100, 200, 150], dtype=np.int64)
        result = build_rows_per_month(
            rng=_rng(), total_rows=500,
            eligible_counts=eligible,
            macro_cfg={"base_level": 1.0},
        )
        assert result.sum() == 500

    def test_all_zero_eligible_returns_zeros(self):
        eligible = np.array([0, 0, 0], dtype=np.int64)
        result = build_rows_per_month(
            rng=_rng(), total_rows=100,
            eligible_counts=eligible,
            macro_cfg=None,
        )
        np.testing.assert_array_equal(result, [0, 0, 0])

    def test_single_month(self):
        result = build_rows_per_month(
            rng=_rng(), total_rows=42,
            eligible_counts=np.array([100], dtype=np.int64),
            macro_cfg=None,
        )
        assert result[0] == 42


# ===================================================================
# 6. Customer sampling
# ===================================================================

class TestNormalizeEndMonth:
    def test_none_returns_neg_ones(self):
        result = _normalize_end_month(None, 5)
        np.testing.assert_array_equal(result, [-1, -1, -1, -1, -1])

    def test_integer_array(self):
        result = _normalize_end_month(np.array([3, -1, 5]), 3)
        np.testing.assert_array_equal(result, [3, -1, 5])

    def test_float_nan_becomes_neg_one(self):
        result = _normalize_end_month(np.array([1.0, np.nan, 3.0]), 3)
        np.testing.assert_array_equal(result, [1, -1, 3])


class TestEligibleCustomerMask:
    def test_basic_eligibility(self):
        active = np.array([1, 1, 0, 1], dtype=np.int64)
        start = np.array([0, 2, 0, 1], dtype=np.int64)
        end = np.array([-1, -1, -1, 5], dtype=np.int64)

        mask = _eligible_customer_mask_for_month(3, active, start, end)
        # Customer 0: active, start<=3, no end -> True
        # Customer 1: active, start=2<=3, no end -> True
        # Customer 2: inactive -> False
        # Customer 3: active, start=1<=3, end=5>=3 -> True
        np.testing.assert_array_equal(mask, [True, True, False, True])

    def test_end_month_before_current(self):
        active = np.array([1], dtype=np.int64)
        start = np.array([0], dtype=np.int64)
        end = np.array([2], dtype=np.int64)

        mask = _eligible_customer_mask_for_month(5, active, start, end)
        np.testing.assert_array_equal(mask, [False])

    def test_start_after_current(self):
        active = np.array([1], dtype=np.int64)
        start = np.array([10], dtype=np.int64)
        end = np.array([-1], dtype=np.int64)

        mask = _eligible_customer_mask_for_month(5, active, start, end)
        np.testing.assert_array_equal(mask, [False])


class TestParticipationDistinctTarget:
    def test_zero_eligible(self):
        assert _participation_distinct_target(_rng(), 0, 0, 10, {}) == 0

    def test_zero_orders(self):
        assert _participation_distinct_target(_rng(), 0, 100, 0, {}) == 0

    def test_basic_ratio(self):
        k = _participation_distinct_target(
            _rng(), 0, 100, 200,
            {"base_distinct_ratio": 0.5},
        )
        assert 1 <= k <= 100

    def test_capped_by_eligible(self):
        k = _participation_distinct_target(
            _rng(), 0, 10, 200,
            {"base_distinct_ratio": 1.0},
        )
        assert k <= 10

    def test_capped_by_n_orders(self):
        k = _participation_distinct_target(
            _rng(), 0, 100, 5,
            {"base_distinct_ratio": 1.0},
        )
        assert k <= 5


class TestNormalizeWeights:
    def test_normalizes(self):
        result = _normalize_weights(np.array([1.0, 3.0]))
        np.testing.assert_allclose(result, [0.25, 0.75])

    def test_all_zero_returns_uniform(self):
        result = _normalize_weights(np.array([0.0, 0.0]))
        np.testing.assert_allclose(result, [0.5, 0.5])

    def test_nan_cleaned(self):
        result = _normalize_weights(np.array([float("nan"), 2.0]))
        assert result is not None
        assert abs(result.sum() - 1.0) < 1e-12


class TestBuildSeenMask:
    def test_empty_seen_all_false(self):
        keys = np.array([1, 2, 3], dtype=np.int32)
        mask = _build_seen_mask(keys, set())
        np.testing.assert_array_equal(mask, [False, False, False])

    def test_some_seen(self):
        keys = np.array([1, 2, 3, 4], dtype=np.int32)
        mask = _build_seen_mask(keys, {2, 4})
        np.testing.assert_array_equal(mask, [False, True, False, True])

    def test_numpy_lookup(self):
        keys = np.array([1, 2, 3], dtype=np.int32)
        lookup = np.array([False, True, False, True], dtype=bool)  # index 1=True, 3=True
        mask = _build_seen_mask(keys, lookup)
        np.testing.assert_array_equal(mask, [True, False, True])


class TestMakeUpdateSeenLookup:
    def test_create_and_update(self):
        keys = np.array([1, 2, 5], dtype=np.int32)
        lookup = _make_seen_lookup(keys)
        assert lookup.shape == (6,)  # max key is 5
        assert not lookup.any()

        _update_seen_lookup(lookup, np.array([2, 5], dtype=np.int32))
        assert lookup[2] is np.True_
        assert lookup[5] is np.True_
        assert not lookup[1]


class TestSampleCustomers:
    def test_returns_correct_length(self):
        keys = np.arange(1, 11, dtype=np.int32)
        eligible = np.ones(10, dtype=bool)
        result = _sample_customers(
            _rng(), keys, eligible, set(), 20,
            use_discovery=False, discovery_cfg={},
        )
        assert result.shape == (20,)

    def test_zero_n(self):
        keys = np.arange(1, 6, dtype=np.int32)
        eligible = np.ones(5, dtype=bool)
        result = _sample_customers(
            _rng(), keys, eligible, set(), 0,
            use_discovery=False, discovery_cfg={},
        )
        assert result.shape == (0,)

    def test_no_eligible_returns_empty(self):
        keys = np.arange(1, 6, dtype=np.int32)
        eligible = np.zeros(5, dtype=bool)
        result = _sample_customers(
            _rng(), keys, eligible, set(), 10,
            use_discovery=False, discovery_cfg={},
        )
        assert result.shape == (0,)

    def test_discovery_includes_new_customers(self):
        keys = np.arange(1, 21, dtype=np.int32)
        eligible = np.ones(20, dtype=bool)
        seen = {1, 2, 3}
        result = _sample_customers(
            _rng(), keys, eligible, seen, 50,
            use_discovery=True,
            discovery_cfg={"_target_new_customers": 5, "stochastic_discovery": False},
        )
        # Should include some customers not in seen
        new_customers = set(result) - seen
        assert len(new_customers) > 0

    def test_target_distinct_limits_unique(self):
        keys = np.arange(1, 101, dtype=np.int32)
        eligible = np.ones(100, dtype=bool)
        result = _sample_customers(
            _rng(), keys, eligible, set(), 50,
            use_discovery=False, discovery_cfg={},
            target_distinct=5,
        )
        assert len(np.unique(result)) <= 5


# ===================================================================
# 7. Budget micro-aggregation
# ===================================================================

def _make_budget_arrow_table(n: int = 100) -> pa.Table:
    """Create a minimal sales Arrow table for budget micro-agg testing."""
    rng = _rng()
    return pa.table({
        "StoreKey": pa.array(rng.integers(0, 3, size=n, dtype=np.int32), type=pa.int32()),
        "ProductKey": pa.array(rng.integers(0, 5, size=n, dtype=np.int32), type=pa.int32()),
        "SalesChannelKey": pa.array(rng.integers(1, 3, size=n, dtype=np.int32), type=pa.int32()),
        "OrderDate": pa.array(
            np.array(["2023-03-15"] * n, dtype="datetime64[D]"), type=pa.date32()
        ),
        "Quantity": pa.array(rng.integers(1, 10, size=n, dtype=np.int32), type=pa.int32()),
        "NetPrice": pa.array(rng.uniform(5.0, 50.0, size=n), type=pa.float64()),
    })


class TestBudgetMicroAgg:
    def test_basic_aggregation(self):
        table = _make_budget_arrow_table(100)
        store_to_country = np.array([0, 1, 0], dtype=np.int32)
        product_to_cat = np.array([0, 0, 1, 1, 2], dtype=np.int32)

        result = micro_aggregate_sales(
            table,
            store_to_country=store_to_country,
            product_to_cat=product_to_cat,
        )
        assert "sales_amount" in result
        assert "sales_qty" in result
        assert "country_id" in result
        assert "category_id" in result
        assert result["sales_amount"].sum() > 0

    def test_total_amount_preserved(self):
        table = _make_budget_arrow_table(50)
        store_to_country = np.array([0, 0, 0], dtype=np.int32)
        product_to_cat = np.array([0, 0, 0, 0, 0], dtype=np.int32)

        qty = table.column("Quantity").to_numpy(zero_copy_only=False).astype(np.float64)
        price = table.column("NetPrice").to_numpy(zero_copy_only=False).astype(np.float64)
        expected_total = (qty * price).sum()

        result = micro_aggregate_sales(
            table,
            store_to_country=store_to_country,
            product_to_cat=product_to_cat,
        )
        assert abs(result["sales_amount"].sum() - expected_total) < 1e-6

    def test_total_qty_preserved(self):
        table = _make_budget_arrow_table(50)
        store_to_country = np.array([0, 0, 0], dtype=np.int32)
        product_to_cat = np.array([0, 0, 0, 0, 0], dtype=np.int32)

        expected_qty = table.column("Quantity").to_numpy(zero_copy_only=False).astype(np.float64).sum()

        result = micro_aggregate_sales(
            table,
            store_to_country=store_to_country,
            product_to_cat=product_to_cat,
        )
        assert abs(result["sales_qty"].sum() - expected_qty) < 1e-6


class TestDecodeFlatKey:
    def test_roundtrip(self):
        stride_ch = 2
        stride_m = 12 * stride_ch
        stride_y = 1 * stride_m
        stride_cat = 3 * stride_y

        # Encode a known cell: country=1, cat=2, year_idx=0, month_idx=5, ch=1
        flat = (
            np.int64(1) * stride_cat
            + np.int64(2) * stride_y
            + np.int64(0) * stride_m
            + np.int64(5) * stride_ch
            + np.int64(1)
        )
        decoded = _decode_flat_key(
            np.array([flat], dtype=np.int64),
            stride_cat=stride_cat,
            stride_y=stride_y,
            stride_m=stride_m,
            stride_ch=stride_ch,
            min_year=2023,
            channel_uniq=np.array([1, 2], dtype=np.int32),
        )
        assert decoded["country_id"][0] == 1
        assert decoded["category_id"][0] == 2
        assert decoded["year"][0] == 2023
        assert decoded["month"][0] == 6  # month_idx=5 -> month=6
        assert decoded["channel_key"][0] == 2  # ch_idx=1 -> channel_uniq[1]=2


# ===================================================================
# 8. Inventory micro-aggregation
# ===================================================================

def _make_inventory_arrow_table(n: int = 100) -> pa.Table:
    rng = _rng()
    return pa.table({
        "ProductKey": pa.array(rng.integers(1, 5, size=n, dtype=np.int32), type=pa.int32()),
        "StoreKey": pa.array(rng.integers(1, 3, size=n, dtype=np.int32), type=pa.int32()),
        "OrderDate": pa.array(
            np.array(["2023-06-15"] * n, dtype="datetime64[D]"), type=pa.date32()
        ),
        "Quantity": pa.array(rng.integers(1, 10, size=n, dtype=np.int32), type=pa.int32()),
    })


class TestInventoryMicroAgg:
    def test_basic_aggregation(self):
        table = _make_inventory_arrow_table(100)
        result = micro_aggregate_inventory(table)
        assert result is not None
        assert "product_key" in result
        assert "location_key" in result
        assert "quantity_sold" in result
        assert result["grain"] in ("store", "warehouse")

    def test_empty_table_returns_none(self):
        table = pa.table({
            "ProductKey": pa.array([], type=pa.int32()),
            "StoreKey": pa.array([], type=pa.int32()),
            "OrderDate": pa.array([], type=pa.date32()),
            "Quantity": pa.array([], type=pa.int32()),
        })
        assert micro_aggregate_inventory(table) is None

    def test_missing_columns_returns_none(self):
        table = pa.table({
            "ProductKey": pa.array([1], type=pa.int32()),
            "StoreKey": pa.array([1], type=pa.int32()),
        })
        assert micro_aggregate_inventory(table) is None

    def test_total_qty_preserved(self):
        table = _make_inventory_arrow_table(80)
        expected = table.column("Quantity").to_numpy(zero_copy_only=False).astype(np.float64).sum()
        result = micro_aggregate_inventory(table)
        assert abs(result["quantity_sold"].sum() - expected) < 1e-6


# ===================================================================
# 9. Budget accumulator
# ===================================================================

class TestBudgetAccumulator:
    def test_empty_finalize(self):
        acc = BudgetAccumulator(
            country_labels=np.array(["US", "UK"]),
            category_labels=np.array(["Electronics", "Clothing"]),
        )
        df = acc.finalize_sales()
        assert df.empty
        assert "Country" in df.columns
        assert "SalesAmount" in df.columns

    def test_has_data_false_initially(self):
        acc = BudgetAccumulator(
            country_labels=np.array(["US"]),
            category_labels=np.array(["Electronics"]),
        )
        assert acc.has_data is False

    def test_add_and_finalize(self):
        acc = BudgetAccumulator(
            country_labels=np.array(["US", "UK"]),
            category_labels=np.array(["Electronics", "Clothing"]),
        )
        micro = {
            "country_id": np.array([0, 1], dtype=np.int32),
            "category_id": np.array([0, 1], dtype=np.int32),
            "year": np.array([2023, 2023], dtype=np.int16),
            "month": np.array([1, 2], dtype=np.int8),
            "channel_key": np.array([1, 1], dtype=np.int16),
            "sales_amount": np.array([1000.0, 2000.0], dtype=np.float64),
            "sales_qty": np.array([10.0, 20.0], dtype=np.float64),
        }
        acc.add_sales(micro)
        assert acc.has_data is True

        df = acc.finalize_sales()
        assert len(df) == 2
        assert df["Country"].iloc[0] == "US"
        assert df["Country"].iloc[1] == "UK"

    def test_add_none_ignored(self):
        acc = BudgetAccumulator(
            country_labels=np.array(["US"]),
            category_labels=np.array(["Electronics"]),
        )
        acc.add_sales(None)
        assert acc.has_data is False

    def test_add_empty_sales_amount_ignored(self):
        acc = BudgetAccumulator(
            country_labels=np.array(["US"]),
            category_labels=np.array(["Electronics"]),
        )
        acc.add_sales({"sales_amount": []})
        assert acc.has_data is False

    def test_multiple_parts_merged(self):
        acc = BudgetAccumulator(
            country_labels=np.array(["US"]),
            category_labels=np.array(["Electronics"]),
        )
        for i in range(3):
            micro = {
                "country_id": np.array([0], dtype=np.int32),
                "category_id": np.array([0], dtype=np.int32),
                "year": np.array([2023], dtype=np.int16),
                "month": np.array([1], dtype=np.int8),
                "channel_key": np.array([1], dtype=np.int16),
                "sales_amount": np.array([100.0], dtype=np.float64),
                "sales_qty": np.array([10.0], dtype=np.float64),
            }
            acc.add_sales(micro)

        df = acc.finalize_sales()
        # All 3 micros have same key, should be aggregated
        assert len(df) == 1
        assert df["SalesAmount"].iloc[0] == pytest.approx(300.0)

    def test_finalize_returns_none_when_empty(self):
        acc = BudgetAccumulator(
            country_labels=np.array(["US"]),
            category_labels=np.array(["Electronics"]),
        )
        assert acc.finalize_returns() is None


# ===================================================================
# 10. Inventory accumulator
# ===================================================================

class TestInventoryAccumulator:
    def test_empty_finalize(self):
        acc = InventoryAccumulator()
        df = acc.finalize()
        assert df.empty
        assert "ProductKey" in df.columns
        assert "QuantitySold" in df.columns

    def test_has_data_false_initially(self):
        acc = InventoryAccumulator()
        assert acc.has_data is False

    def test_add_and_finalize(self):
        acc = InventoryAccumulator()
        micro = {
            "product_key": np.array([1, 2], dtype=np.int32),
            "store_key": np.array([1, 1], dtype=np.int32),
            "year": np.array([2023, 2023], dtype=np.int16),
            "month": np.array([1, 2], dtype=np.int8),
            "quantity_sold": np.array([50, 30], dtype=np.int64),
        }
        acc.add(micro)
        assert acc.has_data is True

        df = acc.finalize()
        assert len(df) == 2
        assert df["ProductKey"].dtype == np.int32
        assert df["QuantitySold"].dtype == np.int32

    def test_add_none_ignored(self):
        acc = InventoryAccumulator()
        acc.add(None)
        assert acc.has_data is False

    def test_add_empty_qty_ignored(self):
        acc = InventoryAccumulator()
        acc.add({"quantity_sold": []})
        assert acc.has_data is False

    def test_multiple_parts_aggregated(self):
        acc = InventoryAccumulator()
        for _ in range(3):
            micro = {
                "product_key": np.array([1], dtype=np.int32),
                "store_key": np.array([1], dtype=np.int32),
                "year": np.array([2023], dtype=np.int16),
                "month": np.array([6], dtype=np.int8),
                "quantity_sold": np.array([10], dtype=np.int64),
            }
            acc.add(micro)

        df = acc.finalize()
        assert len(df) == 1
        assert df["QuantitySold"].iloc[0] == 30


# ===================================================================
# 11. Returns builder
# ===================================================================

class TestEmptyReturnsTable:
    def test_schema_matches(self):
        t = _empty_returns_table()
        assert t.schema == RETURNS_SCHEMA
        assert t.num_rows == 0


class TestReturnsConfig:
    def test_defaults(self):
        cfg = ReturnsConfig()
        assert cfg.enabled is False
        assert cfg.return_rate == 0.0


def _make_detail_table(n: int = 50) -> pa.Table:
    """Create a minimal SalesOrderDetail Arrow table for returns testing."""
    rng = _rng()
    return pa.table({
        "SalesOrderNumber": pa.array(np.arange(1, n + 1, dtype=np.int32), type=pa.int32()),
        "SalesOrderLineNumber": pa.array(np.ones(n, dtype=np.int32), type=pa.int32()),
        "DeliveryDate": pa.array(
            np.array(["2023-06-15"] * n, dtype="datetime64[D]"), type=pa.date32()
        ),
        "Quantity": pa.array(rng.integers(1, 10, size=n, dtype=np.int32), type=pa.int32()),
        "NetPrice": pa.array(rng.uniform(10.0, 100.0, size=n), type=pa.float64()),
        "IsOrderDelayed": pa.array(rng.integers(0, 2, size=n, dtype=np.int32), type=pa.int32()),
    })


class TestBuildSalesReturns:
    def test_disabled_returns_empty(self):
        detail = _make_detail_table()
        cfg = ReturnsConfig(enabled=False)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        assert result.num_rows == 0
        assert result.schema == RETURNS_SCHEMA

    def test_zero_rate_returns_empty(self):
        detail = _make_detail_table()
        cfg = ReturnsConfig(enabled=True, return_rate=0.0)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        assert result.num_rows == 0

    @staticmethod
    def _int64_detail_table(n: int, start: int) -> pa.Table:
        rng = _rng()
        so = np.arange(start, start + n, dtype=np.int64)
        return pa.table({
            "SalesOrderNumber": pa.array(so, type=pa.int64()),
            "SalesOrderLineNumber": pa.array(np.ones(n, dtype=np.int32), type=pa.int32()),
            "DeliveryDate": pa.array(
                np.array(["2023-06-15"] * n, dtype="datetime64[D]"), type=pa.date32()
            ),
            "Quantity": pa.array(rng.integers(1, 10, size=n, dtype=np.int32), type=pa.int32()),
            "NetPrice": pa.array(rng.uniform(10.0, 100.0, size=n), type=pa.float64()),
            "IsOrderDelayed": pa.array(np.zeros(n, dtype=np.int32), type=pa.int32()),
        })

    def test_int64_detail_preserves_dtype_and_value(self):
        """SCHEMA-1: returns mirror int64 SalesOrderNumber without truncation."""
        start = 2_147_483_647 + 1000  # beyond int32 range
        detail = self._int64_detail_table(50, start)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, min_lag_days=1, max_lag_days=30)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        assert result.schema.field("SalesOrderNumber").type == pa.int64()
        returned = set(result.column("SalesOrderNumber").to_pylist())
        assert returned and returned.issubset(set(range(start, start + 50)))
        assert min(returned) > 2_147_483_647  # not wrapped to int32

    def test_int64_detail_empty_keeps_int64_schema(self):
        """Empty result must still carry int64 SO# (cross-chunk concat consistency)."""
        detail = self._int64_detail_table(1, 5_000_000_000)
        result = build_sales_returns_from_detail(
            detail, chunk_seed=42, cfg=ReturnsConfig(enabled=False),
        )
        assert result.num_rows == 0
        assert result.schema.field("SalesOrderNumber").type == pa.int64()

    def test_full_rate_returns_rows(self):
        detail = _make_detail_table(100)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, min_lag_days=1, max_lag_days=30)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        assert result.num_rows > 0
        assert result.schema == RETURNS_SCHEMA

    def test_return_qty_positive(self):
        detail = _make_detail_table(100)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, min_lag_days=0, max_lag_days=30)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        ret_qty = result.column("ReturnQuantity").to_numpy(zero_copy_only=False)
        assert np.all(ret_qty >= 1)

    def test_return_date_after_delivery(self):
        detail = _make_detail_table(100)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, min_lag_days=1, max_lag_days=30)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        ret_dates = result.column("ReturnDate").to_numpy(zero_copy_only=False)
        delivery = np.datetime64("2023-06-15")
        # All return dates should be >= delivery + 1 day
        assert np.all(ret_dates >= delivery + np.timedelta64(1, "D"))

    def test_return_event_key_unique(self):
        detail = _make_detail_table(100)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, min_lag_days=0, max_lag_days=10)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        keys = result.column("ReturnEventKey").to_numpy(zero_copy_only=False)
        assert len(np.unique(keys)) == len(keys)

    def test_return_event_key_non_negative(self):
        detail = _make_detail_table(100)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, min_lag_days=0, max_lag_days=10)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        keys = result.column("ReturnEventKey").to_numpy(zero_copy_only=False)
        assert np.all(keys >= 0)

    def test_empty_detail_returns_empty(self):
        detail = pa.table({
            "SalesOrderNumber": pa.array([], type=pa.int32()),
            "SalesOrderLineNumber": pa.array([], type=pa.int32()),
            "DeliveryDate": pa.array([], type=pa.date32()),
            "Quantity": pa.array([], type=pa.int32()),
            "NetPrice": pa.array([], type=pa.float64()),
        })
        cfg = ReturnsConfig(enabled=True, return_rate=1.0)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        assert result.num_rows == 0

    def test_deterministic_with_same_seed(self):
        detail = _make_detail_table(50)
        cfg = ReturnsConfig(enabled=True, return_rate=0.5, min_lag_days=1, max_lag_days=30)
        r1 = build_sales_returns_from_detail(detail, chunk_seed=123, cfg=cfg)
        r2 = build_sales_returns_from_detail(detail, chunk_seed=123, cfg=cfg)
        assert r1.num_rows == r2.num_rows
        if r1.num_rows > 0:
            np.testing.assert_array_equal(
                r1.column("ReturnQuantity").to_numpy(zero_copy_only=False),
                r2.column("ReturnQuantity").to_numpy(zero_copy_only=False),
            )

    def test_missing_column_raises(self):
        detail = pa.table({
            "SalesOrderNumber": pa.array([1], type=pa.int32()),
            # Missing other required columns
        })
        cfg = ReturnsConfig(enabled=True, return_rate=1.0)
        with pytest.raises(RuntimeError, match="missing required column"):
            build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)

    def test_same_day_return_allowed(self):
        detail = _make_detail_table(100)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, min_lag_days=0, max_lag_days=0)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        ret_dates = result.column("ReturnDate").to_numpy(zero_copy_only=False)
        delivery = np.datetime64("2023-06-15")
        # All return dates should equal delivery date (lag 0)
        if result.num_rows > 0:
            assert np.all(ret_dates == delivery)


# ===================================================================
# 12. Schemas
# ===================================================================

class TestSchemaDictCols:
    def test_finds_string_columns(self):
        schema = pa.schema([
            pa.field("Name", pa.string()),
            pa.field("Age", pa.int32()),
            pa.field("Status", pa.string()),
        ])
        result = schema_dict_cols(schema)
        assert result == ["Name", "Status"]

    def test_excludes_specified(self):
        schema = pa.schema([
            pa.field("Name", pa.string()),
            pa.field("Status", pa.string()),
        ])
        result = schema_dict_cols(schema, exclude={"Name"})
        assert result == ["Status"]

    def test_no_string_columns(self):
        schema = pa.schema([
            pa.field("Id", pa.int32()),
            pa.field("Value", pa.float64()),
        ])
        assert schema_dict_cols(schema) == []


class TestBuildWorkerSchemas:
    def test_parquet_with_order_cols(self):
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=False,
        )
        assert "SalesOrderNumber" in bundle.sales_schema_gen.names
        assert "SalesOrderLineNumber" in bundle.sales_schema_gen.names

    def test_parquet_skip_order_cols(self):
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=True,
            skip_order_cols_requested=True,
            returns_enabled=False,
        )
        assert "SalesOrderNumber" not in bundle.sales_schema_gen.names

    def test_delta_format_has_year_month(self):
        bundle = build_worker_schemas(
            file_format="deltaparquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=False,
        )
        assert "Year" in bundle.sales_schema_gen.names
        assert "Month" in bundle.sales_schema_gen.names

    def test_returns_schema_present_when_enabled(self):
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=True,
        )
        assert "SalesReturn" in bundle.schema_by_table

    def test_date_cols_present(self):
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=False,
        )
        assert len(bundle.date_cols_by_table) > 0

    def test_gen_schema_no_time_key(self):
        """GEN schema should NOT include TimeKey (injected later in task.py)."""
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=False,
        )
        assert "TimeKey" not in bundle.sales_schema_gen.names

    def test_out_schema_has_time_key(self):
        """OUT schema SHOULD include TimeKey."""
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=False,
        )
        assert "TimeKey" in bundle.sales_schema_out.names

    def test_order_number_int32_by_default(self):
        """SCHEMA-1: small runs keep SalesOrderNumber as int32."""
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=True,
        )
        for tbl in ("SalesOrderDetail", "SalesOrderHeader", "SalesReturn"):
            assert bundle.schema_by_table[tbl].field("SalesOrderNumber").type == pa.int32()
        assert bundle.sales_schema_gen.field("SalesOrderNumber").type == pa.int32()

    def test_order_id_int64_promotes_all_tables(self):
        """SCHEMA-1: order_id_int64=True promotes SalesOrderNumber everywhere."""
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=True,
            order_id_int64=True,
        )
        for tbl in ("SalesOrderDetail", "SalesOrderHeader", "SalesReturn"):
            assert bundle.schema_by_table[tbl].field("SalesOrderNumber").type == pa.int64()
        assert bundle.sales_schema_gen.field("SalesOrderNumber").type == pa.int64()


# ===================================================================
# Budget engine
# ===================================================================

class TestBudgetConfig:
    def test_defaults(self):
        cfg = BudgetConfig()
        assert cfg.enabled is False
        assert cfg.weight_local == 0.60

    def test_load_from_dict(self):
        from src.engine.config.config_schema import AppConfig
        raw = AppConfig.model_validate({
            "budget": {
                "enabled": True,
                "report_currency": "EUR",
                "growth_caps": {"high": 0.50, "low": -0.10},
                "weights": {"local": 0.70, "category": 0.20, "global": 0.10},
            }
        })
        cfg = load_budget_config(raw)
        assert cfg.enabled is True
        assert cfg.report_currency == "EUR"
        assert cfg.growth_cap_high == 0.50
        assert cfg.weight_local == 0.70

    def test_load_empty_dict(self):
        cfg = load_budget_config({})
        assert cfg.enabled is False


class TestJitterPct:
    def test_range(self):
        for country in ["US", "UK", "DE"]:
            for category in ["Electronics", "Clothing"]:
                for year in range(2020, 2025):
                    j = _jitter_pct(country, category, year)
                    assert -0.02 <= j <= 0.02

    def test_deterministic(self):
        j1 = _jitter_pct("US", "Electronics", 2023)
        j2 = _jitter_pct("US", "Electronics", 2023)
        assert j1 == j2

    def test_different_inputs_different_jitter(self):
        j1 = _jitter_pct("US", "Electronics", 2023)
        j2 = _jitter_pct("UK", "Clothing", 2024)
        # Not guaranteed but extremely unlikely to be equal
        # Just verify they are valid
        assert -0.02 <= j1 <= 0.02
        assert -0.02 <= j2 <= 0.02


class TestComputeBudget:
    def _make_actuals(self) -> pd.DataFrame:
        """Create minimal actuals data for budget computation."""
        rows = []
        for year in [2021, 2022, 2023]:
            for month in range(1, 13):
                for country in ["US", "UK"]:
                    for category in ["Electronics", "Clothing"]:
                        rows.append({
                            "Country": country,
                            "Category": category,
                            "Year": year,
                            "Month": month,
                            "SalesChannelKey": 1,
                            "SalesAmount": 1000.0 + year * 10 + month,
                            "SalesQuantity": 100 + month,
                        })
        return pd.DataFrame(rows)

    def test_produces_yearly_and_monthly(self):
        actuals = self._make_actuals()
        bcfg = BudgetConfig(enabled=True)
        yearly, monthly = compute_budget(actuals, bcfg)
        assert not yearly.empty
        assert not monthly.empty
        assert "BudgetYear" in yearly.columns
        assert "BudgetAmount" in monthly.columns

    def test_scenarios_present(self):
        actuals = self._make_actuals()
        bcfg = BudgetConfig(enabled=True)
        yearly, _ = compute_budget(actuals, bcfg)
        scenarios = set(yearly["Scenario"].unique())
        assert scenarios == {"Low", "Medium", "High"}

    def test_budget_amounts_positive(self):
        actuals = self._make_actuals()
        bcfg = BudgetConfig(enabled=True)
        yearly, monthly = compute_budget(actuals, bcfg)
        # Medium scenario with growth should be positive
        medium = yearly[yearly["Scenario"] == "Medium"]
        assert (medium["BudgetSalesAmount"] > 0).all()


# ===================================================================
# Inventory engine
# ===================================================================

class TestInventoryConfig:
    def test_defaults(self):
        cfg = InventoryConfig()
        assert cfg.enabled is False
        assert cfg.seed == 42
        assert cfg.shrinkage_rate == 0.02

    def test_load_from_dict(self):
        from src.engine.config.config_schema import AppConfig
        raw = AppConfig.model_validate({
            "inventory": {
                "enabled": True,
                "seed": 99,
                "shrinkage": {"enabled": True, "rate": 0.05},
                "initial_stock_multiplier": 5.0,
            }
        })
        cfg = load_inventory_config(raw)
        assert cfg.enabled is True
        assert cfg.seed == 99
        assert cfg.shrinkage_rate == 0.05
        assert cfg.initial_stock_multiplier == 5.0

    def test_load_empty_dict(self):
        cfg = load_inventory_config({})
        assert cfg.enabled is False


class TestComputeInventorySnapshots:
    def _make_demand(self) -> pd.DataFrame:
        """Create minimal demand data for inventory simulation."""
        rows = []
        for pk in [1, 2]:
            for wk in [1, 2]:
                for month in range(1, 7):
                    rows.append({
                        "ProductKey": pk,
                        "WarehouseKey": wk,
                        "Year": 2023,
                        "Month": month,
                        "QuantitySold": 10 + month,
                    })
        return pd.DataFrame(rows)

    def _make_product_attrs(self) -> dict:
        return {
            "ProductKey": np.array([1, 2], dtype=np.int32),
            "SafetyStockUnits": np.array([20, 15], dtype=np.int32),
            "ReorderPointUnits": np.array([10, 8], dtype=np.int32),
            "LeadTimeDays": np.array([14, 7], dtype=np.int32),
            "ABCClassification": np.array(["A", "B"]),
            "SeasonalityProfile": np.array(["None", "None"]),
            "IsFragile": np.array([0, 0], dtype=np.int32),
            "CasePackQty": np.array([1, 1], dtype=np.int32),
        }

    def test_produces_snapshots(self):
        demand = self._make_demand()
        icfg = InventoryConfig(enabled=True, min_demand_months=1)
        result = compute_inventory_snapshots(
            demand, ".", icfg,
            product_attrs_arrays=self._make_product_attrs(),
        )
        assert not result.empty
        assert "QuantityOnHand" in result.columns
        assert "StockoutFlag" in result.columns

    def test_empty_demand_returns_empty(self):
        demand = pd.DataFrame(columns=["ProductKey", "WarehouseKey", "Year", "Month", "QuantitySold"])
        icfg = InventoryConfig(enabled=True)
        result = compute_inventory_snapshots(demand, ".", icfg)
        assert result.empty

    def test_quantity_on_hand_non_negative(self):
        demand = self._make_demand()
        icfg = InventoryConfig(enabled=True, min_demand_months=1)
        result = compute_inventory_snapshots(
            demand, ".", icfg,
            product_attrs_arrays=self._make_product_attrs(),
        )
        assert (result["QuantityOnHand"] >= 0).all()

    def test_stockout_flag_binary(self):
        demand = self._make_demand()
        icfg = InventoryConfig(enabled=True, min_demand_months=1)
        result = compute_inventory_snapshots(
            demand, ".", icfg,
            product_attrs_arrays=self._make_product_attrs(),
        )
        assert set(result["StockoutFlag"].unique()).issubset({0, 1})

    def test_reorder_flag_binary(self):
        demand = self._make_demand()
        icfg = InventoryConfig(enabled=True, min_demand_months=1)
        result = compute_inventory_snapshots(
            demand, ".", icfg,
            product_attrs_arrays=self._make_product_attrs(),
        )
        assert set(result["ReorderFlag"].unique()).issubset({0, 1})

    def test_deterministic(self):
        demand = self._make_demand()
        icfg = InventoryConfig(enabled=True, seed=42, min_demand_months=1)
        attrs = self._make_product_attrs()
        r1 = compute_inventory_snapshots(demand, ".", icfg, product_attrs_arrays=attrs)
        r2 = compute_inventory_snapshots(demand, ".", icfg, product_attrs_arrays=attrs)
        pd.testing.assert_frame_equal(r1, r2)

    def test_min_demand_months_filters(self):
        """Pairs with fewer demand months than threshold are excluded."""
        rows = [
            {"ProductKey": 1, "WarehouseKey": 1, "Year": 2023, "Month": 1, "QuantitySold": 10},
        ]
        demand = pd.DataFrame(rows)
        icfg = InventoryConfig(enabled=True, min_demand_months=3)
        result = compute_inventory_snapshots(
            demand, ".", icfg,
            product_attrs_arrays={
                "ProductKey": np.array([1], dtype=np.int32),
                "SafetyStockUnits": np.array([20], dtype=np.int32),
                "ReorderPointUnits": np.array([10], dtype=np.int32),
                "LeadTimeDays": np.array([14], dtype=np.int32),
                "ABCClassification": np.array(["B"]),
                "SeasonalityProfile": np.array(["None"]),
                "IsFragile": np.array([0], dtype=np.int32),
                "CasePackQty": np.array([1], dtype=np.int32),
            },
        )
        assert result.empty


# ===================================================================
# Chunk assembly — CHUNK-3 (null-month padding)
# ===================================================================

class TestAssembleColumn:
    """CHUNK-3: a column produced in some months but null in others must keep
    full length (null months padded), not be silently shortened/misaligned."""

    _ST = {"v": pa.int32()}

    def test_all_data_months(self):
        from src.facts.sales.sales_logic.chunk_builder import _assemble_column
        bufs = [np.array([1, 2], dtype=np.int32), np.array([3], dtype=np.int32)]
        out = _assemble_column("v", bufs, [2, 1], 3, self._ST)
        assert out.to_pylist() == [1, 2, 3]

    def test_all_null_months(self):
        from src.facts.sales.sales_logic.chunk_builder import _assemble_column
        out = _assemble_column("v", [None, None], [2, 1], 3, self._ST)
        assert len(out) == 3 and out.null_count == 3

    def test_mixed_pads_null_months(self):
        from src.facts.sales.sales_logic.chunk_builder import _assemble_column
        # month0: 2 data rows, month1: 3 null rows, month2: 1 data row.
        bufs = [np.array([1, 2], dtype=np.int32), None, np.array([9], dtype=np.int32)]
        out = _assemble_column("v", bufs, [2, 3, 1], 6, self._ST)
        # Must be 6 (the old code dropped the null month, giving a misaligned 3).
        assert len(out) == 6
        assert out.to_pylist() == [1, 2, None, None, None, 9]


# ===================================================================
# Inventory ABC recompute — INV-1 (SCD2 family aggregation)
# ===================================================================

class TestRecomputeABC:
    """INV-1: ABC ranking must aggregate a product-SCD2 family's volume across
    its version keys, not rank each version key separately."""

    def _demand(self):
        # Family (ProductID 1) split across version keys 1,2,3 (40 each = 120 total);
        # single-version products 4..10 with individually-higher per-key volume.
        return pd.DataFrame({
            "ProductKey": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "QuantitySold": [40, 40, 40, 50, 45, 30, 25, 20, 15, 10],
        })

    def _attrs(self):
        return {
            "ProductKey": np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=np.int64),
            "ABCClassification": np.array(["C"] * 10),
        }

    def test_without_family_map_fragments(self):
        # No SCD2 map -> version keys ranked separately, family not top-tier.
        out = _recompute_abc_from_demand(self._demand(), self._attrs())["ABCClassification"]
        assert not all(out[i] == "A" for i in range(3))

    def test_with_family_map_aggregates_to_A(self):
        pk_to_pid = {1: 1, 2: 1, 3: 1, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9, 10: 10}
        out = _recompute_abc_from_demand(self._demand(), self._attrs(), pk_to_pid)["ABCClassification"]
        # Family total (120) is the largest -> all three version keys classified A.
        assert all(out[i] == "A" for i in range(3))
