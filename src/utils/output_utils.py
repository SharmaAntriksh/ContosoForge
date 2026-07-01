from __future__ import annotations

import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, Union

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq

from src.exceptions import PackagingError
from src.tools.sql.dialect import SqlType
from src.utils.logging_utils import stage, done, info
from src.utils.static_schemas import STATIC_SCHEMAS

__all__ = [
    "write_parquet_with_date32",
    "format_number_short",
    "create_final_output_folder",
]

# ---------------------------------------------------------------------------
# Dimension filenames — single source of truth for exclusion logic.
# If a dimension's output filename changes, update here only.
# ---------------------------------------------------------------------------
_DIM_FILE_PLANS = "plans.parquet"
_DIM_FILE_SUBSCRIPTIONS_BRIDGE = "customer_subscriptions.parquet"
_DIM_FILE_WISHLISTS = "customer_wishlists.parquet"
_DIM_FILE_RETURN_REASON = "return_reason.parquet"
_DIM_FILE_EMPLOYEE_TRANSFERS = "employee_transfers.parquet"


# ============================================================
# Parquet helpers (Power BI / Power Query friendliness)
# ============================================================

def _all_null_series(s: pd.Series) -> bool:
    try:
        return bool(s.isna().all())
    except (TypeError, ValueError):
        return False


def _object_series_looks_like_date(s: pd.Series) -> bool:
    """
    True if an object-dtype Series appears to hold Python date/datetime-like
    values, or is entirely null (common for optional date columns in small
    datasets).

    Samples up to 100 non-null rows rather than 25 to reduce false negatives
    on sparse columns.  Also accepts ISO-format date strings so that columns
    stored as plain strings (e.g. "2024-01-15") are correctly detected.
    """
    import datetime as _dt

    import numpy as _np

    try:
        if _all_null_series(s):
            return True

        nonnull = s.dropna()
        if nonnull.empty:
            return True

        # Sample more rows to reduce false negatives on sparse columns
        sample = nonnull.head(100).tolist()
        for v in sample:
            if isinstance(v, pd.Timestamp):
                return True
            if isinstance(v, (_dt.date, _dt.datetime, _np.datetime64)):
                return True
            # Accept ISO-format date strings ("2024-01-15" / "2024-01-15T00:00:00")
            if isinstance(v, str):
                try:
                    pd.Timestamp(v)
                    return True
                except (ValueError, TypeError):
                    pass
        return False
    except (TypeError, ValueError):
        return False


def _datetime_cols(df: pd.DataFrame) -> list[str]:
    """Return column names whose dtype is pandas datetime64 (with or without tz)."""
    cols: list[str] = []
    for c in df.columns:
        try:
            if pd.api.types.is_datetime64_any_dtype(df[c]):
                cols.append(str(c))
        except (TypeError, ValueError):
            continue
    return cols


