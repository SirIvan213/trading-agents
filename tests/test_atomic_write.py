"""tools.atomic_write (#213): every run_day state writer replaces its file atomically, survives
a reader that shares delete, retries a blocked replace for a bounded time, fails closed
exactly as before, and never leaves a temp file behind."""
from __future__ import annotations

import errno
import json
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import tools.atomic_write as aw
from pipeline.budget import CapReached, Caps, DailyBudget
from tools.atomic_write import RETRY_SLEEP_S, RETRY_TOTAL_S, atomic_write_text
from ui.api.readers.shared_open import open_shared

NY = ZoneInfo("America/New_York")
DAY = date(2026, 11, 23)
CAPS = Caps(10, 600_000, 3, 5)
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows sharing modes")
not_windows = pytest.mark.skipif(os.name == "nt", reason="POSIX rename semantics")


def temps(folder):
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


class FakeTime:
    """A clock that only moves when the code sleeps."""

    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


# ---- the helper ----

def test_writes_and_replaces_atomically_with_no_temp_left(tmp_path):
    f = tmp_path / "state.json"
    atomic_write_text(f, '{"a": 1}')
    atomic_write_text(f, '{"a": 2}')
    assert json.loads(f.read_text(encoding="utf-8")) == {"a": 2}
    assert temps(tmp_path) == []


def test_a_transient_permission_error_is_retried(tmp_path, monkeypatch):
    f = tmp_path / "state.json"
    real, calls = aw.replace_file, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(errno.EACCES, "busy")
        real(src, dst)

    monkeypatch.setattr(aw, "replace_file", flaky)
    clock = FakeTime()
    atomic_write_text(f, "ok", sleep=clock.sleep, monotonic=clock.monotonic)
    assert f.read_text(encoding="utf-8") == "ok" and len(calls) == 3
    assert clock.sleeps == [RETRY_SLEEP_S, RETRY_SLEEP_S] and temps(tmp_path) == []


def test_the_retries_are_bounded_and_the_temp_file_is_removed(tmp_path, monkeypatch):
    f = tmp_path / "state.json"
    f.write_text("old", encoding="utf-8")

    def blocked(src, dst):
        raise PermissionError(errno.EACCES, "busy")

    monkeypatch.setattr(aw, "replace_file", blocked)
    clock = FakeTime()
    with pytest.raises(PermissionError):
        atomic_write_text(f, "new", sleep=clock.sleep, monotonic=clock.monotonic)
    assert 1 <= len(clock.sleeps) and sum(clock.sleeps) <= RETRY_TOTAL_S + 1e-9
    assert set(clock.sleeps) == {RETRY_SLEEP_S}
    assert sum(clock.sleeps) > RETRY_TOTAL_S - 2 * RETRY_SLEEP_S  # it used the time it has
    assert f.read_text(encoding="utf-8") == "old" and temps(tmp_path) == []


def test_other_errors_are_not_retried(tmp_path, monkeypatch):
    calls = []

    def broken(src, dst):
        calls.append(1)
        raise OSError(errno.EIO, "disk")

    monkeypatch.setattr(aw, "replace_file", broken)
    clock = FakeTime()
    with pytest.raises(OSError, match="disk"):
        atomic_write_text(tmp_path / "s.json", "x", sleep=clock.sleep,
                          monotonic=clock.monotonic)
    assert calls == [1] and clock.sleeps == [] and temps(tmp_path) == []


def test_a_temp_file_that_cannot_be_written_raises_and_leaves_nothing(tmp_path, monkeypatch):
    from pathlib import Path

    real = Path.write_text

    def half_then_full(self, *a, **k):
        real(self, "partial", encoding="utf-8")
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(Path, "write_text", half_then_full)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(tmp_path / "s.json", "x")
    assert list(tmp_path.iterdir()) == []


def test_a_missing_folder_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        atomic_write_text(tmp_path / "nope" / "s.json", "x")
    assert list(tmp_path.iterdir()) == []


def test_off_windows_it_is_plain_os_replace(tmp_path, monkeypatch):
    """Non-Windows behavior is unchanged: the POSIX-semantics call is never made."""
    monkeypatch.setattr(aw, "WINDOWS", False)

    def never(*a):
        raise AssertionError("posix_replace called off Windows")

    monkeypatch.setattr(aw, "posix_replace", never)
    replaced = []
    real = os.replace
    monkeypatch.setattr(aw.os, "replace", lambda s, d: (replaced.append(1), real(s, d)))
    atomic_write_text(tmp_path / "s.json", "x")
    assert replaced == [1] and (tmp_path / "s.json").read_text(encoding="utf-8") == "x"


def test_posix_replace_is_false_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, "WINDOWS", False)
    assert aw.posix_replace(tmp_path / "a", tmp_path / "b") is False


@windows_only
def test_when_the_posix_call_is_unavailable_os_replace_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, "posix_replace", lambda s, d: False)
    f = tmp_path / "s.json"
    atomic_write_text(f, "x")
    assert f.read_text(encoding="utf-8") == "x" and temps(tmp_path) == []


