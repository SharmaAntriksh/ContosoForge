from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set

import pyarrow as pa

from src.tools.sql.dialect import SqlType
from src.utils.static_schemas import (
    get_sales_order_detail_schema,
    get_sales_order_header_schema,
    get_sales_schema,
)
from ..output_paths import TABLE_SALES, TABLE_SALES_ORDER_DETAIL, TABLE_SALES_ORDER_HEADER, TABLE_SALES_RETURN
from .returns_builder import _returns_schema_for


# ---------------------------------------------------------------------------
# Canonical SQL LogicalType -> Arrow dtype bridge.
#
# The Sales / Order schemas are declared ONCE as (name, ColumnSpec) tuples in
# static_schemas.py. The Arrow schemas built below are projected from those, so
# a column added or retyped there flows into parquet/delta with no second
# hand-maintained list. This map is complete for the fact columns — notably
# BIT -> bool_, which the chunk-builder-oriented map in sales_logic/globals.py
# deliberately omits.
# ---------------------------------------------------------------------------
_SQL_TO_ARROW = {
    SqlType.INT: pa.int32(),
    SqlType.BIGINT: pa.int64(),
    SqlType.SMALLINT: pa.int16(),
    SqlType.TINYINT: pa.int8(),
    SqlType.FLOAT: pa.float64(),
    SqlType.DECIMAL: pa.float64(),
    SqlType.DATE: pa.date32(),
    SqlType.DATETIME: pa.date32(),
    SqlType.DATETIME2: pa.date32(),
    SqlType.BIT: pa.bool_(),
    SqlType.VARCHAR: pa.string(),
    SqlType.CHAR: pa.string(),
    SqlType.TIME: pa.string(),
}


def _arrow_type(spec, name: str, order_id_int64: bool) -> pa.DataType:
    """Arrow dtype for a canonical ColumnSpec, promoting OrderNumber to int64
    when the run's authoritative ID-space decision requires it."""
    if name == "OrderNumber" and order_id_int64:
        return pa.int64()
    return _SQL_TO_ARROW.get(spec.sql_type, pa.string())


def _fields_from_logical(logical, *, drop=frozenset(), order_id_int64: bool = False) -> List[pa.Field]:
    """Project a (name, ColumnSpec) logical schema into pa.Fields, dropping the
    given names and promoting OrderNumber per ``order_id_int64``."""
    return [
        pa.field(name, _arrow_type(spec, name, order_id_int64))
        for name, spec in logical
        if name not in drop
    ]


def schema_dict_cols(schema: pa.Schema, exclude: Optional[Set[str]] = None) -> List[str]:
    """
    Return columns that are candidates for parquet dictionary encoding.
    Includes string and binary columns (consistent with sales_writer/encoding.py).
    """
    exclude = exclude or set()
    out: List[str] = []
    for f in schema:
        if f.name in exclude:
            continue
        if (
            pa.types.is_string(f.type)
            or pa.types.is_large_string(f.type)
            or pa.types.is_binary(f.type)
            or pa.types.is_large_binary(f.type)
        ):
            out.append(f.name)
    return out


@dataclass(frozen=True)
class WorkerSchemaBundle:
    # Sales schemas
    sales_schema_gen: pa.Schema        # used by chunk builder; MUST match arrays it produces
    sales_schema_out: pa.Schema        # used for write/projection/merge; includes injected cols like TimeKey

    # Table schemas + date-col policy
    schema_by_table: dict[str, pa.Schema]
    date_cols_by_table: dict[str, list[str]]

    # Parquet dictionary encoding policy
    parquet_dict_exclude: set[str]
    parquet_dict_cols_by_table: dict[str, list[str]]
    parquet_dict_cols: list[str]

    # Convenience variants (OUTPUT variants)
    schema_no_order: pa.Schema
    schema_with_order: pa.Schema
    schema_no_order_delta: pa.Schema
    schema_with_order_delta: pa.Schema


_ALL_DELTA_FIELDS = {"Year": pa.field("Year", pa.int16()), "Month": pa.field("Month", pa.int16())}


