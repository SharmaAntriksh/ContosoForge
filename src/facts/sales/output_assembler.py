"""Output assembly for the sales fact.

Merges per-chunk parquet/CSV outputs, writes the final tables (parquet / delta),
and holds the result-shape dataclasses (TableOutputs / SalesRunManifest /
SalesFactResult) plus ChunkResultCollector (the pool-completion collector that
also feeds the optional streamed-fact accumulators).

Self-contained output layer: imports only output_paths, sales_writer, pool,
exceptions, logging, config_helpers, the _CSV_COPY_BUF leaf constant, and stdlib.
_merge_parquet_job stays a top-level function (multiprocessing picklability). The
lazy imports inside _assemble_output / _merge_fact_csv_chunks (write_delta_partitioned,
PoolRunSpec/iter_imap_unordered, tempfile) are kept lazy exactly as before.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.exceptions import PackagingError, SalesError
from src.utils.config_helpers import bool_or as _bool_or
from src.utils.logging_utils import info, work

from .chunk_result import ChunkResult
from .format_writer import SalesOutputFormat, manifest_fields_for
from .output_paths import (
    TABLE_SALES,
    TABLE_SALES_ORDER_DETAIL,
    TABLE_SALES_ORDER_HEADER,
    TABLE_SALES_RETURN,
)
from .prep.sales_helpers import _CSV_COPY_BUF
from .sales_writer import merge_parquet_files, optimize_parquet


@dataclass(frozen=True)
class TableOutputs:
    table: str
    file_format: str
    chunks: list[Any]                 # list[str] for csv/parquet; list[{"part":..,"rows":..}] for delta
    merged_path: Optional[str] = None # parquet only
    delta_table_dir: Optional[str] = None  # delta only
    delta_parts_dir: Optional[str] = None  # delta only
    out_dir: Optional[str] = None     # scratch dir holding this table's chunks


@dataclass(frozen=True)
class SalesRunManifest:
    sales_output: str
    file_format: str
    out_folder: str
    tables: dict[str, TableOutputs]
    # Authoritative OrderNumber-width decision for this run, computed once from
    # the real emitted id ceiling. Threaded to the SQL DDL generator so the
    # CREATE TABLE INT/BIGINT choice matches the parquet dtype exactly.
    order_id_int64: bool = False


@dataclass
class SalesFactResult:
    """Structured return from generate_sales_fact()."""
    chunk_files: List[str]
    manifest: SalesRunManifest
    budget_acc: Any = None
    inventory_acc: Any = None
    wishlists_acc: Any = None
    complaints_acc: Any = None


def build_sales_manifest(
    *,
    tables: list[str],
    output_paths: Any,
    fmt: SalesOutputFormat,
    sales_output: str,
    out_folder: Any,
    created_by_table: Optional[Dict[str, List[Any]]] = None,
    order_id_int64: bool = False,
) -> "SalesRunManifest":
    """Assemble a :class:`SalesRunManifest` from the run's tables.

    Single builder for both the populated manifest (post-assembly, with the
    collector's per-table chunk lists) and the empty early-exit manifest
    (``created_by_table=None`` -> empty chunk lists). Centralizing it keeps the
    per-format manifest-field policy (``manifest_fields_for``) in one place
    instead of duplicated ternaries at each call site.
    """
    per_table: Dict[str, TableOutputs] = {}
    for t in tables:
        mf = manifest_fields_for(fmt, output_paths, t)
        per_table[t] = TableOutputs(
            table=t,
            file_format=fmt.value,
            chunks=list(created_by_table.get(t, [])) if created_by_table is not None else [],
            merged_path=mf.merged_path,
            delta_table_dir=mf.delta_table_dir,
            delta_parts_dir=mf.delta_parts_dir,
            out_dir=mf.out_dir,
        )
    return SalesRunManifest(
        sales_output=sales_output,
        file_format=fmt.value,
        out_folder=str(out_folder),
        tables=per_table,
        order_id_int64=bool(order_id_int64),
    )


class ChunkResultCollector:
    """Collects per-chunk :class:`ChunkResult` results from the worker pool.

    Feeds the streamed micro-aggregates into their registered accumulators and
    records the artifact written for each table (for the run manifest + progress
    logging). Output assembly itself re-globs the scratch dir, so this recording
    does not decide which files ship.
    """

    _TABLE_SHORT = {
        TABLE_SALES: "sales",
        TABLE_SALES_ORDER_DETAIL: "detail",
        TABLE_SALES_ORDER_HEADER: "header",
        TABLE_SALES_RETURN: "return",
    }

    def __init__(
        self,
        tables: list[str],
        budget_acc,
        inventory_acc,
        wishlists_acc,
        complaints_acc,
    ):
        self.tables = tables
        self.budget_acc = budget_acc
        self.inventory_acc = inventory_acc
        self.wishlists_acc = wishlists_acc
        self.complaints_acc = complaints_acc
        self.created_by_table: Dict[str, List[Any]] = {t: [] for t in tables}
        self.created_files: List[str] = []
        # (agg key emitted by the worker, accumulator, its add-method name).
        # Budget uses add_sales(); the streamed facts use add(). Each add() is a
        # no-op on a None/empty payload, so feeding an absent agg is harmless.
        self._accumulators = [
            ("_budget_agg", budget_acc, "add_sales"),
            ("_inventory_agg", inventory_acc, "add"),
            ("_wishlists_agg", wishlists_acc, "add"),
            ("_complaints_agg", complaints_acc, "add"),
        ]

    def record(self, r: ChunkResult, completed_units: int, total_units: int) -> None:
        for agg_key, acc, add_name in self._accumulators:
            if acc is not None:
                getattr(acc, add_name)(r.aggs.get(agg_key))

        produced: list[str] = []
        # Tables first (stable, human-ordered), then any stray output keys.
        ordered = [t for t in self.tables if t in r.outputs]
        ordered += [t for t in r.outputs if t not in self.tables]
        for table_name in ordered:
            val = r.outputs[table_name]
            self.created_by_table.setdefault(table_name, []).append(val)
            if isinstance(val, str):
                self.created_files.append(val)
            produced.append(self._TABLE_SHORT.get(table_name, table_name))

        if produced:
            tag = f"chunk{int(r.chunk_idx):04d}"
            work(f"[{completed_units}/{total_units}] {tag} -> " + ", ".join(produced))


def _merge_parquet_job(job: tuple) -> None:
    """Pool task: merge one table's parquet chunks (top-level for spawn pickling)."""
    t, chunks, out, delete_after, compression = job
    merge_parquet_files(
        chunks, out, delete_after=delete_after,
        compression=compression, table_name=t, log=False,
    )


def _merge_fact_csv_chunks(
    csv_chunks: list,
    out_dir: Path,
    chunk_prefix: str,
    chunk_size: int,
    delete_chunks: bool,
) -> None:
    """Re-chunk CSV files for a sales fact table to respect chunk_size.

    Output files keep the existing chunk_prefix naming convention
    (e.g. ``sales_chunk0000.csv``, ``returns_chunk0000.csv``).

    Concatenation is done at the **byte** level (raw block copy with inline
    newline counting) rather than the old per-row Python loop, which decoded and
    re-encoded UTF-8 for every one of up to 10⁹ rows on a single GIL-bound core.
    A new output file is started at whole-source-chunk boundaries once the
    current file has reached ``chunk_size`` rows, so each output file holds
    approximately ``chunk_size`` rows (source chunks are never split mid-file).
    Exact per-file row counts are not preserved — no consumer depends on them
    (``BULK INSERT``/``COPY`` ignore per-file row counts, and the SQL generators
    enumerate whatever ``.csv`` files exist) — but the row *data* is identical.

    Files are written in binary mode so LF terminators pass through verbatim
    (no CRLF translation on Windows).

    Writes to a temporary directory first, then moves files into out_dir
    to avoid overwriting source chunks that share the same filename.
    """
    if not csv_chunks:
        return

    with open(csv_chunks[0], "rb") as f:
        header = f.readline()  # raw header bytes, including the line terminator

    # Write to a temp directory so we never clobber source chunks.
    import shutil
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(dir=out_dir, prefix=".merge_"))

    tmp_files: list[Path] = []
    out_f = None
    rows_in_current = 0
    file_idx = 0

    def _open_next():
        nonlocal out_f, rows_in_current, file_idx
        if out_f is not None:
            out_f.close()
        path = tmp_dir / f"{chunk_prefix}{file_idx:04d}.csv"
        tmp_files.append(path)
        out_f = open(path, "wb")
        out_f.write(header)
        rows_in_current = 0
        file_idx += 1

    try:
        _open_next()
        for chunk_path in csv_chunks:
            # Roll to a fresh output file at the chunk boundary once the current
            # file has met its row target (keeps whole source chunks intact).
            if rows_in_current >= chunk_size:
                _open_next()
            with open(chunk_path, "rb") as in_f:
                in_f.readline()  # skip this chunk's header
                while True:
                    buf = in_f.read(_CSV_COPY_BUF)
                    if not buf:
                        break
                    out_f.write(buf)
                    rows_in_current += buf.count(b"\n")  # data rows (header skipped)
    finally:
        if out_f is not None:
            out_f.close()

    # Move the merged files into out_dir BEFORE deleting any source chunk, so a
    # failed move never destroys data that currently lives only in the temp dir.
    # (The old order deleted every source first, then moved — a move failure lost
    # the whole fact.) os.replace is atomic and overwrites any same-named source
    # chunk, whose rows are already captured in the merge.
    out_names: set[str] = set()
    try:
        for tmp_f in tmp_files:
            dest = out_dir / tmp_f.name
            tmp_f.replace(dest)
            out_names.add(tmp_f.name)
    except OSError as exc:
        # Sources are still intact (deletion happens only after this loop); drop
        # the temp dir and fail loudly rather than ship a half-moved fact.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise PackagingError(
            f"Failed to move merged CSV chunk into {out_dir}: {exc}"
        ) from exc

    # Delete source chunks NOT already overwritten by a same-named merged file:
    # their rows are in the merged output, so leaving them would duplicate data.
    # This cleanup is unconditional — the re-chunk reuses the chunk filenames, so
    # (unlike the parquet merge, whose merged file has a distinct name) the
    # delete_chunks flag cannot gate it without duplicating rows.
    for f in csv_chunks:
        if f.name in out_names:
            continue
        try:
            f.unlink()
        except OSError:
            pass

    # Temp dir is now empty (all files moved out); remove it.
    try:
        tmp_dir.rmdir()
    except OSError:
        pass


