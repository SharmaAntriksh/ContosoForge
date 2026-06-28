"""Pipeline orchestrator — loads config, applies overrides, runs dimensions + sales.

Type annotations follow the project convention of ``Dict[str, Any]`` for
config dicts.  More specific ``TypedDict`` definitions can be introduced
later as the config schema is formalised.
"""
from __future__ import annotations

import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from pprint import pprint
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from src.engine.config.config_loader import load_config, load_config_file
from src.engine.runners.dimensions_runner import generate_dimensions
from src.engine.runners.sales_runner import run_sales_pipeline
from src.exceptions import ConfigError, PipelineError
from src.utils.logging_utils import info, fail, fmt_sec


# ----------------------------
# Public API
# ----------------------------

@dataclass(frozen=True)
class PipelineOverrides:
    # Sales / output
    file_format: Optional[str] = None           # csv | parquet | deltaparquet | delta
    sales_rows: Optional[int] = None            # sales.total_rows
    workers: Optional[int] = None               # sales.workers
    chunk_size: Optional[int] = None            # sales.chunk_size
    skip_order_cols: Optional[bool] = None      # sales.skip_order_cols
    row_group_size: Optional[int] = None        # sales.row_group_size (parquet/deltaparquet only)

    # Global dates (defaults.dates)
    start_date: Optional[str] = None            # YYYY-MM-DD
    end_date: Optional[str] = None              # YYYY-MM-DD

    # Dimension sizes
    customers: Optional[int] = None             # customers.total_customers
    stores: Optional[int] = None                # stores.num_stores
    products: Optional[int] = None              # products.num_products
    promotions: Optional[int] = None            # "total" promotions, distributed across buckets when possible

    # SCD2 toggles
    products_scd2: Optional[bool] = None        # products.scd2.enabled
    customers_scd2: Optional[bool] = None       # customers.scd2.enabled



def _band_to_dict(b):
    """Coerce a band entry (Pydantic ``PriceBandEntry`` or dict) to a plain dict."""
    if hasattr(b, "model_dump"):
        return b.model_dump()
    return dict(b) if isinstance(b, Mapping) else b


def _inject_models_appearance(cfg, models_cfg) -> None:
    """Translate ``models.pricing.appearance`` into the product pricing format.

    This ensures product dimension generation and sales-time pricing_pipeline
    both use the same price/cost bands from a single source in models.yaml.
    Mutates ``cfg.products.pricing["appearance"]`` in place.

    Both ``models_cfg`` and the nested sections are ``_Base`` Pydantic models
    (registered as virtual ``Mapping``), so ``.get()`` works uniformly here.
    """
    pricing = models_cfg.get("pricing") if isinstance(models_cfg, Mapping) else None
    if not isinstance(pricing, Mapping):
        return
    appearance = pricing.get("appearance") if isinstance(pricing, Mapping) else None
    if not isinstance(appearance, Mapping):
        return

    products = cfg.products
    if not isinstance(products, Mapping):
        return
    prod_pricing = products.pricing
    if not isinstance(prod_pricing, Mapping):
        return

    up_cfg = appearance.get("unit_price", {}) or {}
    uc_cfg = appearance.get("unit_cost", {}) or {}

    # Extract the first ending value (product pricing uses a single float)
    endings = up_cfg.get("endings")
    ending = 0.99
    if isinstance(endings, list) and endings:
        first = endings[0]
        if isinstance(first, Mapping):
            ending = float(first.get("value", 0.99))

    # Coerce band entries to plain dicts so cfg.products.pricing stays a pure
    # dict tree. Leaving Pydantic PriceBandEntry instances in place forces the
    # products version hash onto the str() fallback in version_store
    # (json.dumps can't serialize them) instead of the canonical-JSON path.
    price_bands = [_band_to_dict(b) for b in (up_cfg.get("bands") or [])]
    cost_bands = [_band_to_dict(b) for b in (uc_cfg.get("bands") or [])]

    prod_pricing["appearance"] = {
        "snap_unit_price": bool(appearance.get("enabled", True)),
        "price_ending": ending,
        "price_bands": price_bands,
        "round_unit_cost": bool(cost_bands),
        "cost_bands": cost_bands,
    }