def _guess_date_cols(df: pd.DataFrame) -> list[str]:
    """
    Heuristic: return column names that should be written as Arrow date32.

    Rules (conservative — won't touch time strings like OpenTime/CloseTime etc.):
      1. datetime64 columns whose names contain a date-like token.
      2. object-dtype columns whose names contain "date" AND whose values look
         date-like (or are all-null), to prevent Arrow NullType on rewrite.

    Returns a de-duplicated, order-preserving list.
    """
    dt_cols: set[str] = set(_datetime_cols(df))
    out: list[str] = []

    dateish_tokens = ("date", "day", "birth", "created", "updated", "effective", "expiry", "valid", "start", "end")

    # Rule 1 — proper datetime64 columns with date-like names
    for c in df.columns:
        if str(c) in dt_cols and any(tok in str(c).lower() for tok in dateish_tokens):
            out.append(str(c))

    # Rule 2 — object columns whose name contains "date"
    for c in df.columns:
        cs = str(c)
        if cs in dt_cols:
            continue
        if "date" not in cs.lower():
            continue
        try:
            if pd.api.types.is_object_dtype(df[c]) and _object_series_looks_like_date(df[c]):
                out.append(cs)
        except (TypeError, ValueError):
            continue

    # De-duplicate preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def write_parquet_with_date32(
    df: pd.DataFrame,
    out_path: Union[str, Path],
    *,
    date_cols: Optional[Sequence[str]] = None,
    cast_all_datetime: bool = False,
    compression: str = "snappy",
    compression_level: Optional[int] = None,
    force_date32: bool = True,
) -> None:
    """
    Write a Parquet file with selected date-like columns stored as Arrow date32
    (Power BI / Power Query friendly — avoids the NullType crash).

    Behaviour:
      - If ``date_cols`` is given, those columns are cast (dtype not required).
      - If ``cast_all_datetime`` is True, all datetime64 columns are cast.
      - Otherwise, ``_guess_date_cols`` is used as the heuristic.

    Only the target columns are copied; the rest of the DataFrame is passed
    through by reference to keep peak memory low.
    """
    out_path = Path(out_path)

    dt_cols: set[str] = set(_datetime_cols(df))

    if date_cols is not None:
        target = [str(c) for c in date_cols if str(c) in df.columns]
    elif cast_all_datetime:
        target = list(dt_cols)
    else:
        target = _guess_date_cols(df)

    if not target:
        df.to_parquet(out_path, index=False)
        return

    # Surgical copy: only materialise new Series for target columns so we
    # avoid duplicating the full DataFrame in memory.
    overrides = {
        c: pd.to_datetime(df[c], errors="coerce").dt.normalize()
        for c in target
        if c in df.columns
    }
    df2 = df.assign(**overrides)

    table = pa.Table.from_pandas(df2, preserve_index=False)
    # force_date32 gates only the date32 cast: when False the (normalized)
    # date columns are written as their datetime type instead. Compression
    # is applied either way.
    if force_date32:
        target_set = set(target)
        fields = [
            pa.field(f.name, pa.date32()) if f.name in target_set else f
            for f in table.schema
        ]
        table = table.cast(pa.schema(fields), safe=False)

    kwargs: dict = {"compression": str(compression)}
    if compression_level is not None:
        kwargs["compression_level"] = int(compression_level)

    pq.write_table(table, str(out_path), **kwargs)


# ============================================================
# Utility helpers
# ============================================================

def format_number_short(n: int) -> str:
    """Human-readable suffix for large numbers (rounded, not truncated)."""
    if n >= 1_000_000_000:
        return f"{round(n / 1_000_000_000)}B"
    if n >= 1_000_000:
        return f"{round(n / 1_000_000)}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}K"
    return str(n)


def _copy_config_files_into_run_folder(
    final_folder: Path,
    config_yaml_path: Optional[Union[str, Path]] = None,
    model_yaml_path: Optional[Union[str, Path]] = None,
    config_snapshot: Optional[bytes] = None,
    models_snapshot: Optional[bytes] = None,
) -> None:
    """
    Write config/model YAMLs into ``<final_folder>/config/`` for traceability.

    When ``config_snapshot`` / ``models_snapshot`` bytes are provided (snapshotted
    at pipeline start), those are written directly — guaranteeing the packaged
    config matches the run even if the user edits the files mid-pipeline.
    Falls back to copying from *_yaml_path when no snapshot is available.
    """
    config_dir = final_folder / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    def _write(snapshot: Optional[bytes], src: Optional[Union[str, Path]], dest_name: str) -> None:
        dest = config_dir / dest_name
        if snapshot is not None:
            dest.write_bytes(snapshot)
            return
        if not src:
            return
        p = Path(str(src))
        if not p.exists():
            info(f"WARNING: config file not found, skipping copy: {p}")
            return
        shutil.copy2(p, dest)

    _write(config_snapshot, config_yaml_path, "config.yaml")
    _write(models_snapshot, model_yaml_path, "models.yaml")


def _ensure_clean_dir(p: Path) -> None:
    """
    Remove and recreate a directory.

    Raises ``FileExistsError`` (rather than silently deleting) if the path
    exists AND already contains files — a common sign of a wrong path being
    passed in.  An *empty* existing directory is accepted and recreated
    without complaint.
    """
    if p.exists():
        existing_files = list(p.rglob("*"))
        non_empty = [f for f in existing_files if f.is_file()]
        if non_empty:
            raise FileExistsError(
                f"_ensure_clean_dir: refusing to delete non-empty directory '{p}' "
                f"({len(non_empty)} file(s) present). "
                "Pass an explicit empty or non-existent path to avoid data loss."
            )
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)


