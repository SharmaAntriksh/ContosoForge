"""
Public API for versioning utilities.

This package exposes:
- should_regenerate: Check if a dimension must be regenerated.
- save_version: Write version metadata after generation.
- load_version: Load stored metadata.
- delete_version: Remove a dimension's version file (forces regen).
"""

# Core version metadata store
from .version_store import (
    save_version,
    load_version,
    delete_version,
    should_regenerate,
)

__all__ = [
    "save_version",
    "load_version",
    "delete_version",
    "should_regenerate",
]
