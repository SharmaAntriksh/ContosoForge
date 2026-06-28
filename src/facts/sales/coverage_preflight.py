"""Salesperson-coverage pre-flight.

The sales fact assigns one salesperson per (store, order-date) from the
``employee_store_assignments`` bridge. If a sales-eligible store-month has no
salesperson covering it, sales can emit ``EmployeeKey = -1`` — an orphan FK that
breaks the generated SQL constraints and forces a full regeneration.

Coverage is determined entirely by the dimensions (stores + bridge + dates),
which build in seconds, so this module detects the problem cheaply *before* the
expensive sales stage and applies the configured ``sales.coverage_policy``:

    abort  -> raise with a diagnostic + suggested config fixes (default)
    skip   -> warn and continue (uncovered store-months simply get no sales)
    repair -> extend salesperson assignments to close the gaps, then continue

The month first/last-day coverage test mirrors the staffing filter in
``sales_worker/init.py`` so an "uncovered month" here is exactly the condition
that would otherwise trip the unstaffed-store fallback and produce ``-1``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from src.defaults import ONLINE_STORE_KEY_BASE, ONLINE_SALES_REP_ROLE
from src.exceptions import SalesError
from src.utils.logging_utils import info, warn


@dataclass
class CoverageReport:
    """Result of analysing salesperson coverage across the date window.

    EmployeeKey=-1 itself is prevented unconditionally by the chunk builder
    (orphan lines are dropped). This report measures the *data loss* that drop
    implies, so a policy can abort/skip/repair before shipping a dataset with
    silently missing store-months.
    """
    # (store_key, month_start, first_day, last_day, fully_open)
    gap_cells: List[Tuple[int, pd.Timestamp, np.datetime64, np.datetime64, bool]] = field(default_factory=list)
    uncovered_months: List[pd.Timestamp] = field(default_factory=list)  # whole month would be dropped
    n_months: int = 0
    n_stores: int = 0

    @property
    def n_gap_cells(self) -> int:
        return len(self.gap_cells)

    @property
    def n_fully_open_gaps(self) -> int:
        """Store-months open the WHOLE month with no salesperson — avoidable
        loss (orders dropped). Excludes benign open/close-boundary months."""
        return sum(1 for *_ , fully_open in self.gap_cells if fully_open)

    @property
    def has_avoidable_loss(self) -> bool:
        return self.n_fully_open_gaps > 0


def analyze_coverage(
    stores_df: pd.DataFrame,
    bridge_df: pd.DataFrame,
    global_start: pd.Timestamp,
    global_end: pd.Timestamp,
    salesperson_roles,
) -> CoverageReport:
    """Compute salesperson coverage per (open, non-renovating) store-month.

    Every store (physical AND online) needs its own salesperson coverage — an
    online rep does not staff a physical store, so they are checked alike. A
    store-month is covered when some salesperson assignment spans both the first
    and the last day of the month (matching the staffing filter in
    ``sales_worker/init.py``). The per-store work is vectorised across stores so
    only the month axis is a Python loop.
    """
    roles = set(salesperson_roles)
    sk_arr = stores_df["StoreKey"].astype(int).to_numpy()
    n_stores = len(sk_arr)
    op = pd.to_datetime(stores_df["OpeningDate"]).values.astype("datetime64[D]")
    cl = pd.to_datetime(stores_df["ClosingDate"]).values.astype("datetime64[D]")
    has_reno = "RenovationStartDate" in stores_df.columns and "RenovationEndDate" in stores_df.columns
    rs = pd.to_datetime(stores_df["RenovationStartDate"]).values.astype("datetime64[D]") if has_reno else np.full(n_stores, np.datetime64("NaT"))
    re = pd.to_datetime(stores_df["RenovationEndDate"]).values.astype("datetime64[D]") if has_reno else np.full(n_stores, np.datetime64("NaT"))
    isnat_cl = np.isnat(cl)
    reno_store = (~np.isnat(rs)) & (~np.isnat(re))

    # Salesperson assignments mapped to a store's local index (0..n_stores-1);
    # assignments for stores not in stores_df are dropped.
    key_to_idx = {int(k): i for i, k in enumerate(sk_arr)}
    sp = bridge_df[bridge_df["RoleAtStore"].isin(roles)]
    if not sp.empty:
        sp_start = pd.to_datetime(sp["StartDate"]).values.astype("datetime64[D]")
        sp_end = pd.to_datetime(sp["EndDate"]).values.astype("datetime64[D]")
        sp_local = np.fromiter(
            (key_to_idx.get(int(k), -1) for k in sp["StoreKey"].astype(int)),
            dtype=np.int64, count=len(sp),
        )
        _ok = sp_local >= 0
        sp_start, sp_end, sp_local = sp_start[_ok], sp_end[_ok], sp_local[_ok]
    else:
        sp_start = sp_end = np.array([], dtype="datetime64[D]")
        sp_local = np.array([], dtype=np.int64)

    months = pd.date_range(global_start, global_end, freq="MS")
    gs = np.datetime64(pd.Timestamp(global_start).normalize(), "D")
    gd = np.datetime64(pd.Timestamp(global_end).normalize(), "D")

    rep = CoverageReport(n_months=len(months), n_stores=n_stores)

    for m in months:
        fd = np.datetime64(m, "D")
        ld = np.datetime64(m + pd.offsets.MonthEnd(0), "D")
        if fd < gs:
            fd = gs
        if ld > gd:
            ld = gd
        open_mask = (op <= ld) & (isnat_cl | (cl > fd))
        reno_overlap = reno_store & (rs <= ld) & (re > fd)
        eligible = open_mask & ~reno_overlap

        cov_first = np.zeros(n_stores, dtype=bool)
        cov_last = np.zeros(n_stores, dtype=bool)
        if sp_local.size:
            cf = (sp_start <= fd) & (sp_end >= fd)
            cle = (sp_start <= ld) & (sp_end >= ld)
            if cf.any():
                np.logical_or.at(cov_first, sp_local[cf], True)
            if cle.any():
                np.logical_or.at(cov_last, sp_local[cle], True)
        covered = cov_first & cov_last

        gap = eligible & ~covered
        if gap.any():
            fully_open_all = (op <= fd) & (isnat_cl | (cl > ld))
            for i in np.where(gap)[0]:
                rep.gap_cells.append(
                    (int(sk_arr[i]), pd.Timestamp(m), fd, ld, bool(fully_open_all[i]))
                )
        open_here = int(eligible.sum())
        staffed_here = int((eligible & covered).sum())
        # A month where every open store lacks coverage would lose all its sales.
        if open_here > 0 and staffed_here == 0:
            rep.uncovered_months.append(pd.Timestamp(m))
    return rep


def suggest_adjustments(report: CoverageReport, cfg) -> List[str]:
    """Heuristic, human-readable config fixes for the detected gaps."""
    tips: List[str] = []
    emp = getattr(cfg, "employees", None)
    transfers = getattr(emp, "transfers", None) if emp is not None else None
    stores = getattr(cfg, "stores", None)
    closing = getattr(stores, "closing", None) if stores is not None else None

    ar = getattr(transfers, "annual_rate", None) if transfers is not None else None
    if ar is not None and float(ar) > 0.15:
        tips.append(f"lower employees.transfers.annual_rate ({ar} -> ~0.10): high churn empties stores")
    cs = getattr(closing, "close_share", None) if closing is not None else None
    if cs is not None and float(cs) > 0.25:
        tips.append(f"lower stores.closing.close_share ({cs} -> ~0.10): too many stores close")
    if report.n_stores < 10:
        tips.append(f"raise scale.stores ({report.n_stores} stores is very low)")
    if not tips:
        tips.append("widen defaults.dates or raise per-store staffing so each month keeps a staffed store")
    return tips


def format_diagnostic(report: CoverageReport, cfg) -> str:
    gap_months = sorted({m.strftime("%Y-%m") for _s, m, *_rest, fo in report.gap_cells if fo})
    sample = ", ".join(gap_months[:8])
    more = "" if len(gap_months) <= 8 else f" (+{len(gap_months) - 8} more)"
    lines = [
        f"Salesperson coverage gap: {report.n_fully_open_gaps} store-month(s) are open "
        f"the whole month with no salesperson ({len(report.uncovered_months)} month(s) "
        f"fully uncovered). Sales drops those orders to avoid EmployeeKey=-1 (orphan FK), "
        f"so the dataset would be missing those store-months.",
        f"  Affected months: {sample}{more}",
        f"  ({report.n_stores} stores, {report.n_months} months)",
        "  Suggested fixes:",
    ]
    lines += [f"    - {t}" for t in suggest_adjustments(report, cfg)]
    lines.append("  Or set sales.coverage_policy: skip (accept the missing sales) or "
                 "repair (auto-extend assignments to fill the gaps).")
    return "\n".join(lines)


def repair_bridge(
    bridge_df: pd.DataFrame,
    report: CoverageReport,
    salesperson_roles,
) -> Tuple[pd.DataFrame, int]:
    """Extend salesperson assignments to cover fully-open gap store-months.

    For each repairable gap, a salesperson assignment at that store is stretched
    to span the month (which lies fully inside the store's open window by
    definition of ``fully_open``). The candidate is chosen so the extended window
    does NOT overlap any of that employee's other assignments — i.e. it never
    places one employee at two stores on the same day, preserving the bridge's
    per-employee temporal-exclusivity invariant. If no store salesperson is free
    across the gap, it is left unrepaired (those orders are dropped downstream;
    still no ``-1``). Boundary gaps (store opens/closes mid-month) are skipped.
    """
    roles = set(salesperson_roles)
    df = bridge_df.copy()
    df["StartDate"] = pd.to_datetime(df["StartDate"])
    df["EndDate"] = pd.to_datetime(df["EndDate"])
    is_sp = df["RoleAtStore"].isin(roles).to_numpy()
    sk = df["StoreKey"].astype(int).to_numpy()
    emp = df["EmployeeKey"].astype(int).to_numpy()
    start = df["StartDate"].to_numpy().copy()  # writable (parquet-backed views are read-only)
    end = df["EndDate"].to_numpy().copy()

    # employee -> their salesperson row indices, for overlap checks
    emp_rows: dict[int, List[int]] = {}
    for ri in np.where(is_sp)[0]:
        emp_rows.setdefault(int(emp[ri]), []).append(int(ri))

    def _overlaps_other(e: int, r: int, ns, ne) -> bool:
        for o in emp_rows.get(e, ()):
            if o == r:
                continue
            if ns <= end[o] and ne >= start[o]:
                return True
        return False

    n_changes = 0
    for store, _m, fd, ld, fully_open in report.gap_cells:
        if not fully_open:
            continue
        fd_ts = np.datetime64(fd).astype("datetime64[ns]")
        ld_ts = np.datetime64(ld).astype("datetime64[ns]")
        rows = np.where(is_sp & (sk == store))[0]
        if rows.size == 0:
            continue  # store has no salesperson ever — not bridge-repairable
        # try candidates nearest-first; use the first whose extension stays clean
        dist = np.minimum(
            np.abs((start[rows] - ld_ts).astype("timedelta64[D]").astype(np.int64)),
            np.abs((end[rows] - fd_ts).astype("timedelta64[D]").astype(np.int64)),
        )
        for r in rows[np.argsort(dist, kind="stable")]:
            r = int(r)
            ns = min(start[r], fd_ts)
            ne = max(end[r], ld_ts)
            if ns == start[r] and ne == end[r]:
                break  # already spans the month (not actually a gap for this emp)
            if _overlaps_other(int(emp[r]), r, ns, ne):
                continue  # would double-book this employee; try the next candidate
            start[r] = ns
            end[r] = ne
            n_changes += 1
            break

    if n_changes == 0:
        return bridge_df, 0

    df["StartDate"] = pd.to_datetime(start).normalize()
    df["EndDate"] = pd.to_datetime(end).normalize()
    # Reassign keys/sequence so the bridge stays internally consistent.
    df = df.sort_values(["EmployeeKey", "StartDate"]).reset_index(drop=True)
    df["AssignmentKey"] = np.arange(1, len(df) + 1, dtype=np.int32)
    df["AssignmentSequence"] = (df.groupby("EmployeeKey").cumcount() + 1).astype(np.int32)
    return df, n_changes


def resolve_salesperson_roles(cfg) -> List[str]:
    """Mirror the role resolution in sales.py."""
    roles = None
    sales = getattr(cfg, "sales", None)
    if sales is not None:
        roles = getattr(sales, "salesperson_roles", None)
    if not (isinstance(roles, list) and roles):
        emp = getattr(cfg, "employees", None)
        sa = getattr(emp, "store_assignments", None) if emp is not None else None
        primary = getattr(sa, "primary_sales_role", None) if sa is not None else None
        roles = [str(primary or "Sales Associate"), ONLINE_SALES_REP_ROLE]
    return roles


def run_coverage_preflight(cfg, parquet_dims: Path, global_start, global_end) -> None:
    """Load dims, analyse coverage, and apply ``sales.coverage_policy``.

    Called by the sales runner after dimensions exist and before the worker
    pool spawns. ``EmployeeKey=-1`` is prevented unconditionally downstream (the
    chunk builder drops orphan lines); this decides what to do about the
    resulting data loss: ``abort`` raises :class:`SalesError`, ``skip`` warns,
    ``repair`` extends assignments to fill the gaps.
    """
    parquet_dims = Path(parquet_dims)
    stores_path = parquet_dims / "stores.parquet"
    bridge_path = parquet_dims / "employee_store_assignments.parquet"
    if not stores_path.exists() or not bridge_path.exists():
        return  # nothing to validate (e.g. sales-only run without dims)

    policy = str(getattr(getattr(cfg, "sales", None), "coverage_policy", "abort")).lower()
    roles = resolve_salesperson_roles(cfg)

    stores_df = pd.read_parquet(
        stores_path,
        columns=["StoreKey", "OpeningDate", "ClosingDate",
                 "RenovationStartDate", "RenovationEndDate"],
    )
    bridge_df = pd.read_parquet(bridge_path)

    report = analyze_coverage(stores_df, bridge_df, global_start, global_end, roles)

    if report.n_gap_cells:
        info(f"Coverage pre-flight: {report.n_gap_cells} reduced-coverage store-month(s) "
             f"({report.n_fully_open_gaps} avoidable), "
             f"{len(report.uncovered_months)} fully-uncovered.")

    # Only fully-open gaps are avoidable data loss; boundary (open/close) months
    # are expected and don't trigger the policy.
    if not report.has_avoidable_loss:
        return

    if policy == "abort":
        raise SalesError(format_diagnostic(report, cfg))

    if policy == "skip":
        warn("Coverage pre-flight (skip): "
             f"{report.n_fully_open_gaps} store-month(s) have no salesperson and "
             "their sales will be dropped (EmployeeKey=-1 prevented). "
             "Set sales.coverage_policy: repair to fill them.")
        return

    if policy == "repair":
        repaired, n = repair_bridge(bridge_df, report, roles)
        if n > 0:
            from src.utils.output_utils import write_parquet_with_date32
            write_parquet_with_date32(
                repaired, bridge_path,
                date_cols=["StartDate", "EndDate"],
                cast_all_datetime=False, force_date32=True,
            )
            recheck = analyze_coverage(
                pd.read_parquet(stores_path, columns=["StoreKey", "OpeningDate", "ClosingDate",
                                                      "RenovationStartDate", "RenovationEndDate"]),
                repaired, global_start, global_end, roles,
            )
            info(f"Coverage pre-flight (repair): extended {n} assignment(s); "
                 f"remaining avoidable gaps: {recheck.n_fully_open_gaps}.")
            if recheck.has_avoidable_loss:
                warn("Coverage pre-flight (repair): some gaps remain (stores with no "
                     "salesperson to extend); those store-months' sales will be dropped "
                     "(EmployeeKey=-1 still prevented).")
        else:
            warn("Coverage pre-flight (repair): no repairable gaps found; "
                 f"{report.n_fully_open_gaps} store-month(s) will have their sales dropped.")
        return

    # Unknown policy value -> safest behavior
    raise SalesError(
        f"Unknown sales.coverage_policy '{policy}' (expected abort|skip|repair).\n"
        + format_diagnostic(report, cfg)
    )
