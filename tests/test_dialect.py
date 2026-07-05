"""Unit tests for the SQL dialect layer.

Asserts that every SqlType renders to the exact SQL Server string the
previous string-based helpers produced, for both NULL and NOT NULL.
"""
from __future__ import annotations

import pytest

from src.tools.sql.dialect import ColumnSpec, DEFAULT_DIALECT, SqlServerDialect, SqlType
from src.utils.static_schemas import (
    BIT,
    BIGINT,
    CHAR,
    DATE,
    DATETIME,
    DATETIME2,
    DECIMAL,
    FLOAT,
    INT,
    SMALLINT,
    TIME,
    TINYINT,
    VARCHAR,
    STATIC_SCHEMAS,
)


class TestSqlServerRendering:
    """Every SqlType -> expected T-SQL fragment."""

    def setup_method(self) -> None:
        self.dialect = SqlServerDialect()

    @pytest.mark.parametrize(
        "spec,expected",
        [
            (ColumnSpec(SqlType.INT, nullable=False), "INT NOT NULL"),
            (ColumnSpec(SqlType.INT, nullable=True), "INT NULL"),
            (ColumnSpec(SqlType.BIGINT, nullable=False), "BIGINT NOT NULL"),
            (ColumnSpec(SqlType.BIGINT, nullable=True), "BIGINT NULL"),
            (ColumnSpec(SqlType.SMALLINT, nullable=False), "SMALLINT NOT NULL"),
            (ColumnSpec(SqlType.TINYINT, nullable=False), "TINYINT NOT NULL"),
            (ColumnSpec(SqlType.BIT, nullable=False), "BIT NOT NULL"),
            (ColumnSpec(SqlType.FLOAT, nullable=False), "FLOAT NOT NULL"),
            (ColumnSpec(SqlType.DATE, nullable=False), "DATE NOT NULL"),
            (ColumnSpec(SqlType.DATE, nullable=True), "DATE NULL"),
            (ColumnSpec(SqlType.DATETIME, nullable=False), "DATETIME NOT NULL"),
            (ColumnSpec(SqlType.DATETIME2, nullable=False, args=(7,)), "DATETIME2(7) NOT NULL"),
            (ColumnSpec(SqlType.TIME, nullable=False, args=(0,)), "TIME(0) NOT NULL"),
            (ColumnSpec(SqlType.VARCHAR, nullable=False, args=(100,)), "VARCHAR(100) NOT NULL"),
            (ColumnSpec(SqlType.VARCHAR, nullable=True, args=(50,)), "VARCHAR(50) NULL"),
            (ColumnSpec(SqlType.VARCHAR, nullable=True, args=("MAX",)), "VARCHAR(MAX) NULL"),
            (ColumnSpec(SqlType.CHAR, nullable=False, args=(1,)), "CHAR(1) NOT NULL"),
            (ColumnSpec(SqlType.DECIMAL, nullable=False, args=(8, 2)), "DECIMAL(8, 2) NOT NULL"),
            (ColumnSpec(SqlType.DECIMAL, nullable=True, args=(10, 6)), "DECIMAL(10, 6) NULL"),
        ],
    )
    def test_render_type(self, spec: ColumnSpec, expected: str) -> None:
        assert self.dialect.render_type(spec) == expected

    def test_default_dialect_is_sqlserver(self) -> None:
        assert isinstance(DEFAULT_DIALECT, SqlServerDialect)
        assert DEFAULT_DIALECT.name == "sqlserver"

    def test_simple_type_rejects_args(self) -> None:
        with pytest.raises(ValueError, match="takes no args"):
            self.dialect.render_type(ColumnSpec(SqlType.INT, args=(10,)))


class TestStaticSchemaHelpers:
    """Type helper functions in static_schemas should produce ColumnSpecs that
    render to the exact strings the old string-based helpers produced."""

    @pytest.mark.parametrize(
        "spec,expected",
        [
            (INT(), "INT NOT NULL"),
            (INT(not_null=False), "INT NULL"),
            (BIGINT(), "BIGINT NOT NULL"),
            (SMALLINT(not_null=False), "SMALLINT NULL"),
            (TINYINT(), "TINYINT NOT NULL"),
            (BIT(), "BIT NOT NULL"),
            (FLOAT(not_null=False), "FLOAT NULL"),
            (DATE(), "DATE NOT NULL"),
            (DATETIME(), "DATETIME NOT NULL"),
            (DATETIME2(7), "DATETIME2(7) NOT NULL"),
            (TIME(0), "TIME(0) NOT NULL"),
            # VARCHAR / CHAR default to NULL (not_null=False) — matches legacy
            (VARCHAR(100), "VARCHAR(100) NULL"),
            (VARCHAR(50, not_null=True), "VARCHAR(50) NOT NULL"),
            (VARCHAR("MAX"), "VARCHAR(MAX) NULL"),
            (CHAR(1), "CHAR(1) NULL"),
            (CHAR(1, not_null=True), "CHAR(1) NOT NULL"),
            (DECIMAL(8, 2), "DECIMAL(8, 2) NOT NULL"),
            (DECIMAL(10, 6, not_null=False), "DECIMAL(10, 6) NULL"),
        ],
    )
    def test_helper_renders_to_expected_sqlserver_string(
        self, spec: ColumnSpec, expected: str
    ) -> None:
        assert DEFAULT_DIALECT.render_type(spec) == expected

    def test_every_static_schema_column_is_renderable(self) -> None:
        """Round-trip safety: every column in STATIC_SCHEMAS must render."""
        for table, schema in STATIC_SCHEMAS.items():
            for col, spec in schema:
                rendered = DEFAULT_DIALECT.render_type(spec)
                assert rendered, f"{table}.{col} produced empty SQL"
                assert rendered.endswith("NULL"), (
                    f"{table}.{col} rendered as {rendered!r} — missing nullability"
                )


