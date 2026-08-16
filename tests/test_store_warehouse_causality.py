from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.dimensions.stores.generator import GeoContext, generate_store_table
from src.dimensions.stores.lifecycle import backfill_close_reason
from src.dimensions.warehouses.generator import _add_operating_targets
from src.facts.inventory.engine import InventoryConfig, compute_inventory_snapshots
from src.utils.static_schemas import STATIC_SCHEMAS


_DECORATIVE_STORE_COLUMNS = {
    "AvgTransactionValue",
    "CustomerSatisfactionScore",
    "InventoryTurnoverTarget",
    "LastAuditScore",
    "ShrinkageRatePct",
}


def test_store_dimension_drops_disconnected_analytical_columns() -> None:
    stores = generate_store_table(
        geo=GeoContext(geo_keys=np.arange(1, 11, dtype=np.int64)),
        num_stores=50,
        seed=17,
    )

    assert not (_DECORATIVE_STORE_COLUMNS & set(stores.columns))
    assert "SuccessorStoreKey" in stores.columns
    assert not (_DECORATIVE_STORE_COLUMNS & dict(STATIC_SCHEMAS["Stores"]).keys())


def test_close_reasons_require_supporting_business_attributes() -> None:
    n_closed = 400
    close_dates = pd.to_datetime(["2024-06-30"] * n_closed)
    closed = pd.DataFrame({
        "StoreKey": np.arange(1, n_closed + 1, dtype=np.int32),
        "StoreType": ["Supermarket"] * n_closed,
        "StoreRegionId": np.ones(n_closed, dtype=np.int32),
        "Status": ["Closed"] * n_closed,
        "RevenueClass": np.resize(np.array(["A", "C"], dtype=object), n_closed),
        "OwnershipType": np.resize(
            np.array(["Corporate", "Leased"], dtype=object),
            n_closed,
        ),
        "OpeningDate": pd.to_datetime(["2020-01-01"] * n_closed),
        "ClosingDate": close_dates,
        "RenovationStartDate": pd.NaT,
        "RenovationEndDate": pd.NaT,
    })
    successors = pd.DataFrame({
        "StoreKey": np.arange(1_001, 1_021, dtype=np.int32),
        "StoreType": ["Supermarket"] * 20,
        "StoreRegionId": np.ones(20, dtype=np.int32),
        "Status": ["Open"] * 20,
        "RevenueClass": ["B"] * 20,
        "OwnershipType": ["Corporate"] * 20,
        "OpeningDate": pd.to_datetime(["2024-05-01"] * 20),
        "ClosingDate": pd.NaT,
        "RenovationStartDate": pd.NaT,
        "RenovationEndDate": pd.NaT,
    })
    stores = pd.concat([closed, successors], ignore_index=True)

    backfill_close_reason(
        stores,
        np.random.default_rng(23),
        status=stores["Status"].astype(str),
    )

    closed_out = stores[stores["Status"] == "Closed"]
    assert set(closed_out["CloseReason"]) == {
        "Low Sales",
        "Lease Ended",
        "Renovation",
        "Moved Location",
    }
    assert closed_out.loc[
        closed_out["CloseReason"] == "Low Sales", "RevenueClass"
    ].eq("C").all()
    assert closed_out.loc[
        closed_out["CloseReason"] == "Lease Ended", "OwnershipType"
    ].eq("Leased").all()

    moved = closed_out[closed_out["CloseReason"] == "Moved Location"]
    assert moved["SuccessorStoreKey"].notna().all()
    assert set(moved["SuccessorStoreKey"].astype(int)).issubset(
        set(successors["StoreKey"].astype(int))
    )

    renovation = closed_out[closed_out["CloseReason"] == "Renovation"]
    assert renovation["RenovationStartDate"].notna().all()
    assert renovation["RenovationEndDate"].eq(renovation["ClosingDate"]).all()


def test_warehouse_targets_are_explicit_and_audit_drives_shrinkage() -> None:
    warehouses = pd.DataFrame({
        "WarehouseType": ["Distribution Center"] * 2_000,
    })
    enriched = _add_operating_targets(warehouses, np.random.default_rng(31))

    assert enriched["TargetInventoryTurnover"].between(5.0, 8.0).all()
    assert enriched["LastAuditScore"].between(65, 100).all()
    assert enriched["TargetShrinkageRatePct"].ge(0).all()
    assert enriched["LastAuditScore"].rank().corr(
        enriched["TargetShrinkageRatePct"].rank()
    ) < -0.95


def _inventory_demand() -> pd.DataFrame:
    rows = []
    for warehouse_key in (1, 2):
        for month in range(1, 13):
            rows.append({
                "ProductKey": 1,
                "WarehouseKey": warehouse_key,
                "Year": 2024,
                "Month": month,
                "QuantitySold": 100,
            })
    return pd.DataFrame(rows)


