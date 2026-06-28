from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Optional, Sequence, Set, Union

from .utils import _arrow, _ensure_dir_for_file, done, info, skip, warn
from .projection import _project_table_to_schema, project_table_to_schema
from .encoding import _schema_dict_cols, _validate_required, required_pricing_cols_for_table

PathLike = Union[str, os.PathLike]

# ---------------------------------------------------------------------------
# Defaults (module-level so callers can inspect / override)
# ---------------------------------------------------------------------------
DEFAULT_COMPRESSION: str = "snappy"
"""Default Parquet compression codec used by merge_parquet_files."""

# ---------------------------------------------------------------------------
# Module-level cache for _expected_cols_for_table imports
# ---------------------------------------------------------------------------
_EXPECTED_COLS_CACHE: dict[str, tuple[str, ...]] = {}
_EXPECTED_COLS_LOADED = False


def _pm_open_parquet(path: str, pa, pq):
    try:
        source = pa.memory_map(path, "r")
    except OSError:
        source = pa.OSFile(path, "rb")
    reader = pq.ParquetFile(source)
    try:
        setattr(reader, "path", path)
    except AttributeError:
        pass
    return source, reader


def _pm_schema_equals(schema_a, schema_b, *, check_metadata: bool) -> bool:
    if schema_a is schema_b:
        return True
    if len(schema_a) != len(schema_b):
        return False
    for fa, fb in zip(schema_a, schema_b):
        if fa.name != fb.name or not fa.type.equals(fb.type):
            return False
    if check_metadata:
        return getattr(schema_a, "metadata", None) == getattr(schema_b, "metadata", None)
    return True


def _pm_build_canonical_schema(
    parquet_files: list[str],
    *,
    schema_strategy: str,
    pa,
    pq,
):
    if not parquet_files:
        raise ValueError("parquet_files cannot be empty")

    base = pq.read_schema(parquet_files[0])

    if schema_strategy == "first":
        return base

    if schema_strategy != "union":
        raise ValueError(f"Unknown schema_strategy={schema_strategy!r} (use 'first' or 'union')")

    remaining = parquet_files[1:]
    if not remaining:
        return base

    # Parallel schema reads for union strategy (I/O-bound, benefits from threads).
    # For small file counts (<8), sequential is faster due to thread overhead.
    if len(remaining) >= 8:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(remaining))) as pool:
            schemas = list(pool.map(pq.read_schema, remaining))
    else:
        schemas = [pq.read_schema(p) for p in remaining]

    fields_by_name = {f.name: f for f in base}
    ordered_names = list(fields_by_name)

    for sch in schemas:
        for f in sch:
            if f.name not in fields_by_name:
                fields_by_name[f.name] = f
                ordered_names.append(f.name)

    return pa.schema(
        [fields_by_name[n] for n in ordered_names],
        metadata=getattr(base, "metadata", None),
    )


def _pm_restrict_schema_to_expected(schema, expected_cols: Sequence[str], pa, *, table_name: str = "table"):
    fields = {f.name: f for f in schema}
    missing = [c for c in expected_cols if c not in fields]
    if missing:
        raise RuntimeError(
            f"[{table_name}] canonical schema missing expected columns {missing}. "
            "This usually means wrong chunk files were passed to the merge."
        )
    return pa.schema([fields[c] for c in expected_cols], metadata=getattr(schema, "metadata", None))


def _pm_row_group_num_rows(reader, rg_index: int) -> int:
    try:
        md = reader.metadata
        if md is not None:
            return int(md.row_group(rg_index).num_rows)
    except (AttributeError, TypeError, ValueError):
        pass
    return -1


