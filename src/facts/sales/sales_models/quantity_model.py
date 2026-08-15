"""
Quantity / basket-size model.

Generates per-order item counts using Poisson + monthly seasonality +
multiplicative noise.

Config source: models.yaml -> models.quantity
Runtime state:  State.models_cfg  (ModelsInnerConfig Pydantic model)
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from src.exceptions import SalesError
from src.facts.sales.sales_logic import State
from src.facts.sales.sales_logic.weights import cdf_from_probs, normal_ppf, pick_from_cdf
from src.utils.hashing import hash_u01

# Counter-based quantity salts. Both stochastic draws are hashed on the line
# entity key (OrderNumber, OrderLineNumber) instead of the chunk RNG, so a
# line's quantity is a pure function of its identity — independent of
# chunk_size, worker count, or any other stage's draw count. Distinct salts keep
# the Poisson basket-size draw and the lognormal noise draw independent.
_QTY_POISSON_SALT = np.uint64(0x2B0AC0DE00000001)
_QTY_LOGNORMAL_SALT = np.uint64(0x2B0AC0DE00000002)


# ---------------------------------------------------------------
# Defaults (aligned exactly with models.yaml)
# ---------------------------------------------------------------

_DEFAULTS = {
    "base_poisson_lambda": 1.6,
    "monthly_factors": [0.99, 0.98, 1.00, 1.00, 1.01, 1.02,
                        1.02, 1.01, 1.00, 1.03, 1.06, 1.05],
    "noise_sigma": 0.12,
    "min_qty": 1,
    "max_qty": 8,
    "truncate_at_max": True,
    "max_redraw_attempts": 8,
}


# ---------------------------------------------------------------
# Config loading (process-local cache)
# ---------------------------------------------------------------
_CFG_VERSION: int = -1
_CFG_CACHE: dict | None = None
# Reference (median) list price for the elasticity term, cached per product pool.
_REF_PRICE_CACHE: dict = {}
# Truncated Poisson CDF table per lambda, for the counter-based basket-size draw.
_POISSON_CDF_CACHE: dict = {}


def _poisson_cdf(lam: float) -> np.ndarray:
    """Clamped CDF of ``Poisson(lam)`` over ``k = 0 .. kmax``, cached per lambda.

    ``kmax`` is set well beyond ``lam`` so the truncated tail mass is negligible
    (and any residual is absorbed by renormalization + the ``cdf[-1] = 1.0``
    clamp). ``pick_from_cdf(cdf, u)`` then draws a Poisson variate from a uniform
    ``u`` by inverse-CDF — the same marginal as ``rng.poisson(lam)`` but keyed on
    the line entity, not the RNG stream. The pmf is built in log space so large
    ``k!`` never overflows.
    """
    cached = _POISSON_CDF_CACHE.get(lam)
    if cached is not None:
        return cached
    if lam <= 0.0:
        cdf = np.array([1.0], dtype=np.float64)
        _POISSON_CDF_CACHE[lam] = cdf
        return cdf
    kmax = int(lam + 10.0 * np.sqrt(lam) + 20.0)
    k = np.arange(kmax + 1, dtype=np.float64)
    log_factorial = np.concatenate(
        ([0.0], np.cumsum(np.log(np.arange(1, kmax + 1, dtype=np.float64))))
    )
    log_pmf = k * np.log(lam) - lam - log_factorial
    pmf = np.exp(log_pmf)
    cdf = cdf_from_probs(pmf / pmf.sum())
    _POISSON_CDF_CACHE[lam] = cdf
    return cdf


def _to_dict(obj):
    """Convert a Pydantic model or dict to a plain dict (for hashing)."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def _cfg_hash(models) -> int:
    """Content-based, process-stable hash of the quantity-relevant config subset.

    Uses a JSON dump so nested structures (the ``elasticity`` sub-model) hash
    cleanly — a plain ``hash(tuple(items))`` chokes on nested dicts/models. A
    stable SHA-256 digest (not builtin ``hash()``, which is PYTHONHASHSEED-salted
    per process) keeps the version comparable across processes.
    """
    import json
    import hashlib
    raw = _to_dict(models.get("quantity", {}) or {})
    try:
        blob = json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return id(raw)
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big")


