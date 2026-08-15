"""Warehouse dimension runner.

Warehouse allocation is owned by the **stores flow** (single-writer
``stores.parquet``): :func:`src.dimensions.stores.run_stores` builds the store
frame, allocates warehouses on it, stamps the ``WarehouseKey`` FK inline, and
writes both ``stores.parquet`` and ``warehouses.parquet`` once with the
stores-configured writer settings.

``run_warehouses`` stays registered so ``--regen-dimensions warehouses`` keeps
working: when ``warehouses.parquet`` is missing or its version key changed, it
re-invokes the stores flow (which rebuilds both files and re-stamps this
version).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from src.utils.logging_utils import skip
from src.versioning import should_regenerate, delete_version
from src.utils.config_helpers import as_dict
from src.utils.config_precedence import resolve_seed

if TYPE_CHECKING:
    from src.engine.config.config_schema import AppConfig


# Bump when the warehouse version-key layout changes. Shared by the stores flow
# (which writes + stamps the version) and the check below.
WAREHOUSES_SCHEMA_VERSION = 4  # v4: inventory targets moved to warehouse grain


def build_warehouse_version_cfg(cfg: AppConfig, stores_df: pd.DataFrame) -> dict:
    """Version key for ``warehouses.parquet``.

    Folds in the warehouse config, the resolved seed, and a store-count/zone
    signature of the frame allocated over. The stores flow stamps this after the
    single write; this runner checks it. Both must derive an identical key from
    the same store frame — ``_n_stores``/``_zones`` come from row count and
    ``StoreZone`` values, which survive the parquet round-trip unchanged.
    """
    wh_cfg = as_dict(getattr(cfg, "warehouses", None)) or {}
    seed = resolve_seed(cfg, wh_cfg, fallback=42)
    version_cfg = dict(wh_cfg)
    version_cfg["schema_version"] = WAREHOUSES_SCHEMA_VERSION
    version_cfg["seed"] = int(seed)
    version_cfg["_n_stores"] = len(stores_df)
    if "StoreZone" in stores_df.columns:
        version_cfg["_zones"] = sorted(stores_df["StoreZone"].unique().tolist())
    return version_cfg


def run_warehouses(cfg: AppConfig, parquet_folder: Path) -> None:
    """Ensure ``warehouses.parquet`` is fresh; the stores flow builds it.

    In a normal pipeline run the stores stage has already produced both files and
    stamped the warehouse version, so this is a no-op skip. Under
    ``--regen-dimensions warehouses`` (version deleted) it forces the stores flow
    to rebuild the unit.
    """
    parquet_folder = Path(parquet_folder)
    out_path = parquet_folder / "warehouses.parquet"
    stores_path = parquet_folder / "stores.parquet"

    if not stores_path.exists():
        raise FileNotFoundError(f"Missing stores parquet: {stores_path}")

    stores_df = pd.read_parquet(str(stores_path))
    version_cfg = build_warehouse_version_cfg(cfg, stores_df)

    if not should_regenerate("warehouses", version_cfg, out_path):
        skip("Warehouses up-to-date")
        return

    # Stale/forced: the allocator lives in the stores flow, so force stores to
    # rebuild — it rewrites both files and re-stamps this version.
    from src.dimensions.stores import run_stores  # lazy: avoid an import cycle

    delete_version("stores")
    run_stores(cfg, parquet_folder)
