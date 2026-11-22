"""Atomic replace-a-file writes for run_day's state (#213).

Every temp-file + replace writer of run_day's state uses `atomic_write_text`: the daily
budget (pipeline/budget.py), the heartbeat (pipeline/run_day.py) and the AV/FMP earnings
caches (pipeline/events.py). A reader never sees half a file: the text goes to a temp file in
the same folder, which then replaces the target in one step.

Why not plain os.replace: on Windows, os.replace (MoveFileExW) onto a file another process
holds open fails with PermissionError, even when that handle shares delete. For the budget a
failed write is CapReached, which stops the day's decisions. So on Windows:
  1. the replace uses POSIX semantics (SetFileInformationByHandle, FileRenameInfoEx,
     FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS). It succeeds over a
     reader that shares read, write and delete (the UI's open_shared);
  2. if that call is unsupported (an old Windows, a FAT volume) or fails, os.replace is tried;
  3. a PermissionError (a reader that does not share delete: antivirus, the indexer, an
     editor, Python's own open) is retried with short sleeps for up to RETRY_TOTAL_S in total.
Elsewhere it is plain os.replace, with the same bounded retry (it rarely triggers there:
only a PermissionError from the folder's permissions, where retrying is harmless).

After the retries the error is raised to the caller, which decides as before: the budget
fails closed (CapReached), the heartbeat and the caches stay best effort. A failed write
never leaves its temp file behind.
"""
from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

RETRY_TOTAL_S = 0.5  # the most time a write spends retrying a PermissionError
RETRY_SLEEP_S = 0.02
WINDOWS = os.name == "nt"

FILE_RENAME_INFO_EX = 22  # FILE_INFO_BY_HANDLE_CLASS.FileRenameInfoEx (Windows 10 1607+)
FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x1
FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x2
_DELETE = 0x00010000
_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
    _create_file.restype = wintypes.HANDLE
    _set_info = _kernel32.SetFileInformationByHandle
    _set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _set_info.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL
    _INVALID_HANDLE = ctypes.c_void_p(-1).value


def posix_replace(src: Path | str, dst: Path | str) -> bool:
    """Windows only: rename `src` over `dst` with POSIX semantics. True if it was done;
    False if the call is unavailable or failed (the caller then falls back to os.replace).
    Never raises."""
    if not WINDOWS:
        return False
    try:
        name = os.path.abspath(dst)
        units = len(name.encode("utf-16-le")) // 2  # UTF-16 code units (surrogates count 2)

        class _RenameInfo(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                        ("FileNameLength", wintypes.DWORD),
                        ("FileName", wintypes.WCHAR * (units + 1))]

        handle = _create_file(os.path.abspath(src), _DELETE, _SHARE_ALL, None,
                              _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
        if handle is None or handle == _INVALID_HANDLE:
            return False
        try:
            info = _RenameInfo(
                FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS, None,
                units * ctypes.sizeof(wintypes.WCHAR), name)
            return bool(_set_info(handle, FILE_RENAME_INFO_EX, ctypes.byref(info),
                                  ctypes.sizeof(info)))
        finally:
            _close_handle(handle)
    except Exception:  # noqa: BLE001 - any failure here falls back to os.replace
        return False


def replace_file(src: Path | str, dst: Path | str) -> None:
    """Replace `dst` with `src` in one step: POSIX semantics on Windows, falling back to
    os.replace; os.replace elsewhere. Raises OSError (PermissionError while another process
    blocks it) and does not retry: see atomic_write_text."""
    if WINDOWS and posix_replace(src, dst):
        return
    os.replace(src, dst)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass  # nothing more can be done; never hide the original error


def atomic_write_text(
    path: Path | str, text: str, *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Write `text` (UTF-8) to `path` atomically: a temp file in the same folder, then
    replace_file. A PermissionError from the replace is retried every RETRY_SLEEP_S for up to
    RETRY_TOTAL_S in total; any other error is not retried. Raises the last OSError when the
    write cannot be done, and the temp file is removed whatever happens. The folder must
    exist. Returns None."""
    target = Path(path)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        deadline = monotonic() + RETRY_TOTAL_S
        while True:
            try:
                replace_file(tmp, target)
                return
            except PermissionError:
                if monotonic() + RETRY_SLEEP_S > deadline:
                    raise
                sleep(RETRY_SLEEP_S)
    except BaseException:
        _remove(tmp)
        raise
