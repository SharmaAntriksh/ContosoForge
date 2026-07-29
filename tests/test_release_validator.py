from pathlib import Path

import yaml

from scripts.validate_web_release import (
    _newest_packaged_run,
    _small_config,
    _validate_package,
)


def _write_source_config(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "scale": {},
                "defaults": {"dates": {}},
                "sales": {"advanced": {}},
                "products": {"scd2": {}},
                "customers": {"scd2": {}},
                "stores": {"closing": {}},
                "employees": {"transfers": {}},
                "dates": {"include": {"weekly_fiscal": {}}},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_small_release_config_is_bounded_and_isolated(tmp_path):
    config = _small_config(_write_source_config(tmp_path / "config.yaml"), "parquet")

    assert config["scale"]["sales_rows"] == 500
    assert config["scale"]["customers"] == 80
    assert config["defaults"]["final_output"] == "./generated_datasets"
    assert config["sales"]["parquet_folder"].startswith("./release_validation/")
    assert config["sales"]["advanced"]["workers"] == 1
    assert config["returns"]["enabled"] is False
    assert config["inventory"]["enabled"] is False


def test_small_release_config_sets_requested_format(tmp_path):
    source = _write_source_config(tmp_path / "config.yaml")

    assert _small_config(source, "csv")["sales"]["file_format"] == "csv"
    assert (
        _small_config(source, "deltaparquet")["sales"]["file_format"]
        == "deltaparquet"
    )


def test_newest_packaged_run_filters_by_format(tmp_path):
    (tmp_path / "2026-01-01 01_00_00 AM PARQUET").mkdir()
    expected = tmp_path / "2026-01-01 01_00_01 AM CSV"
    expected.mkdir()
    (tmp_path / "2026-01-01 01_00_02 AM DELTAPARQUET").mkdir()

    assert _newest_packaged_run(tmp_path, "csv") == expected


def test_validate_csv_package_requires_data_sql_and_inputs(tmp_path):
    package = tmp_path / "run"
    (package / "config").mkdir(parents=True)
    (package / "config" / "config.yaml").write_text("defaults: {}", encoding="utf-8")
    (package / "config" / "models.yaml").write_text("models: {}", encoding="utf-8")
    (package / "facts").mkdir()
    (package / "facts" / "sales.csv").write_text("SalesKey\n1\n", encoding="utf-8")
    (package / "sql").mkdir()
    (package / "sql" / "create.sql").write_text("SELECT 1;", encoding="utf-8")

    result = _validate_package(package, "csv")

    assert result["suffix_counts"][".csv"] == 1
    assert result["suffix_counts"][".sql"] == 1


def test_validate_delta_package_recognizes_transaction_log(tmp_path):
    package = tmp_path / "run"
    (package / "config").mkdir(parents=True)
    (package / "config" / "config.yaml").write_text("defaults: {}", encoding="utf-8")
    (package / "config" / "models.yaml").write_text("models: {}", encoding="utf-8")
    (package / "facts" / "sales" / "_delta_log").mkdir(parents=True)
    (package / "facts" / "sales" / "_delta_log" / "00000.json").write_text(
        "{}",
        encoding="utf-8",
    )

    result = _validate_package(package, "deltaparquet")

    assert result["suffix_counts"][".json"] == 1
