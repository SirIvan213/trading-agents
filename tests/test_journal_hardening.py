"""Journal and paper-account hardening before Phase 5 PR 4: #194, #191, #192, #190, #197.

Offline: tmp_path journals, fake clocks and fake openers. No network, no billed call.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import pytest

import journal.log as jl
import pipeline.decide as decide
import pipeline.order_flow as of
from journal.log import APPROVED, Journal, JournalError, JournalLockTimeout
from journal.orders import OrderError, apply_event, from_approved, replay
from journal.repair import BAD_LINES
from journal.repair import main as repair_main
from pipeline.order_flow import propose
from tests.order_helpers import (
    T0,
    approved_entry,
    closed_trade,
    events_clear,
    journal_with,
    quotes_at,
    sealed_ledger,
)
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tests.test_phase5_gate_d import run_scenario
from tools.broker import AlpacaPaperBroker, BrokerError
from tools.config import CONFIG_DIR, load_limits

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)


def lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ======================= #194: an event never goes back in time =======================

def ev(cid, status, at, **fields):
    return {"status": status, "client_order_id": cid, "at": at.isoformat(), **fields}


def trade(when):
    """approved at `when`, then submitting +1 s, submitted +2 s, filled +3 s."""
    a = approved_entry(when, "49.84")
    cid = a["client_order_id"]
    return a, cid, [ev(cid, "submitting", when + timedelta(seconds=1)),
                    ev(cid, "submitted", when + timedelta(seconds=2)),
                    ev(cid, "filled", when + timedelta(seconds=3), filled_qty=8,
                       avg_price="364.20")]


def test_194_events_at_the_same_time_or_later_are_accepted():
    a, cid, events = trade(T0)
    state = from_approved(a)
    same = apply_event(state, ev(cid, "submitting", T0))  # the approval's own second
    assert same.status == "submitting"
    orders = replay([a, *events, ev(cid, "closed", T0 + timedelta(seconds=3),
                                    closed_by="stop", realized_pnl="-49.84")])
    assert orders[cid].status == "closed"


def test_194_blocked_an_event_before_the_previous_one_is_refused():
    a, cid, events = trade(T0)
    state = from_approved(a)
    for e in events:
        state = apply_event(state, e)
    with pytest.raises(OrderError, match="before the order's previous event"):
        apply_event(state, ev(cid, "closed", T0 + timedelta(seconds=2), closed_by="stop",
                              realized_pnl="-49.84"))


def test_194_blocked_an_event_before_the_approval_is_refused():
    a = approved_entry(T0)
    with pytest.raises(OrderError, match="before the order's previous event"):
        apply_event(from_approved(a), ev(a["client_order_id"], "submitting",
                                         T0 - timedelta(microseconds=1)))


def test_194_blocked_a_back_dated_close_is_never_written_so_the_loss_stays_today(tmp_path):
    """The #188 risk: a stop-out closed today but stamped yesterday would drop its loss out
    of today's totals. The journal refuses to record it; the order stays open, risk counted."""
    a, cid, events = trade(T0)
    j = journal_with(tmp_path / "j.jsonl", [a, *events])
    with pytest.raises(JournalError, match="before the order's previous event"):
        j.record_order_event(cid, "closed", T0 - timedelta(days=1), closed_by="stop",
                             realized_pnl="-49.84")
    assert [e["status"] for e in lines(j.path)][-1] == "filled"
    state = j.day_state(T0 + timedelta(minutes=5))
    assert state.open_positions == 1 and state.risk_used_today == D("49.84")


def test_194_blocked_a_hand_written_back_dated_event_makes_the_day_fail_closed(tmp_path):
    a, cid, events = trade(T0)
    bad = ev(cid, "closed", T0 - timedelta(days=1), closed_by="stop", realized_pnl="-49.84")
    j = journal_with(tmp_path / "j.jsonl", [a, *events, bad])
    with pytest.raises(JournalError, match="before the order's previous event"):
        j.day_state(T0)


# ======================= #191: one replay per locked operation =======================

@pytest.fixture
def replays(monkeypatch):
    count = {"n": 0}
    real = jl.replay

    def counting(entries):
        count["n"] += 1
        return real(entries)

    monkeypatch.setattr(jl, "replay", counting)
    return count


def stale_approvals(n):
    """n approvals made long ago today, each with a plan that expired."""
    when = T0 - timedelta(hours=1)
    return [approved_entry(when + timedelta(seconds=i)) for i in range(n)]