def _load_cfg() -> dict:
    """
    Load, validate, and cache the quantity config.

    Supports both the current simplified keys and legacy nested keys
    for backward compatibility.
    """
    global _CFG_VERSION, _CFG_CACHE

    models = getattr(State, "models_cfg", None) or {}
    version = _cfg_hash(models)
    if version == _CFG_VERSION and _CFG_CACHE is not None:
        return _CFG_CACHE

    raw = models.get("quantity", {}) or {}
    if not isinstance(raw, Mapping):
        raise SalesError("models.quantity must be a mapping")

    # --- scalar parameters ---
    lam = float(raw.get("base_poisson_lambda", _DEFAULTS["base_poisson_lambda"]))
    if lam < 0:
        raise SalesError("models.quantity.base_poisson_lambda must be >= 0")

    # Support both "noise_sigma" (current) and "noise_sd" (legacy)
    noise = max(0.0, float(raw.get("noise_sigma", raw.get("noise_sd", _DEFAULTS["noise_sigma"]))))

    min_qty = max(1, int(raw.get("min_qty", _DEFAULTS["min_qty"])))
    max_qty = int(raw.get("max_qty", _DEFAULTS["max_qty"]))
    if max_qty < min_qty:
        min_qty, max_qty = max_qty, min_qty
    if min_qty < 1:
        min_qty = 1

    # --- monthly factors (must be 12 floats) ---
    factors = raw.get("monthly_factors")
    if factors is None:
        factors = list(_DEFAULTS["monthly_factors"])
    if len(factors) != 12:
        raise SalesError("models.quantity.monthly_factors must be a list of 12 floats")
    factors_arr = np.asarray(factors, dtype=np.float64)
    factors_arr = np.where(np.isfinite(factors_arr), factors_arr, 1.0)
    factors_arr = np.clip(factors_arr, 0.01, None)  # Prevent zero-factor months

    out = {
        "base_poisson_lambda": lam,
        "monthly_factors": factors_arr,
        "noise_sigma": noise,
        "min_qty": min_qty,
        "max_qty": max_qty,
        "truncate_at_max": bool(raw.get(
            "truncate_at_max", _DEFAULTS["truncate_at_max"],
        )),
        "max_redraw_attempts": max(0, min(32, int(raw.get(
            "max_redraw_attempts", _DEFAULTS["max_redraw_attempts"],
        )))),
        "elasticity": _parse_elasticity(raw.get("elasticity", None)),
    }

    _CFG_VERSION = version
    _CFG_CACHE = out
    return out


def _clip_pair(val, default):
    """Coerce a [lo, hi] config pair to a sorted (lo, hi) float tuple."""
    if not (isinstance(val, (list, tuple)) and len(val) == 2):
        val = default
    lo, hi = float(val[0]), float(val[1])
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _parse_elasticity(el) -> dict:
    """Parse the elasticity sub-config into a plain dict.

    Absent (``None``) => disabled, so unit-test configs that only set the
    scalar quantity keys keep the legacy product-agnostic behavior.
    """
    if el is None:
        return {"enabled": False}
    enabled = bool(el.get("enabled", False))
    if not enabled:
        return {"enabled": False}
    price_eps = float(el.get("price_elasticity", 0.5))
    ref_cfg = el.get("reference_price", None)
    ref_cfg = float(ref_cfg) if (ref_cfg is not None and float(ref_cfg) > 0.0) else None
    f_lo, f_hi = _clip_pair(el.get("factor_clip", None), [0.4, 2.5])
    prop_strength = float(el.get("propensity_strength", 0.4))
    p_lo, p_hi = _clip_pair(el.get("propensity_clip", None), [0.6, 1.6])
    return {
        "enabled": True,
        "price_elasticity": price_eps,
        "reference_price": ref_cfg,
        "factor_lo": f_lo,
        "factor_hi": f_hi,
        "propensity_strength": prop_strength,
        "prop_lo": p_lo,
        "prop_hi": p_hi,
    }


