from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

import numpy as np
import pyarrow as pa

from ..sales_logic import State

# NOTE: Hard Arrow imports are intentional here. io.py is only ever used in
# contexts where Arrow is already required (worker processes), so lazy-loading
# would add complexity without benefit.
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq


@dataclass(frozen=True)
class ChunkIOConfig:
    compression: str = "snappy"
    row_group_size: int = 1_000_000
    write_statistics: bool = True


EnsureColsFn = Callable[[pa.Table], pa.Table]
CsvPostprocessFn = Callable[[pa.Table], pa.Table]

# Directories already created are tracked to skip redundant os.makedirs syscalls
# in hot-path chunk writes. Workers call ensure_dirs() during init, so the first
# write to each directory would succeed regardless, but repeated stat() calls on
# network or FUSE filesystems add measurable latency per chunk.
_DIRS_CREATED: set[str] = set()


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path) or "."
    if d not in _DIRS_CREATED:
        os.makedirs(d, exist_ok=True)
        _DIRS_CREATED.add(d)


def add_year_month_from_date(
    table: pa.Table,
    *,
    date_cols: Sequence[str] = ("OrderDate", "DeliveryDate"),
    year_col: str = "Year",
    month_col: str = "Month",
    year_type: pa.DataType = pa.int16(),
    month_type: pa.DataType = pa.int16(),
) -> pa.Table:
    names = table.schema.names
    date_col = next((c for c in date_cols if c in names), None)
    if not date_col:
        return table

    col = table[date_col]
    year = pc.cast(pc.year(col), year_type)
    month = pc.cast(pc.month(col), month_type)

    need_year = year_col not in names
    need_month = month_col not in names
    if need_year and need_month:
        # Single table reconstruction instead of two sequential append_column calls
        new_names = table.schema.names + [year_col, month_col]
        new_cols = [table.column(i) for i in range(table.num_columns)] + [year, month]
        return pa.table(dict(zip(new_names, new_cols)))
    if need_year:
        return table.append_column(year_col, year)
    if need_month:
        return table.append_column(month_col, month)
    return table


def normalize_to_schema(
    table: pa.Table,
    expected: pa.Schema,
    *,
    table_name: Optional[str] = None,
    ensure_cols: Optional[Sequence[str]] = None,
    ensure_cols_fn: Optional[EnsureColsFn] = None,
) -> pa.Table:
    expected = expected.remove_metadata()

    if ensure_cols and ensure_cols_fn:
        exp_names = set(expected.names)
        got_names = set(table.schema.names)
        if all(c in exp_names for c in ensure_cols) and any(c not in got_names for c in ensure_cols):
            table = ensure_cols_fn(table)

    got_names = set(table.schema.names)
    exp_names = set(expected.names)
    if got_names != exp_names:
        raise RuntimeError(
            "Schema mismatch in writer.\n"
            f"Table: {table_name or 'unknown'}\n"
            f"Missing: {sorted(exp_names - got_names)}\n"
            f"Extra: {sorted(got_names - exp_names)}\n\n"
            f"Expected:\n{expected}\n\nGot:\n{table.schema}"
        )

    # Fast path: if the incoming schema already matches expected (common case),
    # just reorder columns without per-field type checking.
    got_schema = table.schema.remove_metadata()
    if got_schema.equals(expected):
        if list(got_schema.names) == list(expected.names):
            return table
        return table.select(expected.names)

    # Slow path: per-field cast (first chunk typically; result is cached by caller)
    cast_safe = bool(getattr(State, "cast_safe", True))
    arrays = []
    for field in expected:
        col = table[field.name]
        if col.type != field.type:
            try:
                if pa.types.is_decimal(field.type) and pa.types.is_floating(col.type):
                    col = pc.round(col, ndigits=int(field.type.scale))
                col = pc.cast(col, field.type, safe=cast_safe)
            except (ValueError, TypeError, ArithmeticError) as ex:
                raise RuntimeError(
                    f"[{table_name or 'unknown'}] Failed cast '{field.name}': {col.type} -> {field.type} "
                    f"(safe={cast_safe}): {ex}"
                ) from ex
        arrays.append(col)

    return pa.Table.from_arrays(arrays, schema=expected)


