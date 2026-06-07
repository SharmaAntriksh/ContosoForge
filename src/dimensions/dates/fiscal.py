"""Monthly fiscal calendar columns and offsets."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .helpers import _clamp_month


def add_fiscal_columns(
    df: pd.DataFrame,
    *,
    fiscal_start_month: int,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    """Add month-based fiscal calendar columns to *df*.

    Expects ``Date``, ``Year``, ``Month`` columns to already exist.
    """
    fy_start_month = _clamp_month(fiscal_start_month)

    df["FiscalYearStartYear"] = np.where(df["Month"] >= fy_start_month, df["Year"], df["Year"] - 1).astype(np.int32)
    df["FiscalMonthNumber"] = (((df["Month"].astype(int) - fy_start_month + 12) % 12) + 1).astype(np.int32)
    df["FiscalQuarterNumber"] = (((df["FiscalMonthNumber"] - 1) // 3) + 1).astype(np.int32)

    fy_end_add = 0 if fy_start_month == 1 else 1
    fiscal_year_end = (df["FiscalYearStartYear"].astype(int) + fy_end_add).astype(np.int32)

    if fy_start_month == 1:
        df["FiscalYearRange"] = df["FiscalYearStartYear"].astype(str)
    else:
        df["FiscalYearRange"] = df["FiscalYearStartYear"].astype(str) + "-" + fiscal_year_end.astype(str)
    df["FiscalQuarterLabel"] = "Q" + df["FiscalQuarterNumber"].astype(str) + " FY" + fiscal_year_end.astype(str)
    df["FiscalMonthName"] = df["Date"].dt.strftime("%B")
    df["FiscalMonthShort"] = df["Date"].dt.strftime("%b")

    # FiscalMonthIndex / FiscalQuarterIndex (canonical names; former
    # aliases FiscalYearMonthNumber and FiscalYearQuarterNumber removed).
    df["FiscalMonthIndex"] = (df["FiscalYearStartYear"].astype(int) * 12 + df["FiscalMonthNumber"].astype(int)).astype(np.int32)
    df["FiscalQuarterIndex"] = (df["FiscalYearStartYear"].astype(int) * 4 + df["FiscalQuarterNumber"].astype(int)).astype(np.int32)

    # Fiscal year start/end dates (arithmetic, avoids slow string concat).
    fy_start_years = df["FiscalYearStartYear"].astype(int).to_numpy()
    fy_start_months = np.full(len(df), fy_start_month, dtype=np.int32)
    fy_start_days = np.ones(len(df), dtype=np.int32)
    df["FiscalYearStartDate"] = pd.to_datetime(
        pd.DataFrame({"year": fy_start_years, "month": fy_start_months, "day": fy_start_days})
    ).dt.normalize()
    df["FiscalYearEndDate"] = (df["FiscalYearStartDate"] + pd.DateOffset(years=1) - pd.Timedelta(days=1)).dt.normalize()

    # Fiscal quarter start/end dates (arithmetic).
    fq_shift = (df["FiscalQuarterNumber"].astype(int) - 1) * 3
    fq_raw_month = df["FiscalYearStartDate"].dt.month + fq_shift
    fq_year = df["FiscalYearStartDate"].dt.year + ((fq_raw_month - 1) // 12)
    fq_month = (fq_raw_month - 1) % 12 + 1
    df["FiscalQuarterStartDate"] = pd.to_datetime(
        pd.DataFrame({"year": fq_year, "month": fq_month, "day": np.ones(len(df), dtype=np.int32)})
    ).dt.normalize()
    df["FiscalQuarterEndDate"] = (df["FiscalQuarterStartDate"] + pd.DateOffset(months=3) - pd.Timedelta(days=1)).dt.normalize()

    df["IsFiscalYearStart"] = (df["Date"] == df["FiscalYearStartDate"]).astype(bool)
    df["IsFiscalYearEnd"] = (df["Date"] == df["FiscalYearEndDate"]).astype(bool)
    df["IsFiscalQuarterStart"] = (df["Date"] == df["FiscalQuarterStartDate"]).astype(bool)
    df["IsFiscalQuarterEnd"] = (df["Date"] == df["FiscalQuarterEndDate"]).astype(bool)

    df["FiscalYear"] = fiscal_year_end.astype(np.int32)
    df["FiscalYearLabel"] = "FY " + df["FiscalYear"].astype(str)

    # Fiscal period duration and day-of-period columns
    df["FiscalQuarterDays"] = (
        df["FiscalQuarterEndDate"] - df["FiscalQuarterStartDate"]
    ).dt.days.add(1).astype(np.int32)
    df["FiscalYearDays"] = (
        df["FiscalYearEndDate"] - df["FiscalYearStartDate"]
    ).dt.days.add(1).astype(np.int32)
    df["FiscalDayOfQuarter"] = (
        df["Date"] - df["FiscalQuarterStartDate"]
    ).dt.days.add(1).astype(np.int32)
    df["FiscalDayOfYear"] = (
        df["Date"] - df["FiscalYearStartDate"]
    ).dt.days.add(1).astype(np.int32)

    # Fiscal offsets relative to as_of — computed arithmetically from as_of's own
    # fiscal indices (same formulas as the columns above), matching calendar.py's
    # offset logic. The old code located the as_of row in the frame and fell back to
    # all-zeros when as_of wasn't present; the arithmetic form is correct regardless
    # and never silently collapses to zero (DATES-2).
    _am = int(as_of.month)
    _a_fy_start = int(as_of.year) if _am >= fy_start_month else int(as_of.year) - 1
    _a_fiscal_month_number = ((_am - fy_start_month + 12) % 12) + 1
    _a_fiscal_quarter_number = ((_a_fiscal_month_number - 1) // 3) + 1
    as_of_fiscal_month_index = _a_fy_start * 12 + _a_fiscal_month_number
    as_of_fiscal_quarter_index = _a_fy_start * 4 + _a_fiscal_quarter_number
    as_of_fiscal_year = _a_fy_start + (0 if fy_start_month == 1 else 1)
    df["FiscalMonthOffset"] = (df["FiscalMonthIndex"].astype(int) - as_of_fiscal_month_index).astype(np.int32)
    df["FiscalQuarterOffset"] = (df["FiscalQuarterIndex"].astype(int) - as_of_fiscal_quarter_index).astype(np.int32)
    df["FiscalYearOffset"] = (df["FiscalYear"].astype(int) - as_of_fiscal_year).astype(np.int32)

    return df
