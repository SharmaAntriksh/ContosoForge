"""Comprehensive tests for dimension generators.

Covers: stores, dates, currency, promotions, customers, employees, and product pricing.
Each generator is tested for output shape, column presence, determinism, data quality,
edge cases, and (where applicable) probability distributions.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.engine.config.config_schema import AppConfig
from src.exceptions import DimensionError

# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
from src.dimensions.stores import generate_store_table
from src.dimensions.stores.generator import GeoContext

# ---------------------------------------------------------------------------
# Currency / Exchange Rates
# ---------------------------------------------------------------------------
from src.dimensions.exchange_rates import build_dim_currency
from src.dimensions.exchange_rates.currency import _resolve_currency_list
from src.dimensions.exchange_rates.helpers import normalize_currency_list
from src.dimensions.exchange_rates.monthly_rates import build_monthly_rates

# ---------------------------------------------------------------------------
# Promotions
# ---------------------------------------------------------------------------
from src.dimensions.promotions import (
    generate_promotions_catalog,
    _build_year_windows,
)

# ---------------------------------------------------------------------------
# Product pricing
# ---------------------------------------------------------------------------
from src.dimensions.products.pricing import apply_product_pricing

# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
from src.dimensions.employees import (
    generate_employee_dimension,
    generate_employee_store_assignments,
)


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture()
def geo_keys():
    """Small geography key array for store tests."""
    return np.arange(1, 11, dtype=np.int64)


@pytest.fixture()
def iso_by_geo():
    """ISO code lookup keyed by GeographyKey."""
    return {
        1: "USD", 2: "USD", 3: "EUR", 4: "EUR", 5: "GBP",
        6: "INR", 7: "CAD", 8: "AUD", 9: "JPY", 10: "CNY",
    }


@pytest.fixture()
def small_stores(geo_keys, iso_by_geo):
    """Generate a small stores DataFrame for downstream tests."""
    return generate_store_table(
        geo=GeoContext(geo_keys=geo_keys, iso_by_geo=iso_by_geo),
        num_stores=10,
        seed=42,
    )


@pytest.fixture()
def people_pools():
    """Load real name pools from data folder (skip if unavailable)."""
    from src.utils.name_pools import load_people_pools, resolve_people_folder
    folder = resolve_people_folder()
    pf = Path(folder)
    if not pf.exists():
        pytest.skip("Name pool data not available")
    return load_people_pools(folder, enable_asia=True, legacy_support=True)


# ===================================================================
# STORES
# ===================================================================

class TestGenerateStoreTable:
    """Tests for generate_store_table()."""

    EXPECTED_COLS = [
        "StoreKey", "StoreNumber", "StoreName", "StoreType", "StoreFormat",
        "OwnershipType", "RevenueClass", "Status", "GeographyKey",
        "StoreZone", "StoreDistrict", "StoreRegion",
        "OpeningDate", "ClosingDate",
        "RenovationStartDate", "RenovationEndDate",
        "OpenFlag", "SquareFootage",
        "EmployeeCount", "StoreManager", "Phone", "StoreEmail",
        "StoreDescription", "CloseReason",
        "AvgTransactionValue", "CustomerSatisfactionScore",
        "InventoryTurnoverTarget", "LastAuditScore", "ShrinkageRatePct",
    ]

    def test_basic_output_shape(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=20, seed=1)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 20

    def test_expected_columns(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=10, seed=1)
        assert list(df.columns) == self.EXPECTED_COLS

    def test_store_key_sequential(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=15, seed=1)
        expected = list(range(1, 16))
        assert list(df["StoreKey"]) == expected

    def test_store_key_unique(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=50, seed=1)
        assert df["StoreKey"].is_unique

    def test_determinism(self, geo_keys):
        df1 = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=30, seed=99)
        df2 = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=30, seed=99)
        pd.testing.assert_frame_equal(df1, df2)

    def test_different_seeds_differ(self, geo_keys):
        df1 = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=20, seed=1)
        df2 = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=20, seed=2)
        # At least some values should differ
        assert not df1["StoreName"].equals(df2["StoreName"])

    def test_large_random_mode_seed_no_overflow(self, geo_keys):
        # Regression: random mode (defaults.random=true) draws a seed up to
        # 2**31-1. The store name/brand/manager index math
        # (sk * k + int(seed) * c) mixes the int32 StoreKey array with a large
        # Python int and used to raise "Python int too large to convert to C
        # long" on Windows for any seed > ~2**31/17. Must not raise now.
        big_seed = 2_111_222_333  # > 2**31/17 threshold; valid random-mode seed
        # people_pools=None also exercises the _MANAGER_FIRST/_LAST fallback path.
        df = generate_store_table(
            geo=GeoContext(geo_keys=geo_keys), num_stores=25, seed=big_seed,
            people_pools=None,
        )
        assert len(df) == 25
        assert df["StoreManager"].notna().all()
        assert (df["StoreManager"].str.len() > 0).all()
        assert (df["StoreName"].str.len() > 0).all()

    def test_no_nan_in_required_columns(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=30, seed=1)
        required = [
            "StoreKey", "StoreNumber", "StoreName", "StoreType", "Status",
            "GeographyKey", "OpeningDate", "OpenFlag", "SquareFootage",
            "EmployeeCount", "StoreManager",
        ]
        for col in required:
            assert df[col].notna().all(), f"NaN found in required column {col}"

    def test_geography_keys_valid(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=50, seed=1)
        valid_keys = set(geo_keys.tolist())
        assigned = set(df["GeographyKey"].astype(np.int64).tolist())
        assert assigned.issubset(valid_keys)

    def test_open_flag_matches_status(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=100, seed=1)
        open_flag = df["OpenFlag"].astype(int)
        status_open = (df["Status"] == "Open").astype(int)
        pd.testing.assert_series_equal(open_flag, status_open, check_names=False)

    def test_closing_date_only_for_closed(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=100, seed=1)
        not_closed = df["Status"] != "Closed"
        assert df.loc[not_closed, "ClosingDate"].isna().all()

    def test_low_store_count_raised_to_floor(self, geo_keys):
        """num_stores below minimum is silently raised to the floor (6)."""
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=1, seed=1)
        assert len(df) == 6
        df0 = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=0, seed=1)
        assert len(df0) == 6

    def test_empty_geo_keys_raises(self):
        with pytest.raises(DimensionError, match="non-empty"):
            generate_store_table(
                geo=GeoContext(geo_keys=np.array([], dtype=np.int64)),
                num_stores=5,
                seed=1,
            )

    def test_close_share_keeps_one_store_open(self, geo_keys):
        """Layer 1b: close_share=1.0 must not close every physical store — at
        least one stays open so Sales always has a staffed store and never
        falls back to an unstaffed store (which would emit EmployeeKey=-1)."""
        df = generate_store_table(
            geo=GeoContext(geo_keys=geo_keys),
            num_stores=20,
            seed=3,
            dataset_start="2021-01-01",
            dataset_end="2025-12-31",
            close_share=1.0,
            closing_enabled=True,
            online_stores=0,
        )
        assert (df["Status"] != "Closed").any(), "every store was closed"
        assert (df["Status"] == "Open").sum() >= 1

    def test_physical_stores_have_min_two_employees(self, geo_keys):
        """Physical stores must have EmployeeCount >= 2 (manager + >= 1
        salesperson) even when staffing_ranges asks for fewer; online stores
        keep 1. Floor lives where EmployeeCount is born so it stays consistent
        with the derived employee roster."""
        df = generate_store_table(
            geo=GeoContext(geo_keys=geo_keys),
            num_stores=40,
            seed=5,
            online_stores=5,
            staffing_overrides={"Supermarket": [1, 1], "Convenience": [0, 1]},
        )
        physical = df[df["StoreType"] != "Online"]
        assert (physical["EmployeeCount"] >= 2).all()
        online = df[df["StoreType"] == "Online"]
        if len(online):
            assert (online["EmployeeCount"] == 1).all()

    def test_square_footage_positive(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=30, seed=1)
        assert (df["SquareFootage"] > 0).all()

    def test_employee_count_positive(self, geo_keys):
        """All stores get positive EmployeeCount (closed stores keep operational count)."""
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=30, seed=1)
        assert (df["EmployeeCount"] > 0).all()

    def test_store_type_distribution(self, geo_keys):
        """Store types should be from the known set."""
        from src.defaults import STORE_TYPES
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=200, seed=1)
        types = set(df["StoreType"].unique())
        valid_types = set(STORE_TYPES)
        assert types.issubset(valid_types)

    def test_iso_coverage(self, geo_keys, iso_by_geo):
        """With ensure_iso_coverage, multiple ISO codes should appear."""
        df = generate_store_table(
            geo=GeoContext(geo_keys=geo_keys, iso_by_geo=iso_by_geo, ensure_iso_coverage=True),
            num_stores=20,
            seed=1,
        )
        geo_used = set(df["GeographyKey"].astype(np.int64).tolist())
        iso_used = {iso_by_geo[gk] for gk in geo_used if gk in iso_by_geo}
        # Should have at least 3 distinct ISO codes
        assert len(iso_used) >= 3

    def test_analytical_columns_ranges(self, geo_keys):
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=50, seed=1)
        assert (df["CustomerSatisfactionScore"] >= 1.0).all()
        assert (df["CustomerSatisfactionScore"] <= 10.0).all()
        assert (df["LastAuditScore"] >= 50).all()
        assert (df["LastAuditScore"] <= 100).all()
        assert (df["ShrinkageRatePct"] >= 0).all()


# ===================================================================
# CURRENCY
# ===================================================================

class TestBuildDimCurrency:
    def test_basic_output(self):
        df = build_dim_currency(["USD", "EUR", "GBP"])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert list(df.columns) == [
            "CurrencyKey", "CurrencyCode", "CurrencyName",
            "CurrencySymbol", "DecimalPlaces",
        ]

    def test_keys_sequential(self):
        df = build_dim_currency(["USD", "EUR", "INR"])
        assert list(df["CurrencyKey"]) == [1, 2, 3]

    def test_currency_codes_preserved(self):
        df = build_dim_currency(["cad", "gbp"])
        # USD is auto-inserted as base currency when not in the input list
        assert list(df["CurrencyCode"]) == ["USD", "CAD", "GBP"]

    def test_determinism(self):
        df1 = build_dim_currency(["USD", "EUR"])
        df2 = build_dim_currency(["USD", "EUR"])
        pd.testing.assert_frame_equal(df1, df2)

    def test_single_currency(self):
        df = build_dim_currency(["JPY"])
        # USD is auto-inserted as base currency, so JPY-only input yields 2 rows
        assert len(df) == 2
        assert list(df["CurrencyCode"]) == ["USD", "JPY"]

    def test_duplicate_raises(self):
        with pytest.raises(DimensionError, match="Duplicate"):
            build_dim_currency(["USD", "USD"])

    def test_invalid_code_raises(self):
        with pytest.raises(DimensionError, match="3 letters"):
            build_dim_currency(["US"])

    def test_empty_list_raises(self):
        with pytest.raises(DimensionError, match="non-empty"):
            build_dim_currency([])

    def test_non_alpha_raises(self):
        with pytest.raises(DimensionError, match="3 letters"):
            build_dim_currency(["U1D"])

    def test_currency_key_unique(self):
        df = build_dim_currency(["USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY"])
        assert df["CurrencyKey"].is_unique

    def test_currency_name_filled(self):
        df = build_dim_currency(["USD", "EUR"])
        # Known currencies should have proper names
        assert df["CurrencyName"].notna().all()
        assert "Dollar" in df.loc[df["CurrencyCode"] == "USD", "CurrencyName"].iloc[0]

    def test_currency_symbol(self):
        df = build_dim_currency(["USD", "EUR", "JPY"])
        symbols = dict(zip(df["CurrencyCode"], df["CurrencySymbol"]))
        assert symbols["USD"] == "$"
        assert symbols["EUR"] == "€"
        assert symbols["JPY"] == "¥"

    def test_decimal_places(self):
        df = build_dim_currency(["USD", "JPY", "KRW"])
        decimals = dict(zip(df["CurrencyCode"], df["DecimalPlaces"]))
        assert decimals["USD"] == 2
        assert decimals["JPY"] == 0
        assert decimals["KRW"] == 0


class TestNormalizeCurrencyList:
    def test_whitespace_stripped(self):
        result = normalize_currency_list(["  usd ", " eur"])
        assert result == ["USD", "EUR"]

    def test_case_normalized(self):
        result = normalize_currency_list(["usd", "Eur", "GBP"])
        assert result == ["USD", "EUR", "GBP"]


class TestResolveCurrencyList:
    """FX-CUR-1: the currency dim must superset the FX from/to currencies."""

    def test_explicit_omitting_fx_currency_is_unioned_in(self):
        # Explicit list omits EUR, but FX needs it -> EUR must be added.
        result = _resolve_currency_list(["USD", "GBP"], ["USD"], ["EUR"])
        assert "EUR" in result
        assert set(["USD", "GBP", "EUR"]).issubset(result)

    def test_explicit_superset_is_unchanged(self):
        result = _resolve_currency_list(["USD", "EUR", "GBP"], ["USD"], ["EUR"])
        assert result == ["USD", "EUR", "GBP"]

    def test_no_explicit_derives_from_fx_union(self):
        result = _resolve_currency_list(None, ["USD"], ["EUR", "JPY"])
        assert set(["USD", "EUR", "JPY"]).issubset(result)


# ===================================================================
# PROMOTIONS
# ===================================================================

class TestGeneratePromotionsCatalog:
    def _windows(self, start="2023-01-01", end="2023-12-31"):
        return _build_year_windows(pd.Timestamp(start), pd.Timestamp(end))

    def test_basic_output(self):
        years, windows = self._windows()
        df = generate_promotions_catalog(
            years=years, year_windows=windows, seed=42,
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_expected_columns(self):
        years, windows = self._windows()
        df = generate_promotions_catalog(
            years=years, year_windows=windows, seed=42,
        )
        expected = [
            "PromotionKey", "PromotionLabel", "PromotionName",
            "PromotionDescription", "DiscountPct", "PromotionType",
            "PromotionCategory", "PromotionYear", "PromotionSequence",
            "StartDate", "EndDate",
        ]
        assert list(df.columns) == expected

    def test_no_discount_sentinel(self):
        years, windows = self._windows()
        df = generate_promotions_catalog(
            years=years, year_windows=windows, seed=42,
        )
        sentinel = df[df["PromotionKey"] == 1]
        assert len(sentinel) == 1
        assert sentinel.iloc[0]["PromotionName"] == "No Discount"
        assert sentinel.iloc[0]["DiscountPct"] == 0.0

    def test_promotion_keys_unique(self):
        years, windows = self._windows()
        df = generate_promotions_catalog(
            years=years, year_windows=windows, seed=42,
        )
        assert df["PromotionKey"].is_unique

    def test_determinism(self):
        years, windows = self._windows()
        df1 = generate_promotions_catalog(years=years, year_windows=windows, seed=99)
        df2 = generate_promotions_catalog(years=years, year_windows=windows, seed=99)
        pd.testing.assert_frame_equal(df1, df2)

    def test_different_seeds_differ(self):
        years, windows = self._windows()
        df1 = generate_promotions_catalog(years=years, year_windows=windows, seed=1)
        df2 = generate_promotions_catalog(years=years, year_windows=windows, seed=2)
        # Discount values should differ
        assert not df1["DiscountPct"].equals(df2["DiscountPct"])

    def test_discount_range(self):
        years, windows = self._windows()
        df = generate_promotions_catalog(
            years=years, year_windows=windows, seed=42,
        )
        # All discounts should be in [0, 1)
        assert (df["DiscountPct"] >= 0).all()
        assert (df["DiscountPct"] < 1.0).all()

    def test_start_before_end(self):
        years, windows = self._windows()
        df = generate_promotions_catalog(
            years=years, year_windows=windows, seed=42,
        )
        assert (df["StartDate"] < df["EndDate"]).all()

    def test_no_nan_in_required(self):
        years, windows = self._windows()
        df = generate_promotions_catalog(
            years=years, year_windows=windows, seed=42,
        )
        for col in ["PromotionKey", "PromotionName", "DiscountPct",
                     "StartDate", "EndDate"]:
            assert df[col].notna().all(), f"NaN in {col}"

    def test_empty_years_raises(self):
        with pytest.raises(DimensionError, match="No years"):
            generate_promotions_catalog(years=[], year_windows={}, seed=42)

    def test_multi_year_span(self):
        years, windows = self._windows("2021-01-01", "2023-12-31")
        df = generate_promotions_catalog(
            years=years, year_windows=windows, seed=42,
        )
        promo_years = set(df["PromotionYear"].unique())
        # Should span multiple years
        assert len(promo_years) > 1

    def test_minimal_counts(self):
        """All optional promo counts set to zero; only holidays remain."""
        years, windows = self._windows()
        df = generate_promotions_catalog(
            years=years,
            year_windows=windows,
            num_seasonal=0,
            num_clearance=0,
            num_limited=0,
            num_flash=0,
            num_volume=0,
            num_loyalty=0,
            num_bundle=0,
            num_new_customer=0,
            seed=42,
        )
        # Should still have the No Discount sentinel + holidays
        assert len(df) >= 1
        assert df["PromotionKey"].iloc[0] == 1

    def test_single_year(self):
        years, windows = self._windows("2024-01-01", "2024-12-31")
        df = generate_promotions_catalog(
            years=years, year_windows=windows,
            num_seasonal=2, num_clearance=1, num_limited=1,
            num_flash=0, num_volume=0, num_loyalty=0,
            num_bundle=0, num_new_customer=0,
            seed=42,
        )
        assert len(df) >= 1


# ===================================================================
# PRODUCT PRICING
# ===================================================================

class TestApplyProductPricing:
    def _make_products(self, n=20, seed=42):
        rng = np.random.default_rng(seed)
        return pd.DataFrame({
            "ProductKey": np.arange(1, n + 1),
            "ListPrice": rng.uniform(5.0, 500.0, n),
            "UnitCost": rng.uniform(3.0, 300.0, n),
            "Brand": rng.choice(["BrandA", "BrandB", "BrandC"], n),
        })

    def test_passthrough_empty_config(self):
        df = self._make_products()
        result = apply_product_pricing(df, {})
        pd.testing.assert_frame_equal(result, df)

    def test_margin_mode(self):
        df = self._make_products()
        cfg = {
            "base": {"value_scale": 1.0},
            "cost": {
                "mode": "margin",
                "min_margin_pct": 0.20,
                "max_margin_pct": 0.40,
            },
        }
        result = apply_product_pricing(df, cfg, seed=42)
        # UnitCost should be less than UnitPrice
        assert (result["UnitCost"] <= result["ListPrice"]).all()
        # Margins should be in configured range (approximately)
        margins = 1.0 - result["UnitCost"] / result["ListPrice"]
        assert margins.min() >= 0.19  # allow small rounding tolerance
        assert margins.max() <= 0.41

    def test_value_scale(self):
        df = self._make_products()
        cfg = {
            "base": {"value_scale": 2.0},
            "cost": {
                "mode": "margin",
                "min_margin_pct": 0.20,
                "max_margin_pct": 0.35,
            },
        }
        result = apply_product_pricing(df, cfg, seed=42)
        # Scaled prices should be roughly 2x original
        ratio = result["ListPrice"].median() / df["ListPrice"].median()
        assert 1.5 < ratio < 2.5

    def test_snap_unit_price(self):
        df = self._make_products()
        cfg = {
            "base": {"value_scale": 1.0},
            "appearance": {"snap_unit_price": True, "price_ending": 0.99},
            "cost": {
                "mode": "margin",
                "min_margin_pct": 0.20,
                "max_margin_pct": 0.35,
            },
        }
        result = apply_product_pricing(df, cfg, seed=42)
        # Prices should end in .99 (for prices > 1.0)
        decimals = result["ListPrice"] % 1
        big = result["ListPrice"] > 2.0
        if big.any():
            assert (decimals[big].round(2) == 0.99).all()

    def test_determinism(self):
        df = self._make_products()
        cfg = {
            "base": {"value_scale": 1.0},
            "cost": {
                "mode": "margin",
                "min_margin_pct": 0.20,
                "max_margin_pct": 0.35,
            },
        }
        r1 = apply_product_pricing(df.copy(), cfg, seed=42)
        r2 = apply_product_pricing(df.copy(), cfg, seed=42)
        pd.testing.assert_frame_equal(r1, r2)

    def test_no_nan_after_pricing(self):
        df = self._make_products()
        cfg = {
            "base": {"value_scale": 1.0, "min_unit_price": 1.0, "max_unit_price": 5000.0},
            "cost": {
                "mode": "margin",
                "min_margin_pct": 0.20,
                "max_margin_pct": 0.35,
            },
        }
        result = apply_product_pricing(df, cfg, seed=42)
        assert result["ListPrice"].notna().all()
        assert result["UnitCost"].notna().all()

    def test_unit_cost_le_unit_price(self):
        df = self._make_products()
        cfg = {
            "base": {"value_scale": 1.0},
            "cost": {
                "mode": "margin",
                "min_margin_pct": 0.10,
                "max_margin_pct": 0.50,
            },
        }
        result = apply_product_pricing(df, cfg, seed=42)
        assert (result["UnitCost"] <= result["ListPrice"]).all()

    def test_prices_non_negative(self):
        df = self._make_products()
        cfg = {
            "base": {"value_scale": 0.01},  # very small scale
            "cost": {
                "mode": "margin",
                "min_margin_pct": 0.20,
                "max_margin_pct": 0.35,
            },
        }
        result = apply_product_pricing(df, cfg, seed=42)
        assert (result["ListPrice"] >= 0).all()
        assert (result["UnitCost"] >= 0).all()

    def test_min_max_price_clamp(self):
        df = self._make_products()
        cfg = {
            "base": {
                "value_scale": 1.0,
                "min_unit_price": 50.0,
                "max_unit_price": 200.0,
            },
            "cost": {
                "mode": "margin",
                "min_margin_pct": 0.20,
                "max_margin_pct": 0.35,
            },
        }
        result = apply_product_pricing(df, cfg, seed=42)
        assert (result["ListPrice"] >= 50.0).all()
        assert (result["ListPrice"] <= 200.0).all()

    def test_keep_mode(self):
        df = self._make_products()
        cfg = {
            "base": {"value_scale": 1.5},
            "cost": {"mode": "keep"},
        }
        result = apply_product_pricing(df, cfg, seed=42)
        assert result["UnitCost"].notna().all()
        assert (result["UnitCost"] <= result["ListPrice"]).all()


# ===================================================================
# EMPLOYEES
# ===================================================================

class TestGenerateEmployeeDimension:
    """Tests for generate_employee_dimension()."""

    def _make_stores(self, n=5, seed=42):
        rng = np.random.default_rng(seed)
        return pd.DataFrame({
            "StoreKey": np.arange(1, n + 1, dtype=np.int32),
            "GeographyKey": rng.integers(1, 10, size=n, dtype=np.int32),
            "EmployeeCount": rng.integers(5, 20, size=n, dtype=np.int64),
            "StoreType": rng.choice(
                ["Supermarket", "Online", "Hypermarket", "Convenience"], n
            ),
            "StoreDistrict": [f"District {i // 3 + 1}" for i in range(n)],
            "StoreRegion": [f"Region {i // 6 + 1}" for i in range(n)],
        })

    def test_basic_output(self, people_pools):
        stores = self._make_stores()
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_employee_key_unique(self, people_pools):
        stores = self._make_stores()
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        assert df["EmployeeKey"].is_unique

    def test_over_1000_staff_raises_not_silent_collision(self, people_pools):
        """EMP-1: a store with >= STAFF_KEY_STORE_MULT staff would spill into the
        next store's EmployeeKey band. Guard must raise loudly, not corrupt."""
        stores = self._make_stores(n=3)
        # EmployeeCount includes the manager, so 1002 -> 1001 staff -> idx hits 1001.
        stores.loc[1, "EmployeeCount"] = 1002
        stores["StoreType"] = "Supermarket"  # ensure all physical
        with pytest.raises(DimensionError, match="slots per store"):
            generate_employee_dimension(
                stores=stores,
                seed=42,
                global_start=pd.Timestamp("2021-01-01"),
                global_end=pd.Timestamp("2025-12-31"),
                people_pools=people_pools,
            )

    def test_just_under_1000_staff_stays_unique(self, people_pools):
        """EMP-1 boundary: 999 staff/store (idx max 999) stays within the per-store
        slot band -> no collision, keys still unique."""
        stores = self._make_stores(n=3)
        stores["EmployeeCount"] = 1000  # 999 staff each
        stores["StoreType"] = "Supermarket"
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        assert df["EmployeeKey"].is_unique

    def test_hierarchy_has_ceo(self, people_pools):
        stores = self._make_stores()
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        ceo = df[df["EmployeeKey"] == 1]
        assert len(ceo) == 1
        assert ceo.iloc[0]["Title"] == "Chief Executive Officer"

    def test_hierarchy_has_vp(self, people_pools):
        stores = self._make_stores()
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        vp = df[df["EmployeeKey"] == 2]
        assert len(vp) == 1
        assert vp.iloc[0]["Title"] == "VP Operations"

    def test_store_managers_exist(self, people_pools):
        stores = self._make_stores(n=3)
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        mgrs = df[df["Title"] == "Store Manager"]
        assert len(mgrs) == 3  # one per store

    def test_determinism(self, people_pools):
        stores = self._make_stores()
        kw = dict(
            stores=stores,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        df1 = generate_employee_dimension(seed=42, **kw)
        df2 = generate_employee_dimension(seed=42, **kw)
        pd.testing.assert_frame_equal(df1, df2)

    def test_empty_stores_raises(self, people_pools):
        empty = pd.DataFrame(columns=[
            "StoreKey", "GeographyKey", "EmployeeCount", "StoreType",
        ])
        with pytest.raises(DimensionError, match="empty"):
            generate_employee_dimension(
                stores=empty,
                seed=42,
                global_start=pd.Timestamp("2021-01-01"),
                global_end=pd.Timestamp("2025-12-31"),
                people_pools=people_pools,
            )

    def test_org_levels_valid(self, people_pools):
        stores = self._make_stores()
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        valid_levels = {1, 2, 3, 4, 5, 6}
        actual_levels = set(df["OrgLevel"].dropna().astype(int).unique())
        assert actual_levels.issubset(valid_levels)

    def test_static_model_no_attrition(self, people_pools):
        """Static model: no attrition. All SAs at open stores are active."""
        stores = self._make_stores()
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        sa = df[df["Title"] == "Sales Associate"]
        # All SAs at open stores should be active (no attrition)
        sa_open = sa[sa["TerminationDate"].isna()]
        assert len(sa_open) > 0, "No active SAs found"

    def test_every_store_has_sa_hired_before_start(self, people_pools):
        """Every store should have at least one SA hired at or before global_start."""
        from src.dimensions.employees import STAFF_KEY_BASE
        global_start = pd.Timestamp("2021-01-01")
        stores = self._make_stores()
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=global_start,
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        sa = df[(df["Title"] == "Sales Associate") & (df["EmployeeKey"] >= STAFF_KEY_BASE)]
        if len(sa) > 0:
            for sk in sa["StoreKey"].unique():
                store_sa = sa[sa["StoreKey"] == sk]
                hire_dates = pd.to_datetime(store_sa["HireDate"])
                assert (hire_dates <= global_start).any(), (
                    f"Store {sk}: no SA hired before {global_start}"
                )

    def test_employee_count_one_still_gets_salesperson(self, people_pools):
        """Backstop (Layer 2): a physical store budgeted for only a manager
        (EmployeeCount=1) must still get >= 1 Sales Associate, otherwise Sales
        emits EmployeeKey=-1 (orphan FK) for that store."""
        stores = self._make_stores(n=3)
        stores["StoreType"] = "Supermarket"  # all physical
        stores["EmployeeCount"] = 1  # only the manager budgeted
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        for sk in stores["StoreKey"].astype(int):
            store_sa = df[(df["StoreKey"] == sk) & (df["Title"] == "Sales Associate")]
            assert len(store_sa) >= 1, f"Store {sk} has no Sales Associate"

    def test_names_populated(self, people_pools):
        stores = self._make_stores()
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        assert df["EmployeeName"].notna().all()
        assert (df["EmployeeName"].str.len() > 0).all()


# ===================================================================
# SALESPERSON COVERAGE INVARIANT (employees -> bridge)
# ===================================================================

class TestSalespersonCoverageInvariant:
    """Every physical store must have >= 1 salesperson in the bridge table.

    Without this, the Sales fact has no eligible salesperson for those
    (store, date) rows and emits EmployeeKey=-1 — an orphan FK that breaks the
    generated SQL constraints. Exercises the employee-generator backstop
    (Layer 2) end-to-end through the assignments bridge, including the
    production EmployeeKey->StoreKey decode path.
    """

    def _physical_stores(self, n=6, seed=7):
        rng = np.random.default_rng(seed)
        return pd.DataFrame({
            "StoreKey": np.arange(1, n + 1, dtype=np.int32),
            "GeographyKey": rng.integers(1, 10, size=n, dtype=np.int32),
            # Manager-only budget for every store — the adversarial case.
            "EmployeeCount": np.ones(n, dtype=np.int64),
            "StoreType": np.array(["Supermarket"] * n, dtype=object),
            "StoreDistrict": [f"District {i // 3 + 1}" for i in range(n)],
            "StoreRegion": [f"Region {i // 6 + 1}" for i in range(n)],
        })

    def test_every_physical_store_has_salesperson_in_bridge(self, people_pools):
        global_start = pd.Timestamp("2021-01-01")
        global_end = pd.Timestamp("2025-12-31")
        stores = self._physical_stores()
        emp = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=global_start,
            global_end=global_end,
            people_pools=people_pools,
        )
        # Drop StoreKey to force the production decode path (run_employee_store_
        # assignments reads employees.parquet without the StoreKey column).
        emp_for_bridge = emp.drop(columns=["StoreKey"])
        bridge = generate_employee_store_assignments(
            employees=emp_for_bridge,
            global_start=global_start,
            global_end=global_end,
        )
        sp = bridge[bridge["RoleAtStore"] == "Sales Associate"]
        covered = set(sp["StoreKey"].astype(int).unique())
        for sk in stores["StoreKey"].astype(int):
            assert sk in covered, f"Physical store {sk} has no salesperson in bridge"
        # No salesperson row should ever resolve to a Store Manager key band.
        assert (sp["EmployeeKey"].astype(np.int64) >= 40_000_000).all()

    def test_employee_count_matches_roster_size(self, people_pools):
        """Regression: through the normal store->employee path, every store's
        Stores.EmployeeCount must equal its employee-row count — the invariant
        the shipped SQL verification scripts assert (verify_cross_dimension.sql,
        25_stores.sql). The store-generator EmployeeCount floor keeps this true
        even under minimal staffing (the employee backstop, which would break
        it, must not fire for pipeline-generated stores)."""
        geo_keys = np.arange(1, 11, dtype=np.int64)
        stores = generate_store_table(
            geo=GeoContext(geo_keys=geo_keys),
            num_stores=30,
            seed=11,
            online_stores=3,
            closing_enabled=False,  # no terminations -> roster == EmployeeCount
            staffing_overrides={"Supermarket": [1, 1], "Convenience": [1, 2]},
        )
        emp = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        roster = emp[emp["StoreKey"] > 0].groupby("StoreKey").size()
        for _, srow in stores.iterrows():
            sk = int(srow["StoreKey"])
            assert int(roster.get(sk, 0)) == int(srow["EmployeeCount"]), (
                f"Store {sk}: EmployeeCount={int(srow['EmployeeCount'])} "
                f"but roster has {int(roster.get(sk, 0))}"
            )


