"""Wishlist pipeline runner — generates customer_wishlists.parquet using
accumulated (CustomerKey, ProductKey) purchase pairs from the sales pipeline.

Runs AFTER sales generation, using the WishlistAccumulator to create realistic
wishlist-to-purchase conversion rates.  A configurable fraction of each
customer's wishlist items are drawn from products they actually bought;
the remainder are selected via popularity-weighted subcategory affinity.

For large datasets (total_rows >= WISHLIST_PARALLEL_THRESHOLD), the per-customer
loop is distributed across multiple worker processes.  Each worker receives a
slice of participants plus shared read-only product data, writes a chunk parquet,
and the main process merges them.  Small datasets use the serial path unchanged.
"""
from __future__ import annotations

from multiprocessing import cpu_count
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.facts.wishlists.accumulator import WishlistAccumulator
from src.facts.wishlists.constants import (
    NS_PER_DAY,
    WishlistsCfg,
    bridge_schema,
    parse_global_dates,
    read_cfg,
)
from src.facts.wishlists.scd2 import build_scd2_price_lookup, resolve_scd2_prices
from src.facts.wishlists.selection import (
    build_subcategory_pool,
    generate_wishlist_items,
    pool_to_dict,
)
from src.utils.logging_utils import info, skip
from src.defaults import WISHLIST_PARALLEL_THRESHOLD, FACT_MAX_PARALLEL_CHUNKS


# ---------------------------------------------------------------------------
# Customer windows
# ---------------------------------------------------------------------------

