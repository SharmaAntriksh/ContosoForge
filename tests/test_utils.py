"""Comprehensive tests for utility modules that previously had no tests.

Covers:
  - config_helpers (bool_or, int_or, float_or, str_or, range2, as_dict)
  - config_precedence (resolve_seed, resolve_dates)
  - shared_arrays (SharedArrayGroup publish/resolve, _is_shareable)
  - name_pools (load_list, normalize_name_ascii, hash_u64, pick_from_pool, etc.)
  - output_utils (format_number_short, _ensure_clean_dir, _excluded_dim_files)
  - logging_utils (smoke tests for log functions, fmt_sec, stage context manager)
  - cli (build_parser, str2bool)
  - exceptions (hierarchy checks)
"""
from __future__ import annotations

import argparse
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ===================================================================
# 1. config_helpers
# ===================================================================

from src.engine.config.config_schema import AppConfig
from src.exceptions import ConfigError
from src.utils.config_helpers import (
    as_dict,
    bool_or,
    float_or,
    int_or,
    parse_global_dates,
    rand_dates_between,
    rand_single_date,
    range2,
    region_from_iso_code,
    str_or,
)


class TestAsDict:
    def test_returns_dict_unchanged(self):
        d = {"a": 1}
        assert as_dict(d) is d

    def test_returns_empty_for_none(self):
        assert as_dict(None) == {}

    def test_returns_empty_for_non_dict(self):
        assert as_dict("hello") == {}
        assert as_dict(42) == {}
        assert as_dict([1, 2]) == {}


class TestBoolOr:
    # --- True cases ---
    def test_true_bool(self):
        assert bool_or(True, False) is True

    def test_false_bool(self):
        assert bool_or(False, True) is False

    @pytest.mark.parametrize("val", ["true", "True", "TRUE", "t", "T"])
    def test_truthy_strings(self, val):
        assert bool_or(val, False) is True

    @pytest.mark.parametrize("val", ["yes", "y", "on", "1"])
    def test_truthy_strings_extra(self, val):
        assert bool_or(val, False) is True

    @pytest.mark.parametrize("val", ["false", "False", "FALSE", "f", "F"])
    def test_falsy_strings(self, val):
        assert bool_or(val, True) is False

    @pytest.mark.parametrize("val", ["no", "n", "off", "0"])
    def test_falsy_strings_extra(self, val):
        assert bool_or(val, True) is False

    def test_none_returns_default(self):
        assert bool_or(None, True) is True
        assert bool_or(None, False) is False

    def test_int_one(self):
        assert bool_or(1, False) is True

    def test_int_zero(self):
        assert bool_or(0, True) is False

    def test_float_nonzero(self):
        assert bool_or(1.0, False) is True

    def test_float_zero(self):
        assert bool_or(0.0, True) is False

    def test_numpy_int_true(self):
        assert bool_or(np.int64(1), False) is True

    def test_numpy_int_false(self):
        assert bool_or(np.int32(0), True) is False

    def test_unrecognized_string_returns_default(self):
        assert bool_or("maybe", False) is False
        assert bool_or("maybe", True) is True

    def test_whitespace_stripped(self):
        assert bool_or("  true  ", False) is True
        assert bool_or("  false  ", True) is False

    def test_empty_string_returns_default(self):
        # empty string is not in any truthy/falsy set
        assert bool_or("", True) is True


class TestIntOr:
    def test_int_passthrough(self):
        assert int_or(42, 0) == 42

    def test_string_int(self):
        assert int_or("100", 0) == 100

    def test_float_truncated(self):
        assert int_or(3.9, 0) == 3

    def test_none_returns_default(self):
        assert int_or(None, 99) == 99

    def test_empty_string_returns_default(self):
        assert int_or("", 77) == 77

    def test_invalid_string_returns_default(self):
        assert int_or("abc", 10) == 10

    def test_numpy_scalar(self):
        assert int_or(np.int64(5), 0) == 5

    def test_nan_returns_default(self):
        # int(float('nan')) raises ValueError
        assert int_or(float("nan"), -1) == -1


class TestFloatOr:
    def test_float_passthrough(self):
        assert float_or(3.14, 0.0) == 3.14

    def test_string_float(self):
        assert float_or("2.5", 0.0) == 2.5

    def test_int_promoted(self):
        assert float_or(7, 0.0) == 7.0

    def test_none_returns_default(self):
        assert float_or(None, 1.5) == 1.5

    def test_empty_string_returns_default(self):
        assert float_or("", 9.9) == 9.9

    def test_invalid_string_returns_default(self):
        assert float_or("xyz", 0.1) == 0.1

    def test_numpy_float(self):
        assert float_or(np.float32(2.0), 0.0) == pytest.approx(2.0, abs=1e-5)


