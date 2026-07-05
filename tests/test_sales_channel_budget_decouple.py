"""Guardrail — sales channel/time-of-day fidelity must not depend on the budget fact.

The sales fact's ``ChannelKey`` / ``TimeKey`` (and the derived time bins) are
sampled from the real ``channels.parquet`` dimension, which the workers reach via
``State.parquet_folder``. That folder used to be bound into the worker config
*only* inside the budget-enabled branch of ``_setup_accumulators``: with budget
off, every worker saw ``parquet_folder=None``, ``_load_sales_channels`` returned
``None``, and the producer silently fell back to the uniform
``SALES_CHANNEL_CORE_KEYS`` keys and the generic digital hour profile instead of
the actual channel mix and per-channel operating hours.

That coupled two facts that are meant to be independent: flipping the budget
toggle changed the sales fact. This test pins the invariant directly — generate
the same sales fact with budget off and with budget on and assert the channel /
time columns are identical. It fails on the buggy (budget-gated) binding and
passes once ``parquet_folder`` is bound unconditionally.

Like the other end-to-end determinism guards, this spins up a real
multiprocessing pool over a tiny dataset, so it takes a few seconds.
"""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")
pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow.parquet")

from tests import sales_gen

# Columns whose values are produced by the sales-channel / time-of-day sampler
# and therefore depend on channels.parquet reaching the workers.
_CHANNEL_TIME_COLS = [
    "ChannelKey", "TimeKey",
    "Bin15mKey", "Bin30mKey", "Bin1hKey", "Bin6hKey", "Bin12hKey",
]


def _run_sales(base, dims_dir, name, *, budget_enabled):
    run_dir = base / name
    scratch, final = run_dir / "scratch", run_dir / "final"
    scratch.mkdir(parents=True, exist_ok=True)
    final.mkdir(parents=True, exist_ok=True)
    cfg = sales_gen.small_config(
        dims_dir=dims_dir, scratch_dir=scratch, final_dir=final,
        workers=2, chunk_size=4_000,
    )
    cfg["budget"]["enabled"] = budget_enabled
    sales_gen.run_pipeline_stage(run_dir, cfg, only="sales")
    df = sales_gen.load_sales(final, scratch)
    sort_cols = [c for c in ("OrderNumber", "OrderLineNumber") if c in df.columns]
    return df.sort_values(sort_cols).reset_index(drop=True)


def test_channel_time_columns_independent_of_budget(tmp_path_factory):
    """The sales fact's channel/time columns must be identical with budget off vs on."""
    base = tmp_path_factory.mktemp("channel_budget")
    dims_dir = base / "dims"
    dims_dir.mkdir()
    dims_cfg = sales_gen.small_config(
        dims_dir=dims_dir, scratch_dir=base / "_ds", final_dir=base / "_df",
        workers=1, chunk_size=4_000,
    )
    sales_gen.run_pipeline_stage(base, dims_cfg, only="dimensions")

    off = _run_sales(base, dims_dir, "budget_off", budget_enabled=False)
    on = _run_sales(base, dims_dir, "budget_on", budget_enabled=True)

    assert len(off) == len(on), (
        f"budget off/on produced different sales row counts ({len(off)} vs {len(on)}); "
        "budget is a passive observer and must not change the sales fact shape."
    )

    present = [c for c in _CHANNEL_TIME_COLS if c in off.columns and c in on.columns]
    assert "ChannelKey" in present and "TimeKey" in present, (
        f"expected ChannelKey and TimeKey in the sales fact; got columns {list(off.columns)}"
    )

    for col in present:
        assert off[col].equals(on[col]), (
            f"{col} differs between budget-off and budget-on runs — sales channel/"
            "time fidelity is coupled to the budget toggle. The dimensions parquet "
            "folder must reach workers regardless of whether budget is enabled."
        )
