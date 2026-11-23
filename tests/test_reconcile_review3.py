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


# ---- the fourth review's items --------------------------------------------------------------

def test_cancel_entry_refuses_an_earlier_days_symbol(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    old = sent(j, at=T0 - timedelta(days=1))
    sim.add_entry(old)
    sim.fill(old)
    j.record_order_event(old, "filled", T0 - timedelta(days=1, seconds=-5), filled_qty=1,
                         avg_price="718.32")
    cid = sent(j)  # today, same symbol, unfilled: its 60 s cancel would apply
    sim.add_entry(cid)
    clock.step(61)
    r.pass_once()
    assert ("DELETE", f"/v2/orders/{sim.entries[cid]['id']}") not in sim.writes
    assert any("would cancel an entry" in b["reason"] for b in blocks(j))


def test_two_orphan_orders_in_one_symbol_flatten_it_once(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    for n in (1, 2):
        e = sim.add_entry(f"foreign-{n}")
        sim.entries.pop(f"foreign-{n}")
        sim.extra_open.append(e)
        sim.entries[f"hidden-{n}"] = e
    r.pass_once()
    assert len([b for b in blocks(j) if b["kind"] == "orphan"]) == 2
    assert len([a for a in r.report["actions"] if a.startswith("cancel")]) == 2


def test_a_block_field_never_overrides_the_entrys_keys(tmp_path):
    from journal.log import Journal
    j = Journal(tmp_path / "b.jsonl")
    bid = j.add_block("orphan", "k", "r", T0, status="pass", block_id="x")
    (e,) = j.entries()
    assert (e["status"], e["kind"], e["block_id"]) == ("reconcile_block", "orphan", bid)


def test_an_exit_without_a_fill_time_never_wins_closed_by(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    sim.add_entry(cid, qty=10)
    sim.fill(cid)
    r.pass_once()
    tp, stop = sim.leg(cid, "limit"), sim.leg(cid, "stop")
    tp.update(status="partially_filled", filled_qty="4", filled_avg_price="721.72",
              filled_at=None)
    stop.update(status="filled", filled_qty="6", filled_avg_price="714.52",
                filled_at="2026-11-24T15:40:00Z")
    sim.positions.pop("SPY")
    clock.step(15)
    r.pass_once()
    assert events(j, cid)[-1]["closed_by"] == "stop"


def test_done_for_the_day_with_no_fill_is_a_released_expiry(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid, status="done_for_day")
    r.pass_once()
    last = events(j, cid)[-1]
    assert (last["status"], last["filled_qty"]) == ("expired", 0)
    assert j.day_state(clock()).risk_used_today == 0


def test_a_fill_of_fewer_shares_than_the_order_marked_filled_is_a_conflict(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    e = sim.add_entry(cid, qty=10)
    e.update(status="filled", filled_qty="7", filled_avg_price="718.32")
    r.pass_once()
    assert any(b["kind"] == "conflict" and "7 of 10" in b["reason"] for b in blocks(j))


def test_check_shows_a_position_waiting_for_its_legs(tmp_path, capsys):
    import json

    import pipeline.reconcile as rc
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "limit")["status"] = "held"
    rc.main(["--check"], broker=r.broker, journal=j, clock=clock)
    report = json.loads(capsys.readouterr().out)
    assert any("still activating" in w for w in report["would"])


def test_a_rejection_with_a_fill_is_a_conflict(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    e = sim.add_entry(cid, qty=10)
    e.update(status="rejected", filled_qty="3", filled_avg_price="718.32")
    r.pass_once()
    assert any(b["key"] == f"qty:{cid}:rejected" for b in blocks(j))


def test_a_rejection_after_a_recorded_partial_fill_is_a_conflict(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    e = sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    r.pass_once()
    e["status"] = "rejected"
    clock.step(5)
    r.pass_once()
    assert any(b["key"] == f"qty:{cid}:rejected" for b in blocks(j))


def test_a_partial_then_full_fill_raises_no_quantity_conflict(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    r.pass_once()
    sim.fill(cid, qty=10)
    clock.step(5)
    r.pass_once()
    assert statuses(j, cid)[-1] == "filled"
    assert not any(b["key"].startswith("qty:") for b in blocks(j))
