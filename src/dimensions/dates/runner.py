"""Runner for the dates dimension — config parsing, versioning, parquet write."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from src.exceptions import ConfigError, DimensionError
from src.utils import info, skip, stage, warn
from src.utils.logging_utils import debug
from src.utils.config_helpers import int_or as _int_or, bool_or as _bool_or, as_dict
from src.utils.output_utils import write_parquet_with_date32
from src.utils.static_schemas import DATE_ALL_COLUMNS
from src.versioning import should_regenerate, save_version

from .columns import get_date_rename_map, resolve_date_columns
from .generator import generate_date_table
from .helpers import _clamp_month, _normalize_override_dates, _require_start_end
from .weekly_fiscal import resolve_weekly_fiscal_cfg

if TYPE_CHECKING:
    from src.engine.config.config_schema import AppConfig

# Code-version component of the dates version key. Dates was previously the only
# dimension whose version key had no schema component, so shipped output changes
# (alias removals, offset fixes) never reached existing outputs until an
# unrelated config edit. Bump this whenever the dates *output* changes so all
# users get a one-time regen (release-note it).
DATES_SCHEMA_VERSION = 2


def _resolve_date_dimension_bounds(
    raw_start_ts: pd.Timestamp,
    raw_end_ts: pd.Timestamp,
    *,
    buffer_years: int,
    post_sale_tail_days: int,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Resolve conformed calendar coverage without extending sales demand."""
    buffer_years = max(0, int(buffer_years))
    post_sale_tail_days = max(0, int(post_sale_tail_days))
    start_date = pd.Timestamp(raw_start_ts.year - buffer_years, 1, 1)
    buffered_end = pd.Timestamp(raw_end_ts.year + buffer_years, 12, 31)
    event_horizon_end = pd.Timestamp(raw_end_ts) + pd.Timedelta(
        days=post_sale_tail_days)
    return start_date, max(buffered_end, event_horizon_end)


def run_dates(cfg: "AppConfig", parquet_folder: Path) -> None:
    out_path = parquet_folder / "dates.parquet"
    if not hasattr(cfg, "dates"):
        raise ConfigError("Missing required config section: 'dates'")

    dates_cfg = cfg.dates or {}

    defaults_dates = (
        getattr(cfg.defaults, "dates", None) if hasattr(cfg, "defaults") else None
    ) or (
        getattr(getattr(cfg, "_defaults", None), "dates", None)
    ) or {}

    version_cfg = as_dict(dates_cfg)
    version_cfg["global_dates"] = as_dict(defaults_dates) if defaults_dates else {}
    version_cfg["schema_version"] = DATES_SCHEMA_VERSION

    if not should_regenerate("dates", version_cfg, out_path):
        skip("Dates up-to-date")
        return

    override_dates = _normalize_override_dates(dates_cfg)

    raw_start = override_dates.get("start") or getattr(defaults_dates, "start", None)
    raw_end = override_dates.get("end") or getattr(defaults_dates, "end", None)
    raw_start_ts, raw_end_ts = _require_start_end(raw_start, raw_end)

    buffer_years = max(0, _int_or(dates_cfg.buffer_years, 1))
    post_sale_tail_days = max(0, _int_or(dates_cfg.post_sale_tail_days, 120))
    start_date, end_date = _resolve_date_dimension_bounds(
        raw_start_ts,
        raw_end_ts,
        buffer_years=buffer_years,
        post_sale_tail_days=post_sale_tail_days,
    )
    info(
        f"Dates: {start_date.date()} to {end_date.date()} "
        f"(±{buffer_years}yr buffer)"
    )

    fiscal_start_month = _clamp_month(_int_or(dates_cfg.fiscal_start_month, 5))

    as_of_date = dates_cfg.as_of_date or str(raw_end_ts.date())

    compression = dates_cfg.parquet_compression
    compression_level = dates_cfg.parquet_compression_level
    force_date32 = _bool_or(dates_cfg.force_date32, True)

    include = dates_cfg.include or {}
    wf_cfg = resolve_weekly_fiscal_cfg(getattr(include, "weekly_fiscal", None))

    with stage("Generating Dates"):
        df = generate_date_table(
            start_date,
            end_date,
            fiscal_start_month,
            as_of_date=as_of_date,
            weekly_cfg=wf_cfg,
        )

        # Inverse guard: a generated column absent from the registry can never
        # be exposed (resolve_date_columns only lists registry columns), so it
        # would silently vanish at ``df = df[cols]``. Surface it instead.
        registered = set(DATE_ALL_COLUMNS)
        unregistered = [c for c in df.columns if c not in registered]
        if unregistered:
            warn(f"Dates: generated columns absent from registry (will not be emitted): {unregistered}")

        rename_map = get_date_rename_map(dates_cfg)
        if rename_map:
            df = df.rename(columns=rename_map)

        cols = resolve_date_columns(dates_cfg)
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise DimensionError(f"Dates: requested columns missing from generator: {missing}")

        df = df[cols]

        write_parquet_with_date32(
            df,
            out_path,
            compression=compression,
            compression_level=compression_level,
            force_date32=force_date32,
        )

    save_version("dates", version_cfg, out_path)
    debug(f"Dates dimension written: {out_path.name}")
