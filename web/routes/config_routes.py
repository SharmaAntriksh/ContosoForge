"""
web/routes/config_routes.py -- All /api/config/* endpoints.
"""

from __future__ import annotations

from typing import Any, Dict

import copy

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from web.shared_state import (
    ConfigUpdate,
    _config_path,
    _base_config,
    _g,
    _promo_total,
    _set_promotions_total,
    normalize_config_yaml,
    cfg_to_dict,
)
import web.shared_state as _state

router = APIRouter(prefix="/api/config", tags=["config"])

from src.engine.config.config import _VALID_FILE_FORMATS as _VALID_FORMATS

_VALID_SALES_OUTPUTS = {"sales", "sales_order", "both"}


class ConfigYamlUpdate(BaseModel):
    yaml_text: str


# ---------------------------------------------------------------------------
# GET /api/config -- flat shape for the frontend
# ---------------------------------------------------------------------------

@router.get("")
def get_config():
    with _state._cfg_lock:
        cfg = copy.deepcopy(_state._cfg)
    defaults = _g(cfg, "defaults", "dates", default={})
    sales = _g(cfg, "sales", default={})
    cust = _g(cfg, "customers", default={})
    stores = _g(cfg, "stores", default={})
    prods = _g(cfg, "products", default={})
    promos = _g(cfg, "promotions", default={})
    pricing = _g(cfg, "products", "pricing", "base", default={})
    returns = _g(cfg, "returns", default={})
    geo = _g(cfg, "geography", default={})
    dates_cfg = _g(cfg, "dates", default={})
    include = _g(dates_cfg, "include", default={})
    wf = _g(include, "weekly_fiscal", default={})
    if isinstance(wf, bool):
        wf = {"enabled": wf}

    # Extra sections
    wl = _g(cfg, "wishlists", default={})
    cc = _g(cfg, "complaints", default={})
    sub = _g(cfg, "subscriptions", default={})
    emp = _g(cfg, "employees", default={})
    er = _g(cfg, "exchange_rates", default={})
    budget = _g(cfg, "budget", default={})
    inv = _g(cfg, "inventory", default={})
    cost = _g(cfg, "products", "pricing", "cost", default={})
    brand_norm = _g(cfg, "products", "pricing", "brand_normalization", default={})
    return {
        # Global
        "seed": int(_g(cfg, "defaults", "seed", default=42) or 42),
        # Output
        "format": str(getattr(sales, "file_format", "parquet")),
        "salesOutput": str(getattr(sales, "sales_output", "sales")),
        "skipOrderCols": bool(getattr(sales, "skip_order_cols", False)),
        "compression": str(_g(sales, "compression", default="snappy")),
        "rowGroupSize": int(_g(sales, "row_group_size", default=1000000)),
        "mergeParquet": bool(getattr(sales, "merge_parquet", True)),
        "partitionEnabled": bool(_g(sales, "partitioning", "enabled", default=True)),
        "maxLinesPerOrder": int(getattr(sales, "max_lines_per_order", 5)),
        "salesOptimize": bool(getattr(sales, "sort_merged_parquet", False)),
        "qualityReport": bool(getattr(sales, "quality_report", True)),
        # Dates
        "startDate": str(_g(defaults, "start", default="2023-01-01")),
        "endDate": str(_g(defaults, "end", default="2026-12-31")),
        "fiscalMonthOffset": int(_g(dates_cfg, "fiscal_start_month", default=0) or 0),
        "asOfDate": str(_g(dates_cfg, "as_of_date", default="") or ""),
        "includeCalendar": True,
        "includeIso": bool(_g(include, "iso", default=False)),
        "includeFiscal": bool(_g(include, "fiscal", default=True)),
        "includeWeeklyFiscal": bool(_g(wf, "enabled", default=False)),
        "wfFirstDay": int(_g(wf, "first_day_of_week", default=0)),
        "wfWeeklyType": str(_g(wf, "weekly_type", default="Last")),
        "wfQuarterType": str(_g(wf, "quarter_week_type", default="445")),
        "wfTypeStartFiscalYear": int(_g(wf, "type_start_fiscal_year", default=1)),
        # Volume
        "salesRows": int(getattr(sales, "total_rows", 100000)),
        "chunkSize": int(getattr(sales, "chunk_size", 200000)),
        "workers": int(getattr(sales, "workers", 0) or 0),
        # Dimensions
        "customers": int(getattr(cust, "total_customers", 10000) or 10000),
        "stores": int(getattr(stores, "num_stores", 100) or 100),
        "products": int(getattr(prods, "num_products", 5000) or 5000),
        "promotions": _promo_total(promos),
        # Customers detail
        "pctIndia": float(getattr(cust, "pct_india", 10)),
        "pctUs": float(getattr(cust, "pct_us", 51)),
        "pctEu": float(getattr(cust, "pct_eu", 39)),
        "pctAsia": float(getattr(cust, "pct_asia", 0)),
        "pctOrg": float(getattr(cust, "pct_org", 10)),
        "customerActiveRatio": float(getattr(cust, "active_ratio", 0.90)),
        "firstYearPct": float(getattr(cust, "first_year_pct", None) or 0.27),
        "householdPct": float(getattr(cust, "household_pct", 0.35) or 0.35),
        # Customers SCD2
        "custScd2Enabled": bool(getattr(getattr(cust, "scd2", None), "enabled", False)),
        "custScd2ChangeRate": float(getattr(getattr(cust, "scd2", None), "change_rate", 0.15)),
        "custScd2MaxVersions": int(getattr(getattr(cust, "scd2", None), "max_versions", 4)),
        # Products detail (pricing/cost/brand_norm are plain dicts from products.pricing)
        "valueScale": float(_g(pricing, "value_scale", default=1.0)),
        "minPrice": float(_g(pricing, "min_unit_price", default=10)),
        "maxPrice": float(_g(pricing, "max_unit_price", default=3000)),
        "productActiveRatio": float(getattr(prods, "active_ratio", 0.94)),
        "marginMin": float(_g(cost, "min_margin_pct", default=0.20)),
        "marginMax": float(_g(cost, "max_margin_pct", default=0.35)),
        "brandNormalize": bool(_g(brand_norm, "enabled", default=False)),
        "brandNormalizeAlpha": float(_g(brand_norm, "alpha", default=0.35)),
        # Products SCD2
        "prodScd2Enabled": bool(getattr(getattr(prods, "scd2", None), "enabled", False)),
        "prodScd2RevisionFreq": int(getattr(getattr(prods, "scd2", None), "revision_frequency", 12)),
        "prodScd2PriceDrift": float(getattr(getattr(prods, "scd2", None), "price_drift", 0.05)),
        "prodScd2MaxVersions": int(getattr(getattr(prods, "scd2", None), "max_versions", 4)),
        # Geography
        "geoWeights": dict(_g(geo, "country_weights", default={}) or {}),
        # Returns
        "returnsEnabled": bool(getattr(returns, "enabled", True)),
        "returnRate": float(getattr(returns, "return_rate", 0.03)),
        "returnMinDays": int(getattr(returns, "min_days_after_sale", 1)),
        "returnMaxDays": int(getattr(returns, "max_days_after_sale", 60)),
        # Promotions
        "promoNewCustWindow": int(_g(promos, "new_customer_window_months", default=3)),
        "promoSeasonal": int(getattr(promos, "num_seasonal", 20) or 20),
        "promoClearance": int(getattr(promos, "num_clearance", 8) or 8),
        "promoLimited": int(getattr(promos, "num_limited", 12) or 12),
        "promoFlash": int(getattr(promos, "num_flash", 6) or 6),
        "promoVolume": int(getattr(promos, "num_volume", 4) or 4),
        "promoLoyalty": int(getattr(promos, "num_loyalty", 3) or 3),
        "promoBundle": int(getattr(promos, "num_bundle", 3) or 3),
        "promoNewCustomer": int(getattr(promos, "num_new_customer", 3) or 3),
        # Subscriptions
        # Wishlists
        "wlEnabled": bool(getattr(wl, "enabled", False)),
        "wlParticipationRate": float(getattr(wl, "participation_rate", 0.35)),
        "wlAvgItems": float(getattr(wl, "avg_items", 3.5)),
        "wlMaxItems": int(getattr(wl, "max_items", 20)),
        "wlPreBrowseDays": int(getattr(wl, "pre_browse_days", 90)),
        "wlAffinityStrength": float(getattr(wl, "affinity_strength", 0.6)),
        "wlConversionRate": float(getattr(wl, "conversion_rate", 0.30)),
        "wlSeed": int(getattr(wl, "seed", 500) or 500),
        # Complaints
        "ccEnabled": bool(getattr(cc, "enabled", False)),
        "ccComplaintRate": float(getattr(cc, "complaint_rate", 0.03)),
        "ccRepeatComplaintRate": float(getattr(cc, "repeat_complaint_rate", 0.15)),
        "ccMaxComplaints": int(getattr(cc, "max_complaints", 5)),
        "ccResolutionRate": float(getattr(cc, "resolution_rate", 0.85)),
        "ccEscalationRate": float(getattr(cc, "escalation_rate", 0.10)),
        "ccAvgResponseDays": int(getattr(cc, "avg_response_days", 5)),
        "ccMaxResponseDays": int(getattr(cc, "max_response_days", 30)),
        "ccSeed": int(getattr(cc, "seed", 600) or 600),
        # Subscriptions
        "subEnabled": bool(getattr(sub, "enabled", False)),
        "subGenerateBridge": bool(getattr(sub, "generate_bridge", False)),
        "subParticipationRate": float(getattr(sub, "participation_rate", 0.65)),
        "subAvgSubscriptions": float(getattr(sub, "avg_subscriptions_per_customer", 1.5)),
        "subMaxSubscriptions": int(getattr(sub, "max_subscriptions", 5)),
        "subChurnRate": float(getattr(sub, "churn_rate", 0.25)),
        "subTrialRate": float(getattr(sub, "trial_rate", 0.30)),
        "subTrialConversionRate": float(getattr(sub, "trial_conversion_rate", 0.85)),
        "subTrialDays": int(getattr(sub, "trial_days", 14)),
        "subSeed": int(getattr(sub, "seed", 700) or 700),
        # Stores detail
        "storeEnsureIsoCoverage": bool(getattr(stores, "ensure_iso_coverage", True)),
        "storeOpeningStart": str(_g(stores, "opening", "start", default="2018-01-01")),
        "storeOpeningEnd": str(_g(stores, "opening", "end", default="2025-12-31")),
        "storeClosingEnd": str(getattr(stores, "closing_end", "2028-12-31")),
        "storeAssortmentEnabled": bool(_g(stores, "assortment", "enabled", default=True)),
        "storeOnlineStores": int(getattr(stores, "online_stores", 5) or 5),
        "storeClosingEnabled": bool(_g(stores, "closing", "enabled", default=True)),
        "storeCloseShare": float(_g(stores, "closing", "close_share", default=0.10)),
        "storeStaffingRanges": dict(getattr(stores, "staffing_ranges", None) or {}),
        "storeRegionWeights": dict(getattr(stores, "region_weights", None) or {}),
        # Employees
        "employeeEmailDomain": str(_g(emp, "hr", "email_domain", default="contoso.com")),
        "transfersEnabled": bool(_g(emp, "transfers", "enabled", default=False)),
        "transfersAnnualRate": float(_g(emp, "transfers", "annual_rate", default=0.05)),
        "transfersMinTenureMonths": int(_g(emp, "transfers", "min_tenure_months", default=6)),
        "transfersSameRegionPref": float(_g(emp, "transfers", "same_region_pref", default=0.70)),
        # Exchange Rates
        "erFromCurrencies": list(getattr(er, "from_currencies", ["USD"])),
        "erToCurrencies": list(getattr(er, "to_currencies", ["CAD", "GBP", "EUR", "INR", "AUD", "CNY", "JPY"])),
        "erBaseCurrency": str(getattr(er, "base_currency", "USD")),
        "erFutureDrift": float(getattr(er, "future_annual_drift", 0.02)),
        "erIncludeMonthly": bool(getattr(er, "include_monthly", True)),
        # Budget detail
        "budgetEnabled": bool(getattr(budget, "enabled", True)),
        "budgetDefaultGrowth": float(getattr(budget, "default_backcast_growth", 0.05)),
        # Inventory detail
        "inventoryEnabled": bool(getattr(inv, "enabled", True)),
        "inventoryGrain": str(getattr(inv, "grain", "monthly")),
        "inventoryShrinkageEnabled": bool(_g(inv, "shrinkage", "enabled", default=True)),
        "inventoryShrinkageRate": float(_g(inv, "shrinkage", "rate", default=0.02)),
    }


