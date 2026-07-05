"""An EmployeeKey=-1 at chunk assembly (a salesperson-coverage gap) is handled
per the coverage policy, never swallowed silently.

- ``abort`` / ``repair`` guarantee full coverage upstream, so a -1 reaching row
  assembly is a hard precondition violation -> ``SalesError``.
- ``skip`` accepts uncovered store-months, so the -1 orders are dropped (keeping
  FK / order-line integrity) with a loud warning that the month is short of plan.
- A fully covered chunk has no -1 and this is a silent no-op.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.exceptions import SalesError


def test_drop_orphan_employee_key_rows_drops_and_warns_under_skip(monkeypatch):
    from src.facts.sales.sales_logic import chunk_builder as cb
    from src.facts.sales.sales_logic.globals import State

    monkeypatch.setattr(State, "coverage_policy", "skip")
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


def test_drop_orphan_employee_key_rows_raises_under_guaranteeing_policy(monkeypatch):
    from src.facts.sales.sales_logic import chunk_builder as cb
    from src.facts.sales.sales_logic.globals import State

    # abort/repair must guarantee coverage upstream; a -1 here is a bug, not a
    # silently-shortened month.
    monkeypatch.setattr(State, "coverage_policy", "abort")

    cols = {
        "EmployeeKey": np.array([1, -1, 2], dtype=np.int64),
        "CustomerKey": np.array([10, 11, 12], dtype=np.int32),
    }
    with pytest.raises(SalesError, match="coverage_policy"):
        cb._drop_orphan_employee_key_rows(cols, 3, m_offset=4)


def test_drop_orphan_employee_key_rows_is_silent_noop_when_clean(monkeypatch):
    from src.facts.sales.sales_logic import chunk_builder as cb
    from src.facts.sales.sales_logic.globals import State

    # No -1 present -> returns before the policy check, regardless of policy.
    monkeypatch.setattr(State, "coverage_policy", "abort")
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
