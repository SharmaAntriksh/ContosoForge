from .base import ColumnSpec, Dialect, SqlType
from .postgres import PostgresDialect
from .sqlserver import DEFAULT_DIALECT, SqlServerDialect

# SQL Server must reuse DEFAULT_DIALECT (not a fresh instance) so packaging
# can identify it via `dialect is DEFAULT_DIALECT` and route its output to
# the existing sql/ folder.
REGISTRY: dict[str, Dialect] = {
    SqlServerDialect.name: DEFAULT_DIALECT,
    PostgresDialect.name: PostgresDialect(),
}


def resolve_dialect(name: str) -> Dialect:
    """Look up a dialect by canonical name (case-insensitive)."""
    key = str(name).strip().lower()
    try:
        return REGISTRY[key]
    except KeyError:
        valid = ", ".join(sorted(REGISTRY))
        raise ValueError(f"Unknown SQL dialect {name!r}. Valid: {valid}") from None


__all__ = [
    "ColumnSpec",
    "Dialect",
    "SqlType",
    "SqlServerDialect",
    "PostgresDialect",
    "DEFAULT_DIALECT",
    "REGISTRY",
    "resolve_dialect",
]