# ===================================================================
# CONFIG CROSS-SECTION RULES (staffing floor)
# ===================================================================

class TestStaffingFloorRule:
    """apply_cross_section_rules floors physical staffing to >= 2 staff so
    every store keeps >= 1 salesperson after the manager (Layer 1)."""

    def test_low_staffing_min_raised(self):
        from src.engine.config.config import apply_cross_section_rules
        cfg = {"stores": {"staffing_ranges": {
            "Supermarket": [1, 1],   # min and max both below floor
            "Convenience": [0, 4],   # only min below floor
            "Hypermarket": [15, 40], # healthy — untouched
            "Online": [1, 1],        # fixed to 1 — skipped
        }}}
        out = apply_cross_section_rules(cfg)
        sr = out["stores"]["staffing_ranges"]
        assert sr["Supermarket"] == [2, 2]
        assert sr["Convenience"] == [2, 4]
        assert sr["Hypermarket"] == [15, 40]
        assert sr["Online"] == [1, 1]

    def test_healthy_staffing_unchanged(self):
        from src.engine.config.config import apply_cross_section_rules
        cfg = {"stores": {"staffing_ranges": {
            "Supermarket": [8, 20],
            "Convenience": [2, 6],
        }}}
        out = apply_cross_section_rules(cfg)
        assert out["stores"]["staffing_ranges"] == {
            "Supermarket": [8, 20],
            "Convenience": [2, 6],
        }


