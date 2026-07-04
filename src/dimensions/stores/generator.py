from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.utils.logging_utils import warn
from src.utils.name_pools import assign_person_names, hash_u64
from src.utils.config_helpers import as_dict, int_or, region_from_iso_code
from src.defaults import (
    STORE_TYPES as _STORE_TYPES,
    STORE_TYPES_P as _STORE_TYPES_P,
    STORE_BRANDS as _BRANDS,
    STORE_AREAS as _AREAS,
    STORE_MANAGER_FIRST as _MANAGER_FIRST,
    STORE_MANAGER_LAST as _MANAGER_LAST,
    STORE_ONLINE_SUFFIX as _ONLINE_SUFFIX,
    STORE_REVENUE_CLASSES as _REVENUE_CLASSES,
    STORE_REVENUE_CLASSES_P as _REVENUE_CLASSES_P,
    ONLINE_STORE_KEY_BASE as _ONLINE_SK_BASE,
)
from src.dimensions.stores.geo import _sample_geography_keys
from src.dimensions.stores.columns import (
    _square_footage_from_cfg,
    _employee_count_by_store_type,
    _build_store_format,
    _build_ownership_type,
    _build_hierarchy,
    _build_emails,
    _build_analytical,
    _build_phone,
)
from src.dimensions.stores.lifecycle import (
    assign_status,
    assign_opening_closing_dates,
    assign_renovation_dates,
    backfill_close_reason,
    build_store_descriptions,
)


@dataclass(eq=False)
class GeoContext:
    """Geography lookups passed into store generation."""
    geo_keys: np.ndarray
    geo_loc_short: Optional[Dict[int, str]] = None
    geo_loc_full: Optional[Dict[int, str]] = None
    iso_by_geo: Optional[dict[int, str]] = None
    pop_by_geo: Optional[dict[int, int]] = None
    country_by_geo: Optional[dict[int, str]] = None
    ensure_iso_coverage: bool = False
    region_weights: Optional[dict[str, float]] = None


