"""The reconciler's third review round (Phase 5 PR 4, #155). Simulated paper account from
tests/test_reconcile.py; no network."""
from __future__ import annotations

from datetime import timedelta

from tests.test_reconcile import (
    T0,
    blocks,
    events,
    keys,  # noqa: F401 - autouse: the env keys the client needs
    sent,
    statuses,
    stray,
    world,
)


def test_yesterdays_released_order_never_stops_todays_flatten(tmp_path):
    """The blocker: Monday's SPY entry was cancelled unfilled (released). On Tuesday SPY
    fills and its stop is gone: it must be flattened, never refused as an earlier day's."""
    sim, j, clock, r, alerts = world(tmp_path)
    old = sent(j, at=T0 - timedelta(days=1))
    j.record_order_event(old, "cancelled", T0 - timedelta(days=1, seconds=-70),
                         filled_qty=0)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"
    r.pass_once()
    assert ("DELETE", "/v2/positions/SPY") in sim.writes and sim.positions == {}
    assert not any("earlier day" in b["reason"] for b in blocks(j))


def test_yesterdays_open_order_still_holds_the_symbol(tmp_path):
    """The other side: an earlier day's order that is still open keeps the symbol held."""
    sim, j, clock, r, alerts = world(tmp_path)
    old = sent(j, at=T0 - timedelta(days=1))
    sim.add_entry(old)
    sim.fill(old)
    j.record_order_event(old, "filled", T0 - timedelta(days=1, seconds=-5), filled_qty=1,
                         avg_price="718.32")
    sim.leg(old, "stop")["status"] = "expired"
    r.pass_once()
    assert sim.writes == [] and any("earlier day" in b["reason"] for b in blocks(j))


def test_a_partial_fill_done_for_the_day_is_recorded_as_expired(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    e = sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    r.pass_once()
    e["status"] = "done_for_day"
    clock.step(15)
    r.pass_once()
    last = [x for x in events(j, cid) if x["status"] == "expired"]
    assert last and last[0]["filled_qty"] == 4 and last[0]["broker_status"] == "done_for_day"


def test_a_final_status_the_journal_cannot_follow_alerts_once_it_lasts(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    e = sim.add_entry(cid)
    e["status"] = "replaced"  # this system never replaces an order (no fill, no position)
    for _ in range(10):
        clock.step(15)
        r.pass_once()
    assert any("replaced at the broker" in a for a in alerts)


def test_legs_still_held_after_the_fill_get_one_poll_to_activate(tmp_path):
    """Alpaca may show filled / held / held for a moment (the probe saw held/held, then
    new/held): one poll of grace before it counts as unprotected."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "limit")["status"] = "held"  # not activated yet
    r.pass_once()
    assert blocks(j) == [] and sim.writes == []
    sim.leg(cid, "limit")["status"] = "new"  # activated
    clock.step(15)
    r.pass_once()
    assert blocks(j) == [] and sim.writes == []


def test_legs_that_never_activate_are_unprotected_after_one_poll(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "limit")["status"] = "held"
    r.pass_once()
    clock.step(16)
    r.pass_once()
    assert [b["kind"] for b in blocks(j)] == ["unprotected"]


def test_a_close_with_an_unreadable_history_is_flagged(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    r.pass_once()
    sim.fill_leg(cid, "stop", "714.52")
    sim.closes.append(stray(symbol="SPY", status="canceled", id="odd-9"))
    clock.step(15)
    r.pass_once()
    closed = events(j, cid)[-1]
    assert closed["status"] == "closed" and closed["history_unreadable"] is True
    assert statuses(j, cid)[-1] == "closed"


def test_run_day_sends_the_reconcilers_alerts_to_its_console(tmp_path, monkeypatch):
    import pipeline.run_day as rd
    from tests.test_run_day import LIMITS, QuietBroker, make_day
    day, clock, dec, out = make_day(tmp_path, monkeypatch, broker=QuietBroker())
    import sys
    import types
    for name in rd.SENDING_PREREQUISITES:
        mod = types.ModuleType(name)
        mod.RUN_DAY_READY = True
        monkeypatch.setitem(sys.modules, name, mod)
    real = rd.Reconciler.pass_once

    def alerting(self):
        self._print_alert("a test alert")
        return real(self)

    monkeypatch.setattr(rd.Reconciler, "pass_once", alerting)
    rd.run_day(day, limits=LIMITS)
    assert any(line == "\aALERT: a test alert" for line in out)
