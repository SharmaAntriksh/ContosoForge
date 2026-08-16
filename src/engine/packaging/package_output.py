import shutil
import time
from pathlib import Path
from urllib.parse import unquote

from src.exceptions import PackagingError
from src.utils.output_utils import create_final_output_folder
from src.utils.logging_utils import info
from src.utils.write_metrics import artifact_stats, log_write_metric

from .paths import get_first_existing_path, tables_from_sales_cfg
from .parquet_packager import copy_parquet_facts
from .delta_packager import copy_delta_facts
from .csv_packager import copy_csv_facts
from .artifact_transfer import transfer_delta_dir, transfer_file
from .sql_scripts import (
    write_bulk_insert_scripts,
    write_create_table_scripts,
    compose_constraints_sql,
    compose_postgres_constraints_sql,
    compose_postgres_indexes_sql,
    compose_postgres_views_sql,
    compose_verification_sql,
    copy_postgres_admin_sql,
    copy_views_sql,
    copy_static_sql_assets,
)


def _write_sql_generation_manifest(final_folder: Path, sales_cfg, order_id_int64) -> None:
    """Record the effective SQL-shape inputs for import-time re-rendering.

    The packaged config.yaml is a snapshot of the config FILE, taken before CLI
    overrides are applied, so it cannot be trusted for values the SQL generators
    read. This manifest records the effective values (and the authoritative
    OrderNumber width decision) so reemit_run_sql reproduces exactly what
    packaging rendered.
    """
    import json

    manifest = {
        "schema_version": 1,
        "order_id_int64": None if order_id_int64 is None else bool(order_id_int64),
        "total_rows": int(getattr(sales_cfg, "total_rows", 0) or 0),
        "skip_order_cols": bool(getattr(sales_cfg, "skip_order_cols", False)),
        "sales_output": str(getattr(sales_cfg, "sales_output", "sales") or "sales").strip().lower(),
    }
    out = final_folder / "config" / "sql_generation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def package_output(
    cfg,
    sales_cfg,
    parquet_dims: Path,
    fact_out: Path,
    *,
    order_id_int64: bool | None = None,
    manifest=None,
    inventory_artifacts: list[str] | None = None,
):
    """
    Orchestrates packaging:
      - Creates final packaged folder (dims + config copied by output_utils)
      - Copies fact outputs (Sales / OrderHeader / OrderDetail)
      - Generates SQL scripts (CSV only)

    ``order_id_int64`` is the authoritative OrderNumber-width decision from the
    sales run manifest, threaded to the CREATE TABLE generator so the DDL's
    INT/BIGINT choice matches the emitted parquet dtype.

    ``manifest`` is the :class:`SalesRunManifest` from the sales run. When
    present, the fact copiers read each table's output location straight from it
    instead of probing candidate paths — so a stale artifact from a previous run
    can never be shipped in place of the fresh one. When None (legacy / direct
    callers), the copiers fall back to their historical path probing.
    """
    file_format = str(sales_cfg.file_format).lower()
    is_csv = file_format == "csv"
    promote = bool(getattr(getattr(cfg, "packaging", None), "clean_scratch_fact_out", False))

    _raw_folder = unquote(str(cfg.defaults.final_output))
    # Reject path traversal via an explicit ".." path segment. A bare substring
    # check would false-positive on legitimate names like "my..backup".
    if ".." in Path(_raw_folder).parts:
        raise ValueError(f"defaults.final_output must not contain a '..' path segment: {_raw_folder}")
    final_root = Path(_raw_folder).resolve()

    config_yaml_path = get_first_existing_path(
        cfg,
        keys=["config_yaml_path", "config_path", "config_file", "config_yaml", "config"],
    )
    model_yaml_path = get_first_existing_path(
        cfg,
        keys=["model_yaml_path", "model_path", "model_file", "model_yaml", "model"],
    )

    final_folder = create_final_output_folder(
        final_folder_root=final_root,
        parquet_dims=parquet_dims,
        sales_cfg=sales_cfg,
        file_format=file_format,
        cfg=cfg,
        config_yaml_path=config_yaml_path,
        model_yaml_path=model_yaml_path,
    )

    # Remove URL-encoded duplicate run folder (%20)
    parent = final_folder.parent
    real_name = final_folder.name
    for sibling in parent.iterdir():
        if sibling.is_dir() and "%20" in sibling.name and unquote(sibling.name) == real_name:
            shutil.rmtree(sibling, ignore_errors=True)

    dims_out = final_folder / "dimensions"
    facts_out = final_folder / "facts"
    facts_out.mkdir(parents=True, exist_ok=True)


    def _copy_inventory_if_exists():
        """Copy inventory outputs into the packaged facts folder (format-aware)."""
        if file_format == "deltaparquet":
            # Delta table lives directly at fact_out/inventory_snapshot
            inv_delta = fact_out / "inventory_snapshot"
            if inv_delta.exists():
                dst = facts_out / "inventory_snapshot"
                transfer_delta_dir(inv_delta, dst, promote=promote)
            return

        inv_src = fact_out / "inventory"
        if not inv_src.exists():
            return

        if file_format == "parquet":
            parquets = sorted(inv_src.glob("*.parquet"))
            for f in parquets:
                transfer_file(f, facts_out / f.name, promote=promote)
        elif file_format == "csv":
            # The normal pipeline supplies the exact files written by this run.
            # Legacy/direct callers retain path probing for compatibility.
            if inventory_artifacts is not None:
                inventory_root = inv_src.resolve()
                csv_sources = []
                for raw_path in inventory_artifacts:
                    candidate = Path(raw_path).resolve()
                    try:
                        candidate.relative_to(inventory_root)
                    except ValueError as exc:
                        raise PackagingError(
                            f"Inventory artifact is outside the scratch inventory folder: {candidate}"
                        ) from exc
                    if candidate.suffix.lower() != ".csv" or not candidate.is_file():
                        raise PackagingError(
                            f"Current-run Inventory CSV artifact is missing or invalid: {candidate}"
                        )
                    csv_sources.append(candidate)
            else:
                merged = sorted(inv_src.glob("inventory_snapshot*.csv"))
                csv_sources = merged or sorted(inv_src.glob("*.csv"))

            if not csv_sources:
                return
            inv_dst = facts_out / "inventory_snapshot"
            inv_dst.mkdir(parents=True, exist_ok=True)
            for f in sorted(csv_sources):
                transfer_file(f, inv_dst / f.name, promote=promote)


    def _copy_budget_if_exists():
        """Copy budget outputs into the packaged facts folder (format-aware)."""
        if file_format == "deltaparquet":
            # Delta tables live directly at fact_out/budget_yearly and fact_out/budget_monthly
            for name in ("budget_yearly", "budget_monthly"):
                src = fact_out / name
                if src.exists():
                    transfer_delta_dir(src, facts_out / name, promote=promote)
            return

        budget_src = fact_out / "budget"
        if not budget_src.exists():
            return

        ext = "parquet" if file_format == "parquet" else "csv"
        for name in ("budget_yearly", "budget_monthly"):
            src = budget_src / f"{name}.{ext}"
            if src.exists():
                if file_format == "parquet":
                    transfer_file(src, facts_out / src.name, promote=promote)
                else:
                    dst = facts_out / name
                    dst.mkdir(parents=True, exist_ok=True)
                    transfer_file(src, dst / src.name, promote=promote)

    def _copy_complaints_if_exists():
        """Copy complaints output into the packaged facts folder."""
        complaints_src = fact_out / "complaints"
        if not complaints_src.exists():
            return

        if file_format == "parquet":
            for f in complaints_src.glob("*.parquet"):
                transfer_file(f, facts_out / f.name, promote=promote)
        elif file_format == "csv":
            complaints_dst = facts_out / "complaints"
            complaints_dst.mkdir(parents=True, exist_ok=True)
            for f in complaints_src.glob("*.csv"):
                transfer_file(f, complaints_dst / f.name, promote=promote)
        elif file_format == "deltaparquet":
            dst = facts_out / "complaints"
            transfer_delta_dir(complaints_src, dst, promote=promote)

    def _copy_wishlists_if_exists():
        """Copy wishlists output into the packaged facts folder."""
        wl_src = fact_out / "customer_wishlists"
        if not wl_src.exists():
            return

        if file_format == "parquet":
            for f in wl_src.glob("*.parquet"):
                transfer_file(f, facts_out / f.name, promote=promote)
        elif file_format == "csv":
            wl_dst = facts_out / "customer_wishlists"
            wl_dst.mkdir(parents=True, exist_ok=True)
            for f in wl_src.glob("*.csv"):
                transfer_file(f, wl_dst / f.name, promote=promote)
        elif file_format == "deltaparquet":
            dst = facts_out / "customer_wishlists"
            transfer_delta_dir(wl_src, dst, promote=promote)

    tables = tables_from_sales_cfg(sales_cfg, cfg)
    fact_metric_started = time.perf_counter()
    fact_input_stats = artifact_stats([fact_out])

    def _log_fact_packaging() -> None:
        log_write_metric(
            "fact_packaging",
            elapsed_seconds=time.perf_counter() - fact_metric_started,
            output_paths=[facts_out],
            input_stats=fact_input_stats,
            codec=file_format,
            detail=f"format={file_format},promote={promote}",
        )

    if file_format == "parquet":
        copy_parquet_facts(fact_out=fact_out, facts_out=facts_out, sales_cfg=sales_cfg,
                           tables=tables, manifest=manifest, promote=promote)
        _copy_budget_if_exists()
        _copy_inventory_if_exists()
        _copy_complaints_if_exists()
        _copy_wishlists_if_exists()
        _log_fact_packaging()
        return final_folder

    if file_format == "deltaparquet":
        copy_delta_facts(fact_out=fact_out, facts_out=facts_out, sales_cfg=sales_cfg,
                         tables=tables, manifest=manifest, promote=promote)
        _copy_budget_if_exists()
        _copy_inventory_if_exists()
        _copy_complaints_if_exists()
        _copy_wishlists_if_exists()
        _log_fact_packaging()
        return final_folder

    if file_format != "csv":
        raise ValueError(f"Unsupported file_format in packaging: {file_format!r}")

    copy_csv_facts(
        fact_out=fact_out,
        facts_out=facts_out,
        tables=tables,
        manifest=manifest,
        promote=promote,
    )
    _copy_budget_if_exists()
    _copy_inventory_if_exists()
    _copy_complaints_if_exists()
    _copy_wishlists_if_exists()
    _log_fact_packaging()

    # SQL SCRIPT GENERATION — CSV ONLY
    if is_csv:
        sql_root = final_folder / "sql"

        # Generated SQL is always emitted in PascalCase. Identifier casing is a
        # per-import concern (SQL Server casing is cosmetic; Postgres wants snake
        # to avoid quoting), so the importers re-render in the requested case
        # from the same roster — the packaged scripts are the PascalCase baseline.

        # Effective SQL-shape inputs for import-time re-rendering. The packaged
        # config.yaml predates CLI overrides, so the re-emit path cannot trust it
        # for values the generators read (OrderNumber width, skip_order_cols, ...).
        _write_sql_generation_manifest(final_folder, sales_cfg, order_id_int64)

        # CREATE TABLE — emits one script set per registered dialect under the
        # umbrella sql/ root: SQL Server at sql/sqlserver/, Postgres at
        # sql/postgres/, and any future dialect at sql/<dialect>/.
        write_create_table_scripts(dims_out=dims_out, facts_out=facts_out, sql_root=sql_root, cfg=cfg,
                                   order_id_int64=order_id_int64)

        # The remaining T-SQL-only scripts (constraints, views, verification,
        # BULK INSERT, columnstore helpers) emit under sql/sqlserver/.
        compose_constraints_sql(sql_root=sql_root, sales_cfg=sales_cfg, cfg=cfg)
        # Postgres constraints are generated from the same declarative roster.
        compose_postgres_constraints_sql(sql_root=sql_root, sales_cfg=sales_cfg, cfg=cfg)
        compose_postgres_views_sql(sql_root=sql_root, cfg=cfg, sales_cfg=sales_cfg)
        compose_postgres_indexes_sql(sql_root=sql_root)
        copy_postgres_admin_sql(sql_root=sql_root)
        copy_views_sql(sql_root=sql_root, cfg=cfg, sales_cfg=sales_cfg)
        compose_verification_sql(sql_root=sql_root)
        write_bulk_insert_scripts(dims_out=dims_out, facts_out=facts_out, sql_root=sql_root, cfg=cfg)
        copy_static_sql_assets(sql_root=sql_root)
    else:
        info("Skipping SQL script generation for non-CSV format.")

    return final_folder
