"""
web/routes/models_routes.py -- All /api/models/* endpoints.
"""

from __future__ import annotations

import copy

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from web.shared_state import ConfigUpdate, _g, _models_path, _load_yaml
import web.shared_state as _state

router = APIRouter(prefix="/api/models", tags=["models"])


def _form_number(values: dict, field: str, converter):
    """Coerce one numeric form field and return a structured client error."""
    try:
        return converter(values[field])
    except (ValueError, TypeError, OverflowError) as exc:
        expected = "integer" if converter is int else "number"
        raise HTTPException(
            422,
            detail={
                "field": field,
                "message": f"Expected an {expected} value.",
            },
        ) from exc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ModelsUpdate(BaseModel):
    yaml_text: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GET /api/models -- raw YAML text
# ---------------------------------------------------------------------------

@router.get("")
def get_models():
    """Return the current models YAML as raw text for the editor."""
    with _state._cfg_lock:
        text = _state._models_yaml_text
    return Response(content=text, media_type="text/plain")


# ---------------------------------------------------------------------------
# POST /api/models -- replace models YAML
# ---------------------------------------------------------------------------

@router.post("")
def update_models(body: ModelsUpdate):
    """Parse and store updated models YAML in memory. Original file is untouched."""
    text = body.yaml_text
    if len(text) > 1_048_576:
        raise HTTPException(413, "YAML text exceeds 1 MB limit")
    try:
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict):
            raise HTTPException(400, "Models YAML must be a mapping at the top level.")
    except yaml.YAMLError as e:
        raise HTTPException(400, f"Invalid YAML: {e}")
    # Validate against the models schema so typos / invalid values surface here
    # (matching the config-YAML endpoint) rather than at generation time.
    try:
        from src.engine.config.config_schema import ModelsConfig
        ModelsConfig.from_raw_dict(parsed)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, f"Models validation failed: {exc}")
    with _state._cfg_lock:
        _state._models_cfg = parsed
        _state._models_yaml_text = text
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/models/reset
# ---------------------------------------------------------------------------

@router.post("/reset")
def reset_models():
    """Reload models from disk, discarding in-memory edits."""
    with _state._cfg_lock:
        _state._models_cfg = _load_yaml(_models_path)
        _state._models_yaml_text = _models_path.read_text(encoding="utf-8") if _models_path.exists() else ""
    return {"ok": True}


# ---------------------------------------------------------------------------
# Models form API (visual editor)
# ---------------------------------------------------------------------------

@router.get("/form")
def get_models_form():
    """Flat form fields for the models visual editor."""
    import copy
    with _state._cfg_lock:
        models_copy = copy.deepcopy(_state._models_cfg)
    # Use copy instead of live state to avoid races
    m = models_copy.get("models") if isinstance(models_copy, dict) else models_copy
    if not isinstance(m, dict):
        m = models_copy
    md = _g(m, "macro_demand", default={})
    ylf = _g(md, "year_level_factors", default={})
    qty = _g(m, "quantity", default={})
    prc = _g(m, "pricing", default={})
    inf = _g(prc, "inflation", default={})
    mkd = _g(prc, "markdown", default={})
    bp = _g(m, "brand_popularity", default={})
    ret = _g(m, "returns", default={})
    lag = _g(ret, "lag_days", default={})
    rqty = _g(ret, "quantity", default={})

    return {
        # Macro demand
        "trendPreset": str(md.get("trend") or ""),
        "demandMode": str(ylf.get("mode", "once")),
        "demandFactors": list(ylf.get("factor_values", None) or ylf.get("values", [])),
        "hasManualDemandFactors": bool(
            ylf.get("factor_values", None) or ylf.get("values", [])
        ),
        # Quantity
        "qtyLambda": float(qty.get("base_poisson_lambda", 1.7)),
        "qtyMin": int(qty.get("min_qty", 1)),
        "qtyMax": int(qty.get("max_qty", 8)),
        "qtyMonthly": list(qty.get("monthly_factors", [1.0] * 12)),
        "qtyNoise": float(qty.get("noise_sigma", 0.12)),
        # Pricing -- inflation
        "inflationRate": float(inf.get("annual_rate", 0.05)),
        "inflationVolatility": float(inf.get("month_volatility_sigma", 0.012)),
        "inflationClipMin": float((inf.get("factor_clip") or [1.0, 1.3])[0]),
        "inflationClipMax": float((inf.get("factor_clip") or [1.0, 1.3])[1]),
        # Pricing -- markdown
        "markdownEnabled": bool(mkd.get("enabled", True)),
        "markdownMaxPct": float(mkd.get("max_pct_of_price", 0.50)),
        "markdownMinNet": float(mkd.get("min_net_price", 0.01)),
        "markdownAllowNeg": bool(mkd.get("allow_negative_margin", False)),
        "markdownLadder": list(mkd.get("ladder", [])),
        # Brand popularity
        "brandEnabled": bool(bp.get("enabled", True)),
        "brandSeed": int(bp.get("seed", 123) or 123),
        "brandWinnerBoost": float(bp.get("winner_boost", 2.5)),
        "brandWeights": {},  # deprecated: kept for frontend compat
        # Returns (on/off lives in config.yaml -> returns.enabled, not here)
        "retReasons": list(ret.get("reasons", [])),
        "retLagDist": str(lag.get("distribution", "triangular")),
        "retLagMode": int(lag.get("mode", 7)),
        "retFullLinePct": float(rqty.get("full_line_probability", 0.85)),
    }