def write_parquet_table(
    table: pa.Table,
    path: str,
    *,
    expected_schema: pa.Schema,
    cfg: ChunkIOConfig,
    use_dictionary: Optional[Sequence[str]] = None,
    table_name: Optional[str] = None,
    ensure_cols: Optional[Sequence[str]] = None,
    ensure_cols_fn: Optional[EnsureColsFn] = None,
) -> None:

    table = normalize_to_schema(
        table,
        expected_schema,
        table_name=table_name,
        ensure_cols=ensure_cols,
        ensure_cols_fn=ensure_cols_fn,
    )

    _ensure_dir(path)

    pq.write_table(
        table,
        path,
        compression=cfg.compression,
        use_dictionary=list(use_dictionary) if use_dictionary else False,
        write_statistics=bool(cfg.write_statistics),
        row_group_size=int(max(1, cfg.row_group_size)),
    )


def write_csv_table(
    table: pa.Table,
    path: str,
    *,
    expected_schema: pa.Schema,
    table_name: Optional[str] = None,
    ensure_cols: Optional[Sequence[str]] = None,
    ensure_cols_fn: Optional[EnsureColsFn] = None,
    postprocess: Optional[CsvPostprocessFn] = None,
) -> None:

    table = normalize_to_schema(
        table,
        expected_schema,
        table_name=table_name,
        ensure_cols=ensure_cols,
        ensure_cols_fn=ensure_cols_fn,
    )

    if postprocess is not None:
        table = postprocess(table)

    # Arrow CSV writes bool as "true"/"false"; SQL Server BULK INSERT
    # expects 0/1 for BIT columns.  Cast bool → int8 before writing.
    for i, field in enumerate(table.schema):
        if pa.types.is_boolean(field.type):
            table = table.set_column(i, field.name, pc.cast(table.column(i), pa.int8()))

    _ensure_dir(path)

    # batch_size raises the writer's internal block size so it makes far fewer,
    # larger syscalls (the default 1024 rows ⇒ ~1M write calls per GB).
    pacsv.write_csv(
        table,
        path,
        write_options=pacsv.WriteOptions(
            include_header=True, quoting_style="none", batch_size=1 << 20),
    )


def _expected_schema(table_name: Optional[str]) -> pa.Schema:
    schema_by_table = getattr(State, "schema_by_table", None)
    if table_name and isinstance(schema_by_table, dict) and table_name in schema_by_table:
        return schema_by_table[table_name]
    return State.sales_schema


def _dict_cols(table_name: Optional[str]) -> list[str]:
    m = getattr(State, "parquet_dict_cols_by_table", None)
    if table_name and isinstance(m, dict) and table_name in m:
        return list(m[table_name])
    return list(getattr(State, "parquet_dict_cols", []))


def _schema_partition_cols(expected: pa.Schema) -> tuple[str, ...]:
    """Return which partition columns (Year and/or Month) the schema expects."""
    names = set(expected.names)
    return tuple(c for c in ("Year", "Month") if c in names)


