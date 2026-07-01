from __future__ import annotations

import re as _re
from pathlib import Path
from typing import Mapping, Sequence, Tuple

from src.utils.static_schemas import (
    STATIC_SCHEMAS,
    get_dates_schema,
    get_sales_schema,
    order_id_int64_for_rows,
    promote_order_number,
)
from src.utils.logging_utils import work
from src.tools.sql.dialect import ColumnSpec, DEFAULT_DIALECT, Dialect
from src.tools.sql.sql_helpers import (
    returns_enabled as _returns_enabled,
    budget_enabled as _budget_enabled,
    inventory_enabled as _inventory_enabled,
    complaints_enabled as _complaints_enabled,
    wishlists_enabled as _wishlists_enabled,
)

Schema = Sequence[Tuple[str, ColumnSpec]]

# Fact table names (SQL tables should remain PascalCase)
TABLE_SALES = "Sales"
TABLE_SALES_ORDER_HEADER = "OrderHeader"
TABLE_SALES_ORDER_DETAIL = "OrderDetail"
TABLE_SALES_RETURN = "Returns"

# Budget fact table names
TABLE_BUDGET_YEARLY = "BudgetYearly"
TABLE_BUDGET_MONTHLY = "BudgetMonthly"
TABLE_INVENTORY_SNAPSHOT = "InventorySnapshot"
TABLE_COMPLAINTS = "Complaints"
TABLE_WISHLISTS = "CustomerWishlists"

# Tables that should be emitted in the Facts script (not Dimensions),
# even though they live inside STATIC_SCHEMAS.
_FACT_TABLE_NAMES = {
    TABLE_SALES,
    TABLE_SALES_ORDER_HEADER,
    TABLE_SALES_ORDER_DETAIL,
    TABLE_SALES_RETURN,
    TABLE_BUDGET_YEARLY,
    TABLE_BUDGET_MONTHLY,
    TABLE_INVENTORY_SNAPSHOT,
    TABLE_COMPLAINTS,
    TABLE_WISHLISTS,
}

_SAFE_IDENT_RE = _re.compile(r'^[A-Za-z_][A-Za-z0-9_ ]*$')


def _validate_sql_identifier(name: str, label: str = "identifier") -> None:
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"Unsafe SQL {label}: {name!r}")




def _sales_output_mode(cfg: Mapping) -> str:
    sales_cfg = getattr(cfg, "sales", None)
    mode = str(getattr(sales_cfg, "sales_output", "sales") if sales_cfg else "sales").lower().strip()
    if mode not in {"sales", "sales_order", "both"}:
        raise ValueError(f"Invalid sales.sales_output: {mode!r}. Expected sales|sales_order|both.")
    return mode


def _skip_order_cols(cfg: Mapping, default: bool) -> bool:
    sales_cfg = getattr(cfg, "sales", None)
    return bool(getattr(sales_cfg, "skip_order_cols", default) if sales_cfg else default)


def _require_static_schema(table_name: str) -> Schema:
    try:
        return STATIC_SCHEMAS[table_name]
    except KeyError as e:
        raise KeyError(
            f"STATIC_SCHEMAS is missing a schema for '{table_name}'. "
            f"Add it to src/utils/static_schemas.py."
        ) from e


def create_table_from_schema(
    table_name: str,
    cols: Schema,
    *,
    schema: str | None = None,
    drop_existing: bool = True,
    include_batch_separator: bool = True,
    dialect: Dialect = DEFAULT_DIALECT,
) -> str:
    if schema is None:
        schema = dialect.default_schema
    _validate_sql_identifier(table_name, "table name")
    _validate_sql_identifier(schema, "schema name")
    if not cols:
        raise ValueError(f"Cannot generate CREATE TABLE for {table_name!r}: no columns.")
    fq_table = dialect.qualify(schema, table_name)

    emit_separator = include_batch_separator and bool(dialect.batch_separator)

    lines: list[str] = []
    if drop_existing:
        lines.append(dialect.drop_table_if_exists(schema, table_name))
        if emit_separator:
            lines.append(dialect.batch_separator)

    lines.append(f"CREATE TABLE {fq_table} (")
    for col, dtype in cols:
        lines.append(f"    {dialect.quote_ident(col)} {dialect.render_type(dtype)},")
    if lines[-1].endswith(","):
        lines[-1] = lines[-1].rstrip(",")
    lines.append(");")

    if emit_separator:
        lines.append(dialect.batch_separator)

    return "\n".join(lines)


