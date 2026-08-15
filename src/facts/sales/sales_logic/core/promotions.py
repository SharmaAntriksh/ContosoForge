"""Promotion assignment: date-windowed promo selection.

Assigns a PromotionKey to each sales row based on which promotions are
active on that row's order date.  Discount amounts are never computed
here — they live in the Promotions dimension and are joined at query time
via PromotionKey.
"""

from __future__ import annotations

import numpy as np

from src.defaults import PHYSICAL_CHANNELS, DIGITAL_CHANNELS
from src.exceptions import SalesError
from src.utils.logging_utils import warn
from ..weights import weighted_pick_indices

# Warn at most once per process when channel filtering is silently disabled.
_warned_ch_len_mismatch = False


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def _as_date32(x):
    """Normalize incoming date arrays to datetime64[D] for consistent comparisons."""
    if x is None:
        return None
    a = np.asarray(x)
    if a.size == 0:
        return a
    return a.astype("datetime64[D]", copy=False)


def _weighted_pick(rng, candidates, salience_all, size):
    """Pick ``size`` promo indices from ``candidates`` (indices into
    ``promo_keys_all``), weighted by salience when available, else uniform.

    Weighted selection uses inverse-CDF sampling (one ``rng.random(size)`` draw)
    so it is deterministic per RNG state. ``salience_all`` is a per-promo weight
    array aligned to ``promo_keys_all``; ``candidates`` indexes into it.
    """
    k = int(candidates.size)
    if salience_all is None:
        return candidates[rng.integers(0, k, size=size)]
    w = np.asarray(salience_all, dtype=np.float64)[candidates]
    w = np.where(np.isfinite(w) & (w > 0.0), w, 0.0)
    return candidates[weighted_pick_indices(rng, w, size, k)]


