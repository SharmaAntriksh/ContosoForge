"""Validate EVERY Power BI relationship at the data level.

Parses the generated ``relationships.tmdl`` (the authoritative model contract) and,
for each relationship, checks referential integrity (no orphan foreign keys), parent
key uniqueness (the "one" side a relationship requires), and — for every date column
related to the Dates table — that it falls inside the Dates range. Also verifies the
Dates table is unique and contiguous (no missing days), which time intelligence needs.

The "many vs one" side is inferred from the data (whichever side the other is a subset
of), so it works regardless of TMDL cardinality quirks and 1:1 bidirectional tables.

Usage:
    python scripts/verify_model_relationships.py "generated_datasets/<dataset folder>"

Exit code 0 = all passed, 1 = failures found.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# TMDL model-table name -> packaged parquet path (relative to the dataset folder)
TBL2PARQUET = {
    "AcquisitionChannel": "dimensions/customer_acquisition_channels",
    "Budget": "facts/budget_yearly", "BudgetMonthly": "facts/budget_monthly",
    "Complaints": "facts/complaints", "Currency": "dimensions/currency",
    "Customer": "dimensions/customers", "CustomerProfile": "dimensions/customer_profile",
    "Dates": "dimensions/dates", "Employee": "dimensions/employees",
    "EmployeeStoreAssignment": "dimensions/employee_store_assignments",
    "ExchangeRate": "dimensions/exchange_rates",
    "ExchangeRateMonthly": "dimensions/exchange_rates_monthly",
    "Geography": "dimensions/geography", "Inventory": "facts/inventory_snapshot",
    "LoyaltyTiers": "dimensions/loyalty_tiers",
    "OrganizationProfile": "dimensions/organization_profile",
    "Plan": "dimensions/plans", "PlanSubscription": "dimensions/customer_subscriptions",
    "ProductCategory": "dimensions/product_category",
    "ProductProfile": "dimensions/product_profile", "Products": "dimensions/products",
    "ProductSubcategory": "dimensions/product_subcategory",
    "Promotion": "dimensions/promotions", "ReturnReason": "dimensions/return_reason",
    "Sales": "facts/sales", "Channel": "dimensions/channels",
    "Returns": "facts/returns", "Store": "dimensions/stores",
    "Supplier": "dimensions/suppliers", "Time": "dimensions/time",
    "Warehouse": "dimensions/warehouses", "Wishlist": "facts/customer_wishlists",
}
# Columns that hold dates (normalized to day for comparison)
DATE_COLS = {"OrderDate", "DueDate", "DeliveryDate", "Date", "ReturnDate",
             "SnapshotDate", "AddedDate", "StartDate", "EndDate", "ComplaintDate"}

fails: list[str] = []


def _add(name: str, ok: bool, msg: str) -> None:
    tag = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  [{tag}] {name}: {msg}")
    if not ok:
        fails.append(name)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/verify_model_relationships.py <dataset_folder>")
        return 2
    folder = Path(sys.argv[1])
    if not folder.is_absolute():
        folder = Path.cwd() / folder
    if not folder.exists():
        print(f"Folder not found: {folder}")
        return 2

    rel_paths = list(folder.glob("**/definition/relationships.tmdl"))
    if not rel_paths:
        print("No relationships.tmdl found under the dataset (need the generated Power BI project).")
        return 2

    _cache: dict[str, pd.DataFrame | None] = {}

    def load(tbl: str) -> pd.DataFrame | None:
        if tbl not in _cache:
            if tbl not in TBL2PARQUET:
                _cache[tbl] = None
            else:
                p = folder / f"{TBL2PARQUET[tbl]}.parquet"
                _cache[tbl] = pd.read_parquet(p) if p.exists() else None
        return _cache[tbl]

    def col_vals(tbl: str, col: str) -> pd.Series | None:
        df = load(tbl)
        if df is None or col not in df.columns:
            return None
        s = df[col]
        if col in DATE_COLS or pd.api.types.is_datetime64_any_dtype(s):
            s = pd.to_datetime(s, errors="coerce").dt.normalize()
        return s

    # --- parse relationships.tmdl ---
    rels: list[dict] = []
    cur: dict = {}
    for line in rel_paths[0].read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("relationship "):
            if cur:
                rels.append(cur)
            cur = {"isActive": True}
        elif s.startswith("fromColumn:"):
            cur["fromT"], cur["fromC"] = s.split(":", 1)[1].strip().split(".", 1)
        elif s.startswith("toColumn:"):
            cur["toT"], cur["toC"] = s.split(":", 1)[1].strip().split(".", 1)
        elif s.startswith("isActive:"):
            cur["isActive"] = "true" in s.lower()
    if cur:
        rels.append(cur)

    print(f"\nParsed {len(rels)} relationships from {rel_paths[0].name}\n")
    print("=" * 70)
    print("RELATIONSHIP INTEGRITY (no orphan FKs; parent key unique)")
    print("=" * 70)

    for r in rels:
        tag = "" if r.get("isActive", True) else " (inactive)"
        label = f"{r['fromT']}.{r['fromC']} -> {r['toT']}.{r['toC']}{tag}"
        a = col_vals(r["fromT"], r["fromC"])
        b = col_vals(r["toT"], r["toC"])
        if a is None or b is None:
            miss = f"{r['fromT']}.{r['fromC']}" if a is None else f"{r['toT']}.{r['toC']}"
            _add(label, False, f"column/table not found: {miss}")
            continue

        a_nn, b_nn = a.dropna(), b.dropna()
        aset, bset = set(a_nn.tolist()), set(b_nn.tolist())
        n_fwd = int((~a_nn.isin(bset)).sum())  # from-values missing from to
        n_rev = int((~b_nn.isin(aset)).sum())  # to-values missing from from

        # Parent (one-side) = the side the other is a subset of. RI orphans are the
        # child values absent from the parent; whichever direction is clean wins.
        if n_fwd == 0:
            parent_col, parent_s, child = f"{r['toT']}.{r['toC']}", b_nn, a
            ri = 0
        elif n_rev == 0:
            parent_col, parent_s, child = f"{r['fromT']}.{r['fromC']}", a_nn, b
            ri = 0
        else:  # genuine dangling keys both ways -> report against the declared "to"
            parent_col, parent_s, child = f"{r['toT']}.{r['toC']}", b_nn, a
            ri = n_fwd

        dup = int(parent_s.duplicated().sum())
        child_nulls = int(child.isna().sum())
        ok = ri == 0 and dup == 0
        extra = []
        if ri:
            extra.append(f"{ri:,} orphan FK row(s) not in {parent_col}")
        if dup:
            extra.append(f"{dup} duplicate key(s) in parent {parent_col}")
        if child_nulls:
            extra.append(f"{child_nulls:,} null FK (-> blank member)")
        _add(label, ok, "; ".join(extra) if extra else "clean")

    # --- Dates coverage + health (time intelligence) ---
    print()
    print("=" * 70)
    print("DATES COVERAGE & HEALTH")
    print("=" * 70)
    dates = col_vals("Dates", "Date")
    if dates is None:
        _add("Dates table", False, "Dates.Date column not found")
    else:
        dser = dates.dropna()
        dset = set(dser.tolist())
        dmin, dmax = dser.min(), dser.max()
        _add("Dates: unique Date", int(dser.duplicated().sum()) == 0,
             f"{int(dser.duplicated().sum())} duplicate(s)")
        full = pd.date_range(dmin, dmax, freq="D")
        missing = len(full) - len(dset)
        _add("Dates: contiguous daily (no gaps)", missing == 0,
             f"{dmin.date()}..{dmax.date()} ({len(full)} days), {missing} missing")

        date_edges = [(r["fromT"], r["fromC"]) for r in rels
                      if r["toT"] == "Dates" and r["toC"] == "Date"]
        for t, c in date_edges:
            s = col_vals(t, c)
            if s is None:
                continue
            s = s.dropna()
            if len(s) == 0:
                _add(f"{t}.{c} within Dates", True, "no rows")
                continue
            out = int((~s.isin(dset)).sum())
            _add(f"{t}.{c} within Dates", out == 0,
                 f"{out:,} row(s) outside Dates "
                 f"(col {s.min().date()}..{s.max().date()} vs Dates {dmin.date()}..{dmax.date()})")

    print()
    print("=" * 70)
    if fails:
        print(f"SUMMARY: \033[31m{len(fails)} FAILED\033[0m")
        for f in fails:
            print(f"  FAIL: {f}")
    else:
        print("SUMMARY: \033[32mALL PASSED\033[0m")
    print("=" * 70)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
