import pandas as pd
from pathlib import Path
from datetime import timedelta

from src.utils.logging_utils import info, warn
from src.defaults import CURRENCY_BASE, CURRENCY_DEFAULT_LIST


# ---------------------------------------------------------
# Download single currency history (USD -> CUR)
# ---------------------------------------------------------
def download_history(currency, start_date, end_date):
    """
    Download historical FX data and return rates as:
      Rate = units of `currency` per 1 USD  (USD -> currency)

    Strategy:
    - Prefer Yahoo ticker: USD{CUR}=X  (usually already USD -> CUR)
    - Fallback to: {CUR}USD=X and invert
    """
    # Imported lazily: yfinance (and its transitive deps) is heavy, and importing it
    # at module scope pulled it into every spawned sales worker via the dimensions
    # import chain. Workers never download FX, so defer the cost to actual use.
    import yfinance as yf

    # Normalize input dates to Timestamps (safe for yfinance + pd.date_range)
    start_ts = pd.to_datetime(start_date).normalize()
    end_ts = pd.to_datetime(end_date).normalize()

    if currency == CURRENCY_BASE:
        dates = pd.date_range(start=start_ts, end=end_ts, freq="D")
        return pd.DataFrame({"Date": dates, "Rate": 1.0})

    # Primary: USD -> CUR
    primary = f"{CURRENCY_BASE}{currency}=X"
    fallback = f"{currency}{CURRENCY_BASE}=X"  # may represent USD per 1 CUR, invert to get CUR per 1 USD

    def _download(ticker: str) -> pd.DataFrame:
        data = yf.download(
            ticker,
            start=start_ts - timedelta(days=3),
            end=end_ts + timedelta(days=3),
            auto_adjust=False,
            progress=False,
        )
        return data

    invert = False
    data = _download(primary)

    if data.empty:
        data = _download(fallback)
        invert = True

    if data.empty:
        raise ValueError(f"No FX data found for {currency} (tried {primary} and {fallback})")

    # Flatten MultiIndex columns if present
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [
            "_".join([str(c) for c in col if c]).strip()
            for col in data.columns.values
        ]

    # Pick a Close-like column
    rate_col = None
    for col in data.columns:
        if str(col).lower().startswith("close"):
            rate_col = col
            break

    if rate_col is None:
        raise ValueError(f"No 'Close' column found for {currency}. Found: {list(data.columns)}")

    data = data[[rate_col]].rename(columns={rate_col: "Rate"}).reset_index()

    # Ensure Date is datetime64[ns]
    if "Date" in data.columns:
        data["Date"] = pd.to_datetime(data["Date"])
    else:
        # yfinance sometimes returns index under different name, but Date is typical
        data = data.rename(columns={data.columns[0]: "Date"})
        data["Date"] = pd.to_datetime(data["Date"])

    # Invert if fallback ticker used (to get USD -> CUR)
    if invert:
        if (data["Rate"] <= 0).any():
            raise ValueError(f"Non-positive FX rates encountered for {currency} before inversion.")
        data["Rate"] = 1.0 / data["Rate"]

    return data[["Date", "Rate"]]


# ---------------------------------------------------------
# Fill missing days safely
# ---------------------------------------------------------
def fill_missing_days(df, start_date, end_date):
    """
    Fill weekends/holidays with forward fill (previous day),
    then backfill for leading gaps, then default 1.0.
    """
    start_ts = pd.to_datetime(start_date).normalize()
    end_ts = pd.to_datetime(end_date).normalize()

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()

    full = pd.DataFrame({"Date": pd.date_range(start=start_ts, end=end_ts, freq="D")})
    merged = full.merge(df, on="Date", how="left")

    # Your requirement: missing days filled from previous day
    merged["Rate"] = merged["Rate"].ffill()
    merged["Rate"] = merged["Rate"].bfill()

    n_missing = merged["Rate"].isna().sum()
    if n_missing:
        warn(f"fill_missing_days: {n_missing} day(s) have no rate data and no neighbours to fill from; using 1.0 as fallback.")
        merged["Rate"] = merged["Rate"].fillna(1.0)

    return merged


