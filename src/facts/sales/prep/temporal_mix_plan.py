"""Coordinator-side construction of the compact temporal mix plan."""
from __future__ import annotations

import numpy as np

from src.exceptions import SalesError
from ..sales_models.temporal_mix import build_temporal_factors
from .run_config import ProductTemporalMixPlan, TemporalMixPlan


TEMPORAL_MIX_LAYER_INTENSITY = {
    "customer_country": 0.95,
    "customer_gender": 0.55,
    "product_category": 1.10,
    "product_brand_within_category": 0.60,
    "product_color_within_category": 0.60,
}


def _positive_customer_mass(base_weight, customer_count: int) -> np.ndarray:
    if base_weight is None:
        return np.ones(customer_count, dtype=np.float64)
    mass = np.asarray(base_weight, dtype=np.float64)
    if mass.ndim != 1 or mass.size != customer_count:
        raise SalesError("customer_base_weight must align with the customer pool")
    return np.where(np.isfinite(mass) & (mass > 0.0), mass, 1e-12)


def _dense_geography_values(cust) -> np.ndarray:
    if cust.customer_geo_key is None:
        raise SalesError("Customer temporal mix requires GeographyKey")
    keys = np.asarray(cust.customer_keys, dtype=np.int64)
    lookup = np.asarray(cust.customer_geo_key, dtype=np.int32)
    if keys.size and (keys.min() < 0 or keys.max() >= lookup.size):
        raise SalesError("customer_geo_key does not cover every customer pool key")
    return lookup[keys]


