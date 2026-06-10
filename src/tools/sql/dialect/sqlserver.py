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

    def prepare_load_script(self) -> str | None:
        # Switch to BULK_LOGGED so the existing heap + TABLOCK load becomes
        # minimally logged (the missing third ingredient of the trifecta), and
        # remember the prior model in a global temp table so the finish script
        # can restore it. The pre-grow ALTER is left as a commented template
        # because the right size is hardware/row-width specific
        # (~1.2 × BATCHSIZE × row_bytes × concurrent_sessions).
        return (
            "-- Run ONCE before the load scripts. Reversed by the finish script.\n"
            "-- Minimal logging requires: heap (PK dropped) + TABLOCK + "
            "BULK_LOGGED/SIMPLE recovery.\n"
            "SET NOCOUNT ON;\n"
            "\n"
            "-- Remember the current recovery model so the finish script can restore it.\n"
            "IF OBJECT_ID('tempdb..##sdg_prev_recovery') IS NOT NULL\n"
            "    DROP TABLE ##sdg_prev_recovery;\n"
            "SELECT recovery_model_desc AS prev_model\n"
            "    INTO ##sdg_prev_recovery\n"
            "    FROM sys.databases WHERE name = DB_NAME();\n"
            "\n"
            "ALTER DATABASE CURRENT SET RECOVERY BULK_LOGGED;\n"
            "\n"
            "-- Optional: pre-grow the log up front to avoid autogrowth stalls / VLF\n"
            "-- fragmentation during the load. Size to ~1.2 × BATCHSIZE × row_bytes ×\n"
            "-- concurrent load sessions, then uncomment and set the logical log name:\n"
            "-- ALTER DATABASE CURRENT MODIFY FILE (NAME = N'<your_log_logical_name>', "
            "SIZE = 8GB);"
        )

    def finish_load_script(self) -> str | None:
        # Restore the recovery model captured by the prepare script (defaulting
        # to FULL if it was not captured), then remind the operator to take a
        # log backup to re-establish the log chain after minimal logging.
        return (
            "-- Run ONCE after all load scripts complete. Reverses the prepare script.\n"
            "SET NOCOUNT ON;\n"
            "\n"
            "DECLARE @prev SYSNAME = N'FULL';\n"
            "IF OBJECT_ID('tempdb..##sdg_prev_recovery') IS NOT NULL\n"
            "    SELECT @prev = prev_model FROM ##sdg_prev_recovery;\n"
            "\n"
            "DECLARE @sql NVARCHAR(MAX) =\n"
            "    N'ALTER DATABASE CURRENT SET RECOVERY ' + @prev + N';';\n"
            "EXEC sys.sp_executesql @sql;\n"
            "\n"
            "IF OBJECT_ID('tempdb..##sdg_prev_recovery') IS NOT NULL\n"
            "    DROP TABLE ##sdg_prev_recovery;\n"
            "\n"
            "-- Recommended after returning to FULL recovery: BACKUP LOG [<db>] TO "
            "DISK = N'<path>'; to restart the log chain."
        )

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
        batch_rows: int = 1_000_000,
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
        # BATCHSIZE makes each batch its own transaction so the log can truncate
        # between batches (under SIMPLE/BULK_LOGGED recovery), bounding peak LDF
        # growth to ~one batch instead of the whole table — without it a
        # billion-row load is one transaction whose log must hold the entire file.
        if batch_rows and int(batch_rows) > 0:
            opts.append(f"BATCHSIZE = {int(batch_rows)}")
        opts_sql = ",\n    ".join(opts)

        return (
            f"BULK INSERT {qualified}\n"
            f"FROM N'{path_literal}'\n"
            f"WITH (\n"
            f"    {opts_sql}\n"
            f");"
        )


DEFAULT_DIALECT: Dialect = SqlServerDialect()
