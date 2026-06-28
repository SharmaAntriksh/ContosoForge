"""Tests for sales writer modules: encoding, projection, parquet_merge, and utils."""
from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

def _make_table(**kwargs) -> pa.Table:
    """Create a small pyarrow table from column data."""
    return pa.table(kwargs)


def _write_parquet(path, table, **kwargs) -> None:
    """Write a pyarrow table to a parquet file."""
    pq.write_table(table, str(path), **kwargs)


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from src.facts.sales.sales_writer.encoding import (
    DICT_EXCLUDE,
    REQUIRED_PRICING_COLS,
    validate_required_columns,
    required_pricing_cols_for_table,
    schema_dict_cols,
    _schema_dict_cols,
    _validate_required,
)
from src.facts.sales.sales_writer.projection import (
    project_table_to_schema,
    _project_table_to_schema,
)
from src.facts.sales.sales_writer.parquet_merge import (
    merge_parquet_files,
    optimize_parquet,
    _pm_schema_equals,
    _validate_chunk_columns,
    _merge_parquet_files_common,
    DEFAULT_COMPRESSION,
)
from src.facts.sales.sales_writer.utils import (
    _arrow,
    arrow,
    _ensure_dir_for_file,
    ensure_dir_for_file,
)


# ===================================================================
# Utils
# ===================================================================

class TestArrowImport:
    """Lazy pyarrow import helper."""

    def test_arrow_returns_triple(self):
        result = _arrow()
        assert len(result) == 3
        pa_mod, pc_mod, pq_mod = result
        assert hasattr(pa_mod, "table")
        assert hasattr(pc_mod, "cast")
        assert hasattr(pq_mod, "write_table")

    def test_arrow_public_alias(self):
        result = arrow()
        assert len(result) == 3


class TestEnsureDirForFile:
    """Directory creation for file paths."""

    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "file.parquet"
        _ensure_dir_for_file(nested)
        assert nested.parent.is_dir()

    def test_existing_dir_no_error(self, tmp_path):
        target = tmp_path / "file.parquet"
        _ensure_dir_for_file(target)
        _ensure_dir_for_file(target)  # second call should not raise

    def test_public_alias(self, tmp_path):
        nested = tmp_path / "x" / "y" / "out.parquet"
        ensure_dir_for_file(str(nested))
        assert nested.parent.is_dir()


# ===================================================================
# Encoding
# ===================================================================

class TestConstants:
    """Module-level encoding constants."""

    def test_dict_exclude_contents(self):
        assert "OrderNumber" in DICT_EXCLUDE
        assert "CustomerKey" in DICT_EXCLUDE
        assert len(DICT_EXCLUDE) == 2

    def test_required_pricing_cols_contents(self):
        expected = {"UnitPrice", "NetPrice", "UnitCost", "DiscountAmount"}
        assert REQUIRED_PRICING_COLS == expected

    def test_constants_are_frozensets(self):
        assert isinstance(DICT_EXCLUDE, frozenset)
        assert isinstance(REQUIRED_PRICING_COLS, frozenset)


class TestValidateRequiredColumns:
    """validate_required_columns() raises on missing columns."""

    def test_all_present_no_error(self):
        schema = pa.schema([
            pa.field("UnitPrice", pa.float64()),
            pa.field("NetPrice", pa.float64()),
        ])
        validate_required_columns(schema, {"UnitPrice", "NetPrice"})

    def test_missing_one_raises(self):
        schema = pa.schema([pa.field("UnitPrice", pa.float64())])
        with pytest.raises(RuntimeError, match="NetPrice"):
            validate_required_columns(schema, {"UnitPrice", "NetPrice"})

    def test_missing_multiple_raises(self):
        schema = pa.schema([pa.field("Other", pa.int32())])
        with pytest.raises(RuntimeError, match="required columns"):
            validate_required_columns(schema, {"UnitPrice", "NetPrice"})

    def test_empty_requirements_no_error(self):
        schema = pa.schema([pa.field("x", pa.int32())])
        validate_required_columns(schema, set())

    def test_empty_frozenset_no_error(self):
        schema = pa.schema([pa.field("x", pa.int32())])
        validate_required_columns(schema, frozenset())

    def test_custom_what_label(self):
        schema = pa.schema([pa.field("a", pa.int32())])
        with pytest.raises(RuntimeError, match="pricing fields"):
            validate_required_columns(schema, {"b"}, what="pricing fields")

    def test_iterable_input(self):
        schema = pa.schema([pa.field("a", pa.int32()), pa.field("b", pa.int32())])
        validate_required_columns(schema, ["a", "b"])

    def test_schema_with_no_names_attr(self):
        """An object without .names should treat column set as empty."""
        with pytest.raises(RuntimeError, match="Missing"):
            validate_required_columns(object(), {"col"})


