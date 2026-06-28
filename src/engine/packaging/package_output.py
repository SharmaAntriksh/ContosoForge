import shutil
from pathlib import Path
from urllib.parse import unquote

from src.utils.output_utils import create_final_output_folder
from src.utils.logging_utils import info

from .paths import get_first_existing_path, tables_from_sales_cfg
from .parquet_packager import copy_parquet_facts
from .delta_packager import copy_delta_facts
from .csv_packager import copy_csv_facts
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


def package_output(cfg, sales_cfg, parquet_dims: Path, fact_out: Path):
    """
    Orchestrates packaging:
      - Creates final packaged folder (dims + config copied by output_utils)
      - Copies fact outputs (Sales / OrderHeader / OrderDetail)
      - Generates SQL scripts (CSV only)
    """
    file_format = str(sales_cfg.file_format).lower()
    is_csv = file_format == "csv"

    _raw_folder = unquote(str(cfg.defaults.final_output))
    if ".." in _raw_folder:
        raise ValueError(f"defaults.final_output must not contain '..': {_raw_folder}")
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
                shutil.copytree(inv_delta, dst, dirs_exist_ok=True)
            return

        inv_src = fact_out / "inventory"
        if not inv_src.exists():
            return

        if file_format == "parquet":
            parquets = sorted(inv_src.glob("*.parquet"))
            for f in parquets:
                shutil.copy2(f, facts_out / f.name)
        elif file_format == "csv":
            # Prefer merged file(s); fall back to raw chunks in subdirectory
            merged = sorted(inv_src.glob("inventory_snapshot*.csv"))
            if merged:
                inv_dst = facts_out / "inventory_snapshot"
                inv_dst.mkdir(parents=True, exist_ok=True)
                for f in merged:
                    shutil.copy2(f, inv_dst / f.name)
            else:
                inv_dst = facts_out / "inventory_snapshot"
                inv_dst.mkdir(parents=True, exist_ok=True)
                for f in inv_src.glob("*.csv"):
                    shutil.copy2(f, inv_dst / f.name)


    def _copy_budget_if_exists():
        """Copy budget outputs into the packaged facts folder (format-aware)."""
        if file_format == "deltaparquet":
            # Delta tables live directly at fact_out/budget_yearly and fact_out/budget_monthly
            for name in ("budget_yearly", "budget_monthly"):
                src = fact_out / name
                if src.exists():
                    shutil.copytree(src, facts_out / name, dirs_exist_ok=True)
            return

        budget_src = fact_out / "budget"
        if not budget_src.exists():
            return

        ext = "parquet" if file_format == "parquet" else "csv"
        for name in ("budget_yearly", "budget_monthly"):
            src = budget_src / f"{name}.{ext}"
            if src.exists():
                if file_format == "parquet":
                    shutil.copy2(src, facts_out / src.name)
                else:
                    dst = facts_out / name
                    dst.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst / src.name)

    def _copy_complaints_if_exists():
        """Copy complaints output into the packaged facts folder."""
        complaints_src = fact_out / "complaints"
        if not complaints_src.exists():
            return

        if file_format == "parquet":
            for f in complaints_src.glob("*.parquet"):
                shutil.copy2(f, facts_out / f.name)
        elif file_format == "csv":
            complaints_dst = facts_out / "complaints"
            complaints_dst.mkdir(parents=True, exist_ok=True)
            for f in complaints_src.glob("*.csv"):
                shutil.copy2(f, complaints_dst / f.name)
        elif file_format == "deltaparquet":
            dst = facts_out / "complaints"
            shutil.copytree(complaints_src, dst, dirs_exist_ok=True)

    def _copy_wishlists_if_exists():
        """Copy wishlists output into the packaged facts folder."""
        wl_src = fact_out / "customer_wishlists"
        if not wl_src.exists():
            return

        if file_format == "parquet":
            for f in wl_src.glob("*.parquet"):
                shutil.copy2(f, facts_out / f.name)
        elif file_format == "csv":
            wl_dst = facts_out / "customer_wishlists"
            wl_dst.mkdir(parents=True, exist_ok=True)
            for f in wl_src.glob("*.csv"):
                shutil.copy2(f, wl_dst / f.name)
        elif file_format == "deltaparquet":
            dst = facts_out / "customer_wishlists"
            shutil.copytree(wl_src, dst, dirs_exist_ok=True)

    tables = tables_from_sales_cfg(sales_cfg, cfg)

    if file_format == "parquet":
        copy_parquet_facts(fact_out=fact_out, facts_out=facts_out, sales_cfg=sales_cfg, tables=tables)
        _copy_budget_if_exists()
        _copy_inventory_if_exists()
        _copy_complaints_if_exists()
        _copy_wishlists_if_exists()
        return final_folder

    if file_format == "deltaparquet":
        copy_delta_facts(fact_out=fact_out, facts_out=facts_out, sales_cfg=sales_cfg, tables=tables)
        _copy_budget_if_exists()
        _copy_inventory_if_exists()
        _copy_complaints_if_exists()
        _copy_wishlists_if_exists()
        return final_folder

    if file_format != "csv":
        raise ValueError(f"Unsupported file_format in packaging: {file_format!r}")

    copy_csv_facts(fact_out=fact_out, facts_out=facts_out, tables=tables)
    _copy_budget_if_exists()
    _copy_inventory_if_exists()
    _copy_complaints_if_exists()
    _copy_wishlists_if_exists()

    # SQL SCRIPT GENERATION — CSV ONLY
    if is_csv:
        sql_root = final_folder / "sql"

        # CREATE TABLE — emits one script set per registered dialect.
        # SQL Server lands at sql/; other dialects land at <run>/<dialect>/.
        write_create_table_scripts(dims_out=dims_out, facts_out=facts_out, sql_root=sql_root, cfg=cfg)

        # The remaining scripts (constraints, views, verification, BULK INSERT,
        # columnstore helpers) are T-SQL only and always emit at sql/.
        compose_constraints_sql(sql_root=sql_root, sales_cfg=sales_cfg, cfg=cfg)
        # Postgres constraint SQL is hand-translated, not auto-derived from the SQL Server parts.
        compose_postgres_constraints_sql(sql_root=sql_root, sales_cfg=sales_cfg, cfg=cfg)
        compose_postgres_views_sql(sql_root=sql_root, cfg=cfg)
        compose_postgres_indexes_sql(sql_root=sql_root)
        copy_postgres_admin_sql(sql_root=sql_root)
        view_schema = str(getattr(getattr(cfg, "defaults", None), "view_schema", "dbo") or "dbo").strip()
        copy_views_sql(sql_root=sql_root, view_schema=view_schema)
        compose_verification_sql(sql_root=sql_root)
        write_bulk_insert_scripts(dims_out=dims_out, facts_out=facts_out, sql_root=sql_root, cfg=cfg)
        copy_static_sql_assets(sql_root=sql_root)
    else:
        info("Skipping SQL script generation for non-CSV format.")

    return final_folder
