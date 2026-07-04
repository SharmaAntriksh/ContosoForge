"""Tests for the mid-window store-openings feature (`stores.allow_mid_window_openings`).

When the flag is OFF (default) the opening window is shifted so every store opens
before the dataset start. When ON, stores may open inside the data window.

The core invariant the feature upholds — no employee is assigned to a store
before it opens (`bridge.StartDate >= store.OpeningDate`) — is enforced by the
bridge builder, which clamps each assignment's StartDate up to its store's
OpeningDate when `store_opening_dates` is supplied.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.dimensions.stores import generate_store_table
from src.dimensions.stores.generator import GeoContext
from src.dimensions.employees import (
    generate_employee_dimension,
    generate_employee_store_assignments,
)

DS_START = pd.Timestamp("2021-01-01")
DS_END = pd.Timestamp("2025-12-31")
# An opening window whose tail falls INSIDE the data range, so the shift (flag OFF)
# has something to move and mid-window openings appear when the flag is ON.
OPEN_START = "2018-01-01"
OPEN_END = "2024-06-30"


@pytest.fixture()
def geo_keys():
    return np.arange(1, 11, dtype=np.int64)


@pytest.fixture()
def iso_by_geo():
    return {1: "USD", 2: "USD", 3: "EUR", 4: "EUR", 5: "GBP",
            6: "INR", 7: "CAD", 8: "AUD", 9: "JPY", 10: "CNY"}


@pytest.fixture()
def people_pools():
    from src.utils.name_pools import load_people_pools, resolve_people_folder
    folder = resolve_people_folder()
    if not Path(folder).exists():
        pytest.skip("Name pool data not available")
    return load_people_pools(folder, enable_asia=True, legacy_support=True)


def _stores(geo_keys, iso_by_geo, *, allow_mid_window):
    return generate_store_table(
        geo=GeoContext(geo_keys=geo_keys, iso_by_geo=iso_by_geo),
        num_stores=60,
        seed=7,
        opening_start=OPEN_START,
        opening_end=OPEN_END,
        dataset_start=str(DS_START.date()),
        dataset_end=str(DS_END.date()),
        closing_enabled=False,  # keep the focus on openings
        allow_mid_window_openings=allow_mid_window,
    )


class TestOpeningWindowFlag:
    def test_disabled_shifts_all_openings_before_start(self, geo_keys, iso_by_geo):
        stores = _stores(geo_keys, iso_by_geo, allow_mid_window=False)
        od = pd.to_datetime(stores["OpeningDate"])
        assert (od < DS_START).all(), (
            f"flag OFF: every store must open before {DS_START.date()}; "
            f"max opening was {od.max().date()}"
        )

    def test_enabled_allows_mid_window_openings(self, geo_keys, iso_by_geo):
        stores = _stores(geo_keys, iso_by_geo, allow_mid_window=True)
        od = pd.to_datetime(stores["OpeningDate"])
        mid = int((od >= DS_START).sum())
        assert mid > 0, (
            "flag ON: at least one store should open inside the data window "
            f"[{DS_START.date()}, {DS_END.date()}]"
        )
        # Openings must still respect the configured window (no shift, no spill).
        assert od.max() <= pd.Timestamp(OPEN_END)

    def test_disabled_is_the_default(self, geo_keys, iso_by_geo):
        """Omitting the flag reproduces the shifted (all-before-start) behaviour."""
        default = generate_store_table(
            geo=GeoContext(geo_keys=geo_keys, iso_by_geo=iso_by_geo),
            num_stores=60, seed=7,
            opening_start=OPEN_START, opening_end=OPEN_END,
            dataset_start=str(DS_START.date()), dataset_end=str(DS_END.date()),
            closing_enabled=False,
        )
        assert (pd.to_datetime(default["OpeningDate"]) < DS_START).all()

    def test_opening_window_after_cap_raises(self, geo_keys, iso_by_geo):
        """The whole opening window sits after dataset_end - 365d: no store can
        satisfy the minimum operating window, so generation must fail loudly
        instead of silently emitting same-day openings with a sub-year footprint."""
        from src.exceptions import DimensionError
        with pytest.raises(DimensionError, match="allow_mid_window_openings"):
            generate_store_table(
                geo=GeoContext(geo_keys=geo_keys, iso_by_geo=iso_by_geo),
                num_stores=10, seed=7,
                opening_start="2025-06-01", opening_end="2025-11-30",
                dataset_start=str(DS_START.date()), dataset_end=str(DS_END.date()),
                closing_enabled=False,
                allow_mid_window_openings=True,
            )


def _build_cluster(geo_keys, iso_by_geo, people_pools):
    stores = _stores(geo_keys, iso_by_geo, allow_mid_window=True)
    opening = {
        int(sk): pd.to_datetime(od).normalize()
        for sk, od in zip(stores["StoreKey"], stores["OpeningDate"])
    }
    employees = generate_employee_dimension(
        stores=stores,
        seed=7,
        global_start=DS_START,
        global_end=DS_END,
        people_pools=people_pools,
        store_opening_dates=opening,
    )
    bridge = generate_employee_store_assignments(
        employees, DS_START, DS_END, store_opening_dates=opening,
    )
    return opening, employees, bridge


def test_no_assignment_before_store_opens_when_enabled(geo_keys, iso_by_geo, people_pools):
    """Core invariant: no bridge segment may start before its store's OpeningDate."""
    opening, _employees, bridge = _build_cluster(geo_keys, iso_by_geo, people_pools)
    start = pd.to_datetime(bridge["StartDate"]).dt.normalize()
    open_d = bridge["StoreKey"].astype(int).map(opening)
    violations = int((start < open_d).sum())
    assert violations == 0, (
        f"{violations} bridge assignment(s) start before their store's OpeningDate"
    )


