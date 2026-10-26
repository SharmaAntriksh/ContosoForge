"""Execute workforce queries on hand-built cases, independently of generators.

SQLite validates relational results only. It does not validate SQL Server or
PostgreSQL deployment syntax; the dialect/casing suites cover rendering.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from src.tools.sql.dialect import REGISTRY
from src.tools.sql.identifier_case import IdentifierCase, to_case
from src.tools.sql.name_map import build_name_map
from src.tools.sql.view_sql import render_views_sql

DOC = Path(__file__).resolve().parents[1] / "docs" / "WORKFORCE_SQL_EXAMPLES.md"


def _database(case=IdentifierCase.PASCAL):
    conn = sqlite3.connect(":memory:")
    ddl = """
        CREATE TABLE "Employees" (
            "EmployeeKey" INT, "EmployeeName" TEXT, "Title" TEXT,
            "HomeStoreKey" INT, "HireDate" TEXT, "TerminationDate" TEXT,
            "IsSalesperson" INT);
        CREATE TABLE "Stores" (
            "StoreKey" INT, "StoreNumber" TEXT, "StoreName" TEXT,
            "StoreType" TEXT, "StoreRegion" TEXT, "Status" TEXT,
            "EmployeeCount" INT, "OpeningDate" TEXT, "ClosingDate" TEXT,
            "RenovationStartDate" TEXT, "RenovationEndDate" TEXT);
        CREATE TABLE "EmployeeStoreAssignments" (
            "AssignmentKey" INT, "EmployeeKey" INT, "StoreKey" INT,
            "StartDate" TEXT, "EndDate" TEXT, "RoleAtStore" TEXT,
            "FTE" REAL, "IsCurrentAssignment" INT, "AssignmentStatus" TEXT,
            "TransferReason" TEXT);
        CREATE TABLE "vw_Sales" (
            "StoreKey" INT, "CurrencyKey" INT, "OrderNumber" INT,
            "NetPrice" REAL, "Quantity" INT);
    """
    conn.executescript(
        re.sub(r'"([^"]+)"', lambda m: '"' + to_case(m[1], case) + '"', ddl)
    )

    def insert(table, rows):
        table = to_case(table, case)
        conn.executemany(
            f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in rows[0])})', rows
        )

    insert(
        "Stores",
        [
            (
                1,
                "S1",
                "Destination",
                "Supermarket",
                "R1",
                "Open",
                5,
                "2020-01-01",
                None,
                None,
                None,
            ),
            (
                2,
                "S2",
                "Renovating home",
                "Supermarket",
                "R2",
                "Renovating",
                2,
                "2020-01-01",
                None,
                "2024-06-01",
                "2025-01-10",
            ),
            (
                3,
                "S3",
                "Closed",
                "Supermarket",
                "R1",
                "Closed",
                7,
                "2020-01-01",
                "2024-05-01",
                None,
                None,
            ),
            (
                10001,
                "ON",
                "Online",
                "Online",
                "R1",
                "Open",
                1,
                "2020-01-01",
                None,
                None,
                None,
            ),
        ],
    )
    insert(
        "Employees",
        [
            (1, "Home manager", "Store Manager", 1, "2020-01-01", None, 0),
            (2, "Visiting manager", "Store Manager", 2, "2020-01-01", None, 0),
            (3, "Part time", "Cashier", 2, "2020-01-01", None, 0),
            (
                4,
                "Former salesperson",
                "Sales Associate",
                1,
                "2020-01-01",
                "2024-05-31",
                1,
            ),
            (5, "Transferred salesperson", "Sales Associate", 2, "2020-01-01", None, 1),
            (
                6,
                "Online rep",
                "Online Sales Representative",
                10001,
                "2020-01-01",
                None,
                1,
            ),
        ],
    )
    insert(
        "EmployeeStoreAssignments",
        [
            (
                1,
                1,
                1,
                "2024-01-01",
                "2024-12-31",
                "Store Manager",
                1,
                1,
                "Active",
                "Initial",
            ),
            (
                2,
                2,
                1,
                "2024-06-01",
                "2024-12-31",
                "Store Manager",
                1,
                1,
                "Active",
                "Renovation Reassignment",
            ),
            (
                3,
                3,
                1,
                "2024-06-01",
                "2024-12-31",
                "Cashier",
                0.5,
                1,
                "Active",
                "Renovation Reassignment",
            ),
            (
                4,
                4,
                1,
                "2024-01-01",
                "2024-05-31",
                "Sales Associate",
                1,
                1,
                "Completed",
                "Initial",
            ),
            (
                5,
                5,
                2,
                "2024-01-01",
                "2024-06-09",
                "Sales Associate",
                1,
                0,
                "Transferred",
                "Initial",
            ),
            (
                6,
                5,
                1,
                "2024-06-10",
                "2024-12-31",
                "Sales Associate",
                1,
                1,
                "Active",
                "Backfill",
            ),
            (
                7,
                6,
                10001,
                "2024-01-01",
                "2024-12-31",
                "Online Sales Representative",
                1,
                1,
                "Active",
                "Initial",
            ),
        ],
    )
    return conn


@pytest.mark.parametrize("dialect", ["sqlserver", "postgres"])
@pytest.mark.parametrize("case", [IdentifierCase.PASCAL, IdentifierCase.SNAKE])
def test_default_workforce_views_respect_home_manager_and_actual_placement(
    dialect, case
):
    schema = "dbo" if dialect == "sqlserver" else "public"
    sql = render_views_sql(
        REGISTRY[dialect],
        mode="sales",
        skip_order_cols=False,
        view_schema=schema,
        name_map=build_name_map(case),
    )
    if dialect == "postgres":
        bodies = re.findall(r"EXECUTE \$sql\$\n(.*?)\n\$sql\$;", sql, re.S)
    else:
        bodies = [
            body.replace("''", "'")
            for body in re.findall(r"EXEC\('((?:[^']|'')*)'\)", sql, re.S)
        ]
    with _database(case) as conn:
        for name in (
            "CurrentEmployeesByStore",
            "CurrentStoreManager",
            "StoreStaffingActualVsPlan",
        ):
            view = to_case("vw_" + name, case)
            body = next(
                body
                for body in bodies
                if f'"{view}" AS' in body or f"[{view}] AS" in body
            )
            body = body.replace("CREATE OR REPLACE VIEW", "CREATE VIEW").replace(
                "CREATE OR ALTER VIEW", "CREATE VIEW"
            )
            body = (
                body.replace(f'"{schema}".', "")
                .replace(f"[{schema}].", "")
                .replace("N'", "'")
            )
            conn.execute(body)

        def query(name):
            return conn.execute(
                f'SELECT * FROM "{to_case("vw_" + name, case)}"'
            ).fetchall()

        managers = query("CurrentStoreManager")
        assert [(row[0], row[2]) for row in managers] == [(1, 1)]
        employees = query("CurrentEmployeesByStore")
        assert {row[0] for row in employees} == {1, 2, 3, 5, 6}
        staffing = {row[0]: row[5:] for row in query("StoreStaffingActualVsPlan")}
        assert staffing[1] == (5, 4, 3.5, -1)
        assert staffing[2] == (2, 0, 0, -2)
        assert staffing[3] == (7, 0, 0, -7)


def _example(index):
    return re.findall(r"```sql\n(.*?)\n```", DOC.read_text(encoding="utf-8"), re.S)[
        index
    ]


def test_documented_history_preserves_segments_and_home_location():
    with _database() as conn:
        rows = conn.execute(_example(0)).fetchall()
        assert len(rows) == 7
        visitor = next(row for row in rows if row[1] == 2)
        assert visitor[3:5] == (2, 1)  # home and effective store remain distinct


def test_documented_effective_lookup_uses_dates_and_eligibility():
    with _database() as conn:
        query = _example(1)
        assert {(row[0], row[2]) for row in conn.execute(query)} == {(5, 1), (6, 10001)}
        # Bridge existence alone is insufficient after termination or before hire.
        conn.execute(
            'UPDATE "EmployeeStoreAssignments" SET "EndDate" = ? WHERE "EmployeeKey" = 4',
            ("2024-12-31",),
        )
        conn.execute(
            'UPDATE "Employees" SET "HireDate" = ? WHERE "EmployeeKey" = 5',
            ("2024-07-01",),
        )
        assert {row[0] for row in conn.execute(query)} == {6}


def test_documented_store_actuals_average_orders_not_lines_and_keep_currency():
    with _database() as conn:
        conn.executemany(
            'INSERT INTO "vw_Sales" VALUES (?, ?, ?, ?, ?)',
            [
                (1, 1, 10, 10, 2),
                (1, 1, 10, 30, 1),  # order 10 totals 50
                (1, 1, 11, 100, 1),  # order 11 totals 100; average order is 75
                (1, 2, 12, 7, 3),  # separate currency
            ],
        )
        cursor = conn.execute(_example(2))
        assert "NetSalesAmount" in [column[0] for column in cursor.description]
        rows = {(row[0], row[1]): row[2:] for row in cursor}
        assert rows == {(1, 1): (2, 150, 75), (1, 2): (1, 21, 21)}
