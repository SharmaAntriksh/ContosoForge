"""Tests for trend preset system.

Trend presets in ``trend_presets.py`` are the single source of business
shape — controlling revenue curve, customer lifecycle, and demand behavior.
"""
from __future__ import annotations

import pytest

from src.engine.config.config_schema import ModelsInnerConfig
from src.exceptions import ConfigError
from src.utils.config_merge import deep_merge
from src.utils.trend_presets import (
    VALID_TRENDS,
    _PROFILE_TO_TREND,
    get_trend_names,
    get_trend_defaults,
    resolve_trend_preset,
)


# ===================================================================
# deep_merge
# ===================================================================

class TestDeepMerge:
    def test_overrides_win(self):
        base = {"a": 1, "b": 2}

        result = deep_merge(base, {"b": 99})

        assert result == {"a": 1, "b": 99}

    def test_nested_dict_merged(self):
        base = {"a": {"x": 1, "y": 2}}

        result = deep_merge(base, {"a": {"y": 99, "z": 3}})

        assert result["a"] == {"x": 1, "y": 99, "z": 3}

    def test_non_dict_override_replaces(self):
        base = {"a": {"x": 1}}

        result = deep_merge(base, {"a": "replaced"})

        assert result["a"] == "replaced"

    def test_new_keys_added(self):
        result = deep_merge({"a": 1}, {"b": 2})

        assert result == {"a": 1, "b": 2}

    def test_base_not_mutated(self):
        base = {"a": 1}

        deep_merge(base, {"a": 2})

        assert base["a"] == 1


# ===================================================================
# Trend preset definitions integrity
# ===================================================================

class TestTrendPresetDefinitions:
    def test_all_expected_trends_exist(self):
        expected = {
            "steady-growth", "strong-growth", "gradual-growth", "hockey-stick",
            "decline", "boom-and-bust", "recession-recovery", "seasonal-dominant",
            "plateau", "volatile", "double-dip", "new-market-entry",
            "seasonal-with-growth", "slow-decline", "stagnation",
        }
        assert expected == VALID_TRENDS

    def test_each_trend_has_lifecycle_and_customers(self):
        for name in VALID_TRENDS:
            preset = get_trend_defaults(name)
            assert "lifecycle" in preset, f"{name} missing lifecycle"
            assert "customers" in preset, f"{name} missing customers"

    def test_lifecycle_has_expected_keys(self):
        required = {"enable_churn", "base_monthly_churn", "initial_active_customers"}
        for name in VALID_TRENDS:
            preset = get_trend_defaults(name)
            lc = preset["lifecycle"]
            missing = required - set(lc.keys())
            assert not missing, f"{name} lifecycle missing: {missing}"

    def test_customers_seasonal_spikes_valid(self):
        for name in VALID_TRENDS:
            preset = get_trend_defaults(name)
            spikes = preset["customers"].get("seasonal_spikes", [])
            for spike in spikes:
                assert 1 <= spike["month"] <= 12, f"{name}: invalid spike month"
                assert spike["boost"] > 0, f"{name}: boost must be positive"

    def test_profile_to_trend_mapping_valid(self):
        for profile, trend in _PROFILE_TO_TREND.items():
            if trend is not None:
                assert trend in VALID_TRENDS, f"Profile '{profile}' maps to unknown trend '{trend}'"


# ===================================================================
# Trend preset API
# ===================================================================

class TestTrendPresetAPI:
    def test_get_trend_names_sorted(self):
        names = get_trend_names()
        assert names == sorted(names)
        assert len(names) == len(VALID_TRENDS)

    def test_get_trend_defaults_valid(self):
        for name in VALID_TRENDS:
            result = get_trend_defaults(name)
            assert result is not None

    def test_get_trend_defaults_unknown_raises(self):
        with pytest.raises(ConfigError, match="Unknown trend preset"):
            get_trend_defaults("nonexistent")


# ===================================================================
# resolve_trend_preset
# ===================================================================

