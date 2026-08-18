"""Deterministic, trend-derived temporal mix factors.

The coordinator uses this pure engine to build compact customer and product
factor plans before workers start. Sampling code consumes only the resulting
read-only mappings and matrices.

The engine operates on relative month offsets and counter-based draws.  A factor
for a group and month is therefore independent of worker scheduling, chunk size,
the requested end month, and the input ordering of groups.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Sequence

import numpy as np

from src.engine.config.config_schema import MacroDemandConfig
from src.exceptions import SalesError
from src.utils.hashing import hash_u01


_DEFAULT_FACTOR_FLOOR = 0.55
_DEFAULT_FACTOR_CEILING = 1.80
_MIN_U01 = np.float64(2.0 ** -53)
_TWO_PI = np.float64(2.0 * math.pi)


@dataclass(frozen=True)
class TemporalMixProfile:
    """Resolved internal coefficients derived from the existing trend.

    These values are intentionally code-owned calibration constants, not user
    configuration.  ``intensity_scale`` lets a future hierarchical builder
    allocate variance between factor layers without exposing new YAML knobs.
    """

    persistence: float
    innovation_scale: float
    curve_capture_scale: float
    shock_probability: float
    shock_low: float
    shock_high: float
    factor_floor: float
    factor_ceiling: float
    curve_mode: str
    curve_kind: str
    curve_values: tuple[float, ...]
    fallback_yearly_growth: float


def _stable_u64(*parts: object) -> int:
    """Return a specified, process-independent 64-bit hash for text keys."""
    digest = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "little", signed=False))
        digest.update(encoded)
    return int.from_bytes(digest.digest()[:8], "little", signed=False)


def _stable_i63(*parts: object) -> int:
    return _stable_u64(*parts) & 0x7FFF_FFFF_FFFF_FFFF


def _normal_draw(salt_a: int, salt_b: int, keys: np.ndarray, month: int) -> np.ndarray:
    """Counter-based standard-normal draws via a Box-Muller transform."""
    u1 = np.maximum(hash_u01(salt_a, keys, month), _MIN_U01)
    u2 = hash_u01(salt_b, keys, month)
    return np.sqrt(-2.0 * np.log(u1)) * np.cos(_TWO_PI * u2)


def _curve_definition(
    resolved_trend: MacroDemandConfig,
) -> tuple[str, str, tuple[float, ...], float]:
    level_cfg = resolved_trend.year_level_factors
    if level_cfg is not None and level_cfg.factor_values:
        values = tuple(float(v) for v in level_cfg.factor_values)
        if not np.all(np.isfinite(values)) or any(v <= 0.0 for v in values):
            raise SalesError("year_level_factors must contain finite positive values")
        mode = str(level_cfg.mode).lower()
        if mode not in {"once", "repeat"}:
            raise SalesError("year_level_factors mode must be 'once' or 'repeat'")
        return mode, "level", values, 0.0

    growth_cfg = resolved_trend.yoy_growth_schedule
    if growth_cfg is not None and growth_cfg.factor_values:
        values = tuple(float(v) for v in growth_cfg.factor_values)
        if not np.all(np.isfinite(values)) or any(v <= -1.0 for v in values):
            raise SalesError("yoy_growth_schedule must contain finite values greater than -1")
        mode = str(growth_cfg.mode).lower()
        if mode not in {"once", "repeat"}:
            raise SalesError("yoy_growth_schedule mode must be 'once' or 'repeat'")
        return mode, "growth", values, 0.0

    yearly_growth = float(resolved_trend.yearly_growth)
    if not math.isfinite(yearly_growth) or yearly_growth <= -1.0:
        raise SalesError("yearly_growth must be finite and greater than -1")
    return "repeat", "growth", (), yearly_growth


def _annual_curve_changes(
    curve_mode: str,
    curve_kind: str,
    curve_values: tuple[float, ...],
    fallback_yearly_growth: float,
) -> np.ndarray:
    if not curve_values:
        return np.asarray([math.log1p(fallback_yearly_growth)], dtype=np.float64)
    values = np.asarray(curve_values, dtype=np.float64)
    if curve_kind == "growth":
        return np.log1p(values)
    if values.size == 1:
        return np.zeros(1, dtype=np.float64)
    changes = np.diff(np.log(values))
    if curve_mode == "repeat":
        changes = np.concatenate((changes, [math.log(values[0] / values[-1])]))
    return changes


def derive_temporal_mix_profile(
    resolved_trend: MacroDemandConfig,
    *,
    intensity_scale: float = 1.0,
) -> TemporalMixProfile:
    """Derive internal mix behavior from an already resolved macro trend."""
    if not isinstance(resolved_trend, MacroDemandConfig):
        raise SalesError("resolved_trend must be a MacroDemandConfig")
    scale = float(intensity_scale)
    if not math.isfinite(scale) or scale <= 0.0:
        raise SalesError("intensity_scale must be finite and greater than zero")

    noise_std = float(resolved_trend.noise_std)
    shock_probability = float(resolved_trend.shock_probability)
    if not math.isfinite(noise_std) or noise_std < 0.0:
        raise SalesError("noise_std must be finite and non-negative")
    if not math.isfinite(shock_probability) or not 0.0 <= shock_probability <= 1.0:
        raise SalesError("shock_probability must be finite and between zero and one")

    impacts = tuple(float(v) for v in resolved_trend.shock_impact)
    if len(impacts) != 2 or not np.all(np.isfinite(impacts)):
        raise SalesError("shock_impact must contain exactly two finite values")
    impact_low, impact_high = sorted(impacts)

    curve_mode, curve_kind, curve_values, yearly_growth = _curve_definition(resolved_trend)
    annual_changes = _annual_curve_changes(
        curve_mode,
        curve_kind,
        curve_values,
        yearly_growth,
    )
    curve_rms = float(np.sqrt(np.mean(np.square(annual_changes))))
    curve_variation = float(np.std(annual_changes))

    # The global trend controls the movement budget.  Constants cap the mix layer
    # well below macro demand so it reshuffles shares instead of replacing it.
    innovation = scale * (0.006 + 0.26 * noise_std + 0.06 * curve_variation)
    innovation = float(np.clip(innovation, 0.004, 0.12))
    curve_capture = scale * (0.22 + 0.35 * min(curve_rms, 0.50))
    curve_capture = float(np.clip(curve_capture, 0.10, 0.55))
    persistence = float(np.clip(0.978 - 0.18 * noise_std - 0.04 * curve_variation, 0.91, 0.978))

    event_probability = float(np.clip(0.40 * shock_probability * scale, 0.0, 0.20))
    shock_scale = 0.35 * scale
    scaled_low = float(np.clip(impact_low * shock_scale, -0.80, 1.50))
    scaled_high = float(np.clip(impact_high * shock_scale, -0.80, 1.50))
    shock_low = math.log1p(scaled_low)
    shock_high = math.log1p(scaled_high)

    return TemporalMixProfile(
        persistence=persistence,
        innovation_scale=innovation,
        curve_capture_scale=curve_capture,
        shock_probability=event_probability,
        shock_low=shock_low,
        shock_high=shock_high,
        factor_floor=_DEFAULT_FACTOR_FLOOR,
        factor_ceiling=_DEFAULT_FACTOR_CEILING,
        curve_mode=curve_mode,
        curve_kind=curve_kind,
        curve_values=curve_values,
        fallback_yearly_growth=yearly_growth,
    )


def _yearly_value(values: tuple[float, ...], mode: str, year_index: int) -> float:
    if mode == "repeat":
        return values[year_index % len(values)]
    return values[min(year_index, len(values) - 1)]


def _monthly_curve_change(profile: TemporalMixProfile, month: int) -> float:
    values = profile.curve_values
    if not values:
        return math.log1p(profile.fallback_yearly_growth) / 12.0
    year_index = month // 12
    if profile.curve_kind == "growth":
        growth = _yearly_value(values, profile.curve_mode, year_index)
        return math.log1p(growth) / 12.0
    current = _yearly_value(values, profile.curve_mode, year_index)
    following = _yearly_value(values, profile.curve_mode, year_index + 1)
    return math.log(following / current) / 12.0


def _bounded_weighted_normalize(
    values: np.ndarray,
    mass: np.ndarray,
    lower: float,
    upper: float,
) -> np.ndarray:
    """Scale, clip, and preserve a weighted arithmetic mean of exactly one."""
    total_mass = float(mass.sum())
    ordinary_means = (values @ mass) / total_mass
    normalized = values / ordinary_means[:, None]
    needs_bounds = np.any((normalized < lower) | (normalized > upper), axis=1)
    if not np.any(needs_bounds):
        return normalized

    active_values = values[needs_bounds]
    lo_scale = np.zeros(active_values.shape[0], dtype=np.float64)
    hi_scale = np.full(
        active_values.shape[0],
        upper / float(np.min(active_values)),
        dtype=np.float64,
    )
    for _ in range(56):
        mid = (lo_scale + hi_scale) * 0.5
        candidate = np.clip(active_values * mid[:, None], lower, upper)
        means = (candidate @ mass) / total_mass
        too_low = means < 1.0
        lo_scale = np.where(too_low, mid, lo_scale)
        hi_scale = np.where(too_low, hi_scale, mid)
    normalized[needs_bounds] = np.clip(
        active_values * ((lo_scale + hi_scale) * 0.5)[:, None],
        lower,
        upper,
    )
    return normalized


def _validate_inputs(
    group_names: Sequence[object],
    baseline_mass: Sequence[float],
    month_count: int,
    parent_group: Sequence[object] | None,
) -> tuple[list[str], np.ndarray, list[str] | None]:
    names = [str(name) for name in group_names]
    if not names:
        raise SalesError("group_names must contain at least one group")
    if len(set(names)) != len(names):
        raise SalesError("group_names must be unique after string normalization")
    mass = np.asarray(baseline_mass, dtype=np.float64)
    if mass.ndim != 1 or mass.size != len(names):
        raise SalesError("baseline_mass must be one-dimensional and align with group_names")
    if not np.all(np.isfinite(mass)) or np.any(mass <= 0.0):
        raise SalesError("baseline_mass must contain finite positive values")
    if isinstance(month_count, bool) or not isinstance(month_count, (int, np.integer)):
        raise SalesError("month_count must be a non-negative integer")
    if month_count < 0:
        raise SalesError("month_count must be a non-negative integer")

    parents = None if parent_group is None else [str(parent) for parent in parent_group]
    if parents is not None and len(parents) != len(names):
        raise SalesError("parent_group must align with group_names")
    return names, mass, parents


def build_temporal_factors(
    axis_name: str,
    group_names: Sequence[object],
    baseline_mass: Sequence[float],
    month_count: int,
    resolved_trend: MacroDemandConfig,
    run_seed: int,
    parent_group: Sequence[object] | None = None,
    *,
    intensity_scale: float = 1.0,
) -> np.ndarray:
    """Build a positive ``(relative month, group)`` factor matrix.

    When ``parent_group`` is supplied, child residuals are centered separately
    within each parent.  Otherwise the complete axis is centered as one layer.
    The returned columns always match the caller's original group order.
    """
    axis = str(axis_name).strip()
    if not axis:
        raise SalesError("axis_name must be non-empty")
    try:
        seed = int(run_seed)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SalesError("run_seed must be integer-compatible") from exc

    names, mass, parents = _validate_inputs(
        group_names,
        baseline_mass,
        month_count,
        parent_group,
    )
    profile = derive_temporal_mix_profile(resolved_trend, intensity_scale=intensity_scale)

    # Canonical computation order makes normalization bit-identical when input
    # dimension rows arrive in a different order.
    canonical_order = np.asarray(sorted(range(len(names)), key=names.__getitem__), dtype=np.int64)
    inverse_order = np.empty_like(canonical_order)
    inverse_order[canonical_order] = np.arange(canonical_order.size, dtype=np.int64)
    sorted_names = [names[i] for i in canonical_order]
    sorted_mass = mass[canonical_order]
    sorted_parents = None if parents is None else [parents[i] for i in canonical_order]

    if month_count == 0:
        return np.empty((0, len(names)), dtype=np.float64)

    group_keys = np.asarray([_stable_i63(axis, name) for name in sorted_names], dtype=np.int64)
    salt_normal_a = _stable_u64(seed, axis, "innovation-a")
    salt_normal_b = _stable_u64(seed, axis, "innovation-b")
    salt_loading_a = _stable_u64(seed, axis, "curve-loading-a")
    salt_loading_b = _stable_u64(seed, axis, "curve-loading-b")
    salt_shock_event = _stable_u64(seed, axis, "shock-event")
    salt_shock_size = _stable_u64(seed, axis, "shock-size")

    loadings = _normal_draw(salt_loading_a, salt_loading_b, group_keys, 0)
    loadings = np.clip(loadings, -2.5, 2.5)
    state = np.zeros(len(sorted_names), dtype=np.float64)
    raw = np.empty((month_count, len(sorted_names)), dtype=np.float64)

    for month in range(month_count):
        innovation = _normal_draw(salt_normal_a, salt_normal_b, group_keys, month)
        curve_term = profile.curve_capture_scale * _monthly_curve_change(profile, month) * loadings
        shock_event = hash_u01(salt_shock_event, group_keys, month) < profile.shock_probability
        shock_u = hash_u01(salt_shock_size, group_keys, month)
        shock = np.where(
            shock_event,
            profile.shock_low + shock_u * (profile.shock_high - profile.shock_low),
            0.0,
        )
        state = (
            profile.persistence * state
            + curve_term
            + profile.innovation_scale * innovation
            + shock
        )
        state = np.clip(
            state,
            math.log(profile.factor_floor) - 0.50,
            math.log(profile.factor_ceiling) + 0.50,
        )
        raw[month] = np.exp(state)

    factors = np.empty_like(raw)
    if sorted_parents is None:
        factors[:] = _bounded_weighted_normalize(
            raw,
            sorted_mass,
            profile.factor_floor,
            profile.factor_ceiling,
        )
    else:
        for parent in sorted(set(sorted_parents)):
            positions = np.asarray(
                [i for i, value in enumerate(sorted_parents) if value == parent],
                dtype=np.int64,
            )
            factors[:, positions] = _bounded_weighted_normalize(
                raw[:, positions],
                sorted_mass[positions],
                profile.factor_floor,
                profile.factor_ceiling,
            )

    if not np.all(np.isfinite(factors)) or np.any(factors <= 0.0):
        raise SalesError(f"Temporal mix factors for axis {axis!r} are not finite and positive")
    return factors[:, inverse_order]
