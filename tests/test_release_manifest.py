from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.create_release_manifest import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)


def _dataset(root: Path) -> Path:
    release = root / "release"
    for folder in ("dimensions", "facts", "config"):
        (release / folder).mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"CustomerKey": [1, 2]}).to_parquet(
        release / "dimensions" / "customers.parquet", index=False
    )
    pd.DataFrame({"OrderNumber": [10, 11, 12]}).to_parquet(
        release / "facts" / "sales.parquet", index=False
    )
    (release / "config" / "config.yaml").write_text("sales: {}\n", encoding="utf-8")
    return release


def test_manifest_is_portable_complete_and_reproducible(tmp_path: Path) -> None:
    release = _dataset(tmp_path)
    output = release / "release_manifest.json"

    first = write_release_manifest(
        release,
        output,
        generator_commit="abc123",
        validation_commit="def456",
    )
    first_bytes = output.read_bytes()
    second = write_release_manifest(
        release,
        output,
        generator_commit="abc123",
        validation_commit="def456",
    )

    assert first == second
    assert output.read_bytes() == first_bytes
    assert first["dataset"] == "release"
    assert first["summary"] == {
        "artifact_count": 3,
        "total_bytes": sum(
            path.stat().st_size
            for path in release.rglob("*")
            if path.is_file() and path != output
        ),
        "dimension_tables": 1,
        "fact_tables": 1,
    }
    assert all(not Path(item["path"]).is_absolute() for item in first["artifacts"])
    assert all(item["path"] != "release_manifest.json" for item in first["artifacts"])

    sales = next(item for item in first["artifacts"] if item["path"] == "facts/sales.parquet")
    assert sales["parquet"] == {"rows": 3, "columns": 1, "row_groups": 1}
    config = next(item for item in first["artifacts"] if item["path"] == "config/config.yaml")
    config_path = release / "config" / "config.yaml"
    assert config["sha256"] == hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert json.loads(output.read_text(encoding="utf-8")) == first
    assert verify_release_manifest(release, output) == []

    (release / "config" / "config.yaml").write_text("sales:\n  changed: true\n")
    differences = verify_release_manifest(release, output)
    assert "changed artifact: config/config.yaml" in differences
    assert "release summary differs" in differences


def test_manifest_requires_packaged_dataset_structure(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing dimensions"):
        build_release_manifest(tmp_path)
