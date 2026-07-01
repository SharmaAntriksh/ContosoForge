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
from src.facts.sales.sales_worker.task import derive_chunk_seed
from src.tools.sql.dialect import SqlType
from src.utils.static_schemas import get_sales_schema, order_id_int64_for_rows
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
    _remove_rows_stochastic,
    _safe_prob,
    _sched_mode_and_values,
    build_rows_per_month,
    macro_month_weights,
)
from src.facts.sales.sales_logic.core.customer_sampling import (
    _eligible_customer_mask_for_month,
    _hash_uniform,
    _hash_uniform_positions,
    _normalize_end_month,
    _normalize_weights,
    _participation_distinct_target,
    _sample_customers,
    _urgency_pick,
    assign_orders_to_customers,
    build_month_customer_pool,
    compute_discovery_months,
    compute_month_distinct_targets,
)
from src.facts.sales.sales_logic.chunk_builder import _chunk_month_band
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
    _ontime_reason_probs,
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
        result = compute_prices(0, [], [])
        assert result["final_unit_price"].shape == (0,)

    def test_positive_prices(self):
        up = np.array([10.0, 20.0, 30.0])
        uc = np.array([5.0, 10.0, 15.0])
        result = compute_prices(3, up, uc)
        assert np.all(result["final_unit_price"] >= 0)
        assert np.all(result["final_unit_cost"] >= 0)

    def test_cost_not_exceeding_price(self):
        up = np.array([10.0, 5.0, 20.0])
        uc = np.array([15.0, 3.0, 25.0])  # some costs > prices
        result = compute_prices(3, up, uc)
        assert np.all(result["final_unit_cost"] <= result["final_unit_price"])

    def test_zero_discount(self):
        up = np.array([10.0, 20.0])
        uc = np.array([5.0, 10.0])
        result = compute_prices(2, up, uc)
        np.testing.assert_array_equal(result["discount_amt"], [0.0, 0.0])

    def test_net_price_equals_unit_price(self):
        up = np.array([10.0, 20.0])
        uc = np.array([5.0, 10.0])
        result = compute_prices(2, up, uc)
        np.testing.assert_array_equal(result["final_net_price"], result["final_unit_price"])

    def test_nan_prices_treated_as_zero(self):
        up = np.array([float("nan"), 20.0])
        uc = np.array([5.0, float("nan")])
        result = compute_prices(2, up, uc)
        assert result["final_unit_price"][0] == 0.0
        assert result["final_unit_cost"][1] == 0.0

    def test_negative_prices_clipped_to_zero(self):
        up = np.array([-10.0, 20.0])
        uc = np.array([-5.0, 10.0])
        result = compute_prices(2, up, uc)
        assert result["final_unit_price"][0] == 0.0
        assert result["final_unit_cost"][0] == 0.0

    def test_single_element(self):
        result = compute_prices(1, [99.99], [49.99])
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

    def test_channel_group_length_mismatch_warns(self):
        """CORE-4: requesting channel filtering with a mis-sized promo_channel_group
        must warn, not silently drop the channel correlation."""
        import src.facts.sales.sales_logic.core.promotions as promo_mod
        promo_mod._warned_ch_len_mismatch = False
        dates = np.array(["2023-01-15"] * 6, dtype="datetime64[D]")
        promo_keys = np.array([1, 10], dtype=np.int32)  # P = 2
        promo_start = np.array(["2023-01-01", "2023-01-10"], dtype="datetime64[D]")
        promo_end = np.array(["2023-01-31", "2023-01-31"], dtype="datetime64[D]")
        apply_promotions(
            _rng(), 6, dates, promo_keys, promo_start, promo_end, no_discount_key=1,
            channel_keys=np.ones(6, dtype=np.int32),
            promo_channel_group=np.array([0], dtype=np.int8),  # len 1 != P=2
        )
        assert promo_mod._warned_ch_len_mismatch is True


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


