"""Exchange Rates dimension generator.

Produces ``exchange_rates.parquet`` (daily) and optionally
``exchange_rates_monthly.parquet`` (monthly aggregation).

Supports cross-rate triangulation: the FX master always stores
USD-based rates, but output can contain any from/to pair by computing
``rate(A→B) = rate(USD→B) / rate(USD→A)``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.defaults import CURRENCY_BASE
from src.exceptions import ConfigError
from src.integrations.fx_yahoo import build_or_update_fx
from src.utils.config_precedence import resolve_seed
from src.utils.logging_utils import debug, skip, stage
from src.utils.output_utils import write_parquet_with_date32
from src.versioning.version_store import should_regenerate, save_version

from .helpers import normalize_currency_list, parse_fx_date, resolve_fx_dates
from .monthly_rates import build_monthly_rates


# ---------------------------------------------------------
# Master coverage fingerprint (for versioning)
# ---------------------------------------------------------

def _master_signature(master_path: Path, currencies: list[str]):
    """Lightweight fingerprint of the master's real-data coverage.

    Returns ``{currency: [max_date_iso, row_count]}`` for the currencies this
    run needs, or ``None`` when the master file is absent. Reads only the Date
    and ToCurrency columns so the per-run cost stays small.
    """
    if not master_path.exists():
        return None
    try:
        mdf = pd.read_parquet(master_path, columns=["Date", "ToCurrency"])
    except (OSError, ValueError, KeyError):
        return None
    mdf = mdf[mdf["ToCurrency"].isin(currencies)]
    mdf = mdf.assign(Date=pd.to_datetime(mdf["Date"], errors="coerce")).dropna(subset=["Date"])
    if mdf.empty:
        return {}
    agg = mdf.groupby("ToCurrency")["Date"].agg(["max", "count"])
    return {
        cur: [row["max"].strftime("%Y-%m-%d"), int(row["count"])]
        for cur, row in agg.iterrows()
    }


# ---------------------------------------------------------
# Cross-rate triangulation
# ---------------------------------------------------------

def _triangulate_rates(
    master_fx: pd.DataFrame,
    from_currencies: list[str],
    to_currencies: list[str],
) -> pd.DataFrame:
    """Derive all requested from→to pairs from USD-based master data.

    Master invariant: FromCurrency=USD, Rate = units of ToCurrency per 1 USD.

    Triangulation logic:
    - USD → X:  rate = master rate (direct)
    - X → USD:  rate = 1 / master_rate(USD→X)
    - A → B:    rate = master_rate(USD→B) / master_rate(USD→A)
    """
    # Pre-group master by ToCurrency to avoid repeated full-table scans
    by_currency = {cur: grp[["Date", "Rate"]].copy()
                   for cur, grp in master_fx.groupby("ToCurrency")}

    def _need(cur: str) -> pd.DataFrame:
        """Fetch a currency's master rows or raise a clear error.

        A currency can be absent when it has no master rows and none could be
        downloaded — e.g. a fully-future date window for a currency not yet in
        the master (no anchor to project from). Surface that instead of an
        opaque KeyError.
        """
        grp = by_currency.get(cur)
        if grp is None:
            raise ConfigError(
                f"No FX data available for '{cur}'. The master has no rows for it "
                "and none could be obtained for the requested window (a fully-future "
                "window leaves no anchor to project from). Narrow the date window or "
                "refresh the FX master so it covers this currency."
            )
        return grp

    parts = []
    for from_cur in from_currencies:
        for to_cur in to_currencies:
            if from_cur == to_cur:
                continue

            if from_cur == CURRENCY_BASE:
                # Direct: USD → X
                pair = _need(to_cur).copy()
                pair["FromCurrency"] = from_cur
                pair["ToCurrency"] = to_cur
            elif to_cur == CURRENCY_BASE:
                # Inverse: X → USD = 1 / rate(USD → X)
                pair = _need(from_cur).copy()
                pair["Rate"] = 1.0 / pair["Rate"]
                pair["FromCurrency"] = from_cur
                pair["ToCurrency"] = to_cur
            else:
                # Cross: A → B = rate(USD→B) / rate(USD→A)
                df_a = _need(from_cur).rename(columns={"Rate": "RateA"})
                df_b = _need(to_cur).rename(columns={"Rate": "RateB"})
                pair = df_a.merge(df_b, on="Date", how="inner")
                pair["Rate"] = pair["RateB"] / pair["RateA"]
                pair["FromCurrency"] = from_cur
                pair["ToCurrency"] = to_cur
                pair = pair.drop(columns=["RateA", "RateB"])

            parts.append(pair[["Date", "FromCurrency", "ToCurrency", "Rate"]])

    if not parts:
        return pd.DataFrame(columns=["Date", "FromCurrency", "ToCurrency", "Rate"])

    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------
# Main pipeline wrapper
# ---------------------------------------------------------

def run_exchange_rates(cfg, parquet_folder: Path):
    """Exchange Rates dimension: daily (and optionally monthly) tables.

    Writes:
    - ``exchange_rates.parquet`` — daily rates for all from→to pairs
    - ``exchange_rates_monthly.parquet`` — monthly aggregation (if enabled)
    """
    out_path = parquet_folder / "exchange_rates.parquet"
    monthly_path = parquet_folder / "exchange_rates_monthly.parquet"
    fx_cfg = cfg.exchange_rates

    # Resolve date window (always global dates)
    start_str, end_str = resolve_fx_dates(cfg)
    start = parse_fx_date("start", start_str)
    end = parse_fx_date("end", end_str)

    from_currencies = normalize_currency_list(list(fx_cfg.from_currencies or ["USD"]), ensure_base=False)
    to_currencies = normalize_currency_list(list(fx_cfg.to_currencies or []), ensure_base=False)
    base = fx_cfg.base_currency
    master = fx_cfg.master_file
    annual_drift = fx_cfg.future_annual_drift
    include_monthly = fx_cfg.include_monthly
    seed = resolve_seed(cfg, fx_cfg)

    # The master file is always USD-based; ensure base_currency matches
    if base != CURRENCY_BASE:
        raise ConfigError(
            f"base_currency must be '{CURRENCY_BASE}' (master file is USD-based). "
            f"Got base_currency={base!r}. Use from_currencies/to_currencies for "
            f"cross-rate pairs instead."
        )

    # All currencies that need to be in the USD master for triangulation
    all_currencies_for_master = set(from_currencies) | set(to_currencies)
    all_currencies_for_master.discard(CURRENCY_BASE)  # USD→USD is trivial
    master_currencies = sorted(all_currencies_for_master)

    master_path = Path(master).expanduser()

    # Versioning config. master_sig fingerprints the master's real-data coverage
    # for the currencies this run needs, so refreshing/extending the master
    # (e.g. --refresh-fx-master) actually propagates to the output instead of
    # being skipped because the config hash was unchanged. Skip the read when no
    # output exists yet — should_regenerate returns True regardless, so the real
    # signature is recorded only post-generation below.
    pre_sig = _master_signature(master_path, master_currencies) if out_path.exists() else None
    minimal_cfg = {
        "from_currencies": from_currencies,
        "to_currencies": to_currencies,
        "base": base,
        "master_file": master,
        "start": start_str,
        "end": end_str,
        "future_annual_drift": annual_drift,
        "include_monthly": include_monthly,
        "seed": seed,
        "master_sig": pre_sig,
    }

    if not should_regenerate("exchange_rates", minimal_cfg, out_path):
        skip("Exchange Rates up-to-date")
        return

    # Ensure master directory exists
    master_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: Update/build USD-based FX master
    with stage("Updating FX Master"):
        master_fx = build_or_update_fx(
            start, end, str(master_path),
            currencies=master_currencies,
            annual_drift=annual_drift,
            seed=seed,
        )

    master_fx["Date"] = pd.to_datetime(master_fx["Date"], errors="raise").dt.date

    # Step 2: Triangulate to produce all requested from→to pairs
    with stage("Computing cross-rates"):
        df = _triangulate_rates(master_fx, from_currencies, to_currencies)

    df = df[(df["Date"] >= start) & (df["Date"] <= end)].copy()

    if df.empty:
        raise ConfigError(
            "Exchange rates slice is empty after filtering. "
            "Check from_currencies/to_currencies/date window/master_file."
        )

    # Stable ordering
    df = df[["Date", "FromCurrency", "ToCurrency", "Rate"]]
    df = df.sort_values(["Date", "FromCurrency", "ToCurrency"]).reset_index(drop=True)

    # Validate rates
    if not np.isfinite(df["Rate"]).all():
        raise ConfigError("Invalid FX rate: non-finite values found (NaN/inf).")
    if (df["Rate"] <= 0).any():
        raise ConfigError("Invalid FX rate: non-positive values found.")

    # Step 3: Join currency keys for integer-based BI relationships
    currency_path = parquet_folder / "currency.parquet"
    try:
        cur_df = pd.read_parquet(currency_path, columns=["CurrencyKey", "CurrencyCode"])
    except FileNotFoundError:
        raise ConfigError(
            "currency.parquet must exist before exchange_rates can be generated "
            "(needed for FromCurrencyKey/ToCurrencyKey lookups)."
        )
    code_to_key = dict(zip(cur_df["CurrencyCode"], cur_df["CurrencyKey"]))

    df["FromCurrencyKey"] = df["FromCurrency"].map(code_to_key).astype("int32")
    df["ToCurrencyKey"] = df["ToCurrency"].map(code_to_key).astype("int32")

    daily_cols = [
        "Date", "FromCurrencyKey", "ToCurrencyKey",
        "FromCurrency", "ToCurrency", "Rate",
    ]
    df = df[daily_cols]

    parquet_folder.mkdir(parents=True, exist_ok=True)
    write_parquet_with_date32(df, out_path, date_cols=["Date"])
    debug(f"Exchange Rates dimension written: {out_path.name}")

    # Step 4: Monthly aggregation
    if include_monthly:
        with stage("Computing monthly exchange rates"):
            monthly_df = build_monthly_rates(df)

        write_parquet_with_date32(monthly_df, monthly_path, date_cols=["Date"])
        debug(f"Exchange Rates Monthly written: {monthly_path.name}")

    # Re-fingerprint the master to capture any data downloaded during this run,
    # so a rerun on the same day sees a matching hash and skips correctly.
    minimal_cfg["master_sig"] = _master_signature(master_path, master_currencies)
    save_version("exchange_rates", minimal_cfg, out_path)
