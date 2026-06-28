"""Tests for packaging, SQL generation, and PowerBI modules."""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Mapping

import pytest

from src.engine.config.config_schema import AppConfig, SalesConfig
from src.utils.static_schemas import (
    STATIC_SCHEMAS,
    DIM_SCHEMAS,
    FACT_SCHEMAS,
    get_dates_schema,
    get_sales_schema,
    get_sales_order_header_schema,
    get_sales_order_detail_schema,
    DATE_COLUMN_GROUPS,
)
from src.tools.sql.dialect import ColumnSpec
from src.utils.static_schemas import INT, VARCHAR
from src.engine.packaging.paths import (
    get_first_existing_path,
    to_snake,
    table_dir_name,
    tables_from_sales_cfg,
)
from src.engine.packaging.delta_packager import (
    looks_like_delta_table_dir,
    find_delta_table_dir,
    copy_delta_table_dir,
)
from src.engine.packaging.csv_packager import (
    resolve_csv_table_dir,
    copy_csv_facts,
)
from src.engine.packaging.parquet_packager import (
    resolve_merged_parquet,
    copy_parquet_facts,
)
from src.tools.sql.generate_create_table_scripts import (
    create_table_from_schema,
    generate_all_create_tables,
    _validate_sql_identifier,
)
from src.tools.sql.sql_helpers import (
    sql_escape_literal as _sql_escape_literal,
    quote_ident as _quote_ident,
)
from src.tools.sql.generate_bulk_insert_sql import (
    generate_bulk_insert_script,
    generate_dims_and_facts_bulk_insert_scripts,
    _infer_table_from_filename,
)
from src.engine.powerbi_packaging import (
    _rewrite_expression,
    resolve_pbip_template_root,
)


# ===================================================================
# Static Schemas
# ===================================================================

class TestStaticSchemas:
    """Validate STATIC_SCHEMAS completeness and structure."""

    EXPECTED_DIM_TABLES = {
        "Customers",
        "CustomerProfile",
        "OrganizationProfile",
        "CustomerAcquisitionChannels",
        "Geography",
        "Products",
        "ProductProfile",
        "ProductCategory",
        "ProductSubcategory",
        "Promotions",
        "Stores",
        "Dates",
        "Time",
        "Currency",
        "Employees",
        "EmployeeStoreAssignments",
        "Suppliers",
        "Plans",
        "CustomerSubscriptions",
        "LoyaltyTiers",
        "Channels",
        "ReturnReason",
    }

    EXPECTED_FACT_TABLES = {
        "Sales",
        "OrderHeader",
        "OrderDetail",
        "Returns",
        "ExchangeRates",
        "BudgetYearly",
        "BudgetMonthly",
        "InventorySnapshot",
    }

    def test_all_expected_dim_tables_exist(self):
        for table in self.EXPECTED_DIM_TABLES:
            assert table in STATIC_SCHEMAS, f"Missing dimension table: {table}"

    def test_all_expected_fact_tables_exist(self):
        for table in self.EXPECTED_FACT_TABLES:
            assert table in STATIC_SCHEMAS, f"Missing fact table: {table}"

    def test_dim_schemas_subset_of_static(self):
        for table in DIM_SCHEMAS:
            assert table in STATIC_SCHEMAS

    def test_fact_schemas_subset_of_static(self):
        for table in FACT_SCHEMAS:
            assert table in STATIC_SCHEMAS

    def test_no_overlap_between_dim_and_fact(self):
        overlap = set(DIM_SCHEMAS) & set(FACT_SCHEMAS)
        assert overlap == set(), f"Tables in both DIM and FACT schemas: {overlap}"

    def test_all_schemas_have_at_least_two_columns(self):
        for table, schema in STATIC_SCHEMAS.items():
            assert len(schema) >= 2, f"{table} has only {len(schema)} column(s)"

    def test_schemas_are_tuples_of_pairs(self):
        for table, schema in STATIC_SCHEMAS.items():
            assert isinstance(schema, tuple), f"{table} schema is not a tuple"
            for col, dtype in schema:
                assert isinstance(col, str) and col.strip(), f"{table}: empty column name"
                assert isinstance(dtype, ColumnSpec), (
                    f"{table}: dtype for {col} is {type(dtype).__name__}, expected ColumnSpec"
                )

    def test_no_duplicate_columns_in_any_schema(self):
        for table, schema in STATIC_SCHEMAS.items():
            cols = [col for col, _ in schema]
            assert len(cols) == len(set(cols)), f"{table} has duplicate columns"

    def test_column_counts_are_reasonable(self):
        """Each table should have between 2 and 200 columns.

        Dates legitimately runs ~131 columns once calendar + ISO + fiscal +
        weekly fiscal + as-of offsets are all included; the historical 120
        cap was set before those subsystems landed.
        """
        for table, schema in STATIC_SCHEMAS.items():
            assert 2 <= len(schema) <= 200, (
                f"{table} has {len(schema)} columns, outside reasonable range"
            )

    def test_static_schemas_is_immutable(self):
        with pytest.raises(TypeError):
            STATIC_SCHEMAS["NewTable"] = (("x", "INT"),)

    def test_get_sales_schema_with_order_cols(self):
        schema = get_sales_schema(skip_order_cols=False)
        col_names = [c for c, _ in schema]
        assert "OrderNumber" in col_names
        assert "OrderLineNumber" in col_names

    def test_get_sales_schema_without_order_cols(self):
        schema = get_sales_schema(skip_order_cols=True)
        col_names = [c for c, _ in schema]
        assert "OrderNumber" not in col_names
        assert "OrderLineNumber" not in col_names

    def test_sales_order_header_schema_has_key_columns(self):
        schema = get_sales_order_header_schema()
        col_names = [c for c, _ in schema]
        assert "OrderNumber" in col_names
        assert "CustomerKey" in col_names

    def test_sales_order_detail_schema_has_key_columns(self):
        schema = get_sales_order_detail_schema()
        col_names = [c for c, _ in schema]
        assert "OrderNumber" in col_names
        assert "ProductKey" in col_names

    def test_get_dates_schema_base_only(self):
        dates_cfg = {"include": {"calendar": False, "iso": False, "fiscal": False}}
        schema = get_dates_schema(dates_cfg)
        col_names = {c for c, _ in schema}
        assert "Date" in col_names
        assert "DateKey" in col_names
        assert "Year" in col_names
        # Calendar flags should be absent
        assert "IsToday" not in col_names

    def test_get_dates_schema_all_defaults(self):
        schema = get_dates_schema({})
        col_names = {c for c, _ in schema}
        # Defaults include calendar, iso, fiscal
        assert "IsToday" in col_names
        assert "ISOWeekNumber" in col_names
        assert "FiscalYear" in col_names

    def test_date_column_groups_keys(self):
        expected = {"base", "calendar", "iso", "fiscal", "weekly_fiscal", "base_calendar"}
        assert set(DATE_COLUMN_GROUPS.keys()) == expected


