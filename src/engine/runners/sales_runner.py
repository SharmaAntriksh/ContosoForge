"""Sales fact pipeline coordinator.

Resolves config, loads dimension data, binds worker state, and
orchestrates the multi-step sales → budget → inventory → packaging
pipeline.
"""
from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from src.exceptions import ConfigError, SalesError
from src.utils.logging_utils import stage, info
from src.engine.packaging import package_output
from src.engine.powerbi_packaging import maybe_attach_pbip_project
from src.facts.sales.sales import generate_sales_fact
from src.facts.sales.sales_logic import bind_globals, State


# =========================================================
# Helpers
# =========================================================

def _normalize_file_format(sales_cfg) -> str:
    fmt = str(sales_cfg.file_format).strip().lower()
    if fmt not in {"csv", "parquet", "deltaparquet"}:
        raise ConfigError("sales.file_format must be one of: csv | parquet | deltaparquet")
    return fmt


def _resolve_sales_out_folder(fact_out: Path, fmt: str, *, merge_enabled: bool = False) -> Path:
    # When merge is enabled for parquet, write directly under fact_out
    # (no extra subfolder).  Chunks-only runs keep the parquet/ subfolder.
    if fmt == "csv":
        return fact_out / "csv"
    if fmt == "parquet":
        return fact_out if merge_enabled else fact_out / "parquet"
    return fact_out / "sales"  # deltaparquet


