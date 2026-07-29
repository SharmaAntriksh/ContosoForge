"""
web/routes/presets_routes.py -- /api/presets/* endpoints.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Literal

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.engine.config.config_schema import AppConfig, ModelsConfig
from src.utils.trend_presets import VALID_TRENDS
from web.shared_state import (
    PRESETS,
    apply_preset,
    build_presets_by_sales,
    _base_config,
    _set_promotions_total,
)
import web.shared_state as _state

router = APIRouter(prefix="/api/presets", tags=["presets"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PresetApply(BaseModel):
    name: str


class GuidedSetup(BaseModel):
    size: Literal[
        "quick-sample",
        "power-bi-demo",
        "medium-analytics",
        "large-stress-test",
    ]
    start_date: str
    end_date: str
    format: Literal["parquet", "csv", "deltaparquet"]
    catalog: Literal["contoso", "synthetic", "all"]
    trend: str
    feature_bundle: Literal["core", "analytics", "full"]


_GUIDED_SCALES = {
    "quick-sample": {
        "sales_rows": 100_000,
        "products": 2_517,
        "stores": 10,
        "promotions": 12,
    },
    "power-bi-demo": {
        "sales_rows": 2_000_000,
        "products": 7_930,
        "stores": 50,
        "promotions": 20,
    },
    "medium-analytics": {
        "sales_rows": 10_000_000,
        "products": 12_000,
        "stores": 200,
        "promotions": 39,
    },
    "large-stress-test": {
        "sales_rows": 100_000_000,
        "products": 25_000,
        "stores": 500,
        "promotions": 60,
    },
}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
def get_presets():
    if build_presets_by_sales is None:
        return {}
    return build_presets_by_sales()


@router.post("/apply")
def apply_preset_route(body: PresetApply):
    if apply_preset is None:
        raise HTTPException(400, "Presets module not available")
    if body.name not in PRESETS:
        raise HTTPException(404, f"Unknown preset: {body.name}")
    with _state._cfg_lock:
        apply_preset(_state._cfg, _base_config, body.name)
    return {"ok": True, "applied": body.name}


@router.post("/guided")
def apply_guided_setup(body: GuidedSetup):
    """Apply the five beginner choices atomically to in-memory state."""
    try:
        start = date.fromisoformat(body.start_date)
        end = date.fromisoformat(body.end_date)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={
                "field": "dateRange",
                "message": "Use valid ISO dates in YYYY-MM-DD format.",
            },
        ) from exc
    if end < start:
        raise HTTPException(
            422,
            detail={
                "field": "dateRange",
                "message": "End date must be on or after start date.",
            },
        )

    trend = body.trend.strip().lower()
    if trend not in VALID_TRENDS:
        raise HTTPException(
            422,
            detail={
                "field": "trend",
                "message": "Choose a supported business trend.",
            },
        )

    scale = _GUIDED_SCALES[body.size]
    month_count = (
        (end.year - start.year) * 12
        + end.month
        - start.month
        + 1
    )
    customer_count = max(
        1_000,
        round(scale["sales_rows"] / month_count / 1.5),
    )

    with _state._cfg_lock:
        cfg = _state._cfg.model_copy(deep=True)
        models_cfg = copy.deepcopy(_state._models_cfg)

        cfg.sales.total_rows = scale["sales_rows"]
        cfg.customers.total_customers = customer_count
        cfg.stores.num_stores = scale["stores"]
        cfg.products.num_products = scale["products"]
        cfg.products.catalog = body.catalog
        _set_promotions_total(cfg.promotions, scale["promotions"])
        cfg.defaults.dates.start = body.start_date
        cfg.defaults.dates.end = body.end_date
        cfg.sales.file_format = body.format

        analytics_enabled = body.feature_bundle in {"analytics", "full"}
        full_enabled = body.feature_bundle == "full"
        cfg.returns.enabled = analytics_enabled
        cfg.budget.enabled = analytics_enabled
        cfg.inventory.enabled = analytics_enabled
        cfg.subscriptions.enabled = full_enabled
        cfg.subscriptions.generate_bridge = full_enabled
        cfg.wishlists.enabled = full_enabled
        cfg.complaints.enabled = full_enabled
        if analytics_enabled:
            cfg.sales.skip_order_cols = False

        models = models_cfg.get("models")
        if not isinstance(models, dict):
            models = models_cfg
        macro = models.setdefault("macro_demand", {})
        manual_year_factors = macro.get("year_level_factors") or {}
        manual_factors_cleared = bool(
            manual_year_factors.get("values")
            or manual_year_factors.get("factor_values")
        )
        macro["trend"] = trend
        macro.pop("year_level_factors", None)

        # Validate both copies before committing either one.
        validated_cfg = AppConfig.model_validate(cfg.model_dump(mode="python"))
        ModelsConfig.from_raw_dict(models_cfg)

        _state._cfg = validated_cfg
        _state._models_cfg = models_cfg
        _state._models_yaml_text = yaml.safe_dump(
            models_cfg,
            sort_keys=False,
            default_flow_style=False,
        )

    return {
        "ok": True,
        "summary": {
            "sales_rows": scale["sales_rows"],
            "customers": customer_count,
            "products": scale["products"],
            "stores": scale["stores"],
            "format": body.format,
            "catalog": body.catalog,
            "trend": trend,
            "feature_bundle": body.feature_bundle,
            "manual_factors_cleared": manual_factors_cleared,
        },
    }