# ===================================================================
# SQL DDL Generation
# ===================================================================

class TestSQLDDLGeneration:
    """Test CREATE TABLE script generation."""

    def test_create_table_basic(self):
        cols = (("Id", INT()), ("Name", VARCHAR(100)))
        sql = create_table_from_schema("TestTable", cols, schema="dbo")
        assert "CREATE TABLE [dbo].[TestTable]" in sql
        assert "[Id] INT NOT NULL" in sql
        assert "[Name] VARCHAR(100) NULL" in sql
        assert sql.count("GO") == 2  # after DROP and after CREATE

    def test_create_table_no_drop(self):
        cols = (("Id", INT()),)
        sql = create_table_from_schema("T", cols, drop_existing=False)
        assert "DROP TABLE" not in sql
        assert "CREATE TABLE" in sql

    def test_create_table_no_go(self):
        cols = (("Id", INT()),)
        sql = create_table_from_schema("T", cols, include_batch_separator=False)
        assert "GO" not in sql

    def test_create_table_drop_uses_object_id(self):
        cols = (("Id", INT()),)
        sql = create_table_from_schema("Products", cols, schema="dbo")
        assert "OBJECT_ID(N'[dbo].[Products]'" in sql

    def test_all_static_schemas_generate_valid_sql(self):
        """Every table in STATIC_SCHEMAS should produce valid-looking SQL."""
        for table_name, cols in STATIC_SCHEMAS.items():
            sql = create_table_from_schema(table_name, cols)
            assert "CREATE TABLE" in sql, f"{table_name}: missing CREATE TABLE"
            assert f"[{table_name}]" in sql, f"{table_name}: table name not found"
            # Each column should appear
            for col, _ in cols:
                assert f"[{col}]" in sql, f"{table_name}.{col}: column not in DDL"

    def test_generate_all_create_tables_files(self, tmp_path):
        """generate_all_create_tables writes two SQL files."""
        cfg = AppConfig.model_validate({
            "sales": {"sales_output": "sales"},
            "dates": {"include": {"calendar": True, "iso": True, "fiscal": True}},
        })
        dim_out, fact_out = generate_all_create_tables(
            output_folder=tmp_path / "sql",
            cfg=cfg,
        )
        assert dim_out.exists()
        assert fact_out.exists()
        dim_sql = dim_out.read_text(encoding="utf-8")
        fact_sql = fact_out.read_text(encoding="utf-8")
        assert "CREATE TABLE" in dim_sql
        assert "CREATE TABLE" in fact_sql
        # Customers should be in dims, Sales in facts
        assert "[Customers]" in dim_sql
        assert "[Sales]" in fact_sql

    def test_generate_all_create_tables_sales_order_mode(self, tmp_path):
        cfg = AppConfig.model_validate({
            "sales": {"sales_output": "sales_order"},
            "dates": {},
        })
        _, fact_out = generate_all_create_tables(
            output_folder=tmp_path / "sql", cfg=cfg,
        )
        fact_sql = fact_out.read_text(encoding="utf-8")
        assert "[OrderHeader]" in fact_sql
        assert "[OrderDetail]" in fact_sql

    def test_generate_all_create_tables_both_mode(self, tmp_path):
        cfg = AppConfig.model_validate({
            "sales": {"sales_output": "both"},
            "dates": {},
        })
        _, fact_out = generate_all_create_tables(
            output_folder=tmp_path / "sql", cfg=cfg,
        )
        fact_sql = fact_out.read_text(encoding="utf-8")
        assert "[Sales]" in fact_sql
        assert "[OrderHeader]" in fact_sql
        assert "[OrderDetail]" in fact_sql

    def test_generate_all_budget_enabled(self, tmp_path):
        cfg = AppConfig.model_validate({
            "sales": {"sales_output": "sales"},
            "dates": {},
            "budget": {"enabled": True},
        })
        _, fact_out = generate_all_create_tables(
            output_folder=tmp_path / "sql", cfg=cfg,
        )
        fact_sql = fact_out.read_text(encoding="utf-8")
        assert "[BudgetYearly]" in fact_sql
        assert "[BudgetMonthly]" in fact_sql

    def test_generate_all_budget_disabled(self, tmp_path):
        cfg = AppConfig.model_validate({
            "sales": {"sales_output": "sales"},
            "dates": {},
            "budget": {"enabled": False},
        })
        _, fact_out = generate_all_create_tables(
            output_folder=tmp_path / "sql", cfg=cfg,
        )
        fact_sql = fact_out.read_text(encoding="utf-8")
        assert "BudgetYearly" not in fact_sql

    def test_generate_all_inventory_enabled(self, tmp_path):
        cfg = AppConfig.model_validate({
            "sales": {"sales_output": "sales"},
            "dates": {},
            "inventory": {"enabled": True},
        })
        _, fact_out = generate_all_create_tables(
            output_folder=tmp_path / "sql", cfg=cfg,
        )
        fact_sql = fact_out.read_text(encoding="utf-8")
        assert "[InventorySnapshot]" in fact_sql

    def test_generate_all_skips_retired_segment_tables(self, tmp_path):
        cfg = AppConfig.model_validate({
            "sales": {"sales_output": "sales"},
            "dates": {},
        })
        dim_out, _ = generate_all_create_tables(
            output_folder=tmp_path / "sql", cfg=cfg,
        )
        dim_sql = dim_out.read_text(encoding="utf-8")
        assert "CREATE TABLE [dbo].[CustomerSegment]" not in dim_sql
        assert "CREATE TABLE [dbo].[CustomerSegmentMembership]" not in dim_sql


