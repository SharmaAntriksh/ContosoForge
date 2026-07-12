"""Tests for the identifier-case name map builder and its validations."""
from __future__ import annotations

import pytest

from src.exceptions import ValidationError
from src.tools.sql.dialect import PostgresDialect, SqlServerDialect
from src.tools.sql.identifier_case import IdentifierCase
from src.tools.sql.name_map import (
    NameMap,
    _checked_map,
    build_name_map,
    find_reserved_conflicts,
)


class TestBuildSnake:
    @pytest.fixture(scope="class")
    def nm(self) -> NameMap:
        return build_name_map(IdentifierCase.SNAKE)

    def test_table_names_snaked(self, nm: NameMap) -> None:
        assert nm.tables["Customers"] == "customers"
        assert nm.tables["InventorySnapshot"] == "inventory_snapshot"
        assert nm.tables["CustomerWishlists"] == "customer_wishlists"

    def test_column_names_snaked(self, nm: NameMap) -> None:
        assert nm.columns["CustomerKey"] == "customer_key"
        assert nm.columns["OrderLineNumber"] == "order_line_number"
        assert nm.columns["ISOWeekDateRange"] == "iso_week_date_range"

    def test_covers_dynamic_dates_and_sales_columns(self, nm: NameMap) -> None:
        # Weekly-fiscal (Dates) and order columns (Sales superset) must be present.
        assert "FWWeekIndex" in nm.columns
        assert nm.columns["FWWeekIndex"] == "fw_week_index"
        assert "OrderNumber" in nm.columns

    def test_no_collisions_on_real_schema(self, nm: NameMap) -> None:
        # Building did not raise; sanity-check the maps are non-trivial.
        assert len(nm.tables) >= 30
        assert len(nm.columns) >= 100


class TestResolve:
    @pytest.fixture(scope="class")
    def nm(self) -> NameMap:
        return build_name_map(IdentifierCase.SNAKE)

    def test_resolves_known_table_and_column(self, nm: NameMap) -> None:
        assert nm.resolve("Customers") == "customers"
        assert nm.resolve("CustomerKey") == "customer_key"

    def test_unknown_token_passes_through(self, nm: NameMap) -> None:
        assert nm.resolve("NotAnIdentifier") == "NotAnIdentifier"
        assert nm.resolve("Electronics") == "Electronics"  # a CHECK data value, not an identifier

    def test_known_set(self, nm: NameMap) -> None:
        assert "Customers" in nm.known
        assert "CustomerKey" in nm.known


class TestBuildPascalIsIdentity:
    def test_identity(self) -> None:
        nm = build_name_map(IdentifierCase.PASCAL)
        assert all(k == v for k, v in nm.tables.items())
        assert all(k == v for k, v in nm.columns.items())
        # Identity resolve.
        assert nm.resolve("CustomerKey") == "CustomerKey"


class TestCollisionDetection:
    def test_within_scope_collision_raises(self) -> None:
        # Two distinct originals that snake to the same target.
        with pytest.raises(ValidationError, match="collision"):
            _checked_map(["CustomerID", "Customer_ID"], IdentifierCase.SNAKE, "columns of table 'T'")

    def test_repeated_identical_original_is_ok(self) -> None:
        # Same original twice is not a collision (same column across tables).
        out = _checked_map(["CustomerKey", "CustomerKey"], IdentifierCase.SNAKE, "x")
        assert out == {"CustomerKey": "customer_key"}

    def test_pascal_never_collides(self) -> None:
        out = _checked_map(["CustomerID", "Customer_ID"], IdentifierCase.PASCAL, "x")
        assert out == {"CustomerID": "CustomerID", "Customer_ID": "Customer_ID"}


class TestReservedWordDetection:
    def test_flags_reserved_target(self) -> None:
        synthetic = NameMap(
            case=IdentifierCase.SNAKE,
            tables={},
            columns={"Order": "order", "CustomerKey": "customer_key"},
        )
        hits = find_reserved_conflicts(synthetic, PostgresDialect())
        assert hits == [("Order", "order")]

    def test_real_schema_has_no_reserved_conflicts_postgres(self) -> None:
        nm = build_name_map(IdentifierCase.SNAKE)
        assert find_reserved_conflicts(nm, PostgresDialect()) == []

    def test_real_schema_has_no_reserved_conflicts_sqlserver(self) -> None:
        nm = build_name_map(IdentifierCase.SNAKE)
        assert find_reserved_conflicts(nm, SqlServerDialect()) == []


class TestDialectReservedWords:
    def test_dialects_declare_reserved_words(self) -> None:
        assert "order" in PostgresDialect().reserved_words
        assert "user" in PostgresDialect().reserved_words
        assert "order" in SqlServerDialect().reserved_words
        assert "key" in SqlServerDialect().reserved_words  # reserved in T-SQL, not in PG
        assert "key" not in PostgresDialect().reserved_words