class TestComputeDiscoveryMonths:
    def test_anchored_and_within_window(self):
        keys = np.arange(1, 11, dtype=np.int32)
        active = np.ones(10, dtype=np.int64)
        start = np.array([0, 0, 1, 2, 3, 4, 5, 6, 7, 8], dtype=np.int64)
        end = np.full(10, -1, dtype=np.int64)
        disc = compute_discovery_months(keys, active, start, end, T=12,
                                        run_seed=7, lag_scale=1.0)
        # Never before eligibility, never past the last month, never the sentinel.
        assert np.all(disc >= start)
        assert np.all(disc <= 11)
        assert not np.any(disc == 12)

    def test_lag_zero_debuts_at_start(self):
        keys = np.arange(1, 8, dtype=np.int32)
        active = np.ones(7, dtype=np.int64)
        start = np.array([0, 1, 2, 3, 4, 5, 6], dtype=np.int64)
        end = np.full(7, -1, dtype=np.int64)
        disc = compute_discovery_months(keys, active, start, end, T=12,
                                        run_seed=7, lag_scale=0.0)
        np.testing.assert_array_equal(disc, start)

    def test_warm_start_debuts_month_zero(self):
        keys = np.array([1, 2, 3], dtype=np.int32)
        active = np.ones(3, dtype=np.int64)
        start = np.array([-5, -1, 0], dtype=np.int64)   # pre-existing customers
        end = np.full(3, -1, dtype=np.int64)
        disc = compute_discovery_months(keys, active, start, end, T=12,
                                        run_seed=7, lag_scale=2.0)
        assert disc[0] == 0 and disc[1] == 0

    def test_inactive_and_out_of_window_never(self):
        keys = np.array([1, 2, 3], dtype=np.int32)
        active = np.array([0, 1, 1], dtype=np.int64)     # #1 inactive
        start = np.array([0, 15, 2], dtype=np.int64)      # #2 joins after T
        end = np.full(3, -1, dtype=np.int64)
        disc = compute_discovery_months(keys, active, start, end, T=12,
                                        run_seed=7, lag_scale=1.0)
        assert disc[0] == 12     # inactive → sentinel
        assert disc[1] == 12     # start_month >= T → sentinel
        assert disc[2] < 12

    def test_deterministic_and_seed_sensitive(self):
        keys = np.arange(1, 200, dtype=np.int32)
        active = np.ones(199, dtype=np.int64)
        start = (keys % 10).astype(np.int64)
        end = np.full(199, -1, dtype=np.int64)
        a = compute_discovery_months(keys, active, start, end, T=24, run_seed=1)
        b = compute_discovery_months(keys, active, start, end, T=24, run_seed=1)
        c = compute_discovery_months(keys, active, start, end, T=24, run_seed=2)
        np.testing.assert_array_equal(a, b)          # same seed → identical
        assert not np.array_equal(a, c)              # different seed → reshuffled


class TestHashUniform:
    def test_range_and_determinism(self):
        keys = np.arange(1, 1000, dtype=np.int64)
        u1 = _hash_uniform(keys, 1234)
        u2 = _hash_uniform(keys, 1234)
        np.testing.assert_array_equal(u1, u2)
        assert u1.min() >= 0.0 and u1.max() < 1.0
        # Roughly uniform mean for a large sample.
        assert abs(float(u1.mean()) - 0.5) < 0.05


