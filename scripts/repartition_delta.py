"""Change the partition layout of Delta Lake tables in a generated dataset.

Reads each table partition-by-partition (streaming) to keep memory low,
then rewrites with the new layout. Unpartitioned tables (dimensions, etc.)
are automatically skipped.

Usage:
    # Demote Year+Month -> Year (fewer, larger files)
    python scripts/repartition_delta.py <dataset_folder> --partition-by year

    # Promote Year -> Year+Month (finer pruning)
    python scripts/repartition_delta.py <dataset_folder> --partition-by year-month

    # Remove partitions entirely (single consolidated Delta table)
    python scripts/repartition_delta.py <dataset_folder> --partition-by none

    # Recompress while repartitioning
    python scripts/repartition_delta.py <dataset_folder> --partition-by year --compression ZSTD
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.exceptions import PackagingError

_PARTITION_PRESETS = {
    "none": [],
    "year": ["Year"],
    "year-month": ["Year", "Month"],
}
_VALID_COMPRESSIONS = ("UNCOMPRESSED", "SNAPPY", "GZIP", "BROTLI", "LZ4", "ZSTD", "LZ4_RAW")


def find_delta_tables(root: Path) -> list[Path]:
    """Find directories containing _delta_log/ (Delta Lake tables)."""
    tables = []
    for delta_log in root.rglob("_delta_log"):
        if delta_log.is_dir():
            tables.append(delta_log.parent)
    return sorted(tables)


def _get_partition_cols(dt) -> list[str]:
    """Read current partition columns from Delta table metadata."""
    try:
        return list(dt.metadata().partition_columns)
    except Exception:
        return []


def _find_date_col(schema) -> str | None:
    """Return the first date32/date64/timestamp column in the schema."""
    import pyarrow as pa
    for f in schema:
        if pa.types.is_date32(f.type) or pa.types.is_date64(f.type) or pa.types.is_timestamp(f.type):
            return f.name
    return None


def _ensure_partition_cols(table, target_cols: list[str], current_cols: list[str]):
    """Add missing Year/Month columns; drop partition cols not in target."""
    import pyarrow as pa
    import pyarrow.compute as pc

    cols_to_drop = [c for c in current_cols if c not in target_cols and c in table.column_names]
    if cols_to_drop:
        table = table.drop_columns(cols_to_drop)

    col_names = set(table.column_names)
    missing = [c for c in target_cols if c not in col_names]
    if not missing:
        return table

    date_col = _find_date_col(table.schema)
    if date_col is None:
        raise PackagingError(
            f"Cannot derive {missing}: no date column found. "
            f"Available: {table.column_names}"
        )

    date_arr = table[date_col]
    if "Year" in missing:
        table = table.append_column("Year", pc.cast(pc.year(date_arr), pa.int16()))
    if "Month" in missing:
        table = table.append_column("Month", pc.cast(pc.month(date_arr), pa.int16()))
    return table


def _get_partition_values(dt, col: str) -> list:
    """Get sorted unique values for a partition column from file URIs.

    Extracts values from Delta file paths (e.g. Year=2021/...) to avoid
    reading any row data — important for 100M+ row tables.
    """
    import re
    from urllib.parse import unquote
    pattern = re.compile(rf"(?:^|/){re.escape(col)}=([^/]+)")
    values = set()
    for uri in dt.file_uris():
        m = pattern.search(unquote(uri))
        if m:
            try:
                values.add(int(m.group(1)))
            except ValueError:
                values.add(m.group(1))
    return sorted(values)


def _writer_props(compression: str | None):
    """Build WriterProperties if compression is specified."""
    if not compression:
        return None
    from deltalake import WriterProperties
    return WriterProperties(compression=compression)


def repartition_table(
    table_path: Path,
    target_cols: list[str],
    compression: str | None = None,
) -> dict:
    """Repartition a single Delta table by streaming per-partition.

    Skips unpartitioned tables (returns skipped=True).
    """
    from deltalake import DeltaTable, write_deltalake

    dt = DeltaTable(str(table_path))
    current_cols = _get_partition_cols(dt)

    if current_cols == target_cols:
        return {"skipped": True}

    files_before = len(dt.file_uris())
    t0 = time.time()
    total_rows = 0
    wp = _writer_props(compression)

    tmp_path = table_path.parent / f".{table_path.name}_repart_tmp"
    if tmp_path.exists():
        shutil.rmtree(tmp_path)

    stream_col = current_cols[0]
    partition_values = _get_partition_values(dt, stream_col)

    write_kwargs = {"writer_properties": wp}
    if target_cols:
        write_kwargs["partition_by"] = target_cols

    first = True
    for val in partition_values:
        chunk = dt.to_pyarrow_table(filters=[(stream_col, "=", val)])
        chunk = _ensure_partition_cols(chunk, target_cols, current_cols)
        total_rows += chunk.num_rows

        write_deltalake(
            str(tmp_path), chunk,
            mode="error" if first else "append",
            **write_kwargs,
        )
        first = False

    # Swap in the rewritten table without a window where the original is gone:
    # move the original aside, promote the temp, then delete the backup. If the
    # promote fails, restore the original so a crash can't destroy the table.
    backup_path = table_path.parent / f".{table_path.name}_repart_old"
    if backup_path.exists():
        shutil.rmtree(backup_path)
    table_path.rename(backup_path)
    try:
        tmp_path.rename(table_path)
    except OSError:
        backup_path.rename(table_path)
        raise
    shutil.rmtree(backup_path)

    dt_new = DeltaTable(str(table_path))
    files_after = len(dt_new.file_uris())
    new_cols = _get_partition_cols(dt_new)
    elapsed = time.time() - t0

    return {
        "rows": total_rows,
        "partition_before": current_cols,
        "partition_after": new_cols,
        "files_before": files_before,
        "files_after": files_after,
        "elapsed_s": elapsed,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Repartition Delta Lake tables in a generated dataset",
    )
    parser.add_argument("folder", help="Dataset folder to scan for Delta tables")
    parser.add_argument(
        "--partition-by", type=str, required=True,
        choices=list(_PARTITION_PRESETS),
        help="Target partition layout: 'none', 'year', or 'year-month'",
    )
    parser.add_argument(
        "--compression", type=str.upper, default=None, metavar="CODEC",
        choices=_VALID_COMPRESSIONS,
        help=f"Recompress during repartition (default: keep existing). "
             f"Valid: {', '.join(_VALID_COMPRESSIONS)}",
    )
    args = parser.parse_args()

    root = Path(args.folder)
    if not root.is_dir():
        print(f"Error: {root} is not a directory")
        return 1

    target_cols = _PARTITION_PRESETS[args.partition_by]

    from deltalake import DeltaTable

    all_tables = find_delta_tables(root)
    # Only process tables that are already partitioned
    tables = []
    for tp in all_tables:
        try:
            dt = DeltaTable(str(tp))
            if _get_partition_cols(dt):
                tables.append(tp)
        except Exception:
            pass

    if not tables:
        print(f"No partitioned Delta tables found in {root}")
        return 0

    target_label = ", ".join(target_cols) if target_cols else "none"
    print(f"Dataset: {root.name}")
    print(f"Tables:  {len(tables)}")
    print(f"Target:  [{target_label}]"
          f"{f'  compression={args.compression}' if args.compression else ''}")
    print()

    errors = 0
    skipped = 0
    t_start = time.time()

    for table_path in tables:
        rel = table_path.relative_to(root)
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        name = rel.parts[-1]

        try:
            stats = repartition_table(table_path, target_cols, compression=args.compression)
            if stats.get("skipped"):
                skipped += 1
            else:
                before = ", ".join(stats["partition_before"]) or "none"
                after = ", ".join(stats["partition_after"]) or "none"
                print(f"  {folder:12s} {name:30s} [{before}] -> [{after}]"
                      f"  {stats['files_before']:>4d} -> {stats['files_after']:<4d} files"
                      f"  {stats['rows']:>12,} rows"
                      f"  {stats['elapsed_s']:.1f}s")
        except Exception as e:
            print(f"  {folder:12s} {name:30s} Error: {e}")
            errors += 1

    elapsed = time.time() - t_start
    print()
    if skipped:
        print(f"Skipped {skipped} table(s) already at [{target_label}]")
    if errors:
        print(f"Failed: {errors} table(s)")
    print(f"Total: {len(tables) - skipped - errors} table(s) repartitioned in {elapsed:.1f}s")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
