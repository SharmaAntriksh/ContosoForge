"""Tests for the sales pricing pipeline helpers.

These test the pure/stateless functions that don't require State to be bound.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.facts.sales.sales_models.pricing_pipeline import (
    _as_f64,
    _choose_step,
    _global_start_month_int,
    _parse_bands,
    _parse_endings,
    _quantize,
    _reset_caches,
    _safe_prob,
    _snap_discount,
    build_prices,
)
from src.exceptions import SalesError
from src.facts.sales.sales_logic.globals import State


class TestGlobalStartMonth:
    """Phase 1.3 / Finding #30: the inflation anchor is the *configured* dataset
    start (State.date_pool) — a single per-run epoch, never a per-chunk
    min(order_dates). So the inflation factor for a (product, month) is identical
    across chunks regardless of which order dates each chunk happens to contain."""

    def setup_method(self):
        State.reset()
        _reset_caches()

    def teardown_method(self):
        State.reset()
        _reset_caches()

    def test_anchors_to_date_pool_start(self):
        State.date_pool = np.arange(
            np.datetime64("2020-03-01"), np.datetime64("2022-01-01"),
            dtype="datetime64[D]")
        expected = int(np.datetime64("2020-03", "M").astype("int64"))
        assert _global_start_month_int() == expected

    def test_independent_of_order_dates_across_chunks(self):
        # Two "chunks" covering different sub-ranges must resolve the SAME anchor,
        # because it comes from the run-wide date_pool, not the chunk's own dates.
        State.date_pool = np.arange(
            np.datetime64("2021-01-01"), np.datetime64("2023-01-01"),
            dtype="datetime64[D]")
        anchor = _global_start_month_int()
        # anchor is memoized + purely date_pool-derived → stable across calls
        assert _global_start_month_int() == anchor
        assert anchor == int(np.datetime64("2021-01", "M").astype("int64"))

    def test_missing_date_pool_raises(self):
        State.date_pool = None
        with pytest.raises(SalesError, match="date_pool"):
            _global_start_month_int()


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
        # step keyed to the DISCOUNT magnitude: disc<=100 -> step 5; disc>100 -> step 25
        disc = np.array([12.0, 300.0])
        up = np.array([50.0, 5000.0])
        result = _snap_discount(disc, up, self._acfg())

        # every snapped discount must be a multiple of its (discount-band) step
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
        step = np.maximum(_choose_step(safe, acfg["d_band_max"], acfg["d_band_step"]), 1.0)
        snapped = np.floor(safe / step) * step

        # (a) on-grid: exact multiple of the per-row step
        np.testing.assert_array_equal(snapped % step, np.zeros_like(snapped))
        # (b) never exceeds the margin-safe ceiling
        assert np.all(snapped <= safe + 1e-9)
        # thin-margin row (ceiling 0.19 < step 1.0) collapses to 0 discount
        assert snapped[2] == 0.0

    def test_small_discount_on_expensive_item_not_zeroed(self):
        # Regression: the step is keyed to the discount magnitude, not UnitPrice,
        # so a small markdown on a high-priced item survives instead of flooring
        # to $0 (the "promoted line shows no discount" bug on low-ticket SKUs).
        acfg = {
            "enabled": True,
            "d_band_max": np.array([10.0, 50.0, 200.0, 1000.0, 1e18]),
            "d_band_step": np.array([1.0, 5.0, 10.0, 25.0, 50.0]),
            "d_round": "floor",
        }
        disc = np.array([3.0])   # a $3 markdown ...
        up = np.array([500.0])   # ... on a $500 item
        result = _snap_discount(disc, up, acfg)
        # disc 3 is in the <=10 band -> step 1 -> stays 3 (keyed to price it would floor to 0)
        assert result[0] == 3.0


# ===================================================================
# Phase 3.5 — markdown ↔ PromotionKey reconciliation
# ===================================================================

class TestPromotionReconciliation:
    """A discount is a consequence of a promotion: with reconciliation on, a
    "no promotion" row (PromotionKey == no_discount_key) never carries a markdown,
    and every promoted row draws from the *nonzero* ladder (so it does). With it
    off, the legacy independent markdown lottery is unchanged."""

    NO_DISCOUNT_KEY = 1

    def setup_method(self):
        State.reset()
        _reset_caches()

    def teardown_method(self):
        State.reset()
        _reset_caches()

    def _bind_state(self, *, reconcile: bool):
        # Plain-dict models_cfg (build_prices reads via .get); appearance off so
        # a nonzero drawn discount is not snapped away, inflation off (factor 1).
        State.models_cfg = {
            "pricing": {
                "inflation": {
                    "annual_rate": 0.0,
                    "month_volatility_sigma": 0.0,
                    "apply_with_scd2": True,
                },
                "appearance": {"enabled": False},
                "markdown": {
                    "enabled": True,
                    "max_pct_of_price": 0.50,
                    "min_net_price": 0.01,
                    "allow_negative_margin": False,
                    "reconcile_promotions": reconcile,
                    "ladder": [
                        {"kind": "none", "value": 0.0, "weight": 0.5},
                        {"kind": "pct", "value": 0.20, "weight": 0.5},
                    ],
                },
            }
        }
        State.product_scd2_active = False
        State.date_pool = np.array(["2022-01-01"], dtype="datetime64[D]")

    def _price_dict(self, n, up=100.0, uc=40.0):
        up_arr = np.full(n, up, dtype=np.float64)
        uc_arr = np.full(n, uc, dtype=np.float64)
        return {
            "final_unit_price": up_arr,
            "final_unit_cost": uc_arr,
            "discount_amt": np.zeros(n, dtype=np.float64),
            "final_net_price": up_arr.copy(),
        }

    def _run(self, *, reconcile, promo_keys, n=None):
        self._bind_state(reconcile=reconcile)
        n = int(n if n is not None else len(promo_keys))
        order_dates = np.full(n, np.datetime64("2022-06-15"), dtype="datetime64[D]")
        rng = np.random.default_rng(20260701)
        price = build_prices(
            rng, order_dates, np.ones(n, dtype=np.int64), self._price_dict(n),
            promo_keys=promo_keys, no_discount_key=self.NO_DISCOUNT_KEY,
        )
        return price["discount_amt"], price["final_net_price"]

    def test_reconcile_forward_and_converse(self):
        # Alternating no-promo / promo rows.
        n = 400
        promo_keys = np.where(np.arange(n) % 2 == 0, self.NO_DISCOUNT_KEY, 5).astype(np.int32)
        disc, net = self._run(reconcile=True, promo_keys=promo_keys)

        no_promo = promo_keys == self.NO_DISCOUNT_KEY
        promo = ~no_promo

        # Forward: no promotion -> exactly zero discount.
        assert np.all(disc[no_promo] == 0.0)
        # Converse: promoted lines all carry a real discount (nonzero ladder is
        # only pct=0.20, so up*0.20 = 20.0 on a $100 line, appearance off).
        assert np.all(disc[promo] > 0.0)
        np.testing.assert_allclose(disc[promo], 20.0)
        # NetPrice stays consistent with the reconciled discount.
        np.testing.assert_allclose(net, np.round(100.0 - disc, 2))

    def test_reconcile_off_is_promo_independent(self):
        # Legacy behavior: markdown is drawn independently of PromotionKey, so
        # some no-promotion rows still receive a discount (the pre-3.5 bug).
        n = 400
        promo_keys = np.full(n, self.NO_DISCOUNT_KEY, dtype=np.int32)
        disc, _ = self._run(reconcile=False, promo_keys=promo_keys)
        assert (disc > 0.0).any()

    def test_promo_keys_none_uses_legacy_path(self):
        # No promo_keys supplied (unit-test / non-pipeline callers) -> legacy
        # lottery even when reconcile defaults on.
        n = 400
        disc, _ = self._run(reconcile=True, promo_keys=None, n=n)
        # Full ladder includes nonzero entries, so some rows get a discount
        # regardless of any (absent) promotion assignment.
        assert (disc > 0.0).any()


# ===================================================================
# Phase 4.1 — deterministic posted price per (product, month)
# ===================================================================

class TestDeterministicPostedPrice:
    def setup_method(self):
        State.reset()
        _reset_caches()

    def teardown_method(self):
        State.reset()
        _reset_caches()

    def _bind(self, *, deterministic=True):
        State.models_cfg = {
            "pricing": {
                "inflation": {"annual_rate": 0.10, "month_volatility_sigma": 0.0,
                              "apply_with_scd2": True},
                "appearance": {
                    "enabled": True,
                    "deterministic": deterministic,
                    "unit_price": {
                        "rounding": "floor",
                        "endings": [{"value": 0.99, "weight": 1.0}],
                        "bands": [{"max": 100, "step": 1}, {"max": 1e18, "step": 5}],
                    },
                    "unit_cost": {
                        "rounding": "nearest",
                        "endings": [{"value": 0.0, "weight": 1.0}],
                        "bands": [{"max": 1e18, "step": 1}],
                    },
                    "discount": {"bands": [{"max": 1e18, "step": 1}]},
                },
                "markdown": {"enabled": False},
            }
        }
        State.product_scd2_active = False
        State.date_pool = np.array(["2022-01-01"], dtype="datetime64[D]")

    def _run(self, product_ids, base_prices, dates, *, deterministic=True, pass_ids=True):
        self._bind(deterministic=deterministic)
        base = np.asarray(base_prices, dtype=np.float64)
        n = base.size
        price = {
            "final_unit_price": base.copy(),
            "final_unit_cost": base * 0.4,
            "discount_amt": np.zeros(n),
            "final_net_price": base.copy(),
        }
        out = build_prices(
            np.random.default_rng(1),
            np.asarray(dates, dtype="datetime64[D]"),
            np.ones(n, dtype=np.int64), price,
            product_ids=(np.asarray(product_ids, dtype=np.int32) if pass_ids else None),
        )
        return out["final_unit_price"]

    def test_same_product_month_single_posted_price(self):
        n = 500
        pid = np.full(n, 7, dtype=np.int32)
        base = np.full(n, 53.0)             # a product's list price is fixed
        dates = np.array(["2022-06-15"] * n, dtype="datetime64[D]")
        up = self._run(pid, base, dates, deterministic=True)
        assert np.unique(np.round(up, 2)).size == 1, "posted price varies within (product, month)"

    def test_legacy_snap_varies_per_row(self):
        n = 500
        pid = np.full(n, 7, dtype=np.int32)
        base = np.full(n, 53.0)
        dates = np.array(["2022-06-15"] * n, dtype="datetime64[D]")
        up = self._run(pid, base, dates, deterministic=False)
        # stochastic per-row rounding straddles the step boundary -> >1 value
        assert np.unique(np.round(up, 2)).size > 1

    def test_product_ids_none_uses_legacy(self):
        n = 500
        pid = np.full(n, 7, dtype=np.int32)
        base = np.full(n, 53.0)
        dates = np.array(["2022-06-15"] * n, dtype="datetime64[D]")
        up = self._run(pid, base, dates, deterministic=True, pass_ids=False)
        assert np.unique(np.round(up, 2)).size > 1

    def test_distinct_products_can_differ(self):
        # Two products at the same base price / month can snap to different posted
        # prices (independent (product, month) hashes) — structure, not a bug.
        n = 2000
        pid = (np.arange(n) % 40).astype(np.int32)
        base = np.full(n, 53.0)
        dates = np.array(["2022-06-15"] * n, dtype="datetime64[D]")
        up = self._run(pid, base, dates, deterministic=True)
        # each product-month is internally single-valued...
        import pandas as pd
        g = pd.DataFrame({"pid": pid, "up": np.round(up, 2)}).groupby("pid")["up"].nunique()
        assert (g == 1).all()
        # ...but across products there is more than one posted price
        assert np.unique(np.round(up, 2)).size > 1
