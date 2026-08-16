from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, Callable

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from src.exceptions import SalesError
from ..sales_logic import State, build_chunk_table
from ..chunk_result import ChunkResult
from ..output_paths import TABLE_SALES, TABLE_SALES_ORDER_DETAIL, TABLE_SALES_ORDER_HEADER

try:
    from ..output_paths import TABLE_SALES_RETURN  # type: ignore
except ImportError:  # pragma: no cover
    TABLE_SALES_RETURN = None  # type: ignore

from .init import int_or
from .io import _write_csv, _write_parquet_table
from .returns_builder import ReturnsConfig, RETURNS_REQUIRED_DETAIL_COLS, build_sales_returns_from_detail

# Budget streaming aggregation (lazy import to avoid hard dependency)
try:
    from src.facts.budget.micro_agg import micro_aggregate_sales
    _BUDGET_AGG_AVAILABLE = True
except ImportError:
    _BUDGET_AGG_AVAILABLE = False

try:
    from src.facts.inventory.micro_agg import micro_aggregate_inventory
    _INVENTORY_AGG_AVAILABLE = True
except ImportError:
    _INVENTORY_AGG_AVAILABLE = False

try:
    from src.facts.wishlists.micro_agg import micro_aggregate_wishlists
    _WISHLISTS_AGG_AVAILABLE = True
except ImportError:
    _WISHLISTS_AGG_AVAILABLE = False

try:
    from src.facts.complaints.micro_agg import micro_aggregate_complaints
    _COMPLAINTS_AGG_AVAILABLE = True
except ImportError:
    _COMPLAINTS_AGG_AVAILABLE = False


Task = Tuple[int, int, Any]  # (idx, batch_size, seed)
TaskArgs = Union[Task, Sequence[Task]]


def normalize_tasks(args: TaskArgs) -> Tuple[List[Task], bool]:
    if isinstance(args, tuple):
        if len(args) != 3:
            raise ValueError(f"Task tuple must be (idx,batch_size,seed), got len={len(args)}")
        return [args], True
    return list(args), False


def derive_chunk_seed(seed: Any, idx: int) -> int:
    """Deterministic per-chunk RNG seed as a pure function of (run_seed, idx).

    Uses the repo's house pattern ``SeedSequence(run_seed).spawn(n)[idx]`` — the
    same independent-substream derivation the customer/wishlist/complaint workers
    use. ``spawn(idx + 1)[idx]`` yields the identical child SeedSequence as
    ``spawn(n_chunks)[idx]`` (spawn keys are assigned 0..n-1 from a fresh
    SeedSequence), so a single chunk can be regenerated in isolation without
    materializing the whole per-chunk seed array, and output is independent of
    worker count / chunk dispatch order.
    """
    base_seed = int_or(seed, 0)
    child = np.random.SeedSequence(base_seed).spawn(int(idx) + 1)[int(idx)]
    return int(child.generate_state(1, dtype=np.uint32)[0])


def write_table_by_format(
    *,
    file_format: str,
    output_paths: Any,
    table_name: str,
    idx: int,
    table: pa.Table,
    write_csv_fn: Callable[[pa.Table, str], None],
    write_parquet_fn: Callable[[pa.Table, str], None],
) -> Union[str, Dict[str, Any]]:
    ff = (file_format or "").strip().lower()
    if ff == "deltaparquet":
        if not hasattr(output_paths, "delta_part_path"):
            raise SalesError("output_paths must implement delta_part_path() for deltaparquet")
        path = output_paths.delta_part_path(table_name, idx)
        write_parquet_fn(table, path)
        return {"part": os.path.basename(path), "rows": table.num_rows}

    if ff == "csv":
        if not hasattr(output_paths, "chunk_path"):
            raise SalesError("output_paths must implement chunk_path() for csv")
        path = output_paths.chunk_path(table_name, idx, "csv")
        write_csv_fn(table, path)
        return path

    if not hasattr(output_paths, "chunk_path"):
        raise SalesError("output_paths must implement chunk_path() for parquet")
    path = output_paths.chunk_path(table_name, idx, "parquet")
    write_parquet_fn(table, path)
    return path


