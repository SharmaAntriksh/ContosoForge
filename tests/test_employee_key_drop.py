"""An EmployeeKey=-1 at chunk assembly (a salesperson-coverage gap) is dropped
with a loud warning, never swallowed silently.

The coordinator's coverage pre-flight is the coverage gate: it aborts before
generation on avoidable (fully-open) gaps under ``sales.coverage_policy: abort``,
repairs what it can under ``repair``, and otherwise proceeds while documenting
that residual boundary-month / unrepairable gaps are dropped here. So any -1 that
reaches row assembly is an accepted residual — the whole orders are dropped
(keeping FK / order-line integrity) with a warning that the month is short of
plan. A fully-covered chunk has no -1 and this is a silent no-op.
"""
from __future__ import annotations

import numpy as np


def test_drop_orphan_employee_key_rows_drops_and_warns(monkeypatch):
    from src.facts.sales.sales_logic import chunk_builder as cb

    warnings: list[str] = []
    monkeypatch.setattr(cb, "warn", lambda msg: warnings.append(msg))

    cols = {
        "EmployeeKey": np.array([1, -1, 2, -1, 3], dtype=np.int64),
        "CustomerKey": np.array([10, 11, 12, 13, 14], dtype=np.int32),
        "UnitPrice": np.array([1.0, 2.0, 3.0, 4.0, 5.0]),
    }
    new_n = cb._drop_orphan_employee_key_rows(cols, 5, m_offset=7)

    assert new_n == 3
    assert cols["EmployeeKey"].tolist() == [1, 2, 3]
    assert cols["CustomerKey"].tolist() == [10, 12, 14]
    assert cols["UnitPrice"].tolist() == [1.0, 3.0, 5.0]
    assert warnings, "an EmployeeKey=-1 drop must emit a warning, not happen silently"
    assert "EmployeeKey=-1" in warnings[0] and "month offset 7" in warnings[0]


def test_drop_orphan_employee_key_rows_is_silent_noop_when_clean(monkeypatch):
    from src.facts.sales.sales_logic import chunk_builder as cb

    # No -1 present -> untouched, no warning, regardless of columns.
    warnings: list[str] = []
    monkeypatch.setattr(cb, "warn", lambda msg: warnings.append(msg))

    cols = {
        "EmployeeKey": np.array([1, 2, 3], dtype=np.int64),
        "CustomerKey": np.array([10, 20, 30], dtype=np.int32),
    }
    new_n = cb._drop_orphan_employee_key_rows(cols, 3, m_offset=0)

    assert new_n == 3
    assert cols["EmployeeKey"].tolist() == [1, 2, 3]  # untouched
    assert not warnings, "no drop -> no warning"
