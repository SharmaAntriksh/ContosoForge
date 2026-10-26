"""Tests for the salesperson-coverage pre-flight (src.facts.sales.prep.coverage_preflight).

Real generation never produces a coverage gap (the dimension guards prevent it),
so these tests build a synthetic bridge with a deliberate gap to exercise
detection and the abort / skip / repair policies.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from src.exceptions import SalesError
from src.facts.sales.prep.coverage_preflight import (
    analyze_coverage,
    repair_bridge,
    run_coverage_preflight,
)

ROLES = ["Sales Associate", "Online Sales Representative"]
SA = 40_001_001  # staff-band EmployeeKey


def _employees(hire="2020-01-01", termination=None):
    return pd.DataFrame({
        "EmployeeKey": [SA],
        "HireDate": [pd.Timestamp(hire)],
        "TerminationDate": [pd.Timestamp(termination) if termination else pd.NaT],
    })


def _stores(closing=None, reno=(None, None)):
    return pd.DataFrame({
        "StoreKey": [1],
        "OpeningDate": [pd.Timestamp("2020-01-01")],
        "ClosingDate": [pd.Timestamp(closing) if closing else pd.NaT],
        "RenovationStartDate": [pd.Timestamp(reno[0]) if reno[0] else pd.NaT],
        "RenovationEndDate": [pd.Timestamp(reno[1]) if reno[1] else pd.NaT],
    })


def _bridge(intervals):
    """intervals: list of (start, end) for the single Sales Associate at store 1."""
    rows = []
    for i, (s, e) in enumerate(intervals, 1):
        rows.append({
            "AssignmentKey": i, "EmployeeKey": SA, "AssignmentSequence": i,
            "StoreKey": 1, "StartDate": pd.Timestamp(s), "EndDate": pd.Timestamp(e),
            "FTE": 1.0, "RoleAtStore": "Sales Associate", "IsCurrentAssignment": True,
            "TransferReason": "Initial", "AssignmentStatus": "Active",
        })
    return pd.DataFrame(rows)


WIN = (pd.Timestamp("2021-01-01"), pd.Timestamp("2021-06-30"))


class TestAnalyzeCoverage:
    def test_full_coverage_no_gap(self):
        rep = analyze_coverage(_stores(), _bridge([("2020-06-01", "2021-12-31")]), *WIN, ROLES)
        assert rep.n_gap_cells == 0
        assert not rep.has_avoidable_loss

    def test_mid_window_gap_detected(self):
        # SA covers Jan-Feb and May-Jun; Mar+Apr are uncovered (store open all month).
        b = _bridge([("2020-06-01", "2021-02-28"), ("2021-05-01", "2021-12-31")])
        rep = analyze_coverage(_stores(), b, *WIN, ROLES)
        gap_months = sorted({m.strftime("%Y-%m") for _, m, *_ in rep.gap_cells})
        assert gap_months == ["2021-03", "2021-04"]
        assert {d.strftime("%Y-%m") for d in rep.uncovered_months} == {"2021-03", "2021-04"}
        assert rep.has_avoidable_loss
        assert rep.n_fully_open_gaps == 2

    def test_interior_mid_month_hole_detected(self):
        # Store 1 is staffed on Jan 1 (assignment ends Jan 10) and on Jan 31
        # (next assignment starts Jan 20), but Jan 11-19 has no salesperson.
        # The first/last-day test alone marks January covered; interval-union
        # catches the interior hole (those days would drop to EmployeeKey=-1).
        b = _bridge([("2020-06-01", "2021-01-10"), ("2021-01-20", "2021-12-31")])
        rep = analyze_coverage(_stores(), b, *WIN, ROLES)
        gap_months = sorted({m.strftime("%Y-%m") for _, m, *_ in rep.gap_cells})
        assert gap_months == ["2021-01"]
        assert rep.n_fully_open_gaps == 1
        assert rep.interior_hole_gaps == 1
        # Feb-Jun are fully covered by the second assignment: no false positives.
        assert rep.n_gap_cells == 1

    def test_contiguous_two_assignments_no_hole(self):
        # Two back-to-back assignments (adjacent days) fully cover the window —
        # interval-union must NOT report a phantom gap at the seam.
        b = _bridge([("2020-06-01", "2021-03-15"), ("2021-03-16", "2021-12-31")])
        rep = analyze_coverage(_stores(), b, *WIN, ROLES)
        assert rep.n_gap_cells == 0
        assert rep.interior_hole_gaps == 0
        assert not rep.has_avoidable_loss

    def test_online_and_physical_checked_independently(self):
        # An online store with its OWN online rep is covered; a separate physical
        # gap is still flagged (online reps do not staff physical stores).
        stores = pd.concat([_stores(), pd.DataFrame({
            "StoreKey": [10_001], "OpeningDate": [pd.Timestamp("2020-01-01")],
            "ClosingDate": [pd.NaT], "RenovationStartDate": [pd.NaT],
            "RenovationEndDate": [pd.NaT],
        })], ignore_index=True)
        # physical SA leaves a Mar/Apr gap; online rep covers the whole window.
        b = _bridge([("2020-06-01", "2021-02-28"), ("2021-05-01", "2021-12-31")])
        b = pd.concat([b, pd.DataFrame([{
            "AssignmentKey": 99, "EmployeeKey": 50_010_001, "AssignmentSequence": 1,
            "StoreKey": 10_001, "StartDate": pd.Timestamp("2020-06-01"),
            "EndDate": pd.Timestamp("2021-12-31"), "FTE": 1.0,
            "RoleAtStore": "Online Sales Representative", "IsCurrentAssignment": True,
            "TransferReason": "Initial", "AssignmentStatus": "Active",
        }])], ignore_index=True)
        rep = analyze_coverage(stores, b, *WIN, ROLES)
        gap_stores = {s for s, *_ in rep.gap_cells}
        assert gap_stores == {1}            # only the physical store has a gap
        assert rep.has_avoidable_loss       # the physical gap is real data loss


class TestRepair:
    @pytest.mark.parametrize("hire,termination,interval", [
        ("2021-03-15", None, ("2021-03-15", "2021-06-30")),
        ("2020-01-01", "2021-03-15", ("2021-01-01", "2021-03-15")),
    ])
    def test_repair_does_not_invent_employment(self, hire, termination, interval):
        bridge = _bridge([interval])
        employees = _employees(hire, termination)
        before = employees.copy(deep=True)
        report = analyze_coverage(_stores(), bridge, *WIN, ROLES)
        out, n = repair_bridge(bridge, report, ROLES, employees)
        assert n == 0
        pd.testing.assert_frame_equal(out, bridge)
        pd.testing.assert_frame_equal(employees, before)

    def test_repair_accepts_inclusive_employment_boundaries(self):
        bridge = _bridge([("2021-02-01", "2021-05-31")])
        report = analyze_coverage(_stores(), bridge, *WIN, ROLES)
        out, n = repair_bridge(bridge, report, ROLES, _employees(*WIN))
        assert n == 2
        assert out.iloc[0]["StartDate"] == WIN[0]
        assert out.iloc[0]["EndDate"] == WIN[1]

    @pytest.mark.parametrize("metadata", ["unknown", "missing_hire", "bad_term"])
    def test_repair_refuses_unverifiable_employment(self, metadata):
        bridge = _bridge([("2021-02-01", "2021-05-31")])
        employees = _employees()
        if metadata == "unknown":
            employees["EmployeeKey"] = SA + 1
        elif metadata == "missing_hire":
            employees["HireDate"] = pd.NaT
        else:
            employees["TerminationDate"] = "invalid"
        report = analyze_coverage(_stores(), bridge, *WIN, ROLES)
        out, n = repair_bridge(bridge, report, ROLES, employees)
        assert n == 0
        pd.testing.assert_frame_equal(out, bridge)

    def test_repair_respects_overlapping_non_sales_job(self):
        bridge = _bridge([("2021-02-01", "2021-06-30")])
        other = bridge.iloc[0].copy()
        other["StoreKey"] = 2
        other["RoleAtStore"] = "Cashier"
        other["StartDate"] = WIN[0]
        other["EndDate"] = pd.Timestamp("2021-01-31")
        bridge = pd.concat([bridge, other.to_frame().T], ignore_index=True)
        report = analyze_coverage(_stores(), bridge, *WIN, ROLES)
        out, n = repair_bridge(bridge, report, ROLES, _employees())
        assert n == 0
        pd.testing.assert_frame_equal(out, bridge)

    def test_repair_tries_another_employee_with_valid_employment(self):
        bridge = _bridge([("2021-02-01", "2021-06-30")])
        alternative = bridge.copy()
        alternative["EmployeeKey"] = SA + 1
        bridge = pd.concat([bridge, alternative], ignore_index=True)
        employees = pd.concat([
            _employees("2021-02-01"),
            _employees().assign(EmployeeKey=SA + 1),
        ], ignore_index=True)
        report = analyze_coverage(_stores(), bridge, *WIN, ROLES)
        out, n = repair_bridge(bridge, report, ROLES, employees)
        assert n == 1
        assert out.loc[out["EmployeeKey"] == SA, "StartDate"].iloc[0] == pd.Timestamp("2021-02-01")
        assert out.loc[out["EmployeeKey"] == SA + 1, "StartDate"].iloc[0] == WIN[0]

    @pytest.mark.parametrize("invalid", ["missing_dates", "duplicate_identity"])
    def test_repair_rejects_ambiguous_employment_metadata(self, invalid):
        bridge = _bridge([("2021-02-01", "2021-06-30")])
        employees = _employees()
        if invalid == "missing_dates":
            employees = employees.drop(columns="HireDate")
        else:
            employees = pd.concat([employees, employees], ignore_index=True)
        report = analyze_coverage(_stores(), bridge, *WIN, ROLES)
        with pytest.raises(SalesError, match="requires unique employee identities"):
            repair_bridge(bridge, report, ROLES, employees)

    def test_repair_closes_gap(self):
        b = _bridge([("2020-06-01", "2021-02-28"), ("2021-05-01", "2021-12-31")])
        rep = analyze_coverage(_stores(), b, *WIN, ROLES)
        repaired, n = repair_bridge(b, rep, ROLES, _employees())
        assert n > 0
        recheck = analyze_coverage(_stores(), repaired, *WIN, ROLES)
        assert recheck.n_gap_cells == 0
        assert not recheck.has_avoidable_loss

    def test_repair_never_double_books_an_employee(self):
        # E works store 1 (Jan-Jun) then transfers to store 2 (Jul-Dec). Each
        # store has a gap in the other half-year, and E is the only salesperson.
        # Extending E into either gap would place E at two stores at once, so
        # repair must refuse — the per-employee no-overlap invariant must hold.
        stores = pd.DataFrame({
            "StoreKey": [1, 2],
            "OpeningDate": [pd.Timestamp("2020-01-01")] * 2,
            "ClosingDate": [pd.NaT] * 2,
            "RenovationStartDate": [pd.NaT] * 2,
            "RenovationEndDate": [pd.NaT] * 2,
        })
        E = 40_001_001
        common = dict(FTE=1.0, RoleAtStore="Sales Associate", IsCurrentAssignment=True,
                      TransferReason="Initial", AssignmentStatus="Active")
        bridge = pd.DataFrame([
            {"AssignmentKey": 1, "EmployeeKey": E, "AssignmentSequence": 1, "StoreKey": 1,
             "StartDate": pd.Timestamp("2021-01-01"), "EndDate": pd.Timestamp("2021-06-30"), **common},
            {"AssignmentKey": 2, "EmployeeKey": E, "AssignmentSequence": 2, "StoreKey": 2,
             "StartDate": pd.Timestamp("2021-07-01"), "EndDate": pd.Timestamp("2021-12-31"), **common},
        ])
        win = (pd.Timestamp("2021-01-01"), pd.Timestamp("2021-12-31"))
        rep = analyze_coverage(stores, bridge, *win, ROLES)
        assert rep.has_avoidable_loss  # both stores have half-year gaps
        repaired, _n = repair_bridge(bridge, rep, ROLES, _employees())
        # no employee may hold two assignments that overlap in time
        g = repaired.sort_values(["EmployeeKey", "StartDate"])
        for ek, grp in g.groupby("EmployeeKey"):
            sd = pd.to_datetime(grp["StartDate"]).tolist()
            ed = pd.to_datetime(grp["EndDate"]).tolist()
            for i in range(len(grp) - 1):
                assert ed[i] < sd[i + 1], f"employee {ek} double-booked by repair"


    def test_repair_is_deterministic_and_order_independent(self):
        # Principled repair must produce identical output regardless of the order
        # gap_cells arrive in (the core #39 determinism guarantee).
        import random

        b = _bridge([("2020-06-01", "2021-02-28"), ("2021-05-01", "2021-12-31")])
        rep_a = analyze_coverage(_stores(), b, *WIN, ROLES)
        repaired_a, n_a = repair_bridge(b, rep_a, ROLES, _employees())

        rep_b = analyze_coverage(_stores(), b, *WIN, ROLES)
        random.Random(20260701).shuffle(rep_b.gap_cells)
        repaired_b, n_b = repair_bridge(b, rep_b, ROLES, _employees())

        assert n_a == n_b > 0
        cols = ["EmployeeKey", "StoreKey", "StartDate", "EndDate", "TransferReason"]
        a = repaired_a.sort_values(["EmployeeKey", "StartDate"]).reset_index(drop=True)[cols]
        c = repaired_b.sort_values(["EmployeeKey", "StartDate"]).reset_index(drop=True)[cols]
        pd.testing.assert_frame_equal(a, c)

    def test_repair_tags_synthetic_extensions(self):
        # Extended assignments are stamped 'Coverage Gap Repair'; untouched rows
        # keep their original reason.
        b = _bridge([("2020-06-01", "2021-02-28"), ("2021-05-01", "2021-12-31")])
        rep = analyze_coverage(_stores(), b, *WIN, ROLES)
        repaired, n = repair_bridge(b, rep, ROLES, _employees())
        assert n > 0
        reasons = set(repaired["TransferReason"])
        assert "Coverage Gap Repair" in reasons
        # One assignment may absorb several adjacent gap-months, so distinct
        # tagged rows are >= 1 and <= the number of gap store-months closed.
        assert 1 <= (repaired["TransferReason"] == "Coverage Gap Repair").sum() <= n
        # A clean bridge is returned untouched (no tag, original object).
        clean = _bridge([("2020-06-01", "2021-12-31")])
        clean_rep = analyze_coverage(_stores(), clean, *WIN, ROLES)
        out, n0 = repair_bridge(clean, clean_rep, ROLES, _employees())
        assert n0 == 0
        assert "Coverage Gap Repair" not in set(out["TransferReason"])


class TestPolicies:
    def _cfg(self, policy):
        return SimpleNamespace(
            sales=SimpleNamespace(coverage_policy=policy, salesperson_roles=None),
            employees=SimpleNamespace(
                store_assignments=SimpleNamespace(primary_sales_role="Sales Associate")),
        )

    def _write(self, tmp_path, bridge):
        _stores().to_parquet(tmp_path / "stores.parquet", index=False)
        bridge.to_parquet(tmp_path / "employee_store_assignments.parquet", index=False)
        _employees().to_parquet(tmp_path / "employees.parquet", index=False)

    def test_abort_raises(self, tmp_path):
        self._write(tmp_path, _bridge([("2020-06-01", "2021-02-28"), ("2021-05-01", "2021-12-31")]))
        with pytest.raises(SalesError, match="coverage gap"):
            run_coverage_preflight(self._cfg("abort"), tmp_path, *WIN)

    def test_skip_does_not_raise(self, tmp_path):
        self._write(tmp_path, _bridge([("2020-06-01", "2021-02-28"), ("2021-05-01", "2021-12-31")]))
        run_coverage_preflight(self._cfg("skip"), tmp_path, *WIN)  # no exception

    def test_repair_rewrites_bridge(self, tmp_path):
        self._write(tmp_path, _bridge([("2020-06-01", "2021-02-28"), ("2021-05-01", "2021-12-31")]))
        run_coverage_preflight(self._cfg("repair"), tmp_path, *WIN)
        fixed = pd.read_parquet(tmp_path / "employee_store_assignments.parquet")
        rep = analyze_coverage(_stores(), fixed, *WIN, ROLES)
        assert not rep.has_avoidable_loss

    def test_repair_requires_employee_file_without_rewriting_bridge(self, tmp_path):
        bridge = _bridge([("2021-02-01", "2021-06-30")])
        self._write(tmp_path, bridge)
        (tmp_path / "employees.parquet").unlink()
        before = (tmp_path / "employee_store_assignments.parquet").read_bytes()
        with pytest.raises(SalesError, match="requires employees.parquet"):
            run_coverage_preflight(self._cfg("repair"), tmp_path, *WIN)
        assert (tmp_path / "employee_store_assignments.parquet").read_bytes() == before

    def test_clean_config_no_raise_under_abort(self, tmp_path):
        self._write(tmp_path, _bridge([("2020-06-01", "2021-12-31")]))
        run_coverage_preflight(self._cfg("abort"), tmp_path, *WIN)  # no gap -> no raise
