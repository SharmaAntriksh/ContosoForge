import numpy as np
import pandas as pd

from src.exceptions import DimensionError


def _stratified_trim_indices(
    group_values: np.ndarray,
    target_n: int,
    seed: int,
) -> np.ndarray:
    """
    Return row indices (into the base table) sampled WITHOUT replacement,
    stratified by group_values (e.g., SubcategoryKey), with deterministic seed.
    """
    if target_n <= 0:
        return np.empty(0, dtype=np.int64)

    g = np.asarray(group_values)
    if g.size == 0:
        return np.empty(0, dtype=np.int64)

    # Normalize group ids (coerce NaN -> 0)
    if np.issubdtype(g.dtype, np.number):
        g = np.where(np.isfinite(g), g, 0)
    else:
        # last-resort: factorize non-numeric
        g, _ = pd.factorize(pd.Series(g).astype("string"), sort=True)

    g = g.astype(np.int64, copy=False)

    uniq, counts = np.unique(g, return_counts=True)
    total = int(counts.sum())
    if target_n >= total:
        return np.arange(total, dtype=np.int64)

    # Allocation via largest-remainder method (Hamilton)
    raw = counts / total * float(target_n)
    alloc = np.floor(raw).astype(np.int64)
    frac = raw - alloc

    remainder = int(target_n - int(alloc.sum()))
    if remainder > 0:
        rng = np.random.default_rng(seed)
        frac2 = frac + rng.random(frac.shape[0]) * 1e-12  # deterministic tie-break
        order = np.argsort(-frac2, kind="mergesort")
        alloc[order[:remainder]] += 1

    # Cap to available counts, then redistribute any deficit to groups with capacity
    alloc = np.minimum(alloc, counts)
    deficit = int(target_n - int(alloc.sum()))
    if deficit > 0:
        rng = np.random.default_rng(seed + 1)
        rem_cap = counts - alloc
        # Score groups with remaining capacity; prefer larger capacity then frac
        frac2 = frac + rng.random(frac.shape[0]) * 1e-12
        score = rem_cap.astype(np.float64) * 1e6 + frac2
        order = np.argsort(-score, kind="mergesort")
        i = 0
        while deficit > 0 and rem_cap.sum() > 0:
            gi = order[i % order.size]
            if rem_cap[gi] > 0:
                alloc[gi] += 1
                rem_cap[gi] -= 1
                deficit -= 1
            i += 1

    # Now sample within each group without replacement
    rng = np.random.default_rng(seed)
    picked = []
    for u, k in zip(uniq, alloc):
        k = int(k)
        if k <= 0:
            continue
        idx = np.flatnonzero(g == u).astype(np.int64, copy=False)
        if idx.size <= k:
            picked.append(idx)
        else:
            # deterministic shuffle within group
            perm = rng.permutation(idx.size)
            picked.append(idx[perm[:k]])

    if not picked:
        return np.empty(0, dtype=np.int64)

    sel = np.concatenate(picked).astype(np.int64, copy=False)
    # Light shuffle to avoid grouped-by-subcategory blocks, still deterministic
    sel = sel[np.random.default_rng(seed + 2).permutation(sel.size)]
    return sel


def expand_contoso_products(
    base_products: pd.DataFrame,
    num_products: int,
    seed: int = 42,
    price_jitter_pct: float = 0.0,
) -> pd.DataFrame:
    """
    Single scaling path for Products:
      - If num_products < base_count: stratified trim by SubcategoryKey (no replacement)
      - If num_products == base_count: no-op (but still standardizes identity columns)
      - If num_products > base_count: expand by repeating base rows (variants)

    Identity rules (consistent across trim/expand):
      - BaseProductID = original Contoso ProductKey
      - ProductKey     = 1..num_products (dense surrogate)
      - VariantIndex   = 0 for trimmed/no-op; 0.. for expanded per BaseProductID
      - ProductCode    = zero-padded ProductKey (string)

    Pricing MUST be handled elsewhere (apply_product_pricing). price_jitter_pct is ignored.
    """
    _ = float(price_jitter_pct)  # backward compatible arg; intentionally ignored

    if not isinstance(num_products, (int, np.integer)) or int(num_products) <= 0:
        raise DimensionError("num_products must be a positive integer")
    num_products = int(num_products)

    base = base_products.reset_index(drop=True)
    base_count = int(len(base))
    if base_count <= 0:
        raise DimensionError("base_products is empty")

    if "SubcategoryKey" not in base.columns:
        # Fallback: simple deterministic shuffle/trim/expand if SubcategoryKey missing
        rng = np.random.default_rng(seed)
        order = rng.permutation(base_count)
        base = base.iloc[order].reset_index(drop=True)

    if num_products <= base_count:
        # -------- stratified trim (or exact no-op) --------
        if num_products == base_count:
            trimmed = base.copy()
        else:
            if "SubcategoryKey" in base.columns:
                g = pd.to_numeric(base["SubcategoryKey"], errors="coerce").fillna(0).to_numpy(dtype=np.int64)
                sel = _stratified_trim_indices(g, num_products, seed)
            else:
                sel = np.random.default_rng(seed).permutation(base_count)[:num_products]
            trimmed = base.iloc[sel].reset_index(drop=True).copy()

        # New dense surrogate key
        trimmed["ProductKey"] = np.arange(1, num_products + 1, dtype=np.int64)

        # No variants when trimmed: each product IS its own base
        trimmed["BaseProductID"] = trimmed["ProductKey"].copy()

        trimmed["VariantIndex"] = np.zeros(num_products, dtype=np.int64)

        # Business-friendly code
        trimmed["ProductCode"] = trimmed["ProductKey"].astype(str).str.zfill(7)

        return trimmed

    # -------- expansion (variants) --------
    rng = np.random.default_rng(seed)

    # Shuffle base first so "remainder" doesn't bias toward early rows
    base_shuf = base.sample(frac=1.0, random_state=int(seed)).reset_index(drop=True)

    repeat_factor = int(np.ceil(num_products / base_count))
    expanded = pd.concat([base_shuf] * repeat_factor, ignore_index=True).iloc[:num_products].copy()

    # Save original Contoso key for grouping, then assign dense surrogate
    original_key = pd.to_numeric(expanded["ProductKey"], errors="coerce").fillna(0).astype("int64")
    expanded["ProductKey"] = np.arange(1, num_products + 1, dtype=np.int64)

    # Variant index per original Contoso product
    expanded["VariantIndex"] = original_key.groupby(original_key).cumcount().astype("int64")

    # Map BaseProductID to the NEW ProductKey of each group's first variant (VariantIndex=0)
    base_map = expanded.loc[expanded["VariantIndex"] == 0, ["ProductKey"]].copy()
    base_map["_orig"] = original_key[expanded["VariantIndex"] == 0].values
    orig_to_new = base_map.set_index("_orig")["ProductKey"]
    expanded["BaseProductID"] = original_key.map(orig_to_new).astype("int64")

    # ProductCode
    expanded["ProductCode"] = expanded["ProductKey"].astype(str).str.zfill(7)

    # Name suffix only for variants (VariantIndex > 0)
    if "ProductName" in expanded.columns:
        vi = expanded["VariantIndex"].to_numpy(dtype=np.int64, copy=False)
        suffix = pd.Series(vi).astype(str).str.zfill(3)
        expanded.loc[vi > 0, "ProductName"] = (
            expanded.loc[vi > 0, "ProductName"].astype(str) + " - V" + suffix[vi > 0].to_numpy()
        )

    return expanded