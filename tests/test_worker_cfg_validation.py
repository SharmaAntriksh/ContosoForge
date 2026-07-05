"""Guardrails for the worker_cfg from-wire completeness check.

REQUIRED_FIELDS mirrors the ``Required[...]`` markers on ``SalesWorkerCfg`` by
hand, because ``from __future__ import annotations`` stringizes the annotations
and leaves ``TypedDict.__required_keys__`` empty at runtime. These tests pin
that mirror against the source markers (so it cannot silently drift) and the
validator's fail-loud behavior.
"""
import ast
from pathlib import Path

import pytest

from src.exceptions import ConfigError
from src.facts.sales.prep.worker_cfg_schema import (
    REQUIRED_FIELDS,
    validate_worker_cfg,
)

_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src" / "facts" / "sales" / "prep" / "worker_cfg_schema.py"
)


def _required_markers_from_source():
    """Field names annotated ``Required[...]`` in the SalesWorkerCfg class body."""
    tree = ast.parse(_SCHEMA.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "SalesWorkerCfg"
    )
    names = []
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            ann = stmt.annotation
            if (
                isinstance(ann, ast.Subscript)
                and isinstance(ann.value, ast.Name)
                and ann.value.id == "Required"
            ):
                names.append(stmt.target.id)
    return names


def test_required_fields_matches_schema_markers():
    """REQUIRED_FIELDS must equal the Required[...] markers (order-independent)."""
    assert set(REQUIRED_FIELDS) == set(_required_markers_from_source())


def test_validate_passes_on_complete_cfg():
    # A cfg carrying every required key (values irrelevant) must not raise.
    validate_worker_cfg({k: object() for k in REQUIRED_FIELDS})


@pytest.mark.parametrize("drop", REQUIRED_FIELDS)
def test_validate_raises_naming_missing_field(drop):
    cfg = {k: object() for k in REQUIRED_FIELDS if k != drop}
    with pytest.raises(ConfigError) as ei:
        validate_worker_cfg(cfg)
    assert drop in str(ei.value)
