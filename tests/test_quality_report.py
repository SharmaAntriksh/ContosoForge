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


class TestSalesOrderIntegrity:
    """QR-1: detect CHUNK-1 — duplicate SalesOrderNumber in the header and
    broken returns/complaints → header links."""

    @pytest.fixture
    def dims_facts(self, tmp_path):
        dims = tmp_path / "dimensions"; dims.mkdir()
        facts = tmp_path / "facts"; facts.mkdir()
        return dims, facts

    @staticmethod
    def _header(facts, so_numbers, dtype=np.int64):
        pd.DataFrame({
            "SalesOrderNumber": np.array(so_numbers, dtype=dtype),
        }).to_parquet(facts / "sales_order_header.parquet", index=False)

    @staticmethod
    def _find(report, name):
        return next((c for c in report.checks if c.name == name), None)

    def test_duplicate_order_number_in_header_is_flagged(self, dims_facts):
        dims, facts = dims_facts
        # SalesOrderNumber 1002 duplicated — the CHUNK-1 signature.
        self._header(facts, [1001, 1002, 1002, 1003])

        report = QualityReport()
        _check_nulls_and_duplicates(report, dims, facts, _TableCache())

        dup = self._find(report, "Duplicate PK: sales_order_header.SalesOrderNumber")
        assert dup is not None and dup.passed is False
        assert "1 duplicate" in dup.message

    def test_unique_order_number_in_header_passes(self, dims_facts):
        dims, facts = dims_facts
        self._header(facts, [1001, 1002, 1003])

        report = QualityReport()
        _check_nulls_and_duplicates(report, dims, facts, _TableCache())

        dup = self._find(report, "Duplicate PK: sales_order_header.SalesOrderNumber")
        assert dup is not None and dup.passed is True

    def test_returns_orphan_order_number_is_flagged(self, dims_facts):
        dims, facts = dims_facts
        self._header(facts, [1001, 1002, 1003])
        # 9999 has no matching header row.
        pd.DataFrame({
            "SalesOrderNumber": np.array([1001, 9999], dtype=np.int64),
            "ReturnReasonKey": np.array([1, 2], dtype=np.int64),
        }).to_parquet(facts / "sales_return.parquet", index=False)

        report = QualityReport()
        _check_referential_integrity(report, dims, facts, _TableCache())

        fk = self._find(
            report,
            "FK: sales_return.SalesOrderNumber → sales_order_header.SalesOrderNumber",
        )
        assert fk is not None and fk.passed is False
        assert "9999" in fk.details

    def test_complaints_null_order_number_is_ignored(self, dims_facts):
        dims, facts = dims_facts
        self._header(facts, [1001, 1002, 1003])
        # Unlinked complaints carry null SalesOrderNumber — must not count as orphans.
        pd.DataFrame({
            "SalesOrderNumber": pd.array([1002, None, 1003], dtype="Int64"),
        }).to_parquet(facts / "complaints.parquet", index=False)

        report = QualityReport()
        _check_referential_integrity(report, dims, facts, _TableCache())

        fk = self._find(
            report,
            "FK: complaints.SalesOrderNumber → sales_order_header.SalesOrderNumber",
        )
        assert fk is not None and fk.passed is True
