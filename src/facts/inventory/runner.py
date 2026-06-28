"""Inventory snapshot pipeline runner.

Called from sales_runner.py after sales generation completes.
Uses the InventoryAccumulator that was populated during sales generation.

For large datasets (many product-warehouse pairs), partitions demand by
warehouse groups and runs the simulation in parallel across multiple
processes.  Each worker writes its own chunk files directly (CSV +
parquet), avoiding the need to build a single massive DataFrame in one
process.

After parallel chunks are written, they are merged into a single Parquet
file (like Sales).  For deltaparquet mode, chunks are consolidated into a
Delta Lake table partitioned by Year + Month.
"""
from __future__ import annotations

import dataclasses
import os
import time
from multiprocessing import cpu_count
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.utils.logging_utils import done, info, work, short_path
from src.facts.shared.writers import write_fact_table

from .accumulator import InventoryAccumulator
from .engine import load_inventory_config, compute_inventory_snapshots, InventoryConfig, _load_product_attrs
from .worker import _inventory_worker_task, _cast_snapshot_date, _prepare_csv as _prepare_inventory_csv
from src.defaults import INVENTORY_PARALLEL_THRESHOLD as _PARALLEL_THRESHOLD, FACT_MAX_PARALLEL_CHUNKS


def _rollup_demand_to_warehouse(
    demand: pd.DataFrame,
    parquet_dims: Path,
) -> pd.DataFrame:
    """Aggregate store-level demand to warehouse-level.

    Replaces StoreKey with WarehouseKey and re-sums QuantitySold
    so the inventory engine operates at warehouse grain.
    """
    stores_path = parquet_dims / "stores.parquet"
    if not stores_path.exists():
        raise FileNotFoundError(f"Missing stores parquet: {stores_path}")

    store_schema = pq.read_schema(str(stores_path))
    if "WarehouseKey" not in store_schema.names:
        raise FileNotFoundError(
            "stores.parquet is missing WarehouseKey column. "
            "Run with --regen-dimensions stores to rebuild stores and warehouses."
        )
    stores = pd.read_parquet(str(stores_path), columns=["StoreKey", "WarehouseKey"])
    sk_to_wk = dict(zip(
        stores["StoreKey"].astype(np.int32),
        stores["WarehouseKey"].astype(np.int32),
    ))

    demand = demand.copy()
    demand["WarehouseKey"] = demand["StoreKey"].map(sk_to_wk).fillna(-1).astype(np.int32)
    demand = demand[demand["WarehouseKey"] >= 0]

    # Re-aggregate at warehouse grain
    rolled = (
        demand
        .groupby(["ProductKey", "WarehouseKey", "Year", "Month"], sort=False)["QuantitySold"]
        .sum()
        .reset_index()
    )

    info(f"Demand rolled up: {len(sk_to_wk)} stores -> {len(set(sk_to_wk.values()))} warehouses")

    return rolled


def _load_pk_to_pid(parquet_dims: Path) -> Dict[int, int]:
    """ProductKey -> ProductID (family) map from product_profile.

    Returns ``{}`` when the profile has no ProductID column, in which case the
    caller treats ProductKey as the family (no product SCD2). Used so ABC ranking
    aggregates a SCD2 family's volume across its version keys (INV-1).
    """
    pp_path = parquet_dims / "product_profile.parquet"
    if not pp_path.exists():
        return {}
    names = set(pq.read_schema(str(pp_path)).names)
    if "ProductID" not in names or "ProductKey" not in names:
        return {}
    t = pq.read_table(str(pp_path), columns=["ProductKey", "ProductID"])
    pk = t.column("ProductKey").to_numpy()
    pid = t.column("ProductID").to_numpy()
    return {int(k): int(v) for k, v in zip(pk, pid)}


