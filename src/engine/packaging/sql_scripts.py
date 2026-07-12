from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from src.exceptions import ConfigError, PackagingError
from src.tools.sql.dialect import DEFAULT_DIALECT, REGISTRY
from src.tools.sql.generate_bulk_insert_sql import (
    _allowed_fact_tables_from_cfg,
    generate_dims_and_facts_bulk_insert_scripts,
)
from src.tools.sql.generate_create_table_scripts import generate_all_create_tables
from src.utils.logging_utils import stage, skip, done

if TYPE_CHECKING:
    from src.tools.sql.name_map import NameMap


# A schema name is embedded into bracketed ([x]) and double-quoted ("x")
# identifiers in generated SQL. Restrict it to a safe identifier so it cannot
# inject closing brackets/quotes (mirrors _validate_sql_identifier in the SQL
# tools layer). Default schemas ("dbo"/"public") pass trivially.
_SAFE_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ]*$")


def _validate_view_schema(name: str) -> str:
    """Reject view-schema names that aren't safe to embed in SQL identifiers."""
    if not _SAFE_SCHEMA_RE.match(name):
        raise ConfigError(
            f"Unsafe defaults.view_schema {name!r}: must start with a letter or "
            "underscore and contain only letters, digits, spaces, and underscores."
        )
    return name


# ------------------------------------------------------------
# Repo utilities
# ------------------------------------------------------------

def _find_repo_root(start: Path) -> Path:
    """
    Find repo root by walking parents and looking for both 'src' and 'scripts'.
    """
    for p in [start, *start.parents]:
        if (p / "src").is_dir() and (p / "scripts").is_dir():
            return p
    raise PackagingError(f"Could not find repo root (with 'src' and 'scripts' dirs) starting from {start}")


