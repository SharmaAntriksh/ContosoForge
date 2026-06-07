"""Month-level allocation: macro demand weights and rows-per-month distribution."""

from __future__ import annotations

import hashlib
import warnings
from collections.abc import Mapping

import numpy as np

from src.exceptions import SalesError


def _stable_seed(*parts: object) -> int:
    """Deterministic 31-bit seed from arbitrary parts, suitable for `default_rng`."""
    return int(
        hashlib.md5("_".join(str(p) for p in parts).encode()).hexdigest(),
        16,
    ) & 0x7FFFFFFF


# ----------------------------------------------------------------
# Schedule validation
# ----------------------------------------------------------------

def _sched_mode_and_values(node: dict, name: str) -> tuple[str, list[float]]:
    """
    Validate schedule node:
      { mode: "repeat"|"once", values: [..numbers..] }
    """
    if not isinstance(node, Mapping):
        raise SalesError(f"{name} must be a mapping with keys: mode, values")

    mode = str(node.get("mode", "repeat")).strip().lower()
    if mode not in ("repeat", "once"):
        raise SalesError(f"{name}.mode must be 'repeat' or 'once'")

    # Pydantic renames "values" → "factor_values"; accept both keys
    values = node.get("factor_values") or node.get("values")
    if not isinstance(values, list) or len(values) == 0:
        raise SalesError(f"{name}.values must be a non-empty list")

    out: list[float] = []
    for v in values:
        try:
            out.append(float(v))
        except (ValueError, TypeError) as e:
            raise SalesError(f"{name}.values must contain only numbers: {e}") from e

    return mode, out


# ----------------------------------------------------------------
# Probability helpers
# ----------------------------------------------------------------

def _safe_prob(w: np.ndarray) -> np.ndarray:
    """
    Convert an array into a valid probability vector.
    """
    w = np.asarray(w, dtype="float64")
    w = np.where(np.isfinite(w), w, 0.0)
    w = np.clip(w, 0.0, None)
    s = w.sum()
    if s <= 0.0:
        # fallback uniform
        return np.full(w.shape[0], 1.0 / max(1, w.shape[0]), dtype="float64")
    return w / s


def _distribute_remainder_multinomial(
    rng: np.random.Generator,
    base_rows: np.ndarray,
    remainder: int,
    probs: np.ndarray,
) -> np.ndarray:
    """
    Add `remainder` rows across months according to `probs`.
    """
    if remainder <= 0:
        return base_rows
    add = rng.multinomial(int(remainder), _safe_prob(probs))
    return base_rows + add.astype("int64", copy=False)


def _remove_rows_stochastic(
    rng: np.random.Generator,
    rows: np.ndarray,
    need: int,
    candidates: np.ndarray,
    weights: np.ndarray,
) -> None:
    """
    Remove *exactly* `need` rows from `rows` at `candidates` indices,
    weighted by `weights`, looping until the full amount is removed or no
    candidate has rows left.

    Loops to completion rather than capping at a fixed number of passes: the
    caller's candidates hold enough rows, so a fixed cap could silently leave
    `rows.sum()` above target (CORE-3). Each pass removes >= 1 row, so it
    terminates. Mutates `rows` in-place.
    """
    # Pre-allocate the lookup table once at the max possible size
    idx_map_size = int(candidates.max()) + 1 if candidates.size > 0 else 0
    idx_map = np.empty(idx_map_size, dtype="int64") if idx_map_size > 0 else None

    while need > 0:
        # refresh: only candidates that still have rows
        active_mask = rows[candidates] > 0
        active = candidates[active_mask]
        if active.size == 0:
            break

        probs = _safe_prob(weights[active])
        pick = rng.choice(active, size=need, replace=True, p=probs)

        # Reuse pre-allocated lookup table for position mapping
        idx_map[active] = np.arange(active.size, dtype="int64")

        inv = idx_map[pick]
        dec = np.bincount(inv, minlength=active.size).astype("int64", copy=False)
        dec = np.minimum(dec, rows[active])
        rows[active] -= dec
        need -= int(dec.sum())


# ----------------------------------------------------------------
# Macro demand weights
# ----------------------------------------------------------------

_GROWTH_RATIO_WARN_THRESHOLD = 1e6


