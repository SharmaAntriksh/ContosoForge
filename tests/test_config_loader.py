"""Tests for config loading, normalization, and validation."""
from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from src.engine.config.config import (
    _clamp01,
    _distribute_scale,
    _expand_merge_block,
    _expand_partition_by,
    _expand_region_mix,
    _expand_products_pricing,
    _fold_facts_enabled,
    _parse_date,
    normalize_defaults,
    normalize_sales_config,
)
from src.exceptions import ConfigError
from src.engine.config.config_schema import ModelsConfig


# ===================================================================
# _parse_date
# ===================================================================

class TestParseDate:
    def test_iso_string(self):
        assert _parse_date("2023-01-15", "test") == date(2023, 1, 15)

    def test_slash_format(self):
        assert _parse_date("2023/01/15", "test") == date(2023, 1, 15)

    def test_date_object_passthrough(self):
        d = date(2024, 6, 1)

        assert _parse_date(d, "test") is d

    def test_datetime_extracts_date(self):
        from datetime import datetime
        dt = datetime(2024, 6, 1, 12, 30)

        assert _parse_date(dt, "test") == date(2024, 6, 1)

    def test_invalid_raises_valueerror(self):
        with pytest.raises(ConfigError, match="Cannot parse"):
            _parse_date("not-a-date", "test")

    def test_empty_string_raises(self):
        with pytest.raises(ConfigError, match="Cannot parse"):
            _parse_date("", "test")


# ===================================================================
# _clamp01
# ===================================================================

class TestClamp01:
    def test_within_range(self):
        assert _clamp01(0.5) == 0.5

    def test_below_zero(self):
        assert _clamp01(-0.5) == 0.0

    def test_above_one(self):
        assert _clamp01(1.5) == 1.0

    def test_boundary_values(self):
        assert _clamp01(0.0) == 0.0
        assert _clamp01(1.0) == 1.0


# ===================================================================
# normalize_defaults
# ===================================================================

class TestNormalizeDefaults:
    def test_valid_defaults(self):
        cfg = {"defaults": {"dates": {"start": "2021-01-01", "end": "2025-12-31"}}}

        result = normalize_defaults(cfg)

        assert result["defaults"]["dates"]["start"] == "2021-01-01"
        assert result["defaults"]["dates"]["end"] == "2025-12-31"

    def test_missing_defaults_raises(self):
        with pytest.raises(ConfigError, match="Missing 'defaults'"):
            normalize_defaults({})

    def test_underscore_alias(self):
        cfg = {"_defaults": {"dates": {"start": "2021-01-01", "end": "2025-12-31"}}}

        result = normalize_defaults(cfg)

        assert "defaults" in result
        assert "_defaults" not in result

    def test_start_after_end_raises(self):
        cfg = {"defaults": {"dates": {"start": "2025-01-01", "end": "2020-01-01"}}}

        with pytest.raises(ConfigError, match="start must be < end"):
            normalize_defaults(cfg)

    def test_equal_start_end_raises(self):
        cfg = {"defaults": {"dates": {"start": "2023-01-01", "end": "2023-01-01"}}}

        with pytest.raises(ConfigError, match="start must be < end"):
            normalize_defaults(cfg)

    def test_missing_dates_section_raises(self):
        cfg = {"defaults": {"seed": 42}}

        with pytest.raises(ConfigError, match="defaults.dates"):
            normalize_defaults(cfg)

    def test_missing_start_raises(self):
        cfg = {"defaults": {"dates": {"end": "2025-12-31"}}}

        with pytest.raises(ConfigError, match="start or defaults.dates.end"):
            normalize_defaults(cfg)

    def test_non_dict_defaults_raises(self):
        cfg = {"defaults": "not a dict"}

        with pytest.raises(ConfigError, match="Invalid defaults"):
            normalize_defaults(cfg)


# ===================================================================
# normalize_sales_config
# ===================================================================

