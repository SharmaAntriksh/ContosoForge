"""Single source of truth for the customer sales-eligibility predicate.

A customer is eligible to transact in month ``m`` iff::

    active == 1  AND  start_month <= m  AND  (end_month < 0 OR m <= end_month)

(``end_month < 0`` means "no end within the window"; ``_normalize_end_month``
maps every negative sentinel to -1 before it reaches here.)

This rule previously lived in three hand-maintained copies that had to agree for
the plan to be consistent — a per-month boolean mask, a per-month index list,
and an O(N+T) delta/cumsum count. They are consolidated here as three *views*
over one predicate so the coordinator's global plan and the worker's month pool
can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class EligibilityWindow:
    """The lifecycle arrays (int64) plus the three eligibility views.

    Construct via :meth:`from_lifecycle`, which performs the canonical
    ``int64``/C-contiguous casts once. All three views apply the same
    predicate; ``counts`` computes it in O(N+T) rather than materializing a
    mask per month.
    """

    is_active: np.ndarray       # int64; == 1 means active in sales
    start_month: np.ndarray     # int64; first eligible month offset
    end_month_norm: np.ndarray  # int64; < 0 means "no end within window"

    @classmethod
    def from_lifecycle(cls, is_active, start_month, end_month_norm) -> "EligibilityWindow":
        return cls(
            np.asarray(is_active, dtype="int64", order="C"),
            np.asarray(start_month, dtype="int64", order="C"),
            np.asarray(end_month_norm, dtype="int64", order="C"),
        )

    def mask(self, m_offset: int) -> np.ndarray:
        """Boolean eligibility mask over all customers for a single month."""
        m = int(m_offset)
        return (
            (self.is_active == 1)
            & (self.start_month <= m)
            & ((self.end_month_norm < 0) | (m <= self.end_month_norm))
        )

    def indices_by_month(self, T: int) -> list:
        """List (length ``T``) of eligible customer row indices per month."""
        T = int(T)
        active = self.is_active == 1
        start_month = self.start_month
        end_month_norm = self.end_month_norm
        idx_by_month: list = []
        for m in range(T):
            mask = active & (start_month <= m) & ((end_month_norm < 0) | (m <= end_month_norm))
            idx_by_month.append(np.flatnonzero(mask))
        return idx_by_month

    def counts(self, T: int) -> np.ndarray:
        """Per-month eligible customer count in O(N + T) via a delta/cumsum.

        Produces exactly ``sum(mask(m))`` for each ``m`` in ``[0, T)`` but
        without building a mask per month.
        """
        T = int(T)
        if T <= 0:
            return np.zeros(0, dtype="float64")

        start_month = self.start_month
        end_month_norm = self.end_month_norm

        if start_month.size == 0:
            return np.zeros(T, dtype="float64")

        active = self.is_active == 1
        if not active.any():
            return np.zeros(T, dtype="float64")

        s = start_month[active]
        e = end_month_norm[active]

        # Warm-start customers (start_month < 0) joined before the window, so they
        # are eligible from month 0 — clamp to 0 to match the per-month predicate
        # (``start_month <= m``). A no-op on the default path (starts already
        # clamped to >= 0), it keeps the coordinator's global plan and the chunk's
        # month pool computed against the SAME eligible base. Then drop starts
        # at/after the window (never eligible in [0, T-1]).
        s = np.where(s < 0, np.int64(0), s)
        valid_start = s < T
        s = s[valid_start]
        e = e[valid_start]
        if s.size == 0:
            return np.zeros(T, dtype="float64")

        # discard invalid ranges where end < start (and end != -1)
        valid_range = (e < 0) | (e >= s)
        s = s[valid_range]
        e = e[valid_range]
        if s.size == 0:
            return np.zeros(T, dtype="float64")

        delta = np.zeros(T + 1, dtype="int64")
        np.add.at(delta, s, 1)

        finite = e >= 0
        if finite.any():
            endp1 = e[finite] + 1
            endp1 = endp1[(endp1 > 0) & (endp1 < T)]
            if endp1.size:
                np.add.at(delta, endp1, -1)

        counts = np.cumsum(delta[:-1])
        counts = np.maximum(counts, 0)
        return counts.astype("int64", copy=False)
