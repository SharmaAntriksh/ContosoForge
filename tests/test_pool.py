"""Process-boundary checks for the shared generation pool."""
from __future__ import annotations

import multiprocessing
import sys

import pytest

from src.utils.pool import PoolRunSpec, iter_imap_unordered


_PARENT_ONLY_VALUE = "pristine"
_INITIALIZED_VALUE = None


def _initialize(value):
    global _INITIALIZED_VALUE
    _INITIALIZED_VALUE = value


def _inspect_worker(task):
    return (
        task,
        _PARENT_ONLY_VALUE,
        _INITIALIZED_VALUE,
        multiprocessing.get_start_method(),
    )


@pytest.mark.parametrize("recycle_after", [None, 1])
def test_workers_do_not_inherit_parent_module_state(monkeypatch, recycle_after):
    # fork inherits this mutation; a clean interpreter imports the original.
    # This catches the Linux-only leak even though Windows already used spawn.
    monkeypatch.setattr(sys.modules[__name__], "_PARENT_ONLY_VALUE", "changed by parent")
    results = list(iter_imap_unordered(
        tasks=[0, 1, 2, 3],
        task_fn=_inspect_worker,
        spec=PoolRunSpec(processes=2, maxtasksperchild=recycle_after),
        initializer=_initialize,
        initargs=("explicit worker input",),
    ))
    assert sorted(results) == [
        (task, "pristine", "explicit worker input", "spawn")
        for task in range(4)
    ]
