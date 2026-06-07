"""Tests for the sales pricing pipeline helpers.

These test the pure/stateless functions that don't require State to be bound.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.facts.sales.sales_models.pricing_pipeline import (
    _as_f64,
    _choose_step,
    _parse_bands,
    _parse_endings,
    _quantize,
    _safe_prob,
    _snap_discount,
)


# ===================================================================
# _as_f64
# ===================================================================

class TestAsF64:
    def test_basic_conversion(self):
        result = _as_f64([1.0, 2.0, 3.0])

        np.testing.assert_array_equal(result, [1.0, 2.0, 3.0])
        assert result.dtype == np.float64

    def test_nan_replaced_with_zero(self):
        result = _as_f64([1.0, float("nan"), 3.0])

        np.testing.assert_array_equal(result, [1.0, 0.0, 3.0])

    def test_inf_replaced_with_zero(self):
        result = _as_f64([1.0, float("inf"), float("-inf")])

        np.testing.assert_array_equal(result, [1.0, 0.0, 0.0])

    def test_integer_input(self):
        result = _as_f64([1, 2, 3])

        np.testing.assert_array_equal(result, [1.0, 2.0, 3.0])

    def test_empty_array(self):
        result = _as_f64([])

        assert result.shape == (0,)


# ===================================================================
# _safe_prob
# ===================================================================

class TestSafeProb:
    def test_normalizes_weights(self):
        result = _safe_prob(np.array([3.0, 1.0]))

        np.testing.assert_allclose(result, [0.75, 0.25])

    def test_sums_to_one(self):
        result = _safe_prob(np.array([1.0, 2.0, 3.0, 4.0]))

        assert abs(result.sum() - 1.0) < 1e-9

    def test_all_zeros_uniform(self):
        result = _safe_prob(np.array([0.0, 0.0, 0.0]))
        expected = np.full(3, 1.0 / 3.0)

        np.testing.assert_allclose(result, expected)

    def test_negative_weights_clipped(self):
        result = _safe_prob(np.array([-1.0, 2.0]))

        assert result[0] == 0.0
        assert abs(result.sum() - 1.0) < 1e-9

    def test_nan_treated_as_zero(self):
        result = _safe_prob(np.array([float("nan"), 2.0]))

        assert abs(result.sum() - 1.0) < 1e-9

    def test_single_weight(self):
        result = _safe_prob(np.array([5.0]))

        np.testing.assert_array_equal(result, [1.0])


# ===================================================================
# _parse_bands
# ===================================================================

class TestParseBands:
    def test_sorted_by_max(self):
        maxs, steps = _parse_bands(
            [{"max": 500, "step": 10}, {"max": 100, "step": 5}],
            default=[(1e18, 1.0)],
        )

        np.testing.assert_array_equal(maxs, [100.0, 500.0])
        np.testing.assert_array_equal(steps, [5.0, 10.0])

    def test_fallback_to_default(self):
        maxs, steps = _parse_bands([], default=[(1e18, 0.01)])

        np.testing.assert_array_equal(maxs, [1e18])
        np.testing.assert_array_equal(steps, [0.01])

    def test_skips_non_dict_entries(self):
        maxs, steps = _parse_bands(
            [{"max": 100, "step": 5}, "bad", 42],
            default=[(1e18, 1.0)],
        )

        assert len(maxs) == 1
        assert maxs[0] == 100.0

    def test_skips_entries_missing_keys(self):
        maxs, steps = _parse_bands(
            [{"max": 100}, {"step": 5}, {"max": 200, "step": 10}],
            default=[(1e18, 1.0)],
        )

        assert len(maxs) == 1
        assert maxs[0] == 200.0

    def test_skips_zero_step(self):
        maxs, steps = _parse_bands(
            [{"max": 100, "step": 0}, {"max": 200, "step": 10}],
            default=[(1e18, 1.0)],
        )

        assert len(maxs) == 1
        assert maxs[0] == 200.0

    def test_skips_negative_max(self):
        maxs, steps = _parse_bands(
            [{"max": -100, "step": 5}, {"max": 200, "step": 10}],
            default=[(1e18, 1.0)],
        )

        assert len(maxs) == 1

    def test_none_input_uses_default(self):
        maxs, steps = _parse_bands(None, default=[(50.0, 1.0)])

        np.testing.assert_array_equal(maxs, [50.0])


# ===================================================================
# _parse_endings
# ===================================================================

class TestParseEndings:
    def test_basic_parsing(self):
        vals, probs = _parse_endings(
            [{"value": 0.99, "weight": 3.0}, {"value": 0.50, "weight": 1.0}],
            default_if_missing=False,
        )

        assert len(vals) == 2
        np.testing.assert_allclose(vals, [0.99, 0.50])
        assert abs(probs.sum() - 1.0) < 1e-9

    def test_empty_list_no_default(self):
        vals, probs = _parse_endings([], default_if_missing=False)

        assert vals is None
        assert probs is None

    def test_empty_list_with_default(self):
        vals, probs = _parse_endings([], default_if_missing=True)

        assert vals is not None
        assert len(vals) > 0
        assert abs(probs.sum() - 1.0) < 1e-9

    def test_zero_weight_skipped(self):
        vals, probs = _parse_endings(
            [{"value": 0.99, "weight": 1.0}, {"value": 0.50, "weight": 0.0}],
            default_if_missing=False,
        )

        assert len(vals) == 1
        assert vals[0] == 0.99

    def test_value_clamped_to_099(self):
        vals, probs = _parse_endings(
            [{"value": 5.0, "weight": 1.0}],
            default_if_missing=False,
        )

        assert vals[0] == 0.99

    def test_value_clamped_to_zero(self):
        vals, probs = _parse_endings(
            [{"value": -1.0, "weight": 1.0}],
            default_if_missing=False,
        )

        assert vals[0] == 0.0


# ===================================================================
# _choose_step
# ===================================================================

class TestChooseStep:
    def test_picks_correct_band(self):
        band_max = np.array([100.0, 500.0, 1e18])
        band_step = np.array([5.0, 10.0, 50.0])
        prices = np.array([50.0, 250.0, 1000.0])

        steps = _choose_step(prices, band_max, band_step)

        np.testing.assert_array_equal(steps, [5.0, 10.0, 50.0])

    def test_boundary_value(self):
        """Value exactly at band boundary stays in current band (searchsorted side='left')."""
        band_max = np.array([100.0, 500.0])
        band_step = np.array([5.0, 10.0])
        prices = np.array([100.0])

        steps = _choose_step(prices, band_max, band_step)

        np.testing.assert_array_equal(steps, [5.0])

    def test_value_above_all_bands(self):
        """Value exceeding all bands uses last step."""
        band_max = np.array([100.0, 500.0])
        band_step = np.array([5.0, 10.0])
        prices = np.array([9999.0])

        steps = _choose_step(prices, band_max, band_step)

        np.testing.assert_array_equal(steps, [10.0])

    def test_zero_value(self):
        band_max = np.array([100.0])
        band_step = np.array([5.0])
        prices = np.array([0.0])

        steps = _choose_step(prices, band_max, band_step)

        np.testing.assert_array_equal(steps, [5.0])


# ===================================================================
# _quantize
# ===================================================================

class TestQuantize:
    def test_floor(self):
        x = np.array([47.0, 123.0, 99.0])
        step = np.array([10.0, 50.0, 25.0])

        result = _quantize(x, step, "floor")

        np.testing.assert_array_equal(result, [40.0, 100.0, 75.0])

    def test_nearest_rounds_up(self):
        x = np.array([47.0, 126.0])
        step = np.array([10.0, 50.0])

        result = _quantize(x, step, "nearest")

        np.testing.assert_array_equal(result, [50.0, 150.0])

    def test_nearest_rounds_down(self):
        x = np.array([42.0, 110.0])
        step = np.array([10.0, 50.0])

        result = _quantize(x, step, "nearest")

        np.testing.assert_array_equal(result, [40.0, 100.0])

    def test_exact_value_unchanged(self):
        x = np.array([50.0, 100.0])
        step = np.array([10.0, 50.0])

        result_floor = _quantize(x, step, "floor")
        result_nearest = _quantize(x, step, "nearest")

        np.testing.assert_array_equal(result_floor, [50.0, 100.0])
        np.testing.assert_array_equal(result_nearest, [50.0, 100.0])

    def test_zero_value(self):
        x = np.array([0.0])
        step = np.array([10.0])

        result = _quantize(x, step, "floor")

        np.testing.assert_array_equal(result, [0.0])


# ===================================================================
# _snap_discount + SM-1 margin re-fix grid invariant
# ===================================================================

class TestSnapDiscount:
    def _acfg(self, d_round="nearest"):
        return {
            "enabled": True,
            "d_band_max": np.array([100.0, 1e18]),
            "d_band_step": np.array([5.0, 25.0]),
            "d_round": d_round,
        }

    def test_disabled_passthrough(self):
        disc = np.array([12.37, 3.10])
        up = np.array([50.0, 50.0])
        result = _snap_discount(disc, up, {"enabled": False})

        np.testing.assert_array_equal(result, disc)

    def test_snaps_to_band_step(self):
        # up=50 -> band step 5; up=500 -> band step 25
        disc = np.array([12.0, 60.0])
        up = np.array([50.0, 500.0])
        result = _snap_discount(disc, up, self._acfg())

        # every snapped discount must be a multiple of its band step
        np.testing.assert_array_equal(result % np.array([5.0, 25.0]), [0.0, 0.0])

    def test_clipped_to_unit_price(self):
        disc = np.array([999.0])
        up = np.array([40.0])
        result = _snap_discount(disc, up, self._acfg())

        assert result[0] <= up[0]

    def test_sm1_floor_snap_stays_on_grid_and_margin_safe(self):
        """SM-1: the margin re-fix floors the margin-safe discount onto the grid.

        Mirrors the inline logic in ``build_prices``: ``safe = up - uc - 0.01``
        floored to the band step must (a) land on a grid multiple and (b) never
        exceed the margin-safe ceiling, so the re-fixed discount can't re-violate
        the positive-margin guarantee.
        """
        up = np.array([50.0, 120.0, 30.0])
        uc = np.array([20.0, 40.0, 29.80])
        acfg = self._acfg()

        safe = np.maximum(up - uc - 0.01, 0.0)
        step = np.maximum(_choose_step(up, acfg["d_band_max"], acfg["d_band_step"]), 1.0)
        snapped = np.floor(safe / step) * step

        # (a) on-grid: exact multiple of the per-row step
        np.testing.assert_array_equal(snapped % step, np.zeros_like(snapped))
        # (b) never exceeds the margin-safe ceiling
        assert np.all(snapped <= safe + 1e-9)
        # thin-margin row (ceiling 0.19 < step 1.0) collapses to 0 discount
        assert snapped[2] == 0.0
