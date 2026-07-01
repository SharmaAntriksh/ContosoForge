"""Guardrail — the Sales fact schema is declared in two places that must agree.

Finding #15: the Sales fact's columns/dtypes are hand-maintained independently as
(a) a pyarrow field list in ``sales_worker/schemas.py`` (what the parquet/delta
output actually is) and (b) a SQL-typed tuple ``_SALES_SCHEMA`` in
``static_schemas.py`` (what the generated CREATE TABLE / BULK INSERT targets).
Both docstrings call themselves "the single source of truth", yet nothing enforces
that they agree — so a column added, removed, reordered, or retyped in one but not
the other ships silently and breaks SQL import.

(The worker-config ``TypedDict`` is a separate concern: per the review it describes
the worker's *input* array contract, not the Sales *output* column set, so it is not
a third copy of this list and is not compared here.)

These are fast pure-unit assertions — no data generation.

* **Columns + dtype families agree** — same names, same order, and the same
  semantic family (integer / numeric / date / bool / string) for every column, for
  both ``skip_order_cols`` settings. This catches the high-impact drift (add / drop /
  reorder / change-of-kind). (passes today)
* **Exact dtypes agree** — each Arrow field type equals the canonical Arrow type of
  its SQL column type. Holds by construction: the Arrow Sales schema in
  ``sales_worker/schemas.py`` is now *projected* from the single canonical
  ``(name, ColumnSpec)`` schema in ``static_schemas.py``. ``ChannelKey`` / ``TimeKey``
  are canonically ``SMALLINT`` (int16) — matching the SMALLINT dimension keys their
  SQL foreign keys reference — and the writer casts the produced int32 arrays down to
  int16, so the parquet dtype and SQL DDL agree. This is a hard regression guard.
"""
from __future__ import annotations

import pytest

pa = pytest.importorskip("pyarrow")

from src.tools.sql.dialect import SqlType
from src.utils.static_schemas import get_sales_schema
from src.facts.sales.sales_worker.schemas import build_worker_schemas

# Canonical SQL-type -> exact Arrow-type mapping (the intended bridge; mirrors
# globals._build_sql_to_pa_map but with BIT -> bool, which that map omits).
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


def _family(t: pa.DataType) -> str:
    """Collapse an Arrow type to a semantic family for kind-level comparison."""
    if pa.types.is_integer(t):
        return "int"
    if pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "num"
    if pa.types.is_date(t) or pa.types.is_timestamp(t):
        return "date"
    if pa.types.is_boolean(t):
        return "bool"
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "str"
    return str(t)


def _worker_sales_schema(skip_order_cols: bool) -> pa.Schema:
    """The Arrow schema the parquet writer actually emits for the Sales fact."""
    return build_worker_schemas(
        file_format="parquet",
        skip_order_cols=skip_order_cols,
        skip_order_cols_requested=skip_order_cols,
        returns_enabled=False,
        order_id_int64=False,   # match get_sales_schema(total_rows=0), which keeps INT
    ).sales_schema_out


def _sql_sales_columns(skip_order_cols: bool) -> list[tuple[str, object]]:
    """The SQL schema as (name, SqlType) pairs."""
    return [(name, spec.sql_type) for name, spec in get_sales_schema(skip_order_cols)]


@pytest.mark.parametrize("skip_order_cols", [False, True])
def test_sales_arrow_and_sql_columns_and_families_agree(skip_order_cols):
    """Same column names, same order, and the same semantic dtype family."""
    arrow = _worker_sales_schema(skip_order_cols)
    sql = _sql_sales_columns(skip_order_cols)

    assert arrow.names == [name for name, _ in sql], (
        "Arrow (sales_worker/schemas.py) and SQL (static_schemas.py) disagree on "
        f"Sales column names/order (skip_order_cols={skip_order_cols}).\n"
        f"  arrow: {arrow.names}\n"
        f"  sql  : {[name for name, _ in sql]}"
    )

    mismatches = []
    for name, sqltype in sql:
        want = _family(_SQL_TO_ARROW[sqltype])
        got = _family(arrow.field(name).type)
        if want != got:
            mismatches.append(f"{name}: sql={want} arrow={got}")
    assert not mismatches, (
        "Arrow and SQL disagree on the semantic dtype family of Sales columns "
        f"(skip_order_cols={skip_order_cols}): {mismatches}"
    )


@pytest.mark.parametrize("skip_order_cols", [False, True])
def test_sales_arrow_and_sql_exact_dtypes_agree(skip_order_cols):
    """Each Arrow field type equals the canonical Arrow type of its SQL column type."""
    arrow = _worker_sales_schema(skip_order_cols)
    sql = _sql_sales_columns(skip_order_cols)

    mismatches = []
    for name, sqltype in sql:
        want = _SQL_TO_ARROW[sqltype]
        got = arrow.field(name).type
        if want != got:
            mismatches.append(f"{name}: sql->{want} arrow->{got}")
    assert not mismatches, (
        f"Arrow/SQL exact dtype drift (skip_order_cols={skip_order_cols}): {mismatches}"
    )
