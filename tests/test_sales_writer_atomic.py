"""Atomic-write regression tests for the sales writers (parquet merge + CSV re-chunk).

The fact merges wrote directly to the final path (parquet) / deleted source
chunks before the final move (CSV), so a mid-merge crash could ship a
truncated-but-plausible file or lose data outright. These crash-injection tests
pin the fixed behavior: the final path is only ever produced by an atomic
rename, and a failure leaves the destination untouched with no temp residue.
"""
from __future__ import annotations

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


# ---------------------------------------------------------------------------
# Parquet merge must be atomic (tmp + os.replace).
# ---------------------------------------------------------------------------
def _write_chunk(path, a, b):
    pq.write_table(pa.table({"a": a, "b": b}), path)


def test_parquet_merge_crash_leaves_no_final_or_temp(tmp_path, monkeypatch):
    from src.facts.sales.sales_writer import parquet_merge as pm

    c0, c1 = tmp_path / "chunk0.parquet", tmp_path / "chunk1.parquet"
    _write_chunk(c0, [1, 2], ["x", "y"])
    _write_chunk(c1, [3, 4], ["z", "w"])
    final = tmp_path / "merged.parquet"

    orig = pm._write_chunk_row_groups
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:  # let chunk 0 write, blow up on chunk 1
            raise RuntimeError("injected mid-merge crash")
        return orig(*args, **kwargs)

    monkeypatch.setattr(pm, "_write_chunk_row_groups", boom)

    with pytest.raises(RuntimeError, match="injected mid-merge crash"):
        pm._merge_parquet_files_common([c0, c1], final, log=False)

    assert not final.exists(), (
        "a mid-merge crash left a truncated-but-plausible file at the final path"
    )
    assert not (tmp_path / "merged.parquet.tmp").exists(), (
        "the partial temp file was not cleaned up after the crash"
    )


def test_parquet_merge_success_is_atomic_and_complete(tmp_path):
    from src.facts.sales.sales_writer import parquet_merge as pm

    c0, c1 = tmp_path / "chunk0.parquet", tmp_path / "chunk1.parquet"
    _write_chunk(c0, [1, 2], ["x", "y"])
    _write_chunk(c1, [3, 4], ["z", "w"])
    final = tmp_path / "merged.parquet"

    result = pm._merge_parquet_files_common([c0, c1], final, log=False)

    assert result == str(final.resolve()) or result == str(final)
    assert final.exists()
    assert not (tmp_path / "merged.parquet.tmp").exists(), "temp file left behind"
    got = pq.read_table(final).to_pydict()
    assert sorted(got["a"]) == [1, 2, 3, 4], "merged file dropped rows"


# ---------------------------------------------------------------------------
# CSV re-chunk must not delete sources before the move.
# ---------------------------------------------------------------------------
def _write_csv_chunk(path, rows):
    body = b"a,b\n" + b"".join(f"{a},{b}\n".encode() for a, b in rows)
    path.write_bytes(body)


def _data_rows(out_dir, prefix="sales_chunk"):
    rows = []
    for p in sorted(out_dir.glob(f"{prefix}*.csv")):
        lines = p.read_text().splitlines()
        rows.extend(lines[1:])  # drop header
    return rows


def test_csv_rechunk_move_failure_does_not_lose_sources(tmp_path, monkeypatch):
    import pathlib
    from src.exceptions import PackagingError
    from src.facts.sales import output_assembler as oa

    chunks = []
    for i in range(3):
        p = tmp_path / f"sales_chunk{i:04d}.csv"
        _write_csv_chunk(p, [(i * 10, "x"), (i * 10 + 1, "y")])
        chunks.append(p)

    def boom(self, target):  # fail every temp -> out_dir move
        raise OSError("injected move failure")

    monkeypatch.setattr(pathlib.Path, "replace", boom)

    with pytest.raises(PackagingError):
        oa._merge_fact_csv_chunks(chunks, tmp_path, "sales_chunk", chunk_size=2, delete_chunks=True)

    for p in chunks:
        assert p.exists(), (
            "a source chunk was deleted before the merged files were moved into "
            "place — a failed move would lose the fact"
        )
    assert not list(tmp_path.glob(".merge_*")), "temp dir not cleaned up on failure"


def test_csv_rechunk_success_no_duplicate_rows_or_temp(tmp_path):
    from src.facts.sales import output_assembler as oa

    chunks = []
    for i in range(3):
        p = tmp_path / f"sales_chunk{i:04d}.csv"
        _write_csv_chunk(p, [(i * 10, "x"), (i * 10 + 1, "y")])
        chunks.append(p)

    # Large chunk_size -> collapse all 3 into one output file.
    oa._merge_fact_csv_chunks(chunks, tmp_path, "sales_chunk", chunk_size=10_000, delete_chunks=True)

    rows = _data_rows(tmp_path)
    assert sorted(rows) == ["0,x", "1,y", "10,x", "11,y", "20,x", "21,y"], (
        f"re-chunk changed the row set (dup/drop): {sorted(rows)}"
    )
    assert not list(tmp_path.glob(".merge_*")), "temp dir left behind after success"
