# `main` vs `improvements` — what the sales-fact work changed

A measured before/after of the sales fact, generated from **each branch's own shipped
defaults**, patched identically only for scale/seed/dates so the comparison isolates
code + model-default differences.

- **Config:** seed `20250701`, 60,000 sales rows, 8,000 customers, 4 stores, contoso 120-product
  catalog, dates 2022-01-01 … 2023-12-31, parquet. Secondary facts (returns/budget/inventory/
  wishlists/complaints) disabled for speed.
- **Runs per branch:** `w1_c4000`, `w4_c4000`, `w1_c1000` (workers × chunk_size), same seed.
- **`main`** = commit before the improvement work; **`improvements`** = current branch.
- Every number below was **independently re-derived** by separate agents (different methods)
  and passed an adversarial fairness audit; the branches' `config.yaml` differs only in output
  paths, and `models.yaml` differs only by the new Phase-3/4 blocks.

The changes fall into three honest buckets.

---

## A. Reproducibility & determinism — *pure code* (the headline engineering win)

Same seed, same config, only the parallelism knobs vary. This is entirely the
static-schedule + month-plan-sharding refactor (Phases 1–2); no config assist.

| Property | `main` | `improvements` |
|---|---|---|
| **Output identical across `--workers` (1 vs 4), fixed chunk_size** | ❌ **No** — different dataset (digests `a6b4df…` vs `0b7539…`) | ✅ **Yes** — byte-identical (`53c072…` == `53c072…`) |
| **Per-month rows stable across `chunk_size` (4000 vs 1000)** | ❌ No — up to **±32 rows/month** | ✅ **Yes** — 0 difference |
| **Per-month distinct customers stable across `chunk_size`** | ❌ No — up to **±38 customers/month** | ✅ **Yes** — 0 difference |

**Precisely what was wrong on `main`:**
- *Worker count:* per-month **row counts** stay stable, but **which customer** each order
  belongs to (and the resulting per-line content, down to `OrderNumber` assignment) shifts with
  worker count — so the same logical run produces a genuinely different dataset. This is the
  `seen_customers` accumulator bug that Phase 1.1 replaced with a closed-form discovery schedule.
- *Chunk size:* the data **shape itself** moved — per-month row *and* distinct-customer counts
  changed with `chunk_size`. Phase 2 made `chunk_size` a pure performance knob.

**Honest scope:** on `improvements`, worker-invariance is *full byte-identity*. Chunk-invariance
is **shape** invariance — per-month rows and the distinct-customer set are identical, but per-line
RNG decorations still differ across chunk sizes (that is by design and expected). So the accurate
claim is *"chunk_size no longer changes the per-month row/customer shape,"* **not** "identical
output regardless of chunk_size."

---

## B. Analytical realism — *new features shipped default-on* (Phases 3–4)

These are new `models.yaml` capabilities that **do not exist in `main`'s schema at all**. They are
a legitimate "ship it on by default" improvement, but they are *new-capability enablement*, not
re-tuning of an existing pipeline.

| Signal | `main` | `improvements` | Feature |
|---|---|---|---|
| **Price → quantity elasticity** (Spearman `UnitPrice` vs `Quantity`) | −0.00 (no relationship) | **−0.60** (strong, clean) | 3.1 elasticity |
| Mean quantity across price quartiles | flat (~2.85 → 2.86) | **3.75 → 3.18 → 2.69 → 2.00** (monotone) | 3.1 |
| **Basket subcategory concentration** (within-order HHI) | 0.420 | **0.444** | 3.3 basket theme |
| Promotion footprint (discounted lines / mean discount) | 30.8% / 21.5 | 56.2% / 38.4 | 3.2 promo salience |

- **Elasticity (−0.60)** is the clean, safe-to-attribute result: quantity falls monotonically as
  product price rises, exactly as `(unit_price/ref)^(−ε)` predicts, and is absent on `main`.
- **Basket concentration (+0.024 HHI)** is *statistically robust* (z ≈ 12.5, bootstrap CI excludes
  0) but **modest in magnitude** (~5.7% relative) — a real, deliberate lift, not a large structural
  shift.
- **Promotion footprint** grew because promo-salience + markdown-reconcile are enabled. We report
  it as a feature-driven descriptive fact and make **no causal "discounts drive demand" claim** —
  the raw discount%→quantity correlation is a price confound (it collapses within a price band and
  flips positive among promoted-only lines).

---

## C. Pricing & schema consistency

| Property | `main` | `improvements` | Source |
|---|---|---|---|
| **Distinct posted `UnitPrice` per (product, month)** | **1.87** (86.8% of product-months carry >1 price) | **1.00** (0% carry >1) | 4.1 deterministic price |
| `ChannelKey` / `TimeKey` dtype | int32 / int32 | **int16 / int16** | 5.5 canonical schema |
| `OrderNumber` dtype | int32 | int32 (int64 only when the ID ceiling needs it) | 1.2 |

- **Deterministic pricing (4.1):** on `main`, the per-line stochastic snap gave the *same product in
  the same month* up to two different list prices on 87% of product-months; on `improvements`, a
  `(product, month)` self-join recovers **exactly one** posted price. Per-line variation now lives
  only in `DiscountAmount`.
- **Schema (5.5):** `ChannelKey`/`TimeKey` are now `int16` to match the **SQL foreign-key contract**
  (`Time.TimeKey SMALLINT`) — this fixes a parquet-vs-DDL dtype mismatch that would break FK creation
  at import. Values fit in int16 with no truncation. This is a *DB-import correctness* fix, not a
  data-fidelity change.

---

## Caveats & limitations (stated for honesty)

- **B and C differences are gated by new `models.yaml` blocks absent from `main`.** A `main` run
  cannot produce these behaviors because the code/schema isn't there — this is new capability, not
  better tuning of the same capability.
- **Chunk-invariance is shape-level**, not byte-level (see A).
- **Secondary facts were disabled.** The fulfillment-friction *delivery↔returns coupling* (3.4) and
  anything about returns/budget/inventory is **unmeasured here** and is not claimed.
- **Total rows (60,000) are preserved on both branches** — that's a correctness floor, not a
  differentiator; `main` also preserves row totals. The differentiator is per-month *shape*
  stability, not the total.
- **Scale is small** (60k rows, 2 years, 120 products). The elasticity and pricing results are
  structural and would persist at scale; the basket effect is significant but small.

## Verification

Metrics computed in `scratchpad/analyze.py`, then independently re-derived by 3 separate agents
(worker-invariance via cell-by-cell `df.equals`, pricing via a set-based distinct count *and*
`groupby.nunique`, elasticity via a hand-rolled rank-Pearson Spearman) plus an adversarial fairness
auditor that reproduced every value (basket HHI to 6 decimals) and confirmed the config diff is
only output paths + the new Phase-3/4 blocks. All agreed; no discrepancies.
