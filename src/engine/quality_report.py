"""Post-generation data quality report.

Reads the final packaged output folder, checks referential integrity,
distribution sanity, and null/duplicate checks, then writes
an HTML report alongside the data.
"""
from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.utils.logging_utils import info, stage


# ============================================================================
# Check result model
# ============================================================================

@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    details: str = ""
    category: str = "General"


@dataclass
class DistributionSummary:
    column: str
    table: str
    min_val: Any = None
    max_val: Any = None
    mean_val: Any = None
    median_val: Any = None
    std_val: Any = None
    top_values: List[tuple] = field(default_factory=list)


@dataclass
class QualityReport:
    checks: List[CheckResult] = field(default_factory=list)
    distributions: List[DistributionSummary] = field(default_factory=list)
    table_row_counts: Dict[str, int] = field(default_factory=dict)
    elapsed_sec: float = 0.0
    folder_name: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def total(self) -> int:
        return len(self.checks)


# ============================================================================
# File loaders (format-aware) with caching
# ============================================================================

class _TableCache:
    """Caches loaded DataFrames by (path, columns_key) to avoid redundant I/O.

    When columns=None (full read), the result is stored and subsequent
    column-specific requests are sliced from the cached full frame.
    """

    def __init__(self) -> None:
        self._full: Dict[str, pd.DataFrame] = {}      # path -> full DataFrame
        self._partial: Dict[Tuple, pd.DataFrame] = {}  # (path, col_tuple) -> subset

    def get(self, path: Path, columns: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
        key = str(path)

        # Already have the full table — slice if needed
        if key in self._full:
            df = self._full[key]
            if columns is None:
                return df
            available = [c for c in columns if c in df.columns]
            return df[available]

        # Check partial cache
        if columns is not None:
            col_key = (key, tuple(sorted(columns)))
            if col_key in self._partial:
                return self._partial[col_key]

        # Load from disk
        df = _read_table_uncached(path, columns=columns)
        if df is None:
            return None

        if columns is None:
            self._full[key] = df
        else:
            col_key = (key, tuple(sorted(columns)))
            self._partial[col_key] = df

        return df

    def put_full(self, path: Path, df: pd.DataFrame) -> None:
        """Store a full table (used by row-count collection that reads everything)."""
        self._full[str(path)] = df


def _read_table_uncached(path: Path, columns: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
    """Read a table from parquet, CSV dir, or delta — returns None if missing."""
    if path.is_file() and path.suffix == ".parquet":
        try:
            return pd.read_parquet(path, columns=columns)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # CSV: could be a single file or a directory of chunks
    if path.is_file() and path.suffix == ".csv":
        try:
            return pd.read_csv(path, usecols=columns)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    if path.is_dir():
        # Delta table?
        if (path / "_delta_log").is_dir():
            try:
                from deltalake import DeltaTable
                dt = DeltaTable(str(path))
                df = dt.to_pandas()
                if columns:
                    available = [c for c in columns if c in df.columns]
                    df = df[available]
                return df
            except (OSError, ValueError, KeyError, TypeError):
                return None

        # CSV chunk directory
        csvs = sorted(path.glob("*.csv"))
        if csvs:
            try:
                frames = [pd.read_csv(f, usecols=columns) for f in csvs]
                return pd.concat(frames, ignore_index=True)
            except (OSError, ValueError, KeyError, TypeError):
                return None

        # Parquet files in directory
        pqs = sorted(path.glob("*.parquet"))
        if pqs:
            try:
                frames = [pd.read_parquet(f, columns=columns) for f in pqs]
                return pd.concat(frames, ignore_index=True)
            except (OSError, ValueError, KeyError, TypeError):
                return None

    return None


def _row_count_fast(path: Path) -> Optional[int]:
    """Get row count without reading data where possible (parquet metadata)."""
    if path.is_file() and path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
            meta = pq.read_metadata(str(path))
            return meta.num_rows
        except (OSError, ValueError, KeyError, TypeError):
            pass

    if path.is_dir():
        # Delta table
        if (path / "_delta_log").is_dir():
            try:
                from deltalake import DeltaTable
                dt = DeltaTable(str(path))
                # read just one column to count rows cheaply
                files = dt.files()
                if files:
                    import pyarrow.parquet as pq
                    total = 0
                    for f in files:
                        fp = path / f
                        if fp.exists():
                            total += pq.read_metadata(str(fp)).num_rows
                    return total
            except (OSError, ValueError, KeyError, TypeError):
                pass

        # Directory of parquet files — sum metadata
        pqs = sorted(path.glob("*.parquet"))
        if pqs:
            try:
                import pyarrow.parquet as pq
                return sum(pq.read_metadata(str(f)).num_rows for f in pqs)
            except (OSError, ValueError, KeyError, TypeError):
                pass

        # CSV — count lines (cheaper than full parse)
        csvs = sorted(path.glob("*.csv"))
        if csvs:
            try:
                total = 0
                for f in csvs:
                    # Count newlines via raw binary read (faster than line iteration)
                    with open(f, "rb") as fh:
                        total += sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 20), b"")) - 1
                return max(total, 0)
            except (OSError, ValueError, KeyError, TypeError):
                pass

    if path.is_file() and path.suffix == ".csv":
        try:
            with open(path, "rb") as fh:
                return max(sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 20), b"")) - 1, 0)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    return None


def _find_table(folder: Path, name: str) -> Optional[Path]:
    """Find a table by name in dims or facts folder."""
    candidates = [
        folder / f"{name}.parquet",
        folder / f"{name}.csv",
        folder / name,  # directory (delta or csv chunks)
    ]
    snake = _to_snake(name)
    if snake != name:
        candidates.extend([
            folder / f"{snake}.parquet",
            folder / f"{snake}.csv",
            folder / snake,
        ])

    for c in candidates:
        if c.exists():
            return c
    return None


def _to_snake(name: str) -> str:
    """PascalCase to snake_case."""
    import re
    s = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
    return s


# ============================================================================
# Check runners
# ============================================================================

def _emit_fk_check(
    report: QualityReport,
    check_name: str,
    src_keys: np.ndarray,
    target_keys: np.ndarray,
    src_name: str,
    fk_col: str,
    target_label: str,
) -> None:
    """Append an FK result: every key in *src_keys* must exist in *target_keys*.

    Shared by the fact→dimension and fact→fact (returns/complaints → header)
    checks so both report orphans identically. numpy ``isin`` is much faster
    than Python set subtraction for large key arrays.
    """
    in_target = np.isin(src_keys, target_keys)
    n_orphans = int((~in_target).sum())
    if n_orphans > 0:
        sample = sorted(src_keys[~in_target][:10].tolist())
        report.checks.append(CheckResult(
            name=check_name,
            passed=False,
            message=f"{n_orphans} orphan key(s) in {src_name}.{fk_col}",
            details=f"Sample orphans: {sample}",
            category="Referential Integrity",
        ))
    else:
        report.checks.append(CheckResult(
            name=check_name,
            passed=True,
            message=f"All {len(src_keys)} keys found in {target_label}",
            category="Referential Integrity",
        ))


