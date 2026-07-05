"""The OrderDate clamp pushes each order forward to its customer's first start
date, so no order predates the customer's (day-granular) start date — and it is a
no-op for unknown keys or when the lookup table is unbound.

Eligibility is month-granular (CustomerStartMonth) but the start date carries a
0-27 day offset within that month, so an order sampled in the start month can land
a few days before the customer actually joined; the clamp corrects that. The
end-to-end wiring (init binding the lookup onto State) is guarded by the sales
golden snapshot — it would revert if the key were ever left unbound again.
"""
from __future__ import annotations

import numpy as np


def _d(s: str) -> np.datetime64:
    return np.datetime64(s, "D")


def test_clamp_pushes_orders_forward_to_customer_start(monkeypatch):
    from src.facts.sales.sales_logic import chunk_builder as cb
    from src.facts.sales.sales_logic.globals import State

    int64_min = np.iinfo(np.int64).min
    # CustomerKey -> first start date (epoch days); INT64_MIN marks unknown keys
    # so np.maximum against them is a no-op.
    lut = np.full(11, int64_min, dtype=np.int64)
    lut[5] = _d("2023-06-15").astype(np.int64)
    lut[10] = _d("2023-01-01").astype(np.int64)
    monkeypatch.setattr(State, "customer_first_eff_start_by_key", lut)

    order_dates = np.array(
        [_d("2023-06-10"),   # key 5, before start -> clamp forward to 06-15
         _d("2023-06-20"),   # key 5, after start  -> unchanged
         _d("2023-03-01"),   # key 10, after start -> unchanged
         _d("2020-01-01")],  # key 7 unknown       -> unchanged (INT64_MIN slot)
        dtype="datetime64[D]",
    )
    customer_keys = np.array([5, 5, 10, 7], dtype=np.int32)

    out = cb._clamp_order_dates_to_customer_start(order_dates, customer_keys)

    expected = np.array(
        [_d("2023-06-15"), _d("2023-06-20"), _d("2023-03-01"), _d("2020-01-01")],
        dtype="datetime64[D]",
    )
    assert out.dtype == np.dtype("datetime64[D]")
    assert np.array_equal(out, expected)
    # The known-key order that predated its start was actually moved forward.
    assert out[0] == _d("2023-06-15")


def test_clamp_is_noop_when_lut_unbound(monkeypatch):
    from src.facts.sales.sales_logic import chunk_builder as cb
    from src.facts.sales.sales_logic.globals import State

    # A fresh/unbound State (lut is None) must leave order dates untouched.
    monkeypatch.setattr(State, "customer_first_eff_start_by_key", None)
    order_dates = np.array([_d("2020-01-01"), _d("2019-05-05")], dtype="datetime64[D]")
    keys = np.array([1, 2], dtype=np.int32)

    out = cb._clamp_order_dates_to_customer_start(order_dates, keys)

    assert np.array_equal(out, order_dates)
