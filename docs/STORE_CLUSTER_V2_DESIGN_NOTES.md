# Store-Cluster v2 Design Notes

> Status: living design doc. Written after the Phase 0–8 store-cluster refactor
> (branch `code-improvements`) landed. The refactor was **behaviour-preserving
> structural cleanup**; this doc records the *modelling* decisions we deliberately
> left frozen and what a greenfield v2 would do differently.

## Context

The store cluster is four tightly-coupled dimensions plus a bridge:

```
stores  ──co-produces──▶ warehouses
   │
   ├─ (stores.parquet columns) ──▶ employees ──▶ employee_store_assignments (bridge)
```

The Phase 0–8 refactor made this modular, testable, and gated by a byte-identity
golden fingerprint (`tests/_snapshots/store_cluster_fingerprints.json`). What it
did **not** touch was a small set of load-bearing modelling decisions — because
they are part of the *output contract* (keys, column names, schema) that
generated SQL, Power BI, and any downstream consumer depend on. Those are the
subject of this doc.

The headline finding: **the design is sound, but one decision (semantic key
encoding) generates most of the incidental complexity we spent the refactor
taming**, plus a couple of destroy-then-reparse seams and one honestly-named
tension (`Status`).

---

## Item 1 — Opaque surrogate keys (replace semantic key encoding)  ✅ DONE (employee keys)

**Priority: highest payoff. Risk: highest. Output-contract break.**

### Problem

`EmployeeKey` and `StoreKey` pack meaning into integer ranges:

| Tier | Encoding | Source |
|---|---|---|
| Physical store | `1..N` | — |
| Online store | `10_000 + i` | `ONLINE_STORE_KEY_BASE` |
| CEO / VP | `1` / `2` | constants |
| Region manager | `10_000 + RegionId` | `keys.encode_region` |
| District manager | `20_000 + DistrictId` | `keys.encode_district` |
| Store manager | `30_000_000 + StoreKey` | `keys.encode_store_manager` |
| Staff | `40_000_000 + StoreKey*1_000 + idx` | `keys.encode_staff` |
| Online rep | `50_000_000 + StoreKey` | `keys.encode_online_rep` |

Almost every hard problem the refactor touched traces back to this encoding:

- `src/dimensions/employees/keys.py` — the whole `EmployeeKeyCodec`,
  `decode_home_store_key`, `KeyBand`.
- `src/defaults.py` — `ONLINE_STORE_KEY_BASE`, `STAFF_KEY_STORE_MULT`,
  `is_online_store_key` / `is_physical_store_key`.
- `keys.encode_staff` needs a `MAX_STAFF_PER_STORE` overflow guard so
  within-store indices don't spill into the next store's slot range.
- The "boundary predicate spelled four different ways" bug (unified in Phase 2,
  grep-gated in `tests/test_gotchas_and_guards.py::TestOnlineBoundaryPredicate`).
- `_infer_home_store_key` in the bridge — a **second source of truth** for
  `StoreKey`, which the bridge otherwise already reads as an authoritative
  column. The decode exists only as a fallback/cross-check now.

The one real thing semantic keys buy is **decentralized deterministic key
minting** — a worker can compute a key from `(store, index)` with no global
counter. But a **deterministic hash of the natural key** (`hash(store, role,
idx)` folded into an int32/int64 domain) is *also* counter-free, *also*
deterministic, and opaque. So the property is preservable without the semantics.

### Proposed v2

- Mint keys as `stable_hash(natural_key) → surrogate` (collision-checked at
  generation; the domain is tiny relative to int64).
- Carry `StoreKey`, `OrgLevel`, and an explicit `IsOnlineStore` (bool) /
  `OrgUnitType` as **columns** — they already exist on the frames. "Which stores
  are online" becomes `StoreType == "Online"` (already present), not a key-range
  predicate.
- Delete `keys.py` codec, the boundary predicate, the overflow guard.

### Downstream ripple (why this needs sign-off)

Changing key **values** touches the output contract:

- Sales fact salesperson linkage (`SalespersonKey` → `EmployeeKey`) — every
  generated sales dataset's keys change.