def _check_referential_integrity(
    report: QualityReport,
    dims_folder: Path,
    facts_folder: Path,
    cache: _TableCache,
) -> None:
    """Check that all FK values in fact tables exist in dimension tables."""

    # FK relationships: (fact_table, fact_folder, fk_column, dim_table, pk_column)
    fk_checks = [
        # Sales fact → dimensions
        ("sales", "fact", "CustomerKey", "customers", "CustomerKey"),
        ("sales", "fact", "ProductKey", "products", "ProductKey"),
        ("sales", "fact", "StoreKey", "stores", "StoreKey"),
        ("sales", "fact", "PromotionKey", "promotions", "PromotionKey"),
        ("sales", "fact", "CurrencyKey", "currency", "CurrencyKey"),
        ("sales", "fact", "EmployeeKey", "employees", "EmployeeKey"),
        ("sales", "fact", "SalesChannelKey", "sales_channels", "SalesChannelKey"),
        ("sales", "fact", "TimeKey", "time", "TimeKey"),
        # Sales return → dimensions
        ("sales_return", "fact", "ReturnReasonKey", "return_reason", "ReturnReasonKey"),
        # Inventory → dimensions
        ("inventory_snapshot", "fact", "ProductKey", "products", "ProductKey"),
        ("inventory_snapshot", "fact", "StoreKey", "stores", "StoreKey"),
        # Dimension internal FKs
        ("customers", "dim", "GeographyKey", "geography", "GeographyKey"),
        ("stores", "dim", "GeographyKey", "geography", "GeographyKey"),
    ]

    folder_map = {"fact": facts_folder, "dim": dims_folder}

    # Pre-group FK columns needed per (table, folder_type) so we read once
    source_cols_needed: Dict[tuple, List[str]] = {}
    for src_name, src_type, fk_col, _dim_name, _pk_col in fk_checks:
        key = (src_name, src_type)
        source_cols_needed.setdefault(key, [])
        if fk_col not in source_cols_needed[key]:
            source_cols_needed[key].append(fk_col)

    # --- Fast path: use PyArrow to extract unique FK values directly ---
    # For large tables (10M+ sales, 21M+ inventory), reading via PyArrow
    # and computing unique values in Arrow/numpy is 5-10x faster than
    # loading into pandas, calling .dropna().unique(), and converting to set.
    _use_pyarrow = True
    try:
        import pyarrow.parquet as _pq_ri
        import pyarrow.compute as _pc_ri
    except ImportError:
        _use_pyarrow = False

    def _get_unique_keys_fast(path: Path, column: str) -> Optional[np.ndarray]:
        """Extract unique non-null values from a single column.

        Fast path: for parquet files/dirs, uses PyArrow to read only the
        needed column without full pandas conversion.
        Fallback: for CSV and other formats, uses the shared cache (which
        reads all chunks once and concatenates efficiently).
        """
        if _use_pyarrow and path.is_file() and path.suffix == ".parquet":
            try:
                table = _pq_ri.read_table(str(path), columns=[column])
                arr = table.column(column)
                arr = _pc_ri.drop_null(arr)
                return _pc_ri.unique(arr).to_numpy(zero_copy_only=False)
            except (OSError, ValueError, KeyError, TypeError):
                pass

        if _use_pyarrow and path.is_dir():
            pqs = sorted(path.glob("*.parquet"))
            if pqs:
                try:
                    import pyarrow as _pa_ri
                    arrays = []
                    for p in pqs:
                        t = _pq_ri.read_table(str(p), columns=[column])
                        arrays.append(t.column(column))
                    combined = _pa_ri.concat_arrays([a.combine_chunks() for a in arrays])
                    combined = _pc_ri.drop_null(combined)
                    return _pc_ri.unique(combined).to_numpy(zero_copy_only=False)
                except (OSError, ValueError, KeyError, TypeError):
                    pass

        # Fallback: pandas via cache (handles CSV dirs, delta, single files)
        df = cache.get(path, columns=[column])
        if df is not None and column in df.columns:
            arr = df[column].to_numpy()
            # Use numpy for fast unique (avoids pandas overhead)
            valid = ~pd.isna(arr)
            if valid.all():
                return np.unique(arr)
            return np.unique(arr[valid])
        return None

    # Pre-load source fact tables into cache with ALL needed FK columns at once.
    # This ensures CSV directories are read only once (not per-FK-column).
    # We also include date columns needed by subsequent date-range checks
    # so the single read covers both RI and date checks.
    _extra_cols_by_table = {
        "sales": ["OrderDate", "DueDate", "DeliveryDate",
                   "Quantity", "NetPrice", "DiscountAmount"],
        "inventory_snapshot": ["SnapshotDate"],
    }
    for (src_name, src_type), fk_cols in source_cols_needed.items():
        src_path = _find_table(folder_map[src_type], src_name)
        if src_path is not None:
            # Combine FK cols + date cols for a single read
            all_cols = list(fk_cols) + _extra_cols_by_table.get(src_name, [])
            _preloaded = cache.get(src_path, columns=all_cols)
            if _preloaded is not None:
                # Store as full so subsequent column-specific requests slice from it
                cache.put_full(src_path, _preloaded)

    # Cache dimension PK unique values (small, reused across multiple FK checks)
    _dim_key_cache: Dict[str, np.ndarray] = {}

    for src_name, src_type, fk_col, dim_name, pk_col in fk_checks:
        src_path = _find_table(folder_map[src_type], src_name)
        if src_path is None:
            continue

        dim_path = _find_table(dims_folder, dim_name)
        if dim_path is None:
            report.checks.append(CheckResult(
                name=f"FK: {src_name}.{fk_col} → {dim_name}.{pk_col}",
                passed=False,
                message=f"Dimension table '{dim_name}' not found",
                category="Referential Integrity",
            ))
            continue

        # Get unique FK values from source (fast path)
        src_keys = _get_unique_keys_fast(src_path, fk_col)
        if src_keys is None or len(src_keys) == 0:
            continue

        # Get unique PK values from dimension (cached)
        _dim_cache_key = f"{dim_name}.{pk_col}"
        if _dim_cache_key in _dim_key_cache:
            dim_keys = _dim_key_cache[_dim_cache_key]
        else:
            dim_keys = _get_unique_keys_fast(dim_path, pk_col)
            if dim_keys is not None:
                _dim_key_cache[_dim_cache_key] = dim_keys

        if dim_keys is None or len(dim_keys) == 0:
            continue

        _emit_fk_check(
            report,
            f"FK: {src_name}.{fk_col} → {dim_name}.{pk_col}",
            src_keys, dim_keys, src_name, fk_col, dim_name,
        )

    # --- Fact-to-fact links: returns / complaints → SalesOrderHeader ---
    # SalesOrderNumber is the order PK in the header; returns and complaints
    # reference it. CHUNK-1's cross-chunk duplicate order numbers would also
    # surface here as orphan references when the join no longer resolves.
    header_path = _find_table(facts_folder, "sales_order_header")
    if header_path is not None:
        header_keys = _get_unique_keys_fast(header_path, "SalesOrderNumber")
        if header_keys is not None and len(header_keys) > 0:
            # (fact_table, fk_column) — SalesOrderNumber nulls are dropped by the
            # unique extractor, so unlinked complaints are correctly ignored.
            fact_link_checks = [
                ("sales_return", "SalesOrderNumber"),
                ("complaints", "SalesOrderNumber"),
            ]
            for src_name, fk_col in fact_link_checks:
                src_path = _find_table(facts_folder, src_name)
                if src_path is None:
                    continue
                src_keys = _get_unique_keys_fast(src_path, fk_col)
                if src_keys is None or len(src_keys) == 0:
                    continue
                _emit_fk_check(
                    report,
                    f"FK: {src_name}.{fk_col} → sales_order_header.SalesOrderNumber",
                    src_keys, header_keys, src_name, fk_col, "sales_order_header",
                )

    # --- Date-range checks: fact dates must fall within dates dimension ---
    dates_path = _find_table(dims_folder, "dates")
    if dates_path is not None:
        dates_df = cache.get(dates_path, columns=["Date"])
        if dates_df is not None and "Date" in dates_df.columns:
            dim_dates = pd.to_datetime(dates_df["Date"])
            date_min, date_max = dim_dates.min(), dim_dates.max()

            # (fact_table, folder_type, date_column)
            date_checks = [
                ("sales", "fact", "OrderDate"),
                ("sales", "fact", "DueDate"),
                ("sales", "fact", "DeliveryDate"),
                ("inventory_snapshot", "fact", "SnapshotDate"),
            ]

            def _date_min_max_fast(path: Path, col: str):
                """Get min/max of a date column using parquet statistics (zero rows read)."""
                if _use_pyarrow and path.is_file() and path.suffix == ".parquet":
                    try:
                        meta = _pq_ri.read_metadata(str(path))
                        col_idx = None
                        schema = _pq_ri.read_schema(str(path))
                        for i, name in enumerate(schema.names):
                            if name == col:
                                col_idx = i
                                break
                        if col_idx is not None:
                            _dmin, _dmax = None, None
                            for rg in range(meta.num_row_groups):
                                stats = meta.row_group(rg).column(col_idx).statistics
                                if stats is not None and stats.has_min_max:
                                    rg_min, rg_max = stats.min, stats.max
                                    if _dmin is None or rg_min < _dmin:
                                        _dmin = rg_min
                                    if _dmax is None or rg_max > _dmax:
                                        _dmax = rg_max
                            if _dmin is not None:
                                return pd.Timestamp(_dmin), pd.Timestamp(_dmax)
                    except (OSError, ValueError, KeyError, TypeError):
                        pass

                if _use_pyarrow and path.is_dir():
                    pqs = sorted(path.glob("*.parquet"))
                    if pqs:
                        _dmin, _dmax = None, None
                        for p in pqs:
                            result = _date_min_max_fast(p, col)
                            if result:
                                if _dmin is None or result[0] < _dmin:
                                    _dmin = result[0]
                                if _dmax is None or result[1] > _dmax:
                                    _dmax = result[1]
                        if _dmin is not None:
                            return _dmin, _dmax

                # Fallback: read the column
                df = cache.get(path, columns=[col])
                if df is not None and col in df.columns:
                    col_dates = pd.to_datetime(df[col])
                    return col_dates.min(), col_dates.max()
                return None

            for tbl, ftype, col in date_checks:
                tbl_path = _find_table(folder_map[ftype], tbl)
                if tbl_path is None:
                    continue
                result = _date_min_max_fast(tbl_path, col)
                if result is None:
                    continue
                col_min, col_max = result
                in_range = col_min >= date_min and col_max <= date_max
                msg = f"{col_min.date()} to {col_max.date()}"
                if not in_range:
                    msg += f" (dates dim: {date_min.date()} to {date_max.date()})"
                report.checks.append(CheckResult(
                    name=f"Date range: {tbl}.{col} ⊆ dates",
                    passed=in_range,
                    message=msg,
                    category="Referential Integrity",
                ))


