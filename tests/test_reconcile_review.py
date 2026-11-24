"""The reconciler's review fixes (Phase 5 PR 4, #155): each case the code-reviewer found,
against the simulated paper account of tests/test_reconcile.py. No network."""
from __future__ import annotations

import io
import json
import threading
import urllib.error
from datetime import timedelta

import pytest

import pipeline.reconcile as rc
from journal.log import JournalLockTimeout
from pipeline.filelock import exclusive_file_lock
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

# ---- B1: a pass that cannot read everything is not a pass ---------------------------------

def test_a_failing_account_check_is_an_incomplete_pass_that_alerts(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    real = sim.__call__

    def account_down(req, timeout=None):
        if req.full_url.endswith("/v2/account"):
            raise urllib.error.HTTPError(req.full_url, 503, "x", {}, io.BytesIO(b"{}"))
        return real(req, timeout)

    r.broker._open = account_down
    report = r.pass_once()
    assert report["complete"] is False
    assert any("could not read everything" in a for a in alerts)


def test_the_reconciler_verifies_its_own_client_every_pass(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    r.broker._verified = False  # e.g. a decision's re-verify failed on a shared client
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    report = r.pass_once()
    assert report["complete"] is True and statuses(j, cid)[-1] == "filled"


@pytest.mark.parametrize("broken", ["/v2/positions", "/v2/orders?", "by_client_order_id"])
def test_a_failed_read_makes_the_pass_incomplete(tmp_path, broken):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    real = sim.__call__

    def fail(req, timeout=None):
        if broken in req.full_url and req.get_method() == "GET":
            raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b"{}"))
        return real(req, timeout)

    r.broker._open = fail
    assert r.pass_once()["complete"] is False
    assert any("could not read everything" in a for a in alerts)


# ---- B2: an order the broker rejected is not a conflict -----------------------------------

def test_a_broker_rejection_is_recorded_once_and_never_becomes_a_conflict(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid, status="rejected")
    for _ in range(4):
        r.pass_once()
        clock.step(15)
    assert statuses(j, cid)[-1] == "rejected"
    assert blocks(j) == [] and sim.writes == []


# ---- B3: an earlier day's position is never closed automatically --------------------------

def test_an_earlier_days_unprotected_position_is_blocked_not_closed(tmp_path):
    """Decision 8 and the PR 5 morning rule: kill switch, alert, wait for Ivan."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, at=T0 - timedelta(days=1))
    sim.add_entry(cid)
    sim.fill(cid)
    j.record_order_event(cid, "filled", T0 - timedelta(days=1, seconds=-5), filled_qty=1,
                         avg_price="718.32")
    for g in sim.entries[cid]["legs"]:
        g["status"] = "expired"  # day legs expired at yesterday's close
    for _ in range(3):
        r.pass_once()
        clock.step(15)
    (b,) = blocks(j)
    assert b["kind"] == "unprotected" and "earlier day" in b["reason"]
    assert "not closed automatically" in b["reason"]
    assert sim.writes == [] and sim.positions == {"SPY": 1}


# ---- B4: the reconciler and the sender recording the same fact ----------------------------

def test_a_stale_snapshot_versus_the_senders_record_is_not_a_conflict(tmp_path, monkeypatch):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, status="submitting")
    sim.add_entry(cid)
    real_orders = j.orders
    first = {"done": False}

    def snapshot_then_sender_writes():
        out = real_orders()
        if not first["done"]:  # the sender records "submitted" right after this read
            first["done"] = True
            j.record_order_event(cid, "submitted", T0, broker_order_id="ord-x")
        return out

    monkeypatch.setattr(j, "orders", snapshot_then_sender_writes)
    r.pass_once()
    assert blocks(j) == []
    assert statuses(j, cid).count("submitted") == 1


def test_the_sender_takes_the_reconcilers_record_as_done(tmp_path):
    """The other order of the race: the reconciler recorded "submitted" first; send's own
    record is then not an alert."""
    from journal.log import Journal
    from pipeline.send import SUBMITTED as SENT
    from pipeline.send import SendResult, _record
    j = Journal(tmp_path / "s.jsonl")
    cid = sent(j, status="submitting")
    j.record_order_event(cid, "submitted", T0, broker_order_id="ord-1")  # the reconciler
    result = SendResult(SENT, None, cid, "ord-1")
    out = _record(j, cid, SENT, lambda: T0 + timedelta(seconds=1), result,
                  broker_order_id="ord-1")
    assert out.alert is False and out.status == SENT


# ---- should-fix items -----------------------------------------------------------------------

def test_a_journal_lock_timeout_is_retried_not_blocked(tmp_path, monkeypatch):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    real = j.record_order_event
    busy = {"n": 1}

    def once_busy(*a, **k):
        if busy["n"]:
            busy["n"] -= 1
            raise JournalLockTimeout("journal lock timed out")
        return real(*a, **k)

    monkeypatch.setattr(j, "record_order_event", once_busy)
    r.pass_once()
    assert blocks(j) == []
    clock.step(15)
    r.pass_once()
    assert statuses(j, cid)[-1] == "filled"


def test_an_exit_between_the_reads_is_never_a_false_unprotected_block(tmp_path):
    """The target fills after this pass read the positions and before it looked the entry
    up: a filled leg is the bracket closing it, and the state is read again before acting."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    r.pass_once()
    real = sim.__call__
    state = {"positions_read": False}

    def exit_after_positions(req, timeout=None):
        resp = real(req, timeout)
        if req.full_url.endswith("/v2/positions") and not state["positions_read"]:
            state["positions_read"] = True
            sim.fill_leg(cid, "limit", "721.72")  # the target fills right after
        return resp

    r.broker._open = exit_after_positions
    clock.step(15)
    r.pass_once()
    assert blocks(j) == [] and ("DELETE", "/v2/positions/SPY") not in sim.writes
    clock.step(15)
    r.pass_once()
    assert events(j, cid)[-1]["closed_by"] == "target"


