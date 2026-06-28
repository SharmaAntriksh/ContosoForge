from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pyarrow.parquet as pq

from src.defaults import SALES_CHANNEL_CORE_KEYS


# ------------------------------------------------------------
# Profile codes (internal only)
# ------------------------------------------------------------
P_RETAIL = np.int8(0)
P_DIGITAL = np.int8(1)
P_BUSINESS = np.int8(2)
P_ASSISTED = np.int8(3)

_PROFILE_FROM_STR = {
    "RETAIL": P_RETAIL,
    "DIGITAL": P_DIGITAL,
    "BUSINESS": P_BUSINESS,
    "ASSISTED": P_ASSISTED,
    "NA": P_DIGITAL,
    "UNKNOWN": P_DIGITAL,
}

_PROFILE_FROM_GROUP = {
    "physical": P_RETAIL,
    "digital": P_DIGITAL,
    "business": P_BUSINESS,
    "assisted": P_ASSISTED,
    "na": P_DIGITAL,
}


# ------------------------------------------------------------
# Hour-weight profiles (bin-agnostic: works for 1h/4h/6h/8h/etc.)
# ------------------------------------------------------------
RETAIL_HOUR_W = np.array(
    [0.002, 0.001, 0.001, 0.001, 0.002, 0.004,
     0.010, 0.020, 0.050, 0.080, 0.100, 0.110,
     0.110, 0.105, 0.095, 0.090, 0.085, 0.090,
     0.100, 0.095, 0.070, 0.040, 0.015, 0.006],
    dtype=np.float64,
)

DIGITAL_HOUR_W = np.array(
    [0.030, 0.025, 0.020, 0.020, 0.022, 0.026,
     0.030, 0.040, 0.050, 0.055, 0.060, 0.065,
     0.070, 0.070, 0.065, 0.060, 0.060, 0.065,
     0.080, 0.090, 0.090, 0.070, 0.050, 0.040],
    dtype=np.float64,
)

BUSINESS_HOUR_W = np.array(
    [0.001, 0.001, 0.001, 0.001, 0.001, 0.002,
     0.006, 0.020, 0.060, 0.090, 0.110, 0.120,
     0.120, 0.110, 0.100, 0.090, 0.070, 0.040,
     0.020, 0.010, 0.006, 0.003, 0.002, 0.001],
    dtype=np.float64,
)

ASSISTED_HOUR_W = np.array(
    [0.002, 0.001, 0.001, 0.001, 0.001, 0.003,
     0.010, 0.030, 0.060, 0.090, 0.110, 0.120,
     0.120, 0.110, 0.100, 0.090, 0.070, 0.040,
     0.020, 0.010, 0.006, 0.004, 0.003, 0.002],
    dtype=np.float64,
)


def _normalize_prob(w: np.ndarray) -> np.ndarray:
    w = np.asarray(w, dtype=np.float64)
    w = np.where(np.isfinite(w) & (w > 0), w, 0.0)
    s = float(w.sum())
    if s <= 1e-12:
        return np.full(w.shape[0], 1.0 / max(1, w.shape[0]), dtype=np.float64)
    return w / s


def _dims_parquet_folder(State: Any) -> Optional[Path]:
    # Common names across your codebase
    for attr in ("parquet_folder_p", "parquet_folder", "dimensions_parquet_folder", "dims_parquet_folder"):
        v = getattr(State, attr, None)
        if v:
            return Path(v)
    return None


_PROFILE_HOUR_WEIGHTS = [RETAIL_HOUR_W, DIGITAL_HOUR_W, BUSINESS_HOUR_W, ASSISTED_HOUR_W]


def _parse_time_str(t: object) -> int:
    """Parse 'HH:MM' string to hour (0-23). Returns -1 for None/invalid."""
    if t is None or (isinstance(t, float) and np.isnan(t)):
        return -1
    s = str(t).strip()
    if not s or s.lower() in ("none", "nan", "nat"):
        return -1
    parts = s.split(":")
    if len(parts) >= 2:
        try:
            return int(parts[0])
        except (ValueError, TypeError):
            return -1
    return -1


