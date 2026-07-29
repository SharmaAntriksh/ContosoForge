"""
web/routes/data_routes.py -- Dataset listing and data preview endpoints.

Supports CSV, Parquet, and Delta Lake formats with paginated row preview.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import logging
import math
from numbers import Integral, Real
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from web.shared_state import REPO_ROOT

router = APIRouter(prefix="/api", tags=["data"])

_DATASETS_DIR = REPO_ROOT / "generated_datasets"

# ---------------------------------------------------------------------------
# CSV row count cache (avoids re-scanning files on every pagination click)
# Key: file path string → (mtime, row_count)
# ---------------------------------------------------------------------------

_csv_row_cache: Dict[str, tuple[float, int]] = {}
_csv_row_cache_lock = threading.Lock()
_CURRENCY_COLUMN_HINTS = (
    "amount",
    "price",
    "cost",
    "revenue",
    "profit",
    "discount",
    "tax",
    "income",
    "salary",
)


def _csv_row_count(path: Path) -> int:
    """Return the row count (excluding header) for a CSV file, cached by mtime."""
    key = str(path)
    mtime = path.stat().st_mtime
    with _csv_row_cache_lock:
        cached = _csv_row_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        count = max(0, sum(1 for _ in fh) - 1)  # subtract header; clamp empty files
    with _csv_row_cache_lock:
        _csv_row_cache[key] = (mtime, count)
    return count


def _preview_batch_size(limit: int) -> int:
    """Keep decoded Arrow batches small while avoiding tiny-batch overhead."""
    return max(256, min(4_096, limit * 4))


def _display_value(column: str, value: Any) -> Optional[str]:
    """Format a preview cell without mutating its source table."""
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bool, np.bool_)):
        return "true" if value else "false"
    if isinstance(value, Integral):
        # Strings preserve integers beyond JavaScript's safe numeric range.
        return str(int(value))
    if isinstance(value, Decimal):
        if any(hint in column.lower() for hint in _CURRENCY_COLUMN_HINTS):
            return f"{value:.2f}"
        return format(value, "f")
    if isinstance(value, Real):
        number = float(value)
        if not math.isfinite(number):
            return None
        if any(hint in column.lower() for hint in _CURRENCY_COLUMN_HINTS):
            return f"{number:.2f}"
        return format(number, ".12g")
    return str(value)


def _format_preview_rows(
    columns: List[str],
    rows: List[List[Any]],
) -> List[List[Optional[str]]]:
    return [
        [_display_value(column, value) for column, value in zip(columns, row)]
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Folder name parser
# ---------------------------------------------------------------------------

# Pattern: "2026-03-16 01_23_55 PM Customers 88K Sales 1M CSV"
_FOLDER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+"         # date
    r"(\d{2}_\d{2}_\d{2}\s+[AP]M)\s+"  # time
    r"(.+?)\s+"                         # description (e.g. "Customers 88K Sales 1M")
    r"(CSV|PARQUET|DELTAPARQUET)$",     # format tag
    re.IGNORECASE,
)


def _parse_folder_name(name: str) -> Dict[str, str]:
    """Parse a generated dataset folder name into date, time, description, format."""
    m = _FOLDER_RE.match(name)
    if m:
        raw_time = m.group(2).replace("_", ":")
        # Build a sortable ISO datetime string (24h format)
        try:
            from datetime import datetime
            dt = datetime.strptime(f"{m.group(1)} {raw_time}", "%Y-%m-%d %I:%M:%S %p")
            sort_key = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            sort_key = name
        return {
            "date": m.group(1),
            "time": raw_time,
            "description": m.group(3),
            "format_tag": m.group(4).upper(),
            "sort_key": sort_key,
        }
    return {"date": "", "time": "", "description": name, "format_tag": "", "sort_key": name}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_format(dataset_path: Path) -> str:
    """Detect the output format of a dataset folder."""
    dims = dataset_path / "dimensions"
    facts = dataset_path / "facts"
    # Check for delta logs first
    for sub in (dims, facts):
        if sub.is_dir():
            for child in sub.iterdir():
                if child.is_dir() and (child / "_delta_log").is_dir():
                    return "delta"
    # Check file extensions
    for sub in (dims, facts):
        if sub.is_dir():
            for f in sub.iterdir():
                if f.suffix == ".parquet":
                    return "parquet"
                if f.suffix == ".csv":
                    return "csv"
            # Check one level deeper (facts have subfolders)
            for child in sub.iterdir():
                if child.is_dir():
                    for f in child.iterdir():
                        if f.suffix == ".parquet":
                            return "parquet"
                        if f.suffix == ".csv":
                            return "csv"
    return "unknown"


def _discover_tables(dataset_path: Path, fmt: str) -> List[Dict[str, Any]]:
    """Discover all tables in a dataset folder."""
    tables = []

    for category in ("dimensions", "facts"):
        cat_path = dataset_path / category
        if not cat_path.is_dir():
            continue

        if fmt == "delta":
            # Delta tables are directories with _delta_log
            for child in sorted(cat_path.iterdir()):
                if child.is_dir() and (child / "_delta_log").is_dir():
                    tables.append({
                        "name": child.name,
                        "category": category,
                        "format": "delta",
                        "path": str(child.relative_to(dataset_path)),
                    })
        elif fmt == "parquet":
            # Parquet: single files directly in dims/facts
            for f in sorted(cat_path.iterdir()):
                if f.suffix == ".parquet":
                    tables.append({
                        "name": f.stem,
                        "category": category,
                        "format": "parquet",
                        "path": str(f.relative_to(dataset_path)),
                    })
        else:
            # CSV: files directly in dims, or in subfolders for facts
            for entry in sorted(cat_path.iterdir()):
                if entry.suffix == ".csv":
                    tables.append({
                        "name": entry.stem,
                        "category": category,
                        "format": "csv",
                        "path": str(entry.relative_to(dataset_path)),
                    })
                elif entry.is_dir():
                    csv_files = sorted(entry.glob("*.csv"))
                    if csv_files:
                        tables.append({
                            "name": entry.name,
                            "category": category,
                            "format": "csv",
                            "path": str(csv_files[0].relative_to(dataset_path)),
                        })

    return tables


def _read_preview(dataset_path: Path, table: Dict[str, Any],
                  offset: int, limit: int) -> Dict[str, Any]:
    """Read a slice of rows from a table."""
    fmt = table["format"]
    full_path = dataset_path / table["path"]

    if fmt == "csv":
        # For chunked CSVs, find all chunks in the parent dir
        parent = full_path.parent
        csv_files = sorted(parent.glob("*.csv")) if parent.name != "dimensions" else [full_path]
        if not csv_files:
            csv_files = [full_path]

        # Read just the columns/header from first file
        header_df = pd.read_csv(csv_files[0], nrows=0)
        columns = header_df.columns.tolist()

        # Count total rows across all chunks (cached by mtime)
        chunk_counts = [_csv_row_count(f) for f in csv_files]
        total_rows = sum(chunk_counts)

        # Read the requested slice
        rows_skipped = 0
        collected = []
        collected_rows = 0
        for f, chunk_rows in zip(csv_files, chunk_counts):

            if rows_skipped + chunk_rows <= offset:
                rows_skipped += chunk_rows
                continue

            skip_in_file = max(0, offset - rows_skipped)
            need = limit - collected_rows
            df = pd.read_csv(f, skiprows=range(1, skip_in_file + 1), nrows=need)
            collected.append(df)
            collected_rows += len(df)
            rows_skipped += chunk_rows

            if collected_rows >= limit:
                break

        if collected:
            result_df = pd.concat(collected, ignore_index=True).head(limit)
        else:
            result_df = pd.DataFrame(columns=columns)
        raw_rows = result_df.values.tolist()

    elif fmt == "parquet":
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(str(full_path))
        total_rows = pf.metadata.num_rows
        columns = pf.schema_arrow.names

        # Decode bounded record batches and slice before converting values.
        rows_seen = 0
        raw_rows = []
        batch_size = _preview_batch_size(limit)
        for rg_idx in range(pf.metadata.num_row_groups):
            rg_rows = pf.metadata.row_group(rg_idx).num_rows
            if rows_seen + rg_rows <= offset:
                rows_seen += rg_rows
                continue

            skip_in_rg = max(0, offset - rows_seen)
            for batch in pf.iter_batches(
                batch_size=batch_size,
                row_groups=[rg_idx],
                columns=columns,
            ):
                if skip_in_rg >= batch.num_rows:
                    skip_in_rg -= batch.num_rows
                    continue
                remaining = limit - len(raw_rows)
                sliced = batch.slice(skip_in_rg, remaining)
                raw_rows.extend(
                    [[record.get(column) for column in columns]
                     for record in sliced.to_pylist()]
                )
                skip_in_rg = 0
                if len(raw_rows) >= limit:
                    break
            rows_seen += rg_rows

            if len(raw_rows) >= limit:
                break

    elif fmt == "delta":
        from deltalake import DeltaTable
        dt = DeltaTable(str(full_path))
        ds = dt.to_pyarrow_dataset()
        columns = dt.schema().to_arrow().names
        fragments = [
            (fragment, fragment.count_rows())
            for fragment in ds.get_fragments()
        ]
        total_rows = sum(row_count for _fragment, row_count in fragments)

        # Skip whole Delta fragments, then decode only bounded record batches.
        batch_size = _preview_batch_size(limit)
        rows_seen = 0
        raw_rows = []
        for fragment, fragment_rows in fragments:
            if rows_seen + fragment_rows <= offset:
                rows_seen += fragment_rows
                continue

            skip_in_fragment = max(0, offset - rows_seen)
            for batch in fragment.to_batches(
                schema=ds.schema,
                columns=columns,
                batch_size=batch_size,
            ):
                if skip_in_fragment >= batch.num_rows:
                    skip_in_fragment -= batch.num_rows
                    continue
                remaining = limit - len(raw_rows)
                sliced = batch.slice(skip_in_fragment, remaining)
                raw_rows.extend(
                    [[record.get(column) for column in columns]
                     for record in sliced.to_pylist()]
                )
                skip_in_fragment = 0
                if len(raw_rows) >= limit:
                    break
            rows_seen += fragment_rows
            if len(raw_rows) >= limit:
                break

    else:
        raise HTTPException(400, f"Unsupported format: {fmt}")

    return {
        "columns": columns,
        "rows": _format_preview_rows(columns, raw_rows),
        "total_rows": total_rows,
        "offset": offset,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/datasets")
def list_datasets():
    """List all generated datasets with metadata."""
    if not _DATASETS_DIR.is_dir():
        return {"datasets": []}

    datasets = []
    for entry in _DATASETS_DIR.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue

        fmt = _detect_format(entry)
        tables = _discover_tables(entry, fmt)

        total_size = 0
        for root, _dirs, files in os.walk(entry):
            for f in files:
                total_size += os.path.getsize(os.path.join(root, f))

        parsed = _parse_folder_name(entry.name)
        datasets.append({
            "name": entry.name,
            "date": parsed["date"],
            "time": parsed["time"],
            "description": parsed["description"],
            "format": fmt,
            "table_count": len(tables),
            "size_mb": round(total_size / (1024 * 1024), 1),
            "has_config": (entry / "config" / "config.yaml").exists(),
            "_sort_key": parsed["sort_key"],
        })

    datasets.sort(key=lambda d: d["_sort_key"], reverse=True)
    for d in datasets:
        del d["_sort_key"]

    return {"datasets": datasets}


@router.get("/datasets/{folder}/tables")
def list_tables(folder: str):
    """List all tables in a specific dataset."""
    dataset_path = _DATASETS_DIR / folder
    if not dataset_path.is_dir():
        raise HTTPException(404, f"Dataset not found: {folder}")

    # Security: ensure the resolved path is within generated_datasets
    try:
        dataset_path.resolve().relative_to(_DATASETS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid dataset path")

    fmt = _detect_format(dataset_path)
    tables = _discover_tables(dataset_path, fmt)

    return {"folder": folder, "format": fmt, "tables": tables}


@router.get("/datasets/{folder}/tables/{table}/preview")
def preview_table(
    folder: str,
    table: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """Preview rows from a table with pagination."""
    dataset_path = _DATASETS_DIR / folder
    if not dataset_path.is_dir():
        raise HTTPException(404, f"Dataset not found: {folder}")

    try:
        dataset_path.resolve().relative_to(_DATASETS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid dataset path")

    fmt = _detect_format(dataset_path)
    tables = _discover_tables(dataset_path, fmt)

    # Find the requested table
    table_info = None
    for t in tables:
        if t["name"] == table:
            table_info = t
            break

    if table_info is None:
        raise HTTPException(404, f"Table not found: {table}")

    try:
        preview = _read_preview(dataset_path, table_info, offset, limit)
    except FileNotFoundError:
        raise HTTPException(404, f"Table file not found: {table}")
    except (OSError, ValueError, KeyError):
        logging.getLogger(__name__).exception("Failed to read table %s/%s", folder, table)
        raise HTTPException(500, "Failed to read table")

    return {
        "folder": folder,
        "table": table,
        "category": table_info["category"],
        "format": table_info["format"],
        **preview,
    }