# ---------------------------------------------------------------------------
# POST /api/config -- apply partial updates from frontend
# ---------------------------------------------------------------------------

@router.post("")
def update_config(body: ConfigUpdate):
    with _state._cfg_lock:
        cfg = copy.deepcopy(_state._cfg)
        v = body.values

        # Ensure nested sub-models exist (Pydantic models have defaults,
        # but products.pricing is Optional[Dict] and geography may be None).
        if cfg.products.pricing is None:
            cfg.products.pricing = {}
        cfg.products.pricing.setdefault("base", {})
        cfg.products.pricing.setdefault("cost", {})
        cfg.products.pricing.setdefault("brand_normalization", {})
        if cfg.geography is None:
            from src.engine.config.config_schema import GeographyConfig
            cfg.geography = GeographyConfig()
        if cfg.dates.include.weekly_fiscal is None:
            from src.engine.config.config_schema import WeeklyFiscalConfig
            cfg.dates.include.weekly_fiscal = WeeklyFiscalConfig()

        # Global
        if "seed" in v: cfg.defaults.seed = int(v["seed"])

        # Output
        if "format" in v:
            fmt = str(v["format"]).strip().lower()
            if fmt not in _VALID_FORMATS:
                raise HTTPException(400, f"Invalid format: {fmt}. Must be one of {sorted(_VALID_FORMATS)}")
            cfg.sales.file_format = fmt
        if "salesOutput" in v:
            so = str(v["salesOutput"]).strip().lower()
            if so not in _VALID_SALES_OUTPUTS:
                raise HTTPException(400, f"Invalid salesOutput: {so}. Must be one of {sorted(_VALID_SALES_OUTPUTS)}")
            cfg.sales.sales_output = so
        if "skipOrderCols" in v: cfg.sales.skip_order_cols = bool(v["skipOrderCols"])
        if "compression" in v: cfg.sales.compression = v["compression"]
        if "rowGroupSize" in v: cfg.sales.row_group_size = int(v["rowGroupSize"])
        if "mergeParquet" in v: cfg.sales.merge_parquet = bool(v["mergeParquet"])
        if "partitionEnabled" in v:
            if cfg.sales.partitioning is None:
                cfg.sales.partitioning = {}
            cfg.sales.partitioning["enabled"] = bool(v["partitionEnabled"])
        if "maxLinesPerOrder" in v: cfg.sales.max_lines_per_order = int(v["maxLinesPerOrder"])
        if "salesOptimize" in v: cfg.sales.sort_merged_parquet = bool(v["salesOptimize"])
        if "qualityReport" in v: cfg.sales.quality_report = bool(v["qualityReport"])

        # Dates
        if "startDate" in v: cfg.defaults.dates.start = v["startDate"]
        if "endDate" in v: cfg.defaults.dates.end = v["endDate"]
        if "fiscalMonthOffset" in v: cfg.dates.fiscal_start_month = int(v["fiscalMonthOffset"])
        if "asOfDate" in v: cfg.dates.as_of_date = v["asOfDate"] or None
        if "includeIso" in v: cfg.dates.include.iso = bool(v["includeIso"])
        if "includeFiscal" in v: cfg.dates.include.fiscal = bool(v["includeFiscal"])
        if "includeWeeklyFiscal" in v:
            wf = cfg.dates.include.weekly_fiscal
            if not hasattr(wf, "enabled"):
                from src.engine.config.config_schema import WeeklyFiscalConfig
                cfg.dates.include.weekly_fiscal = WeeklyFiscalConfig(enabled=bool(v["includeWeeklyFiscal"]))
            else:
                cfg.dates.include.weekly_fiscal.enabled = bool(v["includeWeeklyFiscal"])
        if "wfFirstDay" in v: cfg.dates.include.weekly_fiscal.first_day_of_week = int(v["wfFirstDay"])
        if "wfWeeklyType" in v: cfg.dates.include.weekly_fiscal.weekly_type = v["wfWeeklyType"]
        if "wfQuarterType" in v: cfg.dates.include.weekly_fiscal.quarter_week_type = v["wfQuarterType"]
        if "wfTypeStartFiscalYear" in v: cfg.dates.include.weekly_fiscal.type_start_fiscal_year = int(v["wfTypeStartFiscalYear"])

        # Volume
        if "salesRows" in v: cfg.sales.total_rows = int(v["salesRows"])
        if "chunkSize" in v: cfg.sales.chunk_size = int(v["chunkSize"])
        if "workers" in v: cfg.sales.workers = int(v["workers"])

        # Dimensions
        if "customers" in v: cfg.customers.total_customers = int(v["customers"])
        if "stores" in v: cfg.stores.num_stores = int(v["stores"])
        if "products" in v: cfg.products.num_products = int(v["products"])
        if "promotions" in v: _set_promotions_total(cfg.promotions, int(v["promotions"]))

        # Customers detail
        if "pctIndia" in v: cfg.customers.pct_india = float(v["pctIndia"])
        if "pctUs" in v: cfg.customers.pct_us = float(v["pctUs"])
        if "pctEu" in v: cfg.customers.pct_eu = float(v["pctEu"])
        if "pctAsia" in v: cfg.customers.pct_asia = float(v["pctAsia"])
        if "pctOrg" in v: cfg.customers.pct_org = float(v["pctOrg"])
        if "customerActiveRatio" in v: cfg.customers.active_ratio = float(v["customerActiveRatio"])
        if "firstYearPct" in v: cfg.customers.first_year_pct = float(v["firstYearPct"])
        if "householdPct" in v: cfg.customers.household_pct = float(v["householdPct"])

        # Customers SCD2
        if any(k.startswith("custScd2") for k in v):
            if cfg.customers.scd2 is None:
                from src.engine.config.config_schema import CustomersSCD2Config
                cfg.customers.scd2 = CustomersSCD2Config()
            if "custScd2Enabled" in v: cfg.customers.scd2.enabled = bool(v["custScd2Enabled"])
            if "custScd2ChangeRate" in v: cfg.customers.scd2.change_rate = float(v["custScd2ChangeRate"])
            if "custScd2MaxVersions" in v: cfg.customers.scd2.max_versions = int(v["custScd2MaxVersions"])

        # Products detail (pricing is Dict[str, Any], so dict access is correct)
        if "valueScale" in v: cfg.products.pricing["base"]["value_scale"] = float(v["valueScale"])
        if "minPrice" in v: cfg.products.pricing["base"]["min_unit_price"] = float(v["minPrice"])
        if "maxPrice" in v: cfg.products.pricing["base"]["max_unit_price"] = float(v["maxPrice"])
        if "productActiveRatio" in v: cfg.products.active_ratio = float(v["productActiveRatio"])
        if "marginMin" in v: cfg.products.pricing["cost"]["min_margin_pct"] = float(v["marginMin"])
        if "marginMax" in v: cfg.products.pricing["cost"]["max_margin_pct"] = float(v["marginMax"])
        if "brandNormalize" in v: cfg.products.pricing["brand_normalization"]["enabled"] = bool(v["brandNormalize"])
        if "brandNormalizeAlpha" in v: cfg.products.pricing["brand_normalization"]["alpha"] = float(v["brandNormalizeAlpha"])

        # Products SCD2
        if any(k.startswith("prodScd2") for k in v):
            if cfg.products.scd2 is None:
                from src.engine.config.config_schema import ProductsSCD2Config
                cfg.products.scd2 = ProductsSCD2Config()
            if "prodScd2Enabled" in v: cfg.products.scd2.enabled = bool(v["prodScd2Enabled"])
            if "prodScd2RevisionFreq" in v: cfg.products.scd2.revision_frequency = int(v["prodScd2RevisionFreq"])
            if "prodScd2PriceDrift" in v: cfg.products.scd2.price_drift = float(v["prodScd2PriceDrift"])
            if "prodScd2MaxVersions" in v: cfg.products.scd2.max_versions = int(v["prodScd2MaxVersions"])

        # Geography
        if "geoWeights" in v and isinstance(v["geoWeights"], dict):
            object.__setattr__(cfg.geography, "country_weights", v["geoWeights"])

        # Returns
        if "returnsEnabled" in v: cfg.returns.enabled = bool(v["returnsEnabled"])
        if "returnRate" in v: cfg.returns.return_rate = float(v["returnRate"])
        if "returnMinDays" in v: cfg.returns.min_days_after_sale = int(v["returnMinDays"])
        if "returnMaxDays" in v: cfg.returns.max_days_after_sale = int(v["returnMaxDays"])

        # Promotions
        if "promoNewCustWindow" in v: cfg.promotions.new_customer_window_months = int(v["promoNewCustWindow"])
        if "promoSeasonal" in v: cfg.promotions.num_seasonal = int(v["promoSeasonal"])
        if "promoClearance" in v: cfg.promotions.num_clearance = int(v["promoClearance"])
        if "promoLimited" in v: cfg.promotions.num_limited = int(v["promoLimited"])
        if "promoFlash" in v: cfg.promotions.num_flash = int(v["promoFlash"])
        if "promoVolume" in v: cfg.promotions.num_volume = int(v["promoVolume"])
        if "promoLoyalty" in v: cfg.promotions.num_loyalty = int(v["promoLoyalty"])
        if "promoBundle" in v: cfg.promotions.num_bundle = int(v["promoBundle"])
        if "promoNewCustomer" in v: cfg.promotions.num_new_customer = int(v["promoNewCustomer"])

        # Wishlists
        if "wlEnabled" in v: cfg.wishlists.enabled = bool(v["wlEnabled"])
        if "wlParticipationRate" in v: cfg.wishlists.participation_rate = float(v["wlParticipationRate"])
        if "wlAvgItems" in v: cfg.wishlists.avg_items = float(v["wlAvgItems"])
        if "wlMaxItems" in v: cfg.wishlists.max_items = int(v["wlMaxItems"])
        if "wlPreBrowseDays" in v: cfg.wishlists.pre_browse_days = int(v["wlPreBrowseDays"])
        if "wlAffinityStrength" in v: cfg.wishlists.affinity_strength = float(v["wlAffinityStrength"])
        if "wlConversionRate" in v: cfg.wishlists.conversion_rate = float(v["wlConversionRate"])
        if "wlSeed" in v: cfg.wishlists.seed = int(v["wlSeed"])

        # Complaints
        if "ccEnabled" in v: cfg.complaints.enabled = bool(v["ccEnabled"])
        if "ccComplaintRate" in v: cfg.complaints.complaint_rate = float(v["ccComplaintRate"])
        if "ccRepeatComplaintRate" in v: cfg.complaints.repeat_complaint_rate = float(v["ccRepeatComplaintRate"])
        if "ccMaxComplaints" in v: cfg.complaints.max_complaints = int(v["ccMaxComplaints"])
        if "ccResolutionRate" in v: cfg.complaints.resolution_rate = float(v["ccResolutionRate"])
        if "ccEscalationRate" in v: cfg.complaints.escalation_rate = float(v["ccEscalationRate"])
        if "ccAvgResponseDays" in v: cfg.complaints.avg_response_days = int(v["ccAvgResponseDays"])
        if "ccMaxResponseDays" in v: cfg.complaints.max_response_days = int(v["ccMaxResponseDays"])
        if "ccSeed" in v: cfg.complaints.seed = int(v["ccSeed"])

        # Subscriptions
        if "subEnabled" in v: cfg.subscriptions.enabled = bool(v["subEnabled"])
        if "subGenerateBridge" in v: cfg.subscriptions.generate_bridge = bool(v["subGenerateBridge"])
        if "subParticipationRate" in v: cfg.subscriptions.participation_rate = float(v["subParticipationRate"])
        if "subAvgSubscriptions" in v: cfg.subscriptions.avg_subscriptions_per_customer = float(v["subAvgSubscriptions"])
        if "subMaxSubscriptions" in v: cfg.subscriptions.max_subscriptions = int(v["subMaxSubscriptions"])
        if "subChurnRate" in v: cfg.subscriptions.churn_rate = float(v["subChurnRate"])
        if "subTrialRate" in v: cfg.subscriptions.trial_rate = float(v["subTrialRate"])
        if "subTrialConversionRate" in v: cfg.subscriptions.trial_conversion_rate = float(v["subTrialConversionRate"])
        if "subTrialDays" in v: cfg.subscriptions.trial_days = int(v["subTrialDays"])
        if "subSeed" in v: cfg.subscriptions.seed = int(v["subSeed"])

        # Stores detail
        if "storeEnsureIsoCoverage" in v: cfg.stores.ensure_iso_coverage = bool(v["storeEnsureIsoCoverage"])
        if "storeOpeningStart" in v: cfg.stores.opening.start = v["storeOpeningStart"]
        if "storeOpeningEnd" in v: cfg.stores.opening.end = v["storeOpeningEnd"]
        if "storeClosingEnd" in v: cfg.stores.closing_end = v["storeClosingEnd"]
        if "storeAssortmentEnabled" in v: cfg.stores.assortment.enabled = bool(v["storeAssortmentEnabled"])
        if "storeOnlineStores" in v: cfg.stores.online_stores = int(v["storeOnlineStores"])
        if "storeClosingEnabled" in v: cfg.stores.closing.enabled = bool(v["storeClosingEnabled"])
        if "storeCloseShare" in v: cfg.stores.closing.close_share = float(v["storeCloseShare"])
        if "storeStaffingRanges" in v and isinstance(v["storeStaffingRanges"], dict): cfg.stores.staffing_ranges = v["storeStaffingRanges"]
        if "storeRegionWeights" in v and isinstance(v["storeRegionWeights"], dict): cfg.stores.region_weights = v["storeRegionWeights"]

        # Employees
        if "employeeEmailDomain" in v: cfg.employees.hr.email_domain = v["employeeEmailDomain"]
        if "transfersEnabled" in v: cfg.employees.transfers.enabled = bool(v["transfersEnabled"])
        if "transfersAnnualRate" in v: cfg.employees.transfers.annual_rate = float(v["transfersAnnualRate"])
        if "transfersMinTenureMonths" in v: cfg.employees.transfers.min_tenure_months = int(v["transfersMinTenureMonths"])
        if "transfersSameRegionPref" in v: cfg.employees.transfers.same_region_pref = float(v["transfersSameRegionPref"])

        # Exchange Rates
        if "erFromCurrencies" in v and isinstance(v["erFromCurrencies"], list): cfg.exchange_rates.from_currencies = v["erFromCurrencies"]
        if "erToCurrencies" in v and isinstance(v["erToCurrencies"], list): cfg.exchange_rates.to_currencies = v["erToCurrencies"]
        if "erBaseCurrency" in v: cfg.exchange_rates.base_currency = v["erBaseCurrency"]
        if "erFutureDrift" in v: cfg.exchange_rates.future_annual_drift = float(v["erFutureDrift"])
        if "erIncludeMonthly" in v: cfg.exchange_rates.include_monthly = bool(v["erIncludeMonthly"])

        # Budget detail
        if "budgetEnabled" in v: cfg.budget.enabled = bool(v["budgetEnabled"])
        if "budgetDefaultGrowth" in v: cfg.budget.default_backcast_growth = float(v["budgetDefaultGrowth"])

        # Inventory detail
        if "inventoryEnabled" in v: cfg.inventory.enabled = bool(v["inventoryEnabled"])
        if "inventoryGrain" in v: cfg.inventory.grain = v["inventoryGrain"]
        if "inventoryShrinkageEnabled" in v: cfg.inventory.shrinkage.enabled = bool(v["inventoryShrinkageEnabled"])
        if "inventoryShrinkageRate" in v: cfg.inventory.shrinkage.rate = float(v["inventoryShrinkageRate"])

        _state._cfg = cfg
        return {"ok": True}