# ===================================================================
# CUSTOMERS (generate_synthetic_customers)
# ===================================================================

class TestCustomerGenerator:
    """Tests for the customer dimension generator via its internal function.

    These tests call generate_synthetic_customers which requires a normalized
    config dict, a geography.parquet file, and loyalty_tiers / acquisition_channels
    parquet files.  We build a minimal config and mock all file dependencies.
    """

    def _minimal_cfg(self, n=50):
        return AppConfig.model_validate({
            "defaults": {"seed": 42, "dates": {"start": "2023-01-01", "end": "2024-12-31"}},
            "customers": {
                "total_customers": n,
                "active_ratio": 0.95,
                "pct_india": 10,
                "pct_us": 50,
                "pct_eu": 40,
                "pct_asia": 0.0,
                "pct_org": 5,
            },
            "geography": {},
        })

    def _fake_geography(self, n=10):
        return pd.DataFrame({
            "GeographyKey": np.arange(1, n + 1, dtype=np.int64),
            "City": [f"City{i}" for i in range(1, n + 1)],
            "State": [f"State{i}" for i in range(1, n + 1)],
            "Country": ["United States"] * 5 + ["United Kingdom"] * 3 + ["India"] * 2,
            "ISOCode": ["USD"] * 5 + ["GBP"] * 3 + ["INR"] * 2,
            "Latitude": [round(40.0 + i * 0.5, 4) for i in range(n)],
            "Longitude": [round(-100.0 + i * 0.5, 4) for i in range(n)],
        })

    def _fake_loyalty_tiers(self):
        return pd.DataFrame({
            "LoyaltyTierKey": np.arange(1, 5, dtype=np.int64),
            "LoyaltyTier": ["Bronze", "Silver", "Gold", "Platinum"],
        })

    def _fake_acq_channels(self):
        return pd.DataFrame({
            "CustomerAcquisitionChannelKey": np.arange(1, 6, dtype=np.int64),
            "CustomerAcquisitionChannel": [
                "Online Search", "Social Media", "Referral", "Direct", "Email Campaign",
            ],
        })

    def _mock_read_parquet(self, folder, dim_name):
        """Return a fake DataFrame for the given dimension name."""
        if dim_name == "loyalty_tiers":
            return self._fake_loyalty_tiers()
        if dim_name == "customer_acquisition_channels":
            return self._fake_acq_channels()
        raise FileNotFoundError(f"Unexpected dim: {dim_name}")

    @staticmethod
    def _fake_org_names():
        return np.array([
            "Northstar Logistics", "Pinnacle Tech", "Summit Industries",
            "Horizon Media", "Vanguard Solutions", "Atlas Manufacturing",
            "Apex Retail", "Beacon Finance", "Catalyst Energy", "Delta Corp",
        ], dtype=object)

    def _run(self, cfg):
        from src.dimensions.customers import generate_synthetic_customers
        from src.utils.name_pools import load_people_pools

        geo_df = self._fake_geography()

        # Load real people pools from disk (they exist in ./data/name_pools/people)
        people_folder = "./data/name_pools/people"
        pf = Path(people_folder)
        if not pf.exists():
            pytest.skip("Name pool data not available")
        pools = load_people_pools(str(people_folder), enable_asia=False, legacy_support=True)

        with patch("src.dimensions.customers.generator.load_dimension", return_value=(geo_df, False)), \
             patch("src.dimensions.customers.generator.read_parquet_dim", side_effect=self._mock_read_parquet), \
             patch("src.dimensions.customers.generator.resolve_org_names_file", return_value="fake_org.csv"), \
             patch("src.dimensions.customers.generator.load_org_names", return_value=self._fake_org_names()), \
             patch("src.dimensions.customers.generator.load_people_pools", return_value=pools):
            result = generate_synthetic_customers(cfg, Path("/tmp/fake"))
        # Returns (customers_df, profile_df, org_profile_df, active_set)
        return result

    def test_basic_output(self):
        cfg = self._minimal_cfg(n=30)
        customers_df, profile_df, org_df, active_set = self._run(cfg)
        assert isinstance(customers_df, pd.DataFrame)
        assert len(customers_df) == 30

    def test_customer_key_sequential(self):
        cfg = self._minimal_cfg(n=20)
        customers_df, *_ = self._run(cfg)
        assert list(customers_df["CustomerKey"]) == list(range(1, 21))

    def test_customer_key_unique(self):
        cfg = self._minimal_cfg(n=50)
        customers_df, *_ = self._run(cfg)
        assert customers_df["CustomerKey"].is_unique

    def test_determinism(self):
        cfg1 = self._minimal_cfg(n=30)
        cfg2 = self._minimal_cfg(n=30)
        df1, *_ = self._run(cfg1)
        df2, *_ = self._run(cfg2)
        pd.testing.assert_frame_equal(df1, df2)

    def test_active_ratio_applied(self):
        cfg = self._minimal_cfg(n=100)
        cfg["customers"]["active_ratio"] = 0.80
        customers_df, _, _, active_set = self._run(cfg)
        # active_set has floor(100 * 0.80) = 80 customers
        assert len(active_set) == 80
        # IsActiveInSales is now internal-only; verify via active_set size
        assert len(active_set) <= len(customers_df)

    def test_geography_keys_valid(self):
        cfg = self._minimal_cfg(n=50)
        geo_df = self._fake_geography()
        customers_df, *_ = self._run(cfg)
        valid_keys = set(geo_df["GeographyKey"].tolist())
        assigned = set(customers_df["GeographyKey"].astype(np.int64).tolist())
        assert assigned.issubset(valid_keys)

    def test_zero_customers_raises(self):
        """Zero customer count should raise before hitting file I/O."""
        from src.dimensions.customers import generate_synthetic_customers
        cfg = self._minimal_cfg(n=0)
        cfg["customers"]["total_customers"] = 0
        # The ValueError is raised early, before any file I/O, but we still
        # need to provide geography mock since load_dimension runs first.
        geo_df = self._fake_geography()
        with patch("src.dimensions.customers.generator.load_dimension", return_value=(geo_df, False)), \
             patch("src.dimensions.customers.generator.read_parquet_dim", side_effect=self._mock_read_parquet), \
             patch("src.dimensions.customers.generator.resolve_org_names_file", return_value="fake_org.csv"), \
             patch("src.dimensions.customers.generator.load_org_names", return_value=self._fake_org_names()), \
             patch("src.dimensions.customers.generator.load_people_pools", return_value=None):
            with pytest.raises(DimensionError, match="must be > 0"):
                generate_synthetic_customers(cfg, Path("/tmp/fake"))

    def test_geography_distribution(self):
        """Geography keys should be drawn from the available pool."""
        cfg = self._minimal_cfg(n=200)
        customers_df, *_ = self._run(cfg)
        geo_counts = customers_df["GeographyKey"].value_counts()
        # All assigned keys should come from our 10-row fake geography
        assert set(geo_counts.index).issubset(set(range(1, 11)))

    def test_org_customers_present(self):
        cfg = self._minimal_cfg(n=200)
        cfg["customers"]["pct_org"] = 10  # 10%
        customers_df, *_ = self._run(cfg)
        org_count = (customers_df["GenderCode"] == "O").sum()
        assert org_count > 0, "Expected some organization customers"

    def test_no_nan_in_customer_key(self):
        cfg = self._minimal_cfg(n=50)
        customers_df, *_ = self._run(cfg)
        assert customers_df["CustomerKey"].notna().all()

    def test_email_populated(self):
        cfg = self._minimal_cfg(n=50)
        customers_df, *_ = self._run(cfg)
        assert customers_df["EmailAddress"].notna().all()
        assert (customers_df["EmailAddress"].str.contains("@")).all()

    def test_single_customer(self):
        cfg = self._minimal_cfg(n=1)
        cfg["customers"]["active_ratio"] = 1.0
        customers_df, *_ = self._run(cfg)
        assert len(customers_df) == 1

    def test_profile_person_only(self):
        cfg = self._minimal_cfg(n=50)
        customers_df, profile_df, *_ = self._run(cfg)
        person_df = customers_df[customers_df["CustomerType"] != "Organization"]
        assert len(profile_df) == len(person_df)
        assert list(profile_df["CustomerKey"]) == list(person_df["CustomerKey"])

    def test_customer_type_matches_org(self):
        cfg = self._minimal_cfg(n=100)
        cfg["customers"]["pct_org"] = 15
        customers_df, *_ = self._run(cfg)
        org_mask = customers_df["GenderCode"] == "O"
        assert (customers_df.loc[org_mask, "CustomerType"] == "Organization").all()
        assert (customers_df.loc[~org_mask, "CustomerType"] == "Individual").all()

    def test_gender_and_gender_code(self):
        """Gender keeps readable labels (Male/Female/Org); GenderCode carries the
        single-char code (M/F/O) with a 1:1 mapping. Gender is NOT overwritten."""
        cfg = self._minimal_cfg(n=200)
        cfg["customers"]["pct_org"] = 15
        customers_df, *_ = self._run(cfg)

        # Readable Gender column preserved.
        assert set(customers_df["Gender"].unique()).issubset({"Male", "Female", "Org"})
        # Parallel GenderCode column with single-char codes.
        assert set(customers_df["GenderCode"].unique()).issubset({"M", "F", "O"})
        # GenderCode lands immediately after Gender.
        cols = list(customers_df.columns)
        assert cols[cols.index("Gender") + 1] == "GenderCode"
        # 1:1 mapping holds for every row.
        expected = customers_df["Gender"].map({"Male": "M", "Female": "F", "Org": "O"})
        assert (customers_df["GenderCode"] == expected).all()

        person_mask = customers_df["CustomerType"] == "Individual"
        assert set(customers_df.loc[person_mask, "Gender"].unique()).issubset({"Male", "Female"})
        assert (customers_df.loc[~person_mask, "Gender"] == "Org").all()

    def test_phone_number_uniform_10_digits(self):
        """PhoneNumber is a uniform 10-digit string with no country/region code."""
        cfg = self._minimal_cfg(n=100)
        customers_df, *_ = self._run(cfg)
        assert customers_df["PhoneNumber"].str.match(r"^\d{10}$").all()

    def test_marketing_consent_channels(self):
        """OptInMarketing replaced by per-channel consent flags; newsletter needs email consent."""
        cfg = self._minimal_cfg(n=500)
        _customers, profile_df, *_ = self._run(cfg)
        assert "OptInMarketing" not in profile_df.columns
        for col in ("ConsentEmail", "ConsentSMS", "ConsentCall"):
            assert col in profile_df.columns
            assert profile_df[col].dtype == bool
        # Email is granted most, then SMS, then call (shared receptiveness gate).
        assert profile_df["ConsentEmail"].sum() >= profile_df["ConsentSMS"].sum() \
            >= profile_df["ConsentCall"].sum()
        # No newsletter without email consent.
        no_email = ~profile_df["ConsentEmail"]
        assert (profile_df.loc[no_email, "NewsletterFrequency"] == "None").all()

    def test_address_street_line_only(self):
        """Home/Work addresses are street-line only (no embedded city/state)."""
        cfg = self._minimal_cfg(n=100)
        customers_df, *_ = self._run(cfg)
        pat = r"^\d+ .+, (Apt|Suite|Unit|Fl|#) \d+$"
        assert customers_df["HomeAddress"].str.match(pat).all()
        assert customers_df["WorkAddress"].str.match(pat).all()
        # Fixture city/state names must not appear in the address string.
        assert not customers_df["HomeAddress"].str.contains(r"City\d|State\d").any()

    def test_postal_code_per_country(self):
        """Postal format matches the customer's actual country, not a region bucket."""
        cfg = self._minimal_cfg(n=300)
        customers_df, *_ = self._run(cfg)
        geo = self._fake_geography()
        key_to_country = dict(zip(geo["GeographyKey"], geo["Country"]))
        country = customers_df["GeographyKey"].map(key_to_country)
        assert not customers_df["PostalCode"].isna().any()
        # United States -> 5 digits, India -> 6 digits, United Kingdom -> alphanumeric.
        assert customers_df.loc[country == "United States", "PostalCode"].str.match(r"^\d{5}$").all()
        assert customers_df.loc[country == "India", "PostalCode"].str.match(r"^\d{6}$").all()
        assert customers_df.loc[country == "United Kingdom", "PostalCode"].str.match(
            r"^[A-Z]{1,2}\d{1,2} \d[A-Z]{2}$"
        ).all()

    def test_lat_lon_anchored_on_city(self):
        """Customer coordinates sit within jitter of their city's centroid."""
        cfg = self._minimal_cfg(n=200)
        customers_df, *_ = self._run(cfg)
        geo = self._fake_geography()
        exp_lat = customers_df["GeographyKey"].map(dict(zip(geo["GeographyKey"], geo["Latitude"])))
        exp_lon = customers_df["GeographyKey"].map(dict(zip(geo["GeographyKey"], geo["Longitude"])))
        assert ((customers_df["Latitude"] - exp_lat).abs() <= 0.1501).all()
        assert ((customers_df["Longitude"] - exp_lon).abs() <= 0.1501).all()

    def test_birth_city_same_country(self):
        """BirthCity is a real city; for non-moved heads it matches their country."""
        cfg = self._minimal_cfg(n=300)
        customers_df, profile_df, *_ = self._run(cfg)
        geo = self._fake_geography()
        assert profile_df["BirthCity"].isin(geo["City"]).all()
        key_to_country = dict(zip(geo["GeographyKey"], geo["Country"]))
        city_to_country = dict(zip(geo["City"], geo["Country"]))
        merged = profile_df.merge(
            customers_df[["CustomerKey", "GeographyKey", "HouseholdRole"]],
            on="CustomerKey", how="left",
        )
        # Heads keep their own geography; spouses/dependents inherit the head's,
        # which can legitimately differ from their birth country.
        heads = merged[merged["HouseholdRole"] == "Head"]
        cur = heads["GeographyKey"].map(key_to_country)
        birth = heads["BirthCity"].map(city_to_country)
        assert (birth == cur).all()

    def test_name_split_columns(self):
        """Name split into Title/First/Middle/Last/FullName; CustomerName removed."""
        cfg = self._minimal_cfg(n=300)
        cfg["customers"]["pct_org"] = 12
        customers_df, *_ = self._run(cfg)
        assert "CustomerName" not in customers_df.columns
        for col in ("Title", "FirstName", "MiddleName", "LastName", "FullName"):
            assert col in customers_df.columns
        person = customers_df["CustomerType"] == "Individual"
        org = ~person
        assert customers_df.loc[person, "FirstName"].notna().all()
        assert customers_df.loc[person, "LastName"].notna().all()
        assert customers_df["FullName"].notna().all()
        # Orgs carry the org name in FullName but no personal name parts/title.
        assert customers_df.loc[org, "FirstName"].isna().all()
        assert customers_df.loc[org, "Title"].isna().all()
        # FullName for persons is "First Last".
        expect = (customers_df.loc[person, "FirstName"].astype(str) + " "
                  + customers_df.loc[person, "LastName"].astype(str))
        assert (customers_df.loc[person, "FullName"] == expect).all()

    def test_middle_name_sparse(self):
        """MiddleName is a sparse single initial (~35% of persons)."""
        cfg = self._minimal_cfg(n=1000)
        customers_df, *_ = self._run(cfg)
        person = customers_df["CustomerType"] == "Individual"
        mid = customers_df.loc[person, "MiddleName"]
        assert 0.25 < mid.notna().mean() < 0.45
        assert mid.dropna().str.match(r"^[A-Z]\.$").all()

    def test_title_salutation(self):
        """Title is a salutation for persons, null for orgs."""
        cfg = self._minimal_cfg(n=300)
        cfg["customers"]["pct_org"] = 12
        customers_df, *_ = self._run(cfg)
        person = customers_df["CustomerType"] == "Individual"
        titles = set(customers_df.loc[person, "Title"].dropna().unique())
        # Gendered salutations + small Dr overlay; no gender-neutral Mx (Gender
        # is strictly M/F/O).
        assert titles.issubset({"Mr", "Mrs", "Ms", "Dr"})
        assert "Mx" not in titles
        assert customers_df.loc[person, "Title"].notna().all()
        assert customers_df.loc[~person, "Title"].isna().all()

    def test_title_consistent_with_marital_after_household(self):
        """Spouse marital-status writeback keeps Title in step: no Married person
        carries the single-female 'Ms' salutation (AN-4a follow-up)."""
        cfg = self._minimal_cfg(n=600)
        customers_df, *_ = self._run(cfg)
        married = customers_df["MaritalStatus"] == "Married"
        assert (customers_df.loc[married, "Title"] != "Ms").all()
        # spouses (a population that includes originally-Single people) are Married
        spouses = customers_df[customers_df["HouseholdRole"] == "Spouse"]
        assert len(spouses) > 0
        assert (spouses["MaritalStatus"] == "Married").all()
        assert (spouses["Title"] != "Ms").all()

    # --- CUST-AN-3: income realism (age curve, regional, high-earner tail) ---
    @staticmethod
    def _income_inputs(n, label_edu, label_occ):
        edu = np.array([label_edu] * n, dtype=object)
        occ = np.array([label_occ] * n, dtype=object)
        return edu, occ, np.ones(n, dtype=bool)

    def test_income_age_experience_curve(self):
        """Holding education/occupation fixed, income rises through mid-career
        then tapers toward retirement (AN-3 experience curve)."""
        from src.dimensions.customers.helpers import generate_correlated_income
        rng = np.random.default_rng(7)
        n = 40000
        edu, occ, pmask = self._income_inputs(n, "Bachelors", "Professional")
        region = np.array(["US"] * n, dtype=object)
        means = [
            generate_correlated_income(
                rng, edu, occ, pmask, n,
                age_bracket=np.full(n, b), region=region,
            ).mean()
            for b in range(6)
        ]
        # 18-24 well below mid-career; a peak in the 45-54 band; taper at 65+.
        assert means[0] < means[1] < means[2] < means[3]
        assert means[5] < means[3]

    def test_income_regional_adjustment(self):
        """Same age/education draws scale down US > EU > AS > IN (AN-3)."""
        from src.dimensions.customers.helpers import generate_correlated_income
        rng = np.random.default_rng(11)
        n = 40000
        edu, occ, pmask = self._income_inputs(n, "Bachelors", "Professional")
        ab = np.full(n, 2)
        m = {
            r: generate_correlated_income(
                rng, edu, occ, pmask, n,
                age_bracket=ab, region=np.array([r] * n, dtype=object),
            ).mean()
            for r in ("US", "EU", "AS", "IN")
        }
        assert m["US"] > m["EU"] > m["AS"] > m["IN"]

    def test_income_high_earner_tail_preserved(self):
        """The raised clip lets a varied high-earner tail (>200K) survive rather
        than piling onto a single 200K wall (AN-3)."""
        from src.dimensions.customers.helpers import generate_correlated_income
        from src.defaults import CUSTOMER_INCOME_MAX
        rng = np.random.default_rng(3)
        n = 60000
        edu, occ, pmask = self._income_inputs(n, "PhD", "Executive")
        inc = generate_correlated_income(
            rng, edu, occ, pmask, n,
            age_bracket=np.full(n, 3), region=np.array(["US"] * n, dtype=object),
        )
        high = inc[inc > 200_000]
        assert high.size > 0
        assert np.unique(high).size > 5          # a spread, not a clipped wall
        assert inc.max() <= CUSTOMER_INCOME_MAX

    # --- CUST-AN-4: household <-> demographic consistency ---
    def test_household_spouse_is_married(self):
        """A matched Spouse is recorded as Married, never Single (AN-4a)."""
        cfg = self._minimal_cfg(n=600)
        customers_df, *_ = self._run(cfg)
        spouses = customers_df[customers_df["HouseholdRole"] == "Spouse"]
        assert len(spouses) > 0
        assert (spouses["MaritalStatus"] == "Married").all()

    def test_household_members_inherit_head_geo_profile(self):
        """Moved members take the head's region-derived profile columns, so no
        stale pre-move timezone/language/distance survives (AN-4b)."""
        cfg = self._minimal_cfg(n=600)
        customers_df, profile_df, *_ = self._run(cfg)
        merged = profile_df.merge(
            customers_df[["CustomerKey", "HouseholdKey", "HouseholdRole"]],
            on="CustomerKey", how="left",
        )
        cols = ["TimeZone", "PreferredLanguage", "UrbanRural",
                "DistanceToNearestStoreKm"]
        heads = merged[merged["HouseholdRole"] == "Head"][["HouseholdKey"] + cols]
        heads = heads.rename(columns={c: f"head_{c}" for c in cols})
        members = merged[merged["HouseholdRole"].isin(["Spouse", "Dependent"])].merge(
            heads, on="HouseholdKey", how="inner",
        )
        assert len(members) > 0
        for c in cols:
            assert (members[c] == members[f"head_{c}"]).all()

    def test_dependent_income_capped(self):
        """Dependents are capped at the entry-level ceiling while non-dependents
        can exceed it (AN-4c)."""
        from src.defaults import CUSTOMER_DEPENDENT_INCOME_CAP
        cfg = self._minimal_cfg(n=800)
        customers_df, *_ = self._run(cfg)
        deps = customers_df[customers_df["HouseholdRole"] == "Dependent"]
        assert len(deps) > 0
        dep_inc = deps["YearlyIncome"].dropna().astype("int64")
        assert (dep_inc <= CUSTOMER_DEPENDENT_INCOME_CAP).all()
        nondep_inc = customers_df[
            (customers_df["CustomerType"] == "Individual")
            & (customers_df["HouseholdRole"] != "Dependent")
        ]["YearlyIncome"].dropna().astype("int64")
        assert (nondep_inc > CUSTOMER_DEPENDENT_INCOME_CAP).any()

    # --- CUST-AN-5: senior segment no longer truncated at 70 ---
    def test_age_distribution_has_senior_tail(self):
        """Ages span a realistic pyramid with a thinning 70-85 senior tail
        instead of a hard uniform cut at 70 (AN-5)."""
        from src.defaults import CUSTOMER_AGE_MAX_YEARS
        cfg = self._minimal_cfg(n=3000)
        customers_df, *_ = self._run(cfg)
        end = pd.Timestamp(cfg["defaults"]["dates"]["end"])
        person = customers_df["CustomerType"] == "Individual"
        dob = customers_df.loc[person, "DOB"].dropna()
        age = (end - pd.to_datetime(dob)).dt.days / 365.25
        assert age.max() > 70                       # seniors past 70 now exist
        assert age.max() <= CUSTOMER_AGE_MAX_YEARS + 0.2
        assert (age >= 70).mean() > 0.02            # a real (thin) senior tail
        assert (age >= 70).mean() < 0.20            # but not over-weighted
        assert age.min() >= 18

    # --- CUST-AN-7: parallel chunk count is worker-count independent ---
    def test_chunk_count_depends_only_on_n(self):
        """The parallel chunk count is a pure function of N (no worker arg), so
        --workers can't reshuffle the per-chunk RNG streams (AN-7)."""
        from src.dimensions.customers.generator import _customer_chunk_count
        assert _customer_chunk_count(10_000) == 2          # floored at 2
        assert _customer_chunk_count(150_000) == 3         # 150000 // 50000
        assert _customer_chunk_count(1_000_000) == 20
        assert _customer_chunk_count(10_000_000) == 64     # capped
        assert _customer_chunk_count(100_000_000) == 64
        # monotonic non-decreasing in N
        prev = 0
        for n in (200_000, 500_000, 2_000_000, 50_000_000):
            cur = _customer_chunk_count(n)
            assert cur >= prev
            prev = cur

    def test_scd2_chunk_count_depends_only_on_changed(self):
        """SCD2 expansion chunk count is a pure function of the changed-row count
        (no worker arg), so --workers can't reshuffle the version history
        (AN-7, SCD2 path)."""
        from src.dimensions.customers.generator import _scd2_chunk_count
        assert _scd2_chunk_count(1_000) == 2            # floored at 2
        assert _scd2_chunk_count(15_000) == 3           # 15000 // 5000
        assert _scd2_chunk_count(600_000) == 64         # capped
        prev = 0
        for n in (20_000, 100_000, 1_000_000):
            cur = _scd2_chunk_count(n)
            assert cur >= prev
            prev = cur

    # --- CUST-AN-9: email uniqueness rests on a globally-unique key suffix ---
    def test_email_unique_with_global_keys(self):
        """build_email_addresses yields unique emails for unique keys even when
        names collide — the property the parallel orchestrator relies on when it
        rebuilds emails against the post-merge global key (AN-9)."""
        from src.dimensions.customers.generator import build_email_addresses
        rng = np.random.default_rng(0)
        n = 5000
        first = np.array(["John"] * n, dtype=object)   # deliberately identical
        last = np.array(["Smith"] * n, dtype=object)
        em = build_email_addresses(
            rng, safe_first=first, safe_last=last,
            org_name=np.array([None] * n, dtype=object),
            keys=np.arange(1, n + 1), is_org=np.zeros(n, dtype=bool),
        )
        assert len(set(em.tolist())) == n
        assert all("@" in e for e in em)

    # --- CUST-AN-10: robust normalization keeps derived buckets non-degenerate ---
    def test_robust_unit_norm_fixed_reference(self):
        """Normalization is against a FIXED reference, so the mapping is
        identical regardless of N and of how the data is chunked (AN-10)."""
        from src.dimensions.customers.generator import (
            _robust_unit_norm, lognormal_p95_ref,
        )
        ref = lognormal_p95_ref(0.6)
        rng = np.random.default_rng(1)
        small = rng.lognormal(0.0, 0.6, 20_000)
        large = rng.lognormal(0.0, 0.6, 1_000_000)
        # Same value -> same normalized result no matter which array it's in.
        x = np.array([0.5, 1.0, ref, 10.0])
        assert np.allclose(_robust_unit_norm(x, ref), np.clip(x / ref, 0, 1))
        assert _robust_unit_norm(np.array([ref]), ref)[0] == 1.0   # p95 -> ~1.0
        # The bulk is spread across [0,1], stable across N (by construction).
        ms, ml = _robust_unit_norm(small, ref).mean(), _robust_unit_norm(large, ref).mean()
        assert 0.2 < ms < 0.65 and abs(ms - ml) < 0.02
        # max-normalization (the old degenerate approach) compresses far more.
        assert (large / large.max()).mean() < ml

    def test_spend_and_churn_buckets_populated(self):
        """AnnualSpendBucket and ChurnRisk populate every tier instead of
        collapsing onto Low (AN-10)."""
        cfg = self._minimal_cfg(n=4000)
        _, profile_df, *_ = self._run(cfg)
        sb = profile_df["AnnualSpendBucket"].value_counts(normalize=True)
        for tier in ("Low", "Medium", "High", "VIP"):
            assert sb.get(tier, 0) > 0.005, f"spend tier {tier} empty"
        cr = profile_df["ChurnRisk"].value_counts(normalize=True)
        for tier in ("Low", "Medium", "High"):
            assert cr.get(tier, 0) > 0.005, f"churn tier {tier} empty"


