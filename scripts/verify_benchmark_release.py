"""Verify the repository's approved benchmark registry and dataset package."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.create_release_manifest import verify_release_manifest  # noqa: E402


REGISTRY_SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _contained_path(root: Path, value: Any, field: str) -> tuple[Path | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"{field} must be a non-empty repository-relative path"
    candidate = Path(value)
    if candidate.is_absolute():
        return None, f"{field} must not be absolute"
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, f"{field} escapes the repository root"
    return resolved, None


def verify_benchmark_registry(registry_path: Path, repo_root: Path) -> list[str]:
    """Return validation errors for the approved benchmark, or an empty list."""
    root = repo_root.resolve()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    errors: list[str] = []

    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {registry.get('schema_version')!r}")
    if registry.get("status") != "approved":
        errors.append("benchmark status is not approved")

    dataset, dataset_error = _contained_path(
        root, registry.get("dataset_path"), "dataset_path"
    )
    manifest, manifest_error = _contained_path(
        root, registry.get("manifest_path"), "manifest_path"
    )
    errors.extend(error for error in (dataset_error, manifest_error) if error)
    if dataset is None or manifest is None:
        return errors
    if not dataset.is_dir():
        errors.append(f"dataset folder is missing: {registry.get('dataset_path')}")
    if not manifest.is_file():
        errors.append(f"manifest is missing: {registry.get('manifest_path')}")
    if errors:
        return errors

    expected_hash = registry.get("manifest_sha256")
    actual_hash = _sha256(manifest)
    if expected_hash != actual_hash:
        errors.append(
            f"manifest SHA-256 mismatch: expected {expected_hash}, found {actual_hash}"
        )
        return errors

    errors.extend(verify_release_manifest(dataset, manifest))
    return errors


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the approved ContosoForge benchmark release."
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("release_manifests/current.json"),
        help="Benchmark registry (default: release_manifests/current.json)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root used to resolve registry paths (default: current directory)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    errors = verify_benchmark_registry(args.registry, args.repo_root)
    if errors:
        print(f"Approved benchmark verification failed ({len(errors)} error(s)):")
        for error in errors:
            print(f"- {error}")
        return 1
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    print(
        f"Approved benchmark verified: {registry['release_id']} "
        f"({registry['dataset_path']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
