from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional

from src.exceptions import SalesError


# -----------------------------
# Logical table names (future)
# -----------------------------
TABLE_SALES = "Sales"
TABLE_SALES_ORDER_DETAIL = "OrderDetail"
TABLE_SALES_ORDER_HEADER = "OrderHeader"
TABLE_SALES_RETURN = "Returns"

@dataclass(frozen=True)
class TableSpec:
    """
    Naming + folder spec for a logical output table.
    """
    # For csv/parquet chunk files
    out_subdir: str           # "" means root out_folder
    chunk_prefix: str         # e.g. "sales_chunk"
    merged_filename: str      # e.g. "sales.parquet"

    # For delta: where _tmp_parts lives (subdir under delta_output_folder)
    # IMPORTANT: for Sales we keep this empty to preserve existing output layout.
    delta_subdir: str         # "" means root delta_output_folder

def _norm(p: Optional[str]) -> Optional[str]:
    if p is None:
        return None
    p = str(p)
    return os.path.normpath(p)


def _is_abs(p: str) -> bool:
    return os.path.isabs(p)


def _to_snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


DEFAULT_TABLE_SPECS: Dict[str, TableSpec] = {
    # Backward-compatible: Sales stays exactly as-is (root folder naming)
    TABLE_SALES: TableSpec(
        out_subdir="",
        chunk_prefix="sales_chunk",
        merged_filename="sales.parquet",  # overridden by config if provided
        delta_subdir="",                  # keep old delta layout
    ),

    # Future tables (not used until you enable sales_output modes)
    TABLE_SALES_ORDER_DETAIL: TableSpec(
        out_subdir=_to_snake(TABLE_SALES_ORDER_DETAIL),
        chunk_prefix=f"{_to_snake(TABLE_SALES_ORDER_DETAIL)}_chunk",     # order_detail_chunk0001.csv
        merged_filename=f"{_to_snake(TABLE_SALES_ORDER_DETAIL)}.parquet", # order_detail.parquet
        delta_subdir=_to_snake(TABLE_SALES_ORDER_DETAIL),
    ),
    TABLE_SALES_ORDER_HEADER: TableSpec(
        out_subdir=_to_snake(TABLE_SALES_ORDER_HEADER),
        chunk_prefix=f"{_to_snake(TABLE_SALES_ORDER_HEADER)}_chunk",      # order_header_chunk0001.csv
        merged_filename=f"{_to_snake(TABLE_SALES_ORDER_HEADER)}.parquet",  # order_header.parquet
        delta_subdir=_to_snake(TABLE_SALES_ORDER_HEADER),
    ),
    TABLE_SALES_RETURN: TableSpec(
        out_subdir=_to_snake(TABLE_SALES_RETURN),
        chunk_prefix=f"{_to_snake(TABLE_SALES_RETURN)}_chunk",       # returns_chunk0001.csv
        merged_filename=f"{_to_snake(TABLE_SALES_RETURN)}.parquet",  # returns.parquet
        delta_subdir=_to_snake(TABLE_SALES_RETURN),
    ),

}

