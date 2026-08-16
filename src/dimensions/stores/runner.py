"""Pipeline entrypoint for the store dimension."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.engine.config.config_schema import AppConfig, StoresConfig

import numpy as np

from src.exceptions import DimensionError
from src.utils.logging_utils import debug, skip, stage, warn
from src.utils.output_utils import write_parquet_with_date32
from src.versioning.version_store import should_regenerate, save_version
from src.utils.name_pools import load_people_pools, resolve_people_folder
from src.utils.config_helpers import as_dict, int_or, float_or
from src.utils.config_precedence import resolve_seed
from src.dimensions.stores.geo import (
    _safe_read_geography,
    _build_location_maps,
    _geography_signature,
)
from src.dimensions.stores.generator import GeoContext, generate_store_table


def _require_cfg(cfg: AppConfig) -> StoresConfig:
    stores_cfg = cfg.stores if hasattr(cfg, "stores") else None
    if not isinstance(stores_cfg, Mapping):
        raise DimensionError("config missing required block: stores")
    return stores_cfg


def run_stores(cfg: AppConfig, parquet_folder: Path) -> None:
    """
    Pipeline wrapper for stores dimension generation.

    Single-writer ``stores.parquet``: this flow also allocates warehouses on the
    in-memory store frame, stamps the ``WarehouseKey`` FK, and writes both
    ``stores.parquet`` and ``warehouses.parquet`` once — so ``stores.parquet``
    carries every column and the configured writer settings from a single write.
    """
    # Lazy imports: the warehouse allocator and version helper live in a package
    # that (via its runner) re-invokes this function, so importing at module
    # level would form a cycle.
    from src.dimensions.warehouses.generator import generate_warehouse_table
    from src.dimensions.warehouses.runner import (
        WAREHOUSES_SCHEMA_VERSION,
        build_warehouse_version_cfg,
    )

    store_cfg = _require_cfg(cfg)

    parquet_folder = Path(parquet_folder)
    parquet_folder.mkdir(parents=True, exist_ok=True)

    out_path = parquet_folder / "stores.parquet"
    warehouses_path = parquet_folder / "warehouses.parquet"
    geo_path = parquet_folder / "geography.parquet"

    if not geo_path.exists():
        raise FileNotFoundError(f"Missing geography parquet: {geo_path}")

    geo_df = _safe_read_geography(geo_path)
    geo_keys = geo_df["GeographyKey"].astype(np.int64).to_numpy()
    loc_short_map, loc_full_map = _build_location_maps(geo_df)

    def _geo_lookup(col: str, dtype=str) -> Optional[dict]:
        if col not in geo_df.columns:
            return None
        g = geo_df[["GeographyKey", col]].dropna()
        return dict(zip(
            g["GeographyKey"].astype(np.int64).to_numpy(),
            g[col].astype(dtype).to_numpy(),
        ))

    iso_by_geo = _geo_lookup("ISOCode")

    country_col = next(
        (c for c in ("Country", "CountryRegionName", "RegionCountryName") if c in geo_df.columns),
        None,
    )
    country_by_geo = _geo_lookup(country_col) if country_col else None

    pop_by_geo = _geo_lookup("Population", dtype=np.int64)

    # Region weights — validate keys against available currencies
    region_weights: Optional[dict[str, float]] = None
    if store_cfg.region_weights:
        region_weights = dict(store_cfg.region_weights)
        if iso_by_geo:
            available_iso = set(iso_by_geo.values())
            invalid = sorted(set(region_weights.keys()) - available_iso)
            if invalid:
                warn(
                    f"stores.region_weights references currencies not in geography: {invalid}. "
                    f"These will be ignored. Available: {sorted(available_iso)}"
                )
                for code in invalid:
                    del region_weights[code]
            if not region_weights:
                region_weights = None

    sqft_cfg = as_dict(store_cfg.square_footage)
    staffing_overrides = as_dict(store_cfg.staffing_ranges) if store_cfg.staffing_ranges else None

    seed = resolve_seed(cfg, store_cfg, fallback=42)

    # Warehouse allocation is part of this flow now, so warehouse-shaping config
    # must move the stores version key too — otherwise editing a warehouse knob
    # would leave stores.parquet (and its WarehouseKey column) stale. num_stores
    # and geography already sit in the stores key, so only the warehouse-specific
    # inputs need folding in here.
    wh_cfg = as_dict(getattr(cfg, "warehouses", None)) or {}
    wh_seed = resolve_seed(cfg, wh_cfg, fallback=42)
    wh_min_spw = int(wh_cfg.get("min_stores_per_warehouse", 15))
    wh_min_sow = int(wh_cfg.get("min_stores_for_own_warehouse", 5))

    version_cfg = dict(store_cfg)
    # v15: Status is the sole store snapshot indicator; redundant OpenFlag was
    # removed from Stores and downstream analytical schemas.
    version_cfg["schema_version"] = 16
    version_cfg["_geography_sig"] = _geography_signature(geo_df)
    version_cfg["seed"] = int(seed)
    version_cfg["_warehouses"] = {
        "schema_version": WAREHOUSES_SCHEMA_VERSION,
        "seed": int(wh_seed),
        "min_stores_per_warehouse": wh_min_spw,
        "min_stores_for_own_warehouse": wh_min_sow,
    }

    if not should_regenerate("stores", version_cfg, out_path):
        skip("Stores up-to-date")
        return

    compression       = store_cfg.parquet_compression
    compression_level = store_cfg.parquet_compression_level
    force_date32      = bool(store_cfg.force_date32)

    opening_cfg = as_dict(store_cfg.opening)

    use_name_pools = bool(store_cfg.use_name_pools)
    people_pools = None
    if use_name_pools:
        people_folder = resolve_people_folder()
        pf = Path(people_folder)
        enable_asia = (
            (pf / "asia_male_first.csv").exists()
            and (pf / "asia_female_first.csv").exists()
            and (pf / "asia_last.csv").exists()
        )
        people_pools = load_people_pools(
            people_folder, enable_asia=enable_asia, legacy_support=True,
        )

    # Resolve dataset window for store closing constraint
    global_dates = as_dict(cfg.defaults.dates) if hasattr(cfg.defaults, "dates") else {}
    ds_start = global_dates.get("start")
    ds_end = global_dates.get("end")

    closing_cfg = as_dict(store_cfg.closing) if hasattr(store_cfg, "closing") and store_cfg.closing is not None else {}
    closing_enabled = closing_cfg.get("enabled", True)
    close_share = float_or(closing_cfg.get("close_share"), 0.10)
    online_stores_count = int_or(store_cfg.online_stores, 0)

    geo_ctx = GeoContext(
        geo_keys=geo_keys,
        geo_loc_short=loc_short_map,
        geo_loc_full=loc_full_map,
        iso_by_geo=iso_by_geo,
        pop_by_geo=pop_by_geo,
        country_by_geo=country_by_geo,
        ensure_iso_coverage=bool(store_cfg.ensure_iso_coverage),
        region_weights=region_weights,
    )

    with stage("Generating Stores"):
        df = generate_store_table(
            geo=geo_ctx,
            num_stores=int_or(store_cfg.num_stores, 200),
            opening_start=opening_cfg.get("start"),
            opening_end=opening_cfg.get("end"),
            closing_end=store_cfg.closing_end,
            seed=seed,
            square_footage_cfg=sqft_cfg,
            staffing_overrides=staffing_overrides,
            people_pools=people_pools,
            district_size=store_cfg.district_size,
            districts_per_region=store_cfg.districts_per_region,
            dataset_start=ds_start,
            dataset_end=ds_end,
            close_share=close_share,
            closing_enabled=closing_enabled,
            online_stores=online_stores_count,
            allow_mid_window_openings=bool(store_cfg.allow_mid_window_openings),
        )

        # Allocate warehouses on the in-memory store frame and stamp the
        # WarehouseKey FK before the single write, so stores.parquet is written
        # exactly once (no second rewrite that would drop the configured
        # compression / date32 settings).
        warehouses_df, store_to_wh = generate_warehouse_table(
            df,
            parquet_folder,
            seed=wh_seed,
            min_stores_per_warehouse=wh_min_spw,
            min_stores_for_own_warehouse=wh_min_sow,
        )
        df["WarehouseKey"] = (
            df["StoreKey"].astype(int).map(store_to_wh).astype("Int32")
        )

        write_parquet_with_date32(
            df,
            out_path,
            date_cols=["OpeningDate", "ClosingDate", "RenovationStartDate", "RenovationEndDate"],
            cast_all_datetime=False,
            compression=str(compression),
            compression_level=(int(compression_level) if compression_level is not None else None),
            force_date32=force_date32,
        )

        write_parquet_with_date32(
            warehouses_df,
            warehouses_path,
            cast_all_datetime=False,
            compression="snappy",
        )
        debug(f"Warehouses written: {warehouses_path.name}")

    save_version("stores", version_cfg, out_path)
    save_version("warehouses", build_warehouse_version_cfg(cfg, df), warehouses_path)
    debug(f"Stores dimension written: {out_path.name}")
