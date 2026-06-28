"""Shared memory helpers for numpy arrays across worker processes.

On Windows (spawn-based multiprocessing), each worker gets a full pickle
copy of all data passed via initializer args.  For large numpy arrays
(products, customers, etc.) this wastes GBs of RAM.

This module uses ``multiprocessing.shared_memory`` to place arrays in
OS-level shared memory so all workers access the *same* physical pages
— zero-copy, read-only.

Usage (main process)::

    with SharedArrayGroup() as shm:
        worker_cfg["product_np"] = shm.publish("product_np", product_np)
        # ... launch pool ...
    # shm automatically cleaned up

Usage (worker init)::

    product_np = resolve_array(worker_cfg["product_np"])
    # Returns a read-only numpy view over shared memory
"""
from __future__ import annotations

import atexit
import uuid
from multiprocessing.shared_memory import SharedMemory
from typing import Any, Dict, List, Optional

import numpy as np

# Marker key so workers can distinguish shared-memory descriptors from plain dicts
_SHM_MARKER = "__shm__"

# Only share arrays larger than this (bytes).  Below this threshold the
# pickle overhead is negligible and shared memory bookkeeping isn't worth it.
MIN_SHARE_BYTES = 100_000  # 100 KB

# Keep SharedMemory handles alive in worker processes so they aren't GC'd
# while numpy views still reference the underlying buffer. Keyed by block
# name so resolving the same descriptor twice reuses a single handle
# (instead of leaking a new one per call).
_worker_shm_handles: Dict[str, SharedMemory] = {}


@atexit.register
def _close_worker_shm_handles() -> None:
    """Close (not unlink) worker-side handles at process exit.

    The main process owns unlinking via SharedArrayGroup.cleanup(); workers
    only close their mappings. Closing here keeps the POSIX resource_tracker
    quiet instead of relying solely on interpreter teardown.
    """
    for shm in _worker_shm_handles.values():
        try:
            shm.close()
        except (OSError, BufferError):
            pass
    _worker_shm_handles.clear()


def _is_shareable(v: Any) -> bool:
    """Return True if *v* is a numpy array suitable for shared memory."""
    return (
        isinstance(v, np.ndarray)
        and v.ndim >= 1
        and v.dtype != object
        and v.nbytes >= MIN_SHARE_BYTES
    )


def resolve_array(value: Any) -> Any:
    """If *value* is a shared-memory descriptor, return a read-only numpy view.

    Otherwise return *value* unchanged.  Safe to call on any worker_cfg value.
    """
    if not isinstance(value, dict) or _SHM_MARKER not in value:
        return value

    name = value["name"]
    shm = _worker_shm_handles.get(name)
    if shm is None:
        shm = SharedMemory(name=name, create=False)
        _worker_shm_handles[name] = shm  # cache + prevent GC

    arr = np.ndarray(
        tuple(value["shape"]),
        dtype=np.dtype(value["dtype"]),
        buffer=shm.buf,
    )
    arr.flags.writeable = False
    return arr


