"""Streaming (serial) bridge writer for small datasets."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from src.defaults import FACT_MAX_PARALLEL_CHUNKS
from src.exceptions import DimensionError

from .helpers import (
    SubscriptionsCfg,
    _NS_PER_DAY,
    build_type_groups,
    compute_customer_windows,
    generate_subscriptions_bulk,
)


def write_bridge_streaming(
    customers: pd.DataFrame,
    dim_plans: pd.DataFrame,
    c: SubscriptionsCfg,
    g_start: pd.Timestamp,
    g_end: pd.Timestamp,
    out_bridge: Path,
) -> int:
    """Stream-write customer_subscriptions billing-period fact to parquet.
    Returns number of rows written.
    """
    if "CustomerKey" not in customers.columns:
        raise DimensionError("customers must include CustomerKey")

    plan_keys = dim_plans["PlanKey"].astype(np.int32).to_numpy()
    plan_types = dim_plans["PlanType"].astype(str).to_numpy()
    plan_cycle_prices = dim_plans["CyclePrice"].to_numpy(dtype=np.float64, na_value=0.0)
    plan_cycle_months = dim_plans["CycleMonths"].astype(np.int32).to_numpy()
    plan_launch_ns = pd.to_datetime(dim_plans["LaunchDate"]).to_numpy(
        dtype="datetime64[ns]",
    ).view(np.int64)
    unique_types, type_members, type_weights = build_type_groups(plan_types)

    cust_keys, cust_start_ns, cust_end_ns = compute_customer_windows(
        customers, g_start, g_end,
    )

    g_end_ns = np.int64(g_end.value)
    max_subs = min(c.max_subscriptions, len(unique_types))

    rng = np.random.default_rng(c.seed)

    n_cust = len(cust_keys)
    participate_mask = rng.random(n_cust) < c.participation_rate

    _part_idx = np.where(participate_mask)[0]
    _spans = np.maximum(0, (cust_end_ns[_part_idx] - cust_start_ns[_part_idx]) // _NS_PER_DAY)
    _ok = _spans >= 30
    eligible_idx = _part_idx[_ok]

    eligible_ck = cust_keys[eligible_idx]
    eligible_lo = cust_start_ns[eligible_idx]
    eligible_hi = cust_end_ns[eligible_idx]
    eligible_span = _spans[_ok]

    out_bridge.parent.mkdir(parents=True, exist_ok=True)
    if out_bridge.exists():
        out_bridge.unlink()

    n_eligible = len(eligible_ck)
    if n_eligible == 0:
        pq.write_table(
            generate_subscriptions_bulk(
                eligible_ck=eligible_ck,
                eligible_lo=eligible_lo,
                eligible_hi=eligible_hi,
                eligible_span=eligible_span,
                plan_keys=plan_keys,
                plan_cycle_prices=plan_cycle_prices,
                plan_cycle_months=plan_cycle_months,
                unique_types=unique_types,
                type_members=type_members,
                type_weights=type_weights,
                g_end_ns=int(g_end_ns),
                max_subs=max_subs,
                avg_subscriptions=c.avg_subscriptions,
                churn_rate=c.churn_rate,
                trial_rate=c.trial_rate,
                trial_conversion_rate=c.trial_conversion_rate,
                trial_days=c.trial_days,
                rng=np.random.default_rng(c.seed),
                plan_launch_ns=plan_launch_ns,
            ),
            out_bridge,
            compression="snappy",
        )
        return 0

    # Stable data-sized chunks make this path byte-equivalent to the parallel
    # writer. Worker count remains a performance choice, never a data choice.
    n_chunks = max(1, min(n_eligible, FACT_MAX_PARALLEL_CHUNKS))
    boundaries = np.array_split(np.arange(n_eligible), n_chunks)
    child_seeds = np.random.SeedSequence(c.seed).spawn(n_chunks)
    total_rows = 0
    sub_key_cursor = 1

    writer = None
    try:
        for chunk_idx, indices in enumerate(boundaries):
            table = generate_subscriptions_bulk(
                eligible_ck=eligible_ck[indices],
                eligible_lo=eligible_lo[indices],
                eligible_hi=eligible_hi[indices],
                eligible_span=eligible_span[indices],
                plan_keys=plan_keys,
                plan_cycle_prices=plan_cycle_prices,
                plan_cycle_months=plan_cycle_months,
                unique_types=unique_types,
                type_members=type_members,
                type_weights=type_weights,
                g_end_ns=int(g_end_ns),
                max_subs=max_subs,
                avg_subscriptions=c.avg_subscriptions,
                churn_rate=c.churn_rate,
                trial_rate=c.trial_rate,
                trial_conversion_rate=c.trial_conversion_rate,
                trial_days=c.trial_days,
                rng=np.random.default_rng(child_seeds[chunk_idx]),
                sub_key_start=sub_key_cursor,
                plan_launch_ns=plan_launch_ns,
            )
            if writer is None:
                writer = pq.ParquetWriter(
                    out_bridge,
                    schema=table.schema,
                    compression="snappy",
                )
            if len(table):
                writer.write_table(table, row_group_size=500_000)
                total_rows += len(table)
            sub_key_cursor += int(len(indices)) * int(max_subs)
    finally:
        if writer is not None:
            writer.close()

    return total_rows