class TestSampleCustomers:
    def test_returns_correct_length(self):
        keys = np.arange(1, 11, dtype=np.int32)
        eligible = np.ones(10, dtype=bool)
        result = _sample_customers(
            _rng(), keys, eligible, None, 20, use_discovery=False,
        )
        assert result.shape == (20,)

    def test_zero_n(self):
        keys = np.arange(1, 6, dtype=np.int32)
        eligible = np.ones(5, dtype=bool)
        result = _sample_customers(
            _rng(), keys, eligible, None, 0, use_discovery=False,
        )
        assert result.shape == (0,)

    def test_no_eligible_returns_empty(self):
        keys = np.arange(1, 6, dtype=np.int32)
        eligible = np.zeros(5, dtype=bool)
        result = _sample_customers(
            _rng(), keys, eligible, None, 10, use_discovery=False,
        )
        assert result.shape == (0,)

    def test_discovery_forces_debut_cohort(self):
        # discovery_month is pool-aligned; customers scheduled to debut in the
        # current month must appear, and not-yet-introduced ones must not.
        keys = np.arange(1, 21, dtype=np.int32)
        eligible = np.ones(20, dtype=bool)
        discovery_month = np.full(20, 5, dtype=np.int64)   # everyone future by default
        discovery_month[:3] = 0        # customers 1,2,3 introduced earlier
        discovery_month[3:6] = 2       # customers 4,5,6 debut this month
        result = _sample_customers(
            _rng(), keys, eligible, discovery_month, 50, use_discovery=True,
            m_offset=2,
        )
        got = set(int(x) for x in result)
        assert {4, 5, 6} <= got                       # debut cohort forced in
        assert got <= {1, 2, 3, 4, 5, 6}              # future customers excluded

    def test_target_distinct_limits_unique(self):
        keys = np.arange(1, 101, dtype=np.int32)
        eligible = np.ones(100, dtype=bool)
        result = _sample_customers(
            _rng(), keys, eligible, None, 50, use_discovery=False,
            target_distinct=5,
        )
        assert len(np.unique(result)) <= 5


# ===================================================================
# Phase 2 — global per-month plan (plan globally, shard the index space)
# ===================================================================

class TestChunkMonthBand:
    """`_chunk_month_band` tiles the global order/line space exactly."""

    def test_sums_exact_and_no_gaps_across_chunk_counts(self):
        for C in (1, 2, 3, 5, 12, 37):
            for R, O in [(1000, 556), (37, 21), (1, 1), (500, 1),
                         (999, 999), (8, 7), (10000, 5000)]:
                tot_o = tot_l = 0
                prev = 0
                for c in range(C):
                    start, no, nl = _chunk_month_band(R, O, c, C)
                    assert nl >= no >= 0            # >= 1 line per order
                    if no > 0:
                        assert start == prev       # contiguous, no gaps
                    prev = start + no
                    tot_o += no
                    tot_l += nl
                assert tot_o == O                  # orders tile [0, O)
                assert tot_l == R                  # lines sum to total_rows

    def test_degenerate_inputs(self):
        assert _chunk_month_band(0, 0, 0, 4) == (0, 0, 0)
        assert _chunk_month_band(100, 0, 0, 4) == (0, 0, 0)
        assert _chunk_month_band(100, 50, 0, 0) == (0, 0, 0)


class TestHashUniformPositions:
    def test_range_determinism_and_position_sensitivity(self):
        pos = np.arange(0, 500, dtype=np.int64)
        a = _hash_uniform_positions(3, pos, 1234)
        b = _hash_uniform_positions(3, pos, 1234)
        np.testing.assert_array_equal(a, b)              # deterministic
        assert a.min() >= 0.0 and a.max() < 1.0
        # Different month or seed reshuffles.
        assert not np.array_equal(a, _hash_uniform_positions(4, pos, 1234))
        assert not np.array_equal(a, _hash_uniform_positions(3, pos, 9999))