# ===================================================================
# BUILD YEAR WINDOWS (promotions helper)
# ===================================================================

class TestBuildYearWindows:
    def test_single_year(self):
        years, windows = _build_year_windows(
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31")
        )
        assert years == [2024]
        assert windows[2024][0] == pd.Timestamp("2024-01-01")
        assert windows[2024][1] == pd.Timestamp("2024-12-31")

    def test_multi_year(self):
        years, windows = _build_year_windows(
            pd.Timestamp("2022-06-01"), pd.Timestamp("2024-03-15")
        )
        assert years == [2022, 2023, 2024]
        # First year should start at the provided start
        assert windows[2022][0] == pd.Timestamp("2022-06-01")
        # Last year should end at the provided end
        assert windows[2024][1] == pd.Timestamp("2024-03-15")

    def test_end_before_start_raises(self):
        with pytest.raises(DimensionError, match="end < start"):
            _build_year_windows(
                pd.Timestamp("2024-12-31"), pd.Timestamp("2024-01-01")
            )


# ===================================================================
# EXCHANGE RATES (resolve_fx_dates)
# ===================================================================

class TestResolveFxDates:
    """Tests for resolve_fx_dates which always reads from cfg.defaults.dates."""

    def _cfg_with_dates(self, start="2021-01-01", end="2025-12-31"):
        from types import SimpleNamespace
        return SimpleNamespace(
            defaults=SimpleNamespace(dates=SimpleNamespace(start=start, end=end))
        )

    def test_returns_global_dates(self):
        from src.dimensions.exchange_rates.helpers import resolve_fx_dates
        cfg = self._cfg_with_dates("2021-01-01", "2025-12-31")
        start, end = resolve_fx_dates(cfg)
        assert start == "2021-01-01"
        assert end == "2025-12-31"

    def test_missing_defaults_raises(self):
        from types import SimpleNamespace
        from src.dimensions.exchange_rates.helpers import resolve_fx_dates
        from src.exceptions import ConfigError
        cfg = SimpleNamespace(defaults=None)
        with pytest.raises(ConfigError, match="global defaults dates"):
            resolve_fx_dates(cfg)

    def test_missing_dates_obj_raises(self):
        from types import SimpleNamespace
        from src.dimensions.exchange_rates.helpers import resolve_fx_dates
        from src.exceptions import ConfigError
        cfg = SimpleNamespace(defaults=SimpleNamespace(dates=None))
        with pytest.raises(ConfigError, match="global defaults dates"):
            resolve_fx_dates(cfg)