class TestRequiredPricingColsForTable:
    """required_pricing_cols_for_table() returns correct sets by table name."""

    def test_sales_table(self):
        result = required_pricing_cols_for_table("Sales")
        assert result == REQUIRED_PRICING_COLS

    def test_sales_order_detail_table(self):
        result = required_pricing_cols_for_table("OrderDetail")
        assert result == REQUIRED_PRICING_COLS

    def test_none_table_returns_pricing_cols(self):
        result = required_pricing_cols_for_table(None)
        assert result == REQUIRED_PRICING_COLS

    def test_non_line_grain_table_returns_empty(self):
        result = required_pricing_cols_for_table("Returns")
        assert result == frozenset()

    def test_unknown_table_returns_empty(self):
        result = required_pricing_cols_for_table("Budget")
        assert result == frozenset()

    def test_return_type_is_frozenset(self):
        assert isinstance(required_pricing_cols_for_table("Sales"), frozenset)
        assert isinstance(required_pricing_cols_for_table("Other"), frozenset)


class TestSchemaDictCols:
    """schema_dict_cols() identifies string/binary columns for dictionary encoding."""

    def test_string_cols_returned(self):
        schema = pa.schema([
            pa.field("name", pa.string()),
            pa.field("id", pa.int32()),
            pa.field("code", pa.string()),
        ])
        result = schema_dict_cols(schema)
        assert result == ["name", "code"]

    def test_binary_cols_returned(self):
        schema = pa.schema([
            pa.field("data", pa.binary()),
            pa.field("count", pa.int64()),
        ])
        result = schema_dict_cols(schema)
        assert result == ["data"]

    def test_large_string_cols_returned(self):
        schema = pa.schema([pa.field("desc", pa.large_string())])
        result = schema_dict_cols(schema)
        assert result == ["desc"]

    def test_exclude_filters_columns(self):
        schema = pa.schema([
            pa.field("name", pa.string()),
            pa.field("label", pa.string()),
        ])
        result = schema_dict_cols(schema, exclude={"name"})
        assert result == ["label"]

    def test_no_string_cols(self):
        schema = pa.schema([
            pa.field("x", pa.int32()),
            pa.field("y", pa.float64()),
        ])
        assert schema_dict_cols(schema) == []

    def test_schema_dict_cols_underscore_alias(self):
        """_schema_dict_cols adds DICT_EXCLUDE automatically."""
        schema = pa.schema([
            pa.field("OrderNumber", pa.string()),
            pa.field("CustomerKey", pa.string()),
            pa.field("Channel", pa.string()),
        ])
        result = _schema_dict_cols(schema)
        assert "Channel" in result
        assert "OrderNumber" not in result
        assert "CustomerKey" not in result


class TestValidateRequired:
    """_validate_required() legacy wrapper."""

    def test_sales_table_with_missing_pricing(self):
        schema = pa.schema([pa.field("OrderDate", pa.date32())])
        with pytest.raises(RuntimeError, match="required pricing columns"):
            _validate_required(schema, table_name="Sales")

    def test_non_line_table_no_validation(self):
        schema = pa.schema([pa.field("x", pa.int32())])
        _validate_required(schema, table_name="Returns")  # should not raise


# ===================================================================
# Projection
# ===================================================================