def test_a_lost_send_with_a_position_is_not_flattened_inside_its_grace(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    sent(j, status="submitting")
    sim.positions["SPY"] = 1  # the broker index lags: the order is not found yet
    r.pass_once()
    assert sim.writes == [] and blocks(j) == []


def test_a_flatten_that_is_not_done_alerts_every_pass_and_goes_loud(tmp_path):
    """Never quiet: cancels that never complete alert each pass, and after 60 s ask for
    manual action in the Alpaca paper dashboard."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"
    sim._cancel = lambda oid: (204, None)  # cancels accepted, never done
    loud = []
    for _ in range(6):
        before = len(alerts)
        r.pass_once()
        new = alerts[before:]
        assert any("SPY" in a and ("not done" in a or "MANUAL ACTION" in a) for a in new), new
        loud.append(any("MANUAL ACTION NEEDED" in a for a in new))
        clock.step(15)
    assert loud[:4] == [False] * 4 and loud[4] and loud[5]


@pytest.mark.parametrize("stop_qty, tp_qty", [("10", "10"), ("4", "10"), ("10", "4")])
def test_legs_not_resized_to_the_fill_are_unprotected(tmp_path, stop_qty, tp_qty):
    """A partial fill whose legs Alpaca left at 10 shares on a 4-share position: a
    triggered leg could oversell. Unprotected: flattened."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    e = sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    e["status"] = "canceled"
    sim.leg(cid, "stop").update(qty=stop_qty)
    sim.leg(cid, "limit").update(qty=tp_qty, status="new")
    r.pass_once()
    assert [b["kind"] for b in blocks(j)] == ["unprotected"]
    assert sim.positions == {}


def test_our_own_close_is_never_an_orphan_order_while_the_fill_is_unrecorded(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.close_fills = False
    sim.leg(cid, "stop")["status"] = "canceled"
    sim.leg(cid, "limit")["status"] = "canceled"
    sim._close("SPY")  # our close is working (placed by an earlier flatten)
    r.pass_once()
    assert "orphan" not in [b["kind"] for b in blocks(j)]


def test_a_fill_seen_during_the_cancel_confirmation_is_still_price_checked(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    e = sim.add_entry(cid)
    real = sim._cancel

    def fill_then_cancel(oid):
        if oid == e["id"]:
            sim.fill(cid, avg="719.00")  # filled above the 718.84 limit as it was cancelled
            return 422, {"code": 42210000}
        return real(oid)

    sim._cancel = fill_then_cancel
    clock.step(61)
    r.pass_once()
    assert "fill_above_limit" in [b["kind"] for b in blocks(j)]


def test_once_refuses_while_run_day_is_running(tmp_path, capsys):
    sim, j, clock, r, alerts = world(tmp_path)
    state = tmp_path / "state"
    held, release = threading.Event(), threading.Event()

    def run_day_holds_its_lock():
        with exclusive_file_lock(state / "run_day.lock"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=run_day_holds_its_lock)
    t.start()
    held.wait(5)
    try:
        code = rc.main(["--once"], broker=r.broker, journal=j, clock=clock, state_dir=state)
    finally:
        release.set()
        t.join()
    assert code == 4 and "run_day is running" in capsys.readouterr().err
    assert sim.writes == []
    assert rc.main(["--once"], broker=r.broker, journal=j, clock=clock, state_dir=state) == 0
    json.loads(capsys.readouterr().out)


def test_check_never_takes_the_journal_lock(tmp_path, capsys, monkeypatch):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)

    def no_lock(*a, **k):
        raise AssertionError("--check took the journal lock")

    monkeypatch.setattr(j, "exclusive", no_lock)
    assert rc.main(["--check"], broker=r.broker, journal=j, clock=clock) == 0
    assert json.loads(capsys.readouterr().out)["complete"] is True


def test_an_unreadable_order_after_resolution_blocks_again(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    sim.extra_open.append(stray())
    r.pass_once()
    (b,) = blocks(j)
    j.resolve_block(b["block_id"], "looked: it is still there", T0)
    clock.step(15)
    r.pass_once()
    assert len(blocks(j)) == 2  # the cause is still there: blocked again
