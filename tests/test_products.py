"""Comprehensive tests for product dimension modules.

Covers: suppliers, contoso_expander, pricing, and product_profile.
Each module is tested for output shape, column presence, determinism,
data quality, edge cases, and (where applicable) probability distributions.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import numpy.testing as npt
import pandas as pd
import pytest

from src.exceptions import DimensionError

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from src.dimensions.products.suppliers import generate_suppliers_table
from src.dimensions.products.contoso_expander import (
    expand_contoso_products,
    _stratified_trim_indices,
)
from src.dimensions.products.pricing import (
    apply_product_pricing,
    _rescale_to_range,
    _snap_unit_price_to_points,
    _round_to_step,
    DEFAULT_PRICE_BANDS,
    DEFAULT_COST_BANDS,
    DEFAULT_PRICE_ENDING,
)
from src.dimensions.products.product_profile import (
    enrich_products_attributes,
    _splitmix64,
)


# ===================================================================
# Shared fixtures
# ===================================================================

@pytest.fixture
def base_products():
    return pd.DataFrame({
        "ProductKey": np.arange(1, 21, dtype=np.int64),
        "SubcategoryKey": np.tile([10, 20, 30, 40], 5).astype(np.int64),
        "ListPrice": np.linspace(10.0, 500.0, 20),
        "UnitCost": np.linspace(5.0, 250.0, 20),
        "ProductName": [f"Product {i}" for i in range(1, 21)],
        "BrandName": [f"Brand{i % 3}" for i in range(20)],
        "Color": ["Red", "Blue", "Green", "Black", "White"] * 4,
        "BaseProductID": np.arange(1, 21, dtype=np.int64),
        "VariantIndex": np.zeros(20, dtype=np.int32),
    })


# ===================================================================
# TestGenerateSuppliersTable
# ===================================================================

class TestGenerateSuppliersTable:
    """Tests for suppliers.generate_suppliers_table()."""

    def test_default_output_shape(self):
        df = generate_suppliers_table()
        assert len(df) == 250
        assert "SupplierKey" in df.columns
        assert "SupplierName" in df.columns
        assert "SupplierType" in df.columns

    def test_custom_num_suppliers(self):
        df = generate_suppliers_table(num_suppliers=10)
        assert len(df) == 10

    def test_key_range_default(self):
        df = generate_suppliers_table(num_suppliers=50)
        keys = df["SupplierKey"].to_numpy()
        assert keys.min() == 1
        assert keys.max() == 50
        assert len(np.unique(keys)) == 50

    def test_key_range_custom_start(self):
        df = generate_suppliers_table(num_suppliers=5, start_key=100)
        keys = df["SupplierKey"].to_numpy()
        npt.assert_array_equal(keys, np.arange(100, 105, dtype=np.int32))

    def test_supplier_key_dtype(self):
        df = generate_suppliers_table(num_suppliers=10)
        assert df["SupplierKey"].dtype == np.int32

    def test_supplier_types_valid(self):
        df = generate_suppliers_table(num_suppliers=500, seed=99)
        valid_types = {"Manufacturer", "Distributor", "PrivateLabel"}
        assert set(df["SupplierType"].unique()).issubset(valid_types)

    def test_supplier_type_distribution_approximate(self):
        """With a large sample the distribution should be roughly 55/35/10."""
        df = generate_suppliers_table(num_suppliers=5000, seed=42)
        counts = df["SupplierType"].value_counts(normalize=True)
        assert abs(counts.get("Manufacturer", 0) - 0.55) < 0.05
        assert abs(counts.get("Distributor", 0) - 0.35) < 0.05
        assert abs(counts.get("PrivateLabel", 0) - 0.10) < 0.05

    def test_determinism_same_seed(self):
        df1 = generate_suppliers_table(num_suppliers=20, seed=123)
        df2 = generate_suppliers_table(num_suppliers=20, seed=123)
        pd.testing.assert_frame_equal(df1, df2)

    def test_different_seed_differs(self):
        df1 = generate_suppliers_table(num_suppliers=20, seed=1)
        df2 = generate_suppliers_table(num_suppliers=20, seed=2)
        # Names should differ (extremely unlikely to be identical)
        assert not df1["SupplierName"].equals(df2["SupplierName"])

    def test_include_country_true(self):
        df = generate_suppliers_table(num_suppliers=10, include_country=True)
        assert "Country" in df.columns
        assert df["Country"].notna().all()

    def test_include_country_false(self):
        df = generate_suppliers_table(num_suppliers=10, include_country=False)
        assert "Country" not in df.columns

    def test_include_reliability_true(self):
        df = generate_suppliers_table(num_suppliers=10, include_reliability=True)
        assert "ReliabilityScore" in df.columns
        scores = df["ReliabilityScore"].to_numpy()
        assert np.all(scores >= 0.0)
        assert np.all(scores <= 1.0)
        assert df["ReliabilityScore"].dtype == np.float64

    def test_include_reliability_false(self):
        df = generate_suppliers_table(num_suppliers=10, include_reliability=False)
        assert "ReliabilityScore" not in df.columns

    def test_custom_countries(self):
        df = generate_suppliers_table(
            num_suppliers=50, include_country=True,
            countries=["Narnia", "Gondor"],
        )
        assert set(df["Country"].unique()).issubset({"Narnia", "Gondor"})

    def test_private_label_naming(self):
        """PrivateLabel suppliers should have 'Contoso' in the name."""
        df = generate_suppliers_table(num_suppliers=500, seed=42)
        pl = df[df["SupplierType"] == "PrivateLabel"]
        assert len(pl) > 0
        assert pl["SupplierName"].str.contains("Contoso").all()

    def test_zero_suppliers_raises(self):
        with pytest.raises(DimensionError):
            generate_suppliers_table(num_suppliers=0)

    def test_negative_start_key_raises(self):
        with pytest.raises(DimensionError):
            generate_suppliers_table(start_key=-1)

    def test_single_supplier(self):
        df = generate_suppliers_table(num_suppliers=1)
        assert len(df) == 1
        assert df["SupplierKey"].iloc[0] == 1


# ===================================================================
# TestStratifiedTrimIndices
# ===================================================================

class TestStratifiedTrimIndices:
    """Tests for contoso_expander._stratified_trim_indices()."""

    def test_returns_correct_count(self):
        groups = np.array([1, 1, 1, 2, 2, 2, 3, 3, 3])
        idx = _stratified_trim_indices(groups, 6, seed=42)
        assert len(idx) == 6

    def test_preserves_group_proportions(self):
        """With equal groups, each should get roughly equal share."""
        groups = np.repeat([1, 2, 3], 100)
        idx = _stratified_trim_indices(groups, 30, seed=42)
        selected_groups = groups[idx]
        counts = np.bincount(selected_groups.astype(int))[1:]  # skip bin 0
        # Each group should get ~10 from 30 total
        assert all(8 <= c <= 12 for c in counts)

    def test_target_exceeds_total_returns_all(self):
        groups = np.array([1, 2, 3])
        idx = _stratified_trim_indices(groups, 100, seed=42)
        assert len(idx) == 3

    def test_target_zero_returns_empty(self):
        groups = np.array([1, 2, 3])
        idx = _stratified_trim_indices(groups, 0, seed=42)
        assert len(idx) == 0

    def test_empty_input_returns_empty(self):
        groups = np.array([], dtype=np.int64)
        idx = _stratified_trim_indices(groups, 5, seed=42)
        assert len(idx) == 0

    def test_deterministic(self):
        groups = np.repeat([1, 2, 3, 4], 25)
        idx1 = _stratified_trim_indices(groups, 20, seed=99)
        idx2 = _stratified_trim_indices(groups, 20, seed=99)
        npt.assert_array_equal(idx1, idx2)


# ===================================================================
# TestExpandContosoProducts
# ===================================================================

class TestExpandContosoProducts:
    """Tests for contoso_expander.expand_contoso_products()."""

    def test_trim_shape(self, base_products):
        df = expand_contoso_products(base_products, num_products=10, seed=42)
        assert len(df) == 10

    def test_noop_shape(self, base_products):
        df = expand_contoso_products(base_products, num_products=20, seed=42)
        assert len(df) == 20

    def test_expand_shape(self, base_products):
        df = expand_contoso_products(base_products, num_products=50, seed=42)
        assert len(df) == 50

    def test_trim_dense_surrogate_keys(self, base_products):
        df = expand_contoso_products(base_products, num_products=10, seed=42)
        keys = df["ProductKey"].to_numpy()
        npt.assert_array_equal(keys, np.arange(1, 11, dtype=np.int64))

    def test_expand_dense_surrogate_keys(self, base_products):
        df = expand_contoso_products(base_products, num_products=50, seed=42)
        keys = df["ProductKey"].to_numpy()
        npt.assert_array_equal(keys, np.arange(1, 51, dtype=np.int64))

    def test_trim_variant_index_zero(self, base_products):
        df = expand_contoso_products(base_products, num_products=10, seed=42)
        npt.assert_array_equal(
            df["VariantIndex"].to_numpy(),
            np.zeros(10, dtype=np.int64),
        )

    def test_trim_base_equals_product_key(self, base_products):
        """In trim mode, BaseProductID equals ProductKey (each is its own base)."""
        df = expand_contoso_products(base_products, num_products=10, seed=42)
        npt.assert_array_equal(
            df["BaseProductID"].to_numpy(),
            df["ProductKey"].to_numpy(),
        )

    def test_expand_variant_naming(self, base_products):
        """Expanded variants with index > 0 get ' - V{index}' suffix."""
        df = expand_contoso_products(base_products, num_products=50, seed=42)
        variants = df[df["VariantIndex"] > 0]
        assert len(variants) > 0
        assert variants["ProductName"].str.contains(" - V").all()

    def test_expand_base_variants_no_suffix(self, base_products):
        """Base variants (VariantIndex == 0) keep original name."""
        df = expand_contoso_products(base_products, num_products=50, seed=42)
        bases = df[df["VariantIndex"] == 0]
        assert len(bases) > 0
        assert not bases["ProductName"].str.contains(" - V").any()

    def test_product_code_present(self, base_products):
        df = expand_contoso_products(base_products, num_products=10, seed=42)
        assert "ProductCode" in df.columns
        # ProductCode should be zero-padded
        assert df["ProductCode"].str.len().min() >= 7

    def test_stratified_proportions_on_trim(self, base_products):
        """Trimming should roughly preserve subcategory proportions."""
        df = expand_contoso_products(base_products, num_products=12, seed=42)
        # Original has 4 subcategories with 5 rows each (25% each).
        # Trimming to 12 should give ~3 per subcategory.
        counts = df["SubcategoryKey"].value_counts()
        assert counts.min() >= 2
        assert counts.max() <= 4

    def test_determinism(self, base_products):
        df1 = expand_contoso_products(base_products, num_products=15, seed=42)
        df2 = expand_contoso_products(base_products, num_products=15, seed=42)
        pd.testing.assert_frame_equal(df1, df2)

    def test_different_seed_differs(self, base_products):
        df1 = expand_contoso_products(base_products, num_products=10, seed=1)
        df2 = expand_contoso_products(base_products, num_products=10, seed=2)
        # With different seeds the trimmed selection should differ
        assert not df1["ProductName"].equals(df2["ProductName"])

    def test_zero_products_raises(self, base_products):
        with pytest.raises(DimensionError):
            expand_contoso_products(base_products, num_products=0)

    def test_negative_products_raises(self, base_products):
        with pytest.raises(DimensionError):
            expand_contoso_products(base_products, num_products=-5)

    def test_empty_base_raises(self):
        empty = pd.DataFrame({
            "ProductKey": pd.array([], dtype=np.int64),
            "SubcategoryKey": pd.array([], dtype=np.int64),
            "ListPrice": pd.array([], dtype=np.float64),
            "UnitCost": pd.array([], dtype=np.float64),
            "ProductName": pd.array([], dtype=object),
        })
        with pytest.raises(DimensionError):
            expand_contoso_products(empty, num_products=5)

    def test_expand_variant_index_increments(self, base_products):
        """Each base product's variants should have incrementing VariantIndex."""
        df = expand_contoso_products(base_products, num_products=60, seed=42)
        for _, grp in df.groupby("BaseProductID"):
            vi = grp["VariantIndex"].sort_values().to_numpy()
            assert vi[0] == 0
            # Each subsequent variant index should be one more than previous
            npt.assert_array_equal(vi, np.arange(len(vi), dtype=np.int64))


