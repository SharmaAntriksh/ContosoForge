"""Unit tests for the Postgres importer and the shared importer helpers.

End-to-end behaviour against a live Postgres requires Docker — see the
manual recipe in ``tests/test_dialect_postgres.py``. These tests cover
the file-discovery + statement-parsing parts that can run hermetically.
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from src.exceptions import PostgresImportError
from src.tools.sql._import_common import (
    _extract_tables_from_create_sql,
    _short_path,
    find_create_sql,
    find_finish_load_script,
    find_prepare_load_script,
    ordered_load_files,
)
from src.tools.sql.postgres_import import (
    _apply_constraints_parallel,
    _apply_copy_script_via_stdin,
    _apply_indexes_parallel,
    _group_ddl_blocks_by_table,
    _index_block_label,
    _index_build_tuning,
    _list_vacuum_targets,
    _resolve_index_build_workers,
    _resolve_maintenance_workers,
    _rewrite_fk_block,
    _run_ddl_phase,
    _run_table_affine_ddl_phase,
    _run_with_spinner,
    _vacuum_freeze_analyze_tables,
    import_postgres,
)


class TestPostgresImportSpinner:
    def test_shows_label_and_clears_line(self, monkeypatch) -> None:
        output = io.StringIO()
        monkeypatch.setattr("src.tools.sql._import_common.sys.stdout", output)

        result = _run_with_spinner(
            "Loading public.Sales",
            lambda: (time.sleep(0.02), "loaded")[1],
        )

        rendered = output.getvalue()
        assert result == "loaded"
        assert "Loading public.Sales" in rendered
        assert rendered.endswith("\r")

    def test_clears_line_when_work_fails(self, monkeypatch) -> None:
        output = io.StringIO()
        monkeypatch.setattr("src.tools.sql._import_common.sys.stdout", output)

        def fail() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            _run_with_spinner("Applying constraints", fail)

        assert output.getvalue().endswith("\r")

    def test_can_report_total_pipeline_elapsed(self, monkeypatch) -> None:
        output = io.StringIO()
        monkeypatch.setattr("src.tools.sql._import_common.sys.stdout", output)

        _run_with_spinner(
            "Restoring Primary Keys & Foreign Keys",
            lambda: time.sleep(0.02),
            elapsed_from=time.time() - 5,
        )

        rendered = output.getvalue()
        assert "Restoring Primary Keys & Foreign Keys" in rendered
        assert "Total elapsed" in rendered

    def test_copy_spinner_reports_parallel_chunk_progress(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        csv_paths = [tmp_path / "sales_1.csv", tmp_path / "sales_2.csv"]
        for csv_path in csv_paths:
            csv_path.write_text("SalesKey\n1\n", encoding="utf-8")

        sql_file = tmp_path / "02_copy_facts.sql"
        sql_file.write_text(
            "\n".join(
                f'COPY "public"."Sales" FROM \'{path.as_posix()}\';'
                for path in csv_paths
            ),
            encoding="utf-8",
        )

        snapshots = []

        def fake_spinner(label, work, *, detail=None):
            snapshots.append((label, detail()))
            result = work()
            snapshots.append((label, detail()))
            return result

        def fake_parallel_copy(_target, paths, *, on_chunk_done, **_kwargs):
            for _path in paths:
                on_chunk_done()

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeConnection:
            def cursor(self):
                return FakeCursor()

        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_with_spinner", fake_spinner
        )
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._parallel_copy_chunks",
            fake_parallel_copy,
        )

        _apply_copy_script_via_stdin(
            FakeConnection(),
            sql_file,
            run_dir=tmp_path,
            load_workers=4,
            parallel_dsn={"dbname": "test"},
        )

        assert snapshots == [
            ('Loading "public"."Sales"', " (0/2 chunks, 2 workers)"),
            ('Loading "public"."Sales"', " (2/2 chunks, 2 workers)"),
        ]


class TestPostgresConstraintScheduling:
    @staticmethod
    def _fk_block(parent: str, constraint: str) -> str:
        return (
            "DO $$\nBEGIN\n"
            "    IF NOT EXISTS (SELECT 1 FROM pg_constraint) THEN\n"
            f'        ALTER TABLE "public"."{parent}"\n'
            f'            ADD CONSTRAINT "{constraint}" FOREIGN KEY ("DimKey")\n'
            '            REFERENCES "public"."Dim" ("DimKey");\n'
            "    END IF;\nEND $$;"
        )

    def test_fk_rewrite_preserves_add_and_targets_validation(self) -> None:
        rewritten = _rewrite_fk_block(self._fk_block("Sales", "FK_Sales_Dim"))

        assert rewritten is not None
        add_block, validate_block = rewritten
        assert 'REFERENCES "public"."Dim" ("DimKey") NOT VALID;' in add_block
        assert (
            'ALTER TABLE "public"."Sales" '
            'VALIDATE CONSTRAINT "FK_Sales_Dim";'
        ) in validate_block

    @pytest.mark.parametrize(
        ("load_workers", "expected"),
        [(1, 1), (2, 2), (4, 4), (8, 4), (64, 4)],
    )
    def test_index_build_workers_are_bounded(
        self, load_workers: int, expected: int
    ) -> None:
        assert _resolve_index_build_workers(load_workers) == expected

    def test_index_build_memory_is_divided_across_workers(self) -> None:
        assert _index_build_tuning(4) == [
            "SET synchronous_commit = off;",
            "SET maintenance_work_mem = '256MB';",
            "SET max_parallel_maintenance_workers = 2;",
        ]

    def test_index_label_includes_table_and_index_name(self) -> None:
        block = (
            "DO $$\nBEGIN\n"
            '    CREATE INDEX IF NOT EXISTS "IX_Sales_CustomerKey"\n'
            '    ON "public"."sales" ("customer_key");\n'
            "END $$;"
        )

        assert _index_block_label(block) == (
            '"public"."sales" "IX_Sales_CustomerKey"'
        )

    def test_independent_ddl_phase_reports_slow_items(self, monkeypatch) -> None:
        observed = {}
        logs = []

        def fake_parallel(items, work_fn, **kwargs):
            observed.update(items=items, kwargs=kwargs)

            class Cursor:
                def execute(self, _statement):
                    return None

            for item in items:
                work_fn(Cursor(), item)

        clock = iter([10.0, 10.7])
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_parallel_in_workers",
            fake_parallel,
        )
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_with_spinner",
            lambda _label, work: work(),
        )
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._time.time",
            lambda: next(clock),
        )
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._log",
            lambda level, message: logs.append((level, message)),
        )

        _run_ddl_phase(
            ["CREATE INDEX test"],
            "Index phase",
            target_dsn={"dbname": "test"},
            n_workers=4,
            setup_sql=["SET maintenance_work_mem = '256MB';"],
            item_label=lambda _block: "public.sales IX_Test",
        )

        assert observed["kwargs"]["n_workers"] == 1
        assert observed["kwargs"]["setup_sql"] == [
            "SET maintenance_work_mem = '256MB';"
        ]
        assert ("WORK", "    public.sales IX_Test (0.7s)") in logs

    def test_secondary_indexes_use_bounded_build_policy(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        index_file = tmp_path / "01_create_indexes.sql"
        index_file.write_text(
            "DO $$\nBEGIN\n"
            '    CREATE INDEX IF NOT EXISTS "IX_Sales_CustomerKey"\n'
            '    ON "public"."sales" ("customer_key");\n'
            "END $$;",
            encoding="utf-8",
        )
        phases = []
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_ddl_phase",
            lambda blocks, label, **kwargs: phases.append((blocks, label, kwargs)),
        )

        _apply_indexes_parallel(
            [index_file],
            target_dsn={"dbname": "test"},
            n_workers=8,
            run_dir=tmp_path,
        )

        assert len(phases) == 1
        blocks, label, kwargs = phases[0]
        assert len(blocks) == 1
        assert label == "Index phase"
        assert kwargs["n_workers"] == 4
        assert kwargs["setup_sql"] == _index_build_tuning(4)
        assert kwargs["item_label"] is _index_block_label

    def test_groups_same_table_into_one_scheduler_item(self) -> None:
        sales_pk = (
            'ALTER TABLE "public"."Sales" '
            'ADD CONSTRAINT "PK_Sales" PRIMARY KEY ("SalesKey");'
        )
        sales_uq = (
            'ALTER TABLE "public"."Sales" '
            'ADD CONSTRAINT "UQ_Sales" UNIQUE ("OrderNumber");'
        )
        returns_pk = (
            'ALTER TABLE "public"."Returns" '
            'ADD CONSTRAINT "PK_Returns" PRIMARY KEY ("ReturnKey");'
        )

        assert _group_ddl_blocks_by_table(
            [returns_pk, sales_pk, sales_uq]
        ) == [
            ('"public"."Sales"', [sales_pk, sales_uq]),
            ('"public"."Returns"', [returns_pk]),
        ]

    def test_table_affine_phase_dispatches_tables_not_statements(
        self, monkeypatch
    ) -> None:
        blocks = [
            'ALTER TABLE "public"."Sales" VALIDATE CONSTRAINT "FK_Sales_A";',
            'ALTER TABLE "public"."Sales" VALIDATE CONSTRAINT "FK_Sales_B";',
            'ALTER TABLE "public"."Returns" VALIDATE CONSTRAINT "FK_Returns_A";',
        ]
        observed = {}
        executed = []

        class Cursor:
            def execute(self, statement):
                executed.append(statement)

        def fake_parallel(items, work_fn, *, n_workers, **_kwargs):
            observed["items"] = items
            observed["workers"] = n_workers
            cursor = Cursor()
            for item in items:
                work_fn(cursor, item)

        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_parallel_in_workers",
            fake_parallel,
        )
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_with_spinner",
            lambda _label, work, **_kwargs: work(),
        )

        _run_table_affine_ddl_phase(
            blocks,
            "VALIDATE CONSTRAINT phase",
            target_dsn={"dbname": "test"},
            n_workers=8,
            setup_sql=[],
        )

        assert observed == {
            "items": [
                ('"public"."Sales"', blocks[:2]),
                ('"public"."Returns"', blocks[2:]),
            ],
            "workers": 2,
        }
        assert executed == blocks

    def test_table_affine_phase_propagates_statement_failure(
        self, monkeypatch
    ) -> None:
        blocks = [
            'ALTER TABLE "public"."Sales" VALIDATE CONSTRAINT "FK_Sales_A";',
            'ALTER TABLE "public"."Sales" VALIDATE CONSTRAINT "FK_Sales_B";',
        ]

        class Cursor:
            def execute(self, statement):
                if "FK_Sales_B" in statement:
                    raise RuntimeError("validation failed")

        def fake_parallel(items, work_fn, **_kwargs):
            work_fn(Cursor(), items[0])

        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_parallel_in_workers",
            fake_parallel,
        )
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_with_spinner",
            lambda _label, work, **_kwargs: work(),
        )

        with pytest.raises(RuntimeError, match="validation failed"):
            _run_table_affine_ddl_phase(
                blocks,
                "VALIDATE CONSTRAINT phase",
                target_dsn={"dbname": "test"},
                n_workers=8,
                setup_sql=[],
            )

    def test_constraint_pipeline_preserves_dependency_phases(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pk_block = (
            "DO $$\nBEGIN\n"
            '    ALTER TABLE "public"."Dim"\n'
            '        ADD CONSTRAINT "PK_Dim" PRIMARY KEY ("DimKey");\n'
            "END $$;"
        )
        constraint_file = tmp_path / "03_create_constraints.sql"
        constraint_file.write_text(
            "\n".join(
                [
                    pk_block,
                    self._fk_block("Sales", "FK_Sales_Dim"),
                    self._fk_block("Returns", "FK_Returns_Dim"),
                ]
            ),
            encoding="utf-8",
        )
        phases = []

        def capture_phase(blocks, label, **kwargs):
            phases.append((label, blocks, kwargs))

        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_table_affine_ddl_phase",
            capture_phase,
        )

        _apply_constraints_parallel(
            [constraint_file],
            target_dsn={"dbname": "test"},
            n_workers=8,
            run_dir=tmp_path,
        )

        assert [label for label, _blocks, _kwargs in phases] == [
            "PK/UQ phase",
            "FK NOT VALID + CHECK phase",
            "VALIDATE CONSTRAINT phase",
        ]
        assert [block.replace("\r\n", "\n") for block in phases[0][1]] == [
            pk_block
        ]
        assert phases[0][2]["n_workers"] == 4
        assert phases[0][2]["setup_sql"] == _index_build_tuning(4)
        assert len(phases[1][1]) == 2
        assert phases[1][2]["n_workers"] == 8
        assert all("NOT VALID" in block for block in phases[1][1])
        assert len(phases[2][1]) == 2
        assert phases[2][2]["n_workers"] == 8
        assert all("VALIDATE CONSTRAINT" in block for block in phases[2][1])


class TestPostgresMaintenance:
    @pytest.mark.parametrize(
        ("load_workers", "requested", "expected"),
        [
            (1, 0, 1),
            (8, 0, 2),
            (8, 1, 1),
            (8, 4, 4),
        ],
    )
    def test_resolves_bounded_auto_workers(
        self,
        load_workers: int,
        requested: int,
        expected: int,
    ) -> None:
        assert _resolve_maintenance_workers(load_workers, requested) == expected

    @pytest.mark.parametrize("requested", [-1, 65])
    def test_rejects_unsafe_worker_counts(self, requested: int) -> None:
        with pytest.raises(PostgresImportError, match="maintenance_workers"):
            _resolve_maintenance_workers(8, requested)

    def test_lists_user_tables_largest_first_and_quotes_names(self) -> None:
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, statement):
                assert "FROM pg_class" in statement
                assert "c.relisshared = false" in statement
                assert "pg_toast" in statement

            @staticmethod
            def fetchall():
                return [
                    ("public", "sales", 2048),
                    ('odd"schema', 'table"name', 1024),
                ]

        class Connection:
            @staticmethod
            def cursor():
                return Cursor()

        assert _list_vacuum_targets(Connection()) == [
            ('"public"."sales"', "public.sales", 2048),
            ('"odd""schema"."table""name"', 'odd"schema.table"name', 1024),
        ]

    def test_parallel_table_vacuum_preserves_full_semantics(
        self,
        monkeypatch,
    ) -> None:
        executed = []
        observed = {}
        spinner_details = []
        targets = [
            ('"public"."sales"', "public.sales", 2048),
            ('"public"."orders"', "public.orders", 1024),
            ('"public"."products"', "public.products", 512),
        ]

        class Cursor:
            def execute(self, statement):
                executed.append(statement)

        def fake_parallel(items, work_fn, *, n_workers, setup_sql, **_kwargs):
            observed["items"] = list(items)
            observed["workers"] = n_workers
            observed["setup"] = setup_sql
            cursor = Cursor()
            for item in items:
                work_fn(cursor, item)

        def fake_spinner(label, work, *, detail=None):
            observed["label"] = label
            spinner_details.append(detail())
            result = work()
            spinner_details.append(detail())
            return result

        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_parallel_in_workers",
            fake_parallel,
        )
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_with_spinner",
            fake_spinner,
        )

        _vacuum_freeze_analyze_tables(
            target_dsn={"dbname": "test"},
            targets=targets,
            maintenance_workers=2,
            load_workers=8,
        )

        assert observed == {
            "items": targets,
            "workers": 2,
            "setup": [
                "SET synchronous_commit = off;",
                "SET maintenance_work_mem = '512MB';",
                "SET max_parallel_maintenance_workers = 0;",
            ],
            "label": "Vacuum + Freeze + Analyze",
        }
        assert executed == [
            f"VACUUM (FREEZE, ANALYZE, PARALLEL 0) ONLY {target[0]};"
            for target in targets
        ]
        assert spinner_details == [
            " (0/3 tables, 2 workers)",
            " (3/3 tables, 2 workers)",
        ]

    def test_single_table_worker_retains_index_parallelism(self, monkeypatch) -> None:
        observed = {}

        def fake_parallel(items, work_fn, *, n_workers, setup_sql, **_kwargs):
            observed["workers"] = n_workers
            observed["setup"] = setup_sql

            class Cursor:
                def execute(self, statement):
                    observed["statement"] = statement

            work_fn(Cursor(), items[0])

        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_parallel_in_workers",
            fake_parallel,
        )
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_with_spinner",
            lambda _label, work, **_kwargs: work(),
        )

        _vacuum_freeze_analyze_tables(
            target_dsn={"dbname": "test"},
            targets=[('"public"."sales"', "public.sales", 2048)],
            maintenance_workers=1,
            load_workers=8,
        )

        assert observed == {
            "workers": 1,
            "setup": [
                "SET synchronous_commit = off;",
                "SET maintenance_work_mem = '1024MB';",
                "SET max_parallel_maintenance_workers = 8;",
            ],
            "statement": (
                'VACUUM (FREEZE, ANALYZE, PARALLEL 8) ONLY "public"."sales";'
            ),
        }

    def test_table_vacuum_propagates_worker_failure(self, monkeypatch) -> None:
        def fail_parallel(*_args, **_kwargs):
            raise RuntimeError("vacuum failed")

        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_parallel_in_workers",
            fail_parallel,
        )
        monkeypatch.setattr(
            "src.tools.sql.postgres_import._run_with_spinner",
            lambda _label, work, **_kwargs: work(),
        )

        with pytest.raises(RuntimeError, match="vacuum failed"):
            _vacuum_freeze_analyze_tables(
                target_dsn={"dbname": "test"},
                targets=[('"public"."sales"', "public.sales", 2048)],
                maintenance_workers=2,
                load_workers=8,
            )


class TestExtractTablesFromCreateSql:
    """The shared parser must handle SQL Server brackets AND Postgres quotes."""

    def test_sql_server_bracket_form(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            "CREATE TABLE [dbo].[Customers] (Id INT);\nGO\n"
            "CREATE TABLE [dbo].[Products] (Id INT);\n",
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers", "Products"]

    def test_postgres_double_quoted(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            'CREATE TABLE "dbo"."Customers" (Id INTEGER);\n'
            'CREATE TABLE "dbo"."Products" (Id INTEGER);\n',
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers", "Products"]

    def test_unqualified(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text("CREATE TABLE Customers (Id INT);\n", encoding="utf-8")
        assert _extract_tables_from_create_sql(sql) == ["Customers"]

    def test_if_not_exists_tolerated(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            'CREATE TABLE IF NOT EXISTS "dbo"."Customers" (Id INTEGER);\n',
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers"]

    def test_dedupes_repeats(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            "CREATE TABLE [Customers] (Id INT);\n"
            "CREATE TABLE [Customers] (Id INT);\n",
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers"]

    def test_ignores_create_in_comment(self, tmp_path: Path) -> None:
        sql = tmp_path / "ddl.sql"
        sql.write_text(
            "-- CREATE TABLE [Sales] would go here\n"
            "CREATE TABLE [Customers] (Id INT);\n",
            encoding="utf-8",
        )
        assert _extract_tables_from_create_sql(sql) == ["Customers"]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _extract_tables_from_create_sql(tmp_path / "missing.sql") == []


class TestShortPath:
    def test_relative_to_base(self, tmp_path: Path) -> None:
        sub = tmp_path / "a" / "b" / "c.sql"
        sub.parent.mkdir(parents=True)
        sub.write_text("")
        assert _short_path(sub, base=tmp_path) == "a/b/c.sql"

    def test_fallback_to_name_when_unrelated(self, tmp_path: Path) -> None:
        p = tmp_path / "file.sql"
        p.write_text("")
        # A genuinely unrelated base (sibling subdir) — relative_to fails.
        unrelated = tmp_path / "sibling"
        unrelated.mkdir()
        assert _short_path(p, base=unrelated) == "file.sql"

    def test_no_base(self, tmp_path: Path) -> None:
        p = tmp_path / "file.sql"
        p.write_text("")
        assert _short_path(p) == "file.sql"


class TestOrderedLoadFiles:
    def test_dims_then_facts(self, tmp_path: Path) -> None:
        (tmp_path / "02_copy_facts.sql").write_text("")
        (tmp_path / "01_copy_dims.sql").write_text("")
        ordered = ordered_load_files(tmp_path)
        assert [p.name for p in ordered] == ["01_copy_dims.sql", "02_copy_facts.sql"]

    def test_only_dims(self, tmp_path: Path) -> None:
        (tmp_path / "01_copy_dims.sql").write_text("")
        ordered = ordered_load_files(tmp_path)
        assert [p.name for p in ordered] == ["01_copy_dims.sql"]

    def test_empty_folder(self, tmp_path: Path) -> None:
        assert ordered_load_files(tmp_path) == []

    def test_prepare_finish_not_warned_as_extras(self, tmp_path: Path, capsys) -> None:
        # The recovery-window scripts are handled by the importer, so they are
        # NOT listed as skipped extras and must not appear in the warning.
        (tmp_path / "00_bulk_insert_prepare_load.sql").write_text("")
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        (tmp_path / "02_bulk_insert_facts.sql").write_text("")
        (tmp_path / "99_bulk_insert_finish_load.sql").write_text("")
        ordered = ordered_load_files(tmp_path)
        assert [p.name for p in ordered] == [
            "01_bulk_insert_dims.sql", "02_bulk_insert_facts.sql",
        ]
        out = capsys.readouterr().out
        assert "Extra load scripts present" not in out

    def test_genuine_extra_still_warned(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        (tmp_path / "02_bulk_insert_facts.sql").write_text("")
        (tmp_path / "50_something_unexpected.sql").write_text("")
        ordered_load_files(tmp_path)
        out = capsys.readouterr().out
        assert "Extra load scripts present" in out
        assert "50_something_unexpected.sql" in out


class TestFindRecoveryScripts:
    def test_finds_prepare_and_finish(self, tmp_path: Path) -> None:
        prep = tmp_path / "00_bulk_insert_prepare_load.sql"
        fin = tmp_path / "99_bulk_insert_finish_load.sql"
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        prep.write_text("")
        fin.write_text("")
        assert find_prepare_load_script(tmp_path) == prep
        assert find_finish_load_script(tmp_path) == fin

    def test_absent_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        assert find_prepare_load_script(tmp_path) is None
        assert find_finish_load_script(tmp_path) is None

    def test_unrelated_prepare_finish_named_files_ignored(self, tmp_path: Path, capsys) -> None:
        # Files that merely contain "prepare"/"finish" but not the documented
        # *_prepare_load / *_finish_load suffix must NOT be treated as recovery
        # scripts (else e.g. a facts-load file could run twice), and they are
        # still surfaced normally by ordered_load_files.
        (tmp_path / "01_bulk_insert_dims.sql").write_text("")
        (tmp_path / "02_bulk_insert_facts.sql").write_text("")
        (tmp_path / "03_facts_prepare_index.sql").write_text("")
        (tmp_path / "04_finish_report.sql").write_text("")
        assert find_prepare_load_script(tmp_path) is None
        assert find_finish_load_script(tmp_path) is None
        # The unrelated files are genuine extras and stay in the warning.
        ordered_load_files(tmp_path)
        out = capsys.readouterr().out
        assert "03_facts_prepare_index.sql" in out
        assert "04_finish_report.sql" in out


class TestFindCreateSql:
    def test_matches_suffix(self, tmp_path: Path) -> None:
        a = tmp_path / "01_create_dimensions.sql"
        b = tmp_path / "02_create_facts.sql"
        a.write_text("")
        b.write_text("")
        assert find_create_sql([a, b], "create_dimensions.sql") == a
        assert find_create_sql([a, b], "create_facts.sql") == b

    def test_missing(self, tmp_path: Path) -> None:
        assert find_create_sql([], "create_dimensions.sql") is None


class TestImportPostgresErrors:
    """Errors raised before any DB connection happens (hermetic)."""

    def test_missing_schema_folder_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PostgresImportError, match="schema folder not found"):
            import_postgres(database="x", run_dir=tmp_path)

    def test_empty_schema_folder_raises(self, tmp_path: Path) -> None:
        (tmp_path / "sql" / "postgres" / "schema").mkdir(parents=True)
        with pytest.raises(PostgresImportError, match="No schema scripts found"):
            import_postgres(database="x", run_dir=tmp_path)
