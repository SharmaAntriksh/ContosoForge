"""Unit tests for the Postgres importer and the shared importer helpers.

End-to-end behaviour against a live Postgres requires Docker — see the
manual recipe in ``tests/test_dialect_postgres.py``. These tests cover
the file-discovery + statement-parsing parts that can run hermetically.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.exceptions import PostgresImportError
from src.tools.sql._import_common import (
    _extract_tables_from_create_sql,
    _short_path,
    find_create_sql,
    find_finish_load_script,
    find_prepare_load_script,
    list_sql_files,
    ordered_load_files,
)
from src.tools.sql.postgres_import import import_postgres


class TestExtractTablesFromCreateSql:
    """The shared parser must handle SQL Server brackets AND Postgres quotes."""

    def test_sql_server_bracket_form(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            "CREATE TABLE [dbo].[Customers] (Id INT);\nGO\n"
            "CREATE TABLE [dbo].[Products] (Id INT);\n",
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers", "Products"]

    def test_postgres_double_quoted(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            'CREATE TABLE "dbo"."Customers" (Id INTEGER);\n'
            'CREATE TABLE "dbo"."Products" (Id INTEGER);\n',
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers", "Products"]

    def test_unqualified(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text("CREATE TABLE Customers (Id INT);\n", encoding="utf-8")
        assert _extract_tables_from_create_sql(sql) == ["Customers"]

    def test_if_not_exists_tolerated(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            'CREATE TABLE IF NOT EXISTS "dbo"."Customers" (Id INTEGER);\n',
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers"]

    def test_dedupes_repeats(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            "CREATE TABLE [Customers] (Id INT);\n"
            "CREATE TABLE [Customers] (Id INT);\n",
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers"]

    def test_ignores_create_in_comment(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            "-- CREATE TABLE [Sales] would go here\n"
            "CREATE TABLE [Customers] (Id INT);\n",
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers"]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _extract_tables_from_create_sql(tmp_path / "missing.sql") == []


class TestShortPath:
    def test_relative_to_base(self, tmp_path: Path) -> None:
        sub = tmp_path / "a" / "b" / "c.sql"
        sub.parent.mkdir(parents=True)
        sub.write_text("")
        assert _short_path(sub, base=tmp_path) == "a/b/c.sql"

    def test_fallback_to_name_when_unrelated(self, tmp_path: Path) -> None:
        p = tmp_path / "file.sql"
        p.write_text("")
        # A genuinely unrelated base (sibling subdir) — relative_to fails.
        unrelated = tmp_path / "sibling"
        unrelated.mkdir()
        assert _short_path(p, base=unrelated) == "file.sql"

    def test_no_base(self, tmp_path: Path) -> None:
        p = tmp_path / "file.sql"
        p.write_text("")
        assert _short_path(p) == "file.sql"


class TestOrderedLoadFiles:
    def test_dims_then_facts(self, tmp_path: Path) -> None:
        (tmp_path / "02_copy_facts.sql").write_text("")
        (tmp_path / "01_copy_dims.sql").write_text("")
        ordered = ordered_load_files(tmp_path)
        assert [p.name for p in ordered] == ["01_copy_dims.sql", "02_copy_facts.sql"]

    def test_only_dims(self, tmp_path: Path) -> None:
        (tmp_path / "01_copy_dims.sql").write_text("")
        ordered = ordered_load_files(tmp_path)
        assert [p.name for p in ordered] == ["01_copy_dims.sql"]

    def test_empty_folder(self, tmp_path: Path) -> None:
        assert ordered_load_files(tmp_path) == []

    def test_prepare_finish_not_warned_as_extras(self, tmp_path: Path, capsys) -> None:
        # The recovery-window scripts are handled by the importer, so they are
        # NOT listed as skipped extras and must not appear in the warning.
        (tmp_path / "00_bulk_insert_prepare_load.sql").write_text("")
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        (tmp_path / "02_bulk_insert_facts.sql").write_text("")
        (tmp_path / "99_bulk_insert_finish_load.sql").write_text("")
        ordered = ordered_load_files(tmp_path)
        assert [p.name for p in ordered] == [
            "01_bulk_insert_dims.sql", "02_bulk_insert_facts.sql",
        ]
        out = capsys.readouterr().out
        assert "Extra load scripts present" not in out

    def test_genuine_extra_still_warned(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        (tmp_path / "02_bulk_insert_facts.sql").write_text("")
        (tmp_path / "50_something_unexpected.sql").write_text("")
        ordered_load_files(tmp_path)
        out = capsys.readouterr().out
        assert "Extra load scripts present" in out
        assert "50_something_unexpected.sql" in out


class TestFindRecoveryScripts:
    def test_finds_prepare_and_finish(self, tmp_path: Path) -> None:
        prep = tmp_path / "00_bulk_insert_prepare_load.sql"
        fin = tmp_path / "99_bulk_insert_finish_load.sql"
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        prep.write_text(""); fin.write_text("")
        assert find_prepare_load_script(tmp_path) == prep
        assert find_finish_load_script(tmp_path) == fin

    def test_absent_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        assert find_prepare_load_script(tmp_path) is None
        assert find_finish_load_script(tmp_path) is None

    def test_unrelated_prepare_finish_named_files_ignored(self, tmp_path: Path, capsys) -> None:
        # Files that merely contain "prepare"/"finish" but not the documented
        # *_prepare_load / *_finish_load suffix must NOT be treated as recovery
        # scripts (else e.g. a facts-load file could run twice), and they are
        # still surfaced normally by ordered_load_files.
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        (tmp_path / "02_bulk_insert_facts.sql").write_text("")
        (tmp_path / "03_facts_prepare_index.sql").write_text("")
        (tmp_path / "04_finish_report.sql").write_text("")
        assert find_prepare_load_script(tmp_path) is None
        assert find_finish_load_script(tmp_path) is None
        # The unrelated files are genuine extras and stay in the warning.
        ordered_load_files(tmp_path)
        out = capsys.readouterr().out
        assert "03_facts_prepare_index.sql" in out
        assert "04_finish_report.sql" in out


class TestFindCreateSql:
    def test_matches_suffix(self, tmp_path: Path) -> None:
        a = tmp_path / "01_create_dimensions.sql"
        b = tmp_path / "02_create_facts.sql"
        a.write_text(""); b.write_text("")
        assert find_create_sql([a, b], "create_dimensions.sql") == a
        assert find_create_sql([a, b], "create_facts.sql") == b

    def test_missing(self, tmp_path: Path) -> None:
        assert find_create_sql([], "create_dimensions.sql") is None


class TestImportPostgresErrors:
    """Errors raised before any DB connection happens (hermetic)."""

    def test_missing_schema_folder_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PostgresImportError, match="schema folder not found"):
            import_postgres(database="x", run_dir=tmp_path)

    def test_empty_schema_folder_raises(self, tmp_path: Path) -> None:
        (tmp_path / "postgres" / "schema").mkdir(parents=True)
        with pytest.raises(PostgresImportError, match="No schema scripts found"):
            import_postgres(database="x", run_dir=tmp_path)
