"""Single-source guardrails for the shared sales arithmetic.

These pin the consolidation of previously-duplicated helpers so a later edit
cannot silently reintroduce a divergent clone. Each check scans the source of
``src/facts/sales`` (skipping comments) and asserts a pattern lives in exactly
one module — the intended home — or has been removed everywhere.

Complements the golden snapshot (which pins *bytes*): a reintroduced clone with
identical behavior would pass the golden gate but regress the single-source
invariant these tests exist to hold.
"""
from __future__ import annotations

import re
from pathlib import Path

_SALES_ROOT = Path(__file__).resolve().parents[1] / "src" / "facts" / "sales"


def _iter_sales_sources():
    """Yield (relative_path, code_lines) for every .py under facts/sales.

    ``code_lines`` drops full-line comments so a pattern named only in an
    explanatory comment does not count as a live occurrence.
    """
    for path in sorted(_SALES_ROOT.rglob("*.py")):
        lines = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.lstrip()
            if stripped.startswith("#"):
                continue
            lines.append(raw)
        yield path.relative_to(_SALES_ROOT).as_posix(), "\n".join(lines)


def _files_matching(pattern: str) -> set[str]:
    rx = re.compile(pattern, re.MULTILINE)
    return {rel for rel, code in _iter_sales_sources() if rx.search(code)}


def test_cdf_clamp_is_single_source():
    """The gotcha #16 ``cdf[-1] = 1.0`` clamp lives only in weights.py."""
    # Assignment form (allow an optional leading underscore for the _cdf locals
    # the old inline sites used); the docstring/comment mentions are stripped.
    hits = _files_matching(r"^\s*_?cdf\[-1\]\s*=\s*1\.0")
    assert hits == {"sales_logic/weights.py"}, (
        f"cdf[-1]=1.0 clamp must be single-source in weights.py; found in {hits}"
    )


def test_normalize_prob_clone_is_gone():
    """The two divergent _normalize_prob clones are unified into weights."""
    assert _files_matching(r"def _normalize_prob\b") == set()


def test_eligibility_predicate_is_single_source():
    """The eligibility predicate lives only in eligibility.py."""
    hits = _files_matching(r"\(end_month_norm < 0\)\s*\|\s*\(m <= end_month_norm\)")
    assert hits == {"sales_logic/eligibility.py"}, (
        f"eligibility predicate must be single-source; found in {hits}"
    )


def test_chunk_month_band_is_single_source():
    """The order/line band-slicing definition lives only in plan_math.py."""
    assert _files_matching(r"def chunk_month_band\b") == {"sales_logic/plan_math.py"}
    # The old private name must not be redefined anywhere.
    assert _files_matching(r"def _chunk_month_band\b") == set()


def test_worker_prebuild_fallbacks_are_gone():
    """The worker no longer rebuilds brand/assortment structures or reads the
    legacy jagged shared-memory keys — it resolves the coordinator's prebuild."""
    assert _files_matching(r"_prebuilt_brand_to_row_idx") == set()
    assert _files_matching(r"_prebuilt_store_to_product_rows") == set()
    assert _files_matching(r"def _build_store_assortment\b") == set()