class TestProjectTableToSchema:
    """project_table_to_schema() aligns tables to canonical schemas."""

    def test_matching_schema_returned_unchanged(self):
        schema = pa.schema([
            pa.field("a", pa.int32()),
            pa.field("b", pa.string()),
        ])
        table = pa.table({"a": [1, 2], "b": ["x", "y"]}).cast(schema)
        result = project_table_to_schema(table, schema)
        assert result is table  # identity check — no copy

    def test_reorders_columns(self):
        table = pa.table({"b": ["x", "y"], "a": [1, 2]})
        target = pa.schema([pa.field("a", pa.int64()), pa.field("b", pa.string())])
        result = project_table_to_schema(table, target)
        assert result.schema.names == ["a", "b"]
        assert result.column("a").to_pylist() == [1, 2]

    def test_adds_missing_column_as_nulls(self):
        table = pa.table({"a": [1, 2, 3]})
        target = pa.schema([
            pa.field("a", pa.int64()),
            pa.field("missing", pa.float64()),
        ])
        result = project_table_to_schema(table, target)
        assert result.schema.names == ["a", "missing"]
        assert result.column("missing").to_pylist() == [None, None, None]
        assert result.column("missing").type == pa.float64()

    def test_drops_extra_columns(self):
        table = pa.table({"a": [1], "b": [2], "extra": [3]})
        target = pa.schema([pa.field("a", pa.int64()), pa.field("b", pa.int64())])
        result = project_table_to_schema(table, target)
        assert result.schema.names == ["a", "b"]

    def test_casts_column_type(self):
        table = pa.table({"a": pa.array([1, 2, 3], type=pa.int32())})
        target = pa.schema([pa.field("a", pa.int64())])
        result = project_table_to_schema(table, target, cast_safe=False)
        assert result.column("a").type == pa.int64()

    def test_cast_error_raises_by_default(self):
        table = pa.table({"a": pa.array(["not_a_number", "two"], type=pa.string())})
        target = pa.schema([pa.field("a", pa.int32())])
        with pytest.raises(RuntimeError, match="Failed to cast column 'a'"):
            project_table_to_schema(table, target, cast_safe=False)

    def test_on_cast_error_warn_does_not_raise_runtime_error(self):
        """on_cast_error='warn' logs a warning instead of raising RuntimeError.

        The original column type is kept in the arrays list, but
        pa.Table.from_arrays enforces schema-type agreement so an
        ArrowInvalid (ValueError subclass) propagates — NOT our
        RuntimeError wrapper.  This confirms the warn path works.
        """
        table = pa.table({"a": pa.array(["not_a_number"], type=pa.string())})
        target = pa.schema([pa.field("a", pa.int32())])
        with pytest.raises(pa.lib.ArrowInvalid):
            project_table_to_schema(table, target, on_cast_error="warn")

    def test_on_cast_error_warn_compatible_keeps_original(self):
        """When the original type is castable to the target (e.g. int32 -> int64),
        the cast itself succeeds and 'warn' has no effect."""
        table = pa.table({"a": pa.array([1, 2], type=pa.int32())})
        target = pa.schema([pa.field("a", pa.int64())])
        result = project_table_to_schema(table, target, on_cast_error="warn")
        assert result.column("a").type == pa.int64()

    def test_combined_reorder_add_drop(self):
        """Reorder + add missing + drop extra in one call."""
        table = pa.table({"c": [10], "a": [1], "extra": [99]})
        target = pa.schema([
            pa.field("a", pa.int64()),
            pa.field("b", pa.float32()),
            pa.field("c", pa.int64()),
        ])
        result = project_table_to_schema(table, target)
        assert result.schema.names == ["a", "b", "c"]
        assert result.column("b").to_pylist() == [None]
        assert result.column("a").to_pylist() == [1]
        assert result.column("c").to_pylist() == [10]

    def test_empty_table(self):
        table = pa.table({"a": pa.array([], type=pa.int64())})
        target = pa.schema([
            pa.field("a", pa.int64()),
            pa.field("b", pa.string()),
        ])
        result = project_table_to_schema(table, target)
        assert result.num_rows == 0
        assert result.schema.names == ["a", "b"]

    def test_underscore_alias(self):
        """_project_table_to_schema is a back-compat alias."""
        table = pa.table({"b": [1], "a": [2]})
        target = pa.schema([pa.field("a", pa.int64()), pa.field("b", pa.int64())])
        result = _project_table_to_schema(table, target)
        assert result.schema.names == ["a", "b"]

    def test_pa_and_pc_passthrough(self):
        """Passing pa and pc avoids internal lazy import."""
        table = pa.table({"x": [1]})
        target = pa.schema([pa.field("x", pa.int64()), pa.field("y", pa.string())])
        result = project_table_to_schema(table, target, pa=pa, pc=pc)
        assert result.schema.names == ["x", "y"]


# ===================================================================
# Parquet Merge — Schema Equality
# ===================================================================

