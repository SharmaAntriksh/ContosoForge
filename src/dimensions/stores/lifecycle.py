"""Store lifecycle: status assignment, opening/closing and renovation dates,
close-reason backfill, and the narrative StoreDescription. Each helper mutates
the store DataFrame in place; the RNG draw order matches the original monolith.

``Status`` (and ``OpenFlag``) is an as-of-dataset-end **snapshot**: renovations
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
from src.utils.logging_utils import debug, warn
from src.defaults import (
    STORE_STATUS as _STORE_STATUS,
    STORE_STATUS_P as _STORE_STATUS_P,
    STORE_CLOSE_REASONS as _CLOSE_REASONS,
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


def build_region_store_map(stores: pd.DataFrame) -> Dict[str, List[int]]:
    """Map StoreRegion -> list of physical StoreKeys."""
    physical = stores[is_physical_store_key(stores["StoreKey"])]
    if "StoreRegion" not in physical.columns:
        return {}
    result: Dict[str, List[int]] = {}
    for region, grp in physical.groupby("StoreRegion"):
        result[str(region)] = grp["StoreKey"].tolist()
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
) -> bool:
    """True if *store_key* was open for the whole window ``[w_start, w_end]``.

    A store qualifies iff it opened on or before ``w_start`` and does not close
    on or before ``w_end``. Renovation is deliberately not considered here — the
    renovation reassignment excludes renovating stores from its candidate pool
    before calling this.
    """
    op = open_dates.get(store_key)
    if op is not None and pd.notna(op) and op > w_start:
        return False
    cl = close_dates.get(store_key)
    if cl is not None and pd.notna(cl) and cl <= w_end:
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

    # Stores opening after dataset_start would have no employees or sales for
    # their pre-open period; shift the window (preserving width) to fit before it.
    if dataset_start:
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
            # Constrain closing dates to [dataset_start, dataset_end]
            ds_start_d = _as_date64d(dataset_start)
            ds_end_d = _as_date64d(dataset_end)
            # Leave a buffer: close at least 30 days into the window,
            # and at least 60 days before the end (for wind-down/transfer)
            min_close_d = _as_date64d(
                (pd.to_datetime(dataset_start) + pd.Timedelta(days=30)).date()
            )
            max_close_d = _as_date64d(
                (pd.to_datetime(dataset_end) - pd.Timedelta(days=60)).date()
            )
            if max_close_d < min_close_d:
                max_close_d = ds_end_d

            n_closed = int(closed_mask.sum())
            close_d = _rand_dates_d(rng, min_close_d, max_close_d, n_closed)
            df.loc[closed_mask, "ClosingDate"] = pd.to_datetime(
                close_d.astype("datetime64[ns]")
            ).normalize()

            # Ensure opening dates for closed stores are before their closing date
            # (opening range may extend into the dataset window)
            closed_idx = np.where(closed_mask)[0]
            open_ts = pd.to_datetime(df.loc[closed_mask, "OpeningDate"])
            close_ts = pd.to_datetime(df.loc[closed_mask, "ClosingDate"])
            bad = open_ts.to_numpy() >= close_ts.to_numpy()
            if bad.any():
                bad_idx = closed_idx[bad]
                for bi in bad_idx:
                    cd = pd.to_datetime(df.at[bi, "ClosingDate"]).normalize()
                    # Place opening at least 180 days before closing
                    new_open = cd - pd.Timedelta(days=rng.integers(180, 730))
                    df.at[bi, "OpeningDate"] = new_open.normalize()
        else:
            open_days     = opening_d.astype("int64")[closed_mask]
            close_end_day = close_end_d.astype("int64")

            late_openers = int((open_days > close_end_day).sum())
            if late_openers:
                warn(
                    f"{late_openers} closed store(s) have OpeningDate after closing_end="
                    f"{closing_end}; their ClosingDate will be set to 30+ days after OpeningDate"
                )

            # Ensure at least 30 days of operation for closed stores
            min_close_offset = 30
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
    closed_mask = status.to_numpy() == "Closed"
    # CloseReason (must exist before StoreDescription references it)
    df["CloseReason"] = ""
    if closed_mask.any():
        df.loc[closed_mask, "CloseReason"] = rng.choice(
            _CLOSE_REASONS, size=int(closed_mask.sum()),
        )

        # Stores closed for renovation: backfill RenovationStartDate/EndDate.
        # Renovation began 30-90 days before ClosingDate; the store never
        # reopened so RenovationEndDate = ClosingDate.
        reno_closed = closed_mask & (df["CloseReason"] == "Renovation")
        n_reno_closed = int(reno_closed.sum())
        if n_reno_closed > 0:
            close_ts = pd.to_datetime(df.loc[reno_closed, "ClosingDate"])
            open_ts = pd.to_datetime(df.loc[reno_closed, "OpeningDate"])
            lead_days = pd.to_timedelta(rng.integers(30, 91, size=n_reno_closed), unit="D")
            reno_start_candidate = (close_ts - lead_days).dt.normalize()
            # Floor at OpeningDate so renovation never starts before the store opens
            # (matters for stores that closed within ~90 days of opening).
            reno_start_floored = reno_start_candidate.where(
                reno_start_candidate >= open_ts, open_ts,
            )
            df.loc[reno_closed, "RenovationStartDate"] = reno_start_floored.values
            df.loc[reno_closed, "RenovationEndDate"] = close_ts.dt.normalize().values


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
