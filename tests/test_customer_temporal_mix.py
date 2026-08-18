"""Phase 2 tests for customer-side temporal mix integration."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.engine.config.config_schema import MacroDemandConfig
from src.exceptions import SalesError
from src.facts.sales.prep.dimension_loaders import _load_customers
from src.facts.sales.prep.scd2_grid import _build_scd2_customer_versions
from src.facts.sales.prep.temporal_mix_plan import (
    build_customer_temporal_mix_plan,
)
from src.facts.sales.sales_logic.core.customer_sampling import (
    build_month_customer_pool,
    effective_customer_weights_for_month,
)


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


def _customer_fixture(order=None):
    keys = np.asarray([2, 9, 15, 21], dtype=np.int32)
    geography = np.asarray([1, 2, 3, 4], dtype=np.int32)
    gender = np.asarray(["Female", "Male", "Female", "Male"], dtype=object)
    mass = np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float64)
    if order is not None:
        keys = keys[order]
        geography = geography[order]
        gender = gender[order]
        mass = mass[order]
    geo_by_key = np.zeros(int(keys.max()) + 1, dtype=np.int32)
    geo_by_key[keys] = geography
    return SimpleNamespace(
        customer_keys=keys,
        customer_geo_key=geo_by_key,
        customer_gender_by_pool=gender,
        customer_base_weight=mass,
        customer_scd2_geography=None,
    )


def _correlations():
    return {
        "geo_to_country_id": np.asarray([0, 0, 0, 1, 1], dtype=np.int32),
        "country_names": np.asarray(["North", "South"], dtype=object),
    }


def test_customer_plan_is_deterministic_and_pool_order_independent():
    original = build_customer_temporal_mix_plan(
        _customer_fixture(),
        _correlations(),
        month_count=48,
        resolved_trend=_trend(),
        run_seed=77,
    )
    permutation = np.asarray([3, 0, 2, 1])
    reordered = build_customer_temporal_mix_plan(
        _customer_fixture(permutation),
        _correlations(),
        month_count=48,
        resolved_trend=_trend(),
        run_seed=77,
    )
    np.testing.assert_array_equal(
        original.customer_country_factors,
        reordered.customer_country_factors,
    )
    np.testing.assert_array_equal(
        original.customer_gender_factors,
        reordered.customer_gender_factors,
    )
    np.testing.assert_array_equal(
        original.customer_country_id_by_pool,
        reordered.customer_country_id_by_pool[np.argsort(permutation)],
    )
    np.testing.assert_array_equal(
        original.customer_gender_id_by_pool,
        reordered.customer_gender_id_by_pool[np.argsort(permutation)],
    )


def test_effective_weights_apply_both_axes_and_center_eligible_mass():
    base = np.asarray([1.0, 2.0, 3.0, 4.0])
    country_ids = np.asarray([0, 0, 1, 1], dtype=np.int32)
    gender_ids = np.asarray([0, 1, 0, 1], dtype=np.int32)
    country_factors = np.asarray([[2.0, 0.5]])
    gender_factors = np.asarray([[1.5, 0.75]])
    eligible = np.asarray([0, 1, 2, 3], dtype=np.int32)

    effective = effective_customer_weights_for_month(
        m_offset=0,
        month_start_day=20_000,
        eligible_idx=eligible,
        base_weight=base,
        country_id_by_pool=country_ids,
        gender_id_by_pool=gender_ids,
        country_factors=country_factors,
        gender_factors=gender_factors,
    )
    combined = country_factors[0, country_ids] * gender_factors[0, gender_ids]
    normalized_factor = effective / base
    np.testing.assert_allclose(
        np.dot(base, normalized_factor) / base.sum(),
        1.0,
        rtol=0.0,
        atol=1e-15,
    )
    np.testing.assert_allclose(
        normalized_factor / normalized_factor[0],
        combined / combined[0],
    )


def test_scd2_country_uses_geography_at_month_start():
    max_day = np.iinfo(np.int64).max
    starts = np.asarray([[0, 20_015], [0, max_day]], dtype=np.int64)
    geography = np.asarray([[1, 2], [1, 1]], dtype=np.int32)
    geo_to_country = np.asarray([-1, 0, 1], dtype=np.int32)
    kwargs = {
        "eligible_idx": np.asarray([0, 1], dtype=np.int32),
        "base_weight": np.ones(2),
        "country_id_by_pool": np.asarray([1, 0], dtype=np.int32),
        "gender_id_by_pool": np.zeros(2, dtype=np.int32),
        "country_factors": np.asarray([[2.0, 0.5], [2.0, 0.5]]),
        "gender_factors": np.ones((2, 1)),
        "customer_scd2_starts": starts,
        "customer_scd2_geography": geography,
        "geo_to_country_id": geo_to_country,
    }
    before_relocation = effective_customer_weights_for_month(
        m_offset=0,
        month_start_day=20_001,
        **kwargs,
    )
    after_relocation = effective_customer_weights_for_month(
        m_offset=1,
        month_start_day=20_032,
        **kwargs,
    )
    assert before_relocation[0] == before_relocation[1]
    assert after_relocation[0] < after_relocation[1]


def test_scd2_grid_carries_geography_for_each_customer_version(tmp_path):
    customers = pd.DataFrame(
        {
            "CustomerID": [1, 1, 2],
            "CustomerKey": [10, 20, 40],
            "GeographyKey": [1, 2, 4],
            "EffectiveStartDate": ["2020-01-01", "2022-01-01", "2020-01-01"],
            "EffectiveEndDate": ["2021-12-31", None, None],
        }
    )
    path = tmp_path / "customers.parquet"
    customers.to_parquet(path, index=False)

    result = _build_scd2_customer_versions(
        path,
        pool_customer_keys=np.asarray([20, 40], dtype=np.int32),
        pool_customer_ids=np.asarray([1, 2], dtype=np.int32),
        pool_customer_geography=np.asarray([2, 4], dtype=np.int32),
    )

    assert result is not None
    starts, keys, geography, key_to_pool_idx = result
    np.testing.assert_array_equal(keys[0], np.asarray([10, 20], dtype=np.int32))
    np.testing.assert_array_equal(geography[0], np.asarray([1, 2], dtype=np.int32))
    assert starts[0, 0] == 0
    assert key_to_pool_idx[20] == 0
    assert key_to_pool_idx[40] == 1


def test_customer_loader_rejects_gender_changes_across_scd2_versions(tmp_path):
    customers = pd.DataFrame(
        {
            "CustomerID": [1, 1],
            "CustomerKey": [10, 20],
            "CustomerStartDate": ["2020-01-01", "2022-01-01"],
            "CustomerEndDate": ["2021-12-31", None],
            "IsCurrent": [0, 1],
            "GeographyKey": [1, 2],
            "Gender": ["Female", "Male"],
        }
    )
    customers.to_parquet(tmp_path / "customers.parquet", index=False)

    with pytest.raises(SalesError, match="changes Gender"):
        _load_customers(tmp_path, {}, "2022-01-01", seed=7)


def test_sparse_customer_keys_use_dense_pool_lookup_for_repeat_cdf():
    keys = np.asarray([10, 20, 30], dtype=np.int32)
    lookup = np.full(31, -1, dtype=np.int32)
    lookup[keys] = np.arange(3, dtype=np.int32)
    weights = np.asarray([1.0, 2.0, 7.0])
    pool, cdf = build_month_customer_pool(
        m_offset=0,
        distinct_target=3,
        eligible_idx=np.arange(3, dtype=np.int32),
        customer_keys=keys,
        discovery_month=np.zeros(3, dtype=np.int64),
        base_weight=weights,
        end_month_norm=np.full(3, -1, dtype=np.int64),
        seed=5,
        key_to_pool_idx=lookup,
    )
    pool_positions = lookup[pool]
    expected = np.cumsum(weights[pool_positions] / weights.sum())
    expected[-1] = 1.0
    np.testing.assert_allclose(cdf, expected)


def test_forced_debut_cohort_is_unchanged_by_temporal_weights():
    keys = np.arange(1, 9, dtype=np.int32)
    discovery = np.asarray([2, 2, 2, 2, 0, 0, 0, 0], dtype=np.int64)
    common = {
        "m_offset": 2,
        "distinct_target": 6,
        "eligible_idx": np.arange(8, dtype=np.int32),
        "customer_keys": keys,
        "discovery_month": discovery,
        "end_month_norm": np.full(8, -1, dtype=np.int64),
        "seed": 11,
    }
    neutral, _ = build_month_customer_pool(
        base_weight=np.ones(8),
        **common,
    )
    shifted, _ = build_month_customer_pool(
        base_weight=np.asarray([0.01, 100, 0.01, 100, 100, 0.01, 100, 0.01]),
        **common,
    )
    debut = {1, 2, 3, 4}
    assert debut <= set(neutral.tolist())
    assert debut <= set(shifted.tolist())
    assert neutral.size == shifted.size == 6
