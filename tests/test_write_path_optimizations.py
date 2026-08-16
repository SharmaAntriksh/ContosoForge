"""Regression tests for the write-path fast paths.

These tests pin the optimizations that remove redundant full-table passes while
preserving the existing output schemas and layouts.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import importlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


def _employee_table(*, timestamp_dates: bool = False) -> pa.Table:
    date_type = pa.timestamp("ns") if timestamp_dates else pa.date32()
    date_value = (
        dt.datetime(2020, 1, 2)
        if timestamp_dates
        else dt.date(2020, 1, 2)
    )
    return pa.table(
        {
            "EmployeeKey": pa.array([1, 2], type=pa.int32()),
            "ParentEmployeeKey": pa.array([1.0, None], type=pa.float64()),
            "HireDate": pa.array([date_value, date_value], type=date_type),
            "IsActive": pa.array([True, False], type=pa.bool_()),
        }
    )


class TestArrowDimensionPackaging:
    def test_canonical_parquet_is_copied_byte_for_byte(self, tmp_path):
        from src.utils.output_utils import _write_one_dim_parquet

        src = tmp_path / "employees.parquet"
        dest = tmp_path / "packaged.parquet"
        pq.write_table(_employee_table(), src, compression="snappy")

        copied = _write_one_dim_parquet(
            src,
            dest,
            compression="snappy",
            compression_level=None,
            force_date32=True,
        )

        assert copied is True
        assert dest.read_bytes() == src.read_bytes()

    def test_parquet_fallback_streams_and_casts_date32(self, tmp_path, monkeypatch):
        from src.utils.output_utils import _write_one_dim_parquet

        src = tmp_path / "employees.parquet"
        dest = tmp_path / "packaged.parquet"
        pq.write_table(_employee_table(timestamp_dates=True), src, compression="snappy")

        def pandas_read_must_not_run(*args, **kwargs):
            raise AssertionError("dimension packaging materialised through Pandas")

        monkeypatch.setattr(pd, "read_parquet", pandas_read_must_not_run)
        copied = _write_one_dim_parquet(
            src,
            dest,
            compression="zstd",
            compression_level=None,
            force_date32=True,
        )

        assert copied is False
        assert pa.types.is_date32(pq.read_schema(dest).field("HireDate").type)
        assert pq.read_table(dest).num_rows == 2

    def test_csv_stream_normalizes_bool_integer_float_and_date(self, tmp_path, monkeypatch):
        from src.utils.output_utils import _write_one_dim_csv

        src = tmp_path / "employees.parquet"
        dest = tmp_path / "employees.csv"
        pq.write_table(_employee_table(timestamp_dates=True), src, compression="snappy")

        def pandas_read_must_not_run(*args, **kwargs):
            raise AssertionError("dimension packaging materialised through Pandas")

        monkeypatch.setattr(pd, "read_parquet", pandas_read_must_not_run)
        _write_one_dim_csv(src, dest, force_date32=True)

        raw = dest.read_bytes().lower()
        assert b"true" not in raw and b"false" not in raw
        assert b"1.0" not in raw
        assert b"2020-01-02" in raw
        assert raw.count(b"\n") == 3  # header + 2 rows


class TestInventoryWriteOptimizations:
    def test_csv_worker_does_not_write_parquet_twin(self, tmp_path, monkeypatch):
        from src.facts.inventory.engine import InventoryConfig
        from src.facts.inventory import worker

        snapshots = pd.DataFrame(
            {
                "ProductKey": [1],
                "WarehouseKey": [2],
                "SnapshotDate": pd.to_datetime(["2024-01-31"]),
                "QuantityOnHand": [10],
                "QuantityOnOrder": [2],
                "QuantitySold": [3],
                "QuantityReceived": [4],
                "ShrinkageQuantity": [0],
                "ReorderFlag": [0],
                "StockoutFlag": [0],
                "DaysOutOfStock": [0],
            }
        )
        monkeypatch.setattr(worker, "compute_inventory_snapshots", lambda **kwargs: snapshots)

        demand_arrays = {
            "ProductKey": np.array([1]),
            "WarehouseKey": np.array([2]),
            "Year": np.array([2024]),
            "Month": np.array([1]),
            "QuantitySold": np.array([3]),
        }
        output_base = str(tmp_path / "inventory_chunk_00000")
        result = worker._inventory_worker_task(
            (
                0,
                demand_arrays,
                str(tmp_path),
                dataclasses.asdict(InventoryConfig()),
                output_base,
                "csv",
                None,
            )
        )

        assert result["rows"] == 1
        assert (tmp_path / "inventory_chunk_00000.csv").is_file()
        assert not (tmp_path / "inventory_chunk_00000.parquet").exists()

    def test_csv_worker_splits_at_configured_row_ceiling(self, tmp_path, monkeypatch):
        from src.facts.inventory.engine import InventoryConfig
        from src.facts.inventory import worker

        snapshots = pd.DataFrame(
            {
                "ProductKey": [1, 2, 3],
                "WarehouseKey": [2, 2, 2],
                "SnapshotDate": pd.to_datetime(["2024-01-31"] * 3),
                "QuantityOnHand": [10, 11, 12],
                "QuantityOnOrder": [2, 2, 2],
                "QuantitySold": [3, 3, 3],
                "QuantityReceived": [4, 4, 4],
                "ShrinkageQuantity": [0, 0, 0],
                "ReorderFlag": [0, 0, 0],
                "StockoutFlag": [0, 0, 0],
                "DaysOutOfStock": [0, 0, 0],
            }
        )
        monkeypatch.setattr(worker, "compute_inventory_snapshots", lambda **kwargs: snapshots)

        result = worker._inventory_worker_task(
            (
                0,
                {
                    "ProductKey": np.array([1]),
                    "WarehouseKey": np.array([2]),
                    "Year": np.array([2024]),
                    "Month": np.array([1]),
                    "QuantitySold": np.array([3]),
                },
                str(tmp_path),
                dataclasses.asdict(InventoryConfig(write_chunk_rows=2)),
                str(tmp_path / "inventory_chunk_00000"),
                "csv",
                None,
            )
        )

        paths = [Path(path) for path in result["files"]]
        assert [path.name for path in paths] == [
            "inventory_chunk_00000_part00000.csv",
            "inventory_chunk_00000_part00001.csv",
        ]
        assert [len(pd.read_csv(path)) for path in paths] == [2, 1]

    def test_current_run_inventory_artifacts_ignore_stale_merged_csv(
        self, tmp_path, monkeypatch
    ):
        from types import SimpleNamespace
        from src.engine.packaging.package_output import package_output

        # Exercise the source-selection contract through a small package run by
        # replacing the unrelated dimension/SQL work with no-ops.
        fact_out = tmp_path / "facts_scratch"
        inv_out = fact_out / "inventory"
        inv_out.mkdir(parents=True)
        stale = inv_out / "inventory_snapshot.csv"
        fresh = inv_out / "inventory_chunk_00000.csv"
        stale.write_text("ProductKey\n999\n", encoding="utf-8")
        fresh.write_text("ProductKey\n1\n", encoding="utf-8")

        cfg = SimpleNamespace(
            defaults=SimpleNamespace(final_output=str(tmp_path / "generated")),
            packaging=SimpleNamespace(
                clean_scratch_fact_out=False,
                dim_parquet_compression="snappy",
                dim_parquet_compression_level=None,
                dim_force_date32=True,
            ),
            customers=SimpleNamespace(total_customers=0),
            inventory=SimpleNamespace(enabled=True),
            budget=SimpleNamespace(enabled=False),
            complaints=SimpleNamespace(enabled=False),
            wishlists=SimpleNamespace(enabled=False),
            returns=SimpleNamespace(enabled=False),
            subscriptions=SimpleNamespace(enabled=False, generate_bridge=False),
        )
        sales_cfg = SimpleNamespace(
            file_format="csv",
            total_rows=0,
            sales_output="sales",
            skip_order_cols=False,
        )

        # The package function imports SQL helpers into its own module namespace.
        package_output_module = importlib.import_module(
            "src.engine.packaging.package_output"
        )
        for name in (
            "copy_csv_facts",
            "write_create_table_scripts",
            "compose_constraints_sql",
            "compose_postgres_constraints_sql",
            "compose_postgres_views_sql",
            "compose_postgres_indexes_sql",
            "copy_postgres_admin_sql",
            "copy_views_sql",
            "compose_verification_sql",
            "write_bulk_insert_scripts",
            "copy_static_sql_assets",
        ):
            monkeypatch.setattr(package_output_module, name, lambda **kwargs: None)

        (tmp_path / "empty_dims").mkdir()

        final = package_output(
            cfg,
            sales_cfg,
            tmp_path / "empty_dims",
            fact_out,
            inventory_artifacts=[str(fresh)],
        )

        packaged = final / "facts" / "inventory_snapshot"
        assert (packaged / fresh.name).read_text(encoding="utf-8") == "ProductKey\n1\n"
        assert not (packaged / stale.name).exists()


class TestBenchmarkComparison:
    @staticmethod
    def _result(*, rows=20_000_000, config_hash="config", hostname="host"):
        return {
            "results_dir": "benchmark",
            "parameters": {
                "rows": rows,
                "workers": 8,
                "chunk_size": 1_000_000,
                "row_group_size": 1_000_000,
            },
            "environment": {
                "config_sha256": config_hash,
                "models_sha256": "models",
                "hostname": hostname,
                "processor": "cpu",
                "cpu_logical": 16,
                "cpu_physical": 8,
            },
            "runs": [{"format": "parquet", "wall_seconds": 100.0}],
        }

    def test_rejects_mismatched_scale(self):
        from scripts.benchmark_output_modes import _comparison

        current = self._result(rows=20_000_000)
        baseline = self._result(rows=10_000_000)
        comparison = _comparison(current, baseline)

        assert comparison["valid"] is False
        assert comparison["runs"] == []
        assert "parameter rows" in comparison["warning"]

    def test_compares_matching_runs(self):
        from scripts.benchmark_output_modes import _comparison

        current = self._result()
        baseline = self._result()
        current["runs"][0]["wall_seconds"] = 90.0
        comparison = _comparison(current, baseline)

        assert comparison["valid"] is True
        assert comparison["runs"][0]["change_percent"] == pytest.approx(-10.0)

    def test_delta_merge_streams_without_concat_tables(self, tmp_path, monkeypatch):
        pytest.importorskip("deltalake")
        from deltalake import DeltaTable
        from src.facts.inventory import runner

        inv_out = tmp_path / "inventory"
        inv_out.mkdir()
        chunks = []
        for idx in range(2):
            path = inv_out / f"inventory_chunk_{idx:05d}.parquet"
            pq.write_table(
                pa.table(
                    {
                        "ProductKey": pa.array([idx + 1, idx + 2], type=pa.int32()),
                        "WarehouseKey": pa.array([10, 10], type=pa.int32()),
                        "SnapshotDate": pa.array(
                            [dt.date(2024, 1, 31), dt.date(2024, 2, 29)],
                            type=pa.date32(),
                        ),
                        "QuantityOnHand": pa.array([5, 6], type=pa.int32()),
                    }
                ),
                path,
            )
            chunks.append(path)

        def concat_must_not_run(*args, **kwargs):
            raise AssertionError("inventory Delta materialised all chunks")

        monkeypatch.setattr(runner.pa, "concat_tables", concat_must_not_run)
        runner._merge_chunks_to_delta(
            chunks,
            inv_out,
            partition_by=["Year", "Month"],
            delete_chunks=False,
        )

        delta = DeltaTable(str(tmp_path / "inventory_snapshot"))
        assert delta.to_pyarrow_table().num_rows == 4
        assert set(delta.schema().to_arrow().names) >= {"Year", "Month"}


def test_sales_and_detail_worker_csv_chunks_are_final():
    from src.facts.sales.output_assembler import _csv_worker_chunks_are_final
    from src.facts.sales.output_paths import (
        TABLE_SALES,
        TABLE_SALES_ORDER_DETAIL,
        TABLE_SALES_ORDER_HEADER,
        TABLE_SALES_RETURN,
    )

    assert _csv_worker_chunks_are_final(TABLE_SALES)
    assert _csv_worker_chunks_are_final(TABLE_SALES_ORDER_DETAIL)
    assert not _csv_worker_chunks_are_final(TABLE_SALES_ORDER_HEADER)
    assert not _csv_worker_chunks_are_final(TABLE_SALES_RETURN)


class TestScratchArtifactPromotion:
    def test_valid_parquet_is_atomically_promoted(self, tmp_path):
        from src.engine.packaging.artifact_transfer import transfer_file

        src = tmp_path / "scratch" / "sales.parquet"
        dst = tmp_path / "final" / "sales.parquet"
        src.parent.mkdir()
        pq.write_table(pa.table({"SalesKey": [1, 2]}), src)

        assert transfer_file(src, dst, promote=True) is True
        assert not src.exists()
        assert pq.read_table(dst).num_rows == 2

    def test_keep_scratch_forces_copy(self, tmp_path):
        from src.engine.packaging.artifact_transfer import transfer_file

        src = tmp_path / "scratch" / "sales.csv"
        dst = tmp_path / "final" / "sales.csv"
        src.parent.mkdir()
        src.write_text("SalesKey\n1\n", encoding="utf-8")

        assert transfer_file(src, dst, promote=False) is False
        assert src.exists()
        assert dst.read_bytes() == src.read_bytes()

    def test_invalid_parquet_is_not_consumed(self, tmp_path):
        from src.engine.packaging.artifact_transfer import transfer_file

        src = tmp_path / "scratch" / "sales.parquet"
        dst = tmp_path / "final" / "sales.parquet"
        src.parent.mkdir()
        src.write_bytes(b"not parquet")

        assert transfer_file(src, dst, promote=True) is False
        assert src.exists()
        assert dst.read_bytes() == b"not parquet"

    def test_delta_promotion_validates_log_and_drops_internal_scratch(self, tmp_path):
        from src.engine.packaging.artifact_transfer import transfer_delta_dir

        src = tmp_path / "scratch" / "sales"
        dst = tmp_path / "final" / "sales"
        (src / "_delta_log").mkdir(parents=True)
        (src / "_delta_log" / "00000000000000000000.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (src / "part-0.parquet").write_bytes(b"data")
        (src / "_tmp_parts").mkdir()
        (src / "_tmp_parts" / "stale.parquet").write_bytes(b"scratch")

        assert transfer_delta_dir(
            src, dst, promote=True, skip_dirnames={"_tmp_parts"}
        ) is True
        assert not src.exists()
        assert (dst / "_delta_log").is_dir()
        assert not (dst / "_tmp_parts").exists()


def test_shared_fact_csv_writer_stays_arrow_native(tmp_path, monkeypatch):
    from src.facts.shared.writers import write_fact_table

    def pandas_csv_must_not_run(*args, **kwargs):
        raise AssertionError("shared fact CSV materialised through Pandas")

    monkeypatch.setattr(pd.DataFrame, "to_csv", pandas_csv_must_not_run)
    table = pa.table(
        {
            "FactKey": pa.array([1.0, None], type=pa.float64()),
            "IsActive": pa.array([True, False], type=pa.bool_()),
            "Amount": pa.array([1.2, 3.4567894], type=pa.float64()),
        }
    )
    write_fact_table(
        table,
        tmp_path,
        "arrow_fact",
        "csv",
        csv_columns=["FactKey", "IsActive", "Amount"],
        csv_int_columns=["FactKey"],
        csv_fill_null_ints=True,
        csv_float_scale=6,
    )

    raw = (tmp_path / "arrow_fact.csv").read_bytes()
    assert b"\r\n" not in raw
    assert b"1.200000" in raw
    assert b"3.456789" in raw
    assert b"true" not in raw.lower() and b"false" not in raw.lower()