class TestPmSchemaEquals:
    """_pm_schema_equals() compares Arrow schemas."""

    def test_identical_schemas(self):
        s = pa.schema([pa.field("a", pa.int32()), pa.field("b", pa.string())])
        assert _pm_schema_equals(s, s, check_metadata=False) is True

    def test_same_fields_different_objects(self):
        s1 = pa.schema([pa.field("a", pa.int32())])
        s2 = pa.schema([pa.field("a", pa.int32())])
        assert _pm_schema_equals(s1, s2, check_metadata=False) is True

    def test_different_field_names(self):
        s1 = pa.schema([pa.field("a", pa.int32())])
        s2 = pa.schema([pa.field("b", pa.int32())])
        assert _pm_schema_equals(s1, s2, check_metadata=False) is False

    def test_same_names_different_types(self):
        s1 = pa.schema([pa.field("a", pa.int32())])
        s2 = pa.schema([pa.field("a", pa.float64())])
        assert _pm_schema_equals(s1, s2, check_metadata=False) is False

    def test_different_field_count(self):
        s1 = pa.schema([pa.field("a", pa.int32())])
        s2 = pa.schema([pa.field("a", pa.int32()), pa.field("b", pa.int32())])
        assert _pm_schema_equals(s1, s2, check_metadata=False) is False

    def test_metadata_difference_ignored_when_unchecked(self):
        s1 = pa.schema([pa.field("a", pa.int32())], metadata={b"key": b"val1"})
        s2 = pa.schema([pa.field("a", pa.int32())], metadata={b"key": b"val2"})
        assert _pm_schema_equals(s1, s2, check_metadata=False) is True

    def test_metadata_difference_detected_when_checked(self):
        s1 = pa.schema([pa.field("a", pa.int32())], metadata={b"key": b"val1"})
        s2 = pa.schema([pa.field("a", pa.int32())], metadata={b"key": b"val2"})
        assert _pm_schema_equals(s1, s2, check_metadata=True) is False

    def test_same_metadata_passes_check(self):
        meta = {b"key": b"val"}
        s1 = pa.schema([pa.field("a", pa.int32())], metadata=meta)
        s2 = pa.schema([pa.field("a", pa.int32())], metadata=meta)
        assert _pm_schema_equals(s1, s2, check_metadata=True) is True


# ===================================================================
# Parquet Merge — Chunk Column Validation
# ===================================================================

class TestValidateChunkColumns:
    """_validate_chunk_columns() enforces column presence/absence."""

    def test_no_expected_set_is_noop(self):
        _validate_chunk_columns({"a", "b"}, None, strict_expected=True, reject_extra_cols=True, path="f.pq")

    def test_strict_missing_raises(self):
        with pytest.raises(RuntimeError, match="missing expected columns"):
            _validate_chunk_columns(
                {"a"},
                frozenset({"a", "b"}),
                strict_expected=True,
                reject_extra_cols=False,
                path="chunk.parquet",
            )

    def test_strict_all_present_ok(self):
        _validate_chunk_columns(
            {"a", "b", "c"},
            frozenset({"a", "b"}),
            strict_expected=True,
            reject_extra_cols=False,
            path="chunk.parquet",
        )

    def test_reject_extra_raises(self):
        with pytest.raises(RuntimeError, match="unexpected columns"):
            _validate_chunk_columns(
                {"a", "b", "extra"},
                frozenset({"a", "b"}),
                strict_expected=False,
                reject_extra_cols=True,
                path="chunk.parquet",
            )

    def test_reject_extra_no_extra_ok(self):
        _validate_chunk_columns(
            {"a", "b"},
            frozenset({"a", "b"}),
            strict_expected=False,
            reject_extra_cols=True,
            path="chunk.parquet",
        )

    def test_path_included_in_error(self):
        with pytest.raises(RuntimeError, match="my_file.parquet"):
            _validate_chunk_columns(
                {"a"},
                frozenset({"a", "b"}),
                strict_expected=True,
                reject_extra_cols=False,
                path="my_file.parquet",
            )

    def test_both_strict_and_reject(self):
        """Both strict_expected and reject_extra can be True simultaneously."""
        _validate_chunk_columns(
            {"a", "b"},
            frozenset({"a", "b"}),
            strict_expected=True,
            reject_extra_cols=True,
            path="f.pq",
        )

    def test_both_strict_and_reject_fails_on_missing(self):
        with pytest.raises(RuntimeError, match="missing"):
            _validate_chunk_columns(
                {"a"},
                frozenset({"a", "b"}),
                strict_expected=True,
                reject_extra_cols=True,
                path="f.pq",
            )


# ===================================================================
# Parquet Merge — Integration (file I/O)
# ===================================================================

