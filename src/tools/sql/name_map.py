"""Canonical identifier name map for the importers' ``--identifier-case``.

Builds, from a single authoritative source, the mapping from every original
(PascalCase) schema identifier to its target-cased form. That single map then
feeds its consumers so they can never drift:

  1. re-rendered DDL / constraints / views / indexes / load scripts at import
     time (transform table + column identifiers consistently),
  2. the importer's CSV-stem -> table-name resolution.

The identifier universe is the union of:
  * every table in ``STATIC_SCHEMAS`` (which already includes all fact tables),
  * every column of those tables, using the dynamic generators for ``Dates``
    (``get_dates_schema``, maximal config) and ``Sales`` (``get_sales_schema``
    with ``skip_order_cols=False``) so the *superset* of possible columns is
    covered regardless of the run's config.

Two validations run at build time:
  * **Collisions are a hard error.** If two distinct originals collapse to the
    same target within one scope (two tables, or two columns of one table), we
    raise ``ValidationError`` rather than silently drop one.
  * **Reserved words are a soft warning.** ``find_reserved_conflicts`` reports
    identifiers whose target lands on a dialect reserved word; those still need
    quoting, so snake mode is not fully quote-free for them. Not an error --
    ``quote_ident`` always wraps identifiers, so output stays correct.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping

from src.exceptions import ValidationError
from src.tools.sql.identifier_case import IdentifierCase, to_case
from src.utils.static_schemas import STATIC_SCHEMAS, get_dates_schema, get_sales_schema

if TYPE_CHECKING:  # avoid importing the dialect package at module load
    from src.tools.sql.dialect.base import Dialect

__all__ = ["NameMap", "build_name_map", "find_reserved_conflicts"]


@dataclass(frozen=True)
class NameMap:
    """Original -> target identifier maps for tables and columns.

    ``tables`` and ``columns`` are kept separate for clarity and validation,
    but :meth:`resolve` merges them for token lookup during the rewrite.
    """

    case: IdentifierCase
    tables: Mapping[str, str]
    columns: Mapping[str, str]

    def resolve(self, ident: str) -> str:
        """Return the transformed identifier if recognised, else ``ident``.

        The hand-written-SQL rewrite calls this per token: only known schema
        identifiers are rewritten; arbitrary words pass through untouched.
        Columns take precedence over tables, but since the transform is
        deterministic an identifier that is both yields the same result either
        way.
        """
        if ident in self.columns:
            return self.columns[ident]
        if ident in self.tables:
            return self.tables[ident]
        return ident

    @property
    def known(self) -> frozenset[str]:
        """Every original identifier this map recognises (tables + columns)."""
        return frozenset(self.tables) | frozenset(self.columns)


def _maximal_dates_cfg():
    """A Dates config with every subsystem on, so ``get_dates_schema`` yields
    the widest possible column set (calendar + ISO + monthly + weekly fiscal)."""
    from src.engine.config.config_schema import AppConfig  # lazy: avoid import cycle

    return AppConfig.model_validate(
        {
            "dates": {
                "include": {
                    "calendar": True,
                    "iso": True,
                    "fiscal": True,
                    "weekly_fiscal": {"enabled": True},
                }
            }
        }
    ).dates


def _table_to_columns() -> dict[str, list[str]]:
    """Canonical ``table -> [original column names]``.

    Uses the dynamic generators for Dates and Sales so every column that could
    ever be emitted is represented, independent of the current run's config.
    """
    t2c = {table: [col for col, _ in cols] for table, cols in STATIC_SCHEMAS.items()}
    t2c["Dates"] = [col for col, _ in get_dates_schema(_maximal_dates_cfg())]
    # skip_order_cols=False -> superset including OrderNumber / OrderLineNumber.
    t2c["Sales"] = [col for col, _ in get_sales_schema(False)]
    return t2c


def _checked_map(originals: Iterable[str], case: IdentifierCase, scope: str) -> dict[str, str]:
    """Map each original to its target case, raising on a within-scope collision.

    A collision is two *distinct* originals producing the same target. Repeated
    identical originals (the same column name across tables) are fine.
    """
    out: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for orig in originals:
        target = to_case(orig, case)
        clash = reverse.get(target)
        if clash is not None and clash != orig:
            raise ValidationError(
                f"identifier_case={case.value!r} produces a collision in {scope}: "
                f"{clash!r} and {orig!r} both map to {target!r}. "
                "Rename one of the source identifiers or choose a different case."
            )
        reverse[target] = orig
        out[orig] = target
    return out


def build_name_map(case: IdentifierCase) -> NameMap:
    """Build the validated name map for ``case``.

    Raises :class:`ValidationError` if any table names collide or any table's
    columns collide under the transform. For ``IdentifierCase.PASCAL`` the map
    is the identity and cannot collide.
    """
    t2c = _table_to_columns()

    tables = _checked_map(t2c.keys(), case, "table names")

    columns: dict[str, str] = {}
    for table, cols in t2c.items():
        # Per-table collision check (within-table columns must stay distinct).
        per_table = _checked_map(cols, case, f"columns of table {table!r}")
        columns.update(per_table)

    return NameMap(case=case, tables=tables, columns=columns)


def find_reserved_conflicts(name_map: NameMap, dialect: "Dialect") -> list[tuple[str, str]]:
    """Return ``(original, target)`` pairs whose target is a ``dialect`` reserved word.

    Sorted and de-duplicated. These identifiers still need quoting even in snake
    mode; callers should log them as a warning, not fail. Returns an empty list
    when the dialect declares no reserved words.
    """
    reserved = getattr(dialect, "reserved_words", frozenset())
    if not reserved:
        return []
    hits: set[tuple[str, str]] = set()
    for orig, target in {**name_map.tables, **name_map.columns}.items():
        if target.lower() in reserved:
            hits.add((orig, target))
    return sorted(hits)
