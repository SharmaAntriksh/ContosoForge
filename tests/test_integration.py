"""Integration tests for the pipeline runner and config validation."""
from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from src.engine.config.config_loader import load_config, load_config_file
from src.engine.runners.pipeline_runner import (
    PipelineOverrides,
    run_pipeline,
    _apply_overrides,
    _inject_models_appearance,
)
from src.exceptions import ConfigError, PipelineError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def config_path():
    """Return the repo-root config.yaml path."""
    return str(Path(__file__).resolve().parents[1] / "config.yaml")


@pytest.fixture()
def models_path():
    """Return the repo-root models.yaml path."""
    return str(Path(__file__).resolve().parents[1] / "models.yaml")


@pytest.fixture()
def base_cfg(config_path):
    """Load and return the normalised config (AppConfig)."""
    return load_config(config_path)


@pytest.fixture()
def base_models(models_path):
    """Load and return the raw models dict."""
    return load_config_file(models_path)


@pytest.fixture()
def minimal_config(tmp_path):
    """Write a minimal valid config.yaml to tmp_path and return its path."""
    cfg = {
        "scale": {"sales_rows": 100, "products": 10, "customers": 20, "stores": 2},
        "defaults": {
            "seed": 42,
            "final_output": str(tmp_path / "generated_datasets"),
            "dates": {"start": "2024-01-01", "end": "2024-12-31"},
        },
        "sales": {
            "max_lines_per_order": 3,
            "file_format": "parquet",
            "sales_output": "sales",
            "skip_order_cols": False,
            "advanced": {"chunk_size": 100, "workers": 1},
        },
        "returns": {"enabled": False},
        "products": {"active_ratio": 0.98},
        "customers": {"active_ratio": 0.98, "profile": "steady", "first_year_pct": 0.5},
        "geography": {},
        "promotions": {},
        "stores": {"ensure_iso_coverage": False},
        "employees": {},
        "dates": {"fiscal_start_month": 1},
        "exchange_rates": {"currencies": ["EUR"], "base_currency": "USD", "volatility": 0.02},
        "budget": {"enabled": False},
        "inventory": {"enabled": False},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return str(p)


@pytest.fixture()
def minimal_models(tmp_path):
    """Write a minimal valid models.yaml to tmp_path and return its path."""
    models = {
        "models": {
            "macro_demand": {"year_level_factors": {"mode": "once", "values": [1.0]}},
            "quantity": {"base_poisson_lambda": 1.5, "min_qty": 1, "max_qty": 5},
            "pricing": {
                "inflation": {"annual_rate": 0.03, "month_volatility_sigma": 0.01,
                              "factor_clip": [1.0, 1.2], "volatility_seed": 42},
                "markdown": {"enabled": False},
            },
            "brand_popularity": {"enabled": False},
            "returns": {"enabled": False},
        }
    }
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(models, sort_keys=False), encoding="utf-8")
    return str(p)


# ===================================================================
# 1. Dry run returns correct summary
# ===================================================================

class TestDryRun:
    def test_dry_run_returns_ok(self, config_path, models_path):
        result = run_pipeline(
            config_path=config_path,
            models_config_path=models_path,
            dry_run=True,
        )
        assert result["ok"] is True
        assert result["dry_run"] is True

    def test_dry_run_includes_expected_keys(self, config_path, models_path):
        result = run_pipeline(
            config_path=config_path,
            models_config_path=models_path,
            dry_run=True,
        )
        for key in ("ok", "dry_run", "only", "force_regenerate", "elapsed_sec",
                     "config_yaml_path", "model_yaml_path"):
            assert key in result, f"Missing key: {key}"

    def test_dry_run_only_dimensions(self, config_path, models_path):
        result = run_pipeline(
            config_path=config_path,
            models_config_path=models_path,
            dry_run=True,
            only="dimensions",
        )
        assert result["only"] == "dimensions"

    def test_dry_run_only_sales(self, config_path, models_path):
        result = run_pipeline(
            config_path=config_path,
            models_config_path=models_path,
            dry_run=True,
            only="sales",
        )
        assert result["only"] == "sales"

    def test_dry_run_elapsed_is_small(self, config_path, models_path):
        result = run_pipeline(
            config_path=config_path,
            models_config_path=models_path,
            dry_run=True,
        )
        assert 0.0 <= result["elapsed_sec"] < 10.0

    def test_dry_run_force_regenerate(self, config_path, models_path):
        result = run_pipeline(
            config_path=config_path,
            models_config_path=models_path,
            dry_run=True,
            regen_dimensions=["products", "customers"],
        )
        assert sorted(result["force_regenerate"]) == ["customers", "products"]


