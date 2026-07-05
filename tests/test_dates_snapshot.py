"""Guardrail suite for the dates package refactor.

These tests are the byte-identity gate for behavior-preserving refactors. The
dates package consumes no RNG, so any diff in ``dates.parquet`` /
``time.parquet`` is a real diff — the golden snapshots below will catch it.

Test groups
-----------
* **Golden snapshots** — a normalized, platform-independent content digest of
  the generated ``dates.parquet`` / ``time.parquet`` over a fixture matrix.
* **Sync test** — the important one: for each fixture, the column list from
  ``resolve_date_columns`` (parquet side) must equal the ``static_schemas`` dates
  column list (SQL/TMDL side) must equal the actual parquet columns, and every
  internal column must carry a spaced-name entry. This mechanically enforces the
  ``NOTE: must be updated to match`` comment the two resolvers ask humans to
  remember.
* **Property tests** — structural invariants that hold regardless of config.
* **Regression tests** — pin the correctness fixes (unified iso default,
  arithmetic weekly-fiscal offsets, versioned key) so they can't silently
  regress.

Regenerating goldens (only when an intentional output change is being made)::

    UPDATE_DATES_GOLDEN=1 pytest tests/test_dates_snapshot.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.dimensions.dates import generate_date_table, resolve_date_columns
from src.dimensions.dates.runner import run_dates
from src.dimensions.dates.time import run_time_table
from src.dimensions.dates.weekly_fiscal import (
    WeeklyFiscalConfig as WFDataclass,
    add_weekly_fiscal_columns,
)
from src.dimensions.dates.columns import _SPACED_NAMES
from src.engine.config.config_schema import (
    DatesTableConfig,
    DefaultsConfig,
    GlobalDatesConfig,
)
from src.utils.static_schemas import (
    DATE_ALL_COLUMNS,
    STATIC_SCHEMAS,
    _DATES_REGISTRY,
    _dates_internal_columns,
    get_dates_schema,
)


GOLDEN_PATH = Path(__file__).parent / "golden" / "dates_snapshots.json"


# ===================================================================
# Fixture matrix
# ===================================================================
# Each fixture is (name, global_start, global_end, dates_config_kwargs).
# dates_config_kwargs are fed to the real Pydantic DatesTableConfig so the
# include/spaced/weekly-fiscal resolution behaves exactly as in the pipeline.

_FIXTURES = {
    # 1. Default: calendar+fiscal on, iso off, weekly fiscal default (on via runner).
    "default": (
        "2021-01-01",
        "2025-12-31",
        {},
    ),
    # 2. Exotic: shifted fiscal year, Nearest 454 weekly, start-year labeling,
    #    iso on, spaced names, no buffer.
    "exotic": (
        "2018-01-01",
        "2022-12-31",
        {
            "fiscal_start_month": 7,
            "buffer_years": 0,
            "spaced_column_names": True,
            "include": {
                "iso": True,
                "weekly_fiscal": {
                    "enabled": True,
                    "weekly_type": "Nearest",
                    "quarter_week_type": "454",
                    "type_start_fiscal_year": 0,
                },
            },
        },
    ),
    # 3. 53-week window: weekly Last 445, range chosen so the 53-week FW year
    #    2022 (fiscal_start_month=5) is fully inside.
    "week53": (
        "2021-01-01",
        "2023-12-31",
        {
            "fiscal_start_month": 5,
            "buffer_years": 0,
            "include": {
                "iso": True,
                "weekly_fiscal": {
                    "enabled": True,
                    "weekly_type": "Last",
                    "quarter_week_type": "445",
                },
            },
        },
    ),
}


def _build_cfg(start: str, end: str, dates_kwargs: dict) -> SimpleNamespace:
    """Build a minimal AppConfig-shaped object accepted by run_dates."""
    dates_cfg = DatesTableConfig(**dates_kwargs)
    defaults = DefaultsConfig(dates=GlobalDatesConfig(start=start, end=end))
    return SimpleNamespace(dates=dates_cfg, defaults=defaults, time=None)


def _generate_dates(cfg: SimpleNamespace, tmp_path: Path, monkeypatch) -> pd.DataFrame:
    """Run the real dates runner into *tmp_path* and read the parquet back.

    ``save_version`` is neutered so the hermetic run does not touch the shared
    ``data/versioning/`` folder.
    """
    monkeypatch.setattr("src.dimensions.dates.runner.save_version", lambda *a, **k: None)
    run_dates(cfg, tmp_path)
    return pd.read_parquet(tmp_path / "dates.parquet")


def _generate_time(cfg: SimpleNamespace, tmp_path: Path, monkeypatch) -> pd.DataFrame:
    monkeypatch.setattr("src.dimensions.lookups.save_version", lambda *a, **k: None)
    run_time_table(cfg, tmp_path)
    return pd.read_parquet(tmp_path / "time.parquet")


def _frame_digest(df: pd.DataFrame) -> str:
    """Platform-independent content digest of a frame.

    Combines column names, dtypes, row count, and pandas' stable (fixed-key)
    row hasher — order-sensitive so a reordered column set is caught too. int
    upcasting means dtype drift alone wouldn't move the row hash, so dtypes are
    folded into the digest explicitly.
    """
    meta = ";".join(f"{c}:{df[c].dtype}" for c in df.columns)
    row_hashes = pd.util.hash_pandas_object(df, index=False).to_numpy(dtype="<u8")
    h = hashlib.sha256()
    h.update(meta.encode("utf-8"))
    h.update(f"|rows={len(df)}|".encode("utf-8"))
    h.update(row_hashes.tobytes())
    return h.hexdigest()


def _load_golden() -> dict:
    if GOLDEN_PATH.exists():
        return json.loads(GOLDEN_PATH.read_text())
    return {}


def _updating_golden() -> bool:
    return os.environ.get("UPDATE_DATES_GOLDEN") == "1"


# ===================================================================
# Golden snapshots
# ===================================================================

class TestGoldenSnapshots:
    """Byte-identity gate: normalized digests of generated output."""

    @pytest.mark.parametrize("name", list(_FIXTURES))
    def test_dates_snapshot(self, name, tmp_path, monkeypatch):
        start, end, kw = _FIXTURES[name]
        df = _generate_dates(_build_cfg(start, end, kw), tmp_path, monkeypatch)
        digest = _frame_digest(df)
        key = f"dates:{name}"

        golden = _load_golden()
        if _updating_golden():
            golden[key] = digest
            GOLDEN_PATH.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
            pytest.skip(f"updated golden {key}")
        assert key in golden, f"no golden for {key}; run UPDATE_DATES_GOLDEN=1"
        assert digest == golden[key], f"{key} digest drift"

    def test_time_snapshot(self, tmp_path, monkeypatch):
        # time.parquet is config-independent; one fixture suffices.
        cfg = _build_cfg("2021-01-01", "2025-12-31", {})
        df = _generate_time(cfg, tmp_path, monkeypatch)
        digest = _frame_digest(df)
        key = "time:default"

        golden = _load_golden()
        if _updating_golden():
            golden[key] = digest
            GOLDEN_PATH.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
            pytest.skip(f"updated golden {key}")
        assert key in golden, "no golden for time:default; run UPDATE_DATES_GOLDEN=1"
        assert digest == golden[key], "time:default digest drift"


# ===================================================================
# Sync test — one source of column truth (across two resolvers + parquet)
# ===================================================================

class TestColumnSync:
    """resolve_date_columns == static_schemas dates list == parquet columns."""

    @pytest.mark.parametrize("name", list(_FIXTURES))
    def test_three_way_column_sync(self, name, tmp_path, monkeypatch):
        start, end, kw = _FIXTURES[name]
        cfg = _build_cfg(start, end, kw)
        df = _generate_dates(cfg, tmp_path, monkeypatch)

        resolved = resolve_date_columns(cfg.dates)
        schema_cols = [display for display, _dtype in get_dates_schema(cfg.dates)]
        parquet_cols = list(df.columns)

        assert resolved == parquet_cols, "resolve_date_columns disagrees with parquet"
        assert schema_cols == parquet_cols, "static_schemas disagrees with parquet"

    @pytest.mark.parametrize("name", list(_FIXTURES))
    def test_every_internal_column_has_spaced_name(self, name):
        start, end, kw = _FIXTURES[name]
        cfg = _build_cfg(start, end, kw)
        internal = _dates_internal_columns(cfg.dates)
        missing = [c for c in internal if c not in _SPACED_NAMES]
        assert not missing, f"columns lack a _SPACED_NAMES entry: {missing}"

    def test_generator_emits_only_registered_columns(self):
        # Inverse guard: every column the generator can emit (all subsystems on)
        # is in the registry, so nothing silently vanishes at df = df[cols].
        wf = WFDataclass(enabled=True, weekly_type="Last", quarter_week_type="445")
        df = generate_date_table(
            pd.Timestamp("2021-01-01"), pd.Timestamp("2023-12-31"), 5,
            as_of_date="2022-06-15", weekly_cfg=wf,
        )
        unregistered = set(df.columns) - set(DATE_ALL_COLUMNS)
        assert not unregistered, f"generator emits columns absent from registry: {unregistered}"

    def test_all_derived_artifacts_track_the_registry(self):
        # The real single-source guarantee: every artifact the codebase actually
        # uses is derived from _DATES_REGISTRY, in registry order. If a derivation
        # (or the row tuple shape) breaks, this fails — unlike a local simulation.
        from src.utils.static_schemas import (
            _DATES_BASE_INTERNAL,
            _DATES_CALENDAR_INTERNAL,
            _DATES_FISCAL_INTERNAL,
            _DATES_ISO_INTERNAL,
            _DATES_SPACED_NAMES,
            _WF_INTERNAL_COLS,
        )

        def names(group):
            return [r[0] for r in _DATES_REGISTRY if r[1] == group]

        assert _DATES_BASE_INTERNAL == names("base")
        assert _DATES_CALENDAR_INTERNAL == names("calendar")
        assert _DATES_ISO_INTERNAL == names("iso")
        assert _DATES_FISCAL_INTERNAL == names("fiscal")
        assert _WF_INTERNAL_COLS == names("weekly_fiscal")
        assert _DATES_SPACED_NAMES == {r[0]: r[2] for r in _DATES_REGISTRY}
        assert list(DATE_ALL_COLUMNS) == [r[0] for r in _DATES_REGISTRY]
        # SQL schema: same names in the same order, and the same name→type map.
        assert [n for n, _t in STATIC_SCHEMAS["Dates"]] == [r[0] for r in _DATES_REGISTRY]
        assert dict(STATIC_SCHEMAS["Dates"]) == {r[0]: r[3] for r in _DATES_REGISTRY}

    def test_registry_and_static_schema_cover_identical_columns(self):
        reg = {r[0] for r in _DATES_REGISTRY}
        schema = {n for n, _t in STATIC_SCHEMAS["Dates"]}
        assert reg == schema

    @pytest.mark.parametrize("enabled_val, expect_fw", [("false", False), (None, True)])
    def test_weekly_fiscal_edge_enabled_agrees_across_output_paths(self, enabled_val, expect_fw):
        # The parquet resolver (resolve_date_columns) and the SQL resolver
        # (_dates_internal_columns) must agree on the FW columns for the edge
        # `enabled` values that used to diverge (string "false", YAML null).
        cfg = {"include": {"weekly_fiscal": {"enabled": enabled_val}}}
        parquet_cols = set(resolve_date_columns(cfg))
        sql_cols = set(_dates_internal_columns(cfg))
        assert parquet_cols == sql_cols
        has_fw = any(c.startswith("FW") for c in sql_cols)
        assert has_fw is expect_fw

    @pytest.mark.parametrize("name", list(_FIXTURES))
    def test_static_schema_types_cover_all_columns(self, name):
        # get_dates_schema raises KeyError if STATIC_SCHEMAS['Dates'] lacks a
        # resolved column; calling it is the assertion.
        start, end, kw = _FIXTURES[name]
        cfg = _build_cfg(start, end, kw)
        cols = get_dates_schema(cfg.dates)
        assert len(cols) == len(_dates_internal_columns(cfg.dates))


# ===================================================================
# Property tests — structural invariants
# ===================================================================

def _all_on_df(start="2019-01-01", end="2024-12-31", fiscal=5):
    """Frame with every subsystem enabled, for offset/index property checks."""
    wf = WFDataclass(enabled=True, weekly_type="Last", quarter_week_type="445")
    return generate_date_table(
        pd.Timestamp(start), pd.Timestamp(end), fiscal,
        as_of_date="2022-06-15", weekly_cfg=wf,
    )


class TestProperties:
    def test_datekey_unique_and_one_row_per_day(self):
        df = _all_on_df("2020-01-01", "2020-12-31")
        assert df["DateKey"].is_unique
        assert len(df) == 366  # 2020 leap year
        diffs = df["Date"].sort_values().diff().dropna().dt.days.unique()
        assert list(diffs) == [1], "dates must be a contiguous daily range"

    @pytest.mark.parametrize(
        "index_col, boundary_col, boundary_val",
        [
            ("CalendarWeekIndex", "DayOfWeek", 0),         # Sunday = week start
            ("ISOYearWeekIndex", "DayOfWeek", 1),          # Monday = ISO week start
        ],
    )
    def test_week_index_increments_by_one_at_boundary(self, index_col, boundary_col, boundary_val):
        df = _all_on_df().sort_values("Date").reset_index(drop=True)
        idx = df[index_col].to_numpy()
        boundary = (df[boundary_col].to_numpy() == boundary_val)
        step = np.diff(idx)
        # Between consecutive days, the index either stays or steps +1.
        assert set(np.unique(step)).issubset({0, 1})
        # It steps exactly at the week-start day (boundary of the *second* day).
        assert np.array_equal(step == 1, boundary[1:])

    def test_fw_week_index_increments_at_fw_week_start(self):
        df = _all_on_df().sort_values("Date").reset_index(drop=True)
        step = np.diff(df["FWWeekIndex"].to_numpy())
        assert set(np.unique(step)).issubset({0, 1})
        assert np.array_equal(step == 1, (df["FWWeekDayNumber"].to_numpy()[1:] == 1))

    def test_all_offsets_zero_on_as_of_row(self):
        df = _all_on_df()
        row = df[df["Date"] == pd.Timestamp("2022-06-15")]
        assert len(row) == 1
        offset_cols = [
            "CurrentDayOffset", "YearOffset",
            "CalendarMonthOffset", "CalendarQuarterOffset", "CalendarWeekOffset",
            "ISOWeekOffset",
            "FiscalMonthOffset", "FiscalQuarterOffset", "FiscalYearOffset",
            "FWWeekOffset", "FWMonthOffset", "FWQuarterOffset",
        ]
        for col in offset_cols:
            assert int(row[col].iloc[0]) == 0, f"{col} != 0 on as_of row"

    def test_fw_year_bounds_partition_timeline(self):
        df = _all_on_df().sort_values("Date").reset_index(drop=True)
        # For each FW year, start/end are constant; successive years abut with
        # no gap or overlap.
        year_bounds = (
            df.groupby("FWYearNumber")
            .agg(start=("FWStartOfYear", "min"), end=("FWEndOfYear", "max"))
            .sort_index()
        )
        starts = year_bounds["start"].to_list()
        ends = year_bounds["end"].to_list()
        for prev_end, next_start in zip(ends[:-1], starts[1:]):
            assert next_start == prev_end + pd.Timedelta(days=1)

    def test_53_week_year_q4_has_98_days(self):
        # FW 2022 is a 53-week year under fiscal_start_month=5, 445 Last.
        df = _all_on_df("2021-01-01", "2023-12-31")
        q4 = df[(df["FWYearNumber"] == 2022) & (df["FWQuarterNumber"] == 4)]
        assert len(q4) > 0
        assert set(q4["FWQuarterDays"].unique()) == {98}

    def test_time_table_shape(self, tmp_path, monkeypatch):
        cfg = _build_cfg("2021-01-01", "2025-12-31", {})
        df = _generate_time(cfg, tmp_path, monkeypatch)
        assert len(df) == 1440
        assert df["TimeKey"].min() == 0
        assert df["TimeKey"].max() == 1439
        assert df["TimeKey"].is_unique


# ===================================================================
# Regression tests — pin the correctness fixes so they can't silently regress
# ===================================================================

class TestBugPins:
    def test_partial_weekly_fiscal_block_enables_not_disables(self):
        # A present-but-partial weekly_fiscal block (enabled omitted), validated
        # through Pydantic, must ENABLE weekly fiscal — matching an omitted block —
        # rather than silently disabling it (the old surprise). Explicit
        # `enabled: false` still turns it off.
        partial = DatesTableConfig(include={"weekly_fiscal": {"first_day_of_week": 1}})
        assert partial.include.weekly_fiscal.enabled is True
        assert any(c.startswith("FW") for c in resolve_date_columns(partial))

        off = DatesTableConfig(include={"weekly_fiscal": {"enabled": False}})
        assert not any(c.startswith("FW") for c in resolve_date_columns(off))

    def test_direct_api_default_stays_off(self):
        # The one place OFF is implicit: a bare generate_date_table call.
        df = generate_date_table(pd.Timestamp("2024-01-01"), pd.Timestamp("2024-03-31"), 5)
        assert not any(c.startswith("FW") for c in df.columns)

    def test_dict_config_without_iso_key_resolvers_agree(self):
        # A dict-shaped config missing include.iso: the two resolvers must agree.
        # Both now read DATE_GROUP_DEFAULTS (iso default False) — the divergence
        # (columns.py False vs static_schemas True) was fixed by the registry.
        cfg = {"include": {"calendar": True, "fiscal": True}}
        resolved = set(resolve_date_columns(cfg))
        internal = set(_dates_internal_columns(cfg))
        assert resolved == internal
        assert not any(c.startswith("ISO") for c in resolved), "iso defaults OFF for dict configs"

    def test_weekly_fiscal_offsets_arithmetic_when_as_of_absent(self):
        dates = pd.date_range("2022-01-01", "2022-12-31", freq="D")
        base = pd.DataFrame({
            "Date": dates,
            "Year": dates.year.astype(np.int32),
            "Month": dates.month.astype(np.int32),
        })
        wf = WFDataclass(enabled=True, weekly_type="Last", quarter_week_type="445")
        out = add_weekly_fiscal_columns(
            base, first_fiscal_month=5, cfg=wf, as_of=pd.Timestamp("1990-01-01"),
        )
        # as_of is not in the frame; arithmetic offsets vary across the year
        # (the old all-zero fallback collapsed them to a single value).
        assert out["FWWeekOffset"].nunique() > 1
        # And they equal FWWeekIndex minus a single constant (the as_of index).
        deltas = (out["FWWeekIndex"] - out["FWWeekOffset"]).unique()
        assert len(deltas) == 1

    def test_weekly_fiscal_offset_zero_on_in_frame_as_of(self):
        # When as_of IS in the frame, the arithmetic index equals that row's
        # value — the offset there is exactly 0 (byte-preserving vs the old path).
        dates = pd.date_range("2022-01-01", "2022-12-31", freq="D")
        base = pd.DataFrame({
            "Date": dates,
            "Year": dates.year.astype(np.int32),
            "Month": dates.month.astype(np.int32),
        })
        wf = WFDataclass(enabled=True, weekly_type="Last", quarter_week_type="445")
        as_of = pd.Timestamp("2022-06-15")
        out = add_weekly_fiscal_columns(base, first_fiscal_month=5, cfg=wf, as_of=as_of)
        row = out[out["Date"] == as_of]
        for col in ("FWWeekOffset", "FWMonthOffset", "FWQuarterOffset"):
            assert int(row[col].iloc[0]) == 0

    def test_dates_version_key_has_schema_version(self, tmp_path, monkeypatch):
        captured = {}

        def _capture(name, cfg_section, out_path):
            captured["cfg"] = cfg_section

        monkeypatch.setattr("src.dimensions.dates.runner.save_version", _capture)
        cfg = _build_cfg("2021-01-01", "2025-12-31", {})
        run_dates(cfg, tmp_path)
        assert "cfg" in captured, "save_version was not called"
        assert "schema_version" in captured["cfg"]

    def test_time_version_key_has_schema_version(self, tmp_path, monkeypatch):
        # time already gets a schema_version via _build_lookup_dim's default —
        # pin it so the two dates-package tables stay consistent.
        captured = {}
        monkeypatch.setattr(
            "src.dimensions.lookups.save_version",
            lambda name, cfg_section, out_path: captured.__setitem__("cfg", cfg_section),
        )
        cfg = _build_cfg("2021-01-01", "2025-12-31", {})
        run_time_table(cfg, tmp_path)
        assert "schema_version" in captured.get("cfg", {})
