"""Tests for the dimension version store."""
from __future__ import annotations

import pandas as pd

from src.versioning.version_store import (
    _compute_hash,
    delete_version,
    frame_content_hash,
    load_version,
    save_version,
    should_regenerate,
)


# ===================================================================
# frame_content_hash
# ===================================================================

class TestFrameContentHash:
    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "StoreKey": pd.array([1, 2, 3, 10001], dtype="int32"),
            "EmployeeCount": pd.array([5, 7, None, 1], dtype="Int64"),
            "OpeningDate": pd.to_datetime(
                ["2020-01-01", "2021-06-01", "2019-03-15", "2022-01-01"]
            ),
            "Name": ["a", "b", "c", "d"],
        })

    _COLS = ["StoreKey", "EmployeeCount", "OpeningDate", "Name", "Missing"]

    def test_deterministic(self):
        df = self._frame()
        assert frame_content_hash(df, self._COLS) == frame_content_hash(df.copy(), self._COLS)

    def test_row_order_independent(self):
        df = self._frame()
        shuffled = df.iloc[::-1].reset_index(drop=True)
        assert frame_content_hash(df, self._COLS) == frame_content_hash(shuffled, self._COLS)

    def test_content_sensitive(self):
        df = self._frame()
        changed = df.copy()
        changed.loc[0, "EmployeeCount"] = 6
        assert frame_content_hash(df, self._COLS) != frame_content_hash(changed, self._COLS)

    def test_missing_columns_are_skipped(self):
        df = self._frame()
        # "Missing" absent from the frame must not raise; hash covers present cols.
        h = frame_content_hash(df, self._COLS)
        h_present = frame_content_hash(df, ["StoreKey", "EmployeeCount", "OpeningDate", "Name"])
        assert h == h_present

    def test_empty_frame(self):
        empty = self._frame().iloc[:0]
        assert frame_content_hash(empty, self._COLS).startswith("empty:0:")


# ===================================================================
# _compute_hash
# ===================================================================

class TestComputeHash:
    def test_deterministic(self):
        h1 = _compute_hash({"a": 1, "b": 2})
        h2 = _compute_hash({"a": 1, "b": 2})

        assert h1 == h2

    def test_key_order_independent(self):
        h1 = _compute_hash({"a": 1, "b": 2})
        h2 = _compute_hash({"b": 2, "a": 1})

        assert h1 == h2

    def test_different_values_differ(self):
        h1 = _compute_hash({"a": 1})
        h2 = _compute_hash({"a": 2})

        assert h1 != h2

    def test_nested_dict(self):
        h1 = _compute_hash({"a": {"b": 1}})
        h2 = _compute_hash({"a": {"b": 1}})

        assert h1 == h2

    def test_string_input(self):
        h = _compute_hash("simple string")

        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex

    def test_list_input(self):
        h = _compute_hash([1, 2, 3])

        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex


# ===================================================================
# should_regenerate
# ===================================================================

class TestShouldRegenerate:
    def test_missing_parquet_returns_true(self, tmp_path):
        missing = tmp_path / "nonexistent.parquet"

        assert should_regenerate("test_dim", {"key": "val"}, missing) is True

    def test_missing_version_file_returns_true(self, tmp_path):
        parquet = tmp_path / "test.parquet"
        parquet.write_text("fake parquet data")

        # No version file exists for this dimension
        assert should_regenerate("__test_no_version__", {"key": "val"}, parquet) is True

    def test_changed_config_returns_true(self, tmp_path):
        parquet = tmp_path / "test.parquet"
        parquet.write_text("fake")
        dim_name = "__test_changed_cfg__"
        save_version(dim_name, {"version": 1}, parquet)

        try:
            assert should_regenerate(dim_name, {"version": 2}, parquet) is True
        finally:
            delete_version(dim_name)

    def test_unchanged_config_returns_false(self, tmp_path):
        parquet = tmp_path / "test.parquet"
        parquet.write_text("fake")
        dim_name = "__test_unchanged_cfg__"
        cfg = {"version": 1, "setting": "abc"}
        save_version(dim_name, cfg, parquet)

        try:
            assert should_regenerate(dim_name, cfg, parquet) is False
        finally:
            delete_version(dim_name)


# ===================================================================
# save_version / load_version / delete_version
# ===================================================================

class TestVersionLifecycle:
    def test_save_and_load(self, tmp_path):
        parquet = tmp_path / "dim.parquet"
        parquet.write_text("fake")
        dim_name = "__test_save_load__"
        save_version(dim_name, {"x": 42}, parquet)

        try:
            loaded = load_version(dim_name)

            assert loaded is not None
            assert "config_hash" in loaded
            assert loaded["config_hash"] == _compute_hash({"x": 42})
        finally:
            delete_version(dim_name)

    def test_delete_existing(self, tmp_path):
        parquet = tmp_path / "dim.parquet"
        parquet.write_text("fake")
        dim_name = "__test_delete__"
        save_version(dim_name, {"x": 1}, parquet)

        assert delete_version(dim_name) is True
        assert load_version(dim_name) is None

    def test_delete_nonexistent(self):
        assert delete_version("__test_nonexistent_dim__") is False

    def test_load_nonexistent(self):
        assert load_version("__test_nonexistent_dim__") is None
