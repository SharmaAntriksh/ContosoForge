"""Employee dimension runner.

Config resolution, versioning, geography/people-pool loading, parquet IO, and
the two-RNG orchestration around :func:`generate_employee_dimension` and the HR
enrichment. ``run_employees`` is the pipeline entrypoint.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Tuple

import numpy as np
import pandas as pd

from src.utils.logging_utils import debug, skip, stage, warn
from src.utils.output_utils import write_parquet_with_date32
from src.versioning import should_regenerate, save_version, frame_content_hash
from src.utils.name_pools import load_people_pools, resolve_people_folder
from src.utils.config_helpers import as_dict, parse_global_dates
from src.utils.config_precedence import resolve_seed
from src.dimensions.employees.generator import generate_employee_dimension
from src.dimensions.employees.hr import (
    _enrich_employee_hr_columns,
    _finalize_employee_integer_cols,
)

if TYPE_CHECKING:
    from src.engine.config.config_schema import AppConfig


# Columns the employee dimension reads from stores.parquet. Hoisted so the
# version signature can hash exactly what generation consumes.
_STORES_READ_COLS = [
    "StoreKey", "GeographyKey", "EmployeeCount", "StoreType",
    "StoreDistrict", "StoreRegion", "StoreDistrictId", "StoreRegionId",
    "StoreManagerFirstName", "StoreManagerLastName", "OpeningDate",
    "ClosingDate", "CloseReason",
]


def _stores_signature(stores: pd.DataFrame) -> str:
    """Content signature of the stores columns the employee dimension consumes.

    A content hash of every consumed column — EmployeeCount, lifecycle dates,
    manager names, hierarchy — not just the row count and StoreKey min/max. The
    employee roster size and manager names track these one-for-one, so a change to
    any of them must regenerate employees; the old rows+min/max signature missed
    all of it and relied on the runner cascade to paper over the gap.
    """
    return frame_content_hash(stores, _STORES_READ_COLS)


def _parse_employee_dates(
    cfg: Dict[str, Any], emp_cfg: Dict[str, Any]
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """
    Resolve the dataset-wide employee window.

    Uses ``defaults.dates.{start,end}`` exclusively.
    Legacy ``employees.start_date / end_date`` keys are ignored with a warning.
    """
    if emp_cfg.get("start_date", None) is not None or emp_cfg.get("end_date", None) is not None:
        warn(
            "employees.start_date / employees.end_date are IGNORED. "
            "Employee dates now follow defaults.dates exclusively. "
            "Remove these keys from config.yaml to silence this warning."
        )
    return parse_global_dates(
        cfg, emp_cfg,
        allow_override=False,
        dimension_name="employees",
    )


def run_employees(cfg: AppConfig, parquet_folder: Path) -> None:
    emp_cfg = cfg.employees

    parquet_folder = Path(parquet_folder)
    parquet_folder.mkdir(parents=True, exist_ok=True)

    stores_path = parquet_folder / "stores.parquet"
    out_path = parquet_folder / "employees.parquet"

    if not stores_path.exists():
        raise FileNotFoundError(f"Missing stores parquet: {stores_path}")

    seed = resolve_seed(cfg, dict(emp_cfg), fallback=42)
    global_start, global_end = _parse_employee_dates(cfg, dict(emp_cfg))

    try:
        stores = pd.read_parquet(stores_path, columns=_STORES_READ_COLS)
    except (KeyError, ValueError):
        # Legacy stores.parquet may lack hierarchy/manager columns
        stores = pd.read_parquet(
            stores_path,
            columns=["StoreKey", "GeographyKey", "EmployeeCount", "StoreType"],
        )

    version_cfg = as_dict(emp_cfg)
    # v20: publish a readable pay-frequency label while retaining the numeric
    # compatibility code in the raw operational table.
    version_cfg["schema_version"] = 20
    version_cfg["seed"] = int(seed)
    version_cfg["_stores_sig"] = _stores_signature(stores)
    version_cfg["_stores_cfg"] = as_dict(cfg.stores)
    version_cfg["_global_dates"] = {
        "start": str(global_start.date()),
        "end": str(global_end.date()),
    }

    if not should_regenerate("employees", version_cfg, out_path):
        skip("Employees up-to-date")
        return

    people_folder = resolve_people_folder()
    pf = Path(people_folder)

    enable_asia = (
        (pf / "asia_male_first.csv").exists()
        and (pf / "asia_female_first.csv").exists()
        and (pf / "asia_last.csv").exists()
    )
    people_pools = load_people_pools(
        people_folder, enable_asia=enable_asia, legacy_support=False,
    )

    iso_by_geo: dict[int, str] = {}
    has_store_hierarchy = (
        "StoreDistrict" in stores.columns and "StoreRegion" in stores.columns
    )
    geo_path = parquet_folder / "geography.parquet"
    if geo_path.exists():
        geo_df = pd.read_parquet(geo_path)
        gk = pd.to_numeric(geo_df["GeographyKey"], errors="coerce").dropna().astype(np.int32).to_numpy()
        iso = geo_df.loc[geo_df["GeographyKey"].notna(), "ISOCode"].astype(str).to_numpy()
        iso_by_geo = dict(zip(gk, iso))

        # Merge Continent/Country only when stores lacks hierarchy columns (legacy fallback)
        if not has_store_hierarchy:
            if "Continent" in geo_df.columns and "Country" in geo_df.columns:
                geo_sort = geo_df[["GeographyKey", "Continent", "Country"]].drop_duplicates("GeographyKey").copy()
                geo_sort["GeographyKey"] = pd.to_numeric(geo_sort["GeographyKey"], errors="coerce").astype(np.int32)
                stores = stores.merge(
                    geo_sort, on="GeographyKey", how="left",
                )

    # Build StoreKey → (first, last) manager name mapping. Stores is the source of
    # truth; consuming the structured columns avoids reverse-parsing a display string.
    store_manager_names: dict[int, tuple[str, str]] | None = None
    if "StoreManagerFirstName" in stores.columns and "StoreManagerLastName" in stores.columns:
        _sk = stores["StoreKey"].astype(np.int32).to_numpy()
        _first = stores["StoreManagerFirstName"].astype(str).to_numpy()
        _last = stores["StoreManagerLastName"].astype(str).to_numpy()
        store_manager_names = {
            int(sk): (fn, ln)
            for sk, fn, ln in zip(_sk.tolist(), _first.tolist(), _last.tolist())
        }
        stores = stores.drop(
            columns=["StoreManagerFirstName", "StoreManagerLastName"], errors="ignore",
        )

    # Build StoreKey → OpeningDate mapping for hire date clamping
    store_opening_dates: dict[int, pd.Timestamp] | None = None
    if "OpeningDate" in stores.columns:
        _od = pd.to_datetime(stores["OpeningDate"], errors="coerce").dt.normalize()
        _sk_od = stores["StoreKey"].astype(np.int32).to_numpy()
        store_opening_dates = {
            int(sk): ts for sk, ts in zip(_sk_od, _od) if pd.notna(ts)
        }
        stores = stores.drop(columns=["OpeningDate"], errors="ignore")

    # Build StoreKey → ClosingDate mapping for store-closure termination
    store_closing_dates: dict[int, pd.Timestamp] | None = None
    store_close_reasons: dict[int, str] | None = None
    if "ClosingDate" in stores.columns:
        _cd = pd.to_datetime(stores["ClosingDate"], errors="coerce").dt.normalize()
        _sk_cd = stores["StoreKey"].astype(np.int32).to_numpy()
        store_closing_dates = {
            int(sk): ts for sk, ts in zip(_sk_cd, _cd) if pd.notna(ts)
        }
        if "CloseReason" in stores.columns:
            _reason = stores["CloseReason"].fillna("").astype(str).to_numpy()
            store_close_reasons = {
                int(sk): reason
                for sk, reason in zip(_sk_cd, _reason)
                if int(sk) in store_closing_dates
            }
        stores = stores.drop(columns=["ClosingDate", "CloseReason"], errors="ignore")

    with stage("Generating Employees"):
        sa_cfg = emp_cfg.store_assignments
        primary_sales_role = str(getattr(sa_cfg, "primary_sales_role", None) or "Sales Associate")
        min_primary_sales_per_store = getattr(sa_cfg, "min_primary_sales_per_store", 1)

        df = generate_employee_dimension(
            stores=stores,
            seed=seed,
            global_start=global_start,
            global_end=global_end,
            people_pools=people_pools,
            iso_by_geo=iso_by_geo,
            default_region="US",
            primary_sales_role=primary_sales_role,
            min_primary_sales_per_store=min_primary_sales_per_store,
            store_manager_names=store_manager_names,
            store_opening_dates=store_opening_dates,
            store_closing_dates=store_closing_dates,
            store_close_reasons=store_close_reasons,
        )

        hr_cfg = emp_cfg.hr
        email_domain = hr_cfg.email_domain

        df = _enrich_employee_hr_columns(
            df,
            rng=np.random.default_rng(int(seed) ^ 0x9E3779B1),
            global_end=global_end,
            email_domain=str(email_domain),
            primary_sales_role=primary_sales_role,
        )

        df = _finalize_employee_integer_cols(df)

        # Reorder columns to match the static schema (CREATE TABLE column order).
        _SCHEMA_ORDER = [
            "EmployeeKey", "ParentEmployeeKey", "EmployeeName", "Title",
            "OrgLevel", "OrgUnitType", "RegionId", "DistrictId",
            "HomeStoreKey", "GeographyKey",
            "HireDate", "TerminationDate", "TerminationReason", "IsActive",
            "EmploymentType", "FTE",
            "Gender", "FirstName", "LastName", "MiddleName",
            "BirthDate", "MaritalStatus", "EmailAddress", "Phone",
            "EmergencyContactName", "EmergencyContactPhone",
            "SalariedFlag", "PayFrequency", "PayFrequencyLabel",
            "BaseRate", "VacationHours",
            "Status",
            "IsSalesperson", "DepartmentName",
        ]
        df = df[_SCHEMA_ORDER]

        compression = emp_cfg.parquet_compression
        compression_level = emp_cfg.parquet_compression_level

        date_cols = ["HireDate", "TerminationDate", "BirthDate"]
        write_parquet_with_date32(
            df,
            out_path,
            date_cols=date_cols,
            cast_all_datetime=False,
            compression=str(compression),
            compression_level=(int(compression_level) if compression_level is not None else None),
            force_date32=True,
        )

    save_version("employees", version_cfg, out_path)
    debug(f"Employees dimension written: {out_path.name}")
