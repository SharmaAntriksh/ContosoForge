from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.exceptions import ConfigError, DimensionError
from src.utils import info, skip, warn
from src.utils.output_utils import write_parquet_with_date32
from src.versioning import should_regenerate, save_version

from src.utils.config_precedence import resolve_dates
from src.defaults import SCD2_END_OF_TIME

from .contoso_loader import load_contoso_products
from .contoso_expander import expand_contoso_products
from .pricing import apply_product_pricing
from .product_profile import enrich_products_attributes, apply_post_merge_enrichment
from .scd2 import generate_scd2_versions


# ---------------------------------------------------------------------
# Supplier assignment
# ---------------------------------------------------------------------
def _load_supplier_keys(output_folder: Path) -> np.ndarray:
    """
    Loads SupplierKey values from suppliers.parquet in the same dims folder.
    Returns sorted unique int64 keys.
    """
    sup_path = output_folder / "suppliers.parquet"
    if not sup_path.exists():
        raise FileNotFoundError(
            f"Missing suppliers dimension parquet: {sup_path}. "
            "Generate dimensions first (Suppliers)."
        )

    sup = pd.read_parquet(sup_path)
    key_col = None
    for c in ["SupplierKey", "Key"]:
        if c in sup.columns:
            key_col = c
            break
    if key_col is None:
        raise DimensionError(f"suppliers.parquet missing SupplierKey/Key. Available: {list(sup.columns)}")

    keys = pd.to_numeric(sup[key_col], errors="coerce").dropna().astype("int64").to_numpy()
    keys = np.unique(keys)
    if keys.size == 0:
        raise DimensionError("suppliers.parquet has zero valid SupplierKey values")
    return np.sort(keys)


