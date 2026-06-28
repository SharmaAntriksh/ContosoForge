"""Main customer dimension generator — orchestrates helpers, org profile,
households, and SCD2 sub-modules to produce Customers, CustomerProfile,
and OrganizationProfile tables.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.utils import info, skip, stage
from src.utils.output_utils import write_parquet_with_date32
from src.utils.config_precedence import resolve_seed
from src.defaults import SCD2_END_OF_TIME
from src.versioning import should_regenerate, save_version
from src.engine.dimension_loader import load_dimension
from src.utils.name_pools import (
    resolve_people_folder,
    load_people_pools,
    assign_person_names,
    resolve_org_names_file,
    load_org_names,
    assign_org_names,
    org_domain_label,
)

from src.defaults import (
    CUSTOMER_PERSONAL_EMAIL_DOMAINS as PERSONAL_EMAIL_DOMAINS,
    CUSTOMER_MARITAL_STATUS_LABELS as MARITAL_STATUS_LABELS,
    CUSTOMER_EDUCATION_LABELS as EDUCATION_LABELS,
    CUSTOMER_EDUCATION_PROBS as EDUCATION_PROBS,
    CUSTOMER_OCCUPATION_LABELS as OCCUPATION_LABELS,
    CUSTOMER_OCCUPATION_PROBS as OCCUPATION_PROBS,
    CUSTOMER_AGE_MIN_YEARS as AGE_MIN_YEARS,
    CUSTOMER_AGE_MODE_YEARS as AGE_MODE_YEARS,
    CUSTOMER_AGE_MAX_YEARS as AGE_MAX_YEARS,
    CUSTOMER_INCOME_MIN as INCOME_MIN,
    CUSTOMER_INCOME_NORM_REF as INCOME_NORM_REF,
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
    CUSTOMER_DEPENDENT_INCOME_CAP as _DEPENDENT_INCOME_CAP,
    CUSTOMER_CHUNK_TARGET_ROWS as CHUNK_TARGET_ROWS,
    CUSTOMER_MAX_PARALLEL_CHUNKS as MAX_PARALLEL_CHUNKS,
    CUSTOMER_SCD2_PARALLEL_THRESHOLD as SCD2_PARALLEL_THRESHOLD,
    CUSTOMER_SCD2_CHUNK_TARGET_ROWS as SCD2_CHUNK_TARGET_ROWS,
    CUSTOMER_GENDER_CODE_MAP,
    CUSTOMER_MARKETING_CONSENT_BASE,
    CUSTOMER_CONSENT_EMAIL_RATE,
    CUSTOMER_CONSENT_SMS_RATE,
    CUSTOMER_CONSENT_CALL_RATE,
    CUSTOMER_CITY_LATLON_JITTER as _CITY_LATLON_JITTER,
    CUSTOMER_MIDDLE_NAME_RATE,
    CUSTOMER_TITLE_DR_RATE,
    CUSTOMER_REFERRAL_SOURCE_LABELS,
    CUSTOMER_REFERRAL_SOURCE_PROBS,
    CUSTOMER_PAYMENT_METHOD_LABELS,
    CUSTOMER_PAYMENT_METHOD_PROBS,
    CUSTOMER_DEVICE_PREF_LABELS,
    CUSTOMER_DEVICE_PROBS_YOUNG,
    CUSTOMER_DEVICE_PROBS_OLD,
    CUSTOMER_NEWSLETTER_FREQ_LABELS,
    CUSTOMER_NEWSLETTER_FREQ_PROBS,
    CUSTOMER_WEIGHT_SIGMA as WEIGHT_SIGMA,
    CUSTOMER_SPEND_BUCKET_EDGES as SPEND_BUCKET_EDGES,
    CUSTOMER_SPEND_BUCKET_LABELS as SPEND_BUCKET_LABELS,
    CUSTOMER_DISTANCE_BY_AREA,
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
    generate_postal_codes,
)
from src.dimensions.customers.org_profile import generate_org_profile
from src.dimensions.customers.households import assign_households, head_indices_for_members
from src.dimensions.customers.scd2 import (
    generate_scd2_versions,
    _build_region_pools,
    _build_geo_cache,
    expand_changed_customers,
)

def _labels_to_codes(values: np.ndarray, labels: Sequence) -> np.ndarray:
    """Map a string array to integer codes given an ordered label list."""
    codes = np.zeros(len(values), dtype=np.intp)
    for i, lbl in enumerate(labels):
        codes[values == lbl] = i
    return codes


def _geo_lookup_with_iso(geography: pd.DataFrame) -> pd.DataFrame:
    """Index geography by GeographyKey, keeping the columns SCD2 relocation needs.

    Includes per-city Latitude/Longitude so customer coordinates (and SCD2
    relocations) can anchor on the real city centroid instead of a region center.
    """
    cols = ["City", "State", "Country"]
    if "ISOCode" in geography.columns:
        cols.append("ISOCode")
    for c in ("Latitude", "Longitude"):
        if c in geography.columns:
            cols.append(c)
    return geography.set_index("GeographyKey")[cols]


def _build_country_city_pools(geography: pd.DataFrame) -> Dict[str, np.ndarray]:
    """Map Country -> array of its City names, for same-country BirthCity sampling."""
    if "Country" not in geography.columns or "City" not in geography.columns:
        return {}
    countries = geography["Country"].to_numpy().astype(object)
    cities = geography["City"].to_numpy().astype(object)
    return {ctry: cities[countries == ctry] for ctry in np.unique(countries)}


def _remap_profiles_to_current_version(customers_df, profile_df, org_profile_df) -> None:
    """Repoint profile / org-profile CustomerKey at the IsCurrent=1 version's key.

    Profiles are entity-level (one row per customer), but SCD2 reassigns a fresh
    CustomerKey to every version row. After expansion, repoint each profile at its
    family's current version via CustomerID. Mutates the profile frames in place.
    """
    current_map = (
        customers_df.loc[customers_df["IsCurrent"] == 1, ["CustomerID", "CustomerKey"]]
        .set_index("CustomerID")["CustomerKey"]
    )
    profile_df["CustomerKey"] = profile_df["CustomerKey"].map(current_map).astype("int64")
    if not org_profile_df.empty:
        org_profile_df["CustomerKey"] = (
            org_profile_df["CustomerKey"].map(current_map).astype("int64")
        )


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


def build_email_addresses(rng, *, safe_first, safe_last, org_name, keys, is_org):
    """Vectorized EmailAddress for all rows.

    The numeric suffix is ``CustomerKey``, so emails are unique only when the
    keys are globally unique. In the parallel path the chunk workers bake emails
    with chunk-LOCAL keys, so ``name+localkey`` collides across chunks (millions
    of customers ended up sharing an address at scale); the orchestrator rebuilds
    this against the post-merge global key (CUST-AN-9).
    """
    N = len(keys)
    is_org = np.asarray(is_org)
    keys = np.asarray(keys)
    email = np.empty(N, dtype=object)

    personal_mask = ~is_org
    n_personal = int(personal_mask.sum())
    if n_personal:
        domain = rng.choice(PERSONAL_EMAIL_DOMAINS, size=n_personal, replace=True)
        user = (
            np.asarray(safe_first)[personal_mask].astype(str)
            + "."
            + np.asarray(safe_last)[personal_mask].astype(str)
        ).astype(str)
        user = np.char.lower(np.char.replace(user, " ", ""))
        suffix = keys[personal_mask].astype(str)
        email[personal_mask] = user + suffix + "@" + domain

    n_org = int(is_org.sum())
    if n_org:
        org_slugs = np.array(
            [org_domain_label(x) for x in np.asarray(org_name)[is_org].astype(str)],
            dtype=object,
        )
        org_key_suffix = keys[is_org].astype(str)
        org_domain = org_slugs + org_key_suffix + np.full(n_org, ".com", dtype=object)
        org_prefix = rng.choice(ORG_EMAIL_PREFIXES, size=n_org, replace=True)
        email[is_org] = org_prefix.astype(str) + "@" + org_domain

    return email


# z-score of the 95th percentile of a standard normal; lognormal(0, sigma) has
# its 95th percentile at exp(_P95_Z * sigma).
_P95_Z = 1.6448536269514722
# Fixed normalization reference for the CustomerWeight lognormal (data- and
# size-independent, so segmentation is identical across chunks / run sizes).
_WEIGHT_NORM_REF = math.exp(_P95_Z * WEIGHT_SIGMA)


def lognormal_p95_ref(sigma: float) -> float:
    """Analytical 95th percentile of ``lognormal(mean=0, sigma)`` — a fixed
    normalization reference that does not depend on the sample."""
    return math.exp(_P95_Z * float(sigma))


def _robust_unit_norm(x: np.ndarray, ref: float) -> np.ndarray:
    """Normalize a non-negative, lognormal-ish array into [0, 1] against a
    **fixed** reference (typically the distribution's analytical 95th
    percentile), not a data-derived statistic.

    Max-normalization compressed the bulk toward 0 (the max of a lognormal grows
    with N), degenerating any segmentation built on it (CUST-AN-10). A *sample*
    quantile fixed the scale-with-N problem but was still data-derived, so under
    parallel chunking each chunk normalized against its own quantile and the same
    value mapped to different tiers in different chunks. A fixed analytical
    reference removes both: the mapping is independent of N and of chunking.
    """
    ref = ref if (np.isfinite(ref) and ref > 0) else 1.0
    return np.clip(x / ref, 0.0, 1.0)


def _chunk_count(n_rows: int, target_rows: int) -> int:
    """Parallel chunk count for ``n_rows`` rows at ~``target_rows`` per chunk,
    floored at 2 and capped at MAX_PARALLEL_CHUNKS.

    A pure function of the row count (no worker-count argument): the chunk count
    seeds the per-chunk RNG streams, so tying it to ``--workers`` silently changed
    the generated data (CUST-AN-7).
    """
    return int(np.clip(n_rows // target_rows, 2, MAX_PARALLEL_CHUNKS))


def _customer_chunk_count(N: int) -> int:
    """Parallel customer-generation chunk count (function of ``N`` only)."""
    return _chunk_count(N, CHUNK_TARGET_ROWS)


def _scd2_chunk_count(n_changed: int) -> int:
    """Parallel SCD2-expansion chunk count (function of the changed-row count)."""
    return _chunk_count(n_changed, SCD2_CHUNK_TARGET_ROWS)


def _cap_dependent_income(household_role, yearly_income, income_group):
    """Cap household-dependent income at an entry-level ceiling and refresh
    their IncomeGroup.

    Dependents (18-24, recruited into a head's household) would otherwise carry
    the full adult income draw. Only those above the cap are pulled down, so the
    natural spread below it is preserved. ``yearly_income`` is a nullable Int32
    pandas array (NA for orgs); ``income_group`` is a full-length object array
    (mutated in place). Returns the updated ``(yearly_income, income_group)``.
    """
    dep_mask = np.asarray(household_role == "Dependent")
    na_mask = np.asarray(pd.isna(yearly_income))
    inc_int = yearly_income.to_numpy(dtype="int64", na_value=0).copy()
    sel = dep_mask & ~na_mask & (inc_int > _DEPENDENT_INCOME_CAP)
    if not sel.any():
        return yearly_income, income_group

    inc_int[sel] = _DEPENDENT_INCOME_CAP
    income_group[sel] = INCOME_GROUP_LABELS[
        np.searchsorted(INCOME_GROUP_EDGES, float(_DEPENDENT_INCOME_CAP))
    ]
    new_income = pd.array(np.where(na_mask, pd.NA, inc_int), dtype="Int32")
    return new_income, income_group


def _reconcile_titles_with_marital(title, marital_status):
    """Keep the salutation consistent with the FINAL MaritalStatus.

    ``Title`` is derived in `_build_demographics` from the initial marital draw,
    but `assign_households` later marks matched spouses as Married — including
    people originally drawn as Single. A Single Female so married would keep
    ``Title="Ms"`` while ``MaritalStatus="Married"``; flip those to ``Mrs`` (Mr /
    Dr are unaffected by marital status). Mutates ``title`` in place.
    """
    title = np.asarray(title)
    fix = (np.asarray(marital_status) == "Married") & (title == "Ms")
    if fix.any():
        title[fix] = "Mrs"
    return title


def _build_demographics(
    *,
    rng, N, IsOrg, Region, Gender, person_mask, person_idx, n_person,
    person_age_bracket, ages_years,
) -> Dict[str, np.ndarray]:
    """Build the conditioned demographic columns (marital status, title,
    education, occupation, income, children, home ownership, cars, and derived
    groups).

    A pure function of the identity + age-bracket setup. Outputs feed the
    Customers / CustomerProfile frames plus the household and loyalty steps.
    Returns a dict of full-length (N) arrays.
    """
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

    # Title / Salutation: gendered (Mr; Mrs if married else Ms) with a small Dr
    # overlay. Orgs have no title (None). (No gender-neutral Mx — Gender is
    # strictly M/F/O, so every person maps to a gendered salutation.)
    Title = np.full(N, None, dtype=object)
    if n_person:
        _male = Gender[person_mask] == "Male"
        _married = MaritalStatus[person_mask] == "Married"
        _t = np.where(_male, "Mr", np.where(_married, "Mrs", "Ms")).astype(object)
        _tu = rng.random(n_person)
        _t[_tu < CUSTOMER_TITLE_DR_RATE] = "Dr"
        Title[person_idx] = _t

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

    income_raw = generate_correlated_income(
        rng, Education, Occupation, person_mask, N,
        age_bracket=person_age_bracket, region=Region,
    )
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
            / max(INCOME_NORM_REF - INCOME_MIN, 1)
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

    return {
        "MaritalStatus": MaritalStatus,
        "Title": Title,
        "Education": Education,
        "Occupation": Occupation,
        "YearlyIncome": YearlyIncome,
        "income_norm": income_norm,
        "IncomeGroup": IncomeGroup,
        "children_raw": children_raw,
        "TotalChildren": TotalChildren,
        "AgeGroup": AgeGroup,
        "CreditScore": CreditScore,
        "HomeOwnership": HomeOwnership,
        "NumberOfCars": NumberOfCars,
    }


def _build_engagement_profile(
    *,
    rng, N, Region, IsOrg, person_mask, n_person, ages_years,
    end_date, _end_dt64, CustomerStartDate, income_norm, CustomerWeight,
    CustomerSatisfactionScore, churn_norm, LoyaltyTierKey, tier_keys,
    T, CustomerStartMonth,
) -> Dict[str, np.ndarray]:
    """Build the engagement / digital / financial / CX columns (all profile-only).

    A pure function of the already-computed identity, demographic and lifecycle
    arrays: every output feeds CustomerProfile and nothing else (no feedback into
    the identity/household/SCD2 flow), so it is safe to compute in isolation.
    Returns a dict of full-length (N) arrays.
    """
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
    for area, (lo, hi) in CUSTOMER_DISTANCE_BY_AREA.items():
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
        # Ramp digital affinity down across the full adult age span (18..max age);
        # divisor tracks AGE_MAX_YEARS so the 70-85 cohort keeps a gradient
        # instead of all saturating at the floor.
        _age_span = max(AGE_MAX_YEARS - 18, 1)
        age_young[person_mask] = np.clip(
            1.0 - (ages_years[person_mask] - 18) / _age_span, 0.1, 0.95
        )
    org_digital = np.full(N, 0.75)
    young_factor = np.where(IsOrg, org_digital, age_young)

    HasOnlineAccount = rng.random(N) < (0.55 + 0.30 * young_factor)
    SocialMediaFollower = rng.random(N) < (0.20 + 0.40 * young_factor)
    AppInstalled = HasOnlineAccount & (rng.random(N) < (0.30 + 0.35 * young_factor))

    # Per-channel marketing consent. A shared receptiveness gate makes the three
    # channels positively correlated; email is granted most often, then SMS, then call.
    _marketing_consent = rng.random(N) < CUSTOMER_MARKETING_CONSENT_BASE
    ConsentEmail = _marketing_consent & (rng.random(N) < CUSTOMER_CONSENT_EMAIL_RATE)
    ConsentSMS = _marketing_consent & (rng.random(N) < CUSTOMER_CONSENT_SMS_RATE)
    ConsentCall = _marketing_consent & (rng.random(N) < CUSTOMER_CONSENT_CALL_RATE)

    NewsletterFrequency = np.where(
        ~ConsentEmail,
        "None",
        rng.choice(CUSTOMER_NEWSLETTER_FREQ_LABELS, size=N, p=CUSTOMER_NEWSLETTER_FREQ_PROBS),
    )

    DevicePreference = np.empty(N, dtype=object)
    young_mask = young_factor > 0.5
    n_young = int(young_mask.sum())
    n_old = N - n_young
    if n_young:
        DevicePreference[young_mask] = rng.choice(
            CUSTOMER_DEVICE_PREF_LABELS, size=n_young, p=CUSTOMER_DEVICE_PROBS_YOUNG,
        )
    if n_old:
        DevicePreference[~young_mask] = rng.choice(
            CUSTOMER_DEVICE_PREF_LABELS, size=n_old, p=CUSTOMER_DEVICE_PROBS_OLD,
        )

    days_back = rng.integers(0, 180, size=N)
    LastWebVisitDate = pd.to_datetime(end_date) - pd.to_timedelta(days_back, unit="D")
    LastWebVisitDate = LastWebVisitDate.to_numpy("datetime64[ns]")

    # --- Financial & Behavioral ---
    PreferredPaymentMethod = rng.choice(
        CUSTOMER_PAYMENT_METHOD_LABELS, size=N, p=CUSTOMER_PAYMENT_METHOD_PROBS,
    )

    start_dates_ts = pd.to_datetime(CustomerStartDate)
    days_after_start = rng.integers(0, 90, size=N)
    MemberSinceDate = (start_dates_ts + pd.to_timedelta(days_after_start, unit="D")).to_numpy("datetime64[ns]")
    MemberSinceDate = np.minimum(MemberSinceDate, _end_dt64)

    IsEmployee = rng.random(N) < 0.02
    IsEmployee[IsOrg] = False

    spend_score = np.clip(
        0.4 * _robust_unit_norm(CustomerWeight, _WEIGHT_NORM_REF)
        + 0.4 * income_norm
        + 0.2 * rng.random(N),
        0, 1,
    )
    # Fixed cut points on spend_score (calibrated to ~40/35/20/5), like
    # IncomeGroup / AgeGroup. The normalization above is data-independent, so a
    # customer's tier is the same regardless of which parallel chunk produced it.
    AnnualSpendBucket = SPEND_BUCKET_LABELS[
        np.searchsorted(SPEND_BUCKET_EDGES, spend_score)
    ]

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

    return {
        "UrbanRural": UrbanRural,
        "TimeZone": TimeZone,
        "DistanceToNearestStoreKm": DistanceToNearestStoreKm,
        "PreferredLanguage": PreferredLanguage,
        "HasOnlineAccount": HasOnlineAccount,
        "ConsentEmail": ConsentEmail,
        "ConsentSMS": ConsentSMS,
        "ConsentCall": ConsentCall,
        "SocialMediaFollower": SocialMediaFollower,
        "AppInstalled": AppInstalled,
        "NewsletterFrequency": NewsletterFrequency,
        "DevicePreference": DevicePreference,
        "LastWebVisitDate": LastWebVisitDate,
        "PreferredPaymentMethod": PreferredPaymentMethod,
        "MemberSinceDate": MemberSinceDate,
        "IsEmployee": IsEmployee,
        "AnnualSpendBucket": AnnualSpendBucket,
        "HasGiftCardBalance": HasGiftCardBalance,
        "RewardPointsBalance": RewardPointsBalance,
        "AvgOrderFrequencyDays": AvgOrderFrequencyDays,
        "NPS": NPS,
        "CustomerLifetimeValue": CustomerLifetimeValue,
        "ChurnRisk": ChurnRisk,
    }


# ---------------------------------------------------------
# Main generator
# ---------------------------------------------------------
def _append_gender_code(customers_df: pd.DataFrame) -> None:
    """Insert the single-char GenderCode (M/F/O) immediately after the readable
    Gender column, in place. Gender itself is left untouched (Male/Female/Org).
    Called at each build path's output boundary, after all post-phases."""
    gender_pos = customers_df.columns.get_loc("Gender")
    customers_df.insert(
        gender_pos + 1,
        "GenderCode",
        customers_df["Gender"].map(CUSTOMER_GENDER_CODE_MAP),
    )


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

    geo_lookup = _geo_lookup_with_iso(geography)

    # Per-region geography pools (keys + population weights) so customers are
    # assigned to cities in their own region. Same helper drives SCD2 relocation
    # so initial assignment and life-event moves can never drift apart.
    _geo_region_pools = _build_region_pools(geography)

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

    # --- names via name_pools (First/Middle/Last emitted as separate columns) ---
    FirstName, LastName, MiddleInitial = assign_person_names(
        keys=CustomerKey,
        region=Region,
        gender=Gender,
        is_org=IsOrg,
        pools=people_pools,
        seed=int(seed),
        include_middle=True,
        default_region="US",
    )
    safe_first = np.where(pd.isna(FirstName), "", FirstName.astype(object))
    safe_last = np.where(pd.isna(LastName), "", LastName.astype(object))

    # MiddleName is sparse — most records carry no middle name. Keep the
    # deterministic initial for ~CUSTOMER_MIDDLE_NAME_RATE of persons, null the rest
    # (and always null for orgs).
    MiddleName = MiddleInitial.astype(object).copy()
    MiddleName[rng.random(N) >= CUSTOMER_MIDDLE_NAME_RATE] = None
    MiddleName[IsOrg] = None

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
    # Email (suffixed with CustomerKey for uniqueness). In the parallel path the
    # chunk-local key collides across chunks, so the orchestrator rebuilds this
    # against the global key after merge (CUST-AN-9 / build_email_addresses).
    # -----------------------------------------------------
    Email = build_email_addresses(
        rng, safe_first=safe_first, safe_last=safe_last,
        org_name=OrgName, keys=CustomerKey, is_org=IsOrg,
    )

    # -----------------------------------------------------
    # FullName (display name; org name for organizations)
    # -----------------------------------------------------
    FullName = np.where(
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
        ages_yr = rng.triangular(
            AGE_MIN_YEARS, AGE_MODE_YEARS, AGE_MAX_YEARS, size=n_person,
        )
        ages_days[person_mask] = np.rint(ages_yr * 365.25).astype("int64")
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

    # --- Conditioned demographics (marital, title, education, income, etc.) ---
    _demo = _build_demographics(
        rng=rng, N=N, IsOrg=IsOrg, Region=Region, Gender=Gender,
        person_mask=person_mask, person_idx=person_idx, n_person=n_person,
        person_age_bracket=person_age_bracket, ages_years=ages_years,
    )
    MaritalStatus = _demo["MaritalStatus"]
    Title = _demo["Title"]
    Education = _demo["Education"]
    Occupation = _demo["Occupation"]
    YearlyIncome = _demo["YearlyIncome"]
    income_norm = _demo["income_norm"]
    IncomeGroup = _demo["IncomeGroup"]
    children_raw = _demo["children_raw"]
    TotalChildren = _demo["TotalChildren"]
    AgeGroup = _demo["AgeGroup"]
    CreditScore = _demo["CreditScore"]
    HomeOwnership = _demo["HomeOwnership"]
    NumberOfCars = _demo["NumberOfCars"]

    PhoneNumber = generate_phone_numbers(rng, N)

    ReferralSource = rng.choice(
        CUSTOMER_REFERRAL_SOURCE_LABELS, size=N, p=CUSTOMER_REFERRAL_SOURCE_PROBS,
    )

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

    CustomerWeight = rng.lognormal(mean=0.0, sigma=WEIGHT_SIGMA, size=N).astype("float64")
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
    # Clamp to end_date so customers never appear to start past the timeline
    # (CustomerStartMonth=T-1 + day_offset can otherwise overshoot a mid-month end_date)
    _end_dt64 = np.datetime64(end_date.normalize().to_datetime64(), "ns")
    CustomerStartDate = np.minimum(CustomerStartDate, _end_dt64)

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
            CustomerEndDate[has_end] = np.minimum(end_base + end_offsets, _end_dt64)
    else:
        CustomerEndDate = np.empty(N, dtype="datetime64[ns]")
        CustomerEndDate[:] = np.datetime64("NaT")

    CustomerType = np.where(IsOrg, "Organization", "Individual")
    CompanyName = np.where(IsOrg, OrgName, None)

    # -----------------------------------------------------
    # Customer satisfaction (correlated with churn bias)
    # -----------------------------------------------------
    churn_norm = _robust_unit_norm(CustomerChurnBias, lognormal_p95_ref(bias_sigma))
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
    # BirthCity: most customers were born in their current city; a relocated
    # minority were born in a *different city of the same country* (no
    # cross-country births from a global permutation).
    BirthCity = geo_city.copy()
    relocated = rng.random(N) < 0.25
    if relocated.any():
        country_to_cities = _build_country_city_pools(geography)
        for ctry in np.unique(geo_country[relocated]):
            m = relocated & (geo_country == ctry)
            pool = country_to_cities.get(ctry)
            if pool is not None and len(pool) > 0:
                BirthCity[m] = rng.choice(pool, size=int(m.sum()))

    # --- Address columns (street line only; City/State come from GeographyKey) ---
    HomeAddress, WorkAddress = generate_addresses(rng, CustomerKey, N)
    PostalCode = generate_postal_codes(rng, geo_country, N)

    # --- Lat/Lon: actual city centroid (geography dim) + small jitter ---
    geo_lat = geo_mapped["Latitude"].to_numpy(dtype="float64")
    geo_lon = geo_mapped["Longitude"].to_numpy(dtype="float64")
    Latitude = np.round(geo_lat + rng.uniform(-_CITY_LATLON_JITTER, _CITY_LATLON_JITTER, size=N), 4)
    Longitude = np.round(geo_lon + rng.uniform(-_CITY_LATLON_JITTER, _CITY_LATLON_JITTER, size=N), 4)

    # --- Engagement / digital / financial / CX columns (all profile-only) ---
    _eng = _build_engagement_profile(
        rng=rng, N=N, Region=Region, IsOrg=IsOrg, person_mask=person_mask,
        n_person=n_person, ages_years=ages_years, end_date=end_date, _end_dt64=_end_dt64,
        CustomerStartDate=CustomerStartDate, income_norm=income_norm,
        CustomerWeight=CustomerWeight, CustomerSatisfactionScore=CustomerSatisfactionScore,
        churn_norm=churn_norm, LoyaltyTierKey=LoyaltyTierKey, tier_keys=tier_keys,
        T=T, CustomerStartMonth=CustomerStartMonth,
    )
    UrbanRural = _eng["UrbanRural"]
    TimeZone = _eng["TimeZone"]
    DistanceToNearestStoreKm = _eng["DistanceToNearestStoreKm"]
    PreferredLanguage = _eng["PreferredLanguage"]
    HasOnlineAccount = _eng["HasOnlineAccount"]
    ConsentEmail = _eng["ConsentEmail"]
    ConsentSMS = _eng["ConsentSMS"]
    ConsentCall = _eng["ConsentCall"]
    SocialMediaFollower = _eng["SocialMediaFollower"]
    AppInstalled = _eng["AppInstalled"]
    NewsletterFrequency = _eng["NewsletterFrequency"]
    DevicePreference = _eng["DevicePreference"]
    LastWebVisitDate = _eng["LastWebVisitDate"]
    PreferredPaymentMethod = _eng["PreferredPaymentMethod"]
    MemberSinceDate = _eng["MemberSinceDate"]
    IsEmployee = _eng["IsEmployee"]
    AnnualSpendBucket = _eng["AnnualSpendBucket"]
    HasGiftCardBalance = _eng["HasGiftCardBalance"]
    RewardPointsBalance = _eng["RewardPointsBalance"]
    AvgOrderFrequencyDays = _eng["AvgOrderFrequencyDays"]
    NPS = _eng["NPS"]
    CustomerLifetimeValue = _eng["CustomerLifetimeValue"]
    ChurnRisk = _eng["ChurnRisk"]

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

        # Copy head's home address columns to household members. The region- and
        # geography-derived profile columns (UrbanRural/TimeZone/language/distance)
        # are refreshed too, so a member that moved into the head's address no
        # longer keeps a stale pre-move region's timezone/language.
        moved, head_of = head_indices_for_members(HouseholdKey, HouseholdRole)
        if moved.any():
            for arr in (Region, HomeAddress, PostalCode, Latitude, Longitude,
                        geo_city, geo_state, CurrentCity,
                        UrbanRural, TimeZone, PreferredLanguage,
                        DistanceToNearestStoreKm):
                arr[moved] = arr[head_of]

        # Dependents (18-24, recruited into a head's household) are students /
        # early-career; cap their income at an entry-level ceiling and refresh
        # IncomeGroup so a dependent no longer carries a full adult salary.
        YearlyIncome, IncomeGroup = _cap_dependent_income(
            HouseholdRole, YearlyIncome, IncomeGroup,
        )

        # assign_households may have flipped Single spouses to Married; keep the
        # salutation in step (Ms -> Mrs) so Title matches the final MaritalStatus.
        _reconcile_titles_with_marital(Title, MaritalStatus)

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
            "Title": Title,
            "FirstName": FirstName,
            "MiddleName": MiddleName,
            "LastName": LastName,
            "FullName": FullName,
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
            # --- Demand weight (drives weighted customer sampling in sales) ---
            "CustomerBaseWeight": CustomerWeight.astype(np.float64),
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

    if _skip_post_phases:
        # Transient columns piped through to the parallel orchestrator so
        # household assignment + org_profile see the real chunk-time values
        # instead of fresh random draws / np.ones approximations. Stripped
        # from customers_df before the final parquet write.
        customers_df["_Region"] = Region
        customers_df["_CustomerChurnBias"] = CustomerChurnBias.astype(np.float64)

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
            "ConsentEmail": ConsentEmail[person_mask],
            "ConsentSMS": ConsentSMS[person_mask],
            "ConsentCall": ConsentCall[person_mask],
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
            _scd2_region_pools = _build_region_pools(geography)
            customers_df = generate_scd2_versions(
                rng=rng,
                base_df=customers_df,
                cust_cfg=scd2_cfg,
                geo_keys=geo_keys,
                tier_keys=tier_keys,
                end_date=end_date,
                geo_lookup=geo_lookup,
                region_pools=_scd2_region_pools,
            )

            _remap_profiles_to_current_version(customers_df, profile_df, org_profile_df)

    if not _skip_post_phases:
        # GenderCode is derived only on the full (serial) path. Chunk workers
        # (_skip_post_phases=True) keep the readable labels so the orchestrator's
        # spouse-matching still keys on Male/Female after merge; the orchestrator
        # adds GenderCode once post-merge.
        _append_gender_code(customers_df)

    active_customer_set = set(active_customer_keys.tolist())
    return customers_df, profile_df, org_profile_df, active_customer_set


# ---------------------------------------------------------
# Parallel orchestrator
# ---------------------------------------------------------
def _generate_parallel(cfg, parquet_dims_folder: Path, n_workers: int):
    """Generate customers in parallel: chunk → merge → households → SCD2."""
    from src.utils.pool import PoolRunSpec, iter_imap_unordered
    from src.dimensions.customers.worker import customer_chunk_worker, scd2_chunk_worker

    cust_cfg = cfg.customers
    N = int(cust_cfg.total_customers)
    seed = resolve_seed(cfg, cust_cfg, fallback=42)

    start_date, end_date = parse_cfg_dates(cfg)

    # Serialize config for workers (must be picklable plain dict)
    cfg_dump = cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg)
    cfg_dump.pop("_config_snapshot", None)
    cfg_dump.pop("_models_snapshot", None)

    # Chunk partitioning. Deliberately independent of n_workers so the same
    # (seed, N, config) yields the same customer population regardless of how
    # many workers run it (CUST-AN-7) — chunk count drives the per-chunk RNG
    # streams, so tying it to worker count made --workers silently change the
    # data. The pool size adapts separately via n_actual_workers.
    n_chunks = _customer_chunk_count(N)
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

        # Rebuild EmailAddress against the now-global, unique CustomerKey. Chunk
        # workers baked emails with chunk-local keys, so name+key collided across
        # chunks and unique emails saturated at ~one chunk's worth (CUST-AN-9).
        email_rng = np.random.default_rng(
            np.random.SeedSequence(seed).spawn(n_chunks + 3)[n_chunks + 2]
        )
        _ef = customers_df["FirstName"].to_numpy(dtype=object)
        _el = customers_df["LastName"].to_numpy(dtype=object)
        customers_df["EmailAddress"] = build_email_addresses(
            email_rng,
            safe_first=np.where(pd.isna(_ef), "", _ef),
            safe_last=np.where(pd.isna(_el), "", _el),
            org_name=customers_df["CompanyName"].to_numpy(dtype=object),
            keys=customers_df["CustomerKey"].to_numpy(),
            is_org=(customers_df["CustomerType"] == "Organization").to_numpy(),
        )

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
        # assign_households marks matched spouses as Married in place; persist it
        # and keep Title (Ms -> Mrs) in step with the final MaritalStatus.
        customers_df["MaritalStatus"] = MaritalStatus
        _title = customers_df["Title"].to_numpy(dtype=object)
        _reconcile_titles_with_marital(_title, MaritalStatus)
        customers_df["Title"] = _title

        # Recover real per-customer Region and ChurnBias emitted by chunk workers
        # (transient columns prefixed with `_`). Stripped from customers_df below
        # before the final parquet write.
        Region = customers_df["_Region"].to_numpy(dtype=object)
        CustomerChurnBias = customers_df["_CustomerChurnBias"].to_numpy(dtype=np.float64)
        enable_asia = bool((Region == "AS").any())

        geography, _ = load_dimension("geography", parquet_dims_folder, cfg.geography)
        geo_lookup = _geo_lookup_with_iso(geography)

        # Copy head's home address columns to household members
        moved, head_of = head_indices_for_members(HouseholdKey, HouseholdRole)
        N_full = len(customers_df)
        if moved.any():
            Region[moved] = Region[head_of]
            for col in ("HomeAddress", "PostalCode", "Latitude", "Longitude"):
                vals = customers_df[col].to_numpy().copy()
                vals[moved] = vals[head_of]
                customers_df[col] = vals
            # CurrentCity and the region/geography-derived profile columns live in
            # profile_df (person-only). Rebuild each in full-N space, copy head ->
            # member, then re-extract persons so moved members no longer keep a
            # stale pre-move timezone/language/distance.
            person_mask_p = (customers_df["CustomerType"] != "Organization").to_numpy()
            full_city = geo_lookup["City"].reindex(
                customers_df["GeographyKey"]
            ).to_numpy(dtype=object)
            full_city[moved] = full_city[head_of]
            profile_df["CurrentCity"] = full_city[person_mask_p]
            for col in ("UrbanRural", "TimeZone", "PreferredLanguage",
                        "DistanceToNearestStoreKm"):
                src = profile_df[col].to_numpy()
                full = np.empty(N_full, dtype=src.dtype)
                full[person_mask_p] = src
                full[moved] = full[head_of]
                profile_df[col] = full[person_mask_p]

        # Dependents are students / early-career; cap their income and refresh
        # IncomeGroup (same as the serial path).
        yi_new, ig_new = _cap_dependent_income(
            HouseholdRole,
            customers_df["YearlyIncome"].array,
            customers_df["IncomeGroup"].to_numpy().copy(),
        )
        customers_df["YearlyIncome"] = yi_new
        customers_df["IncomeGroup"] = ig_new

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

        # Drop chunk-time transient columns; downstream SCD2 + final write
        # should never see them.
        customers_df = customers_df.drop(columns=["_Region", "_CustomerChurnBias"])

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
                scd2_region_pools = _build_region_pools(geography)

                # Parallelize SCD2 when there are enough changed customers. The
                # decision and the chunk count depend only on the changed-row
                # count (not the worker count), so the version history is the
                # same across --workers; the pool size adapts separately.
                if len(changed_df) > SCD2_PARALLEL_THRESHOLD:
                    n_scd2_chunks = _scd2_chunk_count(len(changed_df))
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
                            scd2_region_pools,
                        ))

                    scd2_spec = PoolRunSpec(
                        processes=min(n_actual_workers, n_scd2_chunks),
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
                        region_pools=scd2_region_pools,
                    )
                    expanded_df = pd.concat([unchanged_df, expanded_rows], ignore_index=True)

                expanded_df["CustomerKey"] = np.arange(1, len(expanded_df) + 1, dtype="int64")
                customers_df = expanded_df

                n_versions = len(customers_df) - N
                info(f"SCD2: {n_change} customers expanded, {n_versions} version rows added ({len(customers_df)} total)")

                _remap_profiles_to_current_version(customers_df, profile_df, org_profile_df)

    finally:
        import shutil
        shutil.rmtree(scratch_dir, ignore_errors=True)

    # GenderCode is derived after all post-phases (household matching consumed the
    # readable Gender labels above; SCD2 carried them through).
    _append_gender_code(customers_df)

    # Parallel path doesn't track an active-customer set — sales derives
    # it independently from active_ratio + seed (see sales.py). Returning
    # an empty set keeps the 4-tuple shape consistent with the serial path.
    return customers_df, profile_df, org_profile_df, set()


# ---------------------------------------------------------
# Pipeline entry
# ---------------------------------------------------------
def run_customers(cfg: Dict, parquet_folder: Path):
    out_path = parquet_folder / "customers.parquet"
    profile_out_path = parquet_folder / "customer_profile.parquet"
    org_profile_out_path = parquet_folder / "organization_profile.parquet"

    cust_cfg = cfg.customers

    version_cfg = dict(cust_cfg)
    version_cfg["_schema_version"] = 8
    version_cfg["_has_loyalty_tier"] = True
    version_cfg["_has_acquisition_channel"] = True
    version_cfg["_has_customer_profile"] = True
    version_cfg["_has_org_profile"] = True
    version_cfg["_has_scd2"] = True
    version_cfg["_has_customer_base_weight"] = True
    version_cfg["_has_region_weighted_relocation"] = True

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
