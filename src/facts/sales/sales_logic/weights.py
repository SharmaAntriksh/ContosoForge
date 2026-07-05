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
