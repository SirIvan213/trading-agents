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
    assert any("its flatten was not confirmed" in b["reason"] for b in blocks(j))
    assert events(j, cid)[-1]["status"] != "closed"


def test_check_close_not_saying_closed_blocks(tmp_path):
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    real = r.broker.check_close

    def unclear(symbol):
        raise BrokerError("GET /v2/orders failed: HTTP 503", status=503, may_have_acted=True)

    r.broker.check_close = unclear
    r.pass_once()  # flattened and recorded, but check_close cannot confirm
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


# ---- the review of the decision: never neither confirmed nor blocked ------------------------

def fresh(r):
    """A new Reconciler on the same journal and broker: a restart, or a --once pass."""
    import itertools

    import pipeline.reconcile as rc
    return rc.Reconciler(r.journal, r.broker, clock=r.clock, limits=r.limits,
                         alert=r._print_alert, sleep=lambda s: None,
                         monotonic=itertools.count().__next__)


def test_a_restart_after_the_flatten_still_blocks_mismatched_quantities(tmp_path):
    """Decided from the broker and the journal, not memory: a fresh reconciler finds the
    close's quantities do not match and blocks after 60 s."""
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    real = sim._close

    def short_close(symbol):
        status, body = real(symbol)
        sim.closes[-1]["filled_qty"] = "3"
        return status, body

    sim._close = short_close
    r.pass_once()  # flattened; the close does not add up
    restarted = fresh(r)
    for _ in range(6):
        clock.step(15)
        restarted.pass_once()
    assert kinds(j) == ["unprotected"]
    assert any("do not add up" in b["reason"] for b in blocks(j))


def cancelled_together(sim, cid):
    """Alpaca cancelled the remainder and both legs at one moment (the decided case)."""
    e = sim.entries[cid]
    stamp = "2026-11-24T16:00:00Z"
    e.update(status="canceled", canceled_at=stamp)
    for g in e["legs"]:
        g.update(status="canceled", canceled_at=stamp)


def test_a_once_pass_blocks_what_it_could_not_confirm(tmp_path):
    """A --once pass is final: a flatten still closing at its end blocks."""
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    cancelled_together(sim, cid)
    sim.close_fills = False  # the close is placed but does not fill in this pass
    once = fresh(r)
    once.pass_once(final=True)
    assert kinds(j) == ["unprotected"]
    assert any("before the last pass ended" in b["reason"] for b in blocks(j))


def test_a_once_pass_that_confirms_does_not_block(tmp_path):
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    cancelled_together(sim, cid)
    once = fresh(r)
    once.pass_once(final=True)
    assert kinds(j) == [] and events(j, cid)[-1]["status"] == "closed"
    assert any("flatten is confirmed" in a for a in alerts)


def test_a_block_that_cannot_be_written_is_retried_not_dropped(tmp_path, monkeypatch):
    from journal.log import JournalLockTimeout
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    sim.close_fills = False
    r.pass_once()  # pending: the close is working
    real = j.add_block
    fails = {"n": 1}

    def busy_once(*a, **k):
        if fails["n"]:
            fails["n"] -= 1
            raise JournalLockTimeout("journal lock timed out")
        return real(*a, **k)

    monkeypatch.setattr(j, "add_block", busy_once)
    for _ in range(6):
        clock.step(15)
        r.pass_once()
    assert kinds(j) == ["unprotected"]


def test_a_confirmation_after_a_block_says_the_block_stays(tmp_path):
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    sim.close_fills = False
    r.pass_once()
    for _ in range(5):  # unconfirmed for 60 s: blocked
        clock.step(15)
        r.pass_once()
    assert kinds(j) == ["unprotected"]
    sim.closes[-1].update(status="filled", filled_qty="4", filled_avg_price="718.79")
    sim.positions.pop("SPY")
    clock.step(15)
    r.pass_once()
    assert any("its earlier block stays until resolved" in a for a in alerts)
    assert not any(a.endswith("no block") for a in alerts[-3:])


def test_legs_cancelled_later_by_a_flatten_are_not_the_decided_case(tmp_path):
    """The legs ended a minute after the entry (a later cancel, not with the remainder):
    an ordinary missing stop, blocked at once."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    e = sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    e.update(status="canceled", canceled_at="2026-11-24T16:00:00Z")
    for g in e["legs"]:
        g.update(status="canceled", canceled_at="2026-11-24T16:05:00Z")
    r.pass_once()
    assert kinds(j) == ["unprotected"]
    assert not any("no block if the close is confirmed" in a for a in alerts)


def test_check_mode_describes_the_decided_case_honestly(tmp_path, capsys):
    import json

    import pipeline.reconcile as rc
    sim, j, clock, r, alerts, cid = partial_with_cancelled_legs(tmp_path)
    cancelled_together(sim, cid)
    rc.main(["--check"], broker=r.broker, journal=j, clock=clock)
    report = json.loads(capsys.readouterr().out)
    assert any("blocks only if the close is not confirmed" in w for w in report["would"])
    assert not any(w.startswith("block new entries (unprotected)") for w in report["would"])
