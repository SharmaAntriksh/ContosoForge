"""Run destructive release checks in an isolated copy of the committed tree.

The validator deliberately uses ``git archive HEAD`` instead of the working
tree.  Generation can therefore write dimension versions, scratch files, and
packaged datasets without touching a developer's local config or data.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FORMATS = ("csv", "parquet", "deltaparquet")
_PREVIEW_CHECK = r"""
import json
import sys
from urllib.parse import quote

from fastapi.testclient import TestClient
from web.api import app

folder = sys.argv[1]
results = []
with TestClient(app) as client:
    for prefix in ("/api", "/v1/api"):
        tables_response = client.get(
            f"{prefix}/datasets/{quote(folder, safe='')}/tables"
        )
        tables_response.raise_for_status()
        tables = tables_response.json()["tables"]
        table = next(
            (item for item in tables if item["category"] == "facts"),
            tables[0],
        )
        base = (
            f"{prefix}/datasets/{quote(folder, safe='')}/tables/"
            f"{quote(table['name'], safe='')}/preview"
        )
        first = client.get(base, params={"offset": 0, "limit": 5})
        first.raise_for_status()
        first_body = first.json()
        last_offset = max(0, first_body["total_rows"] - 5)
        last = client.get(base, params={"offset": last_offset, "limit": 5})
        last.raise_for_status()
        last_body = last.json()
        if not first_body["rows"] or not last_body["rows"]:
            raise RuntimeError(f"Empty preview for {prefix} {table['name']}")
        results.append(
            {
                "prefix": prefix,
                "table": table["name"],
                "total_rows": first_body["total_rows"],
                "first_rows": len(first_body["rows"]),
                "last_rows": len(last_body["rows"]),
            }
        )