def build_customer_temporal_mix_plan(
    cust,
    correlations,
    *,
    month_count: int,
    resolved_trend,
    run_seed: int,
) -> TemporalMixPlan:
    """Build country/gender factors and compact customer mappings once."""
    customer_count = int(np.asarray(cust.customer_keys).size)
    if customer_count <= 0:
        raise SalesError("Customer temporal mix requires a non-empty customer pool")
    base_mass = _positive_customer_mass(cust.customer_base_weight, customer_count)

    geo_mapping = correlations.get("geo_to_country_id")
    country_labels = correlations.get("country_names")
    if geo_mapping is None or country_labels is None:
        raise SalesError("Customer temporal mix requires geography country mappings")
    geo_to_original_country = np.asarray(geo_mapping, dtype=np.int32)
    country_names = np.asarray(country_labels, dtype=object)
    if (
        geo_to_original_country.ndim != 1
        or country_names.ndim != 1
        or country_names.size == 0
    ):
        raise SalesError("Customer temporal mix requires geography country mappings")

    current_geography = _dense_geography_values(cust)
    all_geography = current_geography
    if cust.customer_scd2_geography is not None:
        all_geography = np.concatenate(
            (
                all_geography,
                np.asarray(cust.customer_scd2_geography, dtype=np.int32).ravel(),
            )
        )
    valid_geography = all_geography[
        (all_geography >= 0) & (all_geography < geo_to_original_country.size)
    ]
    if valid_geography.size == 0:
        raise SalesError("No customer GeographyKey maps to a country")
    observed_original_ids = np.unique(geo_to_original_country[valid_geography])
    if np.any(
        (observed_original_ids < 0) | (observed_original_ids >= country_names.size)
    ):
        raise SalesError("geo_to_country_id contains an invalid country id")
    observed_original_ids = np.asarray(
        sorted(
            observed_original_ids.tolist(),
            key=lambda value: (str(country_names[value]), int(value)),
        ),
        dtype=np.int32,
    )

    original_to_temporal = np.full(country_names.size, -1, dtype=np.int32)
    original_to_temporal[observed_original_ids] = np.arange(
        observed_original_ids.size, dtype=np.int32,
    )
    geo_to_customer_country_id = np.full(
        geo_to_original_country.size, -1, dtype=np.int32,
    )
    valid_original = (
        (geo_to_original_country >= 0)
        & (geo_to_original_country < original_to_temporal.size)
    )
    geo_to_customer_country_id[valid_original] = original_to_temporal[
        geo_to_original_country[valid_original]
    ]
    if np.any(
        (current_geography < 0)
        | (current_geography >= geo_to_customer_country_id.size)
    ):
        raise SalesError("Customer GeographyKey is outside the geography lookup")
    customer_country_id_by_pool = geo_to_customer_country_id[current_geography]
    if np.any(customer_country_id_by_pool < 0):
        raise SalesError("A customer country is missing from the temporal mapping")

    country_mass = np.bincount(
        customer_country_id_by_pool,
        weights=base_mass.astype(np.float64, copy=False),
        minlength=observed_original_ids.size,
    ).astype(np.float64, copy=False)
    if cust.customer_scd2_geography is not None and np.any(country_mass <= 0.0):
        history_geo = np.asarray(cust.customer_scd2_geography, dtype=np.int32)
        if np.any((history_geo < 0) | (history_geo >= geo_to_customer_country_id.size)):
            raise SalesError("Customer SCD2 GeographyKey is outside the geography lookup")
        history_country = geo_to_customer_country_id[history_geo]
        for country_id in np.flatnonzero(country_mass <= 0.0):
            customer_mask = np.any(history_country == country_id, axis=1)
            country_mass[country_id] = float(base_mass[customer_mask].sum())
    if np.any(country_mass <= 0.0):
        raise SalesError("Every temporal country group must have positive customer mass")

    gender_labels = cust.customer_gender_by_pool
    if gender_labels is None:
        gender_labels = np.full(customer_count, "Unknown", dtype=object)
    gender_labels = np.asarray([str(value) for value in gender_labels], dtype=object)
    if gender_labels.size != customer_count:
        raise SalesError("customer gender mapping must align with the customer pool")
    gender_names = np.asarray(sorted(set(gender_labels.tolist())), dtype=object)
    gender_to_id = {name: index for index, name in enumerate(gender_names.tolist())}
    customer_gender_id_by_pool = np.asarray(
        [gender_to_id[value] for value in gender_labels], dtype=np.int32,
    )
    gender_mass = np.bincount(
        customer_gender_id_by_pool,
        weights=base_mass.astype(np.float64, copy=False),
        minlength=gender_names.size,
    ).astype(np.float64, copy=False)

    country_group_names = [str(country_names[value]) for value in observed_original_ids]
    country_factors = build_temporal_factors(
        "customer-country",
        country_group_names,
        country_mass,
        int(month_count),
        resolved_trend,
        int(run_seed),
        intensity_scale=TEMPORAL_MIX_LAYER_INTENSITY["customer_country"],
    )
    gender_factors = build_temporal_factors(
        "customer-gender",
        gender_names.tolist(),
        gender_mass,
        int(month_count),
        resolved_trend,
        int(run_seed),
        intensity_scale=TEMPORAL_MIX_LAYER_INTENSITY["customer_gender"],
    )
    return TemporalMixPlan(
        customer_country_id_by_pool=customer_country_id_by_pool,
        customer_gender_id_by_pool=customer_gender_id_by_pool,
        geo_to_customer_country_id=geo_to_customer_country_id,
        customer_country_factors=country_factors,
        customer_gender_factors=gender_factors,
    )


def _product_mass(prod, product_count: int) -> np.ndarray:
    popularity = prod.product_popularity
    if popularity is None:
        return np.ones(product_count, dtype=np.float64)
    mass = np.asarray(popularity, dtype=np.float64)
    if mass.ndim != 1 or mass.size != product_count:
        raise SalesError("product_popularity must align with the product pool")
    return np.where(np.isfinite(mass) & (mass > 0.0), np.maximum(mass, 1.0), 1.0)


def _product_labels(values, product_count: int, field_name: str) -> np.ndarray:
    if values is None:
        return np.full(product_count, "Unknown", dtype=object)
    labels = np.asarray([str(value) for value in values], dtype=object)
    if labels.ndim != 1 or labels.size != product_count:
        raise SalesError(f"{field_name} must align with the product pool")
    return labels