# ===================================================================
# CROSS-RATE TRIANGULATION
# ===================================================================

class TestTriangulateRates:
    """Tests for cross-rate triangulation logic."""

    def _master(self):
        """Build a simple 3-day USD master with EUR and GBP."""
        dates = pd.date_range("2024-01-01", periods=3, freq="D")
        rows = []
        for d in dates:
            rows.append({"Date": d.date(), "FromCurrency": "USD", "ToCurrency": "EUR", "Rate": 0.90})
            rows.append({"Date": d.date(), "FromCurrency": "USD", "ToCurrency": "GBP", "Rate": 0.80})
        return pd.DataFrame(rows)

    def test_direct_usd_to_eur(self):
        from src.dimensions.exchange_rates.exchange_rates import _triangulate_rates
        df = _triangulate_rates(self._master(), ["USD"], ["EUR"])
        assert len(df) == 3
        assert (df["FromCurrency"] == "USD").all()
        assert (df["ToCurrency"] == "EUR").all()
        assert np.allclose(df["Rate"], 0.90)

    def test_inverse_eur_to_usd(self):
        from src.dimensions.exchange_rates.exchange_rates import _triangulate_rates
        df = _triangulate_rates(self._master(), ["EUR"], ["USD"])
        assert len(df) == 3
        assert np.allclose(df["Rate"], 1.0 / 0.90)

    def test_cross_eur_to_gbp(self):
        from src.dimensions.exchange_rates.exchange_rates import _triangulate_rates
        df = _triangulate_rates(self._master(), ["EUR"], ["GBP"])
        assert len(df) == 3
        # EUR→GBP = rate(USD→GBP) / rate(USD→EUR) = 0.80 / 0.90
        assert np.allclose(df["Rate"], 0.80 / 0.90)

    def test_same_currency_excluded(self):
        from src.dimensions.exchange_rates.exchange_rates import _triangulate_rates
        df = _triangulate_rates(self._master(), ["EUR"], ["EUR"])
        assert len(df) == 0

    def test_multiple_from_to(self):
        from src.dimensions.exchange_rates.exchange_rates import _triangulate_rates
        df = _triangulate_rates(self._master(), ["USD", "EUR"], ["EUR", "GBP"])
        pairs = set(zip(df["FromCurrency"], df["ToCurrency"]))
        # USD→EUR, USD→GBP, EUR→GBP (EUR→EUR excluded)
        assert ("USD", "EUR") in pairs
        assert ("USD", "GBP") in pairs
        assert ("EUR", "GBP") in pairs
        assert ("EUR", "EUR") not in pairs


