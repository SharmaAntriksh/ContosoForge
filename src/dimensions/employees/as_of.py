"""Point-in-time helpers for EmployeeStoreAssignments.

Assignment ``StartDate`` and ``EndDate`` are inclusive. These helpers keep that
contract in one place so analytical consumers do not need to recreate temporal
filters or rely on dataset-end flags for historical questions.
"""
from __future__ import annotations

import pandas as pd

from src.exceptions import DimensionError


def assignments_as_of(
    assignments: pd.DataFrame,
    as_of_date: pd.Timestamp | str,
) -> pd.DataFrame:
    """Return the single effective assignment per employee on ``as_of_date``."""
    required = {"EmployeeKey", "StartDate", "EndDate"}
    missing = sorted(required - set(assignments.columns))
    if missing:
        raise DimensionError(f"assignment frame missing required columns: {missing}")

    as_of = pd.Timestamp(as_of_date).normalize()
    start = pd.to_datetime(assignments["StartDate"], errors="coerce").dt.normalize()
    end = pd.to_datetime(assignments["EndDate"], errors="coerce").dt.normalize()
    effective = assignments.loc[(start <= as_of) & (end >= as_of)].copy()

    duplicates = effective["EmployeeKey"].duplicated(keep=False)
    if duplicates.any():
        employee_keys = sorted(
            effective.loc[duplicates, "EmployeeKey"].astype(int).unique().tolist()
        )
        raise DimensionError(
            "multiple effective assignments found for employee(s) "
            f"{employee_keys[:10]} on {as_of.date()}"
        )
    return effective


def current_assignments(assignments: pd.DataFrame) -> pd.DataFrame:
    """Return rows explicitly marked as the dataset-end current assignment."""
    required = {"EmployeeKey", "IsCurrentAssignment"}
    missing = sorted(required - set(assignments.columns))
    if missing:
        raise DimensionError(
            f"assignment frame missing required columns: {missing}"
        )
    current = assignments.loc[assignments["IsCurrentAssignment"].astype(bool)].copy()
    duplicates = current["EmployeeKey"].duplicated(keep=False)
    if duplicates.any():
        raise DimensionError("multiple current assignments found for an employee")
    missing_current = set(assignments["EmployeeKey"].unique()) - set(
        current["EmployeeKey"].unique()
    )
    if missing_current:
        raise DimensionError(
            "no current assignment found for employee(s) "
            f"{sorted(int(key) for key in missing_current)[:10]}"
        )
    return current


__all__ = ["assignments_as_of", "current_assignments"]
