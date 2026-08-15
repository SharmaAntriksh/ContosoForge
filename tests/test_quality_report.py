"""Tests for src.engine.quality_report."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.engine.quality_report import (
    QualityReport,
    _TableCache,
    generate_quality_report,
    _check_nulls_and_duplicates,
    _check_referential_integrity,
    _check_complaint_chronology,
    _check_subscription_lifecycle,
    _check_wishlist_conversion_chronology,
    _check_inventory_stockout_semantics,
    _check_employee_assignment_temporal_integrity,
    _check_salesperson_assignment_integrity,
    _check_current_headcount_reconciliation,
    _check_store_organizational_geography,
    _check_employee_organizational_geography,
    _check_workforce_policy_integrity,
    _check_store_lifecycle_causality,
)


class TestQualityReport:
    def test_smoke_generates_html(self, tmp_path):
        from src.engine.quality_report import generate_quality_report

        dims = tmp_path / "dimensions"
        dims.mkdir()
        facts = tmp_path / "facts"
        facts.mkdir()

        customers = pd.DataFrame({
            "CustomerKey": np.arange(1, 6, dtype=np.int64),
            "CustomerName": [f"Cust {i}" for i in range(1, 6)],
        })
        customers.to_parquet(dims / "customers.parquet", index=False)

        products = pd.DataFrame({
            "ProductKey": np.arange(1, 4, dtype=np.int64),
            "ProductName": ["A", "B", "C"],
        })
        products.to_parquet(dims / "products.parquet", index=False)

        stores = pd.DataFrame({
            "StoreKey": np.arange(1, 3, dtype=np.int64),
            "StoreName": ["S1", "S2"],
        })
        stores.to_parquet(dims / "stores.parquet", index=False)

        dates = pd.DataFrame({
            "DateKey": pd.date_range("2024-01-01", periods=10),
        })
        dates.to_parquet(dims / "dates.parquet", index=False)

        sales = pd.DataFrame({
            "CustomerKey": np.array([1, 2, 3, 4, 5], dtype=np.int64),
            "ProductKey": np.array([1, 2, 3, 1, 2], dtype=np.int64),
            "StoreKey": np.array([1, 2, 1, 2, 1], dtype=np.int64),
            "OrderDate": pd.date_range("2024-01-01", periods=5),
            "Quantity": [1, 2, 1, 3, 1],
            "NetPrice": [10.0, 20.0, 30.0, 40.0, 50.0],
            "DiscountAmount": [0.0, 1.0, 0.0, 2.0, 0.0],
        })
        sales.to_parquet(facts / "sales.parquet", index=False)

        report_path = generate_quality_report(tmp_path)
        assert report_path.exists()
        assert report_path.suffix == ".html"
        html = report_path.read_text(encoding="utf-8")
        assert "<html" in html.lower()
        assert "customers" in html.lower()

    def test_html_contains_row_counts(self, tmp_path):
        """Row counts for each table should appear in the HTML report."""
        from src.engine.quality_report import generate_quality_report

        dims = tmp_path / "dimensions"
        dims.mkdir()
        facts = tmp_path / "facts"
        facts.mkdir()

        customers = pd.DataFrame({
            "CustomerKey": np.arange(1, 11, dtype=np.int64),
            "CustomerName": [f"Cust {i}" for i in range(1, 11)],
        })
        customers.to_parquet(dims / "customers.parquet", index=False)

        sales = pd.DataFrame({
            "CustomerKey": np.arange(1, 6, dtype=np.int64),
            "ProductKey": np.array([1, 2, 3, 1, 2], dtype=np.int64),
            "StoreKey": np.array([1, 2, 1, 2, 1], dtype=np.int64),
            "OrderDate": pd.date_range("2024-01-01", periods=5),
            "Quantity": [1, 2, 1, 3, 1],
            "NetPrice": [10.0, 20.0, 30.0, 40.0, 50.0],
            "DiscountAmount": [0.0, 1.0, 0.0, 2.0, 0.0],
        })
        sales.to_parquet(facts / "sales.parquet", index=False)

        report_path = generate_quality_report(tmp_path)
        html = report_path.read_text(encoding="utf-8")
        assert "10" in html  # 10 customers
        assert "sales" in html.lower()

    def test_html_structure(self, tmp_path):
        """Report should contain proper HTML structure with tables."""
        from src.engine.quality_report import generate_quality_report

        dims = tmp_path / "dimensions"
        dims.mkdir()
        pd.DataFrame({"CustomerKey": [1, 2]}).to_parquet(
            dims / "customers.parquet", index=False)

        report_path = generate_quality_report(tmp_path)
        html = report_path.read_text(encoding="utf-8")
        assert "<table" in html.lower()
        assert "</table>" in html.lower()
        assert "quality" in html.lower()

    def test_empty_folder_no_crash(self, tmp_path):
        from src.engine.quality_report import generate_quality_report
        report_path = generate_quality_report(tmp_path)
        assert isinstance(report_path, Path)


class TestOrderIntegrity:
    """QR-1: detect CHUNK-1 — duplicate OrderNumber in the header and
    broken returns/complaints → header links."""

    @pytest.fixture
    def dims_facts(self, tmp_path):
        dims = tmp_path / "dimensions"
        dims.mkdir()
        facts = tmp_path / "facts"
        facts.mkdir()
        return dims, facts

    @staticmethod
    def _header(facts, so_numbers, dtype=np.int64):
        pd.DataFrame({
            "OrderNumber": np.array(so_numbers, dtype=dtype),
        }).to_parquet(facts / "order_header.parquet", index=False)

    @staticmethod
    def _find(report, name):
        return next((c for c in report.checks if c.name == name), None)

    def test_duplicate_order_number_in_header_is_flagged(self, dims_facts):
        dims, facts = dims_facts
        # OrderNumber 1002 duplicated — the CHUNK-1 signature.
        self._header(facts, [1001, 1002, 1002, 1003])

        report = QualityReport()
        _check_nulls_and_duplicates(report, dims, facts, _TableCache())

        dup = self._find(report, "Duplicate PK: order_header.OrderNumber")
        assert dup is not None and dup.passed is False
        assert "1 duplicate" in dup.message

    def test_unique_order_number_in_header_passes(self, dims_facts):
        dims, facts = dims_facts
        self._header(facts, [1001, 1002, 1003])

        report = QualityReport()
        _check_nulls_and_duplicates(report, dims, facts, _TableCache())

        dup = self._find(report, "Duplicate PK: order_header.OrderNumber")
        assert dup is not None and dup.passed is True

    def test_returns_orphan_order_number_is_flagged(self, dims_facts):
        dims, facts = dims_facts
        self._header(facts, [1001, 1002, 1003])
        # 9999 has no matching header row.
        pd.DataFrame({
            "OrderNumber": np.array([1001, 9999], dtype=np.int64),
            "ReturnReasonKey": np.array([1, 2], dtype=np.int64),
        }).to_parquet(facts / "returns.parquet", index=False)

        report = QualityReport()
        _check_referential_integrity(report, dims, facts, _TableCache())

        fk = self._find(
            report,
            "FK: returns.OrderNumber → order_header.OrderNumber",
        )
        assert fk is not None and fk.passed is False
        assert "9999" in fk.details

    def test_complaints_null_order_number_is_ignored(self, dims_facts):
        dims, facts = dims_facts
        self._header(facts, [1001, 1002, 1003])
        # Unlinked complaints carry null OrderNumber — must not count as orphans.
        pd.DataFrame({
            "OrderNumber": pd.array([1002, None, 1003], dtype="Int64"),
        }).to_parquet(facts / "complaints.parquet", index=False)

        report = QualityReport()
        _check_referential_integrity(report, dims, facts, _TableCache())

        fk = self._find(
            report,
            "FK: complaints.OrderNumber → order_header.OrderNumber",
        )
        assert fk is not None and fk.passed is True


class TestPhase7WorkforceReferentialIntegrity:
    @pytest.fixture
    def dims_facts(self, tmp_path):
        dims = tmp_path / "dimensions"
        dims.mkdir()
        facts = tmp_path / "facts"
        facts.mkdir()
        return dims, facts

    @staticmethod
    def _find(report, name):
        return next((check for check in report.checks if check.name == name), None)

    def test_assignment_primary_key_duplicates_are_flagged(self, dims_facts):
        dims, facts = dims_facts
        pd.DataFrame({
            "AssignmentKey": [1, 1],
        }).to_parquet(dims / "employee_store_assignments.parquet", index=False)

        report = QualityReport()
        _check_nulls_and_duplicates(report, dims, facts, _TableCache())

        duplicate = self._find(
            report,
            "Duplicate PK: employee_store_assignments.AssignmentKey",
        )
        assert duplicate is not None and duplicate.passed is False

    def test_workforce_and_warehouse_foreign_keys_are_checked(self, dims_facts):
        dims, facts = dims_facts
        pd.DataFrame({
            "WarehouseKey": [100],
        }).to_parquet(dims / "warehouses.parquet", index=False)
        pd.DataFrame({
            "StoreKey": [10, 20],
            "WarehouseKey": [100, 999],
        }).to_parquet(dims / "stores.parquet", index=False)
        pd.DataFrame({
            "EmployeeKey": [1, 2],
            "ParentEmployeeKey": pd.array([pd.NA, 99], dtype="Int64"),
            "HomeStoreKey": pd.array([pd.NA, 999], dtype="Int64"),
        }).to_parquet(dims / "employees.parquet", index=False)
        pd.DataFrame({
            "AssignmentKey": [1, 2],
            "EmployeeKey": [1, 77],
            "StoreKey": [10, 88],
        }).to_parquet(dims / "employee_store_assignments.parquet", index=False)

        report = QualityReport()
        _check_referential_integrity(report, dims, facts, _TableCache())

        expected_failures = {
            "FK: employee_store_assignments.EmployeeKey → employees.EmployeeKey": "77",
            "FK: employee_store_assignments.StoreKey → stores.StoreKey": "88",
            "FK: employees.ParentEmployeeKey → employees.EmployeeKey": "99",
            "FK: employees.HomeStoreKey → stores.StoreKey": "999",
            "FK: stores.WarehouseKey → warehouses.WarehouseKey": "999",
        }
        for check_name, orphan in expected_failures.items():
            check = self._find(report, check_name)
            assert check is not None and check.passed is False
            assert orphan in check.details

    def test_valid_workforce_relationships_allow_nullable_employee_links(
        self, dims_facts,
    ):
        dims, facts = dims_facts
        pd.DataFrame({
            "WarehouseKey": [100],
        }).to_parquet(dims / "warehouses.parquet", index=False)
        pd.DataFrame({
            "StoreKey": [10],
            "WarehouseKey": [100],
        }).to_parquet(dims / "stores.parquet", index=False)
        pd.DataFrame({
            "EmployeeKey": [1, 2],
            "ParentEmployeeKey": pd.array([pd.NA, 1], dtype="Int64"),
            "HomeStoreKey": pd.array([pd.NA, 10], dtype="Int64"),
        }).to_parquet(dims / "employees.parquet", index=False)
        pd.DataFrame({
            "AssignmentKey": [1],
            "EmployeeKey": [2],
            "StoreKey": [10],
        }).to_parquet(dims / "employee_store_assignments.parquet", index=False)

        report = QualityReport()
        _check_referential_integrity(report, dims, facts, _TableCache())

        expected_checks = (
            "FK: employee_store_assignments.EmployeeKey → employees.EmployeeKey",
            "FK: employee_store_assignments.StoreKey → stores.StoreKey",
            "FK: employees.ParentEmployeeKey → employees.EmployeeKey",
            "FK: employees.HomeStoreKey → stores.StoreKey",
            "FK: stores.WarehouseKey → warehouses.WarehouseKey",
        )
        for check_name in expected_checks:
            check = self._find(report, check_name)
            assert check is not None and check.passed is True

    def test_employee_and_warehouse_geography_foreign_keys_are_checked(
        self, dims_facts,
    ):
        dims, facts = dims_facts
        pd.DataFrame({"GeographyKey": [1]}).to_parquet(
            dims / "geography.parquet", index=False,
        )
        pd.DataFrame({
            "WarehouseKey": [100], "GeographyKey": [999],
        }).to_parquet(dims / "warehouses.parquet", index=False)
        pd.DataFrame({
            "EmployeeKey": [1], "GeographyKey": [998],
        }).to_parquet(dims / "employees.parquet", index=False)

        report = QualityReport()
        _check_referential_integrity(report, dims, facts, _TableCache())

        expected_failures = {
            "FK: warehouses.GeographyKey → geography.GeographyKey": "999",
            "FK: employees.GeographyKey → geography.GeographyKey": "998",
        }
        for check_name, orphan in expected_failures.items():
            check = self._find(report, check_name)
            assert check is not None and check.passed is False
            assert orphan in check.details


class TestPhase7AssignmentTemporalIntegrity:
    @staticmethod
    def _find(report, name):
        return next(check for check in report.checks if check.name == name)

    def test_valid_inclusive_assignment_timeline_passes(self):
        assignments = pd.DataFrame({
            "EmployeeKey": [1, 1, 2],
            "StartDate": pd.to_datetime([
                "2024-01-01", "2024-01-11", "2024-02-01",
            ]),
            "EndDate": pd.to_datetime([
                "2024-01-10", "2024-12-31", "2024-12-31",
            ]),
            "IsCurrentAssignment": [False, True, True],
        })
        report = QualityReport()

        _check_employee_assignment_temporal_integrity(report, assignments)

        assert self._find(report, "Assignment date order").passed
        assert self._find(report, "Assignment segments do not overlap").passed
        assert self._find(report, "Assignment segments are contiguous").passed
        assert self._find(report, "Exactly one current assignment per employee").passed

    def test_invalid_dates_overlaps_gaps_and_current_counts_are_flagged(self):
        assignments = pd.DataFrame({
            "EmployeeKey": [1, 1, 2, 2, 3, 4, 4],
            "StartDate": pd.to_datetime([
                "2024-01-01", "2024-01-10",
                "2024-02-01", "2024-02-07",
                "2024-03-05",
                "2024-04-01", "2024-04-11",
            ]),
            "EndDate": pd.to_datetime([
                "2024-01-10", "2024-01-20",
                "2024-02-05", "2024-02-20",
                "2024-03-04",
                "2024-04-10", "2024-04-20",
            ]),
            "IsCurrentAssignment": [False, True, False, True, True, True, True],
        })
        report = QualityReport()

        _check_employee_assignment_temporal_integrity(report, assignments)

        assert not self._find(report, "Assignment date order").passed
        assert not self._find(report, "Assignment segments do not overlap").passed
        assert not self._find(report, "Assignment segments are contiguous").passed
        assert not self._find(
            report, "Exactly one current assignment per employee",
        ).passed

    def test_generated_report_checks_assignments_without_sales(self, tmp_path):
        dims = tmp_path / "dimensions"
        dims.mkdir()
        pd.DataFrame({
            "AssignmentKey": [1],
            "EmployeeKey": [1],
            "StoreKey": [10],
            "StartDate": pd.to_datetime(["2024-01-01"]),
            "EndDate": pd.to_datetime(["2024-12-31"]),
            "IsCurrentAssignment": [True],
        }).to_parquet(dims / "employee_store_assignments.parquet", index=False)

        report_path = generate_quality_report(tmp_path)
        report_html = report_path.read_text(encoding="utf-8")

        assert "Assignment segments are contiguous" in report_html


class TestPhase7SalespersonAndHeadcountIntegrity:
    @staticmethod
    def _find(report, name):
        return next(check for check in report.checks if check.name == name)

    @staticmethod
    def _assignments():
        return pd.DataFrame({
            "EmployeeKey": [1, 1, 2],
            "StoreKey": [10, 20, 10],
            "StartDate": pd.to_datetime([
                "2024-01-01", "2024-07-01", "2024-01-01",
            ]),
            "EndDate": pd.to_datetime([
                "2024-06-30", "2024-12-31", "2024-06-30",
            ]),
        })

    def test_salesperson_store_and_date_match_effective_assignment(self):
        sales = pd.DataFrame({
            "EmployeeKey": [1, 1, 2],
            "StoreKey": [10, 20, 10],
            "OrderDate": pd.to_datetime([
                "2024-06-30", "2024-07-01", "2024-06-30",
            ]),
        })
        report = QualityReport()

        _check_salesperson_assignment_integrity(
            report, sales, self._assignments(), chunk_size=2,
        )

        assert self._find(
            report, "Salesperson effective assignment matches sale",
        ).passed

    def test_salesperson_wrong_store_and_out_of_period_are_flagged(self):
        sales = pd.DataFrame({
            "EmployeeKey": [1, 2],
            "StoreKey": [10, 10],
            "OrderDate": pd.to_datetime(["2024-07-01", "2024-07-01"]),
        })
        report = QualityReport()

        _check_salesperson_assignment_integrity(
            report, sales, self._assignments(), chunk_size=1,
        )

        check = self._find(
            report, "Salesperson effective assignment matches sale",
        )
        assert not check.passed
        assert "2 sale row(s)" in check.message

    def test_current_headcount_reconciles_active_store_employees(self):
        employees = pd.DataFrame({
            "EmployeeKey": [1, 2, 3],
            "HomeStoreKey": pd.array([10, 10, pd.NA], dtype="Int64"),
            "IsActive": [True, False, True],
        })
        report = QualityReport()

        _check_current_headcount_reconciliation(
            report, employees, self._assignments(),
        )

        assert self._find(
            report, "Current assignment headcount reconciles to active employees",
        ).passed

    def test_headcount_missing_active_and_includes_inactive_are_flagged(self):
        employees = pd.DataFrame({
            "EmployeeKey": [1, 2],
            "HomeStoreKey": [10, 10],
            "IsActive": [True, False],
        })
        assignments = pd.DataFrame({
            "EmployeeKey": [1, 2],
            "StoreKey": [10, 10],
            "StartDate": pd.to_datetime(["2024-01-01", "2024-01-01"]),
            "EndDate": pd.to_datetime(["2024-06-30", "2024-12-31"]),
        })
        report = QualityReport()

        _check_current_headcount_reconciliation(report, employees, assignments)

        check = self._find(
            report, "Current assignment headcount reconciles to active employees",
        )
        assert not check.passed
        assert "1 missing active" in check.message
        assert "1 inactive/unexpected" in check.message

    def test_generated_report_includes_salesperson_and_headcount_checks(
        self, tmp_path,
    ):
        dims = tmp_path / "dimensions"
        facts = tmp_path / "facts"
        dims.mkdir()
        facts.mkdir()
        pd.DataFrame({
            "EmployeeKey": [1],
            "HomeStoreKey": [10],
            "IsActive": [True],
        }).to_parquet(dims / "employees.parquet", index=False)
        pd.DataFrame({
            "EmployeeKey": [1],
            "StoreKey": [10],
            "StartDate": pd.to_datetime(["2024-01-01"]),
            "EndDate": pd.to_datetime(["2024-12-31"]),
            "IsCurrentAssignment": [True],
        }).to_parquet(dims / "employee_store_assignments.parquet", index=False)
        pd.DataFrame({
            "EmployeeKey": [1],
            "StoreKey": [10],
            "OrderDate": pd.to_datetime(["2024-06-01"]),
        }).to_parquet(facts / "sales.parquet", index=False)

        report_path = generate_quality_report(tmp_path)
        report_html = report_path.read_text(encoding="utf-8")

        assert "Salesperson effective assignment matches sale" in report_html
        assert (
            "Current assignment headcount reconciles to active employees"
            in report_html
        )


class TestPhase7OrganizationalQualityContract:
    @staticmethod
    def _find(report, name):
        return next(check for check in report.checks if check.name == name)

    @staticmethod
    def _stores():
        return pd.DataFrame({
            "StoreKey": [10, 20],
            "Status": ["Open", "Open"],
            "GeographyKey": [100, 200],
            "StoreZone": ["Europe", "Europe"],
            "StoreDistrict": ["District 1", "District 2"],
            "StoreRegion": ["Region 1", "Region 2"],
            "StoreDistrictId": [1, 2],
            "StoreRegionId": [1, 2],
            "OpeningDate": pd.to_datetime(["2020-01-01", "2020-01-01"]),
            "ClosingDate": pd.to_datetime([None, None]),
            "CloseReason": ["", ""],
            "RevenueClass": ["A", "B"],
            "OwnershipType": ["Corporate", "Leased"],
            "SuccessorStoreKey": pd.array([pd.NA, pd.NA], dtype="Int64"),
            "RenovationStartDate": pd.to_datetime([None, None]),
            "RenovationEndDate": pd.to_datetime([None, None]),
        })

    @staticmethod
    def _employees():
        return pd.DataFrame({
            "EmployeeKey": [1, 2, 3, 4, 5, 6],
            "ParentEmployeeKey": pd.array(
                [pd.NA, 1, 2, 3, 4, 5], dtype="Int64",
            ),
            "Title": [
                "Chief Executive Officer", "VP Operations",
                "Regional Manager", "District Manager", "Store Manager",
                "Customer Support",
            ],
            "OrgLevel": [1, 2, 3, 4, 5, 6],
            "OrgUnitType": [
                "Corporate", "Corporate", "Region", "District", "Store", "Store",
            ],
            "RegionId": pd.array([pd.NA, pd.NA, 1, 1, 1, 1], dtype="Int64"),
            "DistrictId": pd.array(
                [pd.NA, pd.NA, pd.NA, 1, 1, 1], dtype="Int64",
            ),
            "HomeStoreKey": pd.array(
                [pd.NA, pd.NA, pd.NA, pd.NA, 10, 10], dtype="Int64",
            ),
            "GeographyKey": pd.array(
                [pd.NA, pd.NA, pd.NA, pd.NA, 100, 100], dtype="Int64",
            ),
            "HireDate": pd.to_datetime(["2020-01-01"] * 6),
            "TerminationDate": pd.to_datetime([None] * 6),
            "IsActive": [True] * 6,
            "EmploymentType": ["Full-Time"] * 5 + ["Part-Time"],
            "FTE": [1.0] * 5 + [0.75],
            "DepartmentName": [
                "Executive", "Corporate", "Store Operations",
                "Store Operations", "Store Operations", "Customer Service",
            ],
        })

    def test_valid_country_hierarchy_and_employee_rollups_pass(self):
        stores = self._stores()
        geography = pd.DataFrame({
            "GeographyKey": [100, 200],
            "Country": ["Alpha", "Beta"],
        })
        employees = self._employees()
        report = QualityReport()

        _check_store_organizational_geography(report, stores, geography)
        _check_employee_organizational_geography(report, employees, stores)
        _check_workforce_policy_integrity(report, employees)
        _check_store_lifecycle_causality(report, stores)

        assert all(check.passed for check in report.checks)

    def test_country_crossing_and_display_identity_are_flagged(self):
        stores = self._stores()
        stores.loc[1, ["StoreDistrictId", "StoreRegionId"]] = [1, 1]
        stores.loc[1, ["StoreDistrict", "StoreRegion"]] = [
            "Duplicate district label", "Duplicate region label",
        ]
        geography = pd.DataFrame({
            "GeographyKey": [100, 200],
            "Country": ["Alpha", "Beta"],
        })
        report = QualityReport()

        _check_store_organizational_geography(report, stores, geography)

        assert not self._find(report, "Store district country integrity").passed
        assert not self._find(
            report, "Store region country and zone integrity",
        ).passed
        assert not self._find(
            report, "Store hierarchy labels match structured IDs",
        ).passed

    def test_employee_location_and_parent_rollup_mismatches_are_flagged(self):
        employees = self._employees()
        employees.loc[5, "RegionId"] = 2
        employees.loc[3, "ParentEmployeeKey"] = 2
        employees.loc[0, "HomeStoreKey"] = 10
        report = QualityReport()

        _check_employee_organizational_geography(
            report, employees, self._stores(),
        )

        assert not self._find(
            report, "Employee organizational location contract",
        ).passed
        assert not self._find(
            report, "Employee hierarchy keys match store hierarchy",
        ).passed
        assert not self._find(
            report, "Employee parent hierarchy follows organizational rollups",
        ).passed

    def test_workforce_policy_and_lifecycle_contradictions_are_flagged(self):
        employees = self._employees()
        employees.loc[4, ["EmploymentType", "FTE"]] = ["Part-Time", 0.5]
        employees.loc[5, "DepartmentName"] = "Corporate"
        employees.loc[5, "IsActive"] = False
        report = QualityReport()

        _check_workforce_policy_integrity(report, employees)

        assert not self._find(
            report, "Employee role, department, and FTE policy",
        ).passed
        assert not self._find(
            report, "Employee lifecycle snapshot matches effective dates",
        ).passed

    def test_store_close_reason_prerequisites_are_flagged(self):
        stores = self._stores()
        stores.loc[0, [
            "Status", "ClosingDate", "CloseReason", "RevenueClass",
        ]] = ["Closed", pd.Timestamp("2024-01-01"), "Low Sales", "A"]
        stores.loc[1, [
            "Status", "ClosingDate", "CloseReason", "OwnershipType",
        ]] = ["Closed", pd.Timestamp("2024-01-01"), "Lease Ended", "Corporate"]
        report = QualityReport()

        _check_store_lifecycle_causality(report, stores)

        assert self._find(
            report, "Store lifecycle dates match snapshot status",
        ).passed
        causal = self._find(
            report, "Store close reasons satisfy causal prerequisites",
        )
        assert not causal.passed
        assert "2 unsupported" in causal.message

    def test_packaged_report_surfaces_geography_and_policy_failures(self, tmp_path):
        dims = tmp_path / "dimensions"
        dims.mkdir()
        stores = self._stores()
        stores.loc[1, ["StoreDistrictId", "StoreRegionId"]] = [1, 1]
        stores.loc[1, ["StoreDistrict", "StoreRegion"]] = [
            "District 1", "Region 1",
        ]
        stores.to_parquet(dims / "stores.parquet", index=False)
        pd.DataFrame({
            "GeographyKey": [100, 200], "Country": ["Alpha", "Beta"],
        }).to_parquet(dims / "geography.parquet", index=False)
        employees = self._employees()
        employees.loc[5, "DepartmentName"] = "Corporate"
        employees.to_parquet(dims / "employees.parquet", index=False)

        report_path = generate_quality_report(tmp_path)
        html = report_path.read_text(encoding="utf-8")

        assert "Store district country integrity" in html
        assert "Employee role, department, and FTE policy" in html
        assert 'class="fail-count">4 failed' in html


class TestPhase2AnalyticalIntegrity:
    @staticmethod
    def _find(report, name):
        return next(c for c in report.checks if c.name == name)

    def test_complaint_chronology_and_response_arithmetic(self):
        sales = pd.DataFrame({
            "OrderNumber": [100],
            "OrderLineNumber": [1],
            "OrderDate": pd.to_datetime(["2024-02-10"]),
            "DeliveryDate": pd.to_datetime(["2024-02-11"]),
            "CustomerKey": [7],
        })
        complaints = pd.DataFrame({
            "OrderNumber": [100],
            "LineNumber": [1],
            "ComplaintDate": pd.to_datetime(["2024-02-12"]),
            "ComplaintType": ["Late Delivery"],
            "ResolutionDate": pd.to_datetime(["2024-02-15"]),
            "ResponseDays": [3],
            "CustomerKey": [7],
        })
        customers = pd.DataFrame({
            "CustomerKey": [7],
            "CustomerStartDate": pd.to_datetime(["2024-01-01"]),
            "CustomerEndDate": pd.to_datetime(["2024-12-31"]),
        })
        report = QualityReport()

        _check_complaint_chronology(report, sales, complaints, customers)

        assert self._find(report, "Complaint chronology (linked sale)").passed
        assert self._find(report, "Complaint within customer lifecycle").passed
        assert self._find(report, "Complaint response-day arithmetic").passed

    def test_subscription_lifecycle_and_global_end(self):
        subscriptions = pd.DataFrame({
            "CustomerKey": [7],
            "PeriodStartDate": pd.to_datetime(["2024-02-01"]),
            "PeriodEndDate": pd.to_datetime(["2024-02-29"]),
        })
        customers = pd.DataFrame({
            "CustomerKey": [7],
            "CustomerStartDate": pd.to_datetime(["2024-01-01"]),
            "CustomerEndDate": pd.to_datetime(["2024-06-30"]),
        })
        report = QualityReport()

        _check_subscription_lifecycle(
            report, subscriptions, customers, global_end="2024-12-31",
        )

        assert self._find(
            report, "Subscription period within customer lifecycle",
        ).passed
        assert self._find(report, "Subscription period within global end").passed

    def test_subscription_guard_rejects_censored_trial_nonconversion(self):
        subscriptions = pd.DataFrame({
            "SubscriptionKey": [1],
            "CustomerKey": [7],
            "PeriodStartDate": pd.to_datetime(["2024-06-28"]),
            "PeriodEndDate": pd.to_datetime(["2024-06-30"]),
            "TerminationReason": ["TrialNonConversion"],
        })
        customers = pd.DataFrame({
            "CustomerKey": [7],
            "CustomerStartDate": pd.to_datetime(["2024-01-01"]),
            "CustomerEndDate": pd.to_datetime(["2024-06-30"]),
        })
        report = QualityReport()

        _check_subscription_lifecycle(
            report,
            subscriptions,
            customers,
            global_end="2024-12-31",
            trial_days=14,
        )

        assert not self._find(
            report, "Subscription trial outcome is observed",
        ).passed

    def test_wishlist_dated_conversion_rate(self):
        sales = pd.DataFrame({
            "CustomerKey": [1],
            "ProductKey": [10],
            "OrderDate": pd.to_datetime(["2024-02-10"]),
        })
        wishlists = pd.DataFrame({
            "CustomerKey": [1, 2],
            "ProductKey": [10, 20],
            "AddedDate": pd.to_datetime(["2024-02-01", "2024-02-01"]),
        })
        report = QualityReport()

        _check_wishlist_conversion_chronology(
            report, sales, wishlists, expected_rate=0.5,
        )

        assert self._find(report, "Wishlist dated conversion").passed


class TestPhase4AnalyticalIntegrity:
    @staticmethod
    def _find(report, name):
        return next(c for c in report.checks if c.name == name)

    def test_inventory_stockout_flag_day_consistency(self):
        report = QualityReport()
        inventory = pd.DataFrame({
            "StockoutFlag": [True, False, True],
            "DaysOutOfStock": [1, 0, 0],
        })

        _check_inventory_stockout_semantics(report, inventory)

        check = self._find(report, "Inventory stockout flag/day consistency")
        assert check.passed is False
        assert "1 contradictory" in check.message

    def test_post_sale_fact_dates_are_covered_by_dates_dimension(self, tmp_path):
        dims = tmp_path / "dimensions"
        facts = tmp_path / "facts"
        dims.mkdir()
        facts.mkdir()
        pd.DataFrame({
            "Date": pd.date_range("2024-01-01", "2024-04-30", freq="D"),
        }).to_parquet(dims / "dates.parquet", index=False)
        pd.DataFrame({
            "ReturnDate": pd.to_datetime(["2024-04-20"]),
        }).to_parquet(facts / "returns.parquet", index=False)
        pd.DataFrame({
            "ComplaintDate": pd.to_datetime(["2024-04-21"]),
            "ResolutionDate": pd.to_datetime(["2024-04-25"]),
        }).to_parquet(facts / "complaints.parquet", index=False)

        report = QualityReport()
        _check_referential_integrity(report, dims, facts, _TableCache())

        for table, column in (
            ("returns", "ReturnDate"),
            ("complaints", "ComplaintDate"),
            ("complaints", "ResolutionDate"),
        ):
            assert self._find(
                report, f"Date range: {table}.{column} ⊆ dates",
            ).passed
