from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.utils.config_helpers import as_dict as _as_dict, int_or as _int_or, bool_or as _bool_or
from src.utils.config_precedence import resolve_seed
from src.utils.logging_utils import debug, skip, stage
from src.versioning.version_store import should_regenerate, save_version


def _signature(df: pd.DataFrame) -> Dict[str, Any]:
    """Lightweight signature so changes in generation params trigger regeneration."""
    if df.empty:
        return {"rows": 0, "min_key": None, "max_key": None}
    keys = df["SupplierKey"].to_numpy()
    return {"rows": int(len(df)), "min_key": int(keys.min()), "max_key": int(keys.max())}


# ---------------------------------------------------------
# Generator
# ---------------------------------------------------------

_ADJ = np.array(
    [
        "Apex", "Nimbus", "Summit", "Vertex", "Prime", "Blue", "Green", "Silver", "Golden",
        "Urban", "Pacific", "Atlas", "Pioneer", "Everest", "Nova", "Orchid", "Cedar", "Willow",
        "Metro", "Harbor", "Cobalt", "Aurora",
    ],
    dtype=object,
)

_NOUN = np.array(
    [
        "Trading", "Supplies", "Distributors", "Industries", "Logistics", "Manufacturing",
        "Wholesale", "Imports", "Exports", "Partners", "Holdings", "Enterprises", "Resources",
        "Brands", "Retail Solutions", "Supply Co", "Procurement",
    ],
    dtype=object,
)

_SECTOR = np.array(
    [
        "Foods", "Beverages", "Home", "Electronics", "Apparel", "Beauty", "Pharma",
        "Stationery", "Toys", "Sports", "Furniture", "Kitchen", "Garden", "Personal Care",
    ],
    dtype=object,
)

_SUFFIX = np.array(
    ["Ltd", "Pvt Ltd", "Inc", "LLC", "GmbH", "S.A.", "Co.", "Group"],
    dtype=object,
)

_SUPPLIER_TYPES = np.array(["Manufacturer", "Distributor", "PrivateLabel"], dtype=object)
_SUPPLIER_TYPES_P = np.array([0.55, 0.35, 0.10], dtype=float)

_DEFAULT_COUNTRIES = np.array(
    ["India", "United States", "China", "Germany", "United Kingdom", "Japan", "Vietnam", "Mexico", "Canada"],
    dtype=object,
)


def generate_suppliers_table(
    *,
    num_suppliers: int = 250,
    start_key: int = 1,
    seed: int = 42,
    include_country: bool = True,
    include_reliability: bool = True,
    countries: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """
    Generate synthetic supplier dimension table.

    Output columns (base):
      SupplierKey (int32), SupplierName, SupplierType

    Optional columns:
      Country, ReliabilityScore
    """
    num_suppliers = _int_or(num_suppliers, 250)
    start_key = _int_or(start_key, 1)
    if num_suppliers <= 0:
        raise DimensionError(f"num_suppliers must be > 0, got {num_suppliers}")
    if start_key <= 0:
        raise DimensionError(f"start_key must be > 0, got {start_key}")

    rng = np.random.default_rng(int(seed))

    supplier_key = np.arange(start_key, start_key + num_suppliers, dtype=np.int32)
    supplier_type = rng.choice(_SUPPLIER_TYPES, size=num_suppliers, p=_SUPPLIER_TYPES_P)

    # Deterministic-ish name composition using RNG
    adj = rng.choice(_ADJ, size=num_suppliers, replace=True)
    noun = rng.choice(_NOUN, size=num_suppliers, replace=True)
    sector = rng.choice(_SECTOR, size=num_suppliers, replace=True)
    suffix = rng.choice(_SUFFIX, size=num_suppliers, replace=True)

    # Slightly bias private label naming to look distinct
    is_pl = (supplier_type == "PrivateLabel")
    adj_pl = np.where(is_pl, "Contoso", adj)
    noun_pl = np.where(is_pl, "Private Label", noun)

    supplier_name = (
        pd.Series(adj_pl).astype(str)
        + " "
        + pd.Series(sector).astype(str)
        + " "
        + pd.Series(noun_pl).astype(str)
        + " "
        + pd.Series(suffix).astype(str)
    )

    df = pd.DataFrame(
        {
            "SupplierKey": supplier_key,
            "SupplierName": supplier_name,
            "SupplierType": supplier_type,
        }
    )

    if include_country:
        country_pool = np.array(list(countries), dtype=object) if countries else _DEFAULT_COUNTRIES
        df["Country"] = rng.choice(country_pool, size=num_suppliers, replace=True)

    if include_reliability:
        # ReliabilityScore: mostly high, with a tail for "risky" suppliers
        base = rng.beta(a=8.0, b=2.0, size=num_suppliers)  # skewed toward 1.0
        # Add a few lower outliers
        outlier_mask = rng.random(num_suppliers) < 0.03
        base[outlier_mask] = rng.beta(a=2.0, b=8.0, size=int(outlier_mask.sum()))
        df["ReliabilityScore"] = np.round(base.astype(np.float64), 3)

    # Enforce dtypes
    df["SupplierKey"] = df["SupplierKey"].astype(np.int32)
    df["SupplierName"] = df["SupplierName"].astype(str)
    df["SupplierType"] = df["SupplierType"].astype(str)

    return df


# ---------------------------------------------------------
# Pipeline entrypoint
# ---------------------------------------------------------

def run_suppliers(cfg: Dict[str, Any], parquet_folder: Path) -> None:
    """
    Pipeline wrapper for suppliers dimension generation.
    - Fast skip (version check BEFORE generating dataframe)
    - Deterministic seed
    - Parquet write
    """
    cfg = cfg or {}
    sup_cfg = _as_dict(getattr(cfg, "suppliers", None))

    parquet_folder = Path(parquet_folder)
    parquet_folder.mkdir(parents=True, exist_ok=True)

    out_path = parquet_folder / "suppliers.parquet"
    # Resolve deterministic params up-front
    num_suppliers = _int_or(sup_cfg.get("num_suppliers"), 250)
    start_key = _int_or(sup_cfg.get("start_key"), 1)
    seed = resolve_seed(cfg, sup_cfg, fallback=42)
    include_country = _bool_or(sup_cfg.get("include_country"), True)
    include_reliability = _bool_or(sup_cfg.get("include_reliability"), True)
    countries = sup_cfg.get("countries") if isinstance(sup_cfg.get("countries"), list) else None

    # Optional parquet settings
    compression = sup_cfg.get("parquet_compression", "snappy")
    compression_level = sup_cfg.get("parquet_compression_level", None)

    # Validate early (avoid weird sig math)
    if num_suppliers <= 0:
        raise DimensionError(f"num_suppliers must be > 0, got {num_suppliers}")
    if start_key <= 0:
        raise DimensionError(f"start_key must be > 0, got {start_key}")

    # Build version cfg BEFORE generation so we can skip cheaply.
    version_cfg = dict(sup_cfg)

    # Make seed explicit even if supplied via override/defaults
    version_cfg["seed"] = int(seed)

    version_cfg["schema_version"] = 1
    version_cfg["_sig"] = {
        "rows": int(num_suppliers),
        "min_key": int(start_key),
        "max_key": int(start_key + num_suppliers - 1),
    }
    version_cfg["_flags"] = {
        "include_country": bool(include_country),
        "include_reliability": bool(include_reliability),
    }
    if countries is not None:
        version_cfg["_countries"] = [str(x) for x in countries]

    if not should_regenerate("suppliers", version_cfg, out_path):
        skip("Suppliers up-to-date")
        return

    with stage("Generating Suppliers"):
        df = generate_suppliers_table(
            num_suppliers=num_suppliers,
            start_key=start_key,
            seed=seed,
            include_country=include_country,
            include_reliability=include_reliability,
            countries=countries,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(
        out_path,
        index=False,
        compression=str(compression),
        compression_level=(int(compression_level) if compression_level is not None else None),
    )

    save_version("suppliers", version_cfg, out_path)
    debug(f"Suppliers dimension written: {out_path.name}")

