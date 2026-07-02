"""Guardrails for the store-cluster refactor (stores + warehouses + employees).

This module is the falsifiability harness for the cluster refactor documented in
``docs/EMPLOYEES_REFACTOR_PLAN.md``.  It has three layers:

1. A **golden fingerprint** of all four outputs (``stores`` after warehouse
   enrichment, ``warehouses``, ``employees``, ``employee_store_assignments``) for
   a fixed fixture.  Behavior-preserving phases must leave every fingerprint
   unchanged; behavior phases regenerate them once via
   ``REGEN_STORE_CLUSTER_SNAPSHOT=1``.
2. **Characterization tests** that pin the invariants worth keeping (staffing
   contract, warehouse FK/uniqueness, one-primary/no-overlap bridge, key-band
   integrity, store lifecycle sanity, salesperson coverage).
3. **Bug-pinning tests** marked ``xfail(strict=True)``; each asserts the desired
   post-fix behavior and names the phase that will flip it green.

The chain is driven through the real ``run_*`` entry points so the fingerprint
captures ``stores.parquet`` *after* the warehouse enrichment rewrite — the state
every downstream consumer actually reads.  ``version_store.VERSION_DIR`` is
redirected to a temp dir so the suite never touches the developer's real
``data/versioning/`` files.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.defaults import (
    ONLINE_STORE_KEY_BASE,
    ONLINE_EMP_KEY_BASE,
    ONLINE_SALES_REP_ROLE,
)
from src.dimensions.employees.generator import (
    STORE_MGR_KEY_BASE,
    STAFF_KEY_BASE,
    STAFF_KEY_STORE_MULT,
)
from src.engine.config.config_schema import AppConfig
from src.dimensions.stores import run_stores
from src.dimensions.warehouses.generator import run_warehouses
from src.dimensions.employees import run_employees, run_employee_store_assignments


# ---------------------------------------------------------------------------
# Fixture parameters — pinned so the fixture exercises every lifecycle path
# (closing, renovating, and renovation-closed stores all present at seed 202).
# ---------------------------------------------------------------------------
NUM_STORES = 50
ONLINE_STORES = 2
SEED = 202
DATE_START = "2022-01-01"
DATE_END = "2024-12-31"

_SNAPSHOT_FILE = Path(__file__).parent / "_snapshots" / "store_cluster_fingerprints.json"
_SORT_KEYS = {
    "stores": ["StoreKey"],
    "warehouses": ["WarehouseKey"],
    "employees": ["EmployeeKey"],
    "employee_store_assignments": ["AssignmentKey"],
}


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------
def _geography_df(n_geo: int = 18) -> pd.DataFrame:
    countries = (
        ["United States"] * 8 + ["United Kingdom"] * 4
        + ["Germany"] * 3 + ["Australia"] * 3
    )
    iso = ["USD"] * 8 + ["GBP"] * 4 + ["EUR"] * 3 + ["AUD"] * 3
    continent = ["North America"] * 8 + ["Europe"] * 7 + ["Oceania"] * 3
    states = [
        "New York", "California", "Texas", "Florida", "Illinois",
        "Washington", "Massachusetts", "Georgia",
        "England", "Scotland", "Wales", "Northern Ireland",
        "Bavaria", "Berlin", "Hesse",
        "New South Wales", "Victoria", "Queensland",
    ]
    return pd.DataFrame({
        "GeographyKey": np.arange(1, n_geo + 1, dtype=np.int64),
        "City": [f"City{i}" for i in range(1, n_geo + 1)],
        "State": states[:n_geo],
        "Country": countries[:n_geo],
        "RegionCountryName": countries[:n_geo],
        "Continent": continent[:n_geo],
        "ISOCode": iso[:n_geo],
        "Population": np.linspace(50_000, 5_000_000, n_geo).astype(np.int64),
        "Latitude": np.round(np.linspace(30.0, 55.0, n_geo), 4),
        "Longitude": np.round(np.linspace(-120.0, 15.0, n_geo), 4),
    })


def _cluster_cfg(
    *,
    num_stores: int = NUM_STORES,
    online_stores: int = ONLINE_STORES,
    seed: int = SEED,
    primary_sales_role: str = "Sales Associate",
    transfers: bool = True,
    stores_compression: str = "snappy",
    force_date32: bool = True,
) -> AppConfig:
    return AppConfig.model_validate({
        "defaults": {"seed": seed, "dates": {"start": DATE_START, "end": DATE_END}},
        "stores": {
            "num_stores": num_stores,
            "online_stores": online_stores,
            "district_size": 6,
            "districts_per_region": 4,
            "closing": {"enabled": True, "close_share": 0.15},
            "use_name_pools": True,
            "parquet_compression": stores_compression,
            "force_date32": force_date32,
        },
        "employees": {
            "transfers": {"enabled": transfers, "annual_rate": 0.10},
            "store_assignments": {"primary_sales_role": primary_sales_role},
        },
        "warehouses": {"min_stores_per_warehouse": 8, "min_stores_for_own_warehouse": 3},
        "geography": {},
    })


@contextlib.contextmanager
def _isolated_version_dir(path: Path):
    """Redirect ``version_store.VERSION_DIR`` so tests never write the real one."""
    import src.versioning.version_store as vs
    saved = vs.VERSION_DIR
    path.mkdir(parents=True, exist_ok=True)
    vs.VERSION_DIR = path
    try:
        yield
    finally:
        vs.VERSION_DIR = saved


def _build_cluster(folder: Path, cfg: AppConfig) -> None:
    """Run the full chain into ``folder``: geography → stores → warehouses →
    employees → employee_store_assignments."""
    folder.mkdir(parents=True, exist_ok=True)
    _geography_df().to_parquet(folder / "geography.parquet", index=False)
    run_stores(cfg, folder)
    run_warehouses(cfg, folder)
    run_employees(cfg, folder)
    run_employee_store_assignments(cfg, folder)


def _load_frames(folder: Path) -> dict[str, pd.DataFrame]:
    return {
        "stores": pd.read_parquet(folder / "stores.parquet"),
        "warehouses": pd.read_parquet(folder / "warehouses.parquet"),
        "employees": pd.read_parquet(folder / "employees.parquet"),
        "employee_store_assignments": pd.read_parquet(
            folder / "employee_store_assignments.parquet"
        ),
    }


# ---------------------------------------------------------------------------
# Module-scoped fixture: build the golden cluster exactly once.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cluster_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("store_cluster")
    with _isolated_version_dir(root / "versioning"):
        folder = root / "data"
        _build_cluster(folder, _cluster_cfg())
        yield folder


@pytest.fixture(scope="module")
def cluster(cluster_dir) -> dict[str, pd.DataFrame]:
    return _load_frames(cluster_dir)


# ---------------------------------------------------------------------------
# Layer 1: golden fingerprint
# ---------------------------------------------------------------------------
def _fingerprint(df: pd.DataFrame, sort_keys: list[str]) -> str:
    """Deterministic content+schema hash, robust to incidental row order.

    Column order and dtypes are part of the payload (the output contract is
    positional for BULK INSERT), and rows are sorted by their natural key so a
    reorder does not masquerade as a content change.
    """
    d = df.copy()
    present = [k for k in sort_keys if k in d.columns]
    if present:
        d = d.sort_values(present, kind="stable").reset_index(drop=True)
    payload = {
        "schema": [(str(c), str(d[c].dtype)) for c in d.columns],
        "rows": int(len(d)),
        "csv": d.to_csv(index=False),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _current_fingerprints(cluster: dict[str, pd.DataFrame]) -> dict[str, str]:
    return {name: _fingerprint(df, _SORT_KEYS[name]) for name, df in cluster.items()}


def test_golden_fingerprint(cluster):
    """Every output byte-identical to the recorded baseline.

    First run (or ``REGEN_STORE_CLUSTER_SNAPSHOT=1``) records the baseline and
    skips; thereafter it asserts equality per table. Behavior phases regenerate.
    """
    current = _current_fingerprints(cluster)
    regen = os.environ.get("REGEN_STORE_CLUSTER_SNAPSHOT") == "1"

    if regen or not _SNAPSHOT_FILE.exists():
        _SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT_FILE.write_text(json.dumps(current, indent=2, sort_keys=True))
        pytest.skip(
            f"Store-cluster fingerprint baseline written to {_SNAPSHOT_FILE.name}; "
            "re-run to compare."
        )

    baseline = json.loads(_SNAPSHOT_FILE.read_text())
    mismatches = {
        name: (baseline.get(name), current.get(name))
        for name in current
        if baseline.get(name) != current.get(name)
    }
    assert not mismatches, (
        "Store-cluster output changed vs golden baseline for: "
        f"{sorted(mismatches)}. If this change is intended (a behavior phase), "
        "rerun with REGEN_STORE_CLUSTER_SNAPSHOT=1 to regenerate the baseline."
    )


# ---------------------------------------------------------------------------
# Layer 2: characterization tests (invariants to keep)
# ---------------------------------------------------------------------------
def _physical(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["StoreKey"].astype(np.int64) < ONLINE_STORE_KEY_BASE]


def _online(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["StoreKey"].astype(np.int64) > ONLINE_STORE_KEY_BASE]


class TestFixtureExercisesLifecycle:
    """The fixture is only a useful guardrail if it hits every lifecycle path."""

    def test_has_online_and_physical(self, cluster):
        stores = cluster["stores"]
        assert len(_online(stores)) == ONLINE_STORES
        assert len(_physical(stores)) == NUM_STORES - ONLINE_STORES

    def test_has_closing_renovating_and_reno_closed(self, cluster):
        stores = cluster["stores"]
        status = stores["Status"].astype(str)
        assert (status == "Closed").sum() >= 1, "fixture must include a closing store"
        assert (status == "Renovating").sum() >= 1, "fixture must include a renovating store"
        reno_closed = (status == "Closed") & (stores["CloseReason"].astype(str) == "Renovation")
        assert reno_closed.sum() >= 1, "fixture must include a renovation-closed store"


class TestStaffingContract:
    def test_physical_roster_matches_employee_count(self, cluster):
        stores = _physical(cluster["stores"])
        employees = cluster["employees"]
        # Roster per store = employees whose EmployeeKey decodes to that store.
        home = _decode_home_store(employees["EmployeeKey"].astype(np.int64))
        roster = pd.Series(home).value_counts()
        for sk, count in zip(stores["StoreKey"].astype(np.int64), stores["EmployeeCount"].astype(int)):
            assert roster.get(int(sk), 0) == count, (
                f"store {sk}: roster {roster.get(int(sk), 0)} != EmployeeCount {count}"
            )

    def test_online_store_has_single_rep(self, cluster):
        online = _online(cluster["stores"])
        employees = cluster["employees"]
        home = _decode_home_store(employees["EmployeeKey"].astype(np.int64))
        roster = pd.Series(home).value_counts()
        for sk in online["StoreKey"].astype(np.int64):
            assert roster.get(int(sk), 0) == 1, f"online store {sk} must have exactly 1 rep"


class TestWarehouses:
    def test_every_store_has_warehouse_key(self, cluster):
        stores = cluster["stores"]
        assert "WarehouseKey" in stores.columns
        assert stores["WarehouseKey"].notna().all()

    def test_warehouse_names_unique(self, cluster):
        assert cluster["warehouses"]["WarehouseName"].is_unique

    def test_warehouse_geography_fk_valid(self, cluster, cluster_dir):
        geo = pd.read_parquet(cluster_dir / "geography.parquet")
        valid = set(geo["GeographyKey"].astype(np.int64))
        wh = cluster["warehouses"]
        gk = pd.to_numeric(wh["GeographyKey"], errors="coerce").dropna().astype(np.int64)
        assert set(gk).issubset(valid)


class TestBridgeInvariants:
    def test_exactly_one_primary_per_employee(self, cluster):
        esa = cluster["employee_store_assignments"]
        primaries = esa[esa["IsPrimary"].astype(bool)].groupby("EmployeeKey").size()
        assert (primaries == 1).all(), "every employee must have exactly one IsPrimary=True"
        # And every employee that appears has at least one primary.
        assert set(primaries.index) == set(esa["EmployeeKey"].unique())

    def test_start_before_end_everywhere(self, cluster):
        esa = cluster["employee_store_assignments"]
        start = pd.to_datetime(esa["StartDate"])
        end = pd.to_datetime(esa["EndDate"])
        assert (start <= end).all()

    def test_no_assignment_gaps_or_overlaps(self, cluster):
        esa = cluster["employee_store_assignments"].copy()
        esa["StartDate"] = pd.to_datetime(esa["StartDate"])
        esa["EndDate"] = pd.to_datetime(esa["EndDate"])
        esa = esa.sort_values(["EmployeeKey", "StartDate"])
        one_day = pd.Timedelta(days=1)
        for ek, grp in esa.groupby("EmployeeKey"):
            ends = grp["EndDate"].to_numpy()
            starts = grp["StartDate"].to_numpy()
            for i in range(1, len(grp)):
                gap = pd.Timestamp(starts[i]) - pd.Timestamp(ends[i - 1])
                assert gap == one_day, (
                    f"employee {ek}: segment {i} starts {starts[i]} but previous ends "
                    f"{ends[i - 1]} (expected contiguous +1 day, got {gap})"
                )


class TestKeyBandIntegrity:
    def test_every_store_employee_key_decodes_to_its_store(self, cluster):
        employees = cluster["employees"]
        ek = employees["EmployeeKey"].astype(np.int64).to_numpy()
        store_col = pd.to_numeric(employees["StoreKey"], errors="coerce").to_numpy()
        home = _decode_home_store(ek)
        # Only store-level employees (manager/staff/online rep) carry a StoreKey.
        store_level = ek >= STORE_MGR_KEY_BASE
        for decoded, declared in zip(home[store_level], store_col[store_level]):
            assert not np.isnan(declared)
            assert int(decoded) == int(declared), (
                f"decoded home store {decoded} != declared StoreKey {declared}"
            )


class TestStoreLifecycleSanity:
    def test_closing_after_opening(self, cluster):
        stores = cluster["stores"]
        closed = stores[stores["ClosingDate"].notna()]
        assert (pd.to_datetime(closed["ClosingDate"]) > pd.to_datetime(closed["OpeningDate"])).all()

    def test_renovation_window_inside_dataset(self, cluster):
        stores = cluster["stores"]
        reno = stores[stores["RenovationStartDate"].notna() & stores["RenovationEndDate"].notna()]
        rs = pd.to_datetime(reno["RenovationStartDate"])
        re = pd.to_datetime(reno["RenovationEndDate"])
        assert (rs <= re).all()
        assert (rs >= pd.Timestamp(DATE_START)).all()
        assert (re <= pd.Timestamp(DATE_END)).all()


class TestSalespersonCoverage:
    def test_every_constrained_store_has_a_salesperson(self, cluster):
        """Every physical store that is open for the whole window (not closed,
        not renovating) has at least one salesperson assigned during it."""
        stores = _physical(cluster["stores"])
        status = stores["Status"].astype(str)
        constrained = stores[
            (status == "Open") & stores["ClosingDate"].isna()
        ]["StoreKey"].astype(np.int64)

        employees = cluster["employees"]
        sales_keys = set(
            employees.loc[employees["IsSalesperson"].astype(bool), "EmployeeKey"].astype(np.int64)
        )
        esa = cluster["employee_store_assignments"]
        sales_esa = esa[esa["EmployeeKey"].astype(np.int64).isin(sales_keys)]
        covered = set(sales_esa["StoreKey"].astype(np.int64))
        missing = sorted(set(constrained.astype(int)) - covered)
        assert not missing, f"constrained stores with no salesperson coverage: {missing}"


# ---------------------------------------------------------------------------
# Shared decode helper (mirrors employees' EmployeeKey encoding)
# ---------------------------------------------------------------------------
def _decode_home_store(ek: np.ndarray) -> np.ndarray:
    """Decode EmployeeKey → home StoreKey for store-level bands.

    Manager: 30M + sk.  Staff: 40M + sk*1000 + idx.  Online rep: 50M + sk.
    Org-level keys (CEO/VP/region/district, < 30M) decode to NaN.
    """
    ek = np.asarray(ek, dtype=np.int64)
    out = np.full(ek.shape, np.nan)
    online = ek >= ONLINE_EMP_KEY_BASE
    out[online] = ek[online] - ONLINE_EMP_KEY_BASE
    mgr = (ek >= STORE_MGR_KEY_BASE) & (ek < STAFF_KEY_BASE) & ~online
    out[mgr] = ek[mgr] - STORE_MGR_KEY_BASE
    staff = (ek >= STAFF_KEY_BASE) & ~online
    out[staff] = (ek[staff] - STAFF_KEY_BASE) // STAFF_KEY_STORE_MULT
    return out


# ---------------------------------------------------------------------------
# Layer 3: bug-pinning tests (each asserts the desired POST-FIX behavior)
# ---------------------------------------------------------------------------
def test_bugpin_1_issalesperson_honors_configured_role(tmp_path):
    """With a non-default ``primary_sales_role`` the IsSalesperson flag and the
    Sales department must track the configured role, not the hardcoded literal
    (regression guard)."""
    role = "Retail Specialist"
    with _isolated_version_dir(tmp_path / "versioning"):
        folder = tmp_path / "data"
        cfg = _cluster_cfg(num_stores=20, seed=SEED, primary_sales_role=role, transfers=False)
        folder.mkdir(parents=True, exist_ok=True)
        _geography_df().to_parquet(folder / "geography.parquet", index=False)
        run_stores(cfg, folder)
        run_warehouses(cfg, folder)
        run_employees(cfg, folder)
        employees = pd.read_parquet(folder / "employees.parquet")
    physical_staff = employees[employees["Title"].astype(str) == role]
    assert len(physical_staff) > 0, "fixture should produce staff with the configured role"
    assert physical_staff["IsSalesperson"].astype(bool).any(), (
        "physical staff in the configured sales role must be flagged IsSalesperson"
    )
    assert (physical_staff["DepartmentName"].astype(str) == "Sales").any(), (
        "physical staff in the configured sales role must be in the Sales department"
    )


@pytest.mark.xfail(strict=True, reason="Phase 4.2: renovation temp store must be open the whole temp window")
def test_bugpin_2_renovation_temp_store_must_be_open(tmp_path):
    """The renovation temp-store picker filters on ``Status == 'Open'`` (an
    as-of-dataset-end snapshot), so it can send an employee to a store that was
    not actually open during the temp window.  Post-fix uses date-window logic."""
    from src.dimensions.employees.employee_store_assignments import (
        _apply_renovation_reassignments,
        generate_employee_store_assignments,
    )
    gstart, gend = pd.Timestamp(DATE_START), pd.Timestamp(DATE_END)
    # One manager whose home store (1) renovates mid-window.
    employees = pd.DataFrame({
        "EmployeeKey": [STORE_MGR_KEY_BASE + 1],
        "HireDate": [gstart],
        "TerminationDate": [pd.NaT],
        "Title": ["Store Manager"],
        "FTE": [1.0],
        "IsActive": [True],
    })
    bridge = generate_employee_store_assignments(employees, gstart, gend)
    # Store 1 renovates; the ONLY open candidate (store 2) opens AFTER the temp
    # window — a correct picker must not choose it.
    stores = pd.DataFrame({
        "StoreKey": [1, 2],
        "StoreType": ["Supermarket", "Supermarket"],
        "Status": ["Renovating", "Open"],
        "StoreRegion": ["R1", "R1"],
        "OpeningDate": [gstart, pd.Timestamp("2024-06-01")],
        "ClosingDate": [pd.NaT, pd.NaT],
        "RenovationStartDate": [pd.Timestamp("2022-06-01"), pd.NaT],
        "RenovationEndDate": [pd.Timestamp("2022-09-01"), pd.NaT],
    })
    out = _apply_renovation_reassignments(bridge, stores, seed=1)
    temp = out[out["TransferReason"] == "Renovation Reassignment"]
    assert len(temp) >= 1
    chosen = int(temp["StoreKey"].iloc[0])
    chosen_open = stores.loc[stores["StoreKey"] == chosen, "OpeningDate"].iloc[0]
    temp_start = pd.to_datetime(temp["StartDate"].iloc[0])
    assert pd.Timestamp(chosen_open) <= temp_start, (
        f"temp store {chosen} opened {chosen_open} but employee placed there {temp_start}"
    )


@pytest.mark.xfail(strict=True, reason="Phase 4.3: legacy stores without renovation columns must warn-and-skip")
def test_bugpin_3_renovation_missing_columns_is_graceful(tmp_path):
    """A stores frame lacking RenovationStart/EndDate raises KeyError today; the
    fixed path should degrade gracefully and return assignments unchanged."""
    from src.dimensions.employees.employee_store_assignments import (
        _apply_renovation_reassignments,
        generate_employee_store_assignments,
    )
    gstart, gend = pd.Timestamp(DATE_START), pd.Timestamp(DATE_END)
    employees = pd.DataFrame({
        "EmployeeKey": [STORE_MGR_KEY_BASE + 1],
        "HireDate": [gstart],
        "TerminationDate": [pd.NaT],
        "Title": ["Store Manager"],
        "FTE": [1.0],
        "IsActive": [True],
    })
    bridge = generate_employee_store_assignments(employees, gstart, gend)
    legacy_stores = pd.DataFrame({
        "StoreKey": [1],
        "StoreType": ["Supermarket"],
        "Status": ["Open"],
        "StoreRegion": ["R1"],
    })  # no RenovationStartDate / RenovationEndDate
    out = _apply_renovation_reassignments(bridge, legacy_stores, seed=1)
    assert len(out) == len(bridge), "no renovation columns → bridge must be unchanged"


@pytest.mark.xfail(strict=True, reason="Phase 4.3: warehouses geo-enrich must raise DimensionError on missing Country/State")
def test_bugpin_4_warehouse_missing_geo_columns_raises_dimensionerror(tmp_path):
    """``_enrich_with_geography`` reads geography columns Country/State; when they
    are absent it raises a raw pyarrow/KeyError today.  Post-fix it should raise a
    clear ``DimensionError`` naming the missing columns."""
    from src.dimensions.warehouses.generator import generate_warehouse_table
    from src.exceptions import DimensionError
    # Geography without Country/State.
    pd.DataFrame({
        "GeographyKey": np.arange(1, 6, dtype=np.int64),
        "ISOCode": ["USD"] * 5,
    }).to_parquet(tmp_path / "geography.parquet", index=False)
    stores = pd.DataFrame({
        "StoreKey": np.arange(1, 11, dtype=np.int64),
        "GeographyKey": np.arange(1, 11, dtype=np.int64) % 5 + 1,
        "StoreZone": ["North America"] * 10,
    })
    with pytest.raises(DimensionError):
        generate_warehouse_table(stores, tmp_path, seed=42)


@pytest.mark.xfail(strict=True, reason="Phase 2: single boundary predicate for the online/physical split")
def test_bugpin_5_boundary_predicate_agrees_at_base():
    """The online/physical boundary is spelled four ways across the cluster and
    they disagree at exactly ``ONLINE_STORE_KEY_BASE``.  Phase 2 introduces one
    canonical predicate; this pins its existence and its rule (online ⇔ > BASE)."""
    from src.defaults import is_online_store_key, is_physical_store_key
    b = ONLINE_STORE_KEY_BASE
    # Exactly one classification, and the canonical rule makes BASE physical.
    assert is_online_store_key(b) is not is_physical_store_key(b)
    assert not is_online_store_key(b)
    assert is_physical_store_key(b)
    assert is_online_store_key(b + 1)


@pytest.mark.xfail(strict=True, reason="Phase 6: warehouse rewrite must preserve stores.parquet writer settings")
def test_bugpin_6_stores_compression_stable_across_warehouse_rewrite(tmp_path):
    """``run_stores`` honors ``stores.parquet_compression``; the warehouse runner
    then rewrites stores.parquet with hardcoded snappy, so the physical
    compression flips.  Post-fix the compression must survive the rewrite."""
    import pyarrow.parquet as pq

    def _column_compression(path: Path) -> str:
        md = pq.ParquetFile(str(path)).metadata
        return md.row_group(0).column(0).compression

    with _isolated_version_dir(tmp_path / "versioning"):
        folder = tmp_path / "data"
        folder.mkdir(parents=True, exist_ok=True)
        cfg = _cluster_cfg(num_stores=20, seed=SEED, transfers=False, stores_compression="zstd")
        _geography_df().to_parquet(folder / "geography.parquet", index=False)
        run_stores(cfg, folder)
        before = _column_compression(folder / "stores.parquet")
        run_warehouses(cfg, folder)
        after = _column_compression(folder / "stores.parquet")
    assert before == after, (
        f"stores.parquet compression changed across warehouse rewrite: {before} → {after}"
    )