def _check_nulls_and_duplicates(
    report: QualityReport,
    dims_folder: Path,
    facts_folder: Path,
    cache: _TableCache,
) -> None:
    """Check for null PKs and duplicate PKs in key tables."""

    pk_checks = [
        ("customers", "CustomerKey", dims_folder),
        ("products", "ProductKey", dims_folder),
        ("stores", "StoreKey", dims_folder),
        ("promotions", "PromotionKey", dims_folder),
        ("employees", "EmployeeKey", dims_folder),
        ("dates", "DateKey", dims_folder),
        ("geography", "GeographyKey", dims_folder),
        ("currency", "CurrencyKey", dims_folder),
        # Fact PK: SalesOrderNumber is the true PK of the order header (when emitted).
        # Cross-chunk duplicate order numbers (CHUNK-1) surface here as duplicate PKs.
        ("sales_order_header", "SalesOrderNumber", facts_folder),
    ]

    for table_name, pk_col, folder in pk_checks:
        path = _find_table(folder, table_name)
        if path is None:
            continue

        df = cache.get(path, columns=[pk_col])
        if df is None or pk_col not in df.columns:
            continue

        # Null check
        null_count = int(df[pk_col].isna().sum())
        report.checks.append(CheckResult(
            name=f"Null PK: {table_name}.{pk_col}",
            passed=null_count == 0,
            message=f"{null_count} null(s)" if null_count > 0 else "No nulls",
            category="Null / Duplicate",
        ))

        # Duplicate check
        dup_count = int(df[pk_col].duplicated().sum())
        report.checks.append(CheckResult(
            name=f"Duplicate PK: {table_name}.{pk_col}",
            passed=dup_count == 0,
            message=f"{dup_count} duplicate(s)" if dup_count > 0 else "No duplicates",
            category="Null / Duplicate",
        ))