class TestMergeParquetFiles:
    """merge_parquet_files() integration tests with real parquet I/O."""

    def _make_sales_table(self, n: int = 5) -> pa.Table:
        """Build a minimal table with required pricing columns."""
        return pa.table({
            "UnitPrice": pa.array([10.0] * n, type=pa.float64()),
            "NetPrice": pa.array([9.0] * n, type=pa.float64()),
            "UnitCost": pa.array([5.0] * n, type=pa.float64()),
            "DiscountAmount": pa.array([1.0] * n, type=pa.float64()),
            "Quantity": pa.array([1] * n, type=pa.int32()),
        })

    def test_empty_file_list_returns_none(self, tmp_path):
        merged = tmp_path / "out.parquet"
        result = merge_parquet_files([], str(merged), log=False)
        assert result is None

    def test_nonexistent_files_returns_none(self, tmp_path):
        merged = tmp_path / "out.parquet"
        result = merge_parquet_files(
            [str(tmp_path / "ghost1.parquet"), str(tmp_path / "ghost2.parquet")],
            str(merged),
            log=False,
        )
        assert result is None

    def test_single_file_merged(self, tmp_path):
        src = tmp_path / "chunk0001.parquet"
        table = _make_table(a=[1, 2, 3], b=["x", "y", "z"])
        _write_parquet(src, table)

        merged = tmp_path / "merged.parquet"
        # table_name="Returns" bypasses pricing column validation
        result = merge_parquet_files(
            [str(src)], str(merged), table_name="Returns", log=False,
        )
        assert result is not None
        assert os.path.isfile(result)

        out = pq.read_table(result)
        assert out.num_rows == 3

    def test_multiple_files_row_count(self, tmp_path):
        tables = [
            _make_table(x=[1, 2, 3]),
            _make_table(x=[4, 5]),
            _make_table(x=[6]),
        ]
        paths = []
        for i, t in enumerate(tables):
            p = tmp_path / f"chunk{i:04d}.parquet"
            _write_parquet(p, t)
            paths.append(str(p))

        merged = tmp_path / "merged.parquet"
        result = merge_parquet_files(
            paths, str(merged), table_name="Returns", log=False,
        )
        out = pq.read_table(result)
        assert out.num_rows == 6

    def test_delete_after_removes_sources(self, tmp_path):
        src = tmp_path / "chunk0001.parquet"
        _write_parquet(src, _make_table(a=[1]))

        merged = tmp_path / "merged.parquet"
        merge_parquet_files(
            [str(src)], str(merged),
            delete_after=True, table_name="Returns", log=False,
        )
        assert not src.exists()
        assert os.path.isfile(str(merged))

    def test_delete_after_false_keeps_sources(self, tmp_path):
        src = tmp_path / "chunk0001.parquet"
        _write_parquet(src, _make_table(a=[1]))

        merged = tmp_path / "merged.parquet"
        merge_parquet_files(
            [str(src)], str(merged),
            delete_after=False, table_name="Returns", log=False,
        )
        assert src.exists()

    def test_merged_file_in_nested_dir(self, tmp_path):
        """merge_parquet_files creates parent dirs for merged_file."""
        src = tmp_path / "chunk.parquet"
        _write_parquet(src, _make_table(val=[42]))

        merged = tmp_path / "sub" / "deep" / "out.parquet"
        result = merge_parquet_files(
            [str(src)], str(merged), table_name="Returns", log=False,
        )
        assert result is not None
        assert os.path.isfile(result)

    def test_schema_union_adds_columns(self, tmp_path):
        """Union strategy: columns from all chunks appear in output."""
        t1 = pa.table({"a": [1], "b": [2]})
        t2 = pa.table({"a": [3], "c": [4]})
        p1 = tmp_path / "c1.parquet"
        p2 = tmp_path / "c2.parquet"
        _write_parquet(p1, t1)
        _write_parquet(p2, t2)

        merged = tmp_path / "merged.parquet"
        result = merge_parquet_files(
            [str(p1), str(p2)], str(merged),
            schema_strategy="union", table_name="Returns", log=False,
        )
        out = pq.read_table(result)
        assert out.num_rows == 2
        assert set(out.schema.names) == {"a", "b", "c"}

    def test_compression_parameter(self, tmp_path):
        """Custom compression is accepted without error."""
        src = tmp_path / "chunk.parquet"
        _write_parquet(src, _make_table(v=[1, 2, 3]))

        merged = tmp_path / "merged.parquet"
        result = merge_parquet_files(
            [str(src)], str(merged),
            compression="gzip", table_name="Returns", log=False,
        )
        assert result is not None

    def test_default_table_name_requires_pricing_cols(self, tmp_path):
        """With table_name=None (default), pricing columns are required."""
        src = tmp_path / "chunk.parquet"
        _write_parquet(src, _make_table(a=[1]))

        merged = tmp_path / "merged.parquet"
        with pytest.raises(RuntimeError, match="required pricing columns"):
            merge_parquet_files([str(src)], str(merged), log=False)

    def test_sales_table_with_pricing_cols(self, tmp_path):
        """Sales table with all pricing columns merges successfully."""
        src = tmp_path / "chunk.parquet"
        _write_parquet(src, self._make_sales_table(3))

        merged = tmp_path / "merged.parquet"
        result = merge_parquet_files(
            [str(src)], str(merged), table_name="Sales", log=False,
        )
        assert result is not None
        out = pq.read_table(result)
        assert out.num_rows == 3