def run_pipeline(
    *,
    config_path: str = "config.yaml",
    models_config_path: str = "models.yaml",
    only: Optional[str] = None,                 # None | "dimensions" | "sales"
    clean: bool = False,
    dry_run: bool = False,
    regen_dimensions: Optional[Iterable[str]] = None,
    overrides: Optional[PipelineOverrides] = None,
    report: bool = True,
) -> Dict[str, Any]:
    """
    Shared pipeline runner (CLI + Streamlit UI friendly).

    - Loads config + models config
    - Applies overrides (sales, global dates, dimension sizes)
    - Forces FX dimension to follow global dates
    - Runs dimensions and/or sales pipelines
    - Optionally resets scratch fact output (packaging.reset_scratch_fact_out)
    - Optionally cleans scratch fact output at end (packaging.clean_scratch_fact_out)

    Returns a small run summary dict. Raises on failures.
    """
    if only not in (None, "dimensions", "sales"):
        raise ConfigError("only must be one of: None, 'dimensions', 'sales'")

    overrides = _normalize_overrides(overrides or PipelineOverrides())
    force_regenerate: Set[str] = set(regen_dimensions) if regen_dimensions else set()

    start_ts = time.time()

    try:
        # ----------------------------
        # Load configs
        # ----------------------------
        cfg_raw = load_config(config_path)
        if not isinstance(cfg_raw.sales, Mapping):
            raise ConfigError("Config must contain a 'sales' section")

        from src.engine.config.config_schema import ModelsConfig
        models_raw = load_config_file(models_config_path)
        if "models" not in models_raw or not isinstance(models_raw["models"], Mapping):
            raise ConfigError("models.yaml must contain a top-level 'models' section")
        models_validated = ModelsConfig.from_raw_dict(models_raw)
        models_cfg = models_validated.models

        # Deep-copy so repeated Streamlit runs don't mutate the original.
        cfg = cfg_raw.model_copy(deep=True) if hasattr(cfg_raw, "model_copy") else dict(cfg_raw)
        sales_cfg = cfg.sales

        # Snapshot config files at pipeline start so packaging preserves the
        # exact config that was used, even if the user edits the files mid-run.
        _config_resolved = _resolve_input_path(config_path)
        _models_resolved = _resolve_input_path(models_config_path)
        cfg.config_yaml_path = str(_config_resolved)
        cfg.model_yaml_path = str(_models_resolved)
        cfg["_config_snapshot"] = Path(_config_resolved).read_bytes()
        cfg["_models_snapshot"] = Path(_models_resolved).read_bytes()

        # ----------------------------
        # Apply overrides (copy-on-write)
        # ----------------------------
        cfg, sales_cfg = _apply_overrides(cfg, sales_cfg, overrides)

        # FX always follows global dates (enforced by resolve_fx_dates at generation time)

        # ----------------------------
        # Dry run
        # ----------------------------
        if dry_run:
            info("Dry run enabled. Resolved configuration:")
            _display = cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg)
            _display.pop("_config_snapshot", None)
            _display.pop("_models_snapshot", None)
            pprint(_display)
            return {
                "ok": True,
                "dry_run": True,
                "only": only,
                "force_regenerate": sorted(force_regenerate),
                "config_yaml_path": cfg.config_yaml_path,
                "model_yaml_path": cfg.model_yaml_path,
                "elapsed_sec": time.time() - start_ts,
            }

        # ----------------------------
        # Optional clean (final output root)
        # ----------------------------
        if clean:
            _clean_final_outputs(cfg)

        # ----------------------------
        # Resolve required paths
        # ----------------------------
        parquet_dims, fact_out = _resolve_required_paths(sales_cfg)

        parquet_dims.mkdir(parents=True, exist_ok=True)
        fact_out.mkdir(parents=True, exist_ok=True)

        # ----------------------------
        # Scratch output handling (reset/cleanup)
        # ----------------------------
        packaging_cfg = cfg.packaging
        reset_scratch = bool(packaging_cfg.reset_scratch_fact_out)
        clean_scratch = bool(packaging_cfg.clean_scratch_fact_out)

        if reset_scratch:
            info(f"Resetting fact output folder: {fact_out}")
            if fact_out.exists():
                try:
                    shutil.rmtree(fact_out)
                except OSError as e:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Could not fully remove %s: %s", fact_out, e
                    )
            fact_out.mkdir(parents=True, exist_ok=True)
        else:
            info(f"Keeping existing fact_out folder (packaging.reset_scratch_fact_out=false): {fact_out}")
            fact_out.mkdir(parents=True, exist_ok=True)

        # ----------------------------
        # Resolve trend preset (injects macro demand, lifecycle, and customers)
        # ----------------------------
        from src.utils.trend_presets import resolve_trend_preset
        _profile_name = getattr(getattr(cfg, "customers", None), "profile", None)
        resolve_trend_preset(models_cfg, cfg=cfg, profile_name=_profile_name)

        # ----------------------------
        # Attach models config to runtime state (ONLY if sales will run)
        # ----------------------------
        if only != "dimensions":
            from src.facts.sales.sales_logic import State
            State.models_cfg = models_cfg

        # ----------------------------
        # Inject models.pricing.appearance into products config so both
        # dimension generation and sales use the same price grid.
        # ----------------------------
        _inject_models_appearance(cfg, models_cfg)

        # ----------------------------
        # Run pipelines
        # ----------------------------
        from src.utils.config_precedence import _is_random_mode
        if _is_random_mode(cfg):
            info("Random mode ON — all seeds will use OS entropy (non-deterministic run).")
        info("Starting full pipeline.")
        dim_summary = None

        if only != "sales":
            dim_summary = generate_dimensions(cfg, parquet_dims, force_regenerate=force_regenerate)

        if only != "dimensions":
            # Config-level quality_report toggle (CLI --no-report overrides)
            _report = report and bool(getattr(sales_cfg, "quality_report", True))
            run_sales_pipeline(sales_cfg, fact_out, parquet_dims, cfg, report=_report)

        # ----------------------------
        # Final cleanup (scratch)
        # ----------------------------
        if clean_scratch:
            info(f"Cleaning scratch fact_out folder: {fact_out}")
            try:
                shutil.rmtree(fact_out)
            except OSError as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Could not fully remove %s: %s", fact_out, e
                )
        else:
            info(f"Keeping scratch fact_out folder (packaging.clean_scratch_fact_out=false): {fact_out}")

        elapsed = time.time() - start_ts
        info(f"All pipelines completed in {fmt_sec(elapsed)}.")

        return {
            "ok": True,
            "dry_run": False,
            "only": only,
            "force_regenerate": sorted(force_regenerate),
            "parquet_dims": str(parquet_dims),
            "fact_out_scratch": str(fact_out),
            "config_yaml_path": cfg.config_yaml_path,
            "model_yaml_path": cfg.model_yaml_path,
            "reset_scratch_fact_out": reset_scratch,
            "clean_scratch_fact_out": clean_scratch,
            "dimensions": dim_summary,
            "elapsed_sec": elapsed,
        }

    except (PipelineError, OSError, KeyError, ValueError, TypeError) as ex:
        fail(str(ex))
        raise