class TestSqlServerQuoteIdent:
    """SqlServerDialect.quote_ident replaces the old sql_helpers function."""

    def setup_method(self) -> None:
        self.dialect = SqlServerDialect()

    def test_simple(self) -> None:
        assert self.dialect.quote_ident("Sales") == "[Sales]"

    def test_strips_existing_brackets(self) -> None:
        assert self.dialect.quote_ident("[Sales]") == "[Sales]"

    def test_strips_existing_double_quotes(self) -> None:
        assert self.dialect.quote_ident('"Sales"') == "[Sales]"

    def test_escapes_closing_bracket(self) -> None:
        assert self.dialect.quote_ident("My]Table") == "[My]]Table]"

    def test_drop_table_if_exists(self) -> None:
        sql = self.dialect.drop_table_if_exists("dbo", "Sales")
        assert "IF OBJECT_ID(N'[dbo].[Sales]', N'U') IS NOT NULL" in sql
        assert "DROP TABLE [dbo].[Sales];" in sql

    def test_drop_table_if_exists_escapes_apostrophe(self) -> None:
        # Apostrophes in identifiers are quoted in the bracket form, then the
        # bracketed result is escaped for the N'...' literal.
        sql = self.dialect.drop_table_if_exists("dbo", "O'Brien")
        assert "N'[dbo].[O''Brien]'" in sql
        assert "DROP TABLE [dbo].[O'Brien];" in sql

    def test_batch_separator(self) -> None:
        assert self.dialect.batch_separator == "GO"


class _RecordingDialect(SqlServerDialect):
    """SqlServerDialect with every call wrapped in a sentinel marker.

    Used to prove that ``create_table_from_schema`` actually routes calls
    through the injected dialect parameter rather than the module-level
    DEFAULT_DIALECT.
    """

    name = "recording"
    batch_separator = "/*BATCH*/"

    def quote_ident(self, name: str) -> str:
        return f"<<{name}>>"

    def render_type(self, spec: ColumnSpec) -> str:
        return "TYPE!"

    def drop_table_if_exists(self, schema: str, table: str) -> str:
        return f"-- drop {self.quote_ident(schema)}.{self.quote_ident(table)} --"


class TestDialectInjection:
    """create_table_from_schema must honour the dialect parameter."""

    def test_create_table_uses_injected_dialect(self) -> None:
        from src.tools.sql.generate_create_table_scripts import create_table_from_schema

        sql = create_table_from_schema(
            "Sales",
            [("Id", INT()), ("Amount", DECIMAL(10, 2))],
            dialect=_RecordingDialect(),
        )

        # Identifier quoting came from the dialect, not DEFAULT_DIALECT brackets.
        assert "<<Sales>>" in sql
        assert "<<Id>>" in sql
        assert "<<Amount>>" in sql
        # Type rendering came from the dialect.
        assert "TYPE!" in sql
        # Drop statement came from the dialect (and was called with unquoted names).
        assert "-- drop <<dbo>>.<<Sales>> --" in sql
        # Batch separator came from the dialect.
        assert "/*BATCH*/" in sql
        # SQL Server idioms are absent.
        assert "[Sales]" not in sql
        assert "IF OBJECT_ID" not in sql
        assert "\nGO\n" not in sql

    def test_empty_batch_separator_suppresses_go_lines(self) -> None:
        from src.tools.sql.generate_create_table_scripts import create_table_from_schema

        class _NoSeparator(SqlServerDialect):
            name = "nosep"
            batch_separator = ""

        sql = create_table_from_schema(
            "T",
            [("Id", INT())],
            dialect=_NoSeparator(),
            include_batch_separator=True,
        )
        assert "GO" not in sql.split("\n")
