from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional, Set

from src.tools.sql.dialect import DEFAULT_DIALECT, Dialect
from src.utils.logging_utils import work, skip
from src.tools.sql.sql_helpers import (
    sql_escape_literal as _sql_escape_literal,
    quote_ident as _quote_ident,
    returns_enabled as _returns_enabled_from_cfg,
    budget_enabled as _budget_enabled_from_cfg,
    inventory_enabled as _inventory_enabled_from_cfg,
    complaints_enabled as _complaints_enabled_from_cfg,
    wishlists_enabled as _wishlists_enabled_from_cfg,
)


def _split_qualified(name: str) -> tuple[str, str]:
    """Split a "schema.table" reference into (schema, table). Returns ("", table) if unqualified.

    Handles already-bracketed/quoted identifiers by stripping a single layer
    of wrappers (the dialect re-quotes internally).
    """
    parts = [p.strip() for p in str(name).strip().split(".") if p.strip()]
    if not parts:
        raise ValueError("Empty table name.")
    if len(parts) > 2:
        raise ValueError(f"Too many name parts: {name!r}")
    parts = [Dialect._strip_ident_wrappers(p) for p in parts]
    return (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])


# -----------------------------
# Table inference
# -----------------------------

_CHUNK_SUFFIX_RE = re.compile(r"(?:_chunk\d+|_part\d+)$", flags=re.IGNORECASE)

# facts/<folder>/*.csv -> canonical table
_FOLDER_TABLE_ALIASES: dict[str, str] = {
    # facts
    "sales": "Sales",
    "order_header": "OrderHeader",
    "order_detail": "OrderDetail",

    # returns
    "returns": "Returns",

    # budget (files live under facts/budget/ with individual filenames)
    "budget_yearly": "BudgetYearly",
    "budget_monthly": "BudgetMonthly",

    # inventory (files live under facts/inventory/)
    "inventory_snapshot": "InventorySnapshot",
    "inventory": "InventorySnapshot",

    # complaints (files live under facts/complaints/)
    "complaints": "Complaints",

    # wishlists (files live under facts/customer_wishlists/)
    "customer_wishlists": "CustomerWishlists",
    "wishlists": "CustomerWishlists",
}

def _infer_table_from_filename(csv_file: str) -> str:
    """
    Infer PascalCase table name from a chunked snake_case filename.
    Examples:
      sales_chunk0001.csv              -> Sales
      order_detail_chunk0001.csv -> OrderDetail
    """
    base = Path(csv_file).stem
    base = _CHUNK_SUFFIX_RE.sub("", base)
    return base.replace("_", " ").title().replace(" ", "")


def _allowed_lookup(allowed_tables: Optional[Set[str]]) -> Optional[dict[str, str]]:
    """Map lower(table) -> canonical(table) for case-insensitive membership checks."""
    if not allowed_tables:
        return None
    return {str(t).strip().lower(): str(t).strip() for t in allowed_tables}


def _pick_target_table(csv_path: Path, *, allowed_tables: Optional[Set[str]]) -> Optional[str]:
    """
    Pick target table for a CSV file.
    - Prefer folder mapping (facts/<folder>/file.csv)
    - Fallback to filename inference
    - Apply allowed_tables filter (case-insensitive)
    """
    allowed_map = _allowed_lookup(allowed_tables)

    folder_key = csv_path.parent.name.strip().lower()
    candidate = _FOLDER_TABLE_ALIASES.get(folder_key) or _infer_table_from_filename(csv_path.name)

    if allowed_map is None:
        return candidate

    return allowed_map.get(candidate.strip().lower())