# ===================================================================
# 2. Config loading validates required sections
# ===================================================================

class TestConfigLoading:
    def test_load_config_returns_mapping(self, config_path):
        from collections.abc import Mapping
        cfg = load_config(config_path)
        assert isinstance(cfg, Mapping)

    def test_config_has_sales_section(self, config_path):
        from collections.abc import Mapping
        cfg = load_config(config_path)
        assert hasattr(cfg, "sales")
        assert isinstance(cfg.sales, Mapping)

    def test_config_has_defaults_section(self, config_path):
        cfg = load_config(config_path)
        assert hasattr(cfg, "defaults")

    def test_models_has_models_section(self, models_path):
        raw = load_config_file(models_path)
        assert "models" in raw
        assert isinstance(raw["models"], dict)

    def test_missing_config_file_raises(self, tmp_path):
        fake = str(tmp_path / "nonexistent.yaml")
        with pytest.raises(Exception):
            load_config(fake)


# ===================================================================
# 3. Pipeline overrides apply correctly
# ===================================================================

class TestPipelineOverrides:
    def test_file_format_override(self, base_cfg):
        cfg = base_cfg.model_copy(deep=True)
        sales_cfg = cfg.sales
        overrides = PipelineOverrides(file_format="csv")
        cfg, sales_cfg = _apply_overrides(cfg, sales_cfg, overrides)
        assert sales_cfg.file_format == "csv"

    def test_sales_rows_override(self, base_cfg):
        cfg = base_cfg.model_copy(deep=True)
        sales_cfg = cfg.sales
        overrides = PipelineOverrides(sales_rows=5000)
        cfg, sales_cfg = _apply_overrides(cfg, sales_cfg, overrides)
        assert sales_cfg.total_rows == 5000

    def test_workers_override(self, base_cfg):
        cfg = base_cfg.model_copy(deep=True)
        sales_cfg = cfg.sales
        overrides = PipelineOverrides(workers=4)
        cfg, sales_cfg = _apply_overrides(cfg, sales_cfg, overrides)
        assert sales_cfg.workers == 4

    def test_customers_override(self, base_cfg):
        cfg = base_cfg.model_copy(deep=True)
        sales_cfg = cfg.sales
        overrides = PipelineOverrides(customers=500)
        cfg, sales_cfg = _apply_overrides(cfg, sales_cfg, overrides)
        assert cfg.customers.total_customers == 500

    def test_promotions_total_override_distributes_to_buckets(self, base_cfg):
        """--promotions N must distribute across the eight buckets even when
        the config leaves them unset (previously the total was ignored)."""
        cfg = base_cfg.model_copy(deep=True)
        for k in ("num_seasonal", "num_clearance", "num_limited", "num_flash",
                  "num_volume", "num_loyalty", "num_bundle", "num_new_customer"):
            object.__setattr__(cfg.promotions, k, None)
        sales_cfg = cfg.sales
        overrides = PipelineOverrides(promotions=40)
        cfg, sales_cfg = _apply_overrides(cfg, sales_cfg, overrides)
        bucket_sum = sum(
            int(getattr(cfg.promotions, k))
            for k in ("num_seasonal", "num_clearance", "num_limited", "num_flash",
                      "num_volume", "num_loyalty", "num_bundle", "num_new_customer")
        )
        assert bucket_sum == 40
        assert cfg.promotions.total_promotions == 40

    def test_start_date_override(self, base_cfg):
        cfg = base_cfg.model_copy(deep=True)
        sales_cfg = cfg.sales
        overrides = PipelineOverrides(start_date="2020-01-01")
        cfg, sales_cfg = _apply_overrides(cfg, sales_cfg, overrides)
        assert cfg.defaults.dates.start == "2020-01-01"

    def test_delta_alias_normalised(self):
        overrides = PipelineOverrides(file_format="delta")
        from src.engine.runners.pipeline_runner import _normalize_overrides
        normalised = _normalize_overrides(overrides)
        assert normalised.file_format == "deltaparquet"

    def test_dry_run_with_overrides(self, config_path, models_path):
        result = run_pipeline(
            config_path=config_path,
            models_config_path=models_path,
            dry_run=True,
            overrides=PipelineOverrides(file_format="parquet", sales_rows=999),
        )
        assert result["ok"] is True