@router.get("/download")
def download_config():
    with _state._cfg_lock:
        cfg = copy.deepcopy(_state._cfg)
    return cfg_to_dict(cfg)


# ---------------------------------------------------------------------------
# Config YAML editor (in-memory only, never writes to disk)
# ---------------------------------------------------------------------------

@router.get("/yaml")
def get_config_yaml():
    """Return the current in-memory config serialized as YAML text."""
    with _state._cfg_lock:
        text = yaml.safe_dump(cfg_to_dict(_state._cfg), sort_keys=False, default_flow_style=False)
    return Response(content=text, media_type="text/plain")


@router.get("/yaml/disk")
def get_config_yaml_disk():
    """Return the original config.yaml from disk."""
    with _state._cfg_lock:
        text = _state._cfg_disk_yaml
    return Response(content=text, media_type="text/plain")


@router.post("/yaml")
def update_config_yaml(body: ConfigYamlUpdate):
    """Parse YAML, normalize, replace in-memory config. Original file untouched."""
    text = body.yaml_text
    if len(text) > 1_048_576:
        raise HTTPException(413, "YAML text exceeds 1 MB limit")
    try:
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict):
            raise HTTPException(400, "Config YAML must be a mapping at the top level.")
    except yaml.YAMLError as e:
        raise HTTPException(400, f"Invalid YAML: {e}")

    try:
        normalized = normalize_config_yaml(parsed)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise HTTPException(400, f"Config normalization failed: {exc}")
    with _state._cfg_lock:
        _state._cfg = normalized
    return {"ok": True}


@router.post("/yaml/reset")
def reset_config_yaml():
    """Reload config from disk, discarding in-memory edits."""
    with _state._cfg_lock:
        _state._cfg = _base_config()
        _state._cfg_disk_yaml = _config_path.read_text(encoding="utf-8") if _config_path.exists() else ""
    return {"ok": True}