_DROP_ORDER_COLS = {"OrderNumber", "OrderLineNumber"}


def _drop_order_cols_for_sales(table: pa.Table) -> pa.Table:
    to_drop = _DROP_ORDER_COLS.intersection(table.schema.names)
    if not to_drop:
        return table
    return table.drop_columns(list(to_drop))


# Cached partition cols to avoid repeated State attribute lookups.
_cached_partition_cols: Optional[set[str]] = None


def _partition_cols() -> set[str]:
    global _cached_partition_cols
    if _cached_partition_cols is not None:
        return _cached_partition_cols
    cols = getattr(State, "partition_cols", None)
    if isinstance(cols, (list, tuple)) and cols:
        _cached_partition_cols = {str(c) for c in cols}
    else:
        _cached_partition_cols = {"Year", "Month"}
    return _cached_partition_cols


# Per-table projection: cached as (list, tuple) for fast identity check.
_projection_cache: Dict[str, Tuple[List[str], tuple]] = {}


def reset_task_caches() -> None:
    """Clear worker-lifetime caches keyed off State (called once per worker init).

    Both caches derive from State.partition_cols / State.schema_by_table, which
    are bound fresh on each init_sales_worker. Resetting here keeps a reused
    worker process (web server / in-process tests) from serving stale entries
    when the schema or partition policy changes between runs.
    """
    global _cached_partition_cols
    _cached_partition_cols = None
    _projection_cache.clear()


def _project_for_table(table_name: str, table: pa.Table) -> pa.Table:
    cached = _projection_cache.get(table_name)
    if cached is None:
        expected = State.schema_by_table[table_name]
        part_cols = _partition_cols()
        cols = [n for n in expected.names if n not in part_cols]
        cols_tuple = tuple(cols)
        _projection_cache[table_name] = (cols, cols_tuple)
    else:
        cols, cols_tuple = cached

    # Fast path: tuple equality is a single C-level comparison
    if tuple(table.column_names) == cols_tuple:
        return table

    got_set = set(table.column_names)
    missing = [c for c in cols if c not in got_set]
    if missing:
        raise SalesError(
            f"Cannot project {table_name}: missing columns {sorted(missing)}. "
            f"Available columns: {table.column_names}"
        )
    return table.select(cols)


def _write_table(
    table_name: str,
    idx: int,
    table: pa.Table,
    metrics: Optional[Dict[str, float]] = None,
) -> Union[str, Dict[str, Any]]:
    started = time.perf_counter()
    result = write_table_by_format(
        file_format=State.file_format,
        output_paths=State.output_paths,
        table_name=table_name,
        idx=idx,
        table=table,
        write_csv_fn=lambda t, p: _write_csv(t, p, table_name=table_name),
        write_parquet_fn=lambda t, p: _write_parquet_table(t, p, table_name=table_name, is_chunk=True),
    )
    if metrics is not None:
        metrics["write_seconds"] = metrics.get("write_seconds", 0.0) + (
            time.perf_counter() - started
        )
    return result


def _mode() -> str:
    return str(getattr(State, "sales_output", "sales") or "sales").strip().lower()


def _task_require_cols(table: pa.Table, cols: Sequence[str], *, ctx: str) -> None:
    got = set(table.column_names)
    missing = sorted(set(cols) - got)
    if missing:
        raise SalesError(f"{ctx} missing columns: {missing}. Available: {sorted(got)}")


def _as_list(v: Any, default: Sequence[Any]) -> list[Any]:
    if v is None:
        return list(default)
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    if isinstance(v, (str, bytes)):
        return [v]
    tolist = getattr(v, "tolist", None)
    if callable(tolist):
        try:
            out = tolist()
            if isinstance(out, list):
                return out
        except (TypeError, ValueError):
            pass
    return [v]


def _build_returns_config() -> Optional[ReturnsConfig]:
    """Return the ReturnsConfig bound on State (None when returns disabled).

    Built once in the coordinator and broadcast, then used as-is per chunk:
    ReturnEventKey is a hash of each event's (OrderNumber, line, ReturnSequence),
    so no per-chunk offset is needed to keep keys disjoint across chunks.
    """
    policy = getattr(State, "returns_policy", None)
    if policy is not None and TABLE_SALES_RETURN is None:
        raise SalesError(
            "returns are enabled but TABLE_SALES_RETURN is not defined in output_paths.py"
        )
    return policy


