"""RAM-aware chunk-size sizing for the sales fact.

Estimates the process-tree peak memory during sales generation and shrinks
``chunk_size`` to fit available RAM. The ctypes (Windows) and ``src.defaults``
imports stay FUNCTION-LOCAL (as in the original) to keep import cheap and avoid
touching Windows-only ctypes at import time.
"""
from __future__ import annotations

import os


def _available_phys_bytes() -> int | None:
    """Best-effort available physical RAM in bytes (cross-platform).

    Returns None if it cannot be queried, in which case callers skip the cap.
    """
    try:
        if os.name == "nt":
            import ctypes

            class _MEMSTATEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = _MEMSTATEX(dwLength=ctypes.sizeof(_MEMSTATEX))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullAvailPhys)
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, AttributeError, ValueError):
        return None


def _sales_memory_model_bytes() -> tuple[int, int, int]:
    """Return (parent_base, worker_base, bytes_per_row) of the sales RAM model.

    Single source of the calibrated constants (in ``src.defaults``) so the peak
    projection and the tune-path cap below stay exact inverses by construction:
    the tree peak is modeled as
    ``parent_base + workers * (worker_base + chunk_size * bytes_per_row)``.
    See ``scripts/measure_sales_memory.py`` for how the constants were measured.
    """
    from src.defaults import (
        SALES_PARENT_BASE_MB, SALES_WORKER_BASE_MB, SALES_INFLIGHT_BYTES_PER_ROW,
    )
    return (
        SALES_PARENT_BASE_MB * 1024 * 1024,
        SALES_WORKER_BASE_MB * 1024 * 1024,
        SALES_INFLIGHT_BYTES_PER_ROW,
    )


def _projected_peak_chunk_bytes(chunk_size: int, n_workers: int) -> int:
    """Estimate peak resident bytes of the whole process tree during sales.

    Calibrated against measured peak RSS of real runs (see
    ``scripts/measure_sales_memory.py``). The tree peak is a fixed coordinator
    baseline plus, per worker, an import/shared-dimension baseline and one
    in-flight chunk's row-level Arrow table (with pricing/returns transients):

        parent_base + workers * (worker_base + chunk_size * bytes_per_row)
    """
    parent, worker_base, bytes_per_row = _sales_memory_model_bytes()
    w = max(1, int(n_workers))
    inflight = int(chunk_size) * bytes_per_row
    return int(parent + w * (worker_base + inflight))


def _cap_chunk_size_by_ram(chunk_size: int, n_workers: int) -> int:
    """Shrink chunk_size so the projected process-tree peak fits in RAM.

    Inverts :func:`_projected_peak_chunk_bytes` for a target budget of 75% of
    available physical memory. Only used on the auto-tune path
    (``tune_chunk=True``), where chunk_size is already derived from the
    machine's worker count; pinned chunk_size values are never silently changed
    (determinism), only warned about.
    """
    avail = _available_phys_bytes()
    if not avail:
        return int(chunk_size)
    parent, worker_base, bytes_per_row = _sales_memory_model_bytes()
    budget = avail * 0.75  # leave headroom for OS + page cache
    w = max(1, int(n_workers))
    # Memory left for in-flight chunks after fixed baselines.
    room = budget - parent - w * worker_base
    if room <= 0:
        return 50_000  # baselines alone exhaust the budget; use the floor
    max_rows = int(room / (w * bytes_per_row))
    return max(50_000, min(int(chunk_size), max_rows))
