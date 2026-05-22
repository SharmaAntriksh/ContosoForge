"""SQL Server (T-SQL) dialect renderer.

Renders byte-identically to the previous string-based helpers — any change
to spelling, spacing, or ordering here will diverge from existing baseline
CREATE TABLE / BULK INSERT scripts.
"""
from __future__ import annotations

from pathlib import Path

from .base import ColumnSpec, Dialect, SqlType, sql_escape_literal


class SqlServerDialect(Dialect):
    name = "sqlserver"
    batch_separator = "GO"
    default_schema = "dbo"
    script_preamble = ("SET NOCOUNT ON;",)
    load_script_kind = "bulk_insert"
    load_script_note = "-- NOTE: 'FROM <path>' is evaluated on the SQL Server host."

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
        fq = self.qualify(schema, table)
        return (
            f"IF OBJECT_ID(N'{sql_escape_literal(fq)}', N'U') IS NOT NULL\n"
            f"    DROP TABLE {fq};"
        )

    def bulk_load_statement(
        self,
        *,
        schema: str,
        table: str,
        csv_path: Path,
        use_csv_format: bool = False,
    ) -> str:
        qualified = self.qualify(schema, table)
        path_literal = sql_escape_literal(str(csv_path.resolve()))

        # SQL Server 2017+. ROWTERMINATOR must be specified explicitly when
        # FORMAT='CSV': SQL 2025 defaults to '\r\n' and fails on LF-only files
        # ("Cannot obtain the required interface ('IID_IColumnsInfo') ..."). The
        # 2017-2022 series tolerated LF without it.
        opts: list[str] = []
        if use_csv_format:
            opts.append("FORMAT = 'CSV'")
        opts.extend(
            [
                "FIRSTROW = 2",
                "FIELDTERMINATOR = ','",
                "ROWTERMINATOR = '0x0a'",
                "CODEPAGE = '65001'",
                "TABLOCK",
            ]
        )
        opts_sql = ",\n    ".join(opts)

        return (
            f"BULK INSERT {qualified}\n"
            f"FROM N'{path_literal}'\n"
            f"WITH (\n"
            f"    {opts_sql}\n"
            f");"
        )


DEFAULT_DIALECT: Dialect = SqlServerDialect()
