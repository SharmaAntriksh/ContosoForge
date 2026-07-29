"""Scalability and display contracts for the web dataset preview."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import time

import pandas as pd
import pytest

from web.routes import data_routes


def _table(path: str, fmt: str) -> dict:
    return {
        "name": "sample",
        "category": "facts",
        "format": fmt,
        "path": path,
    }


def test_preview_display_formatting_is_stable_and_json_safe():
    assert data_routes._display_value("UnitPrice", 0.1 + 0.2) == "0.30"
    assert data_routes._display_value("DemandRatio", 0.1 + 0.2) == "0.3"
    assert (
        data_routes._display_value("OrderNumber", 9_007_199_254_740_993)
        == "9007199254740993"
    )
    assert data_routes._display_value("IsActive", True) == "true"
    assert data_routes._display_value("Missing", None) is None
    assert data_routes._display_value("OrderDate", date(2026, 7, 29)) == "2026-07-29"
    assert data_routes._display_value("NetAmount", Decimal("12.345")) == "12.34"


def test_parquet_preview_slices_a_million_row_group_before_python_conversion(
    tmp_path: Path,
):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    dataset = tmp_path / "dataset"
    facts = dataset / "facts"
    facts.mkdir(parents=True)
    parquet_path = facts / "sample.parquet"
    total = 1_000_000
    pq.write_table(
        pa.table({"RowNumber": pa.array(range(total), type=pa.int64())}),
        parquet_path,
        row_group_size=total,
    )

    first_started = time.perf_counter()
    first = data_routes._read_preview(
        dataset,
        _table("facts/sample.parquet", "parquet"),
        0,
        100,
    )
    first_elapsed = time.perf_counter() - first_started
    last_started = time.perf_counter()
    last = data_routes._read_preview(
        dataset,
        _table("facts/sample.parquet", "parquet"),
        total - 100,
        100,
    )
    last_elapsed = time.perf_counter() - last_started

    assert first["total_rows"] == last["total_rows"] == total
    assert first["rows"][0] == ["0"]
    assert len(last["rows"]) == 100
    assert last["rows"][0] == [str(total - 100)]
    assert last["rows"][-1] == [str(total - 1)]
    assert first_elapsed < 10
    assert last_elapsed < 10
    print(
        "preview-benchmark parquet "
        f"rows={total} first={first_elapsed:.4f}s last={last_elapsed:.4f}s",
    )


def test_csv_first_and_last_pages_match(tmp_path: Path):
    dataset = tmp_path / "dataset"
    facts = dataset / "facts"
    facts.mkdir(parents=True)
    csv_path = facts / "sample.csv"
    total = 20_000
    pd.DataFrame(
        {
            "RowNumber": range(total),
            "NetAmount": [0.1 + 0.2] * total,
            "IsActive": [True, False] * (total // 2),
        },
    ).to_csv(csv_path, index=False)
    table = _table("facts/sample.csv", "csv")

    first_started = time.perf_counter()
    first = data_routes._read_preview(dataset, table, 0, 100)
    first_elapsed = time.perf_counter() - first_started
    last_started = time.perf_counter()
    last = data_routes._read_preview(dataset, table, total - 100, 100)
    last_elapsed = time.perf_counter() - last_started

    assert first["rows"][0] == ["0", "0.30", "true"]
    assert last["rows"][-1] == [str(total - 1), "0.30", "false"]
    assert first["total_rows"] == last["total_rows"] == total
    assert first_elapsed < 10
    assert last_elapsed < 10
    print(
        "preview-benchmark csv "
        f"rows={total} first={first_elapsed:.4f}s last={last_elapsed:.4f}s",
    )


def test_delta_preview_skips_complete_fragments(tmp_path: Path):
    pa = pytest.importorskip("pyarrow")
    deltalake = pytest.importorskip("deltalake")
    dataset = tmp_path / "dataset"
    delta_path = dataset / "facts" / "sample"
    delta_path.parent.mkdir(parents=True)
    deltalake.write_deltalake(
        delta_path,
        pa.table({"RowNumber": pa.array(range(1_000), type=pa.int64())}),
        mode="overwrite",
    )
    deltalake.write_deltalake(
        delta_path,
        pa.table({"RowNumber": pa.array(range(1_000, 2_000), type=pa.int64())}),
        mode="append",
    )

    first_started = time.perf_counter()
    first = data_routes._read_preview(
        dataset,
        _table("facts/sample", "delta"),
        0,
        100,
    )
    first_elapsed = time.perf_counter() - first_started
    last_started = time.perf_counter()
    last = data_routes._read_preview(
        dataset,
        _table("facts/sample", "delta"),
        1_900,
        100,
    )
    last_elapsed = time.perf_counter() - last_started

    assert first["total_rows"] == last["total_rows"] == 2_000
    assert len(first["rows"]) == len(last["rows"]) == 100
    assert first_elapsed < 10
    assert last_elapsed < 10
    print(
        "preview-benchmark delta "
        f"rows=2000 first={first_elapsed:.4f}s last={last_elapsed:.4f}s",
    )


def test_delta_preview_includes_virtual_partition_columns(tmp_path: Path):
    pa = pytest.importorskip("pyarrow")
    deltalake = pytest.importorskip("deltalake")
    dataset = tmp_path / "dataset"
    delta_path = dataset / "facts" / "sample"
    delta_path.parent.mkdir(parents=True)
    deltalake.write_deltalake(
        delta_path,
        pa.table(
            {
                "RowNumber": pa.array(range(10), type=pa.int64()),
                "Year": pa.array([2024] * 10, type=pa.int16()),
                "Month": pa.array([1] * 5 + [2] * 5, type=pa.int16()),
            }
        ),
        mode="overwrite",
        partition_by=["Year", "Month"],
    )

    preview = data_routes._read_preview(
        dataset,
        _table("facts/sample", "delta"),
        0,
        10,
    )

    assert preview["columns"] == ["RowNumber", "Year", "Month"]
    assert {row[1] for row in preview["rows"]} == {"2024"}
    assert {row[2] for row in preview["rows"]} == {"1", "2"}


def test_preview_implementation_avoids_full_arrow_to_pandas_conversion():
    source = Path(data_routes.__file__).read_text(encoding="utf-8")

    assert "read_row_group" not in source
    assert ".to_pandas()" not in source
    assert "fragment.to_batches(" in source
    assert "pf.iter_batches(" in source
