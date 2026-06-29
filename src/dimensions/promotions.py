from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import warnings

from src.exceptions import DimensionError
from src.utils.logging_utils import info, skip, stage, warn
from src.utils.config_helpers import as_dict
from src.utils.config_precedence import resolve_seed
from src.versioning import should_regenerate, save_version


# ---------------------------------------------------------
#  CONSTANTS (imported from src.defaults)
# ---------------------------------------------------------

from src.defaults import (
    PROMOTION_PROMO_TYPES as PROMO_TYPES,
    PROMOTION_CATEGORIES as CATEGORIES,
    PROMOTION_HOLIDAYS as HOLIDAYS,
    PROMOTION_SEASON_WINDOWS as SEASON_WINDOWS,
    PROMOTION_DEFAULT_NUM_SEASONAL,
    PROMOTION_DEFAULT_NUM_CLEARANCE,
    PROMOTION_DEFAULT_NUM_LIMITED,
    PROMOTION_DEFAULT_NUM_FLASH,
    PROMOTION_DEFAULT_NUM_VOLUME,
    PROMOTION_DEFAULT_NUM_LOYALTY,
    PROMOTION_DEFAULT_NUM_BUNDLE,
    PROMOTION_DEFAULT_NUM_NEW_CUSTOMER,
)


def _bucket_count(value, default: int) -> int:
    """Resolve a configured promotion-bucket count.

    Unlike ``value or default``, this honors an explicit ``0`` (disable the
    bucket) and only falls back to *default* when the value is unset (None).
    """
    return int(default) if value is None else int(value)


# ---------------------------------------------------------
#  PARQUET WRITER: FORCE DATE TYPES FOR POWER QUERY
# ---------------------------------------------------------

def _infer_datetime_cols(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]


def _write_parquet_with_date32(
    df: pd.DataFrame,
    out_path: Path,
    *,
    compression: str = "snappy",
    compression_level: Optional[int] = None,
    force_date32: bool = True,
) -> None:
    """Write Parquet with date32 columns. Delegates to the shared utility."""
    from src.utils.output_utils import write_parquet_with_date32 as _shared_writer
    _shared_writer(
        df,
        out_path,
        date_cols=_infer_datetime_cols(df) or None,
        compression=compression,
        compression_level=compression_level,
        force_date32=force_date32,
    )


# ---------------------------------------------------------
#  HELPERS
# ---------------------------------------------------------

def _mmdd(mmdd: str, year: int) -> pd.Timestamp:
    m, d = map(int, mmdd.split("-"))
    return pd.Timestamp(year=year, month=m, day=d)


def _valid_window(s: Optional[pd.Timestamp], e: Optional[pd.Timestamp]) -> bool:
    return s is not None and e is not None and s < e


def _build_year_windows(
    start: pd.Timestamp, end: pd.Timestamp
) -> Tuple[List[int], Dict[int, Tuple[pd.Timestamp, pd.Timestamp]]]:
    start = pd.to_datetime(start).normalize()
    end = pd.to_datetime(end).normalize()
    if end < start:
        raise DimensionError(f"Promotions: end < start ({end.date()} < {start.date()})")

    years = list(range(start.year, end.year + 1))
    windows: Dict[int, Tuple[pd.Timestamp, pd.Timestamp]] = {}
    for y in years:
        ys = max(pd.Timestamp(f"{y}-01-01"), start)
        ye = min(pd.Timestamp(f"{y}-12-31"), end)
        windows[y] = (ys, ye)
    return years, windows


def _clamp_to_year_window(
    dt: pd.Timestamp, year_windows: Dict[int, Tuple[pd.Timestamp, pd.Timestamp]]
) -> Optional[pd.Timestamp]:
    ws_we = year_windows.get(int(dt.year))
    if not ws_we:
        # Year outside the generated range: e.g. a holiday/season that wraps
        # into year+1 (New Year, Winter Event) past the last in-range year.
        # Clamp to the overall [start, end] span instead of dropping the row,
        # so the final year's wrapping promotions survive.
        gstart = min(w[0] for w in year_windows.values())
        gend = max(w[1] for w in year_windows.values())
        if dt < gstart:
            return gstart
        if dt > gend:
            return gend
        return dt
    ws, we = ws_we
    if dt < ws:
        return ws
    if dt > we:
        return we
    return dt


