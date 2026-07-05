"""Regression tests for the typed ChunkResult worker->collector protocol.

Pins the two behaviors the golden snapshot cannot see (its fixtures disable the
streamed secondary facts): the collector feeds each ChunkResult.aggs payload
into its registered accumulator, and it records the per-table artifacts for the
manifest / progress log.
"""
from __future__ import annotations

from src.facts.sales.chunk_result import ChunkResult
from src.facts.sales.output_assembler import ChunkResultCollector
from src.facts.sales.output_paths import (
    TABLE_SALES,
    TABLE_SALES_ORDER_DETAIL,
    TABLE_SALES_ORDER_HEADER,
    TABLE_SALES_RETURN,
)


class _RecordingAcc:
    """Stand-in accumulator that records every payload handed to add()/add_sales()."""

    def __init__(self):
        self.seen = []

    def add(self, micro):
        self.seen.append(micro)

    # Budget's accumulator uses add_sales(); mirror it onto the same log.
    add_sales = add


def _collector(tables, budget=None, inv=None, wl=None, comp=None):
    return ChunkResultCollector(tables, budget, inv, wl, comp)


def test_aggs_are_routed_to_the_matching_accumulator():
    budget, inv, wl, comp = (_RecordingAcc() for _ in range(4))
    c = _collector([TABLE_SALES], budget, inv, wl, comp)

    c.record(
        ChunkResult(
            chunk_idx=0,
            outputs={TABLE_SALES: "sales_chunk0000.parquet"},
            aggs={
                "_budget_agg": {"b": 1},
                "_inventory_agg": {"i": 1},
                "_wishlists_agg": {"w": 1},
                "_complaints_agg": {"c": 1},
            },
        ),
        completed_units=1,
        total_units=1,
    )

    assert budget.seen == [{"b": 1}]
    assert inv.seen == [{"i": 1}]
    assert wl.seen == [{"w": 1}]
    assert comp.seen == [{"c": 1}]


def test_absent_agg_feeds_none_not_skipped():
    # A chunk that produced no inventory micro-agg still calls add(None); the
    # accumulator's own None-guard makes it a no-op. This mirrors the old
    # r.pop("_inventory_agg", None) behavior for multi-table results.
    inv = _RecordingAcc()
    c = _collector([TABLE_SALES], inv=inv)

    c.record(
        ChunkResult(chunk_idx=2, outputs={TABLE_SALES: "sales_chunk0002.parquet"}, aggs={}),
        completed_units=1,
        total_units=1,
    )

    assert inv.seen == [None]


def test_disabled_accumulator_is_skipped():
    # A None accumulator (fact disabled) is never called — no attribute error.
    c = _collector([TABLE_SALES], budget=None)
    c.record(
        ChunkResult(chunk_idx=0, outputs={TABLE_SALES: "s0.parquet"}, aggs={"_budget_agg": {"b": 1}}),
        completed_units=1,
        total_units=1,
    )
    # Nothing to assert beyond "did not raise"; the file was still recorded.
    assert c.created_files == ["s0.parquet"]


def test_string_refs_recorded_in_created_files_delta_refs_not():
    tables = [TABLE_SALES, TABLE_SALES_ORDER_DETAIL]
    c = _collector(tables)

    # parquet/csv path (str) -> counts as a created file
    c.record(
        ChunkResult(chunk_idx=0, outputs={TABLE_SALES: "sales_chunk0000.parquet"}),
        completed_units=1, total_units=2,
    )
    # delta part descriptor (dict) -> recorded per-table but not a "file"
    c.record(
        ChunkResult(
            chunk_idx=1,
            outputs={TABLE_SALES_ORDER_DETAIL: {"part": "delta_part_0001.parquet", "rows": 5}},
        ),
        completed_units=2, total_units=2,
    )

    assert c.created_files == ["sales_chunk0000.parquet"]
    assert c.created_by_table[TABLE_SALES] == ["sales_chunk0000.parquet"]
    assert c.created_by_table[TABLE_SALES_ORDER_DETAIL] == [
        {"part": "delta_part_0001.parquet", "rows": 5}
    ]


def test_multi_table_result_records_every_table():
    tables = [TABLE_SALES, TABLE_SALES_ORDER_DETAIL, TABLE_SALES_ORDER_HEADER, TABLE_SALES_RETURN]
    c = _collector(tables)
    c.record(
        ChunkResult(
            chunk_idx=7,
            outputs={
                TABLE_SALES_ORDER_DETAIL: "order_detail_chunk0007.parquet",
                TABLE_SALES_ORDER_HEADER: "order_header_chunk0007.parquet",
                TABLE_SALES_RETURN: "returns_chunk0007.parquet",
            },
        ),
        completed_units=1, total_units=1,
    )
    assert c.created_by_table[TABLE_SALES] == []  # not produced this chunk
    assert c.created_by_table[TABLE_SALES_ORDER_DETAIL] == ["order_detail_chunk0007.parquet"]
    assert c.created_by_table[TABLE_SALES_ORDER_HEADER] == ["order_header_chunk0007.parquet"]
    assert c.created_by_table[TABLE_SALES_RETURN] == ["returns_chunk0007.parquet"]
    assert len(c.created_files) == 3