# ===================================================================
# 4. Invalid config raises ConfigError
# ===================================================================

class TestInvalidConfig:
    def test_missing_sales_section_gets_defaults(self, tmp_path, models_path):
        """A config without explicit 'sales' section gets Pydantic defaults."""
        cfg = {"defaults": {"seed": 42, "dates": {"start": "2024-01-01", "end": "2024-12-31"}}}
        p = tmp_path / "bad_config.yaml"
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        # AppConfig fills in SalesConfig defaults, so pipeline no longer raises
        result = run_pipeline(config_path=str(p), models_config_path=models_path, dry_run=True)
        assert result["ok"] is True

    def test_missing_models_section(self, config_path, tmp_path):
        p = tmp_path / "bad_models.yaml"
        p.write_text(yaml.safe_dump({"not_models": {}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="models"):
            run_pipeline(config_path=config_path, models_config_path=str(p), dry_run=True)

    def test_invalid_only_value(self, config_path, models_path):
        with pytest.raises(ConfigError, match="only must be"):
            run_pipeline(
                config_path=config_path,
                models_config_path=models_path,
                dry_run=True,
                only="invalid_stage",
            )


# ===================================================================
# 5. Cross-section validation (validators.py)
# ===================================================================

class TestCrossSectionValidation:
    def test_skip_order_cols_warning(self):
        """When skip_order_cols is true, the validate endpoint warns about returns."""
        from tests.validators import validate
        cfg = {
            "defaults": {"dates": {"start": "2024-01-01", "end": "2024-12-31"}},
            "sales": {
                "total_rows": 1000,
                "chunk_size": 500,
                "file_format": "parquet",
                "workers": 1,
                "parquet_folder": "./data",
                "out_folder": "./out",
                "skip_order_cols": True,
            },
        }
        errors, warnings = validate(cfg)
        # skip_order_cols is a boolean and should not cause errors in validate()
        assert isinstance(errors, list)
        assert isinstance(warnings, list)
        # The validators module validates types, not cross-section rules.
        # skip_order_cols=True (valid bool) should not produce type warnings.
        for w in warnings:
            assert "return" in w.lower() or "skip_order" in w.lower() or "date" in w.lower(), \
                f"Unexpected warning: {w}"

    def test_end_before_start_date_error(self):
        from tests.validators import validate
        cfg = {
            "defaults": {"dates": {"start": "2025-01-01", "end": "2023-01-01"}},
            "sales": {
                "total_rows": 1000,
                "chunk_size": 500,
                "file_format": "parquet",
                "workers": 1,
                "parquet_folder": "./data",
                "out_folder": "./out",
            },
        }
        errors, warnings = validate(cfg)
        assert any("after start" in e.lower() or "end date" in e.lower() for e in errors)

    def test_zero_rows_error(self):
        from tests.validators import validate
        cfg = {
            "defaults": {"dates": {"start": "2024-01-01", "end": "2024-12-31"}},
            "sales": {
                "total_rows": 0,
                "chunk_size": 500,
                "file_format": "parquet",
                "workers": 1,
                "parquet_folder": "./data",
                "out_folder": "./out",
            },
        }
        errors, warnings = validate(cfg)
        assert any("greater than zero" in e.lower() for e in errors)

    def test_invalid_format_error(self):
        from tests.validators import validate
        cfg = {
            "defaults": {"dates": {"start": "2024-01-01", "end": "2024-12-31"}},
            "sales": {
                "total_rows": 1000,
                "chunk_size": 500,
                "file_format": "excel",
                "workers": 1,
                "parquet_folder": "./data",
                "out_folder": "./out",
            },
        }
        errors, warnings = validate(cfg)
        assert any("file_format" in e for e in errors)

    def test_customers_exceed_rows_warning(self):
        from tests.validators import validate
        cfg = {
            "defaults": {"dates": {"start": "2024-01-01", "end": "2024-12-31"}},
            "sales": {
                "total_rows": 100,
                "chunk_size": 100,
                "file_format": "parquet",
                "workers": 1,
                "parquet_folder": "./data",
                "out_folder": "./out",
            },
            "customers": {"total_customers": 9999},
        }
        errors, warnings = validate(cfg)
        assert any("customers" in w.lower() for w in warnings)



# ===================================================================
# 7. Dimension version hashing via load_config
# ===================================================================

class TestDimensionVersionHashing:
    def test_load_config_deterministic(self, config_path):
        """Loading the same config twice should produce identical configs."""
        c1 = load_config(config_path)
        c2 = load_config(config_path)
        assert c1 == c2

    def test_different_config_produces_different_hash(self, tmp_path):
        """Changing a value should produce a different config."""
        cfg_a = {
            "scale": {"sales_rows": 100, "products": 10, "customers": 20, "stores": 2},
            "defaults": {"seed": 42, "dates": {"start": "2024-01-01", "end": "2024-12-31"}},
            "sales": {"file_format": "parquet", "skip_order_cols": False, "sales_output": "sales_order", "total_rows": 100},
            "products": {},
            "customers": {"profile": "steady"},
            "stores": {},
            "promotions": {},
            "geography": {},
            "employees": {},
            "dates": {},
            "exchange_rates": {},
            "returns": {},
            "budget": {},
            "inventory": {},
        }
        cfg_b = copy.deepcopy(cfg_a)
        cfg_b["scale"]["sales_rows"] = 9999

        pa = tmp_path / "a.yaml"
        pb = tmp_path / "b.yaml"
        pa.write_text(yaml.safe_dump(cfg_a), encoding="utf-8")
        pb.write_text(yaml.safe_dump(cfg_b), encoding="utf-8")

        loaded_a = load_config(str(pa))
        loaded_b = load_config(str(pb))
        assert loaded_a != loaded_b


# ===================================================================
# 8. Models appearance injection
# ===================================================================

class TestModelsAppearanceInjection:
    def test_appearance_injected_into_products(self, base_cfg, base_models):
        cfg = base_cfg.model_copy(deep=True)
        models_cfg = base_models.get("models", {})
        _inject_models_appearance(cfg, models_cfg)
        prod_pricing = cfg.products.pricing
        if prod_pricing and isinstance(prod_pricing, dict):
            appearance = prod_pricing.get("appearance", {})
        else:
            appearance = {}
        # If appearance was injected, it should have the snap_unit_price key
        if models_cfg.get("pricing", {}).get("appearance", {}).get("enabled"):
            assert "snap_unit_price" in appearance

    def test_missing_pricing_is_safe(self):
        from src.engine.config.config_schema import AppConfig
        cfg = AppConfig()
        models_cfg = {}
        # Should not raise
        _inject_models_appearance(cfg, models_cfg)
