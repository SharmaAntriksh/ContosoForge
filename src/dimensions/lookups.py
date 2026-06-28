from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.exceptions import DimensionError
from src.utils.config_helpers import as_dict as _as_dict, int_or as _int_or
from src.utils.logging_utils import info, skip, stage
from src.versioning.version_store import should_regenerate, save_version


# =========================================================
# Internals
# =========================================================

def _strip_force(cfg_section: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of the config section (legacy helper kept for callers)."""
    return dict(cfg_section)


def _write_parquet(df: pd.DataFrame, out_path: Path, compression: str = "snappy") -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Engine default is fine; project uses parquet widely already.
    df.to_parquet(out_path, index=False, compression=compression)


def _build_version_cfg(cfg: Dict[str, Any], dim_key: str, df: pd.DataFrame, schema_version: int = 1) -> Dict[str, Any]:
    dim_cfg = _as_dict(getattr(cfg, dim_key, None))
    base = _strip_force(dim_cfg)

    # Keep version cfg small + stable; include row count and schema version.
    return {
        **base,
        "schema_version": int(schema_version),
        "_rows": int(len(df)),
        "_cols": list(df.columns),
    }


def _maybe_override_rows(dim_cfg: Dict[str, Any], required_cols: Sequence[str]) -> Optional[pd.DataFrame]:
    """
    Optional override:
      <dim_key>:
        rows:
          - {<col>: <val>, ...}
          - ...
    """
    rows = dim_cfg.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    df = pd.DataFrame(rows)

    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise DimensionError(f"Override rows missing required columns: {missing}")
    return df[required_cols].copy()


def _run_lookup_dim(
    *,
    cfg: Dict[str, Any],
    dim_key: str,
    out_name: str,
    build_df: Callable[[Dict[str, Any]], pd.DataFrame],
    parquet_folder: Path,
) -> None:
    dim_cfg = _as_dict(getattr(cfg, dim_key, None))
    parquet_folder = Path(parquet_folder)
    parquet_folder.mkdir(parents=True, exist_ok=True)
    out_path = parquet_folder / out_name

    compression = str(dim_cfg.get("parquet_compression", "snappy"))

    # Build df silently first (very cheap for lookups); avoids noisy stage logs on skip.
    df = build_df(dim_cfg)
    version_cfg = _build_version_cfg(cfg, dim_key, df)

    display_name = dim_key.replace("_", " ").title()

    if not should_regenerate(dim_key, version_cfg, out_path):
        skip(f"{display_name} up-to-date")
        return

    # Only log "Generating ..." when we actually write output
    with stage(f"Generating {display_name}"):
        _write_parquet(df, out_path, compression=compression)

    save_version(dim_key, version_cfg, out_path)
    info(f"{display_name} written: {out_path.name}")

# =========================================================
# Default row sets
# =========================================================

def _df_sales_channels(dim_cfg: Dict[str, Any]) -> pd.DataFrame:
    """
    Sales channel lookup.

    Backward-compatible override:
      sales_channels:
        rows:
          - {ChannelKey: 1, Channel: Store, ChannelGroup: Physical}
          - ...

    If override rows are provided, only the 3 base columns are required; all new columns are derived/fill-defaulted.
    """

    base_required = ["ChannelKey", "Channel", "ChannelGroup"]

    # Output column order — must match STATIC_SCHEMAS["Channels"] for BULK INSERT
    cols = [
        "ChannelKey",
        "Channel",
        "ChannelDescription",
        "ChannelGroup",
        "ChannelGroupDescription",
        "ChannelCode",
        "SortOrder",
        "IsDigital",
        "IsPhysical",
        "IsThirdParty",
        "IsB2B",
        "IsAssisted",
        "IsOwnedChannel",
        "Is24x7",
        "OpenTime",
        "CloseTime",
        "CommissionRate",
        "TypicalFulfillmentDays",
    ]

    _CHANNEL_DESCRIPTIONS: Dict[str, str] = {
        "Unknown":          "Channel could not be determined",
        "Store":            "Customer purchases in-person at a physical retail location",
        "Online":           "Customer orders through the company website",
        "Marketplace":      "Orders placed on third-party platforms (e.g. Amazon, eBay)",
        "B2B":              "Business-to-business orders via account managers or portals",
        "CallCenter":       "Customer places an order by phone with a human agent",
        "Web":              "Direct web storefront (distinct branding from main online store)",
        "MobileApp":        "Orders placed through the company mobile application",
        "SocialCommerce":   "Purchases originating from social media platforms",
        "PartnerReseller":  "Orders through authorized third-party resellers or distributors",
        "Kiosk":            "Self-service terminal in a physical location",
    }

    _GROUP_DESCRIPTIONS: Dict[str, str] = {
        "NA":       "Not applicable or unknown",
        "Physical": "In-person transactions at a physical location",
        "Digital":  "Self-service online or app-based transactions",
        "Business": "Business-to-business or partner transactions",
        "Assisted": "Agent-assisted transactions via phone or chat",
    }

    _COMMISSION_RATES: Dict[str, float] = {
        "Unknown":          0.00,
        "Store":            0.00,
        "Online":           0.00,
        "Marketplace":      0.15,
        "B2B":              0.00,
        "CallCenter":       0.00,
        "Web":              0.00,
        "MobileApp":        0.00,
        "SocialCommerce":   0.12,
        "PartnerReseller":  0.10,
        "Kiosk":            0.00,
    }

    _FULFILLMENT_DAYS: Dict[str, int] = {
        "Unknown":          0,
        "Store":            0,
        "Online":           3,
        "Marketplace":      5,
        "B2B":              7,
        "CallCenter":       3,
        "Web":              3,
        "MobileApp":        3,
        "SocialCommerce":   5,
        "PartnerReseller":  7,
        "Kiosk":            0,
    }

    def _minutes_to_time(m: int) -> object:
        """Convert minutes-since-midnight to 'HH:MM' string, or None for 24x7/unknown."""
        if m < 0:
            return None
        return f"{m // 60:02d}:{m % 60:02d}"

    def _normalize_code(name: str) -> str:
        s = str(name).strip().upper()
        s = "".join(ch if ch.isalnum() else "_" for ch in s)
        while "__" in s:
            s = s.replace("__", "_")
        return s.strip("_") or "UNKNOWN"

    def _derive_flags(df: pd.DataFrame) -> pd.DataFrame:
        df["ChannelKey"] = df["ChannelKey"].astype(np.int32)

        # Defaults
        if "ChannelDescription" not in df.columns:
            df["ChannelDescription"] = df["Channel"].map(
                lambda n: _CHANNEL_DESCRIPTIONS.get(str(n).strip(), "")
            )

        if "ChannelGroupDescription" not in df.columns:
            df["ChannelGroupDescription"] = df["ChannelGroup"].map(
                lambda g: _GROUP_DESCRIPTIONS.get(str(g).strip(), "")
            )

        if "ChannelCode" not in df.columns:
            df["ChannelCode"] = df["Channel"].map(_normalize_code)

        if "SortOrder" not in df.columns:
            df["SortOrder"] = df["ChannelKey"].astype(np.int32)

        # Derive based on ChannelGroup + name heuristics
        grp = df["ChannelGroup"].astype(str).str.strip().str.lower()
        nm = df["Channel"].astype(str).str.strip().str.lower()

        if "IsDigital" not in df.columns:
            df["IsDigital"] = (grp == "digital")
        if "IsPhysical" not in df.columns:
            df["IsPhysical"] = (grp == "physical")
        if "IsB2B" not in df.columns:
            df["IsB2B"] = (grp == "business") | nm.str.contains("b2b", na=False)
        if "IsAssisted" not in df.columns:
            df["IsAssisted"] = (grp == "assisted") | nm.str.contains("call|phone|agent", na=False)

        if "IsThirdParty" not in df.columns:
            df["IsThirdParty"] = nm.str.contains("^marketplace$|reseller|partner", na=False)

        if "IsOwnedChannel" not in df.columns:
            df["IsOwnedChannel"] = ~df["IsThirdParty"].astype(bool)

        if "Is24x7" not in df.columns:
            df["Is24x7"] = df["IsDigital"].astype(bool)

        # OpenTime / CloseTime as "HH:MM" strings (None for 24x7/unknown)
        if "OpenTime" not in df.columns:
            df["OpenTime"] = None
        if "CloseTime" not in df.columns:
            df["CloseTime"] = None

        if "CommissionRate" not in df.columns:
            df["CommissionRate"] = df["Channel"].map(
                lambda n: _COMMISSION_RATES.get(str(n).strip(), 0.00)
            )

        if "TypicalFulfillmentDays" not in df.columns:
            df["TypicalFulfillmentDays"] = df["Channel"].map(
                lambda n: _FULFILLMENT_DAYS.get(str(n).strip(), 0)
            )

        # Hard-cast
        df["SortOrder"] = df["SortOrder"].astype(np.int32)
        for c in ["IsDigital", "IsPhysical", "IsThirdParty", "IsB2B", "IsAssisted", "IsOwnedChannel", "Is24x7"]:
            df[c] = df[c].astype(bool)
        df["TypicalFulfillmentDays"] = df["TypicalFulfillmentDays"].astype(np.int32)

        return df

    # ---- Override support (base_required only) ----
    override_rows = dim_cfg.get("rows")
    if isinstance(override_rows, list) and override_rows:
        df = pd.DataFrame(override_rows)
        missing = [c for c in base_required if c not in df.columns]
        if missing:
            raise DimensionError(f"Override rows missing required columns: {missing}")

        df = _derive_flags(df)

        # Ensure all expected cols exist (fill if override omitted)
        for c in cols:
            if c not in df.columns:
                df[c] = None

        return df[cols].copy()

    include_unknown = bool(dim_cfg.get("include_unknown", True))
    include_extended = bool(dim_cfg.get("include_extended", True))

    rows = []
    _d = _CHANNEL_DESCRIPTIONS
    _g = _GROUP_DESCRIPTIONS
    _cr = _COMMISSION_RATES
    _fd = _FULFILLMENT_DAYS
    _mt = _minutes_to_time

    if include_unknown:
        rows.append(
            dict(
                ChannelKey=0,
                Channel="Unknown",
                ChannelDescription=_d["Unknown"],
                ChannelGroup="NA",
                ChannelGroupDescription=_g["NA"],
                ChannelCode="UNKNOWN",
                SortOrder=0,
                IsDigital=False,
                IsPhysical=False,
                IsThirdParty=False,
                IsB2B=False,
                IsAssisted=False,
                IsOwnedChannel=False,
                Is24x7=False,
                OpenTime=None,
                CloseTime=None,
                CommissionRate=_cr["Unknown"],
                TypicalFulfillmentDays=_fd["Unknown"],
            )
        )

    # Core (keep your existing 1..5 stable)
    rows += [
        dict(ChannelKey=1, Channel="Store",       ChannelDescription=_d["Store"],
             ChannelGroup="Physical", ChannelGroupDescription=_g["Physical"], ChannelCode="STORE",
             SortOrder=1, IsDigital=False, IsPhysical=True,  IsThirdParty=False, IsB2B=False, IsAssisted=False,
             IsOwnedChannel=True, Is24x7=False, OpenTime=_mt(9*60),  CloseTime=_mt(21*60),
             CommissionRate=_cr["Store"], TypicalFulfillmentDays=_fd["Store"]),
        dict(ChannelKey=2, Channel="Online",      ChannelDescription=_d["Online"],
             ChannelGroup="Digital",  ChannelGroupDescription=_g["Digital"],  ChannelCode="ONLINE",
             SortOrder=2, IsDigital=True,  IsPhysical=False, IsThirdParty=False, IsB2B=False, IsAssisted=False,
             IsOwnedChannel=True, Is24x7=True,  OpenTime=None, CloseTime=None,
             CommissionRate=_cr["Online"], TypicalFulfillmentDays=_fd["Online"]),
        dict(ChannelKey=3, Channel="Marketplace", ChannelDescription=_d["Marketplace"],
             ChannelGroup="Digital",  ChannelGroupDescription=_g["Digital"],  ChannelCode="MARKETPLACE",
             SortOrder=3, IsDigital=True,  IsPhysical=False, IsThirdParty=True,  IsB2B=False, IsAssisted=False,
             IsOwnedChannel=False, Is24x7=True,  OpenTime=None, CloseTime=None,
             CommissionRate=_cr["Marketplace"], TypicalFulfillmentDays=_fd["Marketplace"]),
        dict(ChannelKey=4, Channel="B2B",         ChannelDescription=_d["B2B"],
             ChannelGroup="Business", ChannelGroupDescription=_g["Business"], ChannelCode="B2B",
             SortOrder=4, IsDigital=False, IsPhysical=False, IsThirdParty=False, IsB2B=True,  IsAssisted=False,
             IsOwnedChannel=True, Is24x7=False, OpenTime=_mt(8*60),  CloseTime=_mt(18*60),
             CommissionRate=_cr["B2B"], TypicalFulfillmentDays=_fd["B2B"]),
        dict(ChannelKey=5, Channel="CallCenter",  ChannelDescription=_d["CallCenter"],
             ChannelGroup="Assisted", ChannelGroupDescription=_g["Assisted"], ChannelCode="CALLCENTER",
             SortOrder=5, IsDigital=False, IsPhysical=False, IsThirdParty=False, IsB2B=False, IsAssisted=True,
             IsOwnedChannel=True, Is24x7=False, OpenTime=_mt(8*60),  CloseTime=_mt(20*60),
             CommissionRate=_cr["CallCenter"], TypicalFulfillmentDays=_fd["CallCenter"]),
    ]

    if include_extended:
        rows += [
            dict(ChannelKey=6, Channel="Web",          ChannelDescription=_d["Web"],
                 ChannelGroup="Digital",  ChannelGroupDescription=_g["Digital"],  ChannelCode="WEB",
                 SortOrder=6, IsDigital=True,  IsPhysical=False, IsThirdParty=False, IsB2B=False, IsAssisted=False,
                 IsOwnedChannel=True, Is24x7=True,  OpenTime=None, CloseTime=None,
                 CommissionRate=_cr["Web"], TypicalFulfillmentDays=_fd["Web"]),
            dict(ChannelKey=7, Channel="MobileApp",    ChannelDescription=_d["MobileApp"],
                 ChannelGroup="Digital",  ChannelGroupDescription=_g["Digital"],  ChannelCode="APP",
                 SortOrder=7, IsDigital=True,  IsPhysical=False, IsThirdParty=False, IsB2B=False, IsAssisted=False,
                 IsOwnedChannel=True, Is24x7=True,  OpenTime=None, CloseTime=None,
                 CommissionRate=_cr["MobileApp"], TypicalFulfillmentDays=_fd["MobileApp"]),
            dict(ChannelKey=8, Channel="SocialCommerce", ChannelDescription=_d["SocialCommerce"],
                 ChannelGroup="Digital", ChannelGroupDescription=_g["Digital"],  ChannelCode="SOCIAL",
                 SortOrder=8, IsDigital=True,  IsPhysical=False, IsThirdParty=True,  IsB2B=False, IsAssisted=False,
                 IsOwnedChannel=False, Is24x7=True,  OpenTime=None, CloseTime=None,
                 CommissionRate=_cr["SocialCommerce"], TypicalFulfillmentDays=_fd["SocialCommerce"]),
            dict(ChannelKey=9, Channel="PartnerReseller", ChannelDescription=_d["PartnerReseller"],
                 ChannelGroup="Business", ChannelGroupDescription=_g["Business"], ChannelCode="PARTNER",
                 SortOrder=9, IsDigital=False, IsPhysical=False, IsThirdParty=True,  IsB2B=True,  IsAssisted=False,
                 IsOwnedChannel=False, Is24x7=False, OpenTime=_mt(8*60), CloseTime=_mt(18*60),
                 CommissionRate=_cr["PartnerReseller"], TypicalFulfillmentDays=_fd["PartnerReseller"]),
            dict(ChannelKey=10, Channel="Kiosk",       ChannelDescription=_d["Kiosk"],
                 ChannelGroup="Physical", ChannelGroupDescription=_g["Physical"], ChannelCode="KIOSK",
                 SortOrder=10, IsDigital=False, IsPhysical=True, IsThirdParty=False, IsB2B=False, IsAssisted=False,
                 IsOwnedChannel=True, Is24x7=False, OpenTime=_mt(10*60), CloseTime=_mt(20*60),
                 CommissionRate=_cr["Kiosk"], TypicalFulfillmentDays=_fd["Kiosk"]),
        ]

    df = pd.DataFrame(rows, columns=cols)
    return _derive_flags(df)[cols].copy()


def _df_loyalty_tiers(dim_cfg: Dict[str, Any]) -> pd.DataFrame:
    required = ["LoyaltyTierKey", "LoyaltyTier", "TierRank", "PointsMultiplier"]
    override = _maybe_override_rows(dim_cfg, required_cols=required)
    if override is not None:
        override["LoyaltyTierKey"] = override["LoyaltyTierKey"].astype(np.int32)
        override["TierRank"] = override["TierRank"].astype(np.int32)
        override["PointsMultiplier"] = override["PointsMultiplier"].astype(float)
        return override

    rows = [
        (0, "None", 0, 1.00),
        (1, "Silver", 1, 1.05),
        (2, "Gold", 2, 1.10),
        (3, "Platinum", 3, 1.20),
    ]
    df = pd.DataFrame(rows, columns=required)
    df["LoyaltyTierKey"] = df["LoyaltyTierKey"].astype(np.int32)
    df["TierRank"] = df["TierRank"].astype(np.int32)
    df["PointsMultiplier"] = df["PointsMultiplier"].astype(float)
    return df


def _df_customer_acquisition_channels(dim_cfg: Dict[str, Any]) -> pd.DataFrame:
    required = ["CustomerAcquisitionChannelKey", "AcquisitionChannel", "ChannelGroup"]
    override = _maybe_override_rows(dim_cfg, required_cols=required)
    if override is not None:
        override["CustomerAcquisitionChannelKey"] = override["CustomerAcquisitionChannelKey"].astype(np.int32)
        return override

    rows = [
        (1, "Organic", "Owned/Earned"),
        (2, "PaidSearch", "Paid"),
        (3, "Social", "Paid"),
        (4, "Referral", "Owned/Earned"),
        (5, "Email", "Owned/Earned"),
        (6, "Marketplace", "Partner"),
        (7, "Affiliates", "Partner"),
    ]
    df = pd.DataFrame(rows, columns=required)
    df["CustomerAcquisitionChannelKey"] = df["CustomerAcquisitionChannelKey"].astype(np.int32)
    return df



# =========================================================
# Pipeline entrypoints (run_* wrappers)
# =========================================================

def run_sales_channels(cfg: Dict[str, Any], parquet_folder: Path) -> None:
    _run_lookup_dim(cfg=cfg, dim_key="sales_channels", out_name="channels.parquet",
                    build_df=_df_sales_channels, parquet_folder=parquet_folder)



def run_loyalty_tiers(cfg: Dict[str, Any], parquet_folder: Path) -> None:
    _run_lookup_dim(cfg=cfg, dim_key="loyalty_tiers", out_name="loyalty_tiers.parquet",
                    build_df=_df_loyalty_tiers, parquet_folder=parquet_folder)


def run_customer_acquisition_channels(cfg: Dict[str, Any], parquet_folder: Path) -> None:
    _run_lookup_dim(cfg=cfg, dim_key="customer_acquisition_channels", out_name="customer_acquisition_channels.parquet",
                    build_df=_df_customer_acquisition_channels, parquet_folder=parquet_folder)

