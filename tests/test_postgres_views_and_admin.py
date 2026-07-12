"""Tests for the Postgres views composer wiring + admin script copier.

Views are now *generated* from the roster in ``src/tools/sql/view_sql.py`` (see
``test_view_generation.py`` for the generator itself); these cover the composer
wiring (output location, view-schema resolution) and the unrelated admin copier.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.engine.config.config_schema import AppConfig
from src.engine.packaging.sql_scripts import (
    _resolve_postgres_view_schema,
    compose_postgres_views_sql,
    copy_postgres_admin_sql,
)


@pytest.fixture
def run_layout(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True)
    return run


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestResolvePostgresViewSchema:
    def test_none_cfg_defaults_to_public(self) -> None:
        assert _resolve_postgres_view_schema(None) == "public"

    def test_dbo_remaps_to_public(self) -> None:
        cfg = AppConfig.model_validate({"defaults": {"view_schema": "dbo"}})
        assert _resolve_postgres_view_schema(cfg) == "public"

    def test_explicit_public_kept(self) -> None:
        cfg = AppConfig.model_validate({"defaults": {"view_schema": "public"}})
        assert _resolve_postgres_view_schema(cfg) == "public"

    def test_custom_kept_verbatim(self) -> None:
        cfg = AppConfig.model_validate({"defaults": {"view_schema": "bi"}})
        assert _resolve_postgres_view_schema(cfg) == "bi"


class TestComposePostgresViewsOutput:
    def test_writes_under_postgres_schema(self, run_layout: Path) -> None:
        compose_postgres_views_sql(sql_root=run_layout / "sql", cfg=None)
        out = run_layout / "sql" / "postgres" / "schema" / "04_create_views.sql"
        assert out.is_file()

    def test_default_schema_no_create_schema_preamble(self, run_layout: Path) -> None:
        compose_postgres_views_sql(sql_root=run_layout / "sql", cfg=None)
        out = _read(run_layout / "sql" / "postgres" / "schema" / "04_create_views.sql")
        assert "CREATE SCHEMA IF NOT EXISTS" not in out
        assert '"public"."vw_Sales"' in out  # vw_ prefix retained

    def test_custom_schema_emits_create_schema(self, run_layout: Path) -> None:
        cfg = AppConfig.model_validate({"defaults": {"view_schema": "bi"}})
        compose_postgres_views_sql(sql_root=run_layout / "sql", cfg=cfg)
        out = _read(run_layout / "sql" / "postgres" / "schema" / "04_create_views.sql")
        assert 'CREATE SCHEMA IF NOT EXISTS "bi";' in out
        assert '"bi"."Sales"' in out  # vw_ stripped, schema rewritten
        assert '"public"."vw_' not in out
        # Source table refs still under public.
        assert '"public"."Sales"' in out

    def test_dbo_cfg_treated_as_public(self, run_layout: Path) -> None:
        """A SQL Server-style view_schema=dbo should produce default (public) output."""
        cfg = AppConfig.model_validate({"defaults": {"view_schema": "dbo"}})
        compose_postgres_views_sql(sql_root=run_layout / "sql", cfg=cfg)
        out = _read(run_layout / "sql" / "postgres" / "schema" / "04_create_views.sql")
        assert "CREATE SCHEMA IF NOT EXISTS" not in out
        assert '"public"."vw_Sales"' in out


class TestCopyPostgresAdmin:
    def test_copies_pk_proc(self, run_layout: Path) -> None:
        copy_postgres_admin_sql(sql_root=run_layout / "sql")
        out = run_layout / "sql" / "postgres" / "admin" / "create_pk_proc.sql"
        assert out.is_file()
        text = _read(out)
        assert "CREATE OR REPLACE PROCEDURE admin.manage_primary_keys" in text
        assert "DROP" in text
        assert "RESTORE" in text
        # FKs are also managed so PK drops aren't blocked by FK dependencies.
        assert "foreign key" in text.lower()
        assert "contype IN ('p', 'f')" in text

    def test_does_not_touch_schema_folder(self, run_layout: Path) -> None:
        copy_postgres_admin_sql(sql_root=run_layout / "sql")
        # admin file lands under postgres/admin/, NOT postgres/schema/
        assert not (run_layout / "sql" / "postgres" / "schema" / "create_pk_proc.sql").exists()


class TestPostgresAdminProcContent:
    def _proc_file(self) -> Path:
        repo_root = Path(__file__).resolve().parents[1]
        return (
            repo_root / "scripts" / "sql" / "postgres" / "admin" / "create_pk_proc.sql"
        )

    def test_creates_admin_schema(self) -> None:
        text = _read(self._proc_file())
        assert "CREATE SCHEMA IF NOT EXISTS admin" in text

    def test_creates_backup_table(self) -> None:
        text = _read(self._proc_file())
        assert "CREATE TABLE IF NOT EXISTS admin._pk_backup" in text

    def test_rejects_unknown_action(self) -> None:
        text = _read(self._proc_file())
        assert "Unknown action" in text
        assert "RAISE EXCEPTION" in text

    def test_excludes_system_schemas(self) -> None:
        text = _read(self._proc_file())
        for s in ("pg_catalog", "information_schema", "admin"):
            assert s in text, f"Expected system-schema exclusion to mention {s}"