# ===================================================================
# TestApplyProductPricing
# ===================================================================

class TestApplyProductPricing:
    """Tests for pricing.apply_product_pricing()."""

    def _margin_cfg(self, **overrides):
        """Minimal pricing config using margin mode."""
        cfg = {
            "base": {"value_scale": 1.0},
            "cost": {
                "mode": "margin",
                "min_margin_pct": 0.20,
                "max_margin_pct": 0.40,
            },
        }
        cfg.update(overrides)
        return cfg

    def test_cost_leq_price_invariant(self, base_products):
        cfg = self._margin_cfg()
        df = apply_product_pricing(base_products, cfg, seed=42)
        assert (df["UnitCost"] <= df["ListPrice"]).all()

    def test_prices_non_negative(self, base_products):
        cfg = self._margin_cfg()
        df = apply_product_pricing(base_products, cfg, seed=42)
        assert (df["ListPrice"] >= 0).all()
        assert (df["UnitCost"] >= 0).all()

    def test_prices_finite(self, base_products):
        cfg = self._margin_cfg()
        df = apply_product_pricing(base_products, cfg, seed=42)
        assert np.all(np.isfinite(df["ListPrice"].to_numpy()))
        assert np.all(np.isfinite(df["UnitCost"].to_numpy()))

    def test_value_scale_doubles_prices(self, base_products):
        cfg_1x = self._margin_cfg(base={"value_scale": 1.0})
        cfg_2x = self._margin_cfg(base={"value_scale": 2.0})
        # Use keep mode so cost scaling doesn't randomize
        cfg_1x["cost"] = {"mode": "keep"}
        cfg_2x["cost"] = {"mode": "keep"}

        df1 = apply_product_pricing(base_products, cfg_1x, seed=42)
        df2 = apply_product_pricing(base_products, cfg_2x, seed=42)

        # ListPrice after 2x scale should be roughly 2x of 1x
        ratio = df2["ListPrice"].to_numpy() / df1["ListPrice"].to_numpy()
        npt.assert_allclose(ratio, 2.0, atol=0.01)

    def test_margin_mode_bounds(self, base_products):
        """In margin mode, margin = (price - cost) / price should be within bounds."""
        cfg = self._margin_cfg()
        df = apply_product_pricing(base_products, cfg, seed=42)
        up = df["ListPrice"].to_numpy(dtype=np.float64)
        uc = df["UnitCost"].to_numpy(dtype=np.float64)
        margin = (up - uc) / up
        assert np.all(margin >= 0.19)  # slight tolerance for rounding
        assert np.all(margin <= 0.41)

    def test_rescale_to_range(self, base_products):
        cfg = {
            "base": {
                "value_scale": 1.0,
                "rescale_to_range": True,
                "min_unit_price": 5.0,
                "max_unit_price": 100.0,
            },
            "cost": {"mode": "margin", "min_margin_pct": 0.20, "max_margin_pct": 0.40},
        }
        df = apply_product_pricing(base_products, cfg, seed=42)
        assert df["ListPrice"].min() >= 5.0 - 0.01
        assert df["ListPrice"].max() <= 100.0 + 0.01

    def test_empty_dataframe(self, base_products):
        empty = base_products.iloc[:0].copy()
        cfg = self._margin_cfg()
        df = apply_product_pricing(empty, cfg, seed=42)
        assert len(df) == 0

    def test_missing_list_price_raises(self):
        df = pd.DataFrame({"ProductKey": [1, 2, 3]})
        cfg = {"base": {"value_scale": 1.0}, "cost": {"mode": "keep"}}
        with pytest.raises(DimensionError, match="ListPrice"):
            apply_product_pricing(df, cfg, seed=42)

    def test_none_pricing_cfg_returns_unchanged(self, base_products):
        df = apply_product_pricing(base_products, {}, seed=42)
        pd.testing.assert_frame_equal(df, base_products)

    def test_snap_unit_price(self, base_products):
        cfg = {
            "base": {"value_scale": 1.0},
            "cost": {"mode": "keep"},
            "appearance": {"snap_unit_price": True, "price_ending": 0.99},
        }
        df = apply_product_pricing(base_products, cfg, seed=42)
        # Snapped prices should end in .99 (fractional part)
        frac = df["ListPrice"].to_numpy() % 1.0
        # All non-zero prices should have .99 ending
        positive = df["ListPrice"] > 1.0
        npt.assert_allclose(frac[positive], 0.99, atol=0.011)

    def test_invalid_cost_mode_raises(self, base_products):
        cfg = {
            "base": {"value_scale": 1.0},
            "cost": {"mode": "invalid_mode"},
        }
        with pytest.raises(DimensionError, match="mode"):
            apply_product_pricing(base_products, cfg, seed=42)

    def test_keep_mode_preserves_cost_ratio(self, base_products):
        """Keep mode should maintain roughly the same cost-to-price ratio."""
        cfg = {
            "base": {"value_scale": 1.5},
            "cost": {"mode": "keep"},
        }
        df = apply_product_pricing(base_products, cfg, seed=42)
        # Cost should still be <= price
        assert (df["UnitCost"] <= df["ListPrice"]).all()

    def test_output_rounded_to_2_decimals(self, base_products):
        cfg = self._margin_cfg()
        df = apply_product_pricing(base_products, cfg, seed=42)
        # Both columns should be rounded to 2 decimals
        lp = df["ListPrice"].to_numpy()
        uc = df["UnitCost"].to_numpy()
        npt.assert_array_equal(lp, np.round(lp, 2))
        npt.assert_array_equal(uc, np.round(uc, 2))

    def test_margin_mode_variants_share_cost(self):
        """All variants of the same BaseProductID must share UnitCost so
        catalog-level pricing is consistent (SCD2 drift later diverges them)."""
        df = pd.DataFrame({
            "ProductKey": np.arange(1, 7, dtype=np.int64),
            # 2 bases, 3 variants each
            "BaseProductID": np.array([1, 1, 1, 4, 4, 4], dtype=np.int64),
            "VariantIndex": np.array([0, 1, 2, 0, 1, 2], dtype=np.int64),
            "SubcategoryKey": np.array([10, 10, 10, 20, 20, 20], dtype=np.int64),
            "ListPrice": np.array([99.99, 99.99, 99.99, 49.99, 49.99, 49.99]),
            "UnitCost": np.array([50.0, 50.0, 50.0, 25.0, 25.0, 25.0]),
        })
        cfg = self._margin_cfg()
        result = apply_product_pricing(df, cfg, seed=42)
        for _, grp in result.groupby("BaseProductID"):
            assert grp["UnitCost"].nunique() == 1, "variants of same base must share UnitCost"
            assert grp["ListPrice"].nunique() == 1

    def test_margin_mode_different_bases_get_different_costs(self):
        """Per-base sampling should still produce variance across bases."""
        n_bases = 50
        df = pd.DataFrame({
            "ProductKey": np.arange(1, n_bases + 1, dtype=np.int64),
            "BaseProductID": np.arange(1, n_bases + 1, dtype=np.int64),
            "VariantIndex": np.zeros(n_bases, dtype=np.int64),
            "SubcategoryKey": np.full(n_bases, 10, dtype=np.int64),
            "ListPrice": np.full(n_bases, 100.0),
            "UnitCost": np.full(n_bases, 60.0),
        })
        result = apply_product_pricing(df, self._margin_cfg(), seed=42)
        # With 50 distinct bases sampled in [0.20, 0.40], we expect many
        # distinct UnitCost values — definitely more than 5.
        assert result["UnitCost"].nunique() > 5


