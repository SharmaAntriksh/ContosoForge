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

from src.defaults import is_online_store_key, is_physical_store_key
from src.utils.logging_utils import info, skip, stage, warn
from src.utils.output_utils import write_parquet_with_date32
from src.versioning import should_regenerate, save_version, frame_content_hash
if TYPE_CHECKING:
    from src.engine.config.config_schema import AppConfig

from src.utils.config_helpers import (
    as_dict,
    str_or,
    parse_global_dates,
)
from src.utils.config_precedence import resolve_seed


# Columns the bridge reads from employees.parquet — the version signature hashes
# exactly this consumed set.
_ESA_EMP_READ_COLS = [
    "EmployeeKey", "HireDate", "TerminationDate", "Title", "FTE", "IsActive", "StoreKey",
]


# ---------------------------------------------------------------------------
# Renovation reassignment
# ---------------------------------------------------------------------------

def _apply_renovation_reassignments(
    assignments: pd.DataFrame,
    stores: pd.DataFrame,
    seed: int,
) -> pd.DataFrame:
    """Split assignments that span a store's renovation window.

    A renovation is modelled as one or two forced hops through the shared
    ``split_primary_assignment`` primitive: the home assignment is displaced to
    a nearby open store for the window and — unless the store closed permanently
    — returns home when the renovation ends. Each affected assignment becomes up
    to three rows (pre-renovation home, temporary store, post-renovation home);
    superseded segments are non-primary ``Transferred`` and the latest segment
    carries the employee's original status, exactly as a voluntary transfer.
    Assignments fully outside the window are left unchanged.
    """
    from src.dimensions.stores.lifecycle import (
        build_region_store_map,
        build_store_date_maps,
        store_open_through_window,
    )
    from src.dimensions.employees.assignment_core import split_primary_assignment

    if "RenovationStartDate" not in stores.columns or "RenovationEndDate" not in stores.columns:
        warn(
            "stores frame lacks RenovationStartDate/RenovationEndDate; skipping "
            "renovation reassignments. Regenerate stores to enable them."
        )
        return assignments

    # Date-window openness, not the Status label. Status is an as-of-dataset-end
    # snapshot: a store that reopened after its own renovation, or one that opens
    # later in the window, both read "Open" today even though neither was open
    # during a past temp window. The dates are the truth — a valid temp store
    # must have opened on or before the window start and not closed before its
    # end (store_open_through_window enforces exactly that).
    open_dates, close_dates, reno_start_dates, reno_end_dates = build_store_date_maps(stores)
    reno_keys = set(reno_start_dates.keys())
    if not reno_keys:
        return assignments

    rng = np.random.default_rng(seed)

    # Stores that closed permanently for renovation have no return phase.
    closed_reno_keys = {k for k in reno_keys if k in close_dates}

    physical = stores[is_physical_store_key(stores["StoreKey"])]
    candidates = physical.loc[~physical["StoreKey"].isin(reno_keys), "StoreKey"].astype(int).to_numpy()
    if candidates.size == 0:
        return assignments

    # Build region→[store_keys] map for same-region preference
    region_store_map = build_region_store_map(stores)
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
    # Identify them by their (online) home store rather than an EmployeeKey range.
    is_online_emp = is_online_store_key(assignments["StoreKey"])
    affected = assignments["StoreKey"].astype(int).isin(reno_keys) & ~is_online_emp
    if not affected.any():
        return assignments

    keep_rows = assignments[~affected]
    new_rows: list[dict] = []
    one_day = pd.Timedelta(days=1)

    for row in assignments[affected].itertuples(index=False):
        row_d = dict(row._asdict())
        sk = int(row.StoreKey)
        rs = reno_start_dates[sk]
        re = reno_end_dates[sk]
        a_start = pd.Timestamp(row.StartDate)
        a_end = pd.Timestamp(row.EndDate)

        if a_end < rs or a_start >= re:
            new_rows.append(row_d)
            continue

        store_closed_permanently = sk in closed_reno_keys
        temp_start = max(a_start, rs)
        # If the store closed permanently, the employee stays at the temp
        # store for the rest of their assignment (no return phase).
        temp_end = a_end if store_closed_permanently else min(a_end, re - one_day)

        # Choose a temp store open for the entire [temp_start, temp_end] window,
        # preferring the same region. If nothing qualifies, the employee stays at
        # their (renovating) home store rather than being sent to a store that was
        # not open then.
        src_region = region_map.get(sk, "")
        regional = region_candidates.get(src_region)
        regional_open = (
            np.array(
                [s for s in regional.tolist()
                 if store_open_through_window(int(s), temp_start, temp_end, open_dates, close_dates)],
                dtype=np.int32,
            )
            if regional is not None and regional.size > 0
            else np.array([], dtype=np.int32)
        )
        if regional_open.size > 0:
            temp_sk = int(rng.choice(regional_open))
        else:
            global_open = np.array(
                [s for s in candidates.tolist()
                 if store_open_through_window(int(s), temp_start, temp_end, open_dates, close_dates)],
                dtype=np.int32,
            )
            temp_sk = int(rng.choice(global_open)) if global_open.size > 0 else sk

        has_return = (not store_closed_permanently) and (re <= a_end)
        orig_status = row_d["Status"]

        # Displacement hop: home[a_start, temp_start-1] is superseded and the
        # employee works at temp[temp_start, temp_end].
        home_pre, temp = split_primary_assignment(
            row_d, temp_start,
            dest_key=temp_sk, dest_end=temp_end,
            transfer_reason="Renovation Reassignment", dest_status=orig_status,
        )
        if home_pre is not None:
            new_rows.append(home_pre)
        if has_return:
            # Return hop: temp is superseded and the employee returns to
            # home[re, a_end] with their original status.
            temp, ret = split_primary_assignment(
                temp, re,
                dest_key=sk, dest_end=a_end,
                transfer_reason="Renovation Return", dest_status=orig_status,
            )
            new_rows.append(temp)
            new_rows.append(ret)
        else:
            new_rows.append(temp)

    result = pd.concat([keep_rows, pd.DataFrame(new_rows)], ignore_index=True)
    result = result.sort_values(["EmployeeKey", "StartDate"]).reset_index(drop=True)

    # Each split sets IsPrimary explicitly (superseded segments non-primary, the
    # latest segment primary), so exactly one row per employee is primary without
    # a post-hoc recompute. Keys/dtypes are normalised to match the transfer
    # engine and static schema (int32 keys regardless of the split path).
    result["EmployeeKey"] = result["EmployeeKey"].astype(np.int32)
    result["StoreKey"] = result["StoreKey"].astype(np.int32)
    result["IsPrimary"] = result["IsPrimary"].astype(bool)
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
    """Home StoreKey per employee, read from the authoritative ``StoreKey`` column.

    Corporate rows (CEO/VP/region/district) carry ``StoreKey == 0`` (the
    ``fillna(0)`` int cast in the generator), which is not a real store key — map
    it to NA so those rows drop from the bridge. Valid store keys are 1..N
    (physical) and > ONLINE_STORE_KEY_BASE (online); 0 never occurs for a
    store-level employee. With opaque EmployeeKeys the key can no longer be
    decoded, so the StoreKey column is the sole source of home store.
    """
    sk = employees["StoreKey"].astype("Int32")
    return sk.where(sk != 0)


