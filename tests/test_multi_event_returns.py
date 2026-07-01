"""Tests for multi-event returns (split returns across multiple dates)."""
from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest

from src.facts.sales.sales_worker.returns_builder import (
    ReturnsConfig,
    RETURNS_SCHEMA,
    _validate_cfg,
    build_sales_returns_from_detail,
)
from src.exceptions import SalesError


class TestReturnsConfigValidationRaisesSalesError:
    """Config-shape validation routes through the domain SalesError, not a bare
    RuntimeError (Finding #42 / gotcha #9)."""

    @pytest.mark.parametrize("kwargs", [
        {"enabled": "yes", "return_rate": 0.1},           # enabled not a bool
        {"enabled": True, "return_rate": 1.5},            # rate out of [0,1]
        {"enabled": True, "return_rate": float("nan")},   # rate not finite
        {"enabled": True, "return_rate": 0.1, "min_lag_days": -1},
        {"enabled": True, "return_rate": 0.1, "max_lag_days": -3},
    ])
    def test_bad_config_raises_saleserror(self, kwargs):
        with pytest.raises(SalesError):
            _validate_cfg(ReturnsConfig(**kwargs))


def _rng():
    return np.random.default_rng(12345)


def _make_detail(n: int = 100) -> pa.Table:
    rng = _rng()
    return pa.table({
        "OrderNumber": pa.array(np.arange(1, n + 1, dtype=np.int32)),
        "OrderLineNumber": pa.array(np.ones(n, dtype=np.int32)),
        "DeliveryDate": pa.array(
            np.array(["2024-03-15"] * n, dtype="datetime64[D]"), type=pa.date32(),
        ),
        "Quantity": pa.array(rng.integers(2, 10, size=n, dtype=np.int32)),
        "NetPrice": pa.array(rng.uniform(20.0, 200.0, size=n)),
        "IsOrderDelayed": pa.array(rng.integers(0, 2, size=n, dtype=np.int32)),
    })


class TestBackwardCompat:
    def test_no_splits_when_rate_zero(self):
        detail = _make_detail()
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, split_return_rate=0.0)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        assert result.num_rows > 0
        seq = result.column("ReturnSequence").to_numpy()
        assert np.all(seq == 1)

    def test_schema_includes_return_sequence(self):
        assert "ReturnSequence" in RETURNS_SCHEMA.names
        assert "ReturnEventKey" in RETURNS_SCHEMA.names

    def test_empty_table_has_return_sequence(self):
        detail = _make_detail(10)
        cfg = ReturnsConfig(enabled=False)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        assert result.num_rows == 0
        assert "ReturnSequence" in result.schema.names


class TestMultiEventReturns:
    def test_splits_produce_multiple_rows(self):
        detail = _make_detail(200)
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            split_return_rate=1.0, max_splits=3,
        )
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        seq = result.column("ReturnSequence").to_numpy()
        # With split_rate=1.0, some rows should have seq > 1
        assert np.any(seq > 1)

    def test_quantity_sums_match(self):
        detail = _make_detail(100)
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            split_return_rate=1.0, max_splits=3,
            full_line_probability=1.0,
        )
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)

        so = result.column("OrderNumber").to_numpy()
        line = result.column("OrderLineNumber").to_numpy()
        ret_qty = result.column("ReturnQuantity").to_numpy()
        orig_qty = detail.column("Quantity").to_numpy()

        # For each (so, line) group, sum of ReturnQuantity should equal original Quantity
        for order_num in np.unique(so):
            mask = so == order_num
            total_returned = ret_qty[mask].sum()
            orig = orig_qty[order_num - 1]  # 1-indexed
            assert total_returned == orig, (
                f"Order {order_num}: returned {total_returned} != original {orig}"
            )

    def test_return_sequence_starts_at_1(self):
        detail = _make_detail(100)
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            split_return_rate=0.5, max_splits=3,
        )
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)

        so = result.column("OrderNumber").to_numpy()
        line = result.column("OrderLineNumber").to_numpy()
        seq = result.column("ReturnSequence").to_numpy()

        # Every group should start at 1
        seen = set()
        for i in range(len(so)):
            key = (int(so[i]), int(line[i]))
            if key not in seen:
                assert seq[i] == 1, f"Group {key} starts at {seq[i]}, expected 1"
                seen.add(key)

    def test_dates_non_decreasing_within_group(self):
        detail = _make_detail(100)
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            split_return_rate=1.0, max_splits=3,
            min_lag_days=1, max_lag_days=5,
            split_min_gap=3, split_max_gap=10,
        )
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)

        so = result.column("OrderNumber").to_numpy()
        dates = result.column("ReturnDate").to_numpy(zero_copy_only=False)
        seq = result.column("ReturnSequence").to_numpy()

        prev_date = {}
        for i in range(len(so)):
            key = int(so[i])
            if seq[i] == 1:
                prev_date[key] = dates[i]
            else:
                assert dates[i] >= prev_date[key], (
                    f"Order {key} seq {seq[i]}: date {dates[i]} < prev {prev_date[key]}"
                )
                prev_date[key] = dates[i]

    def test_each_event_has_positive_quantity(self):
        detail = _make_detail(100)
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            split_return_rate=1.0, max_splits=3,
        )
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        ret_qty = result.column("ReturnQuantity").to_numpy()
        assert np.all(ret_qty >= 1)

    def test_dates_non_decreasing_wide_lag_range(self):
        """RETURNS-1: with a WIDE base-lag range and small split gaps, an
        independent per-event base lag would let a later event land before an
        earlier one. Sharing one base lag per parent line keeps dates monotonic."""
        detail = _make_detail(200)
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            split_return_rate=1.0, max_splits=3,
            min_lag_days=1, max_lag_days=90,   # wide -> exposes per-event base bug
            split_min_gap=1, split_max_gap=2,
        )
        result = build_sales_returns_from_detail(detail, chunk_seed=7, cfg=cfg)
        so = result.column("OrderNumber").to_numpy()
        dates = result.column("ReturnDate").to_numpy(zero_copy_only=False)
        seq = result.column("ReturnSequence").to_numpy()

        prev_date = {}
        for i in range(len(so)):
            key = int(so[i])
            if seq[i] == 1:
                prev_date[key] = dates[i]
            else:
                assert dates[i] >= prev_date[key], (
                    f"Order {key} seq {seq[i]}: {dates[i]} < prev {prev_date[key]}"
                )
                prev_date[key] = dates[i]
        # Sanity: splits actually happened (otherwise the test proves nothing).
        assert np.any(seq > 1)