def apply_promotions(
    rng,
    n,
    order_dates,
    promo_keys_all,
    promo_start_all,
    promo_end_all,
    no_discount_key=1,
    *,
    channel_keys=None,
    promo_channel_group=None,
    promo_salience_all=None,
    customer_keys=None,
    customer_loyalty_tier_by_key=None,
    customer_start_day_by_key=None,
    customer_new_promo_end_by_key=None,
    promo_audience_group=None,
):
    """
    Assign at most one PromotionKey per row.

    For each row, picks among promotions whose [StartDate, EndDate] window
    covers that row's order date. When ``promo_salience_all`` is provided,
    the pick is weighted by per-promo salience (deeper / more prominent
    promotions redeemed more often); otherwise it is uniform.

    CORRELATION #5: When channel_keys and promo_channel_group are provided,
    promotions are filtered by channel affinity:
      - promo_channel_group=0 → any channel
      - promo_channel_group=1 → physical channels only (1=Store, 10=Kiosk)
      - promo_channel_group=2 → digital channels only (2,3,6,7,8)

    Audience-restricted promotions are filtered in the same candidate pass:
      - audience=0 -> any customer
      - audience=1 -> customers with LoyaltyTierKey > 0
      - audience=2 -> customers within their exact new-customer date window

    Rows with no eligible active promotion receive ``no_discount_key``.

    Returns:
      promo_keys: int64 array (len n)
    """
    n = int(n)
    promo_keys = np.full(n, int(no_discount_key), dtype=np.int32)

    if n <= 0:
        return promo_keys
    if promo_keys_all is None:
        return promo_keys

    promo_keys_all = np.asarray(promo_keys_all, dtype=np.int32)
    P = int(promo_keys_all.size)
    if P == 0:
        return promo_keys

    promo_start_all = _as_date32(promo_start_all)
    promo_end_all = _as_date32(promo_end_all)
    order_dates = _as_date32(order_dates)

    if promo_start_all is None or promo_end_all is None or order_dates is None:
        return promo_keys
    if order_dates.shape[0] != n:
        raise SalesError("order_dates length must match n")
    if promo_start_all.shape[0] != P or promo_end_all.shape[0] != P:
        raise SalesError("promo_start_all/promo_end_all must align with promo_keys_all length")

    promo_valid_glob = (promo_keys_all != int(no_discount_key))
    if not promo_valid_glob.any():
        return promo_keys

    # ------------------------------------------------------------
    # Group rows by unique date (fast path for month-sliced generation)
    # ------------------------------------------------------------
    unique_dates, inv = np.unique(order_dates, return_inverse=True)
    U = unique_dates.size

    # Build channel-group masks for promo filtering
    # Physical: 1=Store, 10=Kiosk (CallCenter is the distinct Assisted group)
    # Digital:  2=Online, 3=Marketplace, 6=Web, 7=MobileApp, 8=SocialCommerce
    # Business: 4=B2B, 9=PartnerReseller (accept only universal promos)
    _PHYSICAL_CH = frozenset(PHYSICAL_CHANNELS.tolist())
    _DIGITAL_CH = frozenset(DIGITAL_CHANNELS.tolist())
    _BUSINESS_CH = frozenset({4, 9})
    _ASSISTED_CH = frozenset({5})
    _has_ch_filter = (channel_keys is not None and promo_channel_group is not None
                      and len(promo_channel_group) == P)
    # CORE-4: channel filtering requested but mis-sized -> it silently disappears.
    # Warn (once) instead of dropping the channel correlation without a trace.
    if (channel_keys is not None and promo_channel_group is not None
            and len(promo_channel_group) != P):
        global _warned_ch_len_mismatch
        if not _warned_ch_len_mismatch:
            _warned_ch_len_mismatch = True
            warn(
                f"promo_channel_group length {len(promo_channel_group)} != number of "
                f"promotions ({P}); channel-affinity promo filtering disabled."
            )

    # Customer audience eligibility is encoded as two bits per row:
    # bit 0 = loyalty member, bit 1 = within new-customer window. If audience
    # metadata exists but customer metadata is absent, restricted promotions are
    # conservatively ineligible rather than leaking to unrelated customers.
    _has_audience_filter = (
        promo_audience_group is not None and len(promo_audience_group) == P
    )
    _row_audience = np.zeros(n, dtype=np.int8)
    if _has_audience_filter and customer_keys is not None:
        _ck = np.asarray(customer_keys, dtype=np.int64)
        if _ck.shape[0] != n:
            raise SalesError("customer_keys length must match n")
        if customer_loyalty_tier_by_key is not None:
            _loyalty = np.asarray(customer_loyalty_tier_by_key, dtype=np.int32)
            _safe = (_ck >= 0) & (_ck < len(_loyalty))
            _loyal_rows = np.zeros(n, dtype=bool)
            _loyal_rows[_safe] = _loyalty[_ck[_safe]] > 0
            _row_audience[_loyal_rows] |= 1
        if (
            customer_start_day_by_key is not None
            and customer_new_promo_end_by_key is not None
        ):
            _starts = np.asarray(customer_start_day_by_key, dtype=np.int64)
            _ends = np.asarray(customer_new_promo_end_by_key, dtype=np.int64)
            _safe = (_ck >= 0) & (_ck < len(_starts)) & (_ck < len(_ends))
            _days = order_dates.astype(np.int64)
            _new_rows = np.zeros(n, dtype=bool)
            _new_rows[_safe] = (
                (_starts[_ck[_safe]] != np.iinfo(np.int64).min)
                & (_days[_safe] >= _starts[_ck[_safe]])
                & (_days[_safe] <= _ends[_ck[_safe]])
            )
            _row_audience[_new_rows] |= 2

    _pag = (
        np.asarray(promo_audience_group, dtype=np.int8)
        if _has_audience_filter else None
    )

    def _assign_eligible(rows, candidates):
        """Pick only from audience-eligible candidates for each row group."""
        if rows.size == 0 or candidates.size == 0:
            return
        if _pag is None:
            chosen = _weighted_pick(rng, candidates, promo_salience_all, len(rows))
            promo_keys[rows] = promo_keys_all[chosen]
            return
        _candidate_audience = _pag[candidates]
        for _aud_code in np.unique(_row_audience[rows]):
            _aud_rows = rows[_row_audience[rows] == _aud_code]
            _ok = _candidate_audience == 0
            if int(_aud_code) & 1:
                _ok |= _candidate_audience == 1
            if int(_aud_code) & 2:
                _ok |= _candidate_audience == 2
            _eligible = candidates[_ok]
            if _eligible.size:
                chosen = _weighted_pick(
                    rng, _eligible, promo_salience_all, len(_aud_rows),
                )
                promo_keys[_aud_rows] = promo_keys_all[chosen]

    # U is typically <= 31 when month-sliced; building lists is fast
    # and avoids a U×P boolean matrix
    active_indices_per_date = []
    for d in unique_dates:
        active = promo_valid_glob & (d >= promo_start_all) & (d <= promo_end_all)
        active_indices_per_date.append(np.nonzero(active)[0])

    group_counts = np.bincount(inv, minlength=U).astype(np.int64)
    group_order = np.argsort(inv, kind="mergesort")
    group_starts = np.zeros(U + 1, dtype=np.int64)
    np.cumsum(group_counts, out=group_starts[1:])

    for code in range(U):
        idx = active_indices_per_date[code]
        if idx.size == 0:
            continue

        count = int(group_counts[code])
        gs = int(group_starts[code])
        ge = int(group_starts[code + 1])
        rows = group_order[gs:ge]

        # CORRELATION #5: filter promos by channel affinity.
        # Batch by channel type (physical/digital/business/other) — only
        # 4 groups, so we filter promos once per group and vectorized-
        # sample all rows in that group at once.  O(groups × promos)
        # instead of O(rows × promos).
        if _has_ch_filter:
            row_channels = np.asarray(channel_keys, dtype=np.int32)[rows]
            _pcg = np.asarray(promo_channel_group, dtype=np.int8)
            _pcg_idx = _pcg[idx]  # promo channel groups for active promos

            # Classify rows: 0=unknown, 1=physical, 2=digital,
            # 3=business, 4=assisted. Business/assisted/unknown accept only
            # universal (group=0) promotions because the promotion dimension has
            # no dedicated audience category for them.
            _ch_type = np.zeros(count, dtype=np.int8)
            for _phk in _PHYSICAL_CH:
                _ch_type[row_channels == _phk] = 1
            for _dgk in _DIGITAL_CH:
                _ch_type[row_channels == _dgk] = 2
            for _bk in _BUSINESS_CH:
                _ch_type[row_channels == _bk] = 3
            for _ak in _ASSISTED_CH:
                _ch_type[row_channels == _ak] = 4

            for _ct in np.unique(_ch_type):
                _ct_mask = _ch_type == _ct
                _ct_count = int(_ct_mask.sum())
                if _ct_count == 0:
                    continue
                _ct_rows = rows[_ct_mask]

                # Filter promos: group=0 (any) always accepted
                _ok = _pcg_idx == 0
                if _ct == 1:    # physical
                    _ok |= _pcg_idx == 1
                elif _ct == 2:  # digital
                    _ok |= _pcg_idx == 2
                elif _ct in (3, 4):  # business/assisted — only universal promos
                    pass
                else:           # unknown — only universal promos
                    pass

                _filtered = idx[_ok]
                if _filtered.size == 0:
                    continue  # keep no_discount_key

                _assign_eligible(_ct_rows, _filtered)
        else:
            _assign_eligible(rows, idx)

    return promo_keys
