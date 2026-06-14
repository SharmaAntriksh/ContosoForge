from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.utils.logging_utils import debug, info, skip, stage, warn
from src.utils.output_utils import write_parquet_with_date32
from src.versioning.version_store import should_regenerate, save_version
from src.utils.name_pools import (
    assign_person_names,
    hash_u64,
    load_people_pools,
    resolve_people_folder,
)
from src.utils.config_helpers import (
    as_dict,
    int_or,
    float_or,
    range2,
    region_from_iso_code,
)
from src.utils.config_precedence import resolve_seed
from src.defaults import (
    STORE_TYPES as _STORE_TYPES,
    STORE_STATUS as _STORE_STATUS,
    STORE_CLOSE_REASONS as _CLOSE_REASONS,
    STORE_TYPES_P as _STORE_TYPES_P,
    STORE_STATUS_P as _STORE_STATUS_P,
    STORE_BRANDS as _BRANDS,
    STORE_AREAS as _AREAS,
    STORE_MANAGER_FIRST as _MANAGER_FIRST,
    STORE_MANAGER_LAST as _MANAGER_LAST,
    STORE_ONLINE_SUFFIX as _ONLINE_SUFFIX,
    STORE_FORMATS as _STORE_FORMATS,
    STORE_DEFAULT_FORMATS as _DEFAULT_FORMATS,
    STORE_OWNERSHIP_TYPES as _OWNERSHIP_TYPES,
    STORE_DEFAULT_OWNERSHIP as _DEFAULT_OWNERSHIP,
    STORE_REVENUE_CLASSES as _REVENUE_CLASSES,
    STORE_REVENUE_CLASSES_P as _REVENUE_CLASSES_P,
    STORE_ISO_TO_ZONE as _ISO_TO_ZONE,
    STORE_BRAND_DOMAINS as _BRAND_DOMAINS,
    STORE_EU_COUNTRY_CODES as _EU_COUNTRY_CODES,
    ONLINE_STORE_KEY_BASE as _ONLINE_SK_BASE,
    STORE_STAFFING_RANGES as _STAFFING_RANGES,
    STORE_STAFFING_DEFAULT as _STAFFING_DEFAULT,
    MIN_PHYSICAL_EMPLOYEE_COUNT as _MIN_PHYSICAL_EMP,
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


# ---------------------------------------------------------
# Internals
# ---------------------------------------------------------

def _safe_read_geography(geo_path: Path) -> pd.DataFrame:
    """Read geography.parquet with best-effort column projection."""
    cols = [
        "GeographyKey",
        "City",
        "StateProvinceName",
        "CountryRegionName",
        "Country",
        "RegionCountryName",
        "ISOCode",
    ]
    try:
        return pd.read_parquet(geo_path, columns=cols)
    except (KeyError, ValueError):
        return pd.read_parquet(geo_path)


def _build_location_maps(geo: pd.DataFrame) -> tuple[dict[int, str], dict[int, str]]:
    """
    Returns ``(loc_short_map, loc_full_map)`` keyed by GeographyKey.
    Uses whatever columns exist; always falls back to ``'Geo <key>'``.
    """
    geo = geo.copy()
    if "GeographyKey" not in geo.columns:
        raise DimensionError("geography.parquet missing required column: GeographyKey")

    def col(name: str) -> pd.Series | None:
        return geo[name].astype(str) if name in geo.columns else None

    city = col("City")
    state = col("StateProvinceName")

    country = col("CountryRegionName")
    if country is None:
        country = col("Country")
    if country is None:
        country = col("RegionCountryName")

    gk = geo["GeographyKey"].astype("int64")

    if city is not None:
        loc_short = city
    elif state is not None:
        loc_short = state
    elif country is not None:
        loc_short = country
    else:
        loc_short = pd.Series([""] * len(geo))

    parts: list[pd.Series] = []
    if city is not None:
        parts.append(city)
    if state is not None:
        parts.append(state)
    if country is not None:
        parts.append(country)

    if parts:
        loc_full = parts[0]
        for p in parts[1:]:
            loc_full = loc_full + ", " + p
    else:
        loc_full = pd.Series([""] * len(geo))

    short_map = {
        int(k): (str(s).strip() if str(s).strip() else f"Geo {int(k)}")
        for k, s in zip(gk, loc_short)
    }
    full_map = {
        int(k): (str(s).strip() if str(s).strip() else f"Geo {int(k)}")
        for k, s in zip(gk, loc_full)
    }
    return short_map, full_map


def _require_cfg(cfg: Dict) -> Dict:
    stores_cfg = cfg.stores if hasattr(cfg, "stores") else None
    if not isinstance(stores_cfg, Mapping):
        raise DimensionError("config missing required block: stores")
    return stores_cfg


def _as_date64d(s: str | np.datetime64) -> np.datetime64:
    if isinstance(s, np.datetime64):
        return s.astype("datetime64[D]")
    return np.datetime64(pd.to_datetime(str(s)).date(), "D")


def _rand_dates_d(
    rng: np.random.Generator,
    start_d: np.datetime64,
    end_d: np.datetime64,
    n: int,
) -> np.ndarray:
    """Returns ``np.datetime64[D]`` array in ``[start_d, end_d]``."""
    start_i = start_d.astype("int64")
    end_i = end_d.astype("int64")
    if end_i < start_i:
        start_i, end_i = end_i, start_i
    days = rng.integers(start_i, end_i + 1, size=n, dtype=np.int64)
    return days.astype("datetime64[D]")


def _geography_signature(geo_keys: np.ndarray) -> str:
    arr = np.asarray(geo_keys, dtype=np.int64)
    if arr.size == 0:
        return "empty"
    return f"{int(arr.size)}:{int(arr.min())}:{int(arr.max())}:{int(arr[: min(5, arr.size)].sum())}"


def _square_footage_from_cfg(
    *,
    rng: np.random.Generator,
    store_type: pd.Series,
    sqft_cfg: Dict,
    n: int,
) -> np.ndarray:
    """
    Config shape (optional)::

      stores:
        square_footage:
          default: [5000, 60000]
          Supermarket: [15000, 80000]
          ...
    """
    default_lo, default_hi = range2(sqft_cfg.get("default"), 5000.0, 60000.0)

    # Use zeros so the missing-mask fallback (`out == 0`) is reliable
    out = np.zeros(n, dtype=np.int64)
    st = store_type.astype(str).to_numpy()

    for t in np.unique(st):
        lo, hi = range2(sqft_cfg.get(t), default_lo, default_hi)
        mask = st == t
        if mask.any():
            vals = rng.uniform(lo, hi, size=int(mask.sum()))
            out[mask] = np.maximum(1, np.round(vals)).astype(np.int64)

    missing = out == 0
    if missing.any():
        vals = rng.uniform(default_lo, default_hi, size=int(missing.sum()))
        out[missing] = np.maximum(1, np.round(vals)).astype(np.int64)

    return out


def _employee_count_by_store_type(
    *,
    rng: np.random.Generator,
    store_type: pd.Series,
    staffing_overrides: Optional[Dict] = None,
) -> np.ndarray:
    """Assign EmployeeCount based on store type staffing ranges.

    Uses ``STORE_STAFFING_RANGES`` from defaults, with optional per-type
    overrides from ``stores.staffing_ranges`` in config.yaml.
    Online stores are always 1 (one online sales representative).
    Closed stores keep their operational count so employees exist
    during the store's open period.
    """
    ranges = dict(_STAFFING_RANGES)
    # Online stores: always 1 (the online sales representative)
    ranges["Online"] = (1, 1)
    if staffing_overrides:
        for stype, spec in staffing_overrides.items():
            if stype == "Online":
                continue  # online staffing is fixed
            if isinstance(spec, (list, tuple)) and len(spec) == 2:
                ranges[stype] = (int(spec[0]), int(spec[1]))

    st = store_type.astype(str).to_numpy()
    n = len(st)
    out = np.zeros(n, dtype=np.int64)

    for stype in np.unique(st):
        mask = st == stype
        lo, hi = ranges.get(stype, _STAFFING_DEFAULT)
        out[mask] = rng.integers(lo, hi + 1, size=int(mask.sum()))

    # Physical stores must have >= MIN_PHYSICAL_EMPLOYEE_COUNT (manager + >= 1
    # salesperson) so every store keeps salesperson coverage and EmployeeCount
    # stays consistent with the employee roster derived from it (1 manager +
    # EmployeeCount-1 staff). Applied after sampling so it consumes no extra RNG
    # (determinism preserved); online stores keep their fixed count of 1.
    phys_mask = st != "Online"
    if phys_mask.any():
        out[phys_mask] = np.maximum(out[phys_mask], _MIN_PHYSICAL_EMP)

    return out


# ---------------------------------------------------------
# Geography sampling
# ---------------------------------------------------------

def _sample_geography_keys(
    *,
    rng: np.random.Generator,
    geo_keys: np.ndarray,
    n: int,
    iso_by_geo: Optional[dict[int, str]] = None,
    pop_by_geo: Optional[dict[int, int]] = None,
    ensure_iso_coverage: bool = False,
    region_weights: Optional[dict[str, float]] = None,
) -> np.ndarray:
    """Sample GeographyKey values for Stores.

    Supports three distribution modes (applied in priority order):

    1. **region_weights** — explicit fraction of stores per currency region.
       Within each region, cities are sampled by population weight.
    2. **ensure_iso_coverage** — seed at least one store per currency group,
       then fill remaining stores by population weight.
    3. **population only** — sample all stores by population weight
       (falls back to uniform if no population data).
    """
    keys = np.asarray(geo_keys, dtype=np.int64)
    if keys.size == 0:
        raise DimensionError("geo_keys empty")
    if n <= 0:
        raise DimensionError(f"n must be > 0, got {n}")

    # Build population weights (uniform fallback if no data)
    pop_arr = np.array(
        [max(1, pop_by_geo.get(int(k), 1)) for k in keys],
        dtype=np.float64,
    ) if pop_by_geo else np.ones(keys.size, dtype=np.float64)

    iso_arr = np.array(
        [iso_by_geo.get(int(k), "") for k in keys], dtype=object,
    ) if iso_by_geo else np.full(keys.size, "", dtype=object)

    # --- Mode 1: explicit region weights ---
    if region_weights and iso_by_geo:
        return _sample_by_region_weights(
            rng, keys, n, iso_arr, pop_arr, region_weights,
        )

    # --- Mode 2: ISO coverage + population weighting ---
    if ensure_iso_coverage and iso_by_geo:
        valid = iso_arr != ""
        if not np.any(valid):
            return _weighted_choice(rng, keys, pop_arr, n)

        uniq_iso = sorted(set(iso_arr[valid].tolist()))
        if not uniq_iso:
            return _weighted_choice(rng, keys, pop_arr, n)

        # Seed one store per currency group (population-weighted pick)
        if n < len(uniq_iso):
            chosen_iso = rng.choice(
                np.array(uniq_iso, dtype=object), size=n, replace=False,
            ).tolist()
        else:
            chosen_iso = uniq_iso

        first: list[int] = []
        for code in chosen_iso:
            mask = iso_arr == code
            pool = keys[mask]
            pool_w = pop_arr[mask]
            first.append(int(_weighted_choice(rng, pool, pool_w, 1)[0]))

        first_arr = np.asarray(first, dtype=np.int64)
        remaining = n - len(first_arr)
        if remaining > 0:
            rest = _weighted_choice(rng, keys, pop_arr, remaining)
            out = np.concatenate([first_arr, rest])
        else:
            out = first_arr
        rng.shuffle(out)
        return out

    # --- Mode 3: population weighting only ---
    return _weighted_choice(rng, keys, pop_arr, n)


def _weighted_choice(
    rng: np.random.Generator,
    keys: np.ndarray,
    weights: np.ndarray,
    n: int,
) -> np.ndarray:
    """Sample *n* keys with replacement, weighted by *weights*."""
    w = weights / weights.sum()
    return rng.choice(keys, size=n, replace=True, p=w)


def _sample_by_region_weights(
    rng: np.random.Generator,
    keys: np.ndarray,
    n: int,
    iso_arr: np.ndarray,
    pop_arr: np.ndarray,
    region_weights: dict[str, float],
) -> np.ndarray:
    """Allocate stores across regions by weight, then population-sample within."""
    # Normalize weights to sum to 1
    total_w = sum(region_weights.values())
    if total_w <= 0:
        return _weighted_choice(rng, keys, pop_arr, n)

    result: list[np.ndarray] = []
    allocated = 0

    # Sort regions for determinism; find last non-empty region for remainder
    sorted_regions = sorted(region_weights.items(), key=lambda x: -x[1])
    non_empty = [
        code for code, _ in sorted_regions
        if (iso_arr == code).any()
    ]
    last_non_empty = non_empty[-1] if non_empty else None

    for code, weight in sorted_regions:
        mask = iso_arr == code
        pool = keys[mask]
        if pool.size == 0:
            continue
        pool_w = pop_arr[mask]

        # Last non-empty region gets the remainder to avoid rounding gaps
        if code == last_non_empty:
            count = n - allocated
        else:
            count = max(1, int(round(n * weight / total_w)))
            count = min(count, n - allocated)

        if count <= 0:
            continue

        result.append(_weighted_choice(rng, pool, pool_w, count))
        allocated += count

    # Fill any unallocated (rounding or missing regions) with global pop-weighted
    if allocated < n:
        result.append(_weighted_choice(rng, keys, pop_arr, n - allocated))

    out = np.concatenate(result)
    rng.shuffle(out)
    return out


# ---------------------------------------------------------
# New column helpers
# ---------------------------------------------------------

def _iso_to_zone(iso: str) -> str:
    """Map an ISO/currency code to a geographic zone label."""
    return _ISO_TO_ZONE.get(str(iso).strip().upper(), "International")


def _build_phone(key: int, iso: str) -> str:
    """Return a country-formatted synthetic phone number for a single store."""
    iso = str(iso).strip().upper()
    a = (key * 7 + 131) % 900 + 100       # 3-digit part  (100-999)
    b = (key * 13 + 271) % 9000 + 1000    # 4-digit part (1000-9999)
    c = key % 10000                         # 4-digit suffix

    if iso in ("USD", "CAD", "MXN"):
        return f"+1 (555) {a:03d}-{c:04d}"
    if iso == "GBP":
        return f"+44 {a:03d} {b // 10:03d} {c:04d}"
    if iso == "EUR":
        cc = _EU_COUNTRY_CODES[key % len(_EU_COUNTRY_CODES)]
        return f"+{cc} {a // 10:02d} {b:04d} {c:04d}"
    if iso == "INR":
        return f"+91 {a * 10 + b % 10:05d} {c:05d}"
    if iso == "AUD":
        return f"+61 {key % 9 + 2} {a:04d} {c:04d}"
    if iso in ("CNY", "HKD"):
        return f"+86 {a:03d} {b:04d} {c:04d}"
    if iso == "JPY":
        return f"+81 {a % 90 + 10:02d} {b:04d} {c:04d}"
    if iso == "SGD":
        return f"+65 {b:04d} {c:04d}"
    # Generic international fallback for unrecognized currency codes
    debug(f"No phone format for ISO code {iso!r} (store key={key}); using generic international format")
    cc = 10 + key % 89
    return f"+{cc} {a:03d} {b // 10:03d} {c:04d}"


def _build_store_format(rng: np.random.Generator, store_type: pd.Series) -> np.ndarray:
    """Assign StoreFormat correlated with StoreType."""
    st = store_type.astype(str).to_numpy()
    out = np.full(len(st), "Standard", dtype=object)
    for t in np.unique(st):
        mask = st == t
        choices, probs = _STORE_FORMATS.get(t, _DEFAULT_FORMATS)
        out[mask] = rng.choice(np.array(choices, dtype=object), size=int(mask.sum()), p=probs)
    return out


def _build_ownership_type(rng: np.random.Generator, store_type: pd.Series) -> np.ndarray:
    """Assign OwnershipType correlated with StoreType."""
    st = store_type.astype(str).to_numpy()
    out = np.full(len(st), "Corporate", dtype=object)
    for t in np.unique(st):
        mask = st == t
        choices, probs = _OWNERSHIP_TYPES.get(t, _DEFAULT_OWNERSHIP)
        out[mask] = rng.choice(np.array(choices, dtype=object), size=int(mask.sum()), p=probs)
    return out


def _build_hierarchy(
    geo_keys: np.ndarray,
    iso_by_geo: Optional[dict[int, str]],
    country_by_geo: Optional[dict[int, str]] = None,
    district_size: int = 10,
    districts_per_region: int = 8,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Derive (StoreZone, StoreDistrict, StoreRegion) from GeographyKey + ISOCode.

    Districts are assigned per *country within zone* so that stores in
    different countries (e.g. China vs Australia) never share a district,
    even when they belong to the same zone ("Asia Pacific").  Region IDs
    roll up districts using ``districts_per_region``.
    """
    n = len(geo_keys)
    zones = np.empty(n, dtype=object)

    if iso_by_geo:
        _iso_arr = np.array([iso_by_geo.get(int(gk), "") for gk in geo_keys], dtype=object)
        _zone_map = np.vectorize(_iso_to_zone, otypes=[object])
        zones = _zone_map(_iso_arr)
    else:
        zones[:] = "International"

    # Country label per store (used for sub-grouping within a zone)
    countries = np.empty(n, dtype=object)
    if country_by_geo:
        countries = np.array([country_by_geo.get(int(gk), "Unknown") for gk in geo_keys], dtype=object)
    else:
        countries[:] = "Unknown"

    # Assign districts: iterate zone → country within zone → sequential IDs
    district_id = np.zeros(n, dtype=np.int32)
    next_did = 1
    for z in sorted(np.unique(zones)):
        z_mask = zones == z
        z_countries = countries[z_mask]
        z_idx = np.where(z_mask)[0]

        for c in sorted(np.unique(z_countries)):
            c_local = z_countries == c
            idx = z_idx[c_local]
            local_did = np.arange(len(idx)) // district_size
            district_id[idx] = (local_did + next_did).astype(np.int32)
            next_did += int(local_did.max()) + 1 if len(idx) > 0 else 1

    region_id = ((district_id - 1) // districts_per_region + 1).astype(np.int32)

    store_districts = np.char.add("District ", district_id.astype(str))
    store_regions   = np.char.add("Region ", region_id.astype(str))

    return zones, store_districts, store_regions


def _build_emails(
    brand_arr: np.ndarray,
    area_arr: np.ndarray,
    store_number_arr: np.ndarray,
    is_online: np.ndarray,
) -> np.ndarray:
    """Generate ``StoreEmail`` addresses keyed to brand domain."""
    n = len(brand_arr)
    # Vectorised domain lookup
    domains = np.array([_BRAND_DOMAINS.get(str(b), "retailstore.com") for b in brand_arr], dtype=object)
    # Vectorised store number slug
    sn_arr = np.array([str(s).lower().replace("-", ".") for s in store_number_arr], dtype=object)
    # Vectorised area slug
    area_slug = np.array([str(a).lower().replace(" ", ".").replace("-", ".") for a in area_arr], dtype=object)

    online_mask = np.asarray(is_online, dtype=bool)
    out = np.where(
        online_mask,
        np.char.add(np.char.add("online.", sn_arr.astype(str)), np.char.add("@", domains.astype(str))),
        np.char.add(np.char.add(area_slug.astype(str), np.char.add(".", sn_arr.astype(str))), np.char.add("@", domains.astype(str))),
    )
    return out


def _build_analytical(
    rng: np.random.Generator,
    store_type: pd.Series,
    revenue_class: np.ndarray,
    n: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Generate five analytical columns:
      AvgTransactionValue, CustomerSatisfactionScore,
      InventoryTurnoverTarget, LastAuditScore, ShrinkageRatePct

    All values are correlated with StoreType and/or RevenueClass.
    """
    st = store_type.astype(str).to_numpy()
    rc = np.asarray(revenue_class, dtype=object)

    # AvgTransactionValue — base ranges by type, then adjusted for revenue class
    atv_base = np.zeros(n, dtype=np.float64)
    for t in np.unique(st):
        mask = st == t
        cnt = int(mask.sum())
        if t == "Hypermarket":
            atv_base[mask] = rng.uniform(80.0, 250.0, cnt)
        elif t == "Supermarket":
            atv_base[mask] = rng.uniform(40.0, 120.0, cnt)
        elif t == "Convenience":
            atv_base[mask] = rng.uniform(10.0, 30.0, cnt)
        elif t == "Online":
            atv_base[mask] = rng.uniform(50.0, 200.0, cnt)
        else:
            warn(f"Unknown StoreType {t!r} in AvgTransactionValue; using Supermarket range.")
            atv_base[mask] = rng.uniform(40.0, 120.0, cnt)

    rc_mult = np.where(rc == "A", 1.30, np.where(rc == "C", 0.80, 1.00))
    avg_transaction = np.round(atv_base * rc_mult, 2)

    # CustomerSatisfactionScore — 1.0-10.0, positive skew via beta distribution
    raw_csat = rng.beta(a=6, b=2, size=n)
    csat = np.round(1.0 + raw_csat * 9.0, 1)

    # InventoryTurnoverTarget — turns/year, varies by store type
    inv_turn = np.zeros(n, dtype=np.float64)
    for t in np.unique(st):
        mask = st == t
        cnt = int(mask.sum())
        if t == "Hypermarket":
            inv_turn[mask] = rng.uniform(15.0, 25.0, cnt)
        elif t == "Supermarket":
            inv_turn[mask] = rng.uniform(18.0, 30.0, cnt)
        elif t == "Convenience":
            inv_turn[mask] = rng.uniform(30.0, 50.0, cnt)
        elif t == "Online":
            inv_turn[mask] = rng.uniform(20.0, 40.0, cnt)
        else:
            warn(f"Unknown StoreType {t!r} in InventoryTurnoverTarget; using Supermarket range.")
            inv_turn[mask] = rng.uniform(18.0, 30.0, cnt)
    inv_turn = np.round(inv_turn, 1)

    # LastAuditScore — integer 50-100
    audit = rng.integers(50, 101, size=n, dtype=np.int64)

    # ShrinkageRatePct — Convenience is higher risk; Online is lower
    shrink = np.zeros(n, dtype=np.float64)
    for t in np.unique(st):
        mask = st == t
        cnt = int(mask.sum())
        if t == "Convenience":
            shrink[mask] = rng.uniform(1.5, 4.0, cnt)
        elif t == "Online":
            shrink[mask] = rng.uniform(0.2, 1.0, cnt)
        else:  # Supermarket, Hypermarket, or any future type
            shrink[mask] = rng.uniform(0.5, 2.5, cnt)
    shrink = np.round(shrink, 2)

    return avg_transaction, csat, inv_turn, audit, shrink


# ---------------------------------------------------------
# Generator
# ---------------------------------------------------------

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
) -> pd.DataFrame:
    """
    Generate synthetic store dimension table.

    Output columns:
      StoreKey, StoreNumber, StoreName, StoreType, StoreFormat, OwnershipType,
      RevenueClass, Status, GeographyKey, StoreZone, StoreDistrict, StoreRegion,
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

    # --- Status assignment ---
    # When closing is enabled with a dataset window, we pick exactly
    # close_share fraction of non-Online stores to close during the window.
    # The rest are Open (with a small Renovating fraction).
    st_arr = df["StoreType"].astype(str).to_numpy()
    phys_idx = np.where(st_arr != "Online")[0]
    if closing_enabled and dataset_start and dataset_end:
        # Physical store closures only — online stores are always Open
        n_phys_eligible = len(phys_idx)
        n_phys_close = (
            max(1, int(round(n_phys_eligible * float(close_share))))
            if n_phys_eligible > 0 and close_share > 0.0
            else 0
        )
        # Always keep >= 1 physical store open for the whole window. A
        # close_share high enough to close every physical store leaves late
        # months with no open store; if there are also no online stores, Sales
        # falls back to unstaffed stores and emits EmployeeKey=-1 (orphan FK).
        # A count-aware clamp here is the only correct fix — a plain config
        # ceiling cannot protect small physical-store counts.
        if n_phys_eligible > 0 and n_phys_close >= n_phys_eligible:
            warn(
                f"close_share ({close_share}) would close all {n_phys_eligible} "
                "physical store(s); keeping 1 open to preserve salesperson "
                "coverage in Sales."
            )
            n_phys_close = n_phys_eligible - 1
        phys_close_idx = rng.choice(phys_idx, size=min(n_phys_close, n_phys_eligible), replace=False) if n_phys_close > 0 else np.array([], dtype=np.intp)

        # Of the remaining physical stores, ~5% are Renovating
        remaining_idx = np.setdiff1d(phys_idx, phys_close_idx)
        n_reno = max(0, int(round(len(remaining_idx) * 0.05)))
        reno_idx = rng.choice(remaining_idx, size=min(n_reno, len(remaining_idx)), replace=False) if n_reno > 0 else np.array([], dtype=np.intp)
        status_arr = np.full(num_stores, "Open", dtype=object)
        status_arr[phys_close_idx] = "Closed"
        if reno_idx.size > 0:
            status_arr[reno_idx] = "Renovating"
        df["Status"] = status_arr
    elif not closing_enabled:
        # Closing disabled: all Open, ~5% physical stores Renovating
        n_reno = max(0, int(round(len(phys_idx) * 0.05)))
        status_arr = np.full(num_stores, "Open", dtype=object)
        if n_reno > 0:
            reno_idx = rng.choice(phys_idx, size=min(n_reno, len(phys_idx)), replace=False)
            status_arr[reno_idx] = "Renovating"
        df["Status"] = status_arr
    else:
        # closing_enabled but no dataset window — probabilistic fallback (physical only)
        status_arr = np.full(num_stores, "Open", dtype=object)
        if phys_idx.size > 0:
            status_arr[phys_idx] = rng.choice(_STORE_STATUS, size=phys_idx.size, p=_STORE_STATUS_P)
        df["Status"] = status_arr

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

    # Hierarchy: StoreZone, StoreDistrict, StoreRegion
    zones, districts, regions = _build_hierarchy(
        geo_keys=df["GeographyKey"].to_numpy(dtype=np.int64),
        iso_by_geo=iso_by_geo,
        country_by_geo=country_by_geo,
        district_size=int_or(district_size, 10),
        districts_per_region=int_or(districts_per_region, 8),
    )
    df["StoreZone"]     = zones
    df["StoreDistrict"] = districts
    df["StoreRegion"]   = regions

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
        df["StoreManager"] = (
            pd.Series(first, dtype="object").astype(str)
            + " "
            + pd.Series(last, dtype="object").astype(str)
        )
    else:
        mf = _MANAGER_FIRST[(sk * 5 + int(seed)) % len(_MANAGER_FIRST)]
        ml = _MANAGER_LAST[(sk * 11 + int(seed) * 3) % len(_MANAGER_LAST)]
        df["StoreManager"] = (
            pd.Series(mf, dtype="object").astype(str)
            + " "
            + pd.Series(ml, dtype="object").astype(str)
        )

    # StoreName
    brand = _BRANDS[(sk + int(seed)) % len(_BRANDS)]
    area  = _AREAS[(sk * 7 + int(seed) * 13) % len(_AREAS)]
    stype = df["StoreType"].astype(str)
    is_online = stype.to_numpy() == "Online"

    online_suffix = _ONLINE_SUFFIX[(sk * 3 + int(seed) * 17) % len(_ONLINE_SUFFIX)]

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

    # Dates at DAY granularity
    open_start_d = _as_date64d(opening_start)
    open_end_d   = _as_date64d(opening_end)
    close_end_d  = _as_date64d(closing_end)

    if open_end_d < open_start_d:
        warn(
            f"opening_end ({opening_end}) < opening_start ({opening_start}); "
            "swapping to fix reversed date range."
        )
        open_start_d, open_end_d = open_end_d, open_start_d

    # Stores opening after dataset_start would have no employees or sales for
    # their pre-open period; shift the window (preserving width) to fit before it.
    if dataset_start:
        ds_start_d = _as_date64d(dataset_start)
        one_day = np.timedelta64(1, "D")
        target_end = ds_start_d - one_day
        if open_end_d > target_end:
            width = open_end_d - open_start_d
            new_start = target_end - width
            debug(
                f"opening window [{open_start_d}, {open_end_d}] extends into "
                f"dataset range (starts {dataset_start}); shifting to "
                f"[{new_start}, {target_end}]."
            )
            open_start_d = new_start
            open_end_d = target_end

    if close_end_d < open_end_d:
        warn(
            f"closing_end ({close_end_d}) < opening_end ({open_end_d}); "
            "closed stores may have unrealistic ClosingDates."
        )

    opening_d = _rand_dates_d(rng, open_start_d, open_end_d, num_stores)
    df["OpeningDate"] = pd.to_datetime(opening_d.astype("datetime64[ns]")).normalize()

    df["ClosingDate"] = pd.NaT
    status = df["Status"].astype(str)
    closed_mask = status.to_numpy() == "Closed"

    if closed_mask.any():
        if closing_enabled and dataset_start and dataset_end:
            # Constrain closing dates to [dataset_start, dataset_end]
            ds_start_d = _as_date64d(dataset_start)
            ds_end_d = _as_date64d(dataset_end)
            # Leave a buffer: close at least 30 days into the window,
            # and at least 60 days before the end (for wind-down/transfer)
            min_close_d = _as_date64d(
                (pd.to_datetime(dataset_start) + pd.Timedelta(days=30)).date()
            )
            max_close_d = _as_date64d(
                (pd.to_datetime(dataset_end) - pd.Timedelta(days=60)).date()
            )
            if max_close_d < min_close_d:
                max_close_d = ds_end_d

            n_closed = int(closed_mask.sum())
            close_d = _rand_dates_d(rng, min_close_d, max_close_d, n_closed)
            df.loc[closed_mask, "ClosingDate"] = pd.to_datetime(
                close_d.astype("datetime64[ns]")
            ).normalize()

            # Ensure opening dates for closed stores are before their closing date
            # (opening range may extend into the dataset window)
            closed_idx = np.where(closed_mask)[0]
            open_ts = pd.to_datetime(df.loc[closed_mask, "OpeningDate"])
            close_ts = pd.to_datetime(df.loc[closed_mask, "ClosingDate"])
            bad = open_ts.to_numpy() >= close_ts.to_numpy()
            if bad.any():
                bad_idx = closed_idx[bad]
                for bi in bad_idx:
                    cd = pd.to_datetime(df.at[bi, "ClosingDate"]).normalize()
                    # Place opening at least 180 days before closing
                    new_open = cd - pd.Timedelta(days=rng.integers(180, 730))
                    df.at[bi, "OpeningDate"] = new_open.normalize()
        else:
            open_days     = opening_d.astype("int64")[closed_mask]
            close_end_day = close_end_d.astype("int64")

            late_openers = int((open_days > close_end_day).sum())
            if late_openers:
                warn(
                    f"{late_openers} closed store(s) have OpeningDate after closing_end="
                    f"{closing_end}; their ClosingDate will be set to 30+ days after OpeningDate"
                )

            # Ensure at least 30 days of operation for closed stores
            min_close_offset = 30
            effective_end = np.maximum(open_days + min_close_offset, close_end_day)
            close_days = rng.integers(open_days + min_close_offset, effective_end + 1, dtype=np.int64)
            close_d = close_days.astype("datetime64[D]")
            df.loc[closed_mask, "ClosingDate"] = pd.to_datetime(
                close_d.astype("datetime64[ns]")
            ).normalize()

    # --- Renovation dates for Renovating stores ---
    df["RenovationStartDate"] = pd.NaT
    df["RenovationEndDate"] = pd.NaT
    reno_mask_dt = df["Status"].astype(str).to_numpy() == "Renovating"
    if reno_mask_dt.any() and dataset_start and dataset_end:
        ds_start_ts = pd.Timestamp(dataset_start)
        ds_end_ts = pd.Timestamp(dataset_end)
        n_reno = int(reno_mask_dt.sum())
        # Renovation starts at least 60 days into the dataset window AND at
        # least 30 days after the store opens (cannot renovate before opening),
        # and at least 120 days before dataset_end (room for 3-month min reno).
        ds_earliest_i = (ds_start_ts + pd.Timedelta(days=60)).to_datetime64().astype("datetime64[D]").astype("int64")
        ds_latest_i = (ds_end_ts - pd.Timedelta(days=120)).to_datetime64().astype("datetime64[D]").astype("int64")
        reno_open_ts = pd.to_datetime(df.loc[reno_mask_dt, "OpeningDate"])
        per_store_earliest_i = (
            (reno_open_ts + pd.Timedelta(days=30))
            .values.astype("datetime64[D]").astype("int64")
        )
        low_i = np.maximum(per_store_earliest_i, ds_earliest_i)
        high_i = np.full(n_reno, ds_latest_i, dtype=np.int64)
        # Drop stores whose available window collapses; they stay flagged
        # "Renovating" but with NaT dates (consistent with the original
        # behavior when reno_latest_start <= reno_earliest globally).
        valid = high_i >= low_i
        if valid.any():
            n_valid = int(valid.sum())
            day_offsets = rng.integers(0, (high_i[valid] - low_i[valid] + 1), dtype=np.int64)
            reno_start_days_valid = (low_i[valid] + day_offsets).astype("datetime64[D]")
            reno_dur_days = rng.integers(90, 271, size=n_valid)
            reno_end_days_raw = reno_start_days_valid + reno_dur_days.astype("timedelta64[D]")
            ds_end_d = np.datetime64(ds_end_ts.normalize().to_datetime64(), "D")
            reno_end_days_valid = np.minimum(reno_end_days_raw, ds_end_d)

            reno_start_ns = reno_start_days_valid.astype("datetime64[ns]")
            reno_end_ns = reno_end_days_valid.astype("datetime64[ns]")

            reno_idx_all = np.where(reno_mask_dt)[0]
            reno_idx_valid = reno_idx_all[valid]
            df.loc[reno_idx_valid, "RenovationStartDate"] = reno_start_ns
            df.loc[reno_idx_valid, "RenovationEndDate"] = reno_end_ns

            # Stores whose renovation ended before dataset_end reopen
            reopened_idx = reno_idx_valid[reno_end_days_valid < ds_end_d]
            if len(reopened_idx) > 0:
                status_arr2 = df["Status"].to_numpy().copy()
                status_arr2[reopened_idx] = "Open"
                df["Status"] = status_arr2

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

    # CloseReason (must exist before StoreDescription references it)
    df["CloseReason"] = ""
    if closed_mask.any():
        df.loc[closed_mask, "CloseReason"] = rng.choice(
            _CLOSE_REASONS, size=int(closed_mask.sum()),
        )

        # Stores closed for renovation: backfill RenovationStartDate/EndDate.
        # Renovation began 30-90 days before ClosingDate; the store never
        # reopened so RenovationEndDate = ClosingDate.
        reno_closed = closed_mask & (df["CloseReason"] == "Renovation")
        n_reno_closed = int(reno_closed.sum())
        if n_reno_closed > 0:
            close_ts = pd.to_datetime(df.loc[reno_closed, "ClosingDate"])
            open_ts = pd.to_datetime(df.loc[reno_closed, "OpeningDate"])
            lead_days = pd.to_timedelta(rng.integers(30, 91, size=n_reno_closed), unit="D")
            reno_start_candidate = (close_ts - lead_days).dt.normalize()
            # Floor at OpeningDate so renovation never starts before the store opens
            # (matters for stores that closed within ~90 days of opening).
            reno_start_floored = reno_start_candidate.where(
                reno_start_candidate >= open_ts, open_ts,
            )
            df.loc[reno_closed, "RenovationStartDate"] = reno_start_floored.values
            df.loc[reno_closed, "RenovationEndDate"] = close_ts.dt.normalize().values

    # StoreDescription
    opened = df["OpeningDate"].dt.strftime("%Y-%m-%d")
    closed = df["ClosingDate"].dt.strftime("%Y-%m-%d").fillna("")

    base = (
        df["StoreName"].astype(str)
        + " is a "
        + stype.str.lower()
        + " ("
        + status.str.lower()
        + ") location in "
        + loc_full.astype(str)
        + ". Opened "
        + opened
        + ". Size "
        + df["SquareFootage"].astype(str)
        + " sqft; headcount "
        + df["EmployeeCount"].astype(str)
        + ". Manager: "
        + df["StoreManager"].astype(str)
        + "."
    )

    online_mask = stype == "Online"
    if online_mask.any():
        base.loc[online_mask] = (
            df.loc[online_mask, "StoreName"].astype(str)
            + " operates as an online channel serving "
            + loc_full.loc[online_mask].astype(str)
            + ". Launched "
            + opened.loc[online_mask]
            + ". Estimated headcount "
            + df.loc[online_mask, "EmployeeCount"].astype(str)
            + "."
        )

    if closed_mask.any():
        base.loc[closed_mask] = (
            base.loc[closed_mask]
            + " Closed "
            + closed.loc[closed_mask]
            + " ("
            + df.loc[closed_mask, "CloseReason"].astype(str)
            + ")."
        )

    reno_mask = df["Status"].astype(str) == "Renovating"
    if reno_mask.any():
        reno_start_str = pd.to_datetime(df.loc[reno_mask, "RenovationStartDate"]).dt.strftime("%Y-%m-%d").fillna("")
        reno_end_str = pd.to_datetime(df.loc[reno_mask, "RenovationEndDate"]).dt.strftime("%Y-%m-%d").fillna("")
        base.loc[reno_mask] = (
            base.loc[reno_mask]
            + " Currently renovating ("
            + reno_start_str + " to " + reno_end_str
            + "); limited operations."
        )

    # Stores that completed renovation get an updated description
    completed_reno = (
        df["RenovationStartDate"].notna()
        & df["RenovationEndDate"].notna()
        & (df["Status"] == "Open")
    )
    if completed_reno.any():
        reno_start_str = pd.to_datetime(df.loc[completed_reno, "RenovationStartDate"]).dt.strftime("%Y-%m-%d")
        reno_end_str = pd.to_datetime(df.loc[completed_reno, "RenovationEndDate"]).dt.strftime("%Y-%m-%d")
        base.loc[completed_reno] = (
            base.loc[completed_reno]
            + " Renovated " + reno_start_str + " to " + reno_end_str
            + "; reopened."
        )

    df["StoreDescription"] = base

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
        "OpeningDate", "ClosingDate",
        "RenovationStartDate", "RenovationEndDate",
        "OpenFlag", "SquareFootage",
        "EmployeeCount", "StoreManager", "Phone", "StoreEmail",
        "StoreDescription", "CloseReason",
        "AvgTransactionValue", "CustomerSatisfactionScore",
        "InventoryTurnoverTarget", "LastAuditScore", "ShrinkageRatePct",
    ]
    df = df[_SCHEMA_ORDER]

    return df


# ---------------------------------------------------------
# Pipeline entrypoint
# ---------------------------------------------------------

def run_stores(cfg: Dict, parquet_folder: Path) -> None:
    """
    Pipeline wrapper for stores dimension generation.
    """
    store_cfg = _require_cfg(cfg)

    parquet_folder = Path(parquet_folder)
    parquet_folder.mkdir(parents=True, exist_ok=True)

    out_path = parquet_folder / "stores.parquet"
    geo_path = parquet_folder / "geography.parquet"

    if not geo_path.exists():
        raise FileNotFoundError(f"Missing geography parquet: {geo_path}")

    geo_df = _safe_read_geography(geo_path)
    geo_keys = geo_df["GeographyKey"].astype(np.int64).to_numpy()
    loc_short_map, loc_full_map = _build_location_maps(geo_df)

    def _geo_lookup(col: str, dtype=str) -> Optional[dict]:
        if col not in geo_df.columns:
            return None
        g = geo_df[["GeographyKey", col]].dropna()
        return dict(zip(
            g["GeographyKey"].astype(np.int64).to_numpy(),
            g[col].astype(dtype).to_numpy(),
        ))

    iso_by_geo = _geo_lookup("ISOCode")

    country_col = next(
        (c for c in ("Country", "CountryRegionName", "RegionCountryName") if c in geo_df.columns),
        None,
    )
    country_by_geo = _geo_lookup(country_col) if country_col else None

    pop_by_geo = _geo_lookup("Population", dtype=np.int64)

    # Region weights — validate keys against available currencies
    region_weights: Optional[dict[str, float]] = None
    if store_cfg.region_weights:
        region_weights = dict(store_cfg.region_weights)
        if iso_by_geo:
            available_iso = set(iso_by_geo.values())
            invalid = sorted(set(region_weights.keys()) - available_iso)
            if invalid:
                warn(
                    f"stores.region_weights references currencies not in geography: {invalid}. "
                    f"These will be ignored. Available: {sorted(available_iso)}"
                )
                for code in invalid:
                    del region_weights[code]
            if not region_weights:
                region_weights = None

    sqft_cfg = as_dict(store_cfg.square_footage)
    staffing_overrides = as_dict(store_cfg.staffing_ranges) if store_cfg.staffing_ranges else None

    version_cfg = dict(store_cfg)
    version_cfg["schema_version"] = 7  # v7: population-weighted geography sampling
    version_cfg["_geography_sig"] = _geography_signature(geo_keys)

    if not should_regenerate("stores", version_cfg, out_path):
        skip("Stores up-to-date")
        return

    compression       = store_cfg.parquet_compression
    compression_level = store_cfg.parquet_compression_level
    force_date32      = bool(store_cfg.force_date32)

    opening_cfg = as_dict(store_cfg.opening)

    use_name_pools = bool(store_cfg.use_name_pools)
    people_pools = None
    if use_name_pools:
        people_folder = resolve_people_folder()
        pf = Path(people_folder)
        enable_asia = (
            (pf / "asia_male_first.csv").exists()
            and (pf / "asia_female_first.csv").exists()
            and (pf / "asia_last.csv").exists()
        )
        people_pools = load_people_pools(
            people_folder, enable_asia=enable_asia, legacy_support=True,
        )

    # Resolve dataset window for store closing constraint
    global_dates = as_dict(cfg.defaults.dates) if hasattr(cfg.defaults, "dates") else {}
    ds_start = global_dates.get("start")
    ds_end = global_dates.get("end")

    closing_cfg = as_dict(store_cfg.closing) if hasattr(store_cfg, "closing") and store_cfg.closing is not None else {}
    closing_enabled = closing_cfg.get("enabled", True)
    close_share = float_or(closing_cfg.get("close_share"), 0.10)
    online_stores_count = int_or(store_cfg.online_stores, 0)

    geo_ctx = GeoContext(
        geo_keys=geo_keys,
        geo_loc_short=loc_short_map,
        geo_loc_full=loc_full_map,
        iso_by_geo=iso_by_geo,
        pop_by_geo=pop_by_geo,
        country_by_geo=country_by_geo,
        ensure_iso_coverage=bool(store_cfg.ensure_iso_coverage),
        region_weights=region_weights,
    )

    with stage("Generating Stores"):
        df = generate_store_table(
            geo=geo_ctx,
            num_stores=int_or(store_cfg.num_stores, 200),
            opening_start=opening_cfg.get("start") or "2018-01-01",
            opening_end=opening_cfg.get("end") or "2025-12-31",
            closing_end=store_cfg.closing_end or "2025-12-31",
            seed=resolve_seed(cfg, store_cfg, fallback=42),
            square_footage_cfg=sqft_cfg,
            staffing_overrides=staffing_overrides,
            people_pools=people_pools,
            district_size=store_cfg.district_size,
            districts_per_region=store_cfg.districts_per_region,
            dataset_start=ds_start,
            dataset_end=ds_end,
            close_share=close_share,
            closing_enabled=closing_enabled,
            online_stores=online_stores_count,
        )

        write_parquet_with_date32(
            df,
            out_path,
            date_cols=["OpeningDate", "ClosingDate", "RenovationStartDate", "RenovationEndDate"],
            cast_all_datetime=False,
            compression=str(compression),
            compression_level=(int(compression_level) if compression_level is not None else None),
            force_date32=force_date32,
        )

    save_version("stores", version_cfg, out_path)
    info(f"Stores dimension written: {out_path.name}")