def _maybe_build_returns(
    source_table: pa.Table, *, chunk_seed: int, mode: str, returns_cfg: Optional[ReturnsConfig]
) -> Optional[pa.Table]:
    if returns_cfg is None:
        return None
    if mode not in {"sales", "sales_order", "both"}:
        return None

    _task_require_cols(source_table, RETURNS_REQUIRED_DETAIL_COLS, ctx="Returns build requires")

    returns_seed = chunk_seed ^ 0x5A5A_1234
    returns_table = build_sales_returns_from_detail(
        source_table,
        chunk_seed=returns_seed,
        cfg=returns_cfg,
        product_return_risk_by_key=getattr(
            State, "product_return_risk_by_key", None,
        ),
        product_defect_risk_by_key=getattr(
            State, "product_defect_risk_by_key", None,
        ),
    )
    return returns_table if returns_table.num_rows > 0 else None


def build_header_from_detail(detail: pa.Table, *, validate_invariants: bool = True) -> pa.Table:
    inv_cols = ["CustomerKey", "StoreKey", "EmployeeKey", "OrderDate"]

    col_names = detail.column_names
    if "PromotionKey" in col_names:
        inv_cols.append("PromotionKey")
    if "CurrencyKey" in col_names:
        inv_cols.append("CurrencyKey")
    if "ChannelKey" in col_names:
        inv_cols.append("ChannelKey")
    if "TimeKey" in col_names:
        inv_cols.append("TimeKey")

    gb = detail.group_by(["OrderNumber"])

    if validate_invariants:
        aggs = []
        for c in inv_cols:
            aggs.append((c, "min"))
            aggs.append((c, "max"))
        aggs.append(("IsOrderDelayed", "max"))

        out = gb.aggregate(aggs)

        bad = None
        for c in inv_cols:
            neq = pc.not_equal(out[f"{c}_min"], out[f"{c}_max"])
            bad = neq if bad is None else pc.or_(bad, neq)

        if bad is not None and bool(pc.any(bad).as_py()):
            bad_out = out.filter(bad).slice(0, 5)

            def _py(name: str):
                return bad_out[name].to_pylist() if name in bad_out.column_names else None

            parts = [f"OrderNumber(s)={_py('OrderNumber')}"]
            for c in inv_cols:
                parts.append(f"{c}_min={_py(f'{c}_min')}")
                parts.append(f"{c}_max={_py(f'{c}_max')}")

            raise SalesError(
                "Invalid OrderNumber invariants: a OrderNumber maps to multiple values. "
                + " | ".join(parts)
            )

        cols, names = [], []

        def _add(src: str, dst: str):
            if src in out.column_names:
                cols.append(out[src])
                names.append(dst)

        _add("OrderNumber", "OrderNumber")
        _add("CustomerKey_min", "CustomerKey")
        _add("StoreKey_min", "StoreKey")
        _add("EmployeeKey_min", "EmployeeKey")
        _add("PromotionKey_min", "PromotionKey")
        _add("CurrencyKey_min", "CurrencyKey")
        _add("ChannelKey_min", "ChannelKey")
        _add("OrderDate_min", "OrderDate")
        _add("TimeKey_min", "TimeKey")
        _add("IsOrderDelayed_max", "IsOrderDelayed")

        return pa.Table.from_arrays(cols, names=names)

    # Fast path: MIN only for invariants (halves group-by work)
    aggs = [(c, "min") for c in inv_cols]
    aggs.append(("IsOrderDelayed", "max"))

    out = gb.aggregate(aggs)

    cols, names = [], []

    def _add(src: str, dst: str):
        if src in out.column_names:
            cols.append(out[src])
            names.append(dst)

    _add("OrderNumber", "OrderNumber")
    _add("CustomerKey_min", "CustomerKey")
    _add("StoreKey_min", "StoreKey")
    _add("EmployeeKey_min", "EmployeeKey")
    _add("PromotionKey_min", "PromotionKey")
    _add("CurrencyKey_min", "CurrencyKey")
    _add("ChannelKey_min", "ChannelKey")
    _add("OrderDate_min", "OrderDate")
    _add("TimeKey_min", "TimeKey")
    _add("IsOrderDelayed_max", "IsOrderDelayed")

    return pa.Table.from_arrays(cols, names=names)


