"""Developer QA for temporal entity-rank movement.

The report compares the observed annual rank persistence in an existing packaged
dataset with deterministic, trend-derived projections over the same entity masses
and calendar window. It does not mutate the dataset or generator configuration.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.engine.config.config_schema import MacroDemandConfig  # noqa: E402
from src.exceptions import SalesError  # noqa: E402
from src.facts.sales.prep.temporal_mix_plan import (  # noqa: E402
    TEMPORAL_MIX_LAYER_INTENSITY,
)
from src.facts.sales.sales_models.temporal_mix import build_temporal_factors  # noqa: E402
from src.facts.sales.sales_models.temporal_mix_diagnostics import (  # noqa: E402
    RankPersistenceMetrics,
    rank_persistence_metrics,
)
from src.utils.trend_presets import get_trend_defaults  # noqa: E402


_TREND_FAMILIES = (
    "stagnation",
    "strong-growth",
    "decline",
    "boom-and-bust",
    "volatile",
)
_AXIS_INTENSITY = {
    "Category": TEMPORAL_MIX_LAYER_INTENSITY["product_category"],
    "Brand": TEMPORAL_MIX_LAYER_INTENSITY["product_brand_within_category"],
    "Color": TEMPORAL_MIX_LAYER_INTENSITY["product_color_within_category"],
    "Country": TEMPORAL_MIX_LAYER_INTENSITY["customer_country"],
    "Gender": TEMPORAL_MIX_LAYER_INTENSITY["customer_gender"],
}


@dataclass(frozen=True)
class AxisObservation:
    names: tuple[str, ...]
    annual_values: np.ndarray
    years: np.ndarray


def _resolved_trend(name: str) -> MacroDemandConfig:
    raw = get_trend_defaults(name)
    raw.pop("lifecycle", None)
    raw.pop("customers", None)
    if isinstance(raw.get("monthly_seasonality"), str):
        raw.pop("monthly_seasonality")
    return MacroDemandConfig.model_validate(raw)


def _load_sales_axes(dataset: Path) -> tuple[dict[str, AxisObservation], pd.Series]:
    facts = dataset / "facts"
    dimensions = dataset / "dimensions"
    required = (
        facts / "sales.parquet",
        dimensions / "customers.parquet",
        dimensions / "geography.parquet",
        dimensions / "products.parquet",
        dimensions / "product_subcategory.parquet",
        dimensions / "product_category.parquet",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SalesError(f"Dataset is missing required temporal QA files: {missing}")

    sales = pd.read_parquet(
        required[0],
        columns=["CustomerKey", "ProductKey", "OrderDate", "Quantity", "NetPrice"],
    )
    order_dates = pd.to_datetime(sales["OrderDate"], errors="raise")
    sales["Year"] = order_dates.dt.year.astype(np.int32)
    sales["Month"] = order_dates.dt.to_period("M")
    sales["SalesAmount"] = (
        sales["Quantity"].to_numpy(dtype=np.float64)
        * sales["NetPrice"].to_numpy(dtype=np.float64)
    )

    customers = pd.read_parquet(
        required[1], columns=["CustomerKey", "GeographyKey", "Gender"]
    )
    geography = pd.read_parquet(required[2], columns=["GeographyKey", "Country"])
    customers = customers.merge(geography, on="GeographyKey", how="left", validate="many_to_one")
    sales = sales.merge(
        customers[["CustomerKey", "Country", "Gender"]],
        on="CustomerKey",
        how="left",
        validate="many_to_one",
    )

    products = pd.read_parquet(
        required[3], columns=["ProductKey", "SubcategoryKey", "Brand", "Color"]
    )
    subcategories = pd.read_parquet(
        required[4], columns=["SubcategoryKey", "CategoryKey"]
    )
    categories = pd.read_parquet(required[5], columns=["CategoryKey", "Category"])
    products = products.merge(
        subcategories, on="SubcategoryKey", how="left", validate="many_to_one"
    ).merge(categories, on="CategoryKey", how="left", validate="many_to_one")
    sales = sales.merge(
        products[["ProductKey", "Category", "Brand", "Color"]],
        on="ProductKey",
        how="left",
        validate="many_to_one",
    )

    years = np.asarray(sorted(sales["Year"].unique()), dtype=np.int32)
    observations: dict[str, AxisObservation] = {}
    for axis in _AXIS_INTENSITY:
        labels = sales[axis].fillna("Unknown").astype(str)
        annual = (
            sales.assign(_Axis=labels)
            .pivot_table(
                index="Year",
                columns="_Axis",
                values="SalesAmount",
                aggfunc="sum",
                fill_value=0.0,
            )
            .reindex(years, fill_value=0.0)
            .sort_index(axis=1)
        )
        observations[axis] = AxisObservation(
            names=tuple(str(value) for value in annual.columns),
            annual_values=annual.to_numpy(dtype=np.float64),
            years=years,
        )

    monthly_totals = sales.groupby("Month", observed=True)["SalesAmount"].sum().sort_index()
    return observations, monthly_totals


def _projected_metrics(
    axis: str,
    observation: AxisObservation,
    monthly_totals: pd.Series,
    trend_name: str,
    seed: int,
) -> RankPersistenceMetrics:
    baseline = observation.annual_values.sum(axis=0)
    positive = baseline > 0.0
    names = tuple(name for name, keep in zip(observation.names, positive) if keep)
    baseline = baseline[positive]
    baseline /= baseline.sum()

    factors = build_temporal_factors(
        f"qa-{axis.lower()}",
        names,
        baseline,
        len(monthly_totals),
        _resolved_trend(trend_name),
        seed,
        intensity_scale=_AXIS_INTENSITY[axis],
    )
    monthly_values = factors * baseline[None, :] * monthly_totals.to_numpy()[:, None]
    month_years = np.asarray([value.year for value in monthly_totals.index], dtype=np.int32)
    projected = np.vstack(
        [monthly_values[month_years == year].sum(axis=0) for year in observation.years]
    )
    return rank_persistence_metrics(projected, entity_names=names, top_n=min(10, len(names)))


def _format_metrics(metrics: RankPersistenceMetrics) -> str:
    return (
        f"persist={metrics.mean_position_persistence:5.3f} "
        f"rho={metrics.mean_adjacent_spearman:6.3f} "
        f"swaps={metrics.adjacent_rank_swaps:4d} "
        f"tenure={metrics.top_entity_tenure:2d} "
        f"share={metrics.minimum_share:7.4f}-{metrics.maximum_share:7.4f} "
        f"hhi={metrics.mean_concentration:7.4f}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Packaged parquet dataset folder")
    parser.add_argument("--seed", type=int, default=20260818)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    dataset = args.dataset.expanduser().resolve()
    observations, monthly_totals = _load_sales_axes(dataset)

    print(f"Dataset: {dataset}")
    print(
        f"Window: {monthly_totals.index.min()} to {monthly_totals.index.max()} "
        f"({len(monthly_totals)} months)"
    )
    for axis, observation in observations.items():
        actual = rank_persistence_metrics(
            observation.annual_values,
            entity_names=observation.names,
            top_n=min(10, len(observation.names)),
        )
        print(f"\n{axis} ({len(observation.names)} entities)")
        print(f"  observed       {_format_metrics(actual)}")
        for trend_name in _TREND_FAMILIES:
            projected = _projected_metrics(
                axis,
                observation,
                monthly_totals,
                trend_name,
                args.seed,
            )
            print(f"  {trend_name:14} {_format_metrics(projected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