def _normalize_override_dates(promo_cfg: Dict) -> Dict:
    """
    Supports either:
      promotions.override: { dates: {start,end} }
    or:
      promotions.override: { start,end }
    """
    override = promo_cfg.override if hasattr(promo_cfg, "override") else ((promo_cfg or {}).get("override") if isinstance(promo_cfg, dict) else None)
    override = override or {}
    if isinstance(override, Mapping) and isinstance(override.get("dates"), Mapping):
        return override["dates"] or {}
    return override if isinstance(override, Mapping) else {}




def _discount(rng: np.random.Generator, lo: float, hi: float) -> float:
    return float(np.round(rng.uniform(lo, hi), 2))


def _category(rng: np.random.Generator) -> str:
    return str(rng.choice(CATEGORIES))


def _seasonal_start_date(rng: np.random.Generator, y: int, start_m: int, end_m: int) -> pd.Timestamp:
    # window wraps year
    if start_m <= end_m:
        m = int(rng.integers(start_m, end_m + 1))
        year_for_start = y
    else:
        months = list(range(start_m, 13)) + list(range(1, end_m + 1))
        m = int(rng.choice(months))
        year_for_start = y if m >= start_m else y + 1

    day = int(rng.integers(1, 25))
    return pd.Timestamp(year=year_for_start, month=m, day=day)


def _random_start_date(rng: np.random.Generator, y: int) -> pd.Timestamp:
    m = int(rng.integers(1, 13))
    d = int(rng.integers(1, 25))
    return pd.Timestamp(year=y, month=m, day=d)


# ---------------------------------------------------------
#  PROMOTION GENERATOR
# ---------------------------------------------------------

