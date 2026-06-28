from __future__ import annotations

from typing import Iterable, List, Optional, Set

from ..output_paths import TABLE_SALES, TABLE_SALES_ORDER_DETAIL

# ===============================================================
# Sales policy/constants
# ===============================================================

DICT_EXCLUDE: frozenset[str] = frozenset({"OrderNumber", "CustomerKey"})

REQUIRED_PRICING_COLS: frozenset[str] = frozenset({
    "UnitPrice",
    "NetPrice",
    "UnitCost",
    "DiscountAmount",
})

_REQUIRED_PRICING_TABLES: frozenset[str] = frozenset({
    TABLE_SALES,
    TABLE_SALES_ORDER_DETAIL,
})


def validate_required_columns(
    schema,
    required: Iterable[str] | Set[str],
    *,
    what: str = "required columns",
) -> None:
    req = set(required) if not isinstance(required, (set, frozenset)) else required
    if not req:
        return

    names = set(getattr(schema, "names", ()) or ())
    missing = req - names
    if missing:
        raise RuntimeError(f"Missing {what}: {sorted(missing)}")


def schema_dict_cols(
    schema,
    exclude: Optional[Iterable[str]] = None,
    *,
    pa=None,
) -> List[str]:
    """Return string/binary column names eligible for dictionary encoding.

    Parameters
    ----------
    pa : optional
        Pre-imported ``pyarrow`` module.  When provided the function
        skips its own lazy import, avoiding repeated module lookups on
        the hot path (called once per chunk during merge/write).
    """
    if pa is None:
        from .utils import _arrow
        pa, _, _ = _arrow()

    exclude_set = set(exclude) if exclude else set()
    types = pa.types
    out: List[str] = []

    for f in schema:
        if f.name in exclude_set:
            continue

        t = f.type
        if (
            types.is_string(t)
            or types.is_large_string(t)
            or types.is_binary(t)
            or types.is_large_binary(t)
        ):
            out.append(f.name)

    return out


# ===============================================================
# Sales-specific encoding policy
# ===============================================================

def required_pricing_cols_for_table(table_name: str | None) -> frozenset[str]:
    """Pricing cols are required only for line-grain tables.

    Returns a *frozenset* — callers that need a mutable copy should
    wrap with ``set(...)``.  Back-compat: if *table_name* is ``None``,
    keep old strict behaviour (require pricing cols).
    """
    if table_name is None or table_name in _REQUIRED_PRICING_TABLES:
        return REQUIRED_PRICING_COLS
    return frozenset()


def _validate_required(schema, *, table_name: str | None = None) -> None:
    """Legacy name kept for compatibility with existing imports."""
    required = required_pricing_cols_for_table(table_name)
    if required:
        validate_required_columns(schema, required, what="required pricing columns")


def _schema_dict_cols(
    schema,
    exclude: Optional[Iterable[str]] = None,
    *,
    table_name: str | None = None,
) -> List[str]:
    """Legacy name kept for compatibility with existing imports.

    NOTE: This no longer calls _validate_required internally.
    Callers that need validation should call _validate_required separately
    before calling this function.  This avoids the double-validation issue
    where merge_parquet_files would validate twice per merge.
    """
    exclude_set = DICT_EXCLUDE | set(exclude) if exclude else DICT_EXCLUDE

    return schema_dict_cols(schema, exclude=exclude_set)