# ---------------------------------------------------------------------
# Parallel enrichment orchestrator
# ---------------------------------------------------------------------
def _generate_parallel_enrichment(
    df: pd.DataFrame,
    config,
    seed: int,
    output_folder: Path,
    n_workers: int,
) -> pd.DataFrame:
    """Enrich products in parallel: chunk -> enrich -> merge -> rank columns."""
    import shutil
    from src.utils.pool import PoolRunSpec, iter_imap_unordered
    from .worker import product_enrich_chunk_worker

    N = len(df)

    # Chunk partitioning (same formula as customers)
    n_chunks = min(n_workers * 2, max(2, N // 10_000))
    n_chunks = max(2, n_chunks)
    n_actual_workers = min(n_chunks, n_workers)

    # Serialize config for workers (must be picklable plain dict)
    from src.utils.config_helpers import as_dict
    cfg_dump = as_dict(config)

    # Scratch directory
    scratch_dir = output_folder / "_product_chunks"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    # Split df into chunks and write to scratch
    chunk_boundaries = np.array_split(np.arange(N), n_chunks)

    tasks = []
    for i, indices in enumerate(chunk_boundaries):
        input_path = str(scratch_dir / f"chunk_{i:05d}_input.parquet")
        output_path = str(scratch_dir / f"chunk_{i:05d}_enriched.parquet")
        df.iloc[indices].to_parquet(input_path, index=False)
        tasks.append((
            i, seed,
            input_path, output_path,
            cfg_dump, str(output_folder),
        ))

    info(f"Product enrichment: {n_chunks} chunks across {n_actual_workers} workers")

    pool_spec = PoolRunSpec(
        processes=n_actual_workers,
        label="product_enrichment",
    )

    try:
        for _ in iter_imap_unordered(
            tasks=tasks,
            task_fn=product_enrich_chunk_worker,
            spec=pool_spec,
        ):
            pass

        # Merge enriched chunks (read in order for determinism)
        enriched_dfs = []
        for i in range(n_chunks):
            path = scratch_dir / f"chunk_{i:05d}_enriched.parquet"
            enriched_dfs.append(pd.read_parquet(path))

        merged = pd.concat(enriched_dfs, ignore_index=True)
        del enriched_dfs

        # Apply rank-dependent columns on full dataset
        merged = apply_post_merge_enrichment(merged, seed)

        return merged
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


# ---------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------
def load_product_dimension(config, output_folder: Path, *, log_skip: bool = True):
    """
    Product dimension loader (single method):

      - Load Contoso catalog as base (2517 rows typically)
      - Scale to target row count via expand_contoso_products():
          * stratified trim by SubcategoryKey when target < base_count
          * repeat/variants when target > base_count
      - Apply pricing from products.pricing (ListPrice/UnitCost finalized here)
      - Enrich attributes (merch/channel/logistics/quality)
      - Assign SupplierKey (optional)
      - Write products.parquet

    Returns:
        (DataFrame, regenerated: bool)
    """
    p = config["products"]
    seed = int(p.get("seed", 42))

    # Supplier assignment
    sup_cfg = p.get("supplier_assignment") or {}
    sup_enabled = bool(sup_cfg.get("enabled", True))
    sup_seed = int(sup_cfg.get("seed", seed))
    sup_strategy = str(sup_cfg.get("strategy", "by_base_product")).lower()

    supplier_keys = None
    supplier_sig = None
    if sup_enabled:
        supplier_keys = _load_supplier_keys(output_folder)
        supplier_sig = {"n": int(supplier_keys.size), "min": int(supplier_keys.min()), "max": int(supplier_keys.max())}

    # Versioning / skip
    parquet_path = output_folder / "products.parquet"
    version_key = _version_key(p)

    if sup_enabled:
        version_key = dict(version_key)
        version_key["supplier_assignment"] = {"enabled": True, "strategy": sup_strategy, "seed": sup_seed}
        version_key["supplier_sig"] = supplier_sig

    if not should_regenerate("products", version_key, parquet_path):
        if log_skip:
            skip("Products up-to-date")
        profile_path = output_folder / "product_profile.parquet"
        profile_df = pd.read_parquet(profile_path) if profile_path.exists() else pd.DataFrame()
        return pd.read_parquet(parquet_path), profile_df, False

    # Base catalog
    from src.utils.config_helpers import str_or
    catalog = str_or(p.get("catalog"), "all").strip().lower()
    base_df = load_contoso_products(output_folder, catalog=catalog)
    base_count = int(len(base_df))

    # Target row count
    target_n = p.get("num_products", None)
    if target_n is None:
        target_n = base_count
    target_n = int(target_n)

    if target_n <= 0:
        raise DimensionError("products.num_products must be a positive integer")

    if "num_products" in p and "use_contoso_products" in p:
        info("products.use_contoso_products is deprecated; ignoring (num_products is set)")

    if target_n < base_count:
        info(f"Trimming catalog ({catalog}): {base_count:,} -> {target_n:,} (stratified by SubcategoryKey)")
    elif target_n == base_count:
        info(f"Using catalog ({catalog}): {target_n:,}")
    else:
        info(f"Expanding catalog ({catalog}): {base_count:,} -> {target_n:,} (variants)")

    df = expand_contoso_products(
        base_products=base_df,
        num_products=target_n,
        seed=seed,
    )

    # Defensive: ensure ProductCode exists
    if "ProductCode" not in df.columns:
        df["ProductCode"] = df["ProductKey"].astype(str).str.zfill(7)


    # Pricing (authoritative)
    df = apply_product_pricing(
        df=df,
        pricing_cfg=p.get("pricing"),
        seed=seed,
    )

    # Enrichment columns (parallel when above threshold)
    from multiprocessing import cpu_count
    from src.defaults import PRODUCT_PARALLEL_THRESHOLD

    sales_cfg = getattr(config, "sales", None)
    configured_workers = getattr(sales_cfg, "workers", None) if sales_cfg else None
    from src.utils.config_helpers import int_or
    n_workers = max(1, int_or(configured_workers, cpu_count() - 1))

    if len(df) >= PRODUCT_PARALLEL_THRESHOLD and n_workers >= 2:
        df = _generate_parallel_enrichment(df, config, seed=seed,
                                            output_folder=output_folder,
                                            n_workers=n_workers)
    else:
        df = enrich_products_attributes(df, config, seed=seed, output_folder=output_folder)

    # SupplierKey (deterministic)
    if sup_enabled:
        n_sup = int(supplier_keys.size)
        base = pd.to_numeric(df.get("BaseProductKey", df["ProductKey"]), errors="coerce").fillna(0).astype("int64").to_numpy()

        if sup_strategy == "by_subcategory" and "SubcategoryKey" in df.columns:
            sub = pd.to_numeric(df["SubcategoryKey"], errors="coerce").fillna(0).astype("int64").to_numpy()
            idx = np.mod(sub, n_sup)
        elif sup_strategy == "uniform":
            rng_sup = np.random.default_rng(sup_seed)
            idx = rng_sup.integers(0, n_sup, size=len(df), dtype=np.int64)
        else:
            idx = np.mod(base, n_sup)

        df["SupplierKey"] = supplier_keys[idx].astype("int64")

    # Minimal required fields for Sales
    required = [
        "ProductKey",
        "BaseProductKey",
        "VariantIndex",
        "SubcategoryKey",
        "ListPrice",
        "UnitCost",
    ]

    missing = [c for c in required if c not in df.columns]
    if missing:
        raise DimensionError(f"Missing required field(s) in Products: {missing}")

    # -----------------------------------------------------------------
    # SCD Type 2 metadata (always present for consistent schema)
    # -----------------------------------------------------------------
    # ProductID is the stable cross-version identity (preserved by SCD2);
    # ProductKey gets reassigned per-version downstream. See CLAUDE.md gotcha #25.
    N = len(df)
    df["ProductID"] = df["ProductKey"].copy()
    df["VersionNumber"] = np.ones(N, dtype=np.int32)

    # Resolve date range for SCD2 effective dates
    try:
        start_date, end_date = resolve_dates(config, p, section_name="products")
    except (KeyError, ValueError, ConfigError):
        warn("Could not resolve dates for products; using fallback 2020-01-01 to 2025-12-31")
        start_date = pd.Timestamp("2020-01-01")
        end_date = pd.Timestamp("2025-12-31")

    df["EffectiveStartDate"] = start_date
    df["EffectiveEndDate"] = SCD2_END_OF_TIME
    df["IsCurrent"] = np.ones(N, dtype=bool)

    # SCD2 expansion (if enabled)
    scd2_cfg = getattr(p, "scd2", None)
    scd2_enabled = bool(getattr(scd2_cfg, "enabled", False)) if scd2_cfg else False
    if scd2_enabled:
        rng_scd2 = np.random.default_rng(seed + 7777)
        df = generate_scd2_versions(
            rng_scd2, df, scd2_cfg, start_date, end_date,
            pricing_cfg=p.get("pricing"),
        )

    # Backfill any null descriptions with the product name
    if "ProductDescription" in df.columns:
        df["ProductDescription"] = df["ProductDescription"].fillna(df["ProductName"])

    # -----------------------------------------------------------------
    # Split into Products (core) and ProductProfile (analytical)
    # -----------------------------------------------------------------
    products_df, profile_df = _split_products_and_profile(df)

    profile_path = output_folder / "product_profile.parquet"
    write_parquet_with_date32(products_df, parquet_path, cast_all_datetime=True)
    write_parquet_with_date32(profile_df, profile_path, cast_all_datetime=True)

    save_version("products", version_key, parquet_path)
    return products_df, profile_df, True


_PRODUCTS_CORE_COLS = (
    "ProductKey", "ProductID",
    "VersionNumber", "EffectiveStartDate", "EffectiveEndDate", "IsCurrent",
    "ProductCode", "ProductName", "ProductDescription",
    "SubcategoryKey", "Brand", "Class", "Color",
    "StockTypeCode", "StockType",
    "UnitCost", "ListPrice",
    "BaseProductKey", "VariantIndex",
    "Source",
)


def _split_products_and_profile(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split the enriched (and optionally SCD2-expanded) DataFrame into
    Products (core) and ProductProfile (analytical) tables.

    ProductProfile carries one row per Products row (same grain).  Under
    SCD2 this means one row per (ProductID, VersionNumber); historical
    sales/inventory rows that store a non-current ProductKey still resolve
    against profile.  ProductID is included so consumers can roll up
    analytical attributes across versions when desired.
    """
    if df.empty:
        raise DimensionError("Products dataframe is empty — cannot build ProductProfile")

    core_cols = [c for c in _PRODUCTS_CORE_COLS if c in df.columns]
    profile_cols = ["ProductKey", "ProductID"] + [c for c in df.columns if c not in core_cols]

    # df[list] already returns a fresh frame; no extra .copy() needed.
    products_df = df[core_cols]
    profile_df = df[profile_cols].reset_index(drop=True)

    # Reorder profile columns to match static_schemas.py (SQL CREATE TABLE order).
    # BULK INSERT is positional — CSV column order must match the schema exactly.
    from src.utils.static_schemas import STATIC_SCHEMAS
    schema_cols = [name for name, _ in STATIC_SCHEMAS.get("ProductProfile", ())]
    if schema_cols:
        ordered = [c for c in schema_cols if c in profile_df.columns]
        extra = [c for c in profile_df.columns if c not in schema_cols]
        profile_df = profile_df[ordered + extra]

    return products_df, profile_df


def _archetype_hash() -> str:
    """Stable hash of subcategory archetypes so edits trigger products regen.

    Imported lazily to avoid pulling product_profile (and its heavy validation)
    at module import time.
    """
    import hashlib
    import json
    from .product_profile import _SUBCATEGORY_ARCHETYPES, _DEFAULT_ARCHETYPE, _SIZE_DIMS
    payload = json.dumps(
        {
            "archetypes": _SUBCATEGORY_ARCHETYPES,
            "default": _DEFAULT_ARCHETYPE,
            "size_dims": _SIZE_DIMS,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _version_key(p: dict) -> dict:
    """
    Version key for Products. Pricing is the economic source of truth.
    """
    key = {
        "catalog": p.get("catalog", "all"),
        "num_products": p.get("num_products"),
        "seed": p.get("seed"),
        "pricing": p.get("pricing"),
        # bump whenever you add/remove enrichment columns (forces one regen)
        "enrichment_v": 9,
        # Auto-detect archetype edits (Material/Style/ProductLine/dims/season)
        "archetype_hash": _archetype_hash(),
    }
    # SCD2 settings affect output shape
    scd2 = p.get("scd2")
    if scd2 and bool(scd2.get("enabled", False)):
        key["scd2"] = {
            "enabled": True,
            "revision_frequency": scd2.get("revision_frequency", 12),
            "price_drift": scd2.get("price_drift", 0.05),
            "max_versions": scd2.get("max_versions", 4),
            "revision_probability": scd2.get("revision_probability", 0.40),
        }
    return key