class TestStrOr:
    def test_string_passthrough(self):
        assert str_or("hello", "x") == "hello"

    def test_none_returns_default(self):
        assert str_or(None, "default") == "default"

    def test_empty_returns_default(self):
        assert str_or("", "fallback") == "fallback"

    def test_whitespace_only_returns_default(self):
        assert str_or("   ", "fb") == "fb"

    def test_int_converted(self):
        assert str_or(42, "x") == "42"

    def test_strips_whitespace(self):
        assert str_or("  abc  ", "") == "abc"


class TestRange2:
    def test_list_input(self):
        assert range2([1.0, 5.0], 0.0, 10.0) == (1.0, 5.0)

    def test_tuple_input(self):
        assert range2((2.0, 8.0), 0.0, 10.0) == (2.0, 8.0)

    def test_swapped_values_corrected(self):
        lo, hi = range2([10.0, 2.0], 0.0, 0.0)
        assert lo <= hi
        assert lo == 2.0
        assert hi == 10.0

    def test_single_element_uses_defaults(self):
        assert range2([5.0], 1.0, 9.0) == (1.0, 9.0)

    def test_none_uses_defaults(self):
        assert range2(None, 3.0, 7.0) == (3.0, 7.0)

    def test_string_uses_defaults(self):
        assert range2("invalid", 1.0, 2.0) == (1.0, 2.0)

    def test_empty_list_uses_defaults(self):
        assert range2([], 4.0, 6.0) == (4.0, 6.0)

    def test_defaults_swapped_if_needed(self):
        # defaults themselves are swapped: hi < lo
        lo, hi = range2(None, 10.0, 2.0)
        assert lo == 2.0
        assert hi == 10.0


class TestParseGlobalDates:
    def test_valid_defaults(self):
        cfg = AppConfig.model_validate({"defaults": {"dates": {"start": "2020-01-01", "end": "2023-12-31"}}})
        gs, ge = parse_global_dates(cfg, AppConfig.model_validate({}))
        assert gs == pd.Timestamp("2020-01-01")
        assert ge == pd.Timestamp("2023-12-31")

    def test_missing_dates_raises(self):
        # Create an AppConfig with empty date strings so parse_global_dates raises
        cfg = AppConfig.model_validate({"defaults": {"dates": {"start": "", "end": ""}}})
        with pytest.raises(ConfigError, match="defaults.dates"):
            parse_global_dates(cfg, AppConfig.model_validate({}))

    def test_swapped_dates_corrected(self):
        cfg = AppConfig.model_validate({"defaults": {"dates": {"start": "2025-01-01", "end": "2020-01-01"}}})
        gs, ge = parse_global_dates(cfg, AppConfig.model_validate({}))
        assert gs < ge

    def test_override_dates_when_allowed(self):
        cfg = AppConfig.model_validate({"defaults": {"dates": {"start": "2020-01-01", "end": "2023-12-31"}}})
        local = AppConfig.model_validate({"override": {"dates": {"start": "2022-06-01", "end": "2022-12-31"}}})
        gs, ge = parse_global_dates(cfg, local, allow_override=True)
        assert gs == pd.Timestamp("2022-06-01")
        assert ge == pd.Timestamp("2022-12-31")

    def test_override_ignored_when_not_allowed(self):
        cfg = AppConfig.model_validate({"defaults": {"dates": {"start": "2020-01-01", "end": "2023-12-31"}}})
        local = AppConfig.model_validate({"override": {"dates": {"start": "2022-06-01", "end": "2022-12-31"}}})
        gs, ge = parse_global_dates(cfg, local, allow_override=False)
        assert gs == pd.Timestamp("2020-01-01")


class TestRegionFromIsoCode:
    def test_usd_returns_us(self):
        assert region_from_iso_code("USD") == "US"

    def test_eur_returns_eu(self):
        assert region_from_iso_code("EUR") == "EU"

    def test_inr_returns_in(self):
        assert region_from_iso_code("INR") == "IN"

    def test_jpy_returns_as(self):
        assert region_from_iso_code("JPY") == "AS"

    def test_unknown_returns_default(self):
        assert region_from_iso_code("XYZ") == "US"
        assert region_from_iso_code("XYZ", default_region="EU") == "EU"

    def test_none_returns_default(self):
        assert region_from_iso_code(None) == "US"

    def test_case_insensitive(self):
        assert region_from_iso_code("usd") == "US"
        assert region_from_iso_code("eur") == "EU"


