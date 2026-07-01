from __future__ import annotations

import os
from dataclasses import replace as _dc_replace
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, Callable

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from ..sales_logic import State, build_chunk_table
from ..sales_logic.columns import (
    SALES_CHANNEL_CORE_KEYS,
    _load_sales_channels,
    _sample_timekey_by_channel,
)
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


# ---------------------------------------------------------------------------
# Shared order encoding — computed once per chunk, reused by all _ensure_* fns
# ---------------------------------------------------------------------------

class _OrderEncoding:
    """Pre-computed OrderNumber dictionary encoding + first-row indices.

    Shared by _ensure_sales_channel_key_on_lines and _ensure_time_key_on_lines
    to avoid redundant pc.dictionary_encode + np.minimum.at calls.
    """
    __slots__ = ("enc", "n_orders", "order_inv", "first_row")

    def __init__(self, enc: pa.DictionaryArray, n_orders: int,
                 order_inv: np.ndarray, first_row: np.ndarray):
        self.enc = enc
        self.n_orders = n_orders
        self.order_inv = order_inv
        self.first_row = first_row


def _encode_orders(table: pa.Table) -> Optional[_OrderEncoding]:
    """Encode OrderNumber once; returns None if column absent."""
    if "OrderNumber" not in table.column_names:
        return None

    order_col = table["OrderNumber"]
    if isinstance(order_col, pa.ChunkedArray):
        order_col = order_col.combine_chunks()

    enc = pc.dictionary_encode(order_col)
    n_orders = len(enc.dictionary)
    order_inv = np.asarray(enc.indices.to_numpy(zero_copy_only=False), dtype=np.int64)

    # First-row index per order (vectorized, single pass)
    pos = np.arange(order_inv.size, dtype=np.int64)
    first_row = np.full(n_orders, order_inv.size, dtype=np.int64)
    np.minimum.at(first_row, order_inv, pos)
    first_row[first_row == order_inv.size] = 0

    return _OrderEncoding(enc=enc, n_orders=n_orders,
                          order_inv=order_inv, first_row=first_row)


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
            raise RuntimeError("output_paths must implement delta_part_path() for deltaparquet")
        path = output_paths.delta_part_path(table_name, idx)
        write_parquet_fn(table, path)
        return {"part": os.path.basename(path), "rows": table.num_rows}

    if ff == "csv":
        if not hasattr(output_paths, "chunk_path"):
            raise RuntimeError("output_paths must implement chunk_path() for csv")
        path = output_paths.chunk_path(table_name, idx, "csv")
        write_csv_fn(table, path)
        return path

    if not hasattr(output_paths, "chunk_path"):
        raise RuntimeError("output_paths must implement chunk_path() for parquet")
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
        raise RuntimeError(
            f"Cannot project {table_name}: missing columns {sorted(missing)}. "
            f"Available columns: {table.column_names}"
        )
    return table.select(cols)


def _write_table(table_name: str, idx: int, table: pa.Table) -> Union[str, Dict[str, Any]]:
    return write_table_by_format(
        file_format=State.file_format,
        output_paths=State.output_paths,
        table_name=table_name,
        idx=idx,
        table=table,
        write_csv_fn=lambda t, p: _write_csv(t, p, table_name=table_name),
        write_parquet_fn=lambda t, p: _write_parquet_table(t, p, table_name=table_name, is_chunk=True),
    )


def _mode() -> str:
    return str(getattr(State, "sales_output", "sales") or "sales").strip().lower()