def _derive_year_month_from_int_order_date(order_date: pa.ChunkedArray) -> Tuple[np.ndarray, np.ndarray]:
    x = order_date.combine_chunks().to_numpy(zero_copy_only=False)

    if x.dtype.kind not in {"i", "u"}:
        raise RuntimeError(f"OrderDate integer derivation expected int dtype, got {x.dtype}")

    xi = x.astype(np.int64, copy=False)
    if xi.size == 0:
        empty = np.empty(0, dtype=np.int16)
        return empty, empty.copy()

    if np.any(xi == np.iinfo(np.int64).min):
        raise RuntimeError("OrderDate contains nulls; cannot derive Year/Month")

    mn = int(xi.min())
    mx = int(xi.max())

    if 19_000_000 <= mn <= 210_012_31 and 19_000_000 <= mx <= 210_012_31:
        year = (xi // 10_000).astype(np.int16, copy=False)
        month = ((xi // 100) % 100).astype(np.int16, copy=False)
        return year, month

    if -100_000 <= mn <= 200_000 and -100_000 <= mx <= 200_000:
        epoch = np.datetime64("1970-01-01", "D")
        dt = (epoch + xi.astype("timedelta64[D]")).astype("datetime64[D]", copy=False)

        year = (dt.astype("datetime64[Y]").astype(np.int32) + 1970).astype(np.int16)
        months = dt.astype("datetime64[M]").astype(np.int32)
        month = ((months % 12) + 1).astype(np.int16)
        return year, month

    raise RuntimeError(f"OrderDate integer format not recognized; min={mn} max={mx}")


def _ensure_partition_cols_for_table(
    table: pa.Table,
    *,
    table_name: str,
    expected_schema: pa.Schema,
) -> pa.Table:
    needed = _schema_partition_cols(expected_schema)
    if not needed:
        return table

    col_names = set(table.column_names)
    missing = [c for c in needed if c not in col_names]
    if not missing:
        return table

    policy = getattr(State, "date_cols_by_table", {}) or {}
    candidates = policy.get(table_name) or ["DeliveryDate", "OrderDate"]

    usable: list[str] = []
    for c in candidates:
        if c not in col_names:
            continue
        t = table.schema.field(c).type
        if pa.types.is_date32(t) or pa.types.is_date64(t) or pa.types.is_timestamp(t):
            usable.append(c)

    if usable:
        date_col_name = usable[0]
        date_arr = table[date_col_name]
        if "Year" in missing:
            table = table.append_column("Year", pc.cast(pc.year(date_arr), pa.int16()))
        if "Month" in missing:
            table = table.append_column("Month", pc.cast(pc.month(date_arr), pa.int16()))
        return table

    if "OrderDate" in col_names and pa.types.is_integer(table.schema.field("OrderDate").type):
        year, month = _derive_year_month_from_int_order_date(table["OrderDate"])
        if "Year" in missing:
            table = table.append_column("Year", pa.array(year, type=pa.int16()))
        if "Month" in missing:
            table = table.append_column("Month", pa.array(month, type=pa.int16()))
        return table

    raise RuntimeError(f"Cannot derive partition cols {missing} for table={table_name}: {candidates}")


def _csv_postprocess_sales(table: pa.Table) -> pa.Table:
    if "IsOrderDelayed" in table.column_names:
        idx = table.schema.get_field_index("IsOrderDelayed")
        table = table.set_column(
            idx,
            "IsOrderDelayed",
            pc.cast(pc.fill_null(table["IsOrderDelayed"], False), pa.bool_()),
        )
    return table


def _build_ensure_fn(
    table_name: str,
    expected_schema: pa.Schema,
) -> Tuple[Optional[Sequence[str]], Optional[EnsureColsFn]]:
    """Shared helper to build partition-col ensure args for parquet and CSV writers."""
    pcols = _schema_partition_cols(expected_schema)
    if not pcols:
        return (), None

    return (
        pcols,
        lambda t: _ensure_partition_cols_for_table(
            t, table_name=table_name, expected_schema=expected_schema
        ),
    )


def _write_parquet_table(table: pa.Table, path: str, *, table_name: Optional[str] = None, is_chunk: bool = False) -> None:
    tn = table_name or "Sales"
    expected = _expected_schema(tn)

    write_stats_chunks = bool(getattr(State, "write_statistics_chunks", False))
    write_stats_merged = bool(getattr(State, "write_statistics", True))
    cfg = ChunkIOConfig(
        compression=getattr(State, "compression", "snappy"),
        row_group_size=int(getattr(State, "row_group_size", 1_000_000)),
        write_statistics=write_stats_chunks if is_chunk else write_stats_merged,
    )

    ensure_cols, ensure_fn = _build_ensure_fn(tn, expected)

    write_parquet_table(
        table,
        path,
        expected_schema=expected,
        cfg=cfg,
        use_dictionary=_dict_cols(tn),
        table_name=tn,
        ensure_cols=ensure_cols,
        ensure_cols_fn=ensure_fn,
    )


def _write_csv(table: pa.Table, path: str, *, table_name: Optional[str] = None) -> None:
    tn = table_name or "Sales"
    expected = _expected_schema(tn)

    ensure_cols, ensure_fn = _build_ensure_fn(tn, expected)

    write_csv_table(
        table,
        path,
        expected_schema=expected,
        table_name=tn,
        ensure_cols=ensure_cols,
        ensure_cols_fn=ensure_fn,
        postprocess=_csv_postprocess_sales,
    )