- Generated SQL BULK INSERT/COPY values (structure unchanged, data changes).
- Power BI relationships (by column, still valid; values change).
- `scripts/verify_employee_store_sales.py` (decodes keys — must be rewritten to
  read columns).
- `tests/test_employee_keys.py` (entire file is about the codec).
- All four golden fingerprints regenerate.

This is a **v2 schema bump**, not a refactor. Do it in its own focused session.

### Resolution (done)

Scoped to **employee keys**. The sales fact already treats `EmployeeKey` as
opaque, so the change is self-contained to the employees package + bridge +
transfers (+ the verify script and tests). **Store keys were left unchanged**:
`StoreKey` is a foreign key in every fact (sales, inventory, wishlists,
complaints, budget), so re-keying stores is a much larger, separate blast radius;
the online-store encoding (`StoreKey > ONLINE_STORE_KEY_BASE`) remains a
documented follow-up.

Delivered in two commits: (1) a **byte-identical** pass replacing every
key-range *classification* with column logic (`OrgLevel`, `StoreKey`,
`is_online_store_key`), verified against the *unchanged* golden fingerprint to
prove equivalence; then (2) the value change — `EmployeeKey` became an opaque
**sequential surrogate** (1..N in build order; a deterministic hash was
unnecessary since the dimension is generated sequentially, not parallel-minted).
`ParentEmployeeKey` is wired from the org columns; `keys.py` (codec), the
staff-slot overflow guard, and the EmployeeKey band constants in `defaults.py`
are deleted. The gate shifted from byte-identity (intentionally broken for
`employees` + the bridge) to **determinism + referential integrity**. New
regression guard: `TestKeyIntegrity` (unique 1..N surrogates, parent-FK
integrity, store-level employees map to real stores).

---

## Item 2 — Structured `StoreDistrictId` / `StoreRegionId` columns

**Priority: high value, low risk. Additive schema change. DONE FIRST.**

### Problem — destroy-then-reparse seam

`stores/columns.py::_build_hierarchy` computes `district_id` / `region_id` as
int32 arrays, then **throws the ints away**, emitting only display strings:

```python
store_districts = np.char.add("District ", district_id.astype(str))
store_regions   = np.char.add("Region ",   region_id.astype(str))
return zones, store_districts, store_regions   # ints discarded
```

`employees/assembly.py::resolve_store_hierarchy` then **regex-reparses the ints
back out** of the strings it just built:

```python
district_id = stores["StoreDistrict"].astype(str).str.extract(r"(\d+)", ...).astype(np.int32)
```

The integer is the real value; the string is a display of it. A dimension should
carry the structured value and *derive* the display string, never round-trip
through a string and reparse. (The manager-name version of this smell was killed
in Phase 4.7 with structured first/last columns; this is the last one.)

### Proposed v2

- `_build_hierarchy` also returns the int arrays.
- Stores emits `StoreDistrictId` / `StoreRegionId` (int) **alongside** the
  human-readable `StoreDistrict` / `StoreRegion` strings (the strings are
  legitimately useful in BI — the sin was reparsing them, not having them).
- Employees reads the int columns directly; the regex reparse becomes a
  legacy-only fallback for stores parquet that predates the columns.
- `transfers.py` / `employee_store_assignments.py` keep grouping on the
  `StoreRegion` **string** (a plain group key, not a reparse — switching to the
  int could reorder `np.unique` and perturb RNG draw order, so leave it).

### Output-contract impact

Additive: stores gains two columns → **only the `stores` fingerprint
regenerates**. Employees output is byte-identical (reading int `12` ==
reparsing `"District 12"` → `12`). Warehouses/ESA untouched.

---

## Item 3 — Generate the cluster as one in-memory unit  ❌ CONSIDERED & DECLINED

**Priority: medium. Internal refactor, no schema change.**

### Decision