def _build_channel_hour_weights(
    profile_code: int, open_hour: int, close_hour: int,
) -> np.ndarray:
    """
    Build a 24-element normalized hour-weight array for a channel.

    Takes the base profile weights and zeros out hours outside the
    operating window [open_hour, close_hour). For 24/7 channels
    (open_hour < 0), returns the unmasked profile weights.
    """
    base = _PROFILE_HOUR_WEIGHTS[min(profile_code, len(_PROFILE_HOUR_WEIGHTS) - 1)].copy()
    if open_hour >= 0 and close_hour > open_hour:
        mask = np.ones(24, dtype=np.bool_)
        mask[:open_hour] = False
        mask[close_hour:] = False
        base *= mask
    return _normalize_prob(base)


def _load_sales_channels(State: Any) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """
    Returns (keys:int16[], p:float64[], channel_hour_lut:ndarray[max_key+1, 24]).

    channel_hour_lut[key] is a 24-element normalized hour-weight array
    that respects the channel's OpenTime/CloseTime operating window.
    Cached on State.
    """
    cached = getattr(State, "_sales_channels_cache", None)
    if cached is not None:
        return cached

    folder = _dims_parquet_folder(State)
    if folder is None:
        return None

    pth = folder / "channels.parquet"
    if not pth.exists():
        return None

    read_cols = ["ChannelKey", "ChannelGroup"]
    # Read OpenTime/CloseTime if available
    schema = pq.read_schema(pth)
    col_names = set(schema.names)
    has_times = "OpenTime" in col_names and "CloseTime" in col_names
    if has_times:
        read_cols += ["OpenTime", "CloseTime"]

    tab = pq.read_table(pth, columns=read_cols)
    keys = np.asarray(tab["ChannelKey"].to_numpy(), dtype=np.int32)
    grp = np.asarray(tab["ChannelGroup"].to_numpy(), dtype=object)
    open_times = np.asarray(tab["OpenTime"].to_numpy(), dtype=object) if has_times else None
    close_times = np.asarray(tab["CloseTime"].to_numpy(), dtype=object) if has_times else None

    # Exclude Unknown (0) from sampling if present
    m = keys != np.int32(0)
    keys = keys[m]
    grp = grp[m]
    if open_times is not None:
        open_times = open_times[m]
        close_times = close_times[m]

    if keys.size == 0:
        return None

    # Sampling prob: uniform across keys
    p = np.full(keys.shape[0], 1.0 / keys.shape[0], dtype=np.float64)

    # Per-channel hour weight LUT: channel_hour_lut[key] -> 24-element array
    maxk = int(keys.max())
    digital_w = _normalize_prob(DIGITAL_HOUR_W)
    channel_hour_lut = np.tile(digital_w, (maxk + 1, 1))  # default: digital

    for i, (k, g) in enumerate(zip(keys.astype(np.int32), grp)):
        profile_code = int(_PROFILE_FROM_GROUP.get(str(g).strip().lower(), P_DIGITAL))
        oh = _parse_time_str(open_times[i]) if open_times is not None else -1
        ch = _parse_time_str(close_times[i]) if close_times is not None else -1
        channel_hour_lut[k] = _build_channel_hour_weights(profile_code, oh, ch)

    State._sales_channels_cache = (keys, p, channel_hour_lut)
    return State._sales_channels_cache


def _sample_hour_weighted_minute(rng: np.random.Generator, size: int, hour_w: np.ndarray) -> np.ndarray:
    size = int(size)
    if size <= 0:
        return np.empty(0, dtype=np.int32)

    w = _normalize_prob(hour_w)
    hours = rng.choice(24, size=size, p=w).astype(np.int32)
    mins = rng.integers(0, 60, size=size, dtype=np.int32)
    return (hours * 60 + mins).astype(np.int32, copy=False)


