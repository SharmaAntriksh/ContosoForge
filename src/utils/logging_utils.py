from __future__ import annotations

import os
import sys
import time
import atexit
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


# ============================================================================
# PROJECT ROOT + PATH SHORTENING
# ============================================================================
# Kept identical semantics: project root is the top-level folder.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def short_path(p: Any) -> Any:
    """
    If p is a path under PROJECT_ROOT, return a relative path string.
    Otherwise return the last component (name), matching previous behavior.
    """
    if not p:
        return p
    try:
        pth = Path(p)
    except TypeError:
        return p

    try:
        return str(pth.relative_to(PROJECT_ROOT))
    except (ValueError, TypeError):
        # Preserve previous fallback behavior: only last component
        return pth.name


def _shorten_path_in_msg(msg: Any) -> Any:
    """
    Backward-compatible behavior:
    - If message ends with a path after ': ', shorten that trailing path.
    - Otherwise return msg unchanged.
    """
    if not isinstance(msg, str):
        return msg

    head, sep, tail = msg.rpartition(": ")
    if sep and (("\\" in tail) or ("/" in tail)):
        shortened = short_path(tail)
        return f"{head}{sep}{shortened}"

    return msg


# ============================================================================
# CONFIG (kept names; logic improved)
# ============================================================================
ENABLE_COLORS = True          # Console colors (effective only if terminal supports)
ENABLE_FILE_LOG = False       # Save logs to file
LOG_FILE = "logs/generator.log"
LOG_VERBOSITY = "normal"      # "normal" (all messages) or "quiet" (top-level + warnings only)

COLORS = {
    "INFO":  "\033[94m",   # Blue
    "WORK":  "\033[93m",   # Yellow
    "DONE":  "\033[92m",   # Green
    "SKIP":  "\033[90m",   # Grey
    "WARN":  "\033[95m",   # Magenta
    "FAIL":  "\033[91m",   # Red
    "RESET": "\033[0m",
}

PIPELINE_START_TIME = time.time()


# ============================================================================
# INTERNAL STATE
# ============================================================================
_PRINT_LOCK = threading.Lock()
_LOG_FD_LOCK = threading.Lock()

_LOG_DIR_READY = False
_LOG_FD: Optional[int] = None
_LOG_FILE_PATH: Optional[Path] = None

_INDENT = threading.local()
_INDENT_STR = "  "
_LAZY_STACK = threading.local()


def _indent_level() -> int:
    """Current nesting depth (0 = top level)."""
    return getattr(_INDENT, "level", 0)


def _indent_prefix() -> str:
    """Whitespace prefix for the current nesting depth."""
    return _INDENT_STR * _indent_level()


def _activate_pending() -> None:
    """Flush deferred INFO headers for all pending lazy stages (outermost first)."""
    stack = getattr(_LAZY_STACK, "stack", [])
    for s in stack:
        if s._lazy and not s._activated:
            saved = _indent_level()
            _INDENT.level = s._depth
            line = _line("INFO", s.msg)
            if _should_emit("INFO", depth=s._depth):
                _flush(line)
            else:
                _write_to_file(line)
            _INDENT.level = saved
            s._activated = True
            for buffered_line in s._skip_buffer:
                if _should_emit("SKIP", depth=s._depth):
                    _flush(buffered_line)
                else:
                    _write_to_file(buffered_line)
            s._skip_buffer.clear()


def _is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


def _should_emit(level: str, depth: Optional[int] = None) -> bool:
    """Return True if a message at *level* and *depth* should print to console.

    In ``quiet`` mode only top-level messages (depth 0) and WARN/FAIL are shown.
    ``normal`` mode shows everything.
    """
    if LOG_VERBOSITY == "normal":
        return True
    # quiet mode: always show warnings and failures
    if level in ("WARN", "FAIL"):
        return True
    d = depth if depth is not None else _indent_level()
    return d == 0


