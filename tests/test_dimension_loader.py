"""Tests for src.engine.dimension_loader."""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest


class TestLoadDimension:
    def test_missing_parquet_returns_none_and_changed(self, tmp_path):
        from src.engine.dimension_loader import load_dimension
        df, changed = load_dimension("nonexistent", tmp_path, {"seed": 42})
        assert df is None
        assert changed is True

    def test_existing_parquet_no_version_returns_changed(self, tmp_path):
        from src.engine.dimension_loader import load_dimension
        df = pd.DataFrame({"A": [1, 2, 3]})
        df.to_parquet(tmp_path / "test_dim.parquet", index=False)

        with patch("src.engine.dimension_loader.load_version", return_value=None):
            result_df, changed = load_dimension("test_dim", tmp_path, {"seed": 42})
        assert result_df is not None
        assert len(result_df) == 3
        assert changed is True

    def test_static_dimension_not_changed(self, tmp_path):
        from src.engine.dimension_loader import load_dimension
        df = pd.DataFrame({"X": [10, 20]})
        df.to_parquet(tmp_path / "static.parquet", index=False)

        with patch("src.engine.dimension_loader.load_version", return_value={"some": "version"}):
            result_df, changed = load_dimension("static", tmp_path, None)
        assert result_df is not None
        assert changed is False

    def test_version_match_not_changed(self, tmp_path):
        from src.engine.dimension_loader import load_dimension
        from src.versioning.version_store import _compute_hash
        cfg = {"seed": 42, "count": 100}
        df = pd.DataFrame({"Y": [1]})
        df.to_parquet(tmp_path / "matched.parquet", index=False)

        # load_version returns the stored metadata envelope, whose config_hash
        # matches the expected config -> not changed.
        envelope = {"config_hash": _compute_hash(cfg), "parquet_mtime": 1.0}
        with patch("src.engine.dimension_loader.load_version", return_value=envelope):
            result_df, changed = load_dimension("matched", tmp_path, cfg)
        assert changed is False

    def test_empty_parquet_returns_empty_df(self, tmp_path):
        from src.engine.dimension_loader import load_dimension
        df = pd.DataFrame({"A": pd.Series([], dtype="int64")})
        df.to_parquet(tmp_path / "empty_dim.parquet", index=False)

        with patch("src.engine.dimension_loader.load_version", return_value=None):
            result_df, changed = load_dimension("empty_dim", tmp_path, {"seed": 42})
        assert result_df is not None
        assert len(result_df) == 0
        assert changed is True

    def test_version_mismatch_changed(self, tmp_path):
        from src.engine.dimension_loader import load_dimension
        from src.versioning.version_store import _compute_hash
        df = pd.DataFrame({"Z": [1]})
        df.to_parquet(tmp_path / "mismatched.parquet", index=False)

        # Stored hash is for a different config -> changed.
        envelope = {"config_hash": _compute_hash({"seed": 1}), "parquet_mtime": 1.0}
        with patch("src.engine.dimension_loader.load_version", return_value=envelope):
            _, changed = load_dimension("mismatched", tmp_path, {"seed": 99})
        assert changed is True