# ----------------------------
# Internals
# ----------------------------

def _normalize_overrides(overrides: PipelineOverrides) -> PipelineOverrides:
    # Normalize format alias
    ff = overrides.file_format
    if ff is not None:
        ff = str(ff).strip().lower()
        if ff == "delta":
            ff = "deltaparquet"
    return PipelineOverrides(**{**overrides.__dict__, "file_format": ff})


def _apply_overrides(cfg, sales_cfg, overrides: PipelineOverrides):
    """
    Apply overrides in a copy-on-write style:
      - cfg and cfg.sales are already deep-copied by caller
      - this function only mutates the copies
    """
    # Sales overrides
    if overrides.file_format:
        sales_cfg.file_format = overrides.file_format

    if overrides.sales_rows is not None:
        sales_cfg.total_rows = int(overrides.sales_rows)

    if overrides.workers is not None:
        sales_cfg.workers = int(overrides.workers)

    if overrides.chunk_size is not None:
        sales_cfg.chunk_size = int(overrides.chunk_size)

    if overrides.skip_order_cols is not None:
        sales_cfg.skip_order_cols = bool(overrides.skip_order_cols)

    if overrides.row_group_size is not None:
        fmt = str(sales_cfg.file_format or "").lower()
        if fmt not in ("parquet", "deltaparquet"):
            fail("--row-group-size is only valid for parquet or deltaparquet output")
            raise ConfigError("row_group_size only valid for parquet/deltaparquet")
        sales_cfg.row_group_size = int(overrides.row_group_size)

    # Global dates overrides
    _ensure_defaults_dates(cfg)
    if overrides.start_date:
        cfg.defaults.dates.start = overrides.start_date
    if overrides.end_date:
        cfg.defaults.dates.end = overrides.end_date

    # Dimension size overrides (cfg is already deep-copied, safe to mutate)
    if overrides.customers is not None:
        cfg.customers.total_customers = int(overrides.customers)

    if overrides.stores is not None:
        n = int(overrides.stores)
        cfg.stores.num_stores = n
        cfg.stores.total_stores = n  # back-compat

    if overrides.products is not None:
        cfg.products.num_products = int(overrides.products)

    if overrides.promotions is not None:
        _apply_promotions_total(cfg.promotions, int(overrides.promotions))

    if overrides.products_scd2 is not None:
        if cfg.products.scd2 is None:
            from src.engine.config.config_schema import ProductsSCD2Config
            cfg.products.scd2 = ProductsSCD2Config()
        cfg.products.scd2.enabled = bool(overrides.products_scd2)
    if overrides.customers_scd2 is not None:
        if cfg.customers.scd2 is None:
            from src.engine.config.config_schema import CustomersSCD2Config
            cfg.customers.scd2 = CustomersSCD2Config()
        cfg.customers.scd2.enabled = bool(overrides.customers_scd2)

    return cfg, sales_cfg



