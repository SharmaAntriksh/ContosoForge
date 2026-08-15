"""Tests for per-salesperson performance spread (Part 2).

``sales.salesperson_performance`` gives each employee a stable lognormal
multiplier (median 1.0) applied to salesperson selection weights, so within a
store some reps sell more than others. It must be a pure function of
EmployeeKey + seed (stable across stores / chunks / workers) and a no-op when
disabled.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.facts.sales.sales_worker.init import (
    _build_salesperson_effective_by_store,
    _salesperson_perf_multiplier,
)


class TestPerfMultiplier:
    def test_deterministic(self):
        emp = np.array([40_000_001, 40_001_002, 50_000_003], dtype=np.int64)
        a = _salesperson_perf_multiplier(emp, 0.5, 42)
        b = _salesperson_perf_multiplier(emp, 0.5, 42)
        assert np.array_equal(a, b)

    def test_disabled_is_ones(self):
        emp = np.array([40_000_001, 40_000_002], dtype=np.int64)
        assert np.allclose(_salesperson_perf_multiplier(emp, 0.0, 42), 1.0)
        assert np.allclose(_salesperson_perf_multiplier(emp, -1.0, 42), 1.0)

    def test_empty(self):
        assert _salesperson_perf_multiplier(np.array([], dtype=np.int64), 0.5, 42).size == 0

    def test_key_stable_regardless_of_position(self):
        # The same EmployeeKey must get the same multiplier no matter which
        # other keys share the array (transfers => an emp appears under many
        # stores; all must agree).
        target = 40_123_456
        arr1 = np.array([target, 40_000_001, 40_000_002], dtype=np.int64)
        arr2 = np.array([40_999_999, 40_000_003, target], dtype=np.int64)
        v1 = _salesperson_perf_multiplier(arr1, 0.6, 7)[0]
        v2 = _salesperson_perf_multiplier(arr2, 0.6, 7)[2]
        assert v1 == v2

    def test_clipped_and_centered(self):
        emp = np.arange(40_000_000, 40_020_000, dtype=np.int64)
        w = _salesperson_perf_multiplier(emp, 0.5, 42, lo=0.25, hi=4.0)
        assert w.min() >= 0.25 and w.max() <= 4.0
        assert abs(float(np.median(w)) - 1.0) < 0.05

    def test_spread_controls_variance(self):
        emp = np.arange(40_000_000, 40_010_000, dtype=np.int64)
        lo = _salesperson_perf_multiplier(emp, 0.3, 42)
        hi = _salesperson_perf_multiplier(emp, 0.9, 42)
        assert hi.std() > lo.std()

    def test_seed_changes_output(self):
        emp = np.arange(40_000_000, 40_005_000, dtype=np.int64)
        assert not np.array_equal(
            _salesperson_perf_multiplier(emp, 0.5, 42),
            _salesperson_perf_multiplier(emp, 0.5, 43),
        )


class TestBuilderAppliesPerf:
    def _inputs(self):
        store_keys = np.array([0, 1], dtype=np.int32)
        assign_store = np.array([0, 0, 1], dtype=np.int32)
        assign_emp = np.array([40_000_001, 40_000_002, 40_000_003], dtype=np.int32)
        start = np.array(["2021-01-01"] * 3, dtype="datetime64[D]")
        end = np.array(["2025-12-31"] * 3, dtype="datetime64[D]")
        return store_keys, assign_store, assign_emp, start, end

    def test_perf_off_gives_flat_weights(self):
        sk, a_store, a_emp, s, e = self._inputs()
        eff = _build_salesperson_effective_by_store(
            store_keys=sk, assign_store=a_store, assign_emp=a_emp,
            assign_start=s, assign_end=e, perf_spread=0.0,
        )
        # FTE defaults to 1.0, none primary -> every weight is exactly 1.0.
        for _emp, _st, _en, w in eff.values():
            assert np.allclose(w, 1.0)

    def test_perf_on_varies_weights(self):
        sk, a_store, a_emp, s, e = self._inputs()
        eff = _build_salesperson_effective_by_store(
            store_keys=sk, assign_store=a_store, assign_emp=a_emp,
            assign_start=s, assign_end=e, perf_spread=0.8, perf_seed=42,
        )
        # Store 0 has two reps; with a spread their weights should differ.
        w0 = eff[0][3]
        assert w0.shape[0] == 2
        assert not np.allclose(w0[0], w0[1])

    def test_emp_weight_consistent_across_stores(self):
        # An employee assigned to two stores must carry the same multiplier in
        # both (weights equal because FTE/primary are identical here).
        store_keys = np.array([0, 1], dtype=np.int32)
        a_store = np.array([0, 1], dtype=np.int32)
        a_emp = np.array([40_555_000, 40_555_000], dtype=np.int32)
        s = np.array(["2021-01-01"] * 2, dtype="datetime64[D]")
        e = np.array(["2025-12-31"] * 2, dtype="datetime64[D]")
        eff = _build_salesperson_effective_by_store(
            store_keys=store_keys, assign_store=a_store, assign_emp=a_emp,
            assign_start=s, assign_end=e, perf_spread=0.7, perf_seed=11,
        )
        assert np.isclose(eff[0][3][0], eff[1][3][0])

    def test_historical_assignment_is_not_deweighted_by_latest_flag(self):
        """IsPrimary is a latest-segment snapshot, not an effective-date weight.

        A future transfer must not halve the employee's salesperson weight on
        the historical segment that preceded it.
        """
        store_keys = np.array([0, 1], dtype=np.int32)
        assign_store = np.array([0, 1], dtype=np.int32)
        assign_emp = np.array([40_555_000, 40_555_000], dtype=np.int32)
        start = np.array(["2021-01-01", "2023-01-01"], dtype="datetime64[D]")
        end = np.array(["2022-12-31", "2025-12-31"], dtype="datetime64[D]")
        eff = _build_salesperson_effective_by_store(
            store_keys=store_keys,
            assign_store=assign_store,
            assign_emp=assign_emp,
            assign_start=start,
            assign_end=end,
            assign_is_primary=np.array([False, True]),
            perf_spread=0.0,
        )
        assert eff[0][3][0] == eff[1][3][0] == 1.0


class TestSalespersonPerformanceConfig:
    def test_valid_parses(self):
        from src.engine.config.config_schema import SalesConfig, SalespersonPerformanceConfig

        s = SalesConfig.model_validate({"salesperson_performance": {"enabled": True, "spread": 0.7}})
        assert isinstance(s.salesperson_performance, SalespersonPerformanceConfig)
        assert s.salesperson_performance.spread == 0.7

    def test_default_is_none(self):
        from src.engine.config.config_schema import SalesConfig

        assert SalesConfig().salesperson_performance is None

    def test_key_typo_rejected(self):
        from pydantic import ValidationError

        from src.engine.config.config_schema import SalesConfig

        with pytest.raises(ValidationError):
            SalesConfig.model_validate({"salesperson_performance": {"spreed": 0.5}})
