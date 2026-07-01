from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, TYPE_CHECKING, Union

if TYPE_CHECKING:
    import pyarrow as pa

from .utils import _arrow, info, warn
from .encoding import _validate_required
from .projection import project_table_to_schema
from .parquet_merge import _pm_schema_equals

ValidateSchemaFn = Callable[..., None]

DEFAULT_SORT_ROW_LIMIT: int = 2_000_000
"""Parts with more rows than this threshold are not sorted before Delta writes."""


@dataclass(frozen=True)
class DeltaWriteResult:
    part_files: List[str]
    delta_output_folder: str
    partition_cols: List[str]


def _delta_import_write_deltalake():
    try:
        from deltalake import write_deltalake  # type: ignore
        return write_deltalake
    except ImportError as e1:
        try:
            from deltalake.writer import write_deltalake  # type: ignore
            return write_deltalake
        except ImportError as e2:
            raise RuntimeError(
                "deltalake is required for Delta output, but import failed. "
                f"top-level error={e1!r}; fallback error={e2!r}"
            ) from e2


def _delta_resolve_part_files(
    *,
    parts_folder: Optional[str],
    parts: Optional[Iterable[Union[str, dict]]],
) -> List[str]:
    part_files: List[str] = []

    if parts is not None:
        for p in parts:
            if isinstance(p, dict):
                name = p.get("part")
                if not name:
                    continue
                if os.path.isabs(name):
                    pf = name
                else:
                    if not parts_folder:
                        raise ValueError("parts_folder is required when passing relative part names")
                    pf = os.path.join(parts_folder, name)
                if os.path.exists(pf) and pf.endswith(".parquet"):
                    part_files.append(os.path.abspath(pf))
            elif isinstance(p, str):
                pf = p
                if not os.path.isabs(pf) and parts_folder:
                    pf = os.path.join(parts_folder, pf)
                pf = os.path.abspath(pf)
                if os.path.exists(pf) and pf.endswith(".parquet"):
                    part_files.append(pf)
    else:
        if not parts_folder or not os.path.exists(parts_folder):
            raise FileNotFoundError(f"Parts folder not found: {parts_folder}")
        part_files = sorted(
            os.path.abspath(os.path.join(parts_folder, f))
            for f in os.listdir(parts_folder)
            if f.endswith(".parquet")
        )

    return part_files


def _open_parquet_mapped(path: str, pa, pq):
    """Open a parquet file with memory mapping for efficient I/O."""
    try:
        source = pa.memory_map(path, "r")
    except OSError:
        source = pa.OSFile(path, "rb")
    reader = pq.ParquetFile(source)
    return source, reader


def _resolve_partition_cols(
    partition_cols: Optional[Sequence[str]],
    canonical_names: set[str],
    *,
    on_missing: str = "drop",
    table_name: Optional[str] = None,
) -> List[str]:
    """Validate partition columns against the canonical schema."""
    pcols = list(partition_cols or [])
    missing = [c for c in pcols if c not in canonical_names]
    if missing:
        if on_missing == "error":
            raise RuntimeError(f"Partition columns missing from schema: {missing}")
        pcols = [c for c in pcols if c in canonical_names]
        info(
            f"[DELTA] Partition cols missing for table={table_name or 'unknown'}; "
            f"dropping {missing}. Remaining={pcols}"
        )
    return pcols


def _read_and_project_part(pf, canonical_schema, *, needs_projection: bool, pa):
    """Read all row groups from a Parquet file, projecting if needed.

    Returns a single Arrow table.
    """
    if pf.num_row_groups == 1:
        table = pf.read_row_group(0)
    else:
        tables = []
        for rg in range(pf.num_row_groups):
            tables.append(pf.read_row_group(rg))
        table = pa.concat_tables(tables, promote_options="none")

    if needs_projection:
        table = project_table_to_schema(
            table, canonical_schema, cast_safe=False, on_cast_error="warn"
        )
    return table


def _cleanup_parts_folder(
    delta_output_abs: str,
    parts_folder_abs: Optional[str],
) -> None:
    """Remove parts folder after verifying delta output exists."""
    if not parts_folder_abs:
        return
    delta_log = os.path.join(delta_output_abs, "_delta_log")
    if not os.path.isdir(delta_log):
        warn(
            f"[DELTA] Delta log not found at {delta_log} after write; "
            "skipping parts folder cleanup to avoid data loss."
        )
    else:
        try:
            shutil.rmtree(parts_folder_abs)
        except OSError as ex:
            warn(f"[DELTA] Failed to clean up parts folder {parts_folder_abs}: {ex}")