# ===================================================================
# TestRescaleToRange (internal helper)
# ===================================================================

class TestRescaleToRange:
    """Tests for pricing._rescale_to_range()."""

    def test_maps_to_target_bounds(self):
        s = pd.Series([10.0, 20.0, 30.0])
        result = _rescale_to_range(s, 100.0, 200.0)
        npt.assert_allclose(result.iloc[0], 100.0)
        npt.assert_allclose(result.iloc[-1], 200.0)

    def test_preserves_ordering(self):
        s = pd.Series([5.0, 15.0, 25.0, 35.0])
        result = _rescale_to_range(s, 0.0, 1.0)
        assert (result.diff().dropna() > 0).all()

    def test_single_value_returns_midpoint(self):
        s = pd.Series([42.0])
        result = _rescale_to_range(s, 10.0, 20.0)
        npt.assert_allclose(result.iloc[0], 15.0)


# ===================================================================
# TestSnapUnitPriceToPoints (internal helper)
# ===================================================================

class TestSnapUnitPriceToPoints:
    """Tests for pricing._snap_unit_price_to_points()."""

    def test_snapped_prices_non_negative(self):
        prices = np.array([0.5, 5.0, 50.0, 500.0, 5000.0])
        result = _snap_unit_price_to_points(prices, DEFAULT_PRICE_BANDS, DEFAULT_PRICE_ENDING)
        assert np.all(result >= 0.0)

    def test_ending_applied(self):
        prices = np.array([10.0, 25.0, 100.0])
        result = _snap_unit_price_to_points(prices, DEFAULT_PRICE_BANDS, 0.99)
        # Fractional parts should be 0.99 for non-zero values
        frac = result[result > 1.0] % 1.0
        npt.assert_allclose(frac, 0.99, atol=0.011)


