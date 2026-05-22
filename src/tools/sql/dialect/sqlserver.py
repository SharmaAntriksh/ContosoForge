"""SQL Server (T-SQL) dialect renderer.

Renders byte-identically to the previous string-based helpers — any change
to spelling, spacing, or ordering here will diverge from existing baseline
CREATE TABLE scripts and break downstream BULK INSERT scripts.
"""
from __future__ import annotations

from .base import ColumnSpec, Dialect, SqlType


class SqlServerDialect(Dialect):
    name = "sqlserver"
    batch_separator = "GO"
    script_preamble = ("SET NOCOUNT ON;",)

    _SIMPLE = {
        SqlType.INT: "INT",
        SqlType.BIGINT: "BIGINT",
        SqlType.SMALLINT: "SMALLINT",
        SqlType.TINYINT: "TINYINT",
        SqlType.BIT: "BIT",
        SqlType.FLOAT: "FLOAT",
        SqlType.DATE: "DATE",
        SqlType.DATETIME: "DATETIME",
    }

    _PARAM_TEMPLATES = {
        SqlType.VARCHAR: "VARCHAR({0})",
        SqlType.CHAR: "CHAR({0})",
        SqlType.DECIMAL: "DECIMAL({0}, {1})",
        SqlType.DATETIME2: "DATETIME2({0})",
        SqlType.TIME: "TIME({0})",
    }

    def quote_ident(self, name: str) -> str:
        raw = self._strip_ident_wrappers(name)
        return f"[{raw.replace(']', ']]')}]"

    def drop_table_if_exists(self, schema: str, table: str) -> str:
        fq = f"{self.quote_ident(schema)}.{self.quote_ident(table)}"
        literal = fq.replace("'", "''")
        return (
            f"IF OBJECT_ID(N'{literal}', N'U') IS NOT NULL\n"
            f"    DROP TABLE {fq};"
        )


DEFAULT_DIALECT: Dialect = SqlServerDialect()
