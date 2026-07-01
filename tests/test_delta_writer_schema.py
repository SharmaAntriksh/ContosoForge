"""Regression tests for the Delta writer's canonical-schema handling.

The Sales Delta writer used to derive its write schema by reading the *first*
part file (``pq.read_schema(part_files[0])``). If that part happened to be
atypical (unusual column order, a degenerate null-only column, …) the committed
Delta table silently adopted the wrong schema. The writer now accepts an
authoritative ``canonical_schema`` (the run's ``WorkerSchemaBundle`` schema for
the table) and only falls back to sniffing the first part for untrusted inputs.

These tests assert:
  * the authoritative schema is honored when supplied,
  * the first-part fallback still works when it is not, and
  * an atypical first part no longer decides the committed schema.
"""
from __future__ import annotations

import pytest

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq  # noqa: E402

pytest.importorskip("deltalake")
from deltalake import DeltaTable  # noqa: E402

from src.facts.sales.sales_writer.delta import (  # noqa: E402
    write_delta_from_parquet_parts,
    write_delta_partitioned,
)


def _auth_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("CustomerKey", pa.int32()),
            pa.field("UnitPrice", pa.float64()),
            pa.field("NetPrice", pa.float64()),
            pa.field("UnitCost", pa.float64()),
            pa.field("DiscountAmount", pa.float64()),
            pa.field("Year", pa.int16()),
            pa.field("Month", pa.int16()),
        ]
    )


def _write_part(path: str, schema: pa.Schema, n: int = 4) -> None:
    cols = {}
    for f in schema:
        if pa.types.is_integer(f.type):
            cols[f.name] = pa.array(list(range(n)), type=f.type)
        else:
            cols[f.name] = pa.array([float(i) for i in range(n)], type=f.type)
    pq.write_table(pa.table(cols, schema=schema), path)


def _committed_schema(out) -> pa.Schema:
    """Read a committed Delta table's schema as a *pyarrow* Schema.

    ``DeltaTable.schema().to_arrow()`` returns an arro3 schema whose types
    stringify differently from pyarrow's; normalize via the Arrow C-schema
    interface so name/type comparisons are apples-to-apples.
    """
    return pa.schema(DeltaTable(str(out)).schema().to_arrow())


def _names_types(schema: pa.Schema) -> set[tuple[str, str]]:
    return {(f.name, str(f.type)) for f in schema}


class TestDeltaCanonicalSchema:
    def test_authoritative_schema_is_honored(self, tmp_path):
        auth = _auth_schema()
        parts = tmp_path / "parts"
        parts.mkdir()
        _write_part(str(parts / "delta_part_0001.parquet"), auth)
        _write_part(str(parts / "delta_part_0002.parquet"), auth)

        out = tmp_path / "delta"
        write_delta_from_parquet_parts(
            parts_folder=str(parts),
            delta_output_folder=str(out),
            canonical_schema=auth,
            validate_schema=None,
        )
        committed = _committed_schema(out)
        assert _names_types(committed) == _names_types(auth)
        assert list(committed.names) == list(auth.names)

    def test_fallback_reads_first_part_when_no_schema(self, tmp_path):
        auth = _auth_schema()
        parts = tmp_path / "parts"
        parts.mkdir()
        _write_part(str(parts / "delta_part_0001.parquet"), auth)

        out = tmp_path / "delta"
        write_delta_from_parquet_parts(
            parts_folder=str(parts),
            delta_output_folder=str(out),
            validate_schema=None,
        )
        committed = _committed_schema(out)
        assert _names_types(committed) == _names_types(auth)

    def test_authoritative_schema_overrides_atypical_first_part(self, tmp_path):
        """The bug this fix closes: the first part must not decide the schema."""
        auth = _auth_schema()
        # part[0] carries the same columns/types but in a different order — the
        # kind of "atypical first part" that used to leak into the commit.
        order = [1, 0, 2, 3, 4, 5, 6]  # UnitPrice, CustomerKey, ...
        reordered = pa.schema([auth.field(i) for i in order])

        parts = tmp_path / "parts"
        parts.mkdir()
        _write_part(str(parts / "delta_part_0001.parquet"), reordered)
        _write_part(str(parts / "delta_part_0002.parquet"), auth)

        # Fallback (no canonical_schema): committed order follows the first part.
        out_fb = tmp_path / "delta_fb"
        write_delta_from_parquet_parts(
            parts_folder=str(parts),
            delta_output_folder=str(out_fb),
            validate_schema=None,
        )
        fb_names = list(_committed_schema(out_fb).names)

        # Authoritative schema supplied: committed order follows it, not part[0].
        out_auth = tmp_path / "delta_auth"
        write_delta_from_parquet_parts(
            parts_folder=str(parts),
            delta_output_folder=str(out_auth),
            canonical_schema=auth,
            validate_schema=None,
        )
        auth_names = list(_committed_schema(out_auth).names)

        assert fb_names == list(reordered.names)  # first part decided the fallback
        assert auth_names == list(auth.names)      # authoritative schema won
        assert fb_names != auth_names              # ...and it actually mattered

    def test_high_level_entry_threads_schema(self, tmp_path):
        # Returns requires no pricing cols, so a minimal schema passes validation.
        auth = pa.schema(
            [pa.field("ReturnKey", pa.int32()), pa.field("Quantity", pa.int32())]
        )
        parts = tmp_path / "parts"
        parts.mkdir()
        _write_part(str(parts / "delta_part_0001.parquet"), auth)

        out = tmp_path / "delta"
        write_delta_partitioned(
            parts_folder=str(parts),
            delta_output_folder=str(out),
            table_name="Returns",
            canonical_schema=auth,
        )
        committed = _committed_schema(out)
        assert list(committed.names) == list(auth.names)
