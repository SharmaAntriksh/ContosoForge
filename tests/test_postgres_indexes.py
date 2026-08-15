"""Tests for the Postgres index generator (btree + BRIN).

Indexes are now *generated* from the declarative roster in
``src/tools/sql/schema_model.py`` (``INDEXES``) rather than composed from
hand-written .sql files, so they honour ``sql.identifier_case``. These tests
cover the composer wiring plus the generated content and snake-case transform.
"""
from __future__ import annotations

from pathlib import Path

from src.engine.packaging.sql_scripts import compose_postgres_indexes_sql
from src.tools.sql.dialect import REGISTRY
from src.tools.sql.constraint_sql import render_indexes_sql
from src.tools.sql.identifier_case import IdentifierCase
from src.tools.sql.name_map import build_name_map
from src.tools.sql.schema_model import INDEXES

PG = REGISTRY["postgres"]


def _run_layout(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "sql").mkdir(parents=True, exist_ok=True)
    return run


class TestComposerOutput:
    def test_writes_under_postgres_indexes(self, tmp_path: Path) -> None:
        run = _run_layout(tmp_path)
        compose_postgres_indexes_sql(sql_root=run / "sql")
        assert (run / "sql" / "postgres" / "indexes" / "01_create_indexes.sql").is_file()

    def test_does_not_touch_sqlserver_tree(self, tmp_path: Path) -> None:
        run = _run_layout(tmp_path)
        compose_postgres_indexes_sql(sql_root=run / "sql")
        assert not (run / "sql" / "sqlserver" / "indexes").exists()


class TestIndexContent:
    def _out(self) -> str:
        return render_indexes_sql(PG, name_map=None)

    def test_uses_create_index_if_not_exists(self) -> None:
        out = self._out()
        assert "CREATE INDEX IF NOT EXISTS" in out
        assert "CREATE UNIQUE INDEX" not in out  # FK source columns are not unique

    def test_no_sql_server_idioms(self) -> None:
        out = self._out()
        for bad in ("[dbo]", "OBJECT_ID(", "NONCLUSTERED", "\nGO\n"):
            assert bad not in out

    def test_covers_key_fact_fk_columns(self) -> None:
        out = self._out()
        for ix in ("IX_Sales_CustomerKey", "IX_Sales_ProductKey", "IX_Sales_StoreKey",
                   "IX_Sales_OrderDate", "IX_OrderHeader_CustomerKey",
                   "IX_OrderDetail_ProductKey", "IX_Returns_ReturnDate",
                   "IX_InventorySnapshot_WarehouseKey"):
            assert ix in out, f"Missing index: {ix}"

    def test_covers_date_columns_with_brin(self) -> None:
        out = self._out()
        for ix in ("BRIN_Sales_OrderDate", "BRIN_Sales_DueDate", "BRIN_Sales_DeliveryDate",
                   "BRIN_Returns_ReturnDate", "BRIN_InventorySnapshot_SnapshotDate"):
            assert ix in out, f"Missing BRIN index: {ix}"
        assert "USING BRIN" in out

    def test_does_not_index_pk_leading_column(self) -> None:
        # ProductKey leads InventorySnapshot's composite PK — a standalone btree is redundant.
        assert not any(i.table == "InventorySnapshot" and i.column == "ProductKey" for i in INDEXES)
        assert "IX_InventorySnapshot_ProductKey" not in self._out()

    def test_every_index_is_guarded(self) -> None:
        out = self._out()
        assert out.count("DO $$") == out.count("END $$;")
        for line in out.splitlines():
            assert not line.startswith("CREATE INDEX"), f"unguarded index: {line!r}"


class TestSnakeCase:
    def test_snake_transforms_identifiers_not_index_names(self) -> None:
        out = render_indexes_sql(PG, name_map=build_name_map(IdentifierCase.SNAKE))
        # DDL + guards lowercased...
        assert 'ON "public"."sales" ("customer_key")' in out
        assert "table_name='warehouses'" in out
        assert '"CustomerKey"' not in out
        # ...but index names stay verbatim (internal metadata).
        assert '"IX_Sales_CustomerKey"' in out

    def test_pascal_keeps_pascalcase(self) -> None:
        out = render_indexes_sql(PG, name_map=None)
        assert 'ON "public"."Sales" ("CustomerKey")' in out


class TestRosterIntegrity:
    def test_index_names_unique(self) -> None:
        names = [i.name for i in INDEXES]
        assert len(names) == len(set(names))

    def test_index_identifiers_in_name_map(self) -> None:
        nm = build_name_map(IdentifierCase.SNAKE)
        for i in INDEXES:
            assert i.table in nm.tables, f"{i.table} not in name map"
            assert i.column in nm.columns, f"{i.column} not in name map"

    def test_employee_home_store_is_indexed(self) -> None:
        assert any(
            i.table == "Employees" and i.column == "HomeStoreKey"
            for i in INDEXES
        )