def _task_require_cols(table: pa.Table, cols: Sequence[str], *, ctx: str) -> None:
    got = set(table.column_names)
    missing = sorted(set(cols) - got)
    if missing:
        raise RuntimeError(f"{ctx} missing columns: {missing}. Available: {sorted(got)}")


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
    """Build the base ReturnsConfig from State, or None if returns disabled.

    ``event_key_offset`` is left at 0 here; _worker_task sets the per-chunk
    offset (idx * returns_event_key_capacity) via dataclasses.replace so each
    chunk's ReturnEventKey range stays disjoint.
    """
    if not bool(getattr(State, "returns_enabled", False)):
        return None
    if TABLE_SALES_RETURN is None:
        raise RuntimeError("returns_enabled=True but TABLE_SALES_RETURN is not defined in output_paths.py")
    # Phase 3.4: fulfillment-friction coupling params (read from
    # models.fulfillment; 0.0 when the feature is off => legacy returns behavior).
    _mdl = getattr(State, "models_cfg", None)
    _ff = _mdl.get("fulfillment", None) if _mdl is not None else None
    _ff_on = bool(_ff.get("enabled", False)) if _ff is not None else False
    _friction_boost = float(_ff.get("return_prob_boost", 0.0)) if _ff_on else 0.0
    _friction_shorten = float(_ff.get("return_lag_shorten", 0.0)) if _ff_on else 0.0
    return ReturnsConfig(
        enabled=True,
        return_rate=float(getattr(State, "returns_rate", 0.0) or 0.0),
        min_lag_days=int(getattr(State, "returns_min_lag_days", 0) or 0),
        max_lag_days=int(getattr(State, "returns_max_lag_days", 60) or 60),
        reason_keys=_as_list(getattr(State, "returns_reason_keys", None), default=[1]),
        reason_probs=_as_list(getattr(State, "returns_reason_probs", None), default=[1.0]),
        full_line_probability=float(getattr(State, "returns_full_line_probability", 0.85)),
        split_return_rate=float(getattr(State, "returns_split_return_rate", 0.0)),
        max_splits=int(getattr(State, "returns_max_splits", 3)),
        split_min_gap=int(getattr(State, "returns_split_min_gap", 3)),
        split_max_gap=int(getattr(State, "returns_split_max_gap", 20)),
        lag_distribution=str(getattr(State, "returns_lag_distribution", "uniform") or "uniform"),
        lag_mode=int(getattr(State, "returns_lag_mode", 7)),
        event_key_offset=0,
        logistics_keys=frozenset(getattr(State, "returns_logistics_keys", ())),
        friction_return_boost=_friction_boost,
        friction_lag_shorten=_friction_shorten,
    )


def _maybe_build_returns(
    source_table: pa.Table, *, chunk_seed: int, mode: str, returns_cfg: Optional[ReturnsConfig]
) -> Optional[pa.Table]:
    if returns_cfg is None:
        return None
    if mode not in {"sales", "sales_order", "both"}:
        return None

    _task_require_cols(source_table, RETURNS_REQUIRED_DETAIL_COLS, ctx="Returns build requires")

    returns_seed = chunk_seed ^ 0x5A5A_1234
    returns_table = build_sales_returns_from_detail(source_table, chunk_seed=returns_seed, cfg=returns_cfg)
    return returns_table if returns_table.num_rows > 0 else None


# -----------------------------
# ChannelKey + TimeKey
# -----------------------------

def _channel_keys_and_probs() -> tuple[np.ndarray, np.ndarray]:
    """(keys, p) for sampling ChannelKey, from the shared channel loader.

    Falls back to the core channel keys from defaults when the dimension
    parquet is unavailable.
    """
    cache = _load_sales_channels(State)
    if cache is None:
        keys = SALES_CHANNEL_CORE_KEYS
        return keys, np.full(keys.shape[0], 1.0 / keys.shape[0], dtype=np.float64)
    keys, p, _ = cache
    return keys, p


def _ensure_sales_channel_key_on_lines(
    table: pa.Table, *, seed: int,
    order_enc: Optional[_OrderEncoding] = None,
) -> pa.Table:
    """Add ChannelKey to a line-level table; constant within OrderNumber if present."""
    if "ChannelKey" in table.column_names:
        return table

    keys, p = _channel_keys_and_probs()
    rng = np.random.default_rng(seed)

    # Build the encoding here when a caller didn't pass one (e.g. direct calls).
    # _encode_orders returns None only when OrderNumber is absent; otherwise it
    # reproduces exactly what _worker_task passes, so RNG draws are identical.
    if order_enc is None:
        order_enc = _encode_orders(table)

    if order_enc is not None:
        per_order = rng.choice(keys, size=order_enc.n_orders, p=p).astype(np.int32, copy=False)
        per_order_arr = pa.array(per_order, type=pa.int32())
        col = pc.take(per_order_arr, order_enc.enc.indices)
    else:
        per_row = rng.choice(keys, size=table.num_rows, p=p).astype(np.int32, copy=False)
        col = pa.array(per_row, type=pa.int32())

    return table.append_column("ChannelKey", col)


