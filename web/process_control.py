"""Cross-platform subprocess-group lifecycle helpers for the web layer."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from typing import Any

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def process_group_popen_kwargs() -> dict[str, Any]:
    """Return ``Popen`` options that isolate the child in a process group."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def attach_process_tree(proc: subprocess.Popen) -> bool:
    """Attach a Windows child to a kill-on-close Job Object."""
    if sys.platform != "win32":
        return True
    process_handle = getattr(proc, "_handle", None)
    if process_handle is None:
        return False

    job_handle = _kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        return False

    info = _ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    configured = _kernel32.SetInformationJobObject(
        job_handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    assigned = configured and _kernel32.AssignProcessToJobObject(
        job_handle,
        wintypes.HANDLE(process_handle),
    )
    if not assigned:
        _kernel32.CloseHandle(job_handle)
        return False

    proc._contosoforge_job_handle = job_handle
    return True


def release_process_tree(proc: subprocess.Popen) -> None:
    """Release process-tree resources, killing leftover Job Object children."""
    if sys.platform != "win32":
        return
    job_handle = getattr(proc, "_contosoforge_job_handle", None)
    if job_handle:
        _kernel32.CloseHandle(job_handle)
        proc._contosoforge_job_handle = None


def signal_process_tree(proc: subprocess.Popen) -> bool:
    """Request graceful termination of a process group, best effort."""
    if proc.poll() is not None:
        return False
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        return True
    except (AttributeError, OSError, ProcessLookupError, ValueError):
        try:
            proc.terminate()
            return True
        except (AttributeError, OSError, ProcessLookupError, ValueError):
            return False


def force_kill_process_tree(proc: subprocess.Popen) -> bool:
    """Hard-kill a process and its descendants, best effort."""
    if sys.platform == "win32":
        job_handle = getattr(proc, "_contosoforge_job_handle", None)
        if job_handle and _kernel32.TerminateJobObject(job_handle, 1):
            return True

    if proc.poll() is not None:
        return False

    killed = False
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            killed = result.returncode == 0
        except (AttributeError, OSError, ValueError):
            killed = False
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            killed = True
        except (AttributeError, OSError, ProcessLookupError, ValueError):
            killed = False

    if proc.poll() is None:
        try:
            proc.kill()
            killed = True
        except (AttributeError, OSError, ProcessLookupError, ValueError):
            pass
    return killed


def request_process_tree_stop(
    proc: subprocess.Popen,
    *,
    grace: float,
) -> threading.Timer | None:
    """Signal a process group, then arm a hard tree-kill after ``grace``."""
    managed_job = getattr(proc, "_contosoforge_job_handle", None)
    if proc.poll() is not None and not managed_job:
        return None

    signal_process_tree(proc)

    def force_if_running() -> None:
        if proc.poll() is None or getattr(
            proc,
            "_contosoforge_job_handle",
            None,
        ):
            force_kill_process_tree(proc)

    timer = threading.Timer(max(0.1, grace), force_if_running)
    timer.daemon = True
    timer.start()
    return timer


def cancel_stop_timers(timers: list[threading.Timer]) -> None:
    """Cancel every pending hard-kill timer."""
    for timer in timers:
        timer.cancel()