def test_mid_window_roster_ramps_in(geo_keys, iso_by_geo, people_pools):
    """A mid-window store's roster ramps up around its opening O rather than
    pre-existing: manager arrives ahead of O, sales role lands on O (coverage),
    and other staff trickle in over [O, O + ramp]."""
    from src.defaults import MID_WINDOW_STAFF_RAMP_DAYS

    opening, employees, bridge = _build_cluster(geo_keys, iso_by_geo, people_pools)

    emp = employees.copy()
    emp["HireDate"] = pd.to_datetime(emp["HireDate"]).dt.normalize()
    emp["OpeningDate"] = emp["StoreKey"].astype(int).map(opening)
    mid = emp[emp["OpeningDate"] > DS_START]
    assert len(mid) > 0, "fixture must produce mid-window stores"

    # Manager arrives strictly before opening (store setup).
    mgr = mid[mid["Title"] == "Store Manager"]
    assert len(mgr) > 0
    assert (mgr["HireDate"] < mgr["OpeningDate"]).all(), (
        "mid-window managers must be hired before their store opens"
    )
    ramp_cap = pd.Timedelta(days=MID_WINDOW_STAFF_RAMP_DAYS)
    assert (mgr["OpeningDate"] - mgr["HireDate"] <= ramp_cap).all()

    # Sales associates are present on opening day → salesperson coverage from
    # the store's first month.
    sa = mid[mid["Title"] == "Sales Associate"]
    assert len(sa) > 0
    assert (sa["HireDate"] == sa["OpeningDate"]).all(), (
        "mid-window sales staff must be hired on opening day"
    )

    # Non-sales, non-manager store staff trickle in over [O, O + ramp] — hired
    # on or after opening, none beyond the ramp window, and genuinely spread
    # (not all clamped to a single day).
    other = mid[~mid["Title"].isin(["Store Manager", "Sales Associate"])]
    other = other[other["StoreKey"].astype(int).isin(opening)]
    assert len(other) > 0
    lag = (other["HireDate"] - other["OpeningDate"]).dt.days
    assert (lag >= 0).all(), "other staff must not predate opening"
    assert (lag <= MID_WINDOW_STAFF_RAMP_DAYS).all(), "ramp must stay within window"
    assert lag.nunique() > 1, "staff hires must be staggered, not all on opening day"

    # The stagger shows through to the bridge: a mid-window store has assignment
    # StartDates spanning more than a single day.
    b = bridge.copy()
    b["StartDate"] = pd.to_datetime(b["StartDate"]).dt.normalize()
    b["OpeningDate"] = b["StoreKey"].astype(int).map(opening)
    b_mid = b[b["OpeningDate"] > DS_START]
    spread = b_mid.groupby("StoreKey")["StartDate"].nunique()
    assert (spread > 1).any(), "at least one mid-window store's bridge starts must be staggered"


def test_mid_window_store_that_closes_within_ramp(people_pools):
    """Regression: a store that opens mid-window AND closes inside the ramp
    window must not hire staff after ClosingDate.

    The staggered ramp trickles staff over [O, O+60]; if the store closes 24
    days after opening, an un-capped ramp would hire people after the store had
    already terminated everyone at ClosingDate-1, producing HireDate >
    TerminationDate and silently dropping them from the bridge (start > end).
    Caught in a full-scale run by scripts/verify_employee_store_sales.py.
    """
    stores = pd.DataFrame({
        "StoreKey": [1, 2],
        "GeographyKey": [1, 1],
        "EmployeeCount": [12, 10],
        "StoreType": ["Supermarket", "Supermarket"],
        "StoreDistrictId": [1, 1],
        "StoreRegionId": [1, 1],
        "StoreDistrict": ["District 1", "District 1"],
        "StoreRegion": ["Region 1", "Region 1"],
    })
    O1 = pd.Timestamp("2023-03-19")           # opens mid-window
    C1 = pd.Timestamp("2023-04-12")           # closes 24 days later (< 60d ramp)
    opening = {1: O1, 2: DS_START - pd.Timedelta(days=100)}
    closing = {1: C1}

    employees = generate_employee_dimension(
        stores=stores, seed=7, global_start=DS_START, global_end=DS_END,
        people_pools=people_pools,
        store_opening_dates=opening, store_closing_dates=closing,
    )

    hd = pd.to_datetime(employees["HireDate"])
    td = pd.to_datetime(employees["TerminationDate"])
    both = hd.notna() & td.notna()
    assert (hd[both] <= td[both]).all(), "no employee may be hired after termination"

    # Store-1 staff must be hired on or before the last operating day (C1 - 1).
    s1 = employees[employees["StoreKey"].astype(int) == 1]
    assert (pd.to_datetime(s1["HireDate"]) <= C1 - pd.Timedelta(days=1)).all()

    # And none are dropped from the bridge by the start<=end filter.
    bridge = generate_employee_store_assignments(
        employees, DS_START, DS_END, store_opening_dates=opening,
    )
    store_level = set(employees.loc[employees["StoreKey"].astype(int) > 0, "EmployeeKey"].astype(int))
    missing = store_level - set(bridge["EmployeeKey"].astype(int))
    assert not missing, f"{len(missing)} store-level employee(s) missing from the bridge"
