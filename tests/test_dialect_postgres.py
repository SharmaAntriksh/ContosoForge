"""Phase 3: PostgresDialect rendering + end-to-end CREATE TABLE generation.

These tests cover the renderer in isolation and run it through the
real ``generate_all_create_tables`` pipeline against a maximal cfg, asserting
the produced SQL is well-formed Postgres DDL (no T-SQL leakage).

Manual end-to-end verification against a live Postgres
-----------------------------------------------------
The automated tests above are hermetic; they don't actually execute the
generated DDL. To verify Postgres accepts every CREATE TABLE in a real
run, do this once after touching dialect/postgres.py::

    # 1. Generate a small CSV run (Postgres DDL always emits alongside SQL Server)
    python main.py --format csv --sales-rows 1000

    # 2. Start a throwaway Postgres container
    docker run --rm -d --name pg-dialect-check \\
        -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:16

    # 3. Apply the generated DDL (Postgres scripts live at <run>/postgres/)
    psql -h localhost -U postgres -d postgres -v ON_ERROR_STOP=1 \\
        -f generated_datasets/<run>/postgres/schema/01_create_dimensions.sql \\
        -f generated_datasets/<run>/postgres/schema/02_create_facts.sql

    # 4. Spot-check table + column counts
    psql -h localhost -U postgres -d postgres -c \\
        "SELECT table_name, count(*) FROM information_schema.columns
           WHERE table_schema='dbo' GROUP BY table_name ORDER BY table_name;"

    docker rm -f pg-dialect-check
"""
from __future__ import annotations

import re

import pytest

from src.engine.config.config_schema import AppConfig
from src.tools.sql.dialect import (
    ColumnSpec,
    PostgresDialect,
    SqlServerDialect,
    SqlType,
    resolve_dialect,
)
from src.tools.sql.generate_create_table_scripts import (
    create_table_from_schema,
    generate_all_create_tables,
)
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
    STATIC_SCHEMAS,
    TIME,
    TINYINT,
    VARCHAR,
)


class TestPostgresRendering:
    """Every SqlType -> expected Postgres SQL fragment."""

    def setup_method(self) -> None:
        self.dialect = PostgresDialect()

    @pytest.mark.parametrize(
        "spec,expected",
        [
            (ColumnSpec(SqlType.INT, nullable=False), "INTEGER NOT NULL"),
            (ColumnSpec(SqlType.INT, nullable=True), "INTEGER NULL"),
            (ColumnSpec(SqlType.BIGINT, nullable=False), "BIGINT NOT NULL"),
            (ColumnSpec(SqlType.SMALLINT, nullable=False), "SMALLINT NOT NULL"),
            (ColumnSpec(SqlType.TINYINT, nullable=False), "SMALLINT NOT NULL"),
            (ColumnSpec(SqlType.BIT, nullable=False), "BOOLEAN NOT NULL"),
            (ColumnSpec(SqlType.BIT, nullable=True), "BOOLEAN NULL"),
            (ColumnSpec(SqlType.FLOAT, nullable=False), "DOUBLE PRECISION NOT NULL"),
            (ColumnSpec(SqlType.DATE, nullable=False), "DATE NOT NULL"),
            (ColumnSpec(SqlType.DATETIME, nullable=False), "TIMESTAMP NOT NULL"),
            (ColumnSpec(SqlType.DATETIME2, nullable=False, args=(7,)), "TIMESTAMP(7) NOT NULL"),
            (ColumnSpec(SqlType.TIME, nullable=False, args=(0,)), "TIME(0) NOT NULL"),
            (ColumnSpec(SqlType.VARCHAR, nullable=False, args=(100,)), "VARCHAR(100) NOT NULL"),
            (ColumnSpec(SqlType.VARCHAR, nullable=True, args=(50,)), "VARCHAR(50) NULL"),
            # VARCHAR("MAX") is SQL Server's unbounded form -> Postgres TEXT.
            (ColumnSpec(SqlType.VARCHAR, nullable=True, args=("MAX",)), "TEXT NULL"),
            (ColumnSpec(SqlType.CHAR, nullable=False, args=(1,)), "CHAR(1) NOT NULL"),
            (ColumnSpec(SqlType.DECIMAL, nullable=False, args=(8, 2)), "DECIMAL(8, 2) NOT NULL"),
        ],
    )
    def test_render_type(self, spec: ColumnSpec, expected: str) -> None:
        assert self.dialect.render_type(spec) == expected

    def test_simple_type_rejects_args(self) -> None:
        with pytest.raises(ValueError, match="takes no args"):
            self.dialect.render_type(ColumnSpec(SqlType.INT, args=(10,)))