print(json.dumps(results))
"""


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    log_path: Path,
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    elapsed = time.perf_counter() - started
    log_path.write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        tail = "\n".join(result.stdout.splitlines()[-40:])
        raise RuntimeError(
            f"{' '.join(command)} failed with exit code {result.returncode} "
            f"after {elapsed:.1f}s.\n\nLast output:\n{tail}"
        )
    return result


def _archive_head(destination: Path) -> Path:
    archive_path = destination / "contosoforge-head.tar"
    result = subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive_path}", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git archive failed:\n{result.stdout}")

    checkout = destination / "checkout"
    checkout.mkdir()
    with tarfile.open(archive_path) as archive:
        archive.extractall(checkout, filter="data")
    archive_path.unlink()
    return checkout


def _small_config(source: Path, file_format: str) -> dict[str, Any]:
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["scale"] = {
        "sales_rows": 500,
        "products": {"catalog": "synthetic", "rows": 100},
        "customers": 80,
        "stores": 8,
        "promotions": {
            "seasonal": 1,
            "clearance": 1,
            "limited": 1,
            "flash": 1,
            "volume": 1,
            "loyalty": 1,
            "bundle": 1,
            "new_customer": 1,
        },
    }

    defaults = raw.setdefault("defaults", {})
    defaults["final_output"] = "./generated_datasets"
    defaults["dates"] = {"start": "2024-01-01", "end": "2024-03-31"}

    sales = raw.setdefault("sales", {})
    sales.update(
        {
            "file_format": file_format,
            "coverage_policy": "repair",
            "quality_report": False,
            "parquet_folder": f"./release_validation/{file_format}/dimensions",
            "out_folder": f"./release_validation/{file_format}/scratch",
            "delta_output_folder": (
                f"./release_validation/{file_format}/scratch/delta"
            ),
        }
    )
    sales["advanced"] = {
        "chunk_size": 250,
        "workers": 1,
        "row_group_size": 250,
        "compression": "snappy",
    }
    sales["partition_by"] = ["Year", "Month"]

    raw.setdefault("products", {}).setdefault("scd2", {})["enabled"] = False
    raw.setdefault("customers", {}).setdefault("scd2", {})["enabled"] = False
    raw.setdefault("stores", {})["online_stores"] = 1
    raw.setdefault("stores", {}).setdefault("closing", {})["enabled"] = False
    raw.setdefault("employees", {}).setdefault("transfers", {})["enabled"] = False
    raw.setdefault("dates", {}).setdefault("include", {}).setdefault(
        "weekly_fiscal", {}
    )["enabled"] = False

    for section in (
        "returns",
        "wishlists",
        "complaints",
        "subscriptions",
        "budget",
        "inventory",
    ):
        raw.setdefault(section, {})["enabled"] = False

    raw["packaging"] = {
        "reset_scratch_fact_out": True,
        "clean_scratch_fact_out": True,
    }
    return raw


def _newest_packaged_run(root: Path, file_format: str) -> Path:
    format_tag = file_format.upper()
    runs = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and path.name.upper().endswith(format_tag)
        ),
        reverse=True,
    )
    if not runs:
        raise RuntimeError(f"No packaged dataset was created under {root}")
    return runs[0]


def _validate_previews(
    checkout: Path,
    package: Path,
    *,
    timeout: int,
    log_path: Path,
) -> list[dict[str, Any]]:
    result = _run(
        [sys.executable, "-c", _PREVIEW_CHECK, package.name],
        cwd=checkout,
        timeout=min(timeout, 120),
        log_path=log_path,
    )
    output_lines = [
        line for line in result.stdout.splitlines() if line.strip().startswith("[")
    ]
    if not output_lines:
        raise RuntimeError("Preview validation returned no result")
    return json.loads(output_lines[-1])


def _validate_package(package: Path, file_format: str) -> dict[str, Any]:
    files = [path for path in package.rglob("*") if path.is_file()]
    suffix_counts: dict[str, int] = {}
    for path in files:
        suffix = path.suffix.lower() or "<none>"
        suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1

    required = ("config.yaml", "models.yaml")
    missing = [name for name in required if not (package / "config" / name).exists()]
    if missing:
        raise RuntimeError(f"{package} is missing packaged inputs: {missing}")

    if file_format == "csv":
        if suffix_counts.get(".csv", 0) == 0:
            raise RuntimeError("CSV validation run produced no CSV files")
        if suffix_counts.get(".sql", 0) == 0:
            raise RuntimeError("CSV validation run produced no SQL bundle")
    elif file_format == "parquet":
        if suffix_counts.get(".parquet", 0) == 0:
            raise RuntimeError("Parquet validation run produced no Parquet files")
    else:
        delta_logs = list(package.rglob("_delta_log"))
        if not delta_logs:
            raise RuntimeError("Delta validation run produced no Delta transaction log")

    return {
        "format": file_format,
        "package": str(package),
        "file_count": len(files),
        "suffix_counts": dict(sorted(suffix_counts.items())),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate small release datasets from an isolated archive of HEAD."
        )
    )
    parser.add_argument(
        "--format",
        dest="formats",
        action="append",
        choices=DEFAULT_FORMATS,
        help="Format to validate; repeat the flag. Defaults to all three.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Per-format generation timeout in seconds (default: 900).",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the isolated checkout and generated datasets for inspection.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    formats = tuple(args.formats or DEFAULT_FORMATS)
    temp_root = Path(tempfile.mkdtemp(prefix="contosoforge-release-"))

    try:
        checkout = _archive_head(temp_root)
        report: dict[str, Any] = {
            "commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
            ).strip(),
            "python": sys.version.split()[0],
            "isolated_checkout": str(checkout),
            "runs": [],
        }

        for file_format in formats:
            config_path = checkout / f"release-{file_format}.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    _small_config(checkout / "config.yaml", file_format),
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            log_path = temp_root / f"{file_format}.log"
            print(f"[release] validating {file_format} in {checkout}", flush=True)

            started = time.perf_counter()
            _run(
                [
                    sys.executable,
                    "main.py",
                    "--config",
                    str(config_path),
                    "--models-config",
                    str(checkout / "models.yaml"),
                    "--dry-run",
                    "--quiet",
                ],
                cwd=checkout,
                timeout=min(args.timeout, 120),
                log_path=temp_root / f"{file_format}-dry-run.log",
            )
            _run(
                [
                    sys.executable,
                    "main.py",
                    "--config",
                    str(config_path),
                    "--models-config",
                    str(checkout / "models.yaml"),
                    "--no-report",
                    "--quiet",
                ],
                cwd=checkout,
                timeout=args.timeout,
                log_path=log_path,
            )
            packaged_root = checkout / "generated_datasets"
            package = _newest_packaged_run(packaged_root, file_format)
            result = _validate_package(
                package,
                file_format,
            )
            result["previews"] = _validate_previews(
                checkout,
                package,
                timeout=args.timeout,
                log_path=temp_root / f"{file_format}-preview.log",
            )
            result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
            result["log"] = str(log_path)
            report["runs"].append(result)
            print(
                f"[release] {file_format} passed in "
                f"{result['elapsed_seconds']:.2f}s",
                flush=True,
            )

        report_path = temp_root / "release-validation.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        if args.keep:
            print(f"[release] kept validation workspace at {temp_root}")
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"[release] FAILED: {exc}", file=sys.stderr)
        if args.keep:
            print(f"[release] kept failed workspace at {temp_root}", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
