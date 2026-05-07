"""SCD Type 2 — price revision versions for the product dimension."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.defaults import SCD2_END_OF_TIME
from src.utils import info

from .pricing import snap_drifted_prices


def generate_scd2_versions(
    rng: np.random.Generator,
    base_df: pd.DataFrame,
    prod_cfg,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    *,
    pricing_cfg=None,
) -> pd.DataFrame:
    """Expand products into SCD2 version rows with price revisions.

    Fully vectorized — no per-product Python loops.  Each version represents
    a price revision period.  Version 1 keeps the original price; subsequent
    versions apply cumulative drift.

    Returns a new DataFrame sorted by ProductID + VersionNumber, with
    ProductKey reassigned sequentially (1..N_total_rows).
    """
    revision_freq = int(getattr(prod_cfg, "revision_frequency", 12))
    price_drift = float(getattr(prod_cfg, "price_drift", 0.05))
    max_versions = int(getattr(prod_cfg, "max_versions", 4))
    revision_prob = float(getattr(prod_cfg, "revision_probability", 0.40))

    N = len(base_df)
    if max_versions <= 1 or revision_freq <= 0:
        return base_df

    total_months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
    max_possible_versions = min(max_versions, max(1, total_months // revision_freq + 1))

    if max_possible_versions <= 1:
        return base_df

    # How many versions each product gets (1-based). Geometric draw skews
    # the mass toward V1 (~60/24/10/6 across V1..V4 at default p=0.40),
    # mirroring real repricing cadence — most SKUs hold their launch price.
    p_stop = max(1e-3, 1.0 - revision_prob)  # geometric needs strictly >0
    n_versions = np.clip(
        rng.geometric(p_stop, size=N).astype(np.int64),
        1, max_possible_versions,
    )

    # Random offset for first revision (months from start_date)
    first_offsets = rng.integers(1, max(2, revision_freq), size=N, dtype=np.int64)

    # Pre-generate all drift values for max_possible_versions-1 extra versions per product
    # Shape: (N, max_possible_versions-1)
    n_extra_max = max_possible_versions - 1
    if n_extra_max > 0:
        all_drifts = 1.0 + rng.uniform(-price_drift, price_drift, size=(N, n_extra_max))
    else:
        all_drifts = np.empty((N, 0), dtype=np.float64)

    # Build row indices: which base row does each output row come from?
    # Product i contributes n_versions[i] rows
    total_rows = int(n_versions.sum())
    row_idx = np.repeat(np.arange(N, dtype=np.int64), n_versions)
    # Version number within each product (0-based): position minus group start
    offsets = np.zeros(N + 1, dtype=np.int64)
    np.cumsum(n_versions, out=offsets[1:])
    ver_within = np.arange(total_rows, dtype=np.int64) - offsets[row_idx]

    # Compute EffectiveStartDate as month offset from start_date
    # Version 0: start_date (offset=0)
    # Version v>=1: first_offsets[product] + (v-1) * revision_freq
    month_offsets = np.where(
        ver_within == 0,
        0,
        first_offsets[row_idx] + (ver_within - 1) * revision_freq,
    )

    # Convert month offsets to dates via pure numpy arithmetic (no Python loop)
    start_day = min(start_date.day, 28)  # safe day for all months
    start_year = start_date.year
    start_month = start_date.month
    total_month = start_month - 1 + month_offsets  # 0-based months from Jan of start_year
    eff_years = start_year + total_month // 12
    eff_months = total_month % 12 + 1  # back to 1-based

    eff_dates = (
        (eff_years - 1970).astype("datetime64[Y]")
        + (eff_months - 1).astype("timedelta64[M]")
        + np.timedelta64(start_day - 1, "D")
    ).astype("datetime64[D]")

    # Clip versions that exceed end_date
    end_np = np.datetime64(end_date.date(), "D")
    valid = (ver_within == 0) | (eff_dates <= end_np)

    # Filter to valid rows only
    row_idx = row_idx[valid]
    ver_within = ver_within[valid]
    eff_dates = eff_dates[valid]
    total_rows = int(valid.sum())

    # Build result DataFrame by repeating base rows
    result = base_df.iloc[row_idx].reset_index(drop=True)
    result["VersionNumber"] = (ver_within + 1).astype(np.int32)
    result["EffectiveStartDate"] = pd.to_datetime(eff_dates)

    # Apply cumulative price drift for versions > 1
    # For each row, compute cumulative drift product up to its version
    list_prices = result["ListPrice"].to_numpy(dtype=np.float64, copy=True)
    unit_costs = result["UnitCost"].to_numpy(dtype=np.float64, copy=True)

    if n_extra_max > 0:
        # Compute cumulative drift per product per version
        cum_drifts = np.cumprod(all_drifts, axis=1)  # shape (N, n_extra_max)

        drifted_mask = np.zeros(total_rows, dtype=bool)
        for v in range(1, max_possible_versions):
            mask = ver_within == v
            if not mask.any():
                continue
            prods = row_idx[mask]
            drift_v = cum_drifts[prods, v - 1]
            list_prices[mask] = list_prices[mask] * drift_v
            unit_costs[mask] = np.minimum(unit_costs[mask] * drift_v, list_prices[mask])
            drifted_mask |= mask

        # Re-snap drifted prices to the same appearance grid used by
        # apply_product_pricing so SCD2 versions look equally realistic.
        if drifted_mask.any():
            lp_snapped, uc_snapped = snap_drifted_prices(
                list_prices[drifted_mask], unit_costs[drifted_mask], pricing_cfg)
            list_prices[drifted_mask] = lp_snapped
            unit_costs[drifted_mask] = uc_snapped

    # Snapping can push cost above list price in edge cases
    unit_costs = np.minimum(unit_costs, list_prices)
    result["ListPrice"] = np.round(list_prices, 2)
    result["UnitCost"] = np.round(unit_costs, 2)

    # Sort by ProductID + VersionNumber
    result = result.sort_values(["ProductID", "VersionNumber"]).reset_index(drop=True)

    # Vectorised EffectiveEndDate
    eff_start_arr = result["EffectiveStartDate"].to_numpy()
    pid_arr = result["ProductID"].to_numpy()
    same_pid_as_next = np.empty(total_rows, dtype=bool)
    same_pid_as_next[:-1] = pid_arr[:-1] == pid_arr[1:]
    same_pid_as_next[-1] = False

    eff_end_arr = np.full(total_rows, SCD2_END_OF_TIME, dtype="datetime64[ns]")
    is_current_arr = np.ones(total_rows, dtype=bool)
    _shift_mask = np.flatnonzero(same_pid_as_next)
    eff_end_arr[_shift_mask] = eff_start_arr[_shift_mask + 1] - np.timedelta64(1, "D")
    is_current_arr[_shift_mask] = False

    result["EffectiveEndDate"] = eff_end_arr
    result["IsCurrent"] = is_current_arr

    # ProductKey becomes the per-version surrogate; ProductID/BaseProductKey
    # retain pre-SCD2 values for cross-version joins. See CLAUDE.md gotcha #25.
    result["ProductKey"] = np.arange(1, total_rows + 1, dtype="int64")

    # Count realized history (post end_date filter) rather than n_versions
    # intent — some sampled versions land past end_date and get dropped above.
    _, _pid_counts = np.unique(pid_arr, return_counts=True)
    n_with_history = int((_pid_counts > 1).sum())
    info(f"Products SCD2: {n_with_history:,}/{N:,} products have price history "
         f"({total_rows:,} total rows, max {max_possible_versions} versions)")

    return result
