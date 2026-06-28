"""Tests for the Postgres index composer (btree + BRIN)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.engine.packaging.sql_scripts import compose_postgres_indexes_sql


@pytest.fixture
def run_layout(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True)
    return run


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestComposerOutput:
    def test_writes_under_postgres_indexes(self, run_layout: Path) -> None:
        compose_postgres_indexes_sql(sql_root=run_layout / "sql")
        out = run_layout / "postgres" / "indexes" / "01_create_indexes.sql"
        assert out.is_file()

    def test_does_not_touch_sql_root(self, run_layout: Path) -> None:
        compose_postgres_indexes_sql(sql_root=run_layout / "sql")
        assert not (run_layout / "sql" / "indexes").exists(), \
            "Postgres indexes must not leak into the SQL Server sql/ tree"


class TestIndexFileContent:
    def _src_dir(self) -> Path:
        return Path(__file__).resolve().parents[1] / "scripts" / "sql" / "postgres" / "indexes"

    def _combined(self) -> str:
        return "".join(_read(p) for p in sorted(self._src_dir().glob("*.sql")))

    @staticmethod
    def _strip_comments(text: str) -> str:
        import re
        text = re.sub(r"/\*[\s\S]*?\*/", "", text)
        text = re.sub(r"--[^\n]*", "", text)
        return text

    def test_both_source_files_present(self) -> None:
        names = {p.name for p in self._src_dir().glob("*.sql")}
        assert {"01_create_btree_indexes.sql", "02_create_brin_indexes.sql"}.issubset(names)

    def test_uses_create_index_if_not_exists(self) -> None:
        text = self._strip_comments(self._combined())
        assert "CREATE INDEX IF NOT EXISTS" in text
        # No CREATE UNIQUE INDEX — FK source columns are not unique.
        assert "CREATE UNIQUE INDEX" not in text

    def test_no_sql_server_idioms(self) -> None:
        text = self._strip_comments(self._combined())
        assert "[dbo]" not in text
        assert "OBJECT_ID(" not in text
        assert "NONCLUSTERED" not in text
        assert "\nGO\n" not in text

    def test_covers_key_fact_fk_columns(self) -> None:
        """Spot-check: the load-bearing FK columns on Sales must be indexed."""
        text = self._combined()
        for ix in (
            "IX_Sales_CustomerKey",
            "IX_Sales_ProductKey",
            "IX_Sales_StoreKey",
            "IX_Sales_OrderDate",
            "IX_OrderHeader_CustomerKey",
            "IX_OrderDetail_ProductKey",
            "IX_Returns_ReturnDate",
            "IX_InventorySnapshot_WarehouseKey",
        ):
            assert ix in text, f"Missing expected index: {ix}"

    def test_covers_key_date_columns_with_brin(self) -> None:
        """BRIN should cover the date columns BI queries filter on."""
        text = self._combined()
        for ix in (
            "BRIN_Sales_OrderDate",
            "BRIN_Sales_DueDate",
            "BRIN_Sales_DeliveryDate",
            "BRIN_Returns_ReturnDate",
            "BRIN_InventorySnapshot_SnapshotDate",
        ):
            assert ix in text, f"Missing expected BRIN index: {ix}"

    def test_brin_uses_brin_clause(self) -> None:
        text = self._combined()
        assert "USING BRIN" in text, "BRIN indexes must declare USING BRIN"

    def test_does_not_duplicate_pk_leading_columns(self) -> None:
        """ProductKey leads InventorySnapshot's composite PK — don't add a redundant standalone btree."""
        text = self._combined()
        assert "IX_InventorySnapshot_ProductKey" not in text, (
            "ProductKey already leads the InventorySnapshot composite PK; "
            "a standalone btree on it would be redundant."
        )

    def test_every_create_index_is_guarded(self) -> None:
        """No raw CREATE INDEX outside a DO $$ block (everything must be table/column-guarded)."""
        text = self._strip_comments(self._combined())
        assert text.count("DO $$") > 0, "expected DO $$ guards"
        for line in text.splitlines():
            if line.startswith("CREATE INDEX"):
                raise AssertionError(
                    f"Unguarded CREATE INDEX at file top level: {line!r}"
                )
