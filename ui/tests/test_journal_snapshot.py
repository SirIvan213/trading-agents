"""The lock-free journal reader: same entries as the journal's own reader, never a lock or a
write, and the same fail-closed view of a cut-off or corrupt journal."""
from __future__ import annotations

import os
import threading
from datetime import timedelta

import pytest

from journal.log import Journal, JournalError
from ui.api.readers.journal_snapshot import JournalReader, SnapshotJournal
from ui.tests.journal_fixtures import MONDAY, approved_entry


class Tick:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def files(folder):
    return sorted(p.name for p in folder.iterdir())


def test_entries_match_the_journals_own_reader(paths, journal):
    snap = JournalReader(paths.journal).read()
    assert snap.exists and snap.trusted and snap.tail_bytes == 0
    assert list(snap.entries) == journal.entries()


def test_reading_never_takes_the_lock_or_creates_a_file(paths, journal):
    folder = paths.journal.parent
    lock = journal.lock_path
    lock.unlink()  # the writes above made it; a read must never make it again
    before = files(folder)
    reader = JournalReader(paths.journal)
    for _ in range(3):
        reader.read()
    assert files(folder) == before and not lock.exists()


def test_reading_works_while_another_process_holds_the_journal_lock(paths, journal):
    with journal.exclusive():  # run_day in the middle of an approval
        snap = JournalReader(paths.journal).read()
    assert snap.trusted and len(snap.entries) == len(journal.entries())


def test_a_missing_journal_is_empty_and_nothing_is_created(tmp_path):
    path = tmp_path / "journal" / "decisions.jsonl"
    snap = JournalReader(path).read()
    assert not snap.exists and snap.entries == () and snap.trusted
    assert list(tmp_path.iterdir()) == []


def test_reads_are_incremental(paths, journal):
    reader = JournalReader(paths.journal)
    first = len(reader.read().entries)
    journal.append({"status": "pass", "run_id": "r-2", "symbol": "MSFT"})
    snap = reader.read()
    assert len(snap.entries) == first + 1 and snap.entries[-1]["symbol"] == "MSFT"
    assert list(snap.entries) == journal.entries()


def test_an_incomplete_last_line_is_a_write_in_progress_then_cut_off(paths, journal):
    tick = Tick()
    reader = JournalReader(paths.journal, monotonic=tick, grace_s=5)
    whole = len(reader.read().entries)
    with paths.journal.open("ab") as fh:
        fh.write(b'{"status": "pa')
    snap = reader.read()
    assert len(snap.entries) == whole and snap.tail_bytes == 14
    assert not snap.tail_stuck and snap.trusted and snap.problems() == []
    tick.t = 4.9
    assert not reader.read().tail_stuck
    tick.t = 5.0
    snap = reader.read()
    assert snap.tail_stuck and not snap.trusted
    assert "journal.repair" in snap.problems()[0]["text"]
    with pytest.raises(JournalError):  # the journal itself refuses the same file
        journal.entries()


def test_a_growing_tail_is_not_stuck(paths, journal):
    tick = Tick()
    reader = JournalReader(paths.journal, monotonic=tick, grace_s=5)
    with paths.journal.open("ab") as fh:
        fh.write(b'{"status"')
    reader.read()
    tick.t = 6
    with paths.journal.open("ab") as fh:
        fh.write(b': "pass"')
    assert not reader.read().tail_stuck  # it changed: still being written


def test_a_completed_tail_is_read(paths, journal):
    reader = JournalReader(paths.journal)
    n = len(reader.read().entries)
    with paths.journal.open("ab") as fh:
        fh.write(b'{"status": "pass", "symbol": "KO"')
    assert len(reader.read().entries) == n
    with paths.journal.open("ab") as fh:
        fh.write(b"}\n")
    snap = reader.read()
    assert len(snap.entries) == n + 1 and snap.tail_bytes == 0


def test_a_repaired_journal_that_shrank_is_read_again(paths, journal):
    from journal.repair import repair

    tick = Tick()
    reader = JournalReader(paths.journal, monotonic=tick)
    with paths.journal.open("ab") as fh:
        fh.write(b'{"torn": ')
    reader.read()
    repair(journal, apply=True)  # truncates in place
    journal.append({"status": "pass", "run_id": "r-3", "symbol": "XOM"})
    snap = reader.read()
    assert snap.trusted and list(snap.entries) == journal.entries()


@pytest.mark.parametrize(("line", "message"), [
    (b"not json\n", "is not JSON"),
    (b"[1, 2]\n", "is not an object"),
    (b"\xff\xfe\n", "is not JSON"),
])
def test_a_bad_line_stops_the_read_and_is_reported(paths, journal, line, message):
    n = len(journal.entries())
    with paths.journal.open("ab") as fh:
        fh.write(line)
        fh.write(b'{"status": "pass"}\n')
    snap = JournalReader(paths.journal).read()
    assert len(snap.entries) == n and not snap.trusted
    assert message in snap.error and f"line {n + 1}" in snap.error
    assert snap.problems()[0]["level"] == "error"
    with pytest.raises(JournalError):  # the journal fails closed on the same file
        journal.entries()


def test_a_bad_line_stays_reported_on_later_reads(paths, journal):
    reader = JournalReader(paths.journal)
    with paths.journal.open("ab") as fh:
        fh.write(b"oops\n")
    assert reader.read().error
    assert reader.read().error  # never silently skipped on the next poll