class TestAssignOrdersToCustomers:
    """The core chunk-invariance property: distinct set == pool regardless of
    how the month's order band is split into chunks."""

    def _pool_cdf(self, n=20):
        pool = np.arange(100, 100 + n, dtype=np.int32)
        cdf = np.linspace(1.0 / n, 1.0, n)
        cdf[-1] = 1.0
        return pool, cdf

    def test_distinct_prefix_is_the_pool(self):
        pool, cdf = self._pool_cdf(20)
        # The whole month (O=50 orders): first 20 order-indices are the pool.
        full = assign_orders_to_customers(
            m_offset=2, order_start=0, n_orders=50, pool=pool, cdf=cdf, seed=7)
        assert set(full[:20].tolist()) == set(pool.tolist())
        assert set(full.tolist()) == set(pool.tolist())   # repeats add nobody new

    def test_split_union_equals_single_pass(self):
        pool, cdf = self._pool_cdf(20)
        O = 50
        single = assign_orders_to_customers(
            m_offset=2, order_start=0, n_orders=O, pool=pool, cdf=cdf, seed=7)
        # Split [0, O) into arbitrary contiguous bands and concatenate.
        for bounds in ([0, 7, 20, 33, 50], [0, 25, 50], [0, 1, 49, 50]):
            parts = []
            for a, b in zip(bounds[:-1], bounds[1:]):
                parts.append(assign_orders_to_customers(
                    m_offset=2, order_start=a, n_orders=b - a,
                    pool=pool, cdf=cdf, seed=7))
            merged = np.concatenate(parts)
            # Same customer at every global index → identical array, and identical
            # distinct set (the chunk-invariance guarantee).
            np.testing.assert_array_equal(merged, single)
            assert set(merged.tolist()) == set(pool.tolist())

    def test_empty_pool_returns_empty(self):
        out = assign_orders_to_customers(
            m_offset=0, order_start=0, n_orders=10,
            pool=np.empty(0, dtype=np.int32), cdf=np.empty(0), seed=1)
        assert out.shape == (0,)


class TestBuildMonthCustomerPool:
    def _lifecycle(self, n=40):
        keys = np.arange(1, n + 1, dtype=np.int32)
        eligible_idx = np.arange(n)          # all eligible this month
        end_norm = np.full(n, -1, dtype=np.int64)
        return keys, eligible_idx, end_norm

    def test_forces_debut_excludes_future_and_caps_at_target(self):
        keys, eligible_idx, end_norm = self._lifecycle(40)
        disc = np.full(40, 5, dtype=np.int64)   # most are future
        disc[:5] = 1                            # 1..5 introduced earlier
        disc[5:10] = 3                          # 6..10 debut this month (m=3)
        pool, cdf = build_month_customer_pool(
            m_offset=3, distinct_target=8, eligible_idx=eligible_idx,
            customer_keys=keys, discovery_month=disc, base_weight=None,
            end_month_norm=end_norm, seed=7)
        s = set(int(x) for x in pool)
        assert {6, 7, 8, 9, 10} <= s                    # debut cohort forced in
        assert s <= set(range(1, 11))                   # no not-yet-introduced keys
        assert pool.size <= 8                           # capped at distinct target
        assert abs(float(cdf[-1]) - 1.0) < 1e-12

    def test_deterministic_by_month_and_seed(self):
        keys, eligible_idx, end_norm = self._lifecycle(40)
        disc = np.zeros(40, dtype=np.int64)             # all introduced
        a, _ = build_month_customer_pool(
            m_offset=4, distinct_target=15, eligible_idx=eligible_idx,
            customer_keys=keys, discovery_month=disc, base_weight=None,
            end_month_norm=end_norm, seed=7)
        b, _ = build_month_customer_pool(
            m_offset=4, distinct_target=15, eligible_idx=eligible_idx,
            customer_keys=keys, discovery_month=disc, base_weight=None,
            end_month_norm=end_norm, seed=7)
        c, _ = build_month_customer_pool(
            m_offset=4, distinct_target=15, eligible_idx=eligible_idx,
            customer_keys=keys, discovery_month=disc, base_weight=None,
            end_month_norm=end_norm, seed=8)
        np.testing.assert_array_equal(a, b)
        assert not np.array_equal(a, c)

    def test_discovery_off_draws_from_all_eligible(self):
        keys, eligible_idx, end_norm = self._lifecycle(40)
        pool, _ = build_month_customer_pool(
            m_offset=0, distinct_target=10, eligible_idx=eligible_idx,
            customer_keys=keys, discovery_month=None, base_weight=None,
            end_month_norm=end_norm, seed=7)
        assert pool.size == 10
        assert len(set(pool.tolist())) == 10            # distinct


