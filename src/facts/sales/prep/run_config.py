"""Resolved run-level inputs for the sales worker-config builder.

Both classes here are frozen holders the coordinator resolves once and hands to
``_build_worker_cfg``, replacing loose positional arguments / dict injection with
typed, attribute-accessed objects:

- ``SalesRunConfig`` — the run-level scalars (output format, chunking, order-id
  sizing).
- ``MonthPlan`` — the global per-month sales plan (discovery month + the row /
  order / distinct-customer curves), which used to be injected into the customer
  loader's dict post-load.

These are *holders* only: the coordinator still does the precedence resolution
(CLI overrides vs config defaults) and the plan computation inline, and
constructs these from the already-resolved locals. Folding the precedence logic
into a ``resolve`` / ``from_overrides`` constructor is deferred — it carries the
explicit-arg-equals-default trap that must be preserved for byte-identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np


@dataclass(frozen=True)
class SalesRunConfig:
    """Run-level scalars resolved once in the coordinator for a sales run."""

    file_format: str
    out_folder: str
    chunk_size: int
    total_rows: int
    seed: int
    order_id_run_id: int
    sales_output: str
    skip_order_cols: bool
    delta_output_folder: Optional[str]
    partition_enabled: bool
    partition_cols: List[str]
    row_group_size: int
    compression: str
    order_id_day_stride: int = 0
    per_chunk_alloc: int = 0
    order_id_int64: bool = False
    total_chunks: int = 1


@dataclass(frozen=True)
class MonthPlan:
    """Global per-month sales plan, computed once in the coordinator.

    ``customer_discovery_month`` is per-customer (length N); the three curves are
    per-month (length T). All are broadcast read-only so the fact is independent
    of worker count / chunk order.
    """

    customer_discovery_month: np.ndarray
    sales_rows_per_month: np.ndarray
    sales_orders_per_month: np.ndarray
    sales_distinct_target: np.ndarray