# ---------------------------------------------------------------------------
# Simple micro-agg registry: (result_key, available_flag, state_attr, agg_fn)
# Budget is excluded — it needs extra State attrs and ChannelKey guard.
# ---------------------------------------------------------------------------

_SIMPLE_AGGS: List[Tuple[str, bool, str, Any]] = [
    ("_inventory_agg", _INVENTORY_AGG_AVAILABLE, "inventory_enabled",
     micro_aggregate_inventory if _INVENTORY_AGG_AVAILABLE else None),
    ("_wishlists_agg", _WISHLISTS_AGG_AVAILABLE, "wishlists_enabled",
     micro_aggregate_wishlists if _WISHLISTS_AGG_AVAILABLE else None),
    ("_complaints_agg", _COMPLAINTS_AGG_AVAILABLE, "complaints_enabled",
     micro_aggregate_complaints if _COMPLAINTS_AGG_AVAILABLE else None),
]


def _resolve_simple_agg_flags() -> List[Tuple[str, Any]]:
    """Return [(result_key, agg_fn), ...] for enabled simple aggs."""
    return [
        (key, fn)
        for key, avail, state_attr, fn in _SIMPLE_AGGS
        if avail and bool(getattr(State, state_attr, False))
    ]


def _budget_enabled() -> bool:
    """Check if budget streaming aggregation is active for this worker."""
    return (
        _BUDGET_AGG_AVAILABLE
        and bool(getattr(State, "budget_enabled", False))
        and getattr(State, "budget_store_to_country", None) is not None
        and getattr(State, "budget_product_to_cat", None) is not None
    )


def _maybe_budget_agg(detail_table: pa.Table) -> Optional[Dict[str, Any]]:
    """Compute budget micro-aggregate from a detail chunk. Returns None if disabled."""
    if not _budget_enabled():
        return None
    if "ChannelKey" not in detail_table.column_names:
        return None
    return micro_aggregate_sales(
        detail_table,
        store_to_country=State.budget_store_to_country,
        product_to_cat=State.budget_product_to_cat,
    )


