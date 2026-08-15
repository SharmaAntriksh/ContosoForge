"""Phase 6 guards for the simplified workforce analytical contract."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from src.dimensions.employees.hr import _enrich_employee_hr_columns
from src.tools.sql.dialect import REGISTRY
from src.tools.sql.view_sql import render_views_sql
from src.utils.static_schemas import STATIC_SCHEMAS


_EMPLOYEE_HIDDEN = {
    "FirstName",
    "LastName",
    "MiddleName",
    "MaritalStatus",
    "EmailAddress",
    "Phone",
    "EmergencyContactName",
    "EmergencyContactPhone",
    "PayFrequency",
    "Status",
}
_STORE_HIDDEN = {
    "StoreManager",
    "StoreManagerFirstName",
    "StoreManagerLastName",
    "Phone",
    "StoreEmail",
    "StoreDescription",
}


def _column_block(text: str, column: str) -> str:
    match = re.search(
        rf"(?ms)^\tcolumn {re.escape(column)}\n(.*?)(?=^\tcolumn |^\tpartition )",
        text,
    )
    assert match is not None, f"missing column {column}"
    return match.group(1)


def test_pay_frequency_has_readable_published_label() -> None:
    frame = pd.DataFrame({
        "EmployeeKey": [1, 2],
        "Title": ["Store Manager", "Cashier"],
        "OrgLevel": [5, 6],
        "HireDate": [pd.Timestamp("2020-01-01")] * 2,
        "TerminationDate": [pd.NaT] * 2,
        "IsActive": [True, True],
        "FTE": [1.0, 1.0],
        "FirstName": ["A", "B"],
        "LastName": ["Manager", "Cashier"],
    })

    result = _enrich_employee_hr_columns(
        frame,
        np.random.default_rng(7),
        global_end=pd.Timestamp("2025-12-31"),
    )

    assert result["PayFrequency"].tolist() == [1, 2]
    assert result["PayFrequencyLabel"].tolist() == ["Monthly", "Biweekly"]
    assert "PayFrequencyLabel" in dict(STATIC_SCHEMAS["Employees"])


def test_sql_views_publish_only_the_curated_workforce_contract() -> None:
    sql = render_views_sql(
        REGISTRY["sqlserver"],
        mode="sales",
        skip_order_cols=False,
        view_schema="dbo",
    )
    curated = sql.split("-- Curated analytical dimension views", 1)[1].split(
        "-- Effective-dated workforce views", 1,
    )[0]

    def view_block(view: str) -> str:
        start = curated.index(f"[dbo].[vw_{view}] AS")
        return curated[start:curated.index(";');\nGO", start)]

    stores = view_block("Stores")
    employees = view_block("Employees")
    assignments = view_block("EmployeeStoreAssignments")

    for view in ("Stores", "Employees", "EmployeeStoreAssignments"):
        assert f"[dbo].[vw_{view}]" in curated
    assert "[PayFrequencyLabel]" in employees
    assert "[IsActive]" in employees
    assert "[Status]" in stores  # store snapshot status remains canonical

    for raw_only in _EMPLOYEE_HIDDEN:
        assert f"[{raw_only}]" not in employees
    for raw_only in _STORE_HIDDEN:
        assert f"[{raw_only}]" not in stores
    assert "[AssignmentSequence]" not in assignments


def test_all_active_powerbi_models_apply_phase6_visibility_contract() -> None:
    templates = Path(__file__).parents[1] / "samples" / "powerbi" / "templates"
    employee_files = [
        path for path in templates.rglob("Employee.tmdl")
        if "definition" in path.parts and "tables" in path.parts
    ]
    store_files = [
        path for path in templates.rglob("Store.tmdl")
        if "definition" in path.parts and "tables" in path.parts
    ]
    assignment_files = [
        path for path in templates.rglob("EmployeeStoreAssignment.tmdl")
        if "definition" in path.parts and "tables" in path.parts
    ]

    assert len(employee_files) == len(store_files) == len(assignment_files) == 6

    for path in employee_files:
        text = path.read_text(encoding="utf-8")
        for column in _EMPLOYEE_HIDDEN:
            assert "\t\tisHidden" in _column_block(text, column), (path, column)
        assert "\t\tisHidden" not in _column_block(text, "IsActive")
        assert "\t\tisHidden" not in _column_block(text, "EmployeeName")
        assert "sourceColumn: PayFrequencyLabel" in _column_block(
            text, "PayFrequencyLabel",
        )
        if "csv" in path.parts:
            assert '{"PayFrequencyLabel", type text}' in text

    for path in store_files:
        text = path.read_text(encoding="utf-8")
        for column in _STORE_HIDDEN:
            assert "\t\tisHidden" in _column_block(text, column), (path, column)
        assert "\t\tisHidden" not in _column_block(text, "Status")

    for path in assignment_files:
        text = path.read_text(encoding="utf-8")
        assert "\t\tisHidden" in _column_block(text, "AssignmentSequence")
