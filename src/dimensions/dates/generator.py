"""Date table generator — orchestrates subsystem enrichment functions."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from src.exceptions import DimensionError
from src.utils import warn

from .helpers import _clamp_month, _safe_parse_as_of
from .calendar import add_calendar_columns
from .iso import add_iso_columns
from .fiscal import add_fiscal_columns
from .weekly_fiscal import WeeklyFiscalConfig, add_weekly_fiscal_columns


def generate_date_table(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    fiscal_start_month: int,
    *,
    as_of_date: Optional[str] = None,
    weekly_cfg: Optional[WeeklyFiscalConfig] = None,
) -> pd.DataFrame:
    """Build the full date table.

    ``weekly_cfg=None`` is the *direct-call* API default: weekly-fiscal columns
    are **disabled** — a bare programmatic call shouldn't silently emit ~40 extra
    columns. This is the single place OFF is the implicit choice; the class
    default is otherwise ON. The pipeline never relies on this — the runner always
    resolves an explicit config via ``resolve_weekly_fiscal_cfg``.
    """
    start_date = pd.to_datetime(start_date).normalize()
    end_date = pd.to_datetime(end_date).normalize()
    if end_date < start_date:
        raise DimensionError(
            f"end_date ({end_date}) must be >= start_date ({start_date}); "
            "check defaults.dates.start/end in config.yaml"
        )

    # pd.date_range with freq="D" already produces midnight-normalized dates.
    dates = pd.date_range(start_date, end_date, freq="D")
    df = pd.DataFrame({"Date": dates})

    as_of = _safe_parse_as_of(as_of_date, fallback=end_date)
    # Clamp as_of into the generated date window to keep offsets meaningful.
    if as_of < start_date:
        warn(f"Dates: as_of_date {as_of.date()} before start_date {start_date.date()}, clamping to {start_date.date()}")
        as_of = start_date
    elif as_of > end_date:
        warn(f"Dates: as_of_date {as_of.date()} after end_date {end_date.date()}, clamping to {end_date.date()}")
        as_of = end_date

    df = add_calendar_columns(df, as_of=as_of)
    df = add_iso_columns(df, as_of=as_of)
    df = add_fiscal_columns(df, fiscal_start_month=fiscal_start_month, as_of=as_of)

    # Explicit disabled default for the direct-API path (the class default is ON).
    weekly_cfg = weekly_cfg if weekly_cfg is not None else WeeklyFiscalConfig(enabled=False)
    fy_start_month = _clamp_month(fiscal_start_month)
    df = add_weekly_fiscal_columns(df, first_fiscal_month=fy_start_month, cfg=weekly_cfg, as_of=as_of)

    if weekly_cfg.enabled:
        df["WeeklyFiscalSystem"] = (
            f"Weekly ({weekly_cfg.quarter_week_type} "
            f"{str(weekly_cfg.weekly_type).strip().title()})"
        )
    return df