def _color_allowed() -> bool:
    """
    Colors are used if:
      - ENABLE_COLORS is True
      - and stdout is a TTY
      - and NO_COLOR isn't set
    """
    if not ENABLE_COLORS:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return _is_tty()


def configure_logging(
    *,
    enable_colors: Optional[bool] = None,
    enable_file_log: Optional[bool] = None,
    log_file: Optional[str] = None,
    verbosity: Optional[str] = None,
) -> None:
    """
    Optional runtime configuration without touching import sites.

    *verbosity* accepts ``"normal"`` (default, all messages) or ``"quiet"``
    (only top-level stage messages, warnings, and failures).
    """
    global ENABLE_COLORS, ENABLE_FILE_LOG, LOG_FILE, LOG_VERBOSITY
    global _LOG_FILE_PATH, _LOG_DIR_READY, _LOG_FD

    if enable_colors is not None:
        ENABLE_COLORS = bool(enable_colors)

    if enable_file_log is not None:
        ENABLE_FILE_LOG = bool(enable_file_log)

    if log_file is not None:
        LOG_FILE = str(log_file)
        # Guard FD state with the same lock _ensure_log_file_open() uses, so
        # reconfiguring after worker threads start can't race _write_to_file.
        with _LOG_FD_LOCK:
            _LOG_FILE_PATH = None
            _LOG_DIR_READY = False
            if _LOG_FD is not None:
                try:
                    os.close(_LOG_FD)
                except OSError:
                    pass
                _LOG_FD = None

    if verbosity is not None:
        LOG_VERBOSITY = verbosity


def fmt_sec(sec: float) -> str:
    """Return a clean human-readable time string."""
    if sec < 1:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    return str(timedelta(seconds=int(sec)))


def human_duration(seconds: float) -> str:
    """Backward-compatible alias."""
    return fmt_sec(seconds)


def pipeline_elapsed() -> str:
    """Elapsed time since module import (pipeline start)."""
    return fmt_sec(time.time() - PIPELINE_START_TIME)


def _format_level(level: str) -> str:
    if _color_allowed():
        color = COLORS.get(level, "")
        reset = COLORS["RESET"]
        return f"{color}{level:<4}{reset}"
    return f"{level:<4}"


def _line(level: str, msg: Any) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    return f"{ts} | {_format_level(level)} | {_indent_prefix()}{msg}"


def _ensure_log_file_open() -> None:
    """
    Lazily open a process-local FD in append mode.
    Keeps overhead low vs open/close on every log line.
    """
    global _LOG_DIR_READY, _LOG_FD, _LOG_FILE_PATH

    if not ENABLE_FILE_LOG:
        return

    with _LOG_FD_LOCK:
        if _LOG_FILE_PATH is None:
            _LOG_FILE_PATH = Path(LOG_FILE)

        if not _LOG_DIR_READY:
            # Avoid repeated mkdir calls
            parent = _LOG_FILE_PATH.parent
            if str(parent) and str(parent) != ".":
                parent.mkdir(parents=True, exist_ok=True)
            _LOG_DIR_READY = True

        if _LOG_FD is None:
            # O_APPEND ensures each write is appended (best-effort atomicity per process)
            _LOG_FD = os.open(str(_LOG_FILE_PATH), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)


@atexit.register
def _close_log_fd() -> None:
    global _LOG_FD
    if _LOG_FD is not None:
        try:
            os.close(_LOG_FD)
        except OSError:
            pass
        _LOG_FD = None


def _write_to_file(line: str) -> None:
    if not ENABLE_FILE_LOG:
        return
    _ensure_log_file_open()
    if _LOG_FD is None:
        return
    try:
        os.write(_LOG_FD, (line + "\n").encode("utf-8", errors="replace"))
    except OSError:
        # File logging should never crash the pipeline
        pass


def _flush(line: str) -> None:
    """
    Prints to stdout safely for multiprocessing (flush) and reduces interleaving
    within the same process via a lock. Multiprocess interleaving is still possible
    (expected), but each call flushes promptly.
    """
    with _PRINT_LOCK:
        print(line, flush=True)
        _write_to_file(line)