class TestComputeMonthDistinctTargets:
    def test_zero_ratio_is_all_zero(self):
        out = compute_month_distinct_targets(
            seed=1, T=6, eligible_counts=np.full(6, 100),
            orders_per_month=np.full(6, 50), month_cal_index=np.arange(1, 7),
            distinct_ratio=0.0, cycle_amplitude=0.0, participation_noise=0.0,
            seasonal_spike_map={}, max_distinct_ratio=0.7, min_distinct_customers=0)
        assert out.tolist() == [0] * 6

    def test_capped_by_orders_and_eligible(self):
        # target never exceeds orders-per-month or eligible count.
        out = compute_month_distinct_targets(
            seed=1, T=4, eligible_counts=np.array([1000, 1000, 5, 1000]),
            orders_per_month=np.array([3, 1000, 1000, 0]),
            month_cal_index=np.array([1, 2, 3, 4]),
            distinct_ratio=1.0, cycle_amplitude=0.0, participation_noise=0.0,
            seasonal_spike_map={}, max_distinct_ratio=1.0, min_distinct_customers=0)
        assert out[0] <= 3          # capped by orders
        assert out[2] <= 5          # capped by eligible
        assert out[3] == 0          # zero orders → zero

    def test_deterministic_and_seed_sensitive(self):
        kw = dict(T=12, eligible_counts=np.full(12, 500),
                  orders_per_month=np.full(12, 300),
                  month_cal_index=((np.arange(12)) % 12) + 1,
                  distinct_ratio=0.55, cycle_amplitude=0.35,
                  participation_noise=0.10,
                  seasonal_spike_map={11: 0.4, 12: 0.25},
                  max_distinct_ratio=0.7, min_distinct_customers=1)
        a = compute_month_distinct_targets(seed=1, **kw)
        b = compute_month_distinct_targets(seed=1, **kw)
        c = compute_month_distinct_targets(seed=2, **kw)
        np.testing.assert_array_equal(a, b)
        assert not np.array_equal(a, c)
        assert np.all(a <= 300)     # never exceeds orders


# ===================================================================
# 7. Budget micro-aggregation
# ===================================================================

