"""optimize_parquet must reuse the merge's dictionary-encode exclude policy.

The merge deliberately excludes the DICT_EXCLUDE columns (high-cardinality keys
like OrderNumber/CustomerKey) from dictionary encoding. optimize_parquet computed
its own string/binary column list that ignored DICT_EXCLUDE, so a re-optimized
file dictionary-encoded exactly the columns the merge excluded. This pins that
optimize_parquet now honors the same exclude set.
"""
from __future__ import annotations

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


def test_optimize_parquet_honors_dict_exclude(tmp_path):
    from src.facts.sales.sales_writer.parquet_merge import optimize_parquet
    from src.facts.sales.sales_writer.encoding import DICT_EXCLUDE

    excl = sorted(DICT_EXCLUDE)[0]
    p = tmp_path / "f.parquet"
    # Give the excluded name a *string* dtype so dictionary encoding would apply
    # unless explicitly excluded, plus a non-excluded string col as the control.
    pq.write_table(
        pa.table({
            "OrderNumber": pa.array([3, 1, 2], pa.int64()),
            excl: pa.array(["a", "b", "c"]),
            "ControlStr": pa.array(["x", "y", "z"]),
        }),
        p,
    )

    out = optimize_parquet(str(p), sort_keys=[("OrderNumber", "ascending")])
    assert out is not None, "optimize_parquet unexpectedly skipped the file"

    rg = pq.ParquetFile(str(p)).metadata.row_group(0)
    enc = {rg.column(i).path_in_schema: rg.column(i).encodings for i in range(rg.num_columns)}

    assert not any("DICTIONARY" in str(e) for e in enc[excl]), (
        f"{excl!r} is in DICT_EXCLUDE but optimize_parquet dictionary-encoded it "
        f"(encodings={enc[excl]})"
    )
    # A non-excluded string column is still dictionary-encoded — the policy is
    # only trimmed by DICT_EXCLUDE, not disabled.
    assert any("DICTIONARY" in str(e) for e in enc["ControlStr"]), (
        f"optimize_parquet stopped dictionary-encoding non-excluded string columns "
        f"(encodings={enc['ControlStr']})"
    )
