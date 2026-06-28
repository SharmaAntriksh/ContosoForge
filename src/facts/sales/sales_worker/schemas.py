from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set

import pyarrow as pa

from ..output_paths import TABLE_SALES, TABLE_SALES_ORDER_DETAIL, TABLE_SALES_ORDER_HEADER, TABLE_SALES_RETURN
from .returns_builder import _returns_schema_for


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
    # GEN: what the chunk builder actually produces (NO TimeKey here)
    base_fields_gen = [
        pa.field("CustomerKey", pa.int32()),
        pa.field("ProductKey", pa.int32()),
        pa.field("StoreKey", pa.int32()),
        pa.field("EmployeeKey", pa.int32()),
        pa.field("PromotionKey", pa.int32()),
        pa.field("CurrencyKey", pa.int32()),
        pa.field("OrderDate", pa.date32()),
        pa.field("DueDate", pa.date32()),
        pa.field("DeliveryDate", pa.date32()),
        pa.field("Quantity", pa.int32()),
        pa.field("UnitPrice", pa.float64()),
        pa.field("NetPrice", pa.float64()),
        pa.field("UnitCost", pa.float64()),
        pa.field("DiscountAmount", pa.float64()),
        pa.field("DeliveryStatus", pa.string()),
        pa.field("IsOrderDelayed", pa.bool_()),
    ]

    # OUT: what we want to write/merge/project (TimeKey injected later in task.py)
    base_fields_out = [
        pa.field("CustomerKey", pa.int32()),
        pa.field("ProductKey", pa.int32()),
        pa.field("StoreKey", pa.int32()),
        pa.field("EmployeeKey", pa.int32()),
        pa.field("PromotionKey", pa.int32()),
        pa.field("CurrencyKey", pa.int32()),
        pa.field("ChannelKey", pa.int32()),
        pa.field("TimeKey", pa.int32()),   # injected later; OUTPUT schema expects it
        pa.field("OrderDate", pa.date32()),
        pa.field("DueDate", pa.date32()),
        pa.field("DeliveryDate", pa.date32()),
        pa.field("Quantity", pa.int32()),
        pa.field("UnitPrice", pa.float64()),
        pa.field("NetPrice", pa.float64()),
        pa.field("UnitCost", pa.float64()),
        pa.field("DiscountAmount", pa.float64()),
        pa.field("DeliveryStatus", pa.string()),
        pa.field("IsOrderDelayed", pa.bool_()),
    ]

    # Promote OrderNumber to int64 when the day-based ID space would exceed
    # int32. `order_id_int64` (computed from the real ~8x-total_rows ID ceiling in
    # sales.py) is the single authoritative signal.
    order_num_type = pa.int64() if order_id_int64 else pa.int32()
    order_fields = [
        pa.field("OrderNumber", order_num_type),
        pa.field("OrderLineNumber", pa.int32()),
    ]
    if partition_cols:
        delta_fields = [_ALL_DELTA_FIELDS[c] for c in partition_cols if c in _ALL_DELTA_FIELDS]
    else:
        delta_fields = list(_ALL_DELTA_FIELDS.values())

    # GEN variants
    schema_no_order_gen = pa.schema(base_fields_gen)
    schema_with_order_gen = pa.schema(order_fields + base_fields_gen)
    schema_no_order_delta_gen = pa.schema(base_fields_gen + delta_fields)
    schema_with_order_delta_gen = pa.schema(order_fields + base_fields_gen + delta_fields)

    # OUT variants
    schema_no_order_out = pa.schema(base_fields_out)
    schema_with_order_out = pa.schema(order_fields + base_fields_out)
    schema_no_order_delta_out = pa.schema(base_fields_out + delta_fields)
    schema_with_order_delta_out = pa.schema(order_fields + base_fields_out + delta_fields)

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
    # OrderDetail / OrderHeader schemas (OUTPUT only)
    # Agreement:
    #   - StoreKey and EmployeeKey are ORDER-level (header)
    #   - Detail remains line-level for product/pricing/shipping facts
    # ---------------------------------------------------------------------
    detail_fields = [
        pa.field("OrderNumber", order_num_type),
        pa.field("OrderLineNumber", pa.int32()),
        pa.field("ProductKey", pa.int32()),
        pa.field("DueDate", pa.date32()),
        pa.field("DeliveryDate", pa.date32()),
        pa.field("Quantity", pa.int32()),
        pa.field("NetPrice", pa.float64()),
        pa.field("UnitCost", pa.float64()),
        pa.field("UnitPrice", pa.float64()),
        pa.field("DiscountAmount", pa.float64()),
        pa.field("DeliveryStatus", pa.string()),
    ]
    detail_schema = pa.schema(detail_fields + delta_fields) if is_delta else pa.schema(detail_fields)

    header_fields = [
        pa.field("OrderNumber", order_num_type),
        pa.field("CustomerKey", pa.int32()),
        pa.field("StoreKey", pa.int32()),
        pa.field("EmployeeKey", pa.int32()),
        pa.field("PromotionKey", pa.int32()),
        pa.field("CurrencyKey", pa.int32()),
        pa.field("ChannelKey", pa.int32()),
        pa.field("OrderDate", pa.date32()),
        pa.field("TimeKey", pa.int32()),
        pa.field("IsOrderDelayed", pa.bool_()),
    ]
    header_schema = pa.schema(header_fields + delta_fields) if is_delta else pa.schema(header_fields)

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
