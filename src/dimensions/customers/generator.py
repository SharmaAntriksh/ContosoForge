"""Main customer dimension generator — orchestrates helpers, org profile,
households, and SCD2 sub-modules to produce Customers, CustomerProfile,
and OrganizationProfile tables.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

from src.exceptions import DimensionError, ValidationError
from src.utils import info, skip, stage
from src.utils.output_utils import write_parquet_with_date32
from src.utils.config_precedence import resolve_seed
from src.defaults import SCD2_END_OF_TIME
from src.versioning import should_regenerate, save_version
from src.engine.dimension_loader import load_dimension
from src.utils.config_helpers import region_from_iso_code
from src.utils.name_pools import (
    resolve_people_folder,
    load_people_pools,
    assign_person_names,
    resolve_org_names_file,
    load_org_names,
    assign_org_names,
    slugify_domain_label,
)

from src.defaults import (
    CUSTOMER_PERSONAL_EMAIL_DOMAINS as PERSONAL_EMAIL_DOMAINS,
    CUSTOMER_MARITAL_STATUS_LABELS as MARITAL_STATUS_LABELS,
    CUSTOMER_EDUCATION_LABELS as EDUCATION_LABELS,
    CUSTOMER_EDUCATION_PROBS as EDUCATION_PROBS,
    CUSTOMER_OCCUPATION_LABELS as OCCUPATION_LABELS,
    CUSTOMER_OCCUPATION_PROBS as OCCUPATION_PROBS,
    CUSTOMER_AGE_MIN_DAYS as AGE_MIN_DAYS,
    CUSTOMER_AGE_MAX_DAYS as AGE_MAX_DAYS,
    CUSTOMER_INCOME_MIN as INCOME_MIN,
    CUSTOMER_INCOME_MAX as INCOME_MAX,
    CUSTOMER_MAX_CHILDREN as MAX_CHILDREN,
    CUSTOMER_LOYALTY_W_WEIGHT as LOYALTY_W_WEIGHT,
    CUSTOMER_LOYALTY_W_TEMP as LOYALTY_W_TEMP,
    CUSTOMER_LOYALTY_W_INCOME as LOYALTY_W_INCOME,
    CUSTOMER_AGE_GROUP_EDGES as AGE_GROUP_EDGES,
    CUSTOMER_AGE_GROUP_LABELS as AGE_GROUP_LABELS,
    CUSTOMER_INCOME_GROUP_EDGES as INCOME_GROUP_EDGES,
    CUSTOMER_INCOME_GROUP_LABELS as INCOME_GROUP_LABELS,
    CUSTOMER_HOME_OWNERSHIP_LABELS as HOME_OWNERSHIP_LABELS,
    CUSTOMER_HOME_OWNERSHIP_PROBS_BY_INCOME as HOME_OWNERSHIP_PROBS_BY_INCOME,
    CUSTOMER_CONTACT_METHOD_LABELS as CONTACT_METHOD_LABELS,
    CUSTOMER_CONTACT_METHOD_PROBS as CONTACT_METHOD_PROBS,
    CUSTOMER_MARITAL_PROBS_BY_AGE as MARITAL_PROBS_BY_AGE,
    CUSTOMER_EDUCATION_PROBS_BY_AGE as EDUCATION_PROBS_BY_AGE,
    CUSTOMER_OCCUPATION_PROBS_BY_EDUCATION as OCCUPATION_PROBS_BY_EDUCATION,
    CUSTOMER_CHILDREN_LAMBDA_BY_MARITAL_AGE as CHILDREN_LAMBDA_BY_MARITAL_AGE,
    CUSTOMER_HOME_OWNERSHIP_AGE_SHIFT as HOME_OWNERSHIP_AGE_SHIFT,
    CUSTOMER_CAR_LAMBDA_BY_AGE as CAR_LAMBDA_BY_AGE,
    CUSTOMER_ORG_EMAIL_PREFIXES as ORG_EMAIL_PREFIXES,
    CUSTOMER_REGION_TIMEZONE as _REGION_TIMEZONE,
    CUSTOMER_URBAN_RURAL_LABELS as _URBAN_RURAL_LABELS,
    CUSTOMER_URBAN_RURAL_PROBS as _URBAN_RURAL_PROBS,
    CUSTOMER_LANGUAGE_BY_REGION as _LANGUAGE_BY_REGION,
    CUSTOMER_HOUSEHOLD_PCT as _HOUSEHOLD_PCT_DEFAULT,
)

from src.dimensions.customers.helpers import (
    parse_cfg_dates,
    month_index_space,
    month_idx_to_date,
    acquisition_weights,
    simulate_end_month,
    validate_percentages,
    read_parquet_dim,
    first_existing_col,
    normalize_probs,
    default_tier_probs,
    assign_tier_by_score,
    acquisition_weights_from_names,
    generate_correlated_income,
    generate_phone_numbers,
    generate_credit_scores,
    generate_addresses,
    generate_lat_lon,
    generate_postal_codes,
)
from src.dimensions.customers.org_profile import generate_org_profile
from src.dimensions.customers.households import assign_households, head_indices_for_members
from src.dimensions.customers.scd2 import generate_scd2_versions

_PAYMENT_METHOD_LABELS = np.array([
    "Credit Card", "Debit Card", "Cash", "Digital Wallet", "Bank Transfer",
])
_PAYMENT_METHOD_PROBS = np.array([0.35, 0.25, 0.10, 0.20, 0.10])

_DEVICE_PREFS = np.array(["Mobile", "Desktop", "Tablet"])
_NEWSLETTER_FREQ = np.array(["Weekly", "Monthly", "None"])

_DISTANCE_BY_AREA = {"Urban": (0.5, 5.0), "Suburban": (3.0, 15.0), "Rural": (10.0, 50.0)}

# Import-time validation for local probability arrays
for _pname, _parr in [
    ("_PAYMENT_METHOD_PROBS", _PAYMENT_METHOD_PROBS),
]:
    if abs(float(_parr.sum()) - 1.0) > 1e-6:
        raise ValidationError(f"generator.{_pname} sums to {float(_parr.sum())}, expected 1.0")
del _pname, _parr


def _labels_to_codes(values: np.ndarray, labels: Sequence) -> np.ndarray:
    """Map a string array to integer codes given an ordered label list."""
    codes = np.zeros(len(values), dtype=np.intp)
    for i, lbl in enumerate(labels):
        codes[values == lbl] = i
    return codes


def _vectorized_cdf_sample(cdfs: np.ndarray, brackets: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Vectorized CDF sampling by bracket — replaces per-element searchsorted loops.

    Args:
        cdfs: shape (n_brackets, n_labels) — CDF per bracket (must be clamped to 1.0)
        brackets: shape (n,) int — bracket index per element
        u: shape (n,) float — uniform random per element

    Returns:
        shape (n,) int — sampled label index per element
    """
    n = len(brackets)
    out = np.empty(n, dtype=np.intp)
    for b in range(cdfs.shape[0]):
        mask = brackets == b
        if mask.any():
            out[mask] = np.searchsorted(cdfs[b], u[mask])
    return out


