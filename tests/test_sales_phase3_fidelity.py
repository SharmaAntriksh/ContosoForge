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

from types import SimpleNamespace

from tests import sales_gen

NO_DISCOUNT_KEY = 1  # the "no promotion" sentinel PromotionKey (default)


@pytest.fixture(scope="module")
def phase3_env(tmp_path_factory):
    """Generate one small sales fact (returns enabled) and expose the run dir +
    loaded DataFrame, shared across all Phase 3 acceptance assertions."""
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
    df = sales_gen.load_sales(base / "final", base / "scratch")
    returns = _load_returns(base / "final", base / "scratch")
    return SimpleNamespace(base=base, dims_dir=dims_dir, sales=df, returns=returns)


_RETURNS_MARKER = {"ReturnReasonKey", "ReturnDate", "ReturnEventKey"}


def _load_returns(*roots):
    """Concatenate every returns-fact parquet found under *roots*."""
    import pandas as pd
    import pyarrow.parquet as pq
    files = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.parquet"):
            try:
                cols = set(pq.read_schema(path).names)
            except Exception:
                continue
            if _RETURNS_MARKER <= cols:
                files.append(path)
    if not files:
        return None
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


@pytest.fixture(scope="module")
def sales_df(phase3_env):
    """The generated sales fact as a DataFrame."""
    return phase3_env.sales


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


# ===================================================================
# 3.1 — quantity elasticity (price + propensity)
# ===================================================================

class TestQuantityElasticity:
    def test_cheaper_products_sell_in_larger_quantities(self, sales_df):
        """Price elasticity: split lines at the median UnitPrice; the cheaper
        half should carry a higher mean Quantity than the pricier half."""
        median_price = sales_df["UnitPrice"].median()
        cheap = sales_df.loc[sales_df["UnitPrice"] <= median_price, "Quantity"]
        pricey = sales_df.loc[sales_df["UnitPrice"] > median_price, "Quantity"]
        assert cheap.mean() > pricey.mean(), (
            f"no elasticity: cheap mean qty {cheap.mean():.3f} !> "
            f"pricey mean qty {pricey.mean():.3f}"
        )

    def test_price_quantity_correlation_is_negative(self, sales_df):
        """Per-product mean price vs mean quantity is negatively correlated
        (a TV and a battery pack no longer share a quantity distribution)."""
        by_prod = sales_df.groupby("ProductKey").agg(
            price=("UnitPrice", "mean"), qty=("Quantity", "mean")
        )
        # Enough distinct products for a meaningful correlation.
        assert len(by_prod) >= 20
        corr = by_prod["price"].corr(by_prod["qty"])
        assert corr < -0.1, f"price↔quantity correlation not negative: {corr:.3f}"


# ===================================================================
# 3.2 — promotion salience weighting
# ===================================================================

def _redeemed_weighted_mean_discount(df, promo):
    """Redemption-count-weighted mean DiscountPct over redeemed promotions."""
    redeemed = df.loc[df["PromotionKey"] != NO_DISCOUNT_KEY, "PromotionKey"]
    counts = redeemed.value_counts().rename_axis("PromotionKey").reset_index(name="n")
    merged = counts.merge(promo, on="PromotionKey", how="left").dropna()
    total = float(merged["n"].sum())
    return float((merged["n"] * merged["DiscountPct"]).sum() / total) if total else 0.0