def test_inventory_targets_drive_stock_and_emit_actual_shrinkage(tmp_path) -> None:
    pd.DataFrame({
        "WarehouseKey": [1, 2],
        "TargetInventoryTurnover": [4.0, 12.0],
        "TargetShrinkageRatePct": [0.0, 0.0],
    }).to_parquet(tmp_path / "warehouses.parquet", index=False)

    turnover_snapshots = compute_inventory_snapshots(
        _inventory_demand(),
        tmp_path,
        InventoryConfig(
            enabled=True,
            seed=7,
            reorder_compliance=0.0,
            shrinkage_enabled=True,
            min_demand_months=1,
        ),
    )
    average_on_hand = turnover_snapshots.groupby("WarehouseKey")[
        "QuantityOnHand"
    ].mean()
    assert average_on_hand.loc[1] > average_on_hand.loc[2]

    pd.DataFrame({
        "WarehouseKey": [1, 2],
        "TargetInventoryTurnover": [2.0, 2.0],
        "TargetShrinkageRatePct": [0.2, 4.0],
    }).to_parquet(tmp_path / "warehouses.parquet", index=False)
    shrinkage_snapshots = compute_inventory_snapshots(
        _inventory_demand(),
        tmp_path,
        InventoryConfig(
            enabled=True,
            seed=7,
            reorder_compliance=0.0,
            shrinkage_enabled=True,
            min_demand_months=1,
        ),
    )
    shrinkage = shrinkage_snapshots.groupby("WarehouseKey")[
        "ShrinkageQuantity"
    ].sum()
    assert shrinkage.loc[2] > shrinkage.loc[1]
    assert "ShrinkageQuantity" in dict(STATIC_SCHEMAS["InventorySnapshot"])


def test_inventory_turnover_target_controls_recurring_replenishment(tmp_path) -> None:
    pd.DataFrame({
        "WarehouseKey": [1, 2],
        "TargetInventoryTurnover": [4.0, 12.0],
        "TargetShrinkageRatePct": [0.0, 0.0],
    }).to_parquet(tmp_path / "warehouses.parquet", index=False)

    rows = []
    for warehouse_key in (1, 2):
        for date in pd.date_range("2022-01-01", "2024-12-01", freq="MS"):
            rows.append({
                "ProductKey": 1,
                "WarehouseKey": warehouse_key,
                "Year": date.year,
                "Month": date.month,
                "QuantitySold": 100,
            })

    snapshots = compute_inventory_snapshots(
        pd.DataFrame(rows),
        tmp_path,
        InventoryConfig(
            enabled=True,
            seed=7,
            reorder_compliance=1.0,
            lead_time_variance=0.0,
            overstock_bias=1.0,
            shrinkage_enabled=False,
            min_demand_months=1,
            abc_stock_multiplier={"A": 1.0, "B": 1.0, "C": 1.0},
        ),
    )

    by_warehouse = snapshots.groupby("WarehouseKey").agg(
        sold=("QuantitySold", "sum"),
        qoh=("QuantityOnHand", "sum"),
    )
    actual_turnover = 12.0 * by_warehouse["sold"] / by_warehouse["qoh"]

    assert actual_turnover.loc[1] == pytest.approx(4.0, rel=0.15)
    assert actual_turnover.loc[2] == pytest.approx(12.0, rel=0.15)
    assert actual_turnover.loc[2] > actual_turnover.loc[1]


def test_inventory_turnover_uses_zero_sale_months_in_demand_rate(tmp_path) -> None:
    pd.DataFrame({
        "WarehouseKey": [1],
        "TargetInventoryTurnover": [6.0],
        "TargetShrinkageRatePct": [0.0],
    }).to_parquet(tmp_path / "warehouses.parquet", index=False)
    demand = pd.DataFrame({
        "ProductKey": [1] * 24,
        "WarehouseKey": [1] * 24,
        "Year": [2023] * 12 + [2024] * 12,
        "Month": list(range(1, 13)) * 2,
        "QuantitySold": [12, 0, 0, 0, 0, 0] * 4,
    })

    snapshots = compute_inventory_snapshots(
        demand,
        tmp_path,
        InventoryConfig(
            enabled=True,
            seed=7,
            reorder_compliance=1.0,
            lead_time_variance=0.0,
            overstock_bias=1.0,
            shrinkage_enabled=False,
            min_demand_months=1,
            abc_stock_multiplier={"A": 1.0, "B": 1.0, "C": 1.0},
        ),
    )
    actual_turnover = (
        12.0 * snapshots["QuantitySold"].sum()
        / snapshots["QuantityOnHand"].sum()
    )

    assert actual_turnover == pytest.approx(6.0, rel=0.35)


def test_all_active_powerbi_models_publish_store_warehouse_contract() -> None:
    templates = Path(__file__).parents[1] / "samples" / "powerbi" / "templates"
    store_files = [
        path
        for path in templates.rglob("Store.tmdl")
        if "definition" in path.parts and "tables" in path.parts
    ]
    warehouse_files = [
        path
        for path in templates.rglob("Warehouse.tmdl")
        if "definition" in path.parts and "tables" in path.parts
    ]
    inventory_files = [
        path
        for path in templates.rglob("Inventory.tmdl")
        if "definition" in path.parts and "tables" in path.parts
    ]

    assert len(store_files) == len(warehouse_files) == len(inventory_files) == 6
    for path in store_files:
        text = path.read_text(encoding="utf-8")
        assert "SuccessorStoreKey" in text
        assert not (_DECORATIVE_STORE_COLUMNS & set(text.split()))
    for path in warehouse_files:
        text = path.read_text(encoding="utf-8")
        for column in (
            "TargetInventoryTurnover",
            "LastAuditScore",
            "TargetShrinkageRatePct",
        ):
            assert column in text
    for path in inventory_files:
        assert "ShrinkageQuantity" in path.read_text(encoding="utf-8")
