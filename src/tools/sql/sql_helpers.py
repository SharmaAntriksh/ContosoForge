"""Shared SQL helpers used by both CREATE TABLE and BULK INSERT generators."""
from __future__ import annotations

from typing import Mapping, Optional

from src.tools.sql.dialect import DEFAULT_DIALECT
from src.tools.sql.dialect.base import sql_escape_literal  # noqa: F401 — re-exported


def quote_ident(name: str) -> str:
    """Quote an identifier using the default (SQL Server) dialect.

    Kept as a free function so callers that pre-date the dialect API
    (notably the BULK INSERT generator) don't need to change. Once those
    generators thread a dialect parameter themselves, prefer
    ``dialect.quote_ident(name)`` directly.
    """
    return DEFAULT_DIALECT.quote_ident(name)


def odbc_brace(value: str) -> str:
    """Brace-quote a value for an ODBC connection string.

    ODBC values may legitimately contain ``;``, ``=``, spaces, etc.;
    enclosing them in braces makes those characters literal, and a literal
    ``}`` is escaped by doubling. Without this, a value containing ``;``
    silently truncates the connection string or, worse, appends
    attacker-chosen connection attributes (e.g. ``;Encrypt=no``).
    """
    return "{" + str(value).replace("}", "}}") + "}"


# -------------------------------------------------------------------
# Config-driven feature flags (used to decide which SQL objects to emit)
# -------------------------------------------------------------------

def returns_enabled(cfg: Optional[Mapping]) -> bool:
    """Return True if returns are effectively enabled."""
    if cfg is None:
        return True
    returns_cfg = getattr(cfg, "returns", None)
    if isinstance(returns_cfg, Mapping) and isinstance(getattr(returns_cfg, "enabled", None), (bool, int)):
        if not bool(returns_cfg.enabled):
            return False
    sales_cfg = getattr(cfg, "sales", None)
    skip_order = bool(getattr(sales_cfg, "skip_order_cols", False) if sales_cfg else False)
    sales_output = str(getattr(sales_cfg, "sales_output", "sales") if sales_cfg else "sales").strip().lower()
    if skip_order and sales_output == "sales":
        return False
    return True


def budget_enabled(cfg: Optional[Mapping]) -> bool:
    """Return True if budget generation is enabled in config."""
    if cfg is None:
        return False
    budget_cfg = getattr(cfg, "budget", None)
    if budget_cfg is None or not isinstance(budget_cfg, Mapping):
        return False
    return bool(getattr(budget_cfg, "enabled", False))


def inventory_enabled(cfg: Optional[Mapping]) -> bool:
    """Return True if inventory snapshot generation is enabled in config."""
    if cfg is None:
        return False
    inv_cfg = getattr(cfg, "inventory", None)
    if inv_cfg is None or not isinstance(inv_cfg, Mapping):
        return False
    return bool(getattr(inv_cfg, "enabled", False))


def complaints_enabled(cfg: Optional[Mapping]) -> bool:
    """Return True if complaints generation is enabled in config."""
    if cfg is None:
        return False
    cc = getattr(cfg, "complaints", None)
    if cc is None or not isinstance(cc, Mapping):
        return False
    return bool(getattr(cc, "enabled", False))


def wishlists_enabled(cfg: Optional[Mapping]) -> bool:
    """Return True if wishlists generation is enabled in config."""
    if cfg is None:
        return False
    wl = getattr(cfg, "wishlists", None)
    if wl is None or not isinstance(wl, Mapping):
        return False
    return bool(getattr(wl, "enabled", False))