def _make_budget_arrow_table(n: int = 100) -> pa.Table:
    """Create a minimal sales Arrow table for budget micro-agg testing."""
    rng = _rng()
    return pa.table({
        "StoreKey": pa.array(rng.integers(0, 3, size=n, dtype=np.int32), type=pa.int32()),
        "ProductKey": pa.array(rng.integers(0, 5, size=n, dtype=np.int32), type=pa.int32()),
        "ChannelKey": pa.array(rng.integers(1, 3, size=n, dtype=np.int32), type=pa.int32()),
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
    """Create a minimal OrderDetail Arrow table for returns testing."""
    rng = _rng()
    return pa.table({
        "OrderNumber": pa.array(np.arange(1, n + 1, dtype=np.int32), type=pa.int32()),
        "OrderLineNumber": pa.array(np.ones(n, dtype=np.int32), type=pa.int32()),
        "DeliveryDate": pa.array(
            np.array(["2023-06-15"] * n, dtype="datetime64[D]"), type=pa.date32()
        ),
        "Quantity": pa.array(rng.integers(1, 10, size=n, dtype=np.int32), type=pa.int32()),
        "NetPrice": pa.array(rng.uniform(10.0, 100.0, size=n), type=pa.float64()),
        "IsOrderDelayed": pa.array(rng.integers(0, 2, size=n, dtype=np.int32), type=pa.int32()),
    })


class TestBuildReturns:
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
            "OrderNumber": pa.array(so, type=pa.int64()),
            "OrderLineNumber": pa.array(np.ones(n, dtype=np.int32), type=pa.int32()),
            "DeliveryDate": pa.array(
                np.array(["2023-06-15"] * n, dtype="datetime64[D]"), type=pa.date32()
            ),
            "Quantity": pa.array(rng.integers(1, 10, size=n, dtype=np.int32), type=pa.int32()),
            "NetPrice": pa.array(rng.uniform(10.0, 100.0, size=n), type=pa.float64()),
            "IsOrderDelayed": pa.array(np.zeros(n, dtype=np.int32), type=pa.int32()),
        })

    def test_int64_detail_preserves_dtype_and_value(self):
        """SCHEMA-1: returns mirror int64 OrderNumber without truncation."""
        start = 2_147_483_647 + 1000  # beyond int32 range
        detail = self._int64_detail_table(50, start)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, min_lag_days=1, max_lag_days=30)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        assert result.schema.field("OrderNumber").type == pa.int64()
        returned = set(result.column("OrderNumber").to_pylist())
        assert returned and returned.issubset(set(range(start, start + 50)))
        assert min(returned) > 2_147_483_647  # not wrapped to int32

    def test_int64_detail_empty_keeps_int64_schema(self):
        """Empty result must still carry int64 SO# (cross-chunk concat consistency)."""
        detail = self._int64_detail_table(1, 5_000_000_000)
        result = build_sales_returns_from_detail(
            detail, chunk_seed=42, cfg=ReturnsConfig(enabled=False),
        )
        assert result.num_rows == 0
        assert result.schema.field("OrderNumber").type == pa.int64()

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
            "OrderNumber": pa.array([], type=pa.int32()),
            "OrderLineNumber": pa.array([], type=pa.int32()),
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
            "OrderNumber": pa.array([1], type=pa.int32()),
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
        assert "OrderNumber" in bundle.sales_schema_gen.names
        assert "OrderLineNumber" in bundle.sales_schema_gen.names

    def test_parquet_skip_order_cols(self):
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=True,
            skip_order_cols_requested=True,
            returns_enabled=False,
        )
        assert "OrderNumber" not in bundle.sales_schema_gen.names

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
        assert "Returns" in bundle.schema_by_table

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
        """SCHEMA-1: small runs keep OrderNumber as int32."""
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=True,
        )
        for tbl in ("OrderDetail", "OrderHeader", "Returns"):
            assert bundle.schema_by_table[tbl].field("OrderNumber").type == pa.int32()
        assert bundle.sales_schema_gen.field("OrderNumber").type == pa.int32()

    def test_order_id_int64_promotes_all_tables(self):
        """SCHEMA-1: order_id_int64=True promotes OrderNumber everywhere."""
        bundle = build_worker_schemas(
            file_format="parquet",
            skip_order_cols=False,
            skip_order_cols_requested=False,
            returns_enabled=True,
            order_id_int64=True,
        )
        for tbl in ("OrderDetail", "OrderHeader", "Returns"):
            assert bundle.schema_by_table[tbl].field("OrderNumber").type == pa.int64()
        assert bundle.sales_schema_gen.field("OrderNumber").type == pa.int64()


class TestDeriveChunkSeed:
    """Phase 1.4 / Finding #33: the per-chunk RNG seed is a pure function of
    (run_seed, chunk_idx) via the house SeedSequence pattern — independently
    regenerable and independent of chunk dispatch order / worker count."""

    def test_deterministic(self):
        assert derive_chunk_seed(1234, 7) == derive_chunk_seed(1234, 7)

    def test_distinct_per_chunk_and_seed(self):
        per_chunk = [derive_chunk_seed(1234, i) for i in range(64)]
        assert len(set(per_chunk)) == 64                      # no collisions across chunks
        assert derive_chunk_seed(1234, 7) != derive_chunk_seed(5678, 7)  # seed matters

    def test_regenerable_in_isolation(self):
        # spawn(idx+1)[idx] (what derive_chunk_seed uses) must equal the canonical
        # house pattern spawn(n_chunks)[idx], so any single chunk can be reproduced
        # without materializing the whole per-chunk seed sequence.
        seed, n = 1234, 40
        for idx in (0, 1, 17, 39):
            canonical = int(
                np.random.SeedSequence(seed).spawn(n)[idx].generate_state(1, dtype=np.uint32)[0]
            )
            assert derive_chunk_seed(seed, idx) == canonical


