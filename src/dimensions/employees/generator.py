from __future__ import annotations

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.utils.logging_utils import warn
from src.utils.config_helpers import int_or, rand_dates_between
from src.defaults import (
    EMPLOYEE_PART_TIME_RATE_BY_ROLE,
    EMPLOYEE_PART_TIME_FTE_VALUES,
    ONLINE_EMP_KEY_BASE,
    ONLINE_SALES_REP_ROLE,
    is_online_store_key,
)
from src.dimensions.employees.keys import (
    EmployeeKeyCodec,
    STORE_MGR_KEY_BASE,
    STAFF_KEY_BASE,
    STAFF_KEY_STORE_MULT,
)
from src.dimensions.employees.names import _apply_deterministic_names
from src.dimensions.employees.hr import _assert_identity_keys


_STAFF_TITLES = np.array(
    ["Sales Associate", "Cashier", "Stock Associate", "Customer Support", "Fulfillment Associate"],
    dtype=object,
)
_STAFF_TITLES_P = np.array([0.35, 0.25, 0.20, 0.10, 0.10], dtype=float)

# Validate at import time (per CLAUDE.md gotcha #10)
assert abs(float(_STAFF_TITLES_P.sum()) - 1.0) < 1e-9, (
    f"_STAFF_TITLES_P must sum to 1.0, got {_STAFF_TITLES_P.sum()}"
)




# ---------------------------------------------------------
# Internals
# ---------------------------------------------------------