def _assemble_output(
    file_format, tables, output_paths, collector,
    partition_cols, sales_cfg, sales_output, out_folder_p,
    chunk_size, delete_chunks, merge_parquet, compression,
    row_group_size, optimize_after_merge,
    *, order_id_int64=False, schema_by_table=None,
):
    """Post-pool output assembly: delta writes, CSV re-chunking, or parquet merge.

    ``schema_by_table`` (Delta only) maps a logical table name to its
    authoritative Arrow schema; when present it becomes the Delta write
    contract instead of the schema read from the first part file.
    """
    fmt = SalesOutputFormat.parse(file_format)

    def _make_result():
        return SalesFactResult(
            chunk_files=collector.created_files,
            manifest=build_sales_manifest(
                tables=tables,
                output_paths=output_paths,
                fmt=fmt,
                sales_output=sales_output,
                out_folder=out_folder_p,
                created_by_table=collector.created_by_table,
                order_id_int64=order_id_int64,
            ),
            budget_acc=collector.budget_acc,
            inventory_acc=collector.inventory_acc,
            wishlists_acc=collector.wishlists_acc,
            complaints_acc=collector.complaints_acc,
        )

    if fmt.is_delta:
        from .sales_writer import write_delta_partitioned

        missing_parts = []
        wrote = 0

        for t in tables:
            parts_dir = output_paths.delta_parts_dir(t)
            delta_dir = output_paths.delta_table_dir(t)

            part_files = glob.glob(os.path.join(parts_dir, "**", "*.parquet"), recursive=True)
            if not part_files:
                missing_parts.append((t, parts_dir))
                continue

            write_delta_partitioned(
                parts_folder=parts_dir,
                delta_output_folder=delta_dir,
                partition_cols=partition_cols,
                table_name=t,
                sort_small_parts=_bool_or(getattr(sales_cfg, "sort_delta_parts", False), False),
                canonical_schema=(schema_by_table.get(t) if schema_by_table else None),
            )
            wrote += 1

        if wrote == 0:
            msg = " | ".join([f"{t} -> {p}" for t, p in missing_parts]) if missing_parts else "no parts found"
            raise SalesError(f"No delta parts found for any table. {msg}")

        return _make_result()

    if fmt.is_csv:
        for t in tables:
            csv_dir = Path(output_paths.table_out_dir(t))
            spec = output_paths.spec(t)
            csv_chunks = sorted(csv_dir.glob(f"{spec.chunk_prefix}*.csv"))
            if len(csv_chunks) <= 1:
                continue
            _merge_fact_csv_chunks(csv_chunks, csv_dir, spec.chunk_prefix, chunk_size, delete_chunks)

        return _make_result()

    if fmt.is_parquet:
        if merge_parquet:
            merge_jobs = []
            skipped = []

            for t in tables:
                chunks = sorted(
                    f for f in glob.glob(output_paths.chunk_glob(t, "parquet"))
                    if os.path.isfile(f)
                )
                if not chunks:
                    skipped.append(t)
                    continue
                merge_jobs.append((t, chunks, output_paths.merged_path(t)))

            if merge_jobs:
                short = {
                    TABLE_SALES: "sales",
                    TABLE_SALES_ORDER_DETAIL: "detail",
                    TABLE_SALES_ORDER_HEADER: "header",
                    TABLE_SALES_RETURN: "return",
                }

                counts = [(short.get(t, t), len(chunks)) for (t, chunks, _out) in merge_jobs]

                if len({c for _, c in counts}) == 1:
                    n = counts[0][1]
                    info(f"Merge parquet: {n} chunks -> " + ", ".join(name for name, _ in counts))
                else:
                    info("Merge parquet: " + ", ".join(f"{name}={n}" for name, n in counts))

                # The per-table merges are independent (disjoint chunk sets),
                # and each is a full re-decode+re-encode pass that holds the GIL
                # in its Python-level row-group loop — so run them across
                # processes when there is more than one table.  For a single
                # table there is nothing to overlap, so merge inline and skip
                # the process-spawn overhead.
                if len(merge_jobs) > 1:
                    from src.utils.pool import iter_imap_unordered, PoolRunSpec
                    n_merge = min(len(merge_jobs), max(1, (os.cpu_count() or 1) - 1))
                    jobs = [
                        (t, chunks, out, bool(delete_chunks), compression)
                        for (t, chunks, out) in merge_jobs
                    ]
                    # Drain to completion; iter_imap_unordered surfaces any worker
                    # exception and shares the pipeline's Windows CTRL_C spawn guard.
                    for _ in iter_imap_unordered(
                        tasks=jobs, task_fn=_merge_parquet_job,
                        spec=PoolRunSpec(processes=n_merge, label="parquet-merge"),
                    ):
                        pass
                else:
                    t, chunks, out = merge_jobs[0]
                    merge_parquet_files(
                        chunks,
                        out,
                        delete_after=bool(delete_chunks),
                        compression=compression,
                        table_name=t,
                        log=False,
                    )

                if optimize_after_merge:
                    info("Optimize parquet: sorting merged files...")
                    for t, _chunks, out in merge_jobs:
                        result = optimize_parquet(
                            out,
                            table_name=t,
                            compression=compression,
                            row_group_size=row_group_size,
                        )
                        if result:
                            info(f"  Optimized: {os.path.basename(out)}")
            else:
                info("Merge parquet: none")

        return _make_result()

    raise SalesError(f"Unknown file_format: {file_format}")
