"""Phase 4 analytical calibration for temporal entity-mix dynamics."""
from __future__ import annotations

from dataclasses import dataclass
import time
from types import SimpleNamespace

import numpy as np
import pytest

from src.engine.config.config_schema import MacroDemandConfig
from src.facts.sales.prep.temporal_mix_plan import (
    TEMPORAL_MIX_LAYER_INTENSITY,
    build_customer_temporal_mix_plan,
    build_product_temporal_mix_plan,
)
from src.facts.sales.sales_models.temporal_mix import build_temporal_factors
from src.facts.sales.sales_models.temporal_mix_diagnostics import (
    RankPersistenceMetrics,
    rank_persistence_metrics,
)
from src.utils.trend_presets import get_trend_defaults


@dataclass(frozen=True)
class _AxisFixture:
    names: tuple[str, ...]
    mass: tuple[float, ...]
    intensity: float
    parents: tuple[str, ...] | None = None


_AXES = {
    "category": _AxisFixture(
        tuple(f"Category {index}" for index in range(8)),
        (100, 97, 94, 90, 86, 82, 77, 72),
        TEMPORAL_MIX_LAYER_INTENSITY["product_category"],
    ),
    "brand": _AxisFixture(
        tuple(f"Category {parent} / Brand {child}" for parent in range(3) for child in range(5)),
        tuple(float(100 - 4 * child - 2 * parent) for parent in range(3) for child in range(5)),
        TEMPORAL_MIX_LAYER_INTENSITY["product_brand_within_category"],
        tuple(f"Category {parent}" for parent in range(3) for _ in range(5)),
    ),
    "color": _AxisFixture(
        tuple(f"Category {parent} / Color {child}" for parent in range(3) for child in range(4)),
        tuple(float(100 - 5 * child - 2 * parent) for parent in range(3) for child in range(4)),
        TEMPORAL_MIX_LAYER_INTENSITY["product_color_within_category"],
        tuple(f"Category {parent}" for parent in range(3) for _ in range(4)),
    ),
    "country": _AxisFixture(
        tuple(f"Country {index}" for index in range(10)),
        (100, 98, 95, 92, 89, 86, 82, 78, 73, 68),
        TEMPORAL_MIX_LAYER_INTENSITY["customer_country"],
    ),
    "gender": _AxisFixture(
        ("Female", "Male"),
        (51, 49),
        TEMPORAL_MIX_LAYER_INTENSITY["customer_gender"],
    ),
}

_TREND_FAMILIES = {
    "stable": "stagnation",
    "growth": "strong-growth",
    "decline": "decline",
    "cyclical": "boom-and-bust",
    "volatile": "volatile",
}


def _resolved_trend(name: str) -> MacroDemandConfig:
    raw = get_trend_defaults(name)
    raw.pop("lifecycle", None)
    raw.pop("customers", None)
    if isinstance(raw.get("monthly_seasonality"), str):
        raw.pop("monthly_seasonality")
    return MacroDemandConfig.model_validate(raw)


def _annual_metrics(axis_name: str, fixture: _AxisFixture, trend_name: str) -> RankPersistenceMetrics:
    factors = build_temporal_factors(
        f"calibration-{axis_name}",
        fixture.names,
        fixture.mass,
        120,
        _resolved_trend(trend_name),
        20260818,
        parent_group=fixture.parents,
        intensity_scale=fixture.intensity,
    )
    monthly_values = factors * np.asarray(fixture.mass, dtype=np.float64)[None, :]
    annual_values = monthly_values.reshape(10, 12, len(fixture.names)).sum(axis=1)
    return rank_persistence_metrics(
        annual_values,
        entity_names=fixture.names,
        top_n=min(5, len(fixture.names)),
    )


@pytest.mark.parametrize("axis_name", tuple(_AXES))
def test_volatile_mix_is_less_rank_static_than_stable_mix(axis_name: str) -> None:
    fixture = _AXES[axis_name]
    stable = _annual_metrics(axis_name, fixture, _TREND_FAMILIES["stable"])
    volatile = _annual_metrics(axis_name, fixture, _TREND_FAMILIES["volatile"])

    assert volatile.mean_position_persistence <= stable.mean_position_persistence
    assert volatile.mean_adjacent_spearman <= stable.mean_adjacent_spearman
    assert volatile.adjacent_rank_swaps >= stable.adjacent_rank_swaps


