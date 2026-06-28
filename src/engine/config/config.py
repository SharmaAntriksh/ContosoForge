from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date as _date
from datetime import datetime as _datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from src.dimensions.geography import normalize_geography_config
from src.exceptions import ConfigError

# ---------------------------------------------------------------------------
# Lazy YAML import (resolved once, avoids per-call try/except overhead)
# ---------------------------------------------------------------------------

try:
    import yaml as _yaml  # type: ignore
except ImportError:
    _yaml = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Format-specific key sets (defined before normalizers that reference them)
# ---------------------------------------------------------------------------

_PARQUET_ONLY_KEYS: frozenset[str] = frozenset(
    {"row_group_size", "compression", "chunk_size", "workers"}
)
_PARQUET_MERGE_ONLY_KEYS: frozenset[str] = frozenset(
    {"merge_parquet", "merged_file"}
)
_DELTA_ONLY_KEYS: frozenset[str] = frozenset(
    {"partition_enabled", "partition_cols", "delta_output_folder"}
)

_VALID_FILE_FORMATS: frozenset[str] = frozenset(
    {"csv", "parquet", "deltaparquet", "delta"}
)


# ---------------------------------------------------------------------------
# Section normalization registry (single source of truth)
# ---------------------------------------------------------------------------

SectionNormalizer = Callable[[Dict[str, Any]], Dict[str, Any]]

# Sections that must be mappings if present but do not have a normalizer.
# Keep this small; normalizer keys are automatically treated as mapping sections.
_SECTION_MAPPING_ONLY_KEYS: frozenset[str] = frozenset({"scale"})

# For config files that intentionally don't have defaults (e.g. models.yaml),
# we keep normalization deliberately narrow to avoid surprising validation failures.
_CONFIG_FILE_NORMALIZE_KEYS: frozenset[str] = frozenset()

# Populated inline after normalizer functions are defined (see bottom of file).
_SECTION_NORMALIZERS: Dict[str, SectionNormalizer] = {}


# ---------------------------------------------------------------------------
# Lightweight helpers
# ---------------------------------------------------------------------------

def _clamp01(x: float) -> float:
    """Clamp *x* to the [0.0, 1.0] interval."""
    return max(0.0, min(1.0, float(x)))


def _parse_date(value: Any, label: str) -> _date:
    """Parse a date value into a :class:`datetime.date`.

    Accepts ``datetime.date``, ``datetime.datetime``, and common ISO-style
    strings (``YYYY-MM-DD``, ``YYYY/MM/DD``).  Raises :class:`ValueError`
    with a clear message on failure.
    """
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, _date):
        return value

    text = str(value).strip()
    # Try ISO first (fast path), then slash-separated variant
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return _datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ConfigError(f"Cannot parse {label} date '{value}' (expected YYYY-MM-DD)")


def _coerce_optional_int(cfg: Dict[str, Any], key: str, section: str = "") -> None:
    """Cast *cfg[key]* to ``int`` in place if it is present and non-None."""
    if key in cfg and cfg[key] is not None:
        try:
            val = int(cfg[key])
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"{section}.{key} must be an integer, got {cfg[key]!r}") from exc
        cfg[key] = val


def _coerce_optional_ratio(
    cfg: Dict[str, Any], key: str, section: str,
) -> None:
    """Cast and validate *cfg[key]* as a float in [0, 1] if present and non-None."""
    if key in cfg and cfg[key] is not None:
        try:
            val = float(cfg[key])
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"{section}.{key} must be a number, got {cfg[key]!r}") from exc
        if not 0.0 <= val <= 1.0:
            raise ConfigError(f"{section}.{key} must be between 0 and 1, got {val}")
        cfg[key] = val


def _coerce_optional_positive_int(
    cfg: Dict[str, Any], key: str, section: str
) -> None:
    """Cast and validate *cfg[key]* as a positive int if present and non-None."""
    if key in cfg and cfg[key] is not None:
        try:
            val = int(cfg[key])
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"{section}.{key} must be an integer, got {cfg[key]!r}") from exc
        if val <= 0:
            raise ConfigError(f"{section}.{key} must be a positive integer, got {val}")
        cfg[key] = val


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(path: str | Path = "config.yaml"):
    """Load the *pipeline* config (config.yaml) and normalize safely.

    This loader REQUIRES defaults (or _defaults) to exist because the pipeline
    depends on a global date window for lifecycle-aware generation.

    Returns a typed :class:`AppConfig` Pydantic model that also supports
    dict-style access (``cfg["key"]``, ``cfg.get()``, etc.) via the
    dict-compatibility mixin.
    """
    from src.engine.config.config_schema import AppConfig

    raw = _load_and_normalize(
        path=path,
        require_defaults=True,
        normalize_keys=None,  # normalize all registered sections
    )
    return AppConfig.from_raw_dict(raw)