# ---------------------------------------------------------
# Refresh master FX store to today
# ---------------------------------------------------------
def refresh_fx_master(out_path):
    """
    Top up the master FX file to today's date.

    Reads whatever currencies are already stored in the master and downloads
    only the gap from each currency's last recorded date to today.  No config
    or date-range arguments needed — the master is self-describing.

    Intended to be run on-demand (e.g. via --refresh-fx-master CLI flag) so
    the bundled file stays current without triggering a full pipeline run.
    """
    out_path = Path(out_path)
    if not out_path.exists():
        raise FileNotFoundError(f"FX master file not found: {out_path}")

    master = pd.read_parquet(out_path)
    if master.empty:
        raise ValueError("FX master file is empty — nothing to refresh.")

    master["Date"] = pd.to_datetime(master["Date"]).dt.normalize()
    today = pd.Timestamp.now().normalize()

    currencies_in_master = master["ToCurrency"].unique().tolist()
    updates = []

    for cur in currencies_in_master:
        df_cur = master[master["ToCurrency"] == cur]
        last_date = pd.to_datetime(df_cur["Date"].max()).normalize()

        if last_date >= today:
            info(f"FX for {cur} already up to date ({last_date.date()}); skipping.")
            continue

        gap_start = last_date + pd.Timedelta(days=1)
        info(f"Refreshing FX for {cur}: {gap_start.date()} -> {today.date()}")
        df_gap = download_history(cur, gap_start, today)
        df_gap = fill_missing_days(df_gap, gap_start, today)
        df_gap["FromCurrency"] = CURRENCY_BASE
        df_gap["ToCurrency"] = cur
        updates.append(df_gap)

    if not updates:
        info("FX master already up to date - nothing downloaded.")
        return master

    updates_df = pd.concat(updates, ignore_index=True)
    master_updated = pd.concat([master, updates_df], ignore_index=True)
    master_updated["Date"] = pd.to_datetime(master_updated["Date"]).dt.normalize()
    master_updated = (
        master_updated
        .drop_duplicates(subset=["Date", "FromCurrency", "ToCurrency"], keep="last")
        .sort_values(["Date", "FromCurrency", "ToCurrency"])
        .reset_index(drop=True)
    )

    master_updated.to_parquet(out_path, index=False)
    info(f"FX master refreshed -> {out_path.name}  ({len(master_updated)} rows)")
    return master_updated


