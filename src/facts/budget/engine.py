"""Budget engine: pandas translation of the SQL budget views.

Stages:
  1. Yearly budget (vw_Budget)          -> _compute_yearly_budget()
  2. Monthly budget (category × month)  -> _compute_monthly_budget()

Input:  actuals DataFrame from BudgetAccumulator.finalize_sales()
Output: (yearly, monthly) budget DataFrames
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


# ================================================================
# Config
# ================================================================

@dataclass(frozen=True)
class BudgetConfig:
    """Budget generation parameters (from config.yaml budget: section)."""
    enabled: bool = False

    # Scenario adjustments
    scenarios: Dict[str, float] = field(default_factory=lambda: {
        "Low": -0.03, "Medium": 0.00, "High": 0.05,
    })

    # Growth blending weights
    weight_local: float = 0.60
    weight_category: float = 0.30
    weight_global: float = 0.10

    # Growth caps
    growth_cap_high: float = 0.30
    growth_cap_low: float = -0.20

    # Backcast default growth
    default_backcast_growth: float = 0.05

    # Default fallback growth (when all tiers are NULL)
    fallback_growth: float = 0.05

    # Rolling window for category/global growth
    rolling_window: int = 3   # 3-year rolling average


def load_budget_config(cfg: Dict[str, Any]) -> BudgetConfig:
    """Extract budget config from the main config dict."""
    raw = getattr(cfg, "budget", None) or {}
    if not isinstance(raw, Mapping):
        return BudgetConfig()

    # Typed access: raw is BudgetConfig (schema) when cfg is AppConfig
    growth_caps = getattr(raw, "growth_caps", None) or {}
    weights = getattr(raw, "weights", None) or {}

    return BudgetConfig(
        enabled=bool(getattr(raw, "enabled", False)),
        scenarios=getattr(raw, "scenarios", {"Low": -0.03, "Medium": 0.00, "High": 0.05}),
        growth_cap_high=float(getattr(growth_caps, "high", 0.30)),
        growth_cap_low=float(getattr(growth_caps, "low", -0.20)),
        weight_local=float(getattr(weights, "local", 0.60)),
        weight_category=float(getattr(weights, "category", 0.30)),
        weight_global=float(getattr(weights, "global_", getattr(weights, "global", 0.10))),
        default_backcast_growth=float(getattr(raw, "default_backcast_growth", 0.05)),
    )

# ================================================================
# Deterministic jitter (replaces SQL CHECKSUM)
# ================================================================

def _jitter_pct(country: str, category: str, year: int) -> float:
    """
    Deterministic pseudo-random jitter in [-0.02, +0.02].
    Replaces SQL: (ABS(CHECKSUM(country, category, year)) % 401 - 200) / 10000.

    NOTE: Won't match SQL Server CHECKSUM byte-for-byte, but produces
    equivalent statistical distribution. Use a stable hash so results
    are reproducible across Python versions.
    """
    key = f"{country}|{category}|{year}".encode("utf-8")
    h = int(hashlib.md5(key).hexdigest(), 16) & ((1 << 64) - 1)
    return ((h % 401) - 200) / 10000.0


def _jitter_series(df: pd.DataFrame) -> pd.Series:
    """Vectorized jitter over a DataFrame with Country, Category, BudgetYear.

    Builds all hash keys as a single byte-string array, hashes each with md5
    via a list comprehension (unavoidable for md5), then applies the modular
    arithmetic in pure numpy.  ~10-20× faster than row-by-row ``apply`` for
    typical budget DataFrames (hundreds to low-thousands of rows).
    """
    keys = (
        df["Country"].astype(str).values
        + "|"
        + df["Category"].astype(str).values
        + "|"
        + df["BudgetYear"].astype(int).astype(str)
    )
    _MASK64 = (1 << 64) - 1
    hashes = np.array(
        [int(hashlib.md5(k.encode("utf-8")).hexdigest(), 16) & _MASK64 for k in keys],
        dtype=np.uint64,
    )
    return pd.Series(
        ((hashes % 401).astype(np.float64) - 200.0) / 10000.0,
        index=df.index,
    )


# ================================================================
# Stage 1: Yearly budget (vw_Budget equivalent)
# ================================================================

def _compute_yearly_budget(
    actuals_annual: pd.DataFrame,
    bcfg: BudgetConfig,
) -> pd.DataFrame:
    """
    Input: actuals_annual with (Country, Category, Year, SalesAmount, SalesQuantity)
           aggregated to annual grain (no channel/month).

    Returns: budget at (Country, Category, BudgetYear, Scenario) grain with
             BudgetGrowthPct, BudgetAmount, BudgetQuantity, BudgetMethod.
    """
    a = actuals_annual.copy()
    if a.empty:
        return pd.DataFrame()
    min_year = int(a["Year"].min())
    max_year = int(a["Year"].max())

    # ---- Local YoY ----
    a = a.sort_values(["Country", "Category", "Year"])
    a["PrevAmount"] = a.groupby(["Country", "Category"])["SalesAmount"].shift(1)
    a["LocalYoY"] = _capped_growth(a["SalesAmount"], a["PrevAmount"], bcfg)

    # ---- Category-Year rolling ----
    cat_year = a.groupby(["Category", "Year"], as_index=False).agg(
        CatAmount=("SalesAmount", "sum")
    )
    cat_year = cat_year.sort_values(["Category", "Year"])
    cat_year["PrevCat"] = cat_year.groupby("Category")["CatAmount"].shift(1)
    cat_year["CatYoY"] = _capped_growth(cat_year["CatAmount"], cat_year["PrevCat"], bcfg)
    cat_year["CatYoY_3yr"] = (
        cat_year.groupby("Category")["CatYoY"]
        .rolling(bcfg.rolling_window, min_periods=1).mean()
        .reset_index(level=0, drop=True)
    )

    # ---- Global-Year rolling ----
    glob_year = a.groupby("Year", as_index=False).agg(
        GlobalAmount=("SalesAmount", "sum")
    )
    glob_year = glob_year.sort_values("Year")
    glob_year["PrevGlobal"] = glob_year["GlobalAmount"].shift(1)
    glob_year["GlobalYoY"] = _capped_growth(
        glob_year["GlobalAmount"], glob_year["PrevGlobal"], bcfg
    )
    glob_year["GlobalYoY_3yr"] = (
        glob_year["GlobalYoY"]
        .rolling(bcfg.rolling_window, min_periods=1).mean()
    )

    # ---- Join growth tiers back ----
    a = a.merge(cat_year[["Category", "Year", "CatYoY_3yr"]],
                on=["Category", "Year"], how="left")
    a = a.merge(glob_year[["Year", "GlobalYoY_3yr"]],
                on="Year", how="left")

    # ---- Blended growth ----
    a["BaseGrowth"] = _blend_growth(
        a["LocalYoY"], a["CatYoY_3yr"], a["GlobalYoY_3yr"], bcfg
    )

    # ---- Standard budgets: BudgetYear = ActualYear + 1 ----
    standard = a[a["Year"] + 1 <= max_year].copy()
    standard["BudgetYear"] = standard["Year"] + 1

    # ---- Backfill budgets for first 2 years ----
    first_year = a[a["Year"] == min_year].copy()

    backfill_same = first_year.copy()
    backfill_same["BudgetYear"] = min_year
    backfill_same["BaseGrowth"] = 0.0
    backfill_same["_method"] = "Backfill: same-year baseline"

    backfill_prior = first_year.copy()
    backfill_prior["BudgetYear"] = min_year - 1
    backcast_growth = bcfg.default_backcast_growth
    if abs(1.0 + backcast_growth) < 1e-9:
        backcast_growth = 0.05  # prevent division by zero; fall back to 5%
    backfill_prior["BaseGrowth"] = -backcast_growth
    backfill_prior["SalesAmount"] = (
        backfill_prior["SalesAmount"] / (1.0 + backcast_growth)
    )
    backfill_prior["SalesQuantity"] = (
        backfill_prior["SalesQuantity"] / (1.0 + backcast_growth)
    )
    backfill_prior["_method"] = "Backfill: back-cast from first-year"

    standard["_method"] = "Standard: LY actual + blended growth + scenario + jitter"

    combined = pd.concat([standard, backfill_same, backfill_prior], ignore_index=True)

    # ---- Expand across scenarios ----
    # Jitter depends only on (Country, Category, BudgetYear) — compute once and
    # broadcast to all scenarios instead of recomputing per scenario.
    jitter = _jitter_series(combined)

    scenario_names = list(bcfg.scenarios.keys())
    scenario_adjs = np.array([bcfg.scenarios[s] for s in scenario_names])
    n_scenarios = len(scenario_names)
    n_rows = len(combined)

    tiled = pd.concat([combined] * n_scenarios, ignore_index=True)
    tiled["Scenario"] = np.repeat(scenario_names, n_rows)
    tiled_jitter = np.tile(jitter.values, n_scenarios)
    tiled_adj = np.repeat(scenario_adjs, n_rows)

    tiled["BudgetGrowthPct"] = tiled["BaseGrowth"].values + tiled_jitter + tiled_adj
    growth_mult = 1.0 + tiled["BudgetGrowthPct"].values
    tiled["BudgetAmount"] = tiled["SalesAmount"].values * growth_mult
    tiled["BudgetQuantity"] = tiled["SalesQuantity"].values * growth_mult

    out = tiled[["Country", "Category", "BudgetYear", "Scenario",
                  "BudgetGrowthPct", "BudgetAmount", "BudgetQuantity",
                  "_method"]].rename(columns={"_method": "BudgetMethod"})
    out["BudgetYear"] = out["BudgetYear"].astype(int)
    out["BudgetGrowthPct"] = out["BudgetGrowthPct"].round(6)
    out["BudgetAmount"] = out["BudgetAmount"].round(2)
    out["BudgetQuantity"] = out["BudgetQuantity"].round(2)
    return out



# ================================================================
# Helpers
# ================================================================

def _capped_growth(
    current: pd.Series,
    previous: pd.Series,
    bcfg: BudgetConfig,
) -> pd.Series:
    """YoY growth, capped to [growth_cap_low, growth_cap_high]."""
    raw = (current - previous) / previous.replace(0, np.nan)
    return raw.clip(lower=bcfg.growth_cap_low, upper=bcfg.growth_cap_high)


def _blend_growth(
    local: pd.Series,
    cat_3yr: pd.Series,
    global_3yr: pd.Series,
    bcfg: BudgetConfig,
) -> pd.Series:
    """
    Dynamic weighted blend of 3 growth tiers.
    Weights are zeroed for NULL tiers, then re-normalized.
    Falls back to bcfg.fallback_growth if all tiers are NULL.
    """
    w_l = np.where(local.notna(), bcfg.weight_local, 0)
    w_c = np.where(cat_3yr.notna(), bcfg.weight_category, 0)
    w_g = np.where(global_3yr.notna(), bcfg.weight_global, 0)
    w_total = w_l + w_c + w_g

    blended = (
        local.fillna(0) * w_l
        + cat_3yr.fillna(0) * w_c
        + global_3yr.fillna(0) * w_g
    )

    return pd.Series(
        np.where(w_total > 0, blended / w_total, bcfg.fallback_growth),
        index=local.index,
    )



# ================================================================
# Stage 2: Monthly budget (category × month × scenario)
# ================================================================

def _compute_monthly_budget(
    yearly: pd.DataFrame,
    actuals_monthly: pd.DataFrame,
    bcfg: BudgetConfig,
) -> pd.DataFrame:
    """
    Spread yearly budget to monthly grain using seasonal weights from actuals.

    Input:
      - yearly: BudgetYearly at (Country, Category, BudgetYear, Scenario) grain
      - actuals_monthly: raw monthly actuals with (Category, Year, Month, SalesAmount, SalesQuantity)

    Output:
      Country, Category, BudgetYear, BudgetMonthStart, Scenario,
      BudgetAmount, BudgetQuantity, BudgetMethod
    """
    # ---- 1. Budget at Country × Category level (keep countries separate) ----
    cat_yearly = yearly.groupby(
        ["Country", "Category", "BudgetYear", "Scenario"], as_index=False
    ).agg(
        BudgetAmount=("BudgetAmount", "sum"),
        BudgetQuantity=("BudgetQuantity", "sum"),
    )

    # ---- 2. Compute seasonal weights from actuals ----
    cat_month = actuals_monthly.groupby(
        ["Category", "Year", "Month"], as_index=False
    ).agg(SalesAmount=("SalesAmount", "sum"))

    year_totals = cat_month.groupby(["Category", "Year"])["SalesAmount"].transform("sum")
    cat_month["MonthShare"] = np.where(
        year_totals > 0,
        cat_month["SalesAmount"].values / year_totals.values,
        1.0 / 12.0,
    )

    # Average month share across years → stable seasonal profile per category
    seasonal = cat_month.groupby(
        ["Category", "Month"], as_index=False
    ).agg(MonthShare=("MonthShare", "mean"))

    # Normalize so shares sum to 1.0 per category
    share_sums = seasonal.groupby("Category")["MonthShare"].transform("sum")
    seasonal["MonthShare"] = seasonal["MonthShare"].values / share_sums.values

    # ---- 3. Expand budget × 12 months, then apply weights ----
    n_budget = len(cat_yearly)
    budget_months = cat_yearly.loc[cat_yearly.index.repeat(12)].reset_index(drop=True)
    budget_months["Month"] = np.tile(np.arange(1, 13), n_budget)

    budget_months = budget_months.merge(
        seasonal, on=["Category", "Month"], how="left"
    )
    budget_months["MonthShare"] = budget_months["MonthShare"].fillna(1.0 / 12.0)

    # BUDGET-1: re-normalize the 12 monthly shares per budget row to sum to 1.0.
    # Seasonal shares were normalized over only the months present in actuals; after
    # expanding to 12 months and filling absent months with 1/12, a sparse category's
    # shares sum to >1.0, so its monthly budgets would overshoot the yearly total.
    # Rows are contiguous 12-per-budget (index.repeat(12)) in Month 1..12 order.
    _shares = budget_months["MonthShare"].to_numpy().reshape(n_budget, 12)
    _shares = _shares / _shares.sum(axis=1, keepdims=True)
    budget_months["MonthShare"] = _shares.reshape(-1)

    budget_months["BudgetAmount"] = (
        budget_months["BudgetAmount"].values * budget_months["MonthShare"].values
    )
    budget_months["BudgetQuantity"] = (
        budget_months["BudgetQuantity"].values * budget_months["MonthShare"].values
    )

    # ---- 4. Build BudgetMonthStart date via integer arithmetic ----
    years_i64 = budget_months["BudgetYear"].values.astype("int64")
    months_i64 = budget_months["Month"].values.astype("int64")
    months_since_epoch = (years_i64 - 1970) * 12 + (months_i64 - 1)
    budget_months["BudgetMonthStart"] = months_since_epoch.astype("datetime64[M]")

    budget_months["BudgetMethod"] = "Monthly: yearly total x seasonal share"

    out = budget_months[[
        "Country", "Category", "BudgetYear", "BudgetMonthStart", "Scenario",
        "BudgetAmount", "BudgetQuantity", "BudgetMethod",
    ]].copy()

    out["BudgetYear"] = out["BudgetYear"].astype(int)
    out["BudgetAmount"] = out["BudgetAmount"].round(2)
    out["BudgetQuantity"] = out["BudgetQuantity"].round(2)
    return out


# ================================================================
# Public API
# ================================================================

def compute_budget(
    actuals_monthly: pd.DataFrame,
    bcfg: BudgetConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Compute all budget tables.

    Returns:
        (yearly_budget, monthly_budget)
    """
    actuals_annual = actuals_monthly.groupby(
        ["Country", "Category", "Year"], as_index=False
    ).agg(SalesAmount=("SalesAmount", "sum"), SalesQuantity=("SalesQuantity", "sum"))

    yearly = _compute_yearly_budget(actuals_annual, bcfg)
    monthly = _compute_monthly_budget(yearly, actuals_monthly, bcfg)

    return yearly, monthly