class TestReturnEventKey:
    def test_sequential_keys(self):
        detail = _make_detail(50)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, event_key_offset=0)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        keys = result.column("ReturnEventKey").to_numpy()
        assert keys[0] == 1
        assert np.all(np.diff(keys) == 1)

    def test_offset_applied(self):
        detail = _make_detail(50)
        cfg = ReturnsConfig(enabled=True, return_rate=1.0, event_key_offset=100000)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        keys = result.column("ReturnEventKey").to_numpy()
        assert keys[0] == 100001

    def test_no_overlap_across_chunks(self):
        detail = _make_detail(50)
        cfg1 = ReturnsConfig(enabled=True, return_rate=1.0, event_key_offset=0)
        cfg2 = ReturnsConfig(enabled=True, return_rate=1.0, event_key_offset=10000)
        r1 = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg1)
        r2 = build_sales_returns_from_detail(detail, chunk_seed=99, cfg=cfg2)
        keys1 = set(r1.column("ReturnEventKey").to_numpy().tolist())
        keys2 = set(r2.column("ReturnEventKey").to_numpy().tolist())
        assert keys1.isdisjoint(keys2)


class TestDeterminism:
    def test_same_seed_same_output(self):
        detail = _make_detail(100)
        cfg = ReturnsConfig(
            enabled=True, return_rate=0.5,
            split_return_rate=0.3, max_splits=3,
        )
        r1 = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        r2 = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        assert r1.equals(r2)


class TestCategoryAwareSampling:
    """Logistics reasons should only appear on delayed orders."""

    from src.defaults import RETURN_REASON_LOGISTICS_KEYS
    LOGISTICS_KEYS = RETURN_REASON_LOGISTICS_KEYS

    def _make_all_ontime(self, n: int = 200) -> pa.Table:
        rng = _rng()
        return pa.table({
            "OrderNumber": pa.array(np.arange(1, n + 1, dtype=np.int32)),
            "OrderLineNumber": pa.array(np.ones(n, dtype=np.int32)),
            "DeliveryDate": pa.array(
                np.array(["2024-03-15"] * n, dtype="datetime64[D]"), type=pa.date32(),
            ),
            "Quantity": pa.array(rng.integers(1, 5, size=n, dtype=np.int32)),
            "NetPrice": pa.array(rng.uniform(10.0, 100.0, size=n)),
            "IsOrderDelayed": pa.array(np.zeros(n, dtype=np.int32)),
        })

    def _make_all_delayed(self, n: int = 200) -> pa.Table:
        rng = _rng()
        return pa.table({
            "OrderNumber": pa.array(np.arange(1, n + 1, dtype=np.int32)),
            "OrderLineNumber": pa.array(np.ones(n, dtype=np.int32)),
            "DeliveryDate": pa.array(
                np.array(["2024-03-15"] * n, dtype="datetime64[D]"), type=pa.date32(),
            ),
            "Quantity": pa.array(rng.integers(1, 5, size=n, dtype=np.int32)),
            "NetPrice": pa.array(rng.uniform(10.0, 100.0, size=n)),
            "IsOrderDelayed": pa.array(np.ones(n, dtype=np.int32)),
        })

    def test_ontime_orders_no_logistics_reasons(self):
        detail = self._make_all_ontime(500)
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            logistics_keys=self.LOGISTICS_KEYS,
        )
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        reasons = set(result.column("ReturnReasonKey").to_numpy().tolist())
        assert not reasons & self.LOGISTICS_KEYS, (
            f"On-time orders should not get Logistics reasons, got: {reasons & self.LOGISTICS_KEYS}"
        )

    def test_delayed_orders_can_get_logistics_reasons(self):
        detail = self._make_all_delayed(500)
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            logistics_keys=self.LOGISTICS_KEYS,
        )
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        reasons = set(result.column("ReturnReasonKey").to_numpy().tolist())
        assert reasons & self.LOGISTICS_KEYS, (
            f"Delayed orders should include Logistics reasons, got: {reasons}"
        )

    def test_no_logistics_keys_falls_back_to_uniform(self):
        detail = self._make_all_ontime(200)
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            logistics_keys=frozenset(),  # empty — no category filtering
        )
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        reasons = set(result.column("ReturnReasonKey").to_numpy().tolist())
        # Without logistics filtering, all reason keys can appear
        assert len(reasons) > 1


