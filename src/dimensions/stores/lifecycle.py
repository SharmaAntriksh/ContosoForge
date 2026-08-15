"""Store lifecycle: status assignment, opening/closing and renovation dates,
close-reason backfill, and the narrative StoreDescription. Each helper mutates
the store DataFrame in place; the RNG draw order matches the original monolith.

``Status`` is an as-of-dataset-end **snapshot**: renovations
that finish reopen the store to "Open", and closures flip it to "Closed". It is a
display label, not a point-in-time truth. Any consumer deciding whether a store
was open on a specific date must use the dates (``OpeningDate``/``ClosingDate``/
``RenovationStartDate``/``RenovationEndDate``), never the ``Status`` string.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.utils.logging_utils import debug, info, warn
from src.defaults import (
    STORE_STATUS as _STORE_STATUS,
    STORE_STATUS_P as _STORE_STATUS_P,
    STORE_MIN_OPERATING_DAYS,
    is_physical_store_key,
)
from src.dimensions.stores.columns import _as_date64d, _rand_dates_d


# ---------------------------------------------------------------------------
# Openness queries — the single home for the "dates are truth" rule. Consumers
# (transfer engine, renovation reassignment, coverage post-condition) ask these
# helpers rather than reading the ``Status`` label or re-deriving the predicate.
# ---------------------------------------------------------------------------

def build_store_date_maps(
    stores: pd.DataFrame,
) -> Tuple[
    Dict[int, pd.Timestamp],
    Dict[int, pd.Timestamp],
    Dict[int, pd.Timestamp],
    Dict[int, pd.Timestamp],
]:
    """Extract per-store lifecycle date dicts from a stores frame.

    Returns ``(open_dates, close_dates, reno_start_dates, reno_end_dates)``,
    each keyed by ``int(StoreKey)`` and holding only non-null entries.
    """
    sk_arr = stores["StoreKey"].astype(np.int32).to_numpy()
    open_dates: Dict[int, pd.Timestamp] = {}
    close_dates: Dict[int, pd.Timestamp] = {}

    if "OpeningDate" in stores.columns:
        od = pd.to_datetime(stores["OpeningDate"], errors="coerce")
        for sk, dt in zip(sk_arr, od):
            if pd.notna(dt):
                open_dates[int(sk)] = dt

    if "ClosingDate" in stores.columns:
        cd = pd.to_datetime(stores["ClosingDate"], errors="coerce")
        for sk, dt in zip(sk_arr, cd):
            if pd.notna(dt):
                close_dates[int(sk)] = dt

    reno_start_dates: Dict[int, pd.Timestamp] = {}
    reno_end_dates: Dict[int, pd.Timestamp] = {}
    if "RenovationStartDate" in stores.columns:
        rs = pd.to_datetime(stores["RenovationStartDate"], errors="coerce")
        re = pd.to_datetime(stores["RenovationEndDate"], errors="coerce")
        reno_mask = rs.notna() & re.notna()
        for sk, rsd, red in zip(sk_arr[reno_mask], rs[reno_mask], re[reno_mask]):
            reno_start_dates[int(sk)] = rsd
            reno_end_dates[int(sk)] = red

    return open_dates, close_dates, reno_start_dates, reno_end_dates


def build_store_region_lookup(stores: pd.DataFrame) -> Dict[int, str]:
    """Return StoreKey -> canonical organizational-region identity.

    Structured ``StoreRegionId`` is authoritative when available. The display
    label remains a compatibility fallback for legacy store frames.
    """
    if "StoreRegionId" in stores.columns:
        region_ids = pd.to_numeric(stores["StoreRegionId"], errors="coerce")
        return {
            int(store_key): f"id:{int(region_id)}"
            for store_key, region_id in zip(stores["StoreKey"], region_ids)
            if pd.notna(region_id)
        }
    if "StoreRegion" in stores.columns:
        return {
            int(store_key): f"label:{region}"
            for store_key, region in zip(stores["StoreKey"], stores["StoreRegion"])
            if pd.notna(region)
        }
    return {}


def build_region_store_map(stores: pd.DataFrame) -> Dict[str, List[int]]:
    """Map canonical region identity -> physical StoreKeys."""
    physical = stores[is_physical_store_key(stores["StoreKey"])]
    region_lookup = build_store_region_lookup(physical)
    result: Dict[str, List[int]] = {}
    for store_key in physical["StoreKey"].astype(int):
        region = region_lookup.get(int(store_key))
        if region is not None:
            result.setdefault(region, []).append(int(store_key))
    return result


def store_open_on(
    store_key: int,
    on_date: pd.Timestamp,
    open_dates: Dict[int, pd.Timestamp],
    close_dates: Dict[int, pd.Timestamp],
    reno_start_dates: Optional[Dict[int, pd.Timestamp]] = None,
    reno_end_dates: Optional[Dict[int, pd.Timestamp]] = None,
) -> bool:
    """True if *store_key* is open on *on_date*.

    A store is closed if it hasn't opened yet, has permanently closed,
    or is within its renovation window [reno_start, reno_end).
    """
    opened = open_dates.get(store_key)
    if opened is not None and on_date < opened:
        return False
    closed = close_dates.get(store_key)
    if closed is not None and on_date >= closed:
        return False
    # Renovation window: store is closed during [start, end)
    if reno_start_dates and reno_end_dates:
        rs = reno_start_dates.get(store_key)
        re = reno_end_dates.get(store_key)
        if rs is not None and re is not None and rs <= on_date < re:
            return False
    return True


def store_open_through_window(
    store_key: int,
    w_start: pd.Timestamp,
    w_end: pd.Timestamp,
    open_dates: Dict[int, pd.Timestamp],
    close_dates: Dict[int, pd.Timestamp],
    reno_start_dates: Optional[Dict[int, pd.Timestamp]] = None,
    reno_end_dates: Optional[Dict[int, pd.Timestamp]] = None,
) -> bool:
    """True if *store_key* was open for the whole window ``[w_start, w_end]``.

    A store qualifies iff it opened on or before ``w_start``, does not close on
    or before ``w_end``, and (when renovation maps are supplied) has no closure
    interval overlapping the requested window.
    """
    op = open_dates.get(store_key)
    if op is not None and pd.notna(op) and op > w_start:
        return False
    cl = close_dates.get(store_key)
    if cl is not None and pd.notna(cl) and cl <= w_end:
        return False
    if reno_start_dates and reno_end_dates:
        rs = reno_start_dates.get(store_key)
        re = reno_end_dates.get(store_key)
        if rs is not None and re is not None and rs <= w_end and re > w_start:
            return False
    return True


def assign_status(
    df,
    rng,
    *,
    num_stores,
    closing_enabled,
    dataset_start,
    dataset_end,
    close_share,
):
    # --- Status assignment ---
    # When closing is enabled with a dataset window, we pick exactly
    # close_share fraction of non-Online stores to close during the window.
    # The rest are Open (with a small Renovating fraction).
    st_arr = df["StoreType"].astype(str).to_numpy()
    phys_idx = np.where(st_arr != "Online")[0]
    if closing_enabled and dataset_start and dataset_end:
        # Physical store closures only — online stores are always Open
        n_phys_eligible = len(phys_idx)
        n_phys_close = (
            max(1, int(round(n_phys_eligible * float(close_share))))
            if n_phys_eligible > 0 and close_share > 0.0
            else 0
        )
        # Always keep >= 1 physical store open for the whole window. A
        # close_share high enough to close every physical store leaves late
        # months with no open store; if there are also no online stores, Sales
        # falls back to unstaffed stores and emits EmployeeKey=-1 (orphan FK).
        # A count-aware clamp here is the only correct fix — a plain config
        # ceiling cannot protect small physical-store counts.
        if n_phys_eligible > 0 and n_phys_close >= n_phys_eligible:
            warn(
                f"close_share ({close_share}) would close all {n_phys_eligible} "
                "physical store(s); keeping 1 open to preserve salesperson "
                "coverage in Sales."
            )
            n_phys_close = n_phys_eligible - 1
        phys_close_idx = rng.choice(phys_idx, size=min(n_phys_close, n_phys_eligible), replace=False) if n_phys_close > 0 else np.array([], dtype=np.intp)

        # Of the remaining physical stores, ~5% are Renovating
        remaining_idx = np.setdiff1d(phys_idx, phys_close_idx)
        n_reno = max(0, int(round(len(remaining_idx) * 0.05)))
        reno_idx = rng.choice(remaining_idx, size=min(n_reno, len(remaining_idx)), replace=False) if n_reno > 0 else np.array([], dtype=np.intp)
        status_arr = np.full(num_stores, "Open", dtype=object)
        status_arr[phys_close_idx] = "Closed"
        if reno_idx.size > 0:
            status_arr[reno_idx] = "Renovating"
        df["Status"] = status_arr
    elif not closing_enabled:
        # Closing disabled: all Open, ~5% physical stores Renovating
        n_reno = max(0, int(round(len(phys_idx) * 0.05)))
        status_arr = np.full(num_stores, "Open", dtype=object)
        if n_reno > 0:
            reno_idx = rng.choice(phys_idx, size=min(n_reno, len(phys_idx)), replace=False)
            status_arr[reno_idx] = "Renovating"
        df["Status"] = status_arr
    else:
        # closing_enabled but no dataset window — probabilistic fallback (physical only)
        status_arr = np.full(num_stores, "Open", dtype=object)
        if phys_idx.size > 0:
            status_arr[phys_idx] = rng.choice(_STORE_STATUS, size=phys_idx.size, p=_STORE_STATUS_P)
        df["Status"] = status_arr


def assign_opening_closing_dates(
    df,
    rng,
    *,
    num_stores,
    opening_start,
    opening_end,
    closing_end,
    closing_enabled,
    dataset_start,
    dataset_end,
    allow_mid_window_openings=False,
):
    # Dates at DAY granularity
    open_start_d = _as_date64d(opening_start)
    open_end_d   = _as_date64d(opening_end)
    close_end_d  = _as_date64d(closing_end)

    if open_end_d < open_start_d:
        raise DimensionError(
            f"stores opening_end ({opening_end}) is before opening_start "
            f"({opening_start}); fix the reversed opening window in config."
        )

    # A data window shorter than the minimum operating span makes the >= 1-year
    # guarantee vacuous: no store can be visible that long, and none will close
    # (the closing floor has no room). Warn rather than fail — the window is a
    # deliberate config choice — but say the invariant cannot hold.
    if dataset_start and dataset_end:
        _window_days = int((_as_date64d(dataset_end) - _as_date64d(dataset_start)).astype("int64"))
        if _window_days < STORE_MIN_OPERATING_DAYS:
            warn(
                f"data window is only {_window_days} days — shorter than "
                f"STORE_MIN_OPERATING_DAYS ({STORE_MIN_OPERATING_DAYS}); the minimum "
                "operating-window guarantee cannot hold and no store will close."
            )

    # Stores opening after dataset_start would have no employees or sales for
    # their pre-open period; shift the window (preserving width) to fit before it.
    # Skipped when mid-window openings are explicitly enabled — then stores may
    # open inside the data range and the employee/bridge generation is expected
    # to be opening-aware (no assignments before OpeningDate).
    if dataset_start and not allow_mid_window_openings:
        ds_start_d = _as_date64d(dataset_start)
        one_day = np.timedelta64(1, "D")
        target_end = ds_start_d - one_day
        if open_end_d > target_end:
            width = open_end_d - open_start_d
            new_start = target_end - width
            debug(
                f"opening window [{open_start_d}, {open_end_d}] extends into "
                f"dataset range (starts {dataset_start}); shifting to "
                f"[{new_start}, {target_end}]."
            )
            open_start_d = new_start
            open_end_d = target_end

    # Minimum operating window: with mid-window openings on, a store must open
    # early enough to run at least STORE_MIN_OPERATING_DAYS before dataset_end,
    # so cap the opening window there (a store opening in the last weeks of the
    # data isn't worth capturing). Flag-off shifts the whole window before
    # dataset_start, so this never binds there.
    if allow_mid_window_openings and dataset_end:
        latest_open_d = _as_date64d(dataset_end) - np.timedelta64(STORE_MIN_OPERATING_DAYS, "D")
        if latest_open_d < open_start_d:
            # The entire opening window sits after the cap, so no store could ever
            # run a full year. Silently clamping produced same-day openings with a
            # sub-year footprint (and no closings) — fail loudly instead; this is a
            # config error. Fires before any RNG draw, so valid configs are unchanged.
            raise DimensionError(
                f"stores opening window [{opening_start}, {opening_end}] is incompatible "
                f"with allow_mid_window_openings: every store must open on or before "
                f"{latest_open_d} (dataset_end - {STORE_MIN_OPERATING_DAYS}d) to operate a "
                f"minimum of {STORE_MIN_OPERATING_DAYS} days. Move opening_start earlier "
                f"or extend defaults.dates.end."
            )
        open_end_d = min(open_end_d, latest_open_d)

    if close_end_d < open_end_d:
        warn(
            f"closing_end ({close_end_d}) < opening_end ({open_end_d}); "
            "closed stores may have unrealistic ClosingDates."
        )

    opening_d = _rand_dates_d(rng, open_start_d, open_end_d, num_stores)
    df["OpeningDate"] = pd.to_datetime(opening_d.astype("datetime64[ns]")).normalize()

    df["ClosingDate"] = pd.NaT
    status = df["Status"].astype(str)
    closed_mask = status.to_numpy() == "Closed"

    if closed_mask.any():
        if closing_enabled and dataset_start and dataset_end:
            # Closing dates respect the minimum operating window: a store must run
            # at least STORE_MIN_OPERATING_DAYS of *visible* operation (measured
            # from max(OpeningDate, dataset_start)) before it may close, and every
            # closing leaves a 60-day wind-down buffer before dataset_end. A store
            # selected to close but with no room for a full-year window before that
            # cutoff simply stays open (reverts to "Open") — a sub-year closure
            # isn't worth capturing. Because closing >= opening + a year, the old
            # "pull the opening back before the closing" fixup is no longer needed.
            ds_start_i = int(_as_date64d(dataset_start).astype("int64"))
            max_close_i = int(
                _as_date64d(
                    (pd.to_datetime(dataset_end) - pd.Timedelta(days=60)).date()
                ).astype("int64")
            )

            closed_idx = np.where(closed_mask)[0]
            open_i = opening_d.astype("int64")[closed_mask]
            visible_start_i = np.maximum(open_i, ds_start_i)
            min_close_i = visible_start_i + STORE_MIN_OPERATING_DAYS
            can_close = min_close_i <= max_close_i

            if can_close.any():
                lo = min_close_i[can_close]
                close_days = rng.integers(lo, max_close_i + 1, dtype=np.int64)
                good_idx = closed_idx[can_close]
                df.loc[good_idx, "ClosingDate"] = (
                    pd.to_datetime(close_days.astype("datetime64[D]").astype("datetime64[ns]"))
                    .normalize().to_numpy()
                )

            reopen_idx = closed_idx[~can_close]
            if reopen_idx.size:
                # Reverting Closed -> Open materially shrinks the effective
                # close_share (sometimes to zero), so surface it at info level.
                info(
                    f"{reopen_idx.size} store(s) selected to close lacked room for a "
                    f"{STORE_MIN_OPERATING_DAYS}-day operating window before the close "
                    "cutoff; keeping them Open."
                )
                status_arr = df["Status"].to_numpy().copy()
                status_arr[reopen_idx] = "Open"
                df["Status"] = status_arr
        else:
            open_days     = opening_d.astype("int64")[closed_mask]
            close_end_day = close_end_d.astype("int64")

            late_openers = int((open_days > close_end_day).sum())
            if late_openers:
                warn(
                    f"{late_openers} closed store(s) have OpeningDate after closing_end="
                    f"{closing_end}; their ClosingDate will be set to "
                    f"{STORE_MIN_OPERATING_DAYS}+ days after OpeningDate"
                )

            # Ensure at least a full operating year for closed stores
            min_close_offset = STORE_MIN_OPERATING_DAYS
            effective_end = np.maximum(open_days + min_close_offset, close_end_day)
            close_days = rng.integers(open_days + min_close_offset, effective_end + 1, dtype=np.int64)
            close_d = close_days.astype("datetime64[D]")
            df.loc[closed_mask, "ClosingDate"] = pd.to_datetime(
                close_d.astype("datetime64[ns]")
            ).normalize()


def assign_renovation_dates(df, rng, *, dataset_start, dataset_end):
    # --- Renovation dates for Renovating stores ---
    df["RenovationStartDate"] = pd.NaT
    df["RenovationEndDate"] = pd.NaT
    reno_mask_dt = df["Status"].astype(str).to_numpy() == "Renovating"
    if reno_mask_dt.any() and dataset_start and dataset_end:
        ds_start_ts = pd.Timestamp(dataset_start)
        ds_end_ts = pd.Timestamp(dataset_end)
        n_reno = int(reno_mask_dt.sum())
        # Renovation starts at least 60 days into the dataset window AND at
        # least 30 days after the store opens (cannot renovate before opening),
        # and at least 120 days before dataset_end (room for 3-month min reno).
        ds_earliest_i = (ds_start_ts + pd.Timedelta(days=60)).to_datetime64().astype("datetime64[D]").astype("int64")
        ds_latest_i = (ds_end_ts - pd.Timedelta(days=120)).to_datetime64().astype("datetime64[D]").astype("int64")
        reno_open_ts = pd.to_datetime(df.loc[reno_mask_dt, "OpeningDate"])
        per_store_earliest_i = (
            (reno_open_ts + pd.Timedelta(days=30))
            .values.astype("datetime64[D]").astype("int64")
        )
        low_i = np.maximum(per_store_earliest_i, ds_earliest_i)
        high_i = np.full(n_reno, ds_latest_i, dtype=np.int64)
        # Drop stores whose available window collapses; they stay flagged
        # "Renovating" but with NaT dates (consistent with the original
        # behavior when reno_latest_start <= reno_earliest globally).
        valid = high_i >= low_i
        if valid.any():
            n_valid = int(valid.sum())
            day_offsets = rng.integers(0, (high_i[valid] - low_i[valid] + 1), dtype=np.int64)
            reno_start_days_valid = (low_i[valid] + day_offsets).astype("datetime64[D]")
            reno_dur_days = rng.integers(90, 271, size=n_valid)
            reno_end_days_raw = reno_start_days_valid + reno_dur_days.astype("timedelta64[D]")
            ds_end_d = np.datetime64(ds_end_ts.normalize().to_datetime64(), "D")
            reno_end_days_valid = np.minimum(reno_end_days_raw, ds_end_d)

            reno_start_ns = reno_start_days_valid.astype("datetime64[ns]")
            reno_end_ns = reno_end_days_valid.astype("datetime64[ns]")

            reno_idx_all = np.where(reno_mask_dt)[0]
            reno_idx_valid = reno_idx_all[valid]
            df.loc[reno_idx_valid, "RenovationStartDate"] = reno_start_ns
            df.loc[reno_idx_valid, "RenovationEndDate"] = reno_end_ns

            # Stores whose renovation ended before dataset_end reopen
            reopened_idx = reno_idx_valid[reno_end_days_valid < ds_end_d]
            if len(reopened_idx) > 0:
                status_arr2 = df["Status"].to_numpy().copy()
                status_arr2[reopened_idx] = "Open"
                df["Status"] = status_arr2


def backfill_close_reason(df, rng, *, status):
    """Assign only closure narratives supported by generated store attributes.

    ``Low Sales`` is restricted to RevenueClass C (the default sales demand
    weighting makes that a sustained under-performing cohort). ``Lease Ended``
    requires leased premises. ``Moved Location`` requires a compatible successor
    store and records its key. ``Renovation`` creates the corresponding lifecycle
    window. The unconditional random close-reason draw used previously could make
    any of these claims without supporting data.
    """
    status_series = df["Status"].astype(str) if "Status" in df.columns else status
    closed_mask = status_series.to_numpy() == "Closed"
    df["CloseReason"] = ""
    df["SuccessorStoreKey"] = pd.Series(
        pd.array([pd.NA] * len(df), dtype="Int32"),
        index=df.index,
    )
    if not closed_mask.any():
        return

    store_keys = pd.to_numeric(df["StoreKey"], errors="coerce")
    physical = np.asarray(is_physical_store_key(store_keys.fillna(0).to_numpy()), dtype=bool)
    opening = pd.to_datetime(df["OpeningDate"], errors="coerce")
    closing = pd.to_datetime(df["ClosingDate"], errors="coerce")

    for pos in np.flatnonzero(closed_mask):
        row = df.iloc[pos]
        row_label = df.index[pos]
        reasons = ["Renovation"]
        if str(row.get("RevenueClass", "")) == "C":
            reasons.append("Low Sales")
        if str(row.get("OwnershipType", "")) == "Leased":
            reasons.append("Lease Ended")

        close_date = closing.iloc[pos]
        successor_key: int | None = None
        if pd.notna(close_date) and physical[pos]:
            candidate = physical.copy()
            candidate[pos] = False
            candidate &= df["StoreType"].astype(str).to_numpy() == str(row["StoreType"])
            if "StoreRegionId" in df.columns:
                candidate &= (
                    pd.to_numeric(df["StoreRegionId"], errors="coerce").to_numpy()
                    == pd.to_numeric(row.get("StoreRegionId"), errors="coerce")
                )
            elif "StoreRegion" in df.columns:
                candidate &= df["StoreRegion"].astype(str).to_numpy() == str(row["StoreRegion"])

            candidate &= opening.notna().to_numpy()
            candidate &= (opening >= close_date - pd.Timedelta(days=365)).to_numpy()
            candidate &= (opening <= close_date + pd.Timedelta(days=180)).to_numpy()
            candidate &= (closing.isna() | (closing > close_date)).to_numpy()
            candidate_pos = np.flatnonzero(candidate)
            if candidate_pos.size:
                # Closest opening to the closure, then lowest StoreKey. Stable
                # ordering avoids an extra RNG draw and survives row reordering.
                ranked = sorted(
                    candidate_pos,
                    key=lambda i: (
                        abs((opening.iloc[i] - close_date).days),
                        int(store_keys.iloc[i]),
                    ),
                )
                successor_key = int(store_keys.iloc[ranked[0]])
                reasons.append("Moved Location")

        reason = reasons[int(rng.integers(0, len(reasons)))]
        df.at[row_label, "CloseReason"] = reason
        if reason == "Moved Location":
            df.at[row_label, "SuccessorStoreKey"] = successor_key
        elif reason == "Renovation":
            lead_days = int(rng.integers(30, 91))
            open_date = opening.iloc[pos]
            renovation_start = close_date - pd.Timedelta(days=lead_days)
            if pd.notna(open_date):
                renovation_start = max(renovation_start, open_date)
            df.at[row_label, "RenovationStartDate"] = renovation_start.normalize()
            df.at[row_label, "RenovationEndDate"] = close_date.normalize()


def build_store_descriptions(df, *, stype, status, loc_full):
    closed_mask = status.to_numpy() == "Closed"
    # StoreDescription
    opened = df["OpeningDate"].dt.strftime("%Y-%m-%d")
    closed = df["ClosingDate"].dt.strftime("%Y-%m-%d").fillna("")

    base = (
        df["StoreName"].astype(str)
        + " is a "
        + stype.str.lower()
        + " ("
        + status.str.lower()
        + ") location in "
        + loc_full.astype(str)
        + ". Opened "
        + opened
        + ". Size "
        + df["SquareFootage"].astype(str)
        + " sqft; headcount "
        + df["EmployeeCount"].astype(str)
        + ". Manager: "
        + df["StoreManager"].astype(str)
        + "."
    )

    online_mask = stype == "Online"
    if online_mask.any():
        base.loc[online_mask] = (
            df.loc[online_mask, "StoreName"].astype(str)
            + " operates as an online channel serving "
            + loc_full.loc[online_mask].astype(str)
            + ". Launched "
            + opened.loc[online_mask]
            + ". Estimated headcount "
            + df.loc[online_mask, "EmployeeCount"].astype(str)
            + "."
        )

    if closed_mask.any():
        base.loc[closed_mask] = (
            base.loc[closed_mask]
            + " Closed "
            + closed.loc[closed_mask]
            + " ("
            + df.loc[closed_mask, "CloseReason"].astype(str)
            + ")."
        )

    reno_mask = df["Status"].astype(str) == "Renovating"
    if reno_mask.any():
        reno_start_str = pd.to_datetime(df.loc[reno_mask, "RenovationStartDate"]).dt.strftime("%Y-%m-%d").fillna("")
        reno_end_str = pd.to_datetime(df.loc[reno_mask, "RenovationEndDate"]).dt.strftime("%Y-%m-%d").fillna("")
        base.loc[reno_mask] = (
            base.loc[reno_mask]
            + " Currently renovating ("
            + reno_start_str + " to " + reno_end_str
            + "); limited operations."
        )

    # Stores that completed renovation get an updated description
    completed_reno = (
        df["RenovationStartDate"].notna()
        & df["RenovationEndDate"].notna()
        & (df["Status"] == "Open")
    )
    if completed_reno.any():
        reno_start_str = pd.to_datetime(df.loc[completed_reno, "RenovationStartDate"]).dt.strftime("%Y-%m-%d")
        reno_end_str = pd.to_datetime(df.loc[completed_reno, "RenovationEndDate"]).dt.strftime("%Y-%m-%d")
        base.loc[completed_reno] = (
            base.loc[completed_reno]
            + " Renovated " + reno_start_str + " to " + reno_end_str
            + "; reopened."
        )

    df["StoreDescription"] = base