class TestPromotionSalience:
    def test_salience_shifts_redemption_toward_deeper_promos(self, tmp_path):
        """Isolate the salience effect from promo-window/exposure confounds by
        comparing salience ON vs OFF on identical dimensions: with salience on,
        the redemption-weighted mean DiscountPct must be higher (deeper promos
        redeemed more often among co-active ones)."""
        import copy
        import yaml
        import pandas as pd
        from src.engine.runners.pipeline_runner import run_pipeline

        base = tmp_path
        dims = base / "dims"
        dims.mkdir()

        def _models(enabled):
            m = copy.deepcopy(sales_gen.models_config())
            m["models"]["promotions"]["enabled"] = enabled
            return m

        cfg = sales_gen.small_config(
            dims_dir=dims, scratch_dir=base / "ds", final_dir=base / "df",
            workers=1, chunk_size=12_000,
        )
        cfg_path = base / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

        m_on = base / "m_on.yaml"
        m_on.write_text(yaml.safe_dump(_models(True), sort_keys=False), encoding="utf-8")
        m_off = base / "m_off.yaml"
        m_off.write_text(yaml.safe_dump(_models(False), sort_keys=False), encoding="utf-8")

        # Dimensions once (salience is a sales-time behavior; dims are identical).
        run_pipeline(config_path=str(cfg_path), models_config_path=str(m_on), only="dimensions")
        promo = pd.read_parquet(next(dims.rglob("promotions.parquet")))[
            ["PromotionKey", "DiscountPct"]
        ]

        def _sales(models_path, tag):
            scfg = sales_gen.small_config(
                dims_dir=dims, scratch_dir=base / f"s_{tag}", final_dir=base / f"f_{tag}",
                workers=1, chunk_size=12_000,
            )
            cp = base / f"c_{tag}.yaml"
            cp.write_text(yaml.safe_dump(scfg, sort_keys=False), encoding="utf-8")
            run_pipeline(config_path=str(cp), models_config_path=str(models_path), only="sales")
            return sales_gen.load_sales(base / f"f_{tag}", base / f"s_{tag}")

        df_on = _sales(m_on, "on")
        df_off = _sales(m_off, "off")

        depth_on = _redeemed_weighted_mean_discount(df_on, promo)
        depth_off = _redeemed_weighted_mean_discount(df_off, promo)
        assert depth_on > depth_off, (
            f"salience did not deepen redeemed promos: on={depth_on:.4f} "
            f"off={depth_off:.4f}"
        )


# ===================================================================
# 3.4 — shared fulfillment-friction latent (delivery ↔ returns)
# ===================================================================

class TestFulfillmentFriction:
    def _joined(self, phase3_env):
        """Sales lines annotated with whether/when they were returned."""
        import pandas as pd
        assert phase3_env.returns is not None and len(phase3_env.returns) > 0
        sales = phase3_env.sales[
            ["OrderNumber", "OrderLineNumber", "DeliveryStatus", "DeliveryDate"]
        ].copy()
        # First return event per line (min ReturnDate).
        ret = (phase3_env.returns.groupby(["OrderNumber", "OrderLineNumber"])["ReturnDate"]
               .min().reset_index().rename(columns={"ReturnDate": "FirstReturnDate"}))
        j = sales.merge(ret, on=["OrderNumber", "OrderLineNumber"], how="left")
        j["returned"] = j["FirstReturnDate"].notna()
        return j

    def test_delivery_status_shares_are_configured(self, phase3_env):
        """Delivery status shares track the configured (p_early, p_delayed)."""
        shares = phase3_env.sales["DeliveryStatus"].value_counts(normalize=True)
        # models.yaml ships p_early=0.10, p_delayed=0.20.
        assert abs(shares.get("Delayed", 0.0) - 0.20) < 0.04
        assert abs(shares.get("Early Delivery", 0.0) - 0.10) < 0.04

    def test_late_deliveries_are_returned_more(self, phase3_env):
        j = self._joined(phase3_env)
        rate_delayed = j.loc[j["DeliveryStatus"] == "Delayed", "returned"].mean()
        rate_ontime = j.loc[j["DeliveryStatus"] == "On Time", "returned"].mean()
        assert rate_delayed > rate_ontime, (
            f"late not returned more: delayed={rate_delayed:.4f} ontime={rate_ontime:.4f}"
        )

    def test_late_deliveries_are_returned_faster(self, phase3_env):
        import pandas as pd
        j = self._joined(phase3_env)
        ret = j[j["returned"]].copy()
        lag = (pd.to_datetime(ret["FirstReturnDate"]) - pd.to_datetime(ret["DeliveryDate"])).dt.days
        ret = ret.assign(lag=lag)
        lag_delayed = ret.loc[ret["DeliveryStatus"] == "Delayed", "lag"].mean()
        lag_ontime = ret.loc[ret["DeliveryStatus"] == "On Time", "lag"].mean()
        assert lag_delayed < lag_ontime, (
            f"late not returned faster: delayed={lag_delayed:.2f} ontime={lag_ontime:.2f}"
        )


