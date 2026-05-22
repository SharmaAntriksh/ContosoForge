from __future__ import annotations

import shutil
from pathlib import Path

from src.tools.sql.dialect import DEFAULT_DIALECT, REGISTRY
from src.tools.sql.generate_bulk_insert_sql import generate_dims_and_facts_bulk_insert_scripts
from src.tools.sql.generate_create_table_scripts import generate_all_create_tables
from src.tools.sql.sql_helpers import sql_escape_literal
from src.utils.logging_utils import stage, skip, done


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
    raise RuntimeError(f"Could not find repo root (with 'src' and 'scripts' dirs) starting from {start}")


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

    dst_dir = sql_root / "indexes"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cci_src, dst_dir / "create_drop_cci.sql")
    done("Copied CCI script")


def _rewrite_view_schema(sql: str, view_schema: str) -> str:
    """
    Rewrite view SQL to use a dedicated schema instead of dbo with vw_ prefix.

    When view_schema is not 'dbo':
      - [dbo].[vw_X]  → [schema].[X]       (simple EXEC views)
      - [dbo].[vw_X]  → [schema].[X]       (dynamic SQL views)
    """
    import re

    qs = f"[{view_schema}]"

    # Pattern 1: EXEC('CREATE OR ALTER VIEW [dbo].[vw_Name] ...')
    # Replace [dbo].[vw_ with [schema].[
    sql = sql.replace("[dbo].[vw_", f"{qs}.[")

    # Pattern 2: Dynamic SQL strings like N'...CREATE OR ALTER VIEW [dbo].[vw_Name]...'
    # Already caught by the above since it's a literal string replacement.

    return sql


def copy_views_sql(*, sql_root: Path, view_schema: str = "dbo") -> None:
    """
    Compose <final>/sql/schema/04_create_views.sql from the modular view scripts
    in scripts/sql/views/*.sql.

    Ordering is lexicographic, so use numeric prefixes:
      00_model_views.sql
      10_budget_views.sql
      90_budget_cache.sql

    If no modular scripts exist, fall back to legacy create_views.sql.

    When view_schema is not 'dbo', views are created under that schema
    with clean names (no vw_ prefix). A CREATE SCHEMA preamble is added.
    """
    repo_root = _find_repo_root(Path(__file__).resolve())
    views_dir = repo_root / "scripts" / "sql" / "views"
    if not views_dir.exists() or not views_dir.is_dir():
        skip(f"Views folder not found; skipping: {views_dir}")
        return

    legacy = views_dir / "create_views.sql"
    use_custom_schema = view_schema.lower() != "dbo"

    # Use modular scripts if present; exclude legacy file to avoid double inclusion.
    parts = sorted(
        [p for p in views_dir.glob("*.sql") if p.is_file() and p.name.lower() != "create_views.sql"],
        key=lambda p: p.name.lower(),
    )

    if not parts:
        if legacy.exists():
            dst = sql_root / "schema" / "04_create_views.sql"
            text = _read_text(legacy)
            if use_custom_schema:
                text = _rewrite_view_schema(text, view_schema)
            _write_text(dst, text)
            done("Copied legacy views script")
            return
        skip(f"No view scripts found in: {views_dir}")
        return

    dst = sql_root / "schema" / "04_create_views.sql"

    chunks: list[str] = []
    chunks.append("-- Auto-generated by packaging: composed views\n")
    chunks.append(f"-- source: {views_dir.as_posix()}\n")
    if use_custom_schema:
        chunks.append(f"-- view_schema: {view_schema}\n")
    chunks.append("SET NOCOUNT ON;\nGO\n")

    # Create the custom schema if needed
    if use_custom_schema:
        safe_schema = sql_escape_literal(view_schema)
        chunks.append(f"\nIF SCHEMA_ID('{safe_schema}') IS NULL\n")
        chunks.append(f"    EXEC('CREATE SCHEMA [{safe_schema}] AUTHORIZATION [dbo];');\n")
        chunks.append("GO\n")

    for p in parts:
        chunks.append("\n-- ============================================================\n")
        chunks.append(f"-- BEGIN {p.name}\n")
        chunks.append("-- ============================================================\n\n")
        # IMPORTANT: use _read_text() so UTF-16 files are handled (your 00_model_views.sql is UTF-16)
        text = _read_text(p).rstrip()
        if use_custom_schema:
            text = _rewrite_view_schema(text, view_schema)
        chunks.append(text)
        chunks.append("\n\n-- ============================================================\n")
        chunks.append(f"-- END {p.name}\n")
        chunks.append("-- ============================================================\nGO\n")

    _write_text(dst, "".join(chunks))
    schema_note = f" (schema: {view_schema})" if use_custom_schema else ""
    done(f"Composed views script from {len(parts)} file(s){schema_note}")


# ------------------------------------------------------------
# Constraints (mode-dependent)
# ------------------------------------------------------------