def write_delta_single_commit(
    *,
    part_files: List[str],
    delta_output_abs: str,
    canonical_schema,
    pcols: List[str],
    sort_small_parts: bool,
    sort_row_limit: int,
    write_deltalake,
    pa,
    pq,
    target_file_size: int = 256 * 1024 * 1024,
) -> None:
    """Write all part files to Delta in ONE streamed commit (mode='overwrite').

    Replaces the per-part ``append`` loop, which (a) paid Delta's full-state log
    replay on *every* append — O(N²) in the number of parts — and (b) scattered
    a tiny file per (part × partition it touched), producing the small-file
    problem.  Here a single ``RecordBatchReader`` streams one row group at a time
    into one ``write_deltalake`` call: one commit (one log entry), peak memory of
    ~one row group (also removes the per-part ``concat_tables`` double-buffer),
    and file sizing handed to delta-rs via ``target_file_size``.

    Small parts are still materialized and sorted by the partition columns for
    locality (bounded by ``sort_row_limit``); large parts stream row-group by
    row-group.
    """
    def _rebatch(b):
        # RecordBatchReader.from_batches requires each batch to carry the reader
        # schema; rewrap cheaply (zero-copy) when only metadata differs.
        if b.schema.equals(canonical_schema):
            return b
        return pa.record_batch(b.columns, schema=canonical_schema)

    def _batch_stream():
        for pf_path in part_files:
            source, pf = _open_parquet_mapped(pf_path, pa, pq)
            try:
                needs_proj = not _pm_schema_equals(
                    pf.schema_arrow, canonical_schema, check_metadata=False
                )
                num_rows = 0
                try:
                    if pf.metadata is not None:
                        num_rows = int(pf.metadata.num_rows)
                except (AttributeError, TypeError, ValueError):
                    num_rows = 0
                needs_sort = bool(
                    pcols and sort_small_parts and num_rows
                    and num_rows <= int(sort_row_limit)
                )

                if needs_sort:
                    table = _read_and_project_part(
                        pf, canonical_schema, needs_projection=needs_proj, pa=pa
                    )
                    try:
                        table = table.sort_by([(c, "ascending") for c in pcols])
                    except (KeyError, ValueError, TypeError) as ex:
                        warn(f"[DELTA] Sort failed for {os.path.basename(pf_path)}: {ex}")
                    for b in table.to_batches():
                        yield _rebatch(b)
                else:
                    for rg in range(pf.num_row_groups):
                        table = pf.read_row_group(rg)
                        if needs_proj:
                            table = project_table_to_schema(
                                table, canonical_schema,
                                cast_safe=False, on_cast_error="warn",
                            )
                        for b in table.to_batches():
                            yield _rebatch(b)
            finally:
                try:
                    source.close()
                except OSError:
                    pass

    # Try with target_file_size (delta-rs packs to ~that size); degrade
    # gracefully on older deltalake that lacks the kwarg. A fresh reader is
    # built per attempt because RecordBatchReader is single-use.
    last_exc: Optional[TypeError] = None
    for extra in ({"target_file_size": int(target_file_size)}, {}):
        reader = pa.RecordBatchReader.from_batches(canonical_schema, _batch_stream())
        try:
            write_deltalake(
                delta_output_abs, reader,
                partition_by=pcols, mode="overwrite", **extra,
            )
            return
        except TypeError as exc:
            last_exc = exc
            continue

    # Both attempts raised TypeError. The second used the historically-valid
    # signature, so this is a genuine failure (e.g. a reader/schema problem),
    # not an unsupported-kwarg case — surface it instead of silently no-op'ing
    # the write and reporting success to the caller.
    raise RuntimeError(
        f"Delta write failed for {delta_output_abs}: {last_exc}"
    ) from last_exc


