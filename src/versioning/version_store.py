import json
import hashlib
from collections.abc import Mapping
from pathlib import Path

# -----------------------------------------------------------
# Absolute versioning folder under project root
# -----------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERSION_DIR = PROJECT_ROOT / "data" / "versioning"
VERSION_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------
def _canonical_default(o):
    """Deterministic coercion for objects ``json`` can't serialize natively.

    Critically, sets are sorted: ``str(set)`` iteration order depends on
    PYTHONHASHSEED, so the old ``str(obj)`` fallback could hash the same
    config to different values across runs and spuriously regenerate.
    """
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if hasattr(o, "model_dump"):          # Pydantic models (config sub-sections)
        return o.model_dump()
    if isinstance(o, Mapping):
        return dict(o)
    return str(o)


def _compute_hash(obj) -> str:
    """Compute deterministic hash for config sections."""
    try:
        data = json.dumps(obj, sort_keys=True, default=_canonical_default).encode("utf-8")
    except TypeError:
        # Last resort: should be unreachable given the default handler above.
        data = str(obj).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _version_file(name: str) -> Path:
    """Return version file path for the dimension."""
    return VERSION_DIR / f"{name}.version.json"


def delete_version(name: str) -> bool:
    """Delete the version file for a dimension. Returns True if file existed."""
    vf = _version_file(name)
    if vf.exists():
        vf.unlink()
        return True
    return False


def load_version(name: str):
    """Load the version metadata for a dimension."""
    vf = _version_file(name)
    if not vf.exists():
        return None
    try:
        return json.loads(vf.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def save_version(name: str, cfg_section, output_path: Path):
    """
    Write version metadata for the given dimension.

    Parameters:
    - name: dimension name (e.g., "geography")
    - cfg_section: resolved config section for that dimension
    - output_path: parquet file path

    We store:
    - config hash (same as before)
    - parquet mtime (useful for debugging)
    """
    vf = _version_file(name)

    data = {
        "config_hash": _compute_hash(cfg_section),
        "parquet_mtime": output_path.stat().st_mtime if output_path.exists() else None
    }

    vf.write_text(json.dumps(data, indent=2))


def should_regenerate(name: str, cfg_section, parquet_path: Path) -> bool:
    """
    Return True if a dimension must be regenerated.

    Conditions:
    1. Parquet missing
    2. Version file missing
    3. Config hash changed
    """
    # Condition A: Parquet file missing
    if not parquet_path.exists():
        return True

    # Load old version
    old = load_version(name)
    if old is None:
        return True

    # Compute new config hash
    new_hash = _compute_hash(cfg_section)

    # Condition C: Config changed
    if old.get("config_hash") != new_hash:
        return True

    return False