def _pm_read_row_group_projected(
    reader,
    rg_index: int,
    schema,
    *,
    available: Set[str],
    required_cols: Optional[Set[str]] = None,
    cast_safe: bool = True,
    pa=None,
    pc=None,
):
    if required_cols:
        missing_required = required_cols - available
        if missing_required:
            raise RuntimeError(
                f"Parquet chunk is missing required columns {sorted(missing_required)}; "
                f"file={getattr(reader, 'path', '')}"
            )

    cols_to_read = [c for c in schema.names if c in available]
    if not cols_to_read:
        n = _pm_row_group_num_rows(reader, rg_index)
        if n < 0:
            t0 = reader.read_row_group(rg_index, columns=[])
            return project_table_to_schema(t0, schema, cast_safe=cast_safe, pa=pa, pc=pc)

        arrays = [pa.nulls(n, type=field.type) for field in schema]
        return pa.Table.from_arrays(arrays, schema=schema)

    t = reader.read_row_group(rg_index, columns=cols_to_read)
    return project_table_to_schema(t, schema, cast_safe=cast_safe, pa=pa, pc=pc)


def _resolve_parquet_files(
    parquet_files: Iterable[PathLike],
    *,
    sort_files: bool = True,
) -> list[str]:
    """Resolve an iterable of path-likes to a sorted list of existing absolute paths."""
    files: list[str] = []
    for f in parquet_files:
        if not f:
            continue
        fp = os.path.abspath(os.fspath(f))
        if os.path.exists(fp):
            files.append(fp)
    if sort_files:
        files.sort()
    return files


def _prepare_schema_and_writer(
    files: list[str],
    merged_file_abs: str,
    *,
    schema_strategy: str,
    canonical_schema,
    expected_cols: Optional[Sequence[str]],
    validate_schema_fn: Optional[Callable[[Any], None]],
    use_dictionary: Union[bool, Sequence[str]],
    dict_exclude: Optional[Set[str]],
    compression: str,
    compression_level: int | None,
    write_statistics: bool,
    pa,
    pq,
):
    """Build the canonical schema and open a ParquetWriter.

    Returns (schema, writer).
    """
    schema = canonical_schema
    if schema is None:
        schema = _pm_build_canonical_schema(files, schema_strategy=schema_strategy, pa=pa, pq=pq)

    if expected_cols:
        schema = _pm_restrict_schema_to_expected(schema, expected_cols, pa, table_name="merge")

    if validate_schema_fn is not None:
        validate_schema_fn(schema)

    use_dict = use_dictionary
    if use_dictionary is True and dict_exclude:
        use_dict = [c for c in schema.names if c not in dict_exclude]

    writer_kwargs = dict(
        compression=compression,
        use_dictionary=use_dict,
        write_statistics=bool(write_statistics),
    )
    if compression_level is not None:
        writer_kwargs["compression_level"] = int(compression_level)

    try:
        writer = pq.ParquetWriter(merged_file_abs, schema, **writer_kwargs)
    except TypeError:
        writer_kwargs.pop("compression_level", None)
        writer = pq.ParquetWriter(merged_file_abs, schema, **writer_kwargs)

    return schema, writer


def _validate_chunk_columns(
    chunk_cols: Set[str],
    expected_set: Optional[frozenset[str]],
    *,
    strict_expected: bool,
    reject_extra_cols: bool,
    path: str,
) -> None:
    """Validate a chunk's columns against expected columns."""
    if not expected_set:
        return
    if strict_expected:
        missing = sorted(expected_set - chunk_cols)
        if missing:
            raise RuntimeError(f"Chunk missing expected columns {missing}: {path}")
    if reject_extra_cols:
        extra = sorted(chunk_cols - expected_set)
        if extra:
            raise RuntimeError(f"Chunk has unexpected columns {extra}: {path}")


