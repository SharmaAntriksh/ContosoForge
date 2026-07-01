"""Column-correlation lookups and pre-built shared structures for the sales fact.

``_build_correlation_lookups`` builds the per-worker correlation lookup arrays
(geography, store types, product channel eligibility, promotions, fulfillment
days). ``_prebuild_shared_structures`` builds expensive derived structures in the
main process for shared-memory broadcast.

NOTE: ``_prebuild_shared_structures``'s imports from ``.sales_worker.init`` stay
FUNCTION-LOCAL (that module imports ``sales.py``, so hoisting them to module level
would create an import-time cycle).
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from src.defaults import (
    ALL_CHANNELS,
    CHANNEL_TO_ELIG_GROUP,
    DEFAULT_CHANNEL_FULFILLMENT_DAYS,
    DEFAULT_CHANNEL_MAP,
    STORE_TYPE_CHANNEL_MAP,
)

from .sales_helpers import load_parquet_df


def _build_correlation_lookups(
    parquet_folder_p, store_keys, store_to_geo, store_type_map,
    product_np, promo_keys_all, promo_df,
):
    """Build all column-correlation lookup arrays for workers."""
    # 1) Geography: customer -> country, store -> country, country -> stores
    _geo_country_df = load_parquet_df(parquet_folder_p / "geography.parquet", ["GeographyKey", "Country"])
    _unique_countries = _geo_country_df["Country"].fillna("Unknown").unique()
    _country_to_id = {c: i for i, c in enumerate(_unique_countries)}
    _n_countries = len(_unique_countries)

    _max_geo = int(_geo_country_df["GeographyKey"].max()) if len(_geo_country_df) else 0
    geo_to_country_id = np.full(_max_geo + 1, 0, dtype=np.int32)
    _geo_keys = _geo_country_df["GeographyKey"].to_numpy(dtype=np.int32)
    _geo_countries = _geo_country_df["Country"].fillna("Unknown").map(_country_to_id).to_numpy(dtype=np.int32)
    geo_to_country_id[_geo_keys] = _geo_countries

    _max_sk = int(store_keys.max()) if store_keys.size else 0
    store_to_country_id = np.full(_max_sk + 1, 0, dtype=np.int32)
    _sg_sk = np.fromiter(store_to_geo.keys(), dtype=np.int32, count=len(store_to_geo))
    _sg_gk = np.fromiter(store_to_geo.values(), dtype=np.int32, count=len(store_to_geo))
    _sg_valid = (_sg_sk <= _max_sk) & (_sg_gk <= _max_geo)
    store_to_country_id[_sg_sk[_sg_valid]] = geo_to_country_id[_sg_gk[_sg_valid]]

    _sk_country_ids = store_to_country_id[store_keys.astype(np.int32)]
    country_to_store_keys = [
        store_keys[_sk_country_ids == cid].astype(np.int32)
        for cid in range(_n_countries)
    ]

    # 2) Store type -> valid ChannelKeys
    store_channel_keys_list = [None] * (_max_sk + 1)
    channel_prob_by_store_list = [None] * (_max_sk + 1)
    if store_type_map is not None:
        for sk in store_keys:
            sk_int = int(sk)
            st = store_type_map.get(sk_int, "")
            keys, probs = STORE_TYPE_CHANNEL_MAP.get(st, DEFAULT_CHANNEL_MAP)
            store_channel_keys_list[sk_int] = keys
            channel_prob_by_store_list[sk_int] = probs / probs.sum()
    else:
        _uniform_p = np.ones(len(ALL_CHANNELS), dtype=np.float64) / len(ALL_CHANNELS)
        for sk in store_keys:
            store_channel_keys_list[int(sk)] = ALL_CHANNELS
            channel_prob_by_store_list[int(sk)] = _uniform_p

    # 3) Product channel eligibility (from ProductProfile)
    product_channel_eligible = None
    _profile_path = parquet_folder_p / "product_profile.parquet"
    if _profile_path.exists():
        try:
            _elig_cols = ["ProductKey", "EligibleStore", "EligibleOnline", "EligibleMarketplace", "EligibleB2B"]
            _elig_df = pd.read_parquet(str(_profile_path), columns=_elig_cols)
            _prod_keys_arr = product_np[:, 0].astype(np.int32)
            _max_pk = int(_prod_keys_arr.max()) if _prod_keys_arr.size else 0
            _pk_to_row = np.full(_max_pk + 1, -1, dtype=np.int32)
            _pk_to_row[_prod_keys_arr] = np.arange(len(_prod_keys_arr), dtype=np.int32)
            product_channel_eligible = np.ones((len(product_np), 4), dtype=np.int8)
            _elig_pks = _elig_df["ProductKey"].to_numpy(dtype=np.int32)
            _elig_mask = (_elig_pks <= _max_pk)
            _elig_pks_valid = _elig_pks[_elig_mask]
            _elig_rows = _pk_to_row[_elig_pks_valid]
            _mapped = _elig_rows >= 0
            _ri = _elig_rows[_mapped]
            _elig_mask_idx = np.where(_elig_mask)[0][_mapped]
            for col_idx, col_name in enumerate(["EligibleStore", "EligibleOnline",
                                                 "EligibleMarketplace", "EligibleB2B"]):
                product_channel_eligible[_ri, col_idx] = (
                    _elig_df[col_name].to_numpy(dtype=np.int8)[_elig_mask_idx]
                )
        except (KeyError, OSError):
            pass

    # 4) Promotion channel group
    promo_channel_group = np.zeros(len(promo_keys_all), dtype=np.int8)
    if not promo_df.empty and "PromotionCategory" in promo_df.columns:
        _cat_series = promo_df["PromotionCategory"].astype(str)
        promo_channel_group[_cat_series.isin({"Store", "Physical"}).to_numpy()] = 1
        promo_channel_group[_cat_series.isin({"Online", "Digital"}).to_numpy()] = 2

    # 5) Channel fulfillment days
    channel_fulfillment_days = DEFAULT_CHANNEL_FULFILLMENT_DAYS.copy()
    _sc_path = parquet_folder_p / "channels.parquet"
    if _sc_path.exists():
        try:
            _sc_df = pd.read_parquet(str(_sc_path))
            if "TypicalFulfillmentDays" in _sc_df.columns and "ChannelKey" in _sc_df.columns:
                _sc_keys = _sc_df["ChannelKey"].to_numpy(dtype=np.int32)
                _sc_days = _sc_df["TypicalFulfillmentDays"]
                _sc_valid = (_sc_keys >= 0) & (_sc_keys < len(channel_fulfillment_days)) & _sc_days.notna()
                channel_fulfillment_days[_sc_keys[_sc_valid]] = _sc_days.to_numpy(dtype=np.int32)[_sc_valid]
        except (KeyError, OSError):
            pass

    return {
        "geo_to_country_id": geo_to_country_id,
        "store_to_country_id": store_to_country_id,
        "country_to_store_keys": country_to_store_keys,
        "store_channel_keys": store_channel_keys_list,
        "channel_prob_by_store": channel_prob_by_store_list,
        "product_channel_eligible": product_channel_eligible,
        "promo_channel_group": promo_channel_group,
        "channel_fulfillment_days": channel_fulfillment_days,
        "_channel_to_elig_group": CHANNEL_TO_ELIG_GROUP,
    }


def _prebuild_shared_structures(
    worker_cfg, _shm, prod, stores, emps, seed,
):
    """Pre-build expensive derived structures in main process for shared memory."""
    product_brand_key = prod["product_brand_key"]
    store_keys = stores["store_keys"]
    store_type_map = stores["store_type_map"]
    product_subcat_key = prod["product_subcat_key"]
    assortment_cfg = prod["assortment_cfg"]
    date_pool = prod["date_pool"]
    employee_assign_store_key = emps["employee_assign_store_key"]
    employee_assign_employee_key = emps["employee_assign_employee_key"]
    employee_assign_start_date = emps["employee_assign_start_date"]
    employee_assign_end_date = emps["employee_assign_end_date"]
    employee_assign_fte = emps["employee_assign_fte"]
    employee_assign_is_primary = emps["employee_assign_is_primary"]
    from .sales_worker.init import (
        _build_store_subcat_matrix,
        _build_brand_prob_by_month_rotate_winner,
        _build_salesperson_effective_by_store,
        _DEFAULT_ASSORTMENT_COVERAGE,
        infer_T_from_date_pool,
        int_or,
        float_or,
    )

    # 1) brand_to_row_idx — sorted index + offsets for zero-copy brand buckets
    _brand_product_counts = None
    if product_brand_key is not None:
        _bk = np.asarray(product_brand_key, dtype=np.int32)
        _brand_product_counts = np.bincount(_bk).astype(np.float64)
        _brand_order = np.argsort(_bk, kind="mergesort").astype(np.int32)
        _bk_sorted = _bk[_brand_order]
        _brand_starts = np.flatnonzero(np.r_[True, _bk_sorted[1:] != _bk_sorted[:-1]])
        B = int(_bk.max()) + 1
        _brand_offsets = np.zeros(B + 1, dtype=np.int64)
        for s_idx in range(len(_brand_starts)):
            k = int(_bk_sorted[_brand_starts[s_idx]])
            e = int(_brand_starts[s_idx + 1]) if s_idx + 1 < len(_brand_starts) else len(_bk_sorted)
            _brand_offsets[k + 1] = e
        np.maximum.accumulate(_brand_offsets, out=_brand_offsets)
        worker_cfg["_brand_flat_idx"] = _shm.publish("brand_flat_idx", _brand_order)
        worker_cfg["_brand_flat_offsets"] = _shm.publish("brand_flat_off", _brand_offsets)
        del _brand_order, _bk_sorted, _brand_starts, _brand_offsets

    # 2) store-product assortment — compact subcat matrix
    if assortment_cfg.get("enabled") and product_subcat_key is not None and store_type_map is not None:
        store_type_arr = np.array(
            [str(store_type_map.get(int(sk), "Supermarket")) for sk in store_keys],
            dtype=object,
        )
        coverage = assortment_cfg.get("coverage", _DEFAULT_ASSORTMENT_COVERAGE)
        assort_seed = int(assortment_cfg.get("seed", seed))
        _unique_subcats, _subcat_matrix = _build_store_subcat_matrix(
            store_keys=store_keys,
            store_type_arr=store_type_arr,
            product_subcat_key=product_subcat_key,
            coverage_cfg=coverage,
            seed=assort_seed,
        )
        worker_cfg["_assortment_subcat_matrix"] = _shm.publish(
            "assort_matrix", _subcat_matrix,
        )
        worker_cfg["_assortment_unique_subcats"] = _shm.publish(
            "assort_subcats", _unique_subcats,
        )
        del _subcat_matrix, _unique_subcats

    # 3) salesperson_effective_by_store
    if employee_assign_employee_key is not None and employee_assign_store_key is not None:
        _sp_perf_spread = float_or(worker_cfg.get("salesperson_perf_spread"), 0.0)
        _sp_perf_seed = int_or(worker_cfg.get("salesperson_perf_seed"), 0)
        _sp_eff = _build_salesperson_effective_by_store(
            store_keys=store_keys,
            assign_store=employee_assign_store_key,
            assign_emp=employee_assign_employee_key,
            assign_start=employee_assign_start_date,
            assign_end=employee_assign_end_date,
            assign_fte=employee_assign_fte,
            assign_is_primary=employee_assign_is_primary,
            primary_boost=2.0,
            perf_spread=_sp_perf_spread,
            perf_seed=_sp_perf_seed,
        )
        worker_cfg["_prebuilt_salesperson_effective_by_store"] = _sp_eff
        if _sp_eff is not None:
            _all_sp = np.concatenate([v[0] for v in _sp_eff.values()])
            worker_cfg["_prebuilt_salesperson_global_pool"] = np.unique(_all_sp).astype(np.int32)
        del _sp_eff

    # 4) brand_prob_by_month
    models_cfg = worker_cfg.get("models_cfg")
    if isinstance(models_cfg, Mapping):
        _brand_cfg = models_cfg.get("brand_popularity") if isinstance(models_cfg, Mapping) else None
        if _brand_cfg and product_brand_key is not None and product_brand_key.size > 0:
            _T = infer_T_from_date_pool(date_pool)
            _B = int(product_brand_key.max()) + 1
            _rng_bp = np.random.default_rng(int(int_or(_brand_cfg.get("seed"), 1234)))

            _bp_counts = _brand_product_counts if (_brand_product_counts is not None and len(_brand_product_counts) == _B) else None

            _brand_prob = _build_brand_prob_by_month_rotate_winner(
                _rng_bp,
                T=_T, B=_B,
                winner_boost=float_or(_brand_cfg.get("winner_boost"), 2.5),
                noise_sd=float_or(_brand_cfg.get("noise_sd"), 0.15),
                min_share=float_or(_brand_cfg.get("min_share"), 0.02),
                year_len_months=int_or(_brand_cfg.get("year_len_months"), 12),
                brand_product_counts=_bp_counts,
                count_exponent=float_or(_brand_cfg.get("count_exponent"), 0.25),
            )
            worker_cfg["_prebuilt_brand_prob_by_month"] = _shm.publish(
                "brand_prob", _brand_prob,
            )
            del _brand_prob
