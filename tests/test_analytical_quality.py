"""Regression guards for analytical defects found in the 20M-row review.

These tests intentionally build small invalid frames and assert that the quality
checks fail. Corrected variants then assert that the same guards pass. Generator
behavior is not changed in Phase 1 / Batch A.
"""
from __future__ import annotations

import pandas as pd

from src.engine.quality_report import (
    QualityReport,
    _check_complaint_chronology,
    _check_customer_sales_lifecycle,
    _check_promotion_alignment,
    _check_quantity_ceiling,
    _check_return_amount_integrity,
    _check_subscription_lifecycle,
    _check_wishlist_conversion_chronology,
    _find_check,
    generate_quality_report,
)


def _passed(report: QualityReport, name: str) -> bool:
    result = _find_check(report, name)
    assert result is not None, f"Expected quality check {name!r}"
    return result.passed


def test_return_amount_guard_reproduces_per_unit_double_division() -> None:
    sales = pd.DataFrame({
        "OrderNumber": [1001],
        "OrderLineNumber": [1],
        "NetPrice": [30.00],
    })
    returns = pd.DataFrame({
        "OrderNumber": [1001],
        "OrderLineNumber": [1],
        "ReturnQuantity": [2],
        # Defect signature: 30 / sold_qty(3) * returned_qty(2).
        "ReturnNetPrice": [20.00],
    })

    report = QualityReport()
    _check_return_amount_integrity(report, sales, returns)
    assert not _passed(report, "Return amount reconciliation")

    returns["ReturnNetPrice"] = 60.00
    report = QualityReport()
    _check_return_amount_integrity(report, sales, returns)
    assert _passed(report, "Return amount reconciliation")


def test_complaint_guard_reproduces_pre_sale_and_response_mismatch() -> None:
    sales = pd.DataFrame({
        "OrderNumber": [1001],
        "OrderLineNumber": [1],
        "OrderDate": ["2024-02-10"],
        "CustomerKey": [7],
    })
    customers = pd.DataFrame({
        "CustomerKey": [7],
        "CustomerStartDate": ["2024-01-01"],
        "CustomerEndDate": ["2024-12-31"],
    })
    complaints = pd.DataFrame({
        "OrderNumber": [1001],
        "LineNumber": [1],
        "CustomerKey": [7],
        "ComplaintDate": ["2024-02-01"],
        "ResolutionDate": ["2024-02-05"],
        "ResponseDays": [3],
    })

    report = QualityReport()
    _check_complaint_chronology(report, sales, complaints, customers)
    assert not _passed(report, "Complaint chronology (linked sale)")
    assert not _passed(report, "Complaint response-day arithmetic")

    complaints.loc[0, "ComplaintDate"] = "2024-02-11"
    complaints.loc[0, "ResolutionDate"] = "2024-02-14"
    report = QualityReport()
    _check_complaint_chronology(report, sales, complaints, customers)
    assert _passed(report, "Complaint chronology (linked sale)")
    assert _passed(report, "Complaint response-day arithmetic")


def test_subscription_guard_reproduces_customer_and_global_overrun() -> None:
    customers = pd.DataFrame({
        "CustomerKey": [7],
        "CustomerStartDate": ["2024-01-15"],
        "CustomerEndDate": ["2024-06-15"],
    })
    subscriptions = pd.DataFrame({
        "CustomerKey": [7],
        "PeriodStartDate": ["2024-01-01"],
        "PeriodEndDate": ["2024-06-30"],
    })

    report = QualityReport()
    _check_subscription_lifecycle(
        report, subscriptions, customers, global_end="2024-05-31")
    assert not _passed(report, "Subscription period within customer lifecycle")
    assert not _passed(report, "Subscription period within global end")

    subscriptions.loc[0, "PeriodStartDate"] = "2024-01-15"
    subscriptions.loc[0, "PeriodEndDate"] = "2024-05-31"
    report = QualityReport()
    _check_subscription_lifecycle(
        report, subscriptions, customers, global_end="2024-05-31")
    assert _passed(report, "Subscription period within customer lifecycle")
    assert _passed(report, "Subscription period within global end")


def test_promotion_guard_reproduces_flat_depth_and_loyalty_leakage() -> None:
    promotions = pd.DataFrame({
        "PromotionKey": [2, 3, 4],
        "DiscountPct": [0.05, 0.20, 0.50],
        "PromotionType": ["Seasonal", "Loyalty Exclusive", "Clearance"],
    })
    customers = pd.DataFrame({
        "CustomerKey": [10, 11, 12],
        "LoyaltyTierKey": [1, 0, 2],
    })
    sales = pd.DataFrame({
        "PromotionKey": [2, 3, 4],
        "CustomerKey": [10, 11, 12],
        "UnitPrice": [100.0, 100.0, 100.0],
        # Same realized depth despite very different nominal percentages.
        "DiscountAmount": [10.0, 10.0, 10.0],
    })

    report = QualityReport()
    _check_promotion_alignment(report, sales, promotions, customers)
    assert not _passed(report, "Promotion nominal-to-realized depth")
    assert not _passed(report, "Loyalty promotion eligibility")

    sales["DiscountAmount"] = [5.0, 20.0, 50.0]
    customers.loc[customers["CustomerKey"] == 11, "LoyaltyTierKey"] = 1
    report = QualityReport()
    _check_promotion_alignment(report, sales, promotions, customers)
    assert _passed(report, "Promotion nominal-to-realized depth")
    assert _passed(report, "Loyalty promotion eligibility")