# Backward-compatible aliases
load_pipeline_config = load_config
load_config_typed = load_config


def load_config_file(path: str | Path) -> dict:
    """Load a config file (YAML/JSON) that intentionally does NOT contain defaults,
    e.g. models.yaml (top-level ``models`` only).

    This applies only very safe, non-default-dependent transforms:
      - tuning → models mapping (if present)
    """
    return _load_and_normalize(
        path=path,
        require_defaults=False,
        normalize_keys=_CONFIG_FILE_NORMALIZE_KEYS,
    )



# ---------------------------------------------------------------------------
# Load + normalize (shared implementation)
# ---------------------------------------------------------------------------

def _load_and_normalize(
    path: str | Path,
    *,
    require_defaults: bool,
    normalize_keys: Optional[frozenset[str] | set[str]],
) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    cfg: Dict[str, Any] = _load_any(path)

    # Distribute scale block into per-section keys (new config format)
    cfg = _distribute_scale(cfg)

    # Flatten sales.advanced into sales (new nesting → old flat keys)
    cfg = _flatten_sales_advanced(cfg)

    # Expand new compact config formats into old internal keys
    cfg = _expand_merge_block(cfg)
    cfg = _expand_partition_by(cfg)
    cfg = _expand_region_mix(cfg)
    cfg = _strip_deprecated_keys(cfg)
    cfg = _fold_facts_enabled(cfg)

    # Expand simplified product pricing knobs into full pricing dict
    cfg = _expand_products_pricing(cfg)

    # Ensure packaging exists (defaults for pipeline runner)
    cfg.setdefault("packaging", {
        "reset_scratch_fact_out": True,
        "clean_scratch_fact_out": True,
    })

    # Pipeline config requires defaults for global date window
    if require_defaults:
        cfg = normalize_defaults(cfg)

    # Normalize/validate known sections
    cfg = _normalize_sections(cfg, normalize_keys=normalize_keys)

    # Cross-section business-rule validation (only for pipeline config)
    if require_defaults:
        cfg = apply_cross_section_rules(cfg)

    return cfg