# ===================================================================
# SQL Identifier Validation & Escaping
# ===================================================================

class TestSQLIdentifierValidation:
    """Test SQL injection prevention in identifiers."""

    def test_safe_identifiers_pass(self):
        for name in ["Sales", "dbo", "Products", "My Table", "Test_123"]:
            _validate_sql_identifier(name)  # should not raise

    def test_unsafe_identifier_with_semicolon(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("Sales; DROP TABLE--")

    def test_unsafe_identifier_with_single_quote(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("O'Brien")

    def test_unsafe_identifier_with_brackets(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("[Sales]")

    def test_unsafe_identifier_with_double_quote(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier('"Sales"')

    def test_unsafe_identifier_with_dash(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("my-table")

    def test_unsafe_identifier_with_dot(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("dbo.Sales")

    def test_empty_identifier(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("")

    def test_sql_escape_literal_single_quote(self):
        assert _sql_escape_literal("O'Brien") == "O''Brien"

    def test_sql_escape_literal_multiple_quotes(self):
        assert _sql_escape_literal("it's a 'test'") == "it''s a ''test''"

    def test_sql_escape_literal_no_quotes(self):
        assert _sql_escape_literal("safe_string") == "safe_string"

    def test_quote_ident_simple(self):
        assert _quote_ident("Sales") == "[Sales]"

    def test_quote_ident_with_closing_bracket(self):
        # Closing brackets are doubled per SQL Server rules
        assert _quote_ident("My]Table") == "[My]]Table]"

    def test_create_table_escapes_object_id_name(self):
        """Table name with apostrophe in OBJECT_ID should be escaped."""
        cols = (("Id", INT()),)
        # _validate_sql_identifier rejects names with apostrophes,
        # so this tests that the validator catches injection
        with pytest.raises(ValueError, match="Unsafe SQL"):
            create_table_from_schema("O'Brien", cols)


# ===================================================================
# SQL BULK INSERT
# ===================================================================

class TestBulkInsert:
    """Test BULK INSERT script generation."""

    def test_bulk_insert_basic(self, tmp_path):
        """Generate BULK INSERT for a simple CSV folder."""
        csv_dir = tmp_path / "dims"
        csv_dir.mkdir()
        (csv_dir / "Customers.csv").write_text("col1,col2\na,b\n")
        (csv_dir / "Products.csv").write_text("col1,col2\nc,d\n")

        out_sql = tmp_path / "out" / "bulk.sql"
        result = generate_bulk_insert_script(
            str(csv_dir),
            output_sql_file=str(out_sql),
        )
        assert result is not None
        sql = out_sql.read_text(encoding="utf-8")
        assert "BULK INSERT" in sql
        assert "[Customers]" in sql
        assert "[Products]" in sql

    def test_bulk_insert_uses_unicode_prefix(self, tmp_path):
        """File paths must use N'...' Unicode prefix."""
        csv_dir = tmp_path / "data"
        csv_dir.mkdir()
        (csv_dir / "Sales.csv").write_text("col1\n1\n")

        out_sql = tmp_path / "bulk.sql"
        generate_bulk_insert_script(str(csv_dir), output_sql_file=str(out_sql))
        sql = out_sql.read_text(encoding="utf-8")
        assert "FROM N'" in sql

    def test_bulk_insert_escapes_path_with_single_quotes(self, tmp_path):
        """Paths containing single quotes must be escaped."""
        # Create a directory with an apostrophe in the name
        csv_dir = tmp_path / "it's data"
        csv_dir.mkdir()
        (csv_dir / "Sales.csv").write_text("col1\n1\n")

        out_sql = tmp_path / "bulk.sql"
        generate_bulk_insert_script(str(csv_dir), output_sql_file=str(out_sql))
        sql = out_sql.read_text(encoding="utf-8")
        # The single quote in the path should be doubled
        assert "it''s data" in sql

    def test_bulk_insert_no_csv_files_returns_none(self, tmp_path):
        """Empty folder should return None."""
        csv_dir = tmp_path / "empty"
        csv_dir.mkdir()
        result = generate_bulk_insert_script(
            str(csv_dir),
            output_sql_file=str(tmp_path / "out.sql"),
        )
        assert result is None

    def test_bulk_insert_nonexistent_folder_returns_none(self, tmp_path):
        result = generate_bulk_insert_script(
            str(tmp_path / "nonexistent"),
            output_sql_file=str(tmp_path / "out.sql"),
        )
        assert result is None

    def test_bulk_insert_csv_format_per_table(self, tmp_path):
        """csv_format_tables flips FORMAT='CSV' on matching tables only."""
        csv_dir = tmp_path / "data"
        csv_dir.mkdir()
        (csv_dir / "Sales.csv").write_text("col1\n1\n")

        out_sql = tmp_path / "bulk.sql"
        generate_bulk_insert_script(
            str(csv_dir), output_sql_file=str(out_sql),
            csv_format_tables={"Sales"},
        )
        sql = out_sql.read_text(encoding="utf-8")
        assert "FORMAT = 'CSV'" in sql

    def test_bulk_insert_default_omits_csv_format(self, tmp_path):
        """Without csv_format_tables, FORMAT='CSV' is omitted (legacy fast path)."""
        csv_dir = tmp_path / "data"
        csv_dir.mkdir()
        (csv_dir / "Sales.csv").write_text("col1\n1\n")

        out_sql = tmp_path / "bulk.sql"
        generate_bulk_insert_script(str(csv_dir), output_sql_file=str(out_sql))
        sql = out_sql.read_text(encoding="utf-8")
        assert "FORMAT = 'CSV'" not in sql
        assert "FIELDTERMINATOR" in sql

    def test_bulk_insert_allowed_tables_filter(self, tmp_path):
        """Only tables in allowed_tables should appear."""
        csv_dir = tmp_path / "data"
        csv_dir.mkdir()
        (csv_dir / "Customers.csv").write_text("col1\n")
        (csv_dir / "Products.csv").write_text("col1\n")

        out_sql = tmp_path / "bulk.sql"
        generate_bulk_insert_script(
            str(csv_dir),
            output_sql_file=str(out_sql),
            allowed_tables={"Customers"},
        )
        sql = out_sql.read_text(encoding="utf-8")
        assert "[Customers]" in sql
        assert "Products" not in sql

    def test_bulk_insert_recursive(self, tmp_path):
        """Recursive mode scans subdirectories."""
        facts_dir = tmp_path / "facts"
        sales_dir = facts_dir / "sales"
        sales_dir.mkdir(parents=True)
        (sales_dir / "sales_chunk0001.csv").write_text("col1\n1\n")

        out_sql = tmp_path / "bulk.sql"
        generate_bulk_insert_script(
            str(facts_dir),
            output_sql_file=str(out_sql),
            recursive=True,
        )
        sql = out_sql.read_text(encoding="utf-8")
        assert "BULK INSERT" in sql
        assert "[Sales]" in sql

    def test_infer_table_from_filename(self):
        assert _infer_table_from_filename("sales_chunk0001.csv") == "Sales"
        assert _infer_table_from_filename("order_detail_chunk0001.csv") == "OrderDetail"
        assert _infer_table_from_filename("order_header_chunk0001.csv") == "OrderHeader"
        assert _infer_table_from_filename("budget_yearly.csv") == "BudgetYearly"

    def test_generate_dims_and_facts_scripts(self, tmp_path):
        """Convenience wrapper writes two files."""
        dims_dir = tmp_path / "dims"
        dims_dir.mkdir()
        (dims_dir / "Customers.csv").write_text("col1\na\n")

        facts_dir = tmp_path / "facts"
        sales_dir = facts_dir / "sales"
        sales_dir.mkdir(parents=True)
        (sales_dir / "sales_chunk0001.csv").write_text("col1\n1\n")

        load_dir = tmp_path / "load"
        cfg = AppConfig.model_validate({"sales": {"sales_output": "sales"}})

        dims_sql, facts_sql = generate_dims_and_facts_bulk_insert_scripts(
            dims_folder=str(dims_dir),
            facts_folder=str(facts_dir),
            cfg=cfg,
            load_output_folder=str(load_dir),
        )
        assert Path(dims_sql).exists()
        assert Path(facts_sql).exists()


# ===================================================================
# CSV Packager
# ===================================================================

class TestCSVPackager:
    """Test CSV fact file copying."""

    def test_resolve_csv_table_dir_snake_case(self, tmp_path):
        csv_root = tmp_path / "csv" / "sales"
        csv_root.mkdir(parents=True)
        result = resolve_csv_table_dir(tmp_path, "Sales")
        assert result == csv_root

    def test_resolve_csv_table_dir_not_found(self, tmp_path):
        # No csv/ directory at all
        result = resolve_csv_table_dir(tmp_path, "Sales")
        assert result is None

    def test_copy_csv_facts_single_table(self, tmp_path):
        """Copy CSV files for a single sales table."""
        fact_out = tmp_path / "scratch"
        csv_dir = fact_out / "csv" / "sales"
        csv_dir.mkdir(parents=True)
        (csv_dir / "sales_chunk0001.csv").write_text("a,b\n1,2\n")
        (csv_dir / "sales_chunk0002.csv").write_text("a,b\n3,4\n")

        facts_out = tmp_path / "packaged" / "facts"
        facts_out.mkdir(parents=True)

        copy_csv_facts(fact_out=fact_out, facts_out=facts_out, tables=["Sales"])
        # Sales-only should go into facts/sales/
        assert (facts_out / "sales" / "sales_chunk0001.csv").exists()
        assert (facts_out / "sales" / "sales_chunk0002.csv").exists()

    def test_copy_csv_facts_missing_raises(self, tmp_path):
        """Missing CSV dirs should raise RuntimeError."""
        fact_out = tmp_path / "scratch"
        fact_out.mkdir()
        facts_out = tmp_path / "packaged"
        facts_out.mkdir()

        with pytest.raises(RuntimeError, match="No CSV fact outputs found"):
            copy_csv_facts(fact_out=fact_out, facts_out=facts_out, tables=["Sales"])

    def test_copy_csv_facts_duplicate_raises(self, tmp_path):
        """Duplicate filename in destination should raise."""
        fact_out = tmp_path / "scratch"
        csv_dir = fact_out / "csv" / "sales"
        csv_dir.mkdir(parents=True)
        (csv_dir / "chunk.csv").write_text("data\n")

        facts_out = tmp_path / "packaged" / "facts"
        sales_dst = facts_out / "sales"
        sales_dst.mkdir(parents=True)
        (sales_dst / "chunk.csv").write_text("existing\n")

        with pytest.raises(RuntimeError, match="Duplicate CSV filename"):
            copy_csv_facts(fact_out=fact_out, facts_out=facts_out, tables=["Sales"])


# ===================================================================
# Parquet Packager
# ===================================================================

class TestParquetPackager:
    """Test parquet fact file resolution and copying."""

    def test_resolve_merged_parquet_found(self, tmp_path):
        pq_dir = tmp_path / "parquet"
        pq_dir.mkdir()
        pq_file = pq_dir / "sales.parquet"
        pq_file.write_bytes(b"PAR1fake")

        result = resolve_merged_parquet(tmp_path, {"merged_file": "sales.parquet"}, "Sales")
        assert result == pq_file

    def test_resolve_merged_parquet_not_found(self, tmp_path):
        result = resolve_merged_parquet(tmp_path, {}, "Sales")
        assert result is None

    def test_resolve_merged_parquet_non_sales(self, tmp_path):
        pq_dir = tmp_path / "parquet" / "order_detail"
        pq_dir.mkdir(parents=True)
        pq_file = pq_dir / "order_detail.parquet"
        pq_file.write_bytes(b"PAR1fake")

        result = resolve_merged_parquet(tmp_path, {}, "OrderDetail")
        assert result == pq_file

    def test_copy_parquet_facts_success(self, tmp_path):
        """Copy a merged parquet file."""
        fact_out = tmp_path / "scratch"
        pq_dir = fact_out / "parquet"
        pq_dir.mkdir(parents=True)
        (pq_dir / "sales.parquet").write_bytes(b"PAR1fake")

        facts_out = tmp_path / "packaged"
        facts_out.mkdir()

        copy_parquet_facts(
            fact_out=fact_out,
            facts_out=facts_out,
            sales_cfg={"merged_file": "sales.parquet"},
            tables=["Sales"],
        )
        assert (facts_out / "sales.parquet").exists()

    def test_copy_parquet_facts_missing_raises(self, tmp_path):
        fact_out = tmp_path / "scratch"
        fact_out.mkdir()
        facts_out = tmp_path / "packaged"
        facts_out.mkdir()

        with pytest.raises(RuntimeError, match="No merged parquet found"):
            copy_parquet_facts(
                fact_out=fact_out, facts_out=facts_out,
                sales_cfg={}, tables=["Sales"],
            )


# ===================================================================
# Delta Packager
# ===================================================================

class TestDeltaPackager:
    """Test delta table detection and copying."""

    def test_looks_like_delta_table_dir_true(self, tmp_path):
        delta_log = tmp_path / "_delta_log"
        delta_log.mkdir()
        assert looks_like_delta_table_dir(tmp_path) is True

    def test_looks_like_delta_table_dir_false(self, tmp_path):
        assert looks_like_delta_table_dir(tmp_path) is False

    def test_looks_like_delta_table_dir_nonexistent(self, tmp_path):
        assert looks_like_delta_table_dir(tmp_path / "nonexistent") is False

    def test_find_delta_table_dir_found(self, tmp_path):
        sales_dir = tmp_path / "sales"
        (sales_dir / "_delta_log").mkdir(parents=True)

        result = find_delta_table_dir(tmp_path, {}, "Sales")
        assert result == sales_dir

    def test_find_delta_table_dir_not_found(self, tmp_path):
        result = find_delta_table_dir(tmp_path, {}, "Sales")
        assert result is None

    def test_find_delta_table_dir_delta_subdir(self, tmp_path):
        delta_dir = tmp_path / "delta" / "sales"
        (delta_dir / "_delta_log").mkdir(parents=True)

        result = find_delta_table_dir(tmp_path, {}, "Sales")
        assert result == delta_dir

    def test_find_delta_table_dir_cfg_root(self, tmp_path):
        custom_root = tmp_path / "custom"
        custom_sales = custom_root / "sales"
        (custom_sales / "_delta_log").mkdir(parents=True)

        result = find_delta_table_dir(
            tmp_path, SalesConfig.model_validate({"delta_output_folder": str(custom_root)}), "Sales",
        )
        assert result == custom_sales

    def test_copy_delta_table_dir(self, tmp_path):
        src = tmp_path / "src_delta"
        (src / "_delta_log").mkdir(parents=True)
        (src / "_delta_log" / "00000.json").write_text("{}")
        (src / "part-0.parquet").write_bytes(b"fake")

        dst = tmp_path / "dst_delta"
        copy_delta_table_dir(src, dst, skip_dirnames=set())

        assert (dst / "_delta_log" / "00000.json").exists()
        assert (dst / "part-0.parquet").exists()

    def test_copy_delta_table_dir_skips_dirs(self, tmp_path):
        src = tmp_path / "src_delta"
        (src / "_delta_log").mkdir(parents=True)
        (src / "_tmp_parts").mkdir()
        (src / "_tmp_parts" / "junk.txt").write_text("junk")

        dst = tmp_path / "dst_delta"
        copy_delta_table_dir(src, dst, skip_dirnames={"_tmp_parts"})

        assert (dst / "_delta_log").exists()
        assert not (dst / "_tmp_parts").exists()


# ===================================================================
# Package Output - Path Traversal Prevention
# ===================================================================

class TestPackageOutputSecurity:
    """Test that path traversal is blocked in package_output."""

    def test_path_traversal_rejected(self):
        """final_output_folder with '..' should raise ValueError."""
        from src.engine.packaging.package_output import package_output

        cfg = AppConfig.model_validate({
            "defaults": {"final_output": "../../../etc/evil"},
            "sales": {"sales_output": "sales"},
        })
        sales_cfg = SalesConfig.model_validate({
            "file_format": "csv",
            "total_rows": 100,
        })
        with pytest.raises(ValueError, match="must not contain"):
            package_output(cfg, sales_cfg, Path("/tmp/dims"), Path("/tmp/facts"))

    def test_encoded_path_traversal_rejected(self):
        """URL-encoded path traversal should also be caught."""
        from src.engine.packaging.package_output import package_output

        cfg = AppConfig.model_validate({
            "defaults": {"final_output": "..%2F..%2Fetc%2Fevil"},
            "sales": {"sales_output": "sales"},
        })
        sales_cfg = SalesConfig.model_validate({
            "file_format": "csv",
            "total_rows": 100,
        })
        # urllib.parse.unquote will decode %2F to / and %2E to .,
        # so "..%2F.." decodes to "../../" which contains ".."
        with pytest.raises(ValueError, match="must not contain"):
            package_output(cfg, sales_cfg, Path("/tmp/dims"), Path("/tmp/facts"))


# ===================================================================
# Path Utilities
# ===================================================================

class TestPathUtilities:
    """Test path helper functions."""

    def test_to_snake_pascal_case(self):
        assert to_snake("OrderDetail") == "order_detail"

    def test_to_snake_already_snake(self):
        assert to_snake("sales_order_detail") == "sales_order_detail"

    def test_to_snake_single_word(self):
        assert to_snake("Sales") == "sales"

    def test_to_snake_all_caps(self):
        assert to_snake("ABC") == "abc"

    def test_table_dir_name_mapped(self):
        assert table_dir_name("Sales") == "sales"
        assert table_dir_name("OrderDetail") == "order_detail"
        assert table_dir_name("OrderHeader") == "order_header"
        assert table_dir_name("Returns") == "returns"

    def test_table_dir_name_unmapped_uses_snake(self):
        assert table_dir_name("BudgetYearly") == "budget_yearly"

    def test_get_first_existing_path_found(self, tmp_path):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("test: true\n")
        cfg = {"config_yaml_path": str(yaml_file)}
        result = get_first_existing_path(cfg, keys=["config_yaml_path"])
        assert result is not None
        assert result.name == "config.yaml"

    def test_get_first_existing_path_not_found(self):
        cfg = {"config_yaml_path": "/nonexistent/path/config.yaml"}
        result = get_first_existing_path(cfg, keys=["config_yaml_path"])
        assert result is None

    def test_get_first_existing_path_empty_value(self):
        cfg = {"config_yaml_path": ""}
        result = get_first_existing_path(cfg, keys=["config_yaml_path"])
        assert result is None

    def test_get_first_existing_path_missing_key(self):
        cfg = {}
        result = get_first_existing_path(cfg, keys=["config_yaml_path"])
        assert result is None

    def test_get_first_existing_path_tries_multiple_keys(self, tmp_path):
        yaml_file = tmp_path / "model.yaml"
        yaml_file.write_text("test: true\n")
        cfg = {"model_path": str(yaml_file)}
        result = get_first_existing_path(cfg, keys=["model_yaml_path", "model_path"])
        assert result is not None
        assert result.name == "model.yaml"

    def test_tables_from_sales_cfg_sales_mode(self):
        tables = tables_from_sales_cfg(SalesConfig.model_validate({"sales_output": "sales"}))
        assert "Sales" in tables
        assert "OrderDetail" not in tables

    def test_tables_from_sales_cfg_sales_order_mode(self):
        tables = tables_from_sales_cfg(SalesConfig.model_validate({"sales_output": "sales_order"}))
        assert "Sales" not in tables
        assert "OrderDetail" in tables
        assert "OrderHeader" in tables

    def test_tables_from_sales_cfg_both_mode(self):
        tables = tables_from_sales_cfg(SalesConfig.model_validate({"sales_output": "both"}))
        assert "Sales" in tables
        assert "OrderDetail" in tables
        assert "OrderHeader" in tables

    def test_tables_from_sales_cfg_invalid_mode(self):
        with pytest.raises(ValueError, match="Invalid sales_output"):
            tables_from_sales_cfg(SalesConfig.model_validate({"sales_output": "invalid"}))

    def test_tables_from_sales_cfg_returns_enabled(self):
        tables = tables_from_sales_cfg(
            SalesConfig.model_validate({"sales_output": "sales"}),
            cfg=AppConfig.model_validate({"returns": {"enabled": True}}),
        )
        assert "Returns" in tables

    def test_tables_from_sales_cfg_returns_disabled(self):
        tables = tables_from_sales_cfg(
            SalesConfig.model_validate({"sales_output": "sales"}),
            cfg=AppConfig.model_validate({"returns": {"enabled": False}}),
        )
        assert "Returns" not in tables

    def test_tables_from_sales_cfg_returns_blocked_by_skip_order(self):
        """Returns disabled when skip_order_cols=True and sales_output=sales."""
        tables = tables_from_sales_cfg(
            SalesConfig.model_validate({"sales_output": "sales", "skip_order_cols": True}),
            cfg=AppConfig.model_validate({"returns": {"enabled": True}}),
        )
        assert "Returns" not in tables


# ===================================================================
# PowerBI Packaging - TMDL Escaping
# ===================================================================

class TestPowerBIPackaging:
    """Test TMDL expression rewriting and path escaping."""

    def _make_expressions_file(self, tmp_path, content):
        expr_file = tmp_path / "expressions.tmdl"
        expr_file.write_text(content, encoding="utf-8")
        return expr_file

    def test_rewrite_expression_basic(self, tmp_path):
        expr_file = self._make_expressions_file(
            tmp_path,
            'expression ContosoFolder = "C:\\\\old\\\\path"\n',
        )
        final = Path("C:/output/my_dataset")
        _rewrite_expression(
            expressions_file=expr_file,
            expression_name="ContosoFolder",
            final_folder=final,
        )
        text = expr_file.read_text(encoding="utf-8")
        # Path separator is OS-specific: Windows yields escaped backslashes
        # (C:\\output\\...), POSIX yields forward slashes (C:/output/...).
        # Both are valid TMDL M paths.
        assert ("C:\\\\output\\\\my_dataset" in text
                or "C:/output/my_dataset" in text)
        assert "old\\\\path" not in text

    def test_rewrite_expression_backslash_escaping(self, tmp_path):
        """Backslashes in final_folder path must be escaped."""
        expr_file = self._make_expressions_file(
            tmp_path,
            'expression ContosoFolder = "placeholder"\n',
        )
        final = Path("C:\\Users\\test\\output")
        _rewrite_expression(
            expressions_file=expr_file,
            expression_name="ContosoFolder",
            final_folder=final,
        )
        text = expr_file.read_text(encoding="utf-8")
        # Backslashes should be doubled for TMDL M expression
        assert "\\\\" in text or "/" in text  # Path may normalize to forward slashes

    def test_rewrite_expression_quote_escaping(self, tmp_path):
        """Double quotes in final_folder path must be escaped to prevent injection."""
        expr_file = self._make_expressions_file(
            tmp_path,
            'expression ContosoFolder = "placeholder"\n',
        )
        # Simulate a path with a double quote (pathological but tests escaping)
        final = Path('/tmp/my"dataset')
        _rewrite_expression(
            expressions_file=expr_file,
            expression_name="ContosoFolder",
            final_folder=final,
        )
        text = expr_file.read_text(encoding="utf-8")
        # The quote must be escaped as \"
        assert '\\"' in text or 'my"dataset' not in text

    def test_rewrite_expression_missing_raises(self, tmp_path):
        """If expression name not found, should raise RuntimeError."""
        expr_file = self._make_expressions_file(
            tmp_path,
            'expression OtherExpr = "something"\n',
        )
        with pytest.raises(RuntimeError, match="Expected exactly one expression"):
            _rewrite_expression(
                expressions_file=expr_file,
                expression_name="ContosoFolder",
                final_folder=Path("/tmp"),
            )

    def test_rewrite_expression_duplicate_raises(self, tmp_path):
        """Duplicate expression names should raise RuntimeError."""
        expr_file = self._make_expressions_file(
            tmp_path,
            'expression ContosoFolder = "path1"\n'
            'expression ContosoFolder = "path2"\n',
        )
        with pytest.raises(RuntimeError, match="Expected exactly one expression"):
            _rewrite_expression(
                expressions_file=expr_file,
                expression_name="ContosoFolder",
                final_folder=Path("/tmp"),
            )

    def test_resolve_pbip_template_deltaparquet_returns_none(self):
        """deltaparquet format should skip PBIP."""
        result = resolve_pbip_template_root(
            file_format="deltaparquet",
            sales_output="sales",
        )
        assert result is None

    def test_resolve_pbip_template_unsupported_format_raises(self):
        with pytest.raises(ValueError, match="Unsupported file_format"):
            resolve_pbip_template_root(file_format="json", sales_output="sales")

    def test_resolve_pbip_template_unsupported_mode_raises(self):
        with pytest.raises(ValueError, match="Unsupported sales_output"):
            resolve_pbip_template_root(file_format="csv", sales_output="invalid")

    def test_resolve_pbip_template_missing_dir_raises(self, tmp_path):
        """Missing template dir should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="PBIP template not found"):
            resolve_pbip_template_root(
                file_format="csv",
                sales_output="sales",
                templates_root=tmp_path / "nonexistent",
            )

    def test_tmdl_injection_prevention(self, tmp_path):
        """A path with embedded double-quote + M code should not inject."""
        expr_file = self._make_expressions_file(
            tmp_path,
            'expression ContosoFolder = "safe_path"\n',
        )
        # Attacker tries to inject M code via the path
        malicious = Path('/tmp/" & Text.From(1/0) & "')
        _rewrite_expression(
            expressions_file=expr_file,
            expression_name="ContosoFolder",
            final_folder=malicious,
        )
        text = expr_file.read_text(encoding="utf-8")
        # The injected quotes should be escaped, so Text.From should appear
        # inside the string literal, not as executable M code
        # The key check: there should be exactly one expression assignment with
        # properly escaped content
        assert text.count('expression ContosoFolder = "') == 1
        # Escaped quotes should be present (\" not bare ")
        # The malicious content should be inside a single string literal
        lines = [l for l in text.splitlines() if "ContosoFolder" in l]
        assert len(lines) == 1


# ===================================================================
# SQL Scripts Module (sql_scripts.py helpers)
# ===================================================================

class TestSQLScriptsHelpers:
    """Test helper functions in sql_scripts.py."""

    def test_sales_mode_default(self):
        from src.engine.packaging.sql_scripts import _sales_mode
        assert _sales_mode(SalesConfig.model_validate({})) == "sales"

    def test_sales_mode_explicit(self):
        from src.engine.packaging.sql_scripts import _sales_mode
        assert _sales_mode(SalesConfig.model_validate({"sales_output": "sales_order"})) == "sales_order"

    def test_budget_enabled_true(self):
        from src.engine.packaging.sql_scripts import _budget_enabled
        assert _budget_enabled(AppConfig.model_validate({"budget": {"enabled": True}})) is True

    def test_budget_enabled_false(self):
        from src.engine.packaging.sql_scripts import _budget_enabled
        assert _budget_enabled(AppConfig.model_validate({"budget": {"enabled": False}})) is False
        assert _budget_enabled(None) is False

    def test_inventory_enabled_true(self):
        from src.engine.packaging.sql_scripts import _inventory_enabled
        assert _inventory_enabled(AppConfig.model_validate({"inventory": {"enabled": True}})) is True

    def test_inventory_enabled_false(self):
        from src.engine.packaging.sql_scripts import _inventory_enabled
        assert _inventory_enabled(AppConfig.model_validate({"inventory": {"enabled": False}})) is False
        assert _inventory_enabled(None) is False
