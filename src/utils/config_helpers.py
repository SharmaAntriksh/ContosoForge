"""
Shared config-parsing and date-generation helpers used by dimension generators
(employees, employee_store_assignments, stores, etc.).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Tuple

import pandas as pd

from src.defaults import NS_PER_DAY as _NS_PER_DAY
from src.exceptions import ConfigError

import numpy as np


# ---------------------------------------------------------------------------
# Safe type coercion
# ---------------------------------------------------------------------------

def as_dict(x: Any) -> Dict[str, Any]:
    """Return *x* as a dict. Pydantic models are dumped; Mappings returned as-is; else empty dict."""
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if hasattr(x, "model_dump"):
        return x.model_dump()
    if isinstance(x, Mapping):
        return dict(x)
    return {}


def int_or(value: Any, default: int) -> int:
    """Convert *value* to ``int``, falling back to *default* on failure."""
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except (ValueError, TypeError):
        return int(default)


def float_or(value: Any, default: float) -> float:
    """Convert *value* to ``float``, falling back to *default* on failure."""
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (ValueError, TypeError):
        return float(default)


def bool_or(value: Any, default: bool) -> bool:
    """Convert *value* to ``bool``, falling back to *default* on failure.

    Accepts common truthy/falsy string representations including
    ``"t"``/``"f"``, ``"on"``/``"off"``, ``"yes"``/``"no"`` and numpy
    integer types.
    """
    if value is None:
        return bool(default)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, np.integer):
        return bool(int(value))
    elif isinstance(value, (int, float)):
        return bool(int(value))
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "t", "1", "yes", "y", "on"}:
            return True
        if s in {"false", "f", "0", "no", "n", "off"}:
            return False
    return bool(default)


def str_or(v: Any, default: str) -> str:
    """Return *v* as a stripped string, or *default* if empty/None."""
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def range2(v: Any, default_lo: float, default_hi: float) -> Tuple[float, float]:
    """Parse a 2-element list/tuple ``[lo, hi]``; ensures *hi >= lo*."""
    import math
    lo = default_lo
    hi = default_hi
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        lo = float_or(v[0], default_lo)
        hi = float_or(v[1], default_hi)
    # Guard against NaN/Inf from malformed config
    if not math.isfinite(lo):
        lo = default_lo
    if not math.isfinite(hi):
        hi = default_hi
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi



# ---------------------------------------------------------------------------
# Date window resolution
# ---------------------------------------------------------------------------

def parse_global_dates(
    cfg: Dict[str, Any],
    local_cfg: Dict[str, Any],
    *,
    allow_override: bool = False,
    dimension_name: str = "",
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """
    Resolve dataset-wide date window.

    Primary source: ``defaults.dates.{start, end}``

    When *allow_override* is True the function first checks
    ``local_cfg.override.dates.{start, end}`` (testing escape-hatch).

    Raises :class:`ConfigError` if the resolved range is inverted (end < start).
    An inverted range is a config bug — silently swapping it lets the other
    dimensions generate against the swapped window before the dates dimension
    aborts on the same condition, so this fails fast and consistently instead
    (matching ``dimensions/dates/helpers.py``).
    """
    if allow_override:
        _override = getattr(local_cfg, "override", None)
        if isinstance(_override, dict):
            _ov_dates = _override.get("dates")
        else:
            _ov_dates = getattr(_override, "dates", None) if _override is not None else None
        ov = as_dict(_ov_dates)
        if ov and ov.get("start") and ov.get("end"):
            gs = pd.to_datetime(ov["start"]).normalize()
            ge = pd.to_datetime(ov["end"]).normalize()
            if ge < gs:
                _label = f" for {dimension_name}" if dimension_name else ""
                raise ConfigError(
                    f"override dates are inverted{_label}: end ({ov['end']!r}) is "
                    f"before start ({ov['start']!r}). Fix the config rather than "
                    "relying on a silent swap."
                )
            return gs, ge

    _defaults = getattr(cfg, "defaults", None)
    _dates = getattr(_defaults, "dates", None) if _defaults is not None else None
    dd = as_dict(_dates)
    if dd and dd.get("start") and dd.get("end"):
        gs = pd.to_datetime(dd["start"]).normalize()
        ge = pd.to_datetime(dd["end"]).normalize()
        if ge < gs:
            _label = f" for {dimension_name}" if dimension_name else ""
            raise ConfigError(
                f"defaults.dates are inverted{_label}: end ({dd['end']!r}) is "
                f"before start ({dd['start']!r}). Fix the config rather than "
                "relying on a silent swap."
            )
        return gs, ge

    label = f" for {dimension_name}" if dimension_name else ""
    raise ConfigError(
        f"defaults.dates.start and defaults.dates.end are required{label}."
    )


# ---------------------------------------------------------------------------
# Date generation
# ---------------------------------------------------------------------------

def rand_dates_between(
    rng: np.random.Generator,
    start: pd.Timestamp,
    end: pd.Timestamp,
    n: int,
) -> pd.Series:
    """Return *n* random dates in ``[start, end]`` as a ``datetime64[ns]`` Series."""
    start = pd.to_datetime(start).normalize()
    end = pd.to_datetime(end).normalize()
    if end < start:
        start, end = end, start
    start_i = int(start.value // _NS_PER_DAY)
    end_i = int(end.value // _NS_PER_DAY)
    days = rng.integers(start_i, end_i + 1, size=int(n), dtype=np.int64)
    dt = pd.to_datetime(days.astype("datetime64[D]")).normalize()
    return pd.Series(dt, dtype="datetime64[ns]")


def rand_single_date(
    rng: np.random.Generator,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.Timestamp:
    """Sample one date in ``[start, end]`` without creating a pandas Series."""
    start = pd.to_datetime(start).normalize()
    end = pd.to_datetime(end).normalize()
    if end < start:
        start, end = end, start
    start_i = int(start.value // _NS_PER_DAY)
    end_i = int(end.value // _NS_PER_DAY)
    day = int(rng.integers(start_i, end_i + 1))
    return pd.Timestamp(day, unit="D")


# ---------------------------------------------------------------------------
# Geography / region mapping
# ---------------------------------------------------------------------------

_AMERICAS_CODES = frozenset({"USD", "CAD", "MXN", "BRL", "ARS", "CLP", "COP", "PEN"})
_EUROPE_CODES = frozenset({"EUR", "GBP", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON"})
_APAC_CODES = frozenset({
    "AUD", "NZD", "JPY", "CNY", "HKD", "SGD",
    "KRW", "TWD", "THB", "IDR", "PHP", "VND", "MYR",
})


def region_from_iso_code(code: str, default_region: str = "US") -> str:
    """Map an ISO/currency code to a name-pool region identifier."""
    c = (code or "").strip().upper()
    if c in {"INR"}:
        return "IN"
    if c in _AMERICAS_CODES:
        return "US"
    if c in _EUROPE_CODES:
        return "EU"
    if c in _APAC_CODES:
        return "AS"
    return default_region
