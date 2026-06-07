"""Currency dimension generator.

Produces ``currency.parquet`` with columns:
  CurrencyKey, CurrencyCode, CurrencyName, CurrencySymbol, DecimalPlaces
"""
from __future__ import annotations

from pathlib import Path
from typing import List

import pandas as pd

from src.utils.logging_utils import info, skip, stage, warn
from src.utils.output_utils import write_parquet_with_date32
from src.versioning.version_store import should_regenerate, save_version

from .helpers import (
    normalize_currency_list,
    currency_name,
    currency_symbol,
    currency_decimal_places,
)


# ---------------------------------------------------------
# Core builder
# ---------------------------------------------------------

def build_dim_currency(currencies: List[str]) -> pd.DataFrame:
    """Build the currency dimension DataFrame from a normalized currency list."""
    currencies = normalize_currency_list(currencies)
    return pd.DataFrame({
        "CurrencyKey": pd.RangeIndex(1, len(currencies) + 1).astype("int32"),
        "CurrencyCode": currencies,
        "CurrencyName": [currency_name(c) for c in currencies],
        "CurrencySymbol": [currency_symbol(c) for c in currencies],
        "DecimalPlaces": [currency_decimal_places(c) for c in currencies],
    })


def _resolve_currency_list(explicit, fx_from, fx_to) -> List[str]:
    """Effective, normalized currency list for the dimension.

    When *explicit* (``cfg.currency.currencies``) is set it is used, but always
    unioned with the FX from/to currencies: the exchange_rates dimension looks up
    a CurrencyKey for every from/to currency, so the currency dim must superset
    them — otherwise the FX key-join maps a missing code to NaN and crashes on
    ``.astype("int32")`` (FX-CUR-1). When unset, the list is derived from the FX
    from/to union (or ``["USD"]``).
    """
    # Pre-dedupe + upper-case the FX codes: normalize_currency_list raises on
    # duplicates and from/to lists overlap (e.g. USD in both), and the membership
    # test below compares against the already-normalized `currencies`.
    fx_codes = list(dict.fromkeys(
        str(c).strip().upper()
        for c in (list(fx_from or []) + list(fx_to or []))
        if str(c).strip()
    ))
    if explicit:
        currencies = normalize_currency_list(explicit)
        missing = [c for c in fx_codes if c not in set(currencies)]
        if missing:
            warn(
                f"currency.currencies omits FX currencies {missing}; adding them so "
                "FromCurrencyKey/ToCurrencyKey resolve (the currency dim must contain "
                "every exchange_rates from/to currency)."
            )
            currencies = normalize_currency_list(currencies + missing)
        return currencies
    return normalize_currency_list(fx_codes or ["USD"])


# ---------------------------------------------------------
# Pipeline wrapper
# ---------------------------------------------------------

def run_currency(cfg, parquet_folder: Path) -> None:
    """Generate and write the currency dimension.

    Currency list is sourced from ``cfg.currency.currencies`` when set
    explicitly, otherwise derived from the union of
    ``cfg.exchange_rates.from_currencies`` and
    ``cfg.exchange_rates.to_currencies``.
    """
    from src.engine.config.config_schema import CurrencyConfig

    cur_cfg = cfg.currency or CurrencyConfig()
    fx_cfg = cfg.exchange_rates
    out_path = Path(parquet_folder) / "currency.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine effective currency list
    currencies = _resolve_currency_list(
        cur_cfg.currencies, fx_cfg.from_currencies, fx_cfg.to_currencies
    )

    version_cfg = {
        "currencies": currencies,
        "base_currency": (fx_cfg.base_currency or "").upper(),
    }

    if not should_regenerate("currency", version_cfg, out_path):
        skip("Currency up-to-date")
        return

    compression = cur_cfg.parquet_compression
    compression_level = cur_cfg.parquet_compression_level
    force_date32 = bool(cur_cfg.force_date32)

    with stage("Generating Currency"):
        df = build_dim_currency(currencies)

        write_parquet_with_date32(
            df,
            out_path,
            cast_all_datetime=False,
            compression=str(compression),
            compression_level=(int(compression_level) if compression_level is not None else None),
            force_date32=force_date32,
        )

    save_version("currency", version_cfg, out_path)
    info(f"Currency dimension written: {out_path.name}")
