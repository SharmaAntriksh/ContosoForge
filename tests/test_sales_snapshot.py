"""Byte-identity golden checksums for the facts/sales sales-fact output.

Byte-identity guardrail. The existing ``test_sales_determinism_workers.py`` /
``test_sales_determinism_chunksize.py`` tests prove the sales fact is *invariant*
across ``--workers`` and ``chunk_size`` — but only relative to a sibling run, so
they catch plan-shape drift, not *any* byte drift. This module adds the missing
piece: a platform-independent **absolute** fingerprint of the sales fact, pinned
to a recorded baseline, across a fixture matrix that exercises the seams the
refactor will move (returns + header mode, CSV writer, partitioned delta writer,
product/customer SCD2, and the ``skip_order_cols`` schema variant).

Two layers:

1. **Worker invariance** — at a fixed (multi-)chunk size, ``--workers 1`` and
   ``--workers 4`` produce a byte-identical sales fact. This is a genuine
   cross-run guarantee (imap_unordered scheduling must not leak into output).
   Note it does NOT extend across *chunk sizes*: OrderNumber is a surrogate ID
   sharded by chunk index (gotcha #28), so its labels legitimately differ when
   the chunk count changes — chunk_size changes the labels, not the shape.
2. **Golden fingerprint** — the fingerprint of every grid cell (fixture ×
   workers × chunk_size) matches the recorded baseline in
   ``_snapshots/sales_fact_fingerprints.json``. This is the byte-identity gate:
   a behavior-preserving refactor must reproduce every cell exactly, including the
   chunk-size-dependent ID labels.

A behavior-preserving refactor must leave every fingerprint unchanged. Any change
that intentionally alters output regenerates the baseline once via
``REGEN_SALES_SNAPSHOT=1`` with the diff explained in the commit.

The fingerprint mirrors ``test_store_cluster_snapshot._fingerprint``: schema
(name+dtype), row count, and LF-normalized CSV of the key-sorted frame, so it is
stable across Windows/Linux and incidental row order. ``version_store.VERSION_DIR``
is redirected to a temp dir so the suite never touches the developer's real
``data/versioning/`` files.

These runs spawn real multiprocessing pools and generate small datasets, so the
module is on the slower side (roughly a minute); it is a guardrail, not a
unit test.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("yaml")
pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow.parquet")
import pyarrow.parquet as pq  # noqa: E402

from tests import sales_gen  # noqa: E402

_SNAPSHOT_FILE = Path(__file__).parent / "_snapshots" / "sales_fact_fingerprints.json"

# Marker columns that uniquely identify the line-grain sales fact among the
# packaged outputs (dimensions and the order header never carry all three).
_MARKER = {"UnitPrice", "CustomerKey", "ProductKey"}

# Natural per-line key; absent under skip_order_cols, where we fall back to
# sorting by the full column set (the row multiset is chunk-invariant, so a
# whole-row sort is still a stable canonical order).
_SORT_KEYS = ["OrderNumber", "OrderLineNumber"]

# Grid: three cells covering both worker counts and both chunk regimes.
# "big" is a single chunk (no cross-chunk state); "small" forces ~12 chunks.
# (w1_small, w4_small) is the worker-invariance pair — same chunk size, so any
# difference is a real worker-order leak, not the expected ID-label reshuffle.
_BIG = sales_gen.DEFAULT_TOTAL_ROWS      # 12_000 -> 1 chunk
_SMALL = 1_000                           # -> 12 chunks
_GRID = [
    ("w1_big", 1, _BIG),
    ("w1_small", 1, _SMALL),
    ("w4_small", 4, _SMALL),
]
# Cells that must be byte-identical to each other (differ only in worker count).
_WORKER_INVARIANT_PAIR = ("w1_small", "w4_small")


# ---------------------------------------------------------------------------
# Version-store isolation (never touch the dev's real data/versioning)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _isolated_version_dir(path: Path):
    import src.versioning.version_store as vs
    saved = vs.VERSION_DIR
    path.mkdir(parents=True, exist_ok=True)
    vs.VERSION_DIR = path
    try:
        yield
    finally:
        vs.VERSION_DIR = saved


# ---------------------------------------------------------------------------
# Fixture configs. Each returns (mutate_fn, file_format, scd2) where mutate_fn
# patches a base small_config in place. ``scd2`` selects which shared dimension
# set the sales run reads from.
# ---------------------------------------------------------------------------
def _fx_default(cfg):
    pass  # base small_config is already: parquet, sales_output=sales, returns off


def _fx_returns_both_csv(cfg):
    cfg["sales"]["file_format"] = "csv"
    cfg["sales"]["sales_output"] = "both"
    cfg["sales"]["skip_order_cols"] = False  # both mode needs OrderNumber
    cfg["returns"]["enabled"] = True


def _fx_delta_partitioned(cfg):
    cfg["sales"]["file_format"] = "deltaparquet"
    cfg["sales"]["partition_by"] = ["Year", "Month"]


def _fx_scd2(cfg):
    cfg["products"]["scd2"]["enabled"] = True
    cfg["customers"]["scd2"]["enabled"] = True


def _fx_skip_order_cols(cfg):
    # skip_order_cols with returns+complaints enabled exercises gotcha #4's
    # auto-disable path (sales_output=sales AND skip_order_cols=True disables
    # both, since they need OrderNumber to link back).
    cfg["sales"]["skip_order_cols"] = True
    cfg["sales"]["sales_output"] = "sales"
    cfg["returns"]["enabled"] = True
    cfg["complaints"]["enabled"] = True


# name -> (mutate_fn, file_format, needs_scd2_dims)
_FIXTURES = {
    "default": (_fx_default, "parquet", False),
    "returns_both_csv": (_fx_returns_both_csv, "csv", False),
    "delta_partitioned": (_fx_delta_partitioned, "deltaparquet", False),
    "scd2": (_fx_scd2, "parquet", True),
    "skip_order_cols": (_fx_skip_order_cols, "parquet", False),
}


def _build_cfg(name, *, dims_dir, scratch, final, workers, chunk_size):
    """A small_config patched for fixture *name* (used for both the dim build
    and the sales run, so SCD2 toggles stay consistent)."""
    mutate, _fmt, _scd2 = _FIXTURES[name]
    cfg = sales_gen.small_config(
        dims_dir=dims_dir, scratch_dir=scratch, final_dir=final,
        workers=workers, chunk_size=chunk_size,
    )
    # small_config disables the secondary sections wholesale; ensure the keys
    # exist so fixture mutators can flip them on.
    for section in ("returns", "complaints"):
        cfg.setdefault(section, {})
    mutate(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Format-aware sales-fact loader (grounded against a real run of each format)
# ---------------------------------------------------------------------------
def _load_sales_fact(final: Path, scratch: Path, fmt: str) -> pd.DataFrame:
    if fmt == "parquet":
        return _load_parquet(final, scratch)
    if fmt == "csv":
        return _load_csv(final, scratch)
    if fmt in ("deltaparquet", "delta"):
        return _load_delta(final, scratch)
    raise AssertionError(f"unknown format {fmt!r}")


def _load_parquet(*roots: Path) -> pd.DataFrame:
    files = []
    for root in roots:
        for p in Path(root).rglob("*.parquet"):
            if "_delta_log" in p.parts or "_tmp_parts" in p.parts:
                continue
            try:
                cols = set(pq.read_schema(p).names)
            except Exception:
                continue
            if _MARKER <= cols:
                files.append(p)
    assert files, f"no sales parquet under {[str(r) for r in roots]}"
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def _load_csv(*roots: Path) -> pd.DataFrame:
    # Read every column as text (keep_default_na=False) so the fingerprint
    # captures the writer's literal output with no dtype-inference drift, and
    # concatenation across chunk files is chunk-count invariant.
    files = []
    for root in roots:
        for p in Path(root).rglob("*.csv"):
            try:
                head = pd.read_csv(p, nrows=0)
            except Exception:
                continue
            if _MARKER <= set(head.columns):
                files.append(p)
    assert files, f"no sales csv under {[str(r) for r in roots]}"
    frames = [pd.read_csv(f, dtype=str, keep_default_na=False) for f in files]
    return pd.concat(frames, ignore_index=True)


def _load_delta(*roots: Path) -> pd.DataFrame:
    from deltalake import DeltaTable
    for root in roots:
        for log in Path(root).rglob("_delta_log"):
            tbl = log.parent
            try:
                df = DeltaTable(str(tbl)).to_pandas()
            except Exception:
                continue
            if _MARKER <= set(df.columns):
                return df
    raise AssertionError(f"no sales delta table under {[str(r) for r in roots]}")


# ---------------------------------------------------------------------------
# Platform-independent fingerprint
# ---------------------------------------------------------------------------
def _fingerprint(df: pd.DataFrame) -> str:
    d = df.copy()
    present = [k for k in _SORT_KEYS if k in d.columns]
    sort_by = present if present else list(d.columns)
    d = d.sort_values(sort_by, kind="stable").reset_index(drop=True)
    payload = {
        "schema": [(str(c), str(d[c].dtype)) for c in d.columns],
        "rows": int(len(d)),
        # lineterminator="\n" pins LF so a baseline recorded on Windows still
        # matches on Linux CI (to_csv defaults to CRLF on Windows).
        "csv": d.to_csv(index=False, lineterminator="\n"),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Build the whole matrix once per session.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    """Return ``{fixture_name: {grid_label: fingerprint}}`` for all fixtures."""
    base = tmp_path_factory.mktemp("sales_snapshot")
    with _isolated_version_dir(base / "versioning"):
        # Two shared dimension sets: SCD2 off and SCD2 on.
        dims = {}
        for scd2 in (False, True):
            ddir = base / ("dims_scd2" if scd2 else "dims_plain")
            ddir.mkdir()
            # Any fixture with this scd2 flag builds identical dims; pick one.
            fx = "scd2" if scd2 else "default"
            dcfg = _build_cfg(
                fx, dims_dir=ddir, scratch=base / f"_ds_{scd2}",
                final=base / f"_df_{scd2}", workers=1, chunk_size=_BIG,
            )
            dimrun = base / f"_dimrun_{scd2}"
            dimrun.mkdir(parents=True, exist_ok=True)
            sales_gen.run_pipeline_stage(dimrun, dcfg, only="dimensions")
            dims[scd2] = ddir

        results: dict[str, dict[str, str]] = {}
        for name, (_mutate, fmt, needs_scd2) in _FIXTURES.items():
            ddir = dims[needs_scd2]
            per_grid: dict[str, str] = {}
            for label, workers, chunk in _GRID:
                run = base / f"{name}__{label}"
                scratch, final = run / "s", run / "f"
                scratch.mkdir(parents=True)
                final.mkdir(parents=True)
                cfg = _build_cfg(
                    name, dims_dir=ddir, scratch=scratch, final=final,
                    workers=workers, chunk_size=chunk,
                )
                sales_gen.run_pipeline_stage(run, cfg, only="sales")
                df = _load_sales_fact(final, scratch, fmt)
                per_grid[label] = _fingerprint(df)
            results[name] = per_grid
    return results


def _flatten(matrix) -> dict[str, str]:
    """``{"fixture/cell": digest}`` over every grid cell."""
    return {
        f"{name}/{label}": grid[label]
        for name, grid in matrix.items()
        for label in grid
    }


# ---------------------------------------------------------------------------
# Layer 1: worker invariance (byte-identical output regardless of --workers)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", list(_FIXTURES))
def test_fingerprint_invariant_across_worker_count(matrix, name):
    a, b = _WORKER_INVARIANT_PAIR
    grid = matrix[name]
    assert grid[a] == grid[b], (
        f"fixture {name!r}: sales fact depends on worker count at fixed "
        f"chunk_size ({a}={grid[a][:12]} vs {b}={grid[b][:12]}). Worker count is "
        "a pure performance knob; imap_unordered scheduling must not affect output."
    )


# ---------------------------------------------------------------------------
# Layer 2: golden fingerprint vs recorded baseline (every grid cell pinned)
# ---------------------------------------------------------------------------
def test_golden_fingerprint(matrix):
    """Every grid cell's fingerprint matches the recorded baseline.

    First run (or ``REGEN_SALES_SNAPSHOT=1``) records the baseline and skips;
    thereafter it asserts equality per cell. A behavior-preserving refactor must
    leave all cells unchanged; an output-altering change regenerates the baseline.
    """
    current = _flatten(matrix)
    regen = os.environ.get("REGEN_SALES_SNAPSHOT") == "1"

    if regen or not _SNAPSHOT_FILE.exists():
        _SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT_FILE.write_text(json.dumps(current, indent=2, sort_keys=True))
        pytest.skip(
            f"Sales-fact fingerprint baseline written to {_SNAPSHOT_FILE.name} "
            f"({len(current)} cells); re-run to compare."
        )

    baseline = json.loads(_SNAPSHOT_FILE.read_text())
    mismatches = sorted(
        cell for cell in current if baseline.get(cell) != current.get(cell)
    )
    missing = sorted(cell for cell in baseline if cell not in current)
    assert not mismatches and not missing, (
        f"Sales fact changed vs golden baseline. Mismatched cells: {mismatches}. "
        f"Cells missing from this run: {missing}. If intended (an output-altering "
        "change), rerun with REGEN_SALES_SNAPSHOT=1 to regenerate the baseline."
    )
