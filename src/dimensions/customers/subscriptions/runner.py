"""run_subscriptions() — config parsing, versioning, path selection."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from src.defaults import SUBSCRIPTION_PARALLEL_THRESHOLD
from src.utils.logging_utils import info, skip, stage
from src.utils.output_utils import write_parquet_with_date32
from src.versioning.version_store import should_regenerate, save_version

from .helpers import SubscriptionsCfg, build_dim_plans, parse_global_dates, read_cfg
from .bridge_serial import write_bridge_streaming
from .bridge_parallel import write_bridge_parallel


def run_subscriptions(cfg: Any, parquet_folder: Path) -> Dict[str, Any]:
    parquet_folder = Path(parquet_folder)

    c = read_cfg(cfg)
    if not c.enabled:
        skip("Subscriptions disabled; skipping.")
        return {"_regenerated": False, "reason": "disabled"}

    out_dim = parquet_folder / "plans.parquet"
    out_bridge = parquet_folder / "customer_subscriptions.parquet"

    customers_fp = parquet_folder / "customers.parquet"
    if not customers_fp.exists():
        alt = parquet_folder / "Customers.parquet"
        if alt.exists():
            customers_fp = alt
        else:
            raise FileNotFoundError(
                f"Customers parquet not found at {parquet_folder}. "
                "Expected customers.parquet (or Customers.parquet)."
            )

    from src.utils.config_helpers import as_dict
    st = os.stat(customers_fp)
    version_cfg = as_dict(cfg.subscriptions)
    version_cfg["_schema_version"] = 5
    version_cfg["_upstream_customers_sig"] = {
        "path": str(customers_fp),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }

    version_files_exist = out_dim.exists() and (out_bridge.exists() or not c.generate_bridge)
    if version_files_exist and (not should_regenerate("subscriptions", version_cfg, out_dim)):
        if not c.generate_bridge and out_bridge.exists():
            out_bridge.unlink()
            info("Removed stale customer_subscriptions bridge file.")
        skip("Subscriptions up-to-date")
        return {"_regenerated": False, "reason": "version"}

    g_start, g_end = parse_global_dates(cfg)

    with stage("Generating Subscriptions"):
        dim = build_dim_plans(g_start)
        write_parquet_with_date32(dim, out_dim, date_cols=["LaunchDate"])
        info(f"Plans written: {out_dim.name} ({len(dim):,} rows)")

        n_rows = 0
        if c.generate_bridge:
            customers = pd.read_parquet(customers_fp)
            # Dedup SCD2 history: subscriptions are entity-level (one customer ->
            # 1..N plans over their lifetime), not per-version, so collapse to the
            # current row when SCD2 is active.
            if "IsCurrent" in customers.columns:
                _n_before = len(customers)
                customers = customers[customers["IsCurrent"] == 1].reset_index(drop=True)
                if len(customers) < _n_before:
                    info(f"Subscriptions SCD2: dedup {_n_before:,} -> {len(customers):,} "
                         f"rows (IsCurrent=1 pool)")
            if customers.empty:
                skip("No customers found — skipping subscription bridge")
                return {
                    "_regenerated": True,
                    "dim": str(out_dim),
                    "bridge": None,
                    "bridge_rows": 0,
                }

            n_cust = len(customers)
            estimated_eligible = int(n_cust * c.participation_rate * 0.9)

            workers: Optional[int] = None
            w_attr = getattr(cfg, "scale", None) or getattr(cfg, "defaults", None)
            if w_attr is not None:
                workers = int(getattr(w_attr, "workers", 0) or 0) or None

            if estimated_eligible >= SUBSCRIPTION_PARALLEL_THRESHOLD:
                info(f"Subscriptions: {n_cust:,} customers -> parallel path "
                     f"(estimated {estimated_eligible:,} eligible)")
                n_rows = write_bridge_parallel(
                    customers=customers,
                    dim_plans=dim,
                    c=c,
                    g_start=g_start,
                    g_end=g_end,
                    out_bridge=out_bridge,
                    workers=workers,
                )
            else:
                n_rows = write_bridge_streaming(
                    customers=customers,
                    dim_plans=dim,
                    c=c,
                    g_start=g_start,
                    g_end=g_end,
                    out_bridge=out_bridge,
                )
            save_version("subscriptions", version_cfg, out_bridge)
            info(f"Customer subscriptions written: {out_bridge.name} ({n_rows:,} rows)")
        else:
            skip("customer_subscriptions bridge skipped (generate_bridge: false)")
            if out_bridge.exists():
                out_bridge.unlink()

    return {
        "_regenerated": True,
        "dim": str(out_dim),
        "bridge": str(out_bridge) if c.generate_bridge else None,
        "bridge_rows": n_rows,
    }
