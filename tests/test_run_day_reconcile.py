"""run_day with the reconciler (Phase 5 PR 4, #155; Ivan's answer 3): a thread every 15 s
plus a pass before each decision; its last-pass time is in the heartbeat; when it is more
than 45 s old or the thread died, run_day starts no decision and refuses every send. All
fakes: run_day still refuses a real broker until PR 5 (#135)."""
from __future__ import annotations

import json
import sys
import types
from datetime import timedelta

import pytest

import pipeline.run_day as rd
from tests.test_run_day import LIMITS, QuietBroker, make_day
from tools.broker import BrokerError


@pytest.fixture
def ready(monkeypatch):
    for name in rd.SENDING_PREREQUISITES:
        mod = types.ModuleType(name)
        mod.RUN_DAY_READY = True
        monkeypatch.setitem(sys.modules, name, mod)


def test_a_pass_runs_before_each_decision_and_lands_in_the_heartbeat(tmp_path, monkeypatch,
                                                                     ready):
    passes = []
    real = rd.Reconciler.pass_once

    def counted(self, **kw):
        passes.append(self.clock())
        return real(self, **kw)

    monkeypatch.setattr(rd.Reconciler, "pass_once", counted)
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA", "SB"],
                                    broker=QuietBroker())
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert len(dec.brokers) == 2
    assert len(passes) >= 3  # the thread's first pass, one before each decision, the last
    hb = json.loads((tmp_path / "state" / "run_day.heartbeat.json").read_text("utf-8"))
    assert hb["reconciled_at"] is not None


def test_a_failing_reconciler_stops_new_decisions_and_alerts(tmp_path, monkeypatch, ready):
    def broken(self, **kw):
        raise RuntimeError("the broker is unreachable")

    monkeypatch.setattr(rd.Reconciler, "pass_once", broken)
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA", "SB"],
                                    broker=QuietBroker())
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert dec.brokers == []  # no decision started
    assert any("no new decision: the reconciler has not completed a pass yet" in line
               for line in out)
    alerts = [e for e in day.journal.entries() if e["status"] == "run_day_alert"]
    assert any("reconcile pass failed" in a["message"] for a in alerts)


def test_a_stale_or_dead_reconciler_is_a_problem(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, broker=QuietBroker())
    day.reconciler = object()
    assert rd.reconcile_problem(day) == "the reconciler has not completed a pass yet"
    day.reconciled_at = clock()
    assert rd.reconcile_problem(day) is None
    clock.now = clock.now + timedelta(seconds=45)
    assert rd.reconcile_problem(day) is None  # 45 s is still fresh
    clock.now = clock.now + timedelta(seconds=1)
    assert "46 s ago" in rd.reconcile_problem(day)

    class Dead:
        def is_alive(self):
            return False

    day.reconciled_at = clock()
    day.reconcile_thread = Dead()
    assert rd.reconcile_problem(day) == "the reconciler thread has stopped"


def test_no_broker_means_nothing_to_watch(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch)
    assert rd.reconcile_problem(day) is None


def test_the_guard_refuses_a_send_while_the_reconciler_is_not_watching(tmp_path, monkeypatch):
    """pipeline/send.py checks the paper account before any POST: refused here, the order
    is journaled not sent."""
    day, clock, dec, out = make_day(tmp_path, monkeypatch, broker=QuietBroker())
    day.reconciler = object()
    guard = rd.GuardedBroker(day)
    with pytest.raises(BrokerError, match="send refused: the reconciler has not completed"):
        guard.verify_paper_account()
    day.reconciled_at = clock()
    assert guard.verify_paper_account()["status"] == "ACTIVE"
    assert guard.list_positions() == []  # everything else passes through
    clock.now = clock.now + timedelta(seconds=60)
    with pytest.raises(BrokerError, match="more than 45"):
        guard.verify_paper_account()


def test_the_guard_makes_send_journal_the_order_not_sent(tmp_path, monkeypatch):
    from journal.log import Journal
    from pipeline.send import NOT_SENT, send_approved
    from tests.order_helpers import T0, StepClock, sealed_ledger
    from tests.test_send import approved, cid_of
    j = Journal(tmp_path / "send.jsonl")
    out = approved(sealed_ledger(), j)
    day, clock, dec, lines = make_day(tmp_path, monkeypatch, broker=QuietBroker())
    day.reconciler = object()  # never passed: not watching
    r = send_approved(out, journal=j, broker=rd.GuardedBroker(day),
                      clock=StepClock(T0 + timedelta(seconds=30)))
    assert r.status == NOT_SENT and "send refused" in r.reason
    last = [e for e in j.entries() if e.get("client_order_id") == cid_of(out)][-1]
    assert last["status"] == "expired" and last["filled_qty"] == 0
