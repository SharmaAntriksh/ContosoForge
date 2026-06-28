"""Budget pipeline runner.

Called from sales_runner.py after sales generation completes.
Uses the BudgetAccumulator that was populated during sales generation.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from src.utils.logging_utils import stage, info, done, short_path
from src.facts.shared.writers import write_fact_table

from .accumulator import BudgetAccumulator
from .engine import load_budget_config, compute_budget


def run_budget_pipeline(
    *,
    accumulator: BudgetAccumulator,
    parquet_dims: Path,
    fact_out: Path,
    cfg: Dict[str, Any],
    file_format: str = "parquet",
) -> Optional[Dict[str, Any]]:
    """
    Generate budget_yearly fact table from streaming-aggregated sales actuals.

    Args:
        accumulator:   BudgetAccumulator populated during sales generation
        parquet_dims:  path to generated dimension parquets
        fact_out:      path to write budget fact parquets
        cfg:           full config dict
        file_format:   "csv" | "parquet" | "deltaparquet"

    Returns:
        summary dict or None if budget is disabled / no data
    """
    bcfg = load_budget_config(cfg)
    if not bcfg.enabled:
        info("Budget generation: disabled in config")
        return None

    if not accumulator.has_data:
        info("Budget generation: no sales actuals accumulated, skipping")
        return None

    t0 = time.time()

    # ---- Finalize actuals from accumulated micro-aggregates ----
    actuals_monthly = accumulator.finalize_sales()

    info(f"Budget actuals: {len(actuals_monthly):,} monthly grain rows "
         f"({actuals_monthly['Year'].nunique()} years × "
         f"{actuals_monthly['Country'].nunique()} countries × "
         f"{actuals_monthly['Category'].nunique()} categories)")

    # ---- Compute budget tables ----
    yearly, monthly = compute_budget(
        actuals_monthly=actuals_monthly,
        bcfg=bcfg,
    )

    # ---- Write output ----
    budget_out = fact_out / "budget"
    budget_out.mkdir(parents=True, exist_ok=True)

    write_fact_table(yearly, budget_out, "budget_yearly", file_format,
                     csv_prep_fn=lambda df: _prepare_budget_csv(df, "budget_yearly"),
                     csv_float_format="%.6f")
    write_fact_table(monthly, budget_out, "budget_monthly", file_format,
                     csv_prep_fn=lambda df: _prepare_budget_csv(df, "budget_monthly"),
                     csv_float_format="%.6f")

    # For deltaparquet the delta tables are written outside budget_out;
    # remove the empty temporary directory.
    if file_format == "deltaparquet":
        try:
            budget_out.rmdir()
        except OSError:
            pass

    elapsed = time.time() - t0
    yearly_rows = len(yearly)
    monthly_rows = len(monthly)

    return {
        "yearly_rows": yearly_rows,
        "monthly_rows": monthly_rows,
        "elapsed_sec": elapsed,
    }


# ----------------------------------------------------------------
# CSV preparation: column selection + type/precision alignment
# ----------------------------------------------------------------

_BUDGET_CSV_COLUMNS: dict[str, list[str]] = {
    "budget_yearly": [
        "Country", "Category", "BudgetYear", "Scenario",
        "BudgetGrowthPct", "BudgetAmount", "BudgetQuantity",
        "BudgetMethod",
    ],
    "budget_monthly": [
        "Country", "Category", "BudgetYear", "BudgetMonthStart", "Scenario",
        "BudgetAmount", "BudgetQuantity", "BudgetMethod",
    ],
}

_BUDGET_CSV_ROUND: dict[str, int] = {
    "BudgetGrowthPct": 6,
    "BudgetAmount": 2,
    "BudgetQuantity": 2,
}

_BUDGET_CSV_INT_COLS: tuple[str, ...] = ("BudgetYear",)


def _prepare_budget_csv(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Select schema columns, round decimals, and cast integers for clean CSV output."""
    expected_cols = _BUDGET_CSV_COLUMNS.get(name)
    if expected_cols is not None:
        out = df.copy()
        for col in expected_cols:
            if col not in out.columns:
                out[col] = None
        out = out[expected_cols]
    else:
        out = df.copy()

    for col, dp in _BUDGET_CSV_ROUND.items():
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").round(dp)

    for col in _BUDGET_CSV_INT_COLS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)

    return out
