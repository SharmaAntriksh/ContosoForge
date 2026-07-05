from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

from src.exceptions import SalesError

# ---------------------------------------------------------------------
# Logging (soft dependency)
# ---------------------------------------------------------------------
try:
    from src.utils.logging_utils import info, skip, done, warn
except ImportError:  # pragma: no cover
    def info(msg: str) -> None:  # type: ignore[misc]
        print(msg)

    def skip(msg: str) -> None:  # type: ignore[misc]
        print(msg)

    def done(msg: str) -> None:  # type: ignore[misc]
        print(msg)

    def warn(msg: str) -> None:  # type: ignore[misc]
        print(f"WARN: {msg}")


PathLike = Union[str, os.PathLike]


def _arrow():
    """
    Lazy Arrow import (keeps CSV-only runs lighter).

    Returns: (pa, pc, pq)
    """
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.compute as pc  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
        return pa, pc, pq
    except ImportError as e:
        raise SalesError("pyarrow is required for Parquet merge/Delta writes") from e


def arrow():
    """Public alias for `_arrow()` (kept for back-compat)."""
    return _arrow()


def _ensure_dir_for_file(path: PathLike) -> None:
    p = Path(os.fspath(path))
    p.parent.mkdir(parents=True, exist_ok=True)


def ensure_dir_for_file(path: str) -> None:
    """Public alias for `_ensure_dir_for_file()` (kept for back-compat)."""
    _ensure_dir_for_file(path)


@contextmanager
def atomic_output(final_path: PathLike, *, suffix: str = ".tmp") -> Iterator[str]:
    """Write to a temp sibling, then atomically ``os.replace`` it onto ``final_path``.

    Yields the temp path to write into. On normal exit the temp is renamed onto
    the final path (atomic on POSIX and Windows). If the body raises, the temp is
    removed and the final path is left untouched — so a mid-write crash never
    ships a truncated-but-plausible file (which the path-probing packager would
    happily pick up). This is the single lifecycle pattern for the writer
    finalizers (parquet merge + optimize).

    The caller MUST close any writer holding the temp open before the block
    exits: on Windows an open file can be neither renamed nor removed.
    """
    final_abs = os.path.abspath(os.fspath(final_path))
    tmp_path = final_abs + suffix
    ok = False
    try:
        yield tmp_path
        ok = True
    finally:
        if not ok and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    os.replace(tmp_path, final_abs)