def generate_all_create_tables(
    output_folder,
    cfg,
    skip_order_cols: bool = False,
    *,
    schema: str | None = None,
    drop_existing: bool = True,
    dialect: Dialect = DEFAULT_DIALECT,
    order_id_int64: bool | None = None,
):
    """
    Writes:
      <output_folder>/schema/01_create_dimensions.sql
      <output_folder>/schema/02_create_facts.sql

    Schemas are sourced from:
      - STATIC_SCHEMAS for everything except Sales and Dates
      - get_sales_schema(skip_order_cols) for Sales
      - get_dates_schema(cfg['dates']) for Dates
    """
    if schema is None:
        schema = dialect.default_schema

    def _emit(table: str, cols: Schema) -> str:
        return create_table_from_schema(
            table,
            cols,
            schema=schema,
            drop_existing=drop_existing,
            include_batch_separator=True,
            dialect=dialect,
        )

    schema_dir = Path(output_folder) / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)

    dim_out_path = schema_dir / "01_create_dimensions.sql"
    fact_out_path = schema_dir / "02_create_facts.sql"

    # No generation timestamp in the header: it would make otherwise-identical
    # scripts differ byte-for-byte between runs. Provenance lives in the
    # timestamped run folder instead.
    header = [
        "-- Auto-generated CREATE TABLE scripts",
        *dialect.script_preamble,
        "",
    ]

    skip_tables: set[str] = {"CustomerSegment", "CustomerSegmentMembership"}
    sub_cfg = getattr(cfg, "subscriptions", None) or {}
    if isinstance(sub_cfg, Mapping):
        if not bool(getattr(sub_cfg, "enabled", True)):
            skip_tables.add("Plans")
            skip_tables.add("CustomerSubscriptions")
        elif not bool(getattr(sub_cfg, "generate_bridge", True)):
            skip_tables.add("CustomerSubscriptions")

    if not _returns_enabled(cfg):
        skip_tables.add("ReturnReason")

    # Dimensions
    dim_scripts: list[str] = []
    for table_name in sorted(STATIC_SCHEMAS.keys()):
        if table_name in _FACT_TABLE_NAMES:
            continue
        if table_name in skip_tables:
            continue

        if table_name == "Dates":
            dates_cfg = getattr(cfg, "dates", None)
            if dates_cfg is None:
                raise KeyError("cfg.dates is required to generate Dates schema.")
            cols = get_dates_schema(dates_cfg)
        else:
            cols = STATIC_SCHEMAS[table_name]

        dim_scripts.append(_emit(table_name, cols))

    dim_out_path.write_text("\n".join(header) + "\n\n" + "\n\n".join(dim_scripts) + "\n", encoding="utf-8")

    # Facts
    mode = _sales_output_mode(cfg)
    include_sales = mode in {"sales", "both"}
    include_sales_order = mode in {"sales_order", "both"}
    include_returns = _returns_enabled(cfg)

    eff_skip_order_cols = _skip_order_cols(cfg, skip_order_cols)
    # int64 OrderNumber decision, shared across all fact tables that carry the
    # column so the DDL stays in sync with the parquet/generation side. Prefer
    # the authoritative per-run flag (computed once from the real emitted id
    # ceiling and threaded in via ``order_id_int64``); fall back to an
    # id-space-aware estimate from total_rows when generating DDL with no run.
    sales_total_rows = int(getattr(getattr(cfg, "sales", None), "total_rows", 0) or 0)
    eff_int64 = (
        order_id_int64 if order_id_int64 is not None
        else order_id_int64_for_rows(sales_total_rows)
    )

    fact_scripts: list[str] = []

    if include_sales:
        fact_scripts.append(
            _emit(TABLE_SALES, get_sales_schema(
                eff_skip_order_cols, total_rows=sales_total_rows, force_int64=eff_int64))
        )

    if include_sales_order:
        fact_scripts.append(_emit(
            TABLE_SALES_ORDER_HEADER,
            promote_order_number(_require_static_schema(TABLE_SALES_ORDER_HEADER),
                                 sales_total_rows, force_int64=eff_int64),
        ))
        fact_scripts.append(_emit(
            TABLE_SALES_ORDER_DETAIL,
            promote_order_number(_require_static_schema(TABLE_SALES_ORDER_DETAIL),
                                 sales_total_rows, force_int64=eff_int64),
        ))

    # Returns placement:
    # - If sales_order tables exist: emit after detail
    # - Else: emit after Sales (if present) or as the only fact
    if include_returns and (include_sales or include_sales_order):
        fact_scripts.append(_emit(
            TABLE_SALES_RETURN,
            promote_order_number(_require_static_schema(TABLE_SALES_RETURN),
                                 sales_total_rows, force_int64=eff_int64),
        ))

    # Budget tables (conditional on budget.enabled)
    if _budget_enabled(cfg):
        for budget_table in (TABLE_BUDGET_YEARLY, TABLE_BUDGET_MONTHLY):
            fact_scripts.append(_emit(budget_table, _require_static_schema(budget_table)))

    # Inventory snapshot (conditional on inventory.enabled)
    if _inventory_enabled(cfg):
        fact_scripts.append(_emit(TABLE_INVENTORY_SNAPSHOT, _require_static_schema(TABLE_INVENTORY_SNAPSHOT)))

    # Complaints (conditional on complaints.enabled)
    if _complaints_enabled(cfg):
        fact_scripts.append(_emit(
            TABLE_COMPLAINTS,
            promote_order_number(_require_static_schema(TABLE_COMPLAINTS),
                                 sales_total_rows, force_int64=eff_int64),
        ))

    # Customer Wishlists (conditional on wishlists.enabled)
    if _wishlists_enabled(cfg):
        fact_scripts.append(_emit(TABLE_WISHLISTS, _require_static_schema(TABLE_WISHLISTS)))

    fact_out_path.write_text("\n".join(header) + "\n\n" + "\n\n".join(fact_scripts) + "\n", encoding="utf-8")

    work(f"Created {dim_out_path.name}")
    work(f"Created {fact_out_path.name}")
    return dim_out_path, fact_out_path
