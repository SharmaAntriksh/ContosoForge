from __future__ import annotations

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.dimensions.employees.assembly import (
    resolve_store_hierarchy,
    build_employee_rows,
    assign_employment_dates_status,
    finalize_employees,
)

__all__ = [
    "generate_employee_dimension",
]


def generate_employee_dimension(
    *,
    stores: pd.DataFrame,
    seed: int,
    global_start: pd.Timestamp,
    global_end: pd.Timestamp,
    people_pools=None,
    iso_by_geo: dict[int, str] | None = None,
    default_region: str = "US",
    primary_sales_role: str = "Sales Associate",
    min_primary_sales_per_store: int = 1,
    store_manager_names: dict[int, str] | None = None,
    store_opening_dates: dict[int, pd.Timestamp] | None = None,
    store_closing_dates: dict[int, pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """
    Build a parent-child employee hierarchy with stable keys.

    Static model: all employees are hired before global_start and remain
    active (IsActive=True, TerminationDate=NaT) for the full window, except
    those at stores that close (ClosingDate triggers termination).
    """
    if stores.empty:
        raise DimensionError("stores dataframe is empty; cannot generate employees")

    required_cols = {"StoreKey", "GeographyKey", "EmployeeCount", "StoreType"}
    missing = [c for c in required_cols if c not in stores.columns]
    if missing:
        raise DimensionError(f"stores.parquet missing required columns: {missing}")

    stores = stores.copy()
    stores["StoreKey"] = stores["StoreKey"].astype(np.int32)

    rng = np.random.default_rng(int(seed))

    n_stores = len(stores)

    stores, district_id, region_id = resolve_store_hierarchy(stores, n_stores)
    df = build_employee_rows(
        stores,
        rng,
        district_id=district_id,
        region_id=region_id,
        primary_sales_role=primary_sales_role,
        min_primary_sales_per_store=min_primary_sales_per_store,
    )
    assign_employment_dates_status(
        df,
        rng,
        primary_sales_role=primary_sales_role,
        global_start=global_start,
        global_end=global_end,
        store_opening_dates=store_opening_dates,
        store_closing_dates=store_closing_dates,
    )
    finalize_employees(
        df,
        seed=int(seed),
        people_pools=people_pools,
        iso_by_geo=iso_by_geo,
        default_region=default_region,
        store_manager_names=store_manager_names,
    )

    return df