def test_191_expire_unsent_replays_the_journal_once_however_many_it_expires(tmp_path, replays):
    entries = stale_approvals(60)
    j = journal_with(tmp_path / "j.jsonl", entries)
    replays["n"] = 0
    done = j.expire_unsent(T0)
    assert len(done) == 60 and replays["n"] == 1  # was 61 (one per event, O(n^2))
    fresh = Journal(j.path).orders()  # a fresh replay of the file agrees
    assert all(o.status == "expired" for o in fresh.values()) and len(fresh) == 60


def test_191_propose_replays_the_journal_once(tmp_path, replays, limits):
    j = journal_with(tmp_path / "j.jsonl", [*stale_approvals(3), *closed_trade(T0 - timedelta(
        hours=2))])
    replays["n"] = 0
    p = propose(sealed_ledger(), "plan-1", journal=j, clock=lambda: T0, quotes=quotes_at(T0),
                events=events_clear())
    assert p.ok, p.reason
    assert replays["n"] == 1  # was 2 + one per expired approval
    assert [e["status"] for e in lines(j.path)].count("expired") == 3


def test_191_outside_the_lock_every_read_replays_the_file(tmp_path, replays):
    j = journal_with(tmp_path / "j.jsonl", [approved_entry(T0)])
    replays["n"] = 0
    j.orders()
    j.day_state(T0)
    assert replays["n"] == 2


def test_191_blocked_the_held_replay_never_outlives_the_lock(tmp_path):
    """Another writer (another Journal object, as another process) appends between two
    locked operations: the second sees it."""
    path = tmp_path / "j.jsonl"
    mine, other = Journal(path), Journal(path)
    first = approved_entry(T0)
    other.append(first)
    with mine.exclusive():
        assert set(mine.orders()) == {first["client_order_id"]}
    second = approved_entry(T0)
    other.append(second)
    with mine.exclusive():
        assert set(mine.orders()) == {first["client_order_id"], second["client_order_id"]}
    assert mine._replayed is None


def test_191_blocked_an_append_inside_the_lock_is_seen_by_the_next_read(tmp_path):
    j = journal_with(tmp_path / "j.jsonl", [approved_entry(T0)])
    with j.exclusive():
        before = j.orders()
        new = approved_entry(T0)
        j.append(new)
        after = j.orders()
    assert new["client_order_id"] in after and new["client_order_id"] not in before
    assert len(after) == 2


def test_191_blocked_a_caller_cannot_change_the_held_replay(tmp_path):
    j = journal_with(tmp_path / "j.jsonl", [approved_entry(T0)])
    with j.exclusive():
        j.orders().clear()
        assert len(j.orders()) == 1


def test_191_blocked_a_refused_event_leaves_the_held_replay_unchanged(tmp_path):
    a = approved_entry(T0)
    j = journal_with(tmp_path / "j.jsonl", [a])
    cid = a["client_order_id"]
    with j.exclusive():
        with pytest.raises(JournalError):
            j.record_order_event(cid, "filled", T0 + timedelta(seconds=1), filled_qty=8,
                                 avg_price="1")  # approved -> filled is impossible
        assert j.orders()[cid].status == APPROVED
        j.record_order_event(cid, "submitting", T0 + timedelta(seconds=1))
        assert j.orders()[cid].status == "submitting"
    assert Journal(j.path).orders()[cid].status == "submitting"


def test_191_the_held_replay_matches_a_fresh_replay_after_many_events(tmp_path):
    a, cid, events = trade(T0)
    j = journal_with(tmp_path / "j.jsonl", [a, *stale_approvals(5)])
    with j.exclusive():
        j.expire_unsent(T0)
        for e in events:
            fields = {k: v for k, v in e.items() if k not in ("status", "client_order_id", "at")}
            from datetime import datetime
            j.record_order_event(cid, e["status"], datetime.fromisoformat(e["at"]), **fields)
        held = j.orders()
    assert held == Journal(j.path).orders()


# ======================= #192: reads never create files; repair; reasons =======================

def test_192_reading_a_missing_journal_creates_nothing(tmp_path):
    j = Journal(tmp_path / "nowhere" / "j.jsonl")
    assert j.entries() == [] and j.orders() == {} and j.expire_unsent(T0) == []
    assert j.day_state(T0).trades_today == 0
    assert not (tmp_path / "nowhere").exists()