def generate_promotions_catalog(
    *,
    years: List[int],
    year_windows: Dict[int, Tuple[pd.Timestamp, pd.Timestamp]],
    num_seasonal: int = PROMOTION_DEFAULT_NUM_SEASONAL,
    num_clearance: int = PROMOTION_DEFAULT_NUM_CLEARANCE,
    num_limited: int = PROMOTION_DEFAULT_NUM_LIMITED,
    num_flash: int = PROMOTION_DEFAULT_NUM_FLASH,
    num_volume: int = PROMOTION_DEFAULT_NUM_VOLUME,
    num_loyalty: int = PROMOTION_DEFAULT_NUM_LOYALTY,
    num_bundle: int = PROMOTION_DEFAULT_NUM_BUNDLE,
    num_new_customer: int = PROMOTION_DEFAULT_NUM_NEW_CUSTOMER,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Returns columns:
      PromotionKey, PromotionLabel, PromotionName, PromotionDescription,
      DiscountPct, PromotionType, PromotionCategory, PromotionYear,
      PromotionSequence, StartDate, EndDate

    DiscountPct is a fraction in [0, 1) (e.g. 0.20 means 20% off), not a
    whole-number percentage.

    PromotionKey=1 is always the "No Discount" sentinel row.
    """
    if not years:
        raise DimensionError("Promotions: No years provided.")
    if not year_windows:
        raise DimensionError("Promotions: year_windows is empty.")

    rng = np.random.default_rng(int(seed))
    rows: List[Dict] = []
    requested = {
        "Seasonal": int(num_seasonal),
        "Clearance": int(num_clearance),
        "Limited": int(num_limited),
        "Flash": int(num_flash),
        "Volume": int(num_volume),
        "Loyalty": int(num_loyalty),
        "Bundle": int(num_bundle),
        "NewCustomer": int(num_new_customer),
    }
    generated = {k: 0 for k in requested}

    # Holidays (one per holiday per year — not configurable count)
    for y in years:
        for name, s_mmdd, e_mmdd, dmin, dmax in HOLIDAYS:
            s = _mmdd(s_mmdd, y)

            e_month = int(e_mmdd.split("-")[0])
            e_year = y + 1 if e_month < s.month else y
            e = _mmdd(e_mmdd, e_year)

            s = _clamp_to_year_window(s, year_windows)
            e = _clamp_to_year_window(e, year_windows)

            if not _valid_window(s, e):
                continue

            rows.append(
                {
                    "TypeGroup": "Holiday",
                    "SeasonType": name,
                    "Year": y,
                    "DiscountPct": _discount(rng, dmin, dmax),
                    "PromotionType": PROMO_TYPES["Holiday"],
                    "PromotionCategory": _category(rng),
                    "StartDate": pd.Timestamp(s),
                    "EndDate": pd.Timestamp(e),
                }
            )

    # Seasonal (10–60 day windows within season months)
    for _ in range(int(num_seasonal)):
        y = int(rng.choice(years))
        season_name = str(rng.choice(list(SEASON_WINDOWS.keys())))
        sm, em = SEASON_WINDOWS[season_name]

        start = _seasonal_start_date(rng, y, sm, em)
        end = start + timedelta(days=int(rng.integers(10, 60)))

        start = _clamp_to_year_window(start, year_windows)
        end = _clamp_to_year_window(end, year_windows)

        if not _valid_window(start, end):
            continue

        generated["Seasonal"] += 1
        rows.append(
            {
                "TypeGroup": "Seasonal",
                "SeasonType": season_name,
                "Year": y,
                "DiscountPct": _discount(rng, 0.05, 0.30),
                "PromotionType": PROMO_TYPES["Seasonal"],
                "PromotionCategory": _category(rng),
                "StartDate": pd.Timestamp(start),
                "EndDate": pd.Timestamp(end),
            }
        )

    # Clearance (3–25 day windows, steep discounts)
    for _ in range(int(num_clearance)):
        y = int(rng.choice(years))
        start = _random_start_date(rng, y)
        end = start + timedelta(days=int(rng.integers(3, 25)))

        start = _clamp_to_year_window(start, year_windows)
        end = _clamp_to_year_window(end, year_windows)

        if not _valid_window(start, end):
            continue

        generated["Clearance"] += 1
        rows.append(
            {
                "TypeGroup": "Clearance",
                "SeasonType": "Clearance",
                "Year": y,
                "DiscountPct": _discount(rng, 0.30, 0.70),
                "PromotionType": PROMO_TYPES["Clearance"],
                "PromotionCategory": _category(rng),
                "StartDate": pd.Timestamp(start),
                "EndDate": pd.Timestamp(end),
            }
        )

    # Limited (1–15 day windows, moderate discounts)
    for _ in range(int(num_limited)):
        y = int(rng.choice(years))
        start = _random_start_date(rng, y)
        end = start + timedelta(days=int(rng.integers(1, 15)))

        start = _clamp_to_year_window(start, year_windows)
        end = _clamp_to_year_window(end, year_windows)

        if not _valid_window(start, end):
            continue

        generated["Limited"] += 1
        rows.append(
            {
                "TypeGroup": "Limited",
                "SeasonType": "Limited Time",
                "Year": y,
                "DiscountPct": _discount(rng, 0.05, 0.35),
                "PromotionType": PROMO_TYPES["Limited"],
                "PromotionCategory": _category(rng),
                "StartDate": pd.Timestamp(start),
                "EndDate": pd.Timestamp(end),
            }
        )

    # Flash Sale (1–2 day windows, steep discounts)
    for _ in range(int(num_flash)):
        y = int(rng.choice(years))
        start = _random_start_date(rng, y)
        end = start + timedelta(days=int(rng.integers(1, 3)))

        start = _clamp_to_year_window(start, year_windows)
        end = _clamp_to_year_window(end, year_windows)

        if not _valid_window(start, end):
            continue

        generated["Flash"] += 1
        rows.append(
            {
                "TypeGroup": "Flash",
                "SeasonType": "Flash Sale",
                "Year": y,
                "DiscountPct": _discount(rng, 0.25, 0.60),
                "PromotionType": PROMO_TYPES["Flash"],
                "PromotionCategory": str(rng.choice(["Store", "Online"])),
                "StartDate": pd.Timestamp(start),
                "EndDate": pd.Timestamp(end),
            }
        )

    # Volume Discount (full-quarter windows, modest discounts)
    for _ in range(int(num_volume)):
        y = int(rng.choice(years))
        q_start_month = int(rng.choice([1, 4, 7, 10]))
        start = pd.Timestamp(year=y, month=q_start_month, day=1)
        end = start + timedelta(days=89)

        start = _clamp_to_year_window(start, year_windows)
        end = _clamp_to_year_window(end, year_windows)

        if not _valid_window(start, end):
            continue

        generated["Volume"] += 1
        rows.append(
            {
                "TypeGroup": "Volume",
                "SeasonType": "Volume Discount",
                "Year": y,
                "DiscountPct": _discount(rng, 0.05, 0.15),
                "PromotionType": PROMO_TYPES["Volume"],
                "PromotionCategory": _category(rng),
                "StartDate": pd.Timestamp(start),
                "EndDate": pd.Timestamp(end),
            }
        )

    # Loyalty Exclusive (month-long windows, targeted discounts)
    for _ in range(int(num_loyalty)):
        y = int(rng.choice(years))
        start = _random_start_date(rng, y)
        end = start + timedelta(days=int(rng.integers(20, 45)))

        start = _clamp_to_year_window(start, year_windows)
        end = _clamp_to_year_window(end, year_windows)

        if not _valid_window(start, end):
            continue

        generated["Loyalty"] += 1
        rows.append(
            {
                "TypeGroup": "Loyalty",
                "SeasonType": "Loyalty Exclusive",
                "Year": y,
                "DiscountPct": _discount(rng, 0.10, 0.25),
                "PromotionType": PROMO_TYPES["Loyalty"],
                "PromotionCategory": str(rng.choice(["Store", "Online"])),
                "StartDate": pd.Timestamp(start),
                "EndDate": pd.Timestamp(end),
            }
        )

    # Bundle Deal (2–6 week windows, moderate discounts)
    for _ in range(int(num_bundle)):
        y = int(rng.choice(years))
        start = _random_start_date(rng, y)
        end = start + timedelta(days=int(rng.integers(14, 42)))

        start = _clamp_to_year_window(start, year_windows)
        end = _clamp_to_year_window(end, year_windows)

        if not _valid_window(start, end):
            continue

        generated["Bundle"] += 1
        rows.append(
            {
                "TypeGroup": "Bundle",
                "SeasonType": "Bundle Deal",
                "Year": y,
                "DiscountPct": _discount(rng, 0.10, 0.20),
                "PromotionType": PROMO_TYPES["Bundle"],
                "PromotionCategory": _category(rng),
                "StartDate": pd.Timestamp(start),
                "EndDate": pd.Timestamp(end),
            }
        )

    # New Customer (runs continuously per year, flat 10–20% discount)
    for _ in range(int(num_new_customer)):
        y = int(rng.choice(years))
        ws, we = year_windows[y]
        start = pd.Timestamp(ws)
        end = pd.Timestamp(we)

        if not _valid_window(start, end):
            continue

        generated["NewCustomer"] += 1
        rows.append(
            {
                "TypeGroup": "NewCustomer",
                "SeasonType": "New Customer",
                "Year": y,
                "DiscountPct": _discount(rng, 0.10, 0.20),
                "PromotionType": PROMO_TYPES["NewCustomer"],
                "PromotionCategory": str(rng.choice(["Store", "Online"])),
                "StartDate": pd.Timestamp(start),
                "EndDate": pd.Timestamp(end),
            }
        )

    # Warn when date-window clamping dropped promotions
    for type_group, req in requested.items():
        got = generated[type_group]
        if got < req:
            warn(f"{type_group} - requested {req}, generated {got} "
                 f"({req - got} dropped by date-window clamping)")

    if not rows:
        raise DimensionError("Promotions: No promotions could be generated (check date windows and config).")

    df = pd.DataFrame(rows)

    # Stable ordering
    df = df.sort_values(["TypeGroup", "Year", "SeasonType", "StartDate"]).reset_index(drop=True)

    # PromotionName = base name (SeasonType), PromotionYear, PromotionSequence
    df["PromotionName"] = df["SeasonType"]
    df["PromotionYear"] = df["Year"].astype(np.int32)

    grp = df.groupby(["Year", "TypeGroup", "SeasonType"], sort=False)
    df["PromotionSequence"] = (grp.cumcount() + 1).astype(np.int32)

    # PromotionDescription — display-friendly composite
    df["PromotionDescription"] = (
        df["PromotionName"].astype(str)
        + " "
        + df["PromotionYear"].astype(str)
        + " #"
        + df["PromotionSequence"].astype(str)
    )

    # Sort promos by StartDate; keys start at 2 (key 1 reserved for No Discount)
    df = df.sort_values("StartDate").reset_index(drop=True)
    df["PromotionKey"] = (df.index + 2).astype(np.int32)

    # Build "No Discount" sentinel — always PromotionKey=1
    min_year = min(years)
    max_year = max(years)
    no_discount = pd.DataFrame(
        [
            {
                "PromotionKey": np.int32(1),
                "PromotionName": "No Discount",
                "PromotionDescription": "No Discount",
                "DiscountPct": 0.0,
                "PromotionType": PROMO_TYPES["NoDiscount"],
                "PromotionCategory": "No Discount",
                "PromotionYear": np.int32(min_year),
                "PromotionSequence": np.int32(1),
                "StartDate": pd.Timestamp(year_windows[min_year][0]),
                "EndDate": pd.Timestamp(year_windows[max_year][1]),
            }
        ]
    )

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=FutureWarning,
            message=r".*DataFrame concatenation with empty or all-NA entries.*",
        )
        final = pd.concat([no_discount, df], ignore_index=True)

    # PromotionLabel — readable short code (e.g. "PROMO-001")
    final["PromotionLabel"] = [f"PROMO-{k:03d}" for k in final["PromotionKey"]]

    return final[
        [
            "PromotionKey",
            "PromotionLabel",
            "PromotionName",
            "PromotionDescription",
            "DiscountPct",
            "PromotionType",
            "PromotionCategory",
            "PromotionYear",
            "PromotionSequence",
            "StartDate",
            "EndDate",
        ]
    ]


# ---------------------------------------------------------
#  PIPELINE ENTRYPOINT
# ---------------------------------------------------------

def run_promotions(cfg: Dict, parquet_folder: Path) -> None:
    out_path = parquet_folder / "promotions.parquet"

    if not isinstance(cfg, Mapping) or not hasattr(cfg, "promotions"):
        raise DimensionError("Missing required config section: 'promotions'")

    promo_cfg = cfg.promotions or {}
    defaults_dates = (
        getattr(cfg.defaults, "dates", None) if hasattr(cfg, "defaults") else None
    ) or (
        getattr(getattr(cfg, "_defaults", None), "dates", None)
    )
    if not defaults_dates or not getattr(defaults_dates, "start", None) or not getattr(defaults_dates, "end", None):
        raise DimensionError("Promotions: missing defaults.dates.start/end (or _defaults.dates.start/end)")

    version_cfg = as_dict(promo_cfg)
    # new_customer_window_months only affects sales-time promo assignment, not
    # the promotions dimension itself, so exclude it: editing it should not
    # force a needless promotions regeneration.
    version_cfg.pop("new_customer_window_months", None)
    version_cfg["global_dates"] = as_dict(defaults_dates) if defaults_dates else {}

    if not should_regenerate("promotions", version_cfg, out_path):
        skip("Promotions up-to-date")
        return

    override_dates = _normalize_override_dates(promo_cfg)

    if override_dates.get("start") and override_dates.get("end"):
        start = pd.to_datetime(override_dates["start"])
        end = pd.to_datetime(override_dates["end"])
    else:
        start = pd.to_datetime(defaults_dates.start)
        end = pd.to_datetime(defaults_dates.end)

    years, windows = _build_year_windows(start, end)
    seed = resolve_seed(cfg, promo_cfg, fallback=42)

    # Optional parquet settings (safe defaults)
    compression = promo_cfg.parquet_compression
    compression_level = promo_cfg.parquet_compression_level
    force_date32 = bool(promo_cfg.force_date32)

    with stage("Generating Promotions"):
        df = generate_promotions_catalog(
            years=years,
            year_windows=windows,
            num_seasonal=_bucket_count(promo_cfg.num_seasonal, PROMOTION_DEFAULT_NUM_SEASONAL),
            num_clearance=_bucket_count(promo_cfg.num_clearance, PROMOTION_DEFAULT_NUM_CLEARANCE),
            num_limited=_bucket_count(promo_cfg.num_limited, PROMOTION_DEFAULT_NUM_LIMITED),
            num_flash=_bucket_count(promo_cfg.num_flash, PROMOTION_DEFAULT_NUM_FLASH),
            num_volume=_bucket_count(promo_cfg.num_volume, PROMOTION_DEFAULT_NUM_VOLUME),
            num_loyalty=_bucket_count(promo_cfg.num_loyalty, PROMOTION_DEFAULT_NUM_LOYALTY),
            num_bundle=_bucket_count(promo_cfg.num_bundle, PROMOTION_DEFAULT_NUM_BUNDLE),
            num_new_customer=_bucket_count(promo_cfg.num_new_customer, PROMOTION_DEFAULT_NUM_NEW_CUSTOMER),
            seed=seed,
        )

        # Write with date32 for Power Query
        _write_parquet_with_date32(
            df,
            out_path,
            compression=str(compression),
            compression_level=(int(compression_level) if compression_level is not None else None),
            force_date32=force_date32,
        )

    save_version("promotions", version_cfg, out_path)
    info(f"Promotions dimension written: {out_path.name}")