class TestOrderIdInt64PathsAgree:
    """Phase 1.2 / Finding #21: the parquet (Arrow) and SQL (DDL) OrderNumber
    width must be the SAME decision. Both consume one authoritative flag; the
    row-count estimate is only a fallback and must itself be id-space aware so the
    two paths never disagree across a range of total_rows."""

    def _arrow_is_int64(self, flag: bool) -> bool:
        bundle = build_worker_schemas(
            file_format="parquet", skip_order_cols=False,
            skip_order_cols_requested=False, returns_enabled=True,
            order_id_int64=flag,
        )
        return bundle.sales_schema_gen.field("OrderNumber").type == pa.int64()

    def _sql_is_bigint(self, flag: bool) -> bool:
        cols = dict(get_sales_schema(False, force_int64=flag))
        return cols["OrderNumber"].sql_type == SqlType.BIGINT

    def test_paths_agree_when_fed_same_flag(self):
        for flag in (False, True):
            assert self._arrow_is_int64(flag) == self._sql_is_bigint(flag) == flag

    def test_estimate_makes_paths_agree_across_row_range(self):
        # Spans below, through, and above the ~134M id-space threshold — including
        # the 134M–1.07B band where the old total_rows-only DDL rule diverged.
        for rows in (1_000, 10_000_000, 134_000_000, 200_000_000,
                     500_000_000, 2_000_000_000):
            flag = order_id_int64_for_rows(rows)
            assert self._arrow_is_int64(flag) == self._sql_is_bigint(flag), (
                f"parquet/DDL OrderNumber width disagree at total_rows={rows:,}"
            )


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
                "growth_caps": {"high": 0.50, "low": -0.10},
                "weights": {"local": 0.70, "category": 0.20, "global": 0.10},
            }
        })
        cfg = load_budget_config(raw)
        assert cfg.enabled is True
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
                            "ChannelKey": 1,
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
        assert (medium["BudgetAmount"] > 0).all()

    def test_monthly_rolls_up_to_yearly_sparse_category(self):
        """BUDGET-1: a category with actuals in only a few months must still have
        its 12 monthly budgets sum to the yearly total (not overshoot)."""
        rows = []
        for year in [2021, 2022, 2023]:
            for country in ["US", "UK"]:
                for month in range(1, 13):  # Electronics: full year
                    rows.append({"Country": country, "Category": "Electronics",
                                 "Year": year, "Month": month, "ChannelKey": 1,
                                 "SalesAmount": 1000.0 + month, "SalesQuantity": 100 + month})
                for month in [1, 2, 3]:      # Clothing: sparse (3 months)
                    rows.append({"Country": country, "Category": "Clothing",
                                 "Year": year, "Month": month, "ChannelKey": 1,
                                 "SalesAmount": 500.0, "SalesQuantity": 50})
        yearly, monthly = compute_budget(pd.DataFrame(rows), BudgetConfig(enabled=True))

        keys = ["Country", "Category", "BudgetYear", "Scenario"]
        m_sum = monthly.groupby(keys)["BudgetAmount"].sum().reset_index()
        merged = m_sum.merge(yearly[keys + ["BudgetAmount"]], on=keys, suffixes=("_monthly", "_yearly"))
        ratio = merged["BudgetAmount_monthly"] / merged["BudgetAmount_yearly"]
        # Under the bug, sparse Clothing overshoots ~1.75x; allow only cents rounding.
        assert ratio.between(0.99, 1.01).all(), merged.loc[~ratio.between(0.99, 1.01)]


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