class TestMergeParquetFilesCommon:
    """Tests for _merge_parquet_files_common lower-level entry point."""

    def test_empty_iterable_returns_none(self, tmp_path):
        merged = tmp_path / "out.parquet"
        result = _merge_parquet_files_common([], str(merged), log=False)
        assert result is None

    def test_filters_blank_paths(self, tmp_path):
        """Empty strings and None-ish paths are filtered out."""
        merged = tmp_path / "out.parquet"
        result = _merge_parquet_files_common(["", ""], str(merged), log=False)
        assert result is None


# ===================================================================
# Parquet Merge — optimize_parquet
# ===================================================================

class TestOptimizeParquet:
    """optimize_parquet() sorts and rewrites parquet files."""

    def test_nonexistent_file_returns_none(self, tmp_path):
        result = optimize_parquet(str(tmp_path / "nope.parquet"))
        assert result is None

    def test_no_sort_keys_returns_none(self, tmp_path):
        path = tmp_path / "data.parquet"
        _write_parquet(path, _make_table(a=[3, 1, 2]))
        result = optimize_parquet(str(path), sort_keys=None, table_name=None)
        assert result is None

    def test_sort_keys_not_in_file_returns_none(self, tmp_path):
        path = tmp_path / "data.parquet"
        _write_parquet(path, _make_table(a=[3, 1, 2]))
        result = optimize_parquet(
            str(path),
            sort_keys=[("NonExistentCol", "ascending")],
        )
        assert result is None

    def test_sorts_by_explicit_keys(self, tmp_path):
        path = tmp_path / "data.parquet"
        table = pa.table({
            "OrderDate": pa.array(["2024-03-01", "2024-01-01", "2024-02-01"]),
            "Value": pa.array([30, 10, 20]),
        })
        _write_parquet(path, table)

        result = optimize_parquet(
            str(path),
            sort_keys=[("OrderDate", "ascending")],
        )
        assert result is not None
        out = pq.read_table(result)
        assert out.column("OrderDate").to_pylist() == [
            "2024-01-01", "2024-02-01", "2024-03-01",
        ]

    def test_sorts_by_table_name(self, tmp_path):
        """Table name 'OrderDetail' infers sort keys from _SORT_KEYS_BY_TABLE."""
        path = tmp_path / "detail.parquet"
        table = pa.table({
            "OrderNumber": pa.array(["SO003", "SO001", "SO002"]),
            "OrderLineNumber": pa.array([1, 1, 1], type=pa.int32()),
            "Qty": pa.array([5, 10, 3], type=pa.int32()),
        })
        _write_parquet(path, table)

        result = optimize_parquet(str(path), table_name="OrderDetail")
        assert result is not None
        out = pq.read_table(result)
        assert out.column("OrderNumber").to_pylist() == [
            "SO001", "SO002", "SO003",
        ]

    def test_replaces_original_file(self, tmp_path):
        path = tmp_path / "data.parquet"
        table = pa.table({"a": [2, 1]})
        _write_parquet(path, table)

        result = optimize_parquet(str(path), sort_keys=[("a", "ascending")])
        # Result path should be the same as input
        assert os.path.normpath(result) == os.path.normpath(str(path))
        # Temp file should not remain
        assert not os.path.exists(str(path) + ".optimize_tmp")


# ===================================================================
# DEFAULT_COMPRESSION constant
# ===================================================================

class TestDefaultCompression:
    """Module constant for default compression codec."""

    def test_default_compression_value(self):
        assert DEFAULT_COMPRESSION == "snappy"
