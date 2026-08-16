"""Benchmark ContosoForge's packaged output formats with reproducible metadata.

The benchmark runs the normal CLI pipeline once per requested format, using
existing dimensions (``--only sales``), and records wall time, stage timings,
output sizes, environment details, raw logs, and packaged row metadata.

Example::

    .venv/Scripts/python.exe scripts/benchmark_output_modes.py \
        --rows 20000000 --workers 8

Results are written under ``benchmark_results/output_modes/`` by default.
Generated datasets remain in the configured final-output folder so their
contents can be inspected after the benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FORMATS = ("parquet", "csv", "deltaparquet")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{size} B"


def _format_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {sec:.1f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m {sec:.1f}s"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_text(command: list[str]) -> str:
    try:
        return subprocess.check_output(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _system_metadata() -> dict[str, Any]:
    memory_total = None
    physical_cores = None
    try:
        import psutil

        memory_total = int(psutil.virtual_memory().total)
        physical_cores = psutil.cpu_count(logical=False)
    except ImportError:
        pass

    disk = shutil.disk_usage(PROJECT_ROOT)
    config_path = PROJECT_ROOT / "config.yaml"
    models_path = PROJECT_ROOT / "models.yaml"

    return {
        "captured_at": datetime.now().astimezone().isoformat(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_logical": os.cpu_count(),
        "cpu_physical": physical_cores,
        "memory_total_bytes": memory_total,
        "disk_total_bytes": int(disk.total),
        "disk_free_bytes_before": int(disk.free),
        "python": sys.version.replace("\n", " "),
        "python_executable": sys.executable,
        "packages": {
            name: _package_version(name)
            for name in ("numpy", "pandas", "pyarrow", "deltalake", "pydantic")
        },
        "git_commit": _run_text(["git", "rev-parse", "HEAD"]),
        "git_branch": _run_text(["git", "branch", "--show-current"]),
        "git_status": _run_text(["git", "status", "--short"]),
        "config_sha256": _sha256(config_path),
        "models_sha256": _sha256(models_path),
    }


def _warm_dimension_cache(folder: Path) -> dict[str, Any]:
    started = time.perf_counter()
    total_bytes = 0
    files = 0
    for path in sorted(folder.glob("*.parquet")):
        files += 1
        with path.open("rb") as handle:
            while block := handle.read(8 * 1024 * 1024):
                total_bytes += len(block)
    return {
        "folder": str(folder.resolve()),
        "files": files,
        "bytes": total_bytes,
        "elapsed_seconds": time.perf_counter() - started,
    }


def _parse_duration(value: str) -> float | None:
    value = value.strip()
    try:
        if value.endswith("ms"):
            return float(value[:-2]) / 1000.0
        if value.endswith("s") and ":" not in value:
            return float(value[:-1])
        parts = value.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None
    return None


def _stage_timings(lines: Iterable[str]) -> dict[str, dict[str, Any]]:
    timings: dict[str, dict[str, Any]] = {}
    for raw in lines:
        line = ANSI_RE.sub("", raw).strip()
        match = re.search(r"([^|]+?) completed in ([^|]+)$", line)
        if not match:
            continue
        label = match.group(1).strip(" └─│")
        duration = match.group(2).strip()
        timings[label] = {
            "display": duration,
            "seconds": _parse_duration(duration),
        }
    return timings


def _directory_inventory(folder: Path) -> dict[str, Any]:
    files = [path for path in folder.rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)

    categories: dict[str, dict[str, int]] = {}
    for path in files:
        relative = path.relative_to(folder)
        category = relative.parts[0] if len(relative.parts) > 1 else "root"
        entry = categories.setdefault(category, {"bytes": 0, "files": 0})
        entry["bytes"] += path.stat().st_size
        entry["files"] += 1

    largest = sorted(files, key=lambda path: path.stat().st_size, reverse=True)[:15]
    return {
        "total_bytes": total,
        "file_count": len(files),
        "categories": categories,
        "largest_files": [
            {
                "path": str(path.relative_to(folder)),
                "bytes": path.stat().st_size,
            }
            for path in largest
        ],
    }


def _parquet_metadata(path: Path) -> dict[str, Any]:
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    codecs = sorted(
        {
            parquet.metadata.row_group(rg).column(col).compression
            for rg in range(parquet.metadata.num_row_groups)
            for col in range(parquet.metadata.num_columns)
        }
    )
    return {
        "path": str(path),
        "rows": int(parquet.metadata.num_rows),
        "row_groups": int(parquet.metadata.num_row_groups),
        "bytes": path.stat().st_size,
        "compression": codecs,
    }


def _fact_metadata(run_folder: Path, output_format: str) -> dict[str, Any]:
    facts = run_folder / "facts"
    result: dict[str, Any] = {}

    if output_format == "parquet":
        for path in sorted(facts.glob("*.parquet")):
            try:
                result[path.stem] = _parquet_metadata(path)
            except (OSError, ValueError):
                continue
        return result

    if output_format == "deltaparquet":
        try:
            from deltalake import DeltaTable
        except ImportError:
            return result
        for path in sorted(facts.iterdir() if facts.exists() else []):
            if not path.is_dir() or not (path / "_delta_log").is_dir():
                continue
            try:
                table = DeltaTable(str(path))
                files = [Path(unquote(name)) for name in table.file_uris()]
                result[path.name] = {
                    "path": str(path),
                    "rows": int(table.to_pyarrow_dataset().count_rows()),
                    "data_files": len(files),
                    "data_bytes": sum(file.stat().st_size for file in files if file.exists()),
                    "version": int(table.version()),
                }
            except (OSError, ValueError):
                continue
        return result

    for path in sorted(facts.rglob("*.csv")):
        key = str(path.parent.relative_to(facts))
        entry = result.setdefault(key, {"files": 0, "bytes": 0, "paths": []})
        entry["files"] += 1
        entry["bytes"] += path.stat().st_size
        entry["paths"].append(str(path.relative_to(facts)))
    return result


def _find_new_run(before: set[Path], final_root: Path, output_format: str) -> Path:
    after = {path.resolve() for path in final_root.iterdir() if path.is_dir()}
    created = list(after - before)
    expected = output_format.upper()
    matching = [path for path in created if path.name.upper().endswith(expected)]
    candidates = matching or created
    if not candidates:
        raise RuntimeError(f"No new packaged dataset found under {final_root}")
    return max(candidates, key=lambda path: path.stat().st_ctime)


def _benchmark_command(args: argparse.Namespace, output_format: str) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        "--only",
        "sales",
        "--format",
        output_format,
        "--sales-rows",
        str(args.rows),
        "--workers",
        str(args.workers),
        "--chunk-size",
        str(args.chunk_size),
        "--no-report",
    ]
    if output_format != "csv":
        command += ["--row-group-size", str(args.row_group_size)]
    return command


def _run_one(
    args: argparse.Namespace,
    output_format: str,
    results_dir: Path,
    final_root: Path,
) -> dict[str, Any]:
    before = {path.resolve() for path in final_root.iterdir() if path.is_dir()}
    command = _benchmark_command(args, output_format)
    log_path = results_dir / f"{output_format}.log"
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"\n=== Benchmarking {output_format} ===", flush=True)
    print("Command:", subprocess.list2cmdline(command), flush=True)

    lines: list[str] = []
    started_at = datetime.now().astimezone().isoformat()
    started = time.perf_counter()
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            lines.append(line)
        return_code = process.wait()
    elapsed = time.perf_counter() - started

    result: dict[str, Any] = {
        "format": output_format,
        "started_at": started_at,
        "completed_at": datetime.now().astimezone().isoformat(),
        "command": command,
        "return_code": return_code,
        "wall_seconds": elapsed,
        "stage_timings": _stage_timings(lines),
        "log": str(log_path.relative_to(PROJECT_ROOT)),
    }
    if return_code != 0:
        return result

    run_folder = _find_new_run(before, final_root, output_format)
    result["run_folder"] = str(run_folder.relative_to(PROJECT_ROOT))
    result["output"] = _directory_inventory(run_folder)
    result["facts"] = _fact_metadata(run_folder, output_format)
    return result


_COMPARISON_PARAMETER_KEYS = ("rows", "workers", "chunk_size", "row_group_size")
_COMPARISON_ENVIRONMENT_KEYS = ("hostname", "processor", "cpu_logical", "cpu_physical")


def _comparison(current: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any] | None:
    if not baseline:
        return None

    incompatibilities: list[str] = []
    current_params = current.get("parameters", {})
    baseline_params = baseline.get("parameters", {})
    for key in _COMPARISON_PARAMETER_KEYS:
        if current_params.get(key) != baseline_params.get(key):
            incompatibilities.append(
                f"parameter {key}: baseline={baseline_params.get(key)!r}, "
                f"current={current_params.get(key)!r}"
            )

    current_env = current.get("environment", {})
    baseline_env = baseline.get("environment", {})
    for key in ("config_sha256", "models_sha256", *_COMPARISON_ENVIRONMENT_KEYS):
        if current_env.get(key) != baseline_env.get(key):
            incompatibilities.append(
                f"environment {key}: baseline={baseline_env.get(key)!r}, "
                f"current={current_env.get(key)!r}"
            )

    baseline_name = baseline.get("results_dir")
    if incompatibilities:
        return {
            "baseline": baseline_name,
            "valid": False,
            "warning": "Incomparable benchmark baseline: " + "; ".join(incompatibilities),
            "runs": [],
        }

    base_by_format = {item["format"]: item for item in baseline.get("runs", [])}
    comparisons = []
    for item in current.get("runs", []):
        previous = base_by_format.get(item["format"])
        if not previous or not previous.get("wall_seconds"):
            continue
        current_seconds = float(item["wall_seconds"])
        baseline_seconds = float(previous["wall_seconds"])
        comparisons.append(
            {
                "format": item["format"],
                "baseline_seconds": baseline_seconds,
                "current_seconds": current_seconds,
                "change_seconds": current_seconds - baseline_seconds,
                "change_percent": (current_seconds / baseline_seconds - 1.0) * 100.0,
            }
        )
    return {"baseline": baseline_name, "valid": True, "runs": comparisons}


def _write_report(results: dict[str, Any], path: Path) -> None:
    metadata = results["environment"]
    args = results["parameters"]
    lines = [
        "# ContosoForge output-mode benchmark",
        "",
        f"- Captured: {results['completed_at']}",
        f"- Rows requested: {args['rows']:,}",
        f"- Workers: {args['workers']}",
        f"- Chunk size: {args['chunk_size']:,}",
        f"- Row-group size: {args['row_group_size']:,}",
        f"- Format order: {', '.join(args['formats'])}",
        f"- Git commit: `{metadata['git_commit']}`",
        f"- Python: `{platform.python_version()}`",
        f"- CPU: {metadata['processor'] or metadata['platform']}",
        f"- Logical/physical CPUs: {metadata['cpu_logical']}/{metadata['cpu_physical']}",
        f"- Memory: {_format_bytes(metadata['memory_total_bytes']) if metadata['memory_total_bytes'] else 'unknown'}",
        "",
        "## Summary",
        "",
        "| Format | Wall time | Output size | Files | Sales rows | Result |",
        "|---|---:|---:|---:|---:|---|",
    ]

    for run in results["runs"]:
        output = run.get("output", {})
        sales_rows: Any = "n/a"
        facts = run.get("facts", {})
        if run["format"] == "parquet" and "sales" in facts:
            sales_rows = f"{facts['sales']['rows']:,}"
        elif run["format"] == "deltaparquet" and "sales" in facts:
            sales_rows = f"{facts['sales']['rows']:,}"
        status = "ok" if run["return_code"] == 0 else f"failed ({run['return_code']})"
        lines.append(
            "| "
            + " | ".join(
                [
                    run["format"],
                    _format_seconds(run["wall_seconds"]),
                    _format_bytes(output.get("total_bytes", 0)),
                    str(output.get("file_count", 0)),
                    str(sales_rows),
                    status,
                ]
            )
            + " |"
        )

    lines += ["", "## Stage timings", ""]
    labels = []
    for run in results["runs"]:
        for label in run.get("stage_timings", {}):
            if label not in labels:
                labels.append(label)
    if labels:
        lines.append("| Stage | " + " | ".join(run["format"] for run in results["runs"]) + " |")
        lines.append("|---|" + "---:|" * len(results["runs"]))
        for label in labels:
            values = [
                run.get("stage_timings", {}).get(label, {}).get("display", "-")
                for run in results["runs"]
            ]
            lines.append("| " + label + " | " + " | ".join(values) + " |")
    else:
        lines.append("No stage timings were parsed from the logs.")

    lines += ["", "## Output inventory", ""]
    for run in results["runs"]:
        lines += [f"### {run['format']}", ""]
        if run.get("run_folder"):
            lines.append(f"- Dataset: `{run['run_folder']}`")
        lines.append(f"- Raw log: `{run['log']}`")
        for category, values in run.get("output", {}).get("categories", {}).items():
            lines.append(
                f"- {category}: {_format_bytes(values['bytes'])} across {values['files']} files"
            )
        lines.append("")

    comparison = results.get("comparison")
    if comparison and comparison.get("valid") is False:
        lines += ["## Baseline comparison", "", f"**Not comparable:** {comparison['warning']}", ""]
    elif comparison and comparison.get("runs"):
        lines += ["## Baseline comparison", ""]
        lines.append(f"Baseline: `{comparison['baseline']}`")
        lines += ["", "| Format | Baseline | Current | Change |", "|---|---:|---:|---:|"]
        for row in comparison["runs"]:
            lines.append(
                f"| {row['format']} | {_format_seconds(row['baseline_seconds'])} | "
                f"{_format_seconds(row['current_seconds'])} | {row['change_percent']:+.2f}% |"
            )

    lines += [
        "",
        "## Reproduction",
        "",
        "```powershell",
        subprocess.list2cmdline(results["reproduction_command"]),
        "```",
        "",
        "Use `--baseline-json <previous-results.json>` on a later run to add a wall-time comparison.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _save(results: dict[str, Any], results_dir: Path) -> None:
    results["completed_at"] = datetime.now().astimezone().isoformat()
    json_path = results_dir / "results.json"
    json_path.write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_report(results, results_dir / "report.md")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=_positive_int, default=20_000_000)
    parser.add_argument("--workers", type=_positive_int, default=8)
    parser.add_argument("--chunk-size", type=_positive_int, default=1_000_000)
    parser.add_argument("--row-group-size", type=_positive_int, default=1_000_000)
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=DEFAULT_FORMATS,
        default=list(DEFAULT_FORMATS),
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=PROJECT_ROOT / "benchmark_results" / "output_modes",
    )
    parser.add_argument("--baseline-json", type=Path)
    parser.add_argument(
        "--resume-results",
        type=Path,
        help="Append or replace selected format runs in an existing results directory.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue to later formats if one benchmark process fails.",
    )
    return parser


def main() -> int:
    # Windows shells commonly expose cp1252 even when subprocess logs contain
    # UTF-8 symbols. Keep live echo best-effort; the raw UTF-8 log remains
    # authoritative and must never be able to abort a benchmark run.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")

    args = build_parser().parse_args()
    os.chdir(PROJECT_ROOT)

    final_root = (PROJECT_ROOT / "generated_datasets").resolve()
    final_root.mkdir(parents=True, exist_ok=True)

    if args.resume_results:
        results_dir = args.resume_results.resolve()
        results = json.loads((results_dir / "results.json").read_text(encoding="utf-8"))
        expected = results["parameters"]
        actual = {
            "rows": args.rows,
            "workers": args.workers,
            "chunk_size": args.chunk_size,
            "row_group_size": args.row_group_size,
        }
        mismatches = {
            key: (expected.get(key), value)
            for key, value in actual.items()
            if expected.get(key) != value
        }
        if mismatches:
            raise ValueError(f"Resume parameters do not match existing results: {mismatches}")
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_dir = args.results_root.resolve() / f"{timestamp}_{args.rows}_rows"
        results_dir.mkdir(parents=True, exist_ok=False)

        reproduction = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--rows",
            str(args.rows),
            "--workers",
            str(args.workers),
            "--chunk-size",
            str(args.chunk_size),
            "--row-group-size",
            str(args.row_group_size),
            "--formats",
            *args.formats,
        ]
        results = {
            "schema_version": 1,
            "results_dir": str(results_dir.relative_to(PROJECT_ROOT)),
            "started_at": datetime.now().astimezone().isoformat(),
            "completed_at": None,
            "parameters": {
                "rows": args.rows,
                "workers": args.workers,
                "chunk_size": args.chunk_size,
                "row_group_size": args.row_group_size,
                "formats": args.formats,
                "only": "sales",
                "quality_report": False,
            },
            "environment": _system_metadata(),
            "dimension_cache_warmup": _warm_dimension_cache(PROJECT_ROOT / "data" / "parquet_dims"),
            "reproduction_command": reproduction,
            "runs": [],
            "comparison": None,
        }

    baseline = None
    if args.baseline_json:
        baseline = json.loads(args.baseline_json.read_text(encoding="utf-8"))

    _save(results, results_dir)
    for output_format in args.formats:
        results["runs"] = [
            run for run in results["runs"] if run.get("format") != output_format
        ]
        run = _run_one(args, output_format, results_dir, final_root)
        results["runs"].append(run)
        results["comparison"] = _comparison(results, baseline)
        _save(results, results_dir)
        if run["return_code"] != 0 and not args.continue_on_error:
            print(f"Benchmark failed for {output_format}; see {run['log']}", file=sys.stderr)
            return run["return_code"] or 1

    disk = shutil.disk_usage(PROJECT_ROOT)
    results["environment"]["disk_free_bytes_after"] = int(disk.free)
    results["comparison"] = _comparison(results, baseline)
    _save(results, results_dir)

    print(f"\nBenchmark results: {results_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