class SharedArrayGroup:
    """Manages a group of shared-memory numpy arrays with RAII cleanup.

    Use as a context manager around the multiprocessing pool lifetime::

        with SharedArrayGroup() as shm:
            cfg["big_array"] = shm.publish("big_array", arr)
            ... run pool ...
        # all shared blocks released here
    """

    def __init__(self) -> None:
        self._prefix = f"sdg_{uuid.uuid4().hex[:8]}_"
        self._blocks: List[SharedMemory] = []

    # -----------------------------------------------------------------
    def publish(self, name_hint: str, arr: Optional[np.ndarray]) -> Any:
        """Copy *arr* into shared memory and return a pickle-safe descriptor.

        Returns ``None`` unchanged if *arr* is None.
        Returns the original array unchanged if it's below the size threshold
        or has an unsupported dtype (object arrays).
        """
        if arr is None:
            return None

        if not _is_shareable(arr):
            return arr  # pass through — will be pickled normally

        arr = np.ascontiguousarray(arr)
        shm_name = f"{self._prefix}{name_hint}"

        try:
            shm = SharedMemory(name=shm_name, create=True, size=arr.nbytes)
        except FileExistsError:
            # Clean up stale shared memory from a previous run
            try:
                old = SharedMemory(name=shm_name, create=False)
                old.close()
                old.unlink()
            except FileNotFoundError:
                pass
            shm = SharedMemory(name=shm_name, create=True, size=arr.nbytes)
        # Copy data into the shared block
        view = np.ndarray(arr.shape, dtype=arr.dtype, buffer=shm.buf)
        view[:] = arr

        self._blocks.append(shm)

        return {
            _SHM_MARKER: True,
            "name": shm.name,
            "shape": list(arr.shape),
            "dtype": str(arr.dtype),
        }

    # -----------------------------------------------------------------
    def publish_dict(self, cfg: Dict[str, Any], keys: List[str]) -> None:
        """Publish multiple keys from *cfg* in-place.

        For each key in *keys*, if ``cfg[key]`` is a shareable numpy array
        it is replaced with a shared-memory descriptor.  Other values
        (None, small arrays, non-arrays) are left untouched.
        """
        for k in keys:
            if k in cfg:
                cfg[k] = self.publish(k, cfg[k])

    # -----------------------------------------------------------------
    def cleanup(self) -> None:
        """Close and unlink all shared memory blocks."""
        for shm in self._blocks:
            try:
                shm.close()
            except (OSError, BufferError):
                pass
            try:
                shm.unlink()
            except (OSError, BufferError):
                pass
        self._blocks.clear()

    # -----------------------------------------------------------------
    def publish_jagged(
        self,
        name_hint: str,
        arrays: List[Optional[np.ndarray]],
        dtype: np.dtype = np.int64,
    ) -> Dict[str, Any]:
        """Flatten a list of variable-length arrays into shared memory.

        ``arrays`` is a list where each element is either a 1-D numpy array
        or ``None``.  The data is packed into a single contiguous buffer
        (``data``) plus an ``offsets`` array so that
        ``data[offsets[i]:offsets[i+1]]`` recovers the *i*-th sub-array.
        ``None`` entries produce zero-length slices (adjacent equal offsets).

        Returns a pickle-safe descriptor that :func:`resolve_jagged` can
        reconstruct on the worker side.
        """
        parts: List[np.ndarray] = []
        offsets = np.empty(len(arrays) + 1, dtype=np.int64)
        pos = 0
        for i, a in enumerate(arrays):
            offsets[i] = pos
            if a is not None and len(a) > 0:
                parts.append(np.asarray(a, dtype=dtype))
                pos += len(a)
            # else: None / empty → offset stays at pos
        offsets[len(arrays)] = pos

        if pos > 0:
            data = np.concatenate(parts)
        else:
            data = np.empty(0, dtype=dtype)

        data_desc = self.publish(f"{name_hint}_data", data)
        offsets_desc = self.publish(f"{name_hint}_offsets", offsets)

        return {
            "__jagged__": True,
            "data": data_desc,
            "offsets": offsets_desc,
            "length": len(arrays),
        }

    def __enter__(self) -> "SharedArrayGroup":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()


def resolve_jagged(desc: Dict[str, Any]) -> List[Optional[np.ndarray]]:
    """Reconstruct a jagged list from a descriptor produced by :meth:`publish_jagged`.

    Returns a list of numpy arrays (or ``None`` for zero-length entries).
    """
    data = resolve_array(desc["data"])
    offsets = resolve_array(desc["offsets"])
    length = desc["length"]

    result: List[Optional[np.ndarray]] = [None] * length
    for i in range(length):
        s, e = int(offsets[i]), int(offsets[i + 1])
        if s < e:
            result[i] = data[s:e]
    return result
