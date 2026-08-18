"""Phase 3 tests for hierarchical product-side temporal mix."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.engine.config.config_schema import MacroDemandConfig
from src.exceptions import SalesError
from src.facts.sales.prep.dimension_loaders import _load_products
from src.facts.sales.prep.temporal_mix_plan import build_product_temporal_mix_plan
from src.facts.sales.sales_logic import State
from src.facts.sales.sales_logic import chunk_builder as cb


def _trend() -> MacroDemandConfig:
    return MacroDemandConfig.model_validate(
        {
            "noise_std": 0.08,
            "shock_probability": 0.08,
            "shock_impact": [-0.15, 0.20],
            "year_level_factors": {
                "mode": "once",
                "values": [1.0, 1.08, 1.18, 1.25],
            },
        }
    )


def _product_fixture(order=None):
    product_np = np.column_stack(
        (
            np.asarray([5, 11, 20, 31, 42, 55], dtype=np.int32),
            np.linspace(10.0, 60.0, 6),
            np.linspace(5.0, 30.0, 6),
        )
    )
    category = np.asarray([1, 1, 1, 2, 2, 2], dtype=np.int16)
    brand = np.asarray(["A", "A", "B", "A", "B", "B"], dtype=object)
    color = np.asarray(["Red", "Blue", "Red", "Red", "Blue", "Red"], dtype=object)
    popularity = np.asarray([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], dtype=np.float64)
    if order is not None:
        product_np = product_np[order]
        category = category[order]
        brand = brand[order]
        color = color[order]
        popularity = popularity[order]
    return SimpleNamespace(
        product_np=product_np,
        product_category_key=category,
        product_brand_by_pool=brand,
        product_color_by_pool=color,
        product_popularity=popularity,
    )


@pytest.fixture(autouse=True)
def _reset_sales_state():
    State.reset()
    cb.reset_worker_caches()
    yield
    State.reset()
    cb.reset_worker_caches()


def test_product_plan_is_deterministic_and_pool_order_independent():
    original = build_product_temporal_mix_plan(
        _product_fixture(),
        month_count=48,
        resolved_trend=_trend(),
        run_seed=91,
    )
    permutation = np.asarray([5, 0, 3, 1, 4, 2])
    reordered = build_product_temporal_mix_plan(
        _product_fixture(permutation),
        month_count=48,
        resolved_trend=_trend(),
        run_seed=91,
    )
    for field in (
        "product_category_factors",
        "product_brand_factors",
        "product_color_factors",
    ):
        np.testing.assert_array_equal(getattr(original, field), getattr(reordered, field))
    inverse = np.argsort(permutation)
    for field in (
        "product_category_id_by_pool",
        "product_brand_id_by_pool",
        "product_color_id_by_pool",
    ):
        np.testing.assert_array_equal(
            getattr(original, field),
            getattr(reordered, field)[inverse],
        )


def test_product_residual_layers_are_mass_centered_within_category():
    prod = _product_fixture()
    plan = build_product_temporal_mix_plan(
        prod,
        month_count=36,
        resolved_trend=_trend(),
        run_seed=17,
    )
    mass = prod.product_popularity

    category_mass = np.bincount(
        plan.product_category_id_by_pool,
        weights=mass,
        minlength=plan.product_category_factors.shape[1],
    )
    np.testing.assert_allclose(
        plan.product_category_factors @ category_mass / category_mass.sum(),
        1.0,
        atol=1e-12,
    )

    for ids, factors in (
        (plan.product_brand_id_by_pool, plan.product_brand_factors),
        (plan.product_color_id_by_pool, plan.product_color_factors),
    ):
        for category_id in np.unique(plan.product_category_id_by_pool):
            product_mask = plan.product_category_id_by_pool == category_id
            group_ids = np.unique(ids[product_mask])
            group_mass = np.asarray(
                [mass[product_mask & (ids == group_id)].sum() for group_id in group_ids]
            )
            np.testing.assert_allclose(
                factors[:, group_ids] @ group_mass / group_mass.sum(),
                1.0,
                atol=1e-12,
            )


def test_product_weight_cache_separates_same_calendar_month_across_years():
    State.product_popularity = np.asarray([1.0, 2.0, 3.0, 4.0])
    State.product_seasonality = np.zeros(4, dtype=np.int8)
    State.product_category_id_by_pool = np.asarray([0, 0, 1, 1], dtype=np.int32)
    State.product_brand_id_by_pool = np.zeros(4, dtype=np.int32)
    State.product_color_id_by_pool = np.zeros(4, dtype=np.int32)
    State.product_category_factors = np.asarray([[2.0, 0.5], [0.5, 2.0]])
    State.product_brand_factors = np.ones((2, 1))
    State.product_color_factors = np.ones((2, 1))

    jan_2020 = np.asarray(["2020-01-01"], dtype="datetime64[D]")
    jan_2021 = np.asarray(["2021-01-01"], dtype="datetime64[D]")
    first = cb._build_product_weight_for_month(jan_2020, 0, cal_month=1)
    second = cb._build_product_weight_for_month(jan_2021, 1, cal_month=1)

    assert set(cb._CACHES.prodweight) == {0, 1}
    assert not np.array_equal(first, second)
    np.testing.assert_allclose(first.sum(), 10.0, atol=1e-12)
    np.testing.assert_allclose(second.sum(), 10.0, atol=1e-12)


def test_store_product_cdf_cache_uses_relative_month():
    product_np = np.zeros((3, 3), dtype=np.float64)
    store_products = [np.asarray([0, 1, 2], dtype=np.int32)]
    stores = np.zeros(40, dtype=np.int32)
    orders = np.arange(1, 41, dtype=np.int64)
    lines = np.ones(40, dtype=np.int16)
    cache = {}

    cb._sample_products_per_store(
        np.random.default_rng(2),
        stores,
        store_products,
        product_np,
        product_weight=np.asarray([9.0, 1.0, 1.0]),
        _cdf_cache=cache,
        _cal_month=1,
        m_offset=0,
        order_numbers=orders,
        line_numbers=lines,
    )
    cb._sample_products_per_store(
        np.random.default_rng(2),
        stores,
        store_products,
        product_np,
        product_weight=np.asarray([1.0, 1.0, 9.0]),
        _cdf_cache=cache,
        _cal_month=1,
        m_offset=12,
        order_numbers=orders,
        line_numbers=lines,
    )

    assert (0, -1, 0) in cache
    assert (0, -1, 12) in cache


def test_product_loader_rejects_taxonomy_changes_across_scd2_versions(tmp_path):
    products = pd.DataFrame(
        {
            "ProductID": [1, 1],
            "ProductKey": [10, 20],
            "ListPrice": [20.0, 22.0],
            "UnitCost": [10.0, 11.0],
            "Brand": ["A", "B"],
            "Color": ["Red", "Red"],
            "SubcategoryKey": [1, 1],
            "IsCurrent": [0, 1],
            "EffectiveStartDate": ["2020-01-01", "2022-01-01"],
            "EffectiveEndDate": ["2021-12-31", None],
        }
    )
    products.to_parquet(tmp_path / "products.parquet", index=False)

    with pytest.raises(SalesError, match="changes taxonomy"):
        _load_products(
            tmp_path,
            {},
            seed=7,
            start_date="2022-01-01",
            end_date="2022-12-31",
        )
