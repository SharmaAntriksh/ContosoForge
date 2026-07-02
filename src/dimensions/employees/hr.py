"""Employee HR-column enrichment, identity-key validation, and integer casts.

``_enrich_employee_hr_columns`` adds the Contoso-like HR attributes (BirthDate,
MaritalStatus, contact info, compensation, vacation, status, department).
``_assert_identity_keys`` guards against silently coercing a corrupt NaN key to
0, and ``_finalize_employee_integer_cols`` forces the integer output dtypes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.defaults import (
    EMPLOYEE_TERMINATION_REASON_LABELS,
    EMPLOYEE_TERMINATION_REASON_PROBS,
    ONLINE_SALES_REP_ROLE,
)


def _enrich_employee_hr_columns(
    df: pd.DataFrame,
    rng: np.random.Generator,
    global_end: pd.Timestamp,
    email_domain: str = "contoso.com",
    primary_sales_role: str = "Sales Associate",
) -> pd.DataFrame:
    """
    Adds Contoso-like HR columns.

    Assumes *df* already has: EmployeeKey, Title, OrgLevel, HireDate,
    TerminationDate, IsActive, and deterministic name columns.
    """
    n = len(df)
    if n == 0:
        return df

    hire = pd.to_datetime(df["HireDate"]).dt.normalize()
    org_level = df["OrgLevel"].astype(int).to_numpy()
    title = df["Title"].astype(str)

    # BirthDate: age-at-hire varies by level (staff younger, management older)
    age_mean = np.where(org_level >= 6, 27, np.where(org_level >= 5, 34, 42))
    ages = np.clip(rng.normal(loc=age_mean, scale=6.0, size=n), 18, 62).astype(int)
    birth_year = hire.dt.year.to_numpy() - ages
    birth_month = rng.integers(1, 13, size=n)
    dim = (
        pd.to_datetime({"year": birth_year, "month": birth_month, "day": np.ones(n, dtype=int)})
        .dt.days_in_month.to_numpy()
    )
    birth_day = np.minimum((rng.random(n) * dim).astype(int) + 1, dim)
    df["BirthDate"] = pd.to_datetime(
        {"year": birth_year, "month": birth_month, "day": birth_day}
    ).dt.normalize()
    # Guarantee >= 18 years old at HireDate: birth_year is a whole-year
    # subtraction, so a row with age == 18 whose random (month, day) falls after
    # the hire's (month, day) would be only 17 at the hire date. Clamp such rows
    # to exactly 18-at-hire. Vectorized, NaT-safe, and consumes no RNG so every
    # subsequent random column stays byte-identical.
    _min_birth = hire - pd.DateOffset(years=18)
    df["BirthDate"] = df["BirthDate"].where(df["BirthDate"] <= _min_birth, _min_birth)

    is_married = rng.random(n) < np.clip((ages - 22) / 25.0, 0.05, 0.75)
    df["MaritalStatus"] = np.where(is_married, "M", "S").astype(object)

    # Email / Phone
    email_local = (
        df["FirstName"].str.lower().str.replace(" ", "", regex=False)
        + "."
        + df["LastName"].str.lower().str.replace(" ", "", regex=False)
        + "."
        + df["EmployeeKey"].astype("Int32").astype(str)
    )
    df["EmailAddress"] = (email_local + "@" + str(email_domain)).astype(str)

    phone_raw = rng.integers(0, 10, size=n * 10, dtype=np.uint8) + np.uint8(48)
    df["Phone"] = pd.Series(phone_raw.view("S10").astype("U10"), dtype="object")

    # Emergency contacts: pick a plausible name from the employee population
    if n > 1:
        pick = rng.integers(0, n - 1, size=n)
        self_idx = np.arange(n)
        # Shift picks >= self to avoid self-selection (uniform over n-1 others)
        pick = np.where(pick >= self_idx, pick + 1, pick)
        df["EmergencyContactName"] = (
            df["FirstName"].iloc[pick].to_numpy(dtype=object)
            + " "
            + df["LastName"].iloc[pick].to_numpy(dtype=object)
        )
    else:
        self_name = df["FirstName"].iloc[0] + " " + df["LastName"].iloc[0] + " (Self)"
        df["EmergencyContactName"] = pd.Series([self_name], dtype="object")
    ec_raw = rng.integers(0, 10, size=n * 10, dtype=np.uint8) + np.uint8(48)
    df["EmergencyContactPhone"] = pd.Series(ec_raw.view("S10").astype("U10"), dtype="object")

    # Compensation
    salaried = (df["OrgLevel"].astype(int) <= 5).to_numpy()
    df["SalariedFlag"] = salaried.astype(bool)
    df["PayFrequency"] = np.where(salaried, 1, 2).astype(np.int32)

    hourly_staff = np.clip(rng.normal(loc=18.0, scale=4.0, size=n), 10.0, 40.0)
    annual_salary = np.clip(rng.normal(loc=70000.0, scale=18000.0, size=n), 38000.0, 160000.0)
    hourly_equiv = annual_salary / 2080.0
    df["BaseRate"] = np.where(salaried, hourly_equiv, hourly_staff).round(2).astype(np.float64)

    # VacationHours: tenure-based
    tenure_days = (global_end.normalize() - hire).dt.days.clip(lower=0)
    base_vac = np.where(salaried, 80, 40) + (tenure_days / 365.0 * np.where(salaried, 6.0, 3.0))
    df["VacationHours"] = np.clip(
        base_vac + rng.normal(0, 10, size=n), 0, 240,
    ).round(0).astype(np.int32)

    # Status
    df["Status"] = np.where(
        df["IsActive"].astype(int) == 1, "Active", "Terminated",
    ).astype(object)

    # TerminationReason (only for terminated employees)
    # Preserve reasons already set by store closures; assign a random reason for the rest
    term_mask = df["TerminationDate"].notna() & (df["IsActive"].astype(int) == 0)
    if "TerminationReason" not in df.columns:
        df["TerminationReason"] = pd.array([pd.NA] * len(df), dtype="object")
    needs_reason = term_mask & df["TerminationReason"].isna()
    n_needs = int(needs_reason.sum())
    if n_needs > 0:
        reasons = rng.choice(
            EMPLOYEE_TERMINATION_REASON_LABELS,
            size=n_needs,
            p=EMPLOYEE_TERMINATION_REASON_PROBS,
        )
        df.loc[needs_reason, "TerminationReason"] = reasons

    # IsSalesperson
    df["IsSalesperson"] = title.isin([primary_sales_role, ONLINE_SALES_REP_ROLE]).astype(bool)

    # DepartmentName
    dept = np.where(
        title.isin([primary_sales_role, "Store Manager"]),
        "Sales",
        np.where(
            title.isin([ONLINE_SALES_REP_ROLE]),
            "Online Sales",
            np.where(
                title.isin(["Cashier"]),
                "Store Operations",
                np.where(
                    title.isin(["Stock Associate"]),
                    "Inventory",
                    np.where(title.isin(["Fulfillment Associate"]), "Fulfillment", "Corporate"),
                ),
            ),
        ),
    )
    df["DepartmentName"] = pd.Series(dept, dtype="object")

    return df


def _assert_identity_keys(df: pd.DataFrame) -> None:
    """Raise on a NaN identity key rather than silently coercing it to 0.

    A null ``EmployeeKey`` or (for store-level rows) ``StoreKey`` indicates
    upstream corruption; ``fillna(0)`` would hide it behind a duplicate 0 key
    and break every key-band decode and FK join. Corporate/region/district
    rows (``OrgUnitType != "Store"``) legitimately have no store, so their NaN
    ``StoreKey`` is left for the intentional 0-fill.
    """
    if df.empty:
        return
    ek = pd.to_numeric(df["EmployeeKey"], errors="coerce")
    if ek.isna().any():
        raise DimensionError(
            f"{int(ek.isna().sum())} employee row(s) have a null/non-numeric "
            "EmployeeKey; an identity key cannot be NaN (upstream corruption). "
            "Refusing to coerce to 0."
        )
    if "OrgUnitType" in df.columns and "StoreKey" in df.columns:
        store_row = df["OrgUnitType"].astype(str) == "Store"
        bad_sk = store_row & pd.to_numeric(df["StoreKey"], errors="coerce").isna()
        if bad_sk.any():
            raise DimensionError(
                f"{int(bad_sk.sum())} store-level employee row(s) have a "
                "null/non-numeric StoreKey; a store employee must reference a "
                "concrete store. Refusing to coerce to 0."
            )


def _finalize_employee_integer_cols(df: pd.DataFrame) -> pd.DataFrame:
    """
    Force specific columns to integer types in parquet output.

    Power BI / Power Query sometimes infers decimal types when a column
    contains nulls.  ParentEmployeeKey stays nullable for DAX ``PATH()``
    semantics; other columns use 0 for corporate-level rows.
    """
    if df.empty:
        return df

    def _to_int(col: str, dtype) -> None:
        if col not in df.columns:
            return
        s = df[col]
        if pd.api.types.is_bool_dtype(s):
            s = s.astype(np.int32)
        s = pd.to_numeric(s, errors="coerce")
        df[col] = s.fillna(0).astype(dtype)

    _to_int("EmployeeKey", np.int32)
    if "ParentEmployeeKey" in df.columns:
        df["ParentEmployeeKey"] = pd.to_numeric(
            df["ParentEmployeeKey"], errors="coerce",
        ).astype("Int32")
    _to_int("OrgLevel", np.int32)
    _to_int("IsSalesperson", bool)
    _to_int("SalariedFlag", bool)
    _to_int("IsActive", bool)
    _to_int("RegionId", np.int32)
    _to_int("DistrictId", np.int32)
    _to_int("StoreKey", np.int32)
    _to_int("GeographyKey", np.int32)
    _to_int("PayFrequency", np.int32)

    return df
