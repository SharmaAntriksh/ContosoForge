"""Employee Store Assignment bridge table generator.

Generates the initial bridge table (one row per employee at their home
store).  When ``employees.transfers.enabled`` is True, the transfer
engine (``transfers.py``) post-processes the table to add inter-store
transfers, producing multiple rows per transferred employee.

Output columns:
  AssignmentKey, EmployeeKey, AssignmentSequence, StoreKey, StartDate,
  EndDate, FTE, RoleAtStore, IsPrimary, TransferReason, Status
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from src.dimensions.employees.generator import (
    STORE_MGR_KEY_BASE,
    STAFF_KEY_BASE,
    STAFF_KEY_STORE_MULT,
)
from src.defaults import ONLINE_EMP_KEY_BASE, ONLINE_STORE_KEY_BASE
from src.utils.logging_utils import info, skip, stage, warn
from src.utils.output_utils import write_parquet_with_date32
from src.versioning import should_regenerate, save_version
if TYPE_CHECKING:
    from src.engine.config.config_schema import AppConfig

from src.utils.config_helpers import (
    as_dict,
    str_or,
    parse_global_dates,
)
from src.utils.config_precedence import resolve_seed


# ---------------------------------------------------------------------------
# Renovation reassignment
# ---------------------------------------------------------------------------

def _apply_renovation_reassignments(
    assignments: pd.DataFrame,
    stores: pd.DataFrame,
    seed: int,
) -> pd.DataFrame:
    """Split assignments that span a store's renovation window.

    Each affected assignment becomes up to three rows: pre-renovation at
    the home store, temporary at a nearby open store, and post-renovation
    return.  Assignments fully outside the window are left unchanged.
    """
    from src.dimensions.employees.transfers import _build_region_store_map

    reno = stores[
        stores["RenovationStartDate"].notna()
        & stores["RenovationEndDate"].notna()
    ]
    if reno.empty:
        return assignments

    rng = np.random.default_rng(seed)

    reno_start_map = dict(zip(
        reno["StoreKey"].astype(int),
        pd.to_datetime(reno["RenovationStartDate"]),
    ))
    reno_end_map = dict(zip(
        reno["StoreKey"].astype(int),
        pd.to_datetime(reno["RenovationEndDate"]),
    ))
    reno_keys = set(reno_start_map.keys())

    # Stores that closed permanently for renovation have no return phase.
    closed_reno_keys: set[int] = set()
    if "ClosingDate" in stores.columns:
        closed_stores = stores[stores["ClosingDate"].notna()]
        closed_reno_keys = set(
            closed_stores.loc[
                closed_stores["StoreKey"].astype(int).isin(reno_keys),
                "StoreKey",
            ].astype(int)
        )

    physical = stores[stores["StoreKey"] <= ONLINE_STORE_KEY_BASE]
    open_mask = physical["Status"].astype(str) == "Open"
    candidates = physical.loc[open_mask & ~physical["StoreKey"].isin(reno_keys), "StoreKey"].astype(int).to_numpy()
    if candidates.size == 0:
        return assignments

    # Build region→[store_keys] map for same-region preference
    region_store_map = _build_region_store_map(stores)
    region_map: dict[int, str] = {}
    if "StoreRegion" in stores.columns:
        for sk, reg in zip(stores["StoreKey"].astype(int), stores["StoreRegion"].astype(str)):
            region_map[int(sk)] = reg
    candidates_set = set(candidates.tolist())
    region_candidates = {
        r: np.array([sk for sk in sks if sk in candidates_set], dtype=np.int32)
        for r, sks in region_store_map.items()
    }

    # Safety net: online employees must never be reassigned to physical stores.
    is_online_emp = assignments["EmployeeKey"] >= ONLINE_EMP_KEY_BASE
    affected = assignments["StoreKey"].astype(int).isin(reno_keys) & ~is_online_emp
    if not affected.any():
        return assignments

    keep_rows = assignments[~affected]
    new_rows: list[dict] = []
    one_day = pd.Timedelta(days=1)

    for _, row in assignments[affected].iterrows():
        sk = int(row["StoreKey"])
        rs = reno_start_map[sk]
        re = reno_end_map[sk]
        a_start = pd.Timestamp(row["StartDate"])
        a_end = pd.Timestamp(row["EndDate"])

        if a_end < rs or a_start >= re:
            new_rows.append(row.to_dict())
            continue

        src_region = region_map.get(sk, "")
        regional = region_candidates.get(src_region)
        temp_pool = regional if (regional is not None and regional.size > 0) else candidates
        temp_sk = int(rng.choice(temp_pool))

        if a_start < rs:
            new_rows.append({**row.to_dict(), "EndDate": rs - one_day, "Status": "Completed"})

        store_closed_permanently = sk in closed_reno_keys
        temp_start = max(a_start, rs)
        # If the store closed permanently, the employee stays at the temp
        # store for the rest of their assignment (no return phase).
        temp_end = a_end if store_closed_permanently else min(a_end, re - one_day)
        if temp_start <= temp_end:
            new_rows.append({
                **row.to_dict(),
                "StoreKey": np.int32(temp_sk),
                "StartDate": temp_start,
                "EndDate": temp_end,
                "IsPrimary": store_closed_permanently,
                "TransferReason": "Renovation Reassignment",
                "Status": row["Status"] if store_closed_permanently else (
                    "Completed" if re <= a_end else row["Status"]
                ),
            })

        if not store_closed_permanently and re <= a_end:
            new_rows.append({
                **row.to_dict(),
                "StartDate": re,
                "EndDate": a_end,
                "TransferReason": "Renovation Return",
                "Status": row["Status"],
            })

    result = pd.concat([keep_rows, pd.DataFrame(new_rows)], ignore_index=True)
    result = result.sort_values(["EmployeeKey", "StartDate"]).reset_index(drop=True)

    # Enforce exactly one IsPrimary=True per employee = their latest (current/home)
    # assignment, mirroring the transfer engine's "latest wins" rule. The split
    # above would otherwise leave the pre-renovation row primary in addition to
    # the temp/return row (two primaries — and, for a permanent closure, at two
    # different stores), which breaks "current store per employee" DAX patterns.
    # Rows are sorted by StartDate ascending, so the last row per employee is the
    # latest. Runs only when renovation actually split rows (early returns above
    # leave the all-True initial bridge untouched).
    result["IsPrimary"] = ~result["EmployeeKey"].duplicated(keep="last")

    result["AssignmentKey"] = np.arange(1, len(result) + 1, dtype=np.int32)
    result["AssignmentSequence"] = (result.groupby("EmployeeKey").cumcount() + 1).astype(np.int32)

    info(
        f"Renovation reassignments: {affected.sum()} assignment(s) at "
        f"{len(reno_keys)} renovating store(s) split into {len(new_rows)} rows"
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_home_store_key(employees: pd.DataFrame) -> pd.Series:
    """Derive the home StoreKey from the EmployeeKey encoding."""
    if "StoreKey" in employees.columns:
        return employees["StoreKey"].astype("Int32")

    ek = employees["EmployeeKey"].astype(np.int64)
    out = pd.Series([pd.NA] * len(employees), dtype="Int32")

    online_mask = ek >= ONLINE_EMP_KEY_BASE
    if online_mask.any():
        out.loc[online_mask] = (ek.loc[online_mask] - ONLINE_EMP_KEY_BASE).astype("Int32")

    mgr_mask = (ek >= STORE_MGR_KEY_BASE) & (ek < STAFF_KEY_BASE)
    if mgr_mask.any():
        out.loc[mgr_mask] = (ek.loc[mgr_mask] - STORE_MGR_KEY_BASE).astype("Int32")

    staff_mask = (ek >= STAFF_KEY_BASE) & ~online_mask
    if staff_mask.any():
        out.loc[staff_mask] = (
            (ek.loc[staff_mask] - STAFF_KEY_BASE) // STAFF_KEY_STORE_MULT
        ).astype("Int32")

    return out


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_employee_store_assignments(
    employees: pd.DataFrame,
    global_start: pd.Timestamp,
    global_end: pd.Timestamp,
) -> pd.DataFrame:
    """Generate the employee store assignments bridge table.

    One row per employee: home store, global_start → global_end
    (clamped to hire/termination dates).
    """
    out_cols = [
        "AssignmentKey", "EmployeeKey", "AssignmentSequence",
        "StoreKey", "StartDate", "EndDate",
        "FTE", "RoleAtStore", "IsPrimary", "TransferReason", "Status",
    ]

    if employees.empty:
        return pd.DataFrame(columns=out_cols)

    emp = employees.copy()
    emp["HomeStoreKey"] = _infer_home_store_key(emp)
    emp["HireDate"] = pd.to_datetime(emp["HireDate"], errors="coerce").dt.normalize()
    emp["TerminationDate"] = pd.to_datetime(emp["TerminationDate"], errors="coerce").dt.normalize()

    # Build one row per employee (corporate hierarchy has no store assignment)
    home_sk = emp["HomeStoreKey"]
    valid = home_sk.notna() & emp["HireDate"].notna()
    dropped = emp[~valid]
    if len(dropped) > 0:
        ek = dropped["EmployeeKey"].astype(np.int64)
        # Store-level keys (30M+) being dropped is unexpected → warn
        store_level = ek >= STORE_MGR_KEY_BASE
        n_unexpected = int(store_level.sum())
        if n_unexpected > 0:
            warn(f"{n_unexpected} store employee(s) dropped: missing StoreKey or HireDate")
    emp = emp[valid]

    if emp.empty:
        return pd.DataFrame(columns=out_cols)

    start = emp["HireDate"].clip(lower=global_start)
    term = emp["TerminationDate"]
    end = term.where(term.notna() & (term < global_end), global_end)

    # Skip employees whose window is invalid
    ok = start <= end
    emp = emp[ok]
    start = start[ok]
    end = end[ok]

    is_terminated = emp["TerminationDate"].notna().values & (emp["IsActive"].values == 0)
    n_rows = len(emp)

    out = pd.DataFrame({
        "AssignmentKey": np.arange(1, n_rows + 1, dtype=np.int32),
        "EmployeeKey": emp["EmployeeKey"].astype(np.int32),
        "StoreKey": emp["HomeStoreKey"].astype(np.int32),
        "StartDate": start,
        "EndDate": end,
        "FTE": emp["FTE"].fillna(1.0).astype(np.float64),
        "RoleAtStore": emp["Title"].astype(str),
        "IsPrimary": True,
        "TransferReason": "Initial",
        "Status": np.where(is_terminated, "Completed", "Active"),
    })

    out["StartDate"] = pd.to_datetime(out["StartDate"]).dt.normalize()
    out["EndDate"] = pd.to_datetime(out["EndDate"]).dt.normalize()

    out = out.sort_values(["EmployeeKey", "StartDate"]).reset_index(drop=True)

    # Per-employee sequential number (1, 2, 3, ... for each employee's assignments)
    out["AssignmentSequence"] = (out.groupby("EmployeeKey").cumcount() + 1).astype(np.int32)

    # Enforce column order to match static schema (BULK INSERT is positional)
    out = out[out_cols]

    info(
        f"Bridge table: {len(out)} rows, "
        f"{out['EmployeeKey'].nunique()} employees, "
        f"{out['StoreKey'].nunique()} stores"
    )

    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_employee_store_assignments(cfg: AppConfig, parquet_folder: Path, out_path: Path = None) -> None:
    """Generate and write the EmployeeStoreAssignments bridge table."""
    if out_path is None:
        out_path = parquet_folder / "employee_store_assignments.parquet"

    a_cfg = as_dict(getattr(cfg, "employee_store_assignments", None)) or {}
    emp_cfg = cfg.employees
    if emp_cfg.store_assignments is not None:
        a_cfg.update(dict(emp_cfg.store_assignments))

    employees_path = parquet_folder / "employees.parquet"
    if not employees_path.exists():
        raise FileNotFoundError(f"Missing employees parquet: {employees_path}")

    global_start, global_end = parse_global_dates(
        cfg, a_cfg,
        allow_override=True,
        dimension_name="employee_store_assignments",
    )

    employees = pd.read_parquet(
        employees_path,
        columns=["EmployeeKey", "HireDate", "TerminationDate", "Title", "FTE", "IsActive"],
    )

    # Transfer config
    transfers_cfg = emp_cfg.transfers
    transfers_enabled = transfers_cfg.enabled

    # Resolve the seed once; the renovation and transfer engines derive their
    # own seeds from it via XOR. Computing it here (a) folds it into the version
    # key so a global-seed change triggers regeneration, and (b) avoids calling
    # resolve_seed twice — which, in random mode, would otherwise yield two
    # unrelated fresh seeds for the two engines within a single run.
    base_seed = resolve_seed(cfg, as_dict(emp_cfg), fallback=42)

    version_cfg = dict(a_cfg)
    version_cfg["schema_version"] = 19  # v19: one IsPrimary=True per employee (renovation split fix)
    version_cfg["seed"] = int(base_seed)
    version_cfg["_stores_cfg"] = dict(cfg.stores)
    version_cfg["_transfers"] = as_dict(transfers_cfg)
    version_cfg["_rows_employees"] = int(len(employees))
    if len(employees) > 0:
        ek = pd.to_numeric(employees["EmployeeKey"], errors="coerce").dropna().astype(np.int32)
        if len(ek) > 0:
            version_cfg["_emp_key_min"] = int(ek.min())
            version_cfg["_emp_key_max"] = int(ek.max())
            version_cfg["_emp_key_sum"] = int(ek.sum())
        hd = pd.to_datetime(employees["HireDate"], errors="coerce").dropna()
        if len(hd) > 0:
            version_cfg["_hire_min"] = str(hd.min().date())
            version_cfg["_hire_max"] = str(hd.max().date())
    version_cfg["_global_dates"] = {
        "start": str(global_start.date()),
        "end": str(global_end.date()),
    }

    if not should_regenerate("employee_store_assignments", version_cfg, out_path):
        skip("Employee Store Assignments up-to-date")
        return

    with stage("Generating Employee Store Assignments"):
        df = generate_employee_store_assignments(
            employees=employees,
            global_start=global_start,
            global_end=global_end,
        )

        # Load stores for renovation handling and transfers
        stores_path = parquet_folder / "stores.parquet"
        stores_df = None
        if stores_path.exists():
            _stores_cols = [
                "StoreKey", "StoreType", "Status", "StoreRegion",
                "OpeningDate", "ClosingDate",
                "RenovationStartDate", "RenovationEndDate",
            ]
            try:
                stores_df = pd.read_parquet(stores_path, columns=_stores_cols)
            except (KeyError, ValueError):
                stores_df = pd.read_parquet(stores_path)

        # Handle renovation reassignments (split assignments at
        # renovating stores into pre/during/post segments)
        if stores_df is not None:
            df = _apply_renovation_reassignments(
                df, stores_df,
                seed=base_seed ^ 0x5E2B,
            )

        if transfers_enabled:
            if stores_df is None:
                raise FileNotFoundError(f"Missing stores parquet: {stores_path}")

            from src.dimensions.employees.transfers import apply_transfers

            _MIN_STORES_FOR_TRANSFERS = 10
            n_physical = int((stores_df["StoreKey"] < ONLINE_STORE_KEY_BASE).sum())
            if n_physical < _MIN_STORES_FOR_TRANSFERS:
                info(
                    f"Transfers skipped: only {n_physical} physical store(s) "
                    f"(minimum {_MIN_STORES_FOR_TRANSFERS})"
                )
                transfers_enabled = False

        if transfers_enabled:
            df = apply_transfers(
                df, stores_df,
                seed=base_seed ^ 0x7F3A,
                global_start=global_start,
                global_end=global_end,
                annual_rate=transfers_cfg.annual_rate,
                min_tenure_months=transfers_cfg.min_tenure_months,
                same_region_pref=transfers_cfg.same_region_pref,
                salesperson_roles=[emp_cfg.store_assignments.primary_sales_role],
            )

        write_parquet_with_date32(
            df,
            out_path,
            date_cols=["StartDate", "EndDate"],
            cast_all_datetime=False,
            compression=str_or(a_cfg.get("parquet_compression"), "snappy"),
            compression_level=(
                int(a_cfg["parquet_compression_level"])
                if "parquet_compression_level" in a_cfg
                else None
            ),
            force_date32=True,
        )

    save_version("employee_store_assignments", version_cfg, out_path)
    info(f"Employee Store Assignments written: {out_path.name}")
