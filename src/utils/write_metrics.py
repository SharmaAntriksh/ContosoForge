"""Low-overhead, phase-level write-path telemetry."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.utils.logging_utils import debug


@dataclass(frozen=True)
class ArtifactStats:
    bytes: int = 0
    files: int = 0


def artifact_stats(paths: Iterable[str | os.PathLike]) -> ArtifactStats:
    """Count files and bytes without opening artifact payloads."""
    total_bytes = 0
    total_files = 0
    seen: set[str] = set()

    for raw in paths:
        path = Path(raw)
        try:
            candidates = [path] if path.is_file() else path.rglob("*")
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                key = str(candidate.resolve())
                if key in seen:
                    continue
                seen.add(key)
                total_bytes += candidate.stat().st_size
                total_files += 1
        except (FileNotFoundError, PermissionError, OSError):
            continue

    return ArtifactStats(bytes=total_bytes, files=total_files)


def log_write_metric(
    phase: str,
    *,
    elapsed_seconds: float,
    input_paths: Iterable[str | os.PathLike] = (),
    output_paths: Iterable[str | os.PathLike] = (),
    rows: int | None = None,
    codec: str | None = None,
    detail: str | None = None,
    input_stats: ArtifactStats | None = None,
    output_stats: ArtifactStats | None = None,
) -> None:
    """Record one debug-only summary line for a completed write phase."""
    source = input_stats or artifact_stats(input_paths)
    output = output_stats or artifact_stats(output_paths)
    fields = [
        f"phase={phase}",
        f"elapsed_s={elapsed_seconds:.3f}",
        f"input_bytes={source.bytes}",
        f"output_bytes={output.bytes}",
        f"input_files={source.files}",
        f"output_files={output.files}",
    ]
    if rows is not None:
        fields.append(f"rows={int(rows)}")
    if codec:
        fields.append(f"codec={codec}")
    if detail:
        fields.append(f"detail={detail}")
    debug("Write metric | " + " ".join(fields))
