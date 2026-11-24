"""The reconciler's second review round (Phase 5 PR 4, #155): an earlier day's position is
never acted on by any path, nothing stays quiet, and the smaller fixes. Simulated paper
account from tests/test_reconcile.py; no network."""
from __future__ import annotations

import io
import urllib.error
from datetime import timedelta

import pipeline.run_day as rd
from journal.log import Journal
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
from tools.protection import PROTECTED, stop_status


def yesterdays_position(sim, j):
    cid = sent(j, at=T0 - timedelta(days=1))
    sim.add_entry(cid)
    sim.fill(cid)
    j.record_order_event(cid, "filled", T0 - timedelta(days=1, seconds=-5), filled_qty=1,
                         avg_price="718.32")
    for g in sim.entries[cid]["legs"]:
        g["status"] = "expired"
    return cid


# ---- an earlier day's position: no path acts on it ---------------------------------------

def test_a_hand_placed_sell_on_an_earlier_days_symbol_is_never_cancelled_or_closed(tmp_path):
    """The review's probe: Ivan follows the alert and places a limit sell by hand; the
    next pass must not cancel it as an orphan nor close the position at market."""
    sim, j, clock, r, alerts = world(tmp_path)
    yesterdays_position(sim, j)
    hand = sim.add_entry("ivan-by-hand")
    sim.entries.pop("ivan-by-hand")
    hand.update(side="sell", type="limit", order_class="", legs=None, status="new")
    sim.extra_open.append(hand)
    for _ in range(3):
        r.pass_once()
        clock.step(15)
    assert sim.writes == [] and sim.positions == {"SPY": 1}  # nothing acted on
    assert any("earlier day" in b["reason"] for b in blocks(j))