def _recompute_abc_from_demand(
    demand: pd.DataFrame,
    product_attrs_arrays: Dict[str, np.ndarray],
    pk_to_pid: Optional[Dict[int, int]] = None,
) -> Dict[str, np.ndarray]:
    """Recompute ABCClassification from actual sales volume.

    Ranks product FAMILIES by total QuantitySold across all stores and months:
      - Top 20% by volume -> A
      - Next 30% -> B
      - Bottom 50% -> C
      - Families with zero sales -> C

    Under product SCD2 the demand ProductKey is version-specific, so *pk_to_pid*
    maps each version key to its ProductID (family): volume is aggregated per
    family before ranking and one ABC is assigned per family, then broadcast to
    all of the family's version keys. Without SCD2 (empty/None map) ProductKey is
    the family and behaviour is unchanged (INV-1).
    """
    pa_pk = product_attrs_arrays["ProductKey"]

    if pk_to_pid:
        # Translate version keys -> family (ProductID); unknown keys map to self.
        def _fam(s: pd.Series) -> pd.Series:
            mapped = s.map(pk_to_pid)
            return mapped.where(mapped.notna(), s).astype(np.int64)

        vol = demand.groupby(_fam(demand["ProductKey"]), sort=False)["QuantitySold"].sum()
        pa_lookup = _fam(pd.Series(pa_pk))
    else:
        vol = demand.groupby("ProductKey", sort=False)["QuantitySold"].sum()
        pa_lookup = pd.Series(pa_pk)

    if vol.empty:
        return product_attrs_arrays

    # Rank: highest volume = 1, lowest = N
    ranks = vol.rank(method="first", ascending=False)
    n = len(ranks)
    abc = np.where(
        ranks <= n * 0.20, "A",
        np.where(ranks <= n * 0.50, "B", "C"),
    )
    vol_abc = pd.Series(abc, index=vol.index)

    # Vectorised override: map each product's family -> volume-based ABC;
    # families with no sales default to C
    mapped = pa_lookup.map(vol_abc)
    pa_abc = mapped.fillna("C").to_numpy()
    updated = int(mapped.notna().sum())
    no_sales = len(pa_abc) - updated

    product_attrs_arrays = dict(product_attrs_arrays)  # shallow copy
    product_attrs_arrays["ABCClassification"] = pa_abc

    info(
        f"ABC reclassified from sales volume: "
        f"{(pa_abc == 'A').sum()} A, {(pa_abc == 'B').sum()} B, "
        f"{(pa_abc == 'C').sum()} C "
        f"({updated} from sales, {no_sales} no-sales -> C)"
    )
    return product_attrs_arrays


def _update_product_profile_abc(
    parquet_dims: Path,
    product_attrs_arrays: Dict[str, np.ndarray],
) -> None:
    """Write volume-based ABC back to product_profile.parquet.

    Under SCD2 the profile carries one row per (ProductID, VersionNumber),
    but sales demand only ever references the current ProductKey of a
    family. Map by ProductID so historical version rows inherit the same
    ABC class as their current sibling — otherwise non-current rows
    silently default to C and contradict the live classification.
    """
    pp_path = parquet_dims / "product_profile.parquet"
    if not pp_path.exists():
        return

    new_abc = product_attrs_arrays["ABCClassification"]
    pa_pk = product_attrs_arrays["ProductKey"]

    table = pq.read_table(str(pp_path))
    schema_names = set(table.schema.names)

    if "ProductID" in schema_names:
        # Translate sales-keyed ABC (ProductKey of current version) into
        # ProductID-keyed ABC, then broadcast across every profile row.
        pp_pk = np.array(table.column("ProductKey").to_pylist(), dtype=np.int64)
        pp_pid = np.array(table.column("ProductID").to_pylist(), dtype=np.int64)
        pid_by_pk = dict(zip(pp_pk.tolist(), pp_pid.tolist()))
        abc_by_pid: Dict[int, str] = {}
        for pk, abc in zip(pa_pk, new_abc):
            pid = pid_by_pk.get(int(pk))
            if pid is not None:
                abc_by_pid[pid] = abc
        updated_abc = [abc_by_pid.get(int(pid), "C") for pid in pp_pid]
        lookup_arr = pp_pid
    else:
        abc_by_pk: Dict[int, str] = dict(zip(pa_pk, new_abc))
        lookup_arr = np.array(table.column("ProductKey").to_pylist(), dtype=np.int64)
        updated_abc = [abc_by_pk.get(int(pk), "C") for pk in lookup_arr]

    idx = table.schema.get_field_index("ABCClassification")
    table = table.set_column(idx, "ABCClassification", pa.array(updated_abc, type=pa.large_string()))

    pq.write_table(table, str(pp_path), compression="snappy")
    info(f"Updated product_profile.parquet with volume-based ABC ({len(lookup_arr):,} rows)")