def _check_distributions(
    report: QualityReport,
    dims_folder: Path,
    facts_folder: Path,
    cache: _TableCache,
    cfg: Optional[Dict[str, Any]] = None,
) -> None:
    """Check that key distributions look reasonable."""

    # Sales: load all needed columns in one read
    sales_path = _find_table(facts_folder, "sales")
    if sales_path is not None:
        sales_df = cache.get(sales_path, columns=["Quantity", "NetPrice", "OrderDate", "DiscountAmount"])
        if sales_df is not None:
            if "Quantity" in sales_df.columns:
                qty = sales_df["Quantity"]
                report.distributions.append(DistributionSummary(
                    column="Quantity", table="Sales",
                    min_val=int(qty.min()), max_val=int(qty.max()),
                    mean_val=round(float(qty.mean()), 2),
                    median_val=round(float(qty.median()), 2),
                    std_val=round(float(qty.std()), 2),
                ))
                # Sanity: avg qty should be reasonable (0.5 - 10)
                avg_qty = float(qty.mean())
                report.checks.append(CheckResult(
                    name="Avg basket size (Quantity)",
                    passed=0.5 <= avg_qty <= 10,
                    message=f"Mean={avg_qty:.2f}",
                    details="Expected between 0.5 and 10 based on typical Poisson config",
                    category="Distribution",
                ))

            if "NetPrice" in sales_df.columns:
                price = sales_df["NetPrice"]
                report.distributions.append(DistributionSummary(
                    column="NetPrice", table="Sales",
                    min_val=round(float(price.min()), 2),
                    max_val=round(float(price.max()), 2),
                    mean_val=round(float(price.mean()), 2),
                    median_val=round(float(price.median()), 2),
                    std_val=round(float(price.std()), 2),
                ))
                # Sanity: no negative net prices
                neg_count = int((price < 0).sum())
                report.checks.append(CheckResult(
                    name="Negative NetPrice",
                    passed=neg_count == 0,
                    message=f"{neg_count} negative price(s)" if neg_count > 0 else "All prices non-negative",
                    category="Distribution",
                ))

            if "DiscountAmount" in sales_df.columns:
                disc = sales_df["DiscountAmount"]
                report.distributions.append(DistributionSummary(
                    column="DiscountAmount", table="Sales",
                    min_val=round(float(disc.min()), 2),
                    max_val=round(float(disc.max()), 2),
                    mean_val=round(float(disc.mean()), 2),
                    median_val=round(float(disc.median()), 2),
                    std_val=round(float(disc.std()), 2),
                ))
                neg_disc = int((disc < 0).sum())
                report.checks.append(CheckResult(
                    name="Negative DiscountAmount",
                    passed=neg_disc == 0,
                    message=f"{neg_disc} negative discount(s)" if neg_disc > 0 else "All discounts non-negative",
                    category="Distribution",
                ))

            if "OrderDate" in sales_df.columns:
                dates = pd.to_datetime(sales_df["OrderDate"])
                report.checks.append(CheckResult(
                    name="Sales date range",
                    passed=True,
                    message=f"{dates.min().date()} to {dates.max().date()}",
                    category="Distribution",
                ))

                # Monthly distribution
                monthly = dates.dt.to_period("M").value_counts().sort_index()
                top_months = [(str(k), int(v)) for k, v in monthly.head(3).items()]
                report.distributions.append(DistributionSummary(
                    column="OrderDate (monthly)", table="Sales",
                    min_val=int(monthly.min()), max_val=int(monthly.max()),
                    mean_val=round(float(monthly.mean()), 0),
                    top_values=top_months,
                ))

    # Returns: check return rate if sales return exists
    returns_path = _find_table(facts_folder, "sales_return")
    if returns_path is not None and sales_path is not None:
        returns_count = _row_count_fast(returns_path)
        sales_count = _row_count_fast(sales_path)
        if returns_count is not None and sales_count is not None and sales_count > 0:
            return_rate = returns_count / sales_count
            report.checks.append(CheckResult(
                name="Return rate",
                passed=True,
                message=f"{return_rate:.2%} ({returns_count:,} returns / {sales_count:,} sales)",
                category="Distribution",
            ))

    # Customers per geography
    cust_path = _find_table(dims_folder, "customers")
    if cust_path is not None:
        cust_df = cache.get(cust_path, columns=["GeographyKey"])
        if cust_df is not None and "GeographyKey" in cust_df.columns:
            geo_dist = cust_df["GeographyKey"].value_counts()
            report.distributions.append(DistributionSummary(
                column="GeographyKey", table="Customers",
                min_val=int(geo_dist.min()), max_val=int(geo_dist.max()),
                mean_val=round(float(geo_dist.mean()), 0),
            ))


