"""Regression tests for the SQL-import hardening fixes (docs/SQL_IMPORT_REVIEW.md).

DB-free: exercises the pure parsing/escaping/splitting helpers on the import
path plus the CLI connection-string builder. No database is touched.

Findings pinned here:
  A1a  — ODBC ``DATABASE`` value is brace-quoted (no attribute injection).
  A1b  — ``build_connection_string`` braces server/user/password; bad driver rejected.
  C3a  — ``_drain_results`` surfaces (does not swallow) a deferred server error.
  C3b  — a failed unattributed batch containing a load keyword raises, not WARNs.
  C3c/B2a — ``_split_load_statements`` accumulates preamble and keeps a trailing residual.
  B5a/B5b — ``_read_sql_text`` encoding handling (BOM, UTF-8-first, NUL strip).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.exceptions import SqlServerImportError
from src.tools.sql.sql_helpers import odbc_brace
from src.tools.sql.sql_server_import import (
    _drain_results,
    _execute_load_with_progress,
    _read_sql_text,
    _restore_primary_keys_with_progress,
    _split_load_statements,
    _with_database,
)

try:
    import pyodbc
except ImportError:  # pragma: no cover - exercised only where pyodbc is absent
    pyodbc = None

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ===================================================================
# A1a — ODBC DATABASE value is brace-quoted
# ===================================================================

class TestOdbcBrace:
    def test_plain_value_wrapped(self):
        assert odbc_brace("ContosoDW") == "{ContosoDW}"

    def test_semicolon_stays_inside_braces(self):
        # A ';' must not be able to start a new keyword=value pair.
        assert odbc_brace("ContosoDW;Encrypt=no") == "{ContosoDW;Encrypt=no}"

    def test_literal_close_brace_doubled(self):
        assert odbc_brace("a}b") == "{a}}b}"


class TestWithDatabase:
    def test_database_is_braced(self):
        out = _with_database("DRIVER={x};SERVER={s};", "ContosoDW")
        assert out.endswith(";DATABASE={ContosoDW}")

    def test_injection_attempt_is_neutralized(self):
        # The classic A1a repro: a ';Encrypt=no' rider stays inside the braces,
        # so ODBC reads it as part of the database name, not a new attribute.
        out = _with_database("DRIVER={x};SERVER={s};", "ContosoDW;Encrypt=no")
        assert out.endswith(";DATABASE={ContosoDW;Encrypt=no}")
        # No bare (unbraced) Encrypt keyword leaked into the string.
        assert ";Encrypt=no" not in out.replace("{ContosoDW;Encrypt=no}", "")


# ===================================================================
# A1b — CLI build_connection_string braces fields; rejects bad driver
# ===================================================================

@pytest.fixture(scope="module")
def cli_module():
    """Load scripts/sql/run_sql_server_import.py (no package __init__)."""
    path = _REPO_ROOT / "scripts" / "sql" / "run_sql_server_import.py"
    spec = importlib.util.spec_from_file_location("run_sql_server_import_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _args(**over):
    base = dict(
        odbc_driver=None, trusted=False, server="srv", user="u",
        password="p", password_env=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestBuildConnectionString:
    def test_trusted_braces_server_with_separator(self, cli_module):
        cs = cli_module.build_connection_string(_args(trusted=True, server="a;b=c"))
        assert "SERVER={a;b=c};" in cs
        assert "Trusted_Connection=yes" in cs

    def test_sql_auth_braces_user_and_password(self, cli_module):
        cs = cli_module.build_connection_string(_args(user="u;x", password="p;y"))
        assert "UID={u;x};" in cs
        assert "PWD={p;y};" in cs

    def test_driver_with_separator_rejected(self, cli_module):
        with pytest.raises(ValueError):
            cli_module.build_connection_string(_args(odbc_driver="Bad;Driver"))

    def test_driver_with_brace_rejected(self, cli_module):
        with pytest.raises(ValueError):
            cli_module.build_connection_string(_args(odbc_driver="Bad}Driver"))


# ===================================================================
# B5a/B5b — _read_sql_text encoding handling
# ===================================================================

class TestReadSqlText:
    def test_plain_utf8(self, tmp_path):
        p = tmp_path / "a.sql"
        p.write_bytes("SELECT 1;".encode("utf-8"))
        assert _read_sql_text(p) == "SELECT 1;"

    def test_utf8_bom(self, tmp_path):
        p = tmp_path / "b.sql"
        p.write_bytes(b"\xef\xbb\xbf" + "SELECT 1;".encode("utf-8"))
        assert _read_sql_text(p) == "SELECT 1;"

    def test_utf16le_bom(self, tmp_path):
        p = tmp_path / "c.sql"
        p.write_bytes("SELECT 1;".encode("utf-16"))  # includes LE BOM
        assert _read_sql_text(p) == "SELECT 1;"

    def test_utf16be_bom(self, tmp_path):
        p = tmp_path / "d.sql"
        # Explicit BE BOM so the reader trusts the endianness.
        p.write_bytes(b"\xfe\xff" + "SELECT 1;".encode("utf-16-be"))
        assert _read_sql_text(p) == "SELECT 1;"

    def test_multibyte_utf8_roundtrips(self, tmp_path):
        p = tmp_path / "e.sql"
        text = "-- café ☕ 日本\nSELECT 1;"
        p.write_bytes(text.encode("utf-8"))
        assert _read_sql_text(p) == text

    def test_utf8_with_embedded_nul_is_stripped_not_misdecoded(self, tmp_path):
        # B5b: a NUL is valid UTF-8; the reader must decode as UTF-8 (not veer
        # into a UTF-16 guess) and strip the NUL, preserving the ASCII content.
        p = tmp_path / "f.sql"
        p.write_bytes(b"SELECT\x00 1;")
        assert _read_sql_text(p) == "SELECT 1;"

    def test_bomless_utf16be_ascii_recovers_content(self, tmp_path):
        # B5a: previously decoded as LE garbage ('匀䔀...'); now UTF-8-first +
        # NUL strip recovers the ASCII SQL instead of silently corrupting it.
        p = tmp_path / "g.sql"
        p.write_bytes("SELECT 1;".encode("utf-16-be"))  # no BOM
        assert _read_sql_text(p) == "SELECT 1;"


# ===================================================================
# C3c/B2a — _split_load_statements preamble + residual handling
# ===================================================================

class TestSplitLoadStatements:
    def test_go_split_labels_batches(self):
        sql = "SET NOCOUNT ON;\nGO\nBULK INSERT dbo.Sales\nFROM N'x';\nGO"
        out = _split_load_statements(sql)
        assert out[0] == ("batch", "SET NOCOUNT ON;")
        assert out[1][0] == "Sales"

    def test_no_go_preamble_prepended_to_first_table(self):
        sql = "SET NOCOUNT ON;\nBULK INSERT dbo.Sales\nFROM N'x';"
        out = _split_load_statements(sql)
        assert len(out) == 1
        table, stmt = out[0]
        assert table == "Sales"
        assert stmt.startswith("SET NOCOUNT ON;")

    def test_no_go_preamble_accumulates_not_overwrites(self):
        # Two consecutive non-table parts: both must survive on the first table.
        sql = (
            "SET NOCOUNT ON;\n"
            "-- a standalone comment part\n"
            "BULK INSERT dbo.Sales\nFROM N'x';"
        )
        out = _split_load_statements(sql)
        assert len(out) == 1
        _, stmt = out[0]
        assert "SET NOCOUNT ON;" in stmt
        assert "standalone comment" in stmt

    def test_trailing_unmatched_part_not_dropped(self):
        # A final BULK INSERT whose bracketed name defeats extraction must not
        # vanish — it should surface as an unattributed 'batch'.
        sql = "BULK INSERT [My-Table]\nFROM N'x'\nWITH (FORMAT='CSV');"
        out = _split_load_statements(sql)
        assert out, "trailing statement was silently dropped"
        assert out[-1][0] == "batch"
        assert "My-Table" in out[-1][1]


# ===================================================================
# C3a / C3b — fake-cursor behavioural tests
# ===================================================================

pytestmark_pyodbc = pytest.mark.skipif(pyodbc is None, reason="pyodbc not installed")


class _NoResultCursor:
    """Cursor whose statements produce no row set (description is None)."""
    description = None

    def fetchall(self):  # pragma: no cover - must never be called
        raise AssertionError("fetchall() called when description is None")

    def nextset(self):
        return False


class _DeferredErrorCursor:
    """Cursor with an open row set whose fetchall() raises a server error."""
    description = [("col", None)]

    def __init__(self):
        self.err = pyodbc.Error("42000", "[SQL Server] deferred THROW") if pyodbc else RuntimeError()

    def fetchall(self):
        raise self.err

    def nextset(self):
        return False


@pytestmark_pyodbc
class TestDrainResults:
    def test_skips_fetch_when_no_result_set(self):
        # description is None -> fetchall must NOT be called (would AssertionError).
        _drain_results(_NoResultCursor())

    def test_deferred_error_is_surfaced(self):
        # C3a: an error at the fetch of the current rowset must propagate.
        with pytest.raises(pyodbc.Error):
            _drain_results(_DeferredErrorCursor())


def test_pk_restore_uses_shared_total_elapsed_spinner(monkeypatch):
    calls = []

    class Cursor(_NoResultCursor):
        def execute(self, statement):
            calls.append(statement)

    def fake_spinner(label, work, *, elapsed_from):
        calls.append((label, elapsed_from))
        work()

    monkeypatch.setattr(
        "src.tools.sql.sql_server_import._run_with_spinner",
        fake_spinner,
    )

    _restore_primary_keys_with_progress(Cursor(), elapsed_from=123.0)

    assert calls == [
        ("Restoring Primary Keys & Foreign Keys", 123.0),
        "EXEC [admin].[ManagePrimaryKeys] @Action = 'RESTORE'",
    ]


class _RaisingExecCursor:
    """Every execute() fails with a driver error; description is None."""
    description = None

    def execute(self, *_a, **_k):
        raise pyodbc.Error("HY000", "boom") if pyodbc else RuntimeError("boom")

    def fetchall(self):
        return []

    def nextset(self):
        return False


@pytestmark_pyodbc
class TestPreambleGuard:
    def test_failed_genuine_preamble_warns_and_continues(self, tmp_path):
        # A non-load batch (SET NOCOUNT) that fails is best-effort — no raise.
        p = tmp_path / "load.sql"
        p.write_bytes(b"SET NOCOUNT ON;\nGO")
        _execute_load_with_progress(_RaisingExecCursor(), p)  # must not raise

    def test_failed_unattributed_load_statement_raises(self, tmp_path):
        # C3b: a BULK INSERT that couldn't be attributed to a table must raise
        # on failure, not be downgraded to a WARN (which would ship an empty
        # table with exit code 0).
        p = tmp_path / "load.sql"
        p.write_bytes(
            b"BULK INSERT [My-Table]\nFROM N'x'\nWITH (FORMAT='CSV');\nGO"
        )
        with pytest.raises(SqlServerImportError):
            _execute_load_with_progress(_RaisingExecCursor(), p)
