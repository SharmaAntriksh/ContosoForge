"""Small end-to-end acceptance test for Phase 2 temporal contracts."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.facts.complaints.runner import _DELIVERY_ANCHORED_TYPES
from tests import sales_gen


def _find_one(roots: list[Path], name: str) -> Path:
    matches = [path for root in roots for path in root.rglob(name)]
    assert matches, f"{name} not found under {roots}"
    return matches[0]


@pytest.fixture(scope="module")
def phase2_run(tmp_path_factory):
    base = tmp_path_factory.mktemp("phase2_temporal")
    dims = base / "dims"
    scratch = base / "scratch"
    final = base / "final"
    dims.mkdir()
    scratch.mkdir()
    final.mkdir()

    cfg = sales_gen.small_config(
        dims_dir=dims,
        scratch_dir=scratch,
        final_dir=final,
        workers=2,
        chunk_size=1_000,
        total_rows=12_000,
        customers=2_000,
        seed=2468,
    )
    cfg["subscriptions"].update({
        "enabled": True,
        "generate_bridge": True,
        "participation_rate": 0.65,
    })
    cfg["complaints"].update({
        "enabled": True,
        "complaint_rate": 0.20,
        "resolution_rate": 1.0,
        "min_days_after_anchor": 0,
        "max_days_after_anchor": 20,
    })
    cfg["wishlists"].update({
        "enabled": True,
        "participation_rate": 0.50,
        "conversion_rate": 0.50,
        "pre_browse_days": 90,
    })

    sales_gen.run_pipeline_stage(base, cfg, only="dimensions")
    sales_gen.run_pipeline_stage(base, cfg, only="sales")
    return dims, scratch, final, cfg


def test_phase2_temporal_contracts(phase2_run):
    dims, scratch, final, cfg = phase2_run
    roots = [dims, scratch, final]
    global_end = pd.Timestamp(cfg["defaults"]["dates"]["end"])

    customers = pd.read_parquet(_find_one(roots, "customers.parquet"))
    if "IsCurrent" in customers.columns:
        customers = customers[customers["IsCurrent"].astype(bool)]
    customer_lifecycle = customers[
        ["CustomerKey", "CustomerStartDate", "CustomerEndDate"]
    ].copy()
    customer_lifecycle["CustomerStartDate"] = pd.to_datetime(
        customer_lifecycle["CustomerStartDate"]
    )
    customer_lifecycle["CustomerEndDate"] = pd.to_datetime(
        customer_lifecycle["CustomerEndDate"]
    )

    plans = pd.read_parquet(_find_one(roots, "plans.parquet"))
    plans["LaunchDate"] = pd.to_datetime(plans["LaunchDate"])
    assert (plans["CycleMonths"] > 0).all()
    assert (plans["CyclePrice"] >= 0).all()

    subscriptions = pd.read_parquet(
        _find_one(roots, "customer_subscriptions.parquet")
    ).merge(customer_lifecycle, on="CustomerKey", how="left", validate="many_to_one")
    subscriptions = subscriptions.merge(
        plans[["PlanKey", "LaunchDate"]],
        on="PlanKey",
        how="left",
        validate="many_to_one",
    )
    subscriptions["PeriodStartDate"] = pd.to_datetime(subscriptions["PeriodStartDate"])
    subscriptions["PeriodEndDate"] = pd.to_datetime(subscriptions["PeriodEndDate"])
    effective_end = subscriptions["CustomerEndDate"].fillna(global_end).clip(
        upper=global_end
    )
    assert (subscriptions["PeriodStartDate"] >= subscriptions["CustomerStartDate"]).all()
    assert (subscriptions["PeriodStartDate"] >= subscriptions["LaunchDate"]).all()
    assert (subscriptions["PeriodEndDate"] <= effective_end).all()
    assert (subscriptions["PeriodStartDate"] <= subscriptions["PeriodEndDate"]).all()
    assert (subscriptions["PeriodPrice"] >= 0).all()

    for _, group in subscriptions.groupby("SubscriptionKey", sort=False):
        ordered = group.sort_values("BillingCycleNumber")
        assert ordered["BillingCycleNumber"].tolist() == list(
            range(1, len(ordered) + 1)
        )
        assert ordered["TerminationReason"].iloc[:-1].isna().all()
        assert pd.notna(ordered["TerminationReason"].iloc[-1])
        assert (
            ordered["PeriodStartDate"].iloc[1:].to_numpy()
            > ordered["PeriodEndDate"].iloc[:-1].to_numpy()
        ).all()

    sales = sales_gen.load_sales(scratch, final)
    sales["OrderDate"] = pd.to_datetime(sales["OrderDate"])
    sales["DeliveryDate"] = pd.to_datetime(sales["DeliveryDate"])

    complaints = pd.read_parquet(_find_one(roots, "complaints.parquet"))
    complaints["ComplaintDate"] = pd.to_datetime(complaints["ComplaintDate"])
    complaints["ResolutionDate"] = pd.to_datetime(complaints["ResolutionDate"])
    complaint_lifecycle = complaints.merge(
        customer_lifecycle,
        on="CustomerKey",
        how="left",
        validate="many_to_one",
    )
    assert (
        complaint_lifecycle["ComplaintDate"]
        >= complaint_lifecycle["CustomerStartDate"]
    ).all()
    assert (
        complaint_lifecycle["CustomerEndDate"].isna()
        | (
            complaint_lifecycle["ComplaintDate"]
            <= complaint_lifecycle["CustomerEndDate"]
        )
    ).all()
    linked = complaints[complaints["OrderNumber"].notna()].merge(
        sales[["OrderNumber", "OrderLineNumber", "OrderDate", "DeliveryDate"]],
        left_on=["OrderNumber", "LineNumber"],
        right_on=["OrderNumber", "OrderLineNumber"],
        how="left",
        validate="many_to_one",
    )
    assert linked["OrderDate"].notna().all()
    assert linked["DeliveryDate"].notna().all()
    causal_anchor = linked["OrderDate"].where(
        ~linked["ComplaintType"].isin(_DELIVERY_ANCHORED_TYPES),
        linked["DeliveryDate"],
    )
    assert (linked["ComplaintDate"] >= causal_anchor).all()
    resolved = complaints[complaints["ResolutionDate"].notna()]
    response_days = (
        resolved["ResolutionDate"] - resolved["ComplaintDate"]
    ).dt.days
    assert (response_days == resolved["ResponseDays"]).all()

    wishlists = pd.read_parquet(_find_one(roots, "customer_wishlists.parquet"))
    wishlists["AddedDate"] = pd.to_datetime(wishlists["AddedDate"])
    wishlist_lifecycle = wishlists.merge(
        customer_lifecycle,
        on="CustomerKey",
        how="left",
        validate="many_to_one",
    )
    browse_start = wishlist_lifecycle["CustomerStartDate"] - pd.to_timedelta(
        cfg["wishlists"]["pre_browse_days"], unit="D",
    )
    assert (wishlist_lifecycle["AddedDate"] >= browse_start).all()
    assert (wishlist_lifecycle["AddedDate"] <= global_end).all()
    products = pd.read_parquet(
        _find_one(roots, "products.parquet"),
        columns=["ProductKey", "EffectiveStartDate", "EffectiveEndDate"],
    )
    products["EffectiveStartDate"] = pd.to_datetime(
        products["EffectiveStartDate"], errors="coerce",
    )
    products["EffectiveEndDate"] = pd.to_datetime(
        products["EffectiveEndDate"], errors="coerce",
    )
    wishlist_products = wishlists.merge(
        products,
        on="ProductKey",
        how="left",
        validate="many_to_one",
    )
    assert wishlist_products["EffectiveStartDate"].notna().all()
    assert (
        wishlist_products["AddedDate"]
        >= wishlist_products["EffectiveStartDate"]
    ).all()
    assert (
        wishlist_products["EffectiveEndDate"].isna()
        | (
            wishlist_products["AddedDate"]
            <= wishlist_products["EffectiveEndDate"]
        )
    ).all()
    latest_purchase = sales.groupby(
        ["CustomerKey", "ProductKey"], as_index=False
    )["OrderDate"].max().rename(columns={"OrderDate": "PurchaseDate"})
    funnel = wishlists.merge(
        latest_purchase,
        on=["CustomerKey", "ProductKey"],
        how="left",
        validate="one_to_one",
    )
    converted = (
        funnel["PurchaseDate"].notna()
        & (funnel["PurchaseDate"] >= funnel["AddedDate"])
    )
    assert (
        funnel.loc[converted, "AddedDate"]
        <= funnel.loc[converted, "PurchaseDate"]
    ).all()
    assert converted.mean() == pytest.approx(
        cfg["wishlists"]["conversion_rate"], abs=0.02,
    )

    dates = pd.to_datetime(pd.read_parquet(
        _find_one(roots, "dates.parquet"), columns=["Date"]
    )["Date"])
    event_dates = pd.concat([
        subscriptions["PeriodStartDate"], subscriptions["PeriodEndDate"],
        complaints["ComplaintDate"], complaints["ResolutionDate"].dropna(),
        wishlists["AddedDate"],
    ])
    assert event_dates.min() >= dates.min()
    assert event_dates.max() <= dates.max()
