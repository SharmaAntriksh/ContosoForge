"""Tests for src.dimensions.products.scd2 (SCD2 price revision versioning)."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest


@pytest.fixture()
def base_products():
    """Minimal product DataFrame for SCD2 tests."""
    return pd.DataFrame({
        "ProductID": np.arange(1, 6),
        "ProductKey": np.arange(1, 6),
        "ProductName": [f"Product {i}" for i in range(1, 6)],
        "ListPrice": [19.99, 49.99, 99.99, 249.99, 499.99],
        "UnitCost": [8.50, 22.00, 45.00, 110.00, 220.00],
    })


class TestGenerateScd2Versions:
    def test_max_versions_1_returns_base(self, base_products):
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        cfg = SimpleNamespace(revision_frequency=12, price_drift=0.05, max_versions=1)
        result = generate_scd2_versions(
            rng, base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        assert len(result) == len(base_products)

    def test_expands_rows(self, base_products):
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        cfg = SimpleNamespace(revision_frequency=12, price_drift=0.05, max_versions=4)
        result = generate_scd2_versions(
            rng, base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        assert len(result) >= len(base_products)

    def test_version_numbers_sequential(self, base_products):
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        cfg = SimpleNamespace(revision_frequency=6, price_drift=0.05, max_versions=4)
        result = generate_scd2_versions(
            rng, base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        for pid, grp in result.groupby("ProductID"):
            versions = grp["VersionNumber"].tolist()
            assert versions == list(range(1, len(versions) + 1)), f"Product {pid}: non-sequential versions"

    def test_is_current_flag(self, base_products):
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        cfg = SimpleNamespace(revision_frequency=6, price_drift=0.05, max_versions=4)
        result = generate_scd2_versions(
            rng, base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        for pid, grp in result.groupby("ProductID"):
            assert grp["IsCurrent"].iloc[-1] == 1, f"Product {pid}: last version not current"
            if len(grp) > 1:
                assert (grp["IsCurrent"].iloc[:-1] == 0).all(), f"Product {pid}: non-last version marked current"

    def test_product_key_unique(self, base_products):
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        cfg = SimpleNamespace(revision_frequency=12, price_drift=0.05, max_versions=3)
        result = generate_scd2_versions(
            rng, base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        assert result["ProductKey"].is_unique

    def test_unit_cost_never_exceeds_list_price(self, base_products):
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        cfg = SimpleNamespace(revision_frequency=6, price_drift=0.10, max_versions=5)
        result = generate_scd2_versions(
            rng, base_products, cfg,
            pd.Timestamp("2020-01-01"), pd.Timestamp("2026-12-31"),
        )
        assert (result["UnitCost"] <= result["ListPrice"]).all()

    def test_determinism(self, base_products):
        from src.dimensions.products.scd2 import generate_scd2_versions
        cfg = SimpleNamespace(revision_frequency=12, price_drift=0.05, max_versions=3)
        r1 = generate_scd2_versions(
            np.random.default_rng(99), base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        r2 = generate_scd2_versions(
            np.random.default_rng(99), base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        pd.testing.assert_frame_equal(r1, r2)

    def test_zero_revision_frequency_returns_base(self, base_products):
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        cfg = SimpleNamespace(revision_frequency=0, price_drift=0.05, max_versions=4)
        result = generate_scd2_versions(
            rng, base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        assert len(result) == len(base_products)

    def test_effective_dates_within_range(self, base_products):
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        start = pd.Timestamp("2023-01-01")
        end = pd.Timestamp("2025-12-31")
        cfg = SimpleNamespace(revision_frequency=6, price_drift=0.05, max_versions=5)
        result = generate_scd2_versions(rng, base_products, cfg, start, end)
        assert (result["EffectiveStartDate"] >= start).all()

    def test_version_dates_sequential(self, base_products):
        """Within each ProductID, EffectiveStartDate should be non-decreasing."""
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        cfg = SimpleNamespace(revision_frequency=6, price_drift=0.05, max_versions=5)
        result = generate_scd2_versions(
            rng, base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        for pid, grp in result.groupby("ProductID"):
            dates = grp["EffectiveStartDate"].values
            assert (dates[1:] >= dates[:-1]).all(), f"ProductID {pid} has non-sequential dates"

    def test_drift_bounded(self, base_products):
        """Price drift should not produce extreme values (>5x or <0.2x original)."""
        from src.dimensions.products.scd2 import generate_scd2_versions
        rng = np.random.default_rng(42)
        cfg = SimpleNamespace(revision_frequency=6, price_drift=0.05, max_versions=5)
        result = generate_scd2_versions(
            rng, base_products, cfg,
            pd.Timestamp("2023-01-01"), pd.Timestamp("2026-12-31"),
        )
        for pid, grp in result.groupby("ProductID"):
            base_price = grp["ListPrice"].iloc[0]
            if base_price > 0:
                ratio = grp["ListPrice"].max() / base_price
                assert ratio < 5.0, f"ProductID {pid} price drifted {ratio:.1f}x"


class TestSnapDriftedPrices:
    def test_basic_snap(self):
        from src.dimensions.products.scd2 import snap_drifted_prices
        lp = np.array([19.37, 52.14, 103.88])
        uc = np.array([8.22, 23.50, 47.00])
        snapped_lp, snapped_uc = snap_drifted_prices(lp, uc)
        assert len(snapped_lp) == 3
        assert len(snapped_uc) == 3
        assert all(np.isfinite(snapped_lp))
        assert all(np.isfinite(snapped_uc))

    def test_snap_preserves_array_length(self):
        from src.dimensions.products.scd2 import snap_drifted_prices
        lp = np.array([10.0, 50.0, 200.0, 1000.0])
        uc = np.array([9.5, 48.0, 195.0, 998.0])
        snapped_lp, snapped_uc = snap_drifted_prices(lp, uc)
        assert len(snapped_lp) == 4
        assert len(snapped_uc) == 4


class TestSplitProductsAndProfile:
    """Regression: ProductProfile must cover every ProductKey in Products,
    not just IsCurrent=1 rows.  Without this, sales/inventory rows that
    carry a historical ProductKey under SCD2 fail to join to profile."""

    @staticmethod
    def _make_post_scd2_df() -> pd.DataFrame:
        # Two products with SCD2 history: 2 versions each, ProductKey
        # reassigned sequentially (mirrors what scd2.generate_scd2_versions
        # produces).
        return pd.DataFrame({
            "ProductKey": [1, 2, 3, 4],
            "ProductID": [10, 10, 20, 20],
            "VersionNumber": [1, 2, 1, 2],
            "EffectiveStartDate": pd.to_datetime(
                ["2023-01-01", "2024-06-01", "2023-01-01", "2025-01-01"]
            ),
            "EffectiveEndDate": pd.to_datetime(
                ["2024-05-31", "9999-12-31", "2024-12-31", "9999-12-31"]
            ),
            "IsCurrent": [False, True, False, True],
            "ProductName": ["A", "A", "B", "B"],
            "SubcategoryKey": [100, 100, 200, 200],
            "Brand": ["X", "X", "Y", "Y"],
            "ListPrice": [19.99, 21.99, 49.99, 54.99],
            "UnitCost": [10.0, 11.0, 25.0, 27.0],
            "BaseProductID": [10, 10, 20, 20],
            "VariantIndex": [0, 0, 0, 0],
            # analytical attribute that should appear in profile
            "PopularityScore": [50, 50, 75, 75],
        })

    def test_profile_covers_all_product_keys(self):
        from src.dimensions.products.generator import _split_products_and_profile
        df = self._make_post_scd2_df()
        products_df, profile_df = _split_products_and_profile(df)
        # ProductProfile grain matches Products grain (no IsCurrent filter)
        assert len(profile_df) == len(products_df) == 4
        assert set(profile_df["ProductKey"]) == set(products_df["ProductKey"])

    def test_profile_includes_product_id(self):
        from src.dimensions.products.generator import _split_products_and_profile
        _, profile_df = _split_products_and_profile(self._make_post_scd2_df())
        assert "ProductID" in profile_df.columns
        # ProductID enables roll-up across versions
        assert profile_df.groupby("ProductID").size().tolist() == [2, 2]

    def test_historical_sales_keys_resolve(self):
        """Simulate a sales fact carrying historical (non-IsCurrent) ProductKeys
        and assert all rows resolve in ProductProfile."""
        from src.dimensions.products.generator import _split_products_and_profile
        _, profile_df = _split_products_and_profile(self._make_post_scd2_df())
        sales_product_keys = pd.Series([1, 2, 3, 4, 1, 3])  # mix of V1 and V2
        merged = sales_product_keys.to_frame("ProductKey").merge(
            profile_df[["ProductKey", "PopularityScore"]],
            on="ProductKey",
            how="left",
        )
        assert merged["PopularityScore"].notna().all()

    def test_analytical_columns_routed_to_profile(self):
        from src.dimensions.products.generator import _split_products_and_profile
        products_df, profile_df = _split_products_and_profile(self._make_post_scd2_df())
        assert "PopularityScore" in profile_df.columns
        assert "PopularityScore" not in products_df.columns
        assert "ListPrice" in products_df.columns
        assert "ListPrice" not in profile_df.columns

    def test_empty_df_raises(self):
        from src.dimensions.products.generator import _split_products_and_profile
        from src.exceptions import DimensionError
        with pytest.raises(DimensionError):
            _split_products_and_profile(pd.DataFrame())