# ---------------------------------------------------------
# Build or update master FX store
# ---------------------------------------------------------
def build_or_update_fx(start_date, end_date, out_path, currencies=None, annual_drift=0.02):
    """
    Build or update a master FX file covering the date range.

    Stored invariant:
      FromCurrency = USD
      ToCurrency   = CUR
      Rate         = CUR per 1 USD (USD -> CUR)

    Date is kept as datetime64[ns] throughout.

    For dates beyond the last real data point (i.e. future dates), rates are
    projected using daily compounding:
      projected_rate = anchor_rate * (1 + annual_drift) ^ (days_ahead / 365.25)
    where anchor_rate is the last known real rate.  These projected values are
    NOT written back to the master file.
    """
    if annual_drift <= -1.0:
        raise ValueError(
            f"annual_drift must be > -1.0 (got {annual_drift}); "
            "values <= -1 produce zero or negative projected rates."
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    curr_list = currencies if currencies is not None else CURRENCY_DEFAULT_LIST

    start_ts = pd.to_datetime(start_date).normalize()
    end_ts = pd.to_datetime(end_date).normalize()

    # Load existing master file (keep datetime64)
    if out_path.exists():
        master = pd.read_parquet(out_path)
        if not master.empty:
            master["Date"] = pd.to_datetime(master["Date"]).dt.normalize()
    else:
        master = pd.DataFrame(columns=["Date", "FromCurrency", "ToCurrency", "Rate"])

    today = pd.Timestamp.now().normalize()
    updates = []
    cached_currencies = []

    for cur in curr_list:
        if master.empty:
            cur_existing_start = None
            cur_existing_end = None
        else:
            df_cur = master[master["ToCurrency"] == cur]
            if df_cur.empty:
                cur_existing_start = None
                cur_existing_end = None
            else:
                cur_existing_start = pd.to_datetime(df_cur["Date"].min()).normalize()
                cur_existing_end = pd.to_datetime(df_cur["Date"].max()).normalize()

        # Determine which gaps need downloading (only fetch what isn't already in the master)
        gaps = []
        if cur_existing_start is None:
            # No existing data — download the full requested range (capped at today for real rates)
            gaps.append((start_ts, min(end_ts, today)))
        else:
            if start_ts < cur_existing_start:
                gaps.append((start_ts, cur_existing_start - pd.Timedelta(days=1)))
            if end_ts > cur_existing_end:
                gaps.append((cur_existing_end + pd.Timedelta(days=1), min(end_ts, today)))

        if not gaps:
            cached_currencies.append(cur)
        else:
            for gap_start, gap_end in gaps:
                if gap_start > gap_end:
                    continue
                info(f"Downloading FX for {cur}: {gap_start.date()} -> {gap_end.date()}")
                df_gap = download_history(cur, gap_start, gap_end)
                df_gap = fill_missing_days(df_gap, gap_start, gap_end)
                df_gap["FromCurrency"] = CURRENCY_BASE
                df_gap["ToCurrency"] = cur
                updates.append(df_gap)

    if cached_currencies:
        info(f"FX cached (already covered): {', '.join(cached_currencies)}")

    # Merge any new downloads into master and persist (only real data goes into the file)
    if updates:
        updates_df = pd.concat(updates, ignore_index=True)
        master_updated = pd.concat([master, updates_df], ignore_index=True) if not master.empty else updates_df
        master_updated["Date"] = pd.to_datetime(master_updated["Date"]).dt.normalize()
        master_updated = (
            master_updated
            .drop_duplicates(subset=["Date", "FromCurrency", "ToCurrency"], keep="last")
            .sort_values(["Date", "FromCurrency", "ToCurrency"])
            .reset_index(drop=True)
        )
        master_updated.to_parquet(out_path, index=False)
    else:
        master_updated = master.copy()

    # Build the return DataFrame for the full requested range.
    # Historical gaps (weekends/holidays) are filled via ffill.
    # Future dates (beyond the last real rate) are projected with daily compounding.
    # Neither projected values nor gap-fills are written back to the master file.
    full_range = pd.date_range(start=start_ts, end=end_ts, freq="D")
    parts = []
    for cur in curr_list:
        df_cur = master_updated[master_updated["ToCurrency"] == cur].copy()
        if df_cur.empty:
            continue

        full = pd.DataFrame({"Date": full_range})
        merged = full.merge(df_cur, on="Date", how="left")

        # Fill weekends/holidays within the real data range
        merged["Rate"] = merged["Rate"].ffill().bfill()

        # Project future dates beyond the last real anchor
        anchor_date = df_cur["Date"].max()
        future_mask = merged["Date"] > anchor_date
        if future_mask.any():
            # Derive anchor_rate from the ffill'd merged frame when possible.
            # When the entire requested range is beyond anchor_date (e.g. a fully
            # future date window), historical within the merged frame is empty —
            # fall back to the last real rate from df_cur directly.
            historical = merged.loc[~future_mask, "Rate"]
            if not historical.empty and not historical.iloc[-1:].isna().all():
                anchor_rate = historical.iloc[-1]
            else:
                anchor_rate = df_cur["Rate"].iloc[-1]
            days_ahead = (merged.loc[future_mask, "Date"] - anchor_date).dt.days
            merged.loc[future_mask, "Rate"] = anchor_rate * (1 + annual_drift) ** (days_ahead / 365.25)

        merged["FromCurrency"] = CURRENCY_BASE
        merged["ToCurrency"] = cur
        parts.append(merged[["Date", "FromCurrency", "ToCurrency", "Rate"]])

    return pd.concat(parts, ignore_index=True) if parts else master_updated