def _clean_final_outputs(cfg) -> None:
    info("Cleaning final output folders before run.")
    gen_root = cfg.defaults.final_output
    if gen_root:
        try:
            shutil.rmtree(gen_root)
        except OSError as e:
            import logging
            logging.getLogger(__name__).warning(
                "Could not fully remove %s: %s", gen_root, e
            )


def _resolve_required_paths(sales_cfg) -> Tuple[Path, Path]:
    pf = sales_cfg.parquet_folder
    of = sales_cfg.out_folder
    if not pf or not of:
        fail("sales.parquet_folder and sales.out_folder must be set in config")
        raise ConfigError("Missing sales.parquet_folder/out_folder")

    parquet_dims = Path(pf).resolve()
    fact_out = Path(of).resolve()
    return parquet_dims, fact_out


def _resolve_input_path(p: str) -> Path:
    """
    Resolve an input file path robustly:
    - expanduser
    - if absolute or exists relative to CWD, use that
    - else try relative to repo root (one level above /src)
    """
    raw = Path(p).expanduser()

    if raw.is_absolute() and raw.exists():
        return raw.resolve()

    cwd_candidate = (Path.cwd() / raw).resolve()
    if cwd_candidate.exists():
        return cwd_candidate

    # Find repo root by locating ".../src" in this file path and taking its parent
    here = Path(__file__).resolve()
    src_dir = None
    for parent in here.parents:
        if parent.name == "src":
            src_dir = parent
            break
    if src_dir is not None:
        repo_root = src_dir.parent
    elif len(here.parents) > 3:
        repo_root = here.parents[3]
    else:
        repo_root = here.parent

    return (repo_root / raw).resolve()


def _ensure_defaults_dates(cfg) -> None:
    """
    Ensure cfg has canonical defaults.dates for overrides.
    With typed AppConfig, defaults and dates are always initialized.
    """
    # AppConfig always has defaults.dates with GlobalDatesConfig defaults
    # This is a no-op guard for safety
    if cfg.defaults is None:
        from src.engine.config.config_schema import DefaultsConfig
        cfg.defaults = DefaultsConfig()
    if cfg.defaults.dates is None:
        from src.engine.config.config_schema import GlobalDatesConfig
        cfg.defaults.dates = GlobalDatesConfig()


def _apply_promotions_total(promotions_cfg, total: int) -> None:
    """
    Config has eight promotion buckets (num_seasonal, num_clearance,
    num_limited, num_flash, num_volume, num_loyalty, num_bundle,
    num_new_customer).

    If those exist, scale them proportionally to match `total`.
    Otherwise, store a back-compat 'total_promotions' key.
    """
    total = max(0, int(total))

    keys = ("num_seasonal", "num_clearance", "num_limited", "num_flash", "num_volume", "num_loyalty", "num_bundle", "num_new_customer")
    for k in keys:
        val = getattr(promotions_cfg, k, None)
        if val is not None and int(val) < 0:
            raise ConfigError(f"promotions.{k} must be non-negative, got {val}")
    if all(getattr(promotions_cfg, k, None) is not None and isinstance(getattr(promotions_cfg, k), (int, float)) for k in keys):
        current = sum(int(getattr(promotions_cfg, k)) for k in keys)
        if current <= 0:
            base = [1] * len(keys)
            current = len(keys)
        else:
            base = [int(getattr(promotions_cfg, k)) for k in keys]

        scaled = [b * total / current for b in base]
        floors = [int(x) for x in scaled]
        remainder = total - sum(floors)

        fracs = [(i, scaled[i] - floors[i]) for i in range(len(keys))]
        fracs.sort(key=lambda t: t[1], reverse=True)

        # Each bucket can receive at most +1 from rounding; cap to len(fracs)
        # to prevent any bucket from getting +2 due to modulo wrap-around.
        for i in range(min(remainder, len(fracs))):
            floors[fracs[i][0]] += 1

        for i, k in enumerate(keys):
            object.__setattr__(promotions_cfg, k, floors[i])
        promotions_cfg.total_promotions = total
    else:
        promotions_cfg.total_promotions = total
