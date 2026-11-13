"""Phase 5 PR 1: the journal's order lifecycle and day totals (#109), and the journal hardening
it needs now that a reconciler will write alongside the approval path: an out-of-range clock
(#127), reads under the lock (#128), a lock timeout after "yes" (#129). Offline."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace as _replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

import journal.log as jl
import pipeline.decide as decide
import pipeline.order_flow as of
from journal.log import APPROVED, Journal, JournalError, JournalLockTimeout
from journal.orders import EXPIRED as _EXPIRED
from journal.orders import client_order_id
from journal.repair import main as repair_main
from journal.repair import repair, torn_tail
from pipeline.order_flow import Outcome, propose
from pipeline.order_flow import approve as _approve
from tests.order_helpers import (
    T0,
    approved_entry,
    closed_trade,
    events_clear,
    gate_for_order,
    journal_with,
    quotes_at,
    sealed_ledger,
    sent_order,
    sized_order,
)
from tests.order_helpers import (
    plan as _plan,
)
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tests.test_phase5_gate_d import run_scenario
from tools.config import CONFIG_DIR, limits_from_dict, load_limits
from tools.risk_check import DayState, check_order

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
    with pytest.raises(JournalError, match="approved_at is not an aware ISO time"):
        j.day_state(T0)


@pytest.mark.parametrize("bad", [None, "2026-10-19", datetime(2026, 10, 19, 10, 15)],
                         ids=["none", "string", "naive"])
def test_127_day_state_rejects_a_clock_that_is_not_aware(tmp_path, bad):
    with pytest.raises(JournalError, match="timezone-aware"):
        Journal(tmp_path / "j.jsonl").day_state(bad)


def test_127_day_state_counts_today_as_before(tmp_path):
    j = journal_with(tmp_path / "j.jsonl",
                     [*sent_order(T0, "10.00"), *sent_order(T0 - timedelta(days=1))])
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
    # the two complete approvals (live, unsent) count; the cut-off one does not
    assert Journal(path).day_state(T0).open_positions == 2


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


# ======================= #129: a lock timeout after "yes" =======================

def lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_129_end_to_end_lock_held_elsewhere_after_yes_is_journal_locked_and_recorded(
        monkeypatch, tmp_path):
    """decide.run_decision with the journal lock held by another process (another Journal
    object) from the moment the human types yes until just after approve gives up. The
    approval times out; nothing is approved; the attempt is journaled."""
    monkeypatch.setattr(jl, "LOCK_TIMEOUT_S", 0.3)
    path = tmp_path / "clean-buy.jsonl"
    holder = Journal(path)
    hold, release = threading.Event(), threading.Event()

    def keep_lock():
        with holder.exclusive():
            hold.set()
            release.wait(5)

    real_append = Journal.append
    attempts = []

    def append_spy(self, entry):
        attempts.append(entry["status"])
        if entry["status"] == decide.JOURNAL_LOCKED:
            release.set()  # the other process finishes; the record's write can now succeed
        return real_append(self, entry)

    monkeypatch.setattr(Journal, "append", append_spy)

    def answer_then_lock(_question):
        threading.Thread(target=keep_lock, daemon=True).start()
        hold.wait(5)
        return "yes"

    result, journal, asked, _ = run_scenario(monkeypatch, tmp_path, "clean-buy",
                                             answers=answer_then_lock)
    release.set()
    assert len(asked) == 1
    assert result.outcome.status == decide.JOURNAL_LOCKED
    assert "nothing approved" in result.outcome.reason
    assert "lock timed out" in result.outcome.reason
    (entry,) = lines(path)
    assert entry["status"] == decide.JOURNAL_LOCKED and entry["plan_id"] == "plan-1"
    assert entry["symbol"] == "BRKO" and entry["run_id"] == result.ledger_run_id
    assert APPROVED not in [e["status"] for e in lines(path)]
    assert Journal(path).day_state(
        datetime.fromisoformat(result.proposal.checked_at)).trades_today == 0


def test_129_the_record_is_retried_once(monkeypatch, tmp_path):
    monkeypatch.setattr(decide, "approve", lambda *a, **k: (_ for _ in ()).throw(
        JournalLockTimeout("journal lock timed out")))
    real_append = Journal.append
    tries = []

    def flaky(self, entry):
        tries.append(entry["status"])
        if len(tries) == 1:
            raise JournalLockTimeout("journal lock timed out")
        return real_append(self, entry)

    monkeypatch.setattr(Journal, "append", flaky)
    result, journal, _, _ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.outcome.status == decide.JOURNAL_LOCKED
    assert tries == [decide.JOURNAL_LOCKED, decide.JOURNAL_LOCKED]
    assert [e["status"] for e in journal.entries()] == [decide.JOURNAL_LOCKED]
    assert "could not be journaled" not in result.outcome.reason


def test_129_when_both_writes_fail_the_outcome_says_it_was_not_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(decide, "approve", lambda *a, **k: (_ for _ in ()).throw(
        JournalLockTimeout("journal lock timed out")))
    real_append = Journal.append
    tries = []

    def broken(self, entry):
        tries.append(entry["status"])
        if entry["status"] == decide.JOURNAL_LOCKED:
            raise JournalLockTimeout("journal lock timed out")
        return real_append(self, entry)

    monkeypatch.setattr(Journal, "append", broken)
    result, journal, _, _ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.outcome.status == decide.JOURNAL_LOCKED
    assert tries == [decide.JOURNAL_LOCKED] * decide.RECORD_ATTEMPTS
    assert result.outcome.reason.endswith("(and the attempt could not be journaled)")
    assert journal.entries() == []


def test_129_a_write_failure_that_is_not_a_lock_timeout_is_journal_error(monkeypatch,
                                                                        tmp_path):
    monkeypatch.setattr(decide, "approve", lambda *a, **k: (_ for _ in ()).throw(
        JournalError("journal write failed (PermissionError)")))
    result, journal, _, _ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.outcome.status == decide.JOURNAL_ERROR
    assert isinstance(result.outcome, Outcome) and result.outcome.proposal.ok
    assert [e["status"] for e in journal.entries()] == [decide.JOURNAL_ERROR]


def test_129_a_normal_approval_is_unchanged(monkeypatch, tmp_path):
    result, journal, _, _ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.outcome.status == APPROVED
    assert [e["status"] for e in journal.entries()] == [APPROVED]


# ======================= #109: day totals from the order lifecycle =======================

MON = datetime(2026, 11, 16, 10, 30, tzinfo=NY)  # a Monday


def events_for(entry, *steps):
    """Lifecycle events after `entry` (an approved entry): (status, fields) pairs."""
    cid, out = entry["client_order_id"], []
    when = datetime.fromisoformat(entry["approved_at"])
    for i, (status, fields) in enumerate(steps, 1):
        out.append({"status": status, "client_order_id": cid,
                    "at": (when + timedelta(seconds=i)).isoformat(), **fields})
    return out


SUBMITTED = [("submitting", {}), ("submitted", {})]
FILLED = [*SUBMITTED, ("filled", {"filled_qty": 8, "avg_price": "100.00"})]


def state_of(tmp_path, *entries, now=MON):
    return journal_with(tmp_path / "j.jsonl", list(entries)).day_state(now)


def test_109_a_live_unsent_approval_holds_its_risk_and_a_position_but_no_slot(tmp_path):
    s = state_of(tmp_path, approved_entry(MON - timedelta(minutes=2), "49.84"))
    assert (s.trades_today, s.risk_used_today, s.open_positions) == (0, D("49.84"), 1)
    assert s.realized_loss_today == 0 and s.stopped_out_symbols == frozenset()


def test_109_an_unsent_approval_past_its_plan_expiry_releases_everything(tmp_path):
    """Even before its "expired" event is written, day_state stops counting it."""
    s = state_of(tmp_path, approved_entry(MON - timedelta(minutes=6), "49.84"))
    assert (s.trades_today, s.risk_used_today, s.open_positions) == (0, 0, 0)


@pytest.mark.parametrize("steps", [
    [("submitting", {})], [*SUBMITTED], [("submitting", {}), ("send_unknown", {})],
    [*SUBMITTED, ("partially_filled", {"filled_qty": 3, "avg_price": "100"})], [*FILLED],
    [*SUBMITTED, ("partially_filled", {"filled_qty": 3, "avg_price": "100"}),
     ("cancelled", {"filled_qty": 3})],
], ids=["submitting", "submitted", "send_unknown", "partial", "filled", "partial-cancelled"])
def test_109_an_order_that_may_hold_a_position_counts_open_and_keeps_its_risk(tmp_path, steps):
    a = approved_entry(MON - timedelta(minutes=5), "49.84")
    s = state_of(tmp_path, a, *events_for(a, *steps))
    assert s.open_positions == 1 and s.trades_today == 1 and s.risk_used_today == D("49.84")


@pytest.mark.parametrize("steps", [
    [("submitting", {}), ("rejected", {})], [*SUBMITTED, ("cancelled", {"filled_qty": 0})],
    [*SUBMITTED, ("expired", {"filled_qty": 0})],
    [("submitting", {}), ("send_unknown", {}), ("cancelled", {"filled_qty": 0})],
], ids=["rejected", "cancelled", "expired", "send_unknown-cancelled"])
def test_109_a_confirmed_zero_fill_releases_risk_but_keeps_the_trade_slot(tmp_path, steps):
    a = approved_entry(MON - timedelta(minutes=5), "49.84")
    s = state_of(tmp_path, a, *events_for(a, *steps))
    assert s.trades_today == 1 and s.risk_used_today == 0 and s.open_positions == 0


def test_109_a_stop_out_records_the_loss_the_symbol_and_frees_the_position(tmp_path):
    a = approved_entry(MON - timedelta(minutes=30), "49.84", symbol="BRKO")
    s = state_of(tmp_path, a, *events_for(a, *FILLED, ("closed", {"closed_by": "stop",
                                                                   "realized_pnl": "-48.50"})))
    assert s.open_positions == 0 and s.trades_today == 1
    assert s.risk_used_today == D("49.84") and s.realized_loss_today == D("48.50")
    assert s.stopped_out_symbols == frozenset({"BRKO"})


def test_109_a_gap_loss_larger_than_max_loss_is_what_counts(tmp_path):
    a = approved_entry(MON - timedelta(minutes=30), "49.84")
    s = state_of(tmp_path, a, *events_for(a, *FILLED, ("closed", {"closed_by": "stop",
                                                                   "realized_pnl": "-80.00"})))
    assert s.risk_used_today == D("80.00") and s.realized_loss_today == D("80.00")


@pytest.mark.parametrize("closed_by", ["target", "flatten"])
def test_109_only_a_stop_bars_re_entry(tmp_path, closed_by):
    a = approved_entry(MON - timedelta(minutes=30), "49.84")
    s = state_of(tmp_path, a, *events_for(a, *FILLED, ("closed", {"closed_by": closed_by,
                                                                   "realized_pnl": "-3.00"})))
    assert s.stopped_out_symbols == frozenset() and s.realized_loss_today == D("3.00")


def test_109_a_profit_is_not_a_realized_loss_and_does_not_offset_one(tmp_path):
    entries = [*closed_trade(MON - timedelta(hours=2), "40", pnl="-30.00", closed_by="stop",
                             symbol="AAA"),
               *closed_trade(MON - timedelta(hours=1), "40", pnl="+90.00", symbol="BBB")]
    s = state_of(tmp_path, *entries)
    assert s.realized_loss_today == D("30.00") and s.trades_today == 2
    assert s.risk_used_today == D("80") and s.open_positions == 0


def test_109_an_open_order_from_an_earlier_day_still_counts_as_open(tmp_path):
    """An overnight position (filled on Friday, never closed) blocks Monday."""
    a = approved_entry(MON - timedelta(days=3), "49.84")
    s = state_of(tmp_path, a, *events_for(a, *FILLED))
    assert s.open_positions == 1 and s.trades_today == 0 and s.risk_used_today == 0


def test_109_an_unsent_approval_from_an_earlier_day_is_not_open(tmp_path):
    """It can never be sent: its plan expired. One that reached submitting still counts."""
    old = approved_entry(MON - timedelta(days=1), "49.84")
    assert state_of(tmp_path, old).open_positions == 0
    a = approved_entry(MON - timedelta(days=1), "49.84")
    later = tmp_path / "later"
    later.mkdir()
    assert state_of(later, a, *events_for(a, ("submitting", {}))).open_positions == 1


def test_109_an_impossible_history_fails_closed(tmp_path):
    a = approved_entry(MON - timedelta(minutes=5), "49.84")
    j = journal_with(tmp_path / "j.jsonl",
                     [a, *events_for(a, ("filled", {"filled_qty": 8, "avg_price": "1"}))])
    with pytest.raises(JournalError, match="order history is invalid"):
        j.day_state(MON)


# ======================= #109: the risk rules in check_order =======================

def chk(state, limits=LIMITS, symbol="TSLA", now=MON):
    o = sized_order(symbol)
    return check_order(o, limits, state, now, gate_for_order(o, now))


def test_109_a_clean_day_passes():
    assert chk(DayState()) == {"verdict": "PASS", "reasons": []}


def test_109_one_open_position_blocks_a_new_entry():
    r = chk(DayState(trades_today=1, risk_used_today=D("49.84"), open_positions=1))
    assert r == {"verdict": "BLOCK", "reasons": ["already 1 open position(s) (max 1)"]}


def test_109_max_open_positions_is_a_limit():
    lim = _replace(LIMITS, max_open_positions=2)
    assert chk(DayState(open_positions=1), lim)["verdict"] == "PASS"
    assert "already 2 open position(s) (max 2)" in chk(DayState(open_positions=2),
                                                       lim)["reasons"]


@pytest.mark.parametrize("lost,blocks", [("149.99", False), ("150.00", True),
                                         ("212.00", True)])
def test_109_the_daily_loss_stop(lost, blocks):
    r = chk(DayState(realized_loss_today=D(lost)))
    reason = (f"daily loss stop: realized loss {D(lost):.2f} today reached the daily limit "
              "150.00; no new entries")
    assert (reason in r["reasons"]) is blocks


def test_109_no_same_day_re_entry_after_a_stop_out():
    state = DayState(stopped_out_symbols=frozenset({"TSLA"}))
    assert "TSLA was stopped out today: no re-entry" in chk(state)["reasons"]
    assert chk(state, symbol="AAPL")["verdict"] == "PASS"


@pytest.mark.parametrize("over,why", [
    ({"open_positions": -1}, "open_positions must be a non-negative whole number"),
    ({"open_positions": True}, "open_positions must be a non-negative whole number"),
    ({"open_positions": 1.0}, "open_positions must be a non-negative whole number"),
    ({"realized_loss_today": D("-1")}, "realized_loss_today must be a finite"),
    ({"realized_loss_today": D("NaN")}, "realized_loss_today must be a finite"),
    ({"realized_loss_today": 1.5}, "realized_loss_today must be a finite"),
    ({"stopped_out_symbols": {"TSLA"}}, "stopped_out_symbols must be a frozenset"),
    ({"stopped_out_symbols": frozenset({1})}, "stopped_out_symbols must be a frozenset"),
], ids=lambda x: x if isinstance(x, str) else None)
def test_109_a_bad_day_state_is_an_early_block(over, why):
    r = chk(_replace(DayState(), **over))
    assert r["verdict"] == "BLOCK" and any(why in x for x in r["reasons"])


@pytest.mark.parametrize("raw,ok", [(1, True), (3, True), (0, False), (-1, False),
                                    (True, False), ("2", True), (1.5, False)])
def test_109_max_open_positions_config(raw, ok):
    d = json.loads((CONFIG_DIR / "limits.example.json").read_text(encoding="utf-8"))
    assert d["max_open_positions"] == 1  # the decided value ships in the example
    d["max_open_positions"] = raw
    if ok:
        assert limits_from_dict(d).max_open_positions == int(raw)
    else:
        with pytest.raises(ValueError, match="max_open_positions"):
            limits_from_dict(d)


def test_109_a_missing_max_open_positions_means_one():
    d = json.loads((CONFIG_DIR / "limits.example.json").read_text(encoding="utf-8"))
    del d["max_open_positions"]
    assert limits_from_dict(d).max_open_positions == 1


# ======================= #109: end to end through propose =======================

def proposal(tmp_path, entries):
    j = journal_with(tmp_path / "j.jsonl", entries)
    return propose(sealed_ledger(), "plan-1", journal=j, clock=lambda: T0,
                   quotes=quotes_at(T0), events=events_clear())


def test_109_propose_refuses_while_todays_order_is_open(limits, tmp_path):
    a = approved_entry(T0 - timedelta(minutes=5), "10.00", symbol="AAPL")
    p = proposal(tmp_path, [a, *events_for(a, *FILLED)])
    assert p.ok is False and p.stage == "risk"
    assert "already 1 open position(s) (max 1)" in p.reason


def test_109_propose_passes_once_that_order_is_closed(limits, tmp_path):
    p = proposal(tmp_path, closed_trade(T0 - timedelta(minutes=30), "10.00", symbol="AAPL"))
    assert p.ok, p.reason


def test_109_propose_refuses_after_the_daily_loss_stop(limits, tmp_path):
    entries = [*closed_trade(T0 - timedelta(hours=2), "40", pnl="-90.00", closed_by="stop",
                             symbol="AAA"),
               *closed_trade(T0 - timedelta(hours=1), "40", pnl="-60.00", closed_by="stop",
                             symbol="BBB")]
    p = proposal(tmp_path, entries)
    assert p.ok is False and p.stage == "risk"
    assert "daily loss stop: realized loss 150.00 today reached the daily limit" in p.reason


def test_109_propose_refuses_a_re_entry_after_a_stop_out(limits, tmp_path):
    p = proposal(tmp_path, closed_trade(T0 - timedelta(hours=1), "10.00", pnl="-9.00",
                                        closed_by="stop", symbol="TSLA"))
    assert p.ok is False and "TSLA was stopped out today: no re-entry" in p.reason


def test_109_the_same_run_and_plan_always_get_the_same_client_order_id():
    led = sealed_ledger()
    assert client_order_id(led.run_id, "plan-1") == client_order_id(led.run_id, "plan-1")


# ======================= Ivan's rule: an unsent approval lasts until its plan expires =========

NINE = datetime(2026, 10, 19, 9, 0, tzinfo=NY)  # Monday 2026-10-19; entries open at 09:45


def at(hh, mm, ss=0):
    return NINE.replace(hour=hh, minute=mm, second=ss)


def ledger_made_at(t):
    """A ledger whose one plan was made at `t` (it expires max_plan_age_s = 300 s later)."""
    return sealed_ledger([_plan(as_of=t.isoformat())])


def propose_at(journal, led, t):
    return propose(led, "plan-1", journal=journal, clock=lambda: t, quotes=quotes_at(t),
                   events=events_clear())


def test_ivan_an_approval_at_950_blocks_952_and_allows_956(limits, tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    first = ledger_made_at(at(9, 50))
    p = propose_at(j, first, at(9, 50, 20))
    assert p.ok, p.reason
    out = _approve(p, first, journal=j, ask=lambda _: "yes", clock=lambda: at(9, 50, 20),
                   quotes=quotes_at(at(9, 50, 20)), events=events_clear())
    assert out.status == APPROVED
    (entry,) = [e for e in j.entries() if e["status"] == APPROVED]
    assert entry["expires_at"] == at(9, 55).isoformat()  # plan as_of 09:50 + 300 s

    # 09:52: the unsent approval still holds its position and its risk
    blocked = propose_at(j, ledger_made_at(at(9, 52)), at(9, 52, 10))
    assert blocked.ok is False and blocked.stage == "risk"
    assert "already 1 open position(s) (max 1)" in blocked.reason
    assert j.day_state(at(9, 52, 10)).trades_today == 0  # never sent: no trade slot

    # 09:56: the plan expired at 09:55, so the approval is journaled expired and released
    allowed = propose_at(j, ledger_made_at(at(9, 56)), at(9, 56, 10))
    assert allowed.ok, allowed.reason
    cid = client_order_id(first.run_id, "plan-1")
    events = [e for e in j.entries() if e.get("client_order_id") == cid and e["status"] != APPROVED]
    assert events == [{"status": _EXPIRED, "client_order_id": cid, "filled_qty": 0,
                       "reason": "plan expired before the order was sent",
                       "at": at(9, 56, 10).isoformat()}]
    s = j.day_state(at(9, 56, 10))
    assert (s.trades_today, s.risk_used_today, s.open_positions) == (0, 0, 0)


def test_an_approval_expires_exactly_at_its_plans_expiry(tmp_path):
    a = approved_entry(at(9, 50), "49.84")  # expires 09:55:00
    j = journal_with(tmp_path / "j.jsonl", [a])
    assert j.day_state(at(9, 54, 59)).open_positions == 1
    assert j.day_state(at(9, 55)).open_positions == 0


def test_expire_unsent_writes_once_and_only_for_expired_unsent_approvals(tmp_path):
    stale, live = approved_entry(at(9, 40), "10"), approved_entry(at(9, 58), "10")
    sent = sent_order(at(9, 41), "10")
    j = journal_with(tmp_path / "j.jsonl", [stale, live, *sent])
    assert j.expire_unsent(at(10, 0)) == [stale["client_order_id"]]
    assert j.expire_unsent(at(10, 0, 30)) == []  # already expired: nothing more to write
    orders = j.orders()
    assert orders[stale["client_order_id"]].status == _EXPIRED
    assert orders[live["client_order_id"]].status == APPROVED
    assert orders[sent[0]["client_order_id"]].status == "submitting"


def test_an_old_entry_without_expires_at_counts_until_the_end_of_its_day(tmp_path):
    a = approved_entry(at(9, 50), "10")
    del a["expires_at"]
    j = journal_with(tmp_path / "j.jsonl", [a])
    assert j.day_state(at(15, 59)).open_positions == 1
    assert j.day_state(at(9, 50) + timedelta(days=1)).open_positions == 0


def test_sending_takes_the_trade_slot_and_expiry_after_sending_is_impossible(tmp_path):
    entries = sent_order(at(9, 50), "10")
    j = journal_with(tmp_path / "j.jsonl", entries)
    assert j.day_state(at(10, 30)).trades_today == 1  # sent: the slot stays taken
    assert j.expire_unsent(at(10, 30)) == []  # only unsent approvals expire
    assert j.day_state(at(10, 30)).open_positions == 1  # sent and never resolved: still open


def test_the_expired_step_needs_a_zero_fill_and_only_follows_an_unsent_approval(tmp_path):
    a = approved_entry(at(9, 50), "10")
    j = journal_with(tmp_path / "j.jsonl", [a])
    with pytest.raises(JournalError, match="needs the broker's filled_qty"):
        j.record_order_event(a["client_order_id"], _EXPIRED, at(9, 56))
    with pytest.raises(JournalError, match="never sent cannot have a fill"):
        j.record_order_event(a["client_order_id"], _EXPIRED, at(9, 56), filled_qty=1)


# ======================= review fixes =======================

def test_b1_append_refuses_to_glue_onto_a_cut_off_line_and_repair_still_works(tmp_path):
    path = tmp_path / "j.jsonl"
    cut_off(path)
    before = path.read_bytes()
    with pytest.raises(JournalError, match=r"python -m journal\.repair"):
        Journal(path).append({"status": "pass", "run_id": "r"})
    assert path.read_bytes() == before  # nothing written
    assert repair_main(["--path", str(path), "--yes"]) == 0
    Journal(path).append({"status": "pass", "run_id": "r"})
    assert [e["status"] for e in Journal(path).entries()] == [APPROVED, "pass"]


def test_b1_a_decision_after_a_crash_leaves_the_journal_repairable(monkeypatch, tmp_path):
    """The next decision's writes (here a refused proposal's best-effort no_order entry)
    must not bury the cut-off line mid-file."""
    path = tmp_path / "clean-buy.jsonl"
    cut_off(path)
    before = path.read_bytes()
    result, journal, asked, _ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.proposal.ok is False and result.proposal.stage == "journal"
    assert asked == [] and path.read_bytes() == before
    assert repair_main(["--path", str(path), "--yes"]) == 0
    assert len(Journal(path).entries()) == 1


def test_s1_an_overnight_position_closed_this_morning_counts_today(tmp_path):
    """Approved and filled on Friday, stopped out on Monday morning: Monday's loss and
    stop-out, though not Monday's trade or risk."""
    a = approved_entry(MON - timedelta(days=3), "49.84", symbol="GAPA")
    cid = a["client_order_id"]
    events = [*events_for(a, *FILLED),
              {"status": "closed", "client_order_id": cid, "closed_by": "stop",
               "realized_pnl": "-120.00", "at": (MON - timedelta(hours=1)).isoformat()}]
    s = state_of(tmp_path, a, *events)
    assert s.realized_loss_today == D("120.00")
    assert s.stopped_out_symbols == frozenset({"GAPA"})
    assert s.trades_today == 0 and s.risk_used_today == 0 and s.open_positions == 0