def build_worker_schemas(
    *,
    file_format: str,
    skip_order_cols: bool,
    skip_order_cols_requested: bool,
    returns_enabled: bool,
    parquet_dict_exclude: Optional[Set[str]] = None,
    models_cfg: Optional[dict] = None,
    order_id_int64: bool = False,
    partition_cols: Optional[Sequence[str]] = None,
) -> WorkerSchemaBundle:
    """
    Single source of truth for Sales / Order / Returns schemas & date-col policy.

    IMPORTANT:
      - sales_schema_gen is used by build_chunk_table (chunk builder). It must match the arrays that
        the builder currently emits. If you inject columns later in task.py (e.g., TimeKey), DO NOT
        add them to sales_schema_gen.
      - sales_schema_out is the "contract" for output chunks/merged files. It can include injected
        columns because projection/writing happens after injection.
    """

    ff = (file_format or "").strip().lower()
    is_delta = ff == "deltaparquet"

    # ---------------------------------------------------------------------
    # Sales schemas (GEN vs OUT)
    # ---------------------------------------------------------------------
    # Project the canonical Sales schema (declared once as (name, ColumnSpec)
    # in static_schemas.py) into Arrow, so a column added/retyped there flows to
    # parquet/delta with no second hand-maintained list.
    #   GEN = what the chunk builder emits — ChannelKey/TimeKey are injected
    #         later in task.py, so they are excluded here.
    #   OUT = the write/merge/project contract — includes the injected columns.
    # OrderNumber promotes to int64 per the run's authoritative order_id_int64
    # decision (the ~8x-total_rows ID ceiling computed in sales.py).
    sales_full = get_sales_schema(skip_order_cols=False)   # canonical, with order cols
    sales_noord = get_sales_schema(skip_order_cols=True)   # canonical, no order cols
    _INJECTED = frozenset({"ChannelKey", "TimeKey"})

    # Arrow dtype of OrderNumber for this run (int64 when the ID space needs it);
    # used to derive the Returns OrderNumber field via _returns_schema_for.
    order_num_type = pa.int64() if order_id_int64 else pa.int32()

    fields_with_order_gen = _fields_from_logical(sales_full, drop=_INJECTED, order_id_int64=order_id_int64)
    fields_no_order_gen = _fields_from_logical(sales_noord, drop=_INJECTED, order_id_int64=order_id_int64)
    fields_with_order_out = _fields_from_logical(sales_full, order_id_int64=order_id_int64)
    fields_no_order_out = _fields_from_logical(sales_noord, order_id_int64=order_id_int64)

    if partition_cols:
        delta_fields = [_ALL_DELTA_FIELDS[c] for c in partition_cols if c in _ALL_DELTA_FIELDS]
    else:
        delta_fields = list(_ALL_DELTA_FIELDS.values())

    # GEN variants
    schema_no_order_gen = pa.schema(fields_no_order_gen)
    schema_with_order_gen = pa.schema(fields_with_order_gen)
    schema_no_order_delta_gen = pa.schema(fields_no_order_gen + delta_fields)
    schema_with_order_delta_gen = pa.schema(fields_with_order_gen + delta_fields)

    # OUT variants
    schema_no_order_out = pa.schema(fields_no_order_out)
    schema_with_order_out = pa.schema(fields_with_order_out)
    schema_no_order_delta_out = pa.schema(fields_no_order_out + delta_fields)
    schema_with_order_delta_out = pa.schema(fields_with_order_out + delta_fields)

    if is_delta:
        sales_schema_gen = schema_no_order_delta_gen if skip_order_cols else schema_with_order_delta_gen
        sales_schema_out = schema_no_order_delta_out if skip_order_cols_requested else schema_with_order_delta_out
        schema_no_order = schema_no_order_delta_out
        schema_with_order = schema_with_order_delta_out
        schema_no_order_delta = schema_no_order_delta_out
        schema_with_order_delta = schema_with_order_delta_out
    else:
        sales_schema_gen = schema_no_order_gen if skip_order_cols else schema_with_order_gen
        sales_schema_out = schema_no_order_out if skip_order_cols_requested else schema_with_order_out
        schema_no_order = schema_no_order_out
        schema_with_order = schema_with_order_out
        schema_no_order_delta = schema_no_order_delta_out
        schema_with_order_delta = schema_with_order_delta_out

    # ---------------------------------------------------------------------
    # OrderHeader / OrderDetail schemas (OUTPUT only) — projections of the
    # canonical Sales schema. Dtypes come from that single source.
    #   - StoreKey and EmployeeKey are ORDER-level (header)
    #   - Detail remains line-level for product/pricing/shipping facts
    # Header follows the canonical projection order. Detail keeps its
    # established output column order (the price-trio order differs from the SQL
    # projection — a pre-existing layout preserved here so the OrderDetail
    # output is byte-identical).
    # ---------------------------------------------------------------------
    header_fields = _fields_from_logical(
        get_sales_order_header_schema(), order_id_int64=order_id_int64
    )
    header_schema = pa.schema(header_fields + delta_fields) if is_delta else pa.schema(header_fields)

    _detail_specs = {name: spec for name, spec in get_sales_order_detail_schema()}
    _detail_order = (
        "OrderNumber", "OrderLineNumber", "ProductKey", "DueDate", "DeliveryDate",
        "Quantity", "NetPrice", "UnitCost", "UnitPrice", "DiscountAmount", "DeliveryStatus",
    )
    detail_fields = [
        pa.field(name, _arrow_type(_detail_specs[name], name, order_id_int64))
        for name in _detail_order
    ]
    detail_schema = pa.schema(detail_fields + delta_fields) if is_delta else pa.schema(detail_fields)

    schema_by_table: dict[str, pa.Schema] = {
        TABLE_SALES: sales_schema_out,
        TABLE_SALES_ORDER_DETAIL: detail_schema,
        TABLE_SALES_ORDER_HEADER: header_schema,
    }

    # ---------------------------------------------------------------------
    # Returns schema — derived from the canonical RETURNS_SCHEMA in
    # returns_builder.py (single source of truth for column names & types).
    # ---------------------------------------------------------------------
    if returns_enabled:
        # OrderNumber dtype mirrors the sales schema. _returns_schema_for is
        # the single source for that field swap (shared with returns_builder).
        base_return_fields = list(_returns_schema_for(order_num_type))
        return_schema = pa.schema(base_return_fields + delta_fields) if is_delta else pa.schema(base_return_fields)
        schema_by_table[TABLE_SALES_RETURN] = return_schema

    # ---------------------------------------------------------------------
    # Date-column policy (must reference existing columns for each table)
    # ---------------------------------------------------------------------
    date_cols_by_table: dict[str, list[str]] = {
        TABLE_SALES: ["OrderDate", "DeliveryDate"],
        TABLE_SALES_ORDER_DETAIL: ["DueDate", "DeliveryDate"],  # OrderDate is order-level (header only)
        TABLE_SALES_ORDER_HEADER: ["OrderDate"],
    }
    if returns_enabled:
        date_cols_by_table[TABLE_SALES_RETURN] = ["ReturnDate"]

    # Optional models.yaml override (preserve previous behavior)
    if models_cfg and isinstance(models_cfg, Mapping):
        overrides = None
        returns_cfg = models_cfg.get("returns")
        if isinstance(returns_cfg, Mapping):
            overrides = returns_cfg.get("date_cols_by_table")
        if isinstance(overrides, Mapping):
            for k, v in overrides.items():
                if isinstance(k, str) and isinstance(v, (list, tuple)) and v:
                    date_cols_by_table[k] = [str(x) for x in v]

    # ---------------------------------------------------------------------
    # Parquet dictionary encoding policy
    # ---------------------------------------------------------------------
    pdx = set(parquet_dict_exclude) if parquet_dict_exclude else {"OrderNumber", "CustomerKey"}
    parquet_dict_cols_by_table = {t: schema_dict_cols(s, pdx) for t, s in schema_by_table.items()}
    parquet_dict_cols = parquet_dict_cols_by_table[TABLE_SALES]

    return WorkerSchemaBundle(
        sales_schema_gen=sales_schema_gen,
        sales_schema_out=sales_schema_out,
        schema_by_table=schema_by_table,
        date_cols_by_table=date_cols_by_table,
        parquet_dict_exclude=pdx,
        parquet_dict_cols_by_table=parquet_dict_cols_by_table,
        parquet_dict_cols=parquet_dict_cols,
        schema_no_order=schema_no_order,
        schema_with_order=schema_with_order,
        schema_no_order_delta=schema_no_order_delta,
        schema_with_order_delta=schema_with_order_delta,
    )


