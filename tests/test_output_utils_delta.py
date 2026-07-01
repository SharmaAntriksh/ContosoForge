"""Regression tests for deltaparquet dimension packaging with all-null columns.

Background
----------
An entirely-null object column (common on small / short-window runs — e.g.
``employees.parquet`` with no terminations, so ``TerminationDate`` and
``TerminationReason`` are all null; or ``stores.parquet`` with no closed stores)
makes ``pa.Table.from_pandas`` infer the Arrow ``null`` type. delta-rs rejects
that type ("Invalid data type for Delta Lake: Null"), which crashed the
"Creating Final Output Folder" step for ``file_format: deltaparquet``.

The fix casts null-typed columns to a concrete type — derived from the static
schema where possible — before ``write_deltalake``. These tests assert:

  * ``_coerce_null_columns`` picks schema-derived types (date -> date32,
    string -> string) and falls back to string for unknown columns/tables, and
  * the full ``create_final_output_folder`` deltaparquet dim path commits a Delta
    table with concrete column types that round-trips.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

pa = pytest.importorskip("pyarrow")
pytest.importorskip("deltalake")
from deltalake import DeltaTable  # noqa: E402

from src.utils.output_utils import (  # noqa: E402
    _coerce_null_columns,
    _dim_table_name_for_file,
    create_final_output_folder,
)


def _committed_schema(path) -> pa.Schema:
    """Read a committed Delta table's schema as a pyarrow Schema.

    ``DeltaTable.schema().to_arrow()`` returns an arro3 schema whose types
    stringify differently from pyarrow's; normalize via the Arrow C-schema
    interface so name/type comparisons are apples-to-apples.
    """
    return pa.schema(DeltaTable(str(path)).schema().to_arrow())


def _field_type(schema: pa.Schema, name: str) -> str:
    return str(schema.field(name).type)


class TestCoerceNullColumns:
    def test_stem_resolves_to_static_schema_table(self):
        assert _dim_table_name_for_file("employees") == "Employees"
        assert _dim_table_name_for_file("stores") == "Stores"
        assert _dim_table_name_for_file("customer_profile") == "CustomerProfile"
        assert _dim_table_name_for_file("not_a_real_dim") is None

    def test_null_columns_take_schema_types(self):
        table = pa.table(
            {
                "EmployeeKey": pa.array([1, 2], type=pa.int32()),
                "TerminationDate": pa.array([None, None], type=pa.null()),
                "TerminationReason": pa.array([None, None], type=pa.null()),
            }
        )
        out = _coerce_null_columns(table, "Employees")

        assert _field_type(out.schema, "EmployeeKey") == "int32"
        assert _field_type(out.schema, "TerminationDate") == "date32[day]"
        assert _field_type(out.schema, "TerminationReason") == "string"
        # No null-typed columns survive.
        assert not any(pa.types.is_null(f.type) for f in out.schema)
        # Populated column is untouched; null columns stay all-null.
        assert out.column("EmployeeKey").to_pylist() == [1, 2]
        assert out.column("TerminationDate").to_pylist() == [None, None]

    def test_all_null_concrete_column_is_realigned_to_schema(self):
        # force_date32 leaves an all-null date column as timestamp (pandas
        # .dt.date -> datetime64 for all-NaT); it must be pulled back to date32.
        table = pa.table(
            {
                "EmployeeKey": pa.array([1, 2], type=pa.int32()),
                "TerminationDate": pa.array([None, None], type=pa.timestamp("s")),
            }
        )
        out = _coerce_null_columns(table, "Employees")
        assert _field_type(out.schema, "TerminationDate") == "date32[day]"

    def test_populated_concrete_column_is_never_retyped(self):
        # A populated column whose arrow type differs from the schema must be
        # left alone (only entirely-null columns are realigned).
        import datetime as dt

        table = pa.table(
            {
                "TerminationDate": pa.array([None, None], type=pa.timestamp("s")),
                "HireDate": pa.array(
                    [None, dt.datetime(2020, 1, 1)], type=pa.timestamp("s")
                ),
            }
        )
        out = _coerce_null_columns(table, "Employees")
        # HireDate has a value -> not entirely null -> untouched.
        assert _field_type(out.schema, "HireDate") == "timestamp[s]"
        # TerminationDate is all-null -> realigned.
        assert _field_type(out.schema, "TerminationDate") == "date32[day]"

    def test_unknown_column_and_table_fall_back_to_string(self):
        table = pa.table({"MysteryCol": pa.array([None, None], type=pa.null())})
        # Unknown table -> string fallback.
        out = _coerce_null_columns(table, None)
        assert _field_type(out.schema, "MysteryCol") == "string"
        # Known table but column not in its schema -> string fallback.
        out2 = _coerce_null_columns(table, "Employees")
        assert _field_type(out2.schema, "MysteryCol") == "string"


class TestDeltaparquetDimPackaging:
    def _make_cfg(self, *, force_date32: bool):
        return SimpleNamespace(
            customers=SimpleNamespace(total_customers=2),
            packaging=SimpleNamespace(
                dim_parquet_compression="snappy",
                dim_parquet_compression_level=None,
                dim_force_date32=force_date32,
            ),
        )

    @pytest.mark.parametrize("force_date32", [True, False])
    def test_all_null_columns_do_not_crash_and_round_trip(self, tmp_path, force_date32):
        # A realistic "no terminations" employees dim: TerminationDate and
        # TerminationReason are entirely null; HireDate is populated.
        df = pd.DataFrame(
            {
                "EmployeeKey": pd.array([1, 2], dtype="int32"),
                "HireDate": pd.to_datetime(["2020-01-15", "2021-06-01"]),
                "TerminationDate": pd.Series([None, None], dtype="object"),
                "TerminationReason": pd.Series([None, None], dtype="object"),
            }
        )
        parquet_dims = tmp_path / "dims_in"
        parquet_dims.mkdir()
        df.to_parquet(parquet_dims / "employees.parquet", index=False)

        # Sanity: the on-disk parquet really carries an Arrow null-typed column
        # (the exact input that used to crash the Delta write).
        raw = pa.Table.from_pandas(
            pd.read_parquet(parquet_dims / "employees.parquet"), preserve_index=False
        )
        assert pa.types.is_null(raw.schema.field("TerminationReason").type)

        final_root = tmp_path / "out"
        final_folder = create_final_output_folder(
            final_folder_root=final_root,
            parquet_dims=parquet_dims,
            sales_cfg=SimpleNamespace(total_rows=0),
            file_format="deltaparquet",
            cfg=self._make_cfg(force_date32=force_date32),
        )

        delta_dir = final_folder / "dimensions" / "employees"
        assert (delta_dir / "_delta_log").is_dir()

        schema = _committed_schema(delta_dir)
        # Null-typed columns became concrete schema-derived types.
        assert _field_type(schema, "TerminationDate") == "date32[day]"
        assert _field_type(schema, "TerminationReason") == "string"
        assert not any(pa.types.is_null(f.type) for f in schema)

        # Round-trips: 2 rows, the optional columns still all-null.
        back = DeltaTable(str(delta_dir)).to_pyarrow_table()
        assert back.num_rows == 2
        assert set(back.column("EmployeeKey").to_pylist()) == {1, 2}
        assert back.column("TerminationDate").to_pylist() == [None, None]
        assert back.column("TerminationReason").to_pylist() == [None, None]
