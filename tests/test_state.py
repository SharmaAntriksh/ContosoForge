"""Tests for the Sales State class and bind_globals."""
from __future__ import annotations

import numpy as np
import pytest

from src.facts.sales.sales_logic.globals import State, bind_globals, fmt


# ===================================================================
# State lifecycle
# ===================================================================

class TestState:
    def setup_method(self):
        State.reset()

    def teardown_method(self):
        State.reset()

    def test_reset_clears_all(self):
        State.skip_order_cols = True
        State.file_format = "csv"

        State.reset()

        assert State.skip_order_cols is None
        assert State.file_format is None

    def test_bind_after_reset_is_allowed(self):
        # State is per-worker and read-only by convention (no seal machinery);
        # reset + rebind must always work (tests rebind State between cases).
        bind_globals({"skip_order_cols": False})
        State.reset()
        bind_globals({"skip_order_cols": True})

        assert State.skip_order_cols is True


# ===================================================================
# bind_globals
# ===================================================================

class TestBindGlobals:
    def setup_method(self):
        State.reset()

    def teardown_method(self):
        State.reset()

    def test_binds_values(self):
        bind_globals({"skip_order_cols": True, "file_format": "csv"})

        assert State.skip_order_cols is True
        assert State.file_format == "csv"

    def test_non_dict_raises(self):
        with pytest.raises(TypeError, match="expects a dict"):
            bind_globals("not a dict")

    def test_discovery_month_binds_through(self):
        # Discovery is a static broadcast array now (no mutable seen_customers).
        arr = np.array([0, 1, 2, 5], dtype=np.int64)
        bind_globals({"skip_order_cols": False, "customer_discovery_month": arr})

        np.testing.assert_array_equal(State.customer_discovery_month, arr)

    def test_discovery_month_defaults_none(self):
        bind_globals({"skip_order_cols": False})

        assert State.customer_discovery_month is None


# ===================================================================
# fmt (date formatting)
# ===================================================================

class TestFmt:
    def test_single_date(self):
        d = np.datetime64("2023-06-15")

        result = fmt(d)

        assert result == "20230615"

    def test_array_of_dates(self):
        dates = np.array(["2023-01-01", "2023-12-31"], dtype="datetime64[D]")

        result = fmt(dates)

        np.testing.assert_array_equal(result, ["20230101", "20231231"])

    def test_first_day_of_year(self):
        d = np.datetime64("2020-01-01")

        assert fmt(d) == "20200101"

    def test_last_day_of_year(self):
        d = np.datetime64("2020-12-31")

        assert fmt(d) == "20201231"

    def test_leap_day(self):
        d = np.datetime64("2024-02-29")

        assert fmt(d) == "20240229"