# ============================================================================
# BASIC LOG LEVEL FUNCTIONS (same names)
# ============================================================================
def info(msg: Any) -> None:
    msg = _shorten_path_in_msg(msg)
    line = _line("INFO", msg)
    if _should_emit("INFO"):
        _activate_pending()
        _flush(line)
    else:
        _write_to_file(line)


def debug(msg: Any) -> None:
    """Log to file only (suppressed from console output)."""
    _write_to_file(_line("DEBUG", msg))


def warn(msg: Any) -> None:
    _activate_pending()
    _flush(_line("WARN", msg))


def fail(msg: Any) -> None:
    _activate_pending()
    _flush(_line("FAIL", msg))


def skip(msg: Any) -> None:
    line = _line("SKIP", msg)
    if not _should_emit("SKIP"):
        _write_to_file(line)
        return
    stack = getattr(_LAZY_STACK, "stack", [])
    for s in reversed(stack):
        if s._lazy and not s._activated:
            s._skip_buffer.append(line)
            return
    _activate_pending()
    _flush(line)


def done(msg: Any) -> None:
    line = _line("DONE", msg)
    if _should_emit("DONE"):
        _flush(line)
    else:
        _write_to_file(line)


# ============================================================================
# STAGE CONTEXT MANAGER (Auto-timed) – improved exception logging
# ============================================================================
class stage:
    """
    Usage:
        with stage("Generating Dates"):
            ...
        with stage("Generating Products", lazy=True):
            ...   # INFO header deferred until first nested log message

    Behavior:
      - Logs INFO at enter (or defers if lazy=True)
      - Logs DONE on successful exit with timing
      - lazy mode: if no nested messages fired, emits SKIP instead of DONE
      - Logs FAIL on exception with timing, then re-raises
    """
    def __init__(self, msg: str, *, lazy: bool = False):
        self.msg = msg
        self._lazy = lazy
        self._activated = not lazy
        self._depth = 0
        self._skip_buffer: list = []
        self.start: Optional[float] = None

    def __enter__(self) -> "stage":
        self.start = time.time()
        self._depth = _indent_level()
        if not self._lazy:
            info(self.msg)
        stack = getattr(_LAZY_STACK, "stack", None)
        if stack is None:
            _LAZY_STACK.stack = []
            stack = _LAZY_STACK.stack
        stack.append(self)
        _INDENT.level = self._depth + 1
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        _INDENT.level = self._depth
        stack = getattr(_LAZY_STACK, "stack", [])
        if stack and stack[-1] is self:
            stack.pop()
        elapsed = fmt_sec(time.time() - (self.start or time.time()))
        if exc_type is not None:
            if not self._activated:
                _flush(_line("INFO", self.msg))
            fail(f"{self.msg} failed in {elapsed}: {exc_type.__name__}: {exc}")
            return False
        if self._activated:
            done(f"{self.msg} completed in {elapsed}")
        else:
            self._skip_buffer.clear()
            skip_label = self.msg.removeprefix("Generating ").removeprefix("Updating ")
            skip(f"{skip_label} up-to-date")
        return False


# ============================================================================
# ENHANCED WORK LOGGER (For Multiprocessing Workers) – same signature
# ============================================================================
def work(msg: str = "", *, outfile: Any = None, **_ignore: Any) -> None:
    """
    WORK logger (multiprocessing-safe).

    - Message is authoritative
    - If msg is empty and outfile is provided, log outfile name
    - Keeps previous visible format (including arrow) for compatibility
    """
    if msg:
        final = msg
    elif outfile:
        try:
            final = f"-> {Path(outfile).name}"
        except TypeError:
            final = f"-> {outfile}"
    else:
        final = ""

    line = _line("WORK", final)
    if _should_emit("WORK"):
        _activate_pending()
        _flush(line)
    else:
        _write_to_file(line)
