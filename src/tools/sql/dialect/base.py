from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
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


def sql_escape_literal(value: str) -> str:
    """Escape a string for use inside a single-quoted SQL literal.

    Dialect-neutral: SQL Server, Postgres, and MySQL all use doubled
    single quotes for embedded apostrophes.
    """
    return value.replace("'", "''")


class Dialect(ABC):
    name: ClassVar[str]
    # Batch-script terminator (e.g. SQL Server's "GO"). Empty for dialects
    # that have no equivalent — generators skip the line when empty.
    batch_separator: ClassVar[str] = ""
    # Lines emitted in the script header after the timestamp banner.
    # SQL Server uses ``SET NOCOUNT ON;`` to suppress row-count chatter
    # under large batches; Postgres has no equivalent.
    script_preamble: ClassVar[tuple[str, ...]] = ()
    # Filename infix for load scripts: ``01_<load_script_kind>_dims.sql``.
    # SQL Server: "bulk_insert"; Postgres: "copy"; etc. Also drives the
    # script banner ("Auto-generated <KIND_UPPER> script").
    load_script_kind: ClassVar[str] = "load"
    default_schema: ClassVar[str] = ""
    # SQL Server BULK INSERT historically emits unqualified targets and
    # lets the session default schema resolve them; Postgres COPY qualifies.
    qualify_load_target: ClassVar[bool] = False
    # One-line note prepended to load scripts under the timestamp banner,
    # typically calling out where the file path is resolved.
    load_script_note: ClassVar[str] = ""

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

    def qualify(self, schema: str, table: str) -> str:
        """Return ``schema.table`` with each identifier quoted by this dialect.

        Falls back to bare ``table`` when ``schema`` is empty.
        """
        return f"{self.quote_ident(schema)}.{self.quote_ident(table)}" if schema else self.quote_ident(table)

    @staticmethod
    def _strip_ident_wrappers(name: str) -> str:
        """Strip a single layer of ``[..]`` or ``"..."`` wrappers from an identifier."""
        raw = str(name).strip()
        if raw.startswith("[") and raw.endswith("]"):
            return raw[1:-1]
        if raw.startswith('"') and raw.endswith('"'):
            return raw[1:-1]
        return raw

    def prepare_load_script(self) -> str | None:
        """Optional SQL to run *before* the bulk load (own script file).

        Used to switch the database into a minimal-logging-friendly recovery
        model and pre-grow the transaction log so a large load does not blow the
        log out to the full table size. Returns None for dialects that do not
        need it (the generator then emits no prepare file).
        """
        return None

    def finish_load_script(self) -> str | None:
        """Optional SQL to run *after* the bulk load (own script file).

        Reverses :meth:`prepare_load_script` (e.g. restore the recovery model).
        Returns None when not applicable.
        """
        return None

    @abstractmethod
    def quote_ident(self, name: str) -> str: ...

    @abstractmethod
    def drop_table_if_exists(self, schema: str, table: str) -> str:
        """Return the dialect-appropriate "drop this table if it exists" statement.

        Takes unquoted identifiers — the dialect handles its own quoting and
        any escaping needed for embedded string literals.
        """

    @abstractmethod
    def bulk_load_statement(
        self,
        *,
        schema: str,
        table: str,
        csv_path: Path,
        use_csv_format: bool = False,
        batch_rows: int = 1_000_000,
    ) -> str:
        """Return a single bulk-load statement for one CSV file.

        ``use_csv_format`` flags tables whose string columns may contain
        embedded delimiters/quotes. SQL Server toggles to ``FORMAT='CSV'``;
        dialects whose load mechanism is always CSV-aware (e.g. Postgres
        ``COPY ... FORMAT csv``) can ignore the flag.

        ``batch_rows`` sizes the load's commit granularity (SQL Server
        ``BATCHSIZE``) so the transaction log truncates between batches.
        Dialects that commit per statement (e.g. Postgres ``COPY``, where the
        chunk file already is the commit unit) ignore it.
        """