class TestNormalizeSalesConfig:
    def _base(self, **overrides):
        cfg = {
            "file_format": "parquet",
            "total_rows": 1000,
            "skip_order_cols": False,
            "parquet_folder": "./data/parquet_dims",
            "out_folder": "./data/fact_out",
            "delta_output_folder": "./data/fact_out/delta",
        }
        cfg.update(overrides)
        return cfg

    def test_valid_parquet(self):
        result = normalize_sales_config(self._base())

        assert result["file_format"] == "parquet"
        assert result["total_rows"] == 1000

    def test_valid_csv(self):
        result = normalize_sales_config(self._base(file_format="csv"))

        assert result["file_format"] == "csv"

    def test_valid_deltaparquet(self):
        result = normalize_sales_config(self._base(file_format="deltaparquet"))

        assert result["file_format"] == "deltaparquet"
        assert result["partition_enabled"] is True
        assert result["partition_cols"] == ["Year", "Month"]

    def test_missing_format_raises(self):
        cfg = self._base()
        del cfg["file_format"]

        with pytest.raises(ConfigError, match="file_format is required"):
            normalize_sales_config(cfg)

    def test_invalid_format_raises(self):
        with pytest.raises(ConfigError, match="file_format must be one of"):
            normalize_sales_config(self._base(file_format="excel"))

    def test_format_case_insensitive(self):
        result = normalize_sales_config(self._base(file_format="PARQUET"))

        assert result["file_format"] == "parquet"

    def test_zero_rows_raises(self):
        with pytest.raises(ConfigError, match="positive integer"):
            normalize_sales_config(self._base(total_rows=0))

    def test_negative_rows_raises(self):
        with pytest.raises(ConfigError, match="positive integer"):
            normalize_sales_config(self._base(total_rows=-100))

    def test_csv_ignores_parquet_keys(self):
        cfg = normalize_sales_config(self._base(
            file_format="csv", row_group_size=500000,
        ))

        assert "row_group_size" in cfg["_ignored_keys"]

    def test_csv_ignores_delta_keys(self):
        cfg = normalize_sales_config(self._base(
            file_format="csv", partition_enabled=True,
        ))

        assert "partition_enabled" in cfg["_ignored_keys"]

    def test_parquet_ignores_delta_keys(self):
        cfg = normalize_sales_config(self._base(
            file_format="parquet", partition_enabled=True,
        ))

        assert "partition_enabled" in cfg["_ignored_keys"]

    def test_chunk_size_coerced_to_int(self):
        cfg = normalize_sales_config(self._base(chunk_size="500000"))

        assert cfg["chunk_size"] == 500000
        assert isinstance(cfg["chunk_size"], int)

    def test_negative_chunk_size_raises(self):
        with pytest.raises(ConfigError, match="positive integer"):
            normalize_sales_config(self._base(chunk_size=-1))

    def test_partitioning_block_expanded(self):
        cfg = normalize_sales_config(self._base(
            file_format="deltaparquet",
            partitioning={"enabled": True, "columns": ["Year"]},
        ))

        assert cfg["partition_enabled"] is True
        assert cfg["partition_cols"] == ["Year"]


# ===================================================================
# _distribute_scale
# ===================================================================