def _collect_row_counts(
    report: QualityReport,
    dims_folder: Path,
    facts_folder: Path,
) -> None:
    """Count rows in all output tables using metadata when possible."""
    for folder, label in [(dims_folder, "dim"), (facts_folder, "fact")]:
        if not folder.exists():
            continue
        for item in sorted(folder.iterdir()):
            count = _row_count_fast(item)
            if count is not None:
                name = item.stem if item.is_file() else item.name
                report.table_row_counts[f"{label}/{name}"] = count


# ============================================================================
# HTML renderer
# ============================================================================

def _render_html(report: QualityReport) -> str:
    """Render the report as a standalone HTML page."""

    # --- Checks: group by category, failures first within each group ---
    from collections import OrderedDict
    cats: OrderedDict[str, List[CheckResult]] = OrderedDict()
    for c in report.checks:
        cats.setdefault(c.category, []).append(c)

    check_rows = []
    for cat, checks in cats.items():
        # Sort: failures first, then passes
        checks_sorted = sorted(checks, key=lambda c: (c.passed, c.name))
        cat_pass = sum(1 for c in checks_sorted if c.passed)
        cat_fail = len(checks_sorted) - cat_pass
        # Category header row
        summary_parts = []
        if cat_fail:
            summary_parts.append(f'<span class="cat-fail">{cat_fail} failed</span>')
        if cat_pass:
            summary_parts.append(f'<span class="cat-pass">{cat_pass} passed</span>')
        check_rows.append(
            f'<tr class="cat-header">'
            f'<td colspan="3">{html.escape(cat)}</td>'
            f'<td class="cat-summary">{" &middot; ".join(summary_parts)}</td>'
            f'</tr>'
        )
        for c in checks_sorted:
            status = "PASS" if c.passed else "FAIL"
            css = "pass" if c.passed else "fail"
            detail = f'<div class="detail">{html.escape(c.details)}</div>' if c.details else ""
            check_rows.append(
                f'<tr class="{css}">'
                f'<td><span class="badge badge-{css}">{status}</span></td>'
                f'<td class="check-name">{html.escape(c.name)}</td>'
                f'<td>{html.escape(c.message)}{detail}</td>'
                f'<td></td>'
                f'</tr>'
            )

    dist_rows = []
    for d in report.distributions:
        top = ", ".join(f"{k}: {v}" for k, v in d.top_values) if d.top_values else ""
        dist_rows.append(
            f'<tr>'
            f'<td>{html.escape(d.table)}</td>'
            f'<td>{html.escape(d.column)}</td>'
            f'<td>{d.min_val}</td>'
            f'<td>{d.max_val}</td>'
            f'<td>{d.mean_val}</td>'
            f'<td>{d.median_val if d.median_val is not None else ""}</td>'
            f'<td>{d.std_val if d.std_val is not None else ""}</td>'
            f'<td>{html.escape(top)}</td>'
            f'</tr>'
        )

    # --- Row counts: split dim/fact, descending by count ---
    def _count_rows_html(items: List[tuple]) -> str:
        rows = []
        for name, count in sorted(items, key=lambda x: x[1], reverse=True):
            rows.append(
                f'<tr><td>{html.escape(name)}</td><td class="row-count">{count:,}</td></tr>'
            )
        return "".join(rows)

    dim_counts = [(n.split("/", 1)[1], c) for n, c in report.table_row_counts.items() if n.startswith("dim/")]
    fact_counts = [(n.split("/", 1)[1], c) for n, c in report.table_row_counts.items() if n.startswith("fact/")]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Data Quality Report</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f5f5; color: #333; padding: 2rem; max-width: 1200px; margin: 0 auto; }}
  h1 {{ margin-bottom: 0.25rem; }}
  .folder-name {{ font-size: 0.85rem; color: #64748b; margin-bottom: 0.5rem;
       font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; }}
  .summary {{ font-size: 1.1rem; margin-bottom: 1.5rem; color: #555; }}
  .summary .pass-count {{ color: #16a34a; font-weight: 600; }}
  .summary .fail-count {{ color: #dc2626; font-weight: 600; }}
  h2 {{ margin: 1.5rem 0 0.75rem; border-bottom: 2px solid #ddd; padding-bottom: 0.25rem; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 1.5rem; background: #fff;
           border-radius: 6px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  th {{ background: #f8f8f8; text-align: left; padding: 0.6rem 0.8rem; font-weight: 600;
       border-bottom: 2px solid #e5e5e5; font-size: 0.85rem; text-transform: uppercase;
       letter-spacing: 0.03em; }}
  td {{ padding: 0.5rem 0.8rem; border-bottom: 1px solid #eee; font-size: 0.9rem; }}
  tr:last-child td {{ border-bottom: none; }}
  /* Check table: category headers */
  tr.cat-header td {{ background: #f0f4f8; font-weight: 700; font-size: 0.85rem;
       padding: 0.6rem 0.8rem; color: #334155; border-bottom: 2px solid #e2e8f0;
       letter-spacing: 0.02em; }}
  .cat-summary {{ text-align: right; font-weight: 400; font-size: 0.8rem; }}
  .cat-pass {{ color: #16a34a; }}
  .cat-fail {{ color: #dc2626; }}
  /* Status badges */
  .badge {{ display: inline-block; padding: 0.15rem 0.55rem; border-radius: 4px;
           font-size: 0.75rem; font-weight: 700; letter-spacing: 0.04em; }}
  .badge-pass {{ background: #dcfce7; color: #15803d; }}
  .badge-fail {{ background: #fee2e2; color: #b91c1c; }}
  .check-name {{ color: #555; }}
  tr.fail {{ background: #fef2f2; }}
  .detail {{ font-size: 0.8rem; color: #888; margin-top: 0.2rem; }}
  /* Row counts */
  .row-count {{ font-variant-numeric: tabular-nums; text-align: right; }}
  h3 {{ margin: 1rem 0 0.5rem; color: #475569; font-size: 0.95rem; }}
  .row-counts-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; }}
  .elapsed {{ font-size: 0.85rem; color: #999; margin-top: 1rem; }}
</style>
</head>
<body>
<h1>Data Quality Report</h1>
<div class="folder-name">{html.escape(report.folder_name)}</div>
<div class="summary">
  <span class="pass-count">{report.passed} passed</span> &middot;
  <span class="fail-count">{report.failed} failed</span> &middot;
  {report.total} total checks
</div>

<h2>Checks</h2>
<table>
<thead><tr><th style="width:70px">Status</th><th>Check</th><th>Result</th><th></th></tr></thead>
<tbody>
{"".join(check_rows)}
</tbody>
</table>

<h2>Row Counts</h2>
<div class="row-counts-grid">
<div>
<h3>Dimensions</h3>
<table>
<thead><tr><th>Table</th><th>Rows</th></tr></thead>
<tbody>
{_count_rows_html(dim_counts)}
</tbody>
</table>
</div>
<div>
<h3>Facts</h3>
<table>
<thead><tr><th>Table</th><th>Rows</th></tr></thead>
<tbody>
{_count_rows_html(fact_counts)}
</tbody>
</table>
</div>
</div>

<h2>Distribution Summaries</h2>
<table>
<thead><tr><th>Table</th><th>Column</th><th>Min</th><th>Max</th><th>Mean</th><th>Median</th><th>StdDev</th><th>Top Values</th></tr></thead>
<tbody>
{"".join(dist_rows)}
</tbody>
</table>

<div class="elapsed">Report generated in {report.elapsed_sec:.2f}s</div>
</body>
</html>"""


# ============================================================================
# Public API
# ============================================================================

def generate_quality_report(
    final_folder: Path,
    cfg: Optional[Dict[str, Any]] = None,
) -> Path:
    """Run all quality checks on the output in *final_folder* and write an HTML report.

    Returns the path to the generated HTML file.
    """
    with stage("Data Quality Report"):
        t0 = time.time()
        report = QualityReport(folder_name=final_folder.name)
        cache = _TableCache()

        dims_folder = final_folder / "dimensions"
        facts_folder = final_folder / "facts"

        if not dims_folder.exists() and not facts_folder.exists():
            info(f"No dimensions/ or facts/ folder found in {final_folder}, skipping report.")
            return final_folder / "data_quality_report.html"

        info("Running referential integrity checks...")
        _check_referential_integrity(report, dims_folder, facts_folder, cache)

        info("Running null / duplicate checks...")
        _check_nulls_and_duplicates(report, dims_folder, facts_folder, cache)

        info("Checking distributions...")
        _check_distributions(report, dims_folder, facts_folder, cache, cfg)

        info("Counting rows...")
        _collect_row_counts(report, dims_folder, facts_folder)

        report.elapsed_sec = time.time() - t0

        out_path = final_folder / "data_quality_report.html"
        out_path.write_text(_render_html(report), encoding="utf-8")
        info(f"Quality report: {report.passed} passed, {report.failed} failed ({report.total} checks)")
        info(f"Report written - {out_path.name}")

        return out_path
