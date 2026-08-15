"""Tests for src.engine.quality_report."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.engine.quality_report import (
    QualityReport,
    _TableCache,
    _check_nulls_and_duplicates,
    _check_referential_integrity,
    _check_complaint_chronology,
    _check_subscription_lifecycle,
    _check_wishlist_conversion_chronology,
    _check_inventory_stockout_semantics,
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


class TestPhase2AnalyticalIntegrity:
    @staticmethod
    def _find(report, name):
        return next(c for c in report.checks if c.name == name)

    def test_complaint_chronology_and_response_arithmetic(self):
        sales = pd.DataFrame({
            "OrderNumber": [100],
            "OrderLineNumber": [1],
            "OrderDate": pd.to_datetime(["2024-02-10"]),
            "CustomerKey": [7],
        })
        complaints = pd.DataFrame({
            "OrderNumber": [100],
            "LineNumber": [1],
            "ComplaintDate": pd.to_datetime(["2024-02-12"]),
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