def _write_chunk_row_groups(
    writer,
    reader,
    schema,
    *,
    chunk_cols: Set[str],
    expected_cols: Optional[Sequence[str]],
    required_cols: Optional[Set[str]],
    cast_safe: bool,
    pa,
    pc,
) -> None:
    """Read row groups from a single chunk file and write them to the merged writer."""
    schema_exact = _pm_schema_equals(reader.schema_arrow, schema, check_metadata=True)
    schema_same_fields = (
        schema_exact or _pm_schema_equals(reader.schema_arrow, schema, check_metadata=False)
    )
    # Check if field types match (common case: same schema, different metadata)
    _reader_names = set(reader.schema_arrow.names)
    schema_types_match = schema_same_fields and all(
        reader.schema_arrow.field(f.name).type == f.type
        for f in schema
        if f.name in _reader_names
    )
    schema_names = schema.names

    for rg in range(reader.num_row_groups):
        if schema_exact and not expected_cols:
            writer.write_table(reader.read_row_group(rg))
            continue

        if schema_types_match:
            # Fields and types match — read in schema order, skip per-field cast loop
            writer.write_table(reader.read_row_group(rg, columns=schema_names))
            continue

        if schema_same_fields:
            t = reader.read_row_group(rg, columns=schema_names)
            writer.write_table(
                project_table_to_schema(t, schema, cast_safe=cast_safe, pa=pa, pc=pc)
            )
            continue

        t = _pm_read_row_group_projected(
            reader,
            rg,
            schema,
            available=chunk_cols,
            required_cols=required_cols,
            cast_safe=cast_safe,
            pa=pa,
            pc=pc,
        )
        writer.write_table(t)


def _merge_parquet_files_common(
    parquet_files: Iterable[PathLike],
    merged_file: PathLike,
    delete_after: bool = False,
    *,
    compression: str = DEFAULT_COMPRESSION,
    compression_level: int | None = None,
    write_statistics: bool = True,
    schema_strategy: str = "union",
    canonical_schema=None,
    expected_cols: Optional[Sequence[str]] = None,
    strict_expected: bool = False,
    reject_extra_cols: bool = False,
    required_cols: Optional[Set[str]] = None,
    use_dictionary: Union[bool, Sequence[str]] = True,
    dict_exclude: Optional[Set[str]] = None,
    validate_schema_fn: Optional[Callable[[Any], None]] = None,
    cast_safe: bool = True,
    sort_files: bool = True,
    log_prefix: str = "",
    log: bool = True,
) -> Optional[str]:
    pa, pc, pq = _arrow()

    files = _resolve_parquet_files(parquet_files, sort_files=sort_files)
    if not files:
        if log:
            skip(f"{log_prefix}No parquet chunk files to merge".strip())
        return None

    merged_file_abs = os.path.abspath(os.fspath(merged_file))
    _ensure_dir_for_file(merged_file_abs)

    if log:
        info(f"{log_prefix}Merging {len(files)} chunks: {os.path.basename(merged_file_abs)}".strip())

    schema, writer = _prepare_schema_and_writer(
        files, merged_file_abs,
        schema_strategy=schema_strategy,
        canonical_schema=canonical_schema,
        expected_cols=expected_cols,
        validate_schema_fn=validate_schema_fn,
        use_dictionary=use_dictionary,
        dict_exclude=dict_exclude,
        compression=compression,
        compression_level=compression_level,
        write_statistics=write_statistics,
        pa=pa, pq=pq,
    )

    expected_set = frozenset(expected_cols) if expected_cols else None

    try:
        for path in files:
            src, reader = _pm_open_parquet(path, pa, pq)
            try:
                chunk_cols = set(reader.schema_arrow.names)

                _validate_chunk_columns(
                    chunk_cols, expected_set,
                    strict_expected=strict_expected,
                    reject_extra_cols=reject_extra_cols,
                    path=path,
                )

                _write_chunk_row_groups(
                    writer, reader, schema,
                    chunk_cols=chunk_cols,
                    expected_cols=expected_cols,
                    required_cols=required_cols,
                    cast_safe=cast_safe,
                    pa=pa, pc=pc,
                )
            finally:
                try:
                    src.close()
                except OSError:
                    pass
    finally:
        writer.close()

    if delete_after:
        for path in files:
            try:
                os.remove(path)
            except OSError as ex:
                warn(f"[merge] Failed to delete chunk {os.path.basename(path)}: {ex}")

    if log:
        done(f"{log_prefix}Merged chunks: {os.path.basename(merged_file_abs)}".strip())
    return merged_file_abs


