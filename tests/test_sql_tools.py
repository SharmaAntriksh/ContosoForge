"""Comprehensive tests for the SQL tools modules.

Covers:
  - sql_helpers (sql_escape_literal, quote_ident, returns_enabled, budget_enabled,
    inventory_enabled, complaints_enabled, wishlists_enabled)
  - generate_create_table_scripts (create_table_from_schema, _validate_sql_identifier,
    _sales_output_mode, _skip_order_cols)
  - generate_bulk_insert_sql (_quote_table, _infer_table_from_filename, _allowed_lookup,
    codepage validation via generate_bulk_insert_script)
  - sql_server_import (_extract_table_from_batch, _collect_phase_scripts,
    _is_view_file, _is_constraint_file, _is_cci_file, _is_verify_file)
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.tools.sql.sql_helpers import (
    sql_escape_literal,
    quote_ident,
    returns_enabled,
    budget_enabled,
    inventory_enabled,
    complaints_enabled,
    wishlists_enabled,
)
from src.tools.sql.generate_create_table_scripts import (
    create_table_from_schema,
    generate_all_create_tables,
    _validate_sql_identifier,
    _sales_output_mode,
    _skip_order_cols,
)
from src.utils.static_schemas import (
    DECIMAL, INT, BIGINT, VARCHAR, _INT32_HALF, promote_order_number,
)
from src.tools.sql.generate_bulk_insert_sql import (
    _split_qualified,
    _infer_table_from_filename,
    _allowed_lookup,
    generate_bulk_insert_script,
)
from src.tools.sql.sql_server_import import (
    _extract_table_from_batch,
    _collect_phase_scripts,
    _is_view_file,
    _is_constraint_file,
    _is_cci_file,
    _is_verify_file,
)


# ===================================================================
# Helper to build cfg-like namespaces for feature-flag tests
# ===================================================================

class _AttrDict(dict):
    """A dict subclass that supports attribute access — satisfies both
    ``isinstance(x, Mapping)`` (required by sql_helpers) and
    ``getattr(x, key)`` (used by the same helpers on inner sections).
    """
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self[key] = value


def _ns(**kwargs):
    """Build a SimpleNamespace whose inner dicts are ``_AttrDict`` instances.

    The top-level object is a SimpleNamespace (so ``getattr(cfg, 'sales')``
    works), and each inner dict becomes an ``_AttrDict`` which is both a
    ``Mapping`` and supports attribute access — matching how the real
    Pydantic config objects behave for the code under test.
    """
    for k, v in kwargs.items():
        if isinstance(v, dict):
            kwargs[k] = _AttrDict(v)
    return SimpleNamespace(**kwargs)


# ===================================================================
# 1. sql_helpers — sql_escape_literal
# ===================================================================

class TestSqlEscapeLiteral:
    def test_empty_string(self):
        assert sql_escape_literal("") == ""

    def test_no_quotes(self):
        assert sql_escape_literal("hello world") == "hello world"

    def test_single_quote(self):
        assert sql_escape_literal("it's") == "it''s"

    def test_multiple_quotes(self):
        assert sql_escape_literal("it's a 'test'") == "it''s a ''test''"

    def test_consecutive_quotes(self):
        assert sql_escape_literal("''") == "''''"

    def test_unicode_unchanged(self):
        assert sql_escape_literal("caf\u00e9 \u2603") == "caf\u00e9 \u2603"

    def test_unicode_with_quote(self):
        assert sql_escape_literal("l'\u00e9t\u00e9") == "l''\u00e9t\u00e9"


# ===================================================================
# 2. sql_helpers — quote_ident
# ===================================================================

class TestQuoteIdent:
    def test_simple_name(self):
        assert quote_ident("Sales") == "[Sales]"

    def test_already_bracketed(self):
        assert quote_ident("[Sales]") == "[Sales]"

    def test_already_double_quoted(self):
        assert quote_ident('"Sales"') == "[Sales]"

    def test_contains_closing_bracket(self):
        assert quote_ident("My]Table") == "[My]]Table]"

    def test_already_bracketed_with_closing_bracket(self):
        # Input: [My]Table] -> strips outer brackets -> My]Table -> escapes
        assert quote_ident("[My]Table]") == "[My]]Table]"

    def test_empty_string(self):
        # Empty after strip -> []
        assert quote_ident("") == "[]"

    def test_whitespace_stripped(self):
        assert quote_ident("  Sales  ") == "[Sales]"

    def test_numeric_coerced_to_string(self):
        assert quote_ident(123) == "[123]"

    def test_name_with_spaces(self):
        assert quote_ident("My Table") == "[My Table]"


# ===================================================================
# 3. sql_helpers — returns_enabled
# ===================================================================

class TestReturnsEnabled:
    def test_cfg_none_returns_true(self):
        assert returns_enabled(None) is True

    def test_returns_disabled_explicitly(self):
        cfg = _ns(returns={"enabled": False}, sales={"sales_output": "sales", "skip_order_cols": False})
        assert returns_enabled(cfg) is False

    def test_returns_enabled_explicitly(self):
        cfg = _ns(returns={"enabled": True}, sales={"sales_output": "sales", "skip_order_cols": False})
        assert returns_enabled(cfg) is True

    def test_skip_order_cols_with_sales_output_disables(self):
        cfg = _ns(returns={"enabled": True}, sales={"sales_output": "sales", "skip_order_cols": True})
        assert returns_enabled(cfg) is False

    def test_skip_order_cols_with_sales_order_output_keeps_enabled(self):
        cfg = _ns(returns={"enabled": True}, sales={"sales_output": "sales_order", "skip_order_cols": True})
        assert returns_enabled(cfg) is True

    def test_no_returns_section(self):
        cfg = _ns(sales={"sales_output": "sales", "skip_order_cols": False})
        assert returns_enabled(cfg) is True

    def test_no_sales_section(self):
        cfg = _ns(returns={"enabled": True})
        assert returns_enabled(cfg) is True


# ===================================================================
# 4. sql_helpers — budget_enabled
# ===================================================================

class TestBudgetEnabled:
    def test_cfg_none_returns_false(self):
        assert budget_enabled(None) is False

    def test_section_missing(self):
        cfg = _ns(sales={"total_rows": 100})
        assert budget_enabled(cfg) is False

    def test_enabled_true(self):
        cfg = _ns(budget={"enabled": True})
        assert budget_enabled(cfg) is True

    def test_enabled_false(self):
        cfg = _ns(budget={"enabled": False})
        assert budget_enabled(cfg) is False

    def test_enabled_missing_defaults_false(self):
        cfg = _ns(budget={"other": 1})
        assert budget_enabled(cfg) is False


# ===================================================================
# 5. sql_helpers — inventory_enabled
# ===================================================================

class TestInventoryEnabled:
    def test_cfg_none_returns_false(self):
        assert inventory_enabled(None) is False

    def test_section_missing(self):
        cfg = _ns(sales={"total_rows": 100})
        assert inventory_enabled(cfg) is False

    def test_enabled_true(self):
        cfg = _ns(inventory={"enabled": True})
        assert inventory_enabled(cfg) is True

    def test_enabled_false(self):
        cfg = _ns(inventory={"enabled": False})
        assert inventory_enabled(cfg) is False


# ===================================================================
# 6. sql_helpers — complaints_enabled
# ===================================================================

class TestComplaintsEnabled:
    def test_cfg_none_returns_false(self):
        assert complaints_enabled(None) is False

    def test_section_missing(self):
        cfg = _ns(other={"x": 1})
        assert complaints_enabled(cfg) is False

    def test_enabled_true(self):
        cfg = _ns(complaints={"enabled": True})
        assert complaints_enabled(cfg) is True

    def test_enabled_false(self):
        cfg = _ns(complaints={"enabled": False})
        assert complaints_enabled(cfg) is False


# ===================================================================
# 7. sql_helpers — wishlists_enabled
# ===================================================================

class TestWishlistsEnabled:
    def test_cfg_none_returns_false(self):
        assert wishlists_enabled(None) is False

    def test_section_missing(self):
        cfg = _ns(other={"x": 1})
        assert wishlists_enabled(cfg) is False

    def test_enabled_true(self):
        cfg = _ns(wishlists={"enabled": True})
        assert wishlists_enabled(cfg) is True

    def test_enabled_false(self):
        cfg = _ns(wishlists={"enabled": False})
        assert wishlists_enabled(cfg) is False


# ===================================================================
# 8. generate_create_table_scripts — _validate_sql_identifier
# ===================================================================

class TestValidateSqlIdentifier:
    def test_simple_name_ok(self):
        _validate_sql_identifier("Sales")  # no exception

    def test_underscore_start_ok(self):
        _validate_sql_identifier("_MyTable")

    def test_name_with_spaces_ok(self):
        _validate_sql_identifier("My Table")

    def test_alphanumeric_with_underscore_ok(self):
        _validate_sql_identifier("Table_123")

    def test_leading_digit_fails(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("1Table")

    def test_special_chars_fail(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("Table;DROP")

    def test_brackets_fail(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("[Sales]")

    def test_dot_fails(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("dbo.Sales")

    def test_empty_string_fails(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            _validate_sql_identifier("")

    def test_custom_label_in_error(self):
        with pytest.raises(ValueError, match="table name"):
            _validate_sql_identifier("123", label="table name")


# ===================================================================
# 9. generate_create_table_scripts — create_table_from_schema
# ===================================================================

class TestCreateTableFromSchema:
    def test_basic_ddl_structure(self):
        cols = [("ID", INT()), ("Name", VARCHAR(100))]
        ddl = create_table_from_schema("TestTable", cols)
        assert "CREATE TABLE [dbo].[TestTable]" in ddl
        assert "[ID] INT NOT NULL," in ddl
        assert "[Name] VARCHAR(100) NULL" in ddl
        # Last column should not have trailing comma
        lines = ddl.splitlines()
        col_lines = [l for l in lines if l.strip().startswith("[Name]")]
        assert len(col_lines) == 1
        assert not col_lines[0].rstrip().endswith(",")

    def test_drop_existing_present_by_default(self):
        cols = [("ID", INT())]
        ddl = create_table_from_schema("Foo", cols)
        assert "DROP TABLE" in ddl
        assert "IF OBJECT_ID" in ddl

    def test_drop_existing_false(self):
        cols = [("ID", INT())]
        ddl = create_table_from_schema("Foo", cols, drop_existing=False)
        assert "DROP TABLE" not in ddl
        assert "IF OBJECT_ID" not in ddl

    def test_include_batch_separator_true(self):
        cols = [("ID", INT())]
        ddl = create_table_from_schema("Foo", cols, include_batch_separator=True)
        assert ddl.count("GO") >= 1

    def test_include_batch_separator_false(self):
        cols = [("ID", INT())]
        ddl = create_table_from_schema("Foo", cols, include_batch_separator=False)
        assert "GO" not in ddl

    def test_custom_schema(self):
        cols = [("ID", INT())]
        ddl = create_table_from_schema("Foo", cols, schema="staging")
        assert "[staging].[Foo]" in ddl

    def test_empty_cols(self):
        ddl = create_table_from_schema("Empty", [])
        assert "CREATE TABLE [dbo].[Empty]" in ddl

    def test_multiple_columns(self):
        cols = [
            ("Col1", INT()),
            ("Col2", VARCHAR(50)),
            ("Col3", DECIMAL(10, 2)),
        ]
        ddl = create_table_from_schema("Multi", cols)
        assert "[Col1] INT NOT NULL," in ddl
        assert "[Col2] VARCHAR(50) NULL," in ddl
        # Last column should NOT have trailing comma
        assert "[Col3] DECIMAL(10, 2) NOT NULL" in ddl
        assert "[Col3] DECIMAL(10, 2) NOT NULL," not in ddl

    def test_invalid_table_name_raises(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            create_table_from_schema("1Invalid", [("ID", INT())])

    def test_invalid_schema_name_raises(self):
        with pytest.raises(ValueError, match="Unsafe SQL"):
            create_table_from_schema("Foo", [("ID", INT())], schema="bad;schema")


# ===================================================================
# 10. generate_create_table_scripts — _sales_output_mode
# ===================================================================

class TestSalesOutputMode:
    def test_default_sales(self):
        cfg = _ns(sales={"sales_output": "sales"})
        assert _sales_output_mode(cfg) == "sales"

    def test_sales_order(self):
        cfg = _ns(sales={"sales_output": "sales_order"})
        assert _sales_output_mode(cfg) == "sales_order"

    def test_both(self):
        cfg = _ns(sales={"sales_output": "both"})
        assert _sales_output_mode(cfg) == "both"

    def test_case_insensitive(self):
        cfg = _ns(sales={"sales_output": "SALES"})
        assert _sales_output_mode(cfg) == "sales"

    def test_no_sales_section_defaults(self):
        cfg = _ns()
        assert _sales_output_mode(cfg) == "sales"

    def test_invalid_mode_raises(self):
        cfg = _ns(sales={"sales_output": "invalid"})
        with pytest.raises(ValueError, match="Invalid sales.sales_output"):
            _sales_output_mode(cfg)


# ===================================================================
# 11. generate_create_table_scripts — _skip_order_cols
# ===================================================================

class TestSkipOrderCols:
    def test_explicit_true(self):
        cfg = _ns(sales={"skip_order_cols": True})
        assert _skip_order_cols(cfg, False) is True

    def test_explicit_false(self):
        cfg = _ns(sales={"skip_order_cols": False})
        assert _skip_order_cols(cfg, True) is False

    def test_missing_uses_default_true(self):
        cfg = _ns(sales={})
        assert _skip_order_cols(cfg, True) is True

    def test_missing_uses_default_false(self):
        cfg = _ns(sales={})
        assert _skip_order_cols(cfg, False) is False

    def test_no_sales_section_uses_default(self):
        cfg = _ns()
        assert _skip_order_cols(cfg, True) is True


# ===================================================================
# 12. generate_bulk_insert_sql — _split_qualified
# ===================================================================

class TestSplitQualified:
    def test_unqualified(self):
        assert _split_qualified("Sales") == ("", "Sales")

    def test_schema_dot_table(self):
        assert _split_qualified("dbo.Sales") == ("dbo", "Sales")

    def test_strips_existing_brackets(self):
        assert _split_qualified("[dbo].[Sales]") == ("dbo", "Sales")

    def test_strips_existing_double_quotes(self):
        assert _split_qualified('"dbo"."Sales"') == ("dbo", "Sales")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Empty table name"):
            _split_qualified("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="Empty table name"):
            _split_qualified("   ")

    def test_too_many_parts_raises(self):
        with pytest.raises(ValueError, match="Too many name parts"):
            _split_qualified("a.b.c")


# ===================================================================
# 13. generate_bulk_insert_sql — _infer_table_from_filename
# ===================================================================

class TestInferTableFromFilename:
    def test_sales_chunk(self):
        assert _infer_table_from_filename("sales_chunk0001.csv") == "Sales"

    def test_sales_order_detail_chunk(self):
        assert _infer_table_from_filename("sales_order_detail_chunk0002.csv") == "SalesOrderDetail"

    def test_no_chunk_suffix(self):
        assert _infer_table_from_filename("customers.csv") == "Customers"

    def test_part_suffix(self):
        assert _infer_table_from_filename("products_part001.csv") == "Products"

    def test_nested_path(self):
        assert _infer_table_from_filename("data/facts/sales/sales_chunk0001.csv") == "Sales"

    def test_budget_yearly(self):
        assert _infer_table_from_filename("budget_yearly.csv") == "BudgetYearly"

    def test_inventory_snapshot_chunk(self):
        assert _infer_table_from_filename("inventory_snapshot_chunk0001.csv") == "InventorySnapshot"

    def test_case_insensitive_chunk_suffix(self):
        assert _infer_table_from_filename("sales_CHUNK0001.csv") == "Sales"


# ===================================================================
# 14. generate_bulk_insert_sql — _allowed_lookup
# ===================================================================

class TestAllowedLookup:
    def test_none_returns_none(self):
        assert _allowed_lookup(None) is None

    def test_empty_set_returns_none(self):
        assert _allowed_lookup(set()) is None

    def test_case_insensitive_keys(self):
        result = _allowed_lookup({"Sales", "BudgetYearly"})
        assert result is not None
        assert "sales" in result
        assert "budgetyearly" in result

    def test_canonical_values_preserved(self):
        result = _allowed_lookup({"Sales", "BudgetYearly"})
        assert result["sales"] == "Sales"
        assert result["budgetyearly"] == "BudgetYearly"

    def test_whitespace_stripped(self):
        result = _allowed_lookup({"  Sales  "})
        assert "sales" in result
        assert result["sales"] == "Sales"


# The codepage value (formerly user-supplied) is now hardcoded inside
# SqlServerDialect.bulk_load_statement, so the injection-via-codepage
# concern that motivated the legacy TestCodepageValidation class no longer
# applies — there is no caller-supplied codepage to validate.

    def test_missing_folder_returns_none(self, tmp_path):
        result = generate_bulk_insert_script(
            tmp_path / "nonexistent",
            output_sql_file=str(tmp_path / "out.sql"),
        )
        assert result is None

    def test_empty_folder_returns_none(self, tmp_path):
        csv_dir = tmp_path / "csv"
        csv_dir.mkdir()
        result = generate_bulk_insert_script(
            csv_dir,
            output_sql_file=str(tmp_path / "out.sql"),
        )
        assert result is None

    def test_output_contains_bulk_insert(self, tmp_path):
        csv_dir = tmp_path / "csv"
        csv_dir.mkdir()
        (csv_dir / "customers.csv").write_text("a,b\n1,2\n")
        out_sql = tmp_path / "out.sql"
        generate_bulk_insert_script(
            csv_dir,
            output_sql_file=str(out_sql),
        )
        content = out_sql.read_text(encoding="utf-8")
        assert "BULK INSERT" in content
        assert "[Customers]" in content

    def test_unicode_prefix_in_from_path(self, tmp_path):
        csv_dir = tmp_path / "csv"
        csv_dir.mkdir()
        (csv_dir / "sales.csv").write_text("a,b\n1,2\n")
        out_sql = tmp_path / "out.sql"
        generate_bulk_insert_script(
            csv_dir,
            output_sql_file=str(out_sql),
        )
        content = out_sql.read_text(encoding="utf-8")
        assert "FROM N'" in content


# ===================================================================
# 16. sql_server_import — _extract_table_from_batch
# ===================================================================

class TestExtractTableFromBatch:
    def test_bulk_insert_dbo_table(self):
        sql = "BULK INSERT dbo.Sales\nFROM N'path'\nWITH (...);"
        assert _extract_table_from_batch(sql) == "Sales"

    def test_bulk_insert_bracketed(self):
        sql = "BULK INSERT [dbo].[Products]\nFROM N'path'\nWITH (...);"
        assert _extract_table_from_batch(sql) == "Products"

    def test_bulk_insert_no_schema(self):
        sql = "BULK INSERT Customers\nFROM N'path';"
        assert _extract_table_from_batch(sql) == "Customers"

    def test_insert_into_bracketed(self):
        sql = "INSERT INTO [dbo].[Sales] (Col1) VALUES (1);"
        assert _extract_table_from_batch(sql) == "Sales"

    def test_insert_without_into(self):
        sql = "INSERT [Sales] (Col1) VALUES (1);"
        assert _extract_table_from_batch(sql) == "Sales"

    def test_no_match_returns_empty(self):
        sql = "SELECT * FROM Sales;"
        assert _extract_table_from_batch(sql) == ""

    def test_empty_string(self):
        assert _extract_table_from_batch("") == ""

    def test_case_insensitive(self):
        sql = "bulk insert DBO.MyTable\nFROM N'path';"
        assert _extract_table_from_batch(sql) == "MyTable"


# ===================================================================
# 17. sql_server_import — _is_view_file / _is_constraint_file /
#     _is_cci_file / _is_verify_file
# ===================================================================

class TestIsViewFile:
    def test_view_in_name(self):
        assert _is_view_file(Path("03_create_views.sql")) is True

    def test_views_in_name(self):
        assert _is_view_file(Path("views.sql")) is True

    def test_no_match(self):
        assert _is_view_file(Path("01_create_tables.sql")) is False

    def test_case_insensitive(self):
        assert _is_view_file(Path("Create_Views.sql")) is True


class TestIsConstraintFile:
    def test_constraint_in_name(self):
        assert _is_constraint_file(Path("04_constraints.sql")) is True

    def test_fk_prefix(self):
        assert _is_constraint_file(Path("fk_sales.sql")) is True

    def test_fk_infix(self):
        assert _is_constraint_file(Path("add_fk_sales.sql")) is True

    def test_foreignkey(self):
        assert _is_constraint_file(Path("foreignkey_setup.sql")) is True

    def test_foreign_key_underscore(self):
        assert _is_constraint_file(Path("foreign_key_setup.sql")) is True

    def test_pk_prefix(self):
        assert _is_constraint_file(Path("pk_customers.sql")) is True

    def test_pk_infix(self):
        assert _is_constraint_file(Path("add_pk_customers.sql")) is True

    def test_primarykey(self):
        assert _is_constraint_file(Path("primarykey.sql")) is True

    def test_primary_key_underscore(self):
        assert _is_constraint_file(Path("primary_key.sql")) is True

    def test_no_match(self):
        assert _is_constraint_file(Path("01_create_tables.sql")) is False


class TestIsCciFile:
    def test_cci_in_name(self):
        assert _is_cci_file(Path("05_cci_apply.sql")) is True

    def test_columnstore_in_name(self):
        assert _is_cci_file(Path("columnstore_indexes.sql")) is True

    def test_no_match(self):
        assert _is_cci_file(Path("01_create_tables.sql")) is False


class TestIsVerifyFile:
    def test_verify_in_name(self):
        assert _is_verify_file(Path("06_verify.sql")) is True

    def test_no_match(self):
        assert _is_verify_file(Path("01_create_tables.sql")) is False

    def test_verify_checks(self):
        assert _is_verify_file(Path("verify_checks.sql")) is True


# ===================================================================
# 18. sql_server_import — _collect_phase_scripts
# ===================================================================

class TestCollectPhaseScripts:
    def test_empty_dir_returns_five_empty_lists(self, tmp_path):
        tables, views, constraints, cci, verify = _collect_phase_scripts(tmp_path)
        assert tables == []
        assert views == []
        assert constraints == []
        assert cci == []
        assert verify == []

    def test_no_schema_dir_returns_five_empty_lists(self, tmp_path):
        # schema/ doesn't exist at all
        tables, views, constraints, cci, verify = _collect_phase_scripts(tmp_path)
        assert all(lst == [] for lst in (tables, views, constraints, cci, verify))

    def test_layout_a_structured_subdirs(self, tmp_path):
        """Layout A: schema/tables/, schema/views/, schema/constraints/ subdirectories."""
        schema_dir = tmp_path / "schema"
        tables_dir = schema_dir / "tables"
        views_dir = schema_dir / "views"
        constraints_dir = schema_dir / "constraints"
        cci_dir = tmp_path / "cci"

        for d in (tables_dir, views_dir, constraints_dir, cci_dir):
            d.mkdir(parents=True)

        (tables_dir / "01_dims.sql").write_text("CREATE TABLE dbo.Foo (ID INT);")
        (tables_dir / "02_facts.sql").write_text("CREATE TABLE dbo.Bar (ID INT);")
        (views_dir / "01_views.sql").write_text("CREATE VIEW dbo.vFoo AS SELECT 1;")
        (constraints_dir / "01_fk.sql").write_text("ALTER TABLE dbo.Bar ADD CONSTRAINT ..;")
        (cci_dir / "01_cci.sql").write_text("CREATE CLUSTERED COLUMNSTORE INDEX ..;")

        tables, views, constraints, cci, verify = _collect_phase_scripts(tmp_path)

        assert len(tables) == 2
        assert len(views) == 1
        assert len(constraints) == 1
        assert len(cci) == 1
        assert verify == []

    def test_layout_b_flat_schema(self, tmp_path):
        """Layout B: all .sql files in schema/ (flat), classified by name."""
        schema_dir = tmp_path / "schema"
        schema_dir.mkdir()

        (schema_dir / "01_create_tables.sql").write_text("CREATE TABLE ..;")
        (schema_dir / "02_create_views.sql").write_text("CREATE VIEW ..;")
        (schema_dir / "03_constraints.sql").write_text("ALTER TABLE ..;")
        (schema_dir / "04_cci_apply.sql").write_text("CREATE CLUSTERED COLUMNSTORE ..;")
        (schema_dir / "05_verify.sql").write_text("EXEC verify.RunAll;")

        tables, views, constraints, cci, verify = _collect_phase_scripts(tmp_path)

        assert len(tables) == 1  # only 01_create_tables.sql
        assert len(views) == 1
        assert len(constraints) == 1
        assert len(cci) == 1
        assert len(verify) == 1

    def test_layout_b_with_top_level_views_dir(self, tmp_path):
        """Layout B: flat schema/ plus sql/views/ directory takes precedence."""
        schema_dir = tmp_path / "schema"
        schema_dir.mkdir()
        views_dir = tmp_path / "views"
        views_dir.mkdir()

        (schema_dir / "01_tables.sql").write_text("CREATE TABLE ..;")
        (schema_dir / "02_create_views.sql").write_text("-- view in schema, ignored")
        (views_dir / "01_views.sql").write_text("CREATE VIEW ..;")

        tables, views, constraints, cci, verify = _collect_phase_scripts(tmp_path)

        # views_dir takes precedence over inferred view files from schema_dir
        assert len(views) == 1
        assert views[0].parent == views_dir

    def test_layout_a_cci_from_indexes_dir(self, tmp_path):
        """CCI files can come from sql/indexes/ directory too."""
        schema_dir = tmp_path / "schema"
        tables_dir = schema_dir / "tables"
        tables_dir.mkdir(parents=True)
        indexes_dir = tmp_path / "indexes"
        indexes_dir.mkdir()

        (tables_dir / "01.sql").write_text("CREATE TABLE ..;")
        (indexes_dir / "cci_facts.sql").write_text("CREATE CLUSTERED COLUMNSTORE ..;")
        (indexes_dir / "nonclustered.sql").write_text("CREATE NONCLUSTERED INDEX ..;")

        tables, views, constraints, cci, verify = _collect_phase_scripts(tmp_path)

        # Only cci_facts.sql matches _is_cci_file
        assert len(cci) == 1
        assert "cci_facts" in cci[0].name

    def test_layout_b_deduplicates_cci(self, tmp_path):
        """CCI scripts should be deduplicated."""
        schema_dir = tmp_path / "schema"
        schema_dir.mkdir()
        cci_dir = tmp_path / "cci"
        cci_dir.mkdir()

        (schema_dir / "01_tables.sql").write_text("CREATE TABLE ..;")
        (schema_dir / "05_cci_apply.sql").write_text("CCI from schema")
        (cci_dir / "05_cci_apply.sql").write_text("CCI from cci dir")

        tables, views, constraints, cci, verify = _collect_phase_scripts(tmp_path)

        # Both sources contribute, no duplicates (different Path objects)
        assert len(cci) == 2

    def test_empty_schema_dir(self, tmp_path):
        """Schema dir exists but is empty."""
        schema_dir = tmp_path / "schema"
        schema_dir.mkdir()

        tables, views, constraints, cci, verify = _collect_phase_scripts(tmp_path)
        assert all(lst == [] for lst in (tables, views, constraints, cci, verify))


# ===================================================================
# SalesOrderNumber INT->BIGINT DDL promotion (SCHEMA-1 DDL facet)
# ===================================================================

class TestPromoteOrderNumber:
    def test_below_threshold_unchanged(self):
        cols = [("SalesOrderNumber", INT(not_null=True)), ("X", INT(not_null=True))]
        # No promotion at/below the threshold: content is returned as-is.
        assert promote_order_number(cols, _INT32_HALF) == cols

    def test_above_threshold_promotes_to_bigint(self):
        cols = [("SalesOrderNumber", INT(not_null=True)), ("X", INT(not_null=True))]
        out = dict(promote_order_number(cols, _INT32_HALF + 1))
        assert out["SalesOrderNumber"].sql_type == BIGINT().sql_type
        assert out["X"].sql_type == INT(not_null=True).sql_type  # other columns untouched

    def test_preserves_nullability(self):
        # Complaints' SalesOrderNumber is nullable — promotion must keep it nullable.
        cols = [("SalesOrderNumber", INT(not_null=False))]
        out = dict(promote_order_number(cols, _INT32_HALF + 1))
        assert out["SalesOrderNumber"].nullable is True


class TestSalesOrderNumberDDLPromotion:
    """End-to-end: the generated facts DDL widens SalesOrderNumber across every
    fact table that carries it (Sales/Header/Detail/Return/Complaints) once the
    configured row count would overflow int32."""

    def _facts_sql(self, tmp_path, total_rows):
        cfg = _ns(
            sales={"sales_output": "both", "total_rows": total_rows, "skip_order_cols": False},
            returns={"enabled": True},
            complaints={"enabled": True},
            dates={},
        )
        generate_all_create_tables(str(tmp_path), cfg)
        return (tmp_path / "schema" / "02_create_facts.sql").read_text(encoding="utf-8")

    def test_high_rows_all_bigint(self, tmp_path):
        sql = self._facts_sql(tmp_path, _INT32_HALF + 1)
        assert re.search(r"SalesOrderNumber\W+BIGINT\b", sql)
        # no SalesOrderNumber column left as plain INT
        assert not re.search(r"SalesOrderNumber\W+INT\b", sql)

    def test_low_rows_stays_int(self, tmp_path):
        sql = self._facts_sql(tmp_path, 1000)
        assert re.search(r"SalesOrderNumber\W+INT\b", sql)
        assert not re.search(r"SalesOrderNumber\W+BIGINT\b", sql)