def test_s1_a_trade_closed_yesterday_does_not_count_today(tmp_path):
    a = approved_entry(MON - timedelta(days=3), "49.84", symbol="GAPA")
    s = state_of(tmp_path, a, *events_for(a, *FILLED, ("closed", {"closed_by": "stop",
                                                                   "realized_pnl": "-120"})))
    assert s.realized_loss_today == 0 and s.stopped_out_symbols == frozenset()


def test_lock_timeouts_raise_the_journal_lock_timeout_subclass(tmp_path, monkeypatch):
    monkeypatch.setattr(jl, "LOCK_TIMEOUT_S", 0.2)
    path = tmp_path / "j.jsonl"
    hold, release = threading.Event(), threading.Event()

    def keep_lock():
        with Journal(path).exclusive():
            hold.set()
            release.wait(5)

    t = threading.Thread(target=keep_lock)
    t.start()
    hold.wait(5)
    try:
        with pytest.raises(JournalLockTimeout):
            Journal(path).append({"status": "pass"})
    finally:
        release.set()
        t.join(5)


def test_n1_an_approval_cannot_be_sent_after_its_plan_expired(tmp_path):
    a = approved_entry(at(9, 50), "10")  # expires 09:55
    j = journal_with(tmp_path / "j.jsonl", [a])
    before = j.path.read_bytes()
    with pytest.raises(JournalError, match="its plan expired before it was sent"):
        j.record_order_event(a["client_order_id"], "submitting", at(9, 55))
    assert j.path.read_bytes() == before
    assert j.record_order_event(a["client_order_id"], "submitting", at(9, 54, 59)).sent