@windows_only
def test_posix_replace_reports_failure_instead_of_raising(tmp_path):
    assert aw.posix_replace(tmp_path / "missing.tmp", tmp_path / "s.json") is False


@windows_only
def test_the_posix_fast_path_never_calls_os_replace(tmp_path, monkeypatch):
    def never(*a):
        raise AssertionError("os.replace called although the POSIX rename worked")

    monkeypatch.setattr(aw.os, "replace", never)
    f = tmp_path / "s.json"
    f.write_text("old", encoding="utf-8")
    with open_shared(f):
        atomic_write_text(f, "new")
    assert f.read_text(encoding="utf-8") == "new" and temps(tmp_path) == []


@windows_only
def test_a_relative_path_is_replaced_in_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.json").write_text("old", encoding="utf-8")
    with open_shared(tmp_path / "s.json"):
        atomic_write_text("s.json", "new")
    assert (tmp_path / "s.json").read_text(encoding="utf-8") == "new"
    assert temps(tmp_path) == []


@windows_only
def test_a_name_outside_the_bmp_uses_the_posix_rename(tmp_path, monkeypatch):
    """The rename buffer is sized in UTF-16 code units, so a surrogate pair fits."""
    def never(*a):
        raise AssertionError("fell back to os.replace")

    monkeypatch.setattr(aw.os, "replace", never)
    f = tmp_path / "state-\U0001f4c8.json"
    f.write_text("old", encoding="utf-8")
    with open_shared(f):
        atomic_write_text(f, "new")
    assert f.read_text(encoding="utf-8") == "new" and temps(tmp_path) == []


@windows_only
def test_an_invalid_handle_falls_back_to_os_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, "_create_file", lambda *a: aw._INVALID_HANDLE)
    assert aw.posix_replace(tmp_path / "a", tmp_path / "b") is False
    f = tmp_path / "s.json"
    atomic_write_text(f, "x")  # os.replace does it (no reader holds the file)
    assert f.read_text(encoding="utf-8") == "x" and temps(tmp_path) == []


@not_windows
def test_os_replace_over_an_open_reader_works_elsewhere(tmp_path):
    f = tmp_path / "s.json"
    f.write_text("old", encoding="utf-8")
    with f.open("rb"):
        atomic_write_text(f, "new")
    assert f.read_text(encoding="utf-8") == "new"


# ---- Windows: the readers the UI and other programs hold open ----

@windows_only
def test_the_write_succeeds_while_a_share_delete_reader_holds_the_file(tmp_path):
    f = tmp_path / "budget.json"
    f.write_text("old", encoding="utf-8")
    with open_shared(f) as fh:
        atomic_write_text(f, "new")
        assert fh.read() == b"old"  # the reader keeps its snapshot
    assert f.read_text(encoding="utf-8") == "new" and temps(tmp_path) == []


@windows_only
def test_an_ordinary_open_blocks_the_write_for_a_bounded_time(tmp_path):
    f = tmp_path / "budget.json"
    f.write_text("old", encoding="utf-8")
    with f.open("rb"):
        started = time.monotonic()
        with pytest.raises(PermissionError):
            atomic_write_text(f, "new")
        took = time.monotonic() - started
    assert RETRY_TOTAL_S * 0.8 <= took < RETRY_TOTAL_S + 1.5
    assert f.read_text(encoding="utf-8") == "old" and temps(tmp_path) == []


# ---- the callers: fail closed exactly as before ----

def test_the_budget_counts_through_the_helper(tmp_path):
    b = DailyBudget(CAPS, DAY, tmp_path)
    b.open_day(started_today=False)
    assert b.start_decision("TSLA") == 1
    assert b.snapshot()["decisions"] == 1 and temps(tmp_path) == []


@windows_only
def test_the_budget_writes_while_the_ui_reads(tmp_path):
    b = DailyBudget(CAPS, DAY, tmp_path)
    b.open_day(started_today=False)
    with open_shared(b.path):
        assert b.start_decision("TSLA") == 1
        b.settle_tokens(b.reserve_tokens(1000), 400)
    s = b.snapshot()
    assert s["decisions"] == 1 and s["tokens_used"] == 400 and temps(tmp_path) == []


@windows_only
def test_a_blocked_budget_write_still_fails_closed(tmp_path):
    """After the bounded retries: CapReached("could not be written"), the count not taken,
    no temp file left, and nothing else changed."""
    b = DailyBudget(CAPS, DAY, tmp_path)
    b.open_day(started_today=False)
    before = b.path.read_bytes()
    with b.path.open("rb"):  # antivirus, the indexer, an editor
        with pytest.raises(CapReached, match="daily budget file could not be written"):
            b.start_decision("TSLA")
    assert b.path.read_bytes() == before and temps(tmp_path) == []
    assert b.snapshot()["decisions"] == 0  # the failed write took nothing
    assert b.start_decision("TSLA") == 1  # once the reader is gone, writes work again


