"""Column resolver for the dates dimension."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List

from src.utils.config_helpers import bool_or as _bool_or
from src.utils.static_schemas import _DATES_SPACED_NAMES, _dates_internal_columns


# ---------------------------------------------------------------------
# Column resolver
# ---------------------------------------------------------------------
# The column groups, spaced display names, include.* fallback defaults, and the
# selection logic itself all live in static_schemas.py's _DATES_REGISTRY +
# _dates_internal_columns (the single source of truth). resolve_date_columns just
# layers spaced-name renaming on top, so the parquet and SQL sides resolve the
# identical column set. Adding a Dates column is a one-row registry edit.
#
# Duplicate aliases previously consolidated: MonthNameShort→MonthShort,
# DayNameShort→DayShort, YearMonthKey→MonthYearKey, YearMonthLabel→MonthYear,
# YearQuarterLabel→QuarterYear, FiscalYearMonthNumber→FiscalMonthIndex,
# FiscalYearQuarterNumber→FiscalQuarterIndex, FWYearWeekLabel→FWWeekLabel,
# FWYearQuarterLabel→FWQuarterLabel.
# ---------------------------------------------------------------------

# PascalCase → spaced display names (registry-derived). Applied when
# dates.spaced_column_names is true; keys absent here keep their PascalCase name.
_SPACED_NAMES: Dict[str, str] = _DATES_SPACED_NAMES

def _spaced_column_names_enabled(dates_cfg: Dict[str, Any]) -> bool:
    """Return True if ``dates.spaced_column_names`` is set."""
    if isinstance(dates_cfg, dict):
        return _bool_or(dates_cfg.get("spaced_column_names"), False)
    if isinstance(dates_cfg, Mapping):
        return _bool_or(getattr(dates_cfg, "spaced_column_names", None), False)
    return False


def _apply_spaced_names(cols: List[str]) -> List[str]:
    """Replace PascalCase column names with spaced display names."""
    return [_SPACED_NAMES.get(c, c) for c in cols]


def get_date_rename_map(dates_cfg: Dict[str, Any]) -> Dict[str, str]:
    """Return ``{PascalCase: Spaced Name}`` dict when spaced names are enabled.

    Returns an empty dict when the feature is off, so callers can always do
    ``df.rename(columns=get_date_rename_map(cfg))``.
    """
    if not _spaced_column_names_enabled(dates_cfg):
        return {}
    return dict(_SPACED_NAMES)


def resolve_date_columns(dates_cfg: Dict[str, Any]) -> List[str]:
    """Build the ordered output column list based on config ``include`` flags.

    Thin wrapper over ``static_schemas._dates_internal_columns`` (the single
    registry-driven resolver shared with the SQL/DDL side): it returns the
    internal PascalCase names; this function only applies spaced-name renaming
    when ``dates.spaced_column_names`` is true (e.g. ``"Calendar Week Start
    Date"``). Because both output paths call the same resolver, the parquet and
    SQL column sets are identical by construction.
    """
    cols = _dates_internal_columns(dates_cfg)
    if _spaced_column_names_enabled(dates_cfg):
        cols = _apply_spaced_names(cols)
    return cols