def _distribute_scale(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Copy ``scale`` counts into per-section keys (section-level values win)."""
    scale = cfg.get("scale")
    if not isinstance(scale, Mapping):
        return cfg

    _map = [
        ("sales_rows",  "sales",      "total_rows"),
        ("customers",   "customers",  "total_customers"),
        ("stores",      "stores",     "num_stores"),
    ]
    for scale_key, section, target_key in _map:
        v = scale.get(scale_key)
        if v is not None:
            sec = cfg.setdefault(section, {})
            if isinstance(sec, Mapping):
                sec.setdefault(target_key, v)

    # Products: supports both flat int (backward compat) and dict with rows/catalog
    prod_scale = scale.get("products")
    if prod_scale is not None:
        sec = cfg.setdefault("products", {})
        if isinstance(sec, Mapping):
            if isinstance(prod_scale, Mapping):
                if "rows" in prod_scale:
                    sec.setdefault("num_products", prod_scale["rows"])
                if "catalog" in prod_scale:
                    sec.setdefault("catalog", prod_scale["catalog"])
            else:
                sec.setdefault("num_products", prod_scale)

    # Promotions: { seasonal: N, clearance: N, limited: N }
    promos = scale.get("promotions")
    if isinstance(promos, Mapping):
        sec = cfg.setdefault("promotions", {})
        if isinstance(sec, Mapping):
            _promo_map = {
                "seasonal": "num_seasonal",
                "clearance": "num_clearance",
                "limited": "num_limited",
                "flash": "num_flash",
                "volume": "num_volume",
                "loyalty": "num_loyalty",
                "bundle": "num_bundle",
                "new_customer": "num_new_customer",
            }
            for scale_key, cfg_key in _promo_map.items():
                v = promos.get(scale_key)
                if v is not None:
                    sec.setdefault(cfg_key, v)

    return cfg


def _flatten_sales_advanced(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Promote ``sales.advanced.*`` to ``sales.*`` for backward compatibility."""
    sales = cfg.get("sales")
    if not isinstance(sales, Mapping):
        return cfg
    adv = sales.pop("advanced", None)
    if isinstance(adv, Mapping):
        for k, v in adv.items():
            sales.setdefault(k, v)

    # Ensure derived paths exist
    sales.setdefault("parquet_folder", "./data/parquet_dims")
    sales.setdefault("out_folder", "./data/fact_out")
    sales.setdefault("delta_output_folder", "./data/fact_out/delta")
    return cfg


def _expand_merge_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Expand ``sales.merge`` block into flat keys the rest of the code expects.

    New format::

        sales:
          merge:
            enabled: true
            file: "sales.parquet"
            delete_chunks: true

    Produces ``sales.merge_parquet``, ``sales.merged_file``,
    ``sales.delete_chunks``.  Old flat keys win if already present.
    """
    sales = cfg.get("sales")
    if not isinstance(sales, Mapping):
        return cfg
    merge = sales.pop("merge", None)
    if not isinstance(merge, Mapping):
        return cfg

    sales.setdefault("merge_parquet", bool(merge.get("enabled", True)))
    sales.setdefault("merged_file", merge.get("file", "sales.parquet"))
    sales.setdefault("delete_chunks", bool(merge.get("delete_chunks", False)))
    return cfg


def _expand_partition_by(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Expand ``sales.partition_by`` shorthand into the nested block.

    New format::

        sales:
          partition_by: ["Year", "Month"]   # null or [] to disable

    Produces ``sales.partitioning.enabled`` + ``sales.partitioning.columns``.
    The existing ``partitioning`` block wins if already present.
    """
    sales = cfg.get("sales")
    if not isinstance(sales, Mapping):
        return cfg
    if "partitioning" in sales:
        return cfg
    part_by = sales.pop("partition_by", None)
    if part_by is None:
        return cfg

    if isinstance(part_by, list) and part_by:
        sales["partitioning"] = {"enabled": True, "columns": [str(c) for c in part_by]}
    else:
        sales["partitioning"] = {"enabled": False, "columns": []}
    return cfg


def _expand_region_mix(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Expand ``customers.region_mix`` map into flat ``pct_*`` keys.

    New format::

        customers:
          region_mix: { US: 51, EU: 39, India: 10 }
          org_pct: 1

    Produces ``pct_us``, ``pct_eu``, ``pct_india``, ``pct_asia``,
    ``pct_org``.  Old flat keys win if already present.
    """
    cust = cfg.get("customers")
    if not isinstance(cust, Mapping):
        return cfg
    region_mix = cust.pop("region_mix", None)
    if not isinstance(region_mix, Mapping):
        return cfg

    _REGION_MAP = {
        "us": "pct_us", "usa": "pct_us", "united states": "pct_us", "americas": "pct_us",
        "eu": "pct_eu", "europe": "pct_eu",
        "india": "pct_india",
        "asia": "pct_asia", "asia-pacific": "pct_asia", "apac": "pct_asia",
    }
    unknown_regions = []
    for name, pct in region_mix.items():
        flat_key = _REGION_MAP.get(str(name).lower())
        if flat_key:
            cust.setdefault(flat_key, float(pct))
        else:
            unknown_regions.append(name)
    if unknown_regions:
        raise ConfigError(
            f"Unknown region(s) in customers.region_mix: {unknown_regions}. "
            f"Valid regions: Americas, Europe, India, Asia-Pacific "
            f"(aliases: us, usa, eu, apac)"
        )

    cust.setdefault("pct_us", 0.0)
    cust.setdefault("pct_eu", 0.0)
    cust.setdefault("pct_india", 0.0)
    cust.setdefault("pct_asia", 0.0)

    org_pct = cust.pop("org_pct", None)
    if org_pct is not None:
        cust.setdefault("pct_org", float(org_pct))

    return cfg


def _strip_deprecated_keys(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Remove config keys that were removed in the static-model simplification."""
    # StoreClosingConfig: transfer_share, notice_days, prefer_same_district, ramp_days, ramp_start_factor
    closing = cfg.get("stores", {}).get("closing") if isinstance(cfg.get("stores"), Mapping) else None
    if isinstance(closing, Mapping):
        for k in ("transfer_share", "notice_days", "prefer_same_district", "ramp_days", "ramp_start_factor"):
            closing.pop(k, None)
    # EmployeesConfig: termination_rate
    emp = cfg.get("employees")
    if isinstance(emp, Mapping):
        emp.pop("termination_rate", None)
    return cfg


def _fold_facts_enabled(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Fold legacy ``facts.enabled`` list into per-section ``enabled`` flags.

    If ``facts.enabled`` is ``["sales", "returns"]`` and ``returns.enabled``
    is not explicitly set, this sets ``returns.enabled = True``.  If
    ``facts.enabled`` exists but "returns" is absent from the list, this
    forces ``returns.enabled = False``.

    After folding, the ``facts`` section is removed so downstream code only
    needs to check per-section ``enabled`` flags.
    """
    facts = cfg.get("facts")
    if not isinstance(facts, Mapping):
        if isinstance(facts, list):
            facts = {"enabled": facts}
        else:
            cfg.pop("facts", None)
            return cfg

    enabled_list = facts.get("enabled")
    if isinstance(enabled_list, list) and enabled_list:
        names = {str(x).strip().lower() for x in enabled_list}

        returns_cfg = cfg.get("returns")
        if isinstance(returns_cfg, Mapping):
            if "enabled" not in returns_cfg:
                returns_cfg["enabled"] = "returns" in names
            elif returns_cfg.get("enabled"):
                returns_cfg["enabled"] = "returns" in names
        elif "returns" in names:
            cfg.setdefault("returns", {})["enabled"] = True

    cfg.pop("facts", None)
    return cfg


def _expand_products_pricing(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Expand simplified ``products`` knobs into the full ``products.pricing`` dict.

    Simplified config::

        products:
          value_scale: 1
          price_range: [10, 3000]
          margin_range: [0.20, 0.35]
          brand_normalize: true

    Expands to the structure that ``apply_product_pricing()`` expects,
    with hardcoded appearance bands and sensible defaults.
    Section-level ``products.pricing`` wins if already present (backward compat).
    """
    products = cfg.get("products")
    if not isinstance(products, Mapping):
        return cfg

    # If full pricing block already exists, leave it alone
    if isinstance(products.get("pricing"), Mapping) and products["pricing"]:
        return cfg

    pr = products.get("price_range", [10, 3000])
    if not isinstance(pr, (list, tuple)) or len(pr) < 2:
        raise ConfigError(f"products.price_range must be a 2-element list, got {pr!r}")
    min_price, max_price = float(pr[0]), float(pr[1])
    if min_price < 0:
        raise ConfigError(f"products.price_range minimum must be >= 0, got {min_price}")
    if max_price <= min_price:
        raise ConfigError(
            f"products.price_range maximum ({max_price}) must be > minimum ({min_price})"
        )

    mr = products.get("margin_range", [0.20, 0.35])
    if not isinstance(mr, (list, tuple)) or len(mr) < 2:
        raise ConfigError(f"products.margin_range must be a 2-element list, got {mr!r}")
    min_margin, max_margin = float(mr[0]), float(mr[1])
    if not (0.0 <= min_margin <= 1.0) or not (0.0 <= max_margin <= 1.0):
        raise ConfigError(
            f"products.margin_range values must be between 0 and 1, "
            f"got [{min_margin}, {max_margin}]"
        )
    if max_margin < min_margin:
        raise ConfigError(
            f"products.margin_range maximum ({max_margin}) must be >= minimum ({min_margin})"
        )

    brand_norm = bool(products.get("brand_normalize", True))
    brand_norm_alpha = float(products.get("brand_normalize_alpha", 0.35))
    value_scale = float(products.get("value_scale", 1))

    products["pricing"] = {
        "base": {
            "value_scale": value_scale,
            "min_unit_price": min_price,
            "max_unit_price": max_price,
            "rescale_to_range": True,
        },
        "cost": {
            "mode": "margin",
            "min_margin_pct": min_margin,
            "max_margin_pct": max_margin,
        },
        "brand_normalization": {
            "enabled": brand_norm,
            "brand_col": "Brand",
            "alpha": max(0.0, min(1.0, brand_norm_alpha)),
            "min_factor": 0.60,
            "max_factor": 1.60,
            "min_count": 10,
            "noise_sd": 0.0,
        },
        "appearance": {
            "snap_unit_price": True,
            "price_ending": 0.99,
            "price_bands": [
                {"max": 100,  "step": 5},
                {"max": 500,  "step": 10},
                {"max": 2000, "step": 25},
                {"max": 5000, "step": 50},
                {"max": 1e18, "step": 100},
            ],
            "round_unit_cost": True,
            "cost_bands": [
                {"max": 100,   "step": 5},
                {"max": 500,   "step": 10},
                {"max": 2000,  "step": 25},
                {"max": 10000, "step": 50},
                {"max": 1e18,  "step": 100},
            ],
        },
    }

    return cfg


def _normalize_sections(
    cfg: Dict[str, Any],
    *,
    normalize_keys: Optional[frozenset[str] | set[str]],
) -> Dict[str, Any]:
    """Validate section shapes and apply per-section normalizers.

    - Always validates known mapping sections if present.
    - Applies normalizers for sections in *normalize_keys* (or all registered
      normalizers if ``None``).
    """
    cfg = dict(cfg)

    # Validate mapping sections (mapping-only keys + all normalizer keys)
    mapping_keys = _SECTION_MAPPING_ONLY_KEYS | _SECTION_NORMALIZERS.keys()
    for key in mapping_keys:
        if key in cfg and not isinstance(cfg[key], Mapping):
            raise ConfigError(f"Invalid '{key}' section in config (expected mapping)")

    # Decide which normalizers to run
    if normalize_keys is None:
        keys_to_normalize = set(_SECTION_NORMALIZERS.keys())
    else:
        requested = set(normalize_keys)
        keys_to_normalize = requested & _SECTION_NORMALIZERS.keys()
        unknown = requested - _SECTION_NORMALIZERS.keys()
        if unknown:
            import logging
            logging.getLogger(__name__).warning(
                "normalize_keys %s not in registry, skipping", unknown
            )

    for key in keys_to_normalize:
        if key in cfg:
            cfg[key] = _SECTION_NORMALIZERS[key](cfg[key])  # type: ignore[arg-type]

    return cfg


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_any(path: Path) -> dict:
    """Load a YAML or JSON config file.

    When the extension is known (``.yaml``, ``.yml``, ``.json``) the matching
    parser is used directly.  For unknown extensions the file is tried as YAML
    first (structural errors are **not** swallowed) then as JSON.
    """
    ext = path.suffix.lower()

    if ext in (".yaml", ".yml"):
        return _load_yaml(path)

    if ext == ".json":
        return _load_json(path)

    # Unknown extension – try YAML then JSON, but only swallow *format* errors
    # so that genuine structural problems (bad indentation etc.) surface.
    raw = path.read_text(encoding="utf-8")

    if _yaml is not None:
        try:
            cfg = _yaml.safe_load(raw) or {}
            if isinstance(cfg, Mapping):
                return cfg
        except _yaml.YAMLError:
            # Content is not valid YAML – fall through to JSON attempt.
            pass

    try:
        cfg = json.loads(raw) or {}
        if isinstance(cfg, Mapping):
            return cfg
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigError(
            f"Unable to parse config file as YAML or JSON: {path}"
        ) from exc

    raise ConfigError(f"Unsupported or invalid config format: {path}")


def _load_yaml(path: Path) -> dict:
    if _yaml is None:
        raise ConfigError(
            "PyYAML is required to load .yaml/.yml config files"
        )
    with path.open("r", encoding="utf-8") as fh:
        cfg = _yaml.safe_load(fh) or {}
    if not isinstance(cfg, Mapping):
        raise ConfigError("Top-level YAML config must be a mapping/object")
    return cfg


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        cfg = json.load(fh) or {}
    if not isinstance(cfg, Mapping):
        raise ConfigError("Top-level JSON config must be an object")
    return cfg


# ---------------------------------------------------------------------------
# Defaults normalization (dates)
# ---------------------------------------------------------------------------

def normalize_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Canonicalize defaults:

    - Accept ``defaults`` or ``_defaults``
    - Ensure ``cfg["defaults"]`` exists (canonical)
    - Remove stale ``_defaults`` alias to prevent duplication
    - Validate defaults.dates.start/end exist and are parseable
    """
    cfg = dict(cfg)

    defaults_section = cfg.get("defaults")
    underscore_defaults = cfg.get("_defaults")

    if defaults_section is None and underscore_defaults is not None:
        defaults_section = underscore_defaults

    # Clean up the underscore alias so only the canonical key survives
    cfg.pop("_defaults", None)

    if defaults_section is None:
        raise ConfigError("Missing 'defaults' (or '_defaults') section in config")
    if not isinstance(defaults_section, Mapping):
        raise ConfigError("Invalid defaults section in config (expected mapping)")

    dates = defaults_section.get("dates")
    if not isinstance(dates, Mapping):
        raise ConfigError("Missing or invalid defaults.dates section in config")

    raw_start = dates.get("start")
    raw_end = dates.get("end")
    if raw_start is None or raw_end is None:
        raise ConfigError("Missing defaults.dates.start or defaults.dates.end in config")

    try:
        start_date = _parse_date(raw_start, "defaults.dates.start")
        end_date = _parse_date(raw_end, "defaults.dates.end")
    except (ValueError, TypeError, ConfigError) as exc:
        raise ConfigError(
            "Missing or invalid defaults.dates.start/end in config.yaml"
        ) from exc

    if start_date >= end_date:
        raise ConfigError("Invalid defaults.dates range (start must be < end)")

    # Rebuild with canonical ISO strings
    cfg_defaults = dict(defaults_section)
    cfg_defaults["dates"] = {
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
    }
    cfg["defaults"] = cfg_defaults

    return cfg


def get_global_dates(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Return canonical defaults.dates dict with string ISO dates.

    Normalizes the raw values through :func:`_parse_date` so the caller
    always gets consistent ``YYYY-MM-DD`` strings regardless of whether
    :func:`normalize_defaults` was called earlier.
    """
    defaults_section = cfg.get("defaults") or cfg.get("_defaults")
    if not isinstance(defaults_section, Mapping):
        raise ConfigError("Missing defaults section")

    dates = defaults_section.get("dates")
    if not isinstance(dates, Mapping):
        raise ConfigError("Missing defaults.dates section")

    raw_start = dates.get("start")
    raw_end = dates.get("end")
    if raw_start is None or raw_end is None:
        raise ConfigError("Missing defaults.dates.start/end")

    return {
        "start": _parse_date(raw_start, "defaults.dates.start").isoformat(),
        "end": _parse_date(raw_end, "defaults.dates.end").isoformat(),
    }


# ---------------------------------------------------------------------------
# Sales config normalization
# ---------------------------------------------------------------------------

def normalize_sales_config(sales_cfg: Dict[str, Any]) -> Dict[str, Any]:
    sales_cfg = dict(sales_cfg)

    # --- file_format ---
    file_format = sales_cfg.get("file_format")
    if not file_format:
        raise ConfigError("sales.file_format is required")
    file_format = str(file_format).lower()
    if file_format == "delta":
        file_format = "deltaparquet"
        sales_cfg["file_format"] = file_format
    if file_format not in _VALID_FILE_FORMATS:
        raise ConfigError(
            f"sales.file_format must be one of: {', '.join(sorted(_VALID_FILE_FORMATS))}"
        )
    sales_cfg["file_format"] = file_format

    # --- required scalar keys ---
    _validate_required_keys(
        sales_cfg, section="sales", required=("total_rows", "skip_order_cols"),
    )
    total_rows = int(sales_cfg["total_rows"])
    if total_rows <= 0:
        raise ConfigError(f"sales.total_rows must be a positive integer, got {total_rows}")
    sales_cfg["total_rows"] = total_rows
    sales_cfg["skip_order_cols"] = bool(sales_cfg["skip_order_cols"])

    # --- optional numeric keys ---
    _coerce_optional_positive_int(sales_cfg, "chunk_size", "sales")
    _coerce_optional_positive_int(sales_cfg, "row_group_size", "sales")
    _coerce_optional_positive_int(sales_cfg, "workers", "sales")

    # --- nested partitioning block → flat keys ---
    part = sales_cfg.get("partitioning")
    if isinstance(part, Mapping):
        if "partition_enabled" not in sales_cfg and part.get("enabled") is not None:
            sales_cfg["partition_enabled"] = bool(part["enabled"])

        # Use `"columns" in part` so explicit null is respected
        if "partition_cols" not in sales_cfg and "columns" in part:
            cols = part.get("columns")
            if cols is None:
                sales_cfg["partition_cols"] = None
            else:
                if not isinstance(cols, list):
                    raise ConfigError(
                        "sales.partitioning.columns must be a list of column names"
                    )
                sales_cfg["partition_cols"] = [str(c) for c in cols]

    # --- normalize / validate flat partitioning keys ---
    if "partition_enabled" in sales_cfg:
        sales_cfg["partition_enabled"] = bool(sales_cfg["partition_enabled"])
        if sales_cfg["partition_enabled"] is False:
            sales_cfg["partition_cols"] = []

    if "partition_cols" in sales_cfg and sales_cfg["partition_cols"] is not None:
        if not isinstance(sales_cfg["partition_cols"], list):
            raise ConfigError("sales.partition_cols must be a list of column names")
        sales_cfg["partition_cols"] = [str(c) for c in sales_cfg["partition_cols"]]

    # --- ignored keys per format (built as a set, sorted once) ---
    ignored: set[str] = set(sales_cfg.get("_ignored_keys") or [])

    if file_format == "csv":
        ignored.update(k for k in _PARQUET_ONLY_KEYS if k in sales_cfg)
        ignored.update(k for k in _DELTA_ONLY_KEYS if k in sales_cfg)

    elif file_format == "parquet":
        ignored.update(k for k in _DELTA_ONLY_KEYS if k in sales_cfg)

    elif file_format == "deltaparquet":
        ignored.update(k for k in _PARQUET_MERGE_ONLY_KEYS if k in sales_cfg)
        sales_cfg.setdefault("partition_enabled", True)
        sales_cfg.setdefault("partition_cols", ["Year", "Month"])

    sales_cfg["_ignored_keys"] = sorted(ignored)
    return sales_cfg



# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_required_keys(
    cfg: Dict[str, Any], section: str, required: Iterable[str]
) -> None:
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ConfigError(f"Missing required keys in '{section}' config: {missing}")


# ---------------------------------------------------------------------------
# Path preparation (optional utility)
# ---------------------------------------------------------------------------

def prepare_paths(
    cfg: Dict[str, Any],
    parquet_folder_override: Optional[str | Path] = None,
    out_folder_override: Optional[str | Path] = None,
) -> Tuple[Path, Path]:
    sales_cfg = cfg.get("sales") or {}
    if not isinstance(sales_cfg, Mapping):
        raise ConfigError("Invalid sales section in config")

    parquet_folder = parquet_folder_override or sales_cfg.get("parquet_folder")
    out_folder = out_folder_override or sales_cfg.get("out_folder")

    if not parquet_folder or not out_folder:
        raise ConfigError(
            "Missing parquet/out folders. Provide overrides or set "
            "sales.parquet_folder and sales.out_folder."
        )

    parquet_dims = Path(parquet_folder).resolve()
    fact_out = Path(out_folder).resolve()

    parquet_dims.mkdir(parents=True, exist_ok=True)
    fact_out.mkdir(parents=True, exist_ok=True)

    return parquet_dims, fact_out


# ---------------------------------------------------------------------------
# Register section normalizers
# ---------------------------------------------------------------------------

def normalize_products_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce products section."""
    cfg = dict(cfg)
    _coerce_optional_positive_int(cfg, "num_products", "products")
    _coerce_optional_ratio(cfg, "active_ratio", "products")
    return cfg


def normalize_customers_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce customers section."""
    cfg = dict(cfg)
    _coerce_optional_positive_int(cfg, "total_customers", "customers")
    for pct_key in ("pct_us", "pct_eu", "pct_india", "pct_asia", "pct_org"):
        v = cfg.get(pct_key)
        if v is not None:
            v = float(v)
            if v < 0:
                raise ConfigError(f"customers.{pct_key} must be >= 0, got {v}")
            cfg[pct_key] = v
    _coerce_optional_ratio(cfg, "active_ratio", "customers")
    return cfg


def normalize_stores_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce stores section."""
    cfg = dict(cfg)
    _coerce_optional_positive_int(cfg, "num_stores", "stores")
    _coerce_optional_positive_int(cfg, "district_size", "stores")
    _coerce_optional_positive_int(cfg, "districts_per_region", "stores")
    return cfg


def normalize_promotions_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce promotions section."""
    cfg = dict(cfg)
    bucket_keys = (
        "num_seasonal", "num_clearance", "num_limited", "num_flash",
        "num_volume", "num_loyalty", "num_bundle", "num_new_customer",
    )
    for k in bucket_keys:
        _coerce_optional_int(cfg, k, "promotions")
        v = cfg.get(k)
        if v is not None and v < 0:
            raise ConfigError(f"promotions.{k} must be non-negative, got {v}")
    return cfg


_SECTION_NORMALIZERS.update(
    {
        "sales": normalize_sales_config,
        "geography": normalize_geography_config,
        "products": normalize_products_config,
        "customers": normalize_customers_config,
        "stores": normalize_stores_config,
        "promotions": normalize_promotions_config,
    }
)


# ---------------------------------------------------------------------------
# Cross-section business-rule validation
# ---------------------------------------------------------------------------

def skip_order_blocks_feature(cfg) -> bool:
    """Return True when skip_order_cols + sales_output='sales' removes order IDs.

    Shared predicate used by cross-section rules and dimension runners to
    decide whether returns/complaints should be disabled.  Works with both
    raw dicts and Pydantic config objects.
    """
    # .get() works on both plain dicts and Pydantic models (via _MutationMixin)
    sales_cfg = cfg.get("sales") if isinstance(cfg, dict) else getattr(cfg, "sales", None)
    if not sales_cfg or not isinstance(sales_cfg, Mapping):
        return False
    skip = bool(sales_cfg.get("skip_order_cols", False))
    output = str(sales_cfg.get("sales_output", "sales")).strip().lower()
    return skip and output == "sales"


def apply_cross_section_rules(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Validate business rules that span multiple config sections.

    Called after all section normalizers have run.  Mutates *cfg* to
    enforce consistency and returns it.

    Rules applied:
    1. Returns are silently disabled when ``skip_order_cols=true`` with
       ``sales_output='sales'`` because returns need ``OrderNumber``.
    2. Exchange-rate date range is always overridden to match
       ``defaults.dates`` (FX dates cannot be set independently).
    3. Physical store staffing is floored so every store keeps at least one
       salesperson (otherwise Sales emits ``EmployeeKey=-1`` orphan FKs).
    """
    from src.utils.logging_utils import warn as _warn

    sales_cfg = cfg.get("sales")

    # Returns and complaints require order columns
    if skip_order_blocks_feature(cfg):
        for section in ("returns", "complaints"):
            sec_cfg = cfg.get(section)
            if isinstance(sec_cfg, Mapping) and bool(sec_cfg.get("enabled", False)):
                _warn(f"Disabling {section}: skip_order_cols removes order IDs needed by {section}")
                sec_cfg = dict(sec_cfg)
                sec_cfg["enabled"] = False
                cfg[section] = sec_cfg

    # Rule 3: FX dates always follow global dates.
    # Also migrate old ``currencies`` key → ``to_currencies`` at raw-dict level
    # (before Pydantic sees it) for backward compat with existing config files.
    fx_cfg = cfg.get("exchange_rates")
    if isinstance(fx_cfg, Mapping):
        fx_cfg = dict(fx_cfg)
        fx_cfg.pop("use_global_dates", None)
        fx_cfg.pop("dates", None)
        fx_cfg.pop("volatility", None)
        if "currencies" in fx_cfg and "to_currencies" not in fx_cfg:
            fx_cfg["to_currencies"] = fx_cfg.pop("currencies")
        cfg["exchange_rates"] = fx_cfg

    # Rule 4: physical store staffing must leave room for >= 1 salesperson.
    # EmployeeCount includes the store manager, so a per-type minimum below 2
    # produces stores with a manager and zero Sales Associates. Sales then has
    # no eligible salesperson for those (store, date) rows and emits
    # EmployeeKey=-1 — an orphan FK that breaks the generated SQL constraints.
    # Floor each physical type's minimum to 2 (the smallest change that keeps
    # >= 1 salesperson per store). "Online" is fixed to 1 by the store generator
    # and is skipped — online stores get a dedicated sales representative.
    stores_cfg = cfg.get("stores")
    if isinstance(stores_cfg, Mapping):
        staffing = stores_cfg.get("staffing_ranges")
        if isinstance(staffing, Mapping):
            from src.defaults import MIN_PHYSICAL_EMPLOYEE_COUNT as _min_physical_staff
            new_staffing = dict(staffing)
            changed = False
            for stype, spec in list(new_staffing.items()):
                if str(stype) == "Online":
                    continue
                if isinstance(spec, (list, tuple)) and len(spec) == 2:
                    lo, hi = int(spec[0]), int(spec[1])
                    if lo < _min_physical_staff:
                        new_lo = _min_physical_staff
                        new_hi = max(hi, new_lo)
                        _warn(
                            f"stores.staffing_ranges[{stype!r}]=[{lo}, {hi}] is too low: "
                            "a store needs >= 2 staff so >= 1 Sales Associate remains "
                            f"after the manager; raising to [{new_lo}, {new_hi}]."
                        )
                        new_staffing[stype] = [new_lo, new_hi]
                        changed = True
            if changed:
                stores_cfg = dict(stores_cfg)
                stores_cfg["staffing_ranges"] = new_staffing
                cfg["stores"] = stores_cfg

    return cfg