def _verify_coverage_postcondition(
    assignments: pd.DataFrame,
    stores: pd.DataFrame,
    salesperson_roles: list[str],
    global_start: pd.Timestamp,
    global_end: pd.Timestamp,
) -> None:
    """Runner post-condition: every constrained (open, non-renovating) store-month
    must keep at least one salesperson after renovation and transfers.

    The transfer engine already reverts transfers to satisfy this, and renovation
    only displaces employees within a window the store is closed anyway — so a
    violation here signals a coverage bug worth surfacing rather than a reason to
    abort a full generation run. Runs unconditionally whenever the stores frame
    is available; it reads only, so it never changes the written bridge.
    """
    from src.dimensions.stores.lifecycle import build_store_date_maps
    from src.dimensions.employees.assignment_core import check_coverage_invariant

    open_dates, close_dates, reno_start_dates, reno_end_dates = build_store_date_maps(stores)
    violations = check_coverage_invariant(
        assignments, stores, salesperson_roles,
        global_start, global_end,
        open_dates, close_dates, reno_start_dates, reno_end_dates,
    )
    if violations:
        shown = ", ".join(f"store {sk}@{m}" for sk, m in violations)
        warn(
            f"Coverage post-condition: {len(violations)} constrained store-month(s) "
            f"without a salesperson after renovation + transfers: {shown}"
        )


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_employee_store_assignments(
    employees: pd.DataFrame,
    global_start: pd.Timestamp,
    global_end: pd.Timestamp,
    store_opening_dates: dict[int, pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """Generate the employee store assignments bridge table.

    One row per employee: home store, global_start → global_end
    (clamped to hire/termination dates).

    ``store_opening_dates`` (StoreKey → OpeningDate) clamps each assignment's
    StartDate up to its store's opening, so no employee is assigned before the
    store exists. With the default (shifted) opening window every store opens
    before ``global_start``, making the clamp a no-op and the bridge
    byte-identical; it only bites when mid-window openings are enabled. A store
    opening after ``global_end`` yields StartDate > EndDate and its employees
    drop out (correct — the store never operates within the window).
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
        # Store-level employees (managers/staff/online) being dropped is
        # unexpected → warn. They carry a real home store; corporate/region/
        # district legitimately have none (HomeStoreKey is NA for them).
        store_level = dropped["HomeStoreKey"].notna()
        n_unexpected = int(store_level.sum())
        if n_unexpected > 0:
            warn(f"{n_unexpected} store employee(s) dropped: missing StoreKey or HireDate")
    emp = emp[valid]

    if emp.empty:
        return pd.DataFrame(columns=out_cols)

    start = emp["HireDate"].clip(lower=global_start)

    # Clamp the assignment start up to the store's OpeningDate: an employee
    # cannot work at a store before it opens. No-op when the store opened before
    # global_start (the default shifted window), so the bridge stays
    # byte-identical unless mid-window openings put an opening inside the window.
    if store_opening_dates:
        open_ts = pd.to_datetime(
            emp["HomeStoreKey"].map(
                lambda sk: store_opening_dates.get(int(sk)) if pd.notna(sk) else None
            )
        ).dt.normalize()
        has_open = open_ts.notna()
        if has_open.any():
            start = start.copy()
            start.loc[has_open] = np.maximum(
                start.loc[has_open].to_numpy("datetime64[ns]"),
                open_ts.loc[has_open].to_numpy("datetime64[ns]"),
            )

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

    emp_cfg = cfg.employees
    # Bridge config lives under employees.store_assignments. The top-level
    # employee_store_assignments section is a legacy no-op (store_assignments
    # always took precedence in the old merge), so it is not consulted.
    sa_cfg = dict(emp_cfg.store_assignments) if emp_cfg.store_assignments is not None else {}

    employees_path = parquet_folder / "employees.parquet"
    if not employees_path.exists():
        raise FileNotFoundError(f"Missing employees parquet: {employees_path}")

    # allow_override=False to match the employees runner: the bridge dates must
    # track the same global window the employees were hired against, or the
    # assignments desync from HireDate/TerminationDate.
    global_start, global_end = parse_global_dates(
        cfg, sa_cfg,
        allow_override=False,
        dimension_name="employee_store_assignments",
    )

    employees = pd.read_parquet(employees_path, columns=_ESA_EMP_READ_COLS)

    # Transfer config
    transfers_cfg = emp_cfg.transfers
    transfers_enabled = transfers_cfg.enabled

    # Resolve the seed once; the renovation and transfer engines derive their
    # own seeds from it via XOR. Computing it here (a) folds it into the version
    # key so a global-seed change triggers regeneration, and (b) avoids calling
    # resolve_seed twice — which, in random mode, would otherwise yield two
    # unrelated fresh seeds for the two engines within a single run.
    base_seed = resolve_seed(cfg, as_dict(emp_cfg), fallback=42)

    version_cfg = dict(sa_cfg)
    # renovation runs through the shared split primitive: superseded segments are
    # Transferred (was Completed) and key columns are int32 regardless of split path
    version_cfg["schema_version"] = 21
    version_cfg["seed"] = int(base_seed)
    version_cfg["_stores_cfg"] = dict(cfg.stores)
    version_cfg["_transfers"] = as_dict(transfers_cfg)
    # Content hash of the exact employee columns the bridge consumes, so any
    # change to keys, roles, FTE, or hire/termination dates regenerates the
    # bridge. Replaces the old _emp_key_min/max/sum + _hire_min/max fingerprint,
    # which two different employee frames could collide on.
    version_cfg["_employees_sig"] = frame_content_hash(employees, _ESA_EMP_READ_COLS)
    version_cfg["_global_dates"] = {
        "start": str(global_start.date()),
        "end": str(global_end.date()),
    }

    if not should_regenerate("employee_store_assignments", version_cfg, out_path):
        skip("Employee Store Assignments up-to-date")
        return

    with stage("Generating Employee Store Assignments"):
        # Load stores up front: the bridge clamps each assignment's start to its
        # store's OpeningDate (mid-window openings), and renovation + transfers
        # need the same frame downstream.
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

        store_opening_dates: dict[int, pd.Timestamp] | None = None
        if stores_df is not None and "OpeningDate" in stores_df.columns:
            _od = pd.to_datetime(stores_df["OpeningDate"], errors="coerce")
            store_opening_dates = {
                int(sk): d.normalize()
                for sk, d in zip(stores_df["StoreKey"].astype(int), _od)
                if pd.notna(d)
            }

        df = generate_employee_store_assignments(
            employees=employees,
            global_start=global_start,
            global_end=global_end,
            store_opening_dates=store_opening_dates,
        )

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
            n_physical = int(is_physical_store_key(stores_df["StoreKey"]).sum())
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

        if stores_df is not None:
            _verify_coverage_postcondition(
                df, stores_df,
                [emp_cfg.store_assignments.primary_sales_role],
                global_start, global_end,
            )

        write_parquet_with_date32(
            df,
            out_path,
            date_cols=["StartDate", "EndDate"],
            cast_all_datetime=False,
            compression=str_or(sa_cfg.get("parquet_compression"), "snappy"),
            compression_level=(
                int(sa_cfg["parquet_compression_level"])
                if "parquet_compression_level" in sa_cfg
                else None
            ),
            force_date32=True,
        )

    save_version("employee_store_assignments", version_cfg, out_path)
    info(f"Employee Store Assignments written: {out_path.name}")
