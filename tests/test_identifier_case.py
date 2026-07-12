"""Unit tests for the pure identifier-casing transform.

Covers the tokenizer's acronym/digit handling, snake rendering, the pascal
identity, idempotency, and the reserved camel slot. No I/O, no SQL.
"""
from __future__ import annotations

import pytest

from src.tools.sql.identifier_case import IdentifierCase, split_tokens, to_case


class TestSplitTokens:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("CustomerKey", ["customer", "key"]),
            ("OrderLineNumber", ["order", "line", "number"]),
            ("UnitPrice", ["unit", "price"]),
            ("IsOrderDelayed", ["is", "order", "delayed"]),
            # acronym runs
            ("SKU", ["sku"]),
            ("CustomerID", ["customer", "id"]),
            ("BaseProductID", ["base", "product", "id"]),
            ("ISOWeek", ["iso", "week"]),
            ("HTTPServer", ["http", "server"]),
            # digit-bearing tokens: digits stay attached to their letter run
            ("SCD2Version", ["scd2", "version"]),
            ("Iso8601Week", ["iso8601", "week"]),
            ("Base36", ["base36"]),
            ("Q4", ["q4"]),
            ("Address1Line", ["address1", "line"]),
            # already-snake / mixed input (idempotent tokenization)
            ("customer_key", ["customer", "key"]),
            ("Customer_Key", ["customer", "key"]),
            ("", []),
        ],
    )
    def test_split(self, name, expected):
        assert split_tokens(name) == expected


class TestToCaseSnake:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("CustomerKey", "customer_key"),
            ("OrderLineNumber", "order_line_number"),
            ("CustomerID", "customer_id"),
            ("BaseProductID", "base_product_id"),
            ("SKU", "sku"),
            ("ISOWeek", "iso_week"),
            ("SCD2Version", "scd2_version"),
            ("Iso8601Week", "iso8601_week"),
            ("NetPrice", "net_price"),
            ("DiscountAmount", "discount_amount"),
            ("DeliveryStatus", "delivery_status"),
            ("", ""),
        ],
    )
    def test_snake(self, name, expected):
        assert to_case(name, IdentifierCase.SNAKE) == expected

    @pytest.mark.parametrize(
        "name",
        ["CustomerKey", "OrderLineNumber", "SCD2Version", "SKU", "customer_key", "iso8601_week"],
    )
    def test_snake_is_idempotent(self, name):
        once = to_case(name, IdentifierCase.SNAKE)
        assert to_case(once, IdentifierCase.SNAKE) == once


class TestToCasePascalIdentity:
    @pytest.mark.parametrize(
        "name",
        ["CustomerKey", "OrderLineNumber", "SKU", "already_snake", "BaseProductID", ""],
    )
    def test_pascal_returns_input_unchanged(self, name):
        # PASCAL must be a strict identity so default output is byte-identical.
        assert to_case(name, IdentifierCase.PASCAL) == name


class TestCamelReserved:
    def test_camel_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            to_case("CustomerKey", IdentifierCase.CAMEL)


class TestEnum:
    def test_values_are_config_spellings(self):
        assert IdentifierCase.PASCAL.value == "pascal"
        assert IdentifierCase.SNAKE.value == "snake"
        assert IdentifierCase.CAMEL.value == "camel"

    def test_roundtrips_from_string(self):
        assert IdentifierCase("snake") is IdentifierCase.SNAKE
        assert IdentifierCase("pascal") is IdentifierCase.PASCAL