def _excluded_dim_files(cfg: dict) -> set[str]:
    """
    Return the set of dimension filenames that should be skipped during
    packaging, based on config flags.

    - ``enabled: false``        → exclude both the dim table and its bridge table
    - ``generate_bridge: false`` → exclude only the bridge table
    - returns effectively disabled → exclude ReturnReason
    """
    excluded: set[str] = {
        _DIM_FILE_EMPLOYEE_TRANSFERS,  # internal sidecar, not a publishable dim
    }

    sub_cfg = getattr(cfg, "subscriptions", None)
    if sub_cfg is not None:
        if not bool(getattr(sub_cfg, "enabled", True)):
            excluded.add(_DIM_FILE_PLANS)
            excluded.add(_DIM_FILE_SUBSCRIPTIONS_BRIDGE)
        elif not bool(getattr(sub_cfg, "generate_bridge", True)):
            excluded.add(_DIM_FILE_SUBSCRIPTIONS_BRIDGE)

    wl_cfg = getattr(cfg, "wishlists", None)
    if wl_cfg is not None:
        if not bool(getattr(wl_cfg, "enabled", True)):
            excluded.add(_DIM_FILE_WISHLISTS)

    returns_cfg = getattr(cfg, "returns", None)
    returns_on = bool(getattr(returns_cfg, "enabled", False)) if returns_cfg is not None else False
    if returns_on:
        sales_cfg = getattr(cfg, "sales", None) or {}
        skip_order = bool(getattr(sales_cfg, "skip_order_cols", False))
        sales_output = str(getattr(sales_cfg, "sales_output", "sales")).strip().lower()
        if skip_order and sales_output == "sales":
            returns_on = False
    if not returns_on:
        excluded.add(_DIM_FILE_RETURN_REASON)

    return excluded


# ============================================================
# Dimension iteration + CSV writer (pyarrow + parallel)
# ============================================================

# Cap dim-CSV thread pool to bound peak memory (each worker holds a full dim
# DataFrame + Arrow table in flight). 8 saturates pyarrow's internal threads
# without serializing too many big dims at once.
_DIM_CSV_MAX_WORKERS = 8


def _iter_dim_files(parquet_dims: Path, excluded_dims: set[str]) -> list[Path]:
    return [f for f in parquet_dims.glob("*.parquet") if f.name not in excluded_dims]


