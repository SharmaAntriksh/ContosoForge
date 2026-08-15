"""Typed dimension pools for the sales fact.

Frozen dataclasses that replace the loose ``dict`` returns of the dimension
loaders in ``dimension_loaders.py``. They carry exactly the same arrays/objects
(same values, same dtypes) — the point is a typed, attribute-accessed contract
between the loaders and their consumers (``worker_cfg_builder`` /
``correlation_lookups``) instead of stringly-keyed dicts.

The per-month plan arrays the coordinator used to inject into the customer dict
now live in their own ``MonthPlan`` holder (``run_config.py``), so
``CustomerPool`` carries only the loaded customer-dimension arrays and can be
frozen like the rest.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass(frozen=True)
class PromoPool:
    """Promotion arrays for the sales pool (from ``_load_promotions``)."""

    promo_df: Any                       # pandas DataFrame (row-aligned with keys)
    promo_keys_all: np.ndarray
    promo_start_all: np.ndarray
    promo_end_all: np.ndarray
    promo_discount_pct_all: np.ndarray
    promo_audience_group: np.ndarray
    new_customer_promo_keys: np.ndarray


@dataclass(frozen=True)
class StorePool:
    """Store arrays + geo/currency maps for the sales pool (from ``_load_stores``)."""

    store_keys: np.ndarray
    store_to_geo: Dict[int, int]
    store_open_month: Optional[np.ndarray]
    store_close_month: Optional[np.ndarray]
    store_open_day: Optional[np.ndarray]
    store_close_day: Optional[np.ndarray]
    store_reno_start_day: Optional[np.ndarray]
    store_reno_end_day: Optional[np.ndarray]
    store_type_map: Optional[Dict[int, str]]
    store_demand_weight: Optional[np.ndarray]
    geo_to_currency: Dict[int, int]


@dataclass(frozen=True)
class EmployeeBridge:
    """Employee store-assignment arrays for salesperson resolution (``_load_employees``)."""

    employee_assign_store_key: np.ndarray
    employee_assign_employee_key: np.ndarray
    employee_assign_start_date: np.ndarray
    employee_assign_end_date: np.ndarray
    employee_assign_role: np.ndarray
    employee_assign_fte: Optional[np.ndarray]
    employee_assign_is_primary: Optional[np.ndarray]
    salesperson_roles: List[str]


@dataclass(frozen=True)
class CustomerPool:
    """Customer dimension arrays for the sales pool (from ``_load_customers``).

    Carries only the loaded arrays; the coordinator-computed per-month plan
    (discovery month + row/order/distinct curves) is a separate ``MonthPlan``.
    """

    customer_keys: np.ndarray
    customer_start_month: np.ndarray
    customer_end_month: np.ndarray
    is_active_in_sales: np.ndarray
    customer_base_weight: Optional[np.ndarray]
    customer_geo_key: Optional[np.ndarray]
    customer_first_eff_start_by_key: Optional[np.ndarray]
    customer_last_eff_end_by_key: Optional[np.ndarray]
    customer_loyalty_tier_by_key: Optional[np.ndarray]
    customer_income_score_by_key: Optional[np.ndarray]
    customer_is_organization_by_key: Optional[np.ndarray]
    customer_org_scale_score_by_key: Optional[np.ndarray]
    customer_org_procurement_score_by_key: Optional[np.ndarray]
    customer_org_preferred_category_mask_by_key: Optional[np.ndarray]
    customer_scd2_active: bool
    customer_scd2_starts: Optional[np.ndarray]
    customer_scd2_keys: Optional[np.ndarray]
    cust_key_to_pool_idx: Optional[np.ndarray]


@dataclass(frozen=True)
class ProductPool:
    """Product arrays, profile, date pool, and SCD2 versions (from ``_load_products``)."""

    product_np: np.ndarray
    product_brand_key: Optional[np.ndarray]
    brand_names: Optional[np.ndarray]
    product_subcat_key: Optional[np.ndarray]
    product_popularity: Optional[np.ndarray]
    product_seasonality: Optional[np.ndarray]
    product_return_risk_by_key: Optional[np.ndarray]
    product_defect_risk_by_key: Optional[np.ndarray]
    product_price_score: Optional[np.ndarray]
    product_category_key: Optional[np.ndarray]
    date_pool: np.ndarray
    date_prob: np.ndarray
    product_scd2_active: bool
    product_scd2_starts: Optional[np.ndarray]
    product_scd2_data: Optional[np.ndarray]
    assortment_cfg: dict