def macro_month_weights(rng: np.random.Generator, T: int, cfg: dict) -> np.ndarray:
    """
    Create base demand weights per month, independent of eligible customer count.
    Produces a smooth trend + seasonality + optional shocks + noise.

    cfg example (models.yaml -> models.macro_demand):
      base_level: 1.0
      yearly_growth: 0.03               # 3% per year (smooth drift)
      seasonality_amplitude: 0.12       # +/-12%
      seasonality_phase: 0.0            # radians
      noise_std: 0.05                   # month-to-month multiplier noise

      # Optional: pin exact per-year levels (multipliers) OR compound YoY schedule.
      year_level_factors:
        mode: "repeat"
        values: [1.0, 1.02, 0.97, 0.94]
      yoy_growth_schedule:
        mode: "repeat"
        values: [0.06, 0.06, -0.03, -0.05]

      shock_probability: 0.06           # per month
      shock_impact: [-0.35, -0.10]      # multiplicative range (low, high)
    """
    if T <= 0:
        return np.zeros(0, dtype="float64")

    base_level = float(cfg.get("base_level", 1.0))
    yearly_growth = float(cfg.get("yearly_growth", 0.0))
    amp = float(cfg.get("seasonality_amplitude", 0.0))
    phase = float(cfg.get("seasonality_phase", 0.0))
    noise_std = float(cfg.get("noise_std", 0.0))

    # Row share of growth: what fraction of the stated YoY growth should
    # come from more rows vs. higher prices/quantities.  Default 1.0
    # preserves legacy behaviour (all growth = more rows).  Values < 1.0
    # dampen the row-level growth so Year 1 isn't starved in a fixed-pie.
    row_share = float(np.clip(cfg.get("row_share_of_growth", 1.0), 0.0, 1.0))

    shock_p = float(cfg.get("shock_probability", 0.0))
    shock_lo, shock_hi = cfg.get("shock_impact", [-0.25, -0.08])
    shock_lo = float(shock_lo)
    shock_hi = float(shock_hi)

    if shock_p > 0.0 and shock_lo > shock_hi:
        raise SalesError("shock_impact must be [low, high] with low <= high")

    m = np.arange(T, dtype="float64")

    # ---- yearly drift (baseline) + optional year-pattern schedule ----
    yoy_node = cfg.get("yoy_growth_schedule")
    lvl_node = cfg.get("year_level_factors")
    if yoy_node and lvl_node:
        raise SalesError("Use only one of: yoy_growth_schedule OR year_level_factors")

    year_idx = (m // 12).astype("int64")  # year index per month, relative to dataset start

    # baseline smooth drift: per-month multiplier derived from yearly_growth
    # Dampened by row_share so only that fraction drives row allocation.
    effective_yearly_growth = yearly_growth * row_share
    if effective_yearly_growth != 0.0:
        g = (1.0 + effective_yearly_growth) ** (m / 12.0)
    else:
        g = 1.0

    # year-level factors — dampened by row_share so only that fraction of
    # the stated trajectory drives row allocation (remainder via prices).
    if lvl_node:
        mode, vals = _sched_mode_and_values(lvl_node, "year_level_factors")
        if any(v <= 0.0 for v in vals):
            raise SalesError("year_level_factors.values must be > 0")

        levels = np.asarray(vals, dtype="float64")
        if mode == "repeat":
            yfac = levels[year_idx % len(levels)]
        else:  # once
            yfac = levels[np.minimum(year_idx, len(levels) - 1)]

        # Interpolate toward 1.0 (flat) based on row_share:
        # row_share=1.0 → full factor, row_share=0.0 → completely flat
        if row_share < 1.0:
            yfac = 1.0 + (yfac - 1.0) * row_share

        g = g * yfac

    # yoy growth schedule (compounding, dampened by row_share)
    elif yoy_node:
        mode, vals = _sched_mode_and_values(yoy_node, "yoy_growth_schedule")
        if any(v <= -0.99 for v in vals):
            raise SalesError("yoy_growth_schedule.values must be > -0.99")

        yoy = np.asarray(vals, dtype="float64") * row_share
        n_years = int((T + 11) // 12)

        year_factor = np.ones(n_years, dtype="float64")
        for y in range(1, n_years):
            step = y - 1  # transition into year y
            if mode == "repeat":
                r = yoy[step % len(yoy)]
            else:  # once
                r = yoy[step] if step < len(yoy) else 0.0
            year_factor[y] = year_factor[y - 1] * (1.0 + r)

        g = g * year_factor[np.minimum(year_idx, n_years - 1)]

    # Warn if growth compounding creates extreme weight skew
    if isinstance(g, np.ndarray):
        g = np.where(g > 1e-9, g, 1e-9)  # Ensure positive growth factors
        g_finite = g[np.isfinite(g)]
        if g_finite.size > 0:
            ratio = g_finite.max() / max(g_finite.min(), 1e-30)
            if ratio > _GROWTH_RATIO_WARN_THRESHOLD:
                warnings.warn(
                    f"Extreme growth ratio detected ({ratio:.1e}). "
                    f"yearly_growth={yearly_growth} over {T} months causes "
                    f"heavy skew — most rows will land in a few months.",
                    stacklevel=2,
                )

    # seasonality: per-month multipliers (from trend preset) or sin wave fallback
    monthly_season = cfg.get("monthly_seasonality", None)
    if monthly_season is not None and len(monthly_season) == 12:
        season_arr = np.asarray(monthly_season, dtype="float64")
        month_of_year = (m % 12).astype("int64")
        s = season_arr[month_of_year]
    elif amp != 0.0:
        s = 1.0 + amp * np.sin((2.0 * np.pi * m / 12.0) + phase)
    else:
        s = 1.0

    # Noise and shocks use seeds derived from their own params (not the
    # chunk RNG) so every chunk draws the same vector — otherwise per-chunk
    # randomness averages out and the configured volatility disappears.
    if noise_std > 0.0:
        noise_rng = np.random.default_rng(_stable_seed("macro_noise", noise_std, T))
        n = np.clip(noise_rng.normal(loc=1.0, scale=noise_std, size=T), 0.5, 1.5)
    else:
        n = 1.0

    if shock_p > 0.0:
        shock_rng = np.random.default_rng(_stable_seed("macro_shock", shock_p, shock_lo, shock_hi, T))
        shock = np.ones(T, dtype="float64")
        hit = shock_rng.random(T) < shock_p
        if hit.any():
            shock[hit] = 1.0 + shock_rng.uniform(shock_lo, shock_hi, size=int(hit.sum()))
            shock = np.clip(shock, 0.1, max(1.0, 1.0 + shock_hi))
    else:
        shock = 1.0

    w = base_level * g * s * n * shock
    w = np.where(np.isfinite(w), w, 0.0)
    w = np.clip(w, 1e-9, None)
    return w / w.sum()


# ----------------------------------------------------------------
# Main allocation entry point
# ----------------------------------------------------------------

def build_rows_per_month(
    *,
    rng: np.random.Generator,
    total_rows: int,
    eligible_counts: np.ndarray,
    macro_cfg: dict | None,
) -> np.ndarray:
    """
    Decide how many rows to generate in each month.

    - If macro_cfg is truthy: uses macro demand weights + optional early_month_cap.
    - Else: falls back to legacy eligible-count proportional allocation.

    Returns: int64 array of length T (months), sum == total_rows (unless total_rows<=0).
    """
    eligible_counts = np.asarray(eligible_counts, dtype="int64")
    T = int(eligible_counts.shape[0])

    if T <= 0:
        return np.zeros(0, dtype="int64")

    total_rows = int(total_rows)
    if total_rows <= 0:
        return np.zeros(T, dtype="int64")

    elig_sum = int(eligible_counts.sum())
    if elig_sum <= 0:
        return np.zeros(T, dtype="int64")

    eligible_nonzero = eligible_counts > 0

    use_macro = macro_cfg is not None and len(macro_cfg) > 0
    if not use_macro:
        macro_cfg = {}

    # ------------------------------------------------------------------
    # Macro demand allocation
    # ------------------------------------------------------------------
    if use_macro:
        macro_w = macro_month_weights(rng, T, macro_cfg)

        # months with no eligible customers cannot receive demand
        macro_w = macro_w * eligible_nonzero.astype("float64")
        if macro_w.sum() <= 0.0:
            return np.zeros(T, dtype="int64")
        macro_w = macro_w / macro_w.sum()

        # Optional: blend macro weights with eligibility weights (0.0 = macro-only)
        # Skip blend when early_month_cap is enabled — the cap already
        # prevents overloading months with few customers, so blending
        # eligible counts into the weights double-penalises early months.
        cap_cfg_peek = macro_cfg.get("early_month_cap", None)
        cap_active = (
            isinstance(cap_cfg_peek, Mapping)
            and cap_cfg_peek
            and bool(cap_cfg_peek.get("enabled", True))
        )

        blend = float(macro_cfg.get("eligible_blend", 0.0))
        if blend > 0.0 and not cap_active:
            blend = float(np.clip(blend, 0.0, 1.0))
            elig_w = eligible_counts.astype("float64")
            elig_s = elig_w.sum()
            if elig_s > 0.0:
                elig_w = elig_w / elig_s
                macro_w = (1.0 - blend) * macro_w + blend * elig_w
                macro_w = _safe_prob(macro_w)

        # Initial floor allocation
        rows_per_month = np.floor(macro_w * total_rows).astype("int64")

        # Distribute remainder stochastically (seed-deterministic)
        remainder = int(total_rows - rows_per_month.sum())
        rows_per_month = _distribute_remainder_multinomial(rng, rows_per_month, remainder, macro_w)

        # --------------------------------------------------------------
        # Optional: cap by eligible base to avoid absurd early-month density
        # IMPORTANT semantic fix: cap is only applied if block exists.
        # --------------------------------------------------------------
        cap_cfg = macro_cfg.get("early_month_cap", None)
        if isinstance(cap_cfg, Mapping) and cap_cfg:
            cap_enabled = bool(cap_cfg.get("enabled", True))
            raw_cap = cap_cfg.get("max_rows_per_customer", 12)
            per_customer_cap = int(round(float(raw_cap)))
            redistribute = bool(cap_cfg.get("redistribute_excess", True))

            if cap_enabled and per_customer_cap > 0:
                max_rows = eligible_counts * per_customer_cap
                # months with no eligible customers stay at 0 cap
                max_rows = np.where(eligible_nonzero, max_rows, 0).astype("int64", copy=False)

                capped = np.minimum(rows_per_month, max_rows)
                excess = int(rows_per_month.sum() - capped.sum())

                rows_per_month = capped

                if redistribute and excess > 0:
                    capacity = (max_rows - rows_per_month).astype("int64", copy=False)
                    capacity = np.maximum(capacity, 0)

                    # Single-pass proportional fill + one multinomial for leftovers
                    cap_f = capacity.astype("float64")
                    cap_total = cap_f.sum()
                    if cap_total > 0.0:
                        share = np.floor(cap_f / cap_total * excess).astype("int64")
                        share = np.minimum(share, capacity)
                        rows_per_month += share
                        capacity -= share
                        excess -= int(share.sum())

                    # Stochastic cleanup for remaining units (typically 0–T)
                    for _ in range(8):
                        if excess <= 0:
                            break
                        cap_months = np.flatnonzero(capacity > 0)
                        if cap_months.size == 0:
                            break

                        probs = _safe_prob(macro_w[cap_months])
                        add = rng.multinomial(excess, probs).astype("int64", copy=False)

                        add = np.minimum(add, capacity[cap_months])
                        rows_per_month[cap_months] += add
                        capacity[cap_months] -= add
                        excess -= int(add.sum())

                    # Last resort: preserve total_rows even if cap is too tight.
                    if excess > 0:
                        elig_months = np.flatnonzero(eligible_nonzero)
                        if elig_months.size > 0:
                            probs = _safe_prob(macro_w[elig_months])
                            add = rng.multinomial(excess, probs).astype("int64", copy=False)
                            rows_per_month[elig_months] += add

        # Final guard: exact total rows with robust negative-diff handling
        diff = int(total_rows - rows_per_month.sum())
        if diff != 0:
            eligible_months = np.flatnonzero(eligible_nonzero)
            if eligible_months.size == 0:
                return rows_per_month

            if diff > 0:
                probs = _safe_prob(macro_w[eligible_months])
                add = rng.multinomial(diff, probs).astype("int64", copy=False)
                rows_per_month[eligible_months] += add
            else:
                _remove_rows_stochastic(
                    rng, rows_per_month, -diff, eligible_months, macro_w,
                )

        return rows_per_month

    # ------------------------------------------------------------------
    # Legacy allocation (eligible-count proportional) + stochastic remainder
    # ------------------------------------------------------------------
    month_weights = eligible_counts.astype("float64") / elig_sum
    month_weights = _safe_prob(month_weights)

    rows = np.floor(month_weights * total_rows).astype("int64")
    remainder = int(total_rows - rows.sum())
    rows = _distribute_remainder_multinomial(rng, rows, remainder, month_weights)

    # exact guard (should already match)
    diff = int(total_rows - rows.sum())
    if diff != 0:
        eligible_months = np.flatnonzero(eligible_nonzero)
        if eligible_months.size > 0:
            if diff > 0:
                probs = _safe_prob(month_weights[eligible_months])
                add = rng.multinomial(diff, probs).astype("int64", copy=False)
                rows[eligible_months] += add
            else:
                _remove_rows_stochastic(
                    rng, rows, -diff, eligible_months, month_weights,
                )

    return rows
