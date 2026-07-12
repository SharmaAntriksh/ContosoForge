"""Re-render a packaged CSV run's SQL scripts in a chosen identifier case.

Generation always emits PascalCase SQL. Identifier casing is a per-import
concern: on SQL Server it is cosmetic (the engine is case-insensitive), while on
PostgreSQL ``snake`` lets users query without double-quoting folded identifiers.
Rather than lock a casing into the packaged dataset, the importers call
:func:`reemit_run_sql` to reproduce the exact CREATE TABLE / constraint / view /
index / load bundle for a run with identifiers routed through a name map.

This is a pure re-render, not a text rewrite: it reads the run's own packaged
config (``<run>/config/config.yaml``) and data files (``<run>/dimensions``,
``<run>/facts``) and drives the same roster-based generators packaging uses, so
the output is byte-for-byte what packaging would have produced in that case. The
effective run shape (CLI overrides such as ``--sales-rows`` / ``--skip-order-cols``
and the authoritative OrderNumber width) is read from
``<run>/config/sql_generation.json``, since the packaged config.yaml is a snapshot
taken before overrides; runs predating that manifest fall back to config.yaml with
a warning. The data files are never touched (CSV load is positional).
"""
from __future__ import annotations

from pathlib import Path

from src.engine.config.config import load_config
from src.exceptions import PackagingError
from src.tools.sql.dialect import REGISTRY
from src.tools.sql.identifier_case import IdentifierCase
from src.tools.sql.name_map import build_name_map, find_reserved_conflicts
from src.utils.logging_utils import warn

from .sql_scripts import (
    compose_constraints_sql,
    compose_postgres_constraints_sql,
    compose_postgres_indexes_sql,
    compose_postgres_views_sql,
    compose_verification_sql,
    copy_postgres_admin_sql,
    copy_static_sql_assets,
    copy_views_sql,
    write_bulk_insert_scripts,
    write_create_table_scripts,
)


def _load_sql_generation_manifest(run_dir: Path) -> dict | None:
    """Load config/sql_generation.json if the run has one (see package_output)."""
    import json

    path = run_dir / "config" / "sql_generation.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warn(f"Ignoring unreadable sql_generation.json ({exc}); falling back to config.yaml values.")
        return None
    return data if isinstance(data, dict) else None


def reemit_run_sql(*, run_dir: Path, out_sql_root: Path, case: str) -> None:
    """Re-render a CSV run's SQL bundle under ``out_sql_root`` in ``case``.

    ``case`` is ``pascal`` (identity, no map) or ``snake``. ``out_sql_root`` gets
    the same ``<dialect>/schema|load|indexes|admin`` layout packaging produces,
    for every registered dialect, so the caller can point an importer at
    ``out_sql_root / "sqlserver"`` or ``out_sql_root / "postgres"``.
    """
    run_dir = Path(run_dir)
    ident = IdentifierCase(str(case).strip().lower())
    name_map = None if ident is IdentifierCase.PASCAL else build_name_map(ident)

    if name_map is not None:
        for dialect in REGISTRY.values():
            for orig, target in find_reserved_conflicts(name_map, dialect):
                warn(
                    f"identifier_case={ident.value}: '{target}' (from '{orig}') is a "
                    f"{dialect.name} reserved word and will still require quoting."
                )

    cfg_path = run_dir / "config" / "config.yaml"
    if not cfg_path.is_file():
        raise PackagingError(
            f"Cannot re-render SQL for identifier_case={ident.value}: packaged "
            f"config not found at {cfg_path}. Only CSV runs packaged with their "
            "config support a non-pascal --identifier-case."
        )
    cfg = load_config(cfg_path)

    # The packaged config.yaml is a snapshot taken before CLI overrides were
    # applied, so overlay the effective run-shape values recorded at packaging.
    order_id_int64: bool | None = None
    manifest = _load_sql_generation_manifest(run_dir)
    if manifest is not None:
        raw_flag = manifest.get("order_id_int64")
        order_id_int64 = None if raw_flag is None else bool(raw_flag)
        if manifest.get("total_rows") is not None:
            cfg.sales.total_rows = int(manifest["total_rows"])
        if manifest.get("skip_order_cols") is not None:
            cfg.sales.skip_order_cols = bool(manifest["skip_order_cols"])
        if manifest.get("sales_output"):
            cfg.sales.sales_output = str(manifest["sales_output"]).strip().lower()
    else:
        warn(
            "Run has no config/sql_generation.json (generated before it existed). "
            "Re-rendering from config.yaml; if the run used CLI overrides such as "
            "--sales-rows or --skip-order-cols, the re-rendered SQL may not match "
            "the data."
        )

    sales_cfg = cfg.sales
    dims_out = run_dir / "dimensions"
    facts_out = run_dir / "facts"

    out_sql_root = Path(out_sql_root)
    out_sql_root.mkdir(parents=True, exist_ok=True)

    # Mirror package_output's CSV SQL sequence exactly. The width flag comes from
    # the run's sql_generation.json when present; the row-count fallback is an
    # estimate only (the manifest flag is computed from the real id ceiling and
    # can promote to BIGINT slightly earlier than the estimate).
    write_create_table_scripts(
        dims_out=dims_out, facts_out=facts_out, sql_root=out_sql_root, cfg=cfg,
        order_id_int64=order_id_int64, name_map=name_map,
    )
    compose_constraints_sql(sql_root=out_sql_root, sales_cfg=sales_cfg, cfg=cfg, name_map=name_map)
    compose_postgres_constraints_sql(sql_root=out_sql_root, sales_cfg=sales_cfg, cfg=cfg, name_map=name_map)
    compose_postgres_views_sql(sql_root=out_sql_root, cfg=cfg, sales_cfg=sales_cfg, name_map=name_map)
    compose_postgres_indexes_sql(sql_root=out_sql_root, name_map=name_map)
    copy_postgres_admin_sql(sql_root=out_sql_root)
    copy_views_sql(sql_root=out_sql_root, cfg=cfg, sales_cfg=sales_cfg, name_map=name_map)
    # Verification diagnostics are PascalCase-only and self-skip for a snake map.
    compose_verification_sql(sql_root=out_sql_root, name_map=name_map)
    write_bulk_insert_scripts(
        dims_out=dims_out, facts_out=facts_out, sql_root=out_sql_root, cfg=cfg,
        name_map=name_map,
    )
    copy_static_sql_assets(sql_root=out_sql_root)