def test_crlf_lines_are_read_on_every_platform(tmp_path):
    """Journal.append writes text mode, so CRLF on Windows; the reader splits on LF."""
    path = tmp_path / "decisions.jsonl"
    path.write_bytes(b'{"status": "pass", "symbol": "A"}\r\n\r\n{"status": "pass"}\r\n')
    snap = JournalReader(path).read()
    assert snap.trusted and [e["status"] for e in snap.entries] == ["pass", "pass"]
    with path.open("ab") as fh:
        fh.write(b"bad\r\n")
    assert "line 4 " in JournalReader(path).read().error


def test_a_replaced_journal_is_read_again_from_the_start(paths, journal):
    reader = JournalReader(paths.journal)
    reader.read()
    new = paths.journal.with_name("new.jsonl")
    big = b"".join(b'{"status": "pass", "symbol": "S%d"}\n' % i for i in range(200))
    new.write_bytes(big)  # longer than the old file: only the identity tells them apart
    os.replace(new, paths.journal)
    snap = reader.read()
    assert snap.trusted and len(snap.entries) == 200 and snap.entries[0]["symbol"] == "S0"


def test_a_fixed_bad_line_is_read_again(paths, journal):
    reader = JournalReader(paths.journal)
    n = len(reader.read().entries)
    data = paths.journal.read_bytes()
    paths.journal.write_bytes(data + b"oops\n")
    assert reader.read().error
    paths.journal.write_bytes(data + b'{"status": "pass", "symbol": "FIX"}\n')
    snap = reader.read()
    assert snap.trusted and len(snap.entries) == n + 1 and snap.entries[-1]["symbol"] == "FIX"


def test_concurrent_reads_agree(paths, journal):
    reader = JournalReader(paths.journal)
    results, errors = [], []

    def worker():
        try:
            for _ in range(20):
                results.append(len(reader.read().entries))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    expected = len(journal.entries())
    assert errors == [] and set(results) == {expected} and len(results) == 160


def test_blank_lines_count_like_the_journal_counts_them(paths, journal):
    n = sum(1 for _ in paths.journal.open("rb"))
    with paths.journal.open("ab") as fh:
        fh.write(b"\n   \nbad\n")
    snap = JournalReader(paths.journal).read()
    assert f"line {n + 3} " in snap.error


# ---- SnapshotJournal: the journal's own totals over the snapshot ----

@pytest.mark.parametrize("minutes", [-1, 0, 2, 11, 16, 60 * 8])
def test_day_state_matches_the_journals_own(paths, journal, minutes):
    now = MONDAY + timedelta(minutes=minutes)
    snap = JournalReader(paths.journal).read()
    assert SnapshotJournal(snap.entries).day_state(now) == journal.day_state(now)
    assert SnapshotJournal(snap.entries).orders() == journal.orders()


def test_day_state_counts_what_the_fixture_says(paths, journal):
    snap = JournalReader(paths.journal).read()
    s = SnapshotJournal(snap.entries).day_state(MONDAY + timedelta(minutes=11))
    assert s.trades_today == 1 and s.open_positions == 2  # TSLA filled, AMD unsent
    assert str(s.realized_loss_today) == "31.65" and s.stopped_out_symbols == {"NVDA"}
    assert str(s.risk_used_today) == "101.49"  # 31.65 lost + 49.84 + 20.00 open


def test_an_impossible_order_history_is_refused_like_the_journal(paths, journal):
    journal.append({"status": "filled", "client_order_id": "ta-unknown",
                    "at": MONDAY.isoformat(), "filled_qty": 1, "avg_price": "1"})
    snap = JournalReader(paths.journal).read()
    assert snap.trusted  # every line is JSON ...
    with pytest.raises(JournalError, match="order history is invalid"):
        SnapshotJournal(snap.entries).orders()  # ... but the lifecycle is impossible
    with pytest.raises(JournalError, match="order history is invalid"):
        journal.orders()


def test_the_snapshot_journal_never_locks_or_writes(tmp_path):
    sj = SnapshotJournal([approved_entry("r", "p", MONDAY)])
    with pytest.raises(JournalError), sj.exclusive():
        pass
    with pytest.raises(JournalError):
        sj.append({"status": "pass"})
    with pytest.raises(JournalError):
        sj.record_order_event("ta-x", "submitting", MONDAY)
    with pytest.raises(JournalError):
        sj.expire_unsent(MONDAY)
    for internal in (sj._missing, sj._ends_cut_off, sj._read_entries):
        with pytest.raises(JournalError):
            internal()
    assert sj.day_state(MONDAY).open_positions == 1


def test_the_snapshot_journal_is_not_changed_by_its_caller():
    entries = [approved_entry("r", "p", MONDAY)]
    sj = SnapshotJournal(entries)
    sj.entries().clear()
    entries.clear()
    assert len(sj.entries()) == 1


def test_the_journal_class_is_unchanged_by_the_snapshot(paths, journal):
    """SnapshotJournal is a subclass: the real Journal still locks and writes."""
    assert type(journal) is Journal
    journal.append({"status": "pass", "run_id": "r-9", "symbol": "KO"})
    assert journal.entries()[-1]["symbol"] == "KO"
