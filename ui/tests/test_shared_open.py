"""open_shared shares read, write and delete (Windows), so the UI never blocks the journal's
appends, journal.repair's truncate, a delete, or a POSIX-semantics rename onto the file.

What it does NOT make safe: os.replace (MoveFileExW) onto a file any process holds open fails
on Windows even when the handle shares delete. That is why run_day's state writes
(tools/atomic_write.py, #213) rename with POSIX semantics on Windows, and why the UI reads the
budget file only through open_shared.
"""
from __future__ import annotations

import ctypes
import os

import pytest

from ui.api.readers.shared_open import open_shared, read_bytes_shared

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows sharing modes")


def write(path, text):
    path.write_bytes(text.encode())


def test_reads_the_whole_file(tmp_path):
    f = tmp_path / "a.json"
    write(f, '{"x": 1}')
    assert read_bytes_shared(f) == b'{"x": 1}'


def test_a_missing_file_raises_file_not_found_and_creates_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        open_shared(tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError):
        open_shared(tmp_path / "no-dir" / "missing.json")
    assert list(tmp_path.iterdir()) == []


def test_the_handle_is_read_only(tmp_path):
    f = tmp_path / "a.json"
    write(f, "x")
    with open_shared(f) as fh, pytest.raises(OSError):
        fh.write(b"y")
    assert f.read_bytes() == b"x"


def posix_replace(src, dst) -> int:
    """Rename `src` over `dst` with POSIX semantics (SetFileInformationByHandle,
    FileRenameInfoEx, REPLACE_IF_EXISTS | POSIX_SEMANTICS), the call tools/atomic_write.py
    makes (#213).
    Returns 0 on success, else the Windows error code."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    name = str(dst)

    class RenameInfo(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.DWORD),
                    ("FileName", wintypes.WCHAR * (len(name) + 1))]

    handle = k32.CreateFileW(str(src), 0x00010000, 7, None, 3, 0x80, None)  # DELETE access
    assert handle not in (None, ctypes.c_void_p(-1).value)
    info = RenameInfo(0x1 | 0x2, None, len(name) * 2, name)
    ok = k32.SetFileInformationByHandle(handle, 22, ctypes.byref(info), ctypes.sizeof(info))
    err = 0 if ok else ctypes.get_last_error()
    k32.CloseHandle(handle)
    return err


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename semantics")
def test_os_replace_succeeds_while_the_ui_holds_the_file_open_posix(tmp_path):
    f, tmp = tmp_path / "budget.json", tmp_path / "budget.json.tmp"
    write(f, "old")
    write(tmp, "new")
    with open_shared(f) as fh:
        os.replace(tmp, f)
        assert fh.read() == b"old"
    assert f.read_bytes() == b"new"


@windows_only
def test_os_replace_still_fails_on_windows_while_the_ui_holds_the_file_open(tmp_path):
    """Why run_day's state writes do not rely on os.replace (#213): MoveFileExW cannot
    replace a file with an open handle, even one that shares delete."""
    f, tmp = tmp_path / "budget.json", tmp_path / "budget.json.tmp"
    write(f, "old")
    write(tmp, "new")
    with open_shared(f), pytest.raises(PermissionError):
        os.replace(tmp, f)


@windows_only
def test_a_posix_rename_succeeds_while_the_ui_holds_the_file_open(tmp_path):
    """The rename tools/atomic_write.py uses (#213): it succeeds over an open_shared reader,
    which keeps its snapshot."""
    f, tmp = tmp_path / "budget.json", tmp_path / "budget.json.tmp"
    write(f, "old")
    write(tmp, "new")
    with open_shared(f) as fh:
        assert posix_replace(tmp, f) == 0
        assert fh.read() == b"old"
    assert f.read_bytes() == b"new" and not tmp.exists()


@windows_only
def test_a_posix_rename_fails_over_a_default_open(tmp_path):
    """Why tools/atomic_write.py also retries (#213), and why the UI never reads with a
    plain open(): a reader that does not share delete (antivirus, the indexer, an editor,
    Python's own open) still blocks the rename (sharing violation)."""
    f, tmp = tmp_path / "budget.json", tmp_path / "budget.json.tmp"
    write(f, "old")
    write(tmp, "new")
    with f.open("rb"):
        assert posix_replace(tmp, f) == 32


def test_truncate_and_append_succeed_while_the_ui_holds_the_journal_open(tmp_path):
    """journal.repair truncates in place; Journal.append opens with "a"."""
    f = tmp_path / "decisions.jsonl"
    write(f, '{"a": 1}\n{"b"')
    with open_shared(f):
        with f.open("r+b") as w:
            w.truncate(9)
        with f.open("a", encoding="utf-8") as w:
            w.write('{"c": 3}\n')
    # text-mode append (like Journal.append) writes CRLF on Windows
    assert f.read_bytes().replace(b"\r\n", b"\n") == b'{"a": 1}\n{"c": 3}\n'


@windows_only
def test_a_default_python_open_would_block_the_replace(tmp_path):
    """Why open_shared exists: an ordinary open() makes the writer's replace fail."""
    f, tmp = tmp_path / "budget.json", tmp_path / "budget.json.tmp"
    write(f, "old")
    write(tmp, "new")
    with f.open("rb"), pytest.raises(PermissionError):
        os.replace(tmp, f)


@windows_only
def test_a_directory_is_access_denied(tmp_path):
    with pytest.raises(PermissionError):
        open_shared(tmp_path)


@windows_only
def test_delete_succeeds_while_the_ui_holds_the_file_open(tmp_path):
    f = tmp_path / "heartbeat.json"
    write(f, "x")
    with open_shared(f):
        os.remove(f)
    assert not f.exists()
