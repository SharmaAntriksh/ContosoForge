"""ISO week columns and offsets."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .helpers import _ISO_WEEK_REF, _format_week_date_range


def add_iso_columns(df: pd.DataFrame, *, as_of: pd.Timestamp) -> pd.DataFrame:
    """Add ISO-8601 week columns and as-of week offset to *df*.

    Expects a ``Date`` column; ``ISOWeekStartDate`` (ISO Monday-based) is
    computed here.
    """
    iso = df["Date"].dt.isocalendar()
    df["ISOWeekNumber"] = iso.week.astype(np.int32)
    df["ISOYear"] = iso.year.astype(np.int32)

    df["ISOWeekStartDate"] = (df["Date"] - pd.to_timedelta(df["Date"].dt.weekday, unit="D")).dt.normalize()
    df["ISOWeekEndDate"] = (df["ISOWeekStartDate"] + pd.Timedelta(days=6)).dt.normalize()

    # ISO week index: contiguous week number relative to _ISO_WEEK_REF.
    df["ISOYearWeekIndex"] = (((df["ISOWeekStartDate"] - _ISO_WEEK_REF).dt.days) // 7).astype(np.int32)
    as_of_week_start = (as_of - pd.Timedelta(days=int(as_of.weekday()))).normalize()
    as_of_iso_year_week_index = int(((as_of_week_start - _ISO_WEEK_REF).days) // 7)
    df["ISOWeekOffset"] = (df["ISOYearWeekIndex"].astype(int) - as_of_iso_year_week_index).astype(np.int32)

    df["ISOWeekDateRange"] = _format_week_date_range(
        df["ISOWeekStartDate"], df["ISOWeekEndDate"],
    )

    return df
