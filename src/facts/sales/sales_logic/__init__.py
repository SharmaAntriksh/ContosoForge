"""
Compatibility shim for sales logic.

This package is the stable import surface.

Keeps old imports working, e.g.:
  from src.facts.sales.sales_logic import State, bind_globals, fmt, PA_AVAILABLE
  from src.facts.sales.sales_logic import chunk_builder
  from src.facts.sales.sales_logic.globals import State, bind_globals
"""

from __future__ import annotations

# Import submodules so `from ...sales_logic import chunk_builder` works
from . import chunk_builder as chunk_builder
from . import columns as columns
from . import core as core
from . import globals as globals

# Re-export key symbols (matches the old sales_logic.py __all__)
# State and bind_globals come from globals; fmt and PA_AVAILABLE come from globals
# (core re-exports them but globals is the canonical source)
from .globals import State, bind_globals, PA_AVAILABLE, fmt
from .chunk_builder import build_chunk_table

__all__ = [
    "State",
    "bind_globals",
    "fmt",
    "PA_AVAILABLE",
    # also expose modules for compatibility / convenience
    "chunk_builder",
    "columns",
    "core",
    "globals",
    "build_chunk_table",
]