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
from src.dimensions.employees import generate_employee_dimension


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

    def test_employee_count_one_means_no_staff(self, people_pools):
        """EmployeeCount=1 means only the store manager, zero staff."""
        stores = self._make_stores(n=3)
        stores["EmployeeCount"] = 1  # just the manager
        df = generate_employee_dimension(
            stores=stores,
            seed=42,
            global_start=pd.Timestamp("2021-01-01"),
            global_end=pd.Timestamp("2025-12-31"),
            people_pools=people_pools,
        )
        # Should still have corporate + regional + district + store managers
        staff = df[df["OrgLevel"] == 6]
        assert len(staff) == 0

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
        org_count = (customers_df["Gender"] == "Org").sum()
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
        org_mask = customers_df["Gender"] == "Org"
        assert (customers_df.loc[org_mask, "CustomerType"] == "Organization").all()
        assert (customers_df.loc[~org_mask, "CustomerType"] == "Individual").all()


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
