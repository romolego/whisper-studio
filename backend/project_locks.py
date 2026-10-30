"""Coordinate project directory moves, deletion and artifact I/O in this process."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from weakref import WeakValueDictionary

_guard = threading.Lock()
_locks: WeakValueDictionary = WeakValueDictionary()


@contextmanager
def project_lock(project_id: str):
    with _guard:
        lock = _locks.get(project_id)
        if lock is None:
            lock = threading.RLock()
            _locks[project_id] = lock
    with lock:
        yield
