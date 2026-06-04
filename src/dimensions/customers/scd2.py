"""SCD Type 2 — Life Event Engine for customer dimension."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.utils import info
from src.utils.config_helpers import region_from_iso_code
from src.defaults import (
    SCD2_END_OF_TIME,
    CUSTOMER_INCOME_MIN as INCOME_MIN,
    CUSTOMER_INCOME_MAX as INCOME_MAX,
    CUSTOMER_MAX_CHILDREN as MAX_CHILDREN,
    CUSTOMER_INCOME_GROUP_EDGES as INCOME_GROUP_EDGES,
    CUSTOMER_INCOME_GROUP_LABELS as INCOME_GROUP_LABELS,
    CUSTOMER_REGION_LAT_LON_CENTER as _REGION_LAT_LON_CENTER,
    CUSTOMER_LAT_LON_JITTER as _LAT_LON_JITTER,
    CUSTOMER_POSTCODE_FMT as _POSTCODE_FMT,
    CUSTOMER_STREET_NAMES as _STREET_NAMES,
    CUSTOMER_STREET_TYPES as _STREET_TYPES,
)


def _income_to_group(income: float) -> str:
    """Map a yearly income value to its income group label."""
    idx = int(np.searchsorted(INCOME_GROUP_EDGES, income))
    return str(INCOME_GROUP_LABELS[min(idx, len(INCOME_GROUP_LABELS) - 1)])


def _available_events(state: dict, tier_keys: np.ndarray) -> list:
    """Return list of (event_name, weight) tuples based on current state."""
    events = []
    ms = state.get("MaritalStatus")
    ho = state.get("HomeOwnership")
    nc = int(state.get("NumberOfChildren") or 0)
    tier = int(state.get("LoyaltyTierKey") or 0)

    # Career growth — always available
    events.append(("career_growth", 0.30))

    # Marriage — if single or divorced
    if ms in ("Single", "Divorced"):
        events.append(("marriage", 0.20))

    # Family growth — if married and room for more children
    if ms == "Married" and nc < MAX_CHILDREN:
        events.append(("family_growth", 0.15))

    # Home purchase — renters and mortgage holders can transition to outright ownership
    if ho in ("Rent", "Mortgage"):
        events.append(("home_purchase", 0.10))

    # Relocation — always available
    events.append(("relocation", 0.10))

    # Divorce — if married
    if ms == "Married":
        events.append(("divorce", 0.05))

    # Tier upgrade — if not at max tier
    if len(tier_keys) > 0 and tier < int(tier_keys[-1]):
        events.append(("tier_upgrade", 0.15))

    return events


def _resolve_geo(gk, geo_lookup, _geo_cache: dict = None):
    """Return (country, city, state, region) for a GeographyKey.

    Prefers the pre-built dict cache, then a dict-shaped geo_lookup, then a
    DataFrame-indexed geo_lookup. Returns ("", "Unknown", "Unknown", None)
    on miss; region is None when the upstream cache predates ISOCode.
    """
    cache = _geo_cache if _geo_cache is not None else (
        geo_lookup if isinstance(geo_lookup, dict) else None
    )
    if cache is not None:
        entry = cache.get(gk)
        if entry is None:
            return ("", "Unknown", "Unknown", None)
        country, city, st = entry[0], entry[1], entry[2]
        region = entry[3] if len(entry) >= 4 else None
        return (country, city, st, region)
    # DataFrame fallback (legacy)
    if hasattr(geo_lookup, "index") and gk in geo_lookup.index:
        row = geo_lookup.loc[gk]
        return (str(row["Country"]), str(row["City"]), str(row["State"]), None)
    return ("", "Unknown", "Unknown", None)


def _relocate(rng: np.random.Generator, state: dict,
              geo_keys: np.ndarray, geo_lookup, _geo_cache: dict = None,
              region_pools: dict = None) -> None:
    """Change GeographyKey and regenerate address columns to match.

    HomeAddress always changes on relocation.  WorkAddress only changes
    when the customer moves to a different country (cross-country
    relocation implies a job change; within the same country, the old
    workplace is still reachable).

    geo_lookup can be a pd.DataFrame (legacy) or a dict (fast path).
    _geo_cache is an optional pre-built dict
        {GeographyKey: (Country, City, State, region_code)}.
    region_pools is an optional dict
        {region_code: (gk_array, weights_array)} — when present, the new
        GeographyKey is sampled from the customer's current region with
        population weights (matches initial-assignment behavior); else falls
        back to uniform sampling over all geo_keys.
    """
    old_gk = state.get("GeographyKey")
    old_country, _, _, old_region = _resolve_geo(old_gk, geo_lookup, _geo_cache)

    if region_pools and old_region in region_pools:
        pool_keys, pool_weights = region_pools[old_region]
        new_gk = int(rng.choice(pool_keys, p=pool_weights))
    else:
        new_gk = int(rng.choice(geo_keys))
    state["GeographyKey"] = new_gk

    new_country, city, st, new_region = _resolve_geo(new_gk, geo_lookup, _geo_cache)
    cross_country = old_country != new_country

    def _make_address(c, s):
        sn = str(rng.integers(1, 9999))
        sname = str(rng.choice(_STREET_NAMES))
        stype = str(rng.choice(_STREET_TYPES))
        ulabel = str(rng.choice(np.array(["Apt", "Suite", "Unit", "Fl", "#"])))
        unum = str(state.get("CustomerID", 0))
        return f"{sn} {sname} {stype}, {ulabel} {unum}, {c}, {s}"

    state["HomeAddress"] = _make_address(city, st)
    if cross_country:
        state["WorkAddress"] = _make_address(city, st)

    # Region code drives lat/lon center and postal code format. Sourced from
    # ISO code via the cache so it covers every country in the geography dim,
    # not just the eight in the legacy hardcoded map.
    rc = new_region or "US"

    # Regenerate lat/lon
    clat, clon = _REGION_LAT_LON_CENTER.get(rc, (39.8, -98.5))
    jlat, jlon = _LAT_LON_JITTER.get(rc, (5.0, 5.0))
    state["Latitude"] = round(float(clat + rng.uniform(-jlat, jlat)), 4)
    state["Longitude"] = round(float(clon + rng.uniform(-jlon, jlon)), 4)

    # Regenerate postal code
    fmt = _POSTCODE_FMT.get(rc, "5digit")
    if fmt == "5digit":
        state["PostalCode"] = f"{rng.integers(10001, 99999):05d}"
    elif fmt == "6digit":
        state["PostalCode"] = f"{rng.integers(100001, 999999):06d}"
    elif fmt == "uk":
        pfx = str(rng.choice(np.array(["SW", "EC", "W", "SE", "N", "NW", "E", "WC"])))
        state["PostalCode"] = f"{pfx}{rng.integers(1, 19)} {rng.integers(1, 9)}{chr(rng.integers(65, 91))}{chr(rng.integers(65, 91))}"
    elif fmt == "jp":
        v = int(rng.integers(1000000, 9999999))
        state["PostalCode"] = f"{v // 10000:03d}-{v % 10000:04d}"


def _apply_life_event(
    rng: np.random.Generator,
    state: dict,
    event: str,
    geo_keys: np.ndarray,
    tier_keys: np.ndarray,
    geo_lookup,
    *,
    _geo_cache: dict = None,
    region_pools: dict = None,
) -> None:
    """Apply a life event to the customer state dict (mutates in place)."""

    if event == "career_growth":
        bump = rng.uniform(0.12, 0.35)
        income = int(state.get("YearlyIncome") or 0)
        new_income = int(np.clip(income * (1 + bump), INCOME_MIN, INCOME_MAX))
        state["YearlyIncome"] = new_income
        state["IncomeGroup"] = _income_to_group(new_income)
        # 30% chance of tier upgrade alongside career growth
        if rng.random() < 0.30 and len(tier_keys) > 0:
            current_idx = int(np.searchsorted(tier_keys, state["LoyaltyTierKey"]))
            if current_idx < len(tier_keys) - 1:
                state["LoyaltyTierKey"] = int(tier_keys[current_idx + 1])

    elif event == "marriage":
        state["MaritalStatus"] = "Married"
        # 30% chance of relocation with marriage
        if rng.random() < 0.30:
            _relocate(rng, state, geo_keys, geo_lookup,
                      _geo_cache=_geo_cache, region_pools=region_pools)

    elif event == "family_growth":
        nc = int(state.get("NumberOfChildren") or 0)
        state["NumberOfChildren"] = nc + 1
        # 40% chance of buying home when having kids
        if state.get("HomeOwnership") in ("Rent", "Mortgage") and rng.random() < 0.40:
            state["HomeOwnership"] = "Own"
        # 20% chance of relocation
        if rng.random() < 0.20:
            _relocate(rng, state, geo_keys, geo_lookup,
                      _geo_cache=_geo_cache, region_pools=region_pools)

    elif event == "home_purchase":
        state["HomeOwnership"] = "Own"

    elif event == "relocation":
        _relocate(rng, state, geo_keys, geo_lookup,
                  _geo_cache=_geo_cache, region_pools=region_pools)

    elif event == "divorce":
        state["MaritalStatus"] = "Divorced"
        # 50% chance income decreases
        if rng.random() < 0.50:
            income = int(state.get("YearlyIncome") or 0)
            new_income = int(np.clip(income * rng.uniform(0.75, 0.90), INCOME_MIN, INCOME_MAX))
            state["YearlyIncome"] = new_income
            state["IncomeGroup"] = _income_to_group(new_income)
        # 60% chance of relocation
        if rng.random() < 0.60:
            _relocate(rng, state, geo_keys, geo_lookup,
                      _geo_cache=_geo_cache, region_pools=region_pools)
        # 35% chance of losing home
        if state.get("HomeOwnership") == "Own" and rng.random() < 0.35:
            state["HomeOwnership"] = "Rent"

    elif event == "tier_upgrade":
        if len(tier_keys) > 0:
            current_idx = int(np.searchsorted(tier_keys, state["LoyaltyTierKey"]))
            if current_idx < len(tier_keys) - 1:
                state["LoyaltyTierKey"] = int(tier_keys[current_idx + 1])


def _vectorize_iso_to_region(iso_arr: np.ndarray) -> np.ndarray:
    """Map an ISO-code array to region codes, memoizing the per-unique lookup.

    region_from_iso_code is a chained-if over sets — fast per call but called
    Python-side. Memoize across the unique values so a 7K-row geography only
    does ~30 lookups instead of 7K.
    """
    uniq = pd.unique(iso_arr)
    table = {c: region_from_iso_code(c) for c in uniq}
    return np.array([table[c] for c in iso_arr], dtype=object)


def _build_geo_cache(geo_lookup) -> dict:
    """Build {GeographyKey: (Country, City, State, region_code)} cache.

    region_code is derived from ISOCode via region_from_iso_code so the cache
    covers every country in the geography dim. Falls back to a 3-tuple when
    geo_lookup lacks ISOCode (legacy callers).
    """
    if isinstance(geo_lookup, dict):
        return geo_lookup
    if not isinstance(geo_lookup, pd.DataFrame) or geo_lookup.empty:
        return {}

    keys = geo_lookup.index.to_numpy()
    country = geo_lookup["Country"].astype(str).to_numpy()
    city = geo_lookup["City"].astype(str).to_numpy()
    state = geo_lookup["State"].astype(str).to_numpy()
    if "ISOCode" in geo_lookup.columns:
        region = _vectorize_iso_to_region(geo_lookup["ISOCode"].astype(str).to_numpy())
        return dict(zip(keys, zip(country, city, state, region)))
    return dict(zip(keys, zip(country, city, state)))


def _build_region_pools(geography: pd.DataFrame) -> dict:
    """Build {region_code: (gk_array, normalized_pop_weights)} for relocation.

    Used both for the initial customer→geography assignment and for SCD2
    relocations so the two paths can never drift. Returns an empty dict if
    geography lacks ISOCode (caller should fall back to uniform sampling).
    """
    pools: dict = {}
    if not isinstance(geography, pd.DataFrame) or geography.empty:
        return pools
    if "ISOCode" not in geography.columns or "GeographyKey" not in geography.columns:
        return pools

    gk_arr = geography["GeographyKey"].to_numpy()
    iso_arr = geography["ISOCode"].astype(str).to_numpy()
    if "Population" in geography.columns:
        pop_arr = geography["Population"].to_numpy(dtype=np.float64)
    else:
        pop_arr = np.ones(len(geography), dtype=np.float64)
    pop_arr = np.maximum(pop_arr, 1.0)

    region_arr = _vectorize_iso_to_region(iso_arr)
    for rc in np.unique(region_arr):
        mask = region_arr == rc
        keys = gk_arr[mask]
        weights = pop_arr[mask]
        weights = weights / weights.sum()
        pools[str(rc)] = (keys, weights)
    return pools


def _event_offsets(rng: np.random.Generator, n_events: int, max_offset: int) -> np.ndarray:
    """Strictly-increasing day offsets for a customer's SCD2 life events.

    Offsets are drawn in ``[90, max_offset)``, spaced >= 60 days apart, then
    de-duplicated. The spacing clamp (`min(prev+60, max_offset-1)`) can leave
    consecutive offsets *equal* when the draws cluster near ``max_offset``; equal
    offsets would chain two versions sharing an EffectiveStartDate, producing a
    version row with EffectiveEndDate < EffectiveStartDate (CUST-SCD2-1). Returning
    only strictly-increasing offsets (possibly fewer than ``n_events``) guarantees
    every emitted interval has end >= start.
    """
    offsets = np.sort(rng.integers(90, max_offset, size=n_events))
    for i in range(1, len(offsets)):
        if offsets[i] - offsets[i - 1] < 60:
            offsets[i] = min(offsets[i - 1] + 60, max_offset - 1)
    return np.unique(offsets)


def expand_changed_customers(
    rng: np.random.Generator,
    changed_df: pd.DataFrame,
    max_versions: int,
    geo_keys: np.ndarray,
    tier_keys: np.ndarray,
    end_date: pd.Timestamp,
    geo_lookup,
    region_pools: dict = None,
) -> pd.DataFrame:
    """Expand pre-selected changed customers with SCD2 version rows.

    Unlike generate_scd2_versions, this does NOT perform random selection —
    all rows in changed_df are expanded.  The caller is responsible for
    selecting which customers to change.

    Returns a DataFrame containing all version rows (including originals).
    """
    _geo_cache = _build_geo_cache(geo_lookup)

    # Pre-allocate with estimated capacity
    new_rows = []
    _est_rows = int(len(changed_df) * 2.5)
    if _est_rows > 0:
        new_rows = [None] * _est_rows
    _row_count = 0

    _col_names = changed_df.columns.tolist()
    _col_arrays = {col: changed_df[col].to_numpy() for col in _col_names}
    _n_changed = len(changed_df)

    _eff_start_raw = _col_arrays.get("EffectiveStartDate")
    if _eff_start_raw is not None:
        _eff_start_ts = pd.to_datetime(pd.Series(_eff_start_raw))
        _eff_start_days = _eff_start_ts.values.astype("datetime64[D]").astype("int64")
        _eff_start_np = _eff_start_ts.to_numpy()
    else:
        _eff_start_days = None
        _eff_start_np = None
    _end_date_days = np.datetime64(end_date, "D").astype("int64")

    for _ri in range(_n_changed):
        row_dict = {col: _col_arrays[col][_ri] for col in _col_names}

        n_events = int(rng.integers(1, max_versions))

        if _eff_start_days is not None:
            total_days = int(_end_date_days - _eff_start_days[_ri])
            cust_start = pd.Timestamp(_eff_start_np[_ri])
        else:
            cust_start = pd.Timestamp(row_dict["EffectiveStartDate"])
            total_days = (end_date - cust_start).days

        if total_days <= 90:
            if _row_count < len(new_rows):
                new_rows[_row_count] = row_dict
            else:
                new_rows.append(row_dict)
            _row_count += 1
            continue

        n_events = min(n_events, (total_days - 90) // 90)
        if n_events <= 0:
            if _row_count < len(new_rows):
                new_rows[_row_count] = row_dict
            else:
                new_rows.append(row_dict)
            _row_count += 1
            continue

        max_offset = max(91, total_days - 90)
        offsets = _event_offsets(rng, n_events, max_offset)
        _offset_td = offsets.astype("timedelta64[D]")
        event_dates = np.datetime64(cust_start, "D") + _offset_td

        current_state = dict(row_dict)
        _one_day = np.timedelta64(1, "D")

        for i in range(len(event_dates)):
            event_date_np = event_dates[i]
            event_date = pd.Timestamp(event_date_np)
            current_state["EffectiveEndDate"] = event_date_np - _one_day
            current_state["IsCurrent"] = False
            if _row_count < len(new_rows):
                new_rows[_row_count] = dict(current_state)
            else:
                new_rows.append(dict(current_state))
            _row_count += 1

            new_state = dict(current_state)
            new_state["VersionNumber"] = np.int32(i + 2)
            new_state["EffectiveStartDate"] = event_date
            new_state["EffectiveEndDate"] = SCD2_END_OF_TIME
            new_state["IsCurrent"] = True

            available = _available_events(new_state, tier_keys)
            if not available:
                break
            event_names, weights = zip(*available)
            weights_arr = np.asarray(weights, dtype="float64")
            weights_arr = weights_arr / weights_arr.sum()
            chosen = event_names[int(rng.choice(len(event_names), p=weights_arr))]
            _apply_life_event(rng, new_state, chosen, geo_keys, tier_keys,
                              geo_lookup, _geo_cache=_geo_cache,
                              region_pools=region_pools)

            current_state = new_state

        if _row_count < len(new_rows):
            new_rows[_row_count] = dict(current_state)
        else:
            new_rows.append(dict(current_state))
        _row_count += 1

    new_rows = new_rows[:_row_count]
    out = pd.DataFrame(new_rows)
    # Mixing pd.Timestamp (SCD2_END_OF_TIME on current rows) and np.datetime64
    # (event_date_np - 1 day on closed rows) leaves these columns as dtype=object,
    # which pyarrow refuses to write. Normalize to datetime64[ns].
    for _dt_col in ("EffectiveStartDate", "EffectiveEndDate"):
        if _dt_col in out.columns:
            out[_dt_col] = pd.to_datetime(out[_dt_col])
    return out


def generate_scd2_versions(
    rng: np.random.Generator,
    base_df: pd.DataFrame,
    cust_cfg,
    geo_keys: np.ndarray,
    tier_keys: np.ndarray,
    end_date: pd.Timestamp,
    geo_lookup: pd.DataFrame,
    region_pools: dict = None,
) -> pd.DataFrame:
    """
    Expand customer rows with SCD Type 2 version history.

    Selects a fraction of individual customers (change_rate), then delegates
    to expand_changed_customers() for the per-row life event simulation.
    """
    max_versions = int(getattr(cust_cfg, "max_versions", 4))

    # max_versions <= 1 means "no extra versions": skip expansion entirely (mirrors
    # products/scd2.py). base_df already carries EffectiveStartDate/EffectiveEndDate/
    # IsCurrent as a single current version, so it is returned as-is. Also avoids
    # rng.integers(1, max_versions) raising ValueError when max_versions == 1.
    if max_versions <= 1:
        return base_df

    change_rate = float(getattr(cust_cfg, "change_rate", 0.15))
    person_mask = base_df["CustomerType"] == "Individual"
    person_ids = base_df.loc[person_mask, "CustomerID"].to_numpy()

    if len(person_ids) == 0:
        return base_df

    n_change = max(1, int(len(person_ids) * change_rate))
    n_change = min(n_change, len(person_ids))
    change_id_set = set(
        rng.choice(person_ids, size=n_change, replace=False).tolist()
    )

    _change_mask = base_df["CustomerID"].isin(change_id_set)
    unchanged_df = base_df[~_change_mask]
    changed_df = base_df[_change_mask]

    expanded_rows_df = expand_changed_customers(
        rng=rng,
        changed_df=changed_df,
        max_versions=max_versions,
        geo_keys=geo_keys,
        tier_keys=tier_keys,
        end_date=end_date,
        geo_lookup=geo_lookup,
        region_pools=region_pools,
    )

    expanded_df = pd.concat(
        [unchanged_df, expanded_rows_df],
        ignore_index=True,
    )

    expanded_df["CustomerKey"] = np.arange(1, len(expanded_df) + 1, dtype="int64")

    n_total = len(expanded_df)
    n_base = len(base_df)
    n_versions = n_total - n_base
    info(f"SCD2: {n_change} customers expanded, {n_versions} version rows added ({n_total} total)")

    return expanded_df
