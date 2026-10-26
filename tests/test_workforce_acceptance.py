"""Independent, day-level acceptance checks over isolated generated datasets.

The oracle deliberately does not use workforce lookup, coverage, or quality-report
helpers. Passing proves the sampled business cases, not all configurations.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest
import pyarrow.parquet as pq

from tests import sales_gen


@pytest.fixture(scope="module")
def workforce_run(tmp_path_factory):
    import src.versioning.version_store as versions

    base = tmp_path_factory.mktemp("workforce_acceptance")
    dims = base / "dims"
    dims.mkdir()
    cfg = sales_gen.small_config(
        dims_dir=dims,
        scratch_dir=base / "scratch",
        final_dir=base / "final",
        workers=2,
        chunk_size=1_000,
        total_rows=6_000,
        customers=1_000,
        seed=2468,
    )
    cfg["scale"]["stores"] = 40
    cfg["stores"].update(
        {
            "online_stores": 2,
            "allow_mid_window_openings": True,
            "opening": {"start": "2019-01-01", "end": "2022-06-30"},
            "closing_end": "2023-12-31",
            "closing": {"enabled": True, "close_share": 0.35},
        }
    )
    cfg["employees"]["transfers"].update({"annual_rate": 0.6, "min_tenure_months": 0})
    source = Path(
        cfg["exchange_rates"].get(
            "master_file",
            "data/exchange_rates_master/fx_master.parquet",
        )
    ).resolve()
    if not source.exists():
        pytest.fail(
            f"Isolated workforce generation needs the local FX master: {source}"
        )
    shutil.copyfile(source, base / "fx_master.parquet")
    cfg["exchange_rates"]["master_file"] = str(base / "fx_master.parquet")
    (base / "versions").mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(versions, "VERSION_DIR", base / "versions")
        sales_gen.run_pipeline_stage(base, cfg, "dimensions")
        employees_before = pd.read_parquet(dims / "employees.parquet")
        outputs = {}
        for mode in ("sales", "sales_order"):
            work = base / mode
            work.mkdir()
            cfg["sales"]["sales_output"] = mode
            cfg["sales"]["out_folder"] = str(work / "scratch")
            cfg["defaults"]["final_output"] = str(work / "final")
            sales_gen.run_pipeline_stage(work, cfg, "sales")
            # Find one physical attribution source, avoiding packaged duplicates.
            marker = {"EmployeeKey", "StoreKey", "OrderDate"}
            parts = [
                pd.read_parquet(path)
                for path in (work / "final").rglob("*.parquet")
                if marker <= set(pq.read_schema(path).names)
            ]
            assert parts, f"No attribution source packaged for {mode}"
            outputs[mode] = pd.concat(parts, ignore_index=True)
        pd.testing.assert_frame_equal(
            pd.read_parquet(dims / "employees.parquet"),
            employees_before,
        )
    return (
        pd.read_parquet(dims / "stores.parquet"),
        employees_before,
        pd.read_parquet(dims / "employee_store_assignments.parquet"),
        outputs,
    )


def _operates(store, day):
    return (
        day >= store.OpeningDate
        and (pd.isna(store.ClosingDate) or day < store.ClosingDate)
        and not (
            pd.notna(store.RenovationStartDate)
            and pd.notna(store.RenovationEndDate)
            and store.RenovationStartDate <= day < store.RenovationEndDate
        )
    )


def _employed(employee, day):
    return employee.HireDate <= day and (
        pd.isna(employee.TerminationDate) or day <= employee.TerminationDate
    )


def _sells(employee, assignment, store):
    role = (
        "Online Sales Representative"
        if store.StoreType == "Online"
        else "Sales Associate"
    )
    return (
        bool(employee.IsSalesperson)
        and employee.Title == assignment.RoleAtStore == role
    )


def _rows(frame, key):
    # date32 parquet may load Python date objects; normalize only date columns.
    frame = frame.copy()
    for name in frame:
        if name.endswith("Date"):
            frame[name] = pd.to_datetime(frame[name])
    return {int(getattr(row, key)): row for row in frame.itertuples(index=False)}


def test_generated_workforce_keys_timelines_and_lifecycle_events(workforce_run):
    stores, employees, assignments, _ = workforce_run
    assert stores.StoreKey.is_unique and employees.EmployeeKey.is_unique
    assert assignments.AssignmentKey.is_unique
    assert set(assignments.EmployeeKey) <= set(employees.EmployeeKey)
    assert set(assignments.StoreKey) <= set(stores.StoreKey)
    assert set(employees.ParentEmployeeKey.dropna()) <= set(employees.EmployeeKey)
    store_rows = _rows(stores, "StoreKey")
    employee_rows = _rows(employees, "EmployeeKey")
    ordered = assignments.sort_values(["EmployeeKey", "StartDate"])
    for employee_key, history in ordered.groupby("EmployeeKey"):
        employee = employee_rows[int(employee_key)]
        previous = None
        for assignment in _rows(history, "AssignmentKey").values():
            assert assignment.StartDate <= assignment.EndDate
            assert _employed(employee, assignment.StartDate)
            assert _employed(employee, assignment.EndDate)
            store = store_rows[assignment.StoreKey]
            assert assignment.StartDate >= store.OpeningDate
            assert pd.isna(store.ClosingDate) or assignment.EndDate < store.ClosingDate
            if previous is not None:
                assert assignment.StartDate == previous + pd.Timedelta(days=1)
            previous = assignment.EndDate
        assert history.IsCurrentAssignment.sum() == 1
        assert history.iloc[-1].IsCurrentAssignment
    # Ensure this is an exercised lifecycle scenario, not a vacuous static run.
    assert (pd.to_datetime(stores.OpeningDate) > pd.Timestamp("2022-01-01")).any()
    assert stores.ClosingDate.notna().any()
    assert stores.RenovationStartDate.notna().any()
    assert assignments.TransferReason.str.startswith("Store Closure:").any()
    assert assignments.TransferReason.eq("Renovation Reassignment").any()
    assert assignments.TransferReason.isin(
        ["Backfill", "New Store", "Lateral Transfer"]
    ).any()
    for employee in employee_rows.values():
        if pd.notna(employee.HomeStoreKey) and employee.Title == "Store Manager":
            home = store_rows[int(employee.HomeStoreKey)]
            if pd.notna(home.ClosingDate) and home.ClosingDate <= pd.Timestamp(
                "2023-12-31"
            ):
                assert employee.TerminationDate == home.ClosingDate - pd.Timedelta(
                    days=1
                )


def test_each_operating_day_has_an_employed_eligible_salesperson(workforce_run):
    stores, employees, assignments, _ = workforce_run
    employee_rows = _rows(employees, "EmployeeKey")
    days = pd.date_range("2022-01-01", "2023-12-31")
    for store in _rows(stores, "StoreKey").values():
        required = {day for day in days if _operates(store, day)}
        covered = set()
        for assignment in _rows(
            assignments[assignments.StoreKey == store.StoreKey], "AssignmentKey"
        ).values():
            employee = employee_rows[assignment.EmployeeKey]
            if _sells(employee, assignment, store):
                covered.update(
                    day
                    for day in required
                    if assignment.StartDate <= day <= assignment.EndDate
                    and _employed(employee, day)
                )
        missing = sorted(required - covered)
        assert not missing, (
            f"Store {store.StoreKey}: uncovered operating days {missing[:5]}"
        )


def test_original_hierarchy_and_dataset_end_headcount_reconcile(workforce_run):
    stores, employees, assignments, _ = workforce_run
    store_rows = _rows(stores, "StoreKey")
    employee_rows = _rows(employees, "EmployeeKey")
    for employee in employee_rows.values():
        if pd.notna(employee.ParentEmployeeKey):
            parent = employee_rows[int(employee.ParentEmployeeKey)]
            assert parent.OrgLevel < employee.OrgLevel
            if pd.notna(employee.HomeStoreKey) and employee.Title not in (
                "Store Manager",
                "Online Sales Representative",
            ):
                assert parent.Title == "Store Manager"
                assert parent.HomeStoreKey == employee.HomeStoreKey
        if pd.notna(employee.HomeStoreKey):
            home = store_rows[int(employee.HomeStoreKey)]
            assert employee.RegionId == home.StoreRegionId
            assert employee.DistrictId == home.StoreDistrictId
            assert employee.GeographyKey == home.GeographyKey
    as_of = pd.Timestamp("2023-12-31")
    active_store_employees = {
        e.EmployeeKey
        for e in employee_rows.values()
        if pd.notna(e.HomeStoreKey) and _employed(e, as_of)
    }
    effective = [
        a
        for a in _rows(assignments, "AssignmentKey").values()
        if a.StartDate <= as_of <= a.EndDate
    ]
    assert {a.EmployeeKey for a in effective} == active_store_employees
    assert len(effective) == len(active_store_employees)
    latest_active = assignments[
        assignments.IsCurrentAssignment & assignments.AssignmentStatus.eq("Active")
    ]
    assert set(latest_active.EmployeeKey) == active_store_employees
    assert latest_active.FTE.sum() == pytest.approx(sum(a.FTE for a in effective))
    # Transfers really occurred; home-store grouping cannot substitute for work location.
    assert any(
        employee_rows[a.EmployeeKey].HomeStoreKey != a.StoreKey for a in effective
    )


@pytest.mark.parametrize("mode", ["sales", "sales_order"])
def test_every_emitted_transaction_has_one_eligible_assignment(workforce_run, mode):
    stores, employees, assignments, outputs = workforce_run
    store_rows = _rows(stores, "StoreKey")
    employee_rows = _rows(employees, "EmployeeKey")
    history = {}
    for assignment in _rows(assignments, "AssignmentKey").values():
        history.setdefault((assignment.EmployeeKey, assignment.StoreKey), []).append(
            assignment
        )
    # An employee/store/date tuple has the same eligibility on every order line.
    transactions = outputs[mode][
        ["EmployeeKey", "StoreKey", "OrderDate"]
    ].drop_duplicates()
    assert len(transactions) > 100
    for sale in _rows(
        transactions.reset_index().rename(columns={"index": "CaseKey"}), "CaseKey"
    ).values():
        assert sale.EmployeeKey in employee_rows and sale.StoreKey in store_rows
        employee, store = employee_rows[sale.EmployeeKey], store_rows[sale.StoreKey]
        matching = [
            a
            for a in history.get((sale.EmployeeKey, sale.StoreKey), ())
            if a.StartDate <= sale.OrderDate <= a.EndDate
        ]
        assert len(matching) == 1
        assert _employed(employee, sale.OrderDate)
        assert _operates(store, sale.OrderDate)
        assert _sells(employee, matching[0], store)