class TestDistributeScale:
    def test_sales_rows(self):
        cfg = _distribute_scale({"scale": {"sales_rows": 5000}})

        assert cfg["sales"]["total_rows"] == 5000

    def test_customers(self):
        cfg = _distribute_scale({"scale": {"customers": 200}})

        assert cfg["customers"]["total_customers"] == 200

    def test_stores(self):
        cfg = _distribute_scale({"scale": {"stores": 10}})

        assert cfg["stores"]["num_stores"] == 10

    def test_products(self):
        cfg = _distribute_scale({"scale": {"products": 500}})

        assert cfg["products"]["num_products"] == 500

    def test_section_level_wins(self):
        """Existing section-level value should not be overwritten."""
        cfg = _distribute_scale({
            "scale": {"sales_rows": 5000},
            "sales": {"total_rows": 999},
        })

        assert cfg["sales"]["total_rows"] == 999

    def test_promotions_scale(self):
        cfg = _distribute_scale({
            "scale": {"promotions": {"seasonal": 11, "clearance": 4}},
        })

        assert cfg["promotions"]["num_seasonal"] == 11
        assert cfg["promotions"]["num_clearance"] == 4

    def test_no_scale_section(self):
        cfg = _distribute_scale({"sales": {"total_rows": 100}})

        assert cfg["sales"]["total_rows"] == 100

    def test_products_dict_format(self):
        """scale.products as dict with rows and catalog."""
        cfg = _distribute_scale({
            "scale": {"products": {"catalog": "contoso", "rows": 2500}},
        })

        assert cfg["products"]["num_products"] == 2500
        assert cfg["products"]["catalog"] == "contoso"

    def test_products_dict_rows_only(self):
        cfg = _distribute_scale({
            "scale": {"products": {"rows": 3000}},
        })

        assert cfg["products"]["num_products"] == 3000
        assert "catalog" not in cfg["products"]

    def test_products_dict_catalog_only(self):
        cfg = _distribute_scale({
            "scale": {"products": {"catalog": "synthetic"}},
        })

        assert cfg["products"]["catalog"] == "synthetic"
        assert "num_products" not in cfg["products"]

    def test_products_int_backward_compat(self):
        """Plain int format still works (backward compat)."""
        cfg = _distribute_scale({"scale": {"products": 5000}})

        assert cfg["products"]["num_products"] == 5000

    def test_products_dict_does_not_override_section(self):
        cfg = _distribute_scale({
            "scale": {"products": {"catalog": "contoso", "rows": 2500}},
            "products": {"catalog": "all", "num_products": 999},
        })

        assert cfg["products"]["catalog"] == "all"
        assert cfg["products"]["num_products"] == 999


# ===================================================================
# _expand_merge_block
# ===================================================================

class TestExpandMergeBlock:
    def test_merge_enabled(self):
        cfg = {"sales": {"merge": {"enabled": True, "file": "out.parquet"}}}

        result = _expand_merge_block(cfg)

        assert result["sales"]["merge_parquet"] is True
        assert result["sales"]["merged_file"] == "out.parquet"

    def test_merge_disabled(self):
        cfg = {"sales": {"merge": {"enabled": False}}}

        result = _expand_merge_block(cfg)

        assert result["sales"]["merge_parquet"] is False

    def test_no_merge_block(self):
        cfg = {"sales": {"total_rows": 100}}

        result = _expand_merge_block(cfg)

        assert "merge_parquet" not in result["sales"]

    def test_existing_flat_key_wins(self):
        cfg = {"sales": {"merge_parquet": False, "merge": {"enabled": True}}}

        result = _expand_merge_block(cfg)

        assert result["sales"]["merge_parquet"] is False


# ===================================================================
# _expand_partition_by
# ===================================================================

class TestExpandPartitionBy:
    def test_list_of_columns(self):
        cfg = {"sales": {"partition_by": ["Year", "Month"]}}

        result = _expand_partition_by(cfg)

        assert result["sales"]["partitioning"]["enabled"] is True
        assert result["sales"]["partitioning"]["columns"] == ["Year", "Month"]

    def test_empty_list_disables(self):
        cfg = {"sales": {"partition_by": []}}

        result = _expand_partition_by(cfg)

        assert result["sales"]["partitioning"]["enabled"] is False

    def test_existing_partitioning_block_wins(self):
        cfg = {"sales": {
            "partitioning": {"enabled": False, "columns": []},
            "partition_by": ["Year"],
        }}

        result = _expand_partition_by(cfg)

        assert result["sales"]["partitioning"]["enabled"] is False


# ===================================================================
# _expand_region_mix
# ===================================================================

class TestExpandRegionMix:
    def test_basic_expansion(self):
        cfg = {"customers": {"region_mix": {"US": 60, "EU": 30, "India": 10}}}

        result = _expand_region_mix(cfg)

        assert result["customers"]["pct_us"] == 60.0
        assert result["customers"]["pct_eu"] == 30.0
        assert result["customers"]["pct_india"] == 10.0

    def test_org_pct_extracted(self):
        cfg = {"customers": {"region_mix": {"US": 100}, "org_pct": 5}}

        result = _expand_region_mix(cfg)

        assert result["customers"]["pct_org"] == 5.0

    def test_case_insensitive_keys(self):
        cfg = {"customers": {"region_mix": {"us": 50, "EU": 30, "INDIA": 20}}}

        result = _expand_region_mix(cfg)

        assert result["customers"]["pct_us"] == 50.0
        assert result["customers"]["pct_eu"] == 30.0
        assert result["customers"]["pct_india"] == 20.0

    def test_missing_regions_default_to_zero(self):
        cfg = {"customers": {"region_mix": {"US": 100}}}

        result = _expand_region_mix(cfg)

        assert result["customers"]["pct_eu"] == 0.0
        assert result["customers"]["pct_india"] == 0.0
        assert result["customers"]["pct_asia"] == 0.0

    def test_aliases(self):
        cfg = {"customers": {"region_mix": {"USA": 50, "Europe": 50}}}

        result = _expand_region_mix(cfg)

        assert result["customers"]["pct_us"] == 50.0
        assert result["customers"]["pct_eu"] == 50.0


