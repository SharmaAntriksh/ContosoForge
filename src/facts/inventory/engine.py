"""Inventory snapshot engine.

Simulates monthly inventory levels for each (ProductKey, WarehouseKey) pair
using accumulated sales demand and ProductProfile replenishment attributes.

Vectorized: loops only over months (sequential dependency), processes all
product-store pairs simultaneously via numpy. Typical runtime: <2s for
240K pairs × 48 months.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


# ================================================================
# Config
# ================================================================

@dataclass(frozen=True)
class InventoryConfig:
    enabled: bool = False
    seed: int = 42
    grain: str = "monthly"

    initial_stock_multiplier: float = 3.0
    reorder_compliance: float = 0.85
    lead_time_variance: float = 0.20
    overstock_bias: float = 1.15

    shrinkage_enabled: bool = True
    shrinkage_rate: float = 0.02

    min_demand_months: int = 3

    abc_filter: Optional[list] = None

    abc_stock_multiplier: Dict[str, float] = field(default_factory=lambda: {
        "A": 1.30,
        "B": 1.00,
        "C": 0.70,
    })


def load_inventory_config(cfg: Dict[str, Any]) -> InventoryConfig:
    raw = getattr(cfg, "inventory", None) or {}
    if not isinstance(raw, Mapping):
        return InventoryConfig()

    shrinkage = getattr(raw, "shrinkage", None) or {}
    abc = getattr(raw, "abc_stock_multiplier", {
        "A": 1.30, "B": 1.00, "C": 0.70,
    })

    abc_filter_raw = getattr(raw, "abc_filter", None)
    abc_filter = None
    if abc_filter_raw is not None and isinstance(abc_filter_raw, (list, tuple)):
        abc_filter = [str(x).upper() for x in abc_filter_raw]

    grain = str(getattr(raw, "grain", "monthly")).lower()
    if grain not in {"monthly", "quarterly"}:
        raise ValueError(f"inventory.grain must be 'monthly' or 'quarterly', got {grain!r}")

    return InventoryConfig(
        enabled=bool(getattr(raw, "enabled", False)),
        seed=int(getattr(raw, "seed", 42)),
        grain=grain,
        initial_stock_multiplier=float(getattr(raw, "initial_stock_multiplier", 3.0)),
        reorder_compliance=float(getattr(raw, "reorder_compliance", 0.85)),
        lead_time_variance=float(getattr(raw, "lead_time_variance", 0.20)),
        overstock_bias=float(getattr(raw, "overstock_bias", 1.15)),
        shrinkage_enabled=bool(getattr(shrinkage, "enabled", True)),
        shrinkage_rate=float(getattr(shrinkage, "rate", 0.02)),
        abc_stock_multiplier=dict(abc),
        min_demand_months=int(getattr(raw, "min_demand_months", 3)),
        abc_filter=abc_filter,
    )


# ================================================================
# ProductProfile attribute loading
# ================================================================

_PRODUCT_PROFILE_COLS = [
    "ProductKey",
    "SafetyStockUnits",
    "ReorderPointUnits",
    "LeadTimeDays",
    "ABCClassification",
    "SeasonalityProfile",
    "IsFragile",
    "CasePackQty",
]

_PROFILE_DEFAULTS = {
    "SafetyStockUnits": 20,
    "ReorderPointUnits": 10,
    "LeadTimeDays": 14,
    "ABCClassification": "B",
    "SeasonalityProfile": "None",
    "IsFragile": 0,
    "CasePackQty": 1,
}


def _load_product_attrs(parquet_dims: Path) -> pd.DataFrame:
    """Load replenishment-relevant columns from ProductProfile."""
    pp_path = parquet_dims / "product_profile.parquet"
    if not pp_path.exists():
        return pd.DataFrame(columns=_PRODUCT_PROFILE_COLS)

    try:
        df = pd.read_parquet(pp_path, columns=_PRODUCT_PROFILE_COLS)
    except (KeyError, ValueError, OSError):
        available = pd.read_parquet(pp_path, columns=None).columns.tolist()
        cols_to_load = [c for c in _PRODUCT_PROFILE_COLS if c in available]
        if "ProductKey" not in cols_to_load:
            return pd.DataFrame(columns=_PRODUCT_PROFILE_COLS)
        df = pd.read_parquet(pp_path, columns=cols_to_load)

    for col, default in _PROFILE_DEFAULTS.items():
        if col not in df.columns:
            df[col] = default

    return df


# ================================================================
# Vectorized simulation
# ================================================================

def compute_inventory_snapshots(
    demand: pd.DataFrame,
    parquet_dims: Any,
    icfg: InventoryConfig,
    product_attrs_arrays: Any = None,
) -> pd.DataFrame:
    """
    Simulate monthly inventory snapshots from accumulated sales demand.

    Fully vectorized across all (ProductKey, WarehouseKey) pairs. Loops only
    over the month axis (sequential dependency).
    """
    if demand.empty:
        return _empty_snapshot()

    parquet_dims = Path(parquet_dims)
    rng = np.random.default_rng(icfg.seed)

    # ------------------------------------------------------------------
    # 1. Build sorted month timeline
    # ------------------------------------------------------------------
    month_keys = demand[["Year", "Month"]].drop_duplicates().sort_values(["Year", "Month"])
    years_arr = month_keys["Year"].to_numpy(dtype=np.int32)
    months_arr = month_keys["Month"].to_numpy(dtype=np.int32)
    n_months = len(years_arr)

    if n_months == 0:
        return _empty_snapshot()

    # ------------------------------------------------------------------
    # 2. Build unique pairs and demand matrix (N_pairs × N_months)
    # ------------------------------------------------------------------
    pair_df = demand[["ProductKey", "WarehouseKey"]].drop_duplicates().reset_index(drop=True)
    n_pairs = len(pair_df)

    pair_pk = pair_df["ProductKey"].to_numpy(dtype=np.int32)
    pair_wk = pair_df["WarehouseKey"].to_numpy(dtype=np.int32)

    # Remap warehouse keys to dense 0..N indices to avoid a sparse
    # lookup array (ONLINE_WAREHOUSE_KEY=9000 would waste ~200MB).
    unique_wk = np.unique(pair_wk)
    wk_to_dense = np.full(int(unique_wk.max()) + 1, -1, dtype=np.int32)
    wk_to_dense[unique_wk] = np.arange(len(unique_wk), dtype=np.int32)

    pair_wk_dense = wk_to_dense[pair_wk]

    max_pk = int(pair_pk.max()) + 1
    max_wk = len(unique_wk)
    pair_lookup = np.full((max_pk, max_wk), -1, dtype=np.int32)
    pair_lookup[pair_pk, pair_wk_dense] = np.arange(n_pairs, dtype=np.int32)

    demand_matrix = np.zeros((n_pairs, n_months), dtype=np.int32)

    d_pk = demand["ProductKey"].to_numpy(dtype=np.int32)
    d_wk = demand["WarehouseKey"].to_numpy(dtype=np.int32)
    d_yr = demand["Year"].to_numpy(dtype=np.int32)
    d_mo = demand["Month"].to_numpy(dtype=np.int32)
    d_qty = demand["QuantitySold"].to_numpy(dtype=np.int32)

    d_wk_dense = wk_to_dense[d_wk]
    d_pair_idx = pair_lookup[d_pk, d_wk_dense]
    # Vectorized month-index mapping: encode year*12+month as a single int, then searchsorted
    month_keys_encoded = years_arr.astype(np.int32) * 12 + months_arr.astype(np.int32)
    d_encoded = d_yr * np.int32(12) + d_mo
    d_month_idx = np.searchsorted(month_keys_encoded, d_encoded).astype(np.int32)

    valid = (d_pair_idx >= 0) & (d_month_idx >= 0) & (d_month_idx < n_months)
    np.add.at(demand_matrix, (d_pair_idx[valid], d_month_idx[valid]), d_qty[valid])

    # Filter to pairs with recurring demand (stocked assortment items)
    months_with_demand = (demand_matrix > 0).sum(axis=1)
    keep_mask = months_with_demand >= icfg.min_demand_months

    if not keep_mask.any():
        return _empty_snapshot()

    demand_matrix = demand_matrix[keep_mask]
    pair_pk = pair_pk[keep_mask]
    pair_wk = pair_wk[keep_mask]
    n_pairs = int(keep_mask.sum())

    # ------------------------------------------------------------------
    # 2b. ABC filter — restrict to specified classifications
    # ------------------------------------------------------------------
    if icfg.abc_filter is not None and len(icfg.abc_filter) > 0:
        if product_attrs_arrays is not None:
            # Use numpy arrays directly — avoid DataFrame round-trip
            _pa_df = product_attrs_arrays  # dict of numpy arrays
        else:
            _pa_df = _load_product_attrs(parquet_dims)

        # Support both dict-of-arrays and DataFrame
        _has_abc = (
            ("ABCClassification" in _pa_df) if isinstance(_pa_df, dict)
            else (not _pa_df.empty and "ABCClassification" in _pa_df.columns)
        )
        if _has_abc:
            allowed_set = set(icfg.abc_filter)
            _pa_pk = np.asarray(_pa_df["ProductKey"], dtype=np.int32)
            _pa_abc = np.asarray(_pa_df["ABCClassification"]).astype(str)
            _pa_max = int(_pa_pk.max()) + 1
            # Dense lookup: 1 = allowed, 0 = excluded
            dense_allowed = np.zeros(_pa_max, dtype=np.int8)
            for cls in allowed_set:
                dense_allowed[_pa_pk[_pa_abc == cls]] = 1

            abc_keep = np.ones(n_pairs, dtype=bool)
            in_range = pair_pk < _pa_max
            abc_keep[in_range] = dense_allowed[pair_pk[in_range]] == 1
            # Products not in product_profile are kept (no ABC data to filter on)

            if not abc_keep.any():
                return _empty_snapshot()

            demand_matrix = demand_matrix[abc_keep]
            pair_pk = pair_pk[abc_keep]
            pair_wk = pair_wk[abc_keep]
            n_pairs = int(abc_keep.sum())

    # ------------------------------------------------------------------
    # 3. Load product attributes into dense arrays aligned to pairs
    # ------------------------------------------------------------------
    # Use numpy arrays directly when available — avoid DataFrame round-trip
    if product_attrs_arrays is not None:
        product_attrs = product_attrs_arrays  # dict of numpy arrays
    else:
        product_attrs = _load_product_attrs(parquet_dims)

    attr_safety = np.full(n_pairs, 20, dtype=np.int32)
    attr_reorder_pt = np.full(n_pairs, 10, dtype=np.int32)
    attr_lead_days = np.full(n_pairs, 14, dtype=np.int32)
    attr_abc_mult = np.ones(n_pairs, dtype=np.float64)

    _pa_has_data = (
        len(product_attrs) > 0 if isinstance(product_attrs, dict)
        else not product_attrs.empty
    )
    if _pa_has_data:
        pa_pk = np.asarray(product_attrs["ProductKey"], dtype=np.int32)
        pa_ss = np.asarray(product_attrs["SafetyStockUnits"], dtype=np.int32)
        pa_rp = np.asarray(product_attrs["ReorderPointUnits"], dtype=np.int32)
        pa_ld = np.asarray(product_attrs["LeadTimeDays"], dtype=np.int32)
        pa_abc = np.asarray(product_attrs["ABCClassification"]).astype(str)

        pa_max = int(pa_pk.max()) + 1
        dense_safety = np.full(pa_max, 20, dtype=np.int32)
        dense_reorder = np.full(pa_max, 10, dtype=np.int32)
        dense_lead = np.full(pa_max, 14, dtype=np.int32)
        dense_abc_mult = np.ones(pa_max, dtype=np.float64)

        dense_safety[pa_pk] = pa_ss
        dense_reorder[pa_pk] = pa_rp
        dense_lead[pa_pk] = pa_ld
        for abc_class, mult in icfg.abc_stock_multiplier.items():
            dense_abc_mult[pa_pk[pa_abc == abc_class]] = mult

        in_range = pair_pk < pa_max
        attr_safety[in_range] = dense_safety[pair_pk[in_range]]
        attr_reorder_pt[in_range] = dense_reorder[pair_pk[in_range]]
        attr_lead_days[in_range] = dense_lead[pair_pk[in_range]]
        attr_abc_mult[in_range] = dense_abc_mult[pair_pk[in_range]]

    # New profile attributes: seasonality (int8 encoded), fragility, case pack qty
    _SEASON_ENCODE = {"Holiday": 1, "Winter": 2, "Summer": 3, "BackToSchool": 4, "Spring": 5}
    attr_seasonality = np.zeros(n_pairs, dtype=np.int8)
    attr_fragile = np.zeros(n_pairs, dtype=np.int32)
    attr_case_pack = np.ones(n_pairs, dtype=np.int32)

    if _pa_has_data:
        pa_season_str = np.asarray(product_attrs["SeasonalityProfile"]).astype(str)
        pa_fragile = np.asarray(product_attrs["IsFragile"], dtype=np.int32)
        pa_case = np.asarray(product_attrs["CasePackQty"], dtype=np.int32)

        # Encode seasonality as int8
        pa_season = np.zeros(len(pa_season_str), dtype=np.int8)
        for sname, scode in _SEASON_ENCODE.items():
            pa_season[pa_season_str == sname] = scode

        dense_season = np.zeros(pa_max, dtype=np.int8)
        dense_fragile = np.zeros(pa_max, dtype=np.int32)
        dense_case = np.ones(pa_max, dtype=np.int32)

        dense_season[pa_pk] = pa_season
        dense_fragile[pa_pk] = pa_fragile
        dense_case[pa_pk] = np.maximum(pa_case, 1)

        attr_seasonality[in_range] = dense_season[pair_pk[in_range]]
        attr_fragile[in_range] = dense_fragile[pair_pk[in_range]]
        attr_case_pack[in_range] = dense_case[pair_pk[in_range]]

    attr_lead_months = np.maximum(1, np.round(attr_lead_days / 30.0).astype(np.int32))
    # Pre-convert to float64 for use in main loop (avoids per-iteration .astype())
    attr_lead_months_f64 = attr_lead_months.astype(np.float64)

    # ------------------------------------------------------------------
    # 4. Compute per-pair initial conditions
    # ------------------------------------------------------------------
    # First demand month per pair — inventory only exists from this month onward.
    has_demand = demand_matrix > 0
    first_demand = np.where(
        has_demand.any(axis=1),
        has_demand.argmax(axis=1),
        n_months,  # sentinel: pair never had demand (shouldn't happen, but safe)
    )

    # Compute average demand without a full float64 copy
    demand_sum = demand_matrix.sum(axis=1).astype(np.float64)
    demand_nonzero_months = (demand_matrix > 0).sum(axis=1)
    avg_demand = np.where(demand_nonzero_months > 0, demand_sum / demand_nonzero_months, 1.0)
    avg_demand = np.maximum(avg_demand, 1.0)

    initial_stock = (
        avg_demand * icfg.initial_stock_multiplier * attr_abc_mult * icfg.overstock_bias
    ).astype(np.int32)

    reorder_qty = (
        (attr_safety + avg_demand * attr_lead_months) * attr_abc_mult * icfg.overstock_bias
    ).astype(np.int32)
    reorder_qty = np.maximum(reorder_qty, (avg_demand * attr_lead_months * attr_abc_mult).astype(np.int32))

    # Round reorder quantities up to CasePackQty multiples
    reorder_qty = (np.ceil(reorder_qty / attr_case_pack) * attr_case_pack).astype(np.int32)

    monthly_shrinkage_base = icfg.shrinkage_rate / 12.0 if icfg.shrinkage_enabled else 0.0
    # Fragile items shrink at 2.5x the base rate
    monthly_shrinkage_arr = np.where(
        attr_fragile == 1,
        monthly_shrinkage_base * 2.5,
        monthly_shrinkage_base,
    )

    # Seasonality calendar multipliers — pre-compute full (n_pairs × 12) table
    # using int8-encoded seasonality codes instead of per-month string comparison
    _SEASON_MONTH_BOOST: dict[int, dict[int, float]] = {
        1: {11: 0.6, 12: 0.6, 1: 0.2, 10: 0.3},           # Holiday
        2: {11: 0.4, 12: 0.4, 1: 0.4, 2: 0.3},             # Winter
        3: {6: 0.4, 7: 0.4, 8: 0.3, 5: 0.2},               # Summer
        4: {7: 0.3, 8: 0.5, 9: 0.3},                         # BackToSchool
        5: {3: 0.3, 4: 0.4, 5: 0.3},                         # Spring
    }

    # Pre-compute: seasonal_mult_table[month_1based] = per-pair multiplier array
    seasonal_mult_table = np.ones((13, n_pairs), dtype=np.float64)  # index 1..12
    for scode, boosts in _SEASON_MONTH_BOOST.items():
        mask = attr_seasonality == scode
        if not mask.any():
            continue
        for month_1, boost in boosts.items():
            seasonal_mult_table[month_1, mask] += boost

    # ------------------------------------------------------------------
    # 5. Pre-generate all random draws (avoids per-step RNG calls)
    # ------------------------------------------------------------------
    rand_compliance = rng.random((n_pairs, n_months))
    rand_lt_jitter = rng.uniform(
        -icfg.lead_time_variance, icfg.lead_time_variance,
        size=(n_pairs, n_months),
    )

    # ------------------------------------------------------------------
    # 6. Vectorized month-by-month simulation
    # ------------------------------------------------------------------
    out_qoh = np.zeros((n_pairs, n_months), dtype=np.int32)
    out_on_order = np.zeros((n_pairs, n_months), dtype=np.int32)
    # out_sold is demand_matrix itself (never modified) — no copy needed
    out_received = np.zeros((n_pairs, n_months), dtype=np.int32)
    out_reorder = np.zeros((n_pairs, n_months), dtype=np.int8)
    out_stockout = np.zeros((n_pairs, n_months), dtype=np.int8)
    out_days_oos = np.zeros((n_pairs, n_months), dtype=np.int8)

    # active[i] tracks whether pair i has been activated (first sale reached)
    active_mask = np.zeros((n_pairs, n_months), dtype=bool)

    # Buffer for future replenishment arrivals: derived from max lead time + jitter headroom
    max_lead_buffer = max(int(attr_lead_months.max()) * 2, 12) if n_pairs > 0 else 12
    pending = np.zeros((n_pairs, n_months + max_lead_buffer), dtype=np.int32)

    qoh = np.zeros(n_pairs, dtype=np.int32)

    for t in range(n_months):
        # Activate pairs whose first demand month is this month
        newly_active = first_demand == t
        qoh[newly_active] = initial_stock[newly_active]

        is_active = t >= first_demand
        active_mask[:, t] = is_active

        received = pending[:, t]
        qoh += received * is_active
        out_received[:, t] = received * is_active

        sold = demand_matrix[:, t]
        if monthly_shrinkage_base > 0:
            shrink = np.multiply(qoh, monthly_shrinkage_arr, dtype=np.float64).astype(np.int32)
        else:
            shrink = 0
        qoh -= (sold + shrink) * is_active

        stockout_mask = (qoh < 0) & is_active
        daily_demand = np.maximum(1.0, sold * (1.0 / 30.0))
        days_oos = np.where(
            stockout_mask,
            np.minimum(30, np.ceil(np.abs(qoh) / daily_demand).astype(np.int32)),
            0,
        )
        qoh = np.maximum(qoh, 0)

        # Seasonal reorder point: boost threshold during peak months
        cal_month = int(months_arr[t])
        if not (1 <= cal_month <= 12):
            raise ValueError(f"Inventory engine: invalid calendar month {cal_month} at timeline index {t}")
        effective_reorder_pt = (attr_reorder_pt * seasonal_mult_table[cal_month]).astype(np.int32)

        reorder_mask = (qoh <= effective_reorder_pt) & is_active
        comply_mask = rand_compliance[:, t] < icfg.reorder_compliance
        trigger_mask = reorder_mask & comply_mask

        lt_jittered = (
            attr_lead_months_f64 * (1.0 + rand_lt_jitter[:, t])
        ).astype(np.int32)
        arrival_offset = np.maximum(1, lt_jittered)
        arrival_t = t + arrival_offset

        triggered_idx = np.flatnonzero(trigger_mask)
        if triggered_idx.size > 0:
            arr_t = arrival_t[triggered_idx]
            rq = reorder_qty[triggered_idx]
            valid = arr_t < pending.shape[1]
            if valid.any():
                valid_idx = triggered_idx[valid]
                valid_arr_t = arr_t[valid]
                valid_rq = rq[valid]
                pending[valid_idx, valid_arr_t] += valid_rq
            out_on_order[triggered_idx, t] = rq

        out_qoh[:, t] = qoh
        out_reorder[:, t] = reorder_mask.astype(np.int8)
        out_stockout[:, t] = stockout_mask.astype(np.int8)
        out_days_oos[:, t] = np.clip(days_oos, 0, 127).astype(np.int8)

    # ------------------------------------------------------------------
    # 7. Flatten to DataFrame (only active cells)
    # ------------------------------------------------------------------
    snapshot_dates = pd.to_datetime(
        [f"{years_arr[m]}-{months_arr[m]:02d}-01" for m in range(n_months)]
    )

    row_idx, col_idx = np.nonzero(active_mask)
    if row_idx.size == 0:
        return _empty_snapshot()

    # Build DataFrame with correct dtypes directly (avoids 9 separate .astype() passes)
    result = pd.DataFrame({
        "ProductKey": pair_pk[row_idx].astype(np.int32),
        "WarehouseKey": pair_wk[row_idx].astype(np.int32),
        "SnapshotDate": snapshot_dates[col_idx],
        "QuantityOnHand": out_qoh[row_idx, col_idx].astype(np.int32),
        "QuantityOnOrder": out_on_order[row_idx, col_idx].astype(np.int32),
        "QuantitySold": demand_matrix[row_idx, col_idx].astype(np.int32),
        "QuantityReceived": out_received[row_idx, col_idx].astype(np.int32),
        "ReorderFlag": out_reorder[row_idx, col_idx].astype(bool),
        "StockoutFlag": out_stockout[row_idx, col_idx].astype(bool),
        "DaysOutOfStock": out_days_oos[row_idx, col_idx].astype(np.int32),
    })

    if icfg.grain == "quarterly":
        result = _aggregate_quarterly(result)

    return result


def _aggregate_quarterly(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate monthly snapshots to quarterly grain.

    SnapshotDate becomes the first day of each quarter.
    - QuantityOnHand: end-of-quarter value (last month in quarter)
    - QuantitySold, QuantityOnOrder, QuantityReceived: summed over quarter
    - ReorderFlag, StockoutFlag: 1 if any month in quarter flagged
    - DaysOutOfStock: summed over quarter (capped at 90)
    """
    dates = pd.to_datetime(df["SnapshotDate"])
    df = df.copy()
    df["_Quarter"] = dates.dt.to_period("Q")

    group_keys = ["ProductKey", "WarehouseKey", "_Quarter"]
    agg = df.groupby(group_keys, sort=True).agg(
        QuantityOnHand=("QuantityOnHand", "last"),
        QuantityOnOrder=("QuantityOnOrder", "sum"),
        QuantitySold=("QuantitySold", "sum"),
        QuantityReceived=("QuantityReceived", "sum"),
        ReorderFlag=("ReorderFlag", "max"),
        StockoutFlag=("StockoutFlag", "max"),
        DaysOutOfStock=("DaysOutOfStock", "sum"),
    ).reset_index()

    # Quarter start date as SnapshotDate
    agg["SnapshotDate"] = agg["_Quarter"].dt.start_time
    agg.drop(columns=["_Quarter"], inplace=True)

    agg["DaysOutOfStock"] = agg["DaysOutOfStock"].clip(upper=90).astype(np.int32)
    agg["ReorderFlag"] = agg["ReorderFlag"].astype(bool)
    agg["StockoutFlag"] = agg["StockoutFlag"].astype(bool)
    agg["ProductKey"] = agg["ProductKey"].astype(np.int32)
    agg["WarehouseKey"] = agg["WarehouseKey"].astype(np.int32)
    agg["QuantityOnHand"] = agg["QuantityOnHand"].astype(np.int32)
    agg["QuantityOnOrder"] = agg["QuantityOnOrder"].astype(np.int32)
    agg["QuantitySold"] = agg["QuantitySold"].astype(np.int32)
    agg["QuantityReceived"] = agg["QuantityReceived"].astype(np.int32)

    return agg[["ProductKey", "WarehouseKey", "SnapshotDate",
                "QuantityOnHand", "QuantityOnOrder",
                "QuantitySold", "QuantityReceived",
                "ReorderFlag", "StockoutFlag", "DaysOutOfStock"]]


def _empty_snapshot() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "ProductKey", "WarehouseKey", "SnapshotDate",
        "QuantityOnHand", "QuantityOnOrder",
        "QuantitySold", "QuantityReceived",
        "ReorderFlag", "StockoutFlag", "DaysOutOfStock",
    ])