Declined. The two concrete pains this targeted are already gone: the hierarchy
reparse-on-reread was killed by Item 2 (employees reads clean int columns), and
the weak cross-seam version signatures were replaced by content hashes in Phase 8.
What remains — writing `stores.parquet` and re-reading it in the employees and
bridge steps — is a *feature*, not a smell: it lets `--regen-dimensions employees`
rebuild only employees without touching stores, it leaves valid inspectable
parquet if a run crashes partway, and the re-read costs nothing (these are tiny
tables). The Phase 6 warehouse-folding precedent doesn't extend: warehouses is
tiny and 1:1-derived with no independent config, whereas employees is a large,
independently-configured dimension with its own seed, HR config, and temporal
bridge that genuinely benefits from independent regeneration. A full merge is a
sizable orchestration refactor that changes user-facing regen granularity and
risks byte-identity across all four outputs, for negligible gain.

### (Original) Problem

Stores → warehouses → employees → bridge are so coupled that the refactor spent
whole phases on the seams: cross-dimension parquet rewrites, weak version
signatures (now content hashes, Phase 8), the co-production of warehouses
(Phase 6 folded warehouses *into* the stores flow), and employees re-reading
`stores.parquet` to reconstruct hierarchy on the way back in.

They are really **one unit pretending to be four files.**

### Proposed v2

Generate the whole cluster in one pass holding shared in-memory state (hierarchy
ints, lifecycle, manager identities), and split into four parquet files only at
the write step. Version the cluster as one unit. This removes the round-trip and
the need for content-hash signatures to detect drift across the seam. Item 2's
structured columns are a prerequisite step toward this (no reparse on re-read).

---

## Item 4 — `Status` as-of-end honesty  ✅ RESOLVED (documented, not renamed)

**Priority: low (honesty fix).**

### Problem

`Status` is a denormalized snapshot **as of dataset-end**. It is wrong for any
point-in-time query — the lifecycle **dates** are the truth (the Phase 4.2
doctrine: "dates are truth, Status is an as-of snapshot"). This mismatch caused
the renovation temp-store bug. BI consumers legitimately want a status column, so
we keep it — but nobody should trust it for historical logic.

### Decision

The original proposal was to **rename** `Status` → `StatusAsOfEnd` (stores +
employees). Mapping the footprint showed the cost outweighs the cosmetic gain:
`Status` is load-bearing *internal* working state in the stores lifecycle /
renovation code, is read cross-table by the bridge, and — critically — is queried
by name in ~4 hand-written committed SQL verification scripts
(`scripts/sql/verification/*.sql`, `bootstrap/verification/25_stores.sql`) plus
`scripts/verify_employee_store_sales.py`. ~13 files for a purely cosmetic change,
and `Status` is the conventional column name every retail schema uses —
`StatusAsOfEnd` is honest but unconventional and verbose.

**Resolution: keep the conventional `Status` name; document the as-of-end
semantics at the schema source of truth.** `static_schemas.py` now carries a
comment on both the Stores and Employees `Status` columns stating it is an
as-of-dataset-end snapshot and directing point-in-time queries to the lifecycle
date columns (the source of truth). Near-zero churn, no SQL rewrites, convention
preserved. The forcing-function a rename would give up is small: point-in-time
querying is an advanced use, and the date columns already carry the truth.

---

## Recommended implementation sequence

| # | Item | Output impact | Risk | When |
|---|---|---|---|---|
| 1 | **Item 2** structured District/Region IDs | additive (stores only) | low | ✅ done |
| 2 | **Item 4** `Status` honesty | none (doc only) | none | ✅ done — documented, not renamed |
| 3 | **Item 3** one-unit generation | none (internal) | medium | ❌ considered & declined |
| 4 | **Item 1** opaque keys (employee keys) | value change (contract) | high | ✅ done — store keys deferred |

Rationale: do the additive/contained seam-kill first to build confidence with the
snapshot tooling; batch the schema-breaking-but-mechanical rename next; reserve
the key change (the real output-contract break with sales/SQL/PBIP ripple) for a
dedicated, signed-off session; the one-unit refactor is orthogonal and can land
whenever.

### What we keep verbatim

Determinism/idempotency as a first-class invariant, the byte-identity gates, the
star schema, and the temporal bridge (transfers/renovation/coverage) — that
complexity is *earned*; it does real per-day salesperson attribution. The one
bridge tweak worth considering is modelling transfers as a derived-from event log
rather than in-place mutation, for testability — but that is not on this list.
