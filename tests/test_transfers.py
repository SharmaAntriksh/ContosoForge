"""Unit tests for the employee transfer engine's coverage-budget primitives.

These cover the budget-aware selection logic that replaced the previous
per-candidate guard. The full integration is exercised via the dimensions
runner tests; these tests pin down the building blocks in isolation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.dimensions.employees.assignment_core import (
    _adjust_budget,
    _budget_violations,
    _build_coverage_budget,
    _is_transfer_feasible,
    _select_rollback_indices,
    _surgical_rollback,
    _TransferRecord,
    check_coverage_invariant,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ROLE = ["Sales Associate"]


def _stores(keys, opening="2020-01-01", closing=None, reno=None):
    rows = []
    for k in keys:
        row = {
            "StoreKey": int(k),
            "OpeningDate": pd.Timestamp(opening),
            "ClosingDate": pd.Timestamp(closing) if closing else pd.NaT,
            "RenovationStartDate": pd.Timestamp(reno[0]) if reno else pd.NaT,
            "RenovationEndDate": pd.Timestamp(reno[1]) if reno else pd.NaT,
            "StoreRegion": "R",
        }
        rows.append(row)
    return pd.DataFrame(rows)


def _assignments(rows):
    return pd.DataFrame([
        {
            "EmployeeKey": int(r[0]),
            "StoreKey": int(r[1]),
            "StartDate": pd.Timestamp(r[2]),
            "EndDate": pd.Timestamp(r[3]),
            "FTE": 1.0,
            "RoleAtStore": "Sales Associate",
            "IsPrimary": True,
            "Status": "Active",
        }
        for r in rows
    ])


def _date_dicts(stores):
    """Build the open/close/reno date dicts the way apply_transfers does."""
    open_dates, close_dates, reno_s, reno_e = {}, {}, {}, {}
    for _, row in stores.iterrows():
        sk = int(row["StoreKey"])
        if pd.notna(row.get("OpeningDate")):
            open_dates[sk] = pd.Timestamp(row["OpeningDate"])
        if pd.notna(row.get("ClosingDate")):
            close_dates[sk] = pd.Timestamp(row["ClosingDate"])
        if pd.notna(row.get("RenovationStartDate")) and pd.notna(row.get("RenovationEndDate")):
            reno_s[sk] = pd.Timestamp(row["RenovationStartDate"])
            reno_e[sk] = pd.Timestamp(row["RenovationEndDate"])
    return open_dates, close_dates, reno_s, reno_e


# ---------------------------------------------------------------------------
# _build_coverage_budget
# ---------------------------------------------------------------------------

class TestBuildCoverageBudget:
    def test_counts_full_month_coverage_only(self):
        # Employee starts mid-Feb -> Feb is NOT counted; Mar+ are.
        stores = _stores([1])
        assigns = _assignments([(101, 1, "2024-02-15", "2024-12-31")])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        # months: Jan..Dec; cov for store_idx=0
        cov = budget.cov[0]
        # Jan: not covered (start in Feb), Feb: not covered (start mid-Feb),
        # Mar..Dec: covered.
        assert cov[0] == 0
        assert cov[1] == 0
        assert all(c == 1 for c in cov[2:])

    def test_renovation_months_unconstrained(self):
        stores = _stores([1], reno=("2024-04-01", "2024-06-30"))
        assigns = _assignments([(101, 1, "2024-01-01", "2024-12-31")])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        # Apr/May/Jun (and possibly months overlapping reno) should be unconstrained
        c = budget.constrained[0]
        # Jan/Feb/Mar constrained; Apr/May/Jun renovating; Jul..Dec constrained
        assert c[0] and c[1] and c[2]
        assert not c[3] and not c[4] and not c[5]
        assert all(c[6:])

    def test_pre_open_and_post_close_unconstrained(self):
        stores = _stores([1], opening="2024-04-01", closing="2024-10-01")
        assigns = _assignments([(101, 1, "2024-04-01", "2024-09-30")])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        c = budget.constrained[0]
        # Jan-Mar unconstrained (pre-open), Apr-Sep constrained (closes Oct 1),
        # Oct-Dec unconstrained (post-close).
        assert not c[0] and not c[1] and not c[2]
        assert c[3] and c[4] and c[5] and c[6] and c[7] and c[8]
        assert not c[9] and not c[10] and not c[11]

    def test_global_end_midmonth_clamps_last_month(self):
        stores = _stores([1])
        assigns = _assignments([(101, 1, "2024-01-01", "2024-06-15")])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        # global_end mid-month: budget's last month_end clamps to global_end
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-06-15"),
            open_d, close_d, rs, re_,
        )
        # Last month is June, with month_end clamped to 2024-06-15.
        # Assignment ends 2024-06-15, so it covers June fully under the clamp.
        assert pd.Timestamp(budget.month_ends[-1]) == pd.Timestamp("2024-06-15")
        assert budget.cov[0, -1] == 1

    def test_online_store_excluded_from_axis(self):
        from src.defaults import ONLINE_STORE_KEY_BASE
        stores = pd.concat([
            _stores([1, 2]),
            _stores([ONLINE_STORE_KEY_BASE + 1]),
        ], ignore_index=True)
        assigns = _assignments([(101, 1, "2024-01-01", "2024-12-31")])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        assert ONLINE_STORE_KEY_BASE + 1 not in budget.key_to_store_idx
        assert set(budget.store_idx_to_key.tolist()) == {1, 2}


# ---------------------------------------------------------------------------
# _is_transfer_feasible
# ---------------------------------------------------------------------------

class TestIsTransferFeasible:
    def _build(self, n_emp_at_store=2, **kwargs):
        stores = _stores([1, 2])
        rows = [(100 + i, 1, "2024-01-01", "2024-12-31") for i in range(n_emp_at_store)]
        rows.append((200, 2, "2024-01-01", "2024-12-31"))
        assigns = _assignments(rows)
        open_d, close_d, rs, re_ = _date_dicts(stores)
        return _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
            **kwargs,
        )

    def test_blocks_solo_salesperson(self):
        budget = self._build(n_emp_at_store=1)
        feasible, violators = _is_transfer_feasible(
            budget, src_key=1,
            employee_start=pd.Timestamp("2024-01-01"),
            transfer_date=pd.Timestamp("2024-06-15"),
            original_end_date=pd.Timestamp("2024-12-31"),
        )
        assert not feasible
        assert len(violators) >= 1

    def test_allows_when_two_salespeople(self):
        budget = self._build(n_emp_at_store=2)
        feasible, violators = _is_transfer_feasible(
            budget, src_key=1,
            employee_start=pd.Timestamp("2024-01-01"),
            transfer_date=pd.Timestamp("2024-06-15"),
            original_end_date=pd.Timestamp("2024-12-31"),
        )
        assert feasible
        assert violators == []

    def test_unconstrained_cells_skip_check(self):
        # Source store renovating from Jul-Sep -> those months unconstrained.
        # A solo salesperson would normally block transfer, but with the
        # months unconstrained the loss is allowed.
        stores = _stores([1, 2], reno=("2024-07-01", "2024-09-30"))
        # Renovation only on store 1; rebuild with a separate stores df
        s1 = _stores([1], reno=("2024-07-01", "2024-09-30"))
        s2 = _stores([2])
        stores = pd.concat([s1, s2], ignore_index=True)
        assigns = _assignments([
            (101, 1, "2024-01-01", "2024-06-30"),
            (200, 2, "2024-01-01", "2024-12-31"),
        ])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        # Transfer this employee in July; loss months are Jul-onwards but those
        # are unconstrained at store 1 due to renovation, plus the assignment
        # ends in June so loss-month set is empty.
        feasible, _violators = _is_transfer_feasible(
            budget, src_key=1,
            employee_start=pd.Timestamp("2024-01-01"),
            transfer_date=pd.Timestamp("2024-07-15"),
            original_end_date=pd.Timestamp("2024-06-30"),
        )
        assert feasible


# ---------------------------------------------------------------------------
# _adjust_budget (apply / revert via direction)
# ---------------------------------------------------------------------------

class TestApplyAndRevert:
    def test_apply_decrements_source_increments_dest(self):
        stores = _stores([1, 2])
        assigns = _assignments([
            (101, 1, "2024-01-01", "2024-12-31"),
            (102, 1, "2024-01-01", "2024-12-31"),
            (200, 2, "2024-01-01", "2024-12-31"),
        ])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        before_src = budget.cov[budget.key_to_store_idx[1]].copy()
        before_dst = budget.cov[budget.key_to_store_idx[2]].copy()
        _adjust_budget(
            budget, src_key=1, dst_key=2,
            employee_start=pd.Timestamp("2024-01-01"),
            transfer_date=pd.Timestamp("2024-07-15"),
            original_end_date=pd.Timestamp("2024-12-31"),
            dst_end_date=pd.Timestamp("2024-12-31"),
            direction=1,
        )
        # Source loses Jul..Dec coverage (transfer mid-July; month_end >= td and m_end <= old_end)
        # So month_idx 6..11 should each drop by 1 at source
        diff_src = budget.cov[budget.key_to_store_idx[1]] - before_src
        diff_dst = budget.cov[budget.key_to_store_idx[2]] - before_dst
        assert (diff_src[6:12] == -1).all()
        assert (diff_src[:6] == 0).all()
        # Dest gains Aug..Dec (Aug is first full month after Jul-15 transfer)
        assert (diff_dst[7:12] == 1).all()
        assert (diff_dst[:7] == 0).all()

    def test_revert_undoes_apply(self):
        stores = _stores([1, 2])
        assigns = _assignments([
            (101, 1, "2024-01-01", "2024-12-31"),
            (102, 1, "2024-01-01", "2024-12-31"),
            (200, 2, "2024-01-01", "2024-12-31"),
        ])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        snapshot = budget.cov.copy()
        kwargs = dict(
            src_key=1, dst_key=2,
            employee_start=pd.Timestamp("2024-01-01"),
            transfer_date=pd.Timestamp("2024-07-15"),
            original_end_date=pd.Timestamp("2024-12-31"),
            dst_end_date=pd.Timestamp("2024-12-31"),
        )
        _adjust_budget(budget, direction=1, **kwargs)
        _adjust_budget(budget, direction=-1, **kwargs)
        np.testing.assert_array_equal(budget.cov, snapshot)


# ---------------------------------------------------------------------------
# _budget_violations + _select_rollback_indices
# ---------------------------------------------------------------------------

class TestRollbackSelection:
    def test_no_violations_returns_empty(self):
        stores = _stores([1])
        assigns = _assignments([(101, 1, "2024-01-01", "2024-12-31")])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        assert _budget_violations(budget) == []

    def test_select_rollback_drops_offending_only(self):
        stores = _stores([1, 2])
        # Two salespeople at store 1; transferring both would drop coverage.
        assigns = _assignments([
            (101, 1, "2024-01-01", "2024-12-31"),
            (102, 1, "2024-01-01", "2024-12-31"),
            (200, 2, "2024-01-01", "2024-12-31"),
        ])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        budget = _build_coverage_budget(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        # Apply two transfers (force-violate by skipping feasibility check)
        rec1 = _TransferRecord(
            employee_key=101, src_key=1, dst_key=2,
            employee_start=pd.Timestamp("2024-01-01"),
            transfer_date=pd.Timestamp("2024-07-15"),
            original_end=pd.Timestamp("2024-12-31"),
            dst_end=pd.Timestamp("2024-12-31"),
        )
        rec2 = _TransferRecord(
            employee_key=102, src_key=1, dst_key=2,
            employee_start=pd.Timestamp("2024-01-01"),
            transfer_date=pd.Timestamp("2024-08-15"),
            original_end=pd.Timestamp("2024-12-31"),
            dst_end=pd.Timestamp("2024-12-31"),
        )
        for rec in (rec1, rec2):
            _adjust_budget(
                budget, src_key=rec.src_key, dst_key=rec.dst_key,
                employee_start=rec.employee_start, transfer_date=rec.transfer_date,
                original_end_date=rec.original_end, dst_end_date=rec.dst_end,
                direction=1,
            )
        violations = _budget_violations(budget)
        assert violations  # store 1 has 0 cov on Aug..Dec
        rb = _select_rollback_indices([rec1, rec2], budget, violations)
        # Rolling back the most recent (rec2) should clear the late-month violations;
        # then rec1 covers up to its loss months. Greedy picks recent-first.
        assert 1 in rb


# ---------------------------------------------------------------------------
# Chain-aware rollback (an inner hop must not be rolled back on its own)
# ---------------------------------------------------------------------------

class TestRollbackChain:
    """A rolled-back inner hop of a transfer chain must not orphan a later hop.

    Regression guard: employee 101 is transferred 1->2->3 (a two-hop chain).
    Only store 1's coverage collapses (its lone salesperson left), so the greedy
    picker on its own would roll back just the first hop (1->2) — restoring the
    full-length row at store 1 while the primary row at store 3 still exists,
    i.e. two primaries and an overlapping window. The chain-aware rollback must
    cascade and unwind the whole chain from its tail.
    """

    def _setup(self):
        stores = _stores([1, 2, 3])
        gstart, gend = pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31")
        td1, td2 = pd.Timestamp("2024-02-15"), pd.Timestamp("2024-08-15")
        open_d, close_d, rs, re_ = _date_dicts(stores)
        # Pre-transfer bridge: 101 alone at store 1; fillers keep 2 and 3 covered.
        base = _assignments([
            (101, 1, "2024-01-01", "2024-12-31"),
            (500, 2, "2024-01-01", "2024-12-31"),
            (600, 3, "2024-01-01", "2024-12-31"),
        ])
        budget = _build_coverage_budget(
            base, stores, ROLE, gstart, gend, open_d, close_d, rs, re_,
        )
        one_day = pd.Timedelta(days=1)

        def _row(ek, sk, start, end, primary, status):
            return {
                "EmployeeKey": ek, "StoreKey": sk, "StartDate": start, "EndDate": end,
                "FTE": 1.0, "RoleAtStore": "Sales Associate",
                "IsPrimary": primary, "Status": status,
            }

        # Post-transfer bridge state for employee 101 (source rows truncated).
        df = pd.DataFrame([
            _row(101, 1, gstart, td1 - one_day, False, "Transferred"),
            _row(101, 2, td1, td2 - one_day, False, "Transferred"),
            _row(101, 3, td2, gend, True, "Active"),
            _row(500, 2, gstart, gend, True, "Active"),
            _row(600, 3, gstart, gend, True, "Active"),
        ])
        t1 = _TransferRecord(
            employee_key=101, src_key=1, dst_key=2,
            employee_start=gstart, transfer_date=td1,
            original_end=gend, dst_end=gend, chain_next=1,
        )
        t2 = _TransferRecord(
            employee_key=101, src_key=2, dst_key=3,
            employee_start=td1, transfer_date=td2,
            original_end=gend, dst_end=gend,
        )
        for rec in (t1, t2):
            _adjust_budget(
                budget, src_key=rec.src_key, dst_key=rec.dst_key,
                employee_start=rec.employee_start, transfer_date=rec.transfer_date,
                original_end_date=rec.original_end, dst_end_date=rec.dst_end,
                direction=1,
            )
        return df, budget, [t1, t2]

    def test_selection_cascades_whole_chain(self):
        _df, budget, records = self._setup()
        violations = _budget_violations(budget)
        assert violations, "store 1 must be under-covered after the chain"
        rb = _select_rollback_indices(records, budget, violations)
        assert rb == [0, 1], "both hops of the chain must be selected, not just the inner one"

    def test_rollback_leaves_one_primary_no_overlap(self):
        df, budget, records = self._setup()
        n = _surgical_rollback(df, budget, records)
        assert n == 2
        assert _budget_violations(budget) == []
        emp = df[df["EmployeeKey"] == 101]
        # Whole chain unwound: employee restored to a single full-length row at store 1.
        assert len(emp) == 1
        assert int(emp["StoreKey"].iloc[0]) == 1
        assert bool(emp["IsPrimary"].iloc[0]) is True
        assert pd.Timestamp(emp["EndDate"].iloc[0]) == pd.Timestamp("2024-12-31")
        # Exactly one primary per employee, no overlaps left behind.
        for _ek, grp in df.groupby("EmployeeKey"):
            assert int(grp["IsPrimary"].astype(bool).sum()) == 1


# ---------------------------------------------------------------------------
# _check_coverage_invariant (now month-end aware)
# ---------------------------------------------------------------------------

class TestCheckCoverageInvariant:
    def test_returns_no_violations_when_all_covered(self):
        stores = _stores([1])
        assigns = _assignments([(101, 1, "2024-01-01", "2024-12-31")])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        violations = check_coverage_invariant(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        assert violations == []

    def test_detects_midmonth_termination_gap(self):
        # Employee leaves June 15 -> June fails month-end coverage check
        stores = _stores([1])
        assigns = _assignments([(101, 1, "2024-01-01", "2024-06-15")])
        open_d, close_d, rs, re_ = _date_dicts(stores)
        violations = check_coverage_invariant(
            assigns, stores, ROLE,
            pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"),
            open_d, close_d, rs, re_,
        )
        # June, Jul..Dec are all violations (no coverage)
        months = {m for _, m in violations}
        assert "2024-06" in months
        assert "2024-12" in months