def _reference_price(ref_cfg) -> float:
    """Resolve the elasticity reference price.

    Config value wins; otherwise the median catalog ListPrice from the bound
    product pool (``State.product_np`` col 1), computed once per pool. The pool
    is the full, worker-invariant product array, so the reference is identical
    across chunks and worker counts.
    """
    if ref_cfg is not None:
        return float(ref_cfg)
    pnp = getattr(State, "product_np", None)
    if pnp is None or len(pnp) == 0:
        return 0.0
    key = id(pnp)
    cached = _REF_PRICE_CACHE.get(key)
    if cached is not None:
        return cached
    prices = np.asarray(pnp[:, 1], dtype=np.float64)
    prices = prices[np.isfinite(prices) & (prices > 0.0)]
    ref = float(np.median(prices)) if prices.size else 0.0
    _REF_PRICE_CACHE[key] = ref
    return ref


def _propensity_factor(product_row_idx, el: dict) -> np.ndarray | float:
    """Per-line unit-propensity multiplier from PopularityScore.

    Popular products (staples) sell in larger baskets. Returns 1.0 when no
    popularity signal / product index is available (e.g. product_profile
    missing), so the term is a no-op rather than an error.
    """
    strength = el.get("propensity_strength", 0.0)
    if product_row_idx is None or strength == 0.0:
        return 1.0
    pop = getattr(State, "product_popularity", None)
    if pop is None:
        return 1.0
    pop = np.asarray(pop, dtype=np.float64)
    line_pop = pop[np.asarray(product_row_idx, dtype=np.int64)]
    line_pop = np.where(np.isfinite(line_pop) & (line_pop > 0.0), line_pop, 50.0)
    fac = np.power(line_pop / 50.0, strength)
    return np.clip(fac, el.get("prop_lo", 0.6), el.get("prop_hi", 1.6))


def _reset_cache() -> None:
    """Reset module cache.  Called from init_sales_worker() and tests."""
    global _CFG_VERSION, _CFG_CACHE
    _CFG_VERSION = -1
    _CFG_CACHE = None
    _REF_PRICE_CACHE.clear()
    _POISSON_CDF_CACHE.clear()


# ---------------------------------------------------------------
# Public API
# ---------------------------------------------------------------