# ===================================================================
# _expand_products_pricing
# ===================================================================

class TestExpandProductsPricing:
    def test_simplified_expansion(self):
        cfg = {"products": {"price_range": [10, 3000], "margin_range": [0.20, 0.35]}}

        result = _expand_products_pricing(cfg)
        pricing = result["products"]["pricing"]

        assert pricing["base"]["min_unit_price"] == 10.0
        assert pricing["base"]["max_unit_price"] == 3000.0
        assert pricing["cost"]["min_margin_pct"] == 0.20
        assert pricing["cost"]["max_margin_pct"] == 0.35

    def test_existing_pricing_block_untouched(self):
        cfg = {"products": {"pricing": {"base": {"value_scale": 2}}}}

        result = _expand_products_pricing(cfg)

        assert result["products"]["pricing"]["base"]["value_scale"] == 2

    def test_defaults_when_no_ranges(self):
        cfg = {"products": {}}

        result = _expand_products_pricing(cfg)
        pricing = result["products"]["pricing"]

        assert pricing["base"]["min_unit_price"] == 10.0
        assert pricing["base"]["max_unit_price"] == 3000.0


# ===================================================================
# _fold_facts_enabled
# ===================================================================

class TestFoldFactsEnabled:
    def test_returns_enabled_from_list(self):
        cfg = {"facts": {"enabled": ["sales", "returns"]}, "returns": {}}

        result = _fold_facts_enabled(cfg)

        assert result["returns"]["enabled"] is True
        assert "facts" not in result

    def test_returns_disabled_when_not_in_list(self):
        cfg = {"facts": {"enabled": ["sales"]}, "returns": {}}

        result = _fold_facts_enabled(cfg)

        assert result["returns"]["enabled"] is False

    def test_list_shorthand(self):
        cfg = {"facts": ["sales", "returns"]}

        result = _fold_facts_enabled(cfg)

        assert result.get("returns", {}).get("enabled") is True

    def test_no_facts_section(self):
        cfg = {"sales": {"total_rows": 100}}

        result = _fold_facts_enabled(cfg)

        assert "facts" not in result


def test_legacy_sql_section_warns_but_loads(tmp_path, capsys):
    import shutil
    from pathlib import Path
    from src.engine.config.config import load_config

    repo_cfg = Path(__file__).resolve().parents[1] / "config.yaml"
    dst = tmp_path / "config.yaml"
    shutil.copyfile(repo_cfg, dst)
    with dst.open("a", encoding="utf-8") as f:
        f.write("\nsql:\n  identifier_case: snake\n")

    cfg = load_config(dst)
    assert cfg is not None
    out = capsys.readouterr()
    assert "sql:" in (out.out + out.err) or "'sql'" in (out.out + out.err)


class TestSegmentModelsConfig:
    def test_defaults_are_typed(self):
        cfg = ModelsConfig.from_raw_dict({"models": {}})

        assert cfg.models.segments.enabled is True
        assert cfg.models.segments.organization.business_channel_share == 0.70
        assert cfg.models.segments.income_product_preference.strength == 0.45

    @pytest.mark.parametrize(
        "organization",
        [
            {"business_channel_share": 1.01},
            {"b2b_channel_share": -0.01},
            {"quantity_multiplier": 0.99},
        ],
    )
    def test_invalid_organization_settings_fail_fast(self, organization):
        with pytest.raises(ValidationError):
            ModelsConfig.from_raw_dict({
                "models": {"segments": {"organization": organization}},
            })


