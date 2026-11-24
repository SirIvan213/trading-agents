"""run_day with the flat-by job (Phase 5 PR 5): it keeps the reconciler running after the
entry window until the account is flat or the market closes (#248), the reconciler flattens
at the flat-by (#135), the start-of-day broker check (#209), decision 8's block, and no send
from the flat-by on. Against the simulated Alpaca PAPER account (tests/test_reconcile.py),
with the fake clock of tests/test_run_day.py. No network."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import pipeline.run_day as rd
from tests.order_helpers import sent_order
from tests.test_flatten_job import CLOSE, Faulty
from tests.test_reconcile import (
    Sim,
    blocks,
    events,
    keys,  # noqa: F401  (autouse: sentinel Alpaca keys)
    sent,
    stray,
)
from tests.test_run_day import LIMITS, NY, QuietBroker, entries, make_day
from tools.broker import AlpacaPaperBroker, BrokerError

TUESDAY = datetime(2026, 11, 24, 15, 40, tzinfo=NY)  # after the entry window: no decisions


class Inline:
    """The reconcile thread, made deterministic: a pass when it starts, and one after every
    sleep of the fake clock (see paper_day), as the real thread does every 15 s."""

    def __init__(self, day, period=None):
        self.day = day
        self.stopped = False

    def start(self):
        rd.reconcile_pass(self.day)

    def is_alive(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


def paper_day(tmp_path, monkeypatch, *, start=TUESDAY, universe=("SA",)):
    sim = Sim()
    faulty = Faulty(sim)
    broker = AlpacaPaperBroker(LIMITS, opener=faulty)
    broker.verify_paper_account()
    monkeypatch.setattr(rd, "_Reconcile", Inline)
    day, clock, dec, out = make_day(tmp_path, monkeypatch, start=start,
                                    universe=list(universe), broker=broker)

    def sleep(s):
        clock.sleep(s)
        if day.reconcile_thread is not None and day.reconcile_thread.is_alive():
            rd.reconcile_pass(day)

    day.sleep = sleep
    return sim, faulty, day, clock, dec, out


def alerts(day):
    return [e["message"] for e in entries(day, "run_day_alert")]


def holding(sim, day, at):
    cid = sent(day.journal, at=at)
    sim.add_entry(cid)
    sim.fill(cid)
    return cid


# ======================= #248: never exit while a position is open =======================

def test_run_day_waits_for_the_flat_by_flattens_and_only_then_exits(tmp_path, monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch)
    cid = holding(sim, day, TUESDAY - timedelta(minutes=20))
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert dec.calls == []  # the entry window had closed
    assert sim.positions == {} and sim.writes.count(CLOSE) == 1
    (end,) = entries(day, "run_day_end")
    assert datetime.fromisoformat(end["at"]) >= TUESDAY.replace(minute=55)  # not before
    assert end["reason"] == "entry window closed"
    statuses = [e["status"] for e in day.journal.entries()]
    assert statuses.index("run_day_end") > max(
        i for i, e in enumerate(day.journal.entries()) if e.get("client_order_id") == cid)
    assert events(day.journal, cid)[-1]["status"] == "closed"
    assert blocks(day.journal) == []
    assert not any("exits with" in a for a in alerts(day))


def test_not_flat_at_the_close_alerts_three_minutes_before_and_on_exit(tmp_path, monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch)
    holding(sim, day, TUESDAY - timedelta(minutes=20))
    sim.close_fills = False  # the close never fills
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    (end,) = entries(day, "run_day_end")
    assert datetime.fromisoformat(end["at"]) >= TUESDAY.replace(hour=16, minute=0)
    msgs = alerts(day)
    warn = next(i for i, m in enumerate(msgs) if "not flat 3 minutes before the close" in m)
    exit_ = next(i for i, m in enumerate(msgs) if "run_day exits with this still open" in m)
    assert warn < exit_ and "SPY position" in msgs[exit_]
    assert "MANUAL ACTION NEEDED" in msgs[exit_]
    assert sim.writes.count(CLOSE) == 1  # the working close is never sent again


def test_a_flat_account_exits_at_once_after_the_window(tmp_path, monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch)
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert clock() == TUESDAY  # nothing open: no wait
    assert "the paper account is flat: no position and no open order" in out


def test_a_cap_ends_decisions_but_not_the_watch(tmp_path, monkeypatch):
    """The decisions stop at the cap; the position opened earlier is still flattened."""
    start = TUESDAY.replace(hour=15, minute=20)
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=start)
    holding(sim, day, start - timedelta(minutes=5))
    monkeypatch.setattr(rd, "_stop_reason", lambda budget: "decision cap reached (test)")
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert entries(day, "run_day_end")[0]["reason"] == "decision cap reached (test)"
    assert sim.positions == {}


def test_a_stale_reconciler_after_the_window_alerts_and_run_day_makes_the_pass(tmp_path,
                                                                               monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch)
    holding(sim, day, TUESDAY - timedelta(minutes=20))
    day.sleep = clock.sleep  # the thread makes no pass any more
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert any("after the entry window: the reconciler's last pass" in a
               for a in alerts(day))
    assert sim.positions == {}  # run_day's own passes flattened at the flat-by


# ======================= #209: the start-of-day broker check =======================

def test_a_position_at_the_broker_no_order_today_explains_holds_and_blocks(tmp_path,
                                                                          monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch,
                                                  start=TUESDAY.replace(hour=9, minute=30))
    sim.positions["SPY"] = 5  # overnight, and the journal does not know it
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert sim.writes == [] and sim.positions == {"SPY": 5}  # never closed (decision 8)
    assert dec.calls == [] and day.reconcile_thread is None
    (b,) = blocks(day.journal)
    assert b["kind"] == "orphan" and b["key"] == "start-of-day:SPY"
    assert "not closed automatically" in b["reason"]
    assert any("at the broker, not from an order sent today" in a for a in alerts(day))


def test_an_open_order_from_an_earlier_day_holds_and_blocks(tmp_path, monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch,
                                                  start=TUESDAY.replace(hour=9, minute=30))
    sim.add_entry("yesterdays-entry", symbol="AAPL")  # working, not in the journal
    sim.extra_open.append(stray(symbol="MSFT"))  # and one that cannot be read
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert sim.writes == []
    assert sorted(b["kind"] for b in blocks(day.journal)) == ["orphan", "unreadable_order"]


def test_a_broker_that_cannot_be_checked_holds_the_day(tmp_path, monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch,
                                                  start=TUESDAY.replace(hour=9, minute=30))
    faulty.add("GET", "/v2/positions", "500")
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert dec.calls == [] and day.reconcile_thread is None
    assert any("start-of-day broker check failed" in a for a in alerts(day))


def test_a_restart_with_todays_position_is_not_held(tmp_path, monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch)
    holding(sim, day, TUESDAY - timedelta(hours=2))  # sent today, before the restart
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert not any(b["key"].startswith("start-of-day") for b in blocks(day.journal))


def test_the_journal_held_check_also_blocks(tmp_path, monkeypatch):
    """Decision 8's kill switch: the block stops every entry until Ivan resolves it, on later
    days too."""
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])
    friday = datetime(2026, 11, 20, 10, 0, tzinfo=NY)
    for e in sent_order(friday):
        day.journal.append(e)
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    (b,) = blocks(day.journal)
    assert b["kind"] == "unprotected" and b["key"].startswith("earlier-day:")
    assert "not closed automatically" in b["reason"]
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert len(blocks(day.journal)) == 1  # the same block, never twice


# ======================= no send from the flat-by on =======================

@pytest.mark.parametrize("at, refused", [((15, 54, 59), False), ((15, 55, 0), True)])
def test_the_guard_refuses_every_send_from_the_flat_by(tmp_path, monkeypatch, at, refused):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, broker=QuietBroker(),
                                    start=TUESDAY.replace(hour=at[0], minute=at[1],
                                                          second=at[2]))
    day.reconciler = object()
    day.reconciled_at = clock()
    day.limits = LIMITS
    guard = rd.GuardedBroker(day)
    if refused:
        with pytest.raises(BrokerError, match="the flat-by time has passed"):
            guard.verify_paper_account()
    else:
        assert guard.verify_paper_account()["status"] == "ACTIVE"


def test_the_guard_uses_the_early_close_flat_by(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, broker=QuietBroker(),
                                    start=datetime(2026, 11, 27, 12, 55, tzinfo=NY))
    day.reconciler = object()
    day.reconciled_at = clock()
    day.limits = LIMITS
    with pytest.raises(BrokerError, match="flat-by"):
        rd.GuardedBroker(day).verify_paper_account()