def test_192_propose_on_a_missing_journal_creates_nothing(tmp_path, limits):
    j = Journal(tmp_path / "nowhere" / "j.jsonl")
    p = propose(sealed_ledger(), "plan-1", journal=j, clock=lambda: T0, quotes=quotes_at(T0),
                events=events_clear())
    assert p.ok, p.reason
    assert not (tmp_path / "nowhere").exists()


def test_192_blocked_a_write_still_creates_the_journal(tmp_path):
    j = Journal(tmp_path / "new" / "j.jsonl")
    j.append({"status": "pass"})
    assert j.path.exists() and j.lock_path.exists()


def test_192_blocked_repair_of_a_missing_path_is_an_error_and_creates_nothing(tmp_path, capsys):
    typo = tmp_path / "typo" / "decisons.jsonl"
    assert repair_main(["--path", str(typo), "--yes"]) == 2
    assert "no journal at" in capsys.readouterr().err
    assert not (tmp_path / "typo").exists()


def test_192_blocked_repair_warns_about_a_corrupt_line_mid_file(tmp_path, capsys):
    path = tmp_path / "j.jsonl"
    good = json.dumps(approved_entry(T0))
    path.write_text(f"{good}\nnot json\n[1, 2]\n{good}\n", encoding="utf-8")
    before = path.read_bytes()
    assert repair_main(["--path", str(path), "--yes"]) == BAD_LINES
    out = capsys.readouterr()
    assert "nothing to repair" in out.out
    assert "WARNING" in out.err and "line(s) 2, 3 mid-file" in out.err
    assert path.read_bytes() == before  # reported, never changed


def test_192_repair_moves_a_cut_off_tail_and_still_reports_a_bad_line(tmp_path, capsys):
    path = tmp_path / "j.jsonl"
    good = json.dumps(approved_entry(T0))
    path.write_bytes(f"{good}\n{{oops\n{good}\n".encode() + b'{"status": "appr')
    assert repair_main(["--path", str(path), "--yes"]) == BAD_LINES
    out = capsys.readouterr()
    assert "moved the cut-off last line" in out.out and "line(s) 2 mid-file" in out.err
    assert path.read_bytes().endswith(b"\n")


def test_192_a_clean_journal_still_repairs_with_exit_0(tmp_path, capsys):
    path = tmp_path / "j.jsonl"
    path.write_text(json.dumps(approved_entry(T0)) + "\n\n", encoding="utf-8")
    assert repair_main(["--path", str(path)]) == 0
    assert "WARNING" not in capsys.readouterr().err


def test_192_a_journal_failure_after_a_yes_says_so(monkeypatch, tmp_path):
    def approve_then_fail(p, ledger, **k):
        k["ask"]("ticket")  # the human answers, then the journal fails
        raise JournalLockTimeout("journal lock timed out")

    monkeypatch.setattr(decide, "approve", approve_then_fail)
    result, journal, _, _ = run_scenario(monkeypatch, tmp_path, "clean-buy", answers="yes")
    assert result.outcome.status == decide.JOURNAL_LOCKED
    assert "journal unavailable after a yes" in result.outcome.reason
    assert journal.entries()[-1]["answer"] == "yes"


@pytest.mark.parametrize("answer", ["no", "", "YES", "yes please"])
def test_192_a_journal_failure_recording_a_decline_says_so(monkeypatch, tmp_path, answer):
    def approve_then_fail(p, ledger, **k):
        k["ask"]("ticket")
        raise JournalLockTimeout("journal lock timed out")

    monkeypatch.setattr(decide, "approve", approve_then_fail)
    result, journal, _, _ = run_scenario(monkeypatch, tmp_path, "clean-buy",
                                         answers=lambda _q: answer)  # "" stays ""
    assert "journal unavailable recording a decline" in result.outcome.reason
    assert journal.entries()[-1]["answer"] == "declined"


def test_192_a_real_decline_whose_record_fails_is_reported_as_a_decline(monkeypatch, tmp_path):
    """Through the real approve(): the decline's own append fails."""
    real_append = Journal.append

    def broken(self, entry):
        if entry["status"] == "declined":
            raise JournalError("journal write failed (PermissionError)")
        return real_append(self, entry)

    monkeypatch.setattr(Journal, "append", broken)
    result, journal, _, _ = run_scenario(monkeypatch, tmp_path, "clean-buy", answers="no")
    assert result.outcome.status == decide.JOURNAL_ERROR
    assert "recording a decline" in result.outcome.reason