# ===================================================================
# TestRoundToStep (internal helper)
# ===================================================================

class TestRoundToStep:
    """Tests for pricing._round_to_step()."""

    def test_cheap_costs_not_inflated_to_step(self):
        """Sub-step costs (e.g., $0.50 snacks) stay below the smallest band
        step instead of being snapped up to it. Only zero/negative values
        get floored, and only to a penny."""
        cheap = np.array([0.05, 0.50, 1.20, 1.80])
        out = _round_to_step(cheap, DEFAULT_COST_BANDS)
        assert out.min() >= 0.01
        assert (out < 2.50).any()

    def test_zero_floored_to_penny(self):
        out = _round_to_step(np.array([0.0]), DEFAULT_COST_BANDS)
        assert out[0] == 0.01

    def test_normal_costs_round_to_step(self):
        """Costs above the smallest step still round cleanly to the step grid."""
        normal = np.array([23.7, 51.2, 197.0])
        out = _round_to_step(normal, DEFAULT_COST_BANDS)
        # Each output should be a multiple of its band step within tolerance
        for v in out:
            assert v > 1.0  # well above penny floor
            # divisible by 2.50 or 5.0 or 10.0 (matching DEFAULT_COST_BANDS)
            assert any(abs((v / step) - round(v / step)) < 1e-9
                       for step in (2.50, 5.0, 10.0))

    def test_nan_preserved(self):
        out = _round_to_step(np.array([np.nan, 5.0]), DEFAULT_COST_BANDS)
        assert np.isnan(out[0])
        assert np.isfinite(out[1])