class TestEdgeCases:
    def _make_single_unit_detail(self, n=50):
        """All orders have Quantity=1 — cannot be split."""
        import pyarrow as pa
        return pa.table({
            "OrderNumber": np.arange(1, n + 1, dtype=np.int32),
            "OrderLineNumber": np.ones(n, dtype=np.int32),
            "DeliveryDate": np.array(["2024-03-15"] * n, dtype="datetime64[D]"),
            "Quantity": np.ones(n, dtype=np.int32),
            "NetPrice": np.full(n, 25.0),
            "IsOrderDelayed": np.zeros(n, dtype=np.int8),
        })

    def test_single_unit_return_quantity_is_one(self):
        """When Quantity=1, ReturnQuantity cannot exceed 1."""
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0,
            split_return_rate=0.5,
            max_splits=3,
        )
        detail = self._make_single_unit_detail(200)
        result = build_sales_returns_from_detail(detail, chunk_seed=42, cfg=cfg)
        if result.num_rows > 0:
            ret_qty = result.column("ReturnQuantity").to_numpy()
            assert (ret_qty >= 1).all()
            assert (ret_qty <= 1).all()


class TestFrictionCoupling:
    """Phase 3.4: the fulfillment friction latent (keyed on OrderNumber+line)
    couples returns to delivery — higher friction => more / faster returns."""

    def _detail(self, n):
        return pa.table({
            "OrderNumber": pa.array(np.arange(1, n + 1, dtype=np.int32)),
            "OrderLineNumber": pa.array(np.ones(n, dtype=np.int32)),
            "DeliveryDate": pa.array(
                np.array(["2024-03-15"] * n, dtype="datetime64[D]"), type=pa.date32()),
            "Quantity": pa.array(np.full(n, 4, dtype=np.int32)),
            "NetPrice": pa.array(np.full(n, 100.0)),
            "IsOrderDelayed": pa.array(np.zeros(n, dtype=np.int32)),
        })

    def test_high_friction_lines_returned_more(self):
        from src.facts.sales.sales_logic.core.delivery import line_friction
        n = 6000
        cfg = ReturnsConfig(enabled=True, return_rate=0.3, friction_return_boost=2.0)
        result = build_sales_returns_from_detail(self._detail(n), chunk_seed=42, cfg=cfg)
        returned = set(int(x) for x in result.column("OrderNumber").to_numpy().tolist())
        o = np.arange(1, n + 1, dtype=np.int64)
        fr = line_friction(o, np.ones(n, dtype=np.int32))
        is_ret = np.array([int(x) in returned for x in o])
        assert fr[is_ret].mean() > fr[~is_ret].mean() + 0.05

    def test_high_friction_shorter_lag(self):
        from src.facts.sales.sales_logic.core.delivery import line_friction
        n = 6000
        cfg = ReturnsConfig(
            enabled=True, return_rate=1.0, min_lag_days=1, max_lag_days=30,
            lag_distribution="uniform", friction_lag_shorten=0.8,
        )
        result = build_sales_returns_from_detail(self._detail(n), chunk_seed=42, cfg=cfg)
        ords = result.column("OrderNumber").to_numpy().astype(np.int64)
        rdate = result.column("ReturnDate").to_numpy().astype("datetime64[D]")
        lag = (rdate - np.datetime64("2024-03-15")).astype("timedelta64[D]").astype(int)
        fr = line_friction(ords, np.ones(ords.size, dtype=np.int32))
        hi = fr >= np.median(fr)
        assert lag[hi].mean() < lag[~hi].mean()

    def test_boost_and_shorten_zero_is_byte_identical(self):
        detail = self._detail(500)
        r0 = build_sales_returns_from_detail(
            detail, chunk_seed=7, cfg=ReturnsConfig(enabled=True, return_rate=0.4))
        rf = build_sales_returns_from_detail(
            detail, chunk_seed=7,
            cfg=ReturnsConfig(enabled=True, return_rate=0.4,
                              friction_return_boost=0.0, friction_lag_shorten=0.0))
        assert r0.equals(rf)


class TestDefaultsConsistency:
    def test_dimension_matches_defaults(self):
        from src.defaults import RETURN_REASONS as canonical
        from src.dimensions.return_reasons import RETURN_REASONS as dim_reasons
        assert len(dim_reasons) == len(canonical)
        for (dk, dl, dc), (ck, cl, cc, _w) in zip(dim_reasons, canonical):
            assert dk == ck
            assert dl == cl
            assert dc == cc
