"""Typed worker→collector protocol for the sales fact.

One :class:`ChunkResult` carries everything a worker produces for one chunk: the
artifact written per output table, plus the streamed micro-aggregate payloads
for the optional facts (budget / inventory / wishlists / complaints).

This replaces the old ``str``-or-``Mapping``-with-magic-``_*_agg``-keys return
protocol, where a bare path string, a Delta ``{"part","rows"}`` descriptor, and a
multi-table dict all flowed through the same channel and the main process had to
``pop`` reserved keys out of the table map to separate aggregates from outputs.

Both processes import this leaf module (it depends only on the stdlib), so the
dataclass pickles cleanly across the multiprocessing pool boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Union

# A written chunk artifact for one table: either a chunk-file path (csv/parquet)
# or a Delta part descriptor ``{"part": basename, "rows": n}``.
ChunkRef = Union[str, Dict[str, Any]]


@dataclass
class ChunkResult:
    """One chunk's worker output.

    ``chunk_idx`` is the chunk's index (used for progress logging without
    re-parsing filenames). ``outputs`` maps a logical table name to the artifact
    written for it. ``aggs`` maps a streamed-fact agg key (e.g. ``"_budget_agg"``)
    to its micro-aggregate payload; absent facts simply have no entry.
    """
    chunk_idx: int
    outputs: Dict[str, ChunkRef] = field(default_factory=dict)
    aggs: Dict[str, Any] = field(default_factory=dict)
