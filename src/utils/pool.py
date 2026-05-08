"""Generic multiprocessing pool utilities.

Provides ``PoolRunSpec`` (frozen dataclass describing pool configuration) and
``iter_imap_unordered`` (high-level runner that yields results in completion
order with optional per-task timeout). Built on
``concurrent.futures.ProcessPoolExecutor``.

These are layer-agnostic — used by both dimension generators and fact
generators for parallel chunk processing.
"""
from __future__ import annotations

import os
import signal
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple


def _worker_init_wrapper(user_init, user_args):
    """Wrapper that ignores SIGINT in worker processes (Unix only; Windows
    inherits the ignore from the main process via _suppress_ctrl_c context)."""
    if os.name != "nt":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    if user_init is not None:
        user_init(*user_args)


class _suppress_ctrl_c:
    """Context manager: ignore CTRL_C in the main process while the pool
    is being created so that spawned workers inherit the ignore flag.

    On Windows, child processes inherit the console ctrl handler state from
    the parent at spawn time. If a stray CTRL_C_EVENT arrives during spawn
    (e.g. VS Code venv activation), it kills workers mid-import. By
    temporarily ignoring CTRL_C in the parent, spawned children are born
    with the ignore flag and survive the race.

    The main process re-enables CTRL_C after pool creation.
    """

    def __enter__(self):
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
                self._nt = True
            except (AttributeError, OSError):
                self._nt = False
        else:
            self._nt = False
            self._old = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        return self

    def __exit__(self, *exc):
        if self._nt:
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
            except (AttributeError, OSError):
                pass
        else:
            signal.signal(signal.SIGINT, self._old)


@dataclass(frozen=True)
class PoolRunSpec:
    processes: int
    maxtasksperchild: Optional[int] = None   # forwarded to ProcessPoolExecutor.max_tasks_per_child
    timeout_s: Optional[float] = None        # per-task timeout (None disables)
    poll_interval_s: float = 0.05            # cadence for timeout polling
    label: str = ""


_SENTINEL = object()


def iter_imap_unordered(
    *,
    tasks: Sequence[Any],
    task_fn: Callable[[Any], Any],
    spec: PoolRunSpec,
    initializer: Optional[Callable[..., Any]] = None,
    initargs: Tuple[Any, ...] = (),
) -> Iterator[Any]:
    """
    Generic multiprocessing runner.

    - 'tasks' must be pickleable (dict/tuple/list of primitives is ideal)
    - 'task_fn' and 'initializer' must be top-level functions (importable) for Windows spawn
    - Yields results in completion order (unordered).

    Submission uses a bounded sliding window of ``2 × processes`` so that at
    most that many serialised payloads sit in the IPC queue at any time. This
    caps memory and avoids overwhelming the executor's queue-management thread.

    When ``spec.timeout_s`` is set, each in-flight task must complete within
    that many seconds (measured from its submission); otherwise a RuntimeError
    is raised and the executor is shut down.
    """
    if spec.processes <= 0:
        raise ValueError("spec.processes must be >= 1")

    timeout_s = spec.timeout_s if (spec.timeout_s is not None and spec.timeout_s > 0) else None
    poll = max(spec.poll_interval_s, 0.001)

    executor_kwargs: Dict[str, Any] = {
        "max_workers": spec.processes,
        "initializer": _worker_init_wrapper,
        "initargs": (initializer, initargs),
    }
    if spec.maxtasksperchild is not None:
        executor_kwargs["max_tasks_per_child"] = spec.maxtasksperchild

    # Suppress CTRL_C in main process during executor creation so spawned
    # workers inherit the ignore flag and survive stray console events
    # (e.g. VS Code venv activation on Windows).
    with _suppress_ctrl_c():
        executor = ProcessPoolExecutor(**executor_kwargs)

    window_size = spec.processes * 2
    task_iter = iter(tasks)
    pending: Dict[Any, float] = {}  # future -> submit_time

    def _submit_next() -> bool:
        t = next(task_iter, _SENTINEL)
        if t is _SENTINEL:
            return False
        f = executor.submit(task_fn, t)
        pending[f] = time.monotonic()
        return True

    # Prime the window
    for _ in range(window_size):
        if not _submit_next():
            break

    # When timeouts are enabled we wake periodically to check elapsed times;
    # otherwise we block until any future completes.
    wait_timeout: Optional[float] = poll if timeout_s is not None else None

    try:
        while pending:
            done, _not_done = wait(
                pending.keys(),
                timeout=wait_timeout,
                return_when=FIRST_COMPLETED,
            )

            for fut in done:
                pending.pop(fut, None)
                yield fut.result()  # surfaces worker exceptions
                _submit_next()

            # Timeout scan runs after popping done futures, so it only
            # measures elapsed time on still-running tasks.
            if timeout_s is not None:
                now = time.monotonic()
                for fut, submit_t in pending.items():
                    if (now - submit_t) > timeout_s:
                        tag = f" label={spec.label!r}" if spec.label else ""
                        raise RuntimeError(
                            f"Task timed out after {timeout_s} seconds{tag}"
                        )
    except BaseException:  # intentionally broad — executor must shut down on any failure
        for fut in list(pending):
            fut.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