def compose_constraints_sql(*, sql_root: Path, sales_cfg: dict, cfg: dict | None = None) -> None:
    """
    Compose <final>/sql/schema/03_create_constraints.sql from the modular constraint files.
    Falls back to legacy create_constraints.sql if modular parts are missing.
    """
    repo_root = _find_repo_root(Path(__file__).resolve())
    modular_dir = repo_root / "scripts" / "sql" / "bootstrap" / "constraints"
    legacy_file = repo_root / "scripts" / "sql" / "bootstrap" / "create_constraints.sql"

    out_path = sql_root / "schema" / "03_create_constraints.sql"
    mode = _sales_mode(sales_cfg)

    if modular_dir.exists() and modular_dir.is_dir():
        parts: list[Path] = [modular_dir / "00_dimensions.sql"]
        if mode in {"sales", "both"}:
            parts.append(modular_dir / "10_sales.sql")
        if mode in {"sales_order", "both"}:
            parts.extend(
                [
                    modular_dir / "20_sales_order_header.sql",
                    modular_dir / "21_sales_order_detail.sql",
                    modular_dir / "22_sales_order_relations.sql",
                ]
            )

        # Budget constraints (conditional on budget.enabled)
        budget_constraints = modular_dir / "30_budget.sql"
        if budget_constraints.exists() and _budget_enabled(cfg):
            parts.append(budget_constraints)

        # Inventory constraints (conditional on inventory.enabled)
        inventory_constraints = modular_dir / "40_inventory.sql"
        if inventory_constraints.exists() and _inventory_enabled(cfg):
            parts.append(inventory_constraints)

        existing = [p for p in parts if p.exists()]
        if not existing:
            skip(f"No modular constraint parts found in: {modular_dir}; skipping constraints.")
            return

        chunks: list[str] = []
        chunks.append("-- Auto-generated by packaging: composed constraints\n")
        chunks.append(f"-- mode: {mode}\n")

        for p in existing:
            chunks.append("\n-- ============================================================\n" f"-- {p.name}\n" "-- ============================================================\n")
            chunks.append(_read_text(p).rstrip())

        _write_text(out_path, "\n".join(chunks).rstrip() + "\n")
        done("Composed constraints")
        return

    if legacy_file.exists():
        _write_text(out_path, _read_text(legacy_file))
        done("Copied legacy constraints")
        return

    skip("No constraints source found; skipping constraints")


# ------------------------------------------------------------
# Verification views (verify schema)
# ------------------------------------------------------------

def compose_verification_sql(*, sql_root: Path) -> None:
    """
    Compose <final>/sql/schema/05_create_verify_schema.sql from all scripts
    in scripts/sql/bootstrap/verification/*.sql.

    Ordering is lexicographic (use numeric prefixes):
      00_create_verify_schema.sql   (schema + dispatcher proc)
      10_scd2_customers.sql         (additional view)
      ...

    New .sql files dropped into the verification folder are automatically
    included — no code changes needed.
    """
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

    dst = sql_root / "schema" / "05_create_verify_schema.sql"

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

def write_create_table_scripts(*, dims_out: Path, facts_out: Path, sql_root: Path, cfg: dict) -> None:
    """Emit CREATE TABLE scripts for every registered SQL dialect.

    SQL Server output lands at the existing ``sql/`` root for backward
    compatibility; every other dialect lands at ``<run>/<dialect_name>/``
    as a sibling of ``sql/``. The cost is trivial (~27 KB per extra dialect)
    and avoids forcing users to re-run the pipeline to switch DBMSes.
    """
    with stage("Generating CREATE TABLE Scripts"):
        dims_csv = list(dims_out.glob("*.csv"))
        facts_csv = list(facts_out.rglob("*.csv"))

        if not dims_csv and not facts_csv:
            skip("No CSV files found - skipping CREATE TABLE scripts.")
            return

        run_root = sql_root.parent
        for dialect in REGISTRY.values():
            out_dir = sql_root if dialect is DEFAULT_DIALECT else run_root / dialect.name
            generate_all_create_tables(
                output_folder=out_dir,
                cfg=cfg,
                dialect=dialect,
            )


def write_bulk_insert_scripts(*, dims_out: Path, facts_out: Path, sql_root: Path, cfg: dict, **_) -> None:
    """
    Always generate:
      sql/load/01_bulk_insert_dims.sql
      sql/load/02_bulk_insert_facts.sql

    The facts allowlist is computed from the FULL cfg (sales_output + returns enablement).
    """
    with stage("Generating BULK INSERT Scripts"):
        dims_csv = list(dims_out.glob("*.csv"))
        facts_csv = list(facts_out.rglob("*.csv"))

        if not dims_csv and not facts_csv:
            skip("No CSV files found - skipping BULK INSERT scripts.")
            return

        load_root = sql_root / "load"
        generate_dims_and_facts_bulk_insert_scripts(
            dims_folder=str(dims_out),
            facts_folder=str(facts_out),
            cfg=cfg,
            load_output_folder=str(load_root),
            dims_mode="csv",
            facts_mode="legacy",
            row_terminator="0x0a",
        )