def _load_expected_cols_schemas():
    """One-time import of static column tuples from static_schemas."""
    global _EXPECTED_COLS_LOADED
    if _EXPECTED_COLS_LOADED:
        return

    from src.utils.static_schemas import (
        _SALES_ORDER_HEADER_COLS,
        _SALES_ORDER_DETAIL_COLS,
    )

    _EXPECTED_COLS_CACHE["salesorderheader"] = _SALES_ORDER_HEADER_COLS
    _EXPECTED_COLS_CACHE["salesorderdetail"] = _SALES_ORDER_DETAIL_COLS
    _EXPECTED_COLS_LOADED = True


def _expected_cols_for_table(table_name: str | None) -> tuple[str, ...] | None:
    if not table_name:
        return None

    _load_expected_cols_schemas()

    norm = table_name.replace("_", "").replace(" ", "").casefold()
    return _EXPECTED_COLS_CACHE.get(norm)


def _read_row_group_projected(reader, rg_index: int, schema, *, table_name: str | None):
    """Back-compat symbol: sales code may import this.

    Sales-specific required pricing columns enforcement + projection to canonical schema.
    """
    required = required_pricing_cols_for_table(table_name)

    available = set(reader.schema_arrow.names)
    missing_required = required - available
    if missing_required:
        raise RuntimeError(
            f"Parquet chunk is missing required columns {sorted(missing_required)}; "
            f"file={getattr(reader, 'path', '')}"
        )

    cols_to_read = [c for c in schema.names if c in available]
    table = reader.read_row_group(rg_index, columns=cols_to_read)
    return _project_table_to_schema(table, schema)


def merge_parquet_files(
    parquet_files: Iterable[str],
    merged_file: str,
    delete_after: bool = False,
    *,
    compression: str = DEFAULT_COMPRESSION,
    compression_level: int | None = None,
    write_statistics: bool = True,
    table_name: str | None = None,
    schema_strategy: str = "union",
    log: bool = True,
) -> Optional[str]:
    """Merge multiple Parquet chunk files into a single consolidated Parquet file.

    Builds a canonical schema (via union or first-file strategy), validates
    required pricing columns for line-grain tables, restricts to expected
    columns when a known ``table_name`` is provided, and applies dictionary
    encoding to eligible string columns.

    Parameters
    ----------
    parquet_files : Iterable[str]
        Paths to the source Parquet chunk files.
    merged_file : str
        Destination path for the merged output file.
    delete_after : bool
        If True, remove source chunk files after a successful merge.
    compression : str
        Parquet compression codec (default ``DEFAULT_COMPRESSION``).
    compression_level : int | None
        Optional compression level passed to PyArrow.
    write_statistics : bool
        Whether to write column statistics in the Parquet footer.
    table_name : str | None
        Logical table name (e.g. ``"OrderDetail"``).  When set,
        expected-column enforcement and pricing validation are applied.
    schema_strategy : str
        ``"union"`` (default) merges all chunk schemas; ``"first"`` uses
        the first file's schema as-is.
    log : bool
        Emit progress/skip log messages.

    Returns
    -------
    str | None
        Absolute path to the merged file, or ``None`` if no input files
        were found.
    """
    pa, _, pq = _arrow()

    files = [os.path.abspath(p) for p in parquet_files if p and os.path.exists(p)]
    if not files:
        if log:
            skip("No parquet chunk files to merge")
        return None

    files.sort()

    canonical_schema = _pm_build_canonical_schema(files, schema_strategy=schema_strategy, pa=pa, pq=pq)

    expected_cols = _expected_cols_for_table(table_name)
    if expected_cols:
        canonical_schema = _pm_restrict_schema_to_expected(
            canonical_schema, expected_cols, pa, table_name=(table_name or "table")
        )

    _validate_required(canonical_schema, table_name=table_name)
    dict_cols = _schema_dict_cols(canonical_schema, table_name=table_name)

    required_set = required_pricing_cols_for_table(table_name)
    required_cols = required_set if required_set else None

    return _merge_parquet_files_common(
        files,
        merged_file,
        delete_after=delete_after,
        compression=compression,
        compression_level=compression_level,
        write_statistics=write_statistics,
        canonical_schema=canonical_schema,
        expected_cols=expected_cols,
        strict_expected=bool(expected_cols),
        reject_extra_cols=bool(expected_cols),
        required_cols=required_cols,
        use_dictionary=dict_cols,
        sort_files=False,
        log=log,
    )


