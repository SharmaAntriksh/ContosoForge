"""Comprehensive data-quality checks for stores, employees,
employee_store_assignments bridge, and sales alignment.

Usage:
    python scripts/verify_employee_store_sales.py <dataset_folder>

    # Examples:
    python scripts/verify_employee_store_sales.py "generated_datasets/2026-04-05 05_03_16 PM Customers 43K Sales 1M PARQUET"
    python scripts/verify_employee_store_sales.py generated_datasets/latest

Exit code 0 = all passed, 1 = failures found.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Key ranges (mirror src/defaults.py and employees/generator.py)
# ---------------------------------------------------------------------------
ONLINE_STORE_KEY_BASE = 10_000
STORE_MGR_KEY_BASE = 30_000_000
STAFF_KEY_BASE = 40_000_000
ONLINE_EMP_KEY_BASE = 50_000_000


# ============================================================================
# Result model
# ============================================================================

class Check:
    __slots__ = ("category", "name", "passed", "message", "detail")

    def __init__(
        self,
        category: str,
        name: str,
        passed: bool,
        message: str,
        detail: str = "",
    ):
        self.category = category
        self.name = name
        self.passed = passed
        self.message = message
        self.detail = detail


results: List[Check] = []


def _add(
    category: str,
    name: str,
    passed: bool,
    message: str,
    detail: str = "",
) -> None:
    results.append(Check(category, name, passed, message, detail))
    tag = "\033[32mPASS\033[0m" if passed else "\033[31mFAIL\033[0m"
    print(f"  [{tag}] {name}: {message}")
    if detail and not passed:
        for line in detail.strip().splitlines():
            print(f"         {line}")


# ============================================================================
# Loaders
# ============================================================================

def _load(folder: Path, sub: str, name: str) -> Optional[pd.DataFrame]:
    p = folder / sub / f"{name}.parquet"
    if not p.exists():
        print(f"  [SKIP] {name}: file not found ({p})")
        return None
    return pd.read_parquet(p)


def _to_dt(series: pd.Series) -> pd.Series:
    """Coerce any date-like column (date objects, timestamps, strings) to datetime64[ns]."""
    return pd.to_datetime(series, errors="coerce")


# ============================================================================
# Checks: STORES
# ============================================================================

def check_stores(stores: pd.DataFrame) -> None:
    cat = "Stores"
    n = len(stores)

    # --- Duplicate StoreKey ---
    dup_sk = stores["StoreKey"].duplicated().sum()
    _add(cat, "No duplicate StoreKeys",
         dup_sk == 0,
         f"{dup_sk} duplicate(s)")

    # --- Basic counts ---
    n_phys = int((stores["StoreKey"] < ONLINE_STORE_KEY_BASE).sum())
    n_online = n - n_phys
    status_vc = stores["Status"].astype(str).value_counts().to_dict()
    _add(cat, "Store counts",
         n > 0,
         f"{n} stores ({n_phys} physical, {n_online} online). "
         f"Status: {status_vc}")

    # --- Online stores should have StoreType=Online ---
    online_mask = stores["StoreKey"].astype(int) >= ONLINE_STORE_KEY_BASE
    if online_mask.any():
        bad_type = (online_mask & (stores["StoreType"].astype(str) != "Online")).sum()
        _add(cat, "Online StoreKeys have StoreType=Online",
             bad_type == 0,
             f"{bad_type} violation(s)")

    # --- Renovation date sanity ---
    has_reno = stores["RenovationStartDate"].notna() & stores["RenovationEndDate"].notna()
    reno = stores[has_reno]
    n_reno = len(reno)
    _add(cat, "Renovation count", True, f"{n_reno} store(s) with renovation dates")

    if n_reno > 0:
        rs = _to_dt(reno["RenovationStartDate"])
        re = _to_dt(reno["RenovationEndDate"])
        od = _to_dt(reno["OpeningDate"])

        inv = (re < rs).sum()
        _add(cat, "RenovationEnd >= RenovationStart",
             inv == 0,
             f"{inv} violation(s)",
             _sample(reno[re < rs], ["StoreKey", "RenovationStartDate", "RenovationEndDate"]))

        before_open = (rs < od).sum()
        _add(cat, "RenovationStart >= OpeningDate",
             before_open == 0,
             f"{before_open} violation(s)",
             _sample(reno[rs < od], ["StoreKey", "OpeningDate", "RenovationStartDate"]))

    # --- Online stores must always be Open ---
    if online_mask.any():
        online_stores = stores[online_mask]
        bad_status = (online_stores["Status"].astype(str) != "Open").sum()
        _add(cat, "Online stores always Open",
             bad_status == 0,
             f"{bad_status} online store(s) with Status != Open")

        bad_close = online_stores["ClosingDate"].notna().sum()
        _add(cat, "Online stores have no ClosingDate",
             bad_close == 0,
             f"{bad_close} online store(s) with a ClosingDate")

        bad_reno = online_stores["RenovationStartDate"].notna().sum()
        _add(cat, "Online stores have no RenovationDate",
             bad_reno == 0,
             f"{bad_reno} online store(s) with a RenovationStartDate")

    # --- ClosingDate sanity ---
    has_close = stores["ClosingDate"].notna()
    if has_close.any():
        closed_stores = stores[has_close]
        status_mismatch = closed_stores[closed_stores["Status"].astype(str) != "Closed"]
        _add(cat, "ClosingDate implies Status=Closed",
             len(status_mismatch) == 0,
             f"{len(status_mismatch)} store(s) with ClosingDate but Status != Closed",
             _sample(status_mismatch, ["StoreKey", "Status", "ClosingDate"]))

        cd = _to_dt(closed_stores["ClosingDate"])
        od = _to_dt(closed_stores["OpeningDate"])
        bad = (cd <= od).sum()
        _add(cat, "ClosingDate > OpeningDate",
             bad == 0,
             f"{bad} violation(s)")


# ============================================================================
# Checks: EMPLOYEES
# ============================================================================

def check_employees(employees: pd.DataFrame) -> None:
    cat = "Employees"
    n = len(employees)

    # --- Duplicate EmployeeKey ---
    dup_ek = employees["EmployeeKey"].duplicated().sum()
    _add(cat, "No duplicate EmployeeKeys",
         dup_ek == 0,
         f"{dup_ek} duplicate(s)")

    hd = _to_dt(employees["HireDate"])
    td = _to_dt(employees["TerminationDate"])
    active = employees["IsActive"].astype(int)

    # --- HireDate > TerminationDate ---
    has_both = hd.notna() & td.notna()
    inv = (hd[has_both] > td[has_both]).sum()
    _add(cat, "HireDate <= TerminationDate",
         inv == 0,
         f"{inv} violation(s)",
         _sample(employees[has_both & (hd > td)], ["EmployeeKey", "HireDate", "TerminationDate"]))

    # --- IsActive consistency ---
    active_with_term = ((active == 1) & td.notna()).sum()
    _add(cat, "IsActive=1 has no TerminationDate",
         active_with_term == 0,
         f"{active_with_term} violation(s)")

    inactive_no_term = ((active == 0) & td.isna()).sum()
    _add(cat, "IsActive=0 has TerminationDate",
         inactive_no_term == 0,
         f"{inactive_no_term} violation(s)")

    # --- Hierarchy (non-CEO parent exists) ---
    if "ParentEmployeeKey" in employees.columns:
        all_ek = set(employees["EmployeeKey"].astype(int))
        non_root = employees[employees["OrgLevel"].astype(int) > 1]
        if len(non_root) > 0:
            parent_keys = non_root["ParentEmployeeKey"].astype(int)
            orphans = (~parent_keys.isin(all_ek)).sum()
            _add(cat, "Employee hierarchy valid",
                 orphans == 0,
                 f"{orphans} broken parent link(s)")

    # --- No managers should be flagged as salespeople ---
    if "IsSalesperson" in employees.columns:
        mgr_mask = (employees["EmployeeKey"].astype(np.int64) >= STORE_MGR_KEY_BASE) & \
                   (employees["EmployeeKey"].astype(np.int64) < STAFF_KEY_BASE)
        mgr_sp = (mgr_mask & (employees["IsSalesperson"].astype(int) == 1)).sum()
        _add(cat, "No managers flagged as salespeople",
             mgr_sp == 0,
             f"{mgr_sp} manager(s) with IsSalesperson=1")


# ============================================================================
# Checks: EMPLOYEE STORE ASSIGNMENTS
# ============================================================================

def check_esa(
    esa: pd.DataFrame,
    stores: pd.DataFrame,
    employees: pd.DataFrame,
) -> None:
    cat = "ESA-Bridge"

    sd = _to_dt(esa["StartDate"])
    ed = _to_dt(esa["EndDate"])

    # --- Duplicate AssignmentKey ---
    dup_ak = esa["AssignmentKey"].duplicated().sum()
    _add(cat, "No duplicate AssignmentKeys",
         dup_ak == 0,
         f"{dup_ak} duplicate(s)")

    # --- StartDate > EndDate ---
    inv = (sd > ed).sum()
    _add(cat, "StartDate <= EndDate",
         inv == 0,
         f"{inv} violation(s)",
         _sample(esa[sd > ed], ["EmployeeKey", "StoreKey", "StartDate", "EndDate", "TransferReason"]))

    # --- TransferReason / IsPrimary / Status distribution ---
    tr_vc = esa["TransferReason"].value_counts().to_dict()
    _add(cat, "TransferReason distribution", True, str(tr_vc))

    ip_vc = esa["IsPrimary"].value_counts().to_dict()
    _add(cat, "IsPrimary distribution", True, str(ip_vc))

    # --- Exactly one IsPrimary=True (current/home assignment) per employee ---
    # "Current store per employee" DAX patterns rely on a single primary row:
    #   CALCULATE(VALUES(ESA[StoreKey]), ESA[IsPrimary]=TRUE())
    # A second primary at a different store makes VALUES return >1 row and errors.
    prim = esa[esa["IsPrimary"].astype(bool)].groupby("EmployeeKey")
    multi_primary = int((prim.size() > 1).sum())
    multi_primary_store = int((prim["StoreKey"].nunique() > 1).sum())
    _add(cat, "Exactly one IsPrimary=True per employee",
         multi_primary == 0,
         f"{multi_primary} employee(s) with >1 primary row "
         f"({multi_primary_store} at >1 distinct store)")

    st_vc = esa["Status"].astype(str).value_counts().to_dict()
    _add(cat, "Status distribution", True, str(st_vc))

    # --- Shared renovation lookups for IsPrimary + segment checks ---
    reno_store_keys = set(
        stores.loc[
            stores["RenovationStartDate"].notna(), "StoreKey"
        ].astype(int)
    )
    closed_reno_keys = set(
        stores.loc[
            stores["RenovationStartDate"].notna() & stores["ClosingDate"].notna(),
            "StoreKey",
        ].astype(int)
    )

    # Pre-build employee → renovating store lookup (avoids per-call DataFrame scan)
    _emp_to_reno_store: dict[int, int] = {}
    if reno_store_keys:
        esa_at_reno = esa[esa["StoreKey"].astype(int).isin(reno_store_keys)]
        for ek_val, sk_val in zip(
            esa_at_reno["EmployeeKey"].astype(int), esa_at_reno["StoreKey"].astype(int)
        ):
            _emp_to_reno_store.setdefault(ek_val, sk_val)

    def _find_source_reno_store(ek: int) -> int:
        """Find which renovating store an employee came from."""
        cached = _emp_to_reno_store.get(ek)
        if cached is not None:
            return cached
        # Manager key encoding: 30_000_000 + StoreKey
        if STORE_MGR_KEY_BASE <= ek < STAFF_KEY_BASE:
            return ek - STORE_MGR_KEY_BASE
        # Staff key encoding: 40_000_000 + StoreKey * multiplier
        if STAFF_KEY_BASE <= ek < ONLINE_EMP_KEY_BASE:
            return (ek - STAFF_KEY_BASE) // 1000
        return -1

    # --- Renovation Reassignment IsPrimary rules ---
    # Temporary renovation (store reopens): IsPrimary must be False.
    # Permanent closure renovation (store closed): IsPrimary must be True.
    rr = esa[esa["TransferReason"] == "Renovation Reassignment"]
    if len(rr) > 0:
        rr_source = rr["EmployeeKey"].astype(int).apply(_find_source_reno_store)
        rr_from_closed = rr_source.isin(closed_reno_keys)

        # Temp reno (store reopens): IsPrimary must be False
        temp_rr = rr[~rr_from_closed]
        bad_temp = (temp_rr["IsPrimary"].astype(int) == 1).sum() if len(temp_rr) > 0 else 0
        # Permanent reno (store closed): IsPrimary must be True
        perm_rr = rr[rr_from_closed]
        bad_perm = (perm_rr["IsPrimary"].astype(int) == 0).sum() if len(perm_rr) > 0 else 0

        _add(cat, "Renovation Reassignment IsPrimary rules",
             bad_temp == 0 and bad_perm == 0,
             f"{len(temp_rr)} temp (should be False): {bad_temp} violation(s), "
             f"{len(perm_rr)} permanent (should be True): {bad_perm} violation(s)")

    # --- Renovation segment completeness ---
    # Stores that reopened: employees need pre-reno + temp + return (3 segments).
    # Stores that closed permanently: employees need pre-reno + temp (2 segments, no return).
    reno_stores = set(reno_store_keys)  # reuse shared lookup
    if reno_stores:
        reopened_reno = reno_stores - closed_reno_keys
        closed_reno = reno_stores & closed_reno_keys

        reassigned = set(
            esa.loc[esa["TransferReason"] == "Renovation Reassignment", "EmployeeKey"].astype(int)
        )
        returned = set(
            esa.loc[esa["TransferReason"] == "Renovation Return", "EmployeeKey"].astype(int)
        )

        # Classify each reassigned employee by source store
        reopen_emps: set[int] = set()
        closed_emps: set[int] = set()
        for ek in reassigned:
            src = _find_source_reno_store(ek)
            if src in closed_reno:
                closed_emps.add(ek)
            elif src in reopened_reno:
                reopen_emps.add(ek)

        # Reopened stores: need reassign + return
        miss_reassign_reopen = reopen_emps - reassigned
        miss_return_reopen = reopen_emps - returned

        # Closed stores: need reassign only, no return
        miss_reassign_closed = closed_emps - reassigned
        unexpected_return = closed_emps & returned

        ok = (
            len(miss_reassign_reopen) == 0
            and len(miss_return_reopen) == 0
            and len(miss_reassign_closed) == 0
            and len(unexpected_return) == 0
        )
        _add(cat, "Renovation segments complete",
             ok,
             f"Reopened stores ({len(reopened_reno)}): {len(reopen_emps)} emp(s), "
             f"missing reassign={len(miss_reassign_reopen)}, missing return={len(miss_return_reopen)}. "
             f"Closed stores ({len(closed_reno)}): {len(closed_emps)} emp(s), "
             f"missing reassign={len(miss_reassign_closed)}, unexpected return={len(unexpected_return)}")

    # --- No overlapping same-store assignments ---
    esa_sorted = esa.sort_values(["EmployeeKey", "StoreKey", "StartDate"]).reset_index(drop=True)
    same_group = (
        (esa_sorted["EmployeeKey"].values[1:] == esa_sorted["EmployeeKey"].values[:-1])
        & (esa_sorted["StoreKey"].values[1:] == esa_sorted["StoreKey"].values[:-1])
    )
    if same_group.any():
        ends = _to_dt(esa_sorted["EndDate"]).values[:-1]
        starts = _to_dt(esa_sorted["StartDate"]).values[1:]
        overlap_count = int((same_group & (starts <= ends)).sum())
    else:
        overlap_count = 0

    _add(cat, "No overlapping same-store assignments",
         overlap_count == 0,
         f"{overlap_count} overlap(s)")

    # --- ESA EmployeeKey exists in Employees ---
    all_emp_keys = set(employees["EmployeeKey"].astype(int))
    esa_emp_keys = set(esa["EmployeeKey"].astype(int))
    orphan_ek = esa_emp_keys - all_emp_keys
    _add(cat, "All ESA EmployeeKeys exist in Employees",
         len(orphan_ek) == 0,
         f"{len(orphan_ek)} orphan key(s)")

    # --- ESA StoreKey exists in Stores ---
    all_store_keys = set(stores["StoreKey"].astype(int))
    esa_store_keys = set(esa["StoreKey"].astype(int))
    orphan_sk = esa_store_keys - all_store_keys
    _add(cat, "All ESA StoreKeys exist in Stores",
         len(orphan_sk) == 0,
         f"{len(orphan_sk)} orphan key(s)")

    # --- Online/physical ESA cross-channel isolation ---
    esa_ek = esa["EmployeeKey"].astype(np.int64)
    esa_sk = esa["StoreKey"].astype(np.int64)
    online_emp_at_phys = ((esa_ek >= ONLINE_EMP_KEY_BASE) & (esa_sk < ONLINE_STORE_KEY_BASE)).sum()
    phys_emp_at_online = ((esa_ek < ONLINE_EMP_KEY_BASE) & (esa_sk >= ONLINE_STORE_KEY_BASE)).sum()
    _add(cat, "ESA: online employees only at online stores",
         online_emp_at_phys == 0,
         f"{online_emp_at_phys} online employee row(s) assigned to a physical store")
    _add(cat, "ESA: physical employees only at physical stores",
         phys_emp_at_online == 0,
         f"{phys_emp_at_online} physical employee row(s) assigned to an online store")

    # --- Online employee count matches online store count ---
    n_online_stores = int((stores["StoreKey"].astype(int) >= ONLINE_STORE_KEY_BASE).sum())
    n_online_emps   = int((employees["EmployeeKey"].astype(np.int64) >= ONLINE_EMP_KEY_BASE).sum())
    _add(cat, "Online employee count matches online store count",
         n_online_emps == n_online_stores,
         f"{n_online_emps} online employee(s), {n_online_stores} online store(s)")

    # --- Every store-level employee has at least 1 assignment ---
    ek_all = employees["EmployeeKey"].astype(np.int64)
    store_level_set = set(ek_all[ek_all >= STORE_MGR_KEY_BASE].astype(int))
    in_esa = set(esa["EmployeeKey"].astype(int))
    missing_from_esa = store_level_set - in_esa
    _add(cat, "Every store-level employee has an assignment",
         len(missing_from_esa) == 0,
         f"{len(missing_from_esa)} employee(s) with no ESA row")

    # --- AssignmentSequence contiguity (1,2,3... per employee) ---
    seq_agg = esa.groupby("EmployeeKey")["AssignmentSequence"].agg(["min", "max", "count"])
    seq_gaps = int(((seq_agg["max"] != seq_agg["count"]) | (seq_agg["min"] != 1)).sum())
    _add(cat, "AssignmentSequence contiguous per employee",
         seq_gaps == 0,
         f"{seq_gaps} employee(s) with non-contiguous sequence")

    # --- Terminated employees should not have Active assignments ---
    terminated_eks = set(
        employees.loc[employees["IsActive"].astype(int) == 0, "EmployeeKey"].astype(int)
    )
    if terminated_eks:
        term_esa = esa[esa["EmployeeKey"].astype(int).isin(terminated_eks)]
        zombie = (term_esa["Status"].astype(str) == "Active").sum()
        _add(cat, "No Active assignments for terminated employees",
             zombie == 0,
             f"{zombie} assignment(s) with Status=Active for terminated employees")

    # --- Assignment dates within employment window ---
    merged = esa.merge(
        employees[["EmployeeKey", "HireDate", "TerminationDate"]],
        on="EmployeeKey", how="left", suffixes=("", "_emp"),
    )
    a_start = _to_dt(merged["StartDate"])
    hire = _to_dt(merged["HireDate"])
    before_hire = (a_start < hire).sum()
    _add(cat, "Assignment StartDate >= HireDate",
         before_hire == 0,
         f"{before_hire} violation(s)")

    # --- ESA rows past store ClosingDate ---
    store_close = stores[stores["ClosingDate"].notna()][["StoreKey", "ClosingDate"]].copy()
    store_close["ClosingDate"] = _to_dt(store_close["ClosingDate"])
    if len(store_close) > 0:
        esa_c = esa.merge(store_close, on="StoreKey", how="inner")
        if len(esa_c) > 0:
            past_close = (_to_dt(esa_c["StartDate"]) >= esa_c["ClosingDate"]).sum()
            _add(cat, "No ESA rows start after store ClosingDate",
                 past_close == 0,
                 f"{past_close} violation(s)")

    # --- Pairwise checks on consecutive (employee, StartDate) rows ---
    if not esa.empty:
        _esa_emp_sorted = esa.sort_values(["EmployeeKey", "StartDate"]).reset_index(drop=True)
        _same_emp = (
            _esa_emp_sorted["EmployeeKey"].values[1:]
            == _esa_emp_sorted["EmployeeKey"].values[:-1]
        )
        _check_no_assignment_gaps(_esa_emp_sorted, _same_emp, cat)
        _check_transfer_reason_consistency(_esa_emp_sorted, _same_emp, cat)

    # --- Salesperson coverage per open physical store-month ---
    _check_salesperson_coverage(esa, stores, cat)


def _check_no_assignment_gaps(
    sorted_esa: pd.DataFrame,
    same_emp: np.ndarray,
    cat: str,
) -> None:
    """For each employee, consecutive assignments must have
    ``next.StartDate == prev.EndDate + 1 day`` — no date gap, no overlap.
    The transfer engine sets ``new_end = transfer_date - 1 day`` and the new
    row starts on ``transfer_date``, so the only valid gap is exactly 1 day.
    Catches off-by-one errors and any future change that breaks contiguity.
    """
    prev_end = _to_dt(sorted_esa["EndDate"]).values[:-1]
    next_start = _to_dt(sorted_esa["StartDate"]).values[1:]
    one_day = np.timedelta64(1, "D").astype("timedelta64[ns]")
    bad_mask = same_emp & (next_start != prev_end + one_day)
    bad_count = int(bad_mask.sum())

    detail = ""
    if bad_count > 0:
        sample_idx = np.where(bad_mask)[0][:5]
        rows: list[str] = []
        for i in sample_idx:
            ek = int(sorted_esa["EmployeeKey"].iloc[i + 1])
            pe = pd.Timestamp(prev_end[i]).date()
            ns = pd.Timestamp(next_start[i]).date()
            ps = int(sorted_esa["StoreKey"].iloc[i])
            ds = int(sorted_esa["StoreKey"].iloc[i + 1])
            rows.append(f"  emp={ek} store {ps}->{ds}, prev_end={pe}, next_start={ns}")
        detail = "\n".join(rows)

    _add(cat, "No date gaps/overlaps between consecutive assignments",
         bad_count == 0,
         f"{bad_count} contiguity violation(s)",
         detail)


# Reasons that may legitimately appear on a row whose StoreKey differs from
# the employee's prior row. "Initial" is reserved for the very first row.
# Built from the producer's label set so a rename in defaults.py shows up here
# as a check failure instead of silently passing.
try:
    from src.defaults import EMPLOYEE_TRANSFER_REASON_LABELS as _TRANSFER_REASON_LABELS
    _CAREER_REASONS = frozenset(_TRANSFER_REASON_LABELS.tolist())
except (ImportError, AttributeError):
    _CAREER_REASONS = frozenset()
_VALID_TRANSITION_REASONS = _CAREER_REASONS | {"Renovation Reassignment", "Renovation Return"}


def _check_transfer_reason_consistency(
    sorted_esa: pd.DataFrame,
    same_emp: np.ndarray,
    cat: str,
) -> None:
    """When an employee's StoreKey changes between consecutive rows, the
    NEW row's TransferReason must be a valid transition reason — never
    "Initial" (which belongs only to the first row of an employee's history).
    """
    store_changed = (
        sorted_esa["StoreKey"].values[1:] != sorted_esa["StoreKey"].values[:-1]
    )
    transition_mask = same_emp & store_changed
    if not transition_mask.any():
        _add(cat, "TransferReason valid on cross-store transitions",
             True, "no cross-store transitions in dataset")
        return

    transition_rows = sorted_esa.iloc[1:][transition_mask]
    reasons = transition_rows["TransferReason"].astype(str)
    bad_mask = ~reasons.isin(_VALID_TRANSITION_REASONS)
    bad_count = int(bad_mask.sum())

    detail = ""
    if bad_count > 0:
        bad_reasons = reasons[bad_mask].value_counts().to_dict()
        detail = f"  unexpected reasons: {bad_reasons}"

    _add(cat, "TransferReason valid on cross-store transitions",
         bad_count == 0,
         f"{bad_count} transition row(s) with invalid TransferReason",
         detail)


def _check_salesperson_coverage(
    esa: pd.DataFrame,
    stores: pd.DataFrame,
    cat: str,
) -> None:
    """Every open, non-renovating physical store must have >= 1 salesperson
    active *throughout* every month (StartDate <= month_start AND EndDate >=
    month_end). The last month_end is clamped to the latest ESA EndDate to
    handle mid-month dataset ends. Mirrors the contract of the transfer
    engine's coverage budget.
    """
    sp = esa[esa["RoleAtStore"].astype(str) == "Sales Associate"]
    if sp.empty:
        _add(cat, "Salesperson coverage", True, "No Sales Associate assignments (skipped)")
        return

    phys = stores[stores["StoreKey"].astype(int) < ONLINE_STORE_KEY_BASE]
    sp_start = _to_dt(sp["StartDate"]).values.astype("datetime64[ns]")
    sp_end = _to_dt(sp["EndDate"]).values.astype("datetime64[ns]")
    sp_sk = sp["StoreKey"].astype(int).values

    phys_c = phys[["StoreKey", "OpeningDate", "ClosingDate",
                    "RenovationStartDate", "RenovationEndDate"]].copy()
    phys_c["StoreKey"] = phys_c["StoreKey"].astype(int)
    for col in ["OpeningDate", "ClosingDate", "RenovationStartDate", "RenovationEndDate"]:
        phys_c[col] = _to_dt(phys_c[col])

    open_dates = dict(phys_c.dropna(subset=["OpeningDate"])[["StoreKey", "OpeningDate"]].values)
    close_dates = dict(phys_c.dropna(subset=["ClosingDate"])[["StoreKey", "ClosingDate"]].values)
    reno_both = phys_c.dropna(subset=["RenovationStartDate", "RenovationEndDate"])
    reno_start_dates = dict(reno_both[["StoreKey", "RenovationStartDate"]].values)
    reno_end_dates = dict(reno_both[["StoreKey", "RenovationEndDate"]].values)

    all_starts = _to_dt(esa["StartDate"])
    all_ends = _to_dt(esa["EndDate"])
    global_start = all_starts.min()
    global_end = all_ends.max()

    months = pd.date_range(global_start.replace(day=1), global_end, freq="MS")
    if len(months) == 0:
        _add(cat, "Salesperson coverage (every open store-month)", True, "No months to check (skipped)")
        return
    month_ends_raw = months + pd.offsets.MonthEnd(0)
    month_ends_clamped = pd.DatetimeIndex(
        list(month_ends_raw[:-1]) + [min(month_ends_raw[-1], global_end)]
    )

    phys_sks = phys["StoreKey"].astype(int).to_numpy()
    violations: list[tuple[int, str]] = []

    for month_start, month_end in zip(months, month_ends_clamped):
        ms_ns = np.datetime64(month_start, "ns")
        me_ns = np.datetime64(month_end, "ns")
        active_mask = (sp_start <= ms_ns) & (sp_end >= me_ns)
        covered = set(sp_sk[active_mask])

        for sk in phys_sks:
            sk = int(sk)
            od = open_dates.get(sk)
            if od is not None and month_start < od:
                continue
            cd = close_dates.get(sk)
            if cd is not None and month_end >= cd:
                continue
            rs = reno_start_dates.get(sk)
            re = reno_end_dates.get(sk)
            if rs is not None and re is not None and month_start < re and month_end >= rs:
                continue

            if sk not in covered:
                violations.append((sk, month_start.strftime("%Y-%m")))

    _add(cat, "Salesperson coverage (every open store-month, full-month)",
         len(violations) == 0,
         f"{len(violations)} store-month gap(s)",
         "\n".join(f"  Store {sk}, {ym}" for sk, ym in violations[:15]))


# ============================================================================
# Checks: SALES ALIGNMENT
# ============================================================================

def check_sales(
    sales: pd.DataFrame,
    esa: pd.DataFrame,
    stores: pd.DataFrame,
) -> None:
    cat = "Sales"
    n = len(sales)
    order_date = _to_dt(sales["OrderDate"])

    _add(cat, "Sales row count", True,
         f"{n:,} rows, {order_date.min().date()} to {order_date.max().date()}, "
         f"{sales['StoreKey'].nunique()} stores, "
         f"{sales['EmployeeKey'].nunique()} employees")

    # --- No null/zero EmployeeKey ---
    ek = sales["EmployeeKey"].astype(np.int64)
    null_or_zero = (ek <= 0).sum()
    _add(cat, "No null/zero EmployeeKey in sales",
         null_or_zero == 0,
         f"{null_or_zero} sale(s) with EmployeeKey <= 0")

    # --- Sales StoreKey FK → Stores ---
    all_store_keys = set(stores["StoreKey"].astype(int))
    sales_sk = sales["StoreKey"].astype(int)
    orphan_store = (~sales_sk.isin(all_store_keys)).sum()
    _add(cat, "Sales StoreKey exists in Stores",
         orphan_store == 0,
         f"{orphan_store} sale(s) referencing unknown StoreKey")

    # --- Sales EmployeeKey FK → Employees ---
    all_emp_keys = set(esa["EmployeeKey"].astype(int))  # use ESA keys (bridge scope)
    orphan_emp = (~ek.isin(all_emp_keys)).sum()
    _add(cat, "Sales EmployeeKey exists in ESA bridge",
         orphan_emp == 0,
         f"{orphan_emp} sale(s) referencing unknown EmployeeKey")

    # --- No manager keys in sales (30M-40M range) ---
    mgr_in_sales = ((ek >= STORE_MGR_KEY_BASE) & (ek < STAFF_KEY_BASE)).sum()
    _add(cat, "No manager keys in sales",
         mgr_in_sales == 0,
         f"{mgr_in_sales} sale(s) with manager EmployeeKey")

    # --- Online/physical channel alignment ---
    sk_int = sales["StoreKey"].astype(np.int64)
    online_store_sales = sk_int >= ONLINE_STORE_KEY_BASE
    online_emp_sales = ek >= ONLINE_EMP_KEY_BASE
    phys_emp_at_online = (online_store_sales & ~online_emp_sales & (ek > 0)).sum()
    online_emp_at_phys = (~online_store_sales & online_emp_sales).sum()
    _add(cat, "Online/physical channel alignment",
         phys_emp_at_online == 0 and online_emp_at_phys == 0,
         f"{phys_emp_at_online} physical emp at online store, "
         f"{online_emp_at_phys} online emp at physical store")

    # --- Quantity > 0 and UnitPrice > 0 ---
    if "Quantity" in sales.columns and "UnitPrice" in sales.columns:
        bad_qty = (sales["Quantity"].astype(float) <= 0).sum()
        bad_price = (sales["UnitPrice"].astype(float) <= 0).sum()
        _add(cat, "Quantity > 0 and UnitPrice > 0",
             bad_qty == 0 and bad_price == 0,
             f"{bad_qty} zero/negative qty, {bad_price} zero/negative price")

    # --- 100% sales-to-ESA bridge match ---
    _check_sales_esa_match(sales, esa, cat)

    # --- No sales during renovation ---
    _check_no_sales_during_renovation(sales, stores, cat)

    # --- No sales after store closure ---
    _check_no_sales_after_closure(sales, stores, cat)

    # --- Post-transfer leakage ---
    _check_post_transfer_leakage(sales, esa, cat)


def _check_sales_esa_match(
    sales: pd.DataFrame,
    esa: pd.DataFrame,
    cat: str,
) -> None:
    """Every sale must match an ESA row on EmployeeKey + StoreKey + OrderDate.

    Groups sales by (EmployeeKey, StoreKey), then checks each group's dates
    against the ESA windows vectorially. Avoids the cross-product merge that
    blows up memory on large datasets.
    """
    from collections import defaultdict

    s_ek = sales["EmployeeKey"].astype(np.int64).values
    s_sk = sales["StoreKey"].astype(np.int32).values
    s_od = _to_dt(sales["OrderDate"]).values.astype("datetime64[ns]")

    # Build ESA index: (EmployeeKey, StoreKey) -> list of (start_ns, end_ns)
    e_ek = esa["EmployeeKey"].astype(np.int64).values
    e_sk = esa["StoreKey"].astype(np.int32).values
    e_sd = _to_dt(esa["StartDate"]).values.astype("datetime64[ns]")
    e_ed = _to_dt(esa["EndDate"]).values.astype("datetime64[ns]")

    esa_idx: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    for i in range(len(e_ek)):
        esa_idx[(int(e_ek[i]), int(e_sk[i]))].append((e_sd[i], e_ed[i]))

    # Group sales indices by (EmployeeKey, StoreKey) using a vectorized sort
    # instead of pandas groupby (much faster for large N).
    combo = s_ek * np.int64(1_000_000) + s_sk.astype(np.int64)
    order = np.argsort(combo, kind="mergesort")
    combo_sorted = combo[order]

    # Find group boundaries
    breaks = np.flatnonzero(np.diff(combo_sorted)) + 1
    group_starts = np.concatenate([[0], breaks])
    group_ends = np.concatenate([breaks, [len(combo_sorted)]])

    matched = np.ones(len(s_ek), dtype=bool)
    unmatched_samples: list[dict] = []

    for gi in range(len(group_starts)):
        gs, ge = group_starts[gi], group_ends[gi]
        idx = order[gs:ge]
        ek_val = int(s_ek[idx[0]])
        sk_val = int(s_sk[idx[0]])

        windows = esa_idx.get((ek_val, sk_val))
        if windows is None:
            matched[idx] = False
            for j in idx[:3]:
                if len(unmatched_samples) < 10:
                    unmatched_samples.append({
                        "EmployeeKey": ek_val,
                        "StoreKey": sk_val,
                        "OrderDate": str(s_od[j])[:10],
                    })
            continue

        # Vectorized check: is each date within ANY ESA window?
        dates = s_od[idx]
        group_matched = np.zeros(len(dates), dtype=bool)
        for ws, we in windows:
            group_matched |= (dates >= ws) & (dates <= we)

        if not group_matched.all():
            bad = idx[~group_matched]
            matched[bad] = False
            for j in bad[:3]:
                if len(unmatched_samples) < 10:
                    unmatched_samples.append({
                        "EmployeeKey": ek_val,
                        "StoreKey": sk_val,
                        "OrderDate": str(s_od[j])[:10],
                    })

    total = len(s_ek)
    unmatched = int((~matched).sum())
    pct = 100.0 * (total - unmatched) / total if total > 0 else 100.0
    detail = ""
    if unmatched_samples:
        detail = "Samples:\n" + "\n".join(
            f"  EK={s['EmployeeKey']}, SK={s['StoreKey']}, Date={s['OrderDate']}"
            for s in unmatched_samples
        )

    _add(cat, "Sales-to-ESA 100% match",
         unmatched == 0,
         f"{pct:.2f}% ({total - unmatched:,}/{total:,}). {unmatched:,} unmatched.",
         detail)


def _check_no_sales_during_renovation(
    sales: pd.DataFrame,
    stores: pd.DataFrame,
    cat: str,
) -> None:
    reno = stores[
        stores["RenovationStartDate"].notna() & stores["RenovationEndDate"].notna()
    ]
    if reno.empty:
        _add(cat, "No sales during renovation", True, "No renovating stores")
        return

    # Vectorized: merge sales with renovation windows, then filter
    reno_df = reno[["StoreKey", "RenovationStartDate", "RenovationEndDate"]].copy()
    reno_df["RenovationStartDate"] = _to_dt(reno_df["RenovationStartDate"])
    reno_df["RenovationEndDate"] = _to_dt(reno_df["RenovationEndDate"])

    merged = sales[["StoreKey", "OrderDate"]].merge(reno_df, on="StoreKey", how="inner")
    if merged.empty:
        _add(cat, "No sales during renovation", True,
             f"0 violation(s) across {len(reno)} renovating store(s)")
        return

    od = _to_dt(merged["OrderDate"])
    during = (od >= merged["RenovationStartDate"]) & (od < merged["RenovationEndDate"])
    total_violations = int(during.sum())

    _add(cat, "No sales during renovation",
         total_violations == 0,
         f"{total_violations} violation(s) across {len(reno)} renovating store(s)",
         _store_violation_detail(merged, during))


def _check_no_sales_after_closure(
    sales: pd.DataFrame,
    stores: pd.DataFrame,
    cat: str,
) -> None:
    closed = stores[stores["ClosingDate"].notna()]
    if closed.empty:
        _add(cat, "No sales after store closure", True, "No closed stores")
        return

    # Vectorized: merge sales with closing dates, then filter
    close_df = closed[["StoreKey", "ClosingDate"]].copy()
    close_df["ClosingDate"] = _to_dt(close_df["ClosingDate"])

    merged = sales[["StoreKey", "OrderDate"]].merge(close_df, on="StoreKey", how="inner")
    if merged.empty:
        _add(cat, "No sales after store closure", True, "0 violation(s)")
        return

    od = _to_dt(merged["OrderDate"])
    after = od >= merged["ClosingDate"]
    total_violations = int(after.sum())

    _add(cat, "No sales after store closure",
         total_violations == 0,
         f"{total_violations} violation(s)",
         _store_violation_detail(merged, after))


def _check_post_transfer_leakage(
    sales: pd.DataFrame,
    esa: pd.DataFrame,
    cat: str,
) -> None:
    """Employees must not appear in sales at a store after their LAST ESA
    window there has ended.  (The sales-to-ESA match covers this, but this
    check gives a more specific diagnostic.)"""
    transferred = esa[esa["Status"].astype(str) == "Transferred"]
    if transferred.empty:
        _add(cat, "No post-transfer sales leakage", True, "No transferred assignments")
        return

    # Build last EndDate per (EmployeeKey, StoreKey) from ALL ESA rows
    esa_copy = esa.copy()
    esa_copy["_end"] = _to_dt(esa_copy["EndDate"])
    last_end = (
        esa_copy.groupby(["EmployeeKey", "StoreKey"])["_end"]
        .max()
        .reset_index()
        .rename(columns={"_end": "LastEndDate"})
    )

    merged = sales.merge(last_end, on=["EmployeeKey", "StoreKey"], how="inner")
    if merged.empty:
        _add(cat, "No post-transfer sales leakage", True, "No matching sales to check")
        return

    od = _to_dt(merged["OrderDate"])
    leaked = (od > merged["LastEndDate"]).sum()

    _add(cat, "No post-transfer sales leakage",
         leaked == 0,
         f"{leaked} sale(s) after last ESA EndDate for that employee+store")


# ============================================================================
# Helpers
# ============================================================================

def _sample(df: pd.DataFrame, cols: list[str], n: int = 5) -> str:
    if df.empty:
        return ""
    show = df.head(n)
    available = [c for c in cols if c in show.columns]
    return show[available].to_string(index=False)


def _store_violation_detail(merged: pd.DataFrame, mask: pd.Series, n: int = 10) -> str:
    """Format per-store violation counts from a merged sales+stores DataFrame."""
    if not mask.any():
        return ""
    by_store = merged.loc[mask].groupby("StoreKey").size()
    return "\n".join(
        f"  Store {sk}: {cnt} sale(s)" for sk, cnt in by_store.head(n).items()
    )


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/verify_employee_store_sales.py <dataset_folder>")
        return 2

    folder = Path(sys.argv[1])
    if not folder.is_absolute():
        folder = Path.cwd() / folder
    if not folder.exists():
        print(f"Folder not found: {folder}")
        return 2

    dims = folder / "dimensions"
    facts = folder / "facts"

    t0 = time.perf_counter()

    # --- Load ---
    print("\nLoading parquet files...")
    stores = _load(folder, "dimensions", "stores")
    employees = _load(folder, "dimensions", "employees")
    esa = _load(folder, "dimensions", "employee_store_assignments")

    # Sales: try sales.parquet first, then order_header.parquet
    # (sales_output can be "sales", "sales_order", or "both")
    sales = _load(folder, "facts", "sales")
    sales_table = "sales"
    if sales is None:
        sales = _load(folder, "facts", "order_header")
        sales_table = "order_header"

    if stores is None or employees is None or esa is None or sales is None:
        print("\nCannot proceed: required files missing.")
        return 2

    print(f"  Loaded in {time.perf_counter() - t0:.1f}s "
          f"(stores={len(stores)}, employees={len(employees)}, "
          f"ESA={len(esa)}, {sales_table}={len(sales):,})\n")

    # --- Run checks ---
    print("=" * 60)
    print("STORES")
    print("=" * 60)
    check_stores(stores)

    print()
    print("=" * 60)
    print("EMPLOYEES")
    print("=" * 60)
    check_employees(employees)

    print()
    print("=" * 60)
    print("ESA BRIDGE TABLE")
    print("=" * 60)
    check_esa(esa, stores, employees)

    print()
    print("=" * 60)
    print("SALES ALIGNMENT")
    print("=" * 60)
    check_sales(sales, esa, stores)

    # --- Summary ---
    elapsed = time.perf_counter() - t0
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    print()
    print("=" * 60)
    tag = "\033[32mALL PASSED\033[0m" if failed == 0 else f"\033[31m{failed} FAILED\033[0m"
    print(f"SUMMARY: {passed}/{total} passed, {tag}  ({elapsed:.1f}s)")
    print("=" * 60)

    if failed > 0:
        print("\nFailed checks:")
        for r in results:
            if not r.passed:
                print(f"  [{r.category}] {r.name}: {r.message}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
