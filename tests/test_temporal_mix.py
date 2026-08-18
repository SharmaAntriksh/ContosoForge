"""Phase 1 tests for the pure temporal mix engine and diagnostics."""
from __future__ import annotations

import numpy as np
import pytest

from src.engine.config.config_schema import MacroDemandConfig
from src.exceptions import SalesError
from src.facts.sales.sales_models.temporal_mix import (
    build_temporal_factors,
    derive_temporal_mix_profile,
)
from src.facts.sales.sales_models.temporal_mix_diagnostics import rank_persistence_metrics


GROUPS = ["Alpha", "Beta", "Delta", "Gamma", "Omega", "Sigma"]
MASS = np.asarray([30.0, 28.0, 18.0, 12.0, 8.0, 4.0])


def _steady_trend() -> MacroDemandConfig:
    return MacroDemandConfig.model_validate(
        {
            "noise_std": 0.025,
            "shock_probability": 0.015,
            "shock_impact": [-0.06, 0.05],
            "year_level_factors": {
                "mode": "once",
                "values": [1.0, 1.04, 1.08, 1.12, 1.16, 1.20],
            },
        }
    )


def _volatile_trend() -> MacroDemandConfig:
    return MacroDemandConfig.model_validate(
        {
            "noise_std": 0.14,
            "shock_probability": 0.15,
            "shock_impact": [-0.30, 0.35],
            "year_level_factors": {
                "mode": "repeat",
                "values": [1.0, 1.20, 0.70, 1.25, 0.68, 1.15, 0.73, 0.52],
            },
        }
    )


def _build(trend: MacroDemandConfig, months: int = 120) -> np.ndarray:
    return build_temporal_factors(
        "test-axis",
        GROUPS,
        MASS,
        months,
        trend,
        90210,
    )


def test_same_inputs_are_bit_identical():
    first = _build(_steady_trend())
    second = _build(_steady_trend())
    assert np.array_equal(first, second)


def test_arbitrary_timeline_and_extension_preserve_prefix():
    short = _build(_volatile_trend(), months=19)
    long = _build(_volatile_trend(), months=241)
    assert short.shape == (19, len(GROUPS))
    assert long.shape == (241, len(GROUPS))
    assert np.array_equal(short, long[:19])


def test_group_input_order_does_not_change_named_factors():
    permutation = np.asarray([4, 1, 5, 0, 3, 2])
    original = _build(_steady_trend(), months=48)
    reordered = build_temporal_factors(
        "test-axis",
        [GROUPS[i] for i in permutation],
        MASS[permutation],
        48,
        _steady_trend(),
        90210,
    )
    restored = reordered[:, np.argsort(permutation)]
    assert np.array_equal(original, restored)


@pytest.mark.parametrize("trend", [_steady_trend(), _volatile_trend()])
def test_factors_are_bounded_positive_finite_and_mass_centered(trend):
    factors = _build(trend)
    profile = derive_temporal_mix_profile(trend)
    weighted_mean = (factors @ MASS) / MASS.sum()
    assert np.all(np.isfinite(factors))
    assert np.all(factors > 0.0)
    assert factors.min() >= profile.factor_floor
    assert factors.max() <= profile.factor_ceiling
    np.testing.assert_allclose(weighted_mean, 1.0, rtol=0.0, atol=2e-15)


def test_parent_centered_residuals_preserve_each_parent_mass():
    parents = ["Core", "Core", "Core", "Specialty", "Specialty", "Specialty"]
    factors = build_temporal_factors(
        "brand-within-category",
        GROUPS,
        MASS,
        84,
        _volatile_trend(),
        77,
        parent_group=parents,
    )
    for parent in set(parents):
        positions = np.asarray([i for i, value in enumerate(parents) if value == parent])
        parent_mass = MASS[positions]
        weighted_mean = (factors[:, positions] @ parent_mass) / parent_mass.sum()
        np.testing.assert_allclose(weighted_mean, 1.0, rtol=0.0, atol=2e-15)


def test_volatile_trend_has_more_temporal_movement_than_steady_trend():
    steady = _build(_steady_trend())
    volatile = _build(_volatile_trend())
    steady_movement = float(np.std(np.diff(np.log(steady), axis=0)))
    volatile_movement = float(np.std(np.diff(np.log(volatile), axis=0)))
    assert volatile_movement > steady_movement * 1.75


def test_fixed_seed_rank_baseline_is_calmer_for_steady_trend():
    steady = rank_persistence_metrics(
        _build(_steady_trend()) * MASS[None, :],
        entity_names=GROUPS,
    )
    volatile = rank_persistence_metrics(
        _build(_volatile_trend()) * MASS[None, :],
        entity_names=GROUPS,
    )
    assert steady.mean_position_persistence > volatile.mean_position_persistence
    assert steady.mean_adjacent_spearman > volatile.mean_adjacent_spearman
    assert steady.adjacent_rank_swaps < volatile.adjacent_rank_swaps
    assert steady.top_entity_tenure > volatile.top_entity_tenure


def test_rank_diagnostics_distinguish_persistent_and_moving_series():
    persistent = np.asarray(
        [
            [60, 30, 10],
            [58, 31, 11],
            [59, 30, 11],
            [57, 32, 11],
        ],
        dtype=np.float64,
    )
    moving = np.asarray(
        [
            [60, 30, 10],
            [25, 65, 10],
            [20, 25, 55],
            [60, 25, 15],
        ],
        dtype=np.float64,
    )
    stable_metrics = rank_persistence_metrics(persistent, entity_names=["A", "B", "C"])
    moving_metrics = rank_persistence_metrics(moving, entity_names=["A", "B", "C"])
    assert stable_metrics.mean_position_persistence == 1.0
    assert stable_metrics.mean_adjacent_spearman == 1.0
    assert stable_metrics.adjacent_rank_swaps == 0
    assert stable_metrics.top_entity_tenure == 4
    assert moving_metrics.mean_position_persistence < stable_metrics.mean_position_persistence
    assert moving_metrics.mean_adjacent_spearman < stable_metrics.mean_adjacent_spearman
    assert moving_metrics.adjacent_rank_swaps > 0
    assert moving_metrics.top_entity_tenure == 1


def test_diagnostics_are_input_order_independent():
    factors = _build(_volatile_trend(), months=36)
    values = factors * MASS[None, :]
    original = rank_persistence_metrics(values, entity_names=GROUPS, top_n=4)
    permutation = np.asarray([2, 5, 1, 4, 0, 3])
    reordered = rank_persistence_metrics(
        values[:, permutation],
        entity_names=[GROUPS[i] for i in permutation],
        top_n=4,
    )
    assert original == reordered


def test_matrix_memory_is_groups_times_months_only():
    factors = _build(_volatile_trend(), months=240)
    assert factors.nbytes == 240 * len(GROUPS) * np.dtype(np.float64).itemsize


@pytest.mark.parametrize(
    ("groups", "mass", "months", "parents", "message"),
    [
        ([], [], 12, None, "at least one"),
        (["A", "A"], [1, 1], 12, None, "unique"),
        (["A", "B"], [1], 12, None, "align"),
        (["A", "B"], [1, 0], 12, None, "positive"),
        (["A"], [1], -1, None, "non-negative"),
        (["A", "B"], [1, 1], 12, ["P"], "align"),
    ],
)
def test_invalid_inputs_raise_sales_error(groups, mass, months, parents, message):
    with pytest.raises(SalesError, match=message):
        build_temporal_factors(
            "axis",
            groups,
            mass,
            months,
            _steady_trend(),
            1,
            parent_group=parents,
        )
