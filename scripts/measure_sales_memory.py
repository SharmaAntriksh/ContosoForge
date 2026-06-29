"""Measure peak RSS of the sales pipeline and check it against the model.

The sales RAM warning and the tune-path chunk cap in ``src/facts/sales/sales.py``
predict the peak resident set of the whole process tree as:

    parent_base + workers * (worker_base + chunk_size * bytes_per_row)

with constants in ``src.defaults`` (SALES_PARENT_BASE_MB, SALES_WORKER_BASE_MB,
SALES_INFLIGHT_BYTES_PER_ROW). This script re-derives those constants: it runs
``main.py --only sales`` for a sweep of (chunk_size, workers), samples the peak
RSS of the parent + all worker processes, and prints measured-vs-predicted so
the calibration can be checked or refreshed on a different machine.

Requires ``psutil`` and an already-generated set of dimensions (run the full
pipeline once, or ``main.py --only dimensions``). The sales runs need enough
total rows to keep every worker busy for several chunks (≈ chunk*workers*3).

Usage:
    python scripts/measure_sales_memory.py                 # default sweep
    python scripts/measure_sales_memory.py 1000000 5       # single point
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

try:
    import psutil
except ImportError:
    sys.exit("psutil is required: pip install psutil")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GB = 1024 ** 3


def _predict(chunk: int, workers: int) -> float:
    """Model prediction in GB, using the live constants from src.defaults."""
    sys.path.insert(0, REPO)
    from src.defaults import (
        SALES_PARENT_BASE_MB, SALES_WORKER_BASE_MB, SALES_INFLIGHT_BYTES_PER_ROW,
    )
    w = max(1, workers)
    parent = SALES_PARENT_BASE_MB * 1024 * 1024
    worker_base = SALES_WORKER_BASE_MB * 1024 * 1024
    inflight = chunk * SALES_INFLIGHT_BYTES_PER_ROW
    return (parent + w * (worker_base + inflight)) / GB


def measure(chunk: int, workers: int, rows: int, interval_s: float = 0.08) -> dict:
    cmd = [
        sys.executable, "main.py", "--only", "sales",
        "--format", "parquet",
        "--sales-rows", str(rows),
        "--workers", str(workers),
        "--chunk-size", str(chunk),
    ]
    proc = subprocess.Popen(cmd, cwd=REPO,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = psutil.Process(proc.pid)
    peak_tree = 0.0
    t0 = time.time()
    while proc.poll() is None:
        try:
            procs = [parent] + parent.children(recursive=True)
            rss = 0
            for p in procs:
                try:
                    rss += p.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            peak_tree = max(peak_tree, rss / GB)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        time.sleep(interval_s)
    rc = proc.wait()
    return {
        "chunk": chunk, "workers": workers, "rows": rows, "rc": rc,
        "dur": time.time() - t0, "measured_gb": peak_tree,
        "predicted_gb": _predict(chunk, workers),
    }


def _print(r: dict) -> None:
    ratio = (r["predicted_gb"] / r["measured_gb"]) if r["measured_gb"] else float("nan")
    flag = "" if r["rc"] == 0 else f"  [rc={r['rc']} — run did not complete]"
    print(f"chunk={r['chunk']:>9,} workers={r['workers']} rows={r['rows']:>11,}  "
          f"measured={r['measured_gb']:5.2f} GB  predicted={r['predicted_gb']:5.2f} GB  "
          f"safety={ratio:4.2f}x  ({r['dur']:.0f}s){flag}", flush=True)


def main() -> None:
    vm = psutil.virtual_memory()
    print(f"System: total={vm.total/GB:.1f} GB, available={vm.available/GB:.1f} GB\n", flush=True)

    if len(sys.argv) >= 3:
        chunk, workers = int(sys.argv[1]), int(sys.argv[2])
        rows = int(sys.argv[3]) if len(sys.argv) > 3 else chunk * workers * 3
        _print(measure(chunk, workers, rows))
        return

    # (chunk_size, workers); rows sized to keep workers busy for a few chunks.
    sweep = [
        (500_000, 2), (1_000_000, 2), (1_000_000, 4),
        (1_000_000, 6), (2_000_000, 1), (4_000_000, 1),
    ]
    for chunk, workers in sweep:
        _print(measure(chunk, workers, chunk * workers * 3))


if __name__ == "__main__":
    main()