def build_quantity(rng, order_dates, *, product_row_idx=None, unit_price=None,
                   order_numbers=None, line_numbers=None,
                   segment_multiplier=None):
    """
    Generate per-order item quantities.

    Pipeline:
      1. Draw from Poisson(λ) + 1  (minimum 1 item)
      2. Multiply by monthly seasonal factors
      3. Add multiplicative noise
      3b. Elasticity: scale by (price/ref)^(-ε) and a per-product
          unit propensity — pure arithmetic on the float quantity, no RNG, so
          determinism is preserved. Only active when ``unit_price`` is supplied
          (the pipeline path) *and* ``models.quantity.elasticity.enabled``.
      4. Round and clamp to [min_qty, max_qty]

    Both stochastic draws are counter-based: the Poisson basket size is an
    inverse-CDF lookup and the lognormal noise is ``exp(σ·Φ⁻¹(u))``, each keyed on
    the line entity key ``(order_numbers, line_numbers)`` via ``hash_u01`` — so a
    line's quantity is a pure function of its identity, independent of chunk_size
    or worker count. Under ``skip_order_cols`` there is no per-line key and the
    draws fall back to the chunk RNG (``rng.poisson`` / ``rng.lognormal``).

    Parameters
    ----------
    rng : numpy.random.Generator
    order_dates : array-like of datetime64
    product_row_idx : array-like of int or None
        Per-line row index into the product pool (``State.product_np`` /
        ``State.product_popularity``); used for the propensity term.
    unit_price : array-like of float or None
        Per-line base list price; used for the price-elasticity term.
    order_numbers, line_numbers : array-like of int or None
        Per-line entity key for the counter-based draws; ``None`` under
        skip_order_cols, where the chunk RNG is used instead.
    segment_multiplier : array-like of float or None
        Optional deterministic per-line customer-segment multiplier. It consumes
        no random draws and is applied to every truncated-redraw candidate.

    Returns
    -------
    np.ndarray[int32] of shape (n,)
    """
    cfg = _load_cfg()
    n = int(len(order_dates))
    if n <= 0:
        return np.zeros(0, dtype=np.int32)

    _keyed = order_numbers is not None and line_numbers is not None
    lam = cfg["base_poisson_lambda"]

    # Fixed per-row scale: month seasonality + price elasticity + product
    # propensity. Stochastic Poisson/noise candidates are multiplied by this
    # same scale on every truncated redraw.
    order_months = np.asarray(order_dates).astype("datetime64[M]", copy=False)
    unique_months, inv = np.unique(order_months, return_inverse=True)
    month_of_year = (unique_months.astype("int64") % 12).astype(np.int64)
    fixed_scale = cfg["monthly_factors"][month_of_year][inv].astype(
        np.float64, copy=True,
    )
    if segment_multiplier is not None:
        _segment = np.asarray(segment_multiplier, dtype=np.float64)
        if _segment.shape != (n,):
            raise SalesError(
                f"segment_multiplier must have shape ({n},), got {_segment.shape}"
            )
        _segment = np.where(np.isfinite(_segment), _segment, 1.0)
        fixed_scale *= np.clip(_segment, 0.1, 4.0)

    # 3b. Elasticity + propensity — deterministic, no RNG draws.
    el = cfg["elasticity"]
    if el.get("enabled") and unit_price is not None:
        eps = el.get("price_elasticity", 0.0)
        if eps != 0.0:
            ref = _reference_price(el.get("reference_price"))
            if ref > 0.0:
                up = np.asarray(unit_price, dtype=np.float64)
                price_factor = np.power(np.maximum(up, 0.01) / ref, -eps)
                price_factor = np.where(np.isfinite(price_factor), price_factor, 1.0)
                fixed_scale *= np.clip(
                    price_factor, el.get("factor_lo", 0.4), el.get("factor_hi", 2.5),
                )
        fixed_scale *= _propensity_factor(product_row_idx, el)

    sigma = cfg["noise_sigma"]
    _order_numbers = np.asarray(order_numbers) if _keyed else None
    _line_numbers = np.asarray(line_numbers) if _keyed else None

    def _draw_candidate(indices, attempt):
        """Draw a deterministic candidate for the selected row positions."""
        _size = len(indices)
        if _keyed:
            _salt_step = 0x00000100000001B3
            _pois_salt = np.uint64(
                (int(_QTY_POISSON_SALT) + attempt * _salt_step) & 0xFFFFFFFFFFFFFFFF
            )
            _noise_salt = np.uint64(
                (int(_QTY_LOGNORMAL_SALT) + attempt * _salt_step) & 0xFFFFFFFFFFFFFFFF
            )
            _u_pois = hash_u01(
                _pois_salt, _order_numbers[indices], _line_numbers[indices],
            )
            _candidate = (
                pick_from_cdf(_poisson_cdf(lam), _u_pois).astype(np.float64) + 1.0
            )
            if sigma > 0.0:
                _u_ln = hash_u01(
                    _noise_salt, _order_numbers[indices], _line_numbers[indices],
                )
                _candidate *= np.exp(sigma * normal_ppf(_u_ln))
        else:
            _candidate = rng.poisson(lam, _size).astype(np.float64) + 1.0
            if sigma > 0.0:
                _candidate *= rng.lognormal(mean=0.0, sigma=sigma, size=_size)
        return _candidate * fixed_scale[indices]

    all_idx = np.arange(n, dtype=np.int64)
    qty = _draw_candidate(all_idx, 0)
    rounded = np.rint(qty).astype(np.int32)

    # A hard np.clip censors every overflow at max_qty and creates a false modal
    # bucket. Rejection sampling conditions the same model on Quantity<=max_qty;
    # attempt-specific hash salts keep the keyed path byte-identical across
    # chunk sizes and worker counts. The final clip is only a defensive fallback
    # after the bounded number of redraws.
    if cfg["truncate_at_max"] and cfg["max_redraw_attempts"] > 0:
        overflow = rounded > cfg["max_qty"]
        for attempt in range(1, cfg["max_redraw_attempts"] + 1):
            if not overflow.any():
                break
            idx = np.flatnonzero(overflow)
            qty[idx] = _draw_candidate(idx, attempt)
            rounded[idx] = np.rint(qty[idx]).astype(np.int32)
            overflow[idx] = rounded[idx] > cfg["max_qty"]

    return np.clip(rounded, cfg["min_qty"], cfg["max_qty"])