def write_delta_from_parquet_parts(
    *,
    parts_folder: Optional[str],
    delta_output_folder: str,
    partition_cols: Optional[Sequence[str]] = None,
    parts: Optional[Iterable[Union[str, dict]]] = None,
    table_name: Optional[str] = None,
    # Authoritative schema. When supplied (the pipeline passes the run's
    # WorkerSchemaBundle schema for this table), it is the write contract; the
    # first part file is only *read* as a fallback for untrusted external inputs.
    canonical_schema: "Optional[pa.Schema]" = None,
    # Policy knobs
    validate_schema: Optional[ValidateSchemaFn] = None,
    on_missing_partition_cols: str = "drop",  # "drop" | "error"
    # Performance knobs
    sort_small_parts: bool = True,
    sort_row_limit: int = DEFAULT_SORT_ROW_LIMIT,
    # Housekeeping
    cleanup_parts_folder: bool = False,
) -> DeltaWriteResult:
    pa, _, pq = _arrow()
    write_deltalake = _delta_import_write_deltalake()

    parts_folder_abs = os.path.abspath(parts_folder) if parts_folder else None
    delta_output_abs = os.path.abspath(delta_output_folder)

    part_files = _delta_resolve_part_files(parts_folder=parts_folder_abs, parts=parts)
    if not part_files:
        raise RuntimeError(
            f"No delta part files found in folder: {parts_folder_abs or '(no folder specified)'}"
        )

    # Prefer the caller's authoritative schema; only sniff the first part file
    # when none was supplied (which risks adopting an atypical part's schema).
    if canonical_schema is None:
        canonical_schema = pq.read_schema(part_files[0])

    if validate_schema is not None:
        validate_schema(canonical_schema, table_name=table_name)

    pcols = _resolve_partition_cols(
        partition_cols, set(canonical_schema.names),
        on_missing=on_missing_partition_cols, table_name=table_name,
    )

    os.makedirs(delta_output_abs, exist_ok=True)

    suffix = f" table={table_name}" if table_name else ""
    info(f"[DELTA] Writing {len(part_files)} parts (Arrow -> Delta, single commit){suffix}")

    write_delta_single_commit(
        part_files=part_files,
        delta_output_abs=delta_output_abs,
        canonical_schema=canonical_schema,
        pcols=pcols,
        sort_small_parts=sort_small_parts,
        sort_row_limit=sort_row_limit,
        write_deltalake=write_deltalake,
        pa=pa, pq=pq,
    )

    if cleanup_parts_folder and parts is None:
        _cleanup_parts_folder(delta_output_abs, parts_folder_abs)

    return DeltaWriteResult(
        part_files=part_files,
        delta_output_folder=delta_output_abs,
        partition_cols=pcols,
    )


def write_delta_partitioned(
    parts_folder: str,
    delta_output_folder: str,
    partition_cols: Optional[List[str]] = None,
    parts: Optional[Iterable[Union[str, dict]]] = None,
    *,
    sort_small_parts: bool = True,
    sort_row_limit: int = DEFAULT_SORT_ROW_LIMIT,
    table_name: str | None = None,
    canonical_schema: "Optional[pa.Schema]" = None,
):
    """Write Parquet part files to a partitioned Delta Lake table.

    This is the high-level sales-specific entry point.  It validates
    required pricing columns, resolves partition columns against the
    schema, optionally sorts small parts for better Delta locality, and
    cleans up the parts folder on success.

    Parameters
    ----------
    parts_folder : str
        Directory containing the source Parquet part files.
    delta_output_folder : str
        Destination directory for the Delta Lake table.
    partition_cols : list[str] | None
        Columns to partition by.  Missing columns are dropped (with a
        warning) when ``table_name`` is set, or cause an error otherwise.
    parts : Iterable[str | dict] | None
        Explicit list of part files/dicts.  When ``None``, all ``.parquet``
        files in ``parts_folder`` are used.
    sort_small_parts : bool
        Sort parts with <= ``sort_row_limit`` rows by partition columns
        before writing (improves Delta read performance).
    sort_row_limit : int
        Row count threshold for sorting (default ``DEFAULT_SORT_ROW_LIMIT``).
    table_name : str | None
        Logical table name for validation and logging.
    canonical_schema : pa.Schema | None
        Authoritative write schema for the table (the run's WorkerSchemaBundle
        schema).  When ``None``, the schema is read from the first part file.
    """
    on_missing = "error" if table_name is None else "drop"

    write_delta_from_parquet_parts(
        parts_folder=parts_folder,
        delta_output_folder=delta_output_folder,
        partition_cols=partition_cols,
        parts=parts,
        table_name=table_name,
        canonical_schema=canonical_schema,
        validate_schema=_validate_required,
        on_missing_partition_cols=on_missing,
        sort_small_parts=sort_small_parts,
        sort_row_limit=sort_row_limit,
        cleanup_parts_folder=(parts is None),
    )


# ---------------------------------------------------------------------------
# Backward-compat aliases for symbols that were refactored away.
# ---------------------------------------------------------------------------

def _delta_import_pyarrow():
    """Deprecated: use ``from .utils import _arrow`` instead."""
    pa, _, pq = _arrow()
    return pa, pq


def _delta_project_table_to_schema_best_effort(table, canonical_schema):
    """Deprecated: use ``project_table_to_schema(..., on_cast_error='warn')``."""
    return project_table_to_schema(
        table, canonical_schema, cast_safe=False, on_cast_error="warn"
    )
