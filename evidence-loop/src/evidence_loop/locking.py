"""Reentrant, cross-process serialization of one file-backed workflow."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import threading
import time

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
_local = threading.local()


@contextmanager
def store_lock(root: Path, timeout: float = 5):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(root))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        depths = getattr(_local, "depths", {})
        _local.depths = depths
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        path = root / ".workflow.lock"
        deadline = time.monotonic() + timeout
        handle = path.open("a+b")
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError("workflow is locked by another writer; no state was changed")
                time.sleep(0.05)
        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            # Keep the inode stable for waiting processes. The OS releases the
            # advisory lock even if a writer crashes; no stale-PID bypass.


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with store_lock(self.root):
            return method(self, *args, **kwargs)
    return call