def _compound_group_ids(
    category_keys: np.ndarray,
    labels: np.ndarray,
    mass: np.ndarray,
) -> tuple[np.ndarray, list[str], np.ndarray, list[str]]:
    pairs = [(int(category), str(label)) for category, label in zip(category_keys, labels)]
    unique_pairs = sorted(set(pairs), key=lambda value: (value[0], value[1]))
    pair_to_id = {value: index for index, value in enumerate(unique_pairs)}
    ids = np.asarray([pair_to_id[value] for value in pairs], dtype=np.int32)
    group_mass = np.bincount(
        ids,
        weights=mass.astype(np.float64, copy=False),
        minlength=len(unique_pairs),
    ).astype(np.float64, copy=False)
    group_names = [repr(value) for value in unique_pairs]
    parents = [f"CategoryKey={value[0]}" for value in unique_pairs]
    return ids, group_names, group_mass, parents


def build_product_temporal_mix_plan(
    prod,
    *,
    month_count: int,
    resolved_trend,
    run_seed: int,
) -> ProductTemporalMixPlan:
    """Build category plus within-category brand/color factor layers once."""
    product_count = int(np.asarray(prod.product_np).shape[0])
    if product_count <= 0:
        raise SalesError("Product temporal mix requires a non-empty product pool")
    mass = _product_mass(prod, product_count)

    if prod.product_category_key is None:
        category_keys = np.zeros(product_count, dtype=np.int32)
    else:
        category_keys = np.asarray(prod.product_category_key, dtype=np.int32)
        if category_keys.ndim != 1 or category_keys.size != product_count:
            raise SalesError("product_category_key must align with the product pool")

    unique_categories = sorted(set(category_keys.tolist()))
    category_to_id = {
        category: index for index, category in enumerate(unique_categories)
    }
    category_ids = np.asarray(
        [category_to_id[int(value)] for value in category_keys], dtype=np.int32,
    )
    category_mass = np.bincount(
        category_ids,
        weights=mass.astype(np.float64, copy=False),
        minlength=len(unique_categories),
    ).astype(np.float64, copy=False)
    category_names = [f"CategoryKey={value}" for value in unique_categories]

    brand_labels = _product_labels(
        prod.product_brand_by_pool, product_count, "product_brand_by_pool",
    )
    color_labels = _product_labels(
        prod.product_color_by_pool, product_count, "product_color_by_pool",
    )
    brand_ids, brand_names, brand_mass, brand_parents = _compound_group_ids(
        category_keys, brand_labels, mass,
    )
    color_ids, color_names, color_mass, color_parents = _compound_group_ids(
        category_keys, color_labels, mass,
    )

    category_factors = build_temporal_factors(
        "product-category",
        category_names,
        category_mass,
        int(month_count),
        resolved_trend,
        int(run_seed),
        intensity_scale=TEMPORAL_MIX_LAYER_INTENSITY["product_category"],
    )
    brand_factors = build_temporal_factors(
        "product-brand-within-category",
        brand_names,
        brand_mass,
        int(month_count),
        resolved_trend,
        int(run_seed),
        parent_group=brand_parents,
        intensity_scale=TEMPORAL_MIX_LAYER_INTENSITY[
            "product_brand_within_category"
        ],
    )
    color_factors = build_temporal_factors(
        "product-color-within-category",
        color_names,
        color_mass,
        int(month_count),
        resolved_trend,
        int(run_seed),
        parent_group=color_parents,
        intensity_scale=TEMPORAL_MIX_LAYER_INTENSITY[
            "product_color_within_category"
        ],
    )
    return ProductTemporalMixPlan(
        product_category_id_by_pool=category_ids,
        product_brand_id_by_pool=brand_ids,
        product_color_id_by_pool=color_ids,
        product_category_factors=category_factors,
        product_brand_factors=brand_factors,
        product_color_factors=color_factors,
    )