def _read_text(path: Path) -> str:
    """
    Simple reader for repo-owned SQL assets.
    We assume UTF-8; also accept UTF-8 BOM.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        # Last-resort fallback if a file was saved as UTF-16 by accident.
        return path.read_text(encoding="utf-16")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _sales_mode(sales_cfg) -> str:
    mode = str(getattr(sales_cfg, "sales_output", "") or "").strip().lower()
    return mode or "sales"


def _budget_enabled(cfg) -> bool:
    """Return True if budget generation is enabled in the top-level config."""
    if cfg is None:
        return False
    budget = getattr(cfg, "budget", None)
    if budget is None:
        return False
    return bool(getattr(budget, "enabled", False))


def _inventory_enabled(cfg) -> bool:
    """Return True if inventory snapshot generation is enabled in the top-level config."""
    if cfg is None:
        return False
    inv = getattr(cfg, "inventory", None)
    if inv is None:
        return False
    return bool(getattr(inv, "enabled", False))


# ------------------------------------------------------------
# Static SQL assets
# ------------------------------------------------------------

def copy_static_sql_assets(*, sql_root: Path) -> None:
    repo_root = _find_repo_root(Path(__file__).resolve())
    cci_src = repo_root / "scripts" / "sql" / "columnstore" / "create_drop_cci.sql"
    if not cci_src.exists():
        skip(f"CCI script not found; skipping: {cci_src}")
        return

    dst_dir = sql_root / DEFAULT_DIALECT.name / "indexes"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cci_src, dst_dir / "create_drop_cci.sql")
    done("Copied CCI script")


def _resolve_sqlserver_view_schema(cfg) -> str:
    """SQL Server view schema from cfg.defaults.view_schema (default ``dbo``)."""
    raw = str(getattr(getattr(cfg, "defaults", None), "view_schema", "dbo") or "dbo").strip()
    if raw.lower() != "dbo":
        _validate_view_schema(raw)
    return raw


def _write_generated_views(
    *, sql_root: Path, dialect, cfg, sales_cfg, view_schema: str,
    name_map: "NameMap | None",
) -> None:
    from src.tools.sql.view_sql import render_views_sql

    mode = _sales_mode(sales_cfg)
    skip_order_cols = bool(getattr(sales_cfg, "skip_order_cols", False))
    text = render_views_sql(
        dialect,
        mode=mode,
        skip_order_cols=skip_order_cols,
        view_schema=view_schema,
        name_map=name_map,
    )
    out_path = sql_root / dialect.name / "schema" / "04_create_views.sql"
    _write_text(out_path, text)
    schema_note = f" (schema: {view_schema})" if view_schema.lower() not in {"dbo", "public"} else ""
    done(f"Generated {dialect.name} views{schema_note}")


def copy_views_sql(
    *, sql_root: Path, cfg=None, sales_cfg=None, name_map: "NameMap | None" = None,
) -> None:
    """Generate <final>/sql/sqlserver/schema/04_create_views.sql.

    Emits pass-through dimension views plus the specialized fact views
    (``vw_Sales`` etc.) for the run's sales_output mode, routing identifiers
    through ``name_map``. A non-default ``defaults.view_schema`` relocates the
    views (dropping the ``vw_`` prefix) and adds a ``CREATE SCHEMA`` preamble.
    """
    _write_generated_views(
        sql_root=sql_root, dialect=REGISTRY["sqlserver"], cfg=cfg, sales_cfg=sales_cfg,
        view_schema=_resolve_sqlserver_view_schema(cfg), name_map=name_map,
    )


# ------------------------------------------------------------
# Constraints (mode-dependent)
#
# Constraints are generated from the declarative roster in
# ``src/tools/sql/schema_model.py`` (rendered by ``constraint_sql.py``) rather
# than composed from hand-written .sql files. One source per constraint lets the
# importers re-render in a chosen identifier case (the identifiers thread through
# the same ``name_map`` as the CREATE TABLE / load scripts) and cannot drift
# between the SQL Server and Postgres dialects.
# ------------------------------------------------------------

def _write_generated_constraints(
    *, sql_root: Path, dialect, sales_cfg, cfg, name_map: "NameMap | None"
) -> None:
    from src.tools.sql.constraint_sql import render_constraints_sql

    mode = _sales_mode(sales_cfg)
    text = render_constraints_sql(
        dialect,
        mode=mode,
        budget_enabled=_budget_enabled(cfg),
        inventory_enabled=_inventory_enabled(cfg),
        name_map=name_map,
    )
    out_path = sql_root / dialect.name / "schema" / "03_create_constraints.sql"
    _write_text(out_path, text)
    done(f"Generated {dialect.name} constraints")


def compose_constraints_sql(
    *, sql_root: Path, sales_cfg: dict, cfg: dict | None = None,
    name_map: "NameMap | None" = None,
) -> None:
    """Generate <final>/sql/sqlserver/schema/03_create_constraints.sql.

    Emits guarded, idempotent T-SQL for the config-gated constraint roster,
    routing identifiers through ``name_map`` (None = PascalCase default).
    """
    _write_generated_constraints(
        sql_root=sql_root, dialect=REGISTRY["sqlserver"], sales_cfg=sales_cfg,
        cfg=cfg, name_map=name_map,
    )


def _resolve_postgres_view_schema(cfg) -> str:
    """Treat ``dbo`` (SQL Server default) and empty as ``public``; pass others through."""
    raw = str(getattr(getattr(cfg, "defaults", None), "view_schema", "") or "").strip()
    if not raw or raw.lower() in {"dbo", "public"}:
        return "public"
    return _validate_view_schema(raw)


def compose_postgres_views_sql(
    *, sql_root: Path, cfg=None, sales_cfg=None, name_map: "NameMap | None" = None,
) -> None:
    """Generate ``<final>/sql/postgres/schema/04_create_views.sql``.

    The Postgres sibling of :func:`copy_views_sql`: same generated view roster,
    rendered through the Postgres dialect (DO-blocks, ``CREATE OR REPLACE VIEW``,
    ``NUMERIC(19,4)`` money casts). ``dbo``/empty resolve to ``public``.
    """
    _write_generated_views(
        sql_root=sql_root, dialect=REGISTRY["postgres"], cfg=cfg, sales_cfg=sales_cfg,
        view_schema=_resolve_postgres_view_schema(cfg), name_map=name_map,
    )


def compose_postgres_indexes_sql(
    *, sql_root: Path, name_map: "NameMap | None" = None
) -> None:
    """Generate ``<final>/sql/postgres/indexes/01_create_indexes.sql``.

    Emits guarded, idempotent ``CREATE INDEX IF NOT EXISTS`` from the
    declarative index roster (btree on FK columns + BRIN on naturally-ordered
    date columns), routing identifiers through ``name_map``. Applied by the
    importer post-load so the COPY phase isn't forced to update indexes per row.
    """
    from src.tools.sql.constraint_sql import render_indexes_sql

    text = render_indexes_sql(REGISTRY["postgres"], name_map=name_map)
    out_path = sql_root / "postgres" / "indexes" / "01_create_indexes.sql"
    _write_text(out_path, text)
    done("Generated Postgres indexes")


def copy_postgres_admin_sql(*, sql_root: Path) -> None:
    """Copy hand-written Postgres admin scripts to ``<final>/postgres/admin/``.

    Currently a single file (``create_pk_proc.sql``) that registers the
    ``admin.manage_primary_keys`` procedure used as a dev-tooling helper
    for drop/restore PK cycles. Applied by the importer alongside the
    main schema phase.
    """
    repo_root = _find_repo_root(Path(__file__).resolve())
    src_dir = repo_root / "scripts" / "sql" / "postgres" / "admin"
    if not src_dir.is_dir():
        skip(f"Postgres admin folder not found; skipping: {src_dir}")
        return

    parts = sorted(p for p in src_dir.glob("*.sql") if p.is_file())
    if not parts:
        skip(f"No Postgres admin scripts found in: {src_dir}")
        return

    out_dir = sql_root / "postgres" / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in parts:
        shutil.copy2(p, out_dir / p.name)
    done(f"Copied {len(parts)} Postgres admin script(s)")


def compose_postgres_constraints_sql(
    *, sql_root: Path, sales_cfg: dict, cfg: dict | None = None,
    name_map: "NameMap | None" = None,
) -> None:
    """Generate <final>/sql/postgres/schema/03_create_constraints.sql.

    The Postgres sibling of :func:`compose_constraints_sql`: same declarative
    roster and config gating, rendered through the Postgres dialect (DO-blocks,
    ``pg_constraint`` guards, BOOLEAN-flag CHECKs dropped, no ``NONCLUSTERED``).
    """
    _write_generated_constraints(
        sql_root=sql_root, dialect=REGISTRY["postgres"], sales_cfg=sales_cfg,
        cfg=cfg, name_map=name_map,
    )


# ------------------------------------------------------------
# Verification views (verify schema)
# ------------------------------------------------------------

def compose_verification_sql(*, sql_root: Path, name_map: "NameMap | None" = None) -> None:
    """
    Compose <final>/sql/schema/05_create_verify_schema.sql from all scripts
    in scripts/sql/bootstrap/verification/*.sql.

    Ordering is lexicographic (use numeric prefixes):
      00_create_verify_schema.sql   (schema + dispatcher proc)
      10_scd2_customers.sql         (additional view)
      ...

    New .sql files dropped into the verification folder are automatically
    included — no code changes needed.

    These are optional, read-only diagnostics with hardcoded PascalCase
    identifiers (they are not routed through the name map), so they are
    **skipped entirely for a non-default identifier case** — a snake_case
    schema would make every reference a broken PascalCase table name, and the
    diagnostics have no functional role in the import. ``name_map is None``
    means the default (PascalCase) case, where they are composed as before.
    """
    if name_map is not None:
        skip(
            "Verification diagnostics are PascalCase-only; skipped for "
            f"identifier_case={name_map.case.value}."
        )
        return

    repo_root = _find_repo_root(Path(__file__).resolve())
    verify_dir = repo_root / "scripts" / "sql" / "bootstrap" / "verification"

    if not verify_dir.exists() or not verify_dir.is_dir():
        skip(f"Verification folder not found; skipping: {verify_dir}")
        return

    parts = sorted(
        [p for p in verify_dir.glob("*.sql") if p.is_file()],
        key=lambda p: p.name.lower(),
    )

    if not parts:
        skip(f"No verification scripts found in: {verify_dir}")
        return

    dst = sql_root / DEFAULT_DIALECT.name / "schema" / "05_create_verify_schema.sql"

    chunks: list[str] = []
    chunks.append("-- Auto-generated by packaging: composed verification schema\n")
    chunks.append(f"-- source: {verify_dir.as_posix()}\n")
    chunks.append("SET NOCOUNT ON;\nGO\n")

    for p in parts:
        chunks.append("\n-- ============================================================\n")
        chunks.append(f"-- BEGIN {p.name}\n")
        chunks.append("-- ============================================================\n\n")
        chunks.append(_read_text(p).rstrip())
        chunks.append("\n\n-- ============================================================\n")
        chunks.append(f"-- END {p.name}\n")
        chunks.append("-- ============================================================\nGO\n")

    _write_text(dst, "".join(chunks))
    done(f"Composed verification schema from {len(parts)} file(s)")


# ------------------------------------------------------------
# SQL generation from packaged CSVs
# ------------------------------------------------------------

def write_create_table_scripts(
    *, dims_out: Path, facts_out: Path, sql_root: Path, cfg: dict,
    order_id_int64: bool | None = None,
    name_map: "NameMap | None" = None,
) -> None:
    """Emit CREATE TABLE scripts for every registered SQL dialect.

    Every dialect lands under the umbrella ``sql/`` root at
    ``sql/<dialect_name>/`` (e.g. ``sql/sqlserver/``, ``sql/postgres/``).
    The cost is trivial (~27 KB per extra dialect) and avoids forcing users
    to re-run the pipeline to switch DBMSes.

    ``order_id_int64`` is the authoritative per-run OrderNumber-width decision
    (from the sales run manifest); passed through so the DDL matches the parquet
    dtype exactly. ``None`` lets the generator fall back to a row-count estimate.
    """
    with stage("Generating CREATE TABLE Scripts"):
        dims_csv = list(dims_out.glob("*.csv"))
        facts_csv = list(facts_out.rglob("*.csv"))

        if not dims_csv and not facts_csv:
            skip("No CSV files found - skipping CREATE TABLE scripts.")
            return

        for dialect in REGISTRY.values():
            out_dir = sql_root / dialect.name
            generate_all_create_tables(
                output_folder=out_dir,
                cfg=cfg,
                dialect=dialect,
                order_id_int64=order_id_int64,
                name_map=name_map,
            )


def write_bulk_insert_scripts(
    *, dims_out: Path, facts_out: Path, sql_root: Path, cfg,
    name_map: "NameMap | None" = None,
) -> None:
    """Emit load scripts for every registered SQL dialect.

    Every dialect lands under the umbrella ``sql/`` root at
    ``sql/<dialect>/load/`` (e.g. ``sql/sqlserver/load/``,
    ``sql/postgres/load/``). The facts allowlist is computed once from the
    FULL cfg (sales_output + returns enablement) and reused across dialects.
    """
    with stage("Generating Load Scripts"):
        dims_csv = list(dims_out.glob("*.csv"))
        facts_csv = list(facts_out.rglob("*.csv"))

        if not dims_csv and not facts_csv:
            skip("No CSV files found - skipping load scripts.")
            return

        allowed_fact_tables = _allowed_fact_tables_from_cfg(cfg)
        for dialect in REGISTRY.values():
            load_root = sql_root / dialect.name / "load"
            generate_dims_and_facts_bulk_insert_scripts(
                dims_folder=str(dims_out),
                facts_folder=str(facts_out),
                cfg=cfg,
                load_output_folder=str(load_root),
                dialect=dialect,
                allowed_fact_tables=allowed_fact_tables,
                name_map=name_map,
            )
