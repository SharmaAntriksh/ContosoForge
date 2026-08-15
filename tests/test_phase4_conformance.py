"""Regression guards for Phase 4 currency and secondary-fact conformance."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.defaults import (
    CURRENCY_DEFAULT_LIST,
    CURRENCY_NAME_MAP,
    CURRENCY_SYMBOL_MAP,
)
from src.dimensions.exchange_rates.monthly_rates import build_monthly_rates
from src.dimensions.geography import FALLBACK_ROWS, _load_master
from src.engine.powerbi_packaging import attach_pbip_project, resolve_pbip_template_root
from src.exceptions import PackagingError, SalesError
from src.facts.inventory.engine import InventoryConfig, compute_inventory_snapshots
from src.facts.sales.prep.dimension_loaders import _load_stores


LOCAL_CURRENCIES = {
    "Norway": "NOK",
    "Poland": "PLN",
    "Sweden": "SEK",
    "Switzerland": "CHF",
}


def test_currency_defaults_cover_phase4_local_currencies():
    expected = set(LOCAL_CURRENCIES.values())
    assert expected.issubset(CURRENCY_DEFAULT_LIST)
    assert expected.issubset(CURRENCY_NAME_MAP)
    assert expected.issubset(CURRENCY_SYMBOL_MAP)
    assert CURRENCY_NAME_MAP["PLN"] == "Polish Zloty"


def test_geography_master_uses_local_currency_for_phase4_countries():
    master = _load_master()
    observed = (
        master.loc[master["Country"].isin(LOCAL_CURRENCIES), ["Country", "ISOCode"]]
        .drop_duplicates()
        .set_index("Country")["ISOCode"]
        .to_dict()
    )
    assert observed == LOCAL_CURRENCIES


def test_geography_fallback_uses_same_local_currency_contract():
    observed = {country: code for _, _, country, _, code in FALLBACK_ROWS}
    for country, code in LOCAL_CURRENCIES.items():
        assert observed[country] == code


def test_bundled_fx_master_has_dense_nok_and_pln_daily_series():
    master = pd.read_parquet(
        "data/exchange_rates_master/fx_master.parquet",
        columns=["Date", "FromCurrency", "ToCurrency", "Rate"],
    )
    for code in ("NOK", "PLN"):
        series = master.loc[master["ToCurrency"] == code].sort_values("Date")
        assert not series.empty
        assert set(series["FromCurrency"]) == {"USD"}
        dates = pd.DatetimeIndex(pd.to_datetime(series["Date"]))
        assert dates.is_unique
        assert len(dates) == len(pd.date_range(dates.min(), dates.max(), freq="D"))
        assert np.isfinite(series["Rate"]).all()
        assert (series["Rate"] > 0).all()


def test_monthly_fx_has_one_row_per_currency_month():
    dates = pd.date_range("2024-01-01", "2024-03-31", freq="D")
    daily = pd.concat(
        [
            pd.DataFrame({
                "Date": dates,
                "FromCurrencyKey": 1,
                "ToCurrencyKey": key,
                "FromCurrency": "USD",
                "ToCurrency": code,
                "Rate": np.linspace(1.0, 1.1, len(dates)),
            })
            for key, code in enumerate(("NOK", "PLN", "SEK", "CHF"), start=2)
        ],
        ignore_index=True,
    )
    monthly = build_monthly_rates(daily)
    counts = monthly.groupby("ToCurrency")["Date"].nunique().to_dict()
    assert counts == {code: 3 for code in ("CHF", "NOK", "PLN", "SEK")}


def _write_store_currency_fixture(folder: Path, geography: pd.DataFrame, currency: pd.DataFrame) -> None:
    pd.DataFrame({"StoreKey": [1], "GeographyKey": [1]}).to_parquet(
        folder / "stores.parquet", index=False,
    )
    geography.to_parquet(folder / "geography.parquet", index=False)
    currency.to_parquet(folder / "currency.parquet", index=False)


def test_sales_store_loader_rejects_missing_currency_join(tmp_path):
    _write_store_currency_fixture(
        tmp_path,
        pd.DataFrame({"GeographyKey": [1], "ISOCode": ["PLN"]}),
        pd.DataFrame({"CurrencyKey": [1], "CurrencyCode": ["USD"]}),
    )
    with pytest.raises(SalesError, match="no matching CurrencyKey"):
        _load_stores(tmp_path, pd.Timestamp("2025-12-31"))


def test_sales_store_loader_preserves_local_currency_keys(tmp_path):
    codes = list(LOCAL_CURRENCIES.values())
    pd.DataFrame({
        "StoreKey": np.arange(1, 5, dtype=np.int32),
        "GeographyKey": np.arange(1, 5, dtype=np.int32),
    }).to_parquet(tmp_path / "stores.parquet", index=False)
    pd.DataFrame({
        "GeographyKey": np.arange(1, 5, dtype=np.int32),
        "ISOCode": codes,
    }).to_parquet(tmp_path / "geography.parquet", index=False)
    pd.DataFrame({
        "CurrencyKey": np.arange(10, 14, dtype=np.int32),
        "CurrencyCode": codes,
    }).to_parquet(tmp_path / "currency.parquet", index=False)

    pool = _load_stores(tmp_path, pd.Timestamp("2025-12-31"))
    assert pool.geo_to_currency == {1: 10, 2: 11, 3: 12, 4: 13}


def test_inventory_stockout_always_has_at_least_one_day_out():
    demand = pd.DataFrame({
        "ProductKey": [1],
        "WarehouseKey": [1],
        "Year": [2024],
        "Month": [1],
        "QuantitySold": [31],
    })
    attrs = {
        "ProductKey": np.array([1], dtype=np.int32),
        "SafetyStockUnits": np.array([0], dtype=np.int32),
        "ReorderPointUnits": np.array([0], dtype=np.int32),
        "LeadTimeDays": np.array([30], dtype=np.int32),
        "ABCClassification": np.array(["B"]),
        "SeasonalityProfile": np.array(["None"]),
        "IsFragile": np.array([0], dtype=np.int32),
        "CasePackQty": np.array([1], dtype=np.int32),
    }
    result = compute_inventory_snapshots(
        demand,
        ".",
        InventoryConfig(
            enabled=True,
            min_demand_months=1,
            initial_stock_multiplier=0.99,
            overstock_bias=1.0,
            shrinkage_enabled=False,
        ),
        product_attrs_arrays=attrs,
    )
    stockouts = result.loc[result["StockoutFlag"]]
    assert not stockouts.empty
    assert (stockouts["DaysOutOfStock"] >= 1).all()
    assert (result.loc[~result["StockoutFlag"], "DaysOutOfStock"] == 0).all()


def _make_pbip_template(root: Path, *, extra_expression: str = "") -> Path:
    template = root / "Template"
    definition = template / "Demo.SemanticModel" / "definition"
    legacy = template / "Demo.SemanticModel" / "TMDLScripts"
    definition.mkdir(parents=True)
    legacy.mkdir(parents=True)
    (definition / "expressions.tmdl").write_text(
        'expression ContosoFolder = "C:\\\\old\\\\dataset"\n' + extra_expression,
        encoding="utf-8",
    )
    (legacy / "Model.tmdl").write_text(
        'expression ContosoFolder = "C:\\\\legacy\\\\dataset"\n',
        encoding="utf-8",
    )
    return template


def test_pbip_packaging_removes_legacy_tmdl_scripts_and_stale_paths(tmp_path):
    template = _make_pbip_template(tmp_path / "templates")
    final = tmp_path / "generated_datasets" / "current"
    final.mkdir(parents=True)

    attach_pbip_project(final, template)

    copied = final / "Template" / "Demo.SemanticModel"
    assert not (copied / "TMDLScripts").exists()
    text = (copied / "definition" / "expressions.tmdl").read_text(encoding="utf-8")
    assert "legacy" not in text.lower()
    assert "C:\\\\old\\\\dataset" not in text


def test_pbip_packaging_rejects_active_external_absolute_path(tmp_path):
    template = _make_pbip_template(
        tmp_path / "templates",
        extra_expression='expression Rogue = "C:\\\\someone_else\\\\dataset"\n',
    )
    final = tmp_path / "generated_datasets" / "current"
    final.mkdir(parents=True)

    with pytest.raises(PackagingError, match="external absolute path"):
        attach_pbip_project(final, template)


@pytest.mark.parametrize("file_format", ["csv", "parquet"])
@pytest.mark.parametrize("sales_output", ["sales", "sales_order", "both"])
def test_all_bundled_pbip_templates_package_without_external_paths(
    tmp_path, file_format, sales_output,
):
    template = resolve_pbip_template_root(
        file_format=file_format,
        sales_output=sales_output,
    )
    final = tmp_path / "generated_datasets" / f"{file_format}-{sales_output}"
    final.mkdir(parents=True)

    attach_pbip_project(final, template, pbip_dst_name="Project")

    project = final / "Project"
    assert not list(project.rglob("TMDLScripts"))

    stale_open_flag_metadata = [
        str(path.relative_to(project))
        for path in project.rglob("*.tmdl")
        if "OpenFlag" in path.read_text(encoding="utf-8")
    ]
    assert not stale_open_flag_metadata

    relationship_files = list(project.rglob("relationships.tmdl"))
    assert len(relationship_files) == 1
    relationships = relationship_files[0].read_text(encoding="utf-8")
    assert (
        "fromColumn: EmployeeStoreAssignment.EmployeeKey\n"
        "\ttoColumn: Employee.EmployeeKey"
    ) in relationships
    assert (
        "fromColumn: EmployeeStoreAssignment.StoreKey\n"
        "\ttoColumn: Store.StoreKey"
    ) in relationships
