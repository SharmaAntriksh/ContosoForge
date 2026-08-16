from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.create_release_manifest import write_release_manifest
from scripts.verify_benchmark_release import verify_benchmark_registry


def _approved_benchmark(root: Path) -> tuple[Path, Path, Path]:
    dataset = root / "generated_datasets" / "approved"
    for folder in ("dimensions", "facts", "config"):
        (dataset / folder).mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"CustomerKey": [1]}).to_parquet(
        dataset / "dimensions" / "customers.parquet", index=False
    )
    pd.DataFrame({"OrderNumber": [10]}).to_parquet(
        dataset / "facts" / "sales.parquet", index=False
    )
    (dataset / "config" / "config.yaml").write_text("sales: {}\n")

    manifest = root / "release_manifests" / "approved.json"
    write_release_manifest(dataset, manifest)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    registry = root / "release_manifests" / "current.json"
    registry.write_text(json.dumps({
        "schema_version": 1,
        "status": "approved",
        "release_id": "approved",
        "dataset_path": "generated_datasets/approved",
        "manifest_path": "release_manifests/approved.json",
        "manifest_sha256": manifest_hash,
    }))
    return dataset, manifest, registry


def test_approved_benchmark_verifies_end_to_end(tmp_path: Path) -> None:
    _, _, registry = _approved_benchmark(tmp_path)
    assert verify_benchmark_registry(registry, tmp_path) == []


def test_registry_detects_manifest_and_dataset_changes(tmp_path: Path) -> None:
    dataset, manifest, registry = _approved_benchmark(tmp_path)
    manifest.write_text("{}")
    errors = verify_benchmark_registry(registry, tmp_path)
    assert any("manifest SHA-256 mismatch" in error for error in errors)

    _, manifest, registry = _approved_benchmark(tmp_path)
    (dataset / "config" / "config.yaml").write_text("changed: true\n")
    errors = verify_benchmark_registry(registry, tmp_path)
    assert "changed artifact: config/config.yaml" in errors


def test_registry_rejects_paths_outside_repository(tmp_path: Path) -> None:
    _, _, registry = _approved_benchmark(tmp_path)
    payload = json.loads(registry.read_text())
    payload["dataset_path"] = "../outside"
    registry.write_text(json.dumps(payload))
    assert "dataset_path escapes the repository root" in verify_benchmark_registry(
        registry, tmp_path
    )