# ======================= #197: the paper account check =======================

class Resp:
    status = 200

    def __init__(self, body, url):
        self.b, self.u = body, url

    def read(self, n=None):
        return self.b

    def geturl(self):
        return self.u

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def account_broker(monkeypatch, **over):
    from tests.order_helpers import BROKER_FIXTURES
    monkeypatch.setenv("ALPACA_API_KEY", "SENTINEL-KEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SENTINEL-SECRET")
    account = json.loads((BROKER_FIXTURES / "account.json").read_text(encoding="utf-8"))
    for k, v in over.items():
        if v is ...:
            account.pop(k)
        else:
            account[k] = v
    body = json.dumps(account).encode()
    return AlpacaPaperBroker(LIMITS, opener=lambda req, timeout=None: Resp(body, req.full_url))


def test_197_a_long_only_account_that_is_not_suspended_verifies(monkeypatch):
    out = account_broker(monkeypatch).verify_paper_account()
    assert out["status"] == "ACTIVE"


@pytest.mark.parametrize("field,value,why", [
    ("trade_suspended_by_user", True, "suspended by the user"),
    ("trade_suspended_by_user", None, "suspended by the user"),
    ("trade_suspended_by_user", ..., "suspended by the user"),
    ("trade_suspended_by_user", "false", "suspended by the user"),
    ("shorting_enabled", True, "shorting enabled"),
    ("shorting_enabled", None, "shorting enabled"),
    ("shorting_enabled", ..., "shorting enabled"),
    ("shorting_enabled", 0, "shorting enabled"),
], ids=["suspended", "suspended-null", "suspended-missing", "suspended-text",
        "shorting", "shorting-null", "shorting-missing", "shorting-zero"])
def test_197_blocked_a_suspended_or_shortable_account_is_refused(monkeypatch, field, value, why):
    b = account_broker(monkeypatch, **{field: value})
    with pytest.raises(BrokerError, match=why):
        b.verify_paper_account()
    with pytest.raises(BrokerError, match="verify_paper_account"):
        b.list_positions()  # every later call refuses too


# ======================= review fixes =======================

def test_192_blocked_a_journal_whose_status_cannot_be_read_never_reads_as_empty(
        tmp_path, monkeypatch):
    """Only a MISSING file reads as empty; a permission error fails closed (S1)."""
    j = journal_with(tmp_path / "j.jsonl", [approved_entry(T0)])
    real = jl.os.stat

    def denied(p, *a, **k):
        if str(p) == str(j.path):
            raise PermissionError(13, "denied")
        return real(p, *a, **k)

    monkeypatch.setattr(jl.os, "stat", denied)
    for read in (j.entries, j.orders, lambda: j.day_state(T0), lambda: j.expire_unsent(T0)):
        with pytest.raises(JournalError, match="unreadable"):
            read()


def test_192_blocked_a_read_inside_the_lock_never_reads_an_unreadable_file_as_empty(
        tmp_path, monkeypatch):
    j = journal_with(tmp_path / "j.jsonl", [approved_entry(T0)])
    real_read = type(j.path).read_text

    def denied(self, *a, **k):
        if str(self) == str(j.path):
            raise PermissionError(13, "denied")
        return real_read(self, *a, **k)

    monkeypatch.setattr(type(j.path), "read_text", denied)
    with j.exclusive(), pytest.raises(JournalError, match="unreadable"):
        j.orders()


def test_191_a_file_that_appears_after_proposes_existence_check_is_still_read(
        tmp_path, limits):
    """propose's lock choice uses Path.exists(); the reads themselves check again (T5)."""
    path = tmp_path / "j.jsonl"
    journal_with(path, [approved_entry(T0, symbol="AAPL")])  # a live approval: 1 open

    class Lying(type(path)):
        def exists(self, *a, **k):
            return False

    j = Journal(path)
    j.path = Lying(str(path))
    p = propose(sealed_ledger(), "plan-1", journal=j, clock=lambda: T0, quotes=quotes_at(T0),
                events=events_clear())
    assert p.ok is False and p.stage == "risk" and "open position" in p.reason


