"""Geography dimension generator.

Loads cities from a master parquet file (``data/geography/geography_master.parquet``)
and filters to currencies listed in ``exchange_rates.from_currencies`` / ``to_currencies``.

The master file ships with the repo and can be extended by users —
add rows for new cities and list their currency in config.yaml.

If the master file is missing, falls back to a minimal built-in city list
for backward compatibility.

Output columns:
  GeographyKey, City, State, Country, Continent, ISOCode,
  Latitude, Longitude, Timezone, Population
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from src.exceptions import ConfigError, DimensionError
from src.utils.logging_utils import debug, skip, stage, warn
from src.versioning.version_store import should_regenerate, save_version


# Default master file path (relative to project root)
_DEFAULT_MASTER_PATH = Path("data/geography/geography_master.parquet")

OUTPUT_COLS = [
    "GeographyKey", "City", "State", "Country", "Continent", "ISOCode",
    "Latitude", "Longitude", "Timezone", "Population",
]

# Minimal fallback when master parquet is missing (original 71 cities).
# Only used for backward compatibility — the master file is preferred.
FALLBACK_ROWS: List[Tuple[str, str, str, str, str]] = [
    ("New York", "New York", "United States", "North America", "USD"),
    ("Los Angeles", "California", "United States", "North America", "USD"),
    ("Chicago", "Illinois", "United States", "North America", "USD"),
    ("Houston", "Texas", "United States", "North America", "USD"),
    ("Miami", "Florida", "United States", "North America", "USD"),
    ("Seattle", "Washington", "United States", "North America", "USD"),
    ("Dallas", "Texas", "United States", "North America", "USD"),
    ("Atlanta", "Georgia", "United States", "North America", "USD"),
    ("Denver", "Colorado", "United States", "North America", "USD"),
    ("Phoenix", "Arizona", "United States", "North America", "USD"),
    ("Boston", "Massachusetts", "United States", "North America", "USD"),
    ("San Francisco", "California", "United States", "North America", "USD"),
    ("Toronto", "Ontario", "Canada", "North America", "CAD"),
    ("Vancouver", "British Columbia", "Canada", "North America", "CAD"),
    ("Montreal", "Quebec", "Canada", "North America", "CAD"),
    ("Calgary", "Alberta", "Canada", "North America", "CAD"),
    ("Ottawa", "Ontario", "Canada", "North America", "CAD"),
    ("London", "London", "United Kingdom", "Europe", "GBP"),
    ("Manchester", "Manchester", "United Kingdom", "Europe", "GBP"),
    ("Birmingham", "West Midlands", "United Kingdom", "Europe", "GBP"),
    ("Edinburgh", "Scotland", "United Kingdom", "Europe", "GBP"),
    ("Leeds", "West Yorkshire", "United Kingdom", "Europe", "GBP"),
    ("Berlin", "Berlin", "Germany", "Europe", "EUR"),
    ("Munich", "Bavaria", "Germany", "Europe", "EUR"),
    ("Hamburg", "Hamburg", "Germany", "Europe", "EUR"),
    ("Frankfurt", "Hesse", "Germany", "Europe", "EUR"),
    ("Cologne", "North Rhine-Westphalia", "Germany", "Europe", "EUR"),
    ("Paris", "Île-de-France", "France", "Europe", "EUR"),
    ("Lyon", "Auvergne-Rhône-Alpes", "France", "Europe", "EUR"),
    ("Marseille", "Provence-Alpes-Côte d'Azur", "France", "Europe", "EUR"),
    ("Toulouse", "Occitanie", "France", "Europe", "EUR"),
    ("Nice", "Provence-Alpes-Côte d'Azur", "France", "Europe", "EUR"),
    ("Madrid", "Madrid", "Spain", "Europe", "EUR"),
    ("Barcelona", "Catalonia", "Spain", "Europe", "EUR"),
    ("Valencia", "Valencia", "Spain", "Europe", "EUR"),
    ("Rome", "Lazio", "Italy", "Europe", "EUR"),
    ("Milan", "Lombardy", "Italy", "Europe", "EUR"),
    ("Naples", "Campania", "Italy", "Europe", "EUR"),
    ("Zurich", "Zurich", "Switzerland", "Europe", "CHF"),
    ("Stockholm", "Stockholm", "Sweden", "Europe", "SEK"),
    ("Warsaw", "Masovia", "Poland", "Europe", "PLN"),
    ("Oslo", "Oslo", "Norway", "Europe", "NOK"),
    ("Mumbai", "Maharashtra", "India", "Asia", "INR"),
    ("Delhi", "Delhi", "India", "Asia", "INR"),
    ("Bengaluru", "Karnataka", "India", "Asia", "INR"),
    ("Hyderabad", "Telangana", "India", "Asia", "INR"),
    ("Chennai", "Tamil Nadu", "India", "Asia", "INR"),
    ("Pune", "Maharashtra", "India", "Asia", "INR"),
    ("Shanghai", "Shanghai", "China", "Asia", "CNY"),
    ("Beijing", "Beijing", "China", "Asia", "CNY"),
    ("Shenzhen", "Guangdong", "China", "Asia", "CNY"),
    ("Guangzhou", "Guangdong", "China", "Asia", "CNY"),
    ("Chengdu", "Sichuan", "China", "Asia", "CNY"),
    ("Tokyo", "Tokyo", "Japan", "Asia", "JPY"),
    ("Osaka", "Osaka", "Japan", "Asia", "JPY"),
    ("Yokohama", "Kanagawa", "Japan", "Asia", "JPY"),
    ("Seoul", "Seoul", "South Korea", "Asia", "KRW"),
    ("Busan", "Busan", "South Korea", "Asia", "KRW"),
    ("Singapore", "Singapore", "Singapore", "Asia", "SGD"),
    ("Dubai", "Dubai", "UAE", "Middle East", "AED"),
    ("Abu Dhabi", "Abu Dhabi", "UAE", "Middle East", "AED"),
    ("Johannesburg", "Gauteng", "South Africa", "Africa", "ZAR"),
    ("Cape Town", "Western Cape", "South Africa", "Africa", "ZAR"),
    ("Durban", "KwaZulu-Natal", "South Africa", "Africa", "ZAR"),
    ("Sydney", "New South Wales", "Australia", "Oceania", "AUD"),
    ("Melbourne", "Victoria", "Australia", "Oceania", "AUD"),
    ("Brisbane", "Queensland", "Australia", "Oceania", "AUD"),
    ("Perth", "Western Australia", "Australia", "Oceania", "AUD"),
    ("Adelaide", "South Australia", "Australia", "Oceania", "AUD"),
    ("São Paulo", "São Paulo", "Brazil", "South America", "BRL"),
    ("Rio de Janeiro", "Rio de Janeiro", "Brazil", "South America", "BRL"),
    ("Brasília", "Federal District", "Brazil", "South America", "BRL"),
    ("Mexico City", "Mexico City", "Mexico", "North America", "MXN"),
    ("Guadalajara", "Jalisco", "Mexico", "North America", "MXN"),
    ("Monterrey", "Nuevo León", "Mexico", "North America", "MXN"),
]



# =============================================================
# INTERNALS
# =============================================================

def _load_master(master_path: Path | None = None) -> pd.DataFrame:
    """Load the geography master file.

    Falls back to built-in FALLBACK_ROWS if the file is missing.
    """
    if master_path is None:
        master_path = _DEFAULT_MASTER_PATH

    if master_path.exists():
        df = pd.read_parquet(master_path)
        # Map CurrencyCode → ISOCode for backward compatibility
        # (prefer existing ISOCode if both columns are present)
        if "CurrencyCode" in df.columns and "ISOCode" not in df.columns:
            df = df.rename(columns={"CurrencyCode": "ISOCode"})
        debug(f"Geography master loaded: {master_path.name} ({len(df)} cities)")
        return df

    warn(
        f"Geography master not found at {master_path}; "
        f"using built-in fallback ({len(FALLBACK_ROWS)} cities). "
        f"Run: python scripts/build_geography_master.py to create it."
    )
    return pd.DataFrame(
        FALLBACK_ROWS,
        columns=["City", "State", "Country", "Continent", "ISOCode"],
    )


def _master_signature(master_path: Path | None = None) -> str:
    """Content-aware hash of the master file (or fallback rows)."""
    if master_path is None:
        master_path = _DEFAULT_MASTER_PATH

    if master_path.exists():
        raw = master_path.read_bytes()
    else:
        raw = repr(FALLBACK_ROWS).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_cfg(cfg: Dict) -> Dict:
    if not isinstance(cfg, Mapping):
        raise TypeError("cfg must be a dict")

    geo = getattr(cfg, "geography", None)
    if not isinstance(geo, Mapping):
        raise DimensionError("Missing required config section: 'geography'")

    er = getattr(cfg, "exchange_rates", None)
    if not isinstance(er, Mapping):
        raise DimensionError("Missing required config section: 'exchange_rates'")

    currencies = list(er.from_currencies or []) + list(er.to_currencies or [])
    if not currencies:
        raise DimensionError("exchange_rates.from_currencies/to_currencies must contain at least one currency code")

    return geo


# =============================================================
# GENERATOR
# =============================================================

def build_dim_geography(
    cfg: Dict,
    *,
    _geo_cfg: Dict | None = None,
    master_path: Path | None = None,
) -> pd.DataFrame:
    """Build the geography dimension from the master file.

    Loads the master parquet, filters to currencies listed in
    ``exchange_rates.currencies``, and assigns sequential GeographyKey.
    New columns (Latitude, Longitude, Timezone, Population) are included
    when present in the master file.
    """
    if _geo_cfg is None:
        _validate_cfg(cfg)

    er = cfg.exchange_rates
    allowed_iso = set(map(str, list(er.from_currencies or []) + list(er.to_currencies or [])))
    # Always include base currency (USD) — same convention as currency dimension
    base = str(getattr(cfg.exchange_rates, "base_currency", "USD")).upper()
    allowed_iso.add(base)

    df = _load_master(master_path)

    # Ensure ISOCode column exists
    if "ISOCode" not in df.columns:
        raise DimensionError(
            "Geography master file missing ISOCode or CurrencyCode column."
        )

    available_codes = sorted(df["ISOCode"].unique())
    df = df[df["ISOCode"].isin(allowed_iso)].reset_index(drop=True)
    if df.empty:
        raise DimensionError(
            f"No geography rows remain after filtering by allowed currencies: {sorted(allowed_iso)}. "
            f"Ensure exchange_rates.from_currencies/to_currencies includes at least one of: {available_codes}"
        )

    # Reject currencies configured but not covered by any geography row
    covered_iso = set(df["ISOCode"].unique())
    uncovered = sorted(allowed_iso - covered_iso)
    if uncovered:
        warn(
            f"exchange_rates currencies contain codes with no geography coverage: {uncovered}. "
            f"These currencies will have no stores or customers assigned."
        )

    df.insert(0, "GeographyKey", np.arange(1, len(df) + 1, dtype=np.int32))

    # Ensure all output columns exist (fill missing enrichment columns with defaults)
    if "Latitude" not in df.columns:
        df["Latitude"] = np.float64(0.0)
    if "Longitude" not in df.columns:
        df["Longitude"] = np.float64(0.0)
    if "Timezone" not in df.columns:
        df["Timezone"] = ""
    if "Population" not in df.columns:
        df["Population"] = np.int64(0)

    return df


# =============================================================
# CONFIG NORMALIZER  (registered in engine/config/config.py)
# =============================================================

def normalize_geography_config(geo_cfg: Dict) -> Dict:
    """Validate and coerce geography config at load time."""
    geo_cfg = dict(geo_cfg)

    # Warn about legacy keys that no longer apply
    for legacy_key in ("target_rows", "sampling", "country_weights"):
        if legacy_key in geo_cfg:
            warn(
                f"geography.{legacy_key} is no longer used. "
                f"Geography is loaded from a master file. "
                f"Remove this key from config.yaml to silence this warning."
            )

    # override sub-block (kept for seed/dates compatibility)
    override = geo_cfg.get("override") or {}
    if not isinstance(override, Mapping):
        raise ConfigError("geography.override must be a mapping")
    override.setdefault("seed", None)
    override.setdefault("dates", {})
    if override["seed"] is not None:
        override["seed"] = int(override["seed"])
    if not isinstance(override["dates"], Mapping):
        raise ConfigError("geography.override.dates must be a mapping")
    geo_cfg["override"] = override

    return geo_cfg


# =============================================================
# PIPELINE WRAPPER
# =============================================================

def run_geography(cfg: Dict, parquet_folder: Path) -> None:
    """Pipeline wrapper: version check → build → write parquet → save version."""
    geo_cfg = _validate_cfg(cfg)
    out_path = parquet_folder / "geography.parquet"

    version_cfg = {
        "schema_version": 2,  # v2: master-file based with enriched columns
        "exchange_rates": {
            "from_currencies": list(map(str, cfg.exchange_rates.from_currencies or [])),
            "to_currencies": list(map(str, cfg.exchange_rates.to_currencies or [])),
        },
        "_master_sig": _master_signature(),
    }

    if not should_regenerate("geography", version_cfg, out_path):
        skip("Geography up-to-date")
        return

    with stage("Generating Geography"):
        df = build_dim_geography(cfg, _geo_cfg=geo_cfg)
        # Reorder columns to match output schema
        cols = [c for c in OUTPUT_COLS if c in df.columns]
        df = df[cols]
        df.to_parquet(out_path, index=False)

    save_version("geography", version_cfg, out_path)
    debug(f"Geography dimension written: {out_path.name}  ({len(df)} rows)")
