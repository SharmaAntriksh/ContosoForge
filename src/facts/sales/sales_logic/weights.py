"""Single source of truth for probability normalization and CDF construction.

Consolidates helpers that were previously duplicated — with subtly divergent
semantics — across the sales chunk builder, columns, and core routines:

  * two ``_normalize_prob`` clones that disagreed on the all-zero case
    (``columns`` returned a uniform distribution; ``chunk_builder`` returned
    ``None``);
  * two ``_weighted_pick`` implementations (promotions vs. store sampling);
  * a scatter of hand-inlined ``cdf[-1] = 1.0`` clamp sites.

CLAUDE.md gotcha #16 — clamp ``cdf[-1]`` to 1.0 so
``np.searchsorted(..., side="right")`` cannot return an out-of-bounds index —
is enforced here, once, instead of at every call site.
"""

from __future__ import annotations

import numpy as np


def normalize_prob(w, *, on_zero: str):
    """Clamp non-finite/negative weights to 0 and normalize to sum 1.

    ``on_zero`` selects the all-zero (``sum <= 1e-12``) result:
      - ``"uniform"`` -> a uniform distribution over the same length
      - ``"none"``    -> ``None`` (the caller supplies its own fallback)
    """
    w = np.asarray(w, dtype=np.float64)
    w = np.where(np.isfinite(w) & (w > 0), w, 0.0)
    s = float(w.sum())
    if s <= 1e-12:
        if on_zero == "uniform":
            return np.full(w.shape[0], 1.0 / max(1, w.shape[0]), dtype=np.float64)
        return None
    return w / s


def normalize_cdf(weights) -> np.ndarray:
    """``cumsum`` -> divide by the cumulative total -> clamp ``cdf[-1] = 1.0``.

    Returns the raw cumsum when all weights are zero or the array is empty, so
    callers can test ``cdf.size and cdf[-1] > 0`` before sampling. ``float64``.
    """
    cdf = np.cumsum(weights, dtype=np.float64)
    if cdf.size > 0 and cdf[-1] > 0:
        cdf /= cdf[-1]
        cdf[-1] = 1.0
    return cdf


def cdf_from_probs(probs) -> np.ndarray:
    """Cumulative sum of an *already-normalized* probability vector, with
    ``cdf[-1]`` clamped to 1.0 to absorb floating-point drift.

    Precondition: ``probs`` is non-empty and sums to ~1.0 (the clamp only
    corrects last-bit drift; it does not renormalize).
    """
    cdf = np.cumsum(np.asarray(probs, dtype=np.float64))
    cdf[-1] = 1.0
    return cdf


def pick_from_cdf(cdf, u) -> np.ndarray:
    """Inverse-CDF lookup: map per-element uniforms ``u`` in ``[0, 1)`` to indices
    via ``searchsorted(cdf, u, side="right")``, clamped to the last index.

    The counter-based counterpart of ``weighted_pick_indices``: the caller
    supplies the uniforms (e.g. ``hash_u01(salt, key)``) instead of drawing them
    from an RNG, so the pick is a pure function of the key. ``cdf`` is a clamped
    cumulative distribution (see ``cdf_from_probs`` / ``normalize_cdf``).
    """
    cdf = np.asarray(cdf, dtype=np.float64)
    picks = np.searchsorted(cdf, np.asarray(u, dtype=np.float64), side="right")
    return np.minimum(picks, cdf.size - 1)


# Acklam's rational approximation of the inverse standard-normal CDF
# (probit). Absolute error < 1.15e-9 across (0, 1) — far finer than any use
# here needs. Used to turn a counter-based uniform into a normal draw without
# an RNG (numpy has no ``erfinv`` / ``ndtri``).
_ACK_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
          1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_ACK_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
          6.680131188771972e+01, -1.328068155288572e+01)
_ACK_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
          -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_ACK_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
          3.754408661907416e+00)
_ACK_PLOW = 0.02425
_ACK_PHIGH = 1.0 - _ACK_PLOW


def normal_ppf(u) -> np.ndarray:
    """Inverse standard-normal CDF (probit) of uniforms ``u`` in ``(0, 1)``.

    The continuous counterpart of ``pick_from_cdf``: turns a counter-based
    uniform (e.g. ``hash_u01(salt, key)``) into a ``N(0, 1)`` draw, so a
    Gaussian/lognormal quantity is a pure function of the key instead of an RNG
    draw. ``u`` is clamped into the open interval so the tails stay finite.
    """
    u = np.clip(np.asarray(u, dtype=np.float64), 1e-15, 1.0 - 1e-15)
    x = np.empty_like(u)

    lo = u < _ACK_PLOW
    hi = u > _ACK_PHIGH
    mid = ~(lo | hi)

    q = np.sqrt(-2.0 * np.log(u[lo]))
    x[lo] = ((((((_ACK_C[0] * q + _ACK_C[1]) * q + _ACK_C[2]) * q + _ACK_C[3]) * q
               + _ACK_C[4]) * q + _ACK_C[5])
             / ((((_ACK_D[0] * q + _ACK_D[1]) * q + _ACK_D[2]) * q + _ACK_D[3]) * q + 1.0))

    q = np.sqrt(-2.0 * np.log1p(-u[hi]))
    x[hi] = -((((((_ACK_C[0] * q + _ACK_C[1]) * q + _ACK_C[2]) * q + _ACK_C[3]) * q
                + _ACK_C[4]) * q + _ACK_C[5])
              / ((((_ACK_D[0] * q + _ACK_D[1]) * q + _ACK_D[2]) * q + _ACK_D[3]) * q + 1.0))

    q = u[mid] - 0.5
    r = q * q
    x[mid] = (((((_ACK_A[0] * r + _ACK_A[1]) * r + _ACK_A[2]) * r + _ACK_A[3]) * r
               + _ACK_A[4]) * r + _ACK_A[5]) * q \
        / (((((_ACK_B[0] * r + _ACK_B[1]) * r + _ACK_B[2]) * r + _ACK_B[3]) * r + _ACK_B[4]) * r + 1.0)
    return x


def weighted_pick_indices(rng, weights, size, n_items):
    """Return ``size`` indices in ``[0, n_items)`` sampled ∝ ``weights``.

    ``weights`` is aligned to the items (length ``n_items``). When the total is
    non-finite or ``<= 0``, falls back to a uniform integer draw. Exactly one
    RNG draw per call — ``rng.random(size)`` (weighted) or ``rng.integers``
    (uniform) — so the RNG stream position is branch-dependent but fixed.
    """
    total = weights.sum()
    if not np.isfinite(total) or total <= 0:
        return rng.integers(0, n_items, size=size)
    cdf = np.cumsum(weights) / total
    cdf[-1] = 1.0
    picks = np.searchsorted(cdf, rng.random(size), side="right")
    return np.minimum(picks, n_items - 1)