class TestPostgresQuoteIdent:
    def setup_method(self) -> None:
        self.dialect = PostgresDialect()

    def test_simple(self) -> None:
        assert self.dialect.quote_ident("Sales") == '"Sales"'

    def test_strips_existing_double_quotes(self) -> None:
        assert self.dialect.quote_ident('"Sales"') == '"Sales"'

    def test_strips_existing_brackets(self) -> None:
        assert self.dialect.quote_ident("[Sales]") == '"Sales"'

    def test_escapes_embedded_double_quote(self) -> None:
        assert self.dialect.quote_ident('My"Table') == '"My""Table"'

    def test_no_batch_separator(self) -> None:
        assert self.dialect.batch_separator == ""


class TestPostgresDropTable:
    def setup_method(self) -> None:
        self.dialect = PostgresDialect()

    def test_basic(self) -> None:
        assert self.dialect.drop_table_if_exists("public", "Sales") == (
            'DROP TABLE IF EXISTS "public"."Sales";'
        )

    def test_quotes_qualifying_schema(self) -> None:
        # Dialect quotes both halves of the qualifier.
        sql = self.dialect.drop_table_if_exists("Schema With Space", "T")
        assert sql == 'DROP TABLE IF EXISTS "Schema With Space"."T";'


class TestRegistry:
    def test_resolve_sqlserver(self) -> None:
        assert isinstance(resolve_dialect("sqlserver"), SqlServerDialect)

    def test_resolve_postgres(self) -> None:
        assert isinstance(resolve_dialect("postgres"), PostgresDialect)

    def test_case_insensitive(self) -> None:
        assert isinstance(resolve_dialect("Postgres"), PostgresDialect)
        assert isinstance(resolve_dialect("SQLSERVER"), SqlServerDialect)

    def test_unknown_raises_with_valid_list(self) -> None:
        with pytest.raises(ValueError, match=r"Unknown SQL dialect.*Valid: postgres, sqlserver"):
            resolve_dialect("oracle")


class TestPostgresRoundTrip:
    """Every column in STATIC_SCHEMAS must render under PostgresDialect."""

    def test_all_columns_renderable(self) -> None:
        dialect = PostgresDialect()
        for table, schema in STATIC_SCHEMAS.items():
            for col, spec in schema:
                rendered = dialect.render_type(spec)
                assert rendered, f"{table}.{col} produced empty SQL"
                assert rendered.endswith("NULL"), (
                    f"{table}.{col} rendered as {rendered!r} — missing nullability"
                )


class TestCreateTableFromSchemaPostgres:
    def setup_method(self) -> None:
        self.dialect = PostgresDialect()

    def test_basic_create(self) -> None:
        cols = [("Id", INT()), ("Name", VARCHAR(100)), ("Price", DECIMAL(10, 2))]
        sql = create_table_from_schema("Product", cols, dialect=self.dialect)

        assert 'CREATE TABLE "dbo"."Product"' in sql
        assert '"Id" INTEGER NOT NULL' in sql
        assert '"Name" VARCHAR(100) NULL' in sql
        assert '"Price" DECIMAL(10, 2) NOT NULL' in sql
        assert 'DROP TABLE IF EXISTS "dbo"."Product";' in sql

        assert "[" not in sql and "]" not in sql, "SQL Server brackets leaked"
        assert "IF OBJECT_ID" not in sql
        assert "\nGO\n" not in sql

    def test_bit_renders_as_boolean(self) -> None:
        cols = [("Id", INT()), ("Active", BIT(not_null=True))]
        sql = create_table_from_schema("Flag", cols, dialect=self.dialect)
        assert '"Active" BOOLEAN NOT NULL' in sql

    def test_tinyint_collapses_to_smallint(self) -> None:
        cols = [("Id", INT()), ("Rank", TINYINT(not_null=True))]
        sql = create_table_from_schema("Loyalty", cols, dialect=self.dialect)
        assert '"Rank" SMALLINT NOT NULL' in sql

    def test_varchar_max_renders_as_text(self) -> None:
        cols = [("Id", INT()), ("Desc", VARCHAR("MAX"))]
        sql = create_table_from_schema("Store", cols, dialect=self.dialect)
        assert '"Desc" TEXT NULL' in sql

    def test_no_batch_separator(self) -> None:
        cols = [("Id", INT())]
        # include_batch_separator=True is ignored because dialect declares "".
        sql = create_table_from_schema("T", cols, dialect=self.dialect, include_batch_separator=True)
        assert "GO" not in sql.split("\n")


