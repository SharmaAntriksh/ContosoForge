"""Contract-pinning guardrails for the stringly-typed seams in facts/sales.

These freeze the loose config/State seams so a later re-plumbing (a typed
``WorkerConfig`` + a real ``State``) is a *visible* change rather than a silent
drift. Two seams are pinned, both via ``ast`` static analysis (robust to
formatting, unlike regex or line numbers) and recorded to a snapshot
(``_snapshots/sales_state_contract.json``):

1. **worker_cfg key gap** — keys ``init_sales_worker`` consumes
   (``worker_cfg["k"]`` / ``worker_cfg.get("k")``) that are NOT declared in the
   ``SalesWorkerCfg`` TypedDict. Today this is ``{"merged_file"}``: init reads a
   top-level ``merged_file`` the builder never sets (it lives nested under
   ``output_paths``), so the read is always ``None`` and only the
   ``or op.get("merged_file")`` fallback saves it — a consumed-but-not-produced
   footgun a typed config would remove.
2. **State phantom attributes** — ``State`` attributes the worker code reads that
   are not declared as class attributes (bound only dynamically by
   ``bind_globals``). Giving these real declarations would shrink the set.

Both are pinned rather than asserted-clean: the job here is to freeze the current
(imperfect) contract so changes to it are deliberate and test-visible. These
cover the seams the golden snapshot can't — a mis-plumbed key or phantom
attribute need not change output bytes.

The declared-State set is read statically from the ``State`` class body (not
``vars(State)``) so the result does not depend on whether an earlier test in the
same process already ran a generation — that would ``setattr`` dynamic attributes
onto the class and mask genuine phantoms.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

_SALES_ROOT = Path(__file__).resolve().parents[1] / "src" / "facts" / "sales"
_INIT_PY = _SALES_ROOT / "sales_worker" / "init.py"
_GLOBALS_PY = _SALES_ROOT / "sales_logic" / "globals.py"
_SNAPSHOT_FILE = Path(__file__).parent / "_snapshots" / "sales_state_contract.json"


def _const_str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _worker_cfg_keys(src: str, var: str = "worker_cfg") -> set[str]:
    """Constant string keys read from *var* via subscript or ``.get()``."""
    tree = ast.parse(src)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name) and node.value.id == var):
            k = _const_str(node.slice)
            if k is not None:
                keys.add(k)
        elif (isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
              and isinstance(node.func.value, ast.Name) and node.func.value.id == var
              and node.args):
            k = _const_str(node.args[0])
            if k is not None:
                keys.add(k)
    return keys


def _state_attr_reads(paths) -> set[str]:
    """Every ``State`` attribute referenced (``State.x`` load, or
    ``getattr(State, "x")``) across *paths*."""
    reads: set[str] = set()
    for p in paths:
        tree = ast.parse(Path(p).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name) and node.value.id == "State"
                    and isinstance(node.ctx, ast.Load)):
                reads.add(node.attr)
            elif (isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                  and len(node.args) >= 2
                  and isinstance(node.args[0], ast.Name) and node.args[0].id == "State"):
                k = _const_str(node.args[1])
                if k is not None:
                    reads.add(k)
    return reads


def _declared_state_attrs() -> set[str]:
    """Names declared in the ``State`` class body (attributes + methods), read
    statically from source so a prior generation's ``setattr`` onto the class
    (via bind_globals) can't mask genuine phantoms."""
    tree = ast.parse(_GLOBALS_PY.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "State":
            for stmt in node.body:  # direct class-body only, not method internals
                if isinstance(stmt, ast.Assign):
                    names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    names.add(stmt.target.id)
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(stmt.name)
    return names


def _contract_surface() -> dict[str, list[str]]:
    from src.facts.sales.prep.worker_cfg_schema import SalesWorkerCfg

    declared_cfg = set(SalesWorkerCfg.__annotations__)
    consumed_cfg = _worker_cfg_keys(_INIT_PY.read_text(encoding="utf-8"))

    declared_state = _declared_state_attrs()
    files = [
        *(_SALES_ROOT / "sales_logic").rglob("*.py"),
        *(_SALES_ROOT / "sales_worker").rglob("*.py"),
    ]
    read_state = _state_attr_reads(files)

    return {
        "undeclared_worker_cfg_keys": sorted(consumed_cfg - declared_cfg),
        "phantom_state_attrs": sorted(read_state - declared_state),
    }


def test_sales_contract_surface_pinned():
    """The stringly-typed worker_cfg / State contract surface is unchanged.

    First run (or ``REGEN_SALES_SNAPSHOT=1``) records the baseline; thereafter it
    asserts equality. A later refactor that formalizes these seams regenerates it.
    """
    current = _contract_surface()

    if os.environ.get("REGEN_SALES_SNAPSHOT") == "1" or not _SNAPSHOT_FILE.exists():
        _SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT_FILE.write_text(json.dumps(current, indent=2, sort_keys=True))
        pytest.skip(f"Sales-contract baseline written to {_SNAPSHOT_FILE.name}; re-run to compare.")

    baseline = json.loads(_SNAPSHOT_FILE.read_text())
    diffs = {}
    for key in set(baseline) | set(current):
        b, c = set(baseline.get(key, [])), set(current.get(key, []))
        if b != c:
            diffs[key] = {"added": sorted(c - b), "removed": sorted(b - c)}
    assert not diffs, (
        f"Sales worker_cfg/State contract surface changed: {json.dumps(diffs, indent=2)}\n"
        "If intended (a refactor formalizing the config/State), rerun with "
        "REGEN_SALES_SNAPSHOT=1 to regenerate the baseline."
    )