def generate_employee_dimension(
    *,
    stores: pd.DataFrame,
    seed: int,
    global_start: pd.Timestamp,
    global_end: pd.Timestamp,
    people_pools=None,
    iso_by_geo: dict[int, str] | None = None,
    default_region: str = "US",
    primary_sales_role: str = "Sales Associate",
    min_primary_sales_per_store: int = 1,
    store_manager_names: dict[int, str] | None = None,
    store_opening_dates: dict[int, pd.Timestamp] | None = None,
    store_closing_dates: dict[int, pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """
    Build a parent-child employee hierarchy with stable keys.

    Static model: all employees are hired before global_start and remain
    active (IsActive=True, TerminationDate=NaT) for the full window, except
    those at stores that close (ClosingDate triggers termination).
    """
    if stores.empty:
        raise DimensionError("stores dataframe is empty; cannot generate employees")

    required_cols = {"StoreKey", "GeographyKey", "EmployeeCount", "StoreType"}
    missing = [c for c in required_cols if c not in stores.columns]
    if missing:
        raise DimensionError(f"stores.parquet missing required columns: {missing}")

    stores = stores.copy()
    stores["StoreKey"] = stores["StoreKey"].astype(np.int32)

    rng = np.random.default_rng(int(seed))

    n_stores = len(stores)

    # ----- Hierarchy: prefer stores.parquet columns (single source of truth) -----
    has_store_hierarchy = (
        "StoreDistrict" in stores.columns and "StoreRegion" in stores.columns
    )

    if has_store_hierarchy:
        district_id = (
            stores["StoreDistrict"].astype(str)
            .str.extract(r"(\d+)", expand=False)
            .astype(np.int32)
            .to_numpy()
        )
        region_id = (
            stores["StoreRegion"].astype(str)
            .str.extract(r"(\d+)", expand=False)
            .astype(np.int32)
            .to_numpy()
        )
        stores = stores.drop(
            columns=["StoreDistrict", "StoreRegion", "StoreZone"],
            errors="ignore",
        )
    else:
        # Legacy fallback: compute hierarchy when stores.parquet lacks columns
        warn(
            "stores.parquet missing StoreDistrict/StoreRegion columns; "
            "computing employee hierarchy independently. "
            "This may produce inconsistent hierarchies. "
            "Run --regen-dimensions all to fix."
        )
        sort_cols = []
        has_continent = "Continent" in stores.columns
        if has_continent:
            sort_cols.append("Continent")
        if "Country" in stores.columns:
            sort_cols.append("Country")
        sort_cols.append("StoreKey")
        stores = stores.sort_values(sort_cols).reset_index(drop=True)

        district_size = 10
        districts_per_region = 8

        if has_continent:
            district_id = np.zeros(n_stores, dtype=np.int32)
            next_did = 1
            for _, grp_idx in stores.groupby("Continent", sort=False):
                idx = grp_idx.index.to_numpy()
                n_grp = len(idx)
                local_did = np.arange(n_grp) // district_size
                district_id[idx] = (local_did + next_did).astype(np.int32)
                next_did += int(local_did.max()) + 1
        else:
            district_id = (np.arange(n_stores) // district_size + 1).astype(np.int32)

        region_id = ((district_id - 1) // districts_per_region + 1).astype(np.int32)
        stores = stores.drop(columns=["Continent", "Country"], errors="ignore")

    stores["DistrictId"] = district_id
    stores["RegionId"] = region_id

    # --- Key-encoding helpers using module constants ---
    CEO_KEY = np.int32(1)
    VP_OPS_KEY = np.int32(2)

    def _region_mgr_key(rid: int) -> np.int32:
        return EmployeeKeyCodec.encode_region(rid)

    def _district_mgr_key(did: int) -> np.int32:
        return EmployeeKeyCodec.encode_district(did)

    # ---------------------------------------------------------------
    # Build corporate / region / district tiers (small — loop is fine)
    # ---------------------------------------------------------------
    rows: list[dict] = []

    rows.append(dict(
        EmployeeKey=CEO_KEY,
        ParentEmployeeKey=pd.NA,
        EmployeeName="",
        Title="Chief Executive Officer",
        OrgLevel=np.int32(1),
        OrgUnitType="Corporate",
        RegionId=pd.NA, DistrictId=pd.NA,
        StoreKey=pd.NA, GeographyKey=pd.NA,
    ))
    rows.append(dict(
        EmployeeKey=VP_OPS_KEY,
        ParentEmployeeKey=CEO_KEY,
        EmployeeName="",
        Title="VP Operations",
        OrgLevel=np.int32(2),
        OrgUnitType="Corporate",
        RegionId=pd.NA, DistrictId=pd.NA,
        StoreKey=pd.NA, GeographyKey=pd.NA,
    ))

    unique_regions = np.unique(region_id)
    for rid in unique_regions:
        rows.append(dict(
            EmployeeKey=_region_mgr_key(int(rid)),
            ParentEmployeeKey=VP_OPS_KEY,
            EmployeeName="",
            Title="Regional Manager",
            OrgLevel=np.int32(3),
            OrgUnitType="Region",
            RegionId=np.int32(rid),
            DistrictId=pd.NA,
            StoreKey=pd.NA, GeographyKey=pd.NA,
        ))

    # Build district → region mapping from actual store data
    district_to_region = dict(zip(district_id.tolist(), region_id.tolist()))

    unique_districts = np.unique(district_id)
    for did in unique_districts:
        rid = int(district_to_region[int(did)])
        rows.append(dict(
            EmployeeKey=_district_mgr_key(int(did)),
            ParentEmployeeKey=_region_mgr_key(rid),
            EmployeeName="",
            Title="District Manager",
            OrgLevel=np.int32(4),
            OrgUnitType="District",
            RegionId=np.int32(rid),
            DistrictId=np.int32(did),
            StoreKey=pd.NA, GeographyKey=pd.NA,
        ))

    corporate_df = pd.DataFrame(rows)

    # ---------------------------------------------------------------
    # Store managers — vectorized (physical stores only)
    # ---------------------------------------------------------------
    sk_arr = stores["StoreKey"].to_numpy(dtype=np.int32)
    did_arr = stores["DistrictId"].to_numpy(dtype=np.int32)
    rid_arr = stores["RegionId"].to_numpy(dtype=np.int32)
    gk_arr = stores["GeographyKey"].to_numpy(dtype=np.int32)

    _is_online_store = is_online_store_key(sk_arr)
    _is_physical_store = ~_is_online_store

    mgr_parent_keys = np.array(
        [_district_mgr_key(int(d)) for d in did_arr], dtype=np.int32,
    )

    # Physical store managers only — online stores have no manager
    _phys_idx = np.where(_is_physical_store)[0]
    mgr_df = pd.DataFrame({
        "EmployeeKey": EmployeeKeyCodec.encode_store_manager(sk_arr[_phys_idx]),
        "ParentEmployeeKey": mgr_parent_keys[_phys_idx],
        "EmployeeName": "",
        "Title": "Store Manager",
        "OrgLevel": np.int32(5),
        "OrgUnitType": "Store",
        "RegionId": rid_arr[_phys_idx],
        "DistrictId": did_arr[_phys_idx],
        "StoreKey": sk_arr[_phys_idx],
        "GeographyKey": gk_arr[_phys_idx],
    })

    # ---------------------------------------------------------------
    # Staff counts — read directly from Stores.EmployeeCount (single source of truth).
    # Subtract 1 for the store manager (already generated above).
    # Online stores get 0 staff here (their representative is generated separately).
    # ---------------------------------------------------------------
    n_physical = int(_is_physical_store.sum())
    if n_physical == 0:
        staff_counts = np.zeros(0, dtype=np.int64)
    else:
        emp_counts = stores.loc[_is_physical_store, "EmployeeCount"].fillna(0).astype(np.int64).to_numpy()
        # EmployeeCount includes the store manager, so subtract 1 for staff
        staff_counts = np.maximum(0, emp_counts - 1)

        # Last-resort backstop: every physical store must have >= 1 staff slot
        # so that at least one Sales Associate exists (the first k_ps staff are
        # forced to the primary sales role below). A store with only a manager
        # has no salesperson, and Sales emits EmployeeKey=-1 (orphan FK) for it.
        # The store generator floors physical EmployeeCount to
        # MIN_PHYSICAL_EMPLOYEE_COUNT, so this is a no-op for pipeline-generated
        # stores; it only fires when generate_employee_dimension is called on a
        # hand-built / legacy stores frame with EmployeeCount<=1. In that case
        # it prevents the hard -1 FK failure, but the forced extra associate
        # makes the roster exceed Stores.EmployeeCount (a soft count-mismatch
        # the SQL verifier flags) — regenerate stores to resolve it cleanly.
        _understaffed = staff_counts < 1
        if _understaffed.any():
            warn(
                f"{int(_understaffed.sum())} physical store(s) had no staff after "
                "reserving the manager; forcing 1 Sales Associate each to keep "
                "salesperson coverage (prevents EmployeeKey=-1 in Sales). "
                "Regenerate stores so EmployeeCount matches the roster."
            )
            staff_counts = np.maximum(staff_counts, 1)

    # ---------------------------------------------------------------
    # Staff rows — vectorized via np.repeat (physical stores only)
    # ---------------------------------------------------------------
    _phys_sk = sk_arr[_phys_idx]
    _phys_did = did_arr[_phys_idx]
    _phys_rid = rid_arr[_phys_idx]
    _phys_gk = gk_arr[_phys_idx]
    total_staff = int(staff_counts.sum())

    if total_staff > 0:
        store_indices = np.repeat(np.arange(n_physical), staff_counts)
        staff_sk = _phys_sk[store_indices]
        staff_did = _phys_did[store_indices]
        staff_rid = _phys_rid[store_indices]
        staff_gk = _phys_gk[store_indices]

        # Per-employee index within each store (1-based)
        # Uses cumsum trick: start with 1s, subtract (prev_count) at each
        # store boundary so cumsum resets to 1 for the next store.
        within_store_idx = np.ones(total_staff, dtype=np.int32)
        offsets = np.cumsum(staff_counts)[:-1]
        if offsets.size > 0:
            np.subtract.at(within_store_idx, offsets, staff_counts[:-1])
        within_store_idx = np.cumsum(within_store_idx)

        # EmployeeKey encoding reserves STAFF_KEY_STORE_MULT slots per store within
        # the staff band. If a store has >= that many staff, within_store_idx spills
        # into the next store's slot range, producing duplicate EmployeeKeys and
        # wrong-store decode in _infer_home_store_key. Guard loudly — the old int64
        # check never fired (max keys are ~5e7, far below int64). The upper band
        # (ONLINE_EMP_KEY_BASE) is already protected: physical StoreKey < 10_000 and
        # idx < 1_000 keep max staff key < 50M.
        max_staff_per_store = int(staff_counts.max())
        if max_staff_per_store >= STAFF_KEY_STORE_MULT:
            raise DimensionError(
                f"A store has {max_staff_per_store} staff but the EmployeeKey encoding "
                f"reserves only {STAFF_KEY_STORE_MULT} slots per store; keys would "
                f"collide across stores. Lower the per-store EmployeeCount."
            )
        staff_ek = EmployeeKeyCodec.encode_staff(staff_sk, within_store_idx)
        staff_parent = EmployeeKeyCodec.encode_store_manager(staff_sk)

        # Sample titles in bulk, then overwrite first k per store with primary sales role
        all_titles = rng.choice(_STAFF_TITLES, size=total_staff, p=_STAFF_TITLES_P).astype(object)
        ps_role = str(primary_sales_role or "Sales Associate")
        k_ps = max(1, int_or(min_primary_sales_per_store, 1))

        # Mark the first k_ps employees of each store as the primary sales role
        k_per_store = np.minimum(staff_counts, k_ps)
        shortfall_stores = int((staff_counts < k_ps).sum())
        if shortfall_stores > 0:
            warn(
                f"{shortfall_stores} store(s) have fewer staff than "
                f"min_primary_sales_per_store={k_ps}; they will have "
                f"fewer '{ps_role}' employees than requested."
            )
        k_total = int(k_per_store.sum())
        if k_total > 0:
            # Build mask of positions that should be primary sales role
            ps_mask = np.zeros(total_staff, dtype=bool)
            pos = 0
            for i in range(n_physical):
                sc = int(staff_counts[i])
                kk = int(k_per_store[i])
                if kk > 0:
                    ps_mask[pos:pos + kk] = True
                pos += sc
            all_titles[ps_mask] = ps_role

        staff_df = pd.DataFrame({
            "EmployeeKey": staff_ek,
            "ParentEmployeeKey": staff_parent,
            "EmployeeName": "",
            "Title": pd.Series(all_titles, dtype="object"),
            "OrgLevel": np.int32(6),
            "OrgUnitType": "Store",
            "RegionId": staff_rid,
            "DistrictId": staff_did,
            "StoreKey": staff_sk,
            "GeographyKey": staff_gk,
        })
    else:
        staff_df = pd.DataFrame(
            columns=corporate_df.columns,
        ).iloc[:0]

    # ---------------------------------------------------------------
    # Online employees — exactly 1 per online store
    # ---------------------------------------------------------------
    _online_idx = np.where(_is_online_store)[0]
    if _online_idx.size > 0:
        _onl_sk = sk_arr[_online_idx]
        _onl_ek = EmployeeKeyCodec.encode_online_rep(_onl_sk)
        online_df = pd.DataFrame({
            "EmployeeKey": _onl_ek,
            "ParentEmployeeKey": mgr_parent_keys[_online_idx],
            "EmployeeName": "",
            "Title": ONLINE_SALES_REP_ROLE,
            "OrgLevel": np.int32(5),
            "OrgUnitType": "Store",
            "RegionId": rid_arr[_online_idx],
            "DistrictId": did_arr[_online_idx],
            "StoreKey": _onl_sk,
            "GeographyKey": gk_arr[_online_idx],
        })
    else:
        online_df = pd.DataFrame(columns=corporate_df.columns).iloc[:0]

    df = pd.concat([corporate_df, mgr_df, staff_df, online_df], ignore_index=True)

    # ------------------------------------------------------------------
    # EmploymentType & FTE — determined at hire based on role
    # ------------------------------------------------------------------
    n_all = len(df)
    titles_np = df["Title"].astype(str).to_numpy()
    pt_prob = np.array(
        [EMPLOYEE_PART_TIME_RATE_BY_ROLE.get(t, 0.10) for t in titles_np],
        dtype=np.float64,
    )
    is_part_time = rng.random(n_all) < pt_prob
    df["EmploymentType"] = np.where(is_part_time, "Part-Time", "Full-Time").astype(object)
    n_pt = int(is_part_time.sum())
    fte = np.ones(n_all, dtype=np.float64)
    if n_pt > 0:
        fte[is_part_time] = rng.choice(EMPLOYEE_PART_TIME_FTE_VALUES, size=n_pt)
    df["FTE"] = fte

    # ------------------------------------------------------------------
    # Dates — hire/termination window assignment
    # ------------------------------------------------------------------
    n = len(df)
    ps_role_str = str(primary_sales_role or "Sales Associate")

    ek_all = pd.to_numeric(df["EmployeeKey"], errors="coerce").fillna(0).astype(np.int32)
    is_sales_associate = (ek_all >= STAFF_KEY_BASE) & (df["Title"].astype(str) == ps_role_str)
    sa_mask_np = is_sales_associate.to_numpy()

    # Hire dates: SAs hired before their store opens (or dataset start);
    # everyone else random within the general window.
    # Static model: all employees hired before global_start
    hire_start_general = global_start - pd.Timedelta(days=365 * 5)
    hire_dates = rand_dates_between(rng, hire_start_general, global_start, n)

    n_sa = int(sa_mask_np.sum())
    if n_sa > 0:
        if store_opening_dates:
            # Per-SA upper bound: min(global_start, store_opening_date)
            sa_store_keys = pd.to_numeric(
                df.loc[sa_mask_np, "StoreKey"], errors="coerce"
            ).fillna(0).astype(np.int32).to_numpy()
            sa_upper = np.array([
                min(global_start, store_opening_dates.get(int(sk), global_start))
                for sk in sa_store_keys
            ], dtype="datetime64[ns]")
            # Clamp: upper must be >= hire_start_general
            sa_lower = np.full(n_sa, hire_start_general, dtype="datetime64[ns]")
            sa_upper = np.maximum(sa_upper, sa_lower + np.timedelta64(1, "D"))
            # Vectorized random hire dates per-SA
            lo_i = sa_lower.astype("int64")
            hi_i = sa_upper.astype("int64")
            sa_hire_i = rng.integers(lo_i, hi_i + 1, dtype=np.int64)
            hire_dates.iloc[sa_mask_np] = pd.to_datetime(
                sa_hire_i, unit="ns"
            ).normalize().to_numpy()
        else:
            sa_hire = rand_dates_between(rng, hire_start_general, global_start, n_sa)
            hire_dates.iloc[sa_mask_np] = sa_hire.to_numpy()

    # Store managers: hire before their store opens
    if store_opening_dates:
        mgr_mask_np = (df["Title"].astype(str) == "Store Manager").to_numpy()
        n_mgr = int(mgr_mask_np.sum())
        if n_mgr > 0:
            mgr_store_keys = pd.to_numeric(
                df.loc[mgr_mask_np, "StoreKey"], errors="coerce"
            ).fillna(0).astype(np.int32).to_numpy()
            mgr_upper = np.array([
                min(global_end, store_opening_dates.get(int(sk), global_end))
                for sk in mgr_store_keys
            ], dtype="datetime64[ns]")
            mgr_lower = np.full(n_mgr, hire_start_general, dtype="datetime64[ns]")
            mgr_upper = np.maximum(mgr_upper, mgr_lower + np.timedelta64(1, "D"))
            lo_i = mgr_lower.astype("int64")
            hi_i = mgr_upper.astype("int64")
            mgr_hire_i = rng.integers(lo_i, hi_i + 1, dtype=np.int64)
            hire_dates.iloc[mgr_mask_np] = pd.to_datetime(
                mgr_hire_i, unit="ns"
            ).normalize().to_numpy()

    df["HireDate"] = hire_dates

    # ------------------------------------------------------------------
    # Static model: all employees active for the full window.
    # No random termination. No attrition. Store closures terminate.
    # ------------------------------------------------------------------
    df["TerminationDate"] = pd.NaT
    df["IsActive"] = True
    df["TerminationReason"] = pd.array([pd.NA] * n, dtype="object")

    # Store closures: terminate all employees at the closing store (vectorized)
    if store_closing_dates:
        sk_all_np = pd.to_numeric(df["StoreKey"], errors="coerce").fillna(0).astype(np.int32).to_numpy()
        for close_sk, close_date in store_closing_dates.items():
            close_date = pd.to_datetime(close_date).normalize()
            if close_date < global_start or close_date > global_end:
                continue
            last_day = (close_date - pd.Timedelta(days=1)).normalize()
            mask = sk_all_np == int(close_sk)
            df.loc[mask, "TerminationDate"] = last_day
            df.loc[mask, "IsActive"] = False
            df.loc[mask, "TerminationReason"] = "Store Closure"

    # Names
    _apply_deterministic_names(
        df,
        seed=int(seed),
        people_pools=people_pools,
        iso_by_geo=iso_by_geo,
        default_region=default_region,
    )

    # Override Store Manager names to match stores.parquet (single source of truth)
    if store_manager_names:
        mgr_mask = df["Title"].astype(str) == "Store Manager"
        if mgr_mask.any():
            mgr_ek = df.loc[mgr_mask, "EmployeeKey"]
            mgr_ek_i32 = pd.to_numeric(mgr_ek, errors="coerce").fillna(0).astype(np.int32)
            mgr_sk = (mgr_ek_i32 - STORE_MGR_KEY_BASE).astype(np.int32)
            names = mgr_sk.map(
                lambda sk: store_manager_names.get(int(sk), "")
            )
            valid = (names != "") & names.notna()
            if valid.any():
                vi = names.index[valid]
                vn = names[valid].to_numpy(dtype=object)
                # Parse "First Middle... Last" — last token is last name,
                # first token is first name, everything in between is middle.
                first_arr = np.empty(len(vn), dtype=object)
                middle_arr = np.empty(len(vn), dtype=object)
                last_arr = np.empty(len(vn), dtype=object)
                for j, full_name in enumerate(vn):
                    parts = str(full_name).split()
                    if len(parts) >= 3:
                        first_arr[j] = parts[0]
                        last_arr[j] = parts[-1]
                        middle_arr[j] = " ".join(parts[1:-1])
                    elif len(parts) == 2:
                        first_arr[j] = parts[0]
                        last_arr[j] = parts[1]
                        middle_arr[j] = ""
                    else:
                        first_arr[j] = str(full_name)
                        last_arr[j] = ""
                        middle_arr[j] = ""
                df.loc[vi, "FirstName"] = first_arr
                df.loc[vi, "LastName"] = last_arr
                df.loc[vi, "MiddleName"] = middle_arr
                df.loc[vi, "EmployeeName"] = vn

    # Final integer casts (single consolidated pass). Guard first: a NaN identity
    # key is corruption, not something to silently fillna(0) into a duplicate 0.
    _assert_identity_keys(df)
    df["EmployeeKey"] = pd.to_numeric(df["EmployeeKey"], errors="coerce").fillna(0).astype(np.int32)
    df["ParentEmployeeKey"] = pd.to_numeric(df["ParentEmployeeKey"], errors="coerce").astype("Int32")
    df["OrgLevel"] = pd.to_numeric(df["OrgLevel"], errors="coerce").fillna(0).astype(np.int32)
    df["RegionId"] = pd.to_numeric(df["RegionId"], errors="coerce").fillna(0).astype(np.int32)
    df["DistrictId"] = pd.to_numeric(df["DistrictId"], errors="coerce").fillna(0).astype(np.int32)
    df["StoreKey"] = pd.to_numeric(df["StoreKey"], errors="coerce").fillna(0).astype(np.int32)
    df["GeographyKey"] = pd.to_numeric(df["GeographyKey"], errors="coerce").fillna(0).astype(np.int32)

    return df
