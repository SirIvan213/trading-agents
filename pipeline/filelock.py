"""A cross-process exclusive file lock (msvcrt on Windows, flock elsewhere).

Used by run_day's daily budget (#157) and its single-instance lock (#156). The same OS
primitives as the journal's lock (journal/log.py), kept separate so the journal's public
interface does not change.
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    """The lock could not be taken in time (another process holds it)."""


def try_lock(fh) -> bool:  # noqa: ANN001 - a binary file object
    """Take an exclusive, non-blocking OS lock on `fh`. True if taken."""
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock(fh) -> None:  # noqa: ANN001
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # closing the file releases the OS lock anyway


@contextmanager
def exclusive_file_lock(path: Path, timeout_s: float = 10.0) -> Iterator[None]:
    """Hold an exclusive lock on `path` (created if missing) for the block. Raises
    LockTimeout if another process holds it for `timeout_s`, OSError if it cannot be opened.
    Not re-entrant: never nest two blocks on the same path in one thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(timeout_s, 0)
    with path.open("a+b") as fh:
        while not try_lock(fh):
            if time.monotonic() >= deadline:
                raise LockTimeout(f"lock {path.name} is held by another process")
            time.sleep(0.01)
        try:
            yield
        finally:
            unlock(fh)