def test_new_customer_promotion_guard_uses_exact_date_window() -> None:
    promotions = pd.DataFrame({
        "PromotionKey": [2, 3, 4],
        "DiscountPct": [0.05, 0.20, 0.50],
        "PromotionType": ["Seasonal", "Clearance", "New Customer"],
    })
    customers = pd.DataFrame({
        "CustomerKey": [10],
        "CustomerStartDate": ["2024-01-15"],
    })
    sales = pd.DataFrame({
        "PromotionKey": [2, 3, 4],
        "CustomerKey": [10, 10, 10],
        "OrderDate": ["2024-02-01", "2024-03-01", "2024-05-01"],
        "UnitPrice": [100.0, 100.0, 100.0],
        "DiscountAmount": [5.0, 20.0, 50.0],
    })

    report = QualityReport()
    _check_promotion_alignment(
        report,
        sales,
        promotions,
        customers,
        new_customer_window_months=3,
    )
    assert not _passed(report, "New-customer promotion window")

    sales.loc[sales["PromotionKey"] == 4, "OrderDate"] = "2024-04-15"
    report = QualityReport()
    _check_promotion_alignment(
        report,
        sales,
        promotions,
        customers,
        new_customer_window_months=3,
    )
    assert _passed(report, "New-customer promotion window")


def test_wishlist_guard_reproduces_purchase_before_added_date() -> None:
    sales = pd.DataFrame({
        "CustomerKey": [7],
        "ProductKey": [21],
        "OrderDate": ["2024-02-01"],
    })
    wishlists = pd.DataFrame({
        "CustomerKey": [7],
        "ProductKey": [21],
        "AddedDate": ["2024-02-10"],
    })

    report = QualityReport()
    _check_wishlist_conversion_chronology(
        report, sales, wishlists, expected_rate=1.0, tolerance=0.0)
    assert not _passed(report, "Wishlist dated conversion")

    # A later repeat purchase must count even when the first purchase predates
    # the wishlist event.
    sales.loc[1] = [7, 21, "2024-02-20"]
    report = QualityReport()
    _check_wishlist_conversion_chronology(
        report, sales, wishlists, expected_rate=1.0, tolerance=0.0)
    assert _passed(report, "Wishlist dated conversion")


def test_customer_lifecycle_guard_reproduces_end_month_leakage() -> None:
    customers = pd.DataFrame({
        "CustomerKey": [7],
        "CustomerStartDate": ["2024-01-01"],
        "CustomerEndDate": ["2024-06-15"],
    })
    sales = pd.DataFrame({
        "CustomerKey": [7],
        "OrderDate": ["2024-06-30"],
    })

    report = QualityReport()
    _check_customer_sales_lifecycle(report, sales, customers)
    assert not _passed(report, "Sales within customer lifecycle")

    sales.loc[0, "OrderDate"] = "2024-06-15"
    report = QualityReport()
    _check_customer_sales_lifecycle(report, sales, customers)
    assert _passed(report, "Sales within customer lifecycle")


def test_quantity_guard_reproduces_hard_ceiling_spike() -> None:
    clipped = pd.DataFrame({
        "Quantity": [1] * 20 + [2] * 23 + [3] * 17 + [4] * 40,
    })
    report = QualityReport()
    _check_quantity_ceiling(report, clipped, max_qty=4)
    assert not _passed(report, "Quantity terminal-bucket concentration")

    balanced = pd.DataFrame({
        "Quantity": [1] * 30 + [2] * 30 + [3] * 25 + [4] * 15,
    })
    report = QualityReport()
    _check_quantity_ceiling(report, balanced, max_qty=4)
    assert _passed(report, "Quantity terminal-bucket concentration")


def test_generate_report_includes_analytical_integrity_checks(tmp_path) -> None:
    dims = tmp_path / "dimensions"
    facts = tmp_path / "facts"
    dims.mkdir()
    facts.mkdir()

    pd.DataFrame({
        "CustomerKey": [7],
        "CustomerStartDate": pd.to_datetime(["2024-01-01"]),
        "CustomerEndDate": pd.to_datetime(["2024-12-31"]),
        "LoyaltyTierKey": [1],
    }).to_parquet(dims / "customers.parquet", index=False)
    pd.DataFrame({
        "OrderNumber": [1001],
        "OrderLineNumber": [1],
        "CustomerKey": [7],
        "ProductKey": [21],
        "OrderDate": pd.to_datetime(["2024-02-01"]),
        "Quantity": [2],
        "UnitPrice": [30.0],
        "NetPrice": [30.0],
        "DiscountAmount": [0.0],
        "PromotionKey": [1],
    }).to_parquet(facts / "sales.parquet", index=False)
    pd.DataFrame({
        "OrderNumber": [1001],
        "OrderLineNumber": [1],
        "ReturnQuantity": [2],
        "ReturnNetPrice": [20.0],
    }).to_parquet(facts / "returns.parquet", index=False)

    report_path = generate_quality_report(tmp_path)
    html = report_path.read_text(encoding="utf-8")
    assert "Analytical Integrity" in html
    assert "Return amount reconciliation" in html
    assert "20.00" in html and "60.00" in html