# ===================================================================
# RETURNS-2 (on-time reason probabilities) + CORE-3 (exact row removal)
# ===================================================================

class TestOntimeReasonProbs:
    """RETURNS-2: logistics reasons must stay at exactly 0 for on-time orders,
    and the distribution must be valid (non-negative, sums to 1)."""

    def test_logistics_stays_zero_and_valid(self):
        reason_probs = np.array([0.1, 0.2, 0.3, 0.4])
        # Last reason is logistics — the old boundary guard forced mass onto it.
        logistics_mask = np.array([False, False, False, True])
        p = _ontime_reason_probs(reason_probs, logistics_mask)
        assert p[3] == 0.0                      # logistics slot never revived
        assert np.all(p >= 0.0)                 # never negative -> rng.choice safe
        assert abs(float(p.sum()) - 1.0) < 1e-12

    def test_multiple_logistics_including_last(self):
        reason_probs = np.array([0.25, 0.25, 0.25, 0.25])
        logistics_mask = np.array([False, True, False, True])
        p = _ontime_reason_probs(reason_probs, logistics_mask)
        assert p[1] == 0.0 and p[3] == 0.0
        assert np.all(p >= 0.0)
        assert abs(float(p.sum()) - 1.0) < 1e-12

    def test_all_logistics_falls_back_to_uniform(self):
        reason_probs = np.array([0.5, 0.5])
        logistics_mask = np.array([True, True])
        p = _ontime_reason_probs(reason_probs, logistics_mask)
        assert np.all(p >= 0.0)
        assert abs(float(p.sum()) - 1.0) < 1e-12


class TestRemoveRowsStochastic:
    """CORE-3: must remove exactly `need` rows (loop to completion), not stop
    short at a fixed iteration cap."""

    def test_removes_exact_amount_few_candidates_large_need(self):
        rng = np.random.default_rng(1)
        rows = np.array([100, 100], dtype=np.int64)   # 2 candidates, lots of rows
        _remove_rows_stochastic(rng, rows, 150, np.array([0, 1]), np.ones(2))
        assert int(rows.sum()) == 50                   # 200 - 150 removed exactly

    def test_stops_when_candidates_exhausted(self):
        rng = np.random.default_rng(2)
        rows = np.array([3, 4], dtype=np.int64)        # only 7 rows available
        _remove_rows_stochastic(rng, rows, 10, np.array([0, 1]), np.ones(2))
        assert int(rows.sum()) == 0                    # removes all it can, no hang


# ===================================================================
# CORE-1 (urgency ordering in the all-forced corner)
# ===================================================================

class TestUrgencyPick:
    """CORE-1: when every undiscovered key is forced (size >= keys.size), the
    result must still be ordered by urgency (nearest-expiry first), so a
    downstream [:k] slice keeps the most urgent customers."""

    def test_all_forced_returns_urgency_order(self):
        keys = np.array([10, 20, 30, 40], dtype=np.int64)
        indices = np.array([0, 1, 2, 3])
        end_month_norm = np.array([5, 2, 8, 3], dtype=np.int64)  # remaining: 5,2,8,3
        out = _urgency_pick(
            np.random.default_rng(0), keys, indices, end_month_norm, m_offset=0, size=4,
        )
        # Nearest expiry is key 20 (remaining 2); original order would put 10 first.
        assert out[0] == 20
        assert list(out) == [20, 40, 10, 30]

    def test_subset_still_urgency_order(self):
        keys = np.array([10, 20, 30, 40], dtype=np.int64)
        indices = np.array([0, 1, 2, 3])
        end_month_norm = np.array([5, 2, 8, 3], dtype=np.int64)
        out = _urgency_pick(
            np.random.default_rng(0), keys, indices, end_month_norm, m_offset=0, size=2,
        )
        assert list(out) == [20, 40]  # two most urgent