# ---------------------------------------------------------
# Main generator
# ---------------------------------------------------------
def generate_synthetic_customers(cfg: Dict, parquet_dims_folder: Path,
                                  *, _skip_post_phases: bool = False):
    cust_cfg = cfg.customers
    total_customers = int(cust_cfg.total_customers)
    if total_customers <= 0:
        raise DimensionError("customers.total_customers must be > 0")

    seed = resolve_seed(cfg, cust_cfg, fallback=42)
    rng = np.random.default_rng(seed)

    start_date, end_date = parse_cfg_dates(cfg)
    start_month0, _end_month0, T = month_index_space(start_date, end_date)

    active_ratio = getattr(cust_cfg, "active_ratio", 1.0)
    if not isinstance(active_ratio, (int, float)) or not (0 < float(active_ratio) <= 1):
        raise DimensionError("customers.active_ratio must be a number in the range (0, 1]")

    pct_india = float(cust_cfg.pct_india)
    pct_us = float(cust_cfg.pct_us)
    pct_eu = float(cust_cfg.pct_eu)
    pct_asia = float(getattr(cust_cfg, "pct_asia", 0.0))  # optional; defaults to 0
    pct_org = float(cust_cfg.pct_org)

    if not np.isfinite(pct_org) or pct_org < 0 or pct_org > 100:
        raise DimensionError("customers.pct_org must be a finite number in [0, 100]")

    p_in, p_us, p_eu, p_as = validate_percentages(pct_india, pct_us, pct_eu, pct_asia)

    # --- shared name pools ---
    names_folder = resolve_people_folder()
    enable_asia = p_as > 0.0
    people_pools = load_people_pools(names_folder, enable_asia=enable_asia, legacy_support=True)

    geography, _ = load_dimension("geography", parquet_dims_folder, cfg.geography)
    geo_keys = geography["GeographyKey"].to_numpy()

    geo_lookup = geography.set_index("GeographyKey")[["City", "State", "Country"]]

    # Build per-region geography pools (keys + population weights)
    # so customers are assigned to cities in their own region,
    # weighted by population for realistic distribution.
    _geo_region_pools: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    if "ISOCode" in geography.columns:
        geo_iso = geography["ISOCode"].astype(str).to_numpy()
        geo_pop = (
            geography["Population"].to_numpy(dtype=np.float64)
            if "Population" in geography.columns
            else np.ones(len(geography), dtype=np.float64)
        )
        geo_pop = np.maximum(geo_pop, 1.0)  # avoid zero weights

        geo_region = np.array(
            [region_from_iso_code(iso) for iso in geo_iso], dtype=object,
        )
        for region_code in np.unique(geo_region):
            mask = geo_region == region_code
            pool_keys = geo_keys[mask]
            pool_weights = geo_pop[mask]
            pool_weights = pool_weights / pool_weights.sum()
            _geo_region_pools[region_code] = (pool_keys, pool_weights)

    N = int(total_customers)
    if N <= 0:
        raise DimensionError("Customer count must be positive")
    CustomerKey = np.arange(1, N + 1, dtype="int64")

    active_count = int(np.floor(N * float(active_ratio)))
    if active_count <= 0:
        raise DimensionError(
            "customers.active_ratio results in zero active customers; "
            "increase active_ratio or total_customers"
        )

    if active_count < N:
        active_customer_keys = rng.choice(CustomerKey, size=active_count, replace=False)
    else:
        active_customer_keys = CustomerKey

    is_active = np.zeros(N, dtype="int64")
    is_active[(active_customer_keys - 1).astype("int64")] = 1

    region_labels = ["IN", "US", "EU"] + (["AS"] if enable_asia else [])
    region_probs = [p_in, p_us, p_eu] + ([p_as] if enable_asia else [])
    Region = rng.choice(region_labels, size=N, p=region_probs)

    IsOrg = rng.random(N) < (pct_org / 100.0)

    Gender = np.empty(N, dtype=object)
    Gender[~IsOrg] = rng.choice(["Male", "Female"], size=(~IsOrg).sum())
    Gender[IsOrg] = "Org"

    # Assign GeographyKey per customer: sample from their region's cities,
    # weighted by population. Falls back to uniform if no region pools.
    GeographyKey = np.empty(N, dtype=np.int32)
    if _geo_region_pools:
        for region_code in np.unique(Region):
            mask = Region == region_code
            n_region = int(mask.sum())
            if region_code in _geo_region_pools:
                pool_keys, pool_weights = _geo_region_pools[region_code]
                GeographyKey[mask] = rng.choice(
                    pool_keys, size=n_region, replace=True, p=pool_weights,
                )
            else:
                # Region has no matching cities — use all cities uniformly
                GeographyKey[mask] = rng.choice(geo_keys, size=n_region, replace=True)
    else:
        GeographyKey = rng.choice(geo_keys, size=N, replace=True)

    # --- names via name_pools (no output schema change) ---
    FirstName, LastName, _ = assign_person_names(
        keys=CustomerKey,
        region=Region,
        gender=Gender,
        is_org=IsOrg,
        pools=people_pools,
        seed=int(seed),
        include_middle=False,
        default_region="US",
    )
    safe_first = np.where(pd.isna(FirstName), "", FirstName.astype(object))
    safe_last = np.where(pd.isna(LastName), "", LastName.astype(object))

    # -----------------------------------------------------
    # Organization handling (meaningful org names from pool)
    # -----------------------------------------------------
    org_file = resolve_org_names_file()
    org_pool = load_org_names(org_file)
    OrgName = assign_org_names(
        keys=CustomerKey,
        is_org=IsOrg,
        org_pool=org_pool,
        seed=int(seed),
    )

    # -----------------------------------------------------
    # Email (with deduplication via CustomerKey suffix)
    # -----------------------------------------------------
    Email = np.empty(N, dtype=object)

    personal_mask = ~IsOrg
    n_personal = int(personal_mask.sum())
    if n_personal:
        domain = rng.choice(PERSONAL_EMAIL_DOMAINS, size=n_personal, replace=True)
        user = (
            safe_first[personal_mask].astype(str)
            + "."
            + safe_last[personal_mask].astype(str)
        ).astype(str)
        user = np.char.lower(np.char.replace(user, " ", ""))
        suffix = CustomerKey[personal_mask].astype(str)
        Email[personal_mask] = user + suffix + "@" + domain

    n_org = int(IsOrg.sum())
    if n_org:
        org_slugs = np.array(
            [slugify_domain_label(x) for x in OrgName[IsOrg].astype(str)],
            dtype=object,
        )
        org_key_suffix = CustomerKey[IsOrg].astype(str)
        OrgDomain = org_slugs + org_key_suffix + np.full(n_org, ".com", dtype=object)
        org_prefix = rng.choice(ORG_EMAIL_PREFIXES, size=n_org, replace=True)
        Email[IsOrg] = org_prefix.astype(str) + "@" + OrgDomain

    # -----------------------------------------------------
    # CustomerName
    # -----------------------------------------------------
    CustomerName = np.where(
        IsOrg,
        OrgName.astype(str),
        safe_first.astype(str) + " " + safe_last.astype(str),
    )

    # -----------------------------------------------------
    # Demographics
    # -----------------------------------------------------
    BirthDate = np.full(N, np.datetime64("NaT"), dtype="datetime64[ns]")
    ages_days = np.zeros(N, dtype="int64")
    person_mask = ~IsOrg
    n_person = int(person_mask.sum())
    if n_person:
        ages_days[person_mask] = rng.integers(AGE_MIN_DAYS, AGE_MAX_DAYS, size=n_person)
        anchor = end_date.normalize()
        dates = anchor - pd.to_timedelta(ages_days[person_mask], unit="D")
        BirthDate[person_mask] = pd.to_datetime(dates).to_numpy("datetime64[ns]")

    # Age bracket for person rows (0=18-24 .. 5=65+), used to condition demographics
    ages_years = ages_days / 365.25
    person_age_bracket = (
        np.searchsorted(AGE_GROUP_EDGES, ages_years[person_mask])
        if n_person else np.array([], dtype="int64")
    )
    person_idx = np.where(person_mask)[0]

    MaritalStatus = np.empty(N, dtype=object)
    MaritalStatus[IsOrg] = None
    if n_person:
        # Vectorized bracket-aware sampling: build CDF per bracket, sample via uniform + searchsorted
        _ms_labels = MARITAL_STATUS_LABELS
        _n_brackets = len(AGE_GROUP_LABELS)
        # Stack CDFs: shape (n_brackets, n_labels)
        _ms_cdfs = np.array([np.cumsum(MARITAL_PROBS_BY_AGE[b]) for b in range(_n_brackets)])
        _ms_cdfs[:, -1] = 1.0  # clamp
        _u = rng.random(n_person)
        _bracket_per_person = person_age_bracket  # int array [0..n_brackets-1]
        # Vectorized: one searchsorted call per unique bracket instead of per person
        _ms_idx = _vectorized_cdf_sample(_ms_cdfs, _bracket_per_person, _u)
        _ms_idx = np.clip(_ms_idx, 0, len(_ms_labels) - 1)
        MaritalStatus[person_idx] = _ms_labels[_ms_idx]

    Education = np.empty(N, dtype=object)
    Education[:] = None
    if n_person:
        _ed_labels = EDUCATION_LABELS
        _ed_cdfs = np.array([np.cumsum(EDUCATION_PROBS_BY_AGE[b]) for b in range(len(AGE_GROUP_LABELS))])
        _ed_cdfs[:, -1] = 1.0
        _u = rng.random(n_person)
        _ed_idx = _vectorized_cdf_sample(_ed_cdfs, person_age_bracket, _u)
        _ed_idx = np.clip(_ed_idx, 0, len(_ed_labels) - 1)
        Education[person_idx] = _ed_labels[_ed_idx]

    Occupation = np.empty(N, dtype=object)
    Occupation[:] = None
    if n_person:
        # Build CDF per education level, then vectorized lookup
        _occ_labels = OCCUPATION_LABELS
        _occ_cdfs = np.array([np.cumsum(OCCUPATION_PROBS_BY_EDUCATION[lbl]) for lbl in EDUCATION_LABELS])
        _occ_cdfs[:, -1] = 1.0
        _person_edu = Education[person_mask]
        _edu_code_arr = _labels_to_codes(_person_edu, EDUCATION_LABELS)
        _u = rng.random(n_person)
        _occ_idx = _vectorized_cdf_sample(_occ_cdfs, _edu_code_arr, _u)
        _occ_idx = np.clip(_occ_idx, 0, len(_occ_labels) - 1)
        Occupation[person_idx] = _occ_labels[_occ_idx]

    income_raw = generate_correlated_income(rng, Education, Occupation, person_mask, N)
    YearlyIncome = pd.array(
        np.where(IsOrg, pd.NA, income_raw), dtype="Int32"
    )

    children_raw = np.zeros(N, dtype="int64")
    if n_person:
        # Vectorized: build per-person lambda from (marital, bracket) lookup,
        # then single Poisson draw for all persons.
        person_marital = MaritalStatus[person_mask]
        _ms_codes = _labels_to_codes(person_marital, MARITAL_STATUS_LABELS)
        _n_ms = len(MARITAL_STATUS_LABELS)
        _n_br = len(AGE_GROUP_LABELS)
        # Build lambda lookup table: (n_marital, n_brackets)
        _lam_table = np.ones((_n_ms, _n_br), dtype=np.float64)
        for _mi, _ml in enumerate(MARITAL_STATUS_LABELS):
            for _bi in range(_n_br):
                _lam_table[_mi, _bi] = CHILDREN_LAMBDA_BY_MARITAL_AGE.get((_ml, _bi), 1.0)
        _per_person_lam = _lam_table[_ms_codes, person_age_bracket]
        children_raw[person_idx] = np.clip(
            rng.poisson(lam=_per_person_lam), 0, MAX_CHILDREN - 1,
        )
    TotalChildren = pd.array(
        np.where(IsOrg, pd.NA, children_raw), dtype="Int32",
    )

    # -----------------------------------------------------
    # Derived demographic columns
    # -----------------------------------------------------
    ages_years = ages_days / 365.25

    AgeGroup = np.empty(N, dtype=object)
    AgeGroup[:] = None
    if n_person:
        idx = np.searchsorted(AGE_GROUP_EDGES, ages_years[person_mask])
        AgeGroup[person_mask] = AGE_GROUP_LABELS[idx]

    income_for_grouping = income_raw.astype("float64")
    IncomeGroup = np.empty(N, dtype=object)
    IncomeGroup[:] = None
    if n_person:
        idx = np.searchsorted(INCOME_GROUP_EDGES, income_for_grouping[person_mask])
        IncomeGroup[person_mask] = INCOME_GROUP_LABELS[idx]

    income_norm = np.zeros(N, dtype="float64")
    if n_person:
        income_norm[person_mask] = (
            (income_for_grouping[person_mask] - INCOME_MIN)
            / max(INCOME_MAX - INCOME_MIN, 1)
        )
    income_norm = np.clip(income_norm, 0.0, 1.0)

    CreditScore = generate_credit_scores(rng, income_norm, Education, person_mask, N)

    HomeOwnership = np.empty(N, dtype=object)
    HomeOwnership[:] = None
    if n_person:
        # Vectorized: pre-build CDF for all (income_group, age_bracket) combos,
        # then single-pass sampling via uniform + searchsorted.
        _ho_labels = HOME_OWNERSHIP_LABELS
        _ig_list = list(HOME_OWNERSHIP_PROBS_BY_INCOME.keys())
        _n_ig = len(_ig_list)
        _n_br = len(AGE_GROUP_LABELS)
        _n_ho = len(_ho_labels)
        # Build CDF table: (n_income_groups, n_brackets, n_ho_labels)
        _ho_cdfs = np.zeros((_n_ig, _n_br, _n_ho), dtype=np.float64)
        for _ii, _ig_lbl in enumerate(_ig_list):
            base_probs = HOME_OWNERSHIP_PROBS_BY_INCOME[_ig_lbl]
            for _bi in range(_n_br):
                adjusted = np.clip(base_probs + HOME_OWNERSHIP_AGE_SHIFT[_bi], 0.01, None)
                _ho_cdfs[_ii, _bi] = np.cumsum(adjusted / adjusted.sum())
                _ho_cdfs[_ii, _bi, -1] = 1.0

        ig = IncomeGroup[person_mask]
        _ig_codes = _labels_to_codes(ig, _ig_list)
        _u = rng.random(n_person)
        # Flatten 2D (income_group, bracket) into single composite key for vectorized sampling
        _ho_cdfs_flat = _ho_cdfs.reshape(_n_ig * _n_br, _n_ho)
        _composite_bracket = _ig_codes * _n_br + person_age_bracket
        _ho_idx = _vectorized_cdf_sample(_ho_cdfs_flat, _composite_bracket, _u)
        _ho_idx = np.clip(_ho_idx, 0, _n_ho - 1)
        HomeOwnership[person_idx] = _ho_labels[_ho_idx]

    NumberOfCars = np.full(N, pd.NA, dtype=object)
    if n_person:
        car_lambda = CAR_LAMBDA_BY_AGE[person_age_bracket]
        base_cars = rng.poisson(lam=car_lambda)
        income_boost_cars = (income_norm[person_mask] > 0.5).astype(int)
        us_boost = (Region[person_mask] == "US").astype(int)
        raw_cars = np.clip(base_cars + income_boost_cars + us_boost, 0, 4)
        NumberOfCars[person_mask] = raw_cars

    PhoneNumber = generate_phone_numbers(rng, Region, N)

    REFERRAL_SOURCE_LABELS = np.array(["None", "Friend", "Family", "Colleague"])
    REFERRAL_SOURCE_PROBS = np.array([0.50, 0.25, 0.13, 0.12])
    ReferralSource = rng.choice(REFERRAL_SOURCE_LABELS, size=N, p=REFERRAL_SOURCE_PROBS)

    PreferredContactMethod = rng.choice(
        CONTACT_METHOD_LABELS, size=N, p=CONTACT_METHOD_PROBS
    )

    # -----------------------------------------------------
    # Lifecycle + behavioral knobs
    # -----------------------------------------------------
    lifecycle_cfg = getattr(cust_cfg, "lifecycle", {}) or {}

    initial_active_raw = lifecycle_cfg.get("initial_active_customers", 0.45) or 0
    initial_active_raw = float(initial_active_raw)
    if 0.0 < initial_active_raw <= 1.0:
        initial_active_customers = int(round(initial_active_raw * N))
    else:
        initial_active_customers = int(initial_active_raw)
    initial_spread_months = int(lifecycle_cfg.get("initial_spread_months", 3) or 0)

    even_start_months = bool(lifecycle_cfg.get("even_start_months", False))

    acquisition_curve = lifecycle_cfg.get("acquisition_curve")
    if acquisition_curve is None:
        acquisition_curve = "logistic"

    acquisition_params = lifecycle_cfg.get("acquisition_params", {}) or {}
    if acquisition_curve == "logistic" and not acquisition_params:
        acquisition_params = {"midpoint": 0.30, "steepness": 8.0}
    weights = acquisition_weights(T, acquisition_curve, acquisition_params)

    CustomerStartMonth = rng.choice(np.arange(T), size=N, p=weights).astype("int64")

    if initial_active_customers > 0:
        k = min(initial_active_customers, N)

        active_idx = np.where(is_active == 1)[0]
        if active_idx.size > 0:
            k2 = min(k, int(active_idx.size))
            warm_idx = rng.choice(active_idx, size=k2, replace=False)
            if k2 < k:
                rest = np.setdiff1d(np.arange(N), warm_idx, assume_unique=False)
                extra = rng.choice(rest, size=(k - k2), replace=False)
                warm_idx = np.concatenate([warm_idx, extra])
        else:
            warm_idx = rng.choice(np.arange(N), size=k, replace=False)

        spread_hi = int(min(max(initial_spread_months, 0), max(T - 1, 0)))
        if spread_hi > 0:
            CustomerStartMonth[warm_idx] = rng.integers(0, spread_hi + 1, size=warm_idx.size, dtype=np.int64)
        else:
            CustomerStartMonth[warm_idx] = 0

    enable_churn = bool(lifecycle_cfg.get("enable_churn", True))
    base_monthly_churn = float(lifecycle_cfg.get("base_monthly_churn", 0.04))
    min_tenure_months = int(lifecycle_cfg.get("min_tenure_months", 3))

    CustomerWeight = rng.lognormal(mean=0.0, sigma=0.6, size=N).astype("float64")
    CustomerTemperature = np.clip(rng.normal(loc=0.6, scale=0.25, size=N), 0.05, 1.0).astype("float64")

    churn_bias_cfg = lifecycle_cfg.get("churn_bias", {}) or {}
    bias_sigma = float(churn_bias_cfg.get("sigma", 0.5))
    CustomerChurnBias = rng.lognormal(mean=0.0, sigma=bias_sigma, size=N).astype("float64")

    # -----------------------------------------------------
    # Loyalty tier + acquisition channel
    # -----------------------------------------------------
    enrich_cfg = cust_cfg.enrichment or {}
    loyalty_cfg = (enrich_cfg.get("loyalty_tier") or {}) if isinstance(enrich_cfg, Mapping) else {}
    acq_cfg = (enrich_cfg.get("acquisition_channel") or {}) if isinstance(enrich_cfg, Mapping) else {}

    loyalty_dim = read_parquet_dim(parquet_dims_folder, "loyalty_tiers")
    acq_dim = read_parquet_dim(parquet_dims_folder, "customer_acquisition_channels")

    loyalty_key_col = first_existing_col(loyalty_dim, ["LoyaltyTierKey", "TierKey", "Key"])
    loyalty_name_col = first_existing_col(loyalty_dim, ["LoyaltyTier", "TierName", "Name"])

    acq_key_col = first_existing_col(acq_dim, ["CustomerAcquisitionChannelKey", "AcquisitionChannelKey", "ChannelKey", "Key"])
    acq_name_col = first_existing_col(acq_dim, ["CustomerAcquisitionChannel", "AcquisitionChannel", "ChannelName", "Name"])

    loyalty_dim = loyalty_dim[[loyalty_key_col, loyalty_name_col]].dropna(subset=[loyalty_key_col]).copy()
    loyalty_dim[loyalty_key_col] = pd.to_numeric(loyalty_dim[loyalty_key_col], errors="coerce")
    loyalty_dim = loyalty_dim.dropna(subset=[loyalty_key_col]).sort_values(loyalty_key_col)

    tier_keys = loyalty_dim[loyalty_key_col].astype("int64").to_numpy()

    score = (
        LOYALTY_W_WEIGHT * np.log1p(CustomerWeight)
        + LOYALTY_W_TEMP * CustomerTemperature
        + LOYALTY_W_INCOME * income_norm
    )

    probs = loyalty_cfg.get("probs_low_to_high")
    if probs is None:
        tier_probs = default_tier_probs(len(tier_keys))
    else:
        tier_probs = normalize_probs(np.array(probs, dtype="float64"))
        if len(tier_probs) != len(tier_keys):
            raise DimensionError(
                f"customers.enrichment.loyalty_tier.probs_low_to_high length must match tiers "
                f"({len(tier_keys)}), got {len(tier_probs)}"
            )

    LoyaltyTierKey = assign_tier_by_score(
        score=score,
        tier_keys_sorted_low_to_high=tier_keys,
        tier_probs_low_to_high=tier_probs,
    )

    if IsOrg.any():
        org_mode = str(loyalty_cfg.get("org_mode", "top2")).lower()
        if org_mode == "top1":
            LoyaltyTierKey[IsOrg] = tier_keys[-1]
        elif org_mode == "top2" and len(tier_keys) >= 2:
            top2 = tier_keys[-2:]
            LoyaltyTierKey[IsOrg] = rng.choice(top2, size=int(IsOrg.sum()), p=[0.35, 0.65])

    acq_dim = acq_dim[[acq_key_col, acq_name_col]].dropna(subset=[acq_key_col]).copy()
    acq_dim[acq_key_col] = pd.to_numeric(acq_dim[acq_key_col], errors="coerce")
    acq_dim = acq_dim.dropna(subset=[acq_key_col]).sort_values(acq_key_col)

    acq_keys = acq_dim[acq_key_col].astype("int64").to_numpy()
    acq_names = acq_dim[acq_name_col].astype(str).to_numpy()

    w_ind = acquisition_weights_from_names(acq_names, org=False)
    w_org = acquisition_weights_from_names(acq_names, org=True)

    CustomerAcquisitionChannelKey = rng.choice(acq_keys, size=N, replace=True, p=w_ind)
    if IsOrg.any():
        CustomerAcquisitionChannelKey[IsOrg] = rng.choice(acq_keys, size=int(IsOrg.sum()), replace=True, p=w_org)

    CustomerEndMonth = simulate_end_month(
        rng=rng,
        start_month=CustomerStartMonth,
        churn_bias=CustomerChurnBias,
        T=T,
        enable=enable_churn,
        base_monthly_churn=base_monthly_churn,
        min_tenure_months=min_tenure_months,
    )

    CustomerStartDate = month_idx_to_date(start_month0, CustomerStartMonth)
    # Add random day-of-month offset (0-27 days) so dates aren't always the 1st
    day_offsets = rng.integers(0, 28, size=N).astype("timedelta64[D]")
    CustomerStartDate = CustomerStartDate + day_offsets

    if enable_churn:
        _cem = pd.to_numeric(CustomerEndMonth, errors="coerce")
        end_idx = np.where(np.isnan(_cem), -1, _cem).astype(np.int64)
        has_end = end_idx >= 0
        CustomerEndDate = np.empty(N, dtype="datetime64[ns]")
        CustomerEndDate[:] = np.datetime64("NaT")
        if has_end.any():
            end_base = month_idx_to_date(start_month0, end_idx[has_end])
            n_with_end = int(has_end.sum())
            end_offsets = rng.integers(0, 28, size=n_with_end).astype("timedelta64[D]")
            CustomerEndDate[has_end] = end_base + end_offsets
    else:
        CustomerEndDate = np.empty(N, dtype="datetime64[ns]")
        CustomerEndDate[:] = np.datetime64("NaT")

    CustomerType = np.where(IsOrg, "Organization", "Individual")
    CompanyName = np.where(IsOrg, OrgName, None)

    # -----------------------------------------------------
    # Customer satisfaction (correlated with churn bias)
    # -----------------------------------------------------
    churn_norm = np.clip(CustomerChurnBias / (CustomerChurnBias.max() + 1e-9), 0, 1)
    csat_raw = 5.0 - 3.0 * churn_norm + rng.normal(0, 0.5, size=N)
    CustomerSatisfactionScore = np.clip(np.round(csat_raw).astype(int), 1, 5)

    # =====================================================
    # CustomerProfile columns
    # =====================================================

    # --- Geography-derived: City, State, Country per customer ---
    geo_mapped = geo_lookup.reindex(GeographyKey)
    geo_city = geo_mapped["City"].to_numpy().astype(object)
    geo_state = geo_mapped["State"].to_numpy().astype(object)
    geo_country = geo_mapped["Country"].to_numpy().astype(object)

    CurrentCity = geo_city.copy()
    BirthCity = geo_city.copy()
    relocated = rng.random(N) < 0.25
    if relocated.any():
        BirthCity[relocated] = rng.permutation(geo_city)[: int(relocated.sum())]

    # --- Address columns ---
    HomeAddress, WorkAddress = generate_addresses(
        rng, Region, CustomerKey, geo_city, geo_state, N,
    )
    PostalCode = generate_postal_codes(rng, Region, N)
    Latitude, Longitude = generate_lat_lon(rng, Region, N)

    # --- Urban/Rural, TimeZone ---
    UrbanRural = np.empty(N, dtype=object)
    for rc, probs in _URBAN_RURAL_PROBS.items():
        mask = Region == rc
        n_rc = int(mask.sum())
        if n_rc:
            UrbanRural[mask] = rng.choice(_URBAN_RURAL_LABELS, size=n_rc, p=probs)
    remaining_ur = pd.isna(UrbanRural) | (UrbanRural == None)  # noqa: E711
    if remaining_ur.any():
        UrbanRural[remaining_ur] = rng.choice(
            _URBAN_RURAL_LABELS, size=int(remaining_ur.sum()),
            p=_URBAN_RURAL_PROBS["US"],
        )

    TimeZone = np.empty(N, dtype=object)
    for rc, tz_pool in _REGION_TIMEZONE.items():
        mask = Region == rc
        n_rc = int(mask.sum())
        if n_rc:
            TimeZone[mask] = rng.choice(tz_pool, size=n_rc)
    remaining_tz = pd.isna(TimeZone) | (TimeZone == None)  # noqa: E711
    if remaining_tz.any():
        TimeZone[remaining_tz] = "America/New_York"

    # --- Distance to nearest store (correlated with UrbanRural) ---
    DistanceToNearestStoreKm = np.zeros(N, dtype="float64")
    for area, (lo, hi) in _DISTANCE_BY_AREA.items():
        mask = UrbanRural == area
        n_a = int(mask.sum())
        if n_a:
            DistanceToNearestStoreKm[mask] = np.round(
                rng.uniform(lo, hi, size=n_a), 1
            )

    # --- Digital & Engagement ---
    PreferredLanguage = np.empty(N, dtype=object)
    for rc, lang_pool in _LANGUAGE_BY_REGION.items():
        mask = Region == rc
        n_rc = int(mask.sum())
        if n_rc:
            PreferredLanguage[mask] = rng.choice(lang_pool, size=n_rc)
    remaining_lang = pd.isna(PreferredLanguage) | (PreferredLanguage == None)  # noqa: E711
    if remaining_lang.any():
        PreferredLanguage[remaining_lang] = "English"

    age_young = np.zeros(N, dtype="float64")
    if n_person:
        age_young[person_mask] = np.clip(1.0 - (ages_years[person_mask] - 18) / 52, 0.1, 0.95)
    org_digital = np.full(N, 0.75)
    young_factor = np.where(IsOrg, org_digital, age_young)

    HasOnlineAccount = rng.random(N) < (0.55 + 0.30 * young_factor)
    OptInMarketing = rng.random(N) < 0.65
    SocialMediaFollower = rng.random(N) < (0.20 + 0.40 * young_factor)
    AppInstalled = HasOnlineAccount & (rng.random(N) < (0.30 + 0.35 * young_factor))

    NewsletterFrequency = np.where(
        ~OptInMarketing,
        "None",
        rng.choice(_NEWSLETTER_FREQ, size=N, p=np.array([0.30, 0.45, 0.25])),
    )

    device_probs_young = np.array([0.60, 0.25, 0.15])
    device_probs_old = np.array([0.25, 0.55, 0.20])
    DevicePreference = np.empty(N, dtype=object)
    young_mask = young_factor > 0.5
    n_young = int(young_mask.sum())
    n_old = N - n_young
    if n_young:
        DevicePreference[young_mask] = rng.choice(_DEVICE_PREFS, size=n_young, p=device_probs_young)
    if n_old:
        DevicePreference[~young_mask] = rng.choice(_DEVICE_PREFS, size=n_old, p=device_probs_old)

    days_back = rng.integers(0, 180, size=N)
    LastWebVisitDate = pd.to_datetime(end_date) - pd.to_timedelta(days_back, unit="D")
    LastWebVisitDate = LastWebVisitDate.to_numpy("datetime64[ns]")

    # --- Financial & Behavioral ---
    PreferredPaymentMethod = rng.choice(
        _PAYMENT_METHOD_LABELS, size=N, p=_PAYMENT_METHOD_PROBS,
    )

    start_dates_ts = pd.to_datetime(CustomerStartDate)
    days_after_start = rng.integers(0, 90, size=N)
    MemberSinceDate = (start_dates_ts + pd.to_timedelta(days_after_start, unit="D")).to_numpy("datetime64[ns]")

    IsEmployee = rng.random(N) < 0.02
    IsEmployee[IsOrg] = False

    spend_score = np.clip(
        0.4 * CustomerWeight / (CustomerWeight.max() + 1e-9)
        + 0.4 * income_norm
        + 0.2 * rng.random(N),
        0, 1,
    )
    AnnualSpendBucket = np.where(
        spend_score < 0.30, "Low",
        np.where(spend_score < 0.60, "Medium",
        np.where(spend_score < 0.85, "High", "VIP")),
    )

    HasGiftCardBalance = rng.random(N) < 0.15

    tier_norm = LoyaltyTierKey.astype("float64") / max(tier_keys.max(), 1)
    RewardPointsBalance = np.clip(
        (tier_norm * 30000 + rng.exponential(5000, size=N)).astype(int),
        0, 50000,
    )

    AvgOrderFrequencyDays = np.clip(
        (90.0 / (CustomerWeight + 0.1) + rng.normal(0, 10, size=N)).astype(int),
        7, 365,
    )

    # --- CX analytics ---
    nps_base = (CustomerSatisfactionScore - 1) * 2.5
    NPS = np.clip(
        (nps_base + rng.normal(0, 1.0, size=N)).astype(int),
        0, 10,
    )

    tenure_months = np.clip(T - CustomerStartMonth, 1, T).astype("float64")
    clv_raw = (
        income_norm * 2000
        + CustomerWeight * 500
        + tenure_months * 20
        + rng.exponential(300, size=N)
    )
    CustomerLifetimeValue = np.round(np.clip(clv_raw, 50, 100_000), 2)

    churn_risk_score = (
        0.45 * churn_norm
        + 0.30 * (1.0 - CustomerSatisfactionScore / 5.0)
        + 0.25 * (1.0 - np.clip(tenure_months / T, 0, 1))
    )
    ChurnRisk = np.where(
        churn_risk_score < 0.33, "Low",
        np.where(churn_risk_score < 0.66, "Medium", "High"),
    )

    # =====================================================
    # Household assignment (skipped in parallel chunk mode)
    # =====================================================
    if _skip_post_phases:
        # Return raw arrays for the parallel orchestrator to merge and process
        HouseholdKey = np.arange(1, N + 1, dtype=np.int32)
        HouseholdRole = np.full(N, "Single", dtype=object)
    else:
        household_pct_cfg = getattr(cust_cfg, "household_pct", None)
        household_pct = float(household_pct_cfg) if household_pct_cfg is not None else _HOUSEHOLD_PCT_DEFAULT

        HouseholdKey, HouseholdRole = assign_households(
            rng=rng,
            N=N,
            is_org=IsOrg,
            geography_key=GeographyKey,
            gender=Gender,
            ages_years=ages_years,
            marital_status=MaritalStatus,
            children_raw=children_raw,
            household_pct=household_pct,
        )

        n_multi = int((HouseholdRole == "Spouse").sum() + (HouseholdRole == "Dependent").sum()
                      + (HouseholdRole == "Relative").sum())
        n_households = int(np.max(HouseholdKey))
        info(f"Households: {n_households} total, {n_multi} customers in multi-person households")

        # Copy head's home address columns to household members
        moved, head_of = head_indices_for_members(HouseholdKey, HouseholdRole)
        if moved.any():
            for arr in (Region, HomeAddress, PostalCode, Latitude, Longitude,
                        geo_city, geo_state, CurrentCity):
                arr[moved] = arr[head_of]

    # =====================================================
    # Build Customers dataframe (identity + engine + SCD2 tracked cols)
    # =====================================================
    customers_df = pd.DataFrame(
        {
            "CustomerKey": CustomerKey,
            "CustomerID": CustomerKey.copy(),       # durable business key (= CustomerKey initially)
            # --- SCD2 metadata (always present, defaults for Type 1 mode) ---
            "VersionNumber": np.ones(N, dtype=np.int32),
            "EffectiveStartDate": pd.to_datetime(CustomerStartDate),
            "EffectiveEndDate": SCD2_END_OF_TIME,
            "IsCurrent": np.ones(N, dtype=bool),
            "CustomerName": CustomerName,
            "DOB": BirthDate,
            "Gender": Gender,
            "EmailAddress": Email,
            "PhoneNumber": PhoneNumber,
            "HomeAddress": HomeAddress,
            "WorkAddress": WorkAddress,
            "Latitude": Latitude,
            "Longitude": Longitude,
            "PostalCode": PostalCode,
            "CustomerType": CustomerType,
            "CompanyName": CompanyName,
            "GeographyKey": GeographyKey,
            "HouseholdKey": HouseholdKey,
            "HouseholdRole": HouseholdRole,
            "LoyaltyTierKey": pd.Series(LoyaltyTierKey, dtype=np.int32),
            "CustomerAcquisitionChannelKey": pd.Series(CustomerAcquisitionChannelKey, dtype=np.int32),
            # --- Columns moved from CustomerProfile (SCD2 tracked) ---
            "YearlyIncome": YearlyIncome,
            "IncomeGroup": IncomeGroup,
            "MaritalStatus": MaritalStatus,
            "HomeOwnership": HomeOwnership,
            "NumberOfChildren": TotalChildren,
            # --- Lifecycle dates ---
            "CustomerStartDate": pd.to_datetime(CustomerStartDate),
            "CustomerEndDate": pd.to_datetime(CustomerEndDate),
        }
    )

    # =====================================================
    # Build CustomerProfile dataframe (analytical slicers — minus moved cols)
    # =====================================================
    # CustomerProfile is person-only; orgs have their own OrganizationProfile table.
    person_mask = ~IsOrg
    profile_df = pd.DataFrame(
        {
            "CustomerKey": CustomerKey[person_mask],
            "AgeGroup": AgeGroup[person_mask],
            "Education": Education[person_mask],
            "Occupation": Occupation[person_mask],
            "NumberOfCars": NumberOfCars[person_mask].astype(np.int32),
            "CreditScore": CreditScore[person_mask].astype(np.int32),
            "UrbanRural": UrbanRural[person_mask],
            "TimeZone": TimeZone[person_mask],
            "BirthCity": BirthCity[person_mask],
            "CurrentCity": CurrentCity[person_mask],
            "DistanceToNearestStoreKm": DistanceToNearestStoreKm[person_mask],
            "PreferredLanguage": PreferredLanguage[person_mask],
            "HasOnlineAccount": HasOnlineAccount[person_mask],
            "OptInMarketing": OptInMarketing[person_mask],
            "SocialMediaFollower": SocialMediaFollower[person_mask],
            "AppInstalled": AppInstalled[person_mask],
            "NewsletterFrequency": NewsletterFrequency[person_mask],
            "DevicePreference": DevicePreference[person_mask],
            "LastWebVisitDate": pd.to_datetime(LastWebVisitDate[person_mask]),
            "PreferredPaymentMethod": PreferredPaymentMethod[person_mask],
            "PreferredContactMethod": PreferredContactMethod[person_mask],
            "ReferralSource": ReferralSource[person_mask],
            "MemberSinceDate": pd.to_datetime(MemberSinceDate[person_mask]),
            "IsEmployee": IsEmployee[person_mask],
            "AnnualSpendBucket": AnnualSpendBucket[person_mask],
            "HasGiftCardBalance": HasGiftCardBalance[person_mask],
            "RewardPointsBalance": RewardPointsBalance[person_mask].astype(np.int32),
            "AvgOrderFrequencyDays": AvgOrderFrequencyDays[person_mask].astype(np.int32),
            "CustomerSatisfactionScore": CustomerSatisfactionScore[person_mask].astype(np.int32),
            "NPS": NPS[person_mask].astype(np.int32),
            "CustomerLifetimeValue": CustomerLifetimeValue[person_mask],
            "ChurnRisk": ChurnRisk[person_mask],
        }
    )

    # =====================================================
    # Build OrganizationProfile dataframe (org-only)
    # — skipped in parallel chunk mode (done after merge)
    # =====================================================
    if _skip_post_phases:
        org_profile_df = pd.DataFrame()
    else:
        org_profile_df = generate_org_profile(
            rng=rng,
            customer_key=CustomerKey,
            is_org=IsOrg,
            org_name=OrgName,
            region=Region,
            customer_start_date=CustomerStartDate,
            churn_bias=CustomerChurnBias,
            people_pools=people_pools,
            end_date=end_date,
        )

    # =====================================================
    # SCD Type 2 expansion (if enabled)
    # — skipped in parallel chunk mode (done after merge)
    # =====================================================
    if not _skip_post_phases:
        scd2_cfg = getattr(cust_cfg, "scd2", None)
        scd2_enabled = bool(getattr(scd2_cfg, "enabled", False)) if scd2_cfg else False
        if scd2_enabled:
            customers_df = generate_scd2_versions(
                rng=rng,
                base_df=customers_df,
                cust_cfg=scd2_cfg,
                geo_keys=geo_keys,
                tier_keys=tier_keys,
                end_date=end_date,
                geo_lookup=geo_lookup,
            )

            # Remap profile/org-profile CustomerKey → IsCurrent=1 version's CustomerKey
            current_map = (
                customers_df.loc[customers_df["IsCurrent"] == 1, ["CustomerID", "CustomerKey"]]
                .set_index("CustomerID")["CustomerKey"]
            )
            profile_df["CustomerKey"] = (
                profile_df["CustomerKey"].map(current_map).astype("int64")
            )
            if not org_profile_df.empty:
                org_profile_df["CustomerKey"] = (
                    org_profile_df["CustomerKey"].map(current_map).astype("int64")
                )

    active_customer_set = set(active_customer_keys.tolist())
    return customers_df, profile_df, org_profile_df, active_customer_set


