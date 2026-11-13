"""Phase 5 PR 1: the journal's order lifecycle and day totals (#109), and the journal hardening
it needs now that a reconciler will write alongside the approval path: an out-of-range clock
(#127), reads under the lock (#128), a lock timeout after "yes" (#129). Offline."""
from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import journal.log as jl
import pipeline.order_flow as of
from journal.log import APPROVED, Journal, JournalError
from journal.repair import main as repair_main
from journal.repair import repair, torn_tail
from pipeline.order_flow import propose
from tests.order_helpers import (
    T0,
    approved_entry,
    events_clear,
    journal_with,
    quotes_at,
    sealed_ledger,
)
from tools.config import CONFIG_DIR, load_limits

NY = ZoneInfo("America/New_York")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")

# Aware clocks too close to the date-range limits to convert to New York time (#126, #127)
EDGE_CLOCKS = [
    datetime(1, 1, 1, tzinfo=UTC),
    datetime(1, 1, 1, 3, tzinfo=timezone(timedelta(hours=14))),
    datetime(9999, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-12))),
]
EDGE_IDS = ["min-utc", "min-east", "max-west"]


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)
    return LIMITS


# ======================= #127: an out-of-range clock =======================

@pytest.mark.parametrize("edge", EDGE_CLOCKS, ids=EDGE_IDS)
def test_127_propose_refuses_an_out_of_range_clock_at_stage_plan(limits, tmp_path, edge):
    p = propose(sealed_ledger(), "plan-1", journal=Journal(tmp_path / "j.jsonl"),
                clock=lambda: edge, quotes=quotes_at(T0), events=events_clear())
    assert p.ok is False and p.stage == "plan"
    assert p.reason == "clock is outside the supported date range"
    assert p.checked_at == str(edge)  # a refusal still carries a timestamp


@pytest.mark.parametrize("bad", [None, "2026-10-19T10:15:00-04:00", 1760883300],
                         ids=["none", "string", "number"])
def test_127_propose_refuses_a_clock_that_is_not_a_datetime(limits, tmp_path, bad):
    p = propose(sealed_ledger(), "plan-1", journal=Journal(tmp_path / "j.jsonl"),
                clock=lambda: bad, quotes=quotes_at(T0), events=events_clear())
    assert p.ok is False and p.stage == "plan"
    assert p.reason == "clock must be timezone-aware"


def test_127_a_normal_clock_still_proposes(limits, tmp_path):
    p = propose(sealed_ledger(), "plan-1", journal=Journal(tmp_path / "j.jsonl"),
                clock=lambda: T0, quotes=quotes_at(T0), events=events_clear())
    assert p.ok, p.reason
    assert p.checked_at == T0.astimezone(NY).isoformat()


@pytest.mark.parametrize("edge", EDGE_CLOCKS, ids=EDGE_IDS)
def test_127_day_state_raises_journal_error_for_an_out_of_range_clock(tmp_path, edge):
    j = journal_with(tmp_path / "j.jsonl", [approved_entry(T0)])
    with pytest.raises(JournalError, match="outside the supported date range"):
        j.day_state(edge)


def test_127_day_state_rejects_an_approved_entry_stamped_at_the_date_range_edge(tmp_path):
    entry = approved_entry(T0)
    entry["approved_at"] = "9999-12-31T23:59:00-12:00"
    j = journal_with(tmp_path / "j.jsonl", [entry])
    with pytest.raises(JournalError, match="malformed approved entry"):
        j.day_state(T0)


@pytest.mark.parametrize("bad", [None, "2026-10-19", datetime(2026, 10, 19, 10, 15)],
                         ids=["none", "string", "naive"])
def test_127_day_state_rejects_a_clock_that_is_not_aware(tmp_path, bad):
    with pytest.raises(JournalError, match="timezone-aware"):
        Journal(tmp_path / "j.jsonl").day_state(bad)


def test_127_day_state_counts_today_as_before(tmp_path):
    j = journal_with(tmp_path / "j.jsonl",
                     [approved_entry(T0, "10.00"), approved_entry(T0 - timedelta(days=1))])
    state = j.day_state(T0)
    assert state.trades_today == 1 and str(state.risk_used_today) == "10.00"


def test_127_journal_lines_are_plain_json(tmp_path):
    """Sanity: the helper writes what the journal reads."""
    j = journal_with(tmp_path / "j.jsonl", [approved_entry(T0)])
    (line,) = j.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["status"] == APPROVED


# ======================= #128: reads under the lock; a cut-off last line =======================

def test_128_a_reader_waits_for_a_writer_and_never_sees_half_a_line(tmp_path):
    path = tmp_path / "j.jsonl"
    writer, reader = Journal(path), Journal(path)  # two objects: the cross-process lock
    entry = json.dumps({"status": "pass", "run_id": "r1"}) + "\n"
    started, seen = threading.Event(), []

    def write_slowly():
        with writer.exclusive():
            with path.open("a", encoding="utf-8") as fh:
                fh.write(entry[:10])
                fh.flush()
                started.set()
                time.sleep(0.3)
                fh.write(entry[10:])

    t = threading.Thread(target=write_slowly)
    t.start()
    started.wait(5)
    seen.append(reader.entries())  # blocks until the writer releases the lock
    t.join(5)
    assert seen == [[{"status": "pass", "run_id": "r1"}]]


