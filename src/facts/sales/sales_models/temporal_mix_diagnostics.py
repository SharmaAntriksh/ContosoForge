"""Reusable analytical diagnostics for temporal rank persistence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from src.exceptions import SalesError


@dataclass(frozen=True)
class RankPersistenceMetrics:
    period_count: int
    entity_count: int
    top_n: int
    mean_position_persistence: float
    mean_adjacent_spearman: float
    adjacent_rank_swaps: int
    top_entity_tenure: int
    minimum_share: float
    maximum_share: float
    mean_concentration: float


def _longest_run(values: np.ndarray) -> int:
    longest = current = 1
    for index in range(1, values.size):
        if values[index] == values[index - 1]:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def rank_persistence_metrics(
    period_entity_values: np.ndarray,
    *,
    entity_names: Sequence[object] | None = None,
    top_n: int = 10,
) -> RankPersistenceMetrics:
    """Summarize rank persistence for a ``(period, entity)`` value matrix."""
    values = np.asarray(period_entity_values, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] < 1 or values.shape[1] < 1:
        raise SalesError("period_entity_values must be a non-empty two-dimensional matrix")
    if not np.all(np.isfinite(values)) or np.any(values < 0.0):
        raise SalesError("period_entity_values must contain finite non-negative values")
    if isinstance(top_n, bool) or not isinstance(top_n, (int, np.integer)) or top_n < 1:
        raise SalesError("top_n must be a positive integer")

    period_count, entity_count = values.shape
    names = [str(i) for i in range(entity_count)] if entity_names is None else [str(v) for v in entity_names]
    if len(names) != entity_count or len(set(names)) != entity_count:
        raise SalesError("entity_names must align with columns and be unique")

    canonical = np.asarray(sorted(range(entity_count), key=names.__getitem__), dtype=np.int64)
    canonical_names = np.asarray([names[i] for i in canonical], dtype=object)
    ordered_values = values[:, canonical]
    totals = ordered_values.sum(axis=1)
    if np.any(totals <= 0.0):
        raise SalesError("every period must have a positive total")
    rankings = np.empty((period_count, entity_count), dtype=np.int64)
    rank_positions = np.empty_like(rankings)
    tie_breaker = np.arange(entity_count, dtype=np.int64)
    for period in range(period_count):
        order = np.lexsort((tie_breaker, -ordered_values[period]))
        rankings[period] = order
        rank_positions[period, order] = np.arange(entity_count, dtype=np.int64)

    effective_top_n = min(int(top_n), entity_count)
    persistence = []
    for position in range(effective_top_n):
        occupants = rankings[:, position]
        counts = np.bincount(occupants, minlength=entity_count)
        persistence.append(float(counts.max()) / period_count)

    if entity_count == 1 or period_count == 1:
        mean_spearman = 1.0
    else:
        correlations = [
            float(np.corrcoef(rank_positions[p - 1], rank_positions[p])[0, 1])
            for p in range(1, period_count)
        ]
        mean_spearman = float(np.mean(correlations))

    swaps = 0
    for period in range(1, period_count):
        prior_order = rankings[period - 1]
        current_positions = rank_positions[period]
        for position in range(entity_count - 1):
            first = prior_order[position]
            second = prior_order[position + 1]
            if current_positions[first] > current_positions[second]:
                swaps += 1

    top_names = canonical_names[rankings[:, 0]]
    shares = ordered_values / totals[:, None]
    return RankPersistenceMetrics(
        period_count=period_count,
        entity_count=entity_count,
        top_n=effective_top_n,
        mean_position_persistence=float(np.mean(persistence)),
        mean_adjacent_spearman=mean_spearman,
        adjacent_rank_swaps=swaps,
        top_entity_tenure=_longest_run(top_names),
        minimum_share=float(shares.min()),
        maximum_share=float(shares.max()),
        mean_concentration=float(np.mean(np.square(shares).sum(axis=1))),
    )
