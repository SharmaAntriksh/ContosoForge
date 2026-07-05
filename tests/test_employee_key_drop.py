"""An EmployeeKey=-1 row drop (a salesperson-coverage gap that reached chunk
assembly) must be surfaced loudly, not swallowed silently.

The drop breaks the per-month plan invariant (sum n_lines == R[m]); previously it
happened with no signal at all. The fix keeps the drop (so orphan FKs never ship)
but warns with the count and month, so the invariant violation is visible. Making
coverage a hard precondition is left to a future refactor.
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