class TestGenerateAllCreateTablesPostgres:
    """End-to-end: feed a maximal cfg through the generator with PostgresDialect."""

    def _maximal_cfg(self) -> AppConfig:
        return AppConfig.model_validate(
            {
                "sales": {"sales_output": "both", "total_rows": 1000},
                "dates": {
                    "include": {
                        "calendar": True,
                        "iso": True,
                        "fiscal": True,
                        "weekly_fiscal": {"enabled": True},
                    }
                },
                "subscriptions": {"enabled": True, "generate_bridge": True},
                "returns": {"enabled": True},
                "budget": {"enabled": True},
                "inventory": {"enabled": True},
                "complaints": {"enabled": True},
                "wishlists": {"enabled": True},
            }
        )

    def test_full_output_is_pure_postgres(self, tmp_path) -> None:
        cfg = self._maximal_cfg()
        dim_path, fact_path = generate_all_create_tables(
            output_folder=tmp_path,
            cfg=cfg,
            dialect=PostgresDialect(),
        )
        dim_sql = dim_path.read_text(encoding="utf-8")
        fact_sql = fact_path.read_text(encoding="utf-8")

        for label, sql in (("dim", dim_sql), ("fact", fact_sql)):
            # No T-SQL idioms.
            assert "IF OBJECT_ID" not in sql, f"{label}: T-SQL IF OBJECT_ID leaked"
            assert re.search(r"\nGO\n", sql) is None, f"{label}: T-SQL GO separator leaked"
            assert "[" not in sql and "]" not in sql, f"{label}: SQL Server brackets leaked"
            # No SQL Server-only types.
            assert " BIT " not in sql.upper(), f"{label}: BIT type leaked (should be BOOLEAN)"
            assert "TINYINT" not in sql.upper(), f"{label}: TINYINT leaked (should be SMALLINT)"
            assert "DATETIME2" not in sql.upper(), f"{label}: DATETIME2 leaked (should be TIMESTAMP)"
            # Postgres idioms present.
            assert "DROP TABLE IF EXISTS" in sql, f"{label}: missing DROP TABLE IF EXISTS"
            assert "CREATE TABLE" in sql, f"{label}: missing CREATE TABLE"
            assert '"dbo".' in sql, f"{label}: schema not double-quoted"

    def test_postgres_output_differs_from_sqlserver(self, tmp_path) -> None:
        """Sanity: rerunning with SQL Server produces different bytes."""
        cfg = self._maximal_cfg()
        pg_dir = tmp_path / "pg"
        ss_dir = tmp_path / "ss"
        pg_dir.mkdir()
        ss_dir.mkdir()

        pg_dim, _ = generate_all_create_tables(output_folder=pg_dir, cfg=cfg, dialect=PostgresDialect())
        ss_dim, _ = generate_all_create_tables(output_folder=ss_dir, cfg=cfg, dialect=SqlServerDialect())

        assert pg_dim.read_text(encoding="utf-8") != ss_dim.read_text(encoding="utf-8")


class TestPackagingRouting:
    """Phase 3 packaging: SQL Server lands at sql/, every other dialect at <run>/<name>/.

    These tests exercise ``write_create_table_scripts`` to lock in the routing
    behaviour — a regression caught here when REGISTRY started holding a
    fresh ``SqlServerDialect()`` instance instead of the ``DEFAULT_DIALECT``
    singleton, which silently sent SQL Server output to ``<run>/sqlserver/``.
    """

    def test_sql_server_lands_at_sql_root(self, tmp_path) -> None:
        from src.engine.packaging.sql_scripts import write_create_table_scripts

        run = tmp_path / "run"
        sql_root = run / "sql"
        dims_out = run / "dims"
        facts_out = run / "facts"
        sql_root.mkdir(parents=True)
        dims_out.mkdir()
        facts_out.mkdir()
        # write_create_table_scripts short-circuits unless CSVs exist
        (dims_out / "Geography.csv").write_text("")

        cfg = AppConfig.model_validate(
            {"sales": {"sales_output": "sales"}, "dates": {}}
        )
        write_create_table_scripts(dims_out=dims_out, facts_out=facts_out, sql_root=sql_root, cfg=cfg)

        assert (sql_root / "schema" / "01_create_dimensions.sql").exists()
        assert (sql_root / "schema" / "02_create_facts.sql").exists()
        assert not (run / "sqlserver").exists(), "SQL Server output must land at sql/, not <run>/sqlserver/"

    def test_postgres_lands_at_run_sibling(self, tmp_path) -> None:
        from src.engine.packaging.sql_scripts import write_create_table_scripts

        run = tmp_path / "run"
        sql_root = run / "sql"
        dims_out = run / "dims"
        facts_out = run / "facts"
        sql_root.mkdir(parents=True)
        dims_out.mkdir()
        facts_out.mkdir()
        (dims_out / "Geography.csv").write_text("")

        cfg = AppConfig.model_validate(
            {"sales": {"sales_output": "sales"}, "dates": {}}
        )
        write_create_table_scripts(dims_out=dims_out, facts_out=facts_out, sql_root=sql_root, cfg=cfg)

        assert (run / "postgres" / "schema" / "01_create_dimensions.sql").exists()
        assert (run / "postgres" / "schema" / "02_create_facts.sql").exists()
        # Postgres output must not contaminate the SQL Server folder.
        assert not (sql_root / "postgres").exists()