def test_an_earlier_days_entry_unknown_to_the_broker_is_not_closed(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = yesterdays_position(sim, j)
    sim.entries.pop(cid)  # the broker answers 404 for it
    r.pass_once()
    assert sim.writes == [] and sim.positions == {"SPY": 1}


def test_a_held_day_starts_no_reconciler_and_closes_nothing(tmp_path, monkeypatch):
    """run_day holds the day when an order from an earlier day may still hold a position:
    no decision, and the reconciler never acts that day (it starts after the held check)."""
    import sys
    import types

    from tests.test_run_day import LIMITS, make_day
    for name in rd.SENDING_PREREQUISITES:
        mod = types.ModuleType(name)
        mod.RUN_DAY_READY = True
        monkeypatch.setitem(sys.modules, name, mod)

    class Recording:
        calls: list = []

        def __getattr__(self, name):
            def call(*a, **k):
                Recording.calls.append(name)
                return {"orders": [], "unreadable": []} if name.endswith("checked") else []
            return call

    day, clock, dec, out = make_day(tmp_path, monkeypatch, broker=Recording())
    when = clock() - timedelta(days=3)
    j = day.journal
    cid = sent(j, at=when)
    j.record_order_event(cid, "filled", when + timedelta(seconds=5), filled_qty=1,
                         avg_price="718.32")
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert day.reconcile_thread is None
    assert not [c for c in Recording.calls
                if c in ("cancel_order", "close_position", "close_all", "submit_bracket")]


# ---- nothing stays quiet ------------------------------------------------------------------

def test_an_unreadable_old_order_never_stalls_a_close_quietly(tmp_path):
    """The review's probe: one unreadable order in the symbol's history made the strict
    listing fail every pass, and the stop's loss was never recorded."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    r.pass_once()
    sim.fill_leg(cid, "stop", "714.52")
    sim.closes.append(stray(symbol="SPY", status="canceled", id="odd-9"))  # in the history
    clock.step(15)
    r.pass_once()
    closed = events(j, cid)[-1]
    assert closed["status"] == "closed" and closed["realized_pnl"] == "-3.80"
    assert any(b["kind"] == "unreadable_order" for b in blocks(j))


def test_a_history_that_cannot_be_read_alerts_once_it_lasts(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    r.pass_once()
    sim.fill_leg(cid, "stop", "714.52")
    real = sim.__call__

    def history_down(req, timeout=None):
        if "status=all" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b"{}"))
        return real(req, timeout)

    r.broker._open = history_down
    for _ in range(10):  # 150 s
        clock.step(15)
        r.pass_once()
    assert any("recent orders cannot be read" in a for a in alerts)
    assert statuses(j, cid)[-1] == "filled"  # not closed on a guess


def test_a_cancel_that_keeps_failing_alerts_once_it_lasts(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim._cancel = lambda oid: (500, {"code": 50010000})
    clock.step(61)
    for _ in range(10):
        r.pass_once()
        clock.step(15)
    assert any("cancel" in a and "failed" in a for a in alerts)


def test_the_stuck_timer_clears_once_flat(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"
    sim.close_fills = False
    r.pass_once()  # the close is placed and working
    assert "SPY" in r.stuck_since
    sim.closes[-1].update(status="filled", filled_qty="1", filled_avg_price="718.79")
    sim.positions.pop("SPY")
    clock.step(15)
    r.pass_once()
    assert "SPY" not in r.stuck_since


def test_before_the_open_nothing_is_queued_it_blocks_and_alerts(tmp_path):
    """#256 (Ivan, replacing #251's "a close queued for the open"): before the open every
    position is an earlier day's (decision 8), so nothing is cancelled or closed. One block,
    and the alert repeats every pass."""
    sim, j, clock, r, alerts = world(tmp_path)
    clock.t = T0.replace(hour=8)  # before the open
    sim.positions["SPY"] = 1  # an orphan
    for _ in range(8):  # 2 minutes, 8 passes
        before = len(alerts)
        r.pass_once()
        assert any("MANUAL ACTION NEEDED" in a for a in alerts[before:])
        clock.step(15)
    assert sim.writes == []
    assert "outside-session:SPY" in [b["key"] for b in blocks(j)]


# ---- smaller fixes -------------------------------------------------------------------------

def test_a_broker_cancel_without_a_fill_of_a_rejected_order_is_not_a_conflict(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, status="submitting")
    j.record_order_event(cid, "rejected", T0 - timedelta(seconds=5), reason="422")
    sim.add_entry(cid, status="canceled")
    r.pass_once()
    assert blocks(j) == []


def test_a_listed_order_whose_client_order_id_is_ours_is_never_an_orphan(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    e = sim.add_entry(cid)
    sim.entries.pop(cid)  # the lookup disagrees with the listing (404)
    sim.extra_open.append(e)
    r.pass_once()
    assert "orphan" not in [b["kind"] for b in blocks(j)]
    assert ("DELETE", f"/v2/orders/{e['id']}") not in sim.writes


def test_a_stop_shrunk_after_a_partial_take_profit_is_still_protected():
    from tests.test_protection_realized import FILLED, entry
    e = entry(FILLED, filled="10",
              stop={"qty": "6", "filled_qty": "0", "status": "held"},
              tp={"qty": "10", "filled_qty": "4", "status": "partially_filled"})
    assert stop_status(e, "6")["state"] == PROTECTED


def test_send_flags_a_journal_that_reads_a_different_final_state(tmp_path):
    from pipeline.send import SEND_UNKNOWN, SendResult, _record
    j = Journal(tmp_path / "s.jsonl")
    cid = sent(j, status="submitting")
    j.record_order_event(cid, "submitted", T0, broker_order_id="ord-1")  # the reconciler
    # submitted -> send_unknown is not a step the journal allows: refused
    out = _record(j, cid, SEND_UNKNOWN, lambda: T0 + timedelta(seconds=1),
                  SendResult(SEND_UNKNOWN, "unclear", cid))
    assert out.alert is True and out.status == "submitted"
    assert "the journal already reads submitted" in out.reason