# ===================================================================
# MONTHLY RATES AGGREGATION
# ===================================================================

class TestBuildMonthlyRates:
    """Tests for monthly exchange rate aggregation."""

    def _daily(self):
        dates = pd.date_range("2024-01-01", "2024-03-31", freq="D")
        rows = []
        for i, d in enumerate(dates):
            rows.append({
                "Date": d.date(),
                "FromCurrencyKey": 1,
                "ToCurrencyKey": 2,
                "FromCurrency": "USD",
                "ToCurrency": "EUR",
                "Rate": 0.90 + (i % 10) * 0.001,
            })
        return pd.DataFrame(rows)

    def test_output_columns(self):
        df = build_monthly_rates(self._daily())
        assert list(df.columns) == [
            "Date", "FromCurrencyKey", "ToCurrencyKey",
            "FromCurrency", "ToCurrency",
            "AvgRate", "MinRate", "MaxRate", "EndOfMonthRate",
        ]

    def test_monthly_grain(self):
        df = build_monthly_rates(self._daily())
        assert len(df) == 3  # Jan, Feb, Mar

    def test_min_max_bounds(self):
        df = build_monthly_rates(self._daily())
        assert (df["MinRate"] <= df["AvgRate"]).all()
        assert (df["AvgRate"] <= df["MaxRate"]).all()

    def test_end_of_month_rate(self):
        daily = self._daily()
        monthly = build_monthly_rates(daily)
        # EndOfMonthRate for Jan should be the last day of Jan
        jan = monthly[monthly["Date"] == pd.Timestamp("2024-01-01")].iloc[0]
        daily_jan = daily[pd.to_datetime(daily["Date"]).dt.month == 1]
        assert jan["EndOfMonthRate"] == daily_jan.iloc[-1]["Rate"]