# ===================================================================
# TestEnrichProductsAttributes
# ===================================================================

class TestEnrichProductsAttributes:
    """Tests for product_profile.enrich_products_attributes()."""

    @pytest.fixture
    def subcategory_parquet(self, tmp_path):
        """Write a minimal product_subcategory.parquet for name resolution."""
        sc = pd.DataFrame({
            "SubcategoryKey": [10, 20, 30, 40],
            "Subcategory": ["Headphones", "Laptops", "Televisions", "T-Shirts & Tops"],
        })
        sc.to_parquet(tmp_path / "product_subcategory.parquet", index=False)
        return tmp_path

    @pytest.fixture
    def enrichable_products(self):
        """Products suitable for enrichment (needs BaseProductID)."""
        return pd.DataFrame({
            "ProductKey": np.arange(1, 11, dtype=np.int64),
            "BaseProductID": np.arange(1, 11, dtype=np.int64),
            "SubcategoryKey": np.tile([10, 20, 30, 40], 3)[:10].astype(np.int64),
            "ListPrice": np.linspace(20.0, 200.0, 10),
            "UnitCost": np.linspace(10.0, 100.0, 10),
            "ProductName": [f"Widget {i}" for i in range(1, 11)],
            "Color": ["Red", "Blue", "Green", "Black", "White",
                       "Red", "Blue", "Green", "Black", "White"],
            "VariantIndex": np.zeros(10, dtype=np.int32),
        })

    def test_adds_expected_columns(self, enrichable_products, subcategory_parquet):
        df = enrich_products_attributes(
            enrichable_products, {}, seed=42,
            output_folder=subcategory_parquet,
        )
        expected_cols = [
            "ColorFamily", "Material", "Style", "ProductLine",
            "AgeGroup", "SeasonalityProfile",
            "WeightKg", "LengthCm", "WidthCm", "HeightCm", "VolumeCm3",
            "ShippingClass", "IsFragile", "IsHazmat",
            "LeadTimeDays", "CasePackQty", "FulfillmentType",
            "EligibleStore", "EligibleOnline",
            "CountryOfOrigin", "CertificationType",
            "PackagingType", "TargetGender",
        ]
        for col in expected_cols:
            assert col in df.columns, f"Missing column: {col}"

    def test_determinism(self, enrichable_products, subcategory_parquet):
        df1 = enrich_products_attributes(
            enrichable_products, {}, seed=42,
            output_folder=subcategory_parquet,
        )
        df2 = enrich_products_attributes(
            enrichable_products, {}, seed=42,
            output_folder=subcategory_parquet,
        )
        pd.testing.assert_frame_equal(df1, df2)

    def test_different_seed_differs(self, enrichable_products, subcategory_parquet):
        df1 = enrich_products_attributes(
            enrichable_products, {}, seed=1,
            output_folder=subcategory_parquet,
        )
        df2 = enrich_products_attributes(
            enrichable_products, {}, seed=2,
            output_folder=subcategory_parquet,
        )
        # At least some hash-derived columns should differ
        assert not df1["Material"].equals(df2["Material"])

    def test_color_family_mapping(self, enrichable_products, subcategory_parquet):
        df = enrich_products_attributes(
            enrichable_products, {}, seed=42,
            output_folder=subcategory_parquet,
        )
        # "Red" -> "Red", "Blue" -> "Blue", etc.
        red_rows = enrichable_products["Color"] == "Red"
        assert (df.loc[red_rows, "ColorFamily"] == "Red").all()
        blue_rows = enrichable_products["Color"] == "Blue"
        assert (df.loc[blue_rows, "ColorFamily"] == "Blue").all()

    def test_variant_consistency(self, subcategory_parquet):
        """Products sharing the same BaseProductID should get identical
        hash-derived attributes (Material, Style, etc.)."""
        df = pd.DataFrame({
            "ProductKey": np.arange(1, 7, dtype=np.int64),
            "BaseProductID": np.array([1, 1, 1, 2, 2, 2], dtype=np.int64),
            "SubcategoryKey": np.array([10, 10, 10, 20, 20, 20], dtype=np.int64),
            "ListPrice": [50.0, 55.0, 60.0, 100.0, 110.0, 120.0],
            "UnitCost": [25.0, 27.5, 30.0, 50.0, 55.0, 60.0],
            "ProductName": ["A", "A - V001", "A - V002", "B", "B - V001", "B - V002"],
            "Color": ["Red"] * 6,
            "VariantIndex": [0, 1, 2, 0, 1, 2],
        })
        result = enrich_products_attributes(
            df, {}, seed=42,
            output_folder=subcategory_parquet,
        )
        # All variants of BaseProductID=1 should share Material
        grp1 = result[result["BaseProductID"] == 1]
        assert grp1["Material"].nunique() == 1
        assert grp1["Style"].nunique() == 1
        assert grp1["ProductLine"].nunique() == 1

        # Same for BaseProductID=2
        grp2 = result[result["BaseProductID"] == 2]
        assert grp2["Material"].nunique() == 1

    def test_weight_non_negative(self, enrichable_products, subcategory_parquet):
        df = enrich_products_attributes(
            enrichable_products, {}, seed=42,
            output_folder=subcategory_parquet,
        )
        assert (df["WeightKg"] >= 0.0).all()
        assert (df["LengthCm"] >= 0.0).all()
        assert (df["WidthCm"] >= 0.0).all()
        assert (df["HeightCm"] >= 0.0).all()

    def test_shipping_class_values(self, enrichable_products, subcategory_parquet):
        df = enrich_products_attributes(
            enrichable_products, {}, seed=42,
            output_folder=subcategory_parquet,
        )
        valid = {"Standard", "Oversize", "Freight", "Digital"}
        assert set(df["ShippingClass"].unique()).issubset(valid)

    def test_age_group_default_adult(self, enrichable_products, subcategory_parquet):
        """Products without age keywords default to 'Adult'."""
        df = enrich_products_attributes(
            enrichable_products, {}, seed=42,
            output_folder=subcategory_parquet,
        )
        # None of our test product names contain age keywords
        assert (df["AgeGroup"] == "Adult").all()

    def test_skip_post_merge_omits_brand_tier(self, enrichable_products, subcategory_parquet):
        df = enrich_products_attributes(
            enrichable_products, {}, seed=42,
            output_folder=subcategory_parquet,
            _skip_post_merge=True,
        )
        # BrandTier is computed in apply_post_merge_enrichment
        assert "BrandTier" not in df.columns

    def test_avg_customer_rating_no_ceiling_pile_up(self, subcategory_parquet):
        """AvgCustomerRating must spread across the band rather than clip
        at 5.0; Premium + low-defect rows are the historical pile-up source."""
        n = 500
        rng = np.random.default_rng(0)
        df = pd.DataFrame({
            "ProductKey": np.arange(1, n + 1, dtype=np.int64),
            "BaseProductID": np.arange(1, n + 1, dtype=np.int64),
            "SubcategoryKey": rng.choice([10, 20, 30, 40], size=n).astype(np.int64),
            "ListPrice": rng.uniform(20.0, 200.0, n),
            "UnitCost": rng.uniform(10.0, 100.0, n),
            "ProductName": [f"Item {i}" for i in range(1, n + 1)],
            "Color": rng.choice(["Red", "Blue", "Green", "Black"], size=n),
            "VariantIndex": np.zeros(n, dtype=np.int32),
        })
        out = enrich_products_attributes(
            df, {}, seed=42, output_folder=subcategory_parquet,
        )
        ratings = out["AvgCustomerRating"].to_numpy()
        # Cap ceiling-share well below 1% to catch any future over-pull toward 5.0.
        ceiling_share = (ratings >= 4.95).mean()
        assert ceiling_share < 0.01, f"too many ratings near ceiling: {ceiling_share:.1%}"
        # Sanity: still a reasonable spread across the band.
        assert ratings.max() > 4.0
        assert ratings.std() > 0.15


