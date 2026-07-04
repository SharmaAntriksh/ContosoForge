"""Base calendar columns and as-of relative offsets."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .helpers import _CAL_WEEK_REF, _EXCEL_EPOCH, _format_week_date_range


def add_calendar_columns(df: pd.DataFrame, *, as_of: pd.Timestamp) -> pd.DataFrame:
    """Add base calendar columns and as-of relative offsets to *df*.

    Expects *df* to contain a ``Date`` column (datetime64).
    """
    # DateKey: YYYYMMDD integer (arithmetic, avoids slow strftime).
    year = df["Date"].dt.year
    month = df["Date"].dt.month
    day = df["Date"].dt.day

    df["DateKey"] = (year * 10000 + month * 100 + day).astype(np.int64)

    # DateSerialNumber: Excel-like serial (2006-12-31 → 39082).
    df["DateSerialNumber"] = (df["Date"] - _EXCEL_EPOCH).dt.days.astype(np.int32)

    # Basic parts
    df["Year"] = year.astype(np.int32)
    df["Month"] = month.astype(np.int32)
    df["Day"] = day.astype(np.int32)
    df["Quarter"] = df["Date"].dt.quarter.astype(np.int32)

    df["MonthName"] = df["Date"].dt.strftime("%B")
    df["MonthShort"] = df["Date"].dt.strftime("%b")
    df["DayName"] = df["Date"].dt.strftime("%A")
    df["DayShort"] = df["Date"].dt.strftime("%a")

    df["DayOfYear"] = df["Date"].dt.dayofyear.astype(np.int32)

    df["MonthYear"] = df["Date"].dt.strftime("%b %Y")
    df["MonthYearKey"] = (df["Year"].astype(int) * 100 + df["Month"].astype(int)).astype(np.int32)

    # Common BI convenience keys/labels
    df["YearQuarterKey"] = (df["Year"].astype(int) * 10 + df["Quarter"].astype(int)).astype(np.int32)
    df["QuarterYear"] = "Q" + df["Quarter"].astype(str) + " " + df["Year"].astype(str)

    df["CalendarMonthIndex"] = (df["Year"].astype(int) * 12 + df["Month"].astype(int)).astype(np.int32)
    df["CalendarQuarterIndex"] = (df["Year"].astype(int) * 4 + df["Quarter"].astype(int)).astype(np.int32)

    # DayOfWeek conventions (compatible with earlier implementation):
    # 0 = Sunday, 1 = Monday, ... 6 = Saturday
    weekday = df["Date"].dt.weekday  # 0=Mon..6=Sun
    df["DayOfWeek"] = ((weekday + 1) % 7).astype(np.int32)  # 0=Sun..6=Sat

    df["IsWeekend"] = df["DayOfWeek"].isin([0, 6]).astype(bool)
    df["IsBusinessDay"] = (~df["IsWeekend"]).astype(bool)

    # Month start/end (pandas-native to avoid datetime64[D] casts)
    df["MonthStartDate"] = df["Date"].dt.to_period("M").dt.start_time.dt.normalize()
    df["MonthEndDate"] = df["Date"].dt.to_period("M").dt.end_time.dt.normalize()

    # Quarter start/end
    qperiod = df["Date"].dt.to_period("Q")
    df["QuarterStartDate"] = qperiod.dt.start_time.dt.normalize()
    df["QuarterEndDate"] = qperiod.dt.end_time.dt.normalize()

    df["IsMonthStart"] = (df["Day"] == 1).astype(bool)
    df["IsMonthEnd"] = df["Date"].dt.is_month_end.astype(bool)
    df["IsQuarterStart"] = df["Date"].dt.is_quarter_start.astype(bool)
    df["IsQuarterEnd"] = df["Date"].dt.is_quarter_end.astype(bool)
    df["IsYearStart"] = ((df["Month"] == 1) & (df["Day"] == 1)).astype(bool)
    df["IsYearEnd"] = ((df["Month"] == 12) & (df["Day"] == 31)).astype(bool)

    # Year boundary dates (arithmetic, avoids slow string concat)
    _years = df["Year"].astype(int).to_numpy()
    df["YearStartDate"] = pd.to_datetime(
        pd.DataFrame({"year": _years, "month": np.ones(len(df), dtype=np.int32), "day": np.ones(len(df), dtype=np.int32)})
    ).dt.normalize()
    df["YearEndDate"] = pd.to_datetime(
        pd.DataFrame({"year": _years, "month": np.full(len(df), 12, dtype=np.int32), "day": np.full(len(df), 31, dtype=np.int32)})
    ).dt.normalize()

    # Period duration columns
    df["MonthDays"] = (df["MonthEndDate"] - df["MonthStartDate"]).dt.days.add(1).astype(np.int32)
    df["QuarterDays"] = (df["QuarterEndDate"] - df["QuarterStartDate"]).dt.days.add(1).astype(np.int32)
    df["YearDays"] = (df["YearEndDate"] - df["YearStartDate"]).dt.days.add(1).astype(np.int32)

    # Day-of-quarter
    df["DayOfQuarter"] = (df["Date"] - df["QuarterStartDate"]).dt.days.add(1).astype(np.int32)

    df["WeekOfMonth"] = ((df["Day"] - 1) // 7 + 1).astype(np.int32)

    # ------------------------------------------------------------------
    # Calendar week (Sunday-based)
    # ------------------------------------------------------------------
    # DayOfWeek: 0=Sun..6=Sat (set above).  Sunday is the week start.
    df["CalendarWeekStartDate"] = (df["Date"] - pd.to_timedelta(df["DayOfWeek"], unit="D")).dt.normalize()
    df["CalendarWeekEndDate"] = (df["CalendarWeekStartDate"] + pd.Timedelta(days=6)).dt.normalize()

    # Week number within the year (1-based, partial first week = week 1).
    #
    # Convention: Sunday-based weeks with a partial first week counted as week 1.
    # This is arithmetically consistent but can yield week 53 or even 54 in a leap
    # year that starts on Saturday — e.g. 2000-12-31 and 2028-12-31 land in week 54.
    # Kept as-is deliberately (changing it would be a data-visible break for zero
    # benefit); BI consumers should expect the 1..54 range, not 1..52.
    #
    # Compute Jan 1 day-of-week once per unique year, then map back.
    unique_years = df["Year"].unique()
    jan1_timestamps = pd.to_datetime(unique_years.astype(str) + "-01-01")
    jan1_sun0 = ((jan1_timestamps.weekday + 1) % 7).astype(int)  # Sun=0..Sat=6
    year_to_jan1_sun0 = pd.Series(jan1_sun0, index=unique_years)
    jan1_mapped = df["Year"].map(year_to_jan1_sun0)
    df["CalendarWeekNumber"] = ((df["DayOfYear"].astype(int) + jan1_mapped - 1) // 7 + 1).astype(np.int32)

    # Contiguous week index (weeks since _CAL_WEEK_REF) and as-of offset.
    df["CalendarWeekIndex"] = (((df["CalendarWeekStartDate"] - _CAL_WEEK_REF).dt.days) // 7).astype(np.int32)
    as_of_cal_week_start = (as_of - pd.Timedelta(days=int((as_of.weekday() + 1) % 7))).normalize()
    as_of_cal_week_index = int(((as_of_cal_week_start - _CAL_WEEK_REF).days) // 7)
    df["CalendarWeekOffset"] = (df["CalendarWeekIndex"].astype(int) - as_of_cal_week_index).astype(np.int32)

    df["CalendarWeekDateRange"] = _format_week_date_range(
        df["CalendarWeekStartDate"], df["CalendarWeekEndDate"],
    )

    # ------------------------------------------------------------------
    # Next/Previous Business Day
    #
    # Semantics (strict):
    #   NextBusinessDay     = first business day *after* the current date
    #   PreviousBusinessDay = last business day *before* the current date
    #
    # Edge behaviour: if no such day exists within the generated date
    # range, the date falls back to itself.
    # ------------------------------------------------------------------
    biz_dates = df.loc[df["IsBusinessDay"] == 1, "Date"].to_numpy(dtype="datetime64[D]")
    date_vals = df["Date"].to_numpy(dtype="datetime64[D]")

    if biz_dates.size > 0:
        # side="right" → first biz_date strictly after current date
        idx_next = np.searchsorted(biz_dates, date_vals, side="right")
        # side="left" - 1 → last biz_date strictly before current date
        idx_prev = np.searchsorted(biz_dates, date_vals, side="left") - 1

        next_bd = date_vals.copy()
        prev_bd = date_vals.copy()

        ok_next = idx_next < biz_dates.size
        ok_prev = idx_prev >= 0

        next_bd[ok_next] = biz_dates[idx_next[ok_next]]
        prev_bd[ok_prev] = biz_dates[idx_prev[ok_prev]]

        df["NextBusinessDay"] = pd.to_datetime(next_bd).normalize()
        df["PreviousBusinessDay"] = pd.to_datetime(prev_bd).normalize()
    else:
        # Degenerate range with no business days; fall back to self.
        df["NextBusinessDay"] = df["Date"]
        df["PreviousBusinessDay"] = df["Date"]

    # ------------------------------------------------------------------
    # DatePrevious columns (same day in prior period, clamped to EOM)
    # ------------------------------------------------------------------
    df["DatePreviousWeek"] = (df["Date"] - pd.Timedelta(days=7)).dt.normalize()
    df["DatePreviousMonth"] = (df["Date"] - pd.DateOffset(months=1)).dt.normalize()
    df["DatePreviousQuarter"] = (df["Date"] - pd.DateOffset(months=3)).dt.normalize()
    df["DatePreviousYear"] = (df["Date"] - pd.DateOffset(years=1)).dt.normalize()

    # For month-end dates, snap each shifted date to end of its landing month
    is_month_end = df["Date"] == df["MonthEndDate"]
    if is_month_end.any():
        eom_idx = is_month_end[is_month_end].index
        df.loc[eom_idx, "DatePreviousMonth"] = (
            df["MonthStartDate"][eom_idx] - pd.Timedelta(days=1)
        ).dt.normalize()
        for col in ("DatePreviousQuarter", "DatePreviousYear"):
            shifted = pd.to_datetime(df.loc[eom_idx, col])
            df.loc[eom_idx, col] = (shifted + pd.offsets.MonthEnd(0)).dt.normalize()

    # ------------------------------------------------------------------
    # As-of relative columns
    # ------------------------------------------------------------------
    df["IsToday"] = (df["Date"] == as_of).astype(bool)
    df["IsCurrentYear"] = (df["Year"] == as_of.year).astype(bool)
    df["IsCurrentMonth"] = ((df["Year"] == as_of.year) & (df["Month"] == as_of.month)).astype(bool)
    current_quarter = (as_of.month - 1) // 3 + 1
    df["IsCurrentQuarter"] = ((df["Year"] == as_of.year) & (df["Quarter"] == current_quarter)).astype(bool)
    df["CurrentDayOffset"] = (df["Date"] - as_of).dt.days.astype(np.int32)

    # Offsets (relative to as_of) for fast "last N" slicing in Power BI
    df["YearOffset"] = (df["Year"].astype(int) - int(as_of.year)).astype(np.int32)
    as_of_cal_month_index = int(as_of.year) * 12 + int(as_of.month)
    as_of_cal_quarter_index = int(as_of.year) * 4 + int((as_of.month - 1) // 3 + 1)
    df["CalendarMonthOffset"] = (df["CalendarMonthIndex"].astype(int) - as_of_cal_month_index).astype(np.int32)
    df["CalendarQuarterOffset"] = (df["CalendarQuarterIndex"].astype(int) - as_of_cal_quarter_index).astype(np.int32)

    return df
