"""SCD2 per-entity version-grid builders for the sales fact.

Reads an SCD2 dimension parquet and builds the version-grid index used to resolve
the correct product/customer version at each sale's date. Pure and deterministic
(parquet reads + numpy) — no State, no RNG.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class _Scd2VersionCtx:
    """Shared index machinery for an SCD2 per-entity version grid.

    ``starts`` and the scatter coordinates (``pi``/``si`` selected by ``valid``)
    are the same for any payload; each caller allocates its own payload grid,
    seeds it with the IsCurrent defaults, then scatters
    ``payload[col][valid]`` into ``[pi, si]``.
    """
    n_pool: int
    max_ver: int
    valid: np.ndarray                 # bool mask over the sorted rows
    pi: np.ndarray                    # pool index of each valid row
    si: np.ndarray                    # version slot of each valid row
    payload: Dict[str, np.ndarray]    # sorted payload columns (index with ``valid``)


def _scd2_version_index(
    source_path: Path,
    *,
    id_col: str,
    pool_ids: np.ndarray,
    payload_cols: Dict[str, type],
) -> Optional[Tuple[np.ndarray, _Scd2VersionCtx]]:
    """Read an SCD2 dimension and build its shared version-grid index.

    ``pool_ids`` is the natural key (e.g. ProductID / CustomerID) per pool slot;
    ``payload_cols`` maps each per-version column to the numpy dtype to read it
    as. Returns ``(starts, ctx)`` where ``starts`` is the (N_pool, max_ver) int64
    grid of version start epoch-days (padded with INT64_MAX, first slot clamped to
    0), or ``None`` when the source lacks the required columns.

    Lookup: ``ver = searchsorted(starts[P], D, side='right') - 1``.
    """
    read_cols = [id_col, "EffectiveStartDate", "EffectiveEndDate", *payload_cols]
    try:
        all_df = pd.read_parquet(str(source_path), columns=read_cols)
    except (KeyError, ValueError):
        return None

    eff_start = pd.to_datetime(all_df["EffectiveStartDate"]).values.astype("datetime64[D]").astype(np.int64)
    n_pool = len(pool_ids)

    # Dense natural-key -> pool-index lookup.
    max_id = max(int(pool_ids.max()), int(all_df[id_col].max())) + 1
    id_lookup = np.full(max_id, -1, dtype=np.int32)
    id_lookup[pool_ids] = np.arange(n_pool, dtype=np.int32)

    pool_idx = id_lookup[all_df[id_col].to_numpy()]
    mask = pool_idx >= 0
    pool_idx = pool_idx[mask]
    eff_start = eff_start[mask]
    payload = {c: all_df[c].to_numpy(dtype=dt)[mask] for c, dt in payload_cols.items()}

    # Sort by (pool_idx, eff_start) via lexsort (secondary key first).
    order = np.lexsort((eff_start, pool_idx))
    pool_idx = pool_idx[order]
    eff_start = eff_start[order]
    payload = {c: v[order] for c, v in payload.items()}

    # Per-entity version slot indices from group boundaries.
    group_starts = np.concatenate([[0], np.where(pool_idx[1:] != pool_idx[:-1])[0] + 1])
    slot = np.arange(len(pool_idx), dtype=np.int32)
    slot -= np.repeat(group_starts, np.diff(np.append(group_starts, len(pool_idx))))
    max_ver = int(slot.max()) + 1 if len(slot) > 0 else 1

    # starts: padded with INT64_MAX, valid slots scattered, first slot clamped to 0.
    # ``valid`` (slot < max_ver) is all-True here — a defensive cap for callers.
    starts = np.full((n_pool, max_ver), np.iinfo(np.int64).max, dtype=np.int64)
    valid = slot < max_ver
    pi = pool_idx[valid]
    si = slot[valid]
    starts[pi, si] = eff_start[valid]
    starts[pi, 0] = 0

    return starts, _Scd2VersionCtx(n_pool=n_pool, max_ver=max_ver, valid=valid, pi=pi, si=si, payload=payload)


def _build_scd2_product_versions(
    products_path: Path,
    pool_product_ids: np.ndarray,
    pool_product_np: np.ndarray,
) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Build per-entity version lookup tables for SCD2 product resolution.

    Returns (starts, data):
      - starts: shape (N_pool, max_ver) int64 — EffectiveStartDate as epoch days,
        sorted ascending per entity, padded with INT64_MAX.
      - data: shape (N_pool, max_ver, 3) float64 — [ProductKey, ListPrice, UnitCost]
        per version slot, padded with IsCurrent=1 values.
    """
    res = _scd2_version_index(
        products_path,
        id_col="ProductID",
        pool_ids=pool_product_ids,
        payload_cols={"ProductKey": np.float64, "ListPrice": np.float64, "UnitCost": np.float64},
    )
    if res is None:
        return None
    starts, ctx = res

    # Seed every slot with the IsCurrent product row, then scatter historical versions.
    data = np.empty((ctx.n_pool, ctx.max_ver, 3), dtype=np.float64)
    data[:, :, 0] = pool_product_np[:, 0:1]  # ProductKey broadcast
    data[:, :, 1] = pool_product_np[:, 1:2]  # ListPrice broadcast
    data[:, :, 2] = pool_product_np[:, 2:3]  # UnitCost broadcast
    data[ctx.pi, ctx.si, 0] = ctx.payload["ProductKey"][ctx.valid]
    data[ctx.pi, ctx.si, 1] = ctx.payload["ListPrice"][ctx.valid]
    data[ctx.pi, ctx.si, 2] = ctx.payload["UnitCost"][ctx.valid]

    return starts, data


def _build_scd2_customer_versions(
    customers_path: Path,
    pool_customer_keys: np.ndarray,
    pool_customer_ids: np.ndarray,
) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Build per-entity version lookup tables for SCD2 customer resolution.

    Returns (starts, keys, key_to_pool_idx):
      - starts: shape (N_pool, max_ver) int64 — EffectiveStartDate as epoch days,
        sorted ascending per entity, padded with INT64_MAX.
      - keys: shape (N_pool, max_ver) int32 — CustomerKey per version slot,
        padded with IsCurrent=1 key.
      - key_to_pool_idx: dense int32 array mapping IsCurrent CustomerKey → pool index.
    """
    res = _scd2_version_index(
        customers_path,
        id_col="CustomerID",
        pool_ids=pool_customer_ids,
        payload_cols={"CustomerKey": np.int32},
    )
    if res is None:
        return None
    starts, ctx = res

    # Dense IsCurrent CustomerKey -> pool index reverse map.
    max_key = int(pool_customer_keys.max()) + 1
    key_to_pool_idx = np.full(max_key, -1, dtype=np.int32)
    key_to_pool_idx[pool_customer_keys] = np.arange(len(pool_customer_keys), dtype=np.int32)

    # Seed every slot with the IsCurrent key, then scatter historical versions.
    keys = np.empty((ctx.n_pool, ctx.max_ver), dtype=np.int32)
    keys[:] = pool_customer_keys.astype(np.int32)[:, np.newaxis]
    keys[ctx.pi, ctx.si] = ctx.payload["CustomerKey"][ctx.valid]

    return starts, keys, key_to_pool_idx