# ===================================================================
# TestSplitmix64 (deterministic hash)
# ===================================================================

class TestSplitmix64:
    """Tests for product_profile._splitmix64()."""

    def test_deterministic(self):
        x = np.array([1, 2, 3], dtype=np.uint64)
        h1 = _splitmix64(x)
        h2 = _splitmix64(x)
        npt.assert_array_equal(h1, h2)

    def test_different_input_different_output(self):
        a = _splitmix64(np.array([100], dtype=np.uint64))
        b = _splitmix64(np.array([200], dtype=np.uint64))
        assert a[0] != b[0]

    def test_output_dtype_uint64(self):
        x = np.array([0, 1, 2**32], dtype=np.uint64)
        h = _splitmix64(x)
        assert h.dtype == np.uint64


# ===================================================================
# TestLoadContosoProducts — catalog selection
# ===================================================================

from src.dimensions.products.contoso_loader import (
    load_contoso_products,
    _normalize_products,
    _trim_house_brand,
)


class TestLoadContosoProducts:
    """Tests for catalog selection in load_contoso_products()."""

    def test_contoso_mode_excludes_synthetic(self):
        df = load_contoso_products(Path("."), catalog="contoso")
        if "Source" in df.columns:
            assert (df["Source"] == "Contoso").all()

    def test_synthetic_mode_excludes_contoso(self):
        df = load_contoso_products(Path("."), catalog="synthetic")
        if "Source" in df.columns:
            assert (df["Source"] == "Synthetic").all()

    def test_all_mode_has_both_sources(self):
        df = load_contoso_products(Path("."), catalog="all")
        if "Source" in df.columns:
            sources = set(df["Source"].unique())
            assert "Contoso" in sources
            assert "Synthetic" in sources

    def test_contoso_mode_has_fewer_products_than_all(self):
        contoso = load_contoso_products(Path("."), catalog="contoso")
        all_df = load_contoso_products(Path("."), catalog="all")
        assert len(contoso) < len(all_df)

    def test_synthetic_mode_has_fewer_products_than_all(self):
        synthetic = load_contoso_products(Path("."), catalog="synthetic")
        all_df = load_contoso_products(Path("."), catalog="all")
        assert len(synthetic) < len(all_df)

    def test_all_mode_trims_house_brand(self):
        df = load_contoso_products(Path("."), catalog="all")
        contoso_brand = len(df[df["Brand"] == "Contoso"])
        assert contoso_brand < 710  # trimmed from original 710

    def test_contoso_mode_preserves_full_house_brand(self):
        df = load_contoso_products(Path("."), catalog="contoso")
        contoso_brand = len(df[df["Brand"] == "Contoso"])
        assert contoso_brand == 710  # untrimmed

    def test_required_columns_present(self):
        for mode in ["contoso", "synthetic", "all"]:
            df = load_contoso_products(Path("."), catalog=mode)
            for col in ["ProductKey", "SubcategoryKey", "ListPrice", "UnitCost"]:
                assert col in df.columns, f"{col} missing in {mode} mode"

    def test_no_negative_prices(self):
        for mode in ["contoso", "synthetic", "all"]:
            df = load_contoso_products(Path("."), catalog=mode)
            assert (df["ListPrice"] >= 0).all()
            assert (df["UnitCost"] >= 0).all()

    def test_cost_never_exceeds_price(self):
        for mode in ["contoso", "synthetic", "all"]:
            df = load_contoso_products(Path("."), catalog=mode)
            assert (df["UnitCost"] <= df["ListPrice"]).all()

    def test_default_catalog_is_all(self):
        default = load_contoso_products(Path("."))
        explicit = load_contoso_products(Path("."), catalog="all")
        assert len(default) == len(explicit)