def test_191_a_failed_append_inside_record_order_event_drops_the_held_replay(
        tmp_path, replays):
    a = approved_entry(T0)
    cid = a["client_order_id"]
    j = journal_with(tmp_path / "j.jsonl", [a])
    with j.exclusive():
        j.orders()
        replays["n"] = 0

        def broken():
            raise JournalError("journal unreadable (OSError)")

        j._ends_cut_off = broken
        with pytest.raises(JournalError):
            j.record_order_event(cid, "submitting", T0 + timedelta(seconds=1))
        del j._ends_cut_off
        assert j._replayed is None
        assert j.orders()[cid].status == APPROVED and replays["n"] == 1  # read the file again


def test_194_repair_reports_a_history_that_does_not_replay(tmp_path, capsys):
    a, cid, events = trade(T0)
    bad = ev(cid, "closed", T0 - timedelta(days=1), closed_by="stop", realized_pnl="-49.84")
    path = tmp_path / "j.jsonl"
    journal_with(path, [a, *events, bad])
    before = path.read_bytes()
    assert repair_main(["--path", str(path)]) == BAD_LINES
    err = capsys.readouterr().err
    assert "order history does not replay" in err and "before the order's previous event" in err
    assert path.read_bytes() == before


# ---- #194 on the sender path (S2) ----

def sent_setup(tmp_path):
    from pipeline.order_flow import approve
    led = sealed_ledger()
    j = Journal(tmp_path / "j.jsonl")
    p = propose(led, "plan-1", journal=j, clock=lambda: T0, quotes=quotes_at(T0),
                events=events_clear())
    out = approve(p, led, journal=j, ask=lambda _: "yes", clock=lambda: T0,
                  quotes=quotes_at(T0), events=events_clear(), sends=True)
    assert out.status == APPROVED
    return j, out


def test_194_blocked_a_send_clock_behind_the_approval_sends_nothing(tmp_path, limits):
    from pipeline.send import NOT_SENT, send_approved
    from tests.order_helpers import FakePaperBroker
    j, out = sent_setup(tmp_path)
    broker = FakePaperBroker()
    r = send_approved(out, journal=j, broker=broker, clock=lambda: T0 - timedelta(seconds=1))
    assert r.status == NOT_SENT and not r.journaled
    assert "before the order's previous event" in r.reason
    assert "submit" not in broker.names()
    assert [e["status"] for e in lines(j.path)] == [APPROVED]


def test_194_blocked_a_post_send_event_dated_before_submitting_alerts_and_stays_counted(
        tmp_path, limits, capsys):
    from pipeline.send import send_approved
    from tests.order_helpers import FakePaperBroker, StepClock
    j, out = sent_setup(tmp_path)
    t1 = T0 + timedelta(seconds=2)
    r = send_approved(out, journal=j, broker=FakePaperBroker(),
                      clock=StepClock(t1, t1 - timedelta(seconds=1)))
    assert r.status == "submitted" and r.alert and not r.journaled
    assert "ALERT" in capsys.readouterr().err
    assert [e["status"] for e in lines(j.path)] == [APPROVED, "submitting"]
    state = j.day_state(t1)
    assert state.open_positions == 1 and state.risk_used_today == D("49.84")


# ---- #192 through the real approve() (T3) ----

def test_192_a_lock_timeout_after_a_real_yes_says_after_a_yes(monkeypatch, tmp_path):
    import threading
    monkeypatch.setattr(jl, "LOCK_TIMEOUT_S", 0.3)
    path = tmp_path / "clean-buy.jsonl"
    holder = Journal(path)
    hold, release = threading.Event(), threading.Event()

    def keep_lock():
        with holder.exclusive():
            hold.set()
            release.wait(5)

    real_append = Journal.append

    def append_spy(self, entry):
        if entry["status"] == decide.JOURNAL_LOCKED:
            release.set()
        return real_append(self, entry)

    monkeypatch.setattr(Journal, "append", append_spy)

    def answer_then_lock(_question):
        threading.Thread(target=keep_lock, daemon=True).start()
        hold.wait(5)
        return "yes"

    result, journal, _, _ = run_scenario(monkeypatch, tmp_path, "clean-buy",
                                         answers=answer_then_lock)
    release.set()
    assert result.outcome.status == decide.JOURNAL_LOCKED
    assert "journal unavailable after a yes" in result.outcome.reason
    assert journal.entries()[-1]["answer"] == "yes"
