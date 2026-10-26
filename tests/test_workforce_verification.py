"""Business cases for packaged Python and SQL workforce verification.

SQLite executes the actual SQL predicates/interval query with only T-SQL variable,
COUNT_BIG and DATEADD syntax adapted. This checks results, not just SQL strings;
it does not replace a live SQL Server procedure compilation/integration check.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.defaults import ONLINE_SALES_REP_ROLE
from scripts import verify_employee_store_sales as standalone
from src.engine.quality_report import (
    QualityReport,
    _TableCache,
    _check_analytical_integrity,
    _check_salesperson_assignment_integrity,
)


ROOT = Path(__file__).resolve().parents[1]
SQL_FILES = [
    ROOT / "scripts/sql/verification/verify_employee_store_sales.sql",
    ROOT / "scripts/sql/bootstrap/verification/30_employee_store_sales.sql",
]


def frames():
    employees = pd.DataFrame(
        {
            "EmployeeKey": [7],
            "Title": ["Sales Associate"],
            "HomeStoreKey": [10],
            "IsSalesperson": [True],
            "HireDate": ["2024-01-10"],
            "TerminationDate": ["2024-12-20"],
            "IsActive": [False],
        }
    )
    stores = pd.DataFrame(
        {
            "StoreKey": [10],
            "StoreType": ["Supermarket"],
            "OpeningDate": ["2024-01-05"],
            "ClosingDate": ["2024-12-25"],
            "RenovationStartDate": ["2024-06-10"],
            "RenovationEndDate": ["2024-06-20"],
        }
    )
    assignments = pd.DataFrame(
        {
            "EmployeeKey": [7],
            "StoreKey": [10],
            "RoleAtStore": ["Sales Associate"],
            "StartDate": ["2024-01-01"],
            "EndDate": ["2024-12-31"],
            "IsCurrentAssignment": [True],
            "AssignmentStatus": ["Completed"],
        }
    )
    return employees, stores, assignments


# A deliberately over-wide bridge must not override employment/store dates.
CASES = [
    ("2024-01-09", False),
    ("2024-01-10", True),
    ("2024-06-09", True),
    ("2024-06-10", False),
    ("2024-06-19", False),
    ("2024-06-20", True),
    ("2024-12-20", True),
    ("2024-12-21", False),
]


def check_python(sales, employees, stores, assignments, role="Sales Associate"):
    report = QualityReport()
    _check_salesperson_assignment_integrity(
        report,
        sales,
        assignments,
        employees=employees,
        stores=stores,
        salesperson_roles=[role, ONLINE_SALES_REP_ROLE],
        chunk_size=2,
    )
    assert len(report.checks) == 1
    return report.checks[0]


@pytest.mark.parametrize("on_date,expected", CASES)
def test_python_eligibility_boundaries(on_date, expected):
    employees, stores, assignments = frames()
    sales = pd.DataFrame({"EmployeeKey": [7], "StoreKey": [10], "OrderDate": [on_date]})
    assert check_python(sales, employees, stores, assignments).passed is expected


@pytest.mark.parametrize("source", ["sales", "order_header", "both"])
@pytest.mark.parametrize("file_format", ["parquet", "csv"])
def test_report_checks_each_published_attribution_source(tmp_path, source, file_format):
    employees, stores, assignments = frames()
    dims, facts = tmp_path / "dimensions", tmp_path / "facts"
    dims.mkdir()
    facts.mkdir()
    for name, frame in [
        ("employees", employees),
        ("stores", stores),
        ("employee_store_assignments", assignments),
    ]:
        frame.to_parquet(dims / f"{name}.parquet", index=False)
    for name in ["sales", "order_header"] if source == "both" else [source]:
        # The valid flat table must not hide the invalid header in both mode.
        frame = pd.DataFrame(
            {
                "EmployeeKey": [7],
                "StoreKey": [10],
                "OrderDate": ["2024-06-10" if name == "order_header" else "2024-06-20"],
            }
        )
        if file_format == "parquet":
            frame.to_parquet(facts / f"{name}.parquet", index=False)
        else:
            (facts / name).mkdir()
            frame.to_csv(facts / name / "part-000.csv", index=False)
    report = QualityReport()
    _check_analytical_integrity(report, dims, facts, _TableCache())
    checks = [
        c
        for c in report.checks
        if c.name == "Salesperson effective assignment matches sale"
    ]
    assert len(checks) == (2 if source == "both" else 1)
    assert all(c.passed == ("Source: sales." in c.details) for c in checks)


@pytest.mark.parametrize(
    "defect", ["role", "flag", "employee", "store", "assignment", "channel"]
)
def test_matching_bridge_is_not_sufficient_for_eligibility(defect):
    employees, stores, assignments = frames()
    if defect == "role":
        assignments.loc[0, "RoleAtStore"] = "Cashier"
    elif defect == "flag":
        employees.loc[0, "IsSalesperson"] = False
    elif defect == "employee":
        employees.loc[0, "EmployeeKey"] = 8
    elif defect == "store":
        stores.loc[0, "StoreKey"] = 11
    elif defect == "assignment":
        assignments.loc[0, "StartDate"] = "2024-07-01"
    else:
        stores.loc[0, "StoreType"] = "Online"
    sales = pd.DataFrame(
        {"EmployeeKey": [7], "StoreKey": [10], "OrderDate": ["2024-06-20"]}
    )
    assert not check_python(sales, employees, stores, assignments).passed


def test_missing_eligibility_columns_are_reported_as_unverified():
    employees, stores, assignments = frames()
    sales = pd.DataFrame(
        {"EmployeeKey": [7], "StoreKey": [10], "OrderDate": ["2024-06-20"]}
    )
    check = check_python(sales, employees.drop(columns="HireDate"), stores, assignments)
    assert not check.passed
    assert "Cannot verify" in check.message


@pytest.mark.parametrize(
    "on_date,expected",
    [
        ("2024-01-04", False),
        ("2024-01-05", True),
        ("2024-12-24", True),
        ("2024-12-25", False),
    ],
)
def test_store_opening_and_closure_are_independent_of_employment(on_date, expected):
    employees, stores, assignments = frames()
    employees.loc[0, "HireDate"] = "2024-01-01"
    employees.loc[0, "TerminationDate"] = None
    sales = pd.DataFrame({"EmployeeKey": [7], "StoreKey": [10], "OrderDate": [on_date]})
    assert check_python(sales, employees, stores, assignments).passed is expected


def sql_connection(employees, stores, assignments, sales):
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ':memory:' AS dbo")
    for name, frame in [
        ("Employees", employees),
        ("Stores", stores),
        ("EmployeeStoreAssignments", assignments),
        ("Sales", sales),
        ("OrderHeader", sales),
    ]:
        frame.to_sql(name, conn, index=False)
        conn.execute(f'CREATE TABLE dbo."{name}" AS SELECT * FROM main."{name}"')
    conn.create_function(
        "DATEADD",
        3,
        lambda unit, n, value: (
            None
            if value is None
            else (date.fromisoformat(value) + timedelta(days=n)).isoformat()
        ),
    )
    return conn


def eligibility_query(path, table):
    sql = path.read_text(encoding="utf-8")
    query = re.search(
        rf"SELECT @invalid_\w+ = COUNT_BIG\(\*\) FROM dbo\.{table} f\n(WHERE NOT EXISTS \(.*?\n\);)",
        sql,
        re.S,
    )
    assert query is not None
    return f"SELECT COUNT(*) FROM dbo.{table} f\n{query.group(1)}"


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("table", ["Sales", "OrderHeader"])
def test_sql_eligibility_executes_boundaries_and_opaque_keys(path, table):
    employees, stores, assignments = frames()
    sales = pd.DataFrame(
        {
            "EmployeeKey": [7] * len(CASES),
            "StoreKey": [10] * len(CASES),
            "OrderDate": [d for d, _ in CASES],
        }
    )
    with sql_connection(employees, stores, assignments, sales) as conn:
        assert conn.execute(eligibility_query(path, table)).fetchone()[0] == sum(
            not ok for _, ok in CASES
        )


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize(
    "role,store_type",
    [("Retail Consultant", "Supermarket"), (ONLINE_SALES_REP_ROLE, "Online")],
)
def test_sql_accepts_custom_role_and_online_employee_with_small_key(
    path, role, store_type
):
    employees, stores, assignments = frames()
    employees.loc[0, "Title"] = role
    assignments.loc[0, "RoleAtStore"] = role
    stores.loc[0, "StoreType"] = store_type
    sales = pd.DataFrame(
        {"EmployeeKey": [7], "StoreKey": [10], "OrderDate": ["2024-06-20"]}
    )
    with sql_connection(employees, stores, assignments, sales) as conn:
        assert conn.execute(eligibility_query(path, "Sales")).fetchone()[0] == 0
    assert check_python(sales, employees, stores, assignments, role).passed


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize(
    "defect", ["role", "manager", "channel", "employee", "store", "assignment"]
)
def test_sql_rejects_matching_but_ineligible_attribution(path, defect):
    employees, stores, assignments = frames()
    if defect == "role":
        assignments.loc[0, "RoleAtStore"] = "Cashier"
    elif defect == "manager":
        employees.loc[0, "Title"] = "Store Manager"
        assignments.loc[0, "RoleAtStore"] = "Store Manager"
    elif defect == "channel":
        stores.loc[0, "StoreType"] = "Online"
    elif defect == "employee":
        employees.loc[0, "EmployeeKey"] = 8
    elif defect == "store":
        stores.loc[0, "StoreKey"] = 11
    else:
        assignments.loc[0, "StartDate"] = "2024-07-01"
    sales = pd.DataFrame(
        {"EmployeeKey": [7], "StoreKey": [10], "OrderDate": ["2024-06-20"]}
    )
    with sql_connection(employees, stores, assignments, sales) as conn:
        assert conn.execute(eligibility_query(path, "Sales")).fetchone()[0] == 1


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("hole", [False, True])
@pytest.mark.parametrize(
    "role,store_type",
    [
        ("Sales Associate", "Supermarket"),
        ("Retail Consultant", "Supermarket"),
        (ONLINE_SALES_REP_ROLE, "Online"),
    ],
)
def test_sql_coverage_checks_union_and_interior_holes(path, hole, role, store_type):
    employees, stores, assignments = frames()
    employees.loc[0, "Title"] = role
    stores.loc[0, "StoreType"] = store_type
    assignments.loc[0, "RoleAtStore"] = role
    employees.loc[0, "HireDate"] = "2024-01-01"
    stores.loc[
        0, ["OpeningDate", "ClosingDate", "RenovationStartDate", "RenovationEndDate"]
    ] = [
        "2024-01-05",
        "2024-01-29",
        "2024-01-10",
        "2024-01-20",
    ]
    assignments = pd.concat([assignments] * 3, ignore_index=True)
    assignments["StartDate"] = ["2024-01-05", "2024-01-08", "2024-01-20"]
    assignments["EndDate"] = [
        "2024-01-06" if hole else "2024-01-07",
        "2024-01-09",
        "2024-01-28",
    ]
    standalone.results.clear()
    standalone._check_salesperson_coverage(assignments, stores, "ESA-Bridge", employees)
    assert standalone.results[-1].passed is (not hole)
    sql = path.read_text(encoding="utf-8")
    query = sql[
        sql.index(";WITH OperatingWindows") : sql.index(") gaps;") + len(") gaps;")
    ]
    query = query.replace("SELECT @coverage_gaps = COUNT(*)", "SELECT COUNT(*)")
    query = query.replace("DATEADD(DAY,", "DATEADD('DAY',")
    sales = pd.DataFrame(
        {"EmployeeKey": [7], "StoreKey": [10], "OrderDate": ["2024-01-20"]}
    )
    with sql_connection(employees, stores, assignments, sales) as conn:
        assert conn.execute(
            query, {"cov_start": "2024-01-01", "cov_end": "2024-01-31"}
        ).fetchone()[0] == int(hole)


def test_sql_verifiers_do_not_decode_employee_keys():
    for path in SQL_FILES:
        sql = path.read_text(encoding="utf-8")
        assert not re.search(
            r"EmployeeKey\s*[<>]=?\s*(30000000|40000000|50000000)", sql
        )
