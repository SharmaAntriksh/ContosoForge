"""
web/shared_state.py -- Shared mutable state and helpers for the web API.

All route modules import from here so that every router sees the same
config dicts, job state, and helper functions.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel

from src.utils.promotion_buckets import PROMOTION_BUCKET_KEYS, distribute_total


class ConfigUpdate(BaseModel):
    """Partial config update payload used by config and models form endpoints."""
    values: Dict[str, Any]


# ---------------------------------------------------------------------------
# Resolve repo root
# ---------------------------------------------------------------------------

def _find_repo_root() -> Path:
    d = Path(__file__).resolve().parent
    for _ in range(10):
        if (d / "main.py").exists():
            return d
        d = d.parent
    return Path.cwd()


REPO_ROOT = _find_repo_root()

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Config normalization (reuse pipeline's normalizers)
# ---------------------------------------------------------------------------

try:
    from src.engine.config.config import load_config as _load_pipeline_config
    _HAS_NORMALIZER = True
except ImportError:
    _HAS_NORMALIZER = False

# ---------------------------------------------------------------------------
# ANSI regex
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

_config_path = REPO_ROOT / "config.yaml"
_models_path = REPO_ROOT / "models.yaml"


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def _load_yaml(p: Path) -> dict:
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _base_config():
    if _HAS_NORMALIZER:
        try:
            return _load_pipeline_config(str(_config_path))
        except (KeyError, ValueError, OSError, TypeError):
            pass
    # Fallback: coerce the raw YAML into an AppConfig so the rest of the web
    # layer can rely on attribute access (the routes assume a Pydantic model,
    # not a plain dict). Fall back to schema defaults if even that fails.
    from src.engine.config.config_schema import AppConfig
    try:
        return AppConfig.from_raw_dict(_load_yaml(_config_path))
    except (ValueError, TypeError, KeyError):
        return AppConfig()


# ---------------------------------------------------------------------------
# Config state  (mutable module-level singletons, guarded by _cfg_lock)
# ---------------------------------------------------------------------------

_cfg_lock = threading.Lock()

_cfg: Dict[str, Any] = _base_config()
_cfg_disk_yaml: str = _config_path.read_text(encoding="utf-8") if _config_path.exists() else ""
_models_cfg: Dict[str, Any] = _load_yaml(_models_path)
_models_yaml_text: str = _models_path.read_text(encoding="utf-8") if _models_path.exists() else ""

# ---------------------------------------------------------------------------
# Preset logic
# ---------------------------------------------------------------------------

try:
    from .presets import PRESETS, apply_preset, build_presets_by_sales
except ImportError:
    PRESETS = {}
    apply_preset = None
    build_presets_by_sales = None

# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

_job_lock = threading.Lock()
_current_job: Optional[Dict[str, Any]] = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _g(d, *keys, default=None):
    """Nested dict/Mapping get (supports both plain dicts and Pydantic models)."""
    from collections.abc import Mapping
    cur = d
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k, default)
        elif isinstance(cur, Mapping) or hasattr(cur, k):
            cur = getattr(cur, k, default)
        else:
            return default
    return cur if cur is not None else default


_PROMO_KEYS = PROMOTION_BUCKET_KEYS


def _promo_total(promos) -> int:
    if isinstance(promos, dict):
        if any(k in promos for k in _PROMO_KEYS):
            return sum(int(promos.get(k, 0) or 0) for k in _PROMO_KEYS)
        return int(promos.get("total_promotions", 0) or 0)
    # Pydantic model path
    if any(hasattr(promos, k) for k in _PROMO_KEYS):
        return sum(int(getattr(promos, k, 0) or 0) for k in _PROMO_KEYS)
    return int(getattr(promos, "total_promotions", 0) or 0)


def _set_promotions_total(promos, total: int):
    """Distribute total across all promo buckets proportionally."""
    total = max(0, int(total))
    keys = _PROMO_KEYS
    if isinstance(promos, dict):
        if any(k in promos for k in keys):
            weights = [int(promos.get(k, 0) or 0) for k in keys]
            for k, c in zip(keys, distribute_total(weights, total)):
                promos[k] = c
        else:
            promos["total_promotions"] = total
    else:
        # Pydantic model path
        if any(hasattr(promos, k) for k in keys):
            weights = [int(getattr(promos, k, 0) or 0) for k in keys]
            for k, c in zip(keys, distribute_total(weights, total)):
                setattr(promos, k, c)
        else:
            setattr(promos, "total_promotions", total)


def write_yaml_secure(path: Path, data: dict) -> None:
    """Write *data* as YAML to *path* with owner-only permissions (0o600)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def cfg_to_dict(obj) -> dict:
    """Convert an AppConfig (or plain dict) to a plain dict for YAML serialization."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj if isinstance(obj, dict) else dict(obj)


def normalize_config_yaml(parsed: dict) -> dict:
    """Run the pipeline normalizer on a parsed YAML dict, return normalized config."""
    global _HAS_NORMALIZER
    if _HAS_NORMALIZER:
        tmp_path = None
        try:
            # mkstemp creates the file with 0o600 by default; use its fd
            # directly to avoid a close-reopen race window.
            raw_fd, tmp_path = tempfile.mkstemp(suffix=".yaml")
            with os.fdopen(raw_fd, "w", encoding="utf-8") as tmp:
                yaml.safe_dump(parsed, tmp, sort_keys=False)
            return _load_pipeline_config(tmp_path)
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
    return parsed
