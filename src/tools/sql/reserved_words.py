"""Per-dialect reserved-keyword sets (lowercased).

Used by the identifier-case name map to warn when a transformed identifier
lands on a word the dialect reserves. A reserved identifier is not an error:
it just still needs quoting/bracketing, so snake mode does not make it
quote-free. ``quote_ident`` always wraps identifiers, so correctness is
unaffected either way; the warning only tells the user which few columns
remain quoted.

Sources: the PostgreSQL "reserved" keyword categories and the SQL Server
T-SQL reserved keyword list. All entries are lowercased for case-insensitive
membership tests.
"""
from __future__ import annotations

# PostgreSQL reserved keywords (the categories that cannot be a bare column or
# table name). Non-reserved keywords like ``date``, ``year``, ``level`` are
# intentionally excluded because Postgres accepts them unquoted as identifiers.
POSTGRES_RESERVED: frozenset[str] = frozenset({
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc",
    "asymmetric", "authorization", "binary", "both", "case", "cast", "check",
    "collate", "collation", "column", "concurrently", "constraint", "create",
    "cross", "current_catalog", "current_date", "current_role",
    "current_schema", "current_time", "current_timestamp", "current_user",
    "default", "deferrable", "desc", "distinct", "do", "else", "end", "except",
    "false", "fetch", "for", "foreign", "freeze", "from", "full", "grant",
    "group", "having", "ilike", "in", "initially", "inner", "intersect",
    "into", "is", "isnull", "join", "lateral", "leading", "left", "like",
    "limit", "localtime", "localtimestamp", "natural", "not", "notnull",
    "null", "offset", "on", "only", "or", "order", "outer", "overlaps",
    "placing", "primary", "references", "returning", "right", "select",
    "session_user", "similar", "some", "symmetric", "system_user", "table",
    "tablesample", "then", "to", "trailing", "true", "union", "unique", "user",
    "using", "variadic", "verbose", "when", "where", "window", "with",
})

# SQL Server (T-SQL) reserved keywords.
SQLSERVER_RESERVED: frozenset[str] = frozenset({
    "add", "all", "alter", "and", "any", "as", "asc", "authorization",
    "backup", "begin", "between", "break", "browse", "bulk", "by", "cascade",
    "case", "check", "checkpoint", "close", "clustered", "coalesce", "collate",
    "column", "commit", "compute", "constraint", "contains", "containstable",
    "continue", "convert", "create", "cross", "current", "current_date",
    "current_time", "current_timestamp", "current_user", "cursor", "database",
    "dbcc", "deallocate", "declare", "default", "delete", "deny", "desc",
    "disk", "distinct", "distributed", "double", "drop", "dump", "else", "end",
    "errlvl", "escape", "except", "exec", "execute", "exists", "exit",
    "external", "fetch", "file", "fillfactor", "for", "foreign", "freetext",
    "freetexttable", "from", "full", "function", "goto", "grant", "group",
    "having", "holdlock", "identity", "identity_insert", "identitycol", "if",
    "in", "index", "inner", "insert", "intersect", "into", "is", "join", "key",
    "kill", "left", "like", "lineno", "load", "merge", "national", "nocheck",
    "nonclustered", "not", "null", "nullif", "of", "off", "offsets", "on",
    "open", "opendatasource", "openquery", "openrowset", "openxml", "option",
    "or", "order", "outer", "over", "percent", "pivot", "plan", "precision",
    "primary", "print", "proc", "procedure", "public", "raiserror", "read",
    "readtext", "reconfigure", "references", "replication", "restore",
    "restrict", "return", "revert", "revoke", "right", "rollback", "rowcount",
    "rowguidcol", "rule", "save", "schema", "securityaudit", "select",
    "session_user", "set", "setuser", "shutdown", "some", "statistics",
    "system_user", "table", "tablesample", "textsize", "then", "to", "top",
    "tran", "transaction", "trigger", "truncate", "tsequal", "union", "unique",
    "unpivot", "update", "updatetext", "use", "user", "values", "varying",
    "view", "waitfor", "when", "where", "while", "with", "writetext",
})
