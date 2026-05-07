"""Customer dimension multiprocessing workers.

Top-level functions for Windows spawn pickling.  Each worker generates
demographics/profile columns for a contiguous chunk of CustomerKeys,
skipping household assignment and SCD2 (done after merge in main process).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from src.utils.output_utils import write_parquet_with_date32


def customer_chunk_worker(args: Tuple) -> Dict[str, Any]:
    """Generate all demographic + profile columns for a chunk of customers.

    Calls generate_synthetic_customers with _skip_post_phases=True, writes
    chunk parquets to disk, and returns household-relevant arrays via IPC.
    """
    (
        chunk_idx, chunk_n, seed, n_chunks,
        cfg_dump, parquet_dims_str, output_base,
    ) = args

    # Chunk-local RNG seed via SeedSequence
    chunk_seed_val = int(np.random.SeedSequence(seed).spawn(n_chunks)[chunk_idx].entropy)

    # Reconstruct config with chunk-specific overrides
    from src.engine.config.config_schema import AppConfig

    cfg_dict = dict(cfg_dump)
    # Override customer count and seed for this chunk
    if "customers" not in cfg_dict:
        cfg_dict["customers"] = {}
    cfg_dict["customers"] = dict(cfg_dict["customers"])
    cfg_dict["customers"]["total_customers"] = chunk_n
    # Override the default seed so this chunk gets unique RNG
    if "defaults" not in cfg_dict:
        cfg_dict["defaults"] = {}
    cfg_dict["defaults"] = dict(cfg_dict["defaults"])
    cfg_dict["defaults"]["seed"] = chunk_seed_val

    cfg = AppConfig.from_raw_dict(cfg_dict)

    parquet_dims = Path(parquet_dims_str)

    from src.dimensions.customers.generator import generate_synthetic_customers

    customers_df, profile_df, _org, _active = generate_synthetic_customers(
        cfg, parquet_dims, _skip_post_phases=True,
    )

    # Reassign CustomerKey based on chunk position (will be fixed up after merge)
    # Workers generate keys 1..chunk_n; orchestrator remaps after concat

    # Write chunk DataFrames to disk (avoid large IPC serialization)
    write_parquet_with_date32(customers_df, f"{output_base}_customers.parquet", cast_all_datetime=True)
    write_parquet_with_date32(profile_df, f"{output_base}_profile.parquet", cast_all_datetime=True)

    return {
        "chunk_idx": chunk_idx,
        "n_customers": chunk_n,
    }


def scd2_chunk_worker(args: Tuple) -> Dict[str, Any]:
    """Run SCD2 life-event expansion on a partition of pre-selected customers."""
    (
        chunk_idx, n_scd2_chunks, seed,
        changed_records, col_names,
        max_versions,
        geo_keys_list, tier_keys_list,
        end_date_str, geo_lookup_dict,
        output_path,
        region_pools,
    ) = args

    chunk_rng = np.random.default_rng(
        np.random.SeedSequence(seed + 9999).spawn(n_scd2_chunks)[chunk_idx]
    )

    changed_df = pd.DataFrame(changed_records, columns=col_names)
    geo_keys = np.array(geo_keys_list, dtype="int64")
    tier_keys = np.array(tier_keys_list, dtype="int64")
    end_date = pd.Timestamp(end_date_str)

    from src.dimensions.customers.scd2 import expand_changed_customers

    expanded_df = expand_changed_customers(
        rng=chunk_rng,
        changed_df=changed_df,
        max_versions=max_versions,
        geo_keys=geo_keys,
        tier_keys=tier_keys,
        end_date=end_date,
        geo_lookup=geo_lookup_dict,
        region_pools=region_pools,
    )

    write_parquet_with_date32(expanded_df, output_path, cast_all_datetime=True)
    return {"chunk_idx": chunk_idx, "rows": len(expanded_df)}