def run_inventory_pipeline(
    *,
    accumulator: InventoryAccumulator,
    parquet_dims: Path,
    fact_out: Path,
    cfg: Dict[str, Any],
    file_format: str = "parquet",
    workers: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Generate InventorySnapshot fact table from streaming-aggregated sales demand.

    For large datasets, automatically partitions by store groups and uses
    multiprocessing for parallel simulation + chunked writes.
    """
    icfg = load_inventory_config(cfg)
    if not icfg.enabled:
        info("Inventory snapshot generation: disabled in config")
        return None

    if not accumulator.has_data:
        info("Inventory snapshot generation: no sales demand accumulated, skipping")
        return None

    t0 = time.time()

    demand = accumulator.finalize()

    # If workers already aggregated at warehouse grain, skip the expensive rollup.
    if "WarehouseKey" in demand.columns:
        info("Demand already at warehouse grain (pre-mapped in workers)")
    else:
        demand = _rollup_demand_to_warehouse(demand, parquet_dims)

    # Single groupby for both metrics
    _pair_groups = demand.groupby(["ProductKey", "WarehouseKey"])
    n_pairs = _pair_groups.ngroups
    qualified_pairs = int((_pair_groups.size() >= icfg.min_demand_months).sum())
    n_warehouses = demand["WarehouseKey"].nunique()
    from src.utils.output_utils import format_number_short
    info(
        f"Inventory demand: {format_number_short(len(demand))} monthly rows "
        f"({format_number_short(n_pairs)} product-warehouse pairs, "
        f"{n_warehouses} warehouses, {demand['Year'].nunique()} years)"
    )

    inv_out = fact_out / "inventory"
    inv_out.mkdir(parents=True, exist_ok=True)

    inv_cfg = getattr(cfg, "inventory", None) or {}
    sales_cfg = getattr(cfg, "sales", None) or {}
    merge_enabled = bool(getattr(sales_cfg, "merge_parquet", True))
    merge_file = "inventory_snapshot.parquet"
    delete_chunks = bool(getattr(sales_cfg, "delete_chunks", True))
    compression = str(getattr(sales_cfg, "compression", "snappy"))
    partition_by: List[str] = getattr(inv_cfg, "partition_by", None) or []

    # Load product attributes ONCE in the main process instead of per-worker
    product_attrs = _load_product_attrs(parquet_dims)
    product_attrs_arrays: Optional[Dict[str, np.ndarray]] = None
    if not product_attrs.empty:
        product_attrs_arrays = {
            col: product_attrs[col].to_numpy(copy=True)
            for col in product_attrs.columns
        }

    # Recompute ABC classification from actual sales volume instead of the
    # static price-based formula in product_profile.  This ensures high-volume
    # low-price products (e.g. Tailspin Toys) are correctly classified as A.
    if product_attrs_arrays is not None and "ABCClassification" in product_attrs_arrays:
        # Family map so a product-SCD2 family's volume is ranked as one unit (INV-1).
        pk_to_pid = _load_pk_to_pid(parquet_dims)
        product_attrs_arrays = _recompute_abc_from_demand(
            demand, product_attrs_arrays, pk_to_pid,
        )
        # Write updated ABC back to product_profile so Power BI sees it
        _update_product_profile_abc(parquet_dims, product_attrs_arrays)

    csv_chunk_size = int(getattr(icfg, "write_chunk_rows", 2_000_000))

    if qualified_pairs >= _PARALLEL_THRESHOLD and n_warehouses >= 2:
        result = _run_parallel(
            demand, parquet_dims, icfg, inv_out, file_format, n_warehouses,
            workers=workers,
            merge_enabled=merge_enabled,
            merge_file=merge_file,
            delete_chunks=delete_chunks,
            partition_by=partition_by,
            product_attrs_arrays=product_attrs_arrays,
            compression=compression,
            csv_chunk_size=csv_chunk_size,
        )
    else:
        result = _run_single(demand, parquet_dims, icfg, inv_out, file_format, partition_by=partition_by,
                             product_attrs_arrays=product_attrs_arrays)

    elapsed = time.time() - t0
    n_rows = result["rows"]
    stockout_pct = result["stockout_pct"]

    info(
        f"Inventory snapshot: {n_rows:,} rows, "
        f"{stockout_pct:.1f}% stockout rate, "
        f"{elapsed:.1f}s"
    )

    result["elapsed_sec"] = round(elapsed, 2)
    return result


# ------------------------------------------------------------------
# Single-process path (original behavior, for small datasets)
# ------------------------------------------------------------------

def _run_single(
    demand: pd.DataFrame,
    parquet_dims: Path,
    icfg: InventoryConfig,
    inv_out: Path,
    file_format: str,
    partition_by: Optional[List[str]] = None,
    product_attrs_arrays: Optional[Dict[str, np.ndarray]] = None,
) -> Dict[str, Any]:
    """Original monolithic path — used for small datasets."""
    snapshots = compute_inventory_snapshots(
        demand=demand,
        parquet_dims=parquet_dims,
        icfg=icfg,
        product_attrs_arrays=product_attrs_arrays,
    )

    _write_inventory(snapshots, inv_out, "inventory_snapshot", file_format, partition_by=partition_by)

    # For deltaparquet the delta table is written outside inv_out;
    # remove the empty temporary directory.
    if file_format == "deltaparquet":
        try:
            inv_out.rmdir()
        except OSError:
            pass

    n_rows = len(snapshots)
    stockout_pct = 0.0
    if n_rows > 0:
        stockout_pct = float(snapshots["StockoutFlag"].sum()) / n_rows * 100

    n_pairs = demand.groupby(["ProductKey", "WarehouseKey"]).ngroups
    return {
        "rows": n_rows,
        "product_warehouse_pairs": n_pairs,
        "stockout_pct": round(stockout_pct, 2),
    }


# ------------------------------------------------------------------
# Parallel path (partitioned by warehouse groups)
# ------------------------------------------------------------------

def _partition_demand_by_warehouse(
    demand: pd.DataFrame,
    n_chunks: int,
) -> list[pd.DataFrame]:
    """Split demand into n_chunks groups by WarehouseKey (round-robin)."""
    unique_wh = np.sort(demand["WarehouseKey"].unique())
    wh_to_chunk = {w: i % n_chunks for i, w in enumerate(unique_wh)}
    chunk_id = demand["WarehouseKey"].map(wh_to_chunk)
    return [group_df for _, group_df in demand.groupby(chunk_id, sort=False)]


def _demand_to_arrays(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    """Convert demand DataFrame to dict of numpy arrays for pickling."""
    return {
        "ProductKey": df["ProductKey"].to_numpy(copy=True),
        "WarehouseKey": df["WarehouseKey"].to_numpy(copy=True),
        "Year": df["Year"].to_numpy(copy=True),
        "Month": df["Month"].to_numpy(copy=True),
        "QuantitySold": df["QuantitySold"].to_numpy(copy=True),
    }


def _run_parallel(
    demand: pd.DataFrame,
    parquet_dims: Path,
    icfg: InventoryConfig,
    inv_out: Path,
    file_format: str,
    n_warehouses: int,
    workers: Optional[int] = None,
    merge_enabled: bool = True,
    merge_file: str = "inventory_snapshot.parquet",
    delete_chunks: bool = True,
    partition_by: Optional[List[str]] = None,
    product_attrs_arrays: Optional[Dict[str, np.ndarray]] = None,
    compression: str = "snappy",
    csv_chunk_size: int = 2_000_000,
) -> Dict[str, Any]:
    """Partition by warehouse groups and run simulation in parallel."""
    from src.utils.pool import PoolRunSpec, iter_imap_unordered

    n_cpus = max(1, cpu_count() - 1)
    if workers is not None and workers >= 1:
        n_cpus = min(n_cpus, workers)

    # Chunk COUNT depends only on the data (warehouse count), not the worker
    # count, so the warehouse->chunk partitioning and the per-chunk seed
    # (icfg.seed + chunk_idx) — and thus the simulated snapshots — are identical
    # across machines. The pool SIZE still adapts to the available workers.
    n_chunks = max(2, min(n_warehouses, FACT_MAX_PARALLEL_CHUNKS))

    partitions = _partition_demand_by_warehouse(demand, n_chunks)
    partitions = [p for p in partitions if len(p) > 0]
    n_chunks = len(partitions)

    n_workers = min(n_chunks, n_cpus)

    info(f"Inventory parallel: {n_chunks} warehouse-group chunks across {n_workers} workers")

    icfg_dict = dataclasses.asdict(icfg)
    parquet_dims_str = str(parquet_dims)

    tasks = []
    for idx, part_df in enumerate(partitions):
        # Base path without extension — worker appends .parquet and/or .csv
        out_base = str(inv_out / f"inventory_chunk_{idx:05d}")
        tasks.append((
            idx,
            _demand_to_arrays(part_df),
            parquet_dims_str,
            icfg_dict,
            out_base,
            file_format,
            product_attrs_arrays,
        ))

    del partitions

    pool_spec = PoolRunSpec(
        processes=n_workers,
        label="inventory",
    )

    total_rows = 0
    total_stockout = 0
    completed = 0

    for result in iter_imap_unordered(
        tasks=tasks,
        task_fn=_inventory_worker_task,
        spec=pool_spec,
    ):
        completed += 1
        total_rows += result["rows"]
        total_stockout += result["stockout_sum"]

    work(f"{completed}/{n_chunks} inventory chunks completed ({total_rows:,} total rows)")

    stockout_pct = 0.0
    if total_rows > 0:
        stockout_pct = total_stockout / total_rows * 100

    # ------------------------------------------------------------------
    # Post-processing: merge chunk parquets into a single file, or
    # consolidate into a Delta Lake table with Year+Month partitioning.
    # ------------------------------------------------------------------
    if total_rows > 0:
        chunk_files = sorted(inv_out.glob("inventory_chunk_*.parquet"))

        if file_format == "deltaparquet":
            _merge_chunks_to_delta(
                chunk_files=chunk_files,
                inv_out=inv_out,
                partition_by=partition_by or [],
                delete_chunks=delete_chunks,
            )
            # Remove the now-empty temporary chunk directory
            try:
                inv_out.rmdir()
            except OSError:
                pass
        elif merge_enabled and file_format in ("parquet", "csv"):
            _merge_inventory_chunks(
                chunk_files=chunk_files,
                merged_path=inv_out / merge_file,
                delete_chunks=delete_chunks,
                compression=compression,
            )
            # Merge CSV chunks into fewer files so the packager and
            # SQL import see a handful of BULK INSERTs instead of N small ones.
            if file_format == "csv":
                _merge_csv_chunks(inv_out, chunk_size=csv_chunk_size, delete_chunks=delete_chunks)

    n_pairs = demand.groupby(["ProductKey", "WarehouseKey"]).ngroups
    return {
        "rows": total_rows,
        "product_warehouse_pairs": n_pairs,
        "stockout_pct": round(stockout_pct, 2),
        "chunks": completed,
    }


# ------------------------------------------------------------------
# Chunk merge helpers (used by parallel path)
# ------------------------------------------------------------------

def _add_year_month_to_table(table: pa.Table) -> pa.Table:
    """Derive Year and Month int columns from SnapshotDate for partitioning."""
    import pyarrow.compute as pc

    dates = table.column("SnapshotDate")
    year = pc.year(dates).cast(pa.int16())
    month = pc.month(dates).cast(pa.int8())

    table = table.append_column("Year", year)
    table = table.append_column("Month", month)
    return table


def _merge_inventory_chunks(
    chunk_files: list[Path],
    merged_path: Path,
    delete_chunks: bool = True,
    compression: str = "snappy",
) -> None:
    """Merge parallel inventory chunk parquets into one file.

    Uses direct PyArrow concat + single write instead of the generic
    per-row-group merge pipeline.  All chunks share an identical schema
    (produced by the same worker), so schema validation and projection
    are unnecessary.  This is ~5-10x faster for 20M+ row inventories.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not chunk_files:
        info(f"Inventory merge: no chunks for {short_path(merged_path)}")
        return

    info(f"Merging {len(chunk_files)} chunks: {merged_path.name}")

    # Read all chunks as Arrow tables and concatenate
    tables = []
    for f in sorted(chunk_files):
        tables.append(pq.read_table(str(f)))

    merged = pa.concat_tables(tables, promote_options="default")
    del tables  # free memory before write

    merged_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        merged,
        str(merged_path),
        compression=compression,
        row_group_size=1_000_000,
        use_dictionary=True,
        write_statistics=True,
    )
    del merged

    if delete_chunks:
        for f in chunk_files:
            try:
                f.unlink()
            except OSError:
                pass

    done(f"Merged chunks: {merged_path.name}")


