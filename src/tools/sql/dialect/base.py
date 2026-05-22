from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, ClassVar, Tuple


class SqlType(Enum):
    INT = auto()
    BIGINT = auto()
    SMALLINT = auto()
    TINYINT = auto()
    BIT = auto()
    FLOAT = auto()
    DATE = auto()
    DATETIME = auto()
    DATETIME2 = auto()
    TIME = auto()
    VARCHAR = auto()
    CHAR = auto()
    DECIMAL = auto()


@dataclass(frozen=True)
class ColumnSpec:
    sql_type: SqlType
    nullable: bool = True
    args: Tuple[Any, ...] = ()


class Dialect(ABC):
    name: ClassVar[str]
    # Batch-script terminator (e.g. SQL Server's "GO"). Empty for dialects
    # that have no equivalent — generators skip the line when empty.
    batch_separator: ClassVar[str] = ""
    # Lines emitted in the script header after the timestamp banner.
    # SQL Server uses ``SET NOCOUNT ON;`` to suppress row-count chatter
    # under large batches; Postgres has no equivalent.
    script_preamble: ClassVar[tuple[str, ...]] = ()

    # Subclasses populate these with the dialect's type spellings. The base
    # render_type/_render_base do the dispatch — subclasses only override if
    # a type needs non-template handling (e.g. Postgres VARCHAR("MAX") -> TEXT).
    _SIMPLE: ClassVar[dict[SqlType, str]] = {}
    _PARAM_TEMPLATES: ClassVar[dict[SqlType, str]] = {}

    def render_type(self, spec: ColumnSpec) -> str:
        suffix = "NULL" if spec.nullable else "NOT NULL"
        return f"{self._render_base(spec)} {suffix}"

    def _render_base(self, spec: ColumnSpec) -> str:
        t = spec.sql_type
        simple = self._SIMPLE.get(t)
        if simple is not None:
            if spec.args:
                raise ValueError(f"{t.name} takes no args, got {spec.args!r}")
            return simple
        template = self._PARAM_TEMPLATES.get(t)
        if template is not None:
            return template.format(*spec.args)
        raise ValueError(f"Unhandled SqlType for {self.name}: {t!r}")

    @staticmethod
    def _strip_ident_wrappers(name: str) -> str:
        """Strip a single layer of ``[..]`` or ``"..."`` wrappers from an identifier."""
        raw = str(name).strip()
        if raw.startswith("[") and raw.endswith("]"):
            return raw[1:-1]
        if raw.startswith('"') and raw.endswith('"'):
            return raw[1:-1]
        return raw

    @abstractmethod
    def quote_ident(self, name: str) -> str: ...

    @abstractmethod
    def drop_table_if_exists(self, schema: str, table: str) -> str:
        """Return the dialect-appropriate "drop this table if it exists" statement.

        Takes unquoted identifiers — the dialect handles its own quoting and
        any escaping needed for embedded string literals.
        """