@router.post("/form")
def update_models_form(body: ConfigUpdate):
    """Apply partial form updates to the in-memory models config, re-serialize YAML."""
    with _state._cfg_lock:
        v = body.values
        # Mutate a deep copy and commit only after every update succeeds, so a
        # bad input (e.g. a non-numeric value) can't leave the live models
        # config half-applied or desynced from the serialized YAML text.
        models_cfg = copy.deepcopy(_state._models_cfg)
        m = models_cfg.get("models")
        if not isinstance(m, dict):
            m = models_cfg

        m.setdefault("macro_demand", {})
        m.setdefault("quantity", {})
        m.setdefault("pricing", {}).setdefault("inflation", {})
        m["pricing"].setdefault("markdown", {})
        m.setdefault("brand_popularity", {})
        m.setdefault("returns", {}).setdefault("lag_days", {})
        m["returns"].setdefault("quantity", {})

        # Coerce/apply form values. A bad value (e.g. a non-numeric in a
        # float field) is a client error -> 400, not a 500. Because we mutate
        # the deep copy above, a failure here leaves live state untouched.
        trend_changed = False
        try:
            # Macro demand
            if "trendPreset" in v:
                from src.utils.trend_presets import VALID_TRENDS

                trend = str(v["trendPreset"]).strip().lower()
                if trend not in VALID_TRENDS:
                    raise HTTPException(
                        422,
                        detail={
                            "field": "trendPreset",
                            "message": "Choose a supported business trend.",
                        },
                    )
                previous_trend = m["macro_demand"].get("trend")
                m["macro_demand"]["trend"] = trend
                if trend != previous_trend:
                    trend_changed = True
                    m["macro_demand"].pop("year_level_factors", None)
            if not trend_changed:
                factors = v.get("demandFactors")
                if isinstance(factors, list) and factors:
                    year_factors = m["macro_demand"].setdefault(
                        "year_level_factors",
                        {},
                    )
                    if "demandMode" in v:
                        year_factors["mode"] = v["demandMode"]
                    year_factors["values"] = [float(x) for x in factors]
                if v.get("clearManualDemandFactors"):
                    m["macro_demand"].pop("year_level_factors", None)

            # Quantity
            if "qtyLambda" in v: m["quantity"]["base_poisson_lambda"] = _form_number(v, "qtyLambda", float)
            if "qtyMin" in v: m["quantity"]["min_qty"] = _form_number(v, "qtyMin", int)
            if "qtyMax" in v: m["quantity"]["max_qty"] = _form_number(v, "qtyMax", int)
            if "qtyMonthly" in v and isinstance(v["qtyMonthly"], list):
                m["quantity"]["monthly_factors"] = [float(x) for x in v["qtyMonthly"]]
            if "qtyNoise" in v: m["quantity"]["noise_sigma"] = _form_number(v, "qtyNoise", float)

            # Pricing -- inflation
            if "inflationRate" in v: m["pricing"]["inflation"]["annual_rate"] = _form_number(v, "inflationRate", float)
            if "inflationVolatility" in v: m["pricing"]["inflation"]["month_volatility_sigma"] = _form_number(v, "inflationVolatility", float)
            if "inflationClipMin" in v or "inflationClipMax" in v:
                clip = m["pricing"]["inflation"].get("factor_clip", [1.0, 1.3])
                if "inflationClipMin" in v: clip[0] = _form_number(v, "inflationClipMin", float)
                if "inflationClipMax" in v: clip[1] = _form_number(v, "inflationClipMax", float)
                m["pricing"]["inflation"]["factor_clip"] = clip

            # Pricing -- markdown
            if "markdownEnabled" in v: m["pricing"]["markdown"]["enabled"] = bool(v["markdownEnabled"])
            if "markdownMaxPct" in v: m["pricing"]["markdown"]["max_pct_of_price"] = _form_number(v, "markdownMaxPct", float)
            if "markdownMinNet" in v: m["pricing"]["markdown"]["min_net_price"] = _form_number(v, "markdownMinNet", float)
            if "markdownAllowNeg" in v: m["pricing"]["markdown"]["allow_negative_margin"] = bool(v["markdownAllowNeg"])

            # Brand popularity
            if "brandEnabled" in v: m["brand_popularity"]["enabled"] = bool(v["brandEnabled"])
            if "brandSeed" in v: m["brand_popularity"]["seed"] = _form_number(v, "brandSeed", int)
            if "brandWinnerBoost" in v: m["brand_popularity"]["winner_boost"] = _form_number(v, "brandWinnerBoost", float)
            # brand_weights deprecated — ignore if sent by frontend

            # Returns (on/off lives in config.yaml -> returns.enabled, not here)
            if "retLagDist" in v: m["returns"]["lag_days"]["distribution"] = v["retLagDist"]
            if "retLagMode" in v: m["returns"]["lag_days"]["mode"] = _form_number(v, "retLagMode", int)
            if "retFullLinePct" in v: m["returns"]["quantity"]["full_line_probability"] = _form_number(v, "retFullLinePct", float)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, f"Invalid models form value: {exc}")

        # Commit only after every update succeeded; re-serialize so the YAML
        # editor stays in sync with the form state.
        new_text = yaml.safe_dump(models_cfg, sort_keys=False, default_flow_style=False)
        _state._models_cfg = models_cfg
        _state._models_yaml_text = new_text
        return {"ok": True, "yaml_text": new_text}