class TestResolveTrendPreset:
    def _cfg(self, d):
        from src.engine.config.config_schema import AppConfig
        return AppConfig.model_validate(d)

    def _models(self, d=None):
        return ModelsInnerConfig.model_validate(d or {})

    def test_unknown_trend_raises(self):
        models = self._models({"macro_demand": {"trend": "unknown"}})

        with pytest.raises(ConfigError, match="Unknown trend preset"):
            resolve_trend_preset(models)

    def test_steady_growth_injects_lifecycle(self):
        cfg = self._cfg({"customers": {"total_customers": 100}})
        models = self._models({"macro_demand": {"trend": "steady-growth"}})

        resolve_trend_preset(models, cfg=cfg)

        assert cfg.customers.lifecycle is not None
        assert cfg.customers.lifecycle["enable_churn"] is True
        assert cfg.customers.lifecycle["base_monthly_churn"] == 0.003

    def test_stagnation_no_churn(self):
        cfg = self._cfg({"customers": {"total_customers": 100}})
        models = self._models({"macro_demand": {"trend": "stagnation"}})

        resolve_trend_preset(models, cfg=cfg)

        assert cfg.customers.lifecycle["enable_churn"] is False
        assert cfg.customers.lifecycle["base_monthly_churn"] == 0.0

    def test_customers_demand_injected(self):
        cfg = self._cfg({"customers": {"total_customers": 100}})
        models = self._models({"macro_demand": {"trend": "gradual-growth"}})

        resolve_trend_preset(models, cfg=cfg)

        assert models.customers is not None
        assert models.customers.distinct_ratio == 0.55

    def test_explicit_lifecycle_override_wins(self):
        cfg = self._cfg({"customers": {
            "total_customers": 100,
            "lifecycle": {"base_monthly_churn": 0.99},
        }})
        models = self._models({"macro_demand": {"trend": "steady-growth"}})

        resolve_trend_preset(models, cfg=cfg)

        assert cfg.customers.lifecycle["base_monthly_churn"] == 0.99

    def test_first_year_pct_applied(self):
        cfg = self._cfg({"customers": {
            "total_customers": 100,
            "first_year_pct": 0.50,
        }})
        models = self._models({"macro_demand": {"trend": "gradual-growth"}})

        resolve_trend_preset(models, cfg=cfg)

        assert cfg.customers.lifecycle["initial_active_customers"] == 0.50

    def test_first_year_pct_out_of_range_raises(self):
        cfg = self._cfg({"customers": {
            "total_customers": 100,
            "first_year_pct": 0.01,
        }})
        models = self._models({"macro_demand": {"trend": "gradual-growth"}})

        with pytest.raises(ConfigError, match="between 0.05 and 1.0"):
            resolve_trend_preset(models, cfg=cfg)

    def test_deprecated_profile_maps_to_trend(self):
        """Backward compat: customers.profile still works via _PROFILE_TO_TREND."""
        cfg = self._cfg({"customers": {"total_customers": 100, "profile": "gradual"}})
        models = self._models({"macro_demand": {}})

        resolve_trend_preset(models, cfg=cfg, profile_name="gradual")

        assert cfg.customers.lifecycle is not None
        assert cfg.customers.lifecycle["enable_churn"] is True

    def test_no_trend_no_profile_returns_unchanged(self):
        cfg = self._cfg({"customers": {"total_customers": 100}})
        models = self._models({"macro_demand": {}})

        resolve_trend_preset(models, cfg=cfg)

        # No trend set, no profile set — nothing injected
        assert cfg.customers.lifecycle is None

    def test_seasonal_spikes_are_pydantic_after_resolve(self):
        """After resolve_trend_preset, seasonal_spikes should be SeasonalSpikeConfig objects."""
        from src.engine.config.config_schema import SeasonalSpikeConfig

        models = self._models({"macro_demand": {"trend": "steady-growth"}})
        resolve_trend_preset(models)

        spikes = models.customers.seasonal_spikes
        assert spikes is not None and len(spikes) > 0
        for spike in spikes:
            assert isinstance(spike, SeasonalSpikeConfig), (
                f"Expected SeasonalSpikeConfig, got {type(spike).__name__}"
            )
            assert 1 <= spike.month <= 12
            assert spike.boost > 0

    def test_seasonal_spikes_parseable_by_chunk_builder_logic(self):
        """The real spike normalizer must handle both dicts and Pydantic models identically."""
        from src.engine.config.config_schema import SeasonalSpikeConfig
        from src.facts.sales.sales_helpers import _normalize_seasonal_spikes

        # Pydantic path (trend preset resolved)
        pydantic_spikes = [
            SeasonalSpikeConfig(month=11, boost=0.40),
            SeasonalSpikeConfig(month=12, boost=0.25),
        ]
        # Dict path (fallback defaults)
        dict_spikes = [
            {"month": 11, "boost": 0.40},
            {"month": 12, "boost": 0.25},
        ]

        pydantic_result = _normalize_seasonal_spikes(pydantic_spikes)
        dict_result = _normalize_seasonal_spikes(dict_spikes)

        assert pydantic_result == dict_result
        assert pydantic_result == {11: 0.40, 12: 0.25}

    def test_seasonal_spikes_normalizer_drops_invalid_entries(self):
        """Out-of-range months and missing month/boost are dropped (both shapes)."""
        from src.engine.config.config_schema import SeasonalSpikeConfig
        from src.facts.sales.sales_helpers import _normalize_seasonal_spikes

        assert _normalize_seasonal_spikes(None) == {}
        assert _normalize_seasonal_spikes([]) == {}
        assert _normalize_seasonal_spikes([{"month": 0, "boost": 0.1}]) == {}
        assert _normalize_seasonal_spikes([{"month": 13, "boost": 0.1}]) == {}
        assert _normalize_seasonal_spikes([{"month": 5}]) == {}
        assert _normalize_seasonal_spikes([{"boost": 0.1}]) == {}
        # Valid Pydantic + valid dict mixed, last-write-wins on duplicate month.
        mixed = [SeasonalSpikeConfig(month=6, boost=0.2), {"month": 6, "boost": 0.3}]
        assert _normalize_seasonal_spikes(mixed) == {6: 0.3}

    def test_all_presets_inject_lifecycle(self):
        for trend_name in VALID_TRENDS:
            cfg = self._cfg({"customers": {"total_customers": 100}})
            models = self._models({"macro_demand": {"trend": trend_name}})

            resolve_trend_preset(models, cfg=cfg)

            assert cfg.customers.lifecycle is not None, f"{trend_name} did not inject lifecycle"
            assert models.customers is not None, f"{trend_name} did not inject customers demand"
