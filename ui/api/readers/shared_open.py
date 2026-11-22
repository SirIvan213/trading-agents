"""Open a file for reading without blocking another process's rename, truncate or delete.

On Windows, a file opened by Python's `open()` is shared for read and write but not for
delete, so while the UI holds it open another process cannot replace it (PermissionError).
For the daily budget, a write that stays blocked is CapReached and stops the day's decisions.
Every file the UI reads is therefore opened with CreateFileW and
FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, then wrapped with
msvcrt.open_osfhandle.

run_day's state writes (tools/atomic_write.py, #213) rename with POSIX semantics on Windows,
which succeeds over this reader. A plain os.replace (MoveFileExW) still fails over ANY open
handle, this one included: atomic_write falls back to it only when the POSIX rename is
unavailable, and then retries for at most 0.5 s. Elsewhere an ordinary open never blocks a
rename.

Read-only: the handle is opened with GENERIC_READ only.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
_NOT_FOUND = (2, 3)  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
_DENIED = (5, 32)  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
    _create_file.restype = wintypes.HANDLE
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL
    _INVALID_HANDLE = ctypes.c_void_p(-1).value


def _windows_error(code: int, path: Path) -> OSError:
    if code in _NOT_FOUND:
        return FileNotFoundError(errno.ENOENT, "file not found", str(path))
    if code in _DENIED:
        return PermissionError(errno.EACCES, "access denied", str(path))
    return OSError(errno.EIO, f"CreateFileW failed (Windows error {code})", str(path))


def open_shared(path: Path | str) -> BinaryIO:
    """Open `path` for binary reading, sharing read, write AND delete with every other
    process (Windows), so a writer's replace, rename, truncate or delete never fails because
    the UI is reading. Raises FileNotFoundError for a missing file, PermissionError when
    access is denied, OSError otherwise."""
    p = Path(path)
    if os.name != "nt":
        return open(p, "rb")  # noqa: SIM115 - the caller closes it
    handle = _create_file(str(p.absolute()), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING,
                          FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == _INVALID_HANDLE:
        raise _windows_error(ctypes.get_last_error(), p)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        _close_handle(handle)
        raise
    try:
        return os.fdopen(fd, "rb")  # closing it closes the fd and the handle
    except Exception:
        os.close(fd)  # the fd owns the handle now
        raise


def read_bytes_shared(path: Path | str) -> bytes:
    """The whole file, read through open_shared."""
    with open_shared(path) as fh:
        return fh.read()