class TestRandDatesBetween:
    def test_returns_correct_count(self):
        rng = np.random.default_rng(42)
        s = rand_dates_between(rng, pd.Timestamp("2020-01-01"), pd.Timestamp("2020-12-31"), 10)
        assert len(s) == 10

    def test_dates_within_range(self):
        rng = np.random.default_rng(42)
        s = rand_dates_between(rng, pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-31"), 100)
        assert s.min() >= pd.Timestamp("2020-01-01")
        assert s.max() <= pd.Timestamp("2020-01-31")

    def test_swapped_dates_handled(self):
        rng = np.random.default_rng(42)
        s = rand_dates_between(rng, pd.Timestamp("2020-12-31"), pd.Timestamp("2020-01-01"), 5)
        assert len(s) == 5


class TestRandSingleDate:
    def test_returns_timestamp(self):
        rng = np.random.default_rng(42)
        d = rand_single_date(rng, pd.Timestamp("2020-01-01"), pd.Timestamp("2020-12-31"))
        assert isinstance(d, pd.Timestamp)

    def test_within_range(self):
        rng = np.random.default_rng(42)
        for _ in range(50):
            d = rand_single_date(rng, pd.Timestamp("2020-06-01"), pd.Timestamp("2020-06-30"))
            assert pd.Timestamp("2020-06-01") <= d <= pd.Timestamp("2020-06-30")


# ===================================================================
# 2. config_precedence
# ===================================================================

from src.utils.config_precedence import resolve_dates, resolve_seed


class TestResolveSeed:
    def test_override_seed_wins(self):
        cfg = AppConfig.model_validate({"defaults": {"seed": 1}})
        section = AppConfig.model_validate({"seed": 2, "override": {"seed": 3}})
        assert resolve_seed(cfg, section) == 3

    def test_section_seed_second(self):
        cfg = AppConfig.model_validate({"defaults": {"seed": 1}})
        section = AppConfig.model_validate({"seed": 2})
        assert resolve_seed(cfg, section) == 2

    def test_defaults_seed_third(self):
        cfg = AppConfig.model_validate({"defaults": {"seed": 10}})
        assert resolve_seed(cfg, AppConfig.model_validate({})) == 10

    def test_fallback_default(self):
        assert resolve_seed(AppConfig.model_validate({}), AppConfig.model_validate({})) == 42

    def test_custom_fallback(self):
        # defaults.seed=42 always present in AppConfig, so fallback is never reached
        assert resolve_seed(AppConfig.model_validate({}), AppConfig.model_validate({}), fallback=123) == 42

    def test_underscore_defaults(self):
        # _defaults stripped by Pydantic (private field prefix); falls back to defaults.seed=42
        cfg = AppConfig.model_validate({"_defaults": {"seed": 55}})
        assert resolve_seed(cfg, AppConfig.model_validate({})) == 42

    def test_random_mode_returns_int(self):
        cfg = AppConfig.model_validate({"defaults": {"seed": 42, "random": True}})
        seed = resolve_seed(cfg, AppConfig.model_validate({}))
        assert isinstance(seed, int)

    def test_random_mode_varies(self):
        cfg = AppConfig.model_validate({"defaults": {"seed": 42, "random": True}})
        seeds = {resolve_seed(cfg, AppConfig.model_validate({})) for _ in range(20)}
        # With OS entropy, 20 draws should produce at least 2 distinct values
        assert len(seeds) > 1

    def test_random_false_is_deterministic(self):
        cfg = AppConfig.model_validate({"defaults": {"seed": 42, "random": False}})
        section = AppConfig.model_validate({})
        assert resolve_seed(cfg, section) == 42
        assert resolve_seed(cfg, section) == 42

    def test_random_mode_with_dict_cfg(self):
        cfg = {"defaults": {"seed": 42, "random": True}}
        seed = resolve_seed(cfg)
        assert isinstance(seed, int)

    def test_section_cfg_none_falls_to_defaults(self):
        cfg = AppConfig.model_validate({"defaults": {"seed": 99}})
        assert resolve_seed(cfg) == 99

    def test_dict_cfg_works(self):
        cfg = {"defaults": {"seed": 77}}
        section = {"seed": 55}
        assert resolve_seed(cfg, section) == 55


class TestResolveDates:
    def test_resolves_from_defaults(self):
        cfg = AppConfig.model_validate({"defaults": {"dates": {"start": "2020-01-01", "end": "2023-12-31"}}})
        gs, ge = resolve_dates(cfg, AppConfig.model_validate({}))
        assert gs == pd.Timestamp("2020-01-01")
        assert ge == pd.Timestamp("2023-12-31")

    def test_override_used_when_allowed(self):
        cfg = AppConfig.model_validate({"defaults": {"dates": {"start": "2020-01-01", "end": "2023-12-31"}}})
        section = AppConfig.model_validate({"override": {"dates": {"start": "2022-01-01", "end": "2022-06-30"}}})
        gs, ge = resolve_dates(cfg, section, allow_override=True)
        assert gs == pd.Timestamp("2022-01-01")

    def test_raises_when_missing(self):
        cfg = AppConfig.model_validate({"defaults": {"dates": {"start": "", "end": ""}}})
        with pytest.raises(ConfigError):
            resolve_dates(cfg, AppConfig.model_validate({}))


# ===================================================================
# 3. shared_arrays
# ===================================================================

from src.utils.shared_arrays import (
    MIN_SHARE_BYTES,
    SharedArrayGroup,
    _SHM_MARKER,
    _is_shareable,
    resolve_array,
)


class TestIsSharable:
    def test_large_float64_is_shareable(self):
        arr = np.ones(MIN_SHARE_BYTES // 8 + 1, dtype=np.float64)
        assert _is_shareable(arr) is True

    def test_small_array_not_shareable(self):
        arr = np.ones(10, dtype=np.float64)
        assert _is_shareable(arr) is False

    def test_object_dtype_not_shareable(self):
        arr = np.array(["a", "b", "c"], dtype=object)
        assert _is_shareable(arr) is False

    def test_scalar_not_shareable(self):
        assert _is_shareable(np.float64(1.0)) is False

    def test_non_array_not_shareable(self):
        assert _is_shareable([1, 2, 3]) is False
        assert _is_shareable(None) is False


class TestResolveArrayPassthrough:
    """Test resolve_array returns non-descriptor values unchanged."""

    def test_none_passthrough(self):
        assert resolve_array(None) is None

    def test_string_passthrough(self):
        assert resolve_array("hello") == "hello"

    def test_int_passthrough(self):
        assert resolve_array(42) == 42

    def test_dict_without_marker_passthrough(self):
        d = {"name": "test", "shape": [10]}
        assert resolve_array(d) is d

    def test_numpy_array_passthrough(self):
        arr = np.array([1, 2, 3])
        result = resolve_array(arr)
        assert result is arr


class TestSharedArrayGroupPublish:
    def test_none_returns_none(self):
        with SharedArrayGroup() as shm:
            assert shm.publish("test", None) is None

    def test_small_array_passthrough(self):
        arr = np.array([1, 2, 3], dtype=np.int64)
        with SharedArrayGroup() as shm:
            result = shm.publish("test", arr)
            # Small array should be returned as-is (not shared)
            assert isinstance(result, np.ndarray)
            np.testing.assert_array_equal(result, arr)

    def test_object_array_passthrough(self):
        arr = np.array(["a", "b"] * 100000, dtype=object)
        with SharedArrayGroup() as shm:
            result = shm.publish("test", arr)
            assert isinstance(result, np.ndarray)

    def test_large_array_returns_descriptor(self):
        # Create array large enough to be shared
        n = MIN_SHARE_BYTES // 8 + 1
        arr = np.arange(n, dtype=np.float64)
        with SharedArrayGroup() as shm:
            desc = shm.publish("big", arr)
            assert isinstance(desc, dict)
            assert _SHM_MARKER in desc
            assert "name" in desc
            assert "shape" in desc
            assert "dtype" in desc
            assert desc["shape"] == list(arr.shape)
            assert desc["dtype"] == str(arr.dtype)

    def test_publish_and_resolve_roundtrip(self):
        n = MIN_SHARE_BYTES // 8 + 1
        arr = np.arange(n, dtype=np.float64)
        with SharedArrayGroup() as shm:
            desc = shm.publish("roundtrip", arr)
            resolved = resolve_array(desc)
            np.testing.assert_array_equal(resolved, arr)
            # resolved should be read-only
            assert not resolved.flags.writeable

    def test_publish_dict_modifies_in_place(self):
        n = MIN_SHARE_BYTES // 8 + 1
        big = np.arange(n, dtype=np.float64)
        small = np.array([1, 2, 3], dtype=np.int64)
        cfg = {"big": big, "small": small, "other": "hello"}
        with SharedArrayGroup() as shm:
            shm.publish_dict(cfg, ["big", "small", "other", "missing_key"])
            # big should be a descriptor
            assert isinstance(cfg["big"], dict)
            assert _SHM_MARKER in cfg["big"]
            # small should be unchanged (too small)
            assert isinstance(cfg["small"], np.ndarray)
            # other is not an array, unchanged
            assert cfg["other"] == "hello"

    def test_cleanup_clears_blocks(self):
        n = MIN_SHARE_BYTES // 8 + 1
        arr = np.arange(n, dtype=np.float64)
        shm = SharedArrayGroup()
        shm.publish("cleanup_test", arr)
        assert len(shm._blocks) == 1
        shm.cleanup()
        assert len(shm._blocks) == 0

    def test_context_manager_cleanup(self):
        n = MIN_SHARE_BYTES // 8 + 1
        arr = np.arange(n, dtype=np.float64)
        shm = SharedArrayGroup()
        with shm:
            shm.publish("ctx_test", arr)
            assert len(shm._blocks) == 1
        assert len(shm._blocks) == 0


class TestSharedArrayGroupJagged:
    def test_publish_jagged_roundtrip(self):
        arrays = [
            np.array([1, 2, 3], dtype=np.int64),
            None,
            np.array([10, 20], dtype=np.int64),
            np.array([], dtype=np.int64),
            np.array([99], dtype=np.int64),
        ]
        # Need enough total data to exceed MIN_SHARE_BYTES for data array
        # For small test data, the arrays will just be pickled, which is fine
        with SharedArrayGroup() as shm:
            desc = shm.publish_jagged("test_jagged", arrays)
            assert "__jagged__" in desc
            assert desc["length"] == 5

    def test_publish_jagged_all_none(self):
        arrays = [None, None, None]
        with SharedArrayGroup() as shm:
            desc = shm.publish_jagged("all_none", arrays)
            assert desc["length"] == 3


# ===================================================================
# 4. name_pools
# ===================================================================

from src.utils.name_pools import (
    hash_u64,
    load_list,
    middle_initial,
    normalize_name_ascii,
    pick_from_pool,
    pick_masked,
    slugify_domain_label,
)


class TestNormalizeNameAscii:
    def test_basic_name(self):
        assert normalize_name_ascii("john doe") == "John Doe"

    def test_unicode_stripped(self):
        result = normalize_name_ascii("José García")
        assert result == "Jose Garcia"

    def test_empty_string(self):
        assert normalize_name_ascii("") == ""

    def test_none_like(self):
        assert normalize_name_ascii("") == ""

    def test_hyphenated_name(self):
        result = normalize_name_ascii("mary-jane watson")
        assert result == "Mary-Jane Watson"

    def test_apostrophe_removed_by_default(self):
        result = normalize_name_ascii("O'Brien")
        assert "'" not in result

    def test_apostrophe_kept_when_requested(self):
        result = normalize_name_ascii("O'Brien", keep_apostrophe=True)
        assert "'" in result

    def test_extra_whitespace_collapsed(self):
        result = normalize_name_ascii("  john   doe  ")
        assert result == "John Doe"

    def test_special_chars_removed(self):
        result = normalize_name_ascii("John@#$Doe")
        assert result == "Johndoe"

    def test_all_special_chars(self):
        result = normalize_name_ascii("@#$%^&*()")
        assert result == ""


class TestLoadList:
    def test_valid_file(self, tmp_path):
        f = tmp_path / "names.csv"
        f.write_text("Alice\nBob\nCharlie\n", encoding="utf-8")
        # Clear the lru_cache for this test
        load_list.cache_clear()
        arr = load_list(str(f))
        assert len(arr) == 3
        assert "Alice" in arr
        assert "Bob" in arr
        assert "Charlie" in arr

    def test_deduplication(self, tmp_path):
        f = tmp_path / "dupes.csv"
        f.write_text("Alice\nAlice\nBob\n", encoding="utf-8")
        load_list.cache_clear()
        arr = load_list(str(f))
        assert len(arr) == 2

    def test_empty_lines_skipped(self, tmp_path):
        f = tmp_path / "gaps.csv"
        f.write_text("\nAlice\n\n\nBob\n\n", encoding="utf-8")
        load_list.cache_clear()
        arr = load_list(str(f))
        assert len(arr) == 2

    def test_csv_format_keeps_first_col(self, tmp_path):
        f = tmp_path / "csv_names.csv"
        f.write_text("Alice,Jones\nBob,Smith\n", encoding="utf-8")
        load_list.cache_clear()
        arr = load_list(str(f))
        assert "Alice" in arr
        assert "Bob" in arr

    def test_missing_file_raises(self):
        load_list.cache_clear()
        with pytest.raises(FileNotFoundError):
            load_list("/nonexistent/path/names.csv")

    def test_empty_file_raises(self, tmp_path):
        f = tmp_path / "empty.csv"
        f.write_text("", encoding="utf-8")
        load_list.cache_clear()
        with pytest.raises(ValueError, match="empty after normalization"):
            load_list(str(f))

    def test_bom_handled(self, tmp_path):
        f = tmp_path / "bom.csv"
        f.write_bytes(b"\xef\xbb\xbfAlice\nBob\n")
        load_list.cache_clear()
        arr = load_list(str(f))
        assert len(arr) == 2
        assert "Alice" in arr

    def test_no_normalize(self, tmp_path):
        f = tmp_path / "raw.csv"
        f.write_text("ALICE\nbob\n", encoding="utf-8")
        load_list.cache_clear()
        arr = load_list(str(f), normalize=False)
        assert "ALICE" in arr
        assert "bob" in arr


class TestHashU64:
    def test_deterministic(self):
        keys = np.array([1, 2, 3], dtype=np.uint64)
        h1 = hash_u64(keys, 42, 0)
        h2 = hash_u64(keys, 42, 0)
        np.testing.assert_array_equal(h1, h2)

    def test_different_seed_differs(self):
        keys = np.array([1, 2, 3], dtype=np.uint64)
        h1 = hash_u64(keys, 42, 0)
        h2 = hash_u64(keys, 99, 0)
        assert not np.array_equal(h1, h2)

    def test_different_salt_differs(self):
        keys = np.array([1, 2, 3], dtype=np.uint64)
        h1 = hash_u64(keys, 42, 0)
        h2 = hash_u64(keys, 42, 1)
        assert not np.array_equal(h1, h2)

    def test_output_shape(self):
        keys = np.array([10, 20, 30, 40, 50], dtype=np.uint64)
        h = hash_u64(keys, 1, 1)
        assert h.shape == keys.shape


class TestPickFromPool:
    def test_basic_selection(self):
        pool = np.array(["a", "b", "c"], dtype=object)
        h = np.array([0, 1, 2, 3, 100], dtype=np.uint64)
        result = pick_from_pool(pool, h)
        assert len(result) == 5
        for v in result:
            assert v in ["a", "b", "c"]

    def test_empty_pool_raises(self):
        with pytest.raises(ValueError, match="Pool is empty"):
            pick_from_pool(np.array([], dtype=object), np.array([1], dtype=np.uint64))

    def test_deterministic(self):
        pool = np.array(["x", "y", "z"], dtype=object)
        h = np.array([10, 20, 30], dtype=np.uint64)
        r1 = pick_from_pool(pool, h)
        r2 = pick_from_pool(pool, h)
        np.testing.assert_array_equal(r1, r2)


class TestPickMasked:
    def test_masked_selection(self):
        pool = np.array(["a", "b", "c"], dtype=object)
        h = np.array([0, 1, 2, 3, 4], dtype=np.uint64)
        mask = np.array([True, False, True, False, True], dtype=bool)
        result = pick_masked(pool, h, mask)
        assert len(result) == 3

    def test_empty_pool_raises(self):
        with pytest.raises(ValueError, match="Pool is empty"):
            pick_masked(
                np.array([], dtype=object),
                np.array([1], dtype=np.uint64),
                np.array([True], dtype=bool),
            )


class TestMiddleInitial:
    def test_returns_letter_dot(self):
        keys = np.array([1, 2, 3], dtype=np.uint64)
        result = middle_initial(keys, 42)
        assert len(result) == 3
        for v in result:
            assert len(v) == 2
            assert v[0].isalpha()
            assert v[1] == "."

    def test_deterministic(self):
        keys = np.array([100, 200], dtype=np.uint64)
        r1 = middle_initial(keys, 42)
        r2 = middle_initial(keys, 42)
        np.testing.assert_array_equal(r1, r2)


class TestSlugifyDomainLabel:
    def test_basic(self):
        result = slugify_domain_label("Northstar Logistics Ltd")
        assert result == result.lower()
        assert " " not in result

    def test_special_chars_removed(self):
        result = slugify_domain_label("O'Brien & Sons")
        assert "'" not in result
        assert "&" not in result

    def test_truncation_at_63(self):
        long_name = "A" * 200
        result = slugify_domain_label(long_name)
        assert len(result) <= 63


class TestOrgDomainLabel:
    def test_uses_first_word_only(self):
        from src.utils.name_pools import org_domain_label
        # Drops the descriptors + legal suffix, keeps just the brand word.
        assert org_domain_label("Meadowstone Studios Manufacturing LLC") == "meadowstone"
        assert org_domain_label("Northstar Logistics Ltd") == "northstar"

    def test_single_word_unchanged(self):
        from src.utils.name_pools import org_domain_label
        assert org_domain_label("Acme") == "acme"

    def test_much_shorter_than_full_slug(self):
        from src.utils.name_pools import org_domain_label
        name = "Premier Mountainview Studios Enterprises LLC"
        assert len(org_domain_label(name)) < len(slugify_domain_label(name))

    def test_empty_falls_back(self):
        from src.utils.name_pools import org_domain_label
        assert org_domain_label("") == ""


# ===================================================================
# 5. output_utils
# ===================================================================

from src.utils.output_utils import (
    _ensure_clean_dir,
    _excluded_dim_files,
    format_number_short,
)


class TestFormatNumberShort:
    def test_billions(self):
        assert format_number_short(1_000_000_000) == "1B"
        assert format_number_short(2_500_000_000) == "2B"  # truncated

    def test_millions(self):
        assert format_number_short(1_000_000) == "1M"
        assert format_number_short(5_500_000) == "6M"

    def test_thousands(self):
        assert format_number_short(1_000) == "1K"
        assert format_number_short(1_500) == "2K"

    def test_small_numbers(self):
        assert format_number_short(999) == "999"
        assert format_number_short(0) == "0"
        assert format_number_short(1) == "1"


class TestEnsureCleanDir:
    def test_creates_new_dir(self, tmp_path):
        target = tmp_path / "new_dir"
        _ensure_clean_dir(target)
        assert target.exists()
        assert target.is_dir()

    def test_recreates_empty_dir(self, tmp_path):
        target = tmp_path / "empty_dir"
        target.mkdir()
        _ensure_clean_dir(target)
        assert target.exists()

    def test_refuses_nonempty_dir(self, tmp_path):
        target = tmp_path / "nonempty"
        target.mkdir()
        (target / "file.txt").write_text("content")
        with pytest.raises(FileExistsError, match="refusing to delete"):
            _ensure_clean_dir(target)

    def test_creates_nested_parents(self, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        _ensure_clean_dir(target)
        assert target.exists()


class TestExcludedDimFiles:
    def test_empty_config(self):
        excluded = _excluded_dim_files(AppConfig.model_validate({}))
        # With no returns config, return_reason should be excluded
        assert "return_reason.parquet" in excluded

    def test_subscriptions_disabled(self):
        cfg = AppConfig.model_validate({"subscriptions": {"enabled": False}})
        excluded = _excluded_dim_files(cfg)
        assert "plans.parquet" in excluded
        assert "customer_subscriptions.parquet" in excluded

    def test_returns_enabled(self):
        cfg = AppConfig.model_validate({"returns": {"enabled": True}, "sales": {"skip_order_cols": False}})
        excluded = _excluded_dim_files(cfg)
        assert "return_reason.parquet" not in excluded

    def test_returns_enabled_but_skip_order(self):
        cfg = AppConfig.model_validate({
            "returns": {"enabled": True},
            "sales": {"skip_order_cols": True, "sales_output": "sales"},
        })
        excluded = _excluded_dim_files(cfg)
        assert "return_reason.parquet" in excluded


# ===================================================================
# 6. logging_utils
# ===================================================================

from src.utils.logging_utils import (
    fmt_sec,
    human_duration,
    info,
    warn,
    fail,
    done,
    skip,
    work,
    debug,
    stage,
    short_path,
    configure_logging,
)


class TestFmtSec:
    def test_milliseconds(self):
        assert "ms" in fmt_sec(0.5)

    def test_seconds(self):
        result = fmt_sec(5.0)
        assert "s" in result

    def test_minutes(self):
        result = fmt_sec(120)
        assert ":" in result or "2:00" in result

    def test_zero(self):
        assert fmt_sec(0) == "0ms"


class TestHumanDuration:
    def test_alias(self):
        assert human_duration(5.0) == fmt_sec(5.0)


class TestShortPath:
    def test_none(self):
        assert short_path(None) is None

    def test_empty(self):
        assert short_path("") == ""


class TestLogFunctionsSmokeTests:
    """Ensure log functions don't crash when called. These are smoke tests."""

    def test_info_no_crash(self, capsys):
        info("test info message")

    def test_warn_no_crash(self, capsys):
        warn("test warning")

    def test_fail_no_crash(self, capsys):
        fail("test failure")

    def test_done_no_crash(self, capsys):
        done("test done")

    def test_skip_no_crash(self, capsys):
        skip("test skip")

    def test_work_no_crash(self, capsys):
        work("test work")

    def test_work_with_outfile(self, capsys):
        work(outfile="/some/path/file.parquet")

    def test_work_empty(self, capsys):
        work()

    def test_debug_no_crash(self):
        debug("test debug message")


class TestStageContextManager:
    def test_normal_exit(self, capsys):
        with stage("Test Stage"):
            pass
        captured = capsys.readouterr()
        assert "Test Stage" in captured.out

    def test_exception_logged_and_reraised(self, capsys):
        with pytest.raises(ValueError):
            with stage("Failing Stage"):
                raise ValueError("boom")
        captured = capsys.readouterr()
        assert "Failing Stage" in captured.out
        assert "FAIL" in captured.out

    def test_lazy_stage_no_output_if_silent(self, capsys):
        with stage("Lazy Stage", lazy=True):
            pass
        captured = capsys.readouterr()
        # lazy stage with no nested logs should emit SKIP, not INFO+DONE
        assert "up-to-date" in captured.out

    def test_lazy_stage_activates_on_nested_log(self, capsys):
        with stage("Lazy Active", lazy=True):
            info("inner message")
        captured = capsys.readouterr()
        assert "Lazy Active" in captured.out
        assert "inner message" in captured.out


class TestConfigureLogging:
    def test_configure_colors(self):
        configure_logging(enable_colors=False)
        from src.utils import logging_utils
        assert logging_utils.ENABLE_COLORS is False
        # reset
        configure_logging(enable_colors=True)

    def test_configure_file_log(self):
        configure_logging(enable_file_log=False)
        from src.utils import logging_utils
        assert logging_utils.ENABLE_FILE_LOG is False


class TestLogThreadSafety:
    """Verify that concurrent log calls don't raise exceptions."""

    def test_concurrent_info_calls(self):
        errors = []

        def log_many():
            try:
                for i in range(20):
                    info(f"thread message {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=log_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0


# ===================================================================
# 7. CLI
# ===================================================================

from src.cli import build_parser, str2bool


class TestStr2Bool:
    @pytest.mark.parametrize("val", [True, False])
    def test_bool_passthrough(self, val):
        assert str2bool(val) is val

    @pytest.mark.parametrize("val", ["yes", "true", "1", "y"])
    def test_truthy_strings(self, val):
        assert str2bool(val) is True

    @pytest.mark.parametrize("val", ["no", "false", "0", "n"])
    def test_falsy_strings(self, val):
        assert str2bool(val) is False

    def test_case_insensitive(self):
        assert str2bool("YES") is True
        assert str2bool("FALSE") is False

    def test_invalid_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            str2bool("maybe")

    def test_whitespace_stripped(self):
        assert str2bool("  true  ") is True


class TestBuildParser:
    def test_returns_argument_parser(self):
        parser = build_parser()
        assert isinstance(parser, argparse.ArgumentParser)

    def test_default_config(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.config == "config.yaml"
        assert args.models_config == "models.yaml"

    def test_format_choices(self):
        parser = build_parser()
        args = parser.parse_args(["--format", "csv"])
        assert args.format == "csv"

    def test_invalid_format_raises(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--format", "invalid"])

    def test_sales_rows(self):
        parser = build_parser()
        args = parser.parse_args(["--sales-rows", "50000"])
        assert args.sales_rows == 50000

    def test_workers(self):
        parser = build_parser()
        args = parser.parse_args(["--workers", "4"])
        assert args.workers == 4

    def test_dry_run(self):
        parser = build_parser()
        args = parser.parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_only_choices(self):
        parser = build_parser()
        args = parser.parse_args(["--only", "dimensions"])
        assert args.only == "dimensions"

    def test_regen_dimensions(self):
        parser = build_parser()
        args = parser.parse_args(["--regen-dimensions", "customers", "products"])
        assert args.regen_dimensions == ["customers", "products"]

    def test_customers_override(self):
        parser = build_parser()
        args = parser.parse_args(["--customers", "5000"])
        assert args.customers == 5000

    def test_skip_order_cols_bool(self):
        parser = build_parser()
        args = parser.parse_args(["--skip-order-cols", "true"])
        assert args.skip_order_cols is True

    def test_skip_order_cols_const(self):
        parser = build_parser()
        args = parser.parse_args(["--skip-order-cols"])
        assert args.skip_order_cols is True

    def test_all_defaults(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.format is None
        assert args.sales_rows is None
        assert args.workers is None
        assert args.chunk_size is None
        assert args.row_group_size is None
        assert args.start_date is None
        assert args.end_date is None
        assert args.only is None
        assert args.clean is False
        assert args.dry_run is False
        assert args.regen_dimensions is None
        assert args.refresh_fx_master is False
        assert args.customers is None
        assert args.stores is None
        assert args.products is None
        assert args.promotions is None
        assert args.skip_order_cols is None


# ===================================================================
# 9. exceptions
# ===================================================================

from src.exceptions import (
    ConfigError,
    DimensionError,
    PackagingError,
    PipelineError,
    SalesError,
    ValidationError,
)


class TestExceptionHierarchy:
    def test_pipeline_error_is_exception(self):
        assert issubclass(PipelineError, Exception)

    def test_config_error_is_pipeline_error(self):
        assert issubclass(ConfigError, PipelineError)

    def test_dimension_error_is_pipeline_error(self):
        assert issubclass(DimensionError, PipelineError)

    def test_sales_error_is_pipeline_error(self):
        assert issubclass(SalesError, PipelineError)

    def test_packaging_error_is_pipeline_error(self):
        assert issubclass(PackagingError, PipelineError)

    def test_validation_error_is_config_error(self):
        assert issubclass(ValidationError, ConfigError)

    def test_validation_error_is_pipeline_error(self):
        assert issubclass(ValidationError, PipelineError)

    def test_catch_pipeline_catches_all(self):
        for exc_cls in (ConfigError, DimensionError, SalesError, PackagingError, ValidationError):
            with pytest.raises(PipelineError):
                raise exc_cls("test")

    def test_catch_config_catches_validation(self):
        with pytest.raises(ConfigError):
            raise ValidationError("test")

    def test_message_preserved(self):
        e = ConfigError("bad config")
        assert str(e) == "bad config"

    def test_exceptions_are_distinct(self):
        # ConfigError should not catch SalesError
        with pytest.raises(SalesError):
            try:
                raise SalesError("sales broke")
            except ConfigError:
                pytest.fail("ConfigError should not catch SalesError")

    def test_pipeline_error_does_not_catch_value_error(self):
        with pytest.raises(ValueError):
            try:
                raise ValueError("standard error")
            except PipelineError:
                pytest.fail("PipelineError should not catch ValueError")