def generate_store_table(
    *,
    geo: GeoContext,
    num_stores: int = 200,
    opening_start: str = "2018-01-01",
    opening_end: str = "2025-12-31",
    closing_end: str = "2025-12-31",
    seed: int = 42,
    square_footage_cfg: Optional[Dict] = None,
    staffing_overrides: Optional[Dict] = None,
    people_pools=None,
    district_size: int = 10,
    districts_per_region: int = 8,
    dataset_start: Optional[str] = None,
    dataset_end: Optional[str] = None,
    close_share: float = 0.10,
    closing_enabled: bool = True,
    online_stores: Optional[int] = None,
    allow_mid_window_openings: bool = False,
) -> pd.DataFrame:
    """
    Generate synthetic store dimension table.

    Output columns:
      StoreKey, StoreNumber, StoreName, StoreType, StoreFormat, OwnershipType,
      RevenueClass, Status, GeographyKey, StoreZone, StoreDistrict, StoreRegion,
      StoreDistrictId, StoreRegionId,
      OpeningDate, ClosingDate, OpenFlag, SquareFootage, EmployeeCount,
      StoreManager, Phone, StoreEmail, StoreDescription, CloseReason,
      AvgTransactionValue, CustomerSatisfactionScore, InventoryTurnoverTarget,
      LastAuditScore, ShrinkageRatePct
    """
    # Unpack geo context
    geo_keys = geo.geo_keys
    geo_loc_short = geo.geo_loc_short
    geo_loc_full = geo.geo_loc_full
    iso_by_geo = geo.iso_by_geo
    pop_by_geo = geo.pop_by_geo
    country_by_geo = geo.country_by_geo
    ensure_iso_coverage = geo.ensure_iso_coverage
    region_weights = geo.region_weights

    num_stores = int_or(num_stores, 200)

    # Floor: guarantee at least _MIN_STORES for a healthy pipeline run
    # (enough for physical + online mix, store closing, assortment, etc.)
    _MIN_STORES = 6
    if num_stores < _MIN_STORES:
        warn(
            f"num_stores ({num_stores}) is below minimum ({_MIN_STORES}), "
            f"raised to {_MIN_STORES}"
        )
        num_stores = _MIN_STORES

    if not isinstance(geo_keys, np.ndarray) or geo_keys.size == 0:
        raise DimensionError("geo_keys must be a non-empty numpy array of GeographyKey values")

    rng = np.random.default_rng(int_or(seed, 42))

    # --- Key allocation: physical 1..N_phys, online 10_001..10_001+N_online ---
    n_online = int_or(online_stores, 0)
    if n_online < 0:
        n_online = 0
    if n_online >= num_stores:
        clamped = max(num_stores - 1, 0)
        warn(
            f"online_stores ({n_online}) >= num_stores ({num_stores}), "
            f"clamped to {clamped}"
        )
        n_online = clamped
    n_physical = num_stores - n_online
    if n_physical >= _ONLINE_SK_BASE:
        raise DimensionError(
            f"Physical store count ({n_physical}) exceeds ONLINE_STORE_KEY_BASE "
            f"({_ONLINE_SK_BASE}). Max physical stores is {_ONLINE_SK_BASE - 1}."
        )

    phys_keys = np.arange(1, n_physical + 1, dtype=np.int32)
    online_keys = (_ONLINE_SK_BASE + np.arange(1, n_online + 1, dtype=np.int32)) if n_online > 0 else np.array([], dtype=np.int32)
    store_key = np.concatenate([phys_keys, online_keys])
    df = pd.DataFrame({"StoreKey": store_key})
    sk = store_key.astype(np.int32)
    # int64 view for seed-mixing index math below: with random-mode seeds
    # (up to 2**31-1), `sk * k + int(seed) * c` overflows the int32 array's
    # element type on Windows ("Python int too large to convert to C long").
    sk64 = sk.astype(np.int64)

    # StoreNumber — STR-xxxx for physical, ONL-xxxx for online
    store_numbers = np.empty(num_stores, dtype=object)
    store_numbers[:n_physical] = [f"STR-{k:04d}" for k in phys_keys]
    if n_online > 0:
        store_numbers[n_physical:] = [f"ONL-{i:04d}" for i in range(1, n_online + 1)]
    df["StoreNumber"] = pd.array(store_numbers, dtype="object")

    # StoreType — physical stores sampled (excl. Online), online stores set directly
    _phys_mask = _STORE_TYPES != "Online"
    _phys_types = _STORE_TYPES[_phys_mask]
    _phys_p = _STORE_TYPES_P[_phys_mask]
    _phys_p = _phys_p / _phys_p.sum()  # renormalize after removing Online
    store_type_arr = np.empty(num_stores, dtype=object)
    store_type_arr[:n_physical] = rng.choice(_phys_types, size=n_physical, p=_phys_p)
    if n_online > 0:
        store_type_arr[n_physical:] = "Online"
    df["StoreType"] = store_type_arr
    df["StoreFormat"] = _build_store_format(rng, df["StoreType"])
    df["OwnershipType"] = _build_ownership_type(rng, df["StoreType"])
    df["RevenueClass"] = rng.choice(_REVENUE_CLASSES, size=num_stores, p=_REVENUE_CLASSES_P)

    # Status assignment: pick the closing/renovating physical stores.
    assign_status(
        df,
        rng,
        num_stores=num_stores,
        closing_enabled=closing_enabled,
        dataset_start=dataset_start,
        dataset_end=dataset_end,
        close_share=close_share,
    )

    df["GeographyKey"] = _sample_geography_keys(
        rng=rng,
        geo_keys=geo_keys.astype(np.int64),
        n=num_stores,
        iso_by_geo=iso_by_geo,
        pop_by_geo=pop_by_geo,
        ensure_iso_coverage=bool(ensure_iso_coverage),
        region_weights=region_weights,
    )

    # Location strings
    if geo_loc_short is None:
        warn("geo_loc_short not provided; store names will use 'Geo <key>' placeholders.")
        loc_short = df["GeographyKey"].astype(np.int64).map(lambda k: f"Geo {int(k)}")
    else:
        _gk = df["GeographyKey"].astype(np.int64)
        loc_short = _gk.map(geo_loc_short)
        _missing = loc_short.isna()
        if _missing.any():
            n_missing = int(_missing.sum())
            warn(f"{n_missing} store(s) have GeographyKey not found in location map; using 'Geo <key>' fallback.")
            loc_short[_missing] = _gk[_missing].map(lambda k: f"Geo {int(k)}")

    if geo_loc_full is None:
        loc_full = loc_short
    else:
        loc_full = df["GeographyKey"].astype(np.int64).map(
            lambda k: geo_loc_full.get(int(k), f"Geo {int(k)}")
        )

    # Hierarchy: StoreZone, StoreDistrict, StoreRegion (+ structured ids).
    # The int ids are the truth; the strings are display renderings of them.
    # Emitting both lets the employee dimension read the ids directly rather
    # than reverse-parsing the display strings.
    zones, districts, regions, district_id, region_id = _build_hierarchy(
        geo_keys=df["GeographyKey"].to_numpy(dtype=np.int64),
        iso_by_geo=iso_by_geo,
        country_by_geo=country_by_geo,
        district_size=int_or(district_size, 10),
        districts_per_region=int_or(districts_per_region, 8),
    )
    df["StoreZone"]       = zones
    df["StoreDistrict"]   = districts
    df["StoreRegion"]     = regions
    df["StoreDistrictId"] = district_id
    df["StoreRegionId"]   = region_id

    # Manager names
    if people_pools is not None:
        if iso_by_geo:
            gk_arr = df["GeographyKey"].to_numpy(dtype=np.int64)
            iso_arr = np.array(
                [iso_by_geo.get(int(k), "") for k in gk_arr], dtype=object,
            )
            region = np.array(
                [region_from_iso_code(x, default_region="EU") for x in iso_arr],
                dtype=object,
            )
        else:
            region = np.full(num_stores, "US", dtype=object)

        h = hash_u64(sk.astype(np.uint64), int(seed), 7001)
        gender = np.where(
            (h & np.uint64(1)) == 0, "Male", "Female",
        ).astype(object)

        first, last, _ = assign_person_names(
            keys=sk,
            region=region,
            gender=gender,
            is_org=np.zeros(num_stores, dtype=bool),
            pools=people_pools,
            seed=int(seed),
            include_middle=False,
            default_region="US",
        )
        mgr_first = pd.Series(first, dtype="object").astype(str)
        mgr_last = pd.Series(last, dtype="object").astype(str)
    else:
        mf = _MANAGER_FIRST[(sk64 * 5 + int(seed)) % len(_MANAGER_FIRST)]
        ml = _MANAGER_LAST[(sk64 * 11 + int(seed) * 3) % len(_MANAGER_LAST)]
        mgr_first = pd.Series(mf, dtype="object").astype(str)
        mgr_last = pd.Series(ml, dtype="object").astype(str)

    # Emit the first/last components alongside the display column so employees
    # can consume the structured names directly instead of reverse-parsing the
    # concatenated string. StoreManager stays the "First Last" display value.
    df["StoreManagerFirstName"] = mgr_first
    df["StoreManagerLastName"] = mgr_last
    df["StoreManager"] = mgr_first + " " + mgr_last

    # StoreName
    brand = _BRANDS[(sk64 + int(seed)) % len(_BRANDS)]
    area  = _AREAS[(sk64 * 7 + int(seed) * 13) % len(_AREAS)]
    stype = df["StoreType"].astype(str)
    is_online = stype.to_numpy() == "Online"

    online_suffix = _ONLINE_SUFFIX[(sk64 * 3 + int(seed) * 17) % len(_ONLINE_SUFFIX)]

    store_name = (
        pd.Series(brand, dtype="object").astype(str)
        + " "
        + loc_short.astype(str)
        + " "
        + pd.Series(area, dtype="object").astype(str)
    )
    store_name_online = (
        pd.Series(brand, dtype="object").astype(str)
        + " "
        + pd.Series(online_suffix, dtype="object").astype(str)
    )

    df["StoreName"] = store_name
    df.loc[is_online, "StoreName"] = store_name_online.loc[is_online].to_numpy()

    # Opening / closing dates at DAY granularity.
    assign_opening_closing_dates(
        df,
        rng,
        num_stores=num_stores,
        opening_start=opening_start,
        opening_end=opening_end,
        closing_end=closing_end,
        closing_enabled=closing_enabled,
        dataset_start=dataset_start,
        dataset_end=dataset_end,
        allow_mid_window_openings=allow_mid_window_openings,
    )

    # Snapshot Status before the renovation pass may reopen stores; the
    # description text reflects the pre-reopen label (a reopened store reads
    # as renovating, then annotated "reopened").
    status = df["Status"].astype(str)

    assign_renovation_dates(df, rng, dataset_start=dataset_start, dataset_end=dataset_end)

    df["OpenFlag"] = (df["Status"] == "Open").astype(bool)

    df["SquareFootage"] = _square_footage_from_cfg(
        rng=rng,
        store_type=df["StoreType"],
        sqft_cfg=as_dict(square_footage_cfg),
        n=num_stores,
    )
    # Online stores have no physical space
    _online_sqft_mask = df["StoreType"].to_numpy() == "Online"
    if _online_sqft_mask.any():
        df.loc[_online_sqft_mask, "SquareFootage"] = 0

    df["EmployeeCount"] = _employee_count_by_store_type(
        rng=rng,
        store_type=df["StoreType"],
        staffing_overrides=as_dict(staffing_overrides),
    )

    # Phone — format varies by country via ISO/currency code
    gk_arr  = df["GeographyKey"].to_numpy(dtype=np.int64)
    iso_arr = np.array(
        [iso_by_geo.get(int(k), "") if iso_by_geo else "" for k in gk_arr],
        dtype=object,
    )
    df["Phone"] = pd.array(
        [_build_phone(int(k), str(iso)) for k, iso in zip(store_key, iso_arr)],
        dtype="object",
    )

    # StoreEmail
    df["StoreEmail"] = _build_emails(
        brand_arr=brand,
        area_arr=area,
        store_number_arr=df["StoreNumber"].to_numpy(),
        is_online=is_online,
    )

    backfill_close_reason(df, rng, status=status)

    build_store_descriptions(df, stype=stype, status=status, loc_full=loc_full)

    # Analytical columns
    avg_txn, csat, inv_turn, audit, shrink = _build_analytical(
        rng=rng,
        store_type=df["StoreType"],
        revenue_class=df["RevenueClass"].to_numpy(),
        n=num_stores,
    )
    df["AvgTransactionValue"]      = avg_txn
    df["CustomerSatisfactionScore"] = csat
    df["InventoryTurnoverTarget"]   = inv_turn
    df["LastAuditScore"]            = audit
    df["ShrinkageRatePct"]          = shrink

    # Reorder columns to match the static schema (CREATE TABLE column order).
    # StoreName is built late (needs geography lookups) so the DataFrame
    # insertion order diverges from the canonical schema order.
    _SCHEMA_ORDER = [
        "StoreKey", "StoreNumber", "StoreName", "StoreType", "StoreFormat",
        "OwnershipType", "RevenueClass", "Status", "GeographyKey",
        "StoreZone", "StoreDistrict", "StoreRegion",
        "StoreDistrictId", "StoreRegionId",
        "OpeningDate", "ClosingDate",
        "RenovationStartDate", "RenovationEndDate",
        "OpenFlag", "SquareFootage",
        "EmployeeCount", "StoreManager",
        "StoreManagerFirstName", "StoreManagerLastName",
        "Phone", "StoreEmail",
        "StoreDescription", "CloseReason",
        "AvgTransactionValue", "CustomerSatisfactionScore",
        "InventoryTurnoverTarget", "LastAuditScore", "ShrinkageRatePct",
    ]
    df = df[_SCHEMA_ORDER]

    return df