def test_a_failed_budget_write_is_cap_reached_on_every_platform(tmp_path, monkeypatch):
    b = DailyBudget(CAPS, DAY, tmp_path)
    b.open_day(started_today=False)

    def blocked(src, dst):
        raise PermissionError(errno.EACCES, "busy")

    monkeypatch.setattr(aw, "replace_file", blocked)
    monkeypatch.setattr(aw, "RETRY_TOTAL_S", 0.05)
    with pytest.raises(CapReached, match="could not be written"):
        b.start_decision("TSLA")
    with pytest.raises(CapReached, match="could not be written"):
        DailyBudget(CAPS, date(2026, 11, 24), tmp_path).open_day(started_today=False)
    assert temps(tmp_path) == []


def heartbeat_day(tmp_path):
    import pipeline.run_day as rd

    return rd.Day(config=None, data=None, clients=None, journal=None,
                  clock=lambda: datetime(2026, 11, 23, 10, 0, tzinfo=NY),
                  sleep=lambda s: None, terminal=None, state_dir=tmp_path, phase="scan")


def test_the_heartbeat_stays_best_effort_and_leaves_no_temp(tmp_path, monkeypatch):
    day = heartbeat_day(tmp_path)

    def blocked(src, dst):
        raise PermissionError(errno.EACCES, "busy")

    monkeypatch.setattr(aw, "replace_file", blocked)
    monkeypatch.setattr(aw, "RETRY_TOTAL_S", 0.05)
    day.beat()  # never raises
    assert not day.heartbeat_path.exists() and temps(tmp_path) == []
    monkeypatch.undo()
    day.beat("scan")
    assert json.loads(day.heartbeat_path.read_text(encoding="utf-8"))["phase"] == "scan"


@windows_only
def test_a_heartbeat_blocked_by_an_ordinary_open_returns_within_the_bound(tmp_path):
    day = heartbeat_day(tmp_path)
    day.beat("one")
    with day.heartbeat_path.open("rb"):
        started = time.monotonic()
        day.beat("two")  # never raises
        took = time.monotonic() - started
    assert took < RETRY_TOTAL_S + 1.5 and temps(tmp_path) == []
    assert json.loads(day.heartbeat_path.read_text(encoding="utf-8"))["phase"] == "one"


@windows_only
def test_the_heartbeat_writes_while_the_ui_reads(tmp_path):
    day = heartbeat_day(tmp_path)
    day.beat("one")
    with open_shared(day.heartbeat_path):
        day.beat("two")
    assert json.loads(day.heartbeat_path.read_text(encoding="utf-8"))["phase"] == "two"
    assert temps(tmp_path) == []


CACHE_DAY, CACHE_END = date(2026, 11, 9), date(2026, 11, 23)


def calendar_cache(tmp_path, fetch):
    from pipeline.events import FmpCalendarCache

    return FmpCalendarCache(tmp_path, fetch,
                            lambda: datetime(2026, 11, 9, 8, 30, tzinfo=NY))


def test_a_failed_cache_write_leaves_no_temp_and_still_returns(tmp_path, monkeypatch):
    from tests.test_phase5_gate_d import BG, Fetch

    def blocked(src, dst):
        raise PermissionError(errno.EACCES, "busy")

    monkeypatch.setattr(aw, "replace_file", blocked)
    monkeypatch.setattr(aw, "RETRY_TOTAL_S", 0.05)
    record = calendar_cache(tmp_path, Fetch(BG)).get(CACHE_DAY, CACHE_END)
    assert record["from"] == "2026-11-09" and temps(tmp_path) == []
    assert list(tmp_path.iterdir()) == []  # nothing cached, nothing left behind


@windows_only
def test_the_cache_writes_while_a_share_delete_reader_holds_it(tmp_path):
    """A stale cache file held open by a share-delete reader is still replaced."""
    from tests.test_phase5_gate_d import BG, Fetch

    path = tmp_path / "fmp-earnings-2026-11-09.json"
    path.write_text("{}", encoding="utf-8")  # unusable: the cache fetches and rewrites it
    fetch = Fetch(BG)
    with open_shared(path) as fh:
        calendar_cache(tmp_path, fetch).get(CACHE_DAY, CACHE_END)
        assert fh.read() == b"{}"
    assert json.loads(path.read_text(encoding="utf-8"))["from"] == "2026-11-09"
    assert len(fetch.calls) == 1 and temps(tmp_path) == []
    calendar_cache(tmp_path, fetch).get(CACHE_DAY, CACHE_END)
    assert len(fetch.calls) == 1  # the rewritten file is used: no second (billed) fetch


def test_every_state_writer_uses_the_helper():
    """No run_day state writer keeps its own temp file + os.replace (#213)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("pipeline/budget.py", "pipeline/run_day.py", "pipeline/events.py"):
        src = open(os.path.join(root, rel), encoding="utf-8").read()
        assert "os.replace(" not in src and "atomic_write_text(" in src, rel