def _merge_csv_chunks(
    inv_out: Path,
    chunk_size: int = 2_000_000,
    delete_chunks: bool = True,
) -> None:
    """Re-chunk inventory CSV files to respect the configured chunk_size.

    Reads all inventory_chunk_*.csv files and writes merged output files
    of up to *chunk_size* data rows each.  A single file is named
    ``inventory_snapshot.csv``; multiple files are named
    ``inventory_snapshot_00000.csv``, etc.
    """
    csv_chunks = sorted(inv_out.glob("inventory_chunk_*.csv"))
    if len(csv_chunks) <= 1:
        return

    # Read header from first chunk
    with open(csv_chunks[0], "r", encoding="utf-8") as f:
        header = f.readline()

    out_files: list[Path] = []
    out_f = None
    rows_in_current = 0
    file_idx = 0

    def _open_next():
        nonlocal out_f, rows_in_current, file_idx
        if out_f is not None:
            out_f.close()
        path = inv_out / f"inventory_snapshot_{file_idx:05d}.csv"
        out_files.append(path)
        out_f = open(path, "w", newline="", encoding="utf-8")
        out_f.write(header)
        rows_in_current = 0
        file_idx += 1

    try:
        _open_next()
        for chunk_path in csv_chunks:
            with open(chunk_path, "r", encoding="utf-8") as in_f:
                next(in_f, None)  # skip header
                for line in in_f:
                    if rows_in_current >= chunk_size:
                        _open_next()
                    out_f.write(line)
                    rows_in_current += 1
    finally:
        if out_f is not None:
            out_f.close()

    # If only one output file, rename to the non-numbered name
    if len(out_files) == 1:
        single = inv_out / "inventory_snapshot.csv"
        out_files[0].rename(single)
        out_files = [single]

    if delete_chunks:
        for f in csv_chunks:
            try:
                f.unlink()
            except OSError:
                pass

    info(f"Merged {len(csv_chunks)} CSV chunks into {len(out_files)} file(s)")


