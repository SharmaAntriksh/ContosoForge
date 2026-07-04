"""Deterministic employee name assignment.

Stable Gender + region-aware First/Last/Middle/EmployeeName per EmployeeKey,
drawn from the shared name pools (no RNG — everything is hash-derived so the
names are reproducible independent of draw order).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.utils.name_pools import assign_person_names, hash_u64
from src.utils.config_helpers import region_from_iso_code


def _apply_deterministic_names(
    df: pd.DataFrame,
    seed: int,
    *,
    people_pools,
    iso_by_geo: dict[int, str] | None = None,
    default_region: str = "US",
) -> None:
    """
    Stable names per EmployeeKey using shared name pools.

    Assigns deterministic Gender (Male/Female) and region-aware first/last/middle names.
    """
    if people_pools is None:
        raise DimensionError(
            "people_pools is required for employee name generation. "
            "Ensure name pool CSV files exist under the configured people folder."
        )

    ek = df["EmployeeKey"].astype(np.int32).to_numpy()
    ek_u64 = ek.astype(np.uint64)

    # Deterministic Gender distribution based on hash. Employees are persons:
    # Gender is the readable label Male/Female (no Other, no single-char codes).
    from src.defaults import EMPLOYEE_GENDER_PROBS
    p_female = EMPLOYEE_GENDER_PROBS["female"]
    h = hash_u64(ek_u64, int(seed), 9101)
    u = (h % np.uint64(10_000)).astype(np.float64) / 10_000.0
    gender_label = np.where(u < p_female, "Female", "Male").astype(object)
    df["Gender"] = gender_label

    # Region per row from GeographyKey → ISOCode → region code
    if "GeographyKey" in df.columns and iso_by_geo:
        gk = pd.to_numeric(df["GeographyKey"], errors="coerce").fillna(-1).astype(np.int32).to_numpy()
        iso = np.array(
            [iso_by_geo.get(int(k), "") if k >= 0 else "" for k in gk],
            dtype=object,
        )
        region = np.array(
            [region_from_iso_code(x, default_region) if x else default_region for x in iso],
            dtype=object,
        )
    else:
        region = np.full(len(df), default_region, dtype=object)

    first, last, mid = assign_person_names(
        keys=ek,
        region=region,
        gender=gender_label,
        is_org=np.zeros(len(df), dtype=bool),
        pools=people_pools,
        seed=int(seed),
        include_middle=True,
        default_region=default_region,
    )

    df["FirstName"] = pd.Series(first, dtype="object").astype(str)
    df["LastName"] = pd.Series(last, dtype="object").astype(str)
    df["MiddleName"] = pd.Series(mid, dtype="object").astype(str)
    df["EmployeeName"] = df["FirstName"] + " " + df["LastName"]
