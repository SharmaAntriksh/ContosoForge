"""
Sales pricing pipeline (single-pass).

The sole pricing transform for Sales rows:
  base price → inflation drift → appearance snap → markdown draw → round

Config source: models.yaml -> models.pricing
Runtime state:  State.models_cfg  (the inner "models" dict)
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from src.facts.sales.sales_logic import State
from src.utils.logging_utils import warn


# ===============================================================
# Helpers
# ===============================================================

def _as_f64(x):
    """Coerce to float64, replacing non-finite values with 0."""
    a = np.asarray(x, dtype=np.float64)
    return np.where(np.isfinite(a), a, 0.0)


def _safe_prob(w: np.ndarray) -> np.ndarray:
    """Normalize non-negative weights to a valid probability vector."""
    w = np.asarray(w, dtype=np.float64)
    w = np.where(np.isfinite(w), w, 0.0)
    w = np.clip(w, 0.0, None)
    s = float(w.sum())
    if s <= 0.0:
        return np.full(w.shape[0], 1.0 / max(1, w.shape[0]), dtype=np.float64)
    return w / s


# ===============================================================
# Band / ending parsers
# ===============================================================

def _parse_bands(bands, default):
    """
    Parse a list of {max, step} dicts into sorted (max_arr, step_arr).

    Falls back to *default* (list of tuples) when bands is empty/invalid.
    """
    out = []
    if isinstance(bands, list):
        for b in bands:
            if not isinstance(b, Mapping):
                continue
            try:
                mx, st = float(b["max"]), float(b["step"])
            except (KeyError, TypeError, ValueError):
                warn(f"Skipping invalid pricing band: {b}")
                continue
            if mx > 0 and st > 0:
                out.append((mx, st))

    if not out:
        out = list(default)

    out.sort(key=lambda t: t[0])
    maxs = np.asarray([m for m, _ in out], dtype=np.float64)
    steps = np.asarray([s for _, s in out], dtype=np.float64)
    if maxs.size == 0:
        maxs = np.asarray([1e18], dtype=np.float64)
        steps = np.asarray([0.01], dtype=np.float64)
    return maxs, steps


def _parse_endings(endings, *, default_if_missing: bool):
    """
    Parse a list of {value, weight} dicts into (values_arr, probs_arr).

    Returns (None, None) if no valid endings and default_if_missing=False.
    """
    if not isinstance(endings, list) or len(endings) == 0:
        if not default_if_missing:
            return None, None
        endings = [{"value": 0.99, "weight": 0.70},
                   {"value": 0.50, "weight": 0.25},
                   {"value": 0.00, "weight": 0.05}]

    vals, wts = [], []
    for e in endings:
        if not isinstance(e, Mapping):
            continue
        try:
            v = float(e.get("value", 0.0))
            w = float(e.get("weight", 0.0))
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        vals.append(float(np.clip(v, 0.0, 0.99)))
        wts.append(w)

    if not vals:
        if not default_if_missing:
            return None, None
        vals, wts = [0.99], [1.0]

    return np.asarray(vals, dtype=np.float64), _safe_prob(np.asarray(wts))


# ===============================================================
# Quantization helpers
# ===============================================================

def _choose_step(x: np.ndarray, band_max: np.ndarray, band_step: np.ndarray) -> np.ndarray:
    """Pick the step size for each value based on magnitude bands."""
    idx = np.searchsorted(band_max, np.asarray(x, dtype=np.float64), side="left")
    idx = np.minimum(idx, band_step.size - 1)
    step = band_step[idx]
    return np.where(step > 0.0, step, 0.01)


def _quantize(x: np.ndarray, step: np.ndarray, rounding: str) -> np.ndarray:
    """Quantize values to the nearest step boundary."""
    if rounding == "floor":
        return np.floor(x / step) * step
    return np.round(x / step) * step


# ===============================================================
# Markdown ladder
# ===============================================================
_MD_CFG_VERSION: int = -1
_MD_CFG_CACHE = None


def _to_dict(obj):
    """Convert Pydantic model or dict to plain dict for hashing."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def _md_cfg_hash(models) -> int:
    """Content-based hash of the pricing config subset (handles nested structures)."""
    import json
    raw = _to_dict(models.get("pricing", {}) or {})
    try:
        return hash(json.dumps(raw, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return id(raw)


def _load_markdown_cfg():
    """
    Load markdown ladder config from models.pricing.markdown.

    Parses the full ladder (none/pct/amt kinds with weights) for
    direct discount drawing. This is the single source of truth
    for all markdown/discount logic.

    Returns
    -------
    tuple: (enabled, kind_codes, values, probs, max_pct, min_net, allow_neg)
      kind_codes : int8 array (0=none, 1=pct, 2=amt)
      values     : float64 array (pct in [0,1], amt >= 0)
      probs      : float64 array (normalized weights)
    """
    global _MD_CFG_VERSION, _MD_CFG_CACHE

    models = getattr(State, "models_cfg", None) or {}
    version = _md_cfg_hash(models)
    if version == _MD_CFG_VERSION and _MD_CFG_CACHE is not None:
        return _MD_CFG_CACHE

    p = models.get("pricing", {}) or {}
    md = p.get("markdown", {}) or {}

    enabled = bool(md.get("enabled", False))

    max_pct = float(md.get("max_pct_of_price", 0.50))
    if not np.isfinite(max_pct) or max_pct <= 0.0:
        max_pct = 0.50

    min_net = float(md.get("min_net_price", 0.01))
    if not np.isfinite(min_net) or min_net < 0.0:
        min_net = 0.01

    allow_neg = bool(md.get("allow_negative_margin", False))

    # Parse full ladder: kind/value/weight triples
    ladder = md.get("ladder", []) or []
    kind_codes: list[int] = []
    values: list[float] = []
    weights: list[float] = []

    for item in ladder:
        if not isinstance(item, Mapping):
            continue
        k = str(item.get("kind", "none")).strip().lower()
        w = float(item.get("weight", 0.0) or 0.0)
        if w <= 0:
            continue
        v = float(item.get("value", 0.0) or 0.0)

        if k == "none":
            kind_codes.append(0)
            values.append(0.0)
            weights.append(w)
        elif k == "pct":
            kind_codes.append(1)
            values.append(float(np.clip(v, 0.0, 1.0)))
            weights.append(w)
        elif k == "amt":
            kind_codes.append(2)
            values.append(max(0.0, v))
            weights.append(w)
        else:
            warn(
                f"Unknown markdown ladder kind {k!r} (expected 'none', 'pct', or 'amt'); "
                "entry ignored. Check models.yaml -> models.pricing.markdown.ladder."
            )

    if not kind_codes:
        kind_codes = [0]
        values = [0.0]
        weights = [1.0]

    probs = np.asarray(weights, dtype=np.float64)
    s = float(probs.sum())
    probs = probs / s if s > 0 else np.ones(1, dtype=np.float64)

    result = (
        enabled,
        np.asarray(kind_codes, dtype=np.int8),
        np.asarray(values, dtype=np.float64),
        probs,
        max_pct,
        min_net,
        allow_neg,
    )
    _MD_CFG_VERSION = version
    _MD_CFG_CACHE = result
    return result



# ===============================================================
# Appearance config
# ===============================================================
_APPEAR_CFG_VERSION: int = -1
_APPEAR_CFG_CACHE: dict | None = None


def _load_appearance_cfg() -> dict:
    """Load and cache appearance snapping config."""
    global _APPEAR_CFG_VERSION, _APPEAR_CFG_CACHE

    models = getattr(State, "models_cfg", None) or {}
    version = _md_cfg_hash(models)
    if version == _APPEAR_CFG_VERSION and _APPEAR_CFG_CACHE is not None:
        return _APPEAR_CFG_CACHE

    p = models.get("pricing", {}) or {}
    appearance = p.get("appearance", {}) or {}
    enabled = bool(appearance.get("enabled", False))

    # --- Unit price ---
    up_cfg = appearance.get("unit_price", {}) or {}
    up_round = str(up_cfg.get("rounding", "floor")).strip().lower()
    if up_round not in ("nearest", "floor"):
        up_round = "floor"
    up_max, up_step = _parse_bands(
        up_cfg.get("bands", None),
        default=[(100.0, 50.0), (1e18, 100.0)],
    )
    up_end_vals, up_end_w = _parse_endings(
        up_cfg.get("endings", None), default_if_missing=True)

    # --- Unit cost ---
    uc_cfg = appearance.get("unit_cost", {}) or {}
    uc_round = str(uc_cfg.get("rounding", "nearest")).strip().lower()
    if uc_round not in ("nearest", "floor"):
        uc_round = "nearest"
    uc_max, uc_step = _parse_bands(
        uc_cfg.get("bands", None),
        default=[(100.0, 1.0), (500.0, 2.0), (2000.0, 5.0),
                 (10000.0, 25.0), (1e18, 50.0)],
    )
    uc_end_vals, uc_end_w = _parse_endings(
        uc_cfg.get("endings", None), default_if_missing=False)

    # --- Discount ---
    d_cfg = appearance.get("discount", {}) or {}
    d_round = str(d_cfg.get("rounding", "floor")).strip().lower()
    if d_round not in ("nearest", "floor"):
        d_round = "floor"
    d_max, d_step = _parse_bands(
        d_cfg.get("bands", None),
        default=[(10.0, 0.5), (100.0, 1.0), (1000.0, 5.0),
                 (10000.0, 25.0), (1e18, 50.0)],
    )

    out = {
        "enabled": enabled,
        # unit price
        "up_round": up_round,
        "up_band_max": up_max, "up_band_step": up_step,
        "up_end_vals": up_end_vals, "up_end_w": up_end_w,
        # unit cost
        "uc_round": uc_round,
        "uc_band_max": uc_max, "uc_band_step": uc_step,
        "uc_end_vals": uc_end_vals, "uc_end_w": uc_end_w,
        # discount
        "d_round": d_round,
        "d_band_max": d_max, "d_band_step": d_step,
    }

    _APPEAR_CFG_VERSION = version
    _APPEAR_CFG_CACHE = out
    return out


# ===============================================================
# Snapping functions
# ===============================================================

def _snap_unit_price(rng, up: np.ndarray, acfg: dict) -> np.ndarray:
    """Snap unit prices to banded increments with configurable endings.

    Uses stochastic rounding when mode is 'floor': the probability of
    rounding up equals the fractional position within the step, so prices
    transition gradually as inflation pushes them through a band rather
    than jumping all at once after a multi-year plateau.
    """
    if not acfg.get("enabled", False):
        return up

    up = np.maximum(_as_f64(up), 0.0)
    step = np.maximum(_choose_step(up, acfg["up_band_max"], acfg["up_band_step"]), 1.0)

    if acfg["up_round"] == "floor":
        # Stochastic rounding: round up with probability = fractional part
        ratio = up / step
        floor_val = np.floor(ratio)
        frac = ratio - floor_val
        anchor = np.where(
            rng.random(up.shape[0]) < frac,
            (floor_val + 1) * step,
            floor_val * step,
        )
    else:
        anchor = _quantize(up, step, acfg["up_round"])

    idx = rng.choice(acfg["up_end_vals"].size, size=up.shape[0], p=acfg["up_end_w"])
    ending = acfg["up_end_vals"][idx]

    return np.maximum(anchor + ending, 0.01)


def _snap_cost(rng, uc: np.ndarray, acfg: dict) -> np.ndarray:
    """Snap unit costs to banded increments."""
    if not acfg.get("enabled", False):
        return uc

    uc = np.maximum(_as_f64(uc), 0.0)
    step = _choose_step(uc, acfg["uc_band_max"], acfg["uc_band_step"])
    anchor = _quantize(uc, step, acfg["uc_round"])

    end_vals = acfg.get("uc_end_vals")
    end_w = acfg.get("uc_end_w")

    if end_vals is not None and end_w is not None and end_vals.size > 0:
        idx = rng.choice(end_vals.size, size=uc.shape[0], p=end_w)
        ending = end_vals[idx]
        snapped = anchor.copy()
        mask = step >= 1.0
        if mask.any():
            snapped[mask] = np.floor(anchor[mask]) + ending[mask]
    else:
        snapped = anchor

    # Prevent zero-cost items from snapping: floor at the smallest step
    snapped = np.maximum(snapped, step)
    return snapped


def _snap_discount(disc: np.ndarray, up: np.ndarray, acfg: dict) -> np.ndarray:
    """Snap discount amounts to banded increments."""
    if not acfg.get("enabled", False):
        return disc

    disc = np.maximum(_as_f64(disc), 0.0)
    up = np.maximum(_as_f64(up), 0.0)

    step = np.maximum(_choose_step(up, acfg["d_band_max"], acfg["d_band_step"]), 1.0)
    snapped = _quantize(disc, step, acfg["d_round"])
    return np.clip(snapped, 0.0, up)


# ===============================================================
# Inflation config
# ===============================================================

def _load_inflation_cfg():
    """Load inflation drift parameters."""
    models = getattr(State, "models_cfg", None) or {}
    p = models.get("pricing", {}) or {}
    infl = p.get("inflation", {}) or {}

    annual_rate = float(np.clip(float(infl.get("annual_rate", 0.03)), -0.50, 2.0))
    month_sigma = float(np.clip(float(infl.get("month_volatility_sigma", 0.0)), 0.0, 0.25))

    clip = infl.get("factor_clip", None)
    if not (isinstance(clip, (list, tuple)) and len(clip) == 2):
        # Default range: allow deflation down to 0.50× if annual_rate < 0,
        # otherwise floor at 1.0 (no price decrease).
        if annual_rate < 0:
            clip = [0.50, 1.25]
        else:
            clip = [1.00, 1.25]
    lo, hi = float(clip[0]), float(clip[1])
    if hi < lo:
        lo, hi = hi, lo
    lo = max(lo, 0.0)
    hi = max(hi, lo)

    vol_seed = int(infl.get("volatility_seed", 123))
    apply_with_scd2 = bool(infl.get("apply_with_scd2", True))

    return annual_rate, month_sigma, lo, hi, vol_seed, apply_with_scd2


# ===============================================================
# Month noise (cached, deterministic per month+seed)
# ===============================================================
_MONTH_NOISE_CACHE: dict = {}
_MONTH_NOISE_MAX_SIZE = 500  # prevent unbounded growth across runs


def _month_noise(month_int: int, seed: int, sigma: float) -> float:
    """
    Deterministic per-month multiplicative noise factor (lognormal, mean≈1).
    Cached to ensure consistency across repeated calls within a run.
    """
    if sigma <= 0.0:
        return 1.0

    key = (int(seed), float(sigma), int(month_int))
    cached = _MONTH_NOISE_CACHE.get(key)
    if cached is not None:
        return cached

    # LRU-style eviction: drop oldest half when cache is full.
    # Full clear would discard entries that may be re-requested in
    # the same run, forcing regeneration with the same seed (correct
    # but wasteful).
    if len(_MONTH_NOISE_CACHE) >= _MONTH_NOISE_MAX_SIZE:
        keys = list(_MONTH_NOISE_CACHE.keys())
        for k in keys[: len(keys) // 2]:
            del _MONTH_NOISE_CACHE[k]

    s = (int(seed) ^ (int(month_int) * 1000003)) & 0xFFFFFFFF
    rng = np.random.default_rng(s)
    mu = -0.5 * (sigma ** 2)
    v = float(rng.lognormal(mean=mu, sigma=sigma))

    _MONTH_NOISE_CACHE[key] = v
    return v


def _reset_caches() -> None:
    """Reset all module-level caches.  Call from State.reset() or tests."""
    global _MD_CFG_VERSION, _MD_CFG_CACHE
    global _APPEAR_CFG_VERSION, _APPEAR_CFG_CACHE
    _MD_CFG_VERSION = -1
    _MD_CFG_CACHE = None
    _APPEAR_CFG_VERSION = -1
    _APPEAR_CFG_CACHE = None
    _MONTH_NOISE_CACHE.clear()


# ===============================================================
# Global start month
# ===============================================================

def _global_start_month_int(order_dates: np.ndarray) -> int:
    """
    Determine the first month of the entire dataset for computing inflation offset.

    Uses State.date_pool if available; falls back to min(order_dates).
    """
    dp = getattr(State, "date_pool", None)
    if dp is not None:
        try:
            if len(dp) > 0:
                d0 = np.min(np.asarray(dp).astype("datetime64[D]"))
                return int(d0.astype("datetime64[M]").astype("int64"))
        except (TypeError, ValueError, OverflowError):
            pass

    d0 = np.min(np.asarray(order_dates).astype("datetime64[D]"))
    return int(d0.astype("datetime64[M]").astype("int64"))


# ===============================================================
# Public API
# ===============================================================

def build_prices(rng, order_dates, qty, price):
    """
    Single-pass sales pricing pipeline.

    Takes base product prices and applies:
      1. Inflation drift (compound annual rate)
      2. UnitPrice appearance snapping (retail grid)
      3. UnitCost inflation + snapping
      4. Markdown discount draw from ladder (none/pct/amt)
      5. Discount constraint enforcement
      6. Round to cents

    Parameters
    ----------
    rng : numpy.random.Generator
    order_dates : array-like of datetime64
    qty : array-like of int (reserved for future basket-size pricing)
    price : dict with keys:
        final_unit_price, final_unit_cost, discount_amt, final_net_price

    Returns
    -------
    dict — same structure, values updated.
    """
    _ = qty

    annual_rate, month_sigma, clip_lo, clip_hi, vol_seed, apply_with_scd2 = (
        _load_inflation_cfg())

    order_dates = np.asarray(order_dates)
    n = order_dates.shape[0]
    if n <= 0:
        return price

    _product_scd2_active = bool(getattr(State, "product_scd2_active", False))
    _skip_inflation = _product_scd2_active or (not apply_with_scd2)

    # ---- 2–3. Resolve UnitPrice and UnitCost ----
    base_up = _as_f64(price["final_unit_price"])
    base_uc = _as_f64(price["final_unit_cost"])
    appearance = _load_appearance_cfg()

    if _product_scd2_active:
        # SCD2 active: preserve catalog prices from the product dimension
        # so star-schema joins stay consistent.  Only discounts vary.
        up = np.maximum(base_up, 0.0)
        uc = np.clip(base_uc, 0.0, up)
    else:
        # No SCD2: apply runtime inflation + appearance snapping
        if _skip_inflation:
            factor = np.ones(n, dtype=np.float64)
        else:
            order_month_i = order_dates.astype("datetime64[M]").astype("int64")
            uniq_months, inv = np.unique(order_month_i, return_inverse=True)

            start_m = _global_start_month_int(order_dates)
            months_since = (uniq_months.astype(np.int64) - start_m).astype(np.float64)

            infl = (1.0 + annual_rate) ** (months_since / 12.0)
            infl = np.where(np.isfinite(infl), infl, 1.0)

            if month_sigma > 0.0:
                noises = np.fromiter(
                    (_month_noise(int(m), vol_seed, month_sigma) for m in uniq_months),
                    dtype=np.float64, count=uniq_months.size)
            else:
                noises = np.ones_like(infl)

            factor = np.clip(infl * noises, clip_lo, clip_hi)[inv]

        up = np.maximum(base_up * factor, 0.0)
        up = _snap_unit_price(rng, up, appearance)
        uc = np.minimum(np.maximum(base_uc * factor, 0.0), up)
        uc = _snap_cost(rng, uc, appearance)
        uc = np.minimum(uc, up)

    # ---- 4. Draw markdown discount from ladder ----
    (md_enabled, kind_codes, values, probs,
     max_pct, min_net, allow_neg) = _load_markdown_cfg()

    disc = np.zeros(n, dtype=np.float64)

    if md_enabled and kind_codes.size > 0:
        idx = rng.choice(kind_codes.size, size=n, replace=True, p=probs)
        kc = kind_codes[idx]
        v = values[idx]

        disc = np.where(kc == 1, up * v, disc)   # pct of snapped price
        disc = np.where(kc == 2, v,      disc)   # flat amt

    # ---- 5. Constraint enforcement ----
    disc = np.maximum(disc, 0.0)
    disc = np.minimum(disc, up * max_pct)
    if min_net > 0.0:
        disc = np.minimum(disc, np.maximum(up - min_net, 0.0))
    if not allow_neg:
        disc = np.minimum(disc, np.maximum(up - (uc + 0.01), 0.0))
    disc = np.minimum(disc, up)

    # Snap discount to appearance grid
    disc = _snap_discount(disc, up, appearance)
    disc = np.minimum(disc, up)

    # ---- 6. Round to cents ----
    up = np.round(np.where(np.isfinite(up), up, 0.0), 2)
    uc = np.round(np.where(np.isfinite(uc), uc, 0.0), 2)
    disc = np.round(np.clip(np.where(np.isfinite(disc), disc, 0.0), 0.0, up), 2)
    net = np.round(np.maximum(up - disc, 0.0), 2)

    uc = np.minimum(uc, up)
    if not allow_neg:
        # Protect positive margin by reducing discount (not distorting cost)
        margin_violated = net < uc + 0.01
        if margin_violated.any():
            disc[margin_violated] = np.round(
                np.maximum(up[margin_violated] - uc[margin_violated] - 0.01, 0.0), 2)
            net[margin_violated] = np.round(
                np.maximum(up[margin_violated] - disc[margin_violated], 0.0), 2)
    uc = np.round(uc, 2)

    price["final_unit_price"] = up
    price["final_unit_cost"] = uc
    price["discount_amt"] = disc
    price["final_net_price"] = net
    return price