def _allowed_fact_tables_from_cfg(cfg: Optional[Mapping]) -> Optional[Set[str]]:
    """
    Allowed fact tables for facts bulk insert.
    - sales.sales_output drives Sales vs OrderHeader/Detail
    - returns flags (above) controls Returns
    - budget.enabled controls BudgetYearly/BudgetMonthly
    - inventory.enabled controls InventorySnapshot
    - complaints.enabled controls Complaints
    """
    if cfg is None:
        return None

    sales_cfg = getattr(cfg, "sales", None)
    mode = str(getattr(sales_cfg, "sales_output", "sales") if sales_cfg else "sales").lower().strip()
    if mode not in {"sales", "sales_order", "both"}:
        raise ValueError(f"Invalid sales.sales_output: {mode!r}. Expected sales|sales_order|both.")

    allowed: set[str] = set()
    if mode in {"sales", "both"}:
        allowed.add("Sales")
    if mode in {"sales_order", "both"}:
        allowed.add("OrderHeader")
        allowed.add("OrderDetail")

    if _returns_enabled_from_cfg(cfg):
        allowed.add("Returns")

    if _budget_enabled_from_cfg(cfg):
        allowed.add("BudgetYearly")
        allowed.add("BudgetMonthly")

    if _inventory_enabled_from_cfg(cfg):
        allowed.add("InventorySnapshot")

    if _complaints_enabled_from_cfg(cfg):
        allowed.add("Complaints")

    if _wishlists_enabled_from_cfg(cfg):
        allowed.add("CustomerWishlists")

    return allowed


# -----------------------------
# Script generator
# -----------------------------

def _iter_csv_files(folder: Path, *, recursive: bool) -> Iterable[Path]:
    if recursive:
        yield from sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".csv")
    else:
        yield from sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".csv")


def generate_bulk_insert_script(
    csv_folder,
    *,
    output_sql_file: str = "bulk_insert.sql",
    table_name: Optional[str] = None,
    csv_format_tables: Optional[Set[str]] = None,
    force_csv_format: bool = False,
    recursive: bool = False,
    allowed_tables: Optional[Set[str]] = None,
    dialect: Dialect = DEFAULT_DIALECT,
) -> Optional[str]:
    """
    Generate a bulk-load script for the active dialect.
    - If ``table_name`` is None, the table is inferred per file from folder + filename.
    - ``recursive=True`` is required for the ``facts/<table>/*.csv`` layout.
    - ``allowed_tables`` filters inferred tables (case-insensitive).
    - ``csv_format_tables`` flags tables whose string columns may contain
      embedded commas/quotes (SQL Server switches to ``FORMAT='CSV'``,
      Postgres ignores it).
    - ``force_csv_format=True`` flags every emitted table (sentinel for
      "all tables in this folder need CSV-aware parsing").
    """
    csv_folder = Path(csv_folder)
    if not csv_folder.exists() or not csv_folder.is_dir():
        skip(f"CSV folder not found or not a directory: {csv_folder}")
        return None

    target_sql = Path(output_sql_file)
    target_sql.parent.mkdir(parents=True, exist_ok=True)

    csv_paths = list(_iter_csv_files(csv_folder, recursive=recursive))
    if not csv_paths:
        skip(f"No CSV files found in {csv_folder}. Skipping load script.")
        return None

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    banner = dialect.load_script_kind.replace("_", " ").upper()
    header: list[str] = [
        f"-- Auto-generated {banner} script",
        f"-- Generated on: {ts}",
    ]
    if dialect.load_script_note:
        header.append(dialect.load_script_note)
    header.extend(dialect.script_preamble)
    header.append("")

    allowed_map = _allowed_lookup(allowed_tables)
    csv_format_lower = {t.lower() for t in (csv_format_tables or set())}

    lines: list[str] = list(header)
    emitted = 0

    for csv_path in csv_paths:
        if table_name is not None:
            tgt = table_name
        else:
            tgt = _pick_target_table(csv_path, allowed_tables=allowed_tables)
            if tgt is None:
                continue

        if allowed_map is not None and table_name is not None:
            if table_name.strip().lower() not in allowed_map:
                continue

        schema, table = _split_qualified(tgt)
        if not schema and dialect.qualify_load_target:
            schema = dialect.default_schema
        rel_hint = str(csv_path.relative_to(csv_folder)) if recursive else csv_path.name

        statement = dialect.bulk_load_statement(
            schema=schema,
            table=table,
            csv_path=csv_path,
            use_csv_format=force_csv_format or tgt.lower() in csv_format_lower,
        )

        lines.append(f"-- Source file: {rel_hint}")
        lines.append(statement)
        lines.append("")
        emitted += 1

    if emitted == 0:
        skip(f"No matching CSV files to emit (allowed_tables filter may have excluded all). Folder: {csv_folder}")
        return None

    target_sql.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    work(f"Wrote load script: {target_sql.name}")
    return str(target_sql)