# ===================================================================
# CROSS-GENERATOR DATA QUALITY
# ===================================================================

class TestCrossGeneratorQuality:
    """Integration-like tests verifying consistency across generators."""

    def test_stores_geography_key_exists_in_geo(self, geo_keys):
        """Every store GeographyKey should be a valid geo key."""
        df = generate_store_table(geo=GeoContext(geo_keys=geo_keys), num_stores=50, seed=42)
        store_geos = set(df["GeographyKey"].astype(np.int64).tolist())
        valid_geos = set(geo_keys.tolist())
        assert store_geos.issubset(valid_geos)

    def test_promotion_no_discount_key_is_one(self):
        """No Discount sentinel must always be PromotionKey=1."""
        years, windows = _build_year_windows(
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31")
        )
        df = generate_promotions_catalog(years=years, year_windows=windows, seed=42)
        assert df["PromotionKey"].min() == 1
        nd = df[df["PromotionKey"] == 1]
        assert nd.iloc[0]["PromotionName"] == "No Discount"

    def test_currency_codes_match_defaults(self):
        """Currency codes from build_dim_currency should be uppercase 3-letter."""
        df = build_dim_currency(["USD", "EUR", "GBP", "INR"])
        for code in df["CurrencyCode"]:
            assert len(code) == 3
            assert code == code.upper()
            assert code.isalpha()