# ---------------------------------------------------------------------------
# Post-merge optimize: sort + rewrite
# ---------------------------------------------------------------------------

# Sort keys per table type — OrderDate + StoreKey for Sales/Header,
# OrderNumber for Detail, ReturnDate for Returns.
_SORT_KEYS_BY_TABLE: dict[str, list[tuple[str, str]]] = {
    "Sales": [("OrderDate", "ascending"), ("StoreKey", "ascending")],
    "OrderHeader": [("OrderDate", "ascending"), ("StoreKey", "ascending")],
    "OrderDetail": [("OrderNumber", "ascending"), ("OrderLineNumber", "ascending")],
    "Returns": [("ReturnDate", "ascending"), ("OrderNumber", "ascending")],
}


def optimize_parquet(
    file_path: str,
    *,
    sort_keys: Optional[list[tuple[str, str]]] = None,
    table_name: Optional[str] = None,
    compression: str = DEFAULT_COMPRESSION,
    row_group_size: int = 1_000_000,
    batch_row_groups: int = 10,
) -> Optional[str]:
    """Sort and rewrite a merged parquet file for better query performance.

    Reads ``batch_row_groups`` row groups at a time, sorts them, and writes to
    a temp file, then replaces the original.  This bounds peak memory to
    roughly ``batch_row_groups * row_group_size`` rows instead of the full file.

    Parameters
    ----------
    file_path : str
        Path to the parquet file to optimize.
    sort_keys : list of (column, order) tuples, optional
        Sort specification.  If *None*, inferred from ``table_name``.
    table_name : str, optional
        Logical table name used to pick default sort keys.
    compression : str
        Compression codec for the rewritten file.
    row_group_size : int
        Row group size for the rewritten file.
    batch_row_groups : int
        Number of row groups to read, sort, and flush at a time.

    Returns
    -------
    str | None
        Path to the optimized file, or None if skipped.
    """
    pa, pc, pq = _arrow()

    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        return None

    # Resolve sort keys
    if sort_keys is None and table_name:
        sort_keys = _SORT_KEYS_BY_TABLE.get(table_name)
    if not sort_keys:
        return None

    pf = pq.ParquetFile(file_path)
    schema = pf.schema_arrow
    n_rg = pf.metadata.num_row_groups

    # Validate that sort columns exist in the file
    file_cols = set(schema.names)
    sort_keys = [(col, order) for col, order in sort_keys if col in file_cols]
    if not sort_keys:
        pf.close() if hasattr(pf, "close") else None
        return None

    # Determine dictionary encoding columns (string/binary)
    dict_cols = [
        f.name for f in schema
        if pa.types.is_string(f.type) or pa.types.is_large_string(f.type)
        or pa.types.is_binary(f.type) or pa.types.is_large_binary(f.type)
    ]

    tmp_path = file_path + ".optimize_tmp"
    writer = pq.ParquetWriter(
        tmp_path,
        schema,
        compression=compression,
        use_dictionary=dict_cols if dict_cols else False,
        write_statistics=True,
    )

    ok = False
    try:
        for start in range(0, n_rg, batch_row_groups):
            end = min(start + batch_row_groups, n_rg)
            tables = [pf.read_row_group(i) for i in range(start, end)]
            batch = pa.concat_tables(tables)
            del tables

            indices = pc.sort_indices(batch, sort_keys=sort_keys)
            batch = batch.take(indices)
            del indices

            writer.write_table(batch, row_group_size=row_group_size)
            del batch
        ok = True
    finally:
        writer.close()
        try:
            pf.close() if hasattr(pf, "close") else None
        except OSError:
            pass
        if not ok and os.path.exists(tmp_path):
            os.remove(tmp_path)

    # Replace original with optimized file
    os.replace(tmp_path, file_path)
    return file_path
