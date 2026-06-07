from __future__ import annotations

from typing import Any

from .utils import _arrow, warn


def project_table_to_schema(
    table: Any,
    schema: Any,
    *,
    cast_safe: bool = False,
    on_cast_error: str = "raise",
    pa: Any = None,
    pc: Any = None,
) -> Any:
    """
    Project/align an Arrow table to the canonical schema:
      - reorder columns
      - add missing columns as typed nulls
      - cast columns to canonical types when safe/possible

    Parameters
    ----------
    cast_safe : bool
        Passed to pyarrow.compute.cast ``safe`` parameter.
    on_cast_error : str
        ``"raise"`` (default) – propagate cast errors.
        ``"warn"``  – log a warning and keep the column in its original type.
    pa, pc : optional
        Pre-imported ``pyarrow`` and ``pyarrow.compute`` modules.  When
        provided the function skips its own lazy import, avoiding repeated
        module lookups on the hot path.
    """
    if pa is None or pc is None:
        pa, pc, _ = _arrow()

    if table.schema == schema:
        return table

    n = table.num_rows
    have = set(table.schema.names)

    arrays = []
    for field in schema:
        name = field.name

        if name not in have:
            arrays.append(pa.nulls(n, type=field.type))
            continue

        col = table[name]  # ChunkedArray
        if not col.type.equals(field.type):
            try:
                col = pc.cast(col, field.type, safe=bool(cast_safe))
            except (ValueError, TypeError, ArithmeticError) as ex:
                if on_cast_error == "warn":
                    warn(
                        f"[projection] Failed to cast column '{name}' "
                        f"from {col.type} to {field.type}: {ex} – keeping original type"
                    )
                else:
                    raise RuntimeError(
                        f"Failed to cast column '{name}' from {col.type} to {field.type}: {ex}"
                    ) from ex

        arrays.append(col)

    return pa.Table.from_arrays(arrays, schema=schema)


def _project_table_to_schema(table: Any, schema: Any) -> Any:
    """Backward-compatible alias (some callers expect the underscore name).

    Uses cast_safe=True so a narrowing cast that overflows raises instead of
    silently truncating (WRITER-1) — matching the live merge path.
    """
    return project_table_to_schema(table, schema, cast_safe=True, on_cast_error="raise")