@pytest.mark.parametrize("family", tuple(_TREND_FAMILIES))
@pytest.mark.parametrize("axis_name", tuple(_AXES))
def test_calibrated_mix_remains_positive_and_bounded(axis_name: str, family: str) -> None:
    metrics = _annual_metrics(axis_name, _AXES[axis_name], _TREND_FAMILIES[family])
    assert metrics.minimum_share > 0.0
    assert metrics.maximum_share < 0.60
    assert 0.0 < metrics.mean_concentration < 1.0


def test_volatile_mix_moves_every_reported_axis() -> None:
    metrics = {
        name: _annual_metrics(name, fixture, _TREND_FAMILIES["volatile"])
        for name, fixture in _AXES.items()
    }
    assert all(value.adjacent_rank_swaps > 0 for value in metrics.values())
    assert all(value.mean_position_persistence < 1.0 for value in metrics.values())


def test_long_range_large_group_factor_plan_is_compact_and_fast() -> None:
    group_count = 1_000
    month_count = 240
    names = tuple(f"Group {index:04d}" for index in range(group_count))
    mass = np.linspace(1.0, 2.0, group_count)

    started = time.perf_counter()
    factors = build_temporal_factors(
        "calibration-large-plan",
        names,
        mass,
        month_count,
        _resolved_trend("volatile"),
        20260818,
    )
    elapsed = time.perf_counter() - started

    assert factors.shape == (month_count, group_count)
    assert factors.nbytes == month_count * group_count * np.dtype(np.float64).itemsize
    assert np.isfinite(factors).all()
    assert elapsed < 10.0


def test_large_customer_and_product_plans_remain_compact() -> None:
    month_count = 240
    customer_count = 250_000
    customer_keys = np.arange(1, customer_count + 1, dtype=np.int32)
    geography_by_key = np.zeros(customer_count + 1, dtype=np.int32)
    geography_by_key[customer_keys] = customer_keys % 53 + 1
    customers = SimpleNamespace(
        customer_keys=customer_keys,
        customer_geo_key=geography_by_key,
        customer_gender_by_pool=np.where(customer_keys % 2, "Female", "Male"),
        customer_base_weight=np.linspace(0.5, 1.5, customer_count),
        customer_scd2_geography=None,
    )
    geo_to_country = np.zeros(54, dtype=np.int32)
    geo_to_country[1:] = np.arange(53, dtype=np.int32) % 21
    correlations = {
        "geo_to_country_id": geo_to_country,
        "country_names": np.asarray(
            [f"Country {index:02d}" for index in range(21)], dtype=object
        ),
    }

    product_count = 100_000
    product_rows = np.arange(product_count, dtype=np.int32)
    products = SimpleNamespace(
        product_np=np.column_stack(
            (
                product_rows + 1,
                np.full(product_count, 20.0),
                np.full(product_count, 10.0),
            )
        ),
        product_category_key=product_rows % 20,
        product_brand_by_pool=np.asarray(
            [f"Brand {value:03d}" for value in product_rows % 500], dtype=object
        ),
        product_color_by_pool=np.asarray(
            [f"Color {value:02d}" for value in product_rows % 20], dtype=object
        ),
        product_popularity=np.linspace(1.0, 100.0, product_count),
    )

    started = time.perf_counter()
    customer_plan = build_customer_temporal_mix_plan(
        customers,
        correlations,
        month_count=month_count,
        resolved_trend=_resolved_trend("volatile"),
        run_seed=20260818,
    )
    product_plan = build_product_temporal_mix_plan(
        products,
        month_count=month_count,
        resolved_trend=_resolved_trend("volatile"),
        run_seed=20260818,
    )
    elapsed = time.perf_counter() - started

    customer_bytes = sum(
        value.nbytes
        for value in (
            customer_plan.customer_country_id_by_pool,
            customer_plan.customer_gender_id_by_pool,
            customer_plan.geo_to_customer_country_id,
            customer_plan.customer_country_factors,
            customer_plan.customer_gender_factors,
        )
    )
    product_bytes = sum(
        value.nbytes
        for value in (
            product_plan.product_category_id_by_pool,
            product_plan.product_brand_id_by_pool,
            product_plan.product_color_id_by_pool,
            product_plan.product_category_factors,
            product_plan.product_brand_factors,
            product_plan.product_color_factors,
        )
    )
    full_customer_matrix_bytes = month_count * customer_count * 8
    full_product_matrix_bytes = month_count * product_count * 8

    assert customer_bytes < full_customer_matrix_bytes / 20
    assert product_bytes < full_product_matrix_bytes / 20
    assert elapsed < 15.0
