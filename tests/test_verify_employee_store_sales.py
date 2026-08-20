"""Regression tests for the standalone employee/store/sales verifier."""
from __future__ import annotations

import numpy as np
import pandas as pd

from scripts import verify_employee_store_sales as verifier


def test_employee_classification_uses_home_store_key() -> None:
    employees = pd.DataFrame(
        {
            "EmployeeKey": [1, 2, 3],
            "Title": ["Store Manager", "Online Sales Representative", "CEO"],
            "HomeStoreKey": pd.Series([1, 10_001, pd.NA], dtype="Int32"),
        }
    )

    managers, online, store_level = verifier._classify_employee_keys(employees)

    assert managers == {1}
    assert online == {2}
    assert store_level == {1, 2}


def test_dynamic_store_closure_reason_is_valid_transition() -> None:
    assignments = pd.DataFrame(
        {
            "EmployeeKey": [7, 7],
            "StoreKey": [1, 2],
            "TransferReason": ["Initial", "Store Closure: Lease Ended"],
        }
    )
    verifier.results.clear()

    verifier._check_transfer_reason_consistency(
        assignments,
        np.array([True]),
        "ESA-Bridge",
    )

    assert verifier.results[-1].passed


def test_post_transfer_leakage_uses_assignment_status() -> None:
    assignments = pd.DataFrame(
        {
            "EmployeeKey": [7, 7],
            "StoreKey": [1, 2],
            "EndDate": pd.to_datetime(["2024-06-30", "2024-12-31"]),
            "AssignmentStatus": ["Transferred", "Active"],
        }
    )
    sales = pd.DataFrame(
        {
            "EmployeeKey": [7],
            "StoreKey": [1],
            "OrderDate": pd.to_datetime(["2024-06-30"]),
        }
    )
    verifier.results.clear()

    verifier._check_post_transfer_leakage(sales, assignments, "Sales")

    assert verifier.results[-1].passed

