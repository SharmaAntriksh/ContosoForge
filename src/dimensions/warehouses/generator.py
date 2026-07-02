"""Warehouse dimension generator.

Allocates warehouses proportionally to store density using a three-tier
approach based on configurable thresholds:

  - **Large countries** (stores > min_stores_per_warehouse): split into
    sub-national warehouses grouped by state.
  - **Medium countries** (between thresholds): one warehouse each.
  - **Small countries** (stores < min_stores_for_own_warehouse): merged
    into shared zone-level regional hubs.
  - **Online**: a dedicated online fulfillment warehouse.

Output columns:
  WarehouseKey, WarehouseName, WarehouseType, Zone, Country, Territory,
  GeographyKey, Capacity, SquareFootage
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Tuple

if TYPE_CHECKING:
    from src.engine.config.config_schema import AppConfig

import numpy as np
import pandas as pd

from src.defaults import (
    ONLINE_STORE_KEY_BASE,
    ONLINE_WAREHOUSE_KEY,
    US_STATE_REGIONS,
    WAREHOUSE_TYPES,
    WAREHOUSE_TYPES_P,
)
from src.exceptions import DimensionError
from src.utils.logging_utils import info, skip, stage
from src.utils.output_utils import write_parquet_with_date32
from src.versioning import should_regenerate, save_version
from src.utils.config_helpers import as_dict
from src.utils.config_precedence import resolve_seed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rep_geo_key(geo_series: pd.Series) -> int:
    """Most common GeographyKey in the group, or 0 if empty."""
    if geo_series.empty:
        return 0
    return int(geo_series.astype(np.int64).mode().iloc[0])


def _enrich_with_geography(
    stores_df: pd.DataFrame,
    parquet_dims: Path,
) -> pd.DataFrame:
    """Join stores to geography to get Country and State per store."""
    geo_path = parquet_dims / "geography.parquet"
    if not geo_path.exists():
        raise FileNotFoundError(f"Missing geography parquet: {geo_path}")

    geo = pd.read_parquet(
        str(geo_path), columns=["GeographyKey", "Country", "State"],
    )
    merged = stores_df.merge(geo, on="GeographyKey", how="left")
    merged["Country"] = merged["Country"].astype(str).fillna("Unknown")
    merged["State"] = merged["State"].astype(str).fillna("Unknown")
    return merged


_WTYPE_ABBREV = {
    "Distribution Center": "DC",
    "Regional Hub": "Hub",
    "Fulfillment Center": "FC",
}


def _us_region_label(states: List[str]) -> str:
    """Compact US region label from state list (e.g. 'Northeast', 'South & West')."""
    regions = sorted({US_STATE_REGIONS.get(s, s) for s in states})
    if len(regions) == 1:
        return regions[0]
    return " & ".join(regions[:3])


def _greedy_group(
    items: List[Tuple[str, int]],
    threshold: int,
) -> List[List[str]]:
    """Greedy-group named items by count until each group reaches threshold.

    Items with count >= threshold stand alone.  Remaining items are
    accumulated into groups.  The last group may be under-threshold.

    Args:
        items: list of (name, count) sorted descending by count.
        threshold: minimum count per group.

    Returns:
        List of groups, where each group is a list of names.
    """
    groups: List[List[str]] = []
    current_group: List[str] = []
    current_count = 0

    for name, count in items:
        if count >= threshold:
            groups.append([name])
        else:
            current_group.append(name)
            current_count += count
            if current_count >= threshold:
                groups.append(current_group)
                current_group = []
                current_count = 0

    if current_group:
        groups.append(current_group)

    return groups


# ---------------------------------------------------------------------------
# Allocation tiers
# ---------------------------------------------------------------------------

def _allocate_large(
    enriched: pd.DataFrame,
    country: str,
    rng: np.random.Generator,
    wk: int,
    min_stores_per_wh: int,
) -> Tuple[List[dict], Dict[int, int], int]:
    """Split a large country into sub-national warehouses grouped by state."""
    country_stores = enriched[enriched["Country"] == country]
    state_counts = (
        country_stores.groupby("State").size()
        .sort_values(ascending=False)
    )
    items = [(s, c) for s, c in state_counts.items()]
    state_groups = _greedy_group(items, min_stores_per_wh)

    rows: List[dict] = []
    store_to_wh: Dict[int, int] = {}
    zone = country_stores["StoreZone"].iloc[0]
    n_groups = len(state_groups)

    for idx, group_states in enumerate(state_groups, 1):
        mask = country_stores["State"].isin(group_states)
        grp = country_stores[mask]
        n_stores = len(grp)
        wtype = rng.choice(WAREHOUSE_TYPES, p=WAREHOUSE_TYPES_P)
        rep_geo = _rep_geo_key(grp["GeographyKey"])

        abbrev = _WTYPE_ABBREV[wtype]
        territory = f"{country}: {', '.join(sorted(group_states))}"

        if country == "United States":
            region = _us_region_label(group_states)
            wh_name = f"US {region} {abbrev}"
        elif n_groups == 1:
            wh_name = f"{country} {abbrev}"
        else:
            wh_name = f"{country} {abbrev} {idx}"

        rows.append({
            "WarehouseKey": np.int32(wk),
            "WarehouseName": wh_name,
            "WarehouseType": wtype,
            "Zone": zone,
            "Country": country,
            "Territory": territory,
            "GeographyKey": np.int32(rep_geo),
            "Capacity": np.int32(int(rng.integers(5_000, 15_000) * n_stores)),
            "SquareFootage": np.int32(int(rng.integers(50_000, 200_000) + n_stores * 5_000)),
        })

        for s in grp["StoreKey"].astype(int):
            store_to_wh[s] = wk
        wk += 1

    return rows, store_to_wh, wk


def _allocate_medium(
    enriched: pd.DataFrame,
    country: str,
    rng: np.random.Generator,
    wk: int,
) -> Tuple[dict, Dict[int, int], int]:
    """One warehouse for a medium-sized country."""
    country_stores = enriched[enriched["Country"] == country]
    n_stores = len(country_stores)
    zone = country_stores["StoreZone"].iloc[0]
    wtype = rng.choice(WAREHOUSE_TYPES, p=WAREHOUSE_TYPES_P)
    rep_geo = _rep_geo_key(country_stores["GeographyKey"])

    row = {
        "WarehouseKey": np.int32(wk),
        "WarehouseName": f"{country} {_WTYPE_ABBREV[wtype]}",
        "WarehouseType": wtype,
        "Zone": zone,
        "Country": country,
        "Territory": country,
        "GeographyKey": np.int32(rep_geo),
        "Capacity": np.int32(int(rng.integers(5_000, 15_000) * n_stores)),
        "SquareFootage": np.int32(int(rng.integers(50_000, 200_000) + n_stores * 5_000)),
    }

    store_to_wh: Dict[int, int] = {}
    for s in country_stores["StoreKey"].astype(int):
        store_to_wh[s] = wk

    return row, store_to_wh, wk + 1


def _allocate_small(
    enriched: pd.DataFrame,
    countries: List[str],
    zone: str,
    rng: np.random.Generator,
    wk: int,
    min_stores_for_own_warehouse: int,
) -> Tuple[List[dict], Dict[int, int], int]:
    """Merge small countries in the same zone into shared regional hubs."""
    if not countries:
        return [], {}, wk

    country_counts = []
    for c in countries:
        n = len(enriched[enriched["Country"] == c])
        country_counts.append((c, n))
    country_counts.sort(key=lambda x: -x[1])

    groups = _greedy_group(country_counts, min_stores_for_own_warehouse)

    rows: List[dict] = []
    store_to_wh: Dict[int, int] = {}

    for group_countries in groups:
        mask = enriched["Country"].isin(group_countries)
        grp = enriched[mask]
        n_stores = len(grp)
        wtype = rng.choice(WAREHOUSE_TYPES, p=WAREHOUSE_TYPES_P)
        rep_geo = _rep_geo_key(grp["GeographyKey"])

        abbrev = _WTYPE_ABBREV[wtype]
        if len(group_countries) == 1:
            name = f"{group_countries[0]} {abbrev}"
            territory = group_countries[0]
        else:
            territory = ", ".join(sorted(group_countries))
            name = f"{zone} {abbrev} {len(rows) + 1}"

        rows.append({
            "WarehouseKey": np.int32(wk),
            "WarehouseName": name,
            "WarehouseType": wtype,
            "Zone": zone,
            "Country": sorted(group_countries)[0],  # primary country
            "Territory": territory,
            "GeographyKey": np.int32(rep_geo),
            "Capacity": np.int32(int(rng.integers(5_000, 15_000) * n_stores)),
            "SquareFootage": np.int32(int(rng.integers(50_000, 200_000) + n_stores * 5_000)),
        })

        for s in grp["StoreKey"].astype(int):
            store_to_wh[s] = wk
        wk += 1

    return rows, store_to_wh, wk


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_warehouse_table(
    stores_df: pd.DataFrame,
    parquet_dims: Path,
    seed: int = 42,
    min_stores_per_warehouse: int = 15,
    min_stores_for_own_warehouse: int = 5,
) -> Tuple[pd.DataFrame, Dict[int, int]]:
    """Generate warehouse dimension using demand-proportional allocation.

    Returns:
        (warehouses_df, store_to_warehouse)
        where store_to_warehouse maps StoreKey -> WarehouseKey.
    """
    rng = np.random.default_rng(seed)

    sk = stores_df["StoreKey"].astype(np.int64)
    physical = stores_df[sk < ONLINE_STORE_KEY_BASE].copy()
    online = stores_df[sk >= ONLINE_STORE_KEY_BASE].copy()

    if "StoreZone" not in physical.columns:
        raise DimensionError("stores.parquet missing StoreZone column")

    enriched = _enrich_with_geography(physical, parquet_dims)
    enriched["StoreZone"] = physical["StoreZone"].values

    # Classify countries by store count
    country_counts = enriched.groupby("Country").size()

    large = sorted(country_counts[country_counts > min_stores_per_warehouse].index)
    medium = sorted(
        country_counts[
            (country_counts >= min_stores_for_own_warehouse)
            & (country_counts <= min_stores_per_warehouse)
        ].index
    )
    small = sorted(
        country_counts[country_counts < min_stores_for_own_warehouse].index
    )

    all_rows: List[dict] = []
    all_store_to_wh: Dict[int, int] = {}
    wk = 1

    # Large countries: split by state
    for country in large:
        rows, s2w, wk = _allocate_large(
            enriched, country, rng, wk, min_stores_per_warehouse,
        )
        all_rows.extend(rows)
        all_store_to_wh.update(s2w)

    # Medium countries: one warehouse each
    for country in medium:
        row, s2w, wk = _allocate_medium(enriched, country, rng, wk)
        all_rows.append(row)
        all_store_to_wh.update(s2w)

    # Small countries: merge within zone
    zone_smalls: Dict[str, List[str]] = {}
    for c in small:
        z = enriched[enriched["Country"] == c]["StoreZone"].iloc[0]
        zone_smalls.setdefault(z, []).append(c)

    for zone_name in sorted(zone_smalls):
        rows, s2w, wk = _allocate_small(
            enriched, zone_smalls[zone_name], zone_name, rng, wk,
            min_stores_for_own_warehouse,
        )
        all_rows.extend(rows)
        all_store_to_wh.update(s2w)

    # --- Online fulfillment warehouse ---
    online_geo = _rep_geo_key(online.get("GeographyKey", pd.Series(dtype="int64")))
    all_rows.append({
        "WarehouseKey": np.int32(ONLINE_WAREHOUSE_KEY),
        "WarehouseName": "Online FC",
        "WarehouseType": WAREHOUSE_TYPES[2],
        "Zone": "Online",
        "Country": "Global",
        "Territory": "Online",
        "GeographyKey": np.int32(online_geo),
        "Capacity": np.int32(rng.integers(100_000, 500_000)),
        "SquareFootage": np.int32(rng.integers(200_000, 800_000)),
    })
    for s in online["StoreKey"].astype(int):
        all_store_to_wh[s] = ONLINE_WAREHOUSE_KEY

    # Guarantee unique WarehouseName: large countries (e.g. the US) can split
    # into multiple state-group warehouses that collapse to the same
    # "{region} {type}" label, and a shared zone hub can repeat too. Names are
    # a natural grouping key in BI, so suffix any collision deterministically.
    _seen_names: Dict[str, int] = {}
    for row in all_rows:
        nm = row["WarehouseName"]
        if nm in _seen_names:
            _seen_names[nm] += 1
            row["WarehouseName"] = f"{nm} ({_seen_names[nm]})"
        else:
            _seen_names[nm] = 1

    warehouses_df = pd.DataFrame(all_rows)
    n_physical = len(warehouses_df) - 1
    info(
        f"Warehouses: {n_physical} physical + 1 online = "
        f"{len(warehouses_df)} total, serving {len(all_store_to_wh)} stores"
    )

    return warehouses_df, all_store_to_wh


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_warehouses(cfg: AppConfig, parquet_folder: Path) -> None:
    """Generate and write the Warehouses dimension + update Stores with FK."""
    parquet_folder = Path(parquet_folder)
    out_path = parquet_folder / "warehouses.parquet"
    stores_path = parquet_folder / "stores.parquet"

    if not stores_path.exists():
        raise FileNotFoundError(f"Missing stores parquet: {stores_path}")

    stores_df = pd.read_parquet(str(stores_path))

    wh_cfg = as_dict(getattr(cfg, "warehouses", None)) or {}
    seed = resolve_seed(cfg, wh_cfg, fallback=42)
    min_spw = int(wh_cfg.get("min_stores_per_warehouse", 15))
    min_sow = int(wh_cfg.get("min_stores_for_own_warehouse", 5))

    version_cfg = dict(wh_cfg)
    version_cfg["schema_version"] = 3  # v3: resolved seed folded into version key
    version_cfg["seed"] = int(seed)
    version_cfg["_n_stores"] = len(stores_df)
    if "StoreZone" in stores_df.columns:
        version_cfg["_zones"] = sorted(stores_df["StoreZone"].unique().tolist())

    if not should_regenerate("warehouses", version_cfg, out_path):
        skip("Warehouses up-to-date")
        return

    with stage("Generating Warehouses"):
        warehouses_df, store_to_wh = generate_warehouse_table(
            stores_df, parquet_folder, seed=seed,
            min_stores_per_warehouse=min_spw,
            min_stores_for_own_warehouse=min_sow,
        )

        write_parquet_with_date32(
            warehouses_df, out_path,
            cast_all_datetime=False,
            compression="snappy",
        )

        # Add WarehouseKey FK to stores and overwrite.
        # NOTE: this rewrite hardcodes cast_all_datetime=True + compression="snappy",
        # ignoring stores' configured parquet_compression / force_date32. Consolidating
        # stores.parquet under a single writer is tracked in docs/EMPLOYEES_REFACTOR_PLAN.md.
        stores_df["WarehouseKey"] = (
            stores_df["StoreKey"].astype(int).map(store_to_wh).astype("Int32")
        )
        write_parquet_with_date32(
            stores_df, stores_path,
            cast_all_datetime=True,
            compression="snappy",
        )
        info(f"Updated {stores_path.name} with WarehouseKey column")

    save_version("warehouses", version_cfg, out_path)
    info(f"Warehouses written: {out_path.name}")