def build_worker_schemas_from_cfg(worker_cfg: Mapping) -> WorkerSchemaBundle:
    """Derive the schema bundle from a ``worker_cfg`` dict.

    This is the single source of truth for turning a ``worker_cfg`` into the
    ``WorkerSchemaBundle``: it replicates exactly the parameter extraction that
    ``init_sales_worker`` performs (including the ``sales_output`` -> forced
    ``skip_order_cols=False`` rule) before delegating to ``build_worker_schemas``.
    Both the per-worker init and the coordinator's Delta assembly call this, so
    the schema the workers write parts with and the schema the Delta commit
    adopts can never drift.
    """
    op = worker_cfg.get("output_paths") or {}
    file_format = worker_cfg.get("file_format") or (
        op.get("file_format") if isinstance(op, Mapping) else None
    )

    sales_output = str(worker_cfg.get("sales_output") or "sales").lower()
    skip_order_cols = bool(worker_cfg.get("skip_order_cols", False))
    # skip_order_cols_requested is resolved BEFORE the sales_output override,
    # mirroring init_sales_worker.
    skip_order_cols_requested = bool(
        worker_cfg.get("skip_order_cols_requested", skip_order_cols)
    )
    if sales_output in {"sales_order", "both"}:
        skip_order_cols = False

    returns_enabled = bool(worker_cfg.get("returns_enabled", False))
    order_id_int64 = bool(worker_cfg.get("order_id_int64", False))
    partition_cols = worker_cfg.get("partition_cols") or []
    models_cfg = worker_cfg.get("models_cfg")
    parquet_dict_exclude = worker_cfg.get("parquet_dict_exclude")

    return build_worker_schemas(
        file_format=file_format,
        skip_order_cols=skip_order_cols,
        skip_order_cols_requested=skip_order_cols_requested,
        returns_enabled=returns_enabled,
        parquet_dict_exclude=set(parquet_dict_exclude) if parquet_dict_exclude else None,
        models_cfg=models_cfg if isinstance(models_cfg, Mapping) else None,
        order_id_int64=order_id_int64,
        partition_cols=partition_cols if partition_cols else None,
    )
