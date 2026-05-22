"""PostgreSQL dialect renderer.

Type mapping notes:
  - BIT      -> BOOLEAN  (Postgres BIT stores bit strings; BOOLEAN is what
                          our legacy SQL Server BIT flag columns mean)
  - TINYINT  -> SMALLINT (Postgres has no TINYINT)
  - FLOAT    -> DOUBLE PRECISION (standard name; FLOAT is just an alias)
  - DATETIME, DATETIME2 -> TIMESTAMP / TIMESTAMP(p)
  - VARCHAR("MAX") -> TEXT (Postgres has no unbounded VARCHAR;
                            TEXT is the equivalent unbounded string type)

No batch separator (``;`` between statements is enough); ``IF EXISTS``
makes the drop statement idempotent.
"""
from __future__ import annotations

from pathlib import Path

from .base import ColumnSpec, Dialect, SqlType, sql_escape_literal


class PostgresDialect(Dialect):
    name = "postgres"
    load_script_kind = "copy"
    default_schema = "public"
    qualify_load_target = True
    # Idempotent so the script runs even if "public" was dropped or renamed.
    script_preamble = (f'CREATE SCHEMA IF NOT EXISTS "{default_schema}";',)
    # COPY ... FROM is server-side: the path must be readable by the
    # Postgres server process. For local/Docker workflows where psql is
    # used, callers can manually swap COPY for \copy (which reads from the
    # client side).
    load_script_note = "-- NOTE: 'FROM <path>' is evaluated on the Postgres server host."

    _SIMPLE = {
        SqlType.INT: "INTEGER",
        SqlType.BIGINT: "BIGINT",
        SqlType.SMALLINT: "SMALLINT",
        SqlType.TINYINT: "SMALLINT",
        SqlType.BIT: "BOOLEAN",
        SqlType.FLOAT: "DOUBLE PRECISION",
        SqlType.DATE: "DATE",
        SqlType.DATETIME: "TIMESTAMP",
    }

    _PARAM_TEMPLATES = {
        # VARCHAR handled below: "MAX" arg routes to TEXT.
        SqlType.CHAR: "CHAR({0})",
        SqlType.DECIMAL: "DECIMAL({0}, {1})",
        SqlType.DATETIME2: "TIMESTAMP({0})",
        SqlType.TIME: "TIME({0})",
    }

    def _render_base(self, spec: ColumnSpec) -> str:
        if spec.sql_type is SqlType.VARCHAR:
            (n,) = spec.args
            return "TEXT" if n == "MAX" else f"VARCHAR({n})"
        return super()._render_base(spec)

    def quote_ident(self, name: str) -> str:
        raw = self._strip_ident_wrappers(name)
        return '"' + raw.replace('"', '""') + '"'

    def drop_table_if_exists(self, schema: str, table: str) -> str:
        return f"DROP TABLE IF EXISTS {self.qualify(schema, table)};"

    def bulk_load_statement(
        self,
        *,
        schema: str,
        table: str,
        csv_path: Path,
        use_csv_format: bool = False,  # COPY ... FORMAT csv is always CSV-aware.
    ) -> str:
        path_literal = sql_escape_literal(str(csv_path.resolve()))
        return (
            f"COPY {self.qualify(schema, table)}\n"
            f"FROM '{path_literal}'\n"
            f"WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');"
        )
