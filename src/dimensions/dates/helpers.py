from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Tuple

import pandas as pd

from src.exceptions import DimensionError


# Excel serial-date epoch: 1899-12-30 (not 12/31) due to the intentional
# Lotus 1-2-3 compatibility bug that treats 1900 as a leap year.
_EXCEL_EPOCH = pd.Timestamp("1899-12-30")

# ISO week reference: Monday of ISO week 1 in year 2000.
_ISO_WEEK_REF = pd.Timestamp("2000-01-03")

# Calendar week reference: Sunday of calendar week 1 in year 2000.
# Calendar weeks start on Sunday (DayOfWeek convention: 0=Sun..6=Sat).
_CAL_WEEK_REF = pd.Timestamp("2000-01-02")


def _format_week_date_range(start_dates: pd.Series, end_dates: pd.Series) -> pd.Series:
    """Format week date ranges as ``'Mon DD - Mon DD, YYYY'``."""
    return start_dates.dt.strftime("%b %d") + " - " + end_dates.dt.strftime("%b %d, %Y")


def _clamp_month(m: int) -> int:
    """Clamp an integer to the valid calendar-month range [1, 12]."""
    return max(1, min(12, int(m)))


def _normalize_override_dates(dates_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return the ``override`` dates dict from *dates_cfg* (empty if unset)."""
    if isinstance(dates_cfg, dict):
        override = dates_cfg.get("override") or {}
    else:
        override = dates_cfg.override or {}
    return dict(override) if isinstance(override, Mapping) else {}


def _require_start_end(raw_start: Any, raw_end: Any) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """Parse and validate the start/end date pair.

    An inverted range (end < start) is a config bug — raise rather than silently
    swap, matching the customers dimension. The shared
    ``config_helpers.parse_global_dates`` (used by employees, products, and
    complaints) now raises on the same condition too; the only difference is the
    exception type — this dimension-layer entry point raises ``DimensionError``
    while the shared helper raises ``ConfigError``.
    """
    if raw_start is None or raw_end is None or raw_start == "" or raw_end == "":
        raise DimensionError("Missing required start/end dates (defaults.dates or dates.override).")

    start_ts = pd.to_datetime(raw_start).normalize()
    end_ts = pd.to_datetime(raw_end).normalize()
    if end_ts < start_ts:
        raise DimensionError(
            f"Dates: end date ({raw_end}) is before start date ({raw_start}); "
            "check defaults.dates.start/end (or dates.override) in config.yaml"
        )
    return start_ts, end_ts


def _safe_parse_as_of(as_of_date: Any, fallback: pd.Timestamp) -> pd.Timestamp:
    """Parse *as_of_date* into a normalized Timestamp with a clear error on failure."""
    if not as_of_date:
        return fallback
    try:
        ts = pd.to_datetime(as_of_date).normalize()
    except (ValueError, TypeError) as exc:
        raise DimensionError(
            f"Unable to parse as_of_date={as_of_date!r} as a date. "
            "Provide an ISO-format string like '2025-12-31'."
        ) from exc
    if pd.isna(ts):
        raise DimensionError(f"as_of_date={as_of_date!r} parsed to NaT.")
    return ts
