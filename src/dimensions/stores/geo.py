"""Geography I/O and store-location sampling for the store dimension."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.versioning import frame_content_hash


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


def _geography_signature(geo: pd.DataFrame) -> str:
    """Hash the geography values that shape store output and hierarchy.

    The former count/min/max signature neither tracked country/ISO changes nor
    resisted collisions. This content digest is row-order independent and makes
    a country-boundary edit invalidate Stores and its dependent dimensions.
    """
    return frame_content_hash(
        geo,
        [
            "GeographyKey", "City", "StateProvinceName",
            "CountryRegionName", "Country", "RegionCountryName",
            "ISOCode", "Population",
        ],
    )


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
