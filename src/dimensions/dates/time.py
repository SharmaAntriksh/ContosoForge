# src/dimensions/dates/time.py
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

import numpy as np
import pandas as pd

from src.dimensions.lookups import _run_lookup_dim

if TYPE_CHECKING:
    from src.engine.config.config_schema import AppConfig


# ── label helpers ────────────────────────────────────────────────────

def _fmt_hhmm(minute_of_day: int) -> str:
    """Format minute-of-day (0..1440) as HH:MM; allow 1440 => 24:00."""
    if minute_of_day == 24 * 60:
        return "24:00"
    h, m = divmod(int(minute_of_day), 60)
    return f"{h:02d}:{m:02d}"


def _label_range(start_min: int, width_min: int) -> str:
    """Half-open label like 13:30-13:45."""
    return f"{_fmt_hhmm(start_min)}-{_fmt_hhmm(start_min + width_min)}"


def _label_inclusive(start_min: int, block_min: int) -> str:
    """Inclusive-end label like 00:00-05:59 (for hour-level blocks)."""
    end_min = min(24 * 60 - 1, start_min + block_min - 1)
    return f"{_fmt_hhmm(start_min)}-{_fmt_hhmm(end_min)}"


# ── period-of-day (6 segments) ──────────────────────────────────────

_PERIOD_BOUNDARIES = np.array([0, 5, 9, 12, 17, 21], dtype=np.int32)
_PERIOD_NAMES = np.array(
    ["Midnight", "Early Morning", "Morning", "Afternoon", "Evening", "Night"]
)
_PERIOD_SORT = np.array([1, 2, 3, 4, 5, 6], dtype=np.int32)


def _period_of_day(hour24: np.ndarray):
    """Map hour (0-23) to period-of-day index (0-5)."""
    return np.searchsorted(_PERIOD_BOUNDARIES, hour24, side="right") - 1


# ── bin definitions (divisor, suffix, label style) ──────────────────

_BIN_DEFS = [
    (15,  "15m", "range"),
    (30,  "30m", "range"),
    (60,  "1h",  "inclusive"),
    (360, "6h",  "inclusive"),
    (720, "12h", "inclusive"),
]


# ── main builder ────────────────────────────────────────────────────

def _df_time_table(dim_cfg: Dict[str, Any]) -> pd.DataFrame:
    """Minute-grain Time dimension (1440 rows) with precomputed rollup bins.

    Config:
      time:
        include_labels: true|false   # default true
        parquet_compression: snappy  # handled by _run_lookup_dim
    """
    include_labels = bool(dim_cfg.get("include_labels", True))

    t_arr = np.arange(24 * 60, dtype=np.int32)
    hour24 = t_arr // 60
    minute = t_arr % 60

    hour12_mod = hour24 % 12
    hour12 = np.where(hour12_mod == 0, 12, hour12_mod).astype(np.int32)
    am_pm = np.where(hour24 < 12, "AM", "PM")

    time_text = np.char.add(
        np.char.zfill(hour24.astype(str), 2),
        np.char.add(":", np.char.zfill(minute.astype(str), 2)),
    )
    hour12_text = np.char.add(
        np.char.add(hour12.astype(str), " "),
        am_pm,
    )

    # Period of day (6 segments)
    pod_idx = _period_of_day(hour24)

    data: dict = {
        "TimeKey": t_arr,
        "Hour24": hour24,
        "Hour12": hour12,
        "Minute": minute,
        "AmPm": am_pm,
        "Hour12Text": hour12_text,
        "TimeText": time_text,
        "PeriodOfDay": _PERIOD_NAMES[pod_idx],
        "PeriodOfDaySort": _PERIOD_SORT[pod_idx],
    }

    # Bin sort keys are always included (useful for joins and aggregation).
    bin_keys: dict[str, np.ndarray] = {}
    for divisor, suffix, _ in _BIN_DEFS:
        k = t_arr // divisor
        bin_keys[suffix] = k
        data[f"Bin{suffix}Key"] = k

    if include_labels:
        _v_range = np.vectorize(
            lambda k, w: _label_range(int(k) * w, w), otypes=[object]
        )
        _v_incl = np.vectorize(
            lambda k, w: _label_inclusive(int(k) * w, w), otypes=[object]
        )
        for divisor, suffix, style in _BIN_DEFS:
            fn = _v_range if style == "range" else _v_incl
            data[f"Bin{suffix}Label"] = fn(bin_keys[suffix], divisor)
        data["Bin12hName"] = np.where(hour24 < 12, "Before Noon", "After Noon")

    data["TimeSeconds"] = (hour24 * 3600 + minute * 60).astype(np.int32)
    # HH:MM:SS text — Power Query parses via Time.FromText
    data["TimeOfDay"] = np.char.add(time_text, ":00")

    return pd.DataFrame(data)


def run_time_table(cfg: "AppConfig", parquet_folder: Path) -> None:
    """Public runner: cfg key "time", output time.parquet."""
    _run_lookup_dim(
        cfg=cfg,
        dim_key="time",
        out_name="time.parquet",
        build_df=_df_time_table,
        parquet_folder=parquet_folder,
    )


__all__ = ["run_time_table"]
