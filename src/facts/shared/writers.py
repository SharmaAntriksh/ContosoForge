"""Shared multi-format writer for batch fact tables.

Handles parquet, CSV, and Delta Lake output for fact tables that produce
a complete DataFrame/Table in memory (budget, inventory, complaints).
NOT used by the sales writer, which has its own chunked merge pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq

from src.utils.logging_utils import info


def write_fact_table(
    df_or_table: pd.DataFrame | pa.Table,
    out_dir: Path,
    name: str,
    file_format: str,
    *,
    csv_prep_fn: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None,
    csv_float_format: Optional[str] = None,
    compression: str = "snappy",
    row_group_size: int = 500_000,
    csv_chunk_size: Optional[int] = None,
    csv_columns: Optional[Sequence[str]] = None,
    csv_int_columns: Optional[Sequence[str]] = None,
    csv_round: Optional[Mapping[str, int]] = None,
    csv_fill_null_ints: bool = False,
    csv_float_scale: Optional[int] = None,
) -> None:
    """Write a fact table in the requested format.

    Args:
        df_or_table:    DataFrame or PyArrow Table to write.
        out_dir:        Directory for parquet/csv output.
        name:           Base filename (without extension).
        file_format:    "csv" | "parquet" | "deltaparquet".
        csv_prep_fn:    Optional function to prepare DataFrame for CSV
                        (column selection, type casting, rounding).
        csv_float_format: Optional float format string for CSV output.
        compression:    Parquet compression codec.
        row_group_size: Parquet row group size.
        csv_chunk_size: Max rows per CSV file.  None or 0 = single file.
    """
    if isinstance(df_or_table, pa.Table):
        table = df_or_table
        n_rows = table.num_rows
    else:
        table = pa.Table.from_pandas(df_or_table, preserve_index=False)
        n_rows = len(df_or_table)

    # Cast any timestamp columns to date32 (Power BI / Power Query friendly)
    new_fields = []
    needs_cast = False
    for f in table.schema:
        if pa.types.is_timestamp(f.type) or pa.types.is_date64(f.type):
            new_fields.append(pa.field(f.name, pa.date32()))
            needs_cast = True
        else:
            new_fields.append(f)
    if needs_cast:
        table = table.cast(pa.schema(new_fields), safe=False)

    if file_format == "deltaparquet":
        delta_dir = out_dir.parent / name
        delta_dir.mkdir(parents=True, exist_ok=True)
        try:
            from deltalake import write_deltalake
        except ImportError:
            from deltalake.writer import write_deltalake
        write_deltalake(str(delta_dir), table, mode="overwrite")
        info(f"Wrote {name}/ ({n_rows:,} rows)")
        return

    if file_format == "parquet":
        parquet_path = out_dir / f"{name}.parquet"
        pq.write_table(
            table, str(parquet_path),
            compression=compression,
            row_group_size=row_group_size,
            use_dictionary=True,
        )
        info(f"Wrote {name}.parquet ({n_rows:,} rows)")
        return

    if file_format == "csv" and csv_prep_fn is None:
        if csv_columns is not None:
            arrays = []
            existing = set(table.column_names)
            for column_name in csv_columns:
                if column_name in existing:
                    arrays.append(table.column(column_name))
                else:
                    arrays.append(pa.nulls(n_rows, type=pa.string()))
            table = pa.table(arrays, names=list(csv_columns))

        for column_idx, field in enumerate(table.schema):
            target = table.column(column_idx)
            if pa.types.is_boolean(field.type):
                target = pc.cast(target, pa.int8(), safe=False)
            if csv_round and field.name in csv_round and pa.types.is_floating(target.type):
                target = pc.round(target, ndigits=int(csv_round[field.name]))
            if csv_int_columns and field.name in csv_int_columns:
                if csv_fill_null_ints:
                    target = pc.fill_null(target, 0)
                target = pc.cast(target, pa.int64(), safe=False)
            elif csv_float_scale is not None and pa.types.is_floating(target.type):
                scale = int(csv_float_scale)
                target = pc.round(target, ndigits=scale)
                target = pc.cast(target, pa.decimal128(38, scale), safe=False)
            if not target.type.equals(field.type):
                table = table.set_column(column_idx, field.name, target)

        write_options = pa_csv.WriteOptions(
            include_header=True,
            quoting_style="needed",
        )
        if csv_chunk_size and csv_chunk_size > 0 and n_rows > csv_chunk_size:
            n_files = 0
            for start in range(0, n_rows, csv_chunk_size):
                chunk_path = out_dir / f"{name}_{n_files:05d}.csv"
                pa_csv.write_csv(
                    table.slice(start, csv_chunk_size),
                    str(chunk_path),
                    write_options=write_options,
                )
                n_files += 1
            info(f"Wrote {name} ({n_rows:,} rows, {n_files} files)")
        else:
            csv_path = out_dir / f"{name}.csv"
            pa_csv.write_csv(table, str(csv_path), write_options=write_options)
            info(f"Wrote {csv_path.name} ({n_rows:,} rows)")
        return

    if file_format == "csv":
        # Use the (date32-cast) table for both Table and DataFrame inputs so CSV
        # dates render as YYYY-MM-DD, matching the parquet/delta output instead of
        # a full timestamp for DataFrame callers.
        df = table.to_pandas()
        # bool → 0/1 for SQL Server BULK INSERT compatibility (BIT columns)
        _bool_cols = list(df.select_dtypes(include=["bool", "boolean"]).columns)
        if _bool_cols:
            df = df.assign(**{c: df[c].astype("Int8") for c in _bool_cols})
        csv_df = csv_prep_fn(df) if csv_prep_fn else df
        # Force LF: pandas to_csv defaults to CRLF on Windows, but the generated
        # SQL BULK INSERT hardcodes ROWTERMINATOR='0x0a' (LF). A trailing \r would
        # break numeric last columns and silently corrupt string ones.
        kwargs: dict = {"index": False, "lineterminator": "\n"}
        if csv_float_format:
            kwargs["float_format"] = csv_float_format

        if csv_chunk_size and csv_chunk_size > 0 and n_rows > csv_chunk_size:
            n_files = 0
            for start in range(0, n_rows, csv_chunk_size):
                chunk_path = out_dir / f"{name}_{n_files:05d}.csv"
                csv_df.iloc[start:start + csv_chunk_size].to_csv(str(chunk_path), **kwargs)
                n_files += 1
            info(f"Wrote {name} ({n_rows:,} rows, {n_files} files)")
        else:
            csv_path = out_dir / f"{name}.csv"
            csv_df.to_csv(str(csv_path), **kwargs)
            info(f"Wrote {csv_path.name} ({n_rows:,} rows)")