# ---------------------------------------------------------
# Parallel orchestrator
# ---------------------------------------------------------
def _generate_parallel(cfg, parquet_dims_folder: Path, n_workers: int):
    """Generate customers in parallel: chunk → merge → households → SCD2."""
    from multiprocessing import cpu_count
    from src.utils.pool import PoolRunSpec, iter_imap_unordered
    from src.dimensions.customers.worker import customer_chunk_worker, scd2_chunk_worker
    from src.dimensions.customers.scd2 import expand_changed_customers, _build_geo_cache

    cust_cfg = cfg.customers
    N = int(cust_cfg.total_customers)
    seed = resolve_seed(cfg, cust_cfg, fallback=42)

    start_date, end_date = parse_cfg_dates(cfg)

    # Serialize config for workers (must be picklable plain dict)
    cfg_dump = cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg)
    cfg_dump.pop("_config_snapshot", None)
    cfg_dump.pop("_models_snapshot", None)

    # Chunk partitioning
    n_chunks = min(n_workers * 2, max(2, N // 50_000))
    n_chunks = max(2, n_chunks)
    n_actual_workers = min(n_chunks, n_workers)

    chunk_boundaries = []
    base_chunk = N // n_chunks
    remainder = N % n_chunks
    for i in range(n_chunks):
        cn = base_chunk + (1 if i < remainder else 0)
        chunk_boundaries.append(cn)

    info(f"Customer parallel: {n_chunks} chunks across {n_actual_workers} workers")

    # Scratch directory for chunk parquets
    scratch_dir = parquet_dims_folder / "_customer_chunks"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    try:

        # Phase 2: Parallel chunk generation
        tasks = []
        for i, cn in enumerate(chunk_boundaries):
            output_base = str(scratch_dir / f"chunk_{i:05d}")
            tasks.append((
                i, cn, seed, n_chunks,
                cfg_dump, str(parquet_dims_folder), output_base,
            ))

        pool_spec = PoolRunSpec(
            processes=n_actual_workers,
            chunksize=1,
            label="customers",
        )

        chunk_results = []
        for result in iter_imap_unordered(
            tasks=tasks,
            task_fn=customer_chunk_worker,
            spec=pool_spec,
        ):
            chunk_results.append(result)

        chunk_results.sort(key=lambda r: r["chunk_idx"])

        # Phase 3: Merge chunks
        cust_dfs = []
        prof_dfs = []
        for i in range(n_chunks):
            base = str(scratch_dir / f"chunk_{i:05d}")
            cust_dfs.append(pd.read_parquet(f"{base}_customers.parquet"))
            prof_dfs.append(pd.read_parquet(f"{base}_profile.parquet"))

        customers_df = pd.concat(cust_dfs, ignore_index=True)
        profile_df = pd.concat(prof_dfs, ignore_index=True)
        del cust_dfs, prof_dfs

        # Reassign CustomerKey sequentially across all chunks
        customers_df["CustomerKey"] = np.arange(1, len(customers_df) + 1, dtype="int64")
        customers_df["CustomerID"] = customers_df["CustomerKey"].copy()
        # Profile is person-only; map keys via the person subset of customers_df
        person_keys = customers_df.loc[
            customers_df["CustomerType"] != "Organization", "CustomerKey"
        ].to_numpy()
        profile_df["CustomerKey"] = person_keys

        # Collect active customer keys
        active_customer_keys = set()
        for r in chunk_results:
            active_customer_keys.update(r["active_keys"])

        # Run household assignment on merged data (serial — shared state)
        household_pct_cfg = getattr(cust_cfg, "household_pct", None)
        household_pct = float(household_pct_cfg) if household_pct_cfg is not None else _HOUSEHOLD_PCT_DEFAULT

        hh_rng = np.random.default_rng(
            np.random.SeedSequence(seed).spawn(n_chunks + 2)[n_chunks]
        )

        # Extract arrays from merged DataFrame (only GeographyKey is mutated in-place)
        IsOrg = (customers_df["CustomerType"] == "Organization").to_numpy()
        Gender = customers_df["Gender"].to_numpy()
        GeographyKey = customers_df["GeographyKey"].to_numpy().copy()
        ages_days_raw = (pd.Timestamp(end_date) - pd.to_datetime(customers_df["DOB"])).dt.days
        ages_years = (ages_days_raw / 365.25).to_numpy(dtype="float64", na_value=0.0)
        MaritalStatus = customers_df["MaritalStatus"].to_numpy()
        children_raw = customers_df["NumberOfChildren"].to_numpy(dtype="int64", na_value=0)

        HouseholdKey, HouseholdRole = assign_households(
            rng=hh_rng,
            N=len(customers_df),
            is_org=IsOrg,
            geography_key=GeographyKey,
            gender=Gender,
            ages_years=ages_years,
            marital_status=MaritalStatus,
            children_raw=children_raw,
            household_pct=household_pct,
        )

        customers_df["HouseholdKey"] = HouseholdKey
        customers_df["HouseholdRole"] = HouseholdRole
        customers_df["GeographyKey"] = GeographyKey  # may have been mutated

        # Re-derive Region (not stored in parquet) for address rebuild + org_profile
        pct_asia = float(getattr(cust_cfg, "pct_asia", 0.0))
        enable_asia = pct_asia > 0.0
        p_in, p_us, p_eu, p_as = validate_percentages(
            float(cust_cfg.pct_india), float(cust_cfg.pct_us),
            float(cust_cfg.pct_eu), pct_asia,
        )
        region_labels = ["IN", "US", "EU"] + (["AS"] if enable_asia else [])
        region_probs = [p_in, p_us, p_eu] + ([p_as] if enable_asia else [])
        org_region_rng = np.random.default_rng(seed + 77777)
        Region = org_region_rng.choice(region_labels, size=len(customers_df), p=region_probs)

        geography, _ = load_dimension("geography", parquet_dims_folder, cfg.geography)
        geo_lookup = geography.set_index("GeographyKey")[["City", "State", "Country"]]

        # Copy head's home address columns to household members
        moved, head_of = head_indices_for_members(HouseholdKey, HouseholdRole)
        if moved.any():
            Region[moved] = Region[head_of]
            for col in ("HomeAddress", "PostalCode", "Latitude", "Longitude"):
                vals = customers_df[col].to_numpy().copy()
                vals[moved] = vals[head_of]
                customers_df[col] = vals
            # CurrentCity lives in profile_df (person-only), not customers_df
            full_city = geo_lookup["City"].reindex(
                customers_df["GeographyKey"]
            ).to_numpy(dtype=object)
            full_city[moved] = full_city[head_of]
            person_mask_p = (customers_df["CustomerType"] != "Organization").to_numpy()
            profile_df["CurrentCity"] = full_city[person_mask_p]

        n_multi = int((HouseholdRole == "Spouse").sum() + (HouseholdRole == "Dependent").sum()
                      + (HouseholdRole == "Relative").sum())
        n_households = int(np.max(HouseholdKey))
        info(f"Households: {n_households} total, {n_multi} customers in multi-person households")

        # Generate org_profile (serial, small)
        org_rng = np.random.default_rng(
            np.random.SeedSequence(seed).spawn(n_chunks + 2)[n_chunks + 1]
        )
        OrgName = customers_df["CompanyName"].to_numpy(dtype=object)
        # Load name pools for org_profile
        names_folder = resolve_people_folder()
        people_pools = load_people_pools(names_folder, enable_asia=enable_asia, legacy_support=True)

        CustomerStartDate = customers_df["CustomerStartDate"].to_numpy()
        CustomerKey = customers_df["CustomerKey"].to_numpy()

        # Derive churn_bias and customer_weight from existing columns
        CustomerWeight = np.ones(len(customers_df), dtype="float64")  # approx
        CustomerChurnBias = np.ones(len(customers_df), dtype="float64")  # approx

        org_profile_df = generate_org_profile(
            rng=org_rng,
            customer_key=CustomerKey,
            is_org=IsOrg,
            org_name=OrgName,
            region=Region,
            customer_start_date=CustomerStartDate,
            churn_bias=CustomerChurnBias,
            people_pools=people_pools,
            end_date=end_date,
        )

        # Phase 4: Parallel SCD2 (if enabled)
        scd2_cfg = getattr(cust_cfg, "scd2", None)
        scd2_enabled = bool(getattr(scd2_cfg, "enabled", False)) if scd2_cfg else False

        if scd2_enabled:
            change_rate = float(getattr(scd2_cfg, "change_rate", 0.15))
            max_versions = int(getattr(scd2_cfg, "max_versions", 4))

            person_mask = customers_df["CustomerType"] == "Individual"
            person_ids = customers_df.loc[person_mask, "CustomerID"].to_numpy()

            if len(person_ids) > 0:
                scd2_rng = np.random.default_rng(
                    np.random.SeedSequence(seed).spawn(n_chunks + 3)[n_chunks + 2]
                )

                n_change = max(1, int(len(person_ids) * change_rate))
                n_change = min(n_change, len(person_ids))
                change_id_set = set(
                    scd2_rng.choice(person_ids, size=n_change, replace=False).tolist()
                )

                _change_mask = customers_df["CustomerID"].isin(change_id_set)
                unchanged_df = customers_df[~_change_mask]
                changed_df = customers_df[_change_mask]

                geo_keys = geography["GeographyKey"].to_numpy()
                loyalty_dim = read_parquet_dim(parquet_dims_folder, "loyalty_tiers")
                loyalty_key_col = first_existing_col(loyalty_dim, ["LoyaltyTierKey", "TierKey", "Key"])
                tier_keys = loyalty_dim[loyalty_key_col].dropna().astype("int64").sort_values().to_numpy()

                geo_cache = _build_geo_cache(geo_lookup)

                # Parallelize SCD2 if enough changed customers
                if len(changed_df) > 10_000 and n_actual_workers > 1:
                    n_scd2_chunks = min(n_actual_workers, max(2, len(changed_df) // 5_000))
                    partitions = np.array_split(np.arange(len(changed_df)), n_scd2_chunks)
                    partitions = [p for p in partitions if len(p) > 0]
                    n_scd2_chunks = len(partitions)

                    scd2_tasks = []
                    col_names = changed_df.columns.tolist()
                    for si, idx_arr in enumerate(partitions):
                        chunk_records = changed_df.iloc[idx_arr].to_numpy().tolist()
                        out_path = str(scratch_dir / f"scd2_chunk_{si:05d}.parquet")
                        scd2_tasks.append((
                            si, n_scd2_chunks, seed,
                            chunk_records, col_names,
                            max_versions,
                            geo_keys.tolist(), tier_keys.tolist(),
                            str(end_date), geo_cache,
                            out_path,
                        ))

                    scd2_spec = PoolRunSpec(
                        processes=min(n_actual_workers, n_scd2_chunks),
                        chunksize=1,
                        label="scd2",
                    )

                    for _r in iter_imap_unordered(
                        tasks=scd2_tasks,
                        task_fn=scd2_chunk_worker,
                        spec=scd2_spec,
                    ):
                        pass

                    scd2_expanded = []
                    for si in range(n_scd2_chunks):
                        path = scratch_dir / f"scd2_chunk_{si:05d}.parquet"
                        if path.exists():
                            scd2_expanded.append(pd.read_parquet(path))
                            path.unlink()

                    expanded_df = pd.concat([unchanged_df] + scd2_expanded, ignore_index=True)
                else:
                    # Serial SCD2 for small change sets
                    expanded_rows = expand_changed_customers(
                        rng=scd2_rng,
                        changed_df=changed_df,
                        max_versions=max_versions,
                        geo_keys=geo_keys,
                        tier_keys=tier_keys,
                        end_date=end_date,
                        geo_lookup=geo_lookup,
                    )
                    expanded_df = pd.concat([unchanged_df, expanded_rows], ignore_index=True)

                expanded_df["CustomerKey"] = np.arange(1, len(expanded_df) + 1, dtype="int64")
                customers_df = expanded_df

                n_versions = len(customers_df) - N
                info(f"SCD2: {n_change} customers expanded, {n_versions} version rows added ({len(customers_df)} total)")

                # Remap profile/org-profile CustomerKey → IsCurrent=1 version's CustomerKey
                current_map = (
                    customers_df.loc[customers_df["IsCurrent"] == 1, ["CustomerID", "CustomerKey"]]
                    .set_index("CustomerID")["CustomerKey"]
                )
                profile_df["CustomerKey"] = (
                    profile_df["CustomerKey"].map(current_map).astype("int64")
                )
                if not org_profile_df.empty:
                    org_profile_df["CustomerKey"] = (
                        org_profile_df["CustomerKey"].map(current_map).astype("int64")
                    )

    finally:
        import shutil
        shutil.rmtree(scratch_dir, ignore_errors=True)

    active_customer_set = active_customer_keys
    return customers_df, profile_df, org_profile_df, active_customer_set


# ---------------------------------------------------------
# Pipeline entry
# ---------------------------------------------------------
def run_customers(cfg: Dict, parquet_folder: Path):
    out_path = parquet_folder / "customers.parquet"
    profile_out_path = parquet_folder / "customer_profile.parquet"
    org_profile_out_path = parquet_folder / "organization_profile.parquet"

    cust_cfg = cfg.customers

    version_cfg = dict(cust_cfg)
    version_cfg["_schema_version"] = 6
    version_cfg["_has_loyalty_tier"] = True
    version_cfg["_has_acquisition_channel"] = True
    version_cfg["_has_customer_profile"] = True
    version_cfg["_has_org_profile"] = True
    version_cfg["_has_scd2"] = True

    if not should_regenerate("customers", version_cfg, out_path):
        skip("Customers up-to-date")
        return

    with stage("Generating Customers"):
        from multiprocessing import cpu_count
        from src.defaults import CUSTOMER_PARALLEL_THRESHOLD

        N = int(cust_cfg.total_customers)
        sales_cfg = getattr(cfg, "sales", None)
        configured_workers = getattr(sales_cfg, "workers", None) if sales_cfg else None
        if configured_workers is not None:
            n_workers = max(1, int(configured_workers))
        else:
            n_workers = max(1, cpu_count() - 1)

        if N >= CUSTOMER_PARALLEL_THRESHOLD and n_workers >= 2:
            customers_df, profile_df, org_profile_df, _active = _generate_parallel(
                cfg, parquet_folder, n_workers,
            )
        else:
            customers_df, profile_df, org_profile_df, _active = generate_synthetic_customers(
                cfg, parquet_folder,
            )
        write_parquet_with_date32(customers_df, out_path, cast_all_datetime=True)
        write_parquet_with_date32(profile_df, profile_out_path, cast_all_datetime=True)

        n_ind = int((customers_df["CustomerType"] == "Individual").sum())
        n_org = int((customers_df["CustomerType"] == "Organization").sum())
        info(f"Customers: {len(customers_df):,} rows ({n_ind:,} individual, {n_org:,} org)")
        info(f"Customer Profile: {len(profile_df):,} rows")

        if not org_profile_df.empty:
            write_parquet_with_date32(org_profile_df, org_profile_out_path, cast_all_datetime=True)
            info(f"Organization Profile: {len(org_profile_df):,} rows")

    save_version("customers", version_cfg, out_path)
