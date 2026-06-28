"""Tests for complaints pipeline modules: accumulator, micro_agg, runner generation logic."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from src.facts.complaints.accumulator import ComplaintsAccumulator
from src.facts.complaints.micro_agg import micro_aggregate_complaints
from src.facts.complaints.runner import (
    _build_order_lookup,
    _generate_rows_batch,
    _complaints_schema,
    _arrays_to_table,
    _ComplaintsCfg,
    _read_cfg,
    _SEVERITY_VALUES,
    _SEVERITY_WEIGHTS,
    _SEVERITY_CDF,
    _CHANNEL_VALUES,
    _CHANNEL_WEIGHTS,
    _CHANNEL_CDF,
    _STATUS_VALUES,
    _RESOLUTION_TYPES,
    _RESOLUTION_WEIGHTS,
    _RESOLUTION_CDF,
    _ORDER_TYPES_FLAT,
    _GENERAL_TYPES_FLAT,
    _COMPLAINT_TYPES_ORDER,
    _COMPLAINT_TYPES_GENERAL,
    _COMPLAINT_DETAILS,
)


# ===================================================================
# ComplaintsAccumulator
# ===================================================================


class TestComplaintsAccumulator:
    def test_empty_finalize(self):
        acc = ComplaintsAccumulator()
        df = acc.finalize()
        assert df.empty
        assert "CustomerKey" in df.columns
        assert "OrderNumber" in df.columns
        assert "OrderLineNumber" in df.columns

    def test_has_data_false_initially(self):
        acc = ComplaintsAccumulator()
        assert acc.has_data is False

    def test_add_and_finalize(self):
        acc = ComplaintsAccumulator()
        micro = {
            "customer_key": np.array([1, 2, 3], dtype=np.int64),
            "sales_order_number": np.array([100, 200, 300], dtype=np.int64),
            "line_number": np.array([1, 1, 2], dtype=np.int64),
        }
        acc.add(micro)
        assert acc.has_data is True
        df = acc.finalize()
        assert len(df) == 3

    def test_add_none_ignored(self):
        acc = ComplaintsAccumulator()
        acc.add(None)
        assert acc.has_data is False

    def test_add_empty_ignored(self):
        acc = ComplaintsAccumulator()
        acc.add({"customer_key": np.array([], dtype=np.int64)})
        assert acc.has_data is False

    def test_concatenation_across_chunks(self):
        """Each chunk has unique OrderNumbers (chunk_idx × stride),
        so cross-chunk duplicates cannot occur.  Accumulator concatenates
        without dedup for performance."""
        acc = ComplaintsAccumulator()
        acc.add({
            "customer_key": np.array([1, 2], dtype=np.int64),
            "sales_order_number": np.array([100, 200], dtype=np.int64),
            "line_number": np.array([1, 1], dtype=np.int64),
        })
        acc.add({
            "customer_key": np.array([1, 3], dtype=np.int64),
            "sales_order_number": np.array([300, 400], dtype=np.int64),
            "line_number": np.array([1, 1], dtype=np.int64),
        })
        df = acc.finalize()
        assert len(df) == 4  # all rows preserved (no cross-chunk overlap)

    def test_multiple_parts(self):
        acc = ComplaintsAccumulator()
        for i in range(3):
            acc.add({
                "customer_key": np.array([i * 10], dtype=np.int64),
                "sales_order_number": np.array([i * 100], dtype=np.int64),
                "line_number": np.array([1], dtype=np.int64),
            })
        df = acc.finalize()
        assert len(df) == 3


# ===================================================================
# Complaints micro-aggregation
# ===================================================================


class TestMicroAggregateComplaints:
    def _make_table(self, ck, so, ln):
        return pa.table({
            "CustomerKey": pa.array(ck, type=pa.int64()),
            "OrderNumber": pa.array(so, type=pa.int64()),
            "OrderLineNumber": pa.array(ln, type=pa.int64()),
        })

    def test_basic_extraction(self):
        tbl = self._make_table([1, 2, 3], [100, 200, 300], [1, 1, 2])
        result = micro_aggregate_complaints(tbl)
        assert result is not None
        assert len(result["customer_key"]) == 3

    def test_deduplication(self):
        tbl = self._make_table([1, 1, 2], [100, 100, 200], [1, 1, 1])
        result = micro_aggregate_complaints(tbl)
        assert result is not None
        assert len(result["customer_key"]) == 2

    def test_missing_columns(self):
        tbl = pa.table({"CustomerKey": pa.array([1], type=pa.int64())})
        assert micro_aggregate_complaints(tbl) is None

    def test_empty_table(self):
        tbl = pa.table({
            "CustomerKey": pa.array([], type=pa.int64()),
            "OrderNumber": pa.array([], type=pa.int64()),
            "OrderLineNumber": pa.array([], type=pa.int64()),
        })
        assert micro_aggregate_complaints(tbl) is None


# ===================================================================
# Constants and CDFs
# ===================================================================


class TestComplaintsConstants:
    def test_severity_weights_sum(self):
        assert _SEVERITY_WEIGHTS.sum() == pytest.approx(1.0)

    def test_severity_cdf_normalized(self):
        assert _SEVERITY_CDF[-1] == pytest.approx(1.0)

    def test_channel_weights_sum(self):
        assert _CHANNEL_WEIGHTS.sum() == pytest.approx(1.0)

    def test_channel_cdf_normalized(self):
        assert _CHANNEL_CDF[-1] == pytest.approx(1.0)

    def test_resolution_weights_sum(self):
        assert _RESOLUTION_WEIGHTS.sum() == pytest.approx(1.0)

    def test_resolution_cdf_normalized(self):
        assert _RESOLUTION_CDF[-1] == pytest.approx(1.0)

    def test_status_values(self):
        assert set(_STATUS_VALUES) == {"Resolved", "Closed", "Open", "Escalated"}

    def test_flat_pools_cover_all_types(self):
        assert len(_ORDER_TYPES_FLAT) > 0
        assert len(_GENERAL_TYPES_FLAT) > 0
        # Every order type should appear in the flat pool
        for ct in _COMPLAINT_TYPES_ORDER:
            assert ct in _ORDER_TYPES_FLAT
        for ct in _COMPLAINT_TYPES_GENERAL:
            assert ct in _GENERAL_TYPES_FLAT

    def test_detail_keys_match_types(self):
        all_types = set(_COMPLAINT_TYPES_ORDER) | set(_COMPLAINT_TYPES_GENERAL)
        for ct in all_types:
            assert ct in _COMPLAINT_DETAILS
            assert len(_COMPLAINT_DETAILS[ct]) > 0


# ===================================================================
# Config
# ===================================================================


class TestComplaintsCfg:
    def test_defaults(self):
        c = _ComplaintsCfg()
        assert c.enabled is False
        assert c.complaint_rate == 0.03
        assert c.seed == 600

    def test_read_cfg_defaults(self):
        cfg = type("Cfg", (), {"complaints": None})()
        c = _read_cfg(cfg)
        assert c.enabled is False

    def test_read_cfg_custom(self):
        cc_section = type("CC", (), {
            "enabled": True,
            "complaint_rate": 0.05,
            "repeat_complaint_rate": 0.20,
            "max_complaints": 10,
            "resolution_rate": 0.90,
            "escalation_rate": 0.15,
            "avg_response_days": 7,
            "max_response_days": 45,
            "seed": 42,
            "write_chunk_rows": 100_000,
        })()
        cfg = type("Cfg", (), {"complaints": cc_section})()
        c = _read_cfg(cfg)
        assert c.enabled is True
        assert c.complaint_rate == 0.05
        assert c.max_complaints == 10
        assert c.seed == 42


# ===================================================================
# Order lookup
# ===================================================================


class TestBuildOrderLookup:
    def test_basic_lookup(self):
        order_arrays = {
            "CustomerKey": np.array([1, 1, 2, 2, 3], dtype=np.int64),
            "OrderNumber": np.array([100, 101, 200, 201, 300], dtype=np.int64),
            "OrderLineNumber": np.array([1, 1, 1, 2, 1], dtype=np.int64),
        }
        lookup = _build_order_lookup(order_arrays)
        assert 1 in lookup
        assert 2 in lookup
        assert 3 in lookup
        assert len(lookup[1][0]) == 2  # customer 1 has 2 orders
        assert len(lookup[2][0]) == 2  # customer 2 has 2 orders
        assert len(lookup[3][0]) == 1  # customer 3 has 1 order

    def test_empty_arrays(self):
        order_arrays = {
            "CustomerKey": np.array([], dtype=np.int64),
            "OrderNumber": np.array([], dtype=np.int64),
            "OrderLineNumber": np.array([], dtype=np.int64),
        }
        lookup = _build_order_lookup(order_arrays)
        assert len(lookup) == 0

    def test_single_customer(self):
        order_arrays = {
            "CustomerKey": np.array([5, 5, 5], dtype=np.int64),
            "OrderNumber": np.array([10, 11, 12], dtype=np.int64),
            "OrderLineNumber": np.array([1, 1, 1], dtype=np.int64),
        }
        lookup = _build_order_lookup(order_arrays)
        assert len(lookup) == 1
        assert len(lookup[5][0]) == 3


# ===================================================================
# Vectorized generation
# ===================================================================


class TestGenerateRowsBatch:
    NS_PER_DAY = 86_400_000_000_000

    def _make_orders(self, n_customers=3, orders_per=2):
        cust_orders = {}
        for i in range(n_customers):
            ck = (i + 1) * 100
            so = np.arange(orders_per, dtype=np.int64) + ck * 10
            ln = np.ones(orders_per, dtype=np.int64)
            cust_orders[ck] = (so, ln)
        return cust_orders

    def test_basic_generation(self):
        rng = np.random.default_rng(42)
        cust_orders = self._make_orders(3, 2)
        complainer_keys = np.array([100, 200, 300], dtype=np.int64)
        complaints_per = np.array([2, 1, 3], dtype=np.int32)

        result = _generate_rows_batch(
            rng,
            complainer_keys=complainer_keys,
            complaints_per=complaints_per,
            cust_orders=cust_orders,
            g_start_ns=0,
            g_end_ns=365 * self.NS_PER_DAY,
            cfg=_ComplaintsCfg(),
        )

        total = complaints_per.sum()
        assert len(result["ckey"]) == total
        assert len(result["so"]) == total
        assert len(result["date_ns"]) == total
        assert len(result["type"]) == total
        assert len(result["severity"]) == total
        assert len(result["channel"]) == total
        assert len(result["status"]) == total

        # Customer keys correct
        assert set(result["ckey"]) == {100, 200, 300}

        # Severity and channel values are valid
        assert all(s in set(_SEVERITY_VALUES) for s in result["severity"])
        assert all(c in set(_CHANNEL_VALUES) for c in result["channel"])

        # Status values are valid
        assert all(s in set(_STATUS_VALUES) for s in result["status"])

    def test_empty_complaints(self):
        rng = np.random.default_rng(42)
        result = _generate_rows_batch(
            rng,
            complainer_keys=np.array([100], dtype=np.int64),
            complaints_per=np.array([0], dtype=np.int32),
            cust_orders={},
            g_start_ns=0,
            g_end_ns=365 * self.NS_PER_DAY,
            cfg=_ComplaintsCfg(),
        )
        assert len(result["ckey"]) == 0

    def test_no_orders(self):
        """Complainers with no order history get general complaints only."""
        rng = np.random.default_rng(42)
        complainer_keys = np.array([100], dtype=np.int64)
        complaints_per = np.array([5], dtype=np.int32)

        result = _generate_rows_batch(
            rng,
            complainer_keys=complainer_keys,
            complaints_per=complaints_per,
            cust_orders={},
            g_start_ns=0,
            g_end_ns=365 * self.NS_PER_DAY,
            cfg=_ComplaintsCfg(),
        )

        # All should be general complaints (no order linking possible)
        assert all(so == -1 for so in result["so"])
        general_types = set(_COMPLAINT_TYPES_GENERAL)
        assert all(t in general_types for t in result["type"])

    def test_dates_within_range(self):
        rng = np.random.default_rng(42)
        g_start = 100 * self.NS_PER_DAY
        g_end = 200 * self.NS_PER_DAY

        result = _generate_rows_batch(
            rng,
            complainer_keys=np.array([1, 2], dtype=np.int64),
            complaints_per=np.array([10, 10], dtype=np.int32),
            cust_orders={},
            g_start_ns=g_start,
            g_end_ns=g_end,
            cfg=_ComplaintsCfg(),
        )

        assert np.all(result["date_ns"] >= g_start)
        assert np.all(result["date_ns"] <= g_end)

    def test_resolution_dates_clamped(self):
        """Resolution dates should not exceed g_end_ns."""
        rng = np.random.default_rng(42)
        g_end = 10 * self.NS_PER_DAY  # very short window

        result = _generate_rows_batch(
            rng,
            complainer_keys=np.array([1], dtype=np.int64),
            complaints_per=np.array([50], dtype=np.int32),
            cust_orders={},
            g_start_ns=0,
            g_end_ns=g_end,
            cfg=_ComplaintsCfg(resolution_rate=1.0, escalation_rate=0.0,
                              avg_response_days=30, max_response_days=60),
        )

        resolved_mask = result["res_date_ns"] != -1
        if resolved_mask.any():
            assert np.all(result["res_date_ns"][resolved_mask] <= g_end)

    def test_resolved_have_resolution_type(self):
        rng = np.random.default_rng(42)

        result = _generate_rows_batch(
            rng,
            complainer_keys=np.array([1], dtype=np.int64),
            complaints_per=np.array([20], dtype=np.int32),
            cust_orders={},
            g_start_ns=0,
            g_end_ns=365 * self.NS_PER_DAY,
            cfg=_ComplaintsCfg(resolution_rate=1.0, escalation_rate=0.0),
        )

        # All resolved → all should have resolution types
        valid_res_types = set(_RESOLUTION_TYPES)
        for rt in result["res_type"]:
            assert rt in valid_res_types

    def test_unresolved_have_no_resolution(self):
        rng = np.random.default_rng(42)

        result = _generate_rows_batch(
            rng,
            complainer_keys=np.array([1], dtype=np.int64),
            complaints_per=np.array([20], dtype=np.int32),
            cust_orders={},
            g_start_ns=0,
            g_end_ns=365 * self.NS_PER_DAY,
            cfg=_ComplaintsCfg(resolution_rate=0.0, escalation_rate=0.5),
        )

        # None resolved → all res_type should be None, res_date should be -1
        assert all(rt is None for rt in result["res_type"])
        assert np.all(result["res_date_ns"] == -1)
        assert np.all(result["resp_days"] == -1)

    def test_deterministic(self):
        cust_orders = self._make_orders(2, 3)
        kwargs = dict(
            complainer_keys=np.array([100, 200], dtype=np.int64),
            complaints_per=np.array([3, 3], dtype=np.int32),
            cust_orders=cust_orders,
            g_start_ns=0,
            g_end_ns=365 * self.NS_PER_DAY,
            cfg=_ComplaintsCfg(),
        )

        rng1 = np.random.default_rng(99)
        r1 = _generate_rows_batch(rng1, **kwargs)

        rng2 = np.random.default_rng(99)
        r2 = _generate_rows_batch(rng2, **kwargs)

        for key in r1:
            np.testing.assert_array_equal(r1[key], r2[key])


# ===================================================================
# Schema and table assembly
# ===================================================================


class TestComplaintsSchema:
    def test_schema_fields(self):
        schema = _complaints_schema()
        names = schema.names
        assert "ComplaintKey" in names
        assert "CustomerKey" in names
        assert "OrderNumber" in names
        assert "LineNumber" in names
        assert "ComplaintDate" in names
        assert "ResolutionDate" in names
        assert "Severity" in names
        assert "Status" in names
        assert "ResolutionType" in names
        assert "ResponseDays" in names


class TestArraysToTable:
    NS_PER_DAY = 86_400_000_000_000

    def test_basic_assembly(self):
        n = 3
        table = _arrays_to_table(
            out_ckey=np.array([1, 2, 3], dtype=np.int64),
            out_so=np.array([100, -1, 300], dtype=np.int64),
            out_ln=np.array([1, -1, 2], dtype=np.int64),
            out_date_ns=np.array([0, self.NS_PER_DAY, 2 * self.NS_PER_DAY], dtype=np.int64),
            out_res_date_ns=np.array([self.NS_PER_DAY, -1, 3 * self.NS_PER_DAY], dtype=np.int64),
            out_type=np.array(["Product Defect", "Service", "Late Delivery"], dtype=object),
            out_detail=np.array(["detail1", "detail2", "detail3"], dtype=object),
            out_severity=np.array(["Low", "High", "Medium"], dtype=object),
            out_channel=np.array(["Email", "Phone", "Chat"], dtype=object),
            out_status=np.array(["Resolved", "Open", "Closed"], dtype=object),
            out_res_type=np.array(["Refund", None, "Replacement"], dtype=object),
            out_resp_days=np.array([5, -1, 3], dtype=np.int32),
            key_offset=0,
        )

        assert table.num_rows == 3
        assert table.num_columns == 13

        # Nullable columns should have nulls where sentinel -1 was
        so_col = table.column("OrderNumber")
        assert so_col[1].as_py() is None  # -1 → null

        ln_col = table.column("LineNumber")
        assert ln_col[1].as_py() is None

        res_date_col = table.column("ResolutionDate")
        assert res_date_col[1].as_py() is None

        resp_days_col = table.column("ResponseDays")
        assert resp_days_col[1].as_py() is None

    def test_complaint_keys_sequential(self):
        n = 4
        table = _arrays_to_table(
            out_ckey=np.arange(n, dtype=np.int64),
            out_so=np.full(n, -1, dtype=np.int64),
            out_ln=np.full(n, -1, dtype=np.int64),
            out_date_ns=np.zeros(n, dtype=np.int64),
            out_res_date_ns=np.full(n, -1, dtype=np.int64),
            out_type=np.full(n, "Service", dtype=object),
            out_detail=np.full(n, "detail", dtype=object),
            out_severity=np.full(n, "Low", dtype=object),
            out_channel=np.full(n, "Email", dtype=object),
            out_status=np.full(n, "Open", dtype=object),
            out_res_type=np.full(n, None, dtype=object),
            out_resp_days=np.full(n, -1, dtype=np.int32),
            key_offset=10,
        )

        keys = table.column("ComplaintKey").to_pylist()
        assert keys == [11, 12, 13, 14]
