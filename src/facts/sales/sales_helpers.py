"""Leaf helpers shared across the sales-fact modules.

Low-level, dependency-free (stdlib + numpy + pandas + a couple of stable ``src``
utilities) helpers used by the dimension loaders, correlation lookups, output
assembler, and the ``generate_sales_fact`` orchestrator. Kept in a leaf module so
every sales sub-module can import them without importing ``sales.py`` (which would
create an import cycle).
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from src.exceptions import SalesError
from src.utils.config_helpers import int_or as _int_or


def _as_np(x, dtype=None) -> np.ndarray:
    """Works for pandas Series/Index AND for already-materialized numpy arrays."""
    return np.asarray(x, dtype=dtype)


def _bool_mask(x) -> np.ndarray:
    """Ensure we always have a numpy bool mask."""
    return np.asarray(x, dtype=bool)


def ensure_dir(path: Union[str, Path]) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def load_parquet_column(path: Union[str, Path], col: str) -> np.ndarray:
    """
    Load a single parquet column as a numpy array.
    """
    s = pd.read_parquet(str(path), columns=[col])[col]
    return _as_np(s)


def load_parquet_df(path: Union[str, Path], cols: Optional[Sequence[str]] = None) -> pd.DataFrame:
    return pd.read_parquet(str(path), columns=list(cols) if cols is not None else None)


def _cfg_get(cfg: Any, path: Sequence[str], default: Any = None) -> Any:
    cur = cfg
    for k in path:
        if not isinstance(cur, Mapping):
            return default
        # Prefer attribute access (Pydantic models) over dict access
        if hasattr(cur, k):
            cur = getattr(cur, k)
        elif isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _apply_cfg_default(current: Any, default: Any, cfg_value: Any) -> Any:
    """
    Treat cfg as source-of-truth defaults when call-site leaves args at their defaults.
    """
    if cfg_value is None:
        return current
    return cfg_value if current == default else current


def _normalize_dt_any(x) -> Union[pd.Series, pd.DatetimeIndex]:
    """
    Normalize date-like inputs to midnight.
    Handles Series (has .dt) and DatetimeIndex (has .normalize()).
    """
    dt = pd.to_datetime(x, errors="coerce")
    return dt.dt.normalize() if hasattr(dt, "dt") else dt.normalize()


def build_weighted_date_pool(start: str, end: str, seed: int = 42) -> Tuple[np.ndarray, np.ndarray]:
    """
    Build a weighted daily date pool with realistic seasonality.
    Returns:
      date_pool: datetime64[D] array
      date_prob: normalized probabilities
    """
    rng = np.random.default_rng(_int_or(seed, 42))

    dates = pd.date_range(start, end, freq="D")
    n = len(dates)
    if n <= 0:
        raise SalesError("Date range produced an empty pool")

    weekdays = _as_np(dates.weekday)

    # Weekday effect (0=Mon..6=Sun) — within-month date distribution only.
    # Retail-typical pattern: midweek soft, Friday strong, weekend peaks.
    # Year growth, monthly seasonality, promotional spikes, and one-off trends
    # are controlled by macro_demand settings in models.yaml.
    weekday_w = np.array([0.85, 0.85, 0.90, 0.95, 1.10, 1.20, 1.15], dtype=np.float64)
    wdw = weekday_w[weekdays]

    noise = rng.uniform(0.98, 1.02, size=n).astype(np.float64)

    weights = wdw * noise

    # Occasional zero-sales days (outages / closures); kept low so day-level
    # charts don't show a dead day every week.
    blackout_rate = rng.uniform(0.01, 0.03)
    blackout = rng.random(n) < blackout_rate
    weights[_bool_mask(blackout)] = 0.0

    total = float(weights.sum())
    if total <= 0:
        weights[:] = 1.0 / n
    else:
        weights /= total
        # Clamp last element to prevent FP rounding from leaving sum != 1.0,
        # which causes searchsorted out-of-bounds (CLAUDE.md gotcha #16).
        # max(0, ...) guards against FP overshoot making it negative.
        weights[-1] = max(0.0, 1.0 - weights[:-1].sum())

    return dates.to_numpy("datetime64[D]"), weights


def _normalize_nullable_int_month(arr: Any, n: int) -> np.ndarray:
    """
    Normalize CustomerEndMonth into int64 with -1 meaning "no end".
    """
    if arr is None:
        return np.full(n, -1, dtype=np.int64)

    s = pd.Series(arr)
    v = pd.to_numeric(s, errors="coerce").fillna(-1).astype("int64").to_numpy(copy=True)
    v[v < 0] = -1
    if v.shape[0] != n:
        v = np.resize(v, n)
    return v


_CSV_COPY_BUF = 1 << 22  # 4 MiB block size for byte-level CSV concatenation