def _date_col_overrides(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Build assign() overrides that cast date-like columns to python ``date``.

    Used by both CSV and Delta branches so pyarrow writes pure date32 / "YYYY-MM-DD"
    instead of leaving NullType (object cols) or appending " 00:00:00" (datetime64).
    """
    return {
        c: pd.to_datetime(df[c], errors="coerce").dt.date
        for c in _guess_date_cols(df)
    }


# ============================================================
# Null-typed column coercion (Delta Lake compatibility)
# ============================================================
# An entirely-null object column (common for optional dims on small/short-window
# runs — e.g. employees.TerminationDate/TerminationReason with no terminations,
# or stores.ClosingDate on a dataset with no closed stores) makes
# ``pa.Table.from_pandas`` infer the Arrow ``null`` type. delta-rs rejects that
# type ("Invalid data type for Delta Lake: Null"), crashing the deltaparquet
# packaging step. Parquet/CSV tolerate null-typed columns, so this only bites
# Delta. We cast each null-typed column to a concrete type — derived from the
# static schema where possible — before the Delta write.

# Non-parametric SqlType -> pyarrow type. DECIMAL is handled separately (needs
# precision/scale args). Widths are exact where cheap, but for an all-null column
# only the type *category* matters to delta-rs.
_SQLTYPE_TO_ARROW: dict[SqlType, pa.DataType] = {
    SqlType.INT: pa.int32(),
    SqlType.BIGINT: pa.int64(),
    SqlType.SMALLINT: pa.int16(),
    SqlType.TINYINT: pa.int8(),
    SqlType.BIT: pa.bool_(),
    SqlType.FLOAT: pa.float64(),
    SqlType.DATE: pa.date32(),
    SqlType.DATETIME: pa.timestamp("us"),
    SqlType.DATETIME2: pa.timestamp("us"),
    SqlType.TIME: pa.time64("us"),
    SqlType.VARCHAR: pa.string(),
    SqlType.CHAR: pa.string(),
}


def _to_snake(name: str) -> str:
    """PascalCase table name -> snake_case dim-file stem (e.g. Stores -> stores)."""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


# Reverse map: dim parquet stem -> canonical STATIC_SCHEMAS table key. Dimension
# files are written as ``to_snake(TableName).parquet`` (see quality_report /
# dimensions_runner), so this resolves e.g. "employees" -> "Employees".
_STEM_TO_TABLE: dict[str, str] = {_to_snake(k): k for k in STATIC_SCHEMAS}


def _arrow_type_for_spec(spec) -> pa.DataType:
    """Map a static-schema ``ColumnSpec`` to a concrete pyarrow type."""
    if spec.sql_type is SqlType.DECIMAL:
        precision, scale = spec.args
        return pa.decimal128(int(precision), int(scale))
    return _SQLTYPE_TO_ARROW.get(spec.sql_type, pa.string())


def _arrow_types_for_table(table_name: Optional[str]) -> dict[str, pa.DataType]:
    """Column-name -> intended pyarrow type for a known table, else empty."""
    if not table_name:
        return {}
    schema = STATIC_SCHEMAS.get(table_name)
    if not schema:
        return {}
    return {col: _arrow_type_for_spec(spec) for col, spec in schema}


def _dim_table_name_for_file(stem: str) -> Optional[str]:
    """Resolve a dim parquet file stem to its STATIC_SCHEMAS key (None if unknown)."""
    return _STEM_TO_TABLE.get(stem)


def _coerce_null_columns(table: pa.Table, table_name: Optional[str]) -> pa.Table:
    """Align entirely-null columns to a concrete type for Delta writes.

    Two cases are handled, both only for columns that carry no data (so nothing
    is ever lost and populated columns are untouched):

    * ``null``-typed columns — ``pa.Table.from_pandas`` infers this for all-null
      object columns, and delta-rs rejects it outright. Cast to the static-schema
      type for ``table_name`` (date -> date32, string -> string, …), falling back
      to ``pa.string()`` for unknown columns/tables.
    * concrete-but-off columns — an all-null date column is left as ``timestamp``
      by the ``force_date32`` path (pandas ``.dt.date`` yields ``datetime64`` for
      all-NaT), which disagrees with the schema. Realign it to the schema type so
      the committed Delta schema matches the static schema in both modes.
    """
    type_map = _arrow_types_for_table(table_name)
    for i in range(table.num_columns):
        field = table.schema.field(i)
        column = table.column(i)
        is_null_typed = pa.types.is_null(field.type)
        if is_null_typed:
            target = type_map.get(field.name, pa.string())
        elif (
            column.null_count == len(column)
            and field.name in type_map
            and not field.type.equals(type_map[field.name])
        ):
            target = type_map[field.name]
        else:
            continue

        try:
            casted = column.cast(target)
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError):
            if not is_null_typed:
                # Column already has a concrete, Delta-valid type; leave it.
                continue
            # A ``null``-typed column must not reach delta-rs; an all-null cast to
            # string always succeeds, so use it as the last-resort concrete type.
            casted = column.cast(pa.string())
        table = table.set_column(i, field.name, casted)
    return table


def _write_one_dim_csv(src: Path, dest: Path, *, force_date32: bool) -> None:
    """Read one dimension parquet and write CSV via pyarrow.

    Normalises bool → 0/1 and integer-like floats → Int64 so SQL Server
    BULK INSERT sees clean values (no "true"/"false", no "10001.0").
    """
    df = pd.read_parquet(src, dtype_backend="numpy_nullable")

    overrides: dict[str, pd.Series] = {}

    for c in df.select_dtypes(include=["bool", "boolean"]).columns:
        overrides[c] = df[c].astype("Int8")

    for c in df.select_dtypes(include=["float", "Float32", "Float64"]).columns:
        arr = df[c].to_numpy(dtype="float64", na_value=np.nan, copy=False)
        mask = ~np.isnan(arr)
        if mask.any() and not np.any(arr[mask] % 1):
            overrides[c] = df[c].astype("Int64")

    if force_date32:
        overrides.update(_date_col_overrides(df))

    if overrides:
        df = df.assign(**overrides)

    table = pa.Table.from_pandas(df, preserve_index=False)
    pa_csv.write_csv(
        table,
        str(dest),
        write_options=pa_csv.WriteOptions(include_header=True, quoting_style="needed"),
    )


def _write_dim_csvs_parallel(
    parquet_dims: Path,
    dims_out: Path,
    excluded_dims: set[str],
    force_date32: bool,
) -> None:
    """Convert all dim parquets to CSV in parallel.

    pyarrow's CSV writer releases the GIL, and dim files are independent —
    so a thread pool gives a real speedup. Files are processed largest-first
    so the bottleneck (typically the customer dim) doesn't tail-block the pool.
    """
    files = sorted(
        _iter_dim_files(parquet_dims, excluded_dims),
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    if not files:
        return

    def _task(src: Path) -> None:
        _write_one_dim_csv(src, dims_out / f"{src.stem}.csv", force_date32=force_date32)

    max_workers = min(len(files), _DIM_CSV_MAX_WORKERS, max(2, os.cpu_count() or 4))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_task, files))


# ============================================================
# Final output folder creation (dimensions only)
# ============================================================

def create_final_output_folder(
    final_folder_root: Path,
    parquet_dims: Path,
    sales_cfg: dict,
    file_format: str,
    cfg,
    config_yaml_path: Optional[Union[str, Path]] = None,
    model_yaml_path: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Create the run output folder and package DIMENSIONS into it.

    Post-modularisation responsibilities:
      - Name and create the run folder hierarchy.
      - Copy ``config.yaml`` / ``models.yaml`` into ``<run>/config/`` for
        traceability.
      - Convert/copy DIMENSIONS from ``parquet_dims`` into the chosen format.

    Facts and SQL packaging are handled by ``src.engine.packaging.package_output()``.
    """

    with stage("Creating Final Output Folder"):
        ff = str(file_format).strip().lower()

        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %I_%M_%S %p")  # windows-safe

        _cust = getattr(cfg, "customers", None) or {}
        customer_total = int(getattr(_cust, "total_customers", 0) or 0)
        sales_total = int(getattr(sales_cfg, "total_rows", 0) or 0)

        dataset_name = (
            f"{timestamp} Customers {format_number_short(customer_total)} "
            f"Sales {format_number_short(sales_total)} {ff.upper()}"
        )
        final_folder = Path(final_folder_root) / dataset_name

        _ensure_clean_dir(final_folder)

        dims_out = final_folder / "dimensions"
        dims_out.mkdir(parents=True, exist_ok=True)
        # NOTE: "facts/" is intentionally NOT created here; facts are packaged
        # externally and will create their own subdirectory as needed.

        _copy_config_files_into_run_folder(
            final_folder,
            config_yaml_path=config_yaml_path,
            model_yaml_path=model_yaml_path,
            config_snapshot=getattr(cfg, "_config_snapshot", None),
            models_snapshot=getattr(cfg, "_models_snapshot", None),
        )

        # --------------------------------------------------------
        # DIMENSIONS
        # --------------------------------------------------------
        packaging_cfg = cfg.packaging
        dim_parquet_compression: str = packaging_cfg.dim_parquet_compression
        dim_parquet_compression_level: Optional[int] = packaging_cfg.dim_parquet_compression_level
        dim_force_date32: bool = bool(packaging_cfg.dim_force_date32)

        parquet_dims = Path(parquet_dims)
        excluded_dims = _excluded_dim_files(cfg)

        if ff == "parquet":
            for f in _iter_dim_files(parquet_dims, excluded_dims):
                df = pd.read_parquet(f)
                write_parquet_with_date32(
                    df,
                    dims_out / f.name,
                    cast_all_datetime=False,
                    compression=dim_parquet_compression,
                    compression_level=dim_parquet_compression_level,
                    force_date32=dim_force_date32,
                )

        elif ff == "csv":
            _write_dim_csvs_parallel(parquet_dims, dims_out, excluded_dims, dim_force_date32)

        elif ff == "deltaparquet":
            try:
                from deltalake import write_deltalake
            except ImportError as e:
                raise PackagingError(
                    "deltaparquet mode requested but 'deltalake' is not installed. "
                    "Run `pip install deltalake` or switch to parquet/csv."
                ) from e

            for f in _iter_dim_files(parquet_dims, excluded_dims):
                delta_out = dims_out / f.stem
                delta_out.mkdir(parents=True, exist_ok=True)

                df = pd.read_parquet(f)
                if dim_force_date32:
                    overrides = _date_col_overrides(df)
                    if overrides:
                        df = df.assign(**overrides)

                table = pa.Table.from_pandas(df, preserve_index=False)
                # delta-rs rejects Arrow null-typed columns (all-null object
                # columns on small/short-window runs); cast them to concrete
                # types derived from the static schema before committing.
                table = _coerce_null_columns(table, _dim_table_name_for_file(f.stem))
                write_deltalake(str(delta_out), table, mode="overwrite")

        else:
            raise ValueError(
                f"Unknown file_format {file_format!r}. "
                "Expected one of: 'parquet', 'csv', 'deltaparquet'."
            )

        done(f"Created final folder: {final_folder.name}")
        return final_folder