def test_128_a_read_that_cannot_take_the_lock_raises_and_propose_refuses(limits, tmp_path,
                                                                      monkeypatch):
    monkeypatch.setattr(jl, "LOCK_TIMEOUT_S", 0.2)
    path = tmp_path / "j.jsonl"
    holder = Journal(path)
    hold, release = threading.Event(), threading.Event()

    def keep_lock():
        with holder.exclusive():
            hold.set()
            release.wait(5)

    t = threading.Thread(target=keep_lock)
    t.start()
    hold.wait(5)
    try:
        with pytest.raises(JournalError, match="lock timed out"):
            Journal(path).entries()
        p = propose(sealed_ledger(), "plan-1", journal=Journal(path), clock=lambda: T0,
                    quotes=quotes_at(T0), events=events_clear())
        assert p.ok is False and p.stage == "journal" and "lock timed out" in p.reason
    finally:
        release.set()
        t.join(5)


def cut_off(path, complete=1):
    """A journal with `complete` whole approved lines, then a line a crash cut off."""
    lines = [json.dumps(approved_entry(T0)) + "\n" for _ in range(complete)]
    path.write_text("".join(lines) + '{"status": "approved_not_sent", "max_lo',
                    encoding="utf-8")


def test_128_a_cut_off_last_line_is_refused_with_the_repair_command(limits, tmp_path):
    path = tmp_path / "j.jsonl"
    cut_off(path)
    with pytest.raises(JournalError, match=r"python -m journal\.repair"):
        Journal(path).entries()
    p = propose(sealed_ledger(), "plan-1", journal=Journal(path), clock=lambda: T0,
                quotes=quotes_at(T0), events=events_clear())
    assert p.ok is False and p.stage == "journal" and "cut off" in p.reason


def test_128_a_complete_last_line_that_parses_is_still_required_to_end_the_file(tmp_path):
    """Even valid JSON without its newline is a write that did not finish."""
    path = tmp_path / "j.jsonl"
    path.write_text(json.dumps(approved_entry(T0)), encoding="utf-8")
    with pytest.raises(JournalError, match="cut off"):
        Journal(path).entries()


def test_128_an_empty_or_missing_journal_reads_as_empty(tmp_path):
    assert Journal(tmp_path / "missing.jsonl").entries() == []
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    assert Journal(tmp_path / "empty.jsonl").entries() == []


def test_128_repair_dry_run_changes_nothing(tmp_path, capsys):
    path = tmp_path / "j.jsonl"
    cut_off(path)
    before = path.read_bytes()
    assert repair_main(["--path", str(path)]) == 1
    assert path.read_bytes() == before and list(tmp_path.glob("*.torn-*")) == []
    assert "Re-run with --yes" in capsys.readouterr().out


def test_128_repair_moves_the_cut_off_line_aside_and_keeps_every_complete_line(tmp_path):
    path = tmp_path / "j.jsonl"
    cut_off(path, complete=2)
    tail = torn_tail(path)
    out = repair(Journal(path), apply=True, now=datetime(2026, 11, 13, 15, 0, tzinfo=UTC))
    assert out["torn_bytes"] == len(tail) > 0
    side = tmp_path / "j.jsonl.torn-20261113T150000000000Z"
    assert out["moved_to"] == str(side) and side.read_bytes() == tail
    entries = Journal(path).entries()
    assert len(entries) == 2 and all(e["status"] == APPROVED for e in entries)
    assert Journal(path).day_state(T0).trades_today == 2  # the cut-off approval is not counted


def test_128_repair_never_overwrites_an_earlier_fragment(tmp_path):
    path = tmp_path / "j.jsonl"
    cut_off(path)
    when = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
    (tmp_path / "j.jsonl.torn-20261113T150000000000Z").write_bytes(b"earlier")
    with pytest.raises(JournalError, match="repair failed"):
        repair(Journal(path), apply=True, now=when)
    assert (tmp_path / "j.jsonl.torn-20261113T150000000000Z").read_bytes() == b"earlier"
    with pytest.raises(JournalError, match="cut off"):
        Journal(path).entries()  # nothing was truncated


def test_128_repair_of_a_whole_journal_is_a_no_op(tmp_path, capsys):
    path = tmp_path / "j.jsonl"
    path.write_text(json.dumps(approved_entry(T0)) + "\n", encoding="utf-8")
    assert repair_main(["--path", str(path), "--yes"]) == 0
    assert "nothing to repair" in capsys.readouterr().out
    assert list(tmp_path.glob("*.torn-*")) == []
