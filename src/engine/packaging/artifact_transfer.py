"""Safe copy-or-promote helpers for packaging generated artifacts.

Promotion is deliberately conservative: it is only attempted when requested,
the source validates as the expected artifact type, the destination is absent,
and both paths are on the same filesystem.  Any failed precondition or rename
falls back to the historical copy behaviour.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable

import pyarrow.parquet as pq


def _same_filesystem(src: Path, dst_parent: Path) -> bool:
    """Return whether an atomic rename can stay on one filesystem."""
    try:
        return os.stat(src).st_dev == os.stat(dst_parent).st_dev
    except OSError:
        return False


def _valid_file(src: Path) -> bool:
    """Perform a cheap format-level validation before consuming ``src``."""
    try:
        if src.suffix.lower() == ".parquet":
            parquet = pq.ParquetFile(src)
            return parquet.metadata is not None
        if src.suffix.lower() == ".csv":
            with src.open("rb") as handle:
                header = handle.readline(1024 * 1024)
            return bool(header.strip())
        return src.is_file()
    except (OSError, ValueError):
        return False


def _valid_delta_dir(src: Path) -> bool:
    """Validate the transaction-log signature of a Delta table."""
    try:
        log_dir = src / "_delta_log"
        return log_dir.is_dir() and any(log_dir.glob("*.json"))
    except OSError:
        return False


def transfer_file(src: Path, dst: Path, *, promote: bool = False) -> bool:
    """Copy a file, or atomically promote it when all safety checks pass.

    Returns ``True`` when promotion succeeded and ``False`` when a copy was
    performed.  Existing destinations retain the historical overwrite
    behaviour and are never promotion targets.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if (
        promote
        and not dst.exists()
        and _valid_file(src)
        and _same_filesystem(src, dst.parent)
    ):
        try:
            src.replace(dst)
            return True
        except OSError:
            pass

    shutil.copy2(src, dst)
    return False


def transfer_delta_dir(
    src: Path,
    dst: Path,
    *,
    promote: bool = False,
    skip_dirnames: Iterable[str] = (),
) -> bool:
    """Copy or safely promote a Delta table directory.

    Internal scratch directories are removed only on the promotion path.  They
    would be discarded by the normal copy path and by the pipeline's later
    scratch cleanup anyway.
    """
    src = Path(src)
    dst = Path(dst)
    skipped = set(skip_dirnames)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if (
        promote
        and not dst.exists()
        and _valid_delta_dir(src)
        and _same_filesystem(src, dst.parent)
    ):
        try:
            for name in skipped:
                scratch = src / name
                if scratch.is_dir():
                    shutil.rmtree(scratch)
                elif scratch.exists():
                    scratch.unlink()
            src.replace(dst)
            return True
        except OSError:
            pass

    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in skipped:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    return False