class TestNormalizeProducts:
    """Tests for _normalize_products()."""

    def test_renames_unit_price_to_list_price(self):
        df = pd.DataFrame({
            "ProductKey": [1], "SubcategoryKey": [10],
            "UnitPrice": [99.99], "UnitCost": [50.0],
        })
        result = _normalize_products(df)
        assert "ListPrice" in result.columns
        assert "UnitPrice" not in result.columns

    def test_renames_product_subcategory_key(self):
        df = pd.DataFrame({
            "ProductKey": [1], "ProductSubcategoryKey": [10],
            "ListPrice": [99.99], "UnitCost": [50.0],
        })
        result = _normalize_products(df)
        assert "SubcategoryKey" in result.columns

    def test_raises_on_missing_required_columns(self):
        df = pd.DataFrame({"ProductKey": [1]})
        with pytest.raises(DimensionError, match="missing required"):
            _normalize_products(df)

    def test_clips_negative_prices(self):
        df = pd.DataFrame({
            "ProductKey": [1], "SubcategoryKey": [10],
            "ListPrice": [-5.0], "UnitCost": [-3.0],
        })
        result = _normalize_products(df)
        assert result["ListPrice"].iloc[0] >= 0
        assert result["UnitCost"].iloc[0] >= 0

    def test_cost_capped_at_price(self):
        df = pd.DataFrame({
            "ProductKey": [1], "SubcategoryKey": [10],
            "ListPrice": [50.0], "UnitCost": [100.0],
        })
        result = _normalize_products(df)
        assert result["UnitCost"].iloc[0] <= result["ListPrice"].iloc[0]


