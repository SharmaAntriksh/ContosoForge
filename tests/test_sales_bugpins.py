"""Regression tests for three latent defects fixed in facts/sales.

Each was first written as a failing ``xfail(strict=True)`` reproduction, then
flipped to a passing test once its fix landed. They cover the defects whose
desired behavior is unambiguous and whose call surface needs no coordinator
context or failure injection:

* a ``None`` merged_file coerced to a file literally named ``"None"``;
* the ``_MONTH_DEMAND`` cache serving a stale lines-per-order curve across an
  in-process rebind of ``models_cfg``;
* partition/merge config silently dropped for a plain-dict cfg.

The other defects from the same review have their own regression tests:
ReturnEventKey capacity overflow (``test_returns_event_key_capacity.py``), the
CSV/parquet atomic-write windows (``test_sales_writer_atomic.py``), the
optimize_parquet dict-encode drift (``test_optimize_parquet_dict.py``), and the
EmployeeKey=-1 row drop (``test_employee_key_drop.py``).
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("yaml")


# ---------------------------------------------------------------------------
# build_output_paths_from_sales_cfg turned a None merged_file into a file
# literally named "None" (str(None)). Fixed: None falls through to the table's
# default filename (sales.parquet).
# ---------------------------------------------------------------------------
def test_bugpin_none_merged_file_not_named_none():
    from src.facts.sales.output_paths import (
        build_output_paths_from_sales_cfg,
        TABLE_SALES,
    )

    sales_cfg = SimpleNamespace(
        file_format="parquet",
        out_folder="/tmp/out",
        merged_file=None,            # caller omitted it
        delta_output_folder=None,
    )
    op = build_output_paths_from_sales_cfg(sales_cfg)
    merged = op.merged_path(TABLE_SALES)
    assert not str(merged).replace("\\", "/").endswith("/None"), (
        f"merged sales path is literally named 'None': {merged!r} — a None "
        "merged_file must fall back to the default 'sales.parquet'."
    )


# ---------------------------------------------------------------------------
# The _MONTH_DEMAND cache in orders.py is a function of State.models_cfg but had
# no production reset, so a between-run rebind served the previous run's
# lines-per-order curve. Fixed: _reset_month_demand is chained into State.reset()
# and init_sales_worker's reset block.
# ---------------------------------------------------------------------------
def test_bugpin_month_demand_cache_follows_models_cfg_rebind():
    from src.facts.sales.sales_logic.core import orders
    from src.facts.sales.sales_logic.globals import State

    orders._reset_month_demand()  # clean slate independent of test order
    try:
        State.reset()
        State.models_cfg = {"lines_per_order": {"base": 1.0, "amplitude": 0.05,
                                                "q4_boost": 0.0, "phase_shift": 0}}
        curve_a = np.array(orders._get_month_demand(), dtype=float, copy=True)

        # A fresh run rebinds a different models_cfg through the same reset path.
        State.reset()
        State.models_cfg = {"lines_per_order": {"base": 1.0, "amplitude": 0.90,
                                                "q4_boost": 0.0, "phase_shift": 0}}
        curve_b = np.array(orders._get_month_demand(), dtype=float, copy=True)

        assert not np.allclose(curve_a, curve_b), (
            "lines-per-order month-demand curve did not follow a models_cfg "
            "rebind — the _MONTH_DEMAND cache served the previous run's curve."
        )
    finally:
        orders._reset_month_demand()
        State.reset()


# ---------------------------------------------------------------------------
# _resolve_partitioning read sales config via getattr, which silently returns
# None for a plain-dict cfg (dicts have no .sales attribute), so partitioning was
# dropped for dict-shaped cfgs. Fixed: reads route through the dict/attr-agnostic
# _cfg_get accessor.
# ---------------------------------------------------------------------------
def test_bugpin_dict_cfg_partitioning_respected():
    from src.facts.sales.sales import _resolve_partitioning

    cfg = {"sales": {"partition_enabled": True, "partition_cols": ["Year", "Month"]}}
    enabled, cols = _resolve_partitioning(cfg, partition_enabled=False, partition_cols=None)
    assert enabled is True and cols == ["Year", "Month"], (
        "dict-shaped cfg partition config was ignored: "
        f"got enabled={enabled!r} cols={cols!r} — getattr(dict, 'sales') returns "
        "None so the sales section is silently dropped."
    )
