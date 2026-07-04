"""Employee transfer engine.

Post-processes the EmployeeStoreAssignments bridge table to add
inter-store transfers for staff employees.  Config-gated: only runs
when ``employees.transfers.enabled = True``.

Design invariants:
  - No store drops below 1 salesperson on any date after a transfer.
  - No date gaps: old assignment ends the day before new one starts.
  - Managers (30M), online reps (50M), and corporate hierarchy never transfer.
  - Only staff in ``salesperson_roles`` are eligible.

The coverage bookkeeping and surgical rollback live in
``src.dimensions.employees.assignment_core``; the "is this store open" rule
lives in ``src.dimensions.stores.lifecycle`` (dates are truth).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.defaults import (
    EMPLOYEE_TRANSFER_REASON_LABELS,
    EMPLOYEE_TRANSFER_REASON_PROBS,
    is_physical_store_key,
)
from src.dimensions.employees.assignment_core import (
    CoverageBudget,
    _TransferRecord,
    _adjust_budget,
    _budget_violations,
    _build_coverage_budget,
    _is_transfer_feasible,
    _surgical_rollback,
)
from src.dimensions.stores.lifecycle import (
    build_region_store_map,
    build_store_date_maps,
    store_open_on,
)
from src.utils.logging_utils import info, warn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_open_store_candidates(
    region_stores: Dict[str, List[int]],
    all_physical_keys: List[int],
    year_start: pd.Timestamp,
    year_end: pd.Timestamp,
    open_dates: Dict[int, pd.Timestamp],
    close_dates: Dict[int, pd.Timestamp],
    reno_start_dates: Optional[Dict[int, pd.Timestamp]] = None,
    reno_end_dates: Optional[Dict[int, pd.Timestamp]] = None,
) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    """Pre-compute open store arrays for the year.

    Returns (region_open, all_open).  A store is included only if open on
    both year_start and year_end — stores that close or are renovating
    mid-year are excluded entirely (they will never appear as transfer
    destinations).
    """
    all_open = np.array([
        sk for sk in all_physical_keys
        if store_open_on(sk, year_start, open_dates, close_dates, reno_start_dates, reno_end_dates)
        and store_open_on(sk, year_end, open_dates, close_dates, reno_start_dates, reno_end_dates)
    ], dtype=np.int32)
    all_open_set = set(all_open)

    region_open: Dict[str, np.ndarray] = {}
    for region, sks in region_stores.items():
        arr = np.array([sk for sk in sks if sk in all_open_set], dtype=np.int32)
        if len(arr) > 0:
            region_open[region] = arr

    return region_open, all_open


def _clamp_dst_end(
    dest_sk: int,
    transfer_date: pd.Timestamp,
    original_end: pd.Timestamp,
    close_dates: Dict[int, pd.Timestamp],
    reno_start_dates: Optional[Dict[int, pd.Timestamp]],
    reno_end_dates: Optional[Dict[int, pd.Timestamp]],
) -> Tuple[pd.Timestamp, str, bool]:
    """Clamp the new destination assignment's EndDate to the destination
    store's last open day before any close/renovation. Matches the convention
    used by ``generate_employee_store_assignments``.

    Returns ``(new_end, status, skip)`` — when ``skip`` is True the caller must
    abandon this candidate (transfer date falls inside the renovation window).
    """
    new_end = original_end
    new_status = "Active"

    dest_close = close_dates.get(dest_sk)
    if dest_close is not None:
        last_open_day = dest_close - pd.Timedelta(days=1)
        if last_open_day < new_end:
            new_end = last_open_day
            new_status = "Completed"

    if reno_start_dates is None or reno_end_dates is None:
        return new_end, new_status, False
    dest_reno_start = reno_start_dates.get(dest_sk)
    dest_reno_end = reno_end_dates.get(dest_sk)
    if dest_reno_start is None or dest_reno_end is None:
        return new_end, new_status, False
    if transfer_date >= dest_reno_end:
        return new_end, new_status, False

    last_pre_reno = dest_reno_start - pd.Timedelta(days=1)
    if last_pre_reno < transfer_date:
        return new_end, new_status, True
    if last_pre_reno < new_end:
        new_end = last_pre_reno
        new_status = "Completed"
    return new_end, new_status, False


def _format_rejection_summary(rejections: Dict[str, int], fallback: str) -> str:
    if not rejections:
        return fallback
    return ", ".join(f"{count} {reason}" for reason, count in sorted(rejections.items()))


def _log_violation_details(
    violations: List[Tuple[int, int]],
    budget: CoverageBudget,
    transfer_records: List[_TransferRecord],
) -> None:
    """Per-violation warn line listing up to 3 related transfers."""
    for s_idx, m_idx in violations[:10]:
        store_key = int(budget.store_idx_to_key[s_idx])
        month_iso = pd.Timestamp(budget.month_starts[m_idx]).strftime("%Y-%m")
        related = [
            r for r in transfer_records
            if not r.rolled_back and r.src_key == store_key
            and r.transfer_date.strftime("%Y-%m") == month_iso
        ][:3]
        rel_str = (
            ", ".join(
                f"emp={r.employee_key}->store={r.dst_key}@{r.transfer_date.date()}"
                for r in related
            )
            if related else "no matching transfers"
        )
        warn(
            f"  violation store={store_key} month={month_iso} "
            f"cov={int(budget.cov[s_idx, m_idx])} min={budget.min_coverage} "
            f"related: {rel_str}"
        )


# ---------------------------------------------------------------------------
# Transfer engine
# ---------------------------------------------------------------------------

def apply_transfers(
    assignments: pd.DataFrame,
    stores: pd.DataFrame,
    *,
    seed: int,
    global_start: pd.Timestamp,
    global_end: pd.Timestamp,
    annual_rate: float = 0.05,
    min_tenure_months: int = 6,
    same_region_pref: float = 0.7,
    salesperson_roles: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Apply employee transfers to the assignments bridge table.

    For each year in the dataset range, a fraction of eligible staff
    employees are randomly transferred to another store.  The source
    assignment is ended and a new assignment is created at the
    destination store.

    Returns the modified assignments DataFrame with transfer rows added.
    If no transfers are possible, returns the original DataFrame unchanged.
    """
    if assignments.empty or annual_rate <= 0:
        return assignments

    rng = np.random.default_rng(seed)

    if salesperson_roles is None:
        salesperson_roles = ["Sales Associate"]

    # --- Store metadata ---------------------------------------------------
    sk_arr = stores["StoreKey"].astype(np.int32).to_numpy()
    open_dates, close_dates, reno_start_dates, reno_end_dates = build_store_date_maps(stores)

    store_region: Dict[int, str] = {}
    if "StoreRegion" in stores.columns:
        sr = stores["StoreRegion"].astype(str).to_numpy()
        for sk, region in zip(sk_arr, sr):
            store_region[int(sk)] = region

    region_stores = build_region_store_map(stores)

    # All physical store keys (for cross-region fallback)
    all_physical_keys = [
        int(sk) for sk in sk_arr if is_physical_store_key(sk)
    ]

    df = assignments.copy()
    transfer_count = 0
    rejections: Dict[str, int] = defaultdict(int)
    transfer_records: List[_TransferRecord] = []
    # Index of the last transfer appended for each employee, so a new hop links
    # its predecessor's chain_next (a chain forms when an employee is transferred
    # again in a later year). Drives chain-aware rollback.
    last_record_by_emp: Dict[int, int] = {}

    budget = _build_coverage_budget(
        df, stores, salesperson_roles, global_start, global_end,
        open_dates, close_dates, reno_start_dates, reno_end_dates,
        min_coverage=1,
    )

    start_year = global_start.year
    end_year = global_end.year

    for year in range(start_year, end_year + 1):
        year_start = pd.Timestamp(f"{year}-01-01")
        year_end = pd.Timestamp(f"{year}-12-31")

        # Eligible: staff employees with active primary assignments
        # and sufficient tenure at current store. In the static model an Active
        # assignment runs to global_end (EndDate == global_end), so a transfer
        # date drawn within the year always falls inside [StartDate, EndDate];
        # the transfer_date <= original_end guard in the loop is the backstop if
        # that assumption is ever broken (e.g. by a mid-window termination).
        tenure_cutoff = year_start - pd.DateOffset(months=min_tenure_months)

        # Physical-store salespeople only: the salesperson_roles filter already
        # excludes managers; is_physical_store_key excludes online reps (whose
        # home store is an online store) — together the staff-at-physical-stores
        # set the old EmployeeKey-band check selected.
        eligible_mask = (
            (df["IsPrimary"] == 1)
            & (df["Status"] == "Active")
            & (df["RoleAtStore"].isin(salesperson_roles))
            & is_physical_store_key(df["StoreKey"])
            & (df["StartDate"] <= tenure_cutoff)
            & (df["EndDate"] >= year_start)
        )
        eligible_idx = df.index[eligible_mask].to_numpy()

        if len(eligible_idx) == 0:
            continue

        n_transfers = max(1, int(round(len(eligible_idx) * annual_rate)))
        n_transfers = min(n_transfers, len(eligible_idx))
        chosen_idx = rng.choice(eligible_idx, size=n_transfers, replace=False)

        # Process chronologically so each candidate sees the budget after
        # prior accepts in the same year.
        t_earliest = max(year_start, global_start + pd.Timedelta(days=1))
        t_latest = min(
            year_end - pd.Timedelta(days=30),
            global_end - pd.Timedelta(days=30),
        )
        if t_earliest >= t_latest:
            continue
        t_range_days = (t_latest - t_earliest).days

        day_offsets = rng.integers(0, t_range_days + 1, size=len(chosen_idx))
        transfer_dates = [t_earliest + pd.Timedelta(days=int(d)) for d in day_offsets]
        chronological = sorted(zip(transfer_dates, chosen_idx))

        # Writable copies for columns mutated in the transfer loop
        col_emp_key = df["EmployeeKey"].values
        col_store_key = df["StoreKey"].values
        col_fte = df["FTE"].values
        col_role = df["RoleAtStore"].values
        col_is_primary = np.array(df["IsPrimary"].values, copy=True)
        col_status = np.array(df["Status"].values, copy=True)

        start_ns = df["StartDate"].values.astype("datetime64[ns]")
        end_ns = df["EndDate"].values.astype("datetime64[ns]").copy()

        region_open, all_open = _build_open_store_candidates(
            region_stores, all_physical_keys, year_start, year_end,
            open_dates, close_dates,
            reno_start_dates, reno_end_dates,
        )

        year_new_rows: list[dict] = []

        # Pre-generate transfer reasons for the year (avoids per-transfer rng.choice)
        _reasons_batch = rng.choice(
            EMPLOYEE_TRANSFER_REASON_LABELS,
            size=len(chronological),
            p=EMPLOYEE_TRANSFER_REASON_PROBS,
        )
        _reason_idx = 0

        for ti, (transfer_date, idx) in enumerate(chronological):
            emp_key = int(col_emp_key[idx])
            source_sk = int(col_store_key[idx])
            source_region = store_region.get(source_sk)
            employee_start = pd.Timestamp(start_ns[idx])
            original_end = pd.Timestamp(end_ns[idx])

            if transfer_date > original_end:
                # The assignment already ended before the drawn transfer date;
                # transferring would emit a zero/negative-length destination row.
                # Should not happen while Active => EndDate == global_end holds.
                rejections["transfer_after_end"] += 1
                continue

            # Pick destination store
            dest_sk = _pick_destination(
                rng=rng,
                source_sk=source_sk,
                source_region=source_region,
                region_open=region_open,
                all_open=all_open,
                same_region_pref=same_region_pref,
            )
            if dest_sk is None:
                rejections["no_destination"] += 1
                continue

            new_end, new_status, dst_skip = _clamp_dst_end(
                dest_sk, transfer_date, original_end,
                close_dates, reno_start_dates, reno_end_dates,
            )
            if dst_skip:
                rejections["dst_renovation_window"] += 1
                continue

            feasible, _violators = _is_transfer_feasible(
                budget, source_sk, employee_start, transfer_date, original_end,
            )
            if not feasible:
                rejections["source_coverage"] += 1
                continue

            _adjust_budget(
                budget, source_sk, dest_sk,
                employee_start, transfer_date, original_end, new_end,
                direction=1,
            )

            new_end_ns = np.datetime64(transfer_date - pd.Timedelta(days=1), "ns")
            end_ns[idx] = new_end_ns
            col_is_primary[idx] = np.int32(0)
            col_status[idx] = "Transferred"

            reason = _reasons_batch[_reason_idx]
            _reason_idx += 1

            new_row = {
                "EmployeeKey": np.int32(emp_key),
                "StoreKey": np.int32(dest_sk),
                "StartDate": transfer_date,
                "EndDate": new_end,
                "FTE": col_fte[idx],
                "RoleAtStore": col_role[idx],
                "IsPrimary": True,
                "TransferReason": str(reason),
                "Status": new_status,
            }
            year_new_rows.append(new_row)
            new_rec_idx = len(transfer_records)
            prev_idx = last_record_by_emp.get(emp_key)
            if prev_idx is not None:
                transfer_records[prev_idx].chain_next = new_rec_idx
            transfer_records.append(_TransferRecord(
                employee_key=emp_key,
                src_key=source_sk,
                dst_key=int(dest_sk),
                employee_start=employee_start,
                transfer_date=transfer_date,
                original_end=original_end,
                dst_end=new_end,
            ))
            last_record_by_emp[emp_key] = new_rec_idx
            transfer_count += 1

        df["EndDate"] = end_ns
        df["IsPrimary"] = col_is_primary
        df["Status"] = col_status

        if year_new_rows:
            df = pd.concat([df, pd.DataFrame(year_new_rows)], ignore_index=True)

    if transfer_count == 0:
        info(
            "Transfers: 0 transfers generated "
            f"({_format_rejection_summary(rejections, 'no candidates')})"
        )
        return assignments

    # Surgical rollback. Feasibility checks should make this a no-op normally;
    # if it does fire, drop only the transfers that intersect violation cells.
    transfer_count -= _surgical_rollback(df, budget, transfer_records)

    final_violations = _budget_violations(budget)
    if final_violations:
        warn(
            f"Transfers: {len(final_violations)} unresolved violations after "
            f"surgical rollback; reverting all transfers."
        )
        info(f"Transfer rejections: {_format_rejection_summary(rejections, 'none')}")
        _log_violation_details(final_violations, budget, transfer_records)
        return assignments

    if transfer_count == 0:
        info(
            "Transfers: 0 transfers retained after rollback "
            f"({_format_rejection_summary(rejections, 'all rolled back')})"
        )
        return assignments

    # Sort and reassign keys/sequences
    df = df.sort_values(["EmployeeKey", "StartDate"]).reset_index(drop=True)
    df["AssignmentKey"] = np.arange(1, len(df) + 1, dtype=np.int32)
    df["AssignmentSequence"] = (df.groupby("EmployeeKey").cumcount() + 1).astype(np.int32)

    # Normalize dates
    df["StartDate"] = pd.to_datetime(df["StartDate"]).dt.normalize()
    df["EndDate"] = pd.to_datetime(df["EndDate"]).dt.normalize()

    info(
        f"Transfers: {transfer_count} transfers across "
        f"{df['EmployeeKey'].nunique()} employees, "
        f"{df['StoreKey'].nunique()} stores"
    )
    if rejections:
        info(f"Transfer rejections: {_format_rejection_summary(rejections, '')}")

    return df


def _pick_destination(
    *,
    rng: np.random.Generator,
    source_sk: int,
    source_region: Optional[str],
    region_open: Dict[str, np.ndarray],
    all_open: np.ndarray,
    same_region_pref: float,
) -> Optional[int]:
    """Pick a destination store from pre-built open-store arrays."""
    use_same_region = rng.random() < same_region_pref

    candidates = None
    if use_same_region and source_region and source_region in region_open:
        arr = region_open[source_region]
        filtered = arr[arr != source_sk]
        if len(filtered) > 0:
            candidates = filtered

    if candidates is None:
        filtered = all_open[all_open != source_sk]
        if len(filtered) > 0:
            candidates = filtered

    if candidates is None:
        return None

    return int(rng.choice(candidates))