def _worker_task(args):
    tasks, single = normalize_tasks(args)
    results = []

    # The chunk_size presence check runs once in init_sales_worker instead of
    # per-task, eliminating ~100-1000 redundant checks.

    # --- Hoist loop-invariant reads from State ---
    validate_header = bool(getattr(State, "validate_header_invariants", False))
    cap_orders = int(getattr(State, "chunk_size", 0) or 0)
    if cap_orders <= 0:
        raise SalesError(f"State.chunk_size must be > 0, got {cap_orders}")

    no_discount_key = State.no_discount_key
    skip_order_cols_flag = bool(getattr(State, "skip_order_cols_requested", False))

    mode = _mode()
    returns_cfg = _build_returns_config()
    do_budget = _budget_enabled()
    simple_aggs = _resolve_simple_agg_flags()

    # Pre-validate column requirements for sales_order/both modes once
    so_require = None
    header_need_set = None
    if mode in {"sales_order", "both"}:
        so_require = {"OrderNumber", "OrderLineNumber",
                      "CustomerKey", "StoreKey", "EmployeeKey",
                      "OrderDate", "IsOrderDelayed"}
        expected_header = State.schema_by_table[TABLE_SALES_ORDER_HEADER]
        header_need_set = {"StoreKey", "EmployeeKey", "ChannelKey",
                           "TimeKey", "PromotionKey", "CurrencyKey"} & set(expected_header.names)

    for idx, batch_size, seed in tasks:
        idx_i = int(idx)
        batch_i = int(batch_size)

        if cap_orders < batch_i:
            raise SalesError(
                f"State.chunk_size={cap_orders} < batch_i={batch_i}; "
                "chunk_size must be the constant order-id stride and >= the maximum batch size."
            )

        chunk_seed = derive_chunk_seed(seed, idx_i)

        build_started = time.perf_counter()
        detail_table = build_chunk_table(
            batch_i,
            chunk_seed,
            no_discount_key=no_discount_key,
            chunk_idx=idx_i,
            chunk_capacity_orders=cap_orders,
        )

        if not isinstance(detail_table, pa.Table):
            raise TypeError("chunk_builder must return pyarrow.Table")
        chunk_metrics = {
            "build_seconds": time.perf_counter() - build_started,
            "write_seconds": 0.0,
        }

        budget_agg = _maybe_budget_agg(detail_table) if do_budget else None
        chunk_aggs: Dict[str, Any] = {}
        if budget_agg is not None:
            chunk_aggs["_budget_agg"] = budget_agg
        for agg_key, agg_fn in simple_aggs:
            agg_result = agg_fn(detail_table)
            if agg_result is not None:
                chunk_aggs[agg_key] = agg_result

        if mode in {"sales_order", "both"}:
            got = set(detail_table.column_names)
            missing = sorted(so_require - got)
            if missing:
                raise SalesError(
                    f"sales_output={mode} / Header build missing columns: {missing}. "
                    f"Available: {detail_table.column_names}")

            header_missing = sorted(header_need_set - got)
            if header_missing:
                raise SalesError(
                    f"Header build missing columns: {header_missing}. "
                    f"Available: {detail_table.column_names}")

        if mode == "sales":
            sales_table = detail_table
            if skip_order_cols_flag:
                sales_table = _drop_order_cols_for_sales(sales_table)

            returns_table = _maybe_build_returns(
                detail_table, chunk_seed=chunk_seed, mode=mode, returns_cfg=returns_cfg)
            sales_out = _project_for_table(TABLE_SALES, sales_table)

            out: Dict[str, Any] = {
                TABLE_SALES: _write_table(
                    TABLE_SALES, idx_i, sales_out, chunk_metrics
                )
            }
            if returns_table is not None:
                returns_out = _project_for_table(TABLE_SALES_RETURN, returns_table)  # type: ignore[arg-type]
                out[TABLE_SALES_RETURN] = _write_table(  # type: ignore[arg-type]
                    TABLE_SALES_RETURN, idx_i, returns_out, chunk_metrics
                )
            results.append(ChunkResult(
                chunk_idx=idx_i, outputs=out, aggs=chunk_aggs, metrics=chunk_metrics
            ))
            continue

        out: Dict[str, Any] = {}

        if mode == "both":
            sales_table = detail_table
            if skip_order_cols_flag:
                sales_table = _drop_order_cols_for_sales(sales_table)
            out[TABLE_SALES] = _write_table(
                TABLE_SALES,
                idx_i,
                _project_for_table(TABLE_SALES, sales_table),
                chunk_metrics,
            )

        header_table = build_header_from_detail(
            detail_table, validate_invariants=validate_header)

        out[TABLE_SALES_ORDER_DETAIL] = _write_table(
            TABLE_SALES_ORDER_DETAIL,
            idx_i,
            _project_for_table(TABLE_SALES_ORDER_DETAIL, detail_table),
            chunk_metrics,
        )
        out[TABLE_SALES_ORDER_HEADER] = _write_table(
            TABLE_SALES_ORDER_HEADER,
            idx_i,
            _project_for_table(TABLE_SALES_ORDER_HEADER, header_table),
            chunk_metrics,
        )

        returns_table = _maybe_build_returns(
            detail_table, chunk_seed=chunk_seed, mode=mode, returns_cfg=returns_cfg)
        if returns_table is not None:
            out[TABLE_SALES_RETURN] = _write_table(
                TABLE_SALES_RETURN,
                idx_i,
                _project_for_table(TABLE_SALES_RETURN, returns_table),  # type: ignore[arg-type]
                chunk_metrics,
            )

        results.append(ChunkResult(
            chunk_idx=idx_i, outputs=out, aggs=chunk_aggs, metrics=chunk_metrics
        ))

    return results[0] if single else results