def generate_dims_and_facts_bulk_insert_scripts(
    *,
    dims_folder,
    facts_folder,
    cfg,
    load_output_folder,
    dialect: Dialect = DEFAULT_DIALECT,
    allowed_fact_tables: Optional[Set[str]] = None,
) -> tuple[str, str]:
    """Write the dialect's load scripts to ``load_output_folder``.

    Always emits exactly two files, named per the active dialect:
      01_<load_script_kind>_dims.sql
      02_<load_script_kind>_facts.sql

    dims is a flat folder; facts is scanned recursively for the
    ``facts/<table>/*.csv`` layout. ``allowed_fact_tables`` may be
    pre-computed by the caller (recommended when this function is invoked
    once per dialect) — if omitted, it is derived from ``cfg``.

    For SQL Server, all dims and the Budget*/Complaints fact tables need
    ``FORMAT='CSV'`` because their string columns may contain embedded
    commas. Sales/Returns stay on the legacy fast path. Postgres
    ignores the flag — ``COPY ... FORMAT csv`` is always CSV-aware.
    """
    load_output_folder = Path(load_output_folder)
    load_output_folder.mkdir(parents=True, exist_ok=True)

    kind = dialect.load_script_kind
    dims_sql = load_output_folder / f"01_{kind}_dims.sql"
    facts_sql = load_output_folder / f"02_{kind}_facts.sql"

    # Optional load-window recovery management (SQL Server): a prepare script
    # (switch to BULK_LOGGED, optionally pre-grow the log) and a finish script
    # (restore the recovery model). Dialects that don't need it return None.
    prepare_text = dialect.prepare_load_script()
    if prepare_text:
        prepare_sql = load_output_folder / f"00_{kind}_prepare_load.sql"
        prepare_sql.write_text(prepare_text.rstrip() + "\n", encoding="utf-8")
        work(f"Wrote load script: {prepare_sql.name}")
    finish_text = dialect.finish_load_script()
    if finish_text:
        finish_sql = load_output_folder / f"99_{kind}_finish_load.sql"
        finish_sql.write_text(finish_text.rstrip() + "\n", encoding="utf-8")
        work(f"Wrote load script: {finish_sql.name}")

    generate_bulk_insert_script(
        dims_folder,
        output_sql_file=str(dims_sql),
        table_name=None,
        force_csv_format=True,
        recursive=False,
        dialect=dialect,
    )

    if allowed_fact_tables is None:
        allowed_fact_tables = _allowed_fact_tables_from_cfg(cfg)

    fact_csv_tables: set[str] = set()
    if _budget_enabled_from_cfg(cfg):
        fact_csv_tables.add("BudgetYearly")
        fact_csv_tables.add("BudgetMonthly")
    if _complaints_enabled_from_cfg(cfg):
        fact_csv_tables.add("Complaints")

    generate_bulk_insert_script(
        facts_folder,
        output_sql_file=str(facts_sql),
        table_name=None,
        csv_format_tables=fact_csv_tables or None,
        recursive=True,
        allowed_tables=allowed_fact_tables,
        dialect=dialect,
    )

    return str(dims_sql), str(facts_sql)