def _combine_if_chunked(col: pa.Array) -> pa.Array:
    """Combine a ChunkedArray into a single array; pass-through otherwise."""
    if isinstance(col, pa.ChunkedArray):
        return col.combine_chunks()
    return col


def _ensure_time_key_on_lines(
    table: pa.Table, *, seed: int,
    order_enc: Optional[_OrderEncoding] = None,
) -> pa.Table:
    """Ensure TimeKey exists and is constant within OrderNumber (if present)."""
    rng = np.random.default_rng(seed)

    cache = _load_sales_channels(State)
    channel_hour_lut = cache[2] if cache is not None else None
    has_channel = "ChannelKey" in table.column_names and channel_hour_lut is not None

    # Build the encoding here when a caller didn't pass one (e.g. direct calls).
    # _encode_orders returns None only when OrderNumber is absent; otherwise it
    # reproduces exactly what _worker_task passes, so RNG draws are identical.
    if order_enc is None:
        order_enc = _encode_orders(table)

    if order_enc is not None:
        enc = order_enc.enc
        n_orders = order_enc.n_orders
        first = order_enc.first_row

        if "TimeKey" in table.column_names:
            tc_np = np.asarray(
                _combine_if_chunked(table["TimeKey"]).to_numpy(zero_copy_only=False),
                dtype=np.int32,
            )
            per_order_arr = pa.array(tc_np[first], type=pa.int32())
            time_col = pc.take(per_order_arr, enc.indices)
            idx = table.schema.get_field_index("TimeKey")
            return table.set_column(idx, "TimeKey", time_col)

        if has_channel:
            sc_np = np.asarray(
                _combine_if_chunked(table["ChannelKey"]).to_numpy(zero_copy_only=False),
                dtype=np.int32,
            )
            per_order_sc = sc_np[first]
            per_order_time = _sample_timekey_by_channel(rng, per_order_sc, channel_hour_lut)
            per_order_arr = pa.array(per_order_time, type=pa.int32())
            time_col = pc.take(per_order_arr, enc.indices)
        else:
            per_order = rng.integers(0, 1440, size=n_orders, dtype=np.int32)
            per_order_arr = pa.array(per_order, type=pa.int32())
            time_col = pc.take(per_order_arr, enc.indices)

        return table.append_column("TimeKey", time_col)

    # No OrderNumber: leave existing TimeKey alone; otherwise sample per row
    if "TimeKey" in table.column_names:
        return table

    if has_channel:
        sc_np = np.asarray(
            _combine_if_chunked(table["ChannelKey"]).to_numpy(zero_copy_only=False),
            dtype=np.int32,
        )
        out = _sample_timekey_by_channel(rng, sc_np, channel_hour_lut)
        time_col = pa.array(out, type=pa.int32())
    else:
        per_row = rng.integers(0, 1440, size=table.num_rows, dtype=np.int32)
        time_col = pa.array(per_row, type=pa.int32())

    return table.append_column("TimeKey", time_col)


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

            raise RuntimeError(
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


def _attach_aggs(
    result: Any,
    aggs: Dict[str, Any],
    table_name_fallback: str = TABLE_SALES,
) -> Any:
    """Attach micro-aggregate dicts to a worker result.

    *aggs* maps result keys (e.g. ``"_budget_agg"``) to their payloads.
    Normalizes bare str results into a dict so the main process can
    pop agg keys without breaking existing result handling.
    """
    if not aggs:
        return result

    if isinstance(result, str):
        result = {table_name_fallback: result}

    if not isinstance(result, dict):
        return result

    result.update(aggs)
    return result


def _worker_task(args):
    tasks, single = normalize_tasks(args)
    results = []

    # State.validate(["chunk_size"]) is now called once in init_sales_worker
    # instead of per-task, eliminating ~100-1000 redundant validation calls.

    # --- Hoist loop-invariant reads from State ---
    validate_header = bool(getattr(State, "validate_header_invariants", False))
    cap_orders = int(getattr(State, "chunk_size", 0) or 0)
    if cap_orders <= 0:
        raise RuntimeError(f"State.chunk_size must be > 0, got {cap_orders}")

    no_discount_key = State.no_discount_key
    skip_order_cols_flag = bool(getattr(State, "skip_order_cols_requested", False))

    mode = _mode()
    returns_cfg_base = _build_returns_config()
    # Per-chunk ReturnEventKey ranges are offset by idx * capacity; read the
    # capacity once (loop-invariant) rather than per chunk.
    returns_capacity = (
        int(getattr(State, "returns_event_key_capacity", 100000))
        if returns_cfg_base is not None else 0
    )
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
            raise RuntimeError(
                f"State.chunk_size={cap_orders} < batch_i={batch_i}; "
                "chunk_size must be the constant order-id stride and >= the maximum batch size."
            )

        chunk_seed = derive_chunk_seed(seed, idx_i)

        # Per-chunk returns config with unique event_key_offset
        if returns_cfg_base is not None:
            returns_cfg = _dc_replace(returns_cfg_base, event_key_offset=idx_i * returns_capacity)
        else:
            returns_cfg = None

        detail_table = build_chunk_table(
            batch_i,
            chunk_seed,
            no_discount_key=no_discount_key,
            chunk_idx=idx_i,
            chunk_capacity_orders=cap_orders,
        )
        order_enc = _encode_orders(detail_table)

        detail_table = _ensure_sales_channel_key_on_lines(
            detail_table, seed=chunk_seed ^ 0xA11CE, order_enc=order_enc)
        detail_table = _ensure_time_key_on_lines(
            detail_table, seed=chunk_seed ^ 0xC0FFEE, order_enc=order_enc)

        if not isinstance(detail_table, pa.Table):
            raise TypeError("chunk_builder must return pyarrow.Table")

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
                raise RuntimeError(
                    f"sales_output={mode} / Header build missing columns: {missing}. "
                    f"Available: {detail_table.column_names}")

            header_missing = sorted(header_need_set - got)
            if header_missing:
                raise RuntimeError(
                    f"Header build missing columns: {header_missing}. "
                    f"Available: {detail_table.column_names}")

        if mode == "sales":
            sales_table = detail_table
            if skip_order_cols_flag:
                sales_table = _drop_order_cols_for_sales(sales_table)

            returns_table = _maybe_build_returns(
                detail_table, chunk_seed=chunk_seed, mode=mode, returns_cfg=returns_cfg)
            sales_out = _project_for_table(TABLE_SALES, sales_table)

            if returns_table is None:
                result = _write_table(TABLE_SALES, idx_i, sales_out)
                results.append(_attach_aggs(result, chunk_aggs, TABLE_SALES))
                continue

            out: Dict[str, Any] = {TABLE_SALES: _write_table(TABLE_SALES, idx_i, sales_out)}
            returns_out = _project_for_table(TABLE_SALES_RETURN, returns_table)  # type: ignore[arg-type]
            out[TABLE_SALES_RETURN] = _write_table(TABLE_SALES_RETURN, idx_i, returns_out)  # type: ignore[arg-type]
            results.append(_attach_aggs(out, chunk_aggs))
            continue

        out: Dict[str, Any] = {}

        if mode == "both":
            sales_table = detail_table
            if skip_order_cols_flag:
                sales_table = _drop_order_cols_for_sales(sales_table)
            out[TABLE_SALES] = _write_table(TABLE_SALES, idx_i, _project_for_table(TABLE_SALES, sales_table))

        header_table = build_header_from_detail(
            detail_table, validate_invariants=validate_header)

        out[TABLE_SALES_ORDER_DETAIL] = _write_table(
            TABLE_SALES_ORDER_DETAIL, idx_i, _project_for_table(TABLE_SALES_ORDER_DETAIL, detail_table)
        )
        out[TABLE_SALES_ORDER_HEADER] = _write_table(
            TABLE_SALES_ORDER_HEADER, idx_i, _project_for_table(TABLE_SALES_ORDER_HEADER, header_table)
        )

        returns_table = _maybe_build_returns(
            detail_table, chunk_seed=chunk_seed, mode=mode, returns_cfg=returns_cfg)
        if returns_table is not None:
            out[TABLE_SALES_RETURN] = _write_table(
                TABLE_SALES_RETURN, idx_i, _project_for_table(TABLE_SALES_RETURN, returns_table)  # type: ignore[arg-type]
            )

        results.append(_attach_aggs(out, chunk_aggs))

    return results[0] if single else results