class TestTrimHouseBrand:
    """Tests for _trim_house_brand()."""

    def test_trims_when_above_threshold(self):
        n = 800
        df = pd.DataFrame({
            "Brand": ["Contoso"] * n,
            "SubcategoryKey": np.tile([1, 2, 3, 4], n // 4),
        })
        result = _trim_house_brand(df)
        assert len(result) < n

    def test_no_trim_when_below_threshold(self):
        n = 100
        df = pd.DataFrame({
            "Brand": ["Contoso"] * n,
            "SubcategoryKey": np.tile([1, 2], n // 2),
        })
        result = _trim_house_brand(df)
        assert len(result) == n

    def test_preserves_other_brands(self):
        df = pd.DataFrame({
            "Brand": ["Contoso"] * 800 + ["Fabrikam"] * 200,
            "SubcategoryKey": [1] * 1000,
        })
        result = _trim_house_brand(df)
        assert len(result[result["Brand"] == "Fabrikam"]) == 200

    def test_preserves_all_subcategories(self):
        n = 800
        subcats = np.tile([1, 2, 3, 4, 5], n // 5)
        df = pd.DataFrame({"Brand": ["Contoso"] * n, "SubcategoryKey": subcats})
        result = _trim_house_brand(df)
        assert set(result["SubcategoryKey"]) == {1, 2, 3, 4, 5}

    def test_no_brand_column_returns_unchanged(self):
        df = pd.DataFrame({"SubcategoryKey": [1, 2, 3]})
        result = _trim_house_brand(df)
        assert len(result) == 3

    def test_deterministic(self):
        n = 800
        df = pd.DataFrame({
            "Brand": ["Contoso"] * n,
            "SubcategoryKey": np.tile([1, 2, 3, 4], n // 4),
        })
        r1 = _trim_house_brand(df.copy())
        r2 = _trim_house_brand(df.copy())
        pd.testing.assert_frame_equal(r1, r2)