def _merge_chunks_to_delta(
    chunk_files: list[Path],
    inv_out: Path,
    partition_by: List[str],
    delete_chunks: bool = True,
) -> None:
    """Consolidate inventory chunk parquets into a partitioned Delta Lake table.

    Concatenates the parts and writes them in ONE overwrite commit. The previous
    overwrite-then-append-per-part loop paid Delta's full-state log replay on
    every part (O(N^2) in the number of parts) and scattered tiny files; a single
    commit produces one log entry and lets delta-rs size the output files.
    """
    if not chunk_files:
        return

    try:
        from deltalake import write_deltalake
    except ImportError:
        from deltalake.writer import write_deltalake

    delta_dir = inv_out.parent / "inventory_snapshot"
    delta_dir.mkdir(parents=True, exist_ok=True)

    needs_year_month = any(c in partition_by for c in ("Year", "Month"))

    tables = []
    for chunk_path in sorted(chunk_files):
        table = pq.read_table(str(chunk_path))
        if needs_year_month and "Year" not in table.column_names:
            table = _add_year_month_to_table(table)
        tables.append(table)

    merged = pa.concat_tables(tables, promote_options="default")
    del tables

    # Validate partition cols against actual schema
    pcols = [c for c in partition_by if c in merged.column_names]

    write_deltalake(
        str(delta_dir),
        merged,
        mode="overwrite",
        partition_by=pcols if pcols else None,
    )
    del merged

    info(f"[DELTA] Wrote {len(chunk_files)} parts (Arrow -> Delta, single commit) table=InventorySnapshot")

    if delete_chunks:
        for f in chunk_files:
            try:
                os.remove(f)
            except OSError:
                pass


