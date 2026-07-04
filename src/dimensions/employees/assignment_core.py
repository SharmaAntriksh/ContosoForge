"""Assignment-mutation core shared by the transfer and renovation engines.

Holds the per-(store, month) salesperson ``CoverageBudget`` and the primitives
that mutate the assignments bridge while keeping it consistent:

  - ``split_primary_assignment`` — truncate a primary assignment at a date and
    emit a new row at the destination store (the one operation both a transfer
    and a renovation displacement perform).
  - coverage bookkeeping (``_build_coverage_budget`` / ``_adjust_budget`` /
    ``_is_transfer_feasible`` / ``_budget_violations``).
  - surgical rollback of transfers that would leave a constrained store-month
    below ``min_coverage``, chain-aware so an inner hop of an employee's
    transfer chain is never rolled back while a later hop survives.
  - ``check_coverage_invariant`` — the runner post-condition.

Openness decisions (whether a store is open on a date / through a window) are
owned by ``src.dimensions.stores.lifecycle`` — dates are the truth, ``Status``
is a display label.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.defaults import is_physical_store_key


# ---------------------------------------------------------------------------
# The one row-level mutation both engines perform: split a primary assignment
# at a date and continue it at another store.
# ---------------------------------------------------------------------------

def split_primary_assignment(
    source: dict,
    split_date: pd.Timestamp,
    *,
    dest_key: int,
    dest_end: pd.Timestamp,
    transfer_reason: str,
    dest_status: str = "Active",
    source_status: str = "Transferred",
) -> Tuple[Optional[dict], dict]:
    """Split a primary assignment at ``split_date``, moving it to ``dest_key``.

    Returns ``(source_row, dest_row)``:
      - ``source_row``: ``source`` truncated to ``EndDate = split_date - 1 day``,
        demoted to non-primary with ``Status = source_status``. ``None`` when the
        truncated window would be empty (``split_date <= source['StartDate']``).
      - ``dest_row``: a new primary row at ``dest_key`` spanning
        ``[split_date, dest_end]``, ``Status = dest_status`` and the given
        ``transfer_reason``. All other columns are carried over from ``source``.

    Both a voluntary transfer and a renovation displacement are this operation;
    the transfer engine's hot path performs the equivalent truncation in place
    on numpy column arrays for speed, and the renovation reassignment builds the
    row dicts through this helper.
    """
    one_day = pd.Timedelta(days=1)
    a_start = pd.Timestamp(source["StartDate"])

    source_row = None
    if split_date > a_start:
        source_row = {
            **source,
            "EndDate": split_date - one_day,
            "IsPrimary": False,
            "Status": source_status,
        }

    dest_row = {
        **source,
        "StoreKey": dest_key,
        "StartDate": split_date,
        "EndDate": dest_end,
        "IsPrimary": True,
        "TransferReason": transfer_reason,
        "Status": dest_status,
    }
    return source_row, dest_row


# ---------------------------------------------------------------------------
# Coverage budget — constraint-aware transfer gating. The budget tracks
# per-(physical_store, month) salesperson coverage and a per-cell constrained
# mask. Each candidate transfer is rejected if it would drop a constrained
# source-month below ``min_coverage``.
# ---------------------------------------------------------------------------

@dataclass
class CoverageBudget:
    """Per-(store, month) salesperson coverage matrix used to gate transfers.

    A salesperson assignment counts toward ``cov[s, m]`` only when it covers
    the *full* month — ``StartDate <= month_start AND EndDate >= month_end``,
    with the last month_end clamped to ``global_end``. This single condition
    enforces both month-start and month-end staffing in one matrix.

    A cell is *constrained* when the store is open through that month and is
    not within a renovation window. Renovation, pre-open, and post-close
    cells are unconstrained — coverage may be 0 there without violation.
    """
    cov: np.ndarray
    constrained: np.ndarray
    store_idx_to_key: np.ndarray
    key_to_store_idx: Dict[int, int]
    month_starts: np.ndarray
    month_ends: np.ndarray
    min_coverage: int = 1


def _build_coverage_budget(
    assignments: pd.DataFrame,
    stores: pd.DataFrame,
    salesperson_roles: List[str],
    global_start: pd.Timestamp,
    global_end: pd.Timestamp,
    open_dates: Dict[int, pd.Timestamp],
    close_dates: Dict[int, pd.Timestamp],
    reno_start_dates: Optional[Dict[int, pd.Timestamp]] = None,
    reno_end_dates: Optional[Dict[int, pd.Timestamp]] = None,
    min_coverage: int = 1,
) -> CoverageBudget:
    """Build the initial coverage matrix from the pre-transfer assignments."""
    month_starts_ts = pd.date_range(global_start, global_end, freq="MS")
    if len(month_starts_ts) == 0:
        month_starts_ts = pd.DatetimeIndex([pd.Timestamp(global_start).normalize()])

    # last day of each month, clamping the final month to global_end
    month_ends_ts_raw = month_starts_ts + pd.offsets.MonthEnd(0)
    global_end_ts = pd.Timestamp(global_end).normalize()
    if month_ends_ts_raw[-1] > global_end_ts:
        month_ends_ts = pd.DatetimeIndex(
            list(month_ends_ts_raw[:-1]) + [global_end_ts]
        )
    else:
        month_ends_ts = month_ends_ts_raw

    month_starts = month_starts_ts.values.astype("datetime64[ns]")
    month_ends = month_ends_ts.values.astype("datetime64[ns]")
    n_months = len(month_starts)

    # Physical store axis (online stores excluded from coverage matrix)
    physical_mask = is_physical_store_key(stores["StoreKey"].to_numpy())
    physical_keys = stores.loc[physical_mask, "StoreKey"].astype(np.int32).to_numpy()
    n_stores = len(physical_keys)
    key_to_store_idx: Dict[int, int] = {int(k): i for i, k in enumerate(physical_keys)}

    cov = np.zeros((n_stores, n_months), dtype=np.int32)

    # Vectorised cov build: for each salesperson row, the run of months it
    # fully covers is contiguous — its first month_idx is the smallest m where
    # m_start >= start, last is the largest m where m_end <= end. Use
    # searchsorted on both axes and accumulate via np.add.at.
    sp_rows = assignments[assignments["RoleAtStore"].isin(salesperson_roles)]
    if not sp_rows.empty and n_stores > 0 and n_months > 0:
        sp_start = sp_rows["StartDate"].values.astype("datetime64[ns]")
        sp_end = sp_rows["EndDate"].values.astype("datetime64[ns]")
        sp_sk = sp_rows["StoreKey"].astype(int).to_numpy()
        sidx_arr = np.fromiter(
            (key_to_store_idx.get(int(k), -1) for k in sp_sk),
            dtype=np.int64, count=len(sp_sk),
        )
        first_m = np.searchsorted(month_starts, sp_start, side="left")
        last_m = np.searchsorted(month_ends, sp_end, side="right") - 1
        valid = (sidx_arr >= 0) & (first_m <= last_m) & (last_m < n_months) & (first_m >= 0)
        for i in np.where(valid)[0]:
            cov[sidx_arr[i], first_m[i]:last_m[i] + 1] += 1

    # Vectorised constrained mask: build per-store dates as 1-D arrays then
    # broadcast against the month_starts / month_ends 1-D arrays. Sentinels
    # must stay inside the ns range (~1677-2262) to avoid overflow wraparound.
    far_past = np.datetime64("1700-01-01", "ns")
    far_future = np.datetime64("2200-01-01", "ns")
    open_arr = np.array(
        [np.datetime64(open_dates[int(k)], "ns") if int(k) in open_dates else far_past
         for k in physical_keys],
        dtype="datetime64[ns]",
    )
    close_arr = np.array(
        [np.datetime64(close_dates[int(k)], "ns") if int(k) in close_dates else far_future
         for k in physical_keys],
        dtype="datetime64[ns]",
    )
    reno_s_arr = np.full(n_stores, far_future, dtype="datetime64[ns]")
    reno_e_arr = np.full(n_stores, far_past, dtype="datetime64[ns]")
    if reno_start_dates and reno_end_dates:
        for i, k in enumerate(physical_keys):
            ki = int(k)
            if ki in reno_start_dates and ki in reno_end_dates:
                reno_s_arr[i] = np.datetime64(reno_start_dates[ki], "ns")
                reno_e_arr[i] = np.datetime64(reno_end_dates[ki], "ns")

    opened = month_starts[None, :] >= open_arr[:, None]
    not_closed = month_ends[None, :] < close_arr[:, None]
    renovating = (month_starts[None, :] < reno_e_arr[:, None]) & (month_ends[None, :] >= reno_s_arr[:, None])
    constrained = opened & not_closed & ~renovating

    return CoverageBudget(
        cov=cov,
        constrained=constrained,
        store_idx_to_key=physical_keys,
        key_to_store_idx=key_to_store_idx,
        month_starts=month_starts,
        month_ends=month_ends,
        min_coverage=min_coverage,
    )


def _affected_source_months(
    budget: CoverageBudget,
    employee_start: np.datetime64,
    transfer_date_ns: np.datetime64,
    original_end_ns: np.datetime64,
) -> np.ndarray:
    """Boolean mask of months at the source store that the OLD assignment fully
    covered but the truncated (post-transfer) assignment no longer covers.

    Old full coverage: ``start <= m_start AND old_end >= m_end``.
    New coverage stops at ``transfer_date - 1``, so the assignment still fully
    covers a month iff ``m_end < transfer_date``. Loss = old AND NOT new =
    ``start <= m_start AND m_end <= old_end AND m_end >= transfer_date``.
    """
    return (
        (budget.month_starts >= employee_start)
        & (budget.month_ends <= original_end_ns)
        & (budget.month_ends >= transfer_date_ns)
    )


def _affected_dest_months(
    budget: CoverageBudget,
    transfer_date_ns: np.datetime64,
    dst_end_ns: np.datetime64,
) -> np.ndarray:
    """Boolean mask of months at the destination store that the new assignment
    fully covers (``m_start >= transfer_date AND m_end <= dst_end``)."""
    return (
        (budget.month_starts >= transfer_date_ns)
        & (budget.month_ends <= dst_end_ns)
    )


def _is_transfer_feasible(
    budget: CoverageBudget,
    src_key: int,
    employee_start: pd.Timestamp,
    transfer_date: pd.Timestamp,
    original_end_date: pd.Timestamp,
) -> Tuple[bool, List[Tuple[int, int]]]:
    """True iff removing one salesperson from ``src_key`` for the months whose
    coverage is lost would keep every constrained source-month at >= min_coverage.

    Returns ``(feasible, violators)`` where violators is a list of
    ``(store_idx, month_idx)`` cells that would drop below threshold.
    """
    src_idx = budget.key_to_store_idx.get(int(src_key))
    if src_idx is None:
        return True, []

    start_ns = np.datetime64(employee_start, "ns")
    td_ns = np.datetime64(transfer_date, "ns")
    end_ns = np.datetime64(original_end_date, "ns")

    loss_mask = _affected_source_months(budget, start_ns, td_ns, end_ns)
    if not loss_mask.any():
        return True, []

    cov_slice = budget.cov[src_idx, loss_mask]
    constrained_slice = budget.constrained[src_idx, loss_mask]
    would_drop_below = (cov_slice - 1 < budget.min_coverage) & constrained_slice
    if not would_drop_below.any():
        return True, []

    affected_indices = np.where(loss_mask)[0]
    violators = [
        (src_idx, int(affected_indices[i]))
        for i in range(len(affected_indices))
        if would_drop_below[i]
    ]
    return False, violators


def _adjust_budget(
    budget: CoverageBudget,
    src_key: int,
    dst_key: int,
    employee_start: pd.Timestamp,
    transfer_date: pd.Timestamp,
    original_end_date: pd.Timestamp,
    dst_end_date: pd.Timestamp,
    direction: int,
) -> None:
    """Apply (``direction=1``) or revert (``direction=-1``) one transfer's
    effect on the coverage matrix."""
    start_ns = np.datetime64(employee_start, "ns")
    td_ns = np.datetime64(transfer_date, "ns")
    src_end_ns = np.datetime64(original_end_date, "ns")
    dst_end_ns = np.datetime64(dst_end_date, "ns")

    src_idx = budget.key_to_store_idx.get(int(src_key))
    if src_idx is not None:
        loss_mask = _affected_source_months(budget, start_ns, td_ns, src_end_ns)
        if loss_mask.any():
            budget.cov[src_idx, loss_mask] -= direction

    dst_idx = budget.key_to_store_idx.get(int(dst_key))
    if dst_idx is not None:
        gain_mask = _affected_dest_months(budget, td_ns, dst_end_ns)
        if gain_mask.any():
            budget.cov[dst_idx, gain_mask] += direction


def _budget_violations(budget: CoverageBudget) -> List[Tuple[int, int]]:
    """Return list of constrained (store_idx, month_idx) cells with cov < min."""
    bad = budget.constrained & (budget.cov < budget.min_coverage)
    if not bad.any():
        return []
    s_idx, m_idx = np.where(bad)
    return list(zip(s_idx.tolist(), m_idx.tolist()))


@dataclass
class _TransferRecord:
    """Per-transfer book-keeping used for surgical rollback.

    ``chain_next`` is the index (in the shared ``transfer_records`` list) of the
    next transfer for the *same* employee, or ``None`` when this is the current
    tail of the employee's chain. A transfer's destination row is the *source*
    row of its successor, so rolling back an inner hop while a later hop survives
    would restore the inner source to full length even though a later
    destination row still exists — two primaries and an overlapping window. The
    rollback logic follows these links so a chain is only ever unwound from its
    tail.
    """
    employee_key: int
    src_key: int
    dst_key: int
    employee_start: pd.Timestamp
    transfer_date: pd.Timestamp
    original_end: pd.Timestamp
    dst_end: pd.Timestamp
    rolled_back: bool = False
    chain_next: Optional[int] = None


def _select_rollback_indices(
    transfer_records: List[_TransferRecord],
    budget: CoverageBudget,
    violations: List[Tuple[int, int]],
) -> List[int]:
    """Pick the smallest set of transfers whose rollback covers all violations.

    Greedy: walk transfers most-recent-first and pick any whose source-loss
    months intersect a remaining violation. Transfers whose source store has
    no violations are skipped without computing the loss mask.

    Chain-aware: selecting a hop cascades to every later un-rolled-back hop of
    the same employee's chain (via ``chain_next``), so an inner hop is never
    rolled back on its own — which would corrupt the bridge (see
    ``_TransferRecord``). Each cascaded hop's freed source-months are also
    cleared from the outstanding violation set.
    """
    if not violations:
        return []
    violation_set = set(violations)
    violated_src_idx = {s for s, _ in violation_set}
    rollback: set[int] = set()

    def _chain_suffix(ti: int) -> List[int]:
        """``ti`` and its not-yet-selected employee-chain successors."""
        out: List[int] = []
        cur: Optional[int] = ti
        while cur is not None:
            rec = transfer_records[cur]
            if not rec.rolled_back and cur not in rollback:
                out.append(cur)
            cur = rec.chain_next
        return out

    for ti in range(len(transfer_records) - 1, -1, -1):
        rec = transfer_records[ti]
        if rec.rolled_back or ti in rollback:
            continue
        src_idx = budget.key_to_store_idx.get(int(rec.src_key))
        if src_idx is None or src_idx not in violated_src_idx:
            continue
        loss_mask = _affected_source_months(
            budget,
            np.datetime64(rec.employee_start, "ns"),
            np.datetime64(rec.transfer_date, "ns"),
            np.datetime64(rec.original_end, "ns"),
        )
        loss_months = np.where(loss_mask)[0]
        hit = any((src_idx, int(m)) in violation_set for m in loss_months)
        if not hit:
            continue
        for cti in _chain_suffix(ti):
            rollback.add(cti)
            crec = transfer_records[cti]
            csrc_idx = budget.key_to_store_idx.get(int(crec.src_key))
            if csrc_idx is None:
                continue
            cmask = _affected_source_months(
                budget,
                np.datetime64(crec.employee_start, "ns"),
                np.datetime64(crec.transfer_date, "ns"),
                np.datetime64(crec.original_end, "ns"),
            )
            for m in np.where(cmask)[0]:
                violation_set.discard((csrc_idx, int(m)))
        if not violation_set:
            break
    return sorted(rollback)


def _surgical_rollback(
    df: pd.DataFrame,
    budget: CoverageBudget,
    transfer_records: List[_TransferRecord],
    max_attempts: int = 3,
) -> int:
    """Roll back the smallest set of transfers needed to clear violations.

    Mutates ``df`` in place and updates ``budget``/``transfer_records``.
    Returns the number of transfers that were rolled back.

    Selected hops are processed tail-first per employee chain (descending record
    index) so a restored source row is never re-dropped by an earlier hop in the
    same attempt.
    """
    rolled_back_count = 0
    for _ in range(max_attempts):
        violations = _budget_violations(budget)
        if not violations:
            break
        rb_indices = _select_rollback_indices(transfer_records, budget, violations)
        if not rb_indices:
            break

        # Build a once-per-attempt index for O(1) source-row and dest-row lookup
        # instead of repeated full-DataFrame boolean scans.
        sig_to_idx: Dict[Tuple[int, int, pd.Timestamp], int] = {}
        for idx, row in zip(df.index, df.itertuples(index=False)):
            sig_to_idx[(int(row.EmployeeKey), int(row.StoreKey), row.StartDate)] = idx

        drop_idx: List[int] = []
        for ti in sorted(rb_indices, reverse=True):
            rec = transfer_records[ti]
            if rec.rolled_back:
                continue
            _adjust_budget(
                budget, rec.src_key, rec.dst_key,
                rec.employee_start, rec.transfer_date, rec.original_end, rec.dst_end,
                direction=-1,
            )
            src_row = sig_to_idx.get((rec.employee_key, rec.src_key, rec.employee_start))
            if src_row is not None:
                df.at[src_row, "EndDate"] = rec.original_end
                df.at[src_row, "IsPrimary"] = True
                df.at[src_row, "Status"] = "Active"
            dst_row = sig_to_idx.get((rec.employee_key, rec.dst_key, rec.transfer_date))
            if dst_row is not None:
                drop_idx.append(dst_row)
            rec.rolled_back = True
            rolled_back_count += 1

        if drop_idx:
            df.drop(index=drop_idx, inplace=True)
            df.reset_index(drop=True, inplace=True)
    return rolled_back_count


def check_coverage_invariant(
    assignments: pd.DataFrame,
    stores: pd.DataFrame,
    salesperson_roles: List[str],
    global_start: pd.Timestamp,
    global_end: pd.Timestamp,
    open_dates: Dict[int, pd.Timestamp],
    close_dates: Dict[int, pd.Timestamp],
    reno_start_dates: Optional[Dict[int, pd.Timestamp]] = None,
    reno_end_dates: Optional[Dict[int, pd.Timestamp]] = None,
    min_coverage: int = 1,
) -> List[Tuple[int, str]]:
    """Check that every open physical store has at least ``min_coverage``
    salespeople active *throughout* every month — i.e. for both first-of-month
    and last-of-month staffing. The last month_end is clamped to ``global_end``.

    Returns a list of ``(StoreKey, 'YYYY-MM')`` violations (capped at 10).
    """
    budget = _build_coverage_budget(
        assignments, stores, salesperson_roles,
        global_start, global_end,
        open_dates, close_dates, reno_start_dates, reno_end_dates,
        min_coverage=min_coverage,
    )
    violations: List[Tuple[int, str]] = []
    bad = budget.constrained & (budget.cov < budget.min_coverage)
    if not bad.any():
        return violations
    s_idx, m_idx = np.where(bad)
    for i in range(min(len(s_idx), 10)):
        sk = int(budget.store_idx_to_key[s_idx[i]])
        month_iso = pd.Timestamp(budget.month_starts[m_idx[i]]).strftime("%Y-%m")
        violations.append((sk, month_iso))
    return violations