def _sample_timekey_by_channel(
    rng: np.random.Generator,
    channel_keys: np.ndarray,
    channel_hour_lut: np.ndarray,
) -> np.ndarray:
    """Sample minute-of-day TimeKey values using per-channel hour weights."""
    keys = np.asarray(channel_keys, dtype=np.int32)
    out = np.empty(keys.shape[0], dtype=np.int32)

    # Group by unique channel key to batch-sample each channel's distribution
    unique_keys = np.unique(keys)
    for ck in unique_keys:
        ck_int = int(ck)
        mask = keys == ck
        n = int(mask.sum())
        if ck_int < channel_hour_lut.shape[0]:
            hour_w = channel_hour_lut[ck_int]
        else:
            hour_w = _normalize_prob(DIGITAL_HOUR_W)
        out[mask] = _sample_hour_weighted_minute(rng, n, hour_w)

    return out


_TIME_BINS = [
    ("Bin15mKey", 15), ("Bin30mKey", 30), ("Bin1hKey", 60),
    ("Bin6hKey", 360), ("Bin12hKey", 720),
]


def build_extra_columns(ctx: Dict[str, Any]) -> Dict[str, Any]:
    schema_types = ctx["schema_types"]
    out: Dict[str, Any] = {}

    State = ctx["State"]
    rng: np.random.Generator = ctx["rng"]
    n = int(ctx["n"])
    order_ids_int = ctx.get("order_ids_int", None)

    cache = _load_sales_channels(State)

    # Pre-compute order-level unique mapping once (reused by ChannelKey + TimeKey)
    _oid_first_idx = _oid_inv = None
    if order_ids_int is not None:
        oid = np.asarray(order_ids_int, dtype=np.int32)
        _, _oid_first_idx, _oid_inv = np.unique(
            oid, return_index=True, return_inverse=True,
        )

    # ----------------------------
    # ChannelKey
    # ----------------------------
    # If chunk_builder already produced ChannelKey (via store-channel
    # correlation), skip generation here — just read it from cols.
    sales_channel = None
    channel_hour_lut = None
    _prebuilt_ch = ctx["cols"].get("ChannelKey")
    if _prebuilt_ch is not None:
        # Already produced by chunk_builder's store-channel correlation
        sales_channel = np.asarray(_prebuilt_ch, dtype=np.int32)
        channel_hour_lut = cache[2] if cache is not None else None
    elif "ChannelKey" in schema_types:
        if cache is None:
            # last-resort fallback: match core channel keys from defaults
            keys = SALES_CHANNEL_CORE_KEYS
            p = np.full(keys.shape[0], 1.0 / keys.shape[0], dtype=np.float64)
        else:
            keys, p, channel_hour_lut = cache

        if _oid_first_idx is not None:
            _n_orders = len(_oid_first_idx)
            per_order_channel = rng.choice(keys, size=_n_orders, p=p).astype(np.int32, copy=False)
            sales_channel = per_order_channel[_oid_inv]
        else:
            sales_channel = rng.choice(keys, size=n, p=p).astype(np.int32, copy=False)

        out["ChannelKey"] = sales_channel
    else:
        channel_hour_lut = cache[2] if cache is not None else None

    # ----------------------------
    # TimeKey depends on Channel operating hours
    # ----------------------------
    if "TimeKey" in schema_types:
        if sales_channel is None:
            sc_base = ctx["cols"].get("ChannelKey")
            sales_channel = None if sc_base is None else np.asarray(sc_base, dtype=np.int32)

        if sales_channel is None or channel_hour_lut is None:
            # digital-like fallback so night bins aren't empty
            timekey = _sample_hour_weighted_minute(rng, n, DIGITAL_HOUR_W)
        else:
            if _oid_first_idx is not None:
                per_order_sc = sales_channel[_oid_first_idx]
                per_order_time = _sample_timekey_by_channel(rng, per_order_sc, channel_hour_lut)
                timekey = per_order_time[_oid_inv]
            else:
                timekey = _sample_timekey_by_channel(rng, sales_channel, channel_hour_lut)

        out["TimeKey"] = timekey

        # int32 division preserves dtype — no promotion to int64
        for col, divisor in _TIME_BINS:
            if col in schema_types:
                out[col] = timekey // divisor

    return out


__all__ = ["build_extra_columns"]