def _safe_clean_folder(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


def _compute_returns_effective(cfg, sales_cfg) -> Tuple[Any, bool]:
    """
    Apply your rule:
      - returns.enabled can be requested at top-level cfg
      - but if sales_output == 'sales' AND skip_order_cols == True -> warn and skip returns (do not fail)
    Returns (cfg_for_run, returns_enabled_effective)
    """
    returns_cfg = cfg.returns if hasattr(cfg, "returns") else None
    requested = bool(getattr(returns_cfg, "enabled", False)) if returns_cfg else False

    sales_output = str(sales_cfg.sales_output).strip().lower()
    skip_order_cols = bool(sales_cfg.skip_order_cols)

    effective = requested
    if requested and sales_output == "sales" and skip_order_cols:
        info("Disabling returns: skip_order_cols removes order IDs needed by returns")
        effective = False

    # If we changed effective value, deep-copy cfg and override returns.enabled
    if effective != requested:
        cfg_for_run = cfg.model_copy(deep=True) if hasattr(cfg, "model_copy") else dict(cfg)
        if hasattr(cfg_for_run, "returns") and cfg_for_run.returns is not None:
            cfg_for_run.returns.enabled = effective
        return cfg_for_run, effective

    return cfg, effective


def _run_coverage_preflight(cfg, parquet_dims: Path) -> None:
    """Validate salesperson coverage before the sales stage (see coverage_preflight)."""
    from src.facts.sales.prep.coverage_preflight import run_coverage_preflight

    defaults = getattr(cfg, "defaults", None)
    dates = getattr(defaults, "dates", None) if defaults is not None else None
    start = getattr(dates, "start", None) if dates is not None else None
    end = getattr(dates, "end", None) if dates is not None else None
    if start is None or end is None:
        return  # dates required elsewhere; nothing to validate without a window
    run_coverage_preflight(cfg, parquet_dims, pd.Timestamp(start), pd.Timestamp(end))


def _load_active_products(parquet_dims: Path, *, active_ratio: float = 1.0, seed: int = 42) -> np.ndarray:
    """
    Loads active products pool: (ProductKey, ListPrice, UnitCost)

    When Products has SCD2 versions, only IsCurrent=1 rows are used for the
    sampling pool.  ``active_ratio`` filters by unique ProductID so that all
    versions of a product are either fully included or fully excluded.

    Raises clear errors if file/columns missing or no active products.
    """
    products_path = parquet_dims / "products.parquet"
    if not products_path.exists():
        raise SalesError(f"Missing products parquet: {products_path}")

    wanted_cols = ["ProductKey", "ListPrice", "UnitCost"]
    scd2_cols = ["ProductID", "IsCurrent"]
    try:
        products_df = pd.read_parquet(products_path, columns=wanted_cols + scd2_cols)
    except (KeyError, ValueError, OSError):
        # Fallback: SCD2 columns may not exist in older outputs
        try:
            products_df = pd.read_parquet(products_path, columns=wanted_cols)
        except (KeyError, ValueError, OSError) as e:
            raise SalesError(
                f"Failed reading {products_path} with columns {wanted_cols}. "
                f"Underlying error: {type(e).__name__}: {e}"
            ) from e

    missing = [c for c in wanted_cols if c not in products_df.columns]
    if missing:
        raise SalesError(f"products.parquet missing required columns: {missing}. Found: {list(products_df.columns)}")

    has_scd2 = (
        "IsCurrent" in products_df.columns
        and "ProductID" in products_df.columns
        and (products_df["IsCurrent"] == 0).any()
    )

    if has_scd2:
        # SCD2 mode: filter active_ratio by unique ProductID, then keep IsCurrent=1
        current_df = products_df[products_df["IsCurrent"] == 1].copy()

        if 0 < active_ratio < 1.0:
            unique_ids = current_df["ProductID"].unique()
            n_active = max(1, int(len(unique_ids) * float(active_ratio)))
            rng = np.random.default_rng(seed)
            active_ids = set(rng.choice(unique_ids, size=n_active, replace=False).tolist())
            current_df = current_df[current_df["ProductID"].isin(active_ids)]

        if current_df.empty:
            raise SalesError("No active products found for sales generation")

        return current_df[wanted_cols].to_numpy()
    else:
        # No SCD2 — filter IsCurrent if column exists, then apply active_ratio
        if "IsCurrent" in products_df.columns:
            products_df = products_df[products_df["IsCurrent"] == 1].copy()

        if 0 < active_ratio < 1.0:
            n_total = len(products_df)
            n_active = max(1, int(n_total * float(active_ratio)))
            rng = np.random.default_rng(seed)
            active_idx = rng.choice(n_total, size=n_active, replace=False)
            active_idx.sort()
            products_df = products_df.iloc[active_idx]

        if products_df.empty:
            raise SalesError("No active products found for sales generation")

        return products_df[wanted_cols].to_numpy()


def _bind_runner_globals(skip_order_cols: bool, active_product_np: Any) -> None:
    """
    Bind ONLY runner-level globals (keep your contract).
    Guard State.models_cfg access.
    """
    models_cfg = getattr(State, "models_cfg", None)
    bind_globals(
        {
            "skip_order_cols": skip_order_cols,
            "active_product_np": active_product_np,
            "models_cfg": models_cfg,
        }
    )


def _normalize_partitioning(sales_cfg) -> Tuple[bool, Optional[list]]:
    partition_enabled = bool(sales_cfg.partition_enabled)
    if not partition_enabled:
        return False, None

    partition_cols = sales_cfg.partition_cols
    if partition_cols is None:
        partition_cols = ["Year", "Month"]
    return True, partition_cols


def _run_step(label: str, fn) -> None:
    with stage(label):
        fn()


# =========================================================
# Pipeline Context
# =========================================================

@dataclass
class SalesRunContext:
    sales_cfg: Dict[str, Any]
    cfg: Dict[str, Any]
    fact_out: Path
    parquet_dims: Path

    fmt: str
    sales_out_folder: Path
    cfg_for_run: Dict[str, Any]
    returns_enabled_effective: bool
    skip_order_cols: bool
    active_product_np: Any
    partition_enabled: bool
    partition_cols: Optional[list]


# =========================================================
# Public Runner
# =========================================================

def run_sales_pipeline(sales_cfg, fact_out, parquet_dims, cfg, *, force_regenerate: bool = True, report: bool = False):
    """
    Run the sales fact pipeline.

    Contract (preserved from current runner):
    - Customer lifecycle is fully driven by customers.parquet; this runner does NOT filter customers.
    - Product active_ratio filtering is applied here at load time (no longer stored in dimension).
    - CSV / Delta outputs are regenerated per run; Parquet output folder is preserved (not pre-deleted).
    """

    # --- Resolve paths
    fact_out_p = Path(fact_out).resolve()
    parquet_dims_p = Path(parquet_dims).resolve()
    fact_out_p.mkdir(parents=True, exist_ok=True)

    fmt = _normalize_file_format(sales_cfg)
    merge_enabled = bool(sales_cfg.merge_parquet)
    sales_out_folder = _resolve_sales_out_folder(fact_out_p, fmt, merge_enabled=merge_enabled)

    skip_order_cols = bool(sales_cfg.skip_order_cols)

    if force_regenerate:
        info("Sales will regenerate (forced).")

    # --- Clean outputs where safe (preserve your rule)
    # CSV and Delta must be regenerated every run
    # Parquet must NOT be deleted before packaging (keep existing behavior)
    if fmt != "parquet" and force_regenerate:
        _safe_clean_folder(sales_out_folder)

    sales_out_folder.mkdir(parents=True, exist_ok=True)

    # --- Returns effective config for THIS run
    cfg_for_run, returns_enabled_effective = _compute_returns_effective(cfg, sales_cfg)

    # --- Salesperson-coverage pre-flight (cheap; before the expensive sales stage)
    # Detects (store, month) cells with no salesperson — the only way sales emits
    # EmployeeKey=-1 — and applies sales.coverage_policy (abort | skip | repair).
    _run_coverage_preflight(cfg_for_run, parquet_dims_p)

    # --- Load active products pool (active_ratio filtering moved here from dimension)
    prod_cfg = getattr(cfg, "products", None)
    _active_ratio = float(getattr(prod_cfg, "active_ratio", 1.0)) if prod_cfg else 1.0
    # Resolve via the same precedence chain the product dimension uses. In
    # deterministic mode this matches the seed products were built with; in
    # random mode each resolve_seed call draws independently, which is fine —
    # the active-product subset is sampled fresh and doesn't depend on the
    # build seed.
    from src.utils.config_precedence import resolve_seed
    _prod_seed = resolve_seed(cfg, prod_cfg, fallback=42)
    active_product_np = _load_active_products(parquet_dims_p, active_ratio=_active_ratio, seed=_prod_seed)

    # --- Bind runner-level globals
    _bind_runner_globals(skip_order_cols=skip_order_cols, active_product_np=active_product_np)

    # --- Partitioning normalization
    partition_enabled, partition_cols = _normalize_partitioning(sales_cfg)

    ctx = SalesRunContext(
        sales_cfg=sales_cfg,
        cfg=cfg,
        fact_out=fact_out_p,
        parquet_dims=parquet_dims_p,
        fmt=fmt,
        sales_out_folder=sales_out_folder,
        cfg_for_run=cfg_for_run,
        returns_enabled_effective=returns_enabled_effective,
        skip_order_cols=skip_order_cols,
        active_product_np=active_product_np,
        partition_enabled=partition_enabled,
        partition_cols=partition_cols,
    )

    # --- Step 1: Generate sales
    budget_acc = None
    inventory_acc = None
    wishlists_acc = None
    complaints_acc = None
    order_id_int64 = False

    def _do_generate():
        nonlocal budget_acc
        nonlocal inventory_acc
        nonlocal wishlists_acc
        nonlocal complaints_acc
        nonlocal order_id_int64
        result = generate_sales_fact(
            ctx.cfg_for_run,
            parquet_folder=str(ctx.parquet_dims),
            out_folder=str(ctx.sales_out_folder),
            total_rows=ctx.sales_cfg.total_rows,
            file_format=ctx.sales_cfg.file_format,
            # Parquet merge options
            merge_parquet=ctx.sales_cfg.merge_parquet,
            merged_file=ctx.sales_cfg.merged_file,
            # Performance / execution
            row_group_size=ctx.sales_cfg.row_group_size,
            compression=ctx.sales_cfg.compression,
            chunk_size=ctx.sales_cfg.chunk_size,
            workers=ctx.sales_cfg.workers,
            # Partitioning / delta
            partition_enabled=ctx.partition_enabled,
            partition_cols=ctx.partition_cols,
            delta_output_folder=str(ctx.sales_out_folder),
            skip_order_cols=ctx.skip_order_cols,
        )
        budget_acc = result.budget_acc
        inventory_acc = result.inventory_acc
        wishlists_acc = result.wishlists_acc
        complaints_acc = result.complaints_acc
        order_id_int64 = bool(getattr(result.manifest, "order_id_int64", False))

    _run_step("Generating Sales", _do_generate)

    # --- Step 2: Budget generation (optional, must run before packaging)
    if budget_acc is not None and budget_acc.has_data:
        from src.facts.budget.runner import run_budget_pipeline

        def _do_budget():
            run_budget_pipeline(
                accumulator=budget_acc,
                parquet_dims=ctx.parquet_dims,
                fact_out=ctx.fact_out,
                cfg=ctx.cfg,
                file_format=ctx.fmt,
            )

        _run_step("Generating Budget", _do_budget)

    # --- Step 2b: Inventory (uses its own multiprocessing pool — run alone)
    if inventory_acc is not None and inventory_acc.has_data:
        from src.facts.inventory.runner import run_inventory_pipeline

        def _do_inventory():
            run_inventory_pipeline(
                accumulator=inventory_acc,
                parquet_dims=ctx.parquet_dims,
                fact_out=ctx.fact_out,
                cfg=ctx.cfg,
                file_format=ctx.fmt,
                workers=ctx.sales_cfg.workers,
            )

        _run_step("Generating Inventory Snapshots", _do_inventory)

    # --- Step 2c-d: Wishlists + Complaints (lightweight, independent — run in parallel)
    _parallel_steps = []

    if wishlists_acc is not None and wishlists_acc.has_data:
        from src.facts.wishlists.runner import run_wishlist_pipeline

        def _do_wishlists():
            run_wishlist_pipeline(
                accumulator=wishlists_acc,
                parquet_dims=ctx.parquet_dims,
                fact_out=ctx.fact_out,
                cfg=ctx.cfg,
                file_format=ctx.fmt,
            )

        _parallel_steps.append(("Generating Customer Wishlists", _do_wishlists))

    if complaints_acc is not None and complaints_acc.has_data:
        from src.facts.complaints.runner import run_complaints_pipeline

        def _do_complaints():
            run_complaints_pipeline(
                accumulator=complaints_acc,
                parquet_dims=ctx.parquet_dims,
                fact_out=ctx.fact_out,
                cfg=ctx.cfg,
                file_format=ctx.fmt,
            )

        _parallel_steps.append(("Generating Fact Complaints", _do_complaints))

    if len(_parallel_steps) > 1:
        # Run lightweight post-sales pipelines concurrently via threads.
        # These are I/O-heavy and release the GIL during numpy/parquet ops.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _run_labeled(label_fn):
            label, fn = label_fn
            _run_step(label, fn)

        with ThreadPoolExecutor(max_workers=len(_parallel_steps)) as pool:
            futures = [pool.submit(_run_labeled, step) for step in _parallel_steps]
            for fut in as_completed(futures):
                fut.result()  # re-raise any exception
    else:
        for label, fn in _parallel_steps:
            _run_step(label, fn)

    # --- Step 3: Package output + PBIP
    final_folder_result = None

    def _do_package():
        nonlocal final_folder_result
        final_folder_result = package_output(
            ctx.cfg_for_run, ctx.sales_cfg, ctx.parquet_dims, ctx.fact_out,
            order_id_int64=order_id_int64)

        # PBIP templates now live under:
        #   samples/powerbi/templates/{csv|parquet}/{Sales|Orders|Sales and Orders}
        # deltaparquet intentionally skips PBIP.
        maybe_attach_pbip_project(
            final_folder=final_folder_result,
            file_format=ctx.sales_cfg.file_format,
            sales_output=ctx.sales_cfg.sales_output,
        )

    _run_step("Packaging Output", _do_package)

    # --- Step 4: Quality report (optional)
    if report and final_folder_result is not None:
        from src.engine.quality_report import generate_quality_report

        def _do_report():
            generate_quality_report(final_folder_result, cfg=ctx.cfg)

        _run_step("Quality Report", _do_report)

    return None