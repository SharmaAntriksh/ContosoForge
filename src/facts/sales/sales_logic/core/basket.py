"""Order-level basket-theme correlation.

Each order is assigned a hash-seeded *theme* (a group of product subcategories);
a configurable share of its lines whose sampled product falls outside that theme
are redirected to a product inside it. This concentrates a multi-line order onto
a coherent subcategory group, so market-basket / affinity mining recovers real
association rules instead of only marginal item frequencies.

Determinism: the theme, the per-line redirect decision, and the replacement
product are all pure SplitMix64 hashes of the globally-unique
``(OrderNumber, OrderLineNumber)`` — never the chunk RNG. So the bias is
identical regardless of chunk_size or worker count, and it only changes *which*
product a line carries (never row counts or customer assignment). Distinct
salts keep it independent of the fulfillment-friction latent.
"""
from __future__ import annotations

import numpy as np

_C1 = np.uint64(0x9E3779B97F4A7C15)
_C2 = np.uint64(0xBF58476D1CE4E5B9)
_C3 = np.uint64(0x94D049BB133111EB)
_SALT_THEME = np.uint64(0xA5A5F00DD00DF00D)
_SALT_PICK = np.uint64(0x0123456789ABCDEF)
_TWO_POW_53 = np.float64(9007199254740992.0)  # 2**53

# Per-worker setup cache: (id(product_subcat_key), num_themes) -> (prod_group, group_rows)
_setup_cache: dict = {}


def reset_basket_cache() -> None:
    """Clear the per-worker basket setup cache (call from reset_worker_cdf_cache)."""
    _setup_cache.clear()


def _mix(x: np.ndarray) -> np.ndarray:
    x = x ^ (x >> np.uint64(30))
    x = x * _C2
    x = x ^ (x >> np.uint64(27))
    x = x * _C3
    x = x ^ (x >> np.uint64(31))
    return x


def _u01(x: np.ndarray) -> np.ndarray:
    """Top 53 bits of a mixed uint64 -> uniform double in [0, 1)."""
    return (x >> np.uint64(11)).astype(np.float64) / _TWO_POW_53


def _theme_of(values_u64: np.ndarray, num_themes: int) -> np.ndarray:
    """Map keys to a theme id in [0, num_themes)."""
    return (_mix(values_u64 * _C1 ^ _SALT_THEME) % np.uint64(num_themes)).astype(np.int64)


def basket_setup(product_subcat_key: np.ndarray, num_themes: int):
    """Precompute (cached) the per-product theme group and per-group row lists.

    A subcategory is assigned to a group by hashing its key, so the grouping is a
    stable, arbitrary partition of subcategories into ``num_themes`` bundles.
    """
    key = (id(product_subcat_key), int(num_themes))
    cached = _setup_cache.get(key)
    if cached is not None:
        return cached
    subcat = np.asarray(product_subcat_key).astype(np.int64, copy=False).astype(np.uint64, copy=False)
    prod_group = _theme_of(subcat, num_themes)
    group_rows = [np.flatnonzero(prod_group == g).astype(np.int64) for g in range(num_themes)]
    result = (prod_group, group_rows)
    _setup_cache[key] = result
    return result


def apply_basket_theme(prod_idx, order_ids_int, line_num, product_subcat_key,
                       *, num_themes: int, strength: float) -> np.ndarray:
    """Return ``prod_idx`` with off-theme lines biased toward their order's theme.

    ``prod_idx`` are row indices into the product pool; ``order_ids_int`` and
    ``line_num`` are the per-line OrderNumber / OrderLineNumber. No-ops (returns
    the input unchanged) when the feature can't apply.
    """
    if (product_subcat_key is None or order_ids_int is None or line_num is None
            or int(num_themes) < 2 or float(strength) <= 0.0):
        return prod_idx

    prod_group, group_rows = basket_setup(product_subcat_key, int(num_themes))

    o = np.asarray(order_ids_int).astype(np.int64, copy=False).astype(np.uint64, copy=False)
    ln = np.asarray(line_num).astype(np.int64, copy=False).astype(np.uint64, copy=False)

    # Order-level theme (a function of OrderNumber only, so all lines agree).
    theme = _theme_of(o, int(num_themes))

    # Per-line decision (u) and replacement pick (v), independent hashes.
    base = _mix(o * _C1 ^ (ln + _C2))
    u = _u01(base)
    v = _u01(_mix(base ^ _SALT_PICK))

    cur_group = prod_group[np.asarray(prod_idx, dtype=np.int64)]
    redirect = (u < float(strength)) & (cur_group != theme)

    out = np.array(prod_idx, dtype=prod_idx.dtype, copy=True)
    for t in range(int(num_themes)):
        m = redirect & (theme == t)
        rows = group_rows[t]
        if rows.size == 0 or not m.any():
            continue
        pick = np.minimum((v[m] * rows.size).astype(np.int64), rows.size - 1)
        out[m] = rows[pick].astype(out.dtype, copy=False)
    return out