# ===================================================================
# 3.3 — basket / co-purchase correlation
# ===================================================================

class TestBasketCorrelation:
    def test_lines_concentrate_on_their_order_theme(self, tmp_path):
        """The basket bias is recoverable end-to-end: each order gets a theme (a
        subcategory group) and its lines land in that theme far more than the
        1/num_themes chance baseline. Uses a diverse catalog with store
        assortment off so the effect isn't muted by few-subcategory / store-pool
        confounds. Group mapping uses the feature's own hash (this is the
        acceptance test for that feature)."""
        import copy
        import numpy as np
        import yaml
        import pandas as pd
        from src.engine.runners.pipeline_runner import run_pipeline
        from src.facts.sales.sales_logic.core.basket import _theme_of

        K = 6
        base = tmp_path
        dims = base / "dims"
        dims.mkdir()

        cfg = sales_gen.small_config(
            dims_dir=dims, scratch_dir=base / "scratch", final_dir=base / "final",
            workers=1, chunk_size=12_000,
        )
        cfg["scale"]["products"] = {"catalog": "all", "rows": 600}
        cfg["stores"]["assortment"] = {"enabled": False}
        cfg_path = base / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

        models = copy.deepcopy(sales_gen.models_config())
        models["models"]["basket"]["enabled"] = True
        models["models"]["basket"]["num_themes"] = K
        models["models"]["basket"]["strength"] = 0.7
        models_path = base / "models.yaml"
        models_path.write_text(yaml.safe_dump(models, sort_keys=False), encoding="utf-8")

        run_pipeline(config_path=str(cfg_path), models_config_path=str(models_path), only="dimensions")
        run_pipeline(config_path=str(cfg_path), models_config_path=str(models_path), only="sales")
        df = sales_gen.load_sales(base / "final", base / "scratch")

        prod = pd.read_parquet(next(dims.rglob("products.parquet")))[
            ["ProductKey", "SubcategoryKey"]
        ].drop_duplicates("ProductKey")

        # Each product's theme group and each order's theme, via the feature hash.
        prod_group = pd.Series(
            _theme_of(prod["SubcategoryKey"].to_numpy().astype(np.uint64), K),
            index=prod["ProductKey"].to_numpy(),
        )
        line_group = df["ProductKey"].map(prod_group)
        order_theme = _theme_of(df["OrderNumber"].to_numpy().astype(np.uint64), K)

        match_rate = float((line_group.to_numpy() == order_theme).mean())
        assert match_rate > 0.40, (
            f"basket-theme not recoverable: match_rate={match_rate:.3f} "
            f"(chance baseline 1/{K}={1/K:.3f})"
        )


# ===================================================================
# 4.1 — deterministic posted price per (product, month)
# ===================================================================

class TestDeterministicPostedPrice:
    def test_unit_price_constant_within_product_month(self, sales_df):
        """Every sales line for the same (ProductKey, month) carries the same
        posted UnitPrice — a (product, month) self-join on the fact recovers a
        single list price (dissolves gotcha #26)."""
        import pandas as pd
        month = pd.to_datetime(sales_df["OrderDate"].astype(str)).dt.to_period("M").astype(str)
        g = pd.DataFrame({
            "ProductKey": sales_df["ProductKey"].to_numpy(),
            "month": month.to_numpy(),
            "UnitPrice": sales_df["UnitPrice"].round(2).to_numpy(),
        })
        nun = g.groupby(["ProductKey", "month"])["UnitPrice"].nunique()
        bad = int((nun > 1).sum())
        assert bad == 0, f"{bad} (product, month) groups carry >1 posted UnitPrice"
        # sanity: the fact still has real price variation across product-months
        assert g["UnitPrice"].nunique() > 5
