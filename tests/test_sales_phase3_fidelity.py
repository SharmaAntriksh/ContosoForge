"""Phase 3 acceptance — statistical-fidelity properties on the real sales fact.

These generate one small end-to-end dataset (via ``tests/sales_gen``) with all
Phase 3 features at their default-on settings, then assert the *connected*
structure each sub-phase is supposed to introduce. Unlike the pure-unit tests
(e.g. ``test_pricing_pipeline``), these exercise the full chunk-builder wiring —
they fail if a feature is implemented in isolation but never threaded into the
pipeline.

The dataset is generated once per module (returns enabled) and shared across the
per-sub-phase assertions to amortize the ~10s generation cost.
"""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")
pytest.importorskip("pandas")
pytest.importorskip("pyarrow.parquet")

from tests import sales_gen

NO_DISCOUNT_KEY = 1  # the "no promotion" sentinel PromotionKey (default)


@pytest.fixture(scope="module")
def sales_df(tmp_path_factory):
    """Generate a small sales fact (returns enabled) and return it as a DataFrame."""
    base = tmp_path_factory.mktemp("phase3")
    dims_dir = base / "dims"
    dims_dir.mkdir()

    cfg = sales_gen.small_config(
        dims_dir=dims_dir, scratch_dir=base / "scratch", final_dir=base / "final",
        workers=1, chunk_size=4_000,
    )
    # Returns on so this fixture also serves the delivery↔returns checks (3.4).
    cfg["returns"]["enabled"] = True

    sales_gen.run_pipeline_stage(base, cfg, only="dimensions")
    sales_gen.run_pipeline_stage(base, cfg, only="sales")
    return sales_gen.load_sales(base / "final", base / "scratch")


# ===================================================================
# 3.5 — markdown ↔ PromotionKey consistency
# ===================================================================

class TestMarkdownPromotionConsistency:
    def test_no_promotion_rows_have_zero_discount(self, sales_df):
        """Forward (strict): PromotionKey == no_discount_key ⇒ DiscountAmount == 0."""
        no_promo = sales_df["PromotionKey"] == NO_DISCOUNT_KEY
        assert no_promo.any(), "test needs some un-promoted rows to be meaningful"
        assert (sales_df.loc[no_promo, "DiscountAmount"] == 0.0).all(), (
            "reconciliation off: some no-promotion rows carry a nonzero discount"
        )

    def test_promoted_rows_mostly_carry_a_discount(self, sales_df):
        """Converse (aggregate): promoted lines draw from the nonzero ladder, so
        most carry a discount (a minority snap to 0 on cheap items / coarse
        discount bands — that's expected, hence a share threshold not ==)."""
        promo = sales_df["PromotionKey"] != NO_DISCOUNT_KEY
        assert promo.any(), "test needs some promoted rows to be meaningful"
        share_with_discount = float((sales_df.loc[promo, "DiscountAmount"] > 0.0).mean())
        assert share_with_discount > 0.5, (
            f"only {share_with_discount:.1%} of promoted lines carry a discount"
        )

    def test_net_price_reconciles(self, sales_df):
        """NetPrice == round(UnitPrice - DiscountAmount, 2) on every row."""
        import numpy as np
        expected = np.round(sales_df["UnitPrice"] - sales_df["DiscountAmount"], 2)
        assert np.allclose(sales_df["NetPrice"], expected, atol=0.01)
