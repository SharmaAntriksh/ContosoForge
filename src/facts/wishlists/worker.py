"""Wishlist multiprocessing worker.

Each worker receives a slice of participant customers plus shared read-only
product data, generates wishlist rows for those customers, and writes a
chunk parquet file.  Returns lightweight stats — no large arrays cross the
IPC boundary on return.

The worker is a top-level function so it is importable / pickleable on
Windows (spawn start method).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq

from src.facts.wishlists.constants import bridge_schema
from src.facts.wishlists.selection import (
    generate_wishlist_items,
    subcategory_pool_from_dict,
)


def _wishlist_worker_task(args: Tuple) -> Dict[str, Any]:
    """
    Worker entry point — must be a top-level function for Windows spawn.

    Args (tuple):
        chunk_idx       : int — chunk sequence number
        seed            : int — base seed from config
        n_chunks        : int — total number of chunks (for SeedSequence.spawn)
        participant_data: dict with keys:
            cust_keys    : list[int]
            earliest_ns  : list[int]
            latest_ns    : list[int]
            items_per    : list[int]
            offsets      : list[int]  — length 1, global row start for this chunk
        product_data    : dict with keys:
            prod_keys    : list[int]
            prod_prices  : list[float]
            prod_subcat  : list[int]
            global_cdf   : list[float]
            subcat_keys  : list[int]
            subcat_starts: list[int]
            subcat_ends  : list[int]
            pool_indices : list[int]
            subcat_cdf_starts: list[int]
            subcat_cdf_ends  : list[int]
            cdf_data     : list[float]
        purchased_pairs : dict[int, list[int]]  customer_key -> list of product indices
        config_scalars  : dict with conversion_rate, affinity_strength, avg_items,
                          max_items, write_chunk_rows
        out_path        : str — path to write chunk parquet

    Returns:
        dict with chunk_idx, rows, wkey_start (first WishlistKey in this chunk)
    """
    if len(args) == 9:
        (
            chunk_idx,
            seed,
            n_chunks,
            participant_data,
            product_data,
            purchased_pairs,
            config_scalars,
            out_path,
            file_format,
        ) = args
    else:
        (
            chunk_idx,
            seed,
            n_chunks,
            participant_data,
            product_data,
            purchased_pairs,
            config_scalars,
            out_path,
        ) = args
        file_format = "parquet"

    # --- Derive per-chunk RNG via SeedSequence for reproducible independence ---
    ss = np.random.SeedSequence(seed)
    child_seeds = ss.spawn(n_chunks)
    rng = np.random.default_rng(child_seeds[chunk_idx])

    # --- Unpack participant data ---
    cust_keys = np.asarray(participant_data["cust_keys"], dtype=np.int64)
    earliest_ns = np.asarray(participant_data["earliest_ns"], dtype=np.int64)
    latest_ns = np.asarray(participant_data["latest_ns"], dtype=np.int64)
    items_per = np.asarray(participant_data["items_per"], dtype=np.int64)

    n_participants = len(cust_keys)
    total_rows = int(items_per.sum()) if n_participants > 0 else 0

    # --- Unpack product data ---
    prod_keys = np.asarray(product_data["prod_keys"], dtype=np.int64)
    prod_prices = np.asarray(product_data["prod_prices"], dtype=np.float64)
    prod_subcat = np.asarray(product_data["prod_subcat"], dtype=np.int64)
    global_cdf = np.asarray(product_data["global_cdf"], dtype=np.float64)
    n_products = len(prod_keys)

    pool = subcategory_pool_from_dict(product_data)

    wkey_start = int(participant_data["offsets"][0]) + 1  # 1-based global WishlistKey offset

    if total_rows == 0 or n_participants == 0:
        return {"chunk_idx": chunk_idx, "rows": 0, "wkey_start": wkey_start}

    # --- Run unified selection loop ---
    out_prod_idx, out_ckey, out_date_ns, out_priority, out_quantity = (
        generate_wishlist_items(
            rng,
            cust_keys=cust_keys,
            earliest_ns=earliest_ns,
            latest_ns=latest_ns,
            items_per=items_per,
            purchased_map=purchased_pairs,
            prod_subcat=prod_subcat,
            n_products=n_products,
            global_cdf=global_cdf,
            pool=pool,
            conversion_rate=float(config_scalars["conversion_rate"]),
            affinity_strength=float(config_scalars["affinity_strength"]),
            pre_browse_days=int(config_scalars["pre_browse_days"]),
        )
    )

    # --- Build output arrays ---
    global_row_start = int(participant_data["offsets"][0])
    out_wkey = np.arange(global_row_start + 1, global_row_start + total_rows + 1, dtype=np.int64)
    _product_start_ns_raw = product_data.get("product_start_ns")
    if _product_start_ns_raw is not None:
        out_date_ns = np.maximum(
            out_date_ns,
            np.asarray(_product_start_ns_raw, dtype=np.int64)[out_prod_idx],
        )
    out_pkey = prod_keys[out_prod_idx]

    # Resolve prices: SCD2 version lookup if available, otherwise current ListPrice
    _scd2_starts_raw = product_data.get("scd2_starts")
    _scd2_prices_raw = product_data.get("scd2_prices")
    _scd2_keys_raw = product_data.get("scd2_keys")
    if _scd2_starts_raw is not None and _scd2_prices_raw is not None:
        from src.facts.wishlists.scd2 import (
            resolve_scd2_prices,
            resolve_scd2_product_keys,
        )
        _scd2_starts = np.asarray(_scd2_starts_raw, dtype=np.int64)
        _scd2_prices = np.asarray(_scd2_prices_raw, dtype=np.float64)
        out_price = resolve_scd2_prices(
            out_prod_idx, out_date_ns, _scd2_starts, _scd2_prices,
        )
        if _scd2_keys_raw is not None:
            out_pkey = resolve_scd2_product_keys(
                out_prod_idx,
                out_date_ns,
                _scd2_starts,
                np.asarray(_scd2_keys_raw, dtype=np.int64),
            )
    else:
        out_price = prod_prices[out_prod_idx]

    out_dates_dt = out_date_ns.view("datetime64[ns]").astype("datetime64[ms]")

    # --- Write chunk parquet ---
    schema = bridge_schema()
    write_chunk_rows = int(config_scalars.get("write_chunk_rows", 250_000))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    if file_format == "csv":
        writer = pa_csv.CSVWriter(
            out_path,
            schema,
            write_options=pa_csv.WriteOptions(
                include_header=True, quoting_style="needed"
            ),
        )
    else:
        writer = pq.ParquetWriter(out_path, schema)
    chunk = write_chunk_rows
    for s in range(0, total_rows, chunk):
        e = min(s + chunk, total_rows)
        batch = pa.record_batch(
            [
                pa.array(out_wkey[s:e], type=pa.int64()),
                pa.array(out_ckey[s:e], type=pa.int64()),
                pa.array(out_pkey[s:e], type=pa.int64()),
                pa.array(out_dates_dt[s:e], type=pa.date32()),
                pa.array(out_priority[s:e], type=pa.string()),
                pa.array(out_quantity[s:e], type=pa.int32()),
                pa.array(out_price[s:e], type=pa.float64()),
            ],
            schema=schema,
        )
        writer.write_batch(batch)
    writer.close()

    return {
        "chunk_idx": chunk_idx,
        "rows": total_rows,
        "wkey_start": wkey_start,
    }