# ------------------------------------------------------------------
# Single-file write helpers (used by single-process path)
# ------------------------------------------------------------------

def _write_inventory(
    df: pd.DataFrame,
    out_dir: Path,
    name: str,
    file_format: str,
    partition_by: Optional[List[str]] = None,
) -> None:
    """Write an inventory DataFrame in the requested format."""
    if file_format == "deltaparquet":
        # Inventory-specific: partition by Year/Month for Delta Lake
        table = pa.Table.from_pandas(df, preserve_index=False)
        table = _cast_snapshot_date(table)
        delta_dir = out_dir.parent / name
        delta_dir.mkdir(parents=True, exist_ok=True)
        try:
            from deltalake import write_deltalake
        except ImportError:
            from deltalake.writer import write_deltalake

        pcols = list(partition_by or [])
        needs_year_month = any(c in pcols for c in ("Year", "Month"))
        if needs_year_month and "Year" not in table.column_names:
            table = _add_year_month_to_table(table)
        pcols = [c for c in pcols if c in table.column_names]

        write_deltalake(
            str(delta_dir), table, mode="overwrite",
            partition_by=pcols if pcols else None,
        )
        info(
            f"Wrote {name}: {len(df):,} rows -> {short_path(delta_dir)}/"
            + (f" (partitioned by {pcols})" if pcols else "")
        )
        return

    if file_format == "csv":
        write_fact_table(df, out_dir, name, file_format,
                         csv_prep_fn=_prepare_inventory_csv)
    else:
        table = pa.Table.from_pandas(df, preserve_index=False)
        table = _cast_snapshot_date(table)
        write_fact_table(table, out_dir, name, file_format,
                         csv_prep_fn=_prepare_inventory_csv)
