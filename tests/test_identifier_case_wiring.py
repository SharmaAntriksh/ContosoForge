"""Identifier casing threaded through CREATE TABLE + load generation.

The name map routes table/column identifiers through ``create_table_from_schema``
and ``generate_bulk_insert_script`` so DROP/CREATE agree and the load target is
cased, while the data-file paths (and thus the actual CSVs) stay untouched. This
is the machinery the importers reuse to re-render a run in a requested case.
"""
from __future__ import annotations

from pathlib import Path

from src.tools.sql.dialect import ColumnSpec, DEFAULT_DIALECT, PostgresDialect, SqlType
from src.tools.sql.generate_bulk_insert_sql import generate_bulk_insert_script
from src.tools.sql.generate_create_table_scripts import create_table_from_schema
from src.tools.sql.identifier_case import IdentifierCase
from src.tools.sql.name_map import build_name_map

_INT = ColumnSpec(SqlType.INT, nullable=False)
_COLS = [("CustomerKey", _INT), ("CustomerID", _INT)]


class TestDdlGeneration:
    def test_pascal_default_is_unchanged(self) -> None:
        # No name_map == identity: SQL Server brackets keep PascalCase.
        sql = create_table_from_schema("Customers", _COLS, dialect=DEFAULT_DIALECT)
        assert "[Customers]" in sql and "[CustomerKey]" in sql
        assert "[customers]" not in sql

    def test_snake_sqlserver(self) -> None:
        nm = build_name_map(IdentifierCase.SNAKE)
        sql = create_table_from_schema("Customers", _COLS, dialect=DEFAULT_DIALECT, name_map=nm)
        assert "[customers]" in sql and "[customer_key]" in sql and "[customer_id]" in sql
        assert "[Customers]" not in sql and "[CustomerKey]" not in sql

    def test_snake_postgres(self) -> None:
        nm = build_name_map(IdentifierCase.SNAKE)
        sql = create_table_from_schema("Customers", _COLS, dialect=PostgresDialect(), name_map=nm)
        assert '"customers"' in sql and '"customer_key"' in sql
        assert '"Customers"' not in sql and '"CustomerKey"' not in sql

    def test_snake_drop_and_create_agree(self) -> None:
        # The DROP references the same snaked table as the CREATE.
        nm = build_name_map(IdentifierCase.SNAKE)
        sql = create_table_from_schema("Customers", _COLS, dialect=PostgresDialect(), name_map=nm)
        assert 'DROP TABLE IF EXISTS "public"."customers"' in sql


class TestLoadGeneration:
    def test_snake_targets_are_mapped_but_csv_path_is_untouched(self, tmp_path: Path) -> None:
        dims = tmp_path / "dims"
        dims.mkdir()
        (dims / "Customers.csv").write_text("CustomerKey\n1\n", encoding="utf-8")
        out = tmp_path / "01_copy_dims.sql"

        nm = build_name_map(IdentifierCase.SNAKE)
        generate_bulk_insert_script(
            dims, output_sql_file=str(out), dialect=PostgresDialect(), name_map=nm
        )
        text = out.read_text(encoding="utf-8")
        # COPY target table is snaked...
        assert 'COPY "public"."customers"' in text
        assert '"public"."Customers"' not in text
        # ...but the source CSV file name (the data file) is untouched.
        assert "Customers.csv" in text

    def test_pascal_load_is_unchanged(self, tmp_path: Path) -> None:
        dims = tmp_path / "dims"
        dims.mkdir()
        (dims / "Customers.csv").write_text("CustomerKey\n1\n", encoding="utf-8")
        out = tmp_path / "01_copy_dims.sql"

        generate_bulk_insert_script(dims, output_sql_file=str(out), dialect=PostgresDialect())
        text = out.read_text(encoding="utf-8")
        assert 'COPY "public"."Customers"' in text
