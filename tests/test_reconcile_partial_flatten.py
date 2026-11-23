"""Ivan's decision on #251: a partial fill whose legs Alpaca cancelled along with the
unfilled remainder only ALERTS (no block) when its flatten is CONFIRMED: check_close says
closed and the journal records the close with matching quantities. If the flatten fails,
is unclear, or the quantities don't match, it blocks as before. Simulated paper account
from tests/test_reconcile.py; no network."""
from __future__ import annotations

from tests.test_reconcile import (
    blocks,
    events,
    keys,  # noqa: F401 - autouse: the env keys the client needs
    sent,
    world,
)
from tools.broker import BrokerError


def partial_with_cancelled_legs(tmp_path):
    """4 of 10 filled; the remainder is cancelled after one poll and Alpaca cancels the legs
    with it (the simulated group cancel)."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    r.pass_once()
    clock.step(16)
    return sim, j, clock, r, alerts, cid


def kinds(j):
    return [b["kind"] for b in blocks(j)]


def test_a_confirmed_flatten_alerts_and_does_not_block(tmp_path):
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    r.pass_once()  # cancels the remainder, then flattens: the close fills
    assert sim.positions == {}
    assert any("no block if the close is confirmed" in a for a in alerts)
    assert kinds(j) == []
    clock.step(15)
    r.pass_once()  # the close is recorded with matching quantities; check_close: closed
    assert events(j, cid)[-1]["status"] == "closed"
    assert any("flatten is confirmed" in a and "no block" in a for a in alerts)
    assert kinds(j) == [] and j.day_state(clock()).reconcile_blocks == ()
    for _ in range(6):  # nothing blocks later either
        clock.step(15)
        r.pass_once()
    assert kinds(j) == []


def test_a_flatten_that_does_not_complete_blocks(tmp_path):
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    real = sim._close
    sim._close = lambda symbol: (500, {"code": 50010000})  # the close keeps failing
    r.pass_once()
    assert kinds(j) == ["unprotected"]
    assert any("its flatten was not confirmed" in b["reason"] for b in blocks(j))
    sim._close = real


def test_a_close_whose_quantities_do_not_match_blocks(tmp_path):
    """The close fills 3 of the 4 shares (the position is gone at the broker anyway): the
    journal cannot record a close with matching quantities, so after 60 s it blocks."""
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    real = sim._close

    def short_close(symbol):
        status, body = real(symbol)
        sim.closes[-1]["filled_qty"] = "3"
        return status, body

    sim._close = short_close
    r.pass_once()
    assert kinds(j) == []  # not yet: the close may still be settling
    for _ in range(4):  # 60 s
        clock.step(15)
        r.pass_once()
    assert kinds(j) == ["unprotected"]
    assert any("no confirmed close within 60 s" in b["reason"] for b in blocks(j))
    assert events(j, cid)[-1]["status"] != "closed"


def test_check_close_not_saying_closed_blocks(tmp_path):
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    r.pass_once()
    clock.step(15)
    real = r.broker.check_close

    def unclear(symbol):
        raise BrokerError("GET /v2/orders failed: HTTP 503", status=503, may_have_acted=True)

    r.broker.check_close = unclear
    r.pass_once()
    assert kinds(j) == ["unprotected"]
    assert any("check_close failed" in b["reason"] for b in blocks(j))
    r.broker.check_close = real


def test_an_ordinary_missing_stop_still_blocks_at_once(tmp_path):
    """Only the decided case changes: a full fill whose stop is gone blocks as before."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"
    r.pass_once()
    assert kinds(j) == ["unprotected"]


def test_a_partial_fill_with_legs_kept_is_not_the_decided_case(tmp_path):
    """Legs Alpaca kept (sized to the fill) are protected: nothing to flatten."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    e = sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    real = sim._cancel

    def keep_legs(oid):
        if oid == e["id"]:
            e["status"] = "canceled"
            for g in e["legs"]:
                g["qty"] = "4"
            sim.leg(cid, "limit")["status"] = "new"
            return 204, None
        return real(oid)

    sim._cancel = keep_legs
    r.pass_once()
    clock.step(16)
    r.pass_once()
    assert kinds(j) == [] and sim.positions == {"SPY": 4}
    assert not any("no block if the close is confirmed" in a for a in alerts)