def _compute_customer_windows(
    customers: pd.DataFrame,
    g_start: pd.Timestamp,
    g_end: pd.Timestamp,
    pre_browse_days: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    cust_keys = customers["CustomerKey"].astype(np.int64).to_numpy()
    order = np.argsort(cust_keys)
    cust_keys = cust_keys[order]

    g_start_ns = np.int64(g_start.value)
    g_end_ns = np.int64(g_end.value)
    browse_offset_ns = np.int64(pre_browse_days) * NS_PER_DAY

    if "CustomerStartDate" in customers.columns:
        start_vals = customers["CustomerStartDate"].to_numpy().astype("datetime64[ns]")
        start_ns = start_vals.view(np.int64).copy()
        start_ns[np.isnat(start_vals)] = g_start_ns
    else:
        start_ns = np.full(len(cust_keys), g_start_ns, dtype=np.int64)

    if "CustomerEndDate" in customers.columns:
        end_vals = customers["CustomerEndDate"].to_numpy().astype("datetime64[ns]")
        end_ns = end_vals.view(np.int64).copy()
        end_ns[np.isnat(end_vals)] = g_end_ns
    else:
        end_ns = np.full(len(cust_keys), g_end_ns, dtype=np.int64)

    start_ns = start_ns[order]
    end_ns = end_ns[order]

    earliest_ns = start_ns - browse_offset_ns
    floor_ns = g_start_ns - browse_offset_ns
    earliest_ns = np.maximum(earliest_ns, floor_ns)
    latest_ns = np.clip(end_ns, g_start_ns, g_end_ns)
    latest_ns = np.maximum(latest_ns, earliest_ns + NS_PER_DAY)

    return cust_keys, earliest_ns, latest_ns


# ---------------------------------------------------------------------------
# Product popularity weights
# ---------------------------------------------------------------------------

def _build_product_weights(
    products: pd.DataFrame,
    parquet_dims: Path,
) -> np.ndarray:
    n = len(products)
    prod_keys = products["ProductKey"].to_numpy().astype(np.int64)

    brand_weight_per_product = np.ones(n, dtype=np.float64)
    if "Brand" in products.columns:
        brands = products["Brand"].fillna("Unknown").astype(str).to_numpy()
        unique_brands, brand_codes = np.unique(brands, return_inverse=True)
        brand_counts = np.bincount(brand_codes).astype(np.float64)
        brand_base = np.sqrt(np.maximum(brand_counts, 1.0))
        brand_base /= brand_base.sum()
        brand_weight_per_product = brand_base[brand_codes]

    pop_scores = np.full(n, 50.0, dtype=np.float64)
    for name in ("product_profile.parquet", "ProductProfile.parquet"):
        profile_path = parquet_dims / name
        if profile_path.exists():
            try:
                pp_df = pd.read_parquet(profile_path, columns=["ProductKey", "PopularityScore"])
                pp_df = pp_df.drop_duplicates("ProductKey", keep="first")
                pop_map = pd.Series(
                    pp_df["PopularityScore"].to_numpy(dtype=np.float64),
                    index=pp_df["ProductKey"].to_numpy(dtype=np.int64),
                )
                pop_scores = pop_map.reindex(prod_keys).fillna(50.0).to_numpy(dtype=np.float64)
            except (KeyError, OSError):
                pass
            break

    weights = brand_weight_per_product * pop_scores
    total = weights.sum()
    if total > 0:
        weights /= total
    else:
        weights = np.ones(n, dtype=np.float64) / n
    return weights


# ---------------------------------------------------------------------------
# Purchased-pairs helpers
# ---------------------------------------------------------------------------

def _build_purchased_map(
    purchased_pairs: pd.DataFrame,
    prod_key_to_idx: pd.Series,
) -> Dict[int, List[int]]:
    """Build {customer_key: [product_indices]} from accumulated sales data."""
    cust_purchased: Dict[int, List[int]] = {}
    if len(purchased_pairs) == 0:
        return cust_purchased

    _pp_ckeys = purchased_pairs["CustomerKey"].to_numpy().astype(np.int64)
    _pp_pkeys = purchased_pairs["ProductKey"].to_numpy().astype(np.int64)
    _pp_idx_series = prod_key_to_idx.reindex(_pp_pkeys)
    _pp_valid = _pp_idx_series.notna()
    _pp_ckeys_valid = _pp_ckeys[_pp_valid.to_numpy()]
    _pp_idxs_valid = _pp_idx_series[_pp_valid].to_numpy(dtype=np.int64)
    if len(_pp_ckeys_valid) > 0:
        _sort_order = np.argsort(_pp_ckeys_valid)
        _sorted_ckeys = _pp_ckeys_valid[_sort_order]
        _sorted_idxs = _pp_idxs_valid[_sort_order]
        _uniq_ckeys, _split_pos = np.unique(_sorted_ckeys, return_index=True)
        _split_groups = np.split(_sorted_idxs, _split_pos[1:])
        for _ck, _idxs in zip(_uniq_ckeys, _split_groups):
            cust_purchased[int(_ck)] = _idxs.tolist()
    return cust_purchased


# ---------------------------------------------------------------------------
# Bridge writer (serial path)
# ---------------------------------------------------------------------------

def _write_bridge(
    customers: pd.DataFrame,
    products: pd.DataFrame,
    product_weights: np.ndarray,
    purchased_pairs: pd.DataFrame,
    c: WishlistsCfg,
    g_start: pd.Timestamp,
    g_end: pd.Timestamp,
    out_path: Path,
    scd2_lookup: Optional[Tuple[np.ndarray, np.ndarray]] = None,
) -> int:
    rng = np.random.default_rng(c.seed)
    schema = bridge_schema()

    cust_keys, earliest_ns, latest_ns = _compute_customer_windows(
        customers, g_start, g_end, c.pre_browse_days,
    )

    n_customers = len(cust_keys)
    n_participants = max(1, int(round(n_customers * c.participation_rate)))

    participant_idx = rng.choice(n_customers, size=n_participants, replace=False)
    participant_idx.sort()

    prod_keys = products["ProductKey"].to_numpy().astype(np.int64)
    prod_prices = products["ListPrice"].to_numpy().astype(np.float64)
    prod_subcat = products["SubcategoryKey"].to_numpy().astype(np.int64)
    n_products = len(prod_keys)

    prod_key_to_idx = pd.Series(np.arange(n_products), index=prod_keys)
    cust_purchased = _build_purchased_map(purchased_pairs, prod_key_to_idx)

    pool, global_cdf = build_subcategory_pool(prod_subcat, product_weights)

    items_per = rng.poisson(lam=c.avg_items, size=n_participants)
    items_per = np.clip(items_per, 1, min(c.max_items, n_products))
    total_rows = int(items_per.sum())

    # Run unified selection loop
    out_prod_idx, out_ckey, out_date_ns, out_priority, out_quantity = (
        generate_wishlist_items(
            rng,
            cust_keys=cust_keys[participant_idx],
            earliest_ns=earliest_ns[participant_idx],
            latest_ns=latest_ns[participant_idx],
            items_per=items_per,
            purchased_map=cust_purchased,
            prod_subcat=prod_subcat,
            n_products=n_products,
            global_cdf=global_cdf,
            pool=pool,
            conversion_rate=c.conversion_rate,
            affinity_strength=c.affinity_strength,
        )
    )

    # Build final output
    out_wkey = np.arange(1, total_rows + 1, dtype=np.int64)
    out_pkey = prod_keys[out_prod_idx]
    if scd2_lookup is not None:
        out_price = resolve_scd2_prices(
            out_prod_idx, out_date_ns, scd2_lookup[0], scd2_lookup[1],
        )
    else:
        out_price = prod_prices[out_prod_idx]
    out_dates_dt = out_date_ns.view("datetime64[ns]").astype("datetime64[ms]")

    writer = pq.ParquetWriter(str(out_path), schema)
    chunk = c.write_chunk_rows
    for start in range(0, total_rows, chunk):
        end = min(start + chunk, total_rows)
        batch = pa.record_batch(
            [
                pa.array(out_wkey[start:end], type=pa.int64()),
                pa.array(out_ckey[start:end], type=pa.int64()),
                pa.array(out_pkey[start:end], type=pa.int64()),
                pa.array(out_dates_dt[start:end], type=pa.date32()),
                pa.array(out_priority[start:end], type=pa.string()),
                pa.array(out_quantity[start:end], type=pa.int32()),
                pa.array(out_price[start:end], type=pa.float64()),
            ],
            schema=schema,
        )
        writer.write_batch(batch)
    writer.close()

    return total_rows


# ---------------------------------------------------------------------------
# Parallel path helpers
# ---------------------------------------------------------------------------

def _build_product_data_dict(
    prod_keys: np.ndarray,
    prod_prices: np.ndarray,
    prod_subcat: np.ndarray,
    product_weights: np.ndarray,
    scd2_lookup: Optional[Tuple[np.ndarray, np.ndarray]] = None,
) -> Dict[str, Any]:
    """Serialize product arrays + CDFs into a picklable dict for workers."""
    pool, global_cdf = build_subcategory_pool(prod_subcat, product_weights)
    pool_dict = pool_to_dict(pool)

    return {
        "prod_keys": prod_keys.tolist(),
        "prod_prices": prod_prices.tolist(),
        "prod_subcat": prod_subcat.tolist(),
        "global_cdf": global_cdf.tolist(),
        **pool_dict,
        "scd2_starts": scd2_lookup[0].tolist() if scd2_lookup is not None else None,
        "scd2_prices": scd2_lookup[1].tolist() if scd2_lookup is not None else None,
    }


def _partition_participants(
    participant_cust_keys: np.ndarray,
    participant_earliest_ns: np.ndarray,
    participant_latest_ns: np.ndarray,
    items_per: np.ndarray,
    n_chunks: int,
) -> List[Dict[str, Any]]:
    """Split participants evenly across n_chunks; each chunk gets its slice."""
    n = len(participant_cust_keys)
    cumulative_offsets = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(items_per, out=cumulative_offsets[1:])

    chunks = []
    splits = np.array_split(np.arange(n), n_chunks)
    for indices in splits:
        if len(indices) == 0:
            continue
        global_offset_start = int(cumulative_offsets[indices[0]])
        chunk_items = items_per[indices]
        chunks.append({
            "cust_keys": participant_cust_keys[indices].tolist(),
            "earliest_ns": participant_earliest_ns[indices].tolist(),
            "latest_ns": participant_latest_ns[indices].tolist(),
            "items_per": chunk_items.tolist(),
            "offsets": [global_offset_start],
        })
    return chunks


def _partition_purchased_pairs(
    purchased_pairs: pd.DataFrame,
    prod_key_to_idx: pd.Series,
    participant_chunks: List[Dict[str, Any]],
) -> List[Dict[int, List[int]]]:
    """For each chunk, build {customer_key: [product_indices]} filtered to that chunk's customers."""
    full_map = _build_purchased_map(purchased_pairs, prod_key_to_idx)
    chunk_maps: List[Dict[int, List[int]]] = []
    for chunk in participant_chunks:
        ck_set = set(chunk["cust_keys"])
        chunk_maps.append({ck: v for ck, v in full_map.items() if ck in ck_set})
    return chunk_maps


def _merge_chunk_parquets(
    chunk_paths: List[Path],
    out_path: Path,
    schema: pa.Schema,
    delete_chunks: bool = True,
) -> None:
    """Concatenate chunk parquets in order into a single output parquet."""
    writer = pq.ParquetWriter(str(out_path), schema)
    for cp in sorted(chunk_paths):
        tbl = pq.read_table(str(cp))
        writer.write_table(tbl)
    writer.close()
    if delete_chunks:
        for cp in chunk_paths:
            try:
                cp.unlink()
            except OSError:
                pass
        if chunk_paths:
            try:
                chunk_paths[0].parent.rmdir()
            except OSError:
                pass


def _run_parallel(
    customers: pd.DataFrame,
    products: pd.DataFrame,
    product_weights: np.ndarray,
    purchased_pairs: pd.DataFrame,
    c: WishlistsCfg,
    g_start: pd.Timestamp,
    g_end: pd.Timestamp,
    out_path: Path,
    workers: Optional[int] = None,
    scd2_lookup: Optional[Tuple[np.ndarray, np.ndarray]] = None,
) -> int:
    """Parallel wishlist generation: distribute customers across worker pool."""
    from src.utils.pool import PoolRunSpec, iter_imap_unordered
    from src.facts.wishlists.worker import _wishlist_worker_task

    n_cpus = max(1, cpu_count() - 1)
    if workers is not None and workers >= 1:
        n_cpus = min(n_cpus, workers)

    rng = np.random.default_rng(c.seed)

    cust_keys, earliest_ns, latest_ns = _compute_customer_windows(
        customers, g_start, g_end, c.pre_browse_days,
    )
    n_customers = len(cust_keys)
    n_participants = max(1, int(round(n_customers * c.participation_rate)))

    participant_idx = rng.choice(n_customers, size=n_participants, replace=False)
    participant_idx.sort()

    participant_cust_keys = cust_keys[participant_idx]
    participant_earliest_ns = earliest_ns[participant_idx]
    participant_latest_ns = latest_ns[participant_idx]

    prod_keys = products["ProductKey"].to_numpy().astype(np.int64)
    prod_prices = products["ListPrice"].to_numpy().astype(np.float64)
    prod_subcat = products["SubcategoryKey"].to_numpy().astype(np.int64)
    n_products = len(prod_keys)

    items_per = rng.poisson(lam=c.avg_items, size=n_participants)
    items_per = np.clip(items_per, 1, min(c.max_items, n_products))
    total_rows = int(items_per.sum())

    # Chunk COUNT depends only on the data (not the worker count) so per-chunk
    # SeedSequence streams and WishlistKey offsets are identical across machines.
    # The pool SIZE still adapts to the available workers.
    n_chunks = max(2, min(n_participants, FACT_MAX_PARALLEL_CHUNKS))
    n_workers = min(n_chunks, n_cpus)

    info(f"Wishlists parallel: {n_chunks} chunks across {n_workers} workers "
         f"({n_participants:,} participants, ~{total_rows:,} rows)")

    product_data = _build_product_data_dict(prod_keys, prod_prices, prod_subcat, product_weights, scd2_lookup)

    prod_key_to_idx = pd.Series(np.arange(n_products), index=prod_keys)

    participant_chunks = _partition_participants(
        participant_cust_keys,
        participant_earliest_ns,
        participant_latest_ns,
        items_per,
        n_chunks,
    )
    n_chunks = len(participant_chunks)

    chunk_purchased_maps = _partition_purchased_pairs(
        purchased_pairs, prod_key_to_idx, participant_chunks,
    )

    config_scalars = {
        "conversion_rate": c.conversion_rate,
        "affinity_strength": c.affinity_strength,
        "avg_items": c.avg_items,
        "max_items": c.max_items,
        "write_chunk_rows": c.write_chunk_rows,
    }

    chunk_dir = out_path.parent / "_wishlist_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    tasks = []
    for idx, (pdata, pmap) in enumerate(zip(participant_chunks, chunk_purchased_maps)):
        chunk_out = str(chunk_dir / f"wishlist_chunk_{idx:05d}.parquet")
        tasks.append((
            idx,
            c.seed,
            n_chunks,
            pdata,
            product_data,
            pmap,
            config_scalars,
            chunk_out,
        ))

    pool_spec = PoolRunSpec(
        processes=n_workers,
        label="wishlists",
    )

    total_written = 0
    completed = 0
    for result in iter_imap_unordered(tasks=tasks, task_fn=_wishlist_worker_task, spec=pool_spec):
        completed += 1
        total_written += result["rows"]

    info(f"Wishlists: {completed}/{n_chunks} chunks done ({total_written:,} rows)")

    chunk_files = sorted(chunk_dir.glob("wishlist_chunk_*.parquet"))
    if chunk_files:
        _merge_chunk_parquets(chunk_files, out_path, bridge_schema(), delete_chunks=True)

    return total_written


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run_wishlist_pipeline(
    *,
    accumulator: WishlistAccumulator,
    parquet_dims: Path,
    fact_out: Path,
    cfg: Any,
    file_format: str = "parquet",
) -> Optional[Dict[str, Any]]:
    """Generate customer_wishlists into the facts output folder."""
    c = read_cfg(cfg)
    if not c.enabled:
        return None

    if not accumulator.has_data:
        skip("Wishlists: no sales data accumulated; skipping.")
        return None

    g_start, g_end = parse_global_dates(cfg)

    # Load dimension tables
    customers_fp = _find_parquet(parquet_dims, "customers")
    products_fp = _find_parquet(parquet_dims, "products")

    _cust_cols = ["CustomerKey", "CustomerStartDate", "CustomerEndDate"]
    _cust_schema_names = set(pq.read_schema(str(customers_fp)).names)
    if "IsCurrent" in _cust_schema_names:
        _cust_cols.append("IsCurrent")
    customers = pd.read_parquet(customers_fp, columns=_cust_cols)
    if "IsCurrent" in customers.columns:
        customers = customers[customers["IsCurrent"] == 1].copy()
        customers = customers.drop(columns=["IsCurrent"])
    _prod_cols = ["ProductKey", "ListPrice", "SubcategoryKey", "Brand"]
    _prod_schema_names = set(pq.read_schema(str(products_fp)).names) if hasattr(pq, "read_schema") else set()
    _has_scd2 = "EffectiveStartDate" in _prod_schema_names
    if _has_scd2:
        _prod_cols.append("EffectiveStartDate")
        # ProductID is the SCD2 version-family key (WISH-2); needed to resolve
        # historical wishlist prices since ProductKey is unique per version.
        if "ProductID" in _prod_schema_names:
            _prod_cols.append("ProductID")
    if "IsCurrent" in _prod_schema_names:
        _prod_cols.append("IsCurrent")
    all_products = pd.read_parquet(products_fp, columns=_prod_cols)
    if "IsCurrent" in all_products.columns:
        products = all_products[all_products["IsCurrent"] == 1].copy()
        products = products.drop(columns=["EffectiveStartDate", "IsCurrent"] if _has_scd2 else ["IsCurrent"])
    else:
        products = all_products

    product_weights = _build_product_weights(products, parquet_dims)
    purchased_pairs = accumulator.finalize()

    # Build SCD2 price lookup (None if SCD2 not active). Keyed by ProductID, aligned
    # with the current-product row order so resolve_scd2_prices' prod_idx matches.
    prod_prices_arr = products["ListPrice"].to_numpy().astype(np.float64)
    prod_ids_arr = (
        products["ProductID"].to_numpy().astype(np.int64)
        if "ProductID" in products.columns else None
    )
    scd2_lookup = build_scd2_price_lookup(all_products, prod_ids_arr, prod_prices_arr)

    wishlists_dir = fact_out / "customer_wishlists"
    wishlists_dir.mkdir(parents=True, exist_ok=True)
    pq_path = wishlists_dir / "customer_wishlists.parquet"

    # Estimate total rows to decide serial vs parallel
    _rng_est = np.random.default_rng(c.seed)
    _n_custs = len(customers)
    _n_participants_est = max(1, int(round(_n_custs * c.participation_rate)))
    _items_est = _rng_est.poisson(lam=c.avg_items, size=_n_participants_est)
    _items_est = np.clip(_items_est, 1, min(c.max_items, len(products)))
    _total_rows_est = int(_items_est.sum())

    _workers: Optional[int] = None
    _sales_cfg = getattr(cfg, "sales", None)
    if _sales_cfg is not None:
        _workers = getattr(_sales_cfg, "workers", None)

    if _total_rows_est >= WISHLIST_PARALLEL_THRESHOLD:
        n_rows = _run_parallel(
            customers=customers,
            products=products,
            product_weights=product_weights,
            purchased_pairs=purchased_pairs,
            c=c,
            g_start=g_start,
            g_end=g_end,
            out_path=pq_path,
            workers=_workers,
            scd2_lookup=scd2_lookup,
        )
    else:
        n_rows = _write_bridge(
            customers=customers,
            products=products,
            product_weights=product_weights,
            purchased_pairs=purchased_pairs,
            c=c,
            g_start=g_start,
            g_end=g_end,
            out_path=pq_path,
            scd2_lookup=scd2_lookup,
        )

    # Format-specific post-processing
    if file_format == "csv" and n_rows > 0:
        _csv_chunk = int(getattr(_sales_cfg, "chunk_size", 0)) if _sales_cfg else 0
        df = pq.read_table(str(pq_path)).to_pandas()
        pq_path.unlink()

        # Force LF: the generated BULK INSERT uses ROWTERMINATOR='0x0a', but pandas
        # to_csv defaults to CRLF on Windows (trailing \r breaks the last column).
        if _csv_chunk and _csv_chunk > 0 and n_rows > _csv_chunk:
            n_files = 0
            for start in range(0, n_rows, _csv_chunk):
                chunk_path = wishlists_dir / f"customer_wishlists_{n_files:05d}.csv"
                df.iloc[start:start + _csv_chunk].to_csv(str(chunk_path), index=False, lineterminator="\n")
                n_files += 1
        else:
            df.to_csv(str(wishlists_dir / "customer_wishlists.csv"), index=False, lineterminator="\n")

    elif file_format == "deltaparquet" and n_rows > 0:
        table = pq.read_table(str(pq_path))
        try:
            from deltalake import write_deltalake
        except ImportError:
            from deltalake.writer import write_deltalake
        write_deltalake(str(wishlists_dir), table, mode="overwrite")
        pq_path.unlink()

    info(f"Customer wishlists: {n_rows:,} rows")

    return {
        "wishlists": str(wishlists_dir),
        "wishlists_rows": n_rows,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_parquet(folder: Path, name: str) -> Path:
    for variant in (f"{name}.parquet", f"{name.title()}.parquet"):
        p = folder / variant
        if p.exists():
            return p
    raise FileNotFoundError(f"{name}.parquet not found in {folder}")
