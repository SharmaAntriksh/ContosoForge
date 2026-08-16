"""Create a reproducible integrity manifest for a packaged dataset release."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


MANIFEST_SCHEMA_VERSION = 1
_HASH_CHUNK_BYTES = 4 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def build_release_manifest(
    dataset_path: Path,
    *,
    output_path: Path | None = None,
    generator_commit: str | None = None,
    validation_commit: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic manifest for every file in *dataset_path*.

    Paths are release-relative so the manifest is portable. If *output_path* is
    inside the release, it is excluded, allowing the command to be rerun without
    making the manifest hash itself or changing its result.
    """
    root = dataset_path.resolve()
    if not root.is_dir():
        raise ValueError(f"Dataset folder does not exist: {dataset_path}")
    for required in ("dimensions", "facts", "config"):
        if not (root / required).is_dir():
            raise ValueError(f"Packaged dataset is missing {required}/: {root}")

    excluded = output_path.resolve() if output_path is not None else None
    artifacts: list[dict[str, Any]] = []
    table_counts = {"dimensions": 0, "facts": 0}

    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if not path.is_file() or (excluded is not None and path.resolve() == excluded):
            continue
        if path.is_symlink():
            raise ValueError(f"Release contains a symbolic link: {path}")

        relative = path.relative_to(root).as_posix()
        artifact: dict[str, Any] = {
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        if path.suffix.lower() == ".parquet":
            metadata = pq.ParquetFile(path).metadata
            artifact["parquet"] = {
                "rows": metadata.num_rows,
                "columns": metadata.num_columns,
                "row_groups": metadata.num_row_groups,
            }
            area = relative.split("/", 1)[0]
            if area in table_counts:
                table_counts[area] += 1
        artifacts.append(artifact)

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset": root.name,
        "source": {
            "generator_commit": generator_commit,
            "validation_commit": validation_commit,
        },
        "summary": {
            "artifact_count": len(artifacts),
            "total_bytes": sum(item["bytes"] for item in artifacts),
            "dimension_tables": table_counts["dimensions"],
            "fact_tables": table_counts["facts"],
        },
        "artifacts": artifacts,
    }


def write_release_manifest(
    dataset_path: Path,
    output_path: Path,
    *,
    generator_commit: str | None = None,
    validation_commit: str | None = None,
) -> dict[str, Any]:
    manifest = build_release_manifest(
        dataset_path,
        output_path=output_path,
        generator_commit=generator_commit,
        validation_commit=validation_commit,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_release_manifest(dataset_path: Path, manifest_path: Path) -> list[str]:
    """Return human-readable differences between a release and its manifest."""
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = expected.get("source", {})
    actual = build_release_manifest(
        dataset_path,
        output_path=manifest_path,
        generator_commit=source.get("generator_commit"),
        validation_commit=source.get("validation_commit"),
    )

    differences: list[str] = []
    if expected.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        differences.append(
            f"unsupported schema_version: {expected.get('schema_version')!r}"
        )
    if expected.get("dataset") != actual["dataset"]:
        differences.append(
            f"dataset name: expected {expected.get('dataset')!r}, "
            f"found {actual['dataset']!r}"
        )

    expected_files = {item["path"]: item for item in expected.get("artifacts", [])}
    actual_files = {item["path"]: item for item in actual["artifacts"]}
    for path in sorted(expected_files.keys() - actual_files.keys()):
        differences.append(f"missing artifact: {path}")
    for path in sorted(actual_files.keys() - expected_files.keys()):
        differences.append(f"unexpected artifact: {path}")
    for path in sorted(expected_files.keys() & actual_files.keys()):
        if expected_files[path] != actual_files[path]:
            differences.append(f"changed artifact: {path}")

    if expected.get("summary") != actual["summary"]:
        differences.append("release summary differs")
    return differences


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hash and inventory a packaged ContosoForge dataset."
    )
    parser.add_argument("dataset", type=Path, help="Packaged dataset folder")
    parser.add_argument(
        "--output",
        type=Path,
        help="Manifest path (default: <dataset>/release_manifest.json)",
    )
    parser.add_argument(
        "--verify",
        type=Path,
        help="Verify the dataset against an existing manifest instead of writing one",
    )
    parser.add_argument("--generator-commit")
    parser.add_argument("--validation-commit")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.verify is not None:
        differences = verify_release_manifest(args.dataset, args.verify)
        if differences:
            print(f"Release verification failed ({len(differences)} difference(s)):")
            for difference in differences:
                print(f"- {difference}")
            return 1
        print(f"Release verified: {args.dataset} ({args.verify})")
        return 0

    output = args.output or args.dataset / "release_manifest.json"
    manifest = write_release_manifest(
        args.dataset,
        output,
        generator_commit=args.generator_commit,
        validation_commit=args.validation_commit,
    )
    print(
        f"Manifest written: {output} "
        f"({manifest['summary']['artifact_count']} artifacts, "
        f"{manifest['summary']['total_bytes']:,} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
