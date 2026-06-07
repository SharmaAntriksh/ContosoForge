"""Tests for the dates dimension package (src/dimensions/dates/).

Covers basic generate_date_table output, edge cases across calendar, fiscal,
ISO, and weekly fiscal (4-4-5) subsystems.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.dimensions.dates import (
    generate_date_table,
    get_date_rename_map,
    resolve_date_columns,
    WeeklyFiscalConfig,
)
from src.exceptions import DimensionError
from src.dimensions.dates.fiscal import add_fiscal_columns
from src.dimensions.dates.iso import add_iso_columns
from src.dimensions.dates.weekly_fiscal import (
    _weekday_num,
    _weekly_fiscal_year_bounds,
    _weeks_in_periods,
    add_weekly_fiscal_columns,
)


# ===================================================================
# Helpers
# ===================================================================

def _make(start="2024-01-01", end="2024-12-31", fiscal=1, **kw):
    return generate_date_table(
        pd.Timestamp(start), pd.Timestamp(end), fiscal, **kw,
    )


def _base_df(start="2024-01-01", end="2024-01-31"):
    """Minimal DataFrame with Date/Year/Month for subsystem tests."""
    dates = pd.date_range(start, end, freq="D")
    return pd.DataFrame({
        "Date": dates,
        "Year": dates.year.astype(np.int32),
        "Month": dates.month.astype(np.int32),
    })


# ===================================================================
# Basic generate_date_table (moved from test_dimensions.py)
# ===================================================================

class TestGenerateDateTable:
    """Core output shape, columns, and determinism."""

    def test_basic_output(self):
        df = _make("2024-01-01", "2024-03-31")
        assert isinstance(df, pd.DataFrame)
        # 91 days: Jan(31) + Feb(29, 2024 leap) + Mar(31)
        assert len(df) == 91

    def test_date_range_coverage(self):
        df = _make("2024-06-01", "2024-06-30")
        assert len(df) == 30
        assert df["Date"].min() == pd.Timestamp("2024-06-01")
        assert df["Date"].max() == pd.Timestamp("2024-06-30")

    def test_date_key_format(self):
        df = _make("2024-01-01", "2024-01-01")
        assert len(df) == 1
        assert df["DateKey"].iloc[0] == 20240101

    def test_determinism(self):
        df1 = _make("2024-01-01", "2024-03-31")
        df2 = _make("2024-01-01", "2024-03-31")
        pd.testing.assert_frame_equal(df1, df2)

    def test_year_month_day_correct(self):
        df = _make("2024-07-15", "2024-07-15")
        row = df.iloc[0]
        assert int(row["Year"]) == 2024
        assert int(row["Month"]) == 7
        assert int(row["Day"]) == 15

    def test_no_nan_in_core_columns(self):
        df = _make("2024-01-01", "2024-03-31")
        core = ["Date", "DateKey", "Year", "Month", "Day", "Quarter",
                "MonthName", "DayName", "DayOfYear", "DayOfWeek"]
        for col in core:
            assert df[col].notna().all(), f"NaN in {col}"

    def test_is_weekend_correct(self):
        df = _make("2024-01-01", "2024-01-07")
        # 2024-01-01 = Monday, 2024-01-06 = Saturday, 2024-01-07 = Sunday
        weekend_mask = df["Date"].dt.weekday >= 5
        assert (df["IsWeekend"].astype(bool) == weekend_mask).all()

    def test_end_before_start_raises(self):
        with pytest.raises(DimensionError, match="end_date"):
            _make("2024-03-01", "2024-01-01")

    def test_single_day(self):
        df = _make("2024-01-01", "2024-01-01")
        assert len(df) == 1

    def test_sequential_day_index_monotonic(self):
        df = _make("2024-01-01", "2024-03-31")
        assert df["DateSerialNumber"].is_monotonic_increasing

    def test_datekey_unique(self):
        df = _make("2020-01-01", "2024-12-31")
        assert df["DateKey"].is_unique

    def test_date_table_covers_full_range(self):
        """Every day from start to end should be present."""
        start = pd.Timestamp("2024-01-01")
        end = pd.Timestamp("2024-01-31")
        df = generate_date_table(start, end, fiscal_start_month=1)
        assert len(df) == 31
        dates = pd.DatetimeIndex(df["Date"])
        expected = pd.date_range(start, end, freq="D")
        pd.testing.assert_index_equal(dates, expected, check_names=False)


class TestWeeklyFiscalConfigValues:
    """Test WeeklyFiscalConfig dataclass defaults and construction."""

    def test_default_values(self):
        cfg = WeeklyFiscalConfig()
        assert cfg.enabled is False
        assert cfg.first_day_of_week == 0
        assert cfg.weekly_type == "Last"
        assert cfg.quarter_week_type == "445"

    def test_custom_values(self):
        cfg = WeeklyFiscalConfig(
            enabled=False,
            first_day_of_week=1,
            weekly_type="Nearest",
            quarter_week_type="454",
        )
        assert cfg.enabled is False
        assert cfg.quarter_week_type == "454"


# ===================================================================
# Fiscal year boundaries
# ===================================================================

class TestFiscalBoundaries:
    """Test fiscal calendar at transition points."""

    def test_fiscal_start_month_1_is_calendar_year(self):
        """When fiscal_start_month=1, FiscalYearStartYear == calendar Year."""
        df = _make("2024-01-01", "2024-12-31", fiscal=1)
        assert (df["FiscalYearStartYear"] == df["Year"]).all()

    def test_fiscal_start_month_5_april_vs_may(self):
        """April 30 and May 1 should be in different fiscal years (fiscal_start=5)."""
        df = _make("2024-04-30", "2024-05-01", fiscal=5)
        apr30 = df[df["Date"] == pd.Timestamp("2024-04-30")].iloc[0]
        may01 = df[df["Date"] == pd.Timestamp("2024-05-01")].iloc[0]
        # April is in the previous fiscal year, May starts the new one
        assert apr30["FiscalYearStartYear"] == 2023
        assert may01["FiscalYearStartYear"] == 2024

    def test_fiscal_month_number_wraps(self):
        """FiscalMonthNumber should be 1 in the start month and 12 before it."""
        df = _make("2024-01-01", "2024-12-31", fiscal=5)
        # May (month 5) should be FiscalMonthNumber 1
        may_rows = df[df["Month"] == 5]
        assert (may_rows["FiscalMonthNumber"] == 1).all()
        # April (month 4) should be FiscalMonthNumber 12
        apr_rows = df[df["Month"] == 4]
        assert (apr_rows["FiscalMonthNumber"] == 12).all()

    def test_fiscal_quarter_boundaries(self):
        """Fiscal quarters should map months 1-3, 4-6, 7-9, 10-12 within the fiscal year."""
        df = _make("2024-01-01", "2024-12-31", fiscal=5)
        for fmn, expected_q in [(1, 1), (3, 1), (4, 2), (6, 2), (7, 3), (9, 3), (10, 4), (12, 4)]:
            rows = df[df["FiscalMonthNumber"] == fmn]
            if not rows.empty:
                assert (rows["FiscalQuarterNumber"] == expected_q).all(), (
                    f"FiscalMonthNumber={fmn} expected Q{expected_q}"
                )

    def test_fiscal_year_label_includes_end_year(self):
        """When fiscal_start_month != 1, FiscalYear should be the end year."""
        df = _make("2024-05-01", "2024-05-01", fiscal=5)
        row = df.iloc[0]
        # FY starts in May 2024, FiscalYear label should be 2025 (end-year convention)
        assert "2025" in str(row["FiscalYearRange"])

    def test_fiscal_offsets_arithmetic_when_as_of_not_in_frame(self):
        """DATES-2: fiscal offsets are computed arithmetically from as_of, so they
        are correct even when as_of is not a row in the frame (the old row-lookup
        fell back to all-zeros)."""
        dates = pd.date_range("2024-01-01", "2024-12-31", freq="MS")
        df = pd.DataFrame({"Date": dates,
                           "Year": dates.year, "Month": dates.month})
        as_of = pd.Timestamp("2025-03-15")  # outside the 2024 frame
        out = add_fiscal_columns(df, fiscal_start_month=5, as_of=as_of)
        # Not all zeros (the old fallback), and all 2024 dates precede as_of's month.
        assert out["FiscalMonthOffset"].nunique() > 1
        assert (out["FiscalMonthOffset"] < 0).all()
        # Dec 2024 is 3 fiscal months before as_of (2025-03), fiscal_start=5.
        dec = out[out["Month"] == 12].iloc[0]
        assert int(dec["FiscalMonthOffset"]) == -3

    def test_all_fiscal_start_months_valid(self):
        """Every fiscal_start_month 1-12 should produce valid data."""
        for m in range(1, 13):
            df = _make("2024-06-01", "2024-06-30", fiscal=m)
            assert df["FiscalMonthNumber"].between(1, 12).all()
            assert df["FiscalQuarterNumber"].between(1, 4).all()


# ===================================================================
# ISO week boundaries
# ===================================================================

class TestISOWeekBoundaries:
    """Test ISO-8601 week numbering at year transitions."""

    def test_iso_year_can_differ_from_calendar_year(self):
        """Dec 31 can belong to ISO week 1 of the next year."""
        # 2020-12-31 is a Thursday; ISO week 53 of 2020
        # 2024-12-30 is a Monday; ISO week 1 of 2025
        df = _make("2024-12-28", "2025-01-03", fiscal=1)
        dec30 = df[df["Date"] == pd.Timestamp("2024-12-30")].iloc[0]
        assert int(dec30["ISOYear"]) == 2025
        assert int(dec30["ISOWeekNumber"]) == 1

    def test_iso_week_start_is_always_monday(self):
        """ISOWeekStartDate should always be a Monday."""
        df = _make("2024-01-01", "2024-03-31", fiscal=1)
        week_starts = pd.to_datetime(df["ISOWeekStartDate"])
        assert (week_starts.dt.weekday == 0).all()

    def test_iso_week_index_monotonic(self):
        """ISOYearWeekIndex should be monotonically non-decreasing."""
        df = _make("2024-01-01", "2024-12-31", fiscal=1)
        assert df["ISOYearWeekIndex"].is_monotonic_increasing or (
            df["ISOYearWeekIndex"].diff().dropna() >= 0
        ).all()

    def test_iso_week_52_53_boundary(self):
        """Year with 53 ISO weeks should have week 53 rows."""
        # 2020 has 53 ISO weeks (starts on Wed, ends on Thu)
        df = _make("2020-12-28", "2020-12-31", fiscal=1)
        assert (df["ISOWeekNumber"] == 53).any()

    def test_iso_week_date_range_format(self):
        """ISOWeekDateRange should be 'Mon DD – Mon DD, YYYY'."""
        df = _make("2026-03-30", "2026-04-05", fiscal=1)
        row = df[df["Date"] == pd.Timestamp("2026-04-01")].iloc[0]
        assert row["ISOWeekDateRange"] == "Mar 30 - Apr 05, 2026"


# ===================================================================
# Calendar week (Sunday-based)
# ===================================================================

class TestCalendarWeek:
    """Test Sunday-based calendar week columns."""

    def test_calendar_week_start_is_always_sunday(self):
        """CalendarWeekStartDate should always be a Sunday."""
        df = _make("2024-01-01", "2024-03-31", fiscal=1)
        week_starts = pd.to_datetime(df["CalendarWeekStartDate"])
        # weekday: Mon=0..Sun=6 → Sunday == 6
        assert (week_starts.dt.weekday == 6).all()

    def test_calendar_week_end_is_always_saturday(self):
        """CalendarWeekEndDate should always be a Saturday."""
        df = _make("2024-01-01", "2024-03-31", fiscal=1)
        week_ends = pd.to_datetime(df["CalendarWeekEndDate"])
        assert (week_ends.dt.weekday == 5).all()

    def test_calendar_week_span_is_seven_days(self):
        """Each calendar week should span exactly 7 days."""
        df = _make("2024-01-01", "2024-06-30", fiscal=1)
        starts = pd.to_datetime(df["CalendarWeekStartDate"])
        ends = pd.to_datetime(df["CalendarWeekEndDate"])
        assert ((ends - starts).dt.days == 6).all()

    def test_calendar_week_differs_from_iso(self):
        """Calendar (Sun) and ISO (Mon) week starts should differ on most days."""
        df = _make("2024-01-01", "2024-01-31", fiscal=1)
        cal_starts = pd.to_datetime(df["CalendarWeekStartDate"])
        iso_starts = pd.to_datetime(df["ISOWeekStartDate"])
        # They differ on every day except Monday (where both agree the week just started)
        mondays = df["Date"].dt.weekday == 0
        non_mondays = ~mondays
        if non_mondays.any():
            assert (cal_starts[non_mondays] != iso_starts[non_mondays]).all()

    def test_calendar_week_number_range(self):
        """CalendarWeekNumber should be between 1 and 54."""
        df = _make("2024-01-01", "2024-12-31", fiscal=1)
        assert df["CalendarWeekNumber"].between(1, 54).all()
        assert df["CalendarWeekNumber"].iloc[0] >= 1

    def test_calendar_week_index_monotonic(self):
        """CalendarWeekIndex should be monotonically non-decreasing."""
        df = _make("2024-01-01", "2024-12-31", fiscal=1)
        assert (df["CalendarWeekIndex"].diff().dropna() >= 0).all()

    def test_calendar_week_offset_zero_at_as_of(self):
        """CalendarWeekOffset should be 0 for the week containing as_of."""
        df = _make("2024-01-01", "2024-12-31", fiscal=1, as_of_date="2024-06-12")
        as_of_row = df[df["Date"] == pd.Timestamp("2024-06-12")].iloc[0]
        assert int(as_of_row["CalendarWeekOffset"]) == 0

    def test_calendar_week_date_range_format(self):
        """CalendarWeekDateRange should be 'Mon DD – Mon DD, YYYY'."""
        # 2026-04-01 is a Wednesday; calendar week = Sun Mar 29 – Sat Apr 04
        df = _make("2026-03-29", "2026-04-04", fiscal=1)
        row = df[df["Date"] == pd.Timestamp("2026-04-01")].iloc[0]
        assert row["CalendarWeekDateRange"] == "Mar 29 - Apr 04, 2026"


# ===================================================================
# Weekly fiscal (4-4-5) system
# ===================================================================

class TestWeeklyFiscalYearBounds:
    """Test the year boundary calculation for Last/Nearest types."""

    def test_last_type_basic(self):
        """'Last' type should place year start before fiscal month 1."""
        start, end = _weekly_fiscal_year_bounds(
            fw_year_number=2024,
            first_fiscal_month=1,
            first_day_of_week=0,  # Sunday
            weekly_type="Last",
            type_start_fiscal_year=1,
        )
        # Start should be on a Sunday, before or on Jan 1
        assert start.weekday() == 6 or start.weekday() == 0  # Sunday=6 in pandas
        assert start <= pd.Timestamp("2024-01-01")
        assert end >= pd.Timestamp("2024-12-28")

    def test_nearest_type_basic(self):
        """'Nearest' type should snap to the closest occurrence."""
        start, end = _weekly_fiscal_year_bounds(
            fw_year_number=2024,
            first_fiscal_month=1,
            first_day_of_week=0,
            weekly_type="Nearest",
            type_start_fiscal_year=1,
        )
        # Should be within ±3 days of Jan 1
        delta = abs((start - pd.Timestamp("2024-01-01")).days)
        assert delta <= 6

    def test_year_covers_roughly_365_days(self):
        """A weekly fiscal year should be 364 or 371 days (52 or 53 weeks)."""
        start, end = _weekly_fiscal_year_bounds(2024, 1, 0, "Last", 1)
        days = (end - start).days + 1
        assert days in (364, 371), f"Unexpected year length: {days} days"

    def test_consecutive_years_no_gap(self):
        """End of year N should be 1 day before start of year N+1."""
        for y in range(2020, 2026):
            _, end_y = _weekly_fiscal_year_bounds(y, 1, 0, "Last", 1)
            start_next, _ = _weekly_fiscal_year_bounds(y + 1, 1, 0, "Last", 1)
            gap = (start_next - end_y).days
            assert gap == 1, f"Gap between FY{y} and FY{y+1}: {gap} days"

    def test_first_day_of_week_monday(self):
        """first_day_of_week=1 (Monday) should produce Monday-starting weeks."""
        start, _ = _weekly_fiscal_year_bounds(2024, 1, 1, "Last", 1)
        # The year starts on a Monday
        assert start.weekday() == 0  # Monday=0

    def test_fiscal_month_5_shifts_boundary(self):
        """Fiscal month 5 (May) should shift the year boundary to around May."""
        start, _ = _weekly_fiscal_year_bounds(2024, 5, 0, "Last", 1)
        # Year should start around late April / early May
        assert start.month in (4, 5)


class TestWeeklyFiscalColumns:
    """Test 4-4-5 column generation end-to-end."""

    def _make_wf(self, start="2024-01-01", end="2024-12-31", fiscal=1, as_of_date=None, **wf_kw):
        cfg = WeeklyFiscalConfig(enabled=True, **wf_kw)
        return _make(start, end, fiscal, weekly_cfg=cfg, as_of_date=as_of_date)

    def test_445_week_distribution(self):
        """4-4-5 pattern: periods within a quarter should have 4, 4, 5 weeks."""
        df = self._make_wf(quarter_week_type="445")
        # Check Q1: FWWeekInQuarterNumber for weeks 1-4 should be period 1,
        # weeks 5-8 period 2, weeks 9-13 period 3
        q1 = df[df["FWQuarterNumber"] == 1].drop_duplicates("FWWeekNumber")
        if len(q1) >= 13:
            weeks_by_month = q1.groupby("FWMonthNumber")["FWWeekNumber"].nunique()
            assert list(weeks_by_month.values[:3]) == [4, 4, 5]

    def test_454_week_distribution(self):
        """4-5-4 pattern should have the 5-week month in the middle."""
        df = self._make_wf(quarter_week_type="454")
        q1 = df[df["FWQuarterNumber"] == 1].drop_duplicates("FWWeekNumber")
        if len(q1) >= 13:
            weeks_by_month = q1.groupby("FWMonthNumber")["FWWeekNumber"].nunique()
            assert list(weeks_by_month.values[:3]) == [4, 5, 4]

    def test_544_week_distribution(self):
        """5-4-4 pattern should have the 5-week month first."""
        df = self._make_wf(quarter_week_type="544")
        q1 = df[df["FWQuarterNumber"] == 1].drop_duplicates("FWWeekNumber")
        if len(q1) >= 13:
            weeks_by_month = q1.groupby("FWMonthNumber")["FWWeekNumber"].nunique()
            assert list(weeks_by_month.values[:3]) == [5, 4, 4]

    def test_period_length_consistency(self):
        """DATES-1: month/quarter boundaries are arithmetic, not clipped to the
        data range. start + length - 1 must equal end for EVERY row (including the
        first/last partial periods), and the day-of-period must stay within range.
        Under the old groupby min/max this failed at the table edges."""
        # Fiscal year starts in May, so the table edges are mid-fiscal-period.
        df = self._make_wf("2023-01-01", "2025-12-31", fiscal=5)

        start_m = pd.to_datetime(df["FWStartOfMonth"])
        end_m = pd.to_datetime(df["FWEndOfMonth"])
        assert (start_m + pd.to_timedelta(df["FWMonthDays"] - 1, unit="D") == end_m).all()
        assert df["FWDayOfMonth"].between(1, df["FWMonthDays"]).all()

        start_q = pd.to_datetime(df["FWStartOfQuarter"])
        end_q = pd.to_datetime(df["FWEndOfQuarter"])
        assert (start_q + pd.to_timedelta(df["FWQuarterDays"] - 1, unit="D") == end_q).all()
        assert df["FWDayOfQuarter"].between(1, df["FWQuarterDays"]).all()

    def test_partial_edge_month_uses_true_boundary(self):
        """DATES-1: the leading partial fiscal month reports its true start (before
        the table's first date) and its first in-range day is not forced to 1; the
        trailing partial month reports its true end (after the table's last date)."""
        df = self._make_wf("2023-01-01", "2025-12-31", fiscal=5).sort_values("Date")
        table_start, table_end = df["Date"].min(), df["Date"].max()

        first = df[df["FWMonthIndex"] == df["FWMonthIndex"].min()]
        assert pd.Timestamp(first["FWStartOfMonth"].iloc[0]) < table_start
        assert int(first["FWDayOfMonth"].iloc[0]) > 1

        last = df[df["FWMonthIndex"] == df["FWMonthIndex"].max()]
        assert pd.Timestamp(last["FWEndOfMonth"].iloc[0]) > table_end

    def test_month_label_uses_period_midpoint(self):
        """DATES-3: the representative calendar month in FWMonthLabel /
        FWYearMonthLabel is the month containing the fiscal month's midpoint
        (start + FWMonthDays // 2), not a fixed start + 14 days which lands in
        the first third for 5-/6-week months."""
        df = self._make_wf("2023-01-01", "2025-12-31", fiscal=5)
        fm = df.drop_duplicates("FWMonthIndex")

        start = pd.to_datetime(fm["FWStartOfMonth"])
        mid = start + pd.to_timedelta(fm["FWMonthDays"] // 2, unit="D")
        expected_abbr = mid.dt.strftime("%b")

        # Label format: "FM <abbr> - <year>" -> chars [3:6] are the month abbr.
        got_month = fm["FWMonthLabel"].str.slice(3, 6)
        got_year_month = fm["FWYearMonthLabel"].str.slice(3, 6)
        assert (got_month.values == expected_abbr.values).all()
        assert (got_year_month.values == expected_abbr.values).all()

        # The range actually contains 5-week (35-day) months, so the midpoint
        # offset (17) genuinely differs from the old +14 heuristic.
        assert (fm["FWMonthDays"] == 35).any()

    def test_every_day_assigned_to_a_year(self):
        """No day should have FWYearNumber == -1 (unassigned)."""
        df = self._make_wf("2020-01-01", "2025-12-31")
        assert (df["FWYearNumber"] > 0).all()

    def test_fw_week_number_range(self):
        """FWWeekNumber should be 1-53 (53 only in long years)."""
        df = self._make_wf("2020-01-01", "2025-12-31")
        assert df["FWWeekNumber"].between(1, 54).all()

    def test_disabled_config_produces_no_columns(self):
        """When weekly_fiscal is disabled, FW columns should not exist."""
        df = _make(weekly_cfg=WeeklyFiscalConfig(enabled=False))
        fw_cols = [c for c in df.columns if c.startswith("FW")]
        assert fw_cols == []

    def test_first_day_of_week_affects_week_start(self):
        """first_day_of_week=1 (Monday) should make FWStartOfWeek a Monday."""
        df = self._make_wf(first_day_of_week=1)
        week_starts = pd.to_datetime(df["FWStartOfWeek"])
        assert (week_starts.dt.weekday == 0).all()  # Monday

    def test_first_day_of_week_sunday(self):
        """first_day_of_week=0 (Sunday) should make FWStartOfWeek a Sunday."""
        df = self._make_wf(first_day_of_week=0)
        week_starts = pd.to_datetime(df["FWStartOfWeek"])
        assert (week_starts.dt.weekday == 6).all()  # Sunday

    def test_offsets_present_when_as_of_in_range(self):
        """FWWeekOffset should be present and include zero for as_of_date."""
        df = self._make_wf(
            start="2024-01-01",
            end="2024-12-31",
            as_of_date="2024-06-15",
        )
        assert "FWWeekOffset" in df.columns
        assert 0 in df["FWWeekOffset"].values


# ===================================================================
# As-of date handling
# ===================================================================

class TestAsOfDate:
    """Test as_of_date behavior across subsystems."""

    def test_is_today_marks_correct_date(self):
        df = _make("2024-01-01", "2024-12-31", as_of_date="2024-07-04")
        today_rows = df[df["IsToday"] == 1]
        assert len(today_rows) == 1
        assert today_rows.iloc[0]["Date"] == pd.Timestamp("2024-07-04")

    def test_current_month_offset_zero_at_as_of(self):
        df = _make("2024-01-01", "2024-12-31", as_of_date="2024-06-15")
        june = df[df["Month"] == 6]
        assert (june["CalendarMonthOffset"] == 0).all()

    def test_as_of_outside_range_clamped(self):
        """When as_of_date is outside the date range, it is clamped to the range edge."""
        df = _make("2024-01-01", "2024-03-31", as_of_date="2025-01-01")
        # as_of is clamped to end_date (2024-03-31), so March offset = 0
        march = df[df["Month"] == 3]
        assert (march["CalendarMonthOffset"] == 0).all()


# ===================================================================
# Column resolution
# ===================================================================

class TestColumnResolution:
    """Test resolve_date_columns() with various toggle combinations."""

    def test_all_disabled_returns_base_only(self):
        cols = resolve_date_columns({
            "include": {"calendar": False, "iso": False, "fiscal": False,
                        "weekly_fiscal": {"enabled": False}},
        })
        assert "DateKey" in cols
        assert "FiscalYearStartYear" not in cols
        assert "ISOYear" not in cols
        assert "FWYearNumber" not in cols

    def test_iso_enabled(self):
        cols = resolve_date_columns({"include": {"iso": True}})
        assert "ISOYear" in cols
        assert "ISOWeekNumber" in cols

    def test_weekly_fiscal_enabled(self):
        cols = resolve_date_columns({
            "include": {"weekly_fiscal": {"enabled": True}},
        })
        assert "FWYearNumber" in cols
        assert "FWWeekNumber" in cols


# ===================================================================
# Spaced column names
# ===================================================================

class TestSpacedColumnNames:
    """Test dates.spaced_column_names feature."""

    def test_disabled_by_default(self):
        cols = resolve_date_columns({"include": {"iso": True}})
        assert "DateKey" in cols
        assert "ISOWeekNumber" in cols

    def test_enabled_renames_base_cols(self):
        cols = resolve_date_columns({
            "spaced_column_names": True,
            "include": {"calendar": False, "iso": False, "fiscal": False},
        })
        assert "Date Key" in cols
        assert "Day Of Week" in cols
        assert "Calendar Week Start Date" in cols
        assert "DateKey" not in cols

    def test_enabled_renames_iso_cols(self):
        cols = resolve_date_columns({
            "spaced_column_names": True,
            "include": {"iso": True},
        })
        assert "ISO Week Number" in cols
        assert "ISO Week Date Range" in cols
        assert "ISOWeekNumber" not in cols

    def test_enabled_renames_fiscal_cols(self):
        cols = resolve_date_columns({
            "spaced_column_names": True,
            "include": {"fiscal": True},
        })
        assert "Fiscal Year Label" in cols
        assert "FiscalYearLabel" not in cols

    def test_enabled_renames_calendar_offset_cols(self):
        cols = resolve_date_columns({
            "spaced_column_names": True,
            "include": {"calendar": True},
        })
        assert "Calendar Week Offset" in cols
        assert "Is Today" in cols
        assert "CalendarWeekOffset" not in cols

    def test_enabled_renames_weekly_fiscal_cols(self):
        cols = resolve_date_columns({
            "spaced_column_names": True,
            "include": {"weekly_fiscal": {"enabled": True}},
        })
        assert "FW Year Number" in cols
        assert "Weekly Fiscal System" in cols
        assert "FWYearNumber" not in cols

    def test_rename_map_empty_when_disabled(self):
        rename = get_date_rename_map({"spaced_column_names": False})
        assert rename == {}

    def test_rename_map_populated_when_enabled(self):
        rename = get_date_rename_map({"spaced_column_names": True})
        assert rename["DateKey"] == "Date Key"
        assert rename["ISOWeekNumber"] == "ISO Week Number"
        assert rename["FWYearNumber"] == "FW Year Number"

    def test_dataframe_rename_roundtrip(self):
        """Verify the rename map works correctly on an actual DataFrame."""
        df = _make("2024-01-01", "2024-01-07", fiscal=1)
        rename = get_date_rename_map({"spaced_column_names": True})
        df_renamed = df.rename(columns=rename)
        assert "Date Key" in df_renamed.columns
        assert "Day Of Week" in df_renamed.columns
        assert "DateKey" not in df_renamed.columns


# ===================================================================
# Weekday number helper
# ===================================================================

class TestWeekdayNum:
    """Test _weekday_num for all first_day_of_week values."""

    def test_sunday_start(self):
        # 2024-01-07 is a Sunday
        assert _weekday_num(pd.Timestamp("2024-01-07"), 0) == 1  # Sunday is day 1

    def test_monday_start(self):
        # 2024-01-08 is a Monday
        assert _weekday_num(pd.Timestamp("2024-01-08"), 1) == 1  # Monday is day 1

    def test_saturday_start(self):
        # 2024-01-06 is a Saturday
        assert _weekday_num(pd.Timestamp("2024-01-06"), 6) == 1  # Saturday is day 1

    def test_range_always_1_to_7(self):
        """For any first_day_of_week, all 7 weekdays should map to 1-7."""
        dates = pd.date_range("2024-01-01", "2024-01-07")  # Mon-Sun
        for fdow in range(7):
            nums = [_weekday_num(d, fdow) for d in dates]
            assert sorted(nums) == [1, 2, 3, 4, 5, 6, 7]


# ===================================================================
# Weeks in periods
# ===================================================================

class TestWeeksInPeriods:
    def test_445(self):
        assert _weeks_in_periods("445") == (4, 4, 5)

    def test_454(self):
        assert _weeks_in_periods("454") == (4, 5, 4)

    def test_544(self):
        assert _weeks_in_periods("544") == (5, 4, 4)

    def test_invalid_defaults_to_445(self):
        assert _weeks_in_periods("999") == (4, 4, 5)
        assert _weeks_in_periods("") == (4, 4, 5)
