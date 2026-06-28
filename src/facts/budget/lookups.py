"""Budget dimension lookups (worker-side).

Built once per worker process during init_sales_worker.
Stored on State so micro_aggregate() can resolve keys without disk I/O.

Memory footprint: ~few hundred KB (dense arrays sized to max key).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


def build_budget_lookups(parquet_dims: Path) -> dict:
    """
    Build dense lookup arrays for budget aggregation.

    Returns dict to be bound onto State:
        budget_store_to_country:  int64[max_store_key+1]  -> country_id
        budget_product_to_cat:    int64[max_product_key+1] -> category_id
        budget_country_labels:    str[num_countries]
        budget_category_labels:   str[num_categories]
        budget_channel_keys:      int16[] (distinct ChannelKey values)
    """
    # ---- Store -> Geography -> Country ----
    import pyarrow.parquet as pq
    _store_cols = ["StoreKey", "GeographyKey"]
    try:
        _store_schema = set(pq.read_schema(str(parquet_dims / "stores.parquet")).names)
    except (OSError, ValueError):
        _store_schema = set(pd.read_parquet(parquet_dims / "stores.parquet", columns=[]).columns)
    if "IsCurrent" in _store_schema:
        _store_cols.append("IsCurrent")
    stores = pd.read_parquet(parquet_dims / "stores.parquet", columns=_store_cols)
    if "IsCurrent" in stores.columns:
        stores = stores[stores["IsCurrent"] == 1].drop(columns=["IsCurrent"])

    geo = pd.read_parquet(parquet_dims / "geography.parquet",
                          columns=["GeographyKey", "Country", "ISOCode"])

    store_geo = stores.merge(geo, on="GeographyKey", how="left")

    # Encode countries as dense integer ids
    countries = store_geo["Country"].fillna("Unknown").unique()
    country_to_id = {c: i for i, c in enumerate(countries)}

    if store_geo.empty:
        raise ValueError("No stores found after geography merge — cannot build budget lookups")
    max_store = int(store_geo["StoreKey"].max())
    store_to_country = np.full(max_store + 1, -1, dtype=np.int32)
    _sg_sk = store_geo["StoreKey"].to_numpy(dtype=np.intp)
    _sg_cid = store_geo["Country"].fillna("Unknown").map(country_to_id).to_numpy(dtype=np.int32)
    store_to_country[_sg_sk] = _sg_cid

    # ---- Product -> Subcategory -> Category ----
    _prod_cols = ["ProductKey", "SubcategoryKey"]
    try:
        import pyarrow.parquet as pq
        _prod_schema = set(pq.read_schema(str(parquet_dims / "products.parquet")).names)
    except (OSError, ValueError):
        _prod_schema = set()
    if "IsCurrent" in _prod_schema:
        _prod_cols.append("IsCurrent")
    products = pd.read_parquet(parquet_dims / "products.parquet", columns=_prod_cols)
    if "IsCurrent" in products.columns:
        products = products[products["IsCurrent"] == 1].drop(columns=["IsCurrent"])
    subcat = pd.read_parquet(parquet_dims / "product_subcategory.parquet",
                             columns=["SubcategoryKey", "CategoryKey"])
    cat = pd.read_parquet(parquet_dims / "product_category.parquet",
                          columns=["CategoryKey", "Category"])

    prod_cat = (products
                .merge(subcat, on="SubcategoryKey", how="left")
                .merge(cat, on="CategoryKey", how="left"))

    categories = prod_cat["Category"].fillna("Unknown").unique()
    category_to_id = {c: i for i, c in enumerate(categories)}

    if prod_cat.empty:
        raise ValueError("No products found after category merge — cannot build budget lookups")
    max_prod = int(prod_cat["ProductKey"].max())
    product_to_cat = np.full(max_prod + 1, -1, dtype=np.int32)
    _pc_pk = prod_cat["ProductKey"].to_numpy(dtype=np.intp)
    _pc_cid = prod_cat["Category"].fillna("Unknown").map(category_to_id).to_numpy(dtype=np.int32)
    product_to_cat[_pc_pk] = _pc_cid

    # ---- Country -> Currency (for FX layer) ----
    _geo_dedup = geo.drop_duplicates("Country")
    _geo_countries = _geo_dedup["Country"].to_numpy()
    _geo_iso = _geo_dedup["ISOCode"].fillna("USD").to_numpy() if "ISOCode" in _geo_dedup.columns else np.full(len(_geo_dedup), "USD", dtype=object)
    country_to_currency = {}
    for _gc, _gi in zip(_geo_countries, _geo_iso):
        cid = country_to_id.get(_gc)
        if cid is not None:
            country_to_currency[cid] = _gi

    # ---- Sales channels ----
    sc_path = parquet_dims / "channels.parquet"
    if sc_path.exists():
        sc = pd.read_parquet(sc_path)
        channel_keys = sc["ChannelKey"].to_numpy(dtype=np.int32)
        is_digital = sc.get("IsDigital", pd.Series(dtype="int8")).to_numpy()
        is_physical = sc.get("IsPhysical", pd.Series(dtype="int8")).to_numpy()
    else:
        channel_keys = np.arange(1, 6, dtype=np.int32)
        is_digital = np.zeros(5, dtype=np.int8)
        is_physical = np.zeros(5, dtype=np.int8)

    return {
        "budget_store_to_country": store_to_country,
        "budget_product_to_cat": product_to_cat,
        "budget_country_labels": np.array(countries, dtype=object),
        "budget_category_labels": np.array(categories, dtype=object),
        "budget_country_to_currency": country_to_currency,
        "budget_channel_keys": channel_keys,
        "budget_channel_is_digital": is_digital,
        "budget_channel_is_physical": is_physical,
    }
