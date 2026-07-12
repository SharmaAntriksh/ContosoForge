"""Pure identifier-casing transforms for generated SQL.

This module is deliberately dependency-free and side-effect-free: it turns a
PascalCase identifier (the canonical form used throughout the schema, e.g.
``CustomerKey``) into another casing style. It is the deterministic heart of
the import-time identifier-casing feature; the wiring that threads a chosen
case through the generators and the name map lives elsewhere.

Only ``snake`` changes anything functionally. Postgres folds unquoted
identifiers to lowercase, so ``snake_case`` is the one style that lets users
query without double-quoting every table and column. ``pascal`` is the
identity (guaranteeing byte-identical default output) and ``camel`` is a
reserved, cosmetic-only style not yet implemented.

The transform is:
  - deterministic: same input always yields the same output;
  - idempotent under snake: ``to_case(to_case(x, SNAKE), SNAKE) == to_case(x, SNAKE)``;
  - total over any input string (empty string included).
"""
from __future__ import annotations

import re
from enum import Enum

__all__ = ["IdentifierCase", "split_tokens", "to_case"]


class IdentifierCase(str, Enum):
    """SQL identifier casing styles.

    Values are the lowercase spellings accepted by the importers'
    ``--identifier-case`` flag. ``str`` mixin so ``IdentifierCase("snake")``
    round-trips and members compare equal to their string value.
    """

    PASCAL = "pascal"
    SNAKE = "snake"
    CAMEL = "camel"  # reserved: cosmetic only (still requires quotes in Postgres), not implemented


# Boundary 1: a lowercase letter or digit immediately followed by an uppercase
#             letter  ->  ``customer|Key``, ``scd2|Version``, ``address1|Line``.
_BOUNDARY_LOWER_UPPER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# Boundary 2: an acronym run followed by a normal word, i.e. an uppercase letter
#             followed by an uppercase+lowercase pair  ->  ``ISO|Week``, ``HTTP|Server``.
_BOUNDARY_ACRONYM_WORD = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def split_tokens(name: str) -> list[str]:
    """Split a PascalCase/camelCase identifier into lowercase word tokens.

    Handles acronym runs (``SKU`` -> ``['sku']``, ``ISOWeek`` -> ``['iso', 'week']``,
    ``CustomerID`` -> ``['customer', 'id']``) and digit-bearing tokens
    (``SCD2Version`` -> ``['scd2', 'version']``, ``Iso8601Week`` -> ``['iso8601', 'week']``).
    Digits stay attached to the adjacent letter run rather than forming their own
    token, so ``Base36`` -> ``['base36']``.

    Underscores already present act as boundaries too, which makes the function
    idempotent on already-snake input (``customer_key`` -> ``['customer', 'key']``).
    """
    if not name:
        return []
    s = _BOUNDARY_LOWER_UPPER.sub("_", name)
    s = _BOUNDARY_ACRONYM_WORD.sub("_", s)
    return [tok for tok in s.lower().split("_") if tok]


def to_case(name: str, case: IdentifierCase) -> str:
    """Return ``name`` rendered in the target ``case``.

    ``PASCAL`` is the identity: it returns ``name`` unchanged so default output
    stays byte-identical to the current generator. ``SNAKE`` lowercases and
    underscore-joins the word tokens. ``CAMEL`` is reserved for a future phase
    and raises :class:`NotImplementedError`.
    """
    if case is IdentifierCase.PASCAL:
        return name
    if case is IdentifierCase.SNAKE:
        return "_".join(split_tokens(name))
    raise NotImplementedError(
        f"identifier_case {case.value!r} is not implemented yet "
        "(only 'pascal' and 'snake' are supported)"
    )
