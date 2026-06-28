"""Optimize parquet files in a generated dataset folder.

Applies per-table type downcasting, row-group sorting, dictionary encoding,
and configurable compression to reduce file size and improve query performance.

Usage:
    python scripts/optimize_parquet.py <dataset_folder>
    python scripts/optimize_parquet.py <dataset_folder> --compression zstd --level 3
    python scripts/optimize_parquet.py <dataset_folder> --row-group-size 500000
    python scripts/optimize_parquet.py <dataset_folder> --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# ── Compression codecs that support a level parameter ──
_SUPPORTS_LEVEL = {"zstd", "gzip", "brotli", "lz4"}

# ── Per-table INT downcast rules ──
# Maps column name → smallest safe Arrow type.
# Applied across all tables; columns not present are silently skipped.
# NOTE: Do NOT downcast below int32 — Power Query reads int8/int16 from
# Parquet as Decimal instead of Integer.
_INT_DOWNCASTS: dict[str, pa.DataType] = {
    # Sales fact
    "OrderLineNumber": pa.int32(),
    "Quantity":             pa.int32(),
    "IsOrderDelayed":       pa.bool_(),
    "CurrencyKey":          pa.int32(),
    "PromotionKey":         pa.int32(),
    "ReturnQuantity":       pa.int32(),
    "ChannelKey":           pa.int32(),
    "TimeKey":              pa.int32(),
    "StoreKey":             pa.int32(),
    "ProductKey":           pa.int32(),
    "ReturnReasonKey":      pa.int32(),
    # Inventory
    "ReorderFlag":          pa.bool_(),
    "StockoutFlag":         pa.bool_(),
    "DaysOutOfStock":       pa.int32(),
    "QuantityOnHand":       pa.int32(),
    "QuantityOnOrder":      pa.int32(),
    "QuantitySold":         pa.int32(),
    "QuantityReceived":     pa.int32(),
    # Customers
    "HouseholdKey":         pa.int32(),
    "GeographyKey":         pa.int32(),
    "LoyaltyTierKey":       pa.int32(),
    "CustomerAcquisitionChannelKey": pa.int32(),
    # Subscriptions
    "PlanKey":              pa.int32(),
    "BillingCycleNumber":   pa.int32(),
    "IsFirstPeriod":        pa.bool_(),
    "IsChurnPeriod":        pa.bool_(),
    "IsTrialPeriod":        pa.bool_(),
    # Budget
    "BudgetYear":           pa.int32(),
    # Wishlists
    "ResponseDays":         pa.int32(),
    # Products
    "SubcategoryKey":       pa.int32(),
    "VariantIndex":         pa.int32(),
    "VersionNumber":        pa.int32(),
    "IsCurrent":            pa.bool_(),
    # Employees
    "StoreKey":             pa.int32(),
}

# ── Columns to convert from float64 → float32 ──
_FLOAT32_COLS = {
    "NetPrice", "UnitCost", "ListPrice", "DiscountAmount",
    "ReturnNetPrice", "PeriodPrice",
    "BudgetAmount", "BudgetQuantity",
    "BudgetGrowthPct",
    "CustomerLifetimeValue", "DistanceToNearestStoreKm", "CreditScore",
    "Latitude", "Longitude", "YearlyIncome",
}

# ── Per-table sort orders (improves compression via value clustering) ──
# Tables not listed here get no sort.
_SORT_ORDERS: dict[str, list[str]] = {
    "sales": [
        "IsOrderDelayed", "DeliveryStatus", "OrderLineNumber",
        "DiscountAmount", "CurrencyKey", "Quantity", "ChannelKey",
        "PromotionKey", "StoreKey", "TimeKey",
    ],
    "returns": [
        "ReturnReasonKey", "ReturnQuantity", "OrderNumber",
    ],
    "inventory_snapshot": [
        "StockoutFlag", "ReorderFlag", "StoreKey", "ProductKey", "SnapshotDate",
    ],
    "customer_subscriptions": [
        "IsChurnPeriod", "IsTrialPeriod", "PlanKey", "CustomerKey",
    ],
    "customers": [
        "CustomerType", "Gender", "GeographyKey", "LoyaltyTierKey",
    ],
    "products": [
        "IsCurrent", "SubcategoryKey", "Brand", "Class",
    ],
    "exchange_rates": [
        "FromCurrency", "ToCurrency", "Date",
    ],
    "budget_monthly": [
        "Scenario", "Country", "Category", "BudgetMonthStart",
    ],
}


def _table_key(path: Path) -> str:
    """Derive a table name from a parquet file path (e.g. 'sales', 'customers')."""
    return path.stem.lower()


def _apply_downcasts(table: pa.Table) -> pa.Table:
    """Downcast integer columns to smallest safe type."""
    for col_name, target_type in _INT_DOWNCASTS.items():
        if col_name not in table.schema.names:
            continue
        if table.schema.field(col_name).type == target_type:
            continue
        try:
            idx = table.schema.get_field_index(col_name)
            table = table.set_column(
                idx, pa.field(col_name, target_type),
                table.column(col_name).cast(target_type, safe=True),
            )
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
            pass  # values don't fit — skip silently
    return table


def _apply_float32(table: pa.Table) -> pa.Table:
    """Convert float64 columns to float32 where configured."""
    for col_name in _FLOAT32_COLS:
        if col_name not in table.schema.names:
            continue
        if table.schema.field(col_name).type != pa.float64():
            continue
        idx = table.schema.get_field_index(col_name)
        table = table.set_column(
            idx, pa.field(col_name, pa.float32()),
            table.column(col_name).cast(pa.float32()),
        )
    return table


def _apply_sort(table: pa.Table, sort_cols: list[str]) -> pa.Table:
    """Sort table by the given columns (only those present in schema)."""
    valid = [c for c in sort_cols if c in table.schema.names]
    if not valid:
        return table
    # Decode any dictionary-encoded sort columns (Arrow can't sort dict types)
    for col_name in valid:
        if pa.types.is_dictionary(table.schema.field(col_name).type):
            idx = table.schema.get_field_index(col_name)
            decoded = table.column(col_name).cast(pa.string())
            table = table.set_column(idx, pa.field(col_name, pa.string()), decoded)
    return table.sort_by([(c, "ascending") for c in valid])


def _dict_encode_strings(table: pa.Table) -> pa.Table:
    """Dictionary-encode all string columns."""
    for i, field in enumerate(table.schema):
        if field.type in (pa.string(), pa.large_string(), pa.utf8(), pa.large_utf8()):
            encoded = table.column(i).dictionary_encode()
            table = table.set_column(i, pa.field(field.name, encoded.type), encoded)
    return table


def optimize_file(
    input_path: Path,
    output_path: Path,
    *,
    compression: str = "zstd",
    compression_level: int | None = None,
    row_group_size: int = 1_000_000,
    label: str = "",
) -> tuple[int, int, float]:
    """Optimize a single parquet file. Returns (original_bytes, new_bytes, elapsed_s)."""
    t_file_start = time.perf_counter()
    original_size = input_path.stat().st_size
    pf = pq.ParquetFile(str(input_path))
    total_rows = pf.metadata.num_rows
    table_key = _table_key(input_path)
    sort_cols = _SORT_ORDERS.get(table_key, [])

    write_args: dict = dict(
        compression=compression,
        use_dictionary=True,
        data_page_size=1024 * 1024,
        write_statistics=True,
    )
    if compression_level is not None and compression in _SUPPORTS_LEVEL:
        write_args["compression_level"] = compression_level

    writer = None
    rows_written = 0
    buffer = None

    for i in range(pf.metadata.num_row_groups):
        chunk = pf.read_row_group(i)
        chunk = _apply_downcasts(chunk)
        chunk = _apply_float32(chunk)

        if buffer is not None:
            chunk = pa.concat_tables([buffer, chunk])
            buffer = None

        while chunk.num_rows >= row_group_size:
            batch = chunk.slice(0, row_group_size)
            chunk = chunk.slice(row_group_size)

            batch = _apply_sort(batch, sort_cols)
            batch = _dict_encode_strings(batch)

            if writer is None:
                writer = pq.ParquetWriter(str(output_path), batch.schema, **write_args)
            writer.write_table(batch)
            rows_written += batch.num_rows
            if total_rows > row_group_size:
                pct_done = rows_written * 100 // total_rows
                elapsed = time.perf_counter() - t_file_start
                elapsed = time.perf_counter() - t_file_start
                print(f"\r    {rows_written:,}/{total_rows:,} rows ({pct_done}%) {elapsed:.0f}s", end="", flush=True)

        if chunk.num_rows > 0:
            buffer = chunk

    # Flush remaining rows
    if buffer is not None and buffer.num_rows > 0:
        buffer = _apply_sort(buffer, sort_cols)
        buffer = _dict_encode_strings(buffer)
        if writer is None:
            writer = pq.ParquetWriter(str(output_path), buffer.schema, **write_args)
        writer.write_table(buffer)
        rows_written += buffer.num_rows

    if writer is not None:
        writer.close()

    new_size = output_path.stat().st_size if output_path.exists() else 0
    elapsed = time.perf_counter() - t_file_start
    return original_size, new_size, elapsed


def optimize_dataset(
    dataset_dir: Path,
    *,
    compression: str = "zstd",
    compression_level: int | None = None,
    row_group_size: int = 1_000_000,
    dry_run: bool = False,
    in_place: bool = False,
):
    """Optimize all parquet files in a dataset folder."""
    parquet_files = sorted(
        p for p in dataset_dir.rglob("*.parquet")
        if "optimized" not in p.relative_to(dataset_dir).parts
    )
    if not parquet_files:
        print(f"No parquet files found in {dataset_dir}")
        return

    print(f"Dataset: {dataset_dir.name}")
    print(f"Files:   {len(parquet_files)}")
    print(f"Options: compression={compression}"
          f"{f' level={compression_level}' if compression_level else ''}"
          f" row_group_size={row_group_size:,}")
    print()

    if dry_run:
        for p in parquet_files:
            meta = pq.read_metadata(str(p))
            rel = p.relative_to(dataset_dir)
            size_mb = p.stat().st_size / 1e6
            print(f"  {rel}  ({meta.num_rows:,} rows, {size_mb:.1f} MB)")
        print("\n(dry run — no files modified)")
        return

    total_original = 0
    total_new = 0
    t_start = time.perf_counter()

    _MIN_SIZE = 2 * 1024 * 1024  # skip files under 2 MB (not worth the effort)
    skipped = 0
    no_savings = []

    for p in parquet_files:
        rel = p.relative_to(dataset_dir)
        file_size = p.stat().st_size
        if file_size < _MIN_SIZE:
            skipped += 1
            continue
        meta = pq.read_metadata(str(p))

        file_label = p.stem

        if in_place:
            tmp = p.with_suffix(".parquet.tmp")
            orig, new, file_elapsed = optimize_file(
                p, tmp,
                compression=compression,
                compression_level=compression_level,
                row_group_size=row_group_size,
                label=file_label,
            )
            if new < orig:
                tmp.replace(p)
            else:
                tmp.unlink(missing_ok=True)
                new = orig
        else:
            optimized_dir = dataset_dir / "optimized"
            out_path = optimized_dir / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            orig, new, file_elapsed = optimize_file(
                p, out_path,
                compression=compression,
                compression_level=compression_level,
                row_group_size=row_group_size,
                label=file_label,
            )
            if new >= orig:
                out_path.unlink(missing_ok=True)
                new = orig

        # Clear progress line if file was large enough to show progress
        if meta.num_rows > row_group_size:
            print("\r" + " " * 100 + "\r", end="", flush=True)

        total_original += orig
        total_new += new
        pct = (1 - new / orig) * 100 if orig > 0 else 0
        time_str = f"{file_elapsed:.1f}s" if file_elapsed >= 1.0 else ""

        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        fname = rel.name

        if pct <= 0:
            no_savings.append(str(rel))
        else:
            print(f"  {folder:12s} {fname:42s} {orig/1e6:7.1f} MB -> {new/1e6:7.1f} MB ({pct:4.1f}% smaller) {meta.num_rows:>12,} rows  {time_str}")

    elapsed = time.perf_counter() - t_start
    pct_total = (1 - total_new / total_original) * 100 if total_original > 0 else 0
    if no_savings:
        print(f"\nSkipped {len(no_savings)} file(s) with no savings: {', '.join(no_savings)}")
    if skipped:
        print(f"Skipped {skipped} file(s) under {_MIN_SIZE // (1024 * 1024)} MB")
    print(f"Total: {total_original/1e6:.1f} MB -> {total_new/1e6:.1f} MB ({pct_total:.1f}% smaller) in {elapsed:.1f}s")

    if not in_place:
        print(f"Optimized files written to: {dataset_dir / 'optimized'}")


def main():
    parser = argparse.ArgumentParser(
        prog="optimize_parquet",
        description="Optimize parquet files in a generated dataset folder",
    )
    parser.add_argument(
        "dataset_dir",
        type=Path,
        help="Path to the dataset folder (e.g. generated_datasets/2026-03-22...PARQUET)",
    )
    parser.add_argument(
        "--compression", "-c",
        default="zstd",
        choices=["snappy", "zstd", "gzip", "brotli", "lz4", "none"],
        help="Compression codec (default: zstd)",
    )
    parser.add_argument(
        "--level", "-l",
        type=int,
        default=None,
        help="Compression level (codec-dependent, e.g. zstd 1-22)",
    )
    parser.add_argument(
        "--row-group-size", "-r",
        type=int,
        default=1_000_000,
        help="Target rows per row group (default: 1,000,000)",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite original files instead of writing to optimized/ subfolder",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files without optimizing",
    )
    args = parser.parse_args()

    if not args.dataset_dir.is_dir():
        print(f"Error: {args.dataset_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    optimize_dataset(
        args.dataset_dir,
        compression=args.compression,
        compression_level=args.level,
        row_group_size=args.row_group_size,
        dry_run=args.dry_run,
        in_place=args.in_place,
    )


if __name__ == "__main__":
    main()
