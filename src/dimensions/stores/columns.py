"""Pure per-column builders for the store dimension (no I/O, no config)."""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from src.utils.logging_utils import debug
from src.utils.config_helpers import range2
from src.defaults import (
    STORE_STAFFING_RANGES as _STAFFING_RANGES,
    STORE_STAFFING_DEFAULT as _STAFFING_DEFAULT,
    STORE_SQUARE_FOOTAGE_RANGES as _SQUARE_FOOTAGE_RANGES,
    STORE_SQUARE_FOOTAGE_DEFAULT as _SQUARE_FOOTAGE_DEFAULT,
    STORE_STAFFING_PER_1000_SQFT as _STAFFING_DENSITY,
    STORE_STAFFING_DENSITY_DEFAULT as _STAFFING_DENSITY_DEFAULT,
    STORE_FORMAT_STAFFING_MULTIPLIER as _FORMAT_STAFFING_MULTIPLIER,
    STORE_OWNERSHIP_STAFFING_MULTIPLIER as _OWNERSHIP_STAFFING_MULTIPLIER,
    MIN_PHYSICAL_EMPLOYEE_COUNT as _MIN_PHYSICAL_EMP,
    STORE_ISO_TO_ZONE as _ISO_TO_ZONE,
    STORE_EU_COUNTRY_CODES as _EU_COUNTRY_CODES,
    STORE_FORMATS as _STORE_FORMATS,
    STORE_DEFAULT_FORMATS as _DEFAULT_FORMATS,
    STORE_OWNERSHIP_TYPES as _OWNERSHIP_TYPES,
    STORE_DEFAULT_OWNERSHIP as _DEFAULT_OWNERSHIP,
    STORE_BRAND_DOMAINS as _BRAND_DOMAINS,
)


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
    explicit_default = sqft_cfg.get("default")
    default_lo, default_hi = range2(
        explicit_default,
        float(_SQUARE_FOOTAGE_DEFAULT[0]),
        float(_SQUARE_FOOTAGE_DEFAULT[1]),
    )

    # Use zeros so the missing-mask fallback (`out == 0`) is reliable
    out = np.zeros(n, dtype=np.int64)
    st = store_type.astype(str).to_numpy()

    for t in np.unique(st):
        if t in sqft_cfg:
            lo, hi = range2(sqft_cfg.get(t), default_lo, default_hi)
        elif explicit_default is not None:
            lo, hi = default_lo, default_hi
        else:
            built_in = _SQUARE_FOOTAGE_RANGES.get(t, _SQUARE_FOOTAGE_DEFAULT)
            lo, hi = float(built_in[0]), float(built_in[1])
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
    square_footage: Optional[pd.Series] = None,
    store_format: Optional[pd.Series] = None,
    ownership_type: Optional[pd.Series] = None,
    staffing_overrides: Optional[Dict] = None,
) -> np.ndarray:
    """Assign planned EmployeeCount from footprint and operating model.

    Uses ``STORE_STAFFING_RANGES`` from defaults, with optional per-type
    overrides from ``stores.staffing_ranges`` in config.yaml. Within those
    guardrails, square footage is the primary driver; StoreFormat and
    OwnershipType apply modest, documented operating-model adjustments.
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

    sqft = (
        pd.to_numeric(square_footage, errors="coerce").fillna(0).to_numpy(dtype=np.float64)
        if square_footage is not None
        else None
    )
    formats = (
        store_format.astype(str).to_numpy()
        if store_format is not None
        else np.full(n, "Standard", dtype=object)
    )
    ownership = (
        ownership_type.astype(str).to_numpy()
        if ownership_type is not None
        else np.full(n, "Franchise", dtype=object)
    )

    for stype in np.unique(st):
        mask = st == stype
        lo, hi = ranges.get(stype, _STAFFING_DEFAULT)
        count = int(mask.sum())
        if stype == "Online":
            out[mask] = 1
            continue
        if sqft is None:
            out[mask] = rng.integers(lo, hi + 1, size=count)
            continue

        density = float(_STAFFING_DENSITY.get(stype, _STAFFING_DENSITY_DEFAULT))
        format_mult = np.array(
            [_FORMAT_STAFFING_MULTIPLIER.get(str(value), 1.0) for value in formats[mask]],
            dtype=np.float64,
        )
        ownership_mult = np.array(
            [_OWNERSHIP_STAFFING_MULTIPLIER.get(str(value), 1.0) for value in ownership[mask]],
            dtype=np.float64,
        )
        operating_noise = np.clip(rng.normal(1.0, 0.04, size=count), 0.90, 1.10)
        target = (
            sqft[mask] / 1_000.0
            * density
            * format_mult
            * ownership_mult
            * operating_noise
        )
        out[mask] = np.clip(np.rint(target), lo, hi).astype(np.int64)

    # Physical stores must have >= MIN_PHYSICAL_EMPLOYEE_COUNT (manager + >= 1
    # salesperson) so every store keeps salesperson coverage and EmployeeCount
    # stays consistent with the employee roster derived from it (1 manager +
    # EmployeeCount-1 staff). Applied after sampling so it consumes no extra RNG
    # (determinism preserved); online stores keep their fixed count of 1.
    phys_mask = st != "Online"
    if phys_mask.any():
        out[phys_mask] = np.maximum(out[phys_mask], _MIN_PHYSICAL_EMP)

    return out


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
    store_keys: Optional[np.ndarray] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Derive (StoreZone, StoreDistrict, StoreRegion, district_id, region_id)
    from GeographyKey + ISOCode.

    Districts and regions are assigned per *country within zone* so stores in
    different countries (e.g. China vs Australia) never share a district,
    or a region, even when they belong to the same zone ("Asia Pacific").
    Within each country, stores are ordered by ``(GeographyKey, StoreKey)`` so
    input row order cannot silently redefine the management hierarchy. Region
    and district IDs remain globally unique integer surrogate identities.

    The ``district_id`` / ``region_id`` int32 arrays are the structured truth;
    the ``StoreDistrict`` / ``StoreRegion`` strings are display renderings of
    them. Both are returned so consumers can read the ints directly instead of
    reverse-parsing the strings.
    """
    n = len(geo_keys)
    geo_keys = np.asarray(geo_keys, dtype=np.int64)
    if store_keys is None:
        store_keys = np.arange(1, n + 1, dtype=np.int64)
    else:
        store_keys = np.asarray(store_keys, dtype=np.int64)
        if len(store_keys) != n:
            raise ValueError("store_keys must align with geo_keys")
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
    region_id = np.zeros(n, dtype=np.int32)
    next_did = 1
    next_rid = 1
    for z in sorted(np.unique(zones)):
        z_mask = zones == z
        z_countries = countries[z_mask]
        z_idx = np.where(z_mask)[0]

        for c in sorted(np.unique(z_countries)):
            c_local = z_countries == c
            idx = z_idx[c_local]
            idx = idx[np.lexsort((store_keys[idx], geo_keys[idx]))]
            local_did = np.arange(len(idx)) // district_size
            district_id[idx] = (local_did + next_did).astype(np.int32)
            n_districts = int(local_did.max()) + 1 if len(idx) > 0 else 0
            local_rid_by_district = (
                np.arange(n_districts, dtype=np.int32) // districts_per_region
            )
            region_id[idx] = (
                local_rid_by_district[local_did] + next_rid
            ).astype(np.int32)
            next_did += n_districts
            next_rid += (
                int(local_rid_by_district.max()) + 1
                if n_districts > 0
                else 0
            )

    store_districts = np.char.add("District ", district_id.astype(str))
    store_regions   = np.char.add("Region ", region_id.astype(str))

    return zones, store_districts, store_regions, district_id, region_id


def _build_emails(
    brand_arr: np.ndarray,
    area_arr: np.ndarray,
    store_number_arr: np.ndarray,
    is_online: np.ndarray,
) -> np.ndarray:
    """Generate ``StoreEmail`` addresses keyed to brand domain."""
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


