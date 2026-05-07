"""Household assignment for customer dimension.

Optimized for large N (1M+): uses fully vectorized numpy operations
for spouse matching and dependent recruitment with no per-head Python loops.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np

from src.defaults import (
    CUSTOMER_HOUSEHOLD_HEAD_MIN_AGE as _HEAD_MIN_AGE,
    CUSTOMER_HOUSEHOLD_SPOUSE_MAX_AGE_GAP as _SPOUSE_MAX_AGE_GAP,
    CUSTOMER_HOUSEHOLD_SPOUSE_MIN_AGE as _SPOUSE_MIN_AGE,
    CUSTOMER_HOUSEHOLD_DEPENDENT_MIN_AGE_GAP as _DEPENDENT_MIN_AGE_GAP,
    CUSTOMER_HOUSEHOLD_DEPENDENT_MAX_AGE as _DEPENDENT_MAX_AGE,
)


def _match_sorted_1to1(
    head_ages: np.ndarray,
    cand_ages: np.ndarray,
    max_gap: float,
    min_cand_age: float,
) -> np.ndarray:
    """Greedy 1-to-1 matching of sorted head ages to sorted candidate ages.

    Both arrays must be sorted ascending.  Returns an int array of length
    len(head_ages) where result[i] is the index into cand_ages matched to
    head i, or -1 if no match.  Each candidate is used at most once.

    O(n_heads log n_cands) for the bulk searchsorted, plus a Python loop
    over heads. Faster than the original per-head while-loop because the
    inner advance is replaced by a single bulk searchsorted call.
    """
    n_h = len(head_ages)
    n_c = len(cand_ages)
    result = np.full(n_h, -1, dtype=np.intp)
    if n_h == 0 or n_c == 0:
        return result

    # Bulk window bounds: lo[i] is the leftmost cand index with age >= head[i]-gap;
    # hi[i] is the leftmost cand index with age >  head[i]+gap (exclusive upper).
    lo_vals = np.maximum(head_ages - max_gap, min_cand_age)
    hi_vals = head_ages + max_gap
    lo_idx = np.searchsorted(cand_ages, lo_vals, side="left")
    hi_idx = np.searchsorted(cand_ages, hi_vals, side="right")

    avail = 0
    for i in range(n_h):
        s = lo_idx[i] if lo_idx[i] > avail else avail
        if s < hi_idx[i]:
            result[i] = s
            avail = s + 1
    return result


def assign_households(
    rng: np.random.Generator,
    N: int,
    is_org: np.ndarray,
    geography_key: np.ndarray,
    gender: np.ndarray,
    ages_years: np.ndarray,
    marital_status: np.ndarray,
    children_raw: np.ndarray,
    household_pct: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """Assign HouseholdKey and HouseholdRole for all customers.

    Mutates *geography_key* in place so household members share
    the same geography.

    Returns (HouseholdKey, HouseholdRole) arrays of length N.
    """
    household_key = np.zeros(N, dtype="int64")
    household_role = np.empty(N, dtype=object)
    household_role[:] = None

    person_mask = ~is_org
    person_idx = np.where(person_mask)[0]
    n_person = len(person_idx)

    if n_person == 0:
        household_key[:] = np.arange(1, N + 1)
        return household_key, household_role

    married_mask = (marital_status[person_idx] == "Married") & (ages_years[person_idx] >= _HEAD_MIN_AGE)
    married_idx = person_idx[married_mask]

    n_target_heads = max(1, int(round(n_person * household_pct)))
    n_heads = min(n_target_heads, len(married_idx))

    if n_heads == 0:
        household_key[:] = np.arange(1, N + 1)
        household_role[person_mask] = "Head"
        return household_key, household_role

    head_indices = rng.choice(married_idx, size=n_heads, replace=False)
    ages_f64 = ages_years.astype(np.float64)

    # Assign household IDs to all heads upfront
    hh_ids = np.arange(1, n_heads + 1, dtype=np.int32)
    household_key[head_indices] = hh_ids
    household_role[head_indices] = "Head"

    avail = np.zeros(N, dtype=bool)
    avail[person_idx] = True
    avail[head_indices] = False

    # =================================================================
    # SPOUSE MATCHING — vectorized greedy 1:1 by sorted age
    # =================================================================
    all_matched_spouses = []
    all_matched_hh_ids = []
    all_matched_head_idx = []

    for target_g, head_g in [("Female", "Male"), ("Male", "Female")]:
        h_mask = gender[head_indices] == head_g
        h_local = np.where(h_mask)[0]
        if len(h_local) == 0:
            continue

        # Sort heads by age
        h_ages = ages_f64[head_indices[h_local]]
        h_order = np.argsort(h_ages)
        h_sorted_local = h_local[h_order]
        h_sorted_ages = h_ages[h_order]

        # Candidate pool sorted by age
        c_idx = np.where(avail & (gender == target_g))[0]
        if len(c_idx) == 0:
            continue
        c_ages = ages_f64[c_idx]
        c_order = np.argsort(c_ages)
        c_sorted = c_idx[c_order]
        c_sorted_ages = c_ages[c_order]

        # O(n) greedy match
        matched_c = _match_sorted_1to1(
            h_sorted_ages, c_sorted_ages, _SPOUSE_MAX_AGE_GAP, _SPOUSE_MIN_AGE,
        )

        got = matched_c >= 0
        if got.any():
            spouse_global = c_sorted[matched_c[got]]
            head_local_matched = h_sorted_local[got]
            all_matched_spouses.append(spouse_global)
            all_matched_hh_ids.append(hh_ids[head_local_matched])
            all_matched_head_idx.append(head_indices[head_local_matched])
            avail[spouse_global] = False

    # Apply spouse matches in bulk
    if all_matched_spouses:
        sp = np.concatenate(all_matched_spouses)
        sp_hh = np.concatenate(all_matched_hh_ids)
        sp_head = np.concatenate(all_matched_head_idx)
        household_key[sp] = sp_hh
        household_role[sp] = "Spouse"
        geography_key[sp] = geography_key[sp_head]

    # =================================================================
    # DEPENDENT RECRUITMENT — vectorized greedy
    # =================================================================
    dep_wanted = children_raw[head_indices].astype(int)
    heads_wanting = np.where(dep_wanted > 0)[0]

    if len(heads_wanting) > 0:
        dep_pool_idx = np.where(avail & (ages_f64 <= _DEPENDENT_MAX_AGE))[0]

        if len(dep_pool_idx) > 0:
            dep_ages = ages_f64[dep_pool_idx]
            # Sort descending (oldest first)
            dep_order = np.argsort(-dep_ages)
            dep_sorted = dep_pool_idx[dep_order]
            dep_sorted_ages = dep_ages[dep_order]

            # Head info
            hwd_global = head_indices[heads_wanting]
            hwd_max_dep_age = ages_f64[hwd_global] - _DEPENDENT_MIN_AGE_GAP
            hwd_n_wanted = dep_wanted[heads_wanting]
            hwd_hh = hh_ids[heads_wanting]

            # Sort heads by max_dep_age descending
            hwd_order = np.argsort(-hwd_max_dep_age)

            # Collect assignments in bulk arrays
            assign_dep = []
            assign_hh = []
            assign_head = []

            dep_ptr = 0
            n_deps = len(dep_sorted)

            for wi in range(len(hwd_order)):
                hi_idx = hwd_order[wi]
                max_age = hwd_max_dep_age[hi_idx]
                if max_age <= 0:
                    continue
                n_want = int(hwd_n_wanted[hi_idx])
                n_got = 0

                while dep_ptr < n_deps and n_got < n_want:
                    if dep_sorted_ages[dep_ptr] <= max_age:
                        assign_dep.append(int(dep_sorted[dep_ptr]))
                        assign_hh.append(hwd_hh[hi_idx])
                        assign_head.append(hwd_global[hi_idx])
                        n_got += 1
                    dep_ptr += 1

            # Apply in bulk
            if assign_dep:
                dep_arr = np.array(assign_dep, dtype="int64")
                hh_arr = np.array(assign_hh, dtype="int64")
                head_arr = np.array(assign_head, dtype="int64")
                household_key[dep_arr] = hh_arr
                household_role[dep_arr] = "Dependent"
                avail[dep_arr] = False
                geography_key[dep_arr] = geography_key[head_arr]

    # --- Assign solo households ---
    hh_id = n_heads
    unassigned = np.where(household_key == 0)[0]
    household_key[unassigned] = np.arange(hh_id + 1, hh_id + 1 + len(unassigned))
    solo_persons = unassigned[person_mask[unassigned]]
    household_role[solo_persons] = "Head"
    # Org customers are standalone — None is the convention for org rows
    # (consistent with MaritalStatus, Education, Occupation for orgs)

    return household_key, household_role


def head_indices_for_members(
    household_key: np.ndarray,
    household_role: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return (moved_mask, head_of_moved) for spouse/dependent members.

    ``moved_mask`` is a bool array of length N (True for members that need
    the head's address).  ``head_of_moved`` is an int index array mapping
    each True position to its household head's position.
    """
    moved = (household_role == "Spouse") | (household_role == "Dependent")
    if not moved.any():
        return moved, np.empty(0, dtype=np.intp)
    head_mask = household_role == "Head"
    hk_to_head = np.empty(int(household_key.max()) + 1, dtype=np.intp)
    hk_to_head[household_key[head_mask]] = np.where(head_mask)[0]
    return moved, hk_to_head[household_key[moved]]
