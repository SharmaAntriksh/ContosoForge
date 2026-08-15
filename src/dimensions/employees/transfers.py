"""Employee transfer engine.

Post-processes the EmployeeStoreAssignments bridge table to add
inter-store transfers for staff employees.  Config-gated: only runs
when ``employees.transfers.enabled = True``.

Design invariants:
  - No store drops below 1 salesperson on any date after a transfer.
  - No date gaps: old assignment ends the day before new one starts.
  - Managers, online reps, and corporate hierarchy are excluded by default.
  - Only physical-store staff in configured ``eligible_roles`` are eligible.
  - Salesperson coverage is adjusted only when the transferred role sells.
  - Every emitted transfer reason has an observable business precondition.

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
    EMPLOYEE_NEW_STORE_TRANSFER_WINDOW_DAYS,
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
    build_store_region_lookup,
    build_store_date_maps,
    store_open_through_window,
)
from src.utils.logging_utils import info, warn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_open_store_candidates(
    region_stores: Dict[str, List[int]],
    all_physical_keys: List[int],
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    open_dates: Dict[int, pd.Timestamp],
    close_dates: Dict[int, pd.Timestamp],
    reno_start_dates: Optional[Dict[int, pd.Timestamp]] = None,
    reno_end_dates: Optional[Dict[int, pd.Timestamp]] = None,
) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    """Return stores open throughout a candidate assignment window."""
    all_open = np.array([
        sk for sk in all_physical_keys
        if store_open_through_window(
            sk,
            window_start,
            window_end,
            open_dates,
            close_dates,
            reno_start_dates,
            reno_end_dates,
        )
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


def _classify_transfer_reason(
    destination_store: int,
    transfer_date: pd.Timestamp,
    opening_dates: Dict[int, pd.Timestamp],
    planned_headcount: Dict[int, int],
    observed_headcount: int,
) -> str:
    """Classify a move from observable destination conditions.

    A recently opened destination is a ``New Store Opening`` move. An older
    destination below its planned staffing level is a ``Backfill``. All other
    voluntary moves are ``Employee Request``. Narrative reasons without a
    generated causal signal are intentionally not emitted.
    """
    opening_date = opening_dates.get(int(destination_store))
    if opening_date is not None:
        days_open = (pd.Timestamp(transfer_date) - pd.Timestamp(opening_date)).days
        if 0 <= days_open <= EMPLOYEE_NEW_STORE_TRANSFER_WINDOW_DAYS:
            return "New Store Opening"

    planned = int(planned_headcount.get(int(destination_store), 0))
    if planned > 0 and int(observed_headcount) < planned:
        return "Backfill"
    return "Employee Request"


def _headcount_as_of(
    row_indices: np.ndarray,
    start_dates: np.ndarray,
    end_dates: np.ndarray,
    on_date: pd.Timestamp,
    accepted_rows: List[dict],
) -> int:
    """Return destination staffing effective on ``on_date``.

    ``row_indices`` points into the year-start assignment arrays. Accepted rows
    are kept separately until the end-of-year concat, so they are included here
    explicitly. This prevents a dataset-end snapshot from leaking backward into
    historical ``Backfill`` classification.
    """
    on_date_ns = np.datetime64(on_date, "ns")
    count = int(np.count_nonzero(
        (start_dates[row_indices] <= on_date_ns)
        & (end_dates[row_indices] >= on_date_ns)
    ))
    count += sum(
        pd.Timestamp(row["StartDate"]) <= on_date <= pd.Timestamp(row["EndDate"])
        for row in accepted_rows
    )
    return count


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
    eligible_roles: Optional[List[str]] = None,
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
    if eligible_roles is None:
        eligible_roles = list(salesperson_roles)
    salesperson_role_set = set(salesperson_roles)
    eligible_role_set = set(eligible_roles)

    # --- Store metadata ---------------------------------------------------
    sk_arr = stores["StoreKey"].astype(np.int32).to_numpy()
    open_dates, close_dates, reno_start_dates, reno_end_dates = build_store_date_maps(stores)

    store_region = build_store_region_lookup(stores)

    region_stores = build_region_store_map(stores)

    # All physical store keys (for cross-region fallback)
    all_physical_keys = [
        int(sk) for sk in sk_arr if is_physical_store_key(sk)
    ]

    df = assignments.copy()
    planned_headcount = (
        dict(zip(
            stores["StoreKey"].astype(int),
            pd.to_numeric(stores["EmployeeCount"], errors="coerce")
            .fillna(0).astype(int),
        ))
        if "EmployeeCount" in stores.columns else {}
    )
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

        # Eligible: staff employees with effective current assignments and
        # sufficient tenure at the current store. AssignmentStatus is deliberately
        # not a filter: "Completed" is a dataset-end segment outcome and using it here
        # would leak a future termination backward into earlier years. The
        # effective StartDate/EndDate window is the point-in-time truth; the
        # transfer_date <= original_end guard below enforces the exact day.
        tenure_cutoff = year_start - pd.DateOffset(months=min_tenure_months)

        # Configured physical-store roles only. Managers and online/corporate
        # employees stay excluded unless a future policy explicitly adds them.
        eligible_mask = (
            (df["IsCurrentAssignment"] == 1)
            & (df["RoleAtStore"].isin(eligible_role_set))
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
        col_is_current = np.array(df["IsCurrentAssignment"].values, copy=True)
        col_status = np.array(df["AssignmentStatus"].values, copy=True)

        start_ns = df["StartDate"].values.astype("datetime64[ns]")
        end_ns = df["EndDate"].values.astype("datetime64[ns]").copy()

        row_indices_by_store: Dict[int, list[int]] = defaultdict(list)
        for row_idx, store_key in enumerate(col_store_key):
            row_indices_by_store[int(store_key)].append(row_idx)
        row_indices_by_store_np = {
            store_key: np.asarray(indices, dtype=np.int64)
            for store_key, indices in row_indices_by_store.items()
        }

        year_new_rows: list[dict] = []
        year_new_rows_by_store: Dict[int, list[dict]] = defaultdict(list)

        for transfer_date, idx in chronological:
            emp_key = int(col_emp_key[idx])
            source_sk = int(col_store_key[idx])
            source_region = store_region.get(source_sk)
            role = str(col_role[idx])
            is_salesperson = role in salesperson_role_set
            employee_start = pd.Timestamp(start_ns[idx])
            original_end = pd.Timestamp(end_ns[idx])

            if transfer_date > original_end:
                # The assignment already ended before the drawn transfer date;
                # transferring would emit a zero/negative-length destination row.
                # Should not happen while Active => EndDate == global_end holds.
                rejections["transfer_after_end"] += 1
                continue

            # Pick from stores open for the candidate's effective interval. This
            # admits genuine mid-year openings after they begin operating.
            region_open, all_open = _build_open_store_candidates(
                region_stores, all_physical_keys,
                transfer_date, original_end,
                open_dates, close_dates,
                reno_start_dates, reno_end_dates,
            )
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

            if is_salesperson:
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
            col_is_current[idx] = np.int32(0)
            col_status[idx] = "Transferred"

            destination_headcount = _headcount_as_of(
                row_indices_by_store_np.get(int(dest_sk), np.array([], dtype=np.int64)),
                start_ns,
                end_ns,
                transfer_date,
                year_new_rows_by_store[int(dest_sk)],
            )
            reason = _classify_transfer_reason(
                dest_sk, transfer_date, open_dates,
                planned_headcount, destination_headcount,
            )

            new_row = {
                "EmployeeKey": np.int32(emp_key),
                "StoreKey": np.int32(dest_sk),
                "StartDate": transfer_date,
                "EndDate": new_end,
                "FTE": col_fte[idx],
                "RoleAtStore": role,
                "IsCurrentAssignment": True,
                "TransferReason": str(reason),
                "AssignmentStatus": new_status,
            }
            year_new_rows.append(new_row)
            year_new_rows_by_store[int(dest_sk)].append(new_row)
            if is_salesperson:
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
        df["IsCurrentAssignment"] = col_is_current
        df["AssignmentStatus"] = col_status

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
    """Pick a destination store from pre-built open-store arrays.

    Cross-region moves are permitted by policy when ``same_region_pref < 1``.
    A value of exactly 1 is a hard organizational boundary: if no peer store is
    open in the source region, the transfer is skipped instead of crossing it.
    """
    use_same_region = rng.random() < same_region_pref

    candidates = None
    if use_same_region and source_region and source_region in region_open:
        arr = region_open[source_region]
        filtered = arr[arr != source_sk]
        if len(filtered) > 0:
            candidates = filtered

    if candidates is None and use_same_region and same_region_pref >= 1.0:
        return None

    if candidates is None:
        filtered = all_open[all_open != source_sk]
        if len(filtered) > 0:
            candidates = filtered

    if candidates is None:
        return None

    return int(rng.choice(candidates))
