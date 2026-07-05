"""The returns builder must not silently overflow its per-chunk ReturnEventKey
capacity.

Adjacent chunks space their ReturnEventKey ranges exactly ``event_key_capacity``
apart (``event_key_offset = chunk_idx * capacity``). If a chunk emits more events
than the capacity, its keys spill into the next chunk's range and collide with no
error. The coordinator sizes the capacity as a structural upper bound, so this
guards against that bound ever being wrong: an over-capacity chunk now fails loudly
instead of shipping colliding keys.
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest

from src.exceptions import SalesError
from src.facts.sales.sales_worker.returns_builder import (
    ReturnsConfig,
    build_sales_returns_from_detail,
)


def _detail(n: int) -> pa.Table:
    rng = np.random.default_rng(12345)
    return pa.table({
        "OrderNumber": pa.array(np.arange(1, n + 1, dtype=np.int32)),
        "OrderLineNumber": pa.array(np.ones(n, dtype=np.int32)),
        "DeliveryDate": pa.array(
            np.array(["2024-03-15"] * n, dtype="datetime64[D]"), type=pa.date32()),
        "Quantity": pa.array(rng.integers(3, 10, size=n, dtype=np.int32)),
        "NetPrice": pa.array(rng.uniform(20.0, 200.0, size=n)),
        "IsOrderDelayed": pa.array(rng.integers(0, 2, size=n, dtype=np.int32)),
    })


# Forces many events: every line returns and splits into up to max_splits events.
_MANY = dict(enabled=True, return_rate=1.0, split_return_rate=1.0,
             full_line_probability=1.0)


def test_over_capacity_chunk_raises_saleserror():
    detail = _detail(100)  # ~100 return lines, each up to 5 events -> >= 200
    cfg = ReturnsConfig(**_MANY, max_splits=5, event_key_capacity=10)
    with pytest.raises(SalesError, match="capacity"):
        build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)


def test_capacity_zero_is_unchecked_legacy_behavior():
    # Direct/test construction leaves capacity at 0 -> no guard, as before.
    detail = _detail(100)
    cfg = ReturnsConfig(**_MANY, max_splits=5)
    out = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
    assert out.num_rows > 0


def test_keys_stay_within_the_chunk_band():
    detail = _detail(100)
    offset, cap = 5_000, 1_000  # cap comfortably exceeds ~100*3 events
    cfg = ReturnsConfig(**_MANY, max_splits=3,
                        event_key_offset=offset, event_key_capacity=cap)
    out = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
    keys = out.column("ReturnEventKey").to_numpy()
    assert keys.min() > offset, "keys must start above the chunk's offset"
    assert keys.max() <= offset + cap, (
        "a ReturnEventKey landed in the next chunk's band (offset + capacity)"
    )
