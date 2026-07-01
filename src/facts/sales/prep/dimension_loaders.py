"""Dimension loaders for the sales fact.

Each loader reads a dimension parquet and returns the arrays/dicts the sales
workers need (customer / product / store / promotion / employee) plus the
per-promotion salience weights. RNG use is pure and seed-based; no State reads and
no shared-memory broadcasts happen here.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as _pq

from src.defaults import (
    ONLINE_SALES_REP_ROLE,
)
from src.exceptions import SalesError
from src.utils.config_helpers import float_or as _float_or
from src.utils.logging_utils import info, warn

from .sales_helpers import (
    _as_np,
    _cfg_get,
    _normalize_dt_any,
    _normalize_nullable_int_month,
    build_weighted_date_pool,
    load_parquet_df,
)
from .scd2_grid import _build_scd2_customer_versions, _build_scd2_product_versions


def _load_customers(
    parquet_folder_p: Path,
    cfg,
    start_date,
    seed: int,
) -> dict:
    """Load customer dimension arrays for the sales pool.

    Returns a dict with all customer-related arrays that the caller
    needs for worker_cfg and correlation lookups.
    """
    customers_path = parquet_folder_p / "customers.parquet"

    # Single parquet read: discover available columns via schema, then
    # load ALL needed columns in one I/O call instead of 3-5 separate opens.
    _cust_schema_names = set(_pq.read_schema(str(customers_path)).names)

    _cust_load_cols = ["CustomerKey", "CustomerStartDate", "CustomerEndDate"]
    # Backward compat: also load legacy columns if they still exist
    _cust_legacy = ["IsActiveInSales", "CustomerStartMonth", "CustomerEndMonth"]
    # SCD2 columns (previously a separate parquet open)
    _cust_scd2_cols_wanted = ["CustomerID", "IsCurrent"]
    # Weight columns (previously a separate parquet open)
    _cust_weight_cols = ["CustomerBaseWeight", "CustomerWeight"]
    # Geo column (previously a separate parquet open)
    _cust_geo_cols = ["GeographyKey"]

    _cust_all_cols = list(dict.fromkeys(
        _cust_load_cols
        + [c for c in _cust_legacy if c in _cust_schema_names]
        + [c for c in _cust_scd2_cols_wanted if c in _cust_schema_names]
        + [c for c in _cust_weight_cols if c in _cust_schema_names]
        + [c for c in _cust_geo_cols if c in _cust_schema_names]
    ))

    cust_df_full = load_parquet_df(customers_path, _cust_all_cols)

    if cust_df_full.empty:
        raise SalesError("customers.parquet is empty; cannot generate sales")

    # --- SCD2 customer deduplication ---
    _cust_scd2_detected = False
    _cust_pool_ids = None   # CustomerID array (parallel to pool) — set if SCD2 active
    # Save pre-filter geo column before SCD2 filtering (needed later for geo mapping)
    _cust_geo_full = _as_np(cust_df_full["GeographyKey"], np.int32) if "GeographyKey" in cust_df_full.columns else None
    _cust_is_current_full = cust_df_full["IsCurrent"].values if "IsCurrent" in cust_df_full.columns else None

    if ("CustomerID" in cust_df_full.columns and "IsCurrent" in cust_df_full.columns
            and (cust_df_full["IsCurrent"] == 0).any()):
        _cust_scd2_detected = True
        _n_before = len(cust_df_full)
        # Keep only IsCurrent=1 rows for the sampling pool
        _is_current_mask = cust_df_full["IsCurrent"] == 1
        cust_df = cust_df_full[_is_current_mask].reset_index(drop=True)
        cust_df = cust_df.drop(columns=["IsCurrent"], errors="ignore")
        _cust_pool_ids = _as_np(cust_df["CustomerID"], np.int32)
        info(f"Customer SCD2: dedup {_n_before:,} -> {len(cust_df):,} rows (IsCurrent=1 pool)")
    else:
        cust_df = cust_df_full.drop(columns=["IsCurrent"], errors="ignore")

    # Free the full DataFrame — cust_df now holds the filtered pool
    del cust_df_full

    customer_keys = _as_np(cust_df["CustomerKey"], np.int32)

    # CustomerKey -> first EffectiveStartDate (epoch days) lookup. Eligibility
    # is month-granular but EffectiveStartDate is day-granular (random 0-27 day
    # offset within CustomerStartMonth), so without a per-row clamp the chunk
    # builder can place orders earlier in the start month than the customer's
    # actual join date. INT64_MIN fills unknown-key slots so np.maximum against
    # OrderDate is a natural no-op there. Used by chunk_builder.py.
    if "CustomerStartDate" in cust_df.columns:
        _cust_start_days = pd.to_datetime(
            cust_df["CustomerStartDate"], errors="coerce"
        ).values.astype("datetime64[D]").astype(np.int64)
        customer_first_eff_start_by_key = np.full(
            int(customer_keys.max()) + 1, np.iinfo(np.int64).min, dtype=np.int64,
        )
        customer_first_eff_start_by_key[customer_keys] = _cust_start_days
    else:
        customer_first_eff_start_by_key = None

    # --- Derive month indices from dates ---
    config_start = pd.to_datetime(start_date).to_period("M")

    if "CustomerStartMonth" in cust_df.columns:
        # Legacy path: use stored month indices
        customer_start_month = _as_np(cust_df["CustomerStartMonth"], np.int64)
    elif "CustomerStartDate" in cust_df.columns:
        cust_start_ts = pd.to_datetime(cust_df["CustomerStartDate"], errors="coerce")
        cust_start_period = cust_start_ts.dt.to_period("M")
        customer_start_month = (cust_start_period.apply(lambda p: p.ordinal) - config_start.ordinal).to_numpy(dtype=np.int64)
        customer_start_month = np.clip(customer_start_month, 0, None)
    else:
        customer_start_month = np.zeros(len(customer_keys), dtype=np.int64)

    if "CustomerEndMonth" in cust_df.columns:
        customer_end_month = _normalize_nullable_int_month(_as_np(cust_df["CustomerEndMonth"]), len(customer_keys))
    elif "CustomerEndDate" in cust_df.columns:
        cust_end_ts = pd.to_datetime(cust_df["CustomerEndDate"], errors="coerce")
        customer_end_month = np.full(len(customer_keys), -1, dtype=np.int64)
        valid_end = cust_end_ts.notna()
        if valid_end.any():
            end_periods = cust_end_ts[valid_end].dt.to_period("M")
            customer_end_month[valid_end.to_numpy()] = (end_periods.apply(lambda p: p.ordinal) - config_start.ordinal).to_numpy(dtype=np.int64)
    else:
        customer_end_month = np.full(len(customer_keys), -1, dtype=np.int64)

    # --- Derive is_active_in_sales ---
    # active_ratio marks a permanent inactive fraction (never transact).
    # Derived from seed for reproducibility without a persisted column.
    if "IsActiveInSales" in cust_df.columns:
        is_active_in_sales = _as_np(cust_df["IsActiveInSales"], np.int32)
    else:
        _cust_active_ratio = _float_or(
            _cfg_get(cfg, ["customers", "active_ratio"], 1.0), 1.0
        )
        N_cust = len(customer_keys)
        active_count = int(np.floor(N_cust * _cust_active_ratio))
        if 0 < active_count < N_cust:
            _ar_rng = np.random.default_rng(seed + 7)
            active_idx = _ar_rng.choice(N_cust, size=active_count, replace=False)
            is_active_in_sales = np.zeros(N_cust, dtype=np.int32)
            is_active_in_sales[active_idx] = 1
        else:
            is_active_in_sales = np.ones(N_cust, dtype=np.int32)

    # Extract customer weight from the already-loaded DataFrame (no extra parquet open)
    customer_base_weight = None
    for wcol in ("CustomerBaseWeight", "CustomerWeight"):
        if wcol in cust_df.columns:
            customer_base_weight = _as_np(cust_df[wcol], np.float64)
            break

    # --- Resolve customer_geo_key (for correlation lookups) ---
    # Dense array indexed by CustomerKey (not pool position) so that
    # geo-bias store sampling works even when keys are sparse (SCD2).
    customer_geo_key = None
    _pool_geo = None
    if _cust_geo_full is not None:
        if _cust_scd2_detected and _cust_is_current_full is not None:
            _pool_geo = _cust_geo_full[_cust_is_current_full == 1]
        else:
            _pool_geo = _cust_geo_full
    elif "GeographyKey" in cust_df.columns:
        _pool_geo = _as_np(cust_df["GeographyKey"], np.int32)

    if _pool_geo is not None:
        _max_ck = int(customer_keys.max()) + 1
        customer_geo_key = np.zeros(_max_ck, dtype=np.int32)
        customer_geo_key[customer_keys] = _pool_geo
    del _cust_geo_full, _cust_is_current_full, _pool_geo

    # --- Build customer SCD2 version tables ---
    _customer_scd2_active = False
    _customer_scd2_starts = None
    _customer_scd2_keys = None
    _cust_key_to_pool_idx = None

    if _cust_scd2_detected and _cust_pool_ids is not None:
        _cust_result = _build_scd2_customer_versions(
            customers_path, customer_keys, _cust_pool_ids,
        )
        if _cust_result is not None:
            _customer_scd2_starts, _customer_scd2_keys, _cust_key_to_pool_idx = _cust_result
            _customer_scd2_active = True
            info(f"Customer SCD2: {_customer_scd2_starts.shape[1]} max versions × "
                 f"{_customer_scd2_starts.shape[0]:,} customers")

    return {
        "customer_keys": customer_keys,
        "customer_start_month": customer_start_month,
        "customer_end_month": customer_end_month,
        "is_active_in_sales": is_active_in_sales,
        "customer_base_weight": customer_base_weight,
        "customer_geo_key": customer_geo_key,
        "customer_first_eff_start_by_key": customer_first_eff_start_by_key,
        "customer_scd2_active": _customer_scd2_active,
        "customer_scd2_starts": _customer_scd2_starts,
        "customer_scd2_keys": _customer_scd2_keys,
        "cust_key_to_pool_idx": _cust_key_to_pool_idx,
    }


def _load_products(
    parquet_folder_p: Path,
    cfg,
    seed: int,
    start_date,
    end_date,
    active_product_np=None,
) -> dict:
    """Load product dimension arrays, profile, date pool, and SCD2 versions.

    Returns a dict with all product-related arrays plus date_pool/date_prob.
    """
    product_brand_key = None
    brand_names = None
    product_subcat_key = None
    products_path = parquet_folder_p / "products.parquet"
    assortment_cfg = (getattr(cfg, "stores", None) or {}).get("assortment") or {}

    def _brand_codes_from_series(s: pd.Series) -> tuple[np.ndarray, np.ndarray]:
        # Guarantee no NA => no -1 codes
        s2 = s.fillna("Unknown").astype(str)
        codes, uniques = pd.factorize(s2, sort=True)
        return np.asarray(codes, dtype=np.int32), np.asarray(uniques, dtype=object)

    # Read schema once (metadata only) to discover available columns
    _prod_schema = set(_pq.read_schema(str(products_path)).names)
    _has_brand_col = "Brand" in _prod_schema
    _has_subcat_col = "SubcategoryKey" in _prod_schema
    # Load SubcategoryKey whenever available: consumed by store assortment AND by
    # the basket-theme correlation (which is independent of assortment).
    _need_subcat = _has_subcat_col

    # Determine SCD2 capability upfront so we include required columns in
    # the single product load below (avoids extra parquet opens later).
    _prod_has_scd2 = (
        "ProductID" in _prod_schema
        and "IsCurrent" in _prod_schema
        and "EffectiveStartDate" in _prod_schema
    )

    # Cached full product DataFrame — reused for SCD2 version table building
    _prod_df_full = None

    if active_product_np is not None:
        product_np = active_product_np
        active_keys = np.asarray(product_np[:, 0], dtype=np.int32)

        # Single load: Brand + optional SubcategoryKey + SCD2 columns (one parquet open)
        _prod_cols = ["ProductKey"]
        if _has_brand_col:
            _prod_cols.append("Brand")
        if _need_subcat:
            _prod_cols.append("SubcategoryKey")
        # Include SCD2 columns so we can reuse this DataFrame later
        if _prod_has_scd2:
            for _sc in ("ProductID", "IsCurrent"):
                if _sc not in _prod_cols:
                    _prod_cols.append(_sc)

        try:
            _prod_df = load_parquet_df(products_path, _prod_cols)
            # Keep the full DF for SCD2 use before dedup
            if _prod_has_scd2:
                _prod_df_full = _prod_df
            _prod_df_dedup = _prod_df.drop_duplicates("ProductKey", keep="first")
            _prod_keys = _prod_df_dedup["ProductKey"].to_numpy(dtype=np.int32)

            # Sorted-key lookup (avoids pandas reindex float64 promotion)
            _sort_idx = np.argsort(_prod_keys)
            _sorted_keys = _prod_keys[_sort_idx]
            _pos = np.clip(np.searchsorted(_sorted_keys, active_keys), 0, max(len(_sorted_keys) - 1, 0))
            _found = _sorted_keys[_pos] == active_keys

            if _has_brand_col:
                codes, brand_names = _brand_codes_from_series(_prod_df_dedup["Brand"])
                bk = np.full(len(active_keys), -1, dtype=np.int32)
                bk[_found] = codes[_sort_idx][_pos[_found]]
                if np.any(bk < 0):
                    info("Brand mapping missing/invalid for some ProductKeys; disabling brand_popularity for this run.")
                else:
                    product_brand_key = bk

            if _need_subcat and "SubcategoryKey" in _prod_df_dedup.columns:
                subcat_vals = _prod_df_dedup["SubcategoryKey"].to_numpy(dtype=np.int32)
                sc = np.zeros(len(active_keys), dtype=np.int32)
                sc[_found] = subcat_vals[_sort_idx][_pos[_found]]
                product_subcat_key = sc

            del _prod_df_dedup

        except (KeyError, ValueError, TypeError, OSError) as exc:
            info(f"Could not load/derive Brand from products.parquet ({type(exc).__name__}: {exc}); "
                 "disabling brand_popularity for this run.")
            product_brand_key = None

    else:
        # Full product path — single load with all needed columns
        _prod_cols = ["ProductKey", "ListPrice", "UnitCost"]
        if _has_brand_col:
            _prod_cols.append("Brand")
        if _need_subcat:
            _prod_cols.append("SubcategoryKey")
        # SCD2: load IsCurrent + ProductID to reuse for version table building
        if "IsCurrent" in _prod_schema:
            _prod_cols.append("IsCurrent")
        if _prod_has_scd2 and "ProductID" in _prod_schema:
            _prod_cols.append("ProductID")

        prod_df = load_parquet_df(products_path, _prod_cols)
        # Keep the full DF for SCD2 use before filtering
        if _prod_has_scd2:
            _prod_df_full = prod_df
        # SCD2: only use current version rows for the product pool
        if "IsCurrent" in prod_df.columns:
            prod_df = prod_df[prod_df["IsCurrent"] == 1].copy()
            prod_df = prod_df.drop(columns=["IsCurrent"], errors="ignore")
        if "ProductID" in prod_df.columns:
            prod_df = prod_df.drop(columns=["ProductID"], errors="ignore")
        prod_df["ProductKey"] = pd.to_numeric(prod_df["ProductKey"], errors="coerce")
        prod_df["ListPrice"] = pd.to_numeric(prod_df["ListPrice"], errors="coerce")
        prod_df["UnitCost"] = pd.to_numeric(prod_df["UnitCost"], errors="coerce")
        prod_df = prod_df.dropna(subset=["ProductKey", "ListPrice", "UnitCost"])
        prod_df["ProductKey"] = prod_df["ProductKey"].astype("int32", copy=False)

        product_np = np.column_stack([
            prod_df["ProductKey"].to_numpy(dtype=np.int32, copy=False),
            prod_df["ListPrice"].to_numpy(dtype=np.float64, copy=False),
            prod_df["UnitCost"].to_numpy(dtype=np.float64, copy=False),
        ])

        if _has_brand_col:
            codes, brand_names = _brand_codes_from_series(prod_df["Brand"])
            product_brand_key = codes if not np.any(codes < 0) else None

        if _need_subcat and "SubcategoryKey" in prod_df.columns:
            product_subcat_key = prod_df["SubcategoryKey"].to_numpy(dtype=np.int32)

    # PopularityScore + SeasonalityProfile from ProductProfile (for weighted sampling)
    product_popularity = None
    product_seasonality = None
    _profile_path = parquet_folder_p / "product_profile.parquet"
    if _profile_path.exists():
        try:
            _pp_df = load_parquet_df(_profile_path, ["ProductKey", "PopularityScore", "SeasonalityProfile"])
            _pp_df = _pp_df.drop_duplicates("ProductKey", keep="first")
            _pp_df["ProductKey"] = _pp_df["ProductKey"].astype("int32")
            active_keys = np.asarray(product_np[:, 0], dtype=np.int32)
            _pp_map_pop = pd.Series(
                _pp_df["PopularityScore"].to_numpy(dtype=np.float64),
                index=_pp_df["ProductKey"].to_numpy(dtype=np.int32),
            )
            product_popularity = _pp_map_pop.reindex(active_keys).fillna(50.0).to_numpy(dtype=np.float64)
            _pp_map_sea = pd.Series(
                _pp_df["SeasonalityProfile"].to_numpy().astype(str),
                index=_pp_df["ProductKey"].to_numpy(dtype=np.int32),
            )
            _sea_str = _pp_map_sea.reindex(active_keys).fillna("None").to_numpy().astype(str)
            # Encode as int8 so it can be shared via shared memory (avoids 8x pickle of object array)
            _SEASON_ENCODE = {"Holiday": 1, "Winter": 2, "Summer": 3, "BackToSchool": 4, "Spring": 5}
            product_seasonality = np.zeros(len(_sea_str), dtype=np.int8)
            for _sname, _scode in _SEASON_ENCODE.items():
                product_seasonality[_sea_str == _sname] = _scode
            # Warn on unmapped profiles so typos / new categories don't get
            # silently bucketed as "no seasonal boost".
            _known = set(_SEASON_ENCODE) | {"None", "nan"}
            _unknown = [s for s in np.unique(_sea_str).tolist() if s not in _known]
            if _unknown:
                warn(
                    f"Unknown SeasonalityProfile values in product_profile.parquet "
                    f"(no boost applied): {_unknown}. "
                    f"Expected one of: {sorted(_SEASON_ENCODE)}."
                )
        except (KeyError, ValueError, TypeError, OSError) as exc:
            info(f"Could not load product profile ({type(exc).__name__}: {exc}); using uniform product sampling.")
            product_popularity = None
            product_seasonality = None

    # Weighted date pool (deterministic) — needed by SCD2 grid builders below
    date_pool, date_prob = build_weighted_date_pool(start_date, end_date, seed)

    # --- SCD2 product version lookup tables ---
    _product_scd2_active = False
    _product_scd2_starts = None   # (N_pool, max_ver) int64
    _product_scd2_data = None     # (N_pool, max_ver, 3) float64

    # Detect product SCD2 and build version tables (reusing _prod_df_full
    # from the single product load above — no extra parquet opens).
    if _prod_has_scd2 and _prod_df_full is not None:
        try:
            _has_history = (_prod_df_full["IsCurrent"] == 0).any()
        except (KeyError, ValueError):
            _has_history = False

        if _has_history:
            try:
                _pid_current = _prod_df_full[_prod_df_full["IsCurrent"] == 1].drop_duplicates("ProductKey", keep="first")
                _pid_map = pd.Series(
                    _pid_current["ProductID"].to_numpy(dtype=np.int32),
                    index=_pid_current["ProductKey"].to_numpy(dtype=np.int32),
                )
                _pool_keys = np.asarray(product_np[:, 0], dtype=np.int32)
                _reindexed = _pid_map.reindex(_pool_keys)
                _unmapped = _reindexed.isna()
                if _unmapped.any():
                    info(f"Product SCD2: {int(_unmapped.sum())} pool keys have no "
                         f"IsCurrent=1 ProductID mapping; they will use current-version prices.")
                _pool_product_ids = _reindexed.fillna(-1).to_numpy(dtype=np.int32)
                del _pid_current, _pid_map, _reindexed

                _prod_result = _build_scd2_product_versions(
                    products_path, _pool_product_ids, product_np,
                )
                if _prod_result is not None:
                    _product_scd2_starts, _product_scd2_data = _prod_result
                    _product_scd2_active = True
                    info(f"Product SCD2: {_product_scd2_starts.shape[1]} max versions × "
                         f"{_product_scd2_starts.shape[0]:,} products")
            except (KeyError, ValueError, TypeError, OSError) as exc:
                info(f"Product SCD2 build failed ({type(exc).__name__}: {exc}); "
                     "using current-version prices for all months.")

    # Free the full product DataFrame now that SCD2 is built
    del _prod_df_full

    return {
        "product_np": product_np,
        "product_brand_key": product_brand_key,
        "brand_names": brand_names,
        "product_subcat_key": product_subcat_key,
        "product_popularity": product_popularity,
        "product_seasonality": product_seasonality,
        "date_pool": date_pool,
        "date_prob": date_prob,
        "product_scd2_active": _product_scd2_active,
        "product_scd2_starts": _product_scd2_starts,
        "product_scd2_data": _product_scd2_data,
        "assortment_cfg": assortment_cfg,
    }


def _load_stores(parquet_folder_p, end_date, weight_cfg=None):
    """Load store dimension arrays for the sales pool.

    ``weight_cfg`` (optional ``{by_type, revenue_class}``) enables per-store
    demand weighting: when set, a row-ordered ``store_demand_weight`` array is
    returned so the sampler can draw orders toward bigger / higher-revenue
    stores. When None, this stays None and the worker defaults it to all-ones
    (uniform) — the single sampler handles both.
    """
    _store_cols = ["StoreKey", "GeographyKey"]
    _store_path = parquet_folder_p / "stores.parquet"
    if _store_path.exists():
        _store_schema_names = set(_pq.read_schema(str(_store_path)).names)
        if "StoreType" in _store_schema_names:
            _store_cols.append("StoreType")
        if weight_cfg and "RevenueClass" in _store_schema_names:
            _store_cols.append("RevenueClass")
        if "OpeningDate" in _store_schema_names:
            _store_cols.append("OpeningDate")
        if "ClosingDate" in _store_schema_names:
            _store_cols.append("ClosingDate")
        if "RenovationStartDate" in _store_schema_names:
            _store_cols.append("RenovationStartDate")
        if "RenovationEndDate" in _store_schema_names:
            _store_cols.append("RenovationEndDate")
    store_df = load_parquet_df(_store_path, _store_cols)
    store_keys = _as_np(store_df["StoreKey"], np.int32)
    store_to_geo = dict(zip(_as_np(store_df["StoreKey"], np.int32), _as_np(store_df["GeographyKey"], np.int32)))

    store_open_month = None
    store_close_month = None
    store_open_day = None
    store_close_day = None
    _FAR_PAST_DAY = np.datetime64("1900-01-01", "D")
    _FAR_FUTURE_DAY = np.datetime64("2262-04-11", "D")
    if "OpeningDate" in store_df.columns:
        _open_dt = pd.to_datetime(store_df["OpeningDate"]).values.astype("datetime64[M]")
        store_open_month = _open_dt.astype("int64").astype(np.int64)
        _open_dt_d = pd.to_datetime(store_df["OpeningDate"]).values.astype("datetime64[D]")
        _open_nat = np.isnat(_open_dt_d)
        _open_dt_d[_open_nat] = _FAR_PAST_DAY
        store_open_day = _open_dt_d
    if "ClosingDate" in store_df.columns:
        _close_dt = pd.to_datetime(store_df["ClosingDate"]).values
        _close_nat_mask = np.isnat(_close_dt)
        _close_m = _close_dt.astype("datetime64[M]").astype("int64").astype(np.int64)
        _close_m[_close_nat_mask] = np.iinfo(np.int64).max
        store_close_month = _close_m
        _close_dt_d = _close_dt.astype("datetime64[D]")
        _close_dt_d[_close_nat_mask] = _FAR_FUTURE_DAY
        store_close_day = _close_dt_d

    store_reno_start_day = None
    store_reno_end_day = None
    if "RenovationStartDate" in store_df.columns and "RenovationEndDate" in store_df.columns:
        _rs_dt = pd.to_datetime(store_df["RenovationStartDate"]).values.astype("datetime64[D]")
        _re_dt = pd.to_datetime(store_df["RenovationEndDate"]).values.astype("datetime64[D]")
        _rs_nat = np.isnat(_rs_dt)
        _re_nat = np.isnat(_re_dt)
        # Use sentinels that never overlap any month: start = far future, end = far past.
        _rs_dt[_rs_nat | _re_nat] = _FAR_FUTURE_DAY
        _re_dt[_rs_nat | _re_nat] = _FAR_PAST_DAY
        store_reno_start_day = _rs_dt
        store_reno_end_day = _re_dt

    store_type_map = None
    if "StoreType" in store_df.columns:
        store_type_map = dict(zip(
            store_df["StoreKey"].astype(int).tolist(),
            store_df["StoreType"].astype(str).tolist(),
        ))

    # Per-store demand weight (row-ordered, aligned with store_keys). Bigger /
    # higher-revenue stores get a larger weight so the sampler draws more orders
    # to them. Left None when unconfigured; the worker defaults it to all-ones
    # (uniform).
    store_demand_weight = None
    if weight_cfg:
        by_type = {str(k): float(v) for k, v in (weight_cfg.get("by_type") or {}).items()}
        by_rc = {str(k): float(v) for k, v in (weight_cfg.get("revenue_class") or {}).items()}
        n = len(store_df)
        w = np.ones(n, dtype=np.float64)
        if "StoreType" in store_df.columns and by_type:
            w *= store_df["StoreType"].astype(str).map(lambda t: by_type.get(t, 1.0)).to_numpy(dtype=np.float64)
        if "RevenueClass" in store_df.columns and by_rc:
            w *= store_df["RevenueClass"].astype(str).map(lambda c: by_rc.get(c, 1.0)).to_numpy(dtype=np.float64)
        w[~np.isfinite(w) | (w <= 0)] = 1.0  # guard against bad config values
        store_demand_weight = w

    # Geography + currency mapping
    geo_df = load_parquet_df(parquet_folder_p / "geography.parquet", ["GeographyKey", "ISOCode"])
    currency_df = load_parquet_df(parquet_folder_p / "currency.parquet", ["CurrencyKey", "CurrencyCode"])

    geo_df = geo_df.merge(currency_df, left_on="ISOCode", right_on="CurrencyCode", how="left")
    if geo_df["CurrencyKey"].isna().any():
        n_missing = int(geo_df["CurrencyKey"].isna().sum())
        missing_isos = geo_df.loc[geo_df["CurrencyKey"].isna(), "ISOCode"].unique().tolist()
        default_currency = int(currency_df.iloc[0]["CurrencyKey"])
        geo_df["CurrencyKey"] = geo_df["CurrencyKey"].fillna(default_currency)
        info(f"WARNING: {n_missing} geography row(s) have no currency match (ISOs: {missing_isos}); "
             f"defaulting to CurrencyKey={default_currency}.")

    geo_to_currency = dict(zip(_as_np(geo_df["GeographyKey"], np.int32), _as_np(geo_df["CurrencyKey"], np.int32)))

    return {
        "store_keys": store_keys,
        "store_to_geo": store_to_geo,
        "store_open_month": store_open_month,
        "store_close_month": store_close_month,
        "store_open_day": store_open_day,
        "store_close_day": store_close_day,
        "store_reno_start_day": store_reno_start_day,
        "store_reno_end_day": store_reno_end_day,
        "store_type_map": store_type_map,
        "store_demand_weight": store_demand_weight,
        "geo_to_currency": geo_to_currency,
    }


def _load_promotions(parquet_folder_p, promo_df=None):
    """Load promotion arrays for the sales pool."""
    if promo_df is None:
        promo_df = load_parquet_df(parquet_folder_p / "promotions.parquet")

    if promo_df.empty:
        return {
            "promo_df": promo_df,
            "promo_keys_all": np.array([], dtype=np.int32),
            "promo_start_all": np.array([], dtype="datetime64[D]"),
            "promo_end_all": np.array([], dtype="datetime64[D]"),
            "new_customer_promo_keys": np.array([], dtype=np.int32),
        }

    promo_start = _normalize_dt_any(promo_df["StartDate"])
    promo_end = _normalize_dt_any(promo_df["EndDate"])

    promo_keys_all = _as_np(promo_df["PromotionKey"], np.int32)
    promo_start_all = _as_np(promo_start, "datetime64[D]")
    promo_end_all = _as_np(promo_end, "datetime64[D]")

    if "PromotionType" in promo_df.columns:
        nc_mask = promo_df["PromotionType"].astype(str) == "New Customer"
        new_customer_promo_keys = _as_np(promo_df.loc[nc_mask, "PromotionKey"], np.int32)
    else:
        new_customer_promo_keys = np.array([], dtype=np.int32)

    return {
        "promo_df": promo_df,
        "promo_keys_all": promo_keys_all,
        "promo_start_all": promo_start_all,
        "promo_end_all": promo_end_all,
        "new_customer_promo_keys": new_customer_promo_keys,
    }


def _compute_promo_salience(promo_df, promo_keys_all, models_cfg):
    """Per-promo selection weight for promo-salience weighting.

    ``salience[i] = exp(beta * DiscountPct_i) * type_weights[PromotionType_i]``,
    aligned to ``promo_keys_all`` (``promo_df`` is in the same row order). Returns
    ``None`` — so ``apply_promotions`` falls back to a uniform draw — when the
    feature is disabled or the required columns are absent.
    """
    if models_cfg is None:
        return None
    sal_cfg = models_cfg.get("promotions", None)
    if sal_cfg is None or not bool(sal_cfg.get("enabled", True)):
        return None
    if promo_df is None or getattr(promo_df, "empty", True):
        return None
    n = int(len(promo_keys_all))
    if n == 0 or len(promo_df) != n or "DiscountPct" not in promo_df.columns:
        return None

    beta = float(sal_cfg.get("beta", 3.0))
    type_weights = dict(sal_cfg.get("type_weights", {}) or {})
    max_ratio = float(sal_cfg.get("max_weight_ratio", 12.0))

    disc = np.clip(promo_df["DiscountPct"].to_numpy(dtype=np.float64), 0.0, 1.0)
    disc = np.where(np.isfinite(disc), disc, 0.0)
    sal = np.exp(beta * disc)

    if type_weights and "PromotionType" in promo_df.columns:
        ptype = promo_df["PromotionType"].astype(str).to_numpy()
        tw = np.array([float(type_weights.get(t, 1.0)) for t in ptype], dtype=np.float64)
        sal = sal * np.where(np.isfinite(tw) & (tw > 0.0), tw, 1.0)

    sal = np.where(np.isfinite(sal) & (sal > 0.0), sal, 1e-9)
    if max_ratio > 1.0:
        sal = np.maximum(sal, float(sal.max()) / max_ratio)
    return sal.astype(np.float64)


def _load_employees(parquet_folder_p, cfg, end_date):
    """Load employee store assignments for salesperson resolution."""
    emp_assign_path = parquet_folder_p / "employee_store_assignments.parquet"

    if not emp_assign_path.exists():
        raise FileNotFoundError(
            f"employee_store_assignments.parquet is required: {emp_assign_path}. "
            f"Run dimension generation first."
        )

    salesperson_roles = _cfg_get(cfg, ["sales", "salesperson_roles"], default=None)
    if not (isinstance(salesperson_roles, list) and salesperson_roles):
        primary = _cfg_get(cfg, ["employees", "store_assignments", "primary_sales_role"], default="Sales Associate")
        salesperson_roles = [str(primary), ONLINE_SALES_REP_ROLE]

    _esa_base_cols = [
        "EmployeeKey", "StoreKey", "StartDate", "EndDate",
        "FTE", "RoleAtStore",
    ]
    try:
        emp_assign_df = load_parquet_df(
            emp_assign_path, cols=_esa_base_cols + ["IsPrimary"],
        )
    except (KeyError, ValueError):
        emp_assign_df = load_parquet_df(emp_assign_path, cols=_esa_base_cols)

    if "RoleAtStore" in emp_assign_df.columns:
        emp_assign_df = emp_assign_df[emp_assign_df["RoleAtStore"].isin(salesperson_roles)].copy()

    if emp_assign_df.empty:
        raise SalesError(
            f"No employee assignments with role in {salesperson_roles} found in "
            f"{emp_assign_path}. Check employees.store_assignments.primary_sales_role "
            f"and ensure the bridge has been regenerated."
        )

    end_dt = pd.to_datetime(end_date, errors="coerce").normalize()

    start_dt = pd.to_datetime(emp_assign_df["StartDate"], errors="coerce").dt.normalize()
    end_dt_col = pd.to_datetime(emp_assign_df["EndDate"], errors="coerce").dt.normalize()
    end_dt_col = end_dt_col.fillna(end_dt)

    result = {
        "employee_assign_store_key": _as_np(emp_assign_df["StoreKey"], np.int32),
        "employee_assign_employee_key": _as_np(emp_assign_df["EmployeeKey"], np.int32),
        "employee_assign_start_date": _as_np(start_dt, "datetime64[D]"),
        "employee_assign_end_date": _as_np(end_dt_col, "datetime64[D]"),
        "employee_assign_role": _as_np(emp_assign_df["RoleAtStore"].astype(str)),
        "employee_assign_fte": None,
        "employee_assign_is_primary": None,
        "salesperson_roles": salesperson_roles,
    }
    if "FTE" in emp_assign_df.columns:
        result["employee_assign_fte"] = _as_np(emp_assign_df["FTE"], np.float64)
    if "IsPrimary" in emp_assign_df.columns:
        result["employee_assign_is_primary"] = _as_np(emp_assign_df["IsPrimary"], bool)
    return result