@dataclass(frozen=True)
class OutputPaths:
    """
    Canonical output path planner.

    - For Sales, paths are identical to current implementation:
        * chunks: <out_folder>/sales_chunk0001.parquet
        * delta parts: <delta_output_folder>/_tmp_parts/delta_part_0001.parquet
        * merged: <out_folder>/<merged_file>

    - For future tables, we intentionally place outputs under subfolders to avoid collisions.
    """
    file_format: str
    out_folder: str
    merged_file: Optional[str] = None
    delta_output_folder: Optional[str] = None

    # internal spec registry
    table_specs: Dict[str, TableSpec] = None  # type: ignore

    # constants
    delta_parts_dirname: str = "_tmp_parts"

    def __post_init__(self):
        if self.table_specs is None:
            object.__setattr__(self, "table_specs", dict(DEFAULT_TABLE_SPECS))

        ff = self.file_format
        if ff not in ("csv", "parquet", "deltaparquet"):
            raise SalesError(f"Unsupported file_format: {ff!r}")

        if not self.out_folder:
            raise SalesError("out_folder is required")

        if ff == "deltaparquet" and not self.delta_output_folder:
            raise SalesError("delta_output_folder is required when file_format='deltaparquet'")

    # -----------------------------
    # Table specs / validation
    # -----------------------------
    def spec(self, table: str) -> TableSpec:
        if table not in self.table_specs:
            raise SalesError(f"Unknown table: {table!r}")
        return self.table_specs[table]

    # -----------------------------
    # CSV / Parquet chunk outputs
    # -----------------------------
    def table_out_dir(self, table: str) -> str:
        spec = self.spec(table)
        if spec.out_subdir:
            return os.path.join(self.out_folder, spec.out_subdir)
        return self.out_folder

    def chunk_filename(self, table: str, idx: int, ext: str) -> str:
        spec = self.spec(table)
        ext = ext.lstrip(".")
        return f"{spec.chunk_prefix}{idx:04d}.{ext}"

    def chunk_path(self, table: str, idx: int, ext: str) -> str:
        return os.path.join(self.table_out_dir(table), self.chunk_filename(table, idx, ext))

    def chunk_glob(self, table: str, ext: str) -> str:
        """
        Glob pattern for chunk files for a table.
        Example: <out_dir>/sales_chunk*.parquet
        """
        spec = self.spec(table)
        ext = ext.lstrip(".")
        return os.path.join(self.table_out_dir(table), f"{spec.chunk_prefix}*.{ext}")

    def merged_path(self, table: str) -> str:
        """
        Where the final merged parquet should go.
        - Sales uses config merged_file (default: 'sales.parquet').
        - Other tables use their spec default filename.
        """
        spec = self.spec(table)
        merged = spec.merged_filename

        if table == TABLE_SALES and self.merged_file:
            merged = self.merged_file

        # Absolute merged file supported
        if _is_abs(merged):
            return merged

        # Backward-compat: Sales merged parquet sits in root out_folder
        if table == TABLE_SALES:
            return os.path.join(self.out_folder, merged)

        # For future tables, place merged parquet in that table’s folder
        return os.path.join(self.table_out_dir(table), merged)

    # -----------------------------
    # Delta tmp parts outputs
    # -----------------------------
    def delta_table_dir(self, table: str) -> str:
        if not self.delta_output_folder:
            raise SalesError("delta_output_folder is not set")
        spec = self.spec(table)

        # Backward-compat: Sales delta is rooted at delta_output_folder (no subdir)
        # If delta_subdir is empty, table lives directly under delta_output_folder
        if not spec.delta_subdir:
            return self.delta_output_folder

        return os.path.join(self.delta_output_folder, spec.delta_subdir)

    def delta_parts_dir(self, table: str) -> str:
        return os.path.join(self.delta_table_dir(table), self.delta_parts_dirname)

    def delta_part_filename(self, idx: int) -> str:
        return f"delta_part_{idx:04d}.parquet"

    def delta_part_path(self, table: str, idx: int) -> str:
        return os.path.join(self.delta_parts_dir(table), self.delta_part_filename(idx))

    # -----------------------------
    # Directory creation helper
    # -----------------------------
    def ensure_dirs(self, table: str) -> None:
        """
        Safe helper if you want to create dirs up front.
        (You can keep existing behavior in worker init; this is optional.)
        """
        os.makedirs(self.table_out_dir(table), exist_ok=True)

        if self.file_format == "deltaparquet":
            os.makedirs(self.delta_parts_dir(table), exist_ok=True)


def build_output_paths_from_sales_cfg(sales_cfg) -> OutputPaths:
    file_format = str(getattr(sales_cfg, "file_format", "parquet")).lower()
    out_folder = _norm(getattr(sales_cfg, "out_folder", "")) or ""
    merged_file = getattr(sales_cfg, "merged_file", None)
    delta_output_folder = _norm(getattr(sales_cfg, "delta_output_folder", None))

    # IMPORTANT: match sales.py defaulting behavior
    if file_format == "deltaparquet" and not delta_output_folder:
        delta_output_folder = os.path.normpath(os.path.join(out_folder, "delta"))

    return OutputPaths(
        file_format=file_format,
        out_folder=out_folder,
        merged_file=merged_file,
        delta_output_folder=delta_output_folder,
    )
