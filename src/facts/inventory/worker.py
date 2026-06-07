"""Inventory snapshot multiprocessing worker.

Each worker receives a store-group partition of the demand DataFrame,
runs the vectorized inventory simulation, and writes chunk files directly.
Returns lightweight stats (no large data crosses the IPC boundary).

Stores are fully independent — a product's inventory at Store A has zero
dependency on Store B — so partitioning by store groups is embarrassingly
parallel with identical results to the monolithic path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .engine import InventoryConfig, compute_inventory_snapshots

_INVENTORY_CSV_COLUMNS = [
    "ProductKey", "WarehouseKey", "SnapshotDate",
    "QuantityOnHand", "QuantityOnOrder",
    "QuantitySold", "QuantityReceived",
    "ReorderFlag", "StockoutFlag", "DaysOutOfStock",
]

_INVENTORY_CSV_INT_COLS = (
    "ProductKey", "WarehouseKey",
    "QuantityOnHand", "QuantityOnOrder",
    "QuantitySold", "QuantityReceived",
    "ReorderFlag", "StockoutFlag", "DaysOutOfStock",
)


def _cast_snapshot_date(table: pa.Table) -> pa.Table:
    """Cast SnapshotDate to date32 so Power Query reads Date, not DateTime."""
    sd = table.column("SnapshotDate")
    if not pa.types.is_date32(sd.type):
        table = table.set_column(
            table.schema.get_field_index("SnapshotDate"),
            pa.field("SnapshotDate", pa.date32()),
            sd.cast(pa.date32()),
        )
    return table


def _inventory_worker_task(args: Tuple) -> Dict[str, Any]:
    """
    Worker entry point (must be top-level for Windows spawn pickling).

    Args (tuple):
        chunk_idx:       int — chunk sequence number
        demand_arrays:   dict of numpy arrays
        parquet_dims:    str — path to dimension parquets
        icfg_dict:       dict — InventoryConfig fields
        output_base:     str — base path without extension (e.g. .../inventory_chunk_00000)
        file_format:     str — "csv", "parquet", or "deltaparquet"
        product_attrs_arrays: dict of numpy arrays or None — pre-loaded product attributes

    Returns:
        dict with chunk_idx, rows, stockout_sum
    """
    if len(args) == 7:
        chunk_idx, demand_arrays, parquet_dims, icfg_dict, output_base, file_format, product_attrs_arrays = args
    else:
        chunk_idx, demand_arrays, parquet_dims, icfg_dict, output_base, file_format = args
        product_attrs_arrays = None

    demand = pd.DataFrame({
        "ProductKey": demand_arrays["ProductKey"],
        "WarehouseKey": demand_arrays["WarehouseKey"],
        "Year": demand_arrays["Year"],
        "Month": demand_arrays["Month"],
        "QuantitySold": demand_arrays["QuantitySold"],
    })

    # Derive a unique but deterministic seed per chunk to avoid
    # RNG correlations between workers while staying reproducible.
    icfg_dict = dict(icfg_dict)
    icfg_dict["seed"] = icfg_dict["seed"] + chunk_idx
    icfg = InventoryConfig(**icfg_dict)

    snapshots = compute_inventory_snapshots(
        demand=demand,
        parquet_dims=parquet_dims,
        icfg=icfg,
        product_attrs_arrays=product_attrs_arrays,
    )

    n_rows = len(snapshots)
    stockout_sum = 0

    if n_rows > 0:
        stockout_sum = int(snapshots["StockoutFlag"].sum())

        out_dir = Path(output_base).parent
        out_dir.mkdir(parents=True, exist_ok=True)

        # Always write parquet (used as dataset, and fast)
        pq_path = output_base + ".parquet"
        table = pa.Table.from_pandas(snapshots, preserve_index=False)
        # Cast SnapshotDate to date32 so Power Query reads Date, not
        # DateTime (matching Sales behaviour).
        table = _cast_snapshot_date(table)
        pq.write_table(
            table,
            pq_path,
            compression="snappy",
            row_group_size=500_000,
            use_dictionary=True,
        )

        # Write CSV chunk alongside if requested
        if file_format == "csv":
            csv_path = output_base + ".csv"
            csv_df = _prepare_csv(snapshots)
            # Force LF to match the BULK INSERT ROWTERMINATOR='0x0a' (pandas
            # to_csv defaults to CRLF on Windows).
            csv_df.to_csv(csv_path, index=False, lineterminator="\n")

    return {
        "chunk_idx": chunk_idx,
        "rows": n_rows,
        "stockout_sum": stockout_sum,
    }


def _prepare_csv(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in _INVENTORY_CSV_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out = out[_INVENTORY_CSV_COLUMNS]
    for col in _INVENTORY_CSV_INT_COLS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)
    return out
