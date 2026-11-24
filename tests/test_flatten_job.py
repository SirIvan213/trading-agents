"""The flat-by job (Phase 5 PR 5, #135): the reconciler's flat-by mode, the flat-by time from
the NYSE calendar, the unclear-reply paths of the shared flatten, an earlier day's position,
and `python -m pipeline.flatten --check | --now`. Against the simulated Alpaca PAPER account
of tests/test_reconcile.py (recorded reply shapes, tests/fixtures/broker/). No network."""
from __future__ import annotations

import io
import itertools
import json
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import pipeline.flatten as fl
import pipeline.reconcile as rc
import tools.config as tc
from journal.log import Journal
from tests.test_reconcile import (
    LIMITS,
    T0,
    Clock,
    Sim,
    blocks,
    events,
    keys,  # noqa: F401  (autouse: sentinel Alpaca keys)
    sent,
    statuses,
    stray,
)
from tools.broker import AlpacaPaperBroker
from tools.market_calendar import CalendarError

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
FLAT_BY = T0.replace(hour=15, minute=55)  # Tuesday 2026-11-24, a normal day
EARLY = datetime(2026, 11, 27, 12, 55, tzinfo=NY)  # the day after Thanksgiving: 13:00 close
CLOSE = ("DELETE", "/v2/positions/SPY")


class Faulty:
    """The simulated account with faults: each fault fires once, on the first request that
    matches its method and path. "lost": the broker ACTED, then the reply was lost (a
    timeout); "timeout": it timed out before acting; "500": a server error. `then`: faults
    armed once this one fires (to reach a later request of the same kind)."""

    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.faults: list[tuple[str, str, str, list]] = []

    def add(self, method: str, path: str, mode: str, then: list | None = None) -> None:
        self.faults.append((method, path, mode, then or []))

    def __call__(self, req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        for f in list(self.faults):
            method, prefix, mode, then = f
            if req.get_method() == method and path.startswith(prefix):
                self.faults.remove(f)
                self.faults.extend(then)
                if mode == "lost":
                    self.sim(req, timeout)
                    raise TimeoutError
                if mode == "timeout":
                    if method != "GET":
                        self.sim.writes.append((method, path))
                    raise TimeoutError
                raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b"{}"))
        return self.sim(req, timeout)


def world(tmp_path, at=FLAT_BY, **kw):
    sim = Sim()
    faulty = Faulty(sim)
    broker = AlpacaPaperBroker(LIMITS, opener=faulty)
    broker.verify_paper_account()
    j = Journal(tmp_path / "decisions.jsonl")
    clock = Clock(at)
    alerts: list[str] = []
    r = rc.Reconciler(j, broker, clock=clock, limits=LIMITS, alert=alerts.append,
                      sleep=lambda s: None, monotonic=itertools.count().__next__, **kw)
    return sim, faulty, j, clock, r, alerts


def holding(sim, j, clock, *, qty=1, filled=None, at=None):
    """Today's position: an entry sent before `at`, filled, its legs working."""
    cid = sent(j, qty=qty, at=at or clock.t - timedelta(minutes=30))
    sim.add_entry(cid, qty=qty)
    sim.fill(cid, qty=filled)
    return cid


def passes(r, clock, n, step=15):
    for _ in range(n):
        r.pass_once()
        clock.step(step)


# ======================= the flat-by time =======================

def test_the_flat_by_is_1555_on_a_normal_day():
    day = date(2026, 11, 24)
    assert fl.flat_by_at(day, LIMITS) == datetime(2026, 11, 24, 15, 55, tzinfo=NY)
    assert not fl.flat_by_reached(datetime(2026, 11, 24, 15, 54, 59, tzinfo=NY), LIMITS)
    assert fl.flat_by_reached(datetime(2026, 11, 24, 15, 55, tzinfo=NY), LIMITS)
    assert fl.flat_by_line(day, LIMITS) == "flat-by today (2026-11-24): 15:55 ET"


def test_the_flat_by_is_1255_on_an_early_close():
    day = date(2026, 11, 27)
    assert fl.flat_by_at(day, LIMITS) == EARLY
    assert not fl.flat_by_reached(EARLY - timedelta(seconds=1), LIMITS)
    assert fl.flat_by_reached(EARLY, LIMITS)
    assert "12:55 ET (early close at 13:00)" in fl.flat_by_line(day, LIMITS)


@pytest.mark.parametrize("utc, reached", [
    # Friday 2026-10-30, daylight time (UTC-4): 15:55 ET is 19:55 UTC
    (datetime(2026, 10, 30, 19, 54, tzinfo=UTC), False),
    (datetime(2026, 10, 30, 19, 55, tzinfo=UTC), True),
    # Monday 2026-11-02, the first day back on standard time (UTC-5): 15:55 ET is 20:55 UTC
    (datetime(2026, 11, 2, 19, 55, tzinfo=UTC), False),
    (datetime(2026, 11, 2, 20, 54, tzinfo=UTC), False),
    (datetime(2026, 11, 2, 20, 55, tzinfo=UTC), True),
    # Monday 2026-03-09, the first day on daylight time: 19:55 UTC again
    (datetime(2026, 3, 9, 19, 54, tzinfo=UTC), False),
    (datetime(2026, 3, 9, 19, 55, tzinfo=UTC), True),
])
def test_the_flat_by_follows_new_york_across_the_dst_changes(utc, reached):
    assert fl.flat_by_reached(utc, LIMITS) is reached


def test_an_uncovered_year_counts_as_reached_and_a_closed_day_does_not():
    with pytest.raises(CalendarError):
        fl.flat_by_at(date(2031, 6, 2), LIMITS)
    assert fl.flat_by_reached(datetime(2031, 6, 2, 10, 0, tzinfo=NY), LIMITS)  # fails safe
    assert "does not cover 2031" in fl.flat_by_line(date(2031, 6, 2), LIMITS)
    assert fl.flat_by_at(date(2026, 11, 28), LIMITS) is None  # a Saturday
    assert not fl.flat_by_reached(datetime(2026, 11, 28, 16, 0, tzinfo=NY), LIMITS)
    assert fl.flat_by_line(date(2026, 11, 26), LIMITS) == "no session today (Thanksgiving Day)"


# ======================= the flat-by pass =======================

def test_before_the_flat_by_a_protected_position_is_left_alone(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path, at=FLAT_BY - timedelta(seconds=15))
    holding(sim, j, clock)
    r.pass_once()
    assert r.report["flat_by"] is False
    assert sim.writes == [] and sim.positions == {"SPY": 1}


def test_at_the_flat_by_todays_position_is_flattened_and_recorded_without_a_block(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    cid = holding(sim, j, clock)
    r.pass_once()
    assert r.report["flat_by"] is True
    assert sim.positions == {}
    # the legs first (the group cancel), confirmed, then the close
    assert sim.writes[0][0] == "DELETE" and sim.writes[0][1].startswith("/v2/orders/")
    assert sim.writes[-1] == CLOSE and sim.writes.count(CLOSE) == 1
    clock.step(15)
    r.pass_once()
    last = events(j, cid)[-1]
    assert last["status"] == "closed" and last["closed_by"] == "flatten"
    assert blocks(j) == []  # the legs the flat-by cancelled are no reason to block


def test_on_an_early_close_the_flat_by_is_1255(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path, at=EARLY - timedelta(minutes=1))
    holding(sim, j, clock)
    r.pass_once()
    assert sim.writes == []  # 12:54: not yet
    clock.t = EARLY
    r.pass_once()
    assert sim.positions == {} and sim.writes.count(CLOSE) == 1
    assert blocks(j) == []


def test_at_the_flat_by_a_working_entry_is_cancelled_and_confirmed(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, at=clock.t)  # sent 10 s ago: the 60 s rule has not cancelled it
    sim.add_entry(cid)
    r.pass_once()
    assert sim.entries[cid]["status"] == "canceled"
    assert all(g["status"] == "canceled" for g in sim.entries[cid]["legs"])
    assert statuses(j, cid)[-1] == "cancelled"  # confirmed by lookup, then recorded
    assert CLOSE not in sim.writes and blocks(j) == []


def test_at_the_flat_by_a_partial_fill_is_cancelled_then_flattened(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=2, at=clock.t)
    sim.add_entry(cid, qty=2)
    sim.fill(cid, qty=1)
    passes(r, clock, 2)
    assert sim.entries[cid]["status"] == "canceled" and sim.positions == {}
    assert statuses(j, cid)[-2:] == ["cancelled", "closed"]
    assert blocks(j) == []  # confirmed: check_close says closed, quantities match


def test_a_close_that_does_not_fill_alerts_every_pass_then_says_manual_action(tmp_path):
    """Never silent (Ivan's answer h on #155): a flatten the flat-by started repeats its alert
    every pass, then after 60 s says MANUAL ACTION NEEDED. The working close is never sent
    again, and no "unprotected" block is opened for the cancelled legs."""
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    sim.close_fills = False
    loud = []
    for _ in range(6):
        before = len(alerts)
        r.pass_once()
        new = alerts[before:]
        assert any("SPY" in a for a in new), new
        loud.append(any("MANUAL ACTION NEEDED in the Alpaca paper dashboard" in a
                        for a in new))
        clock.step(15)
    assert loud[:4] == [False] * 4 and loud[4] and loud[5]
    assert sim.writes.count(CLOSE) == 1
    assert blocks(j) == []


def test_a_flatten_stuck_since_before_the_flat_by_is_loud_at_once(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path, at=FLAT_BY - timedelta(seconds=15))
    cid = holding(sim, j, clock)
    sim.leg(cid, "stop")["status"] = "canceled"  # unprotected: flattened before the flat-by
    sim.close_fills = False
    r.pass_once()
    assert not any("MANUAL ACTION" in a for a in alerts)
    clock.step(15)  # 15:55
    r.pass_once()
    assert any("MANUAL ACTION NEEDED" in a for a in alerts)


def test_an_earlier_days_position_is_never_closed_at_the_flat_by(tmp_path):
    """Decision 8: blocked (risk_check refuses every entry until Ivan resolves it) and
    alerted; nothing is cancelled or closed."""
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    cid = holding(sim, j, clock, at=FLAT_BY - timedelta(days=1))
    j.record_order_event(cid, "filled", FLAT_BY - timedelta(days=1, minutes=-1),
                         filled_qty=1, avg_price="718.32")
    passes(r, clock, 3)
    assert sim.writes == [] and sim.positions == {"SPY": 1}
    (b,) = blocks(j)
    assert b["kind"] == "unprotected" and "earlier day" in b["reason"]
    assert any("new entries blocked" in a for a in alerts)


# ======================= unclear replies =======================

def test_a_cancel_whose_reply_is_lost_is_confirmed_by_lookup_not_repeated(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    faulty.add("DELETE", "/v2/orders/", "lost")  # the group cancel acted; its reply is lost
    r.pass_once()
    cancels = [w for w in sim.writes if w[1].startswith("/v2/orders/")]
    assert len(set(cancels)) == len(cancels)  # the unclear cancel is never sent again
    assert sim.positions == {} and sim.writes.count(CLOSE) == 1  # the lookup confirmed it


def test_a_cancel_that_timed_out_before_acting_is_retried_by_the_next_pass(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    faulty.add("DELETE", "/v2/orders/", "timeout")  # the stop's cancel
    faulty.add("DELETE", "/v2/orders/", "timeout")  # the take-profit's cancel
    r.pass_once()
    assert CLOSE not in sim.writes  # the legs are not gone: never close over them
    assert any("not gone yet" in a for a in alerts)
    clock.step(15)
    r.pass_once()
    assert sim.positions == {} and sim.writes.count(CLOSE) == 1


@pytest.mark.parametrize("fills", [True, False], ids=["closed", "closing"])
def test_a_close_whose_reply_is_lost_is_checked_and_never_sent_twice(tmp_path, fills):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    sim.close_fills = fills
    faulty.add("DELETE", "/v2/positions/", "lost")
    r.pass_once()
    state = "closed" if fills else "closing"
    assert f"check_close SPY: {state}" in r.report["actions"]
    clock.step(15)
    passes(r, clock, 2)
    assert sim.writes.count(CLOSE) == 1
    assert (sim.positions == {}) is fills


def test_a_close_that_timed_out_unplaced_is_checked_open_then_closed_once(tmp_path):
    """check_close says "open" (no working close): the next pass's close is an informed one."""
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    faulty.add("DELETE", "/v2/positions/", "timeout")
    r.pass_once()
    assert sim.positions == {"SPY": 1}
    assert any("check_close SPY: open" in a for a in r.report["actions"])
    clock.step(15)
    r.pass_once()
    assert sim.positions == {} and sim.writes.count(CLOSE) == 2


def test_a_lost_close_whose_check_fails_is_never_closed_blindly(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    sim.close_fills = False  # the close is working at the broker
    # the close acts, its reply is lost, and check_close's read fails
    faulty.add("DELETE", "/v2/positions/", "lost", then=[("GET", "/v2/orders", "500", [])])
    r.pass_once()
    assert any("could not be checked" in a for a in alerts)
    passes(r, clock, 2)
    assert sim.writes.count(CLOSE) == 1  # the next passes see the working close


def test_an_unreadable_close_reply_is_checked(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    sim.close_reply_broken = True
    r.pass_once()
    assert any("check_close SPY: closed" in a for a in r.report["actions"])
    assert sim.positions == {} and sim.writes.count(CLOSE) == 1


def test_a_403_while_the_legs_hold_the_shares_waits_for_the_next_pass(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    real = sim._cancel
    sim._cancel = lambda oid: (204, None)  # accepted, not done yet
    r.pass_once()
    assert CLOSE not in sim.writes and any("not gone yet" in a for a in alerts)
    sim._cancel = real
    clock.step(15)
    r.pass_once()
    assert sim.positions == {}


def test_a_close_refused_with_403_is_tried_again_next_pass(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    real = sim._close
    sim._close = lambda sym: (403, {"code": 40310000})
    r.pass_once()
    assert any("still holds the shares" in a for a in alerts)
    sim._close = real
    clock.step(15)
    r.pass_once()
    assert sim.positions == {}


def test_an_unreadable_order_in_the_symbol_is_loud_at_the_flat_by_and_never_closed(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    sim.extra_open.append(stray(symbol="SPY"))
    for _ in range(3):
        before = len(alerts)
        r.pass_once()
        assert any("MANUAL ACTION NEEDED" in a for a in alerts[before:])  # every pass
        clock.step(15)
    assert CLOSE not in sim.writes


def test_a_failed_read_is_never_a_blind_close_and_the_next_pass_flattens(tmp_path):
    sim, faulty, j, clock, r, alerts = world(tmp_path)
    holding(sim, j, clock)
    faulty.add("GET", "/v2/positions", "500")
    r.pass_once()
    assert r.report["complete"] is False and CLOSE not in sim.writes
    clock.step(15)
    r.pass_once()
    assert sim.positions == {}


# ======================= python -m pipeline.flatten =======================

@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "load_limits", lambda path=None: LIMITS)
    sim, faulty, j, clock, r, alerts = world(tmp_path, at=T0.replace(hour=11))
    return sim, j, clock, r.broker


def test_check_prints_the_flat_by_and_what_now_would_do_and_writes_nothing(cli, capsys):
    sim, j, clock, broker = cli
    holding(sim, j, clock)
    before = j.path.read_bytes()
    assert fl.main(["--check"], broker=broker, journal=j, clock=clock) == 0
    out = capsys.readouterr().out
    assert "flat-by today (2026-11-24): 15:55 ET" in out
    assert "--now would:" in out and "flatten SPY" in out
    assert sim.writes == [] and j.path.read_bytes() == before


def test_now_flattens_todays_position_and_exits_0(cli, tmp_path, capsys):
    sim, j, clock, broker = cli
    holding(sim, j, clock)
    code = fl.main(["--now"], broker=broker, journal=j, clock=clock,
                   state_dir=tmp_path / "state", sleep=clock.step)
    assert code == 0 and sim.positions == {}
    assert "flat: no position and no open order" in capsys.readouterr().out


def test_now_leaves_an_earlier_days_position_alone_and_exits_1(cli, tmp_path, capsys):
    sim, j, clock, broker = cli
    cid = holding(sim, j, clock, at=clock.t - timedelta(days=1))
    j.record_order_event(cid, "filled", clock.t - timedelta(days=1), filled_qty=1,
                         avg_price="718.32")
    code = fl.main(["--now"], broker=broker, journal=j, clock=clock,
                   state_dir=tmp_path / "state", sleep=clock.step)
    assert code == fl.NOT_FLAT and sim.writes == []
    assert "left alone" in capsys.readouterr().out
    assert [b["kind"] for b in blocks(j)] == ["unprotected"]


def test_now_refuses_with_the_market_closed(cli, tmp_path, capsys):
    sim, j, clock, broker = cli
    clock.t = T0.replace(hour=17)
    code = fl.main(["--now"], broker=broker, journal=j, clock=clock,
                   state_dir=tmp_path / "state")
    assert code == fl.MARKET_CLOSED and sim.writes == []
    assert "market is closed" in capsys.readouterr().err


def test_now_stops_at_the_close_when_it_cannot_get_flat(cli, tmp_path, capsys):
    sim, j, clock, broker = cli
    clock.t = T0.replace(hour=15, minute=59)
    holding(sim, j, clock)
    sim.close_fills = False
    code = fl.main(["--now"], broker=broker, journal=j, clock=clock,
                   state_dir=tmp_path / "state", sleep=clock.step)
    assert code == fl.NOT_FLAT and sim.writes.count(CLOSE) == 1
    assert "MANUAL ACTION NEEDED" in capsys.readouterr().out


def test_now_refuses_while_run_day_is_watching_and_acts_when_it_is_not(cli, tmp_path):
    from pipeline.filelock import exclusive_file_lock
    sim, j, clock, broker = cli
    holding(sim, j, clock)
    state = tmp_path / "state"
    state.mkdir()
    beat = state / "run_day.heartbeat.json"
    with exclusive_file_lock(state / "run_day.lock", timeout_s=0):
        beat.write_text(json.dumps({"reconciled_at": (clock.t - timedelta(seconds=20))
                                    .isoformat()}), encoding="utf-8")
        assert fl.main(["--now"], broker=broker, journal=j, clock=clock, state_dir=state,
                       sleep=clock.step) == fl.LOCK_REFUSED
        assert sim.writes == []
        beat.write_text(json.dumps({"reconciled_at": (clock.t - timedelta(seconds=90))
                                    .isoformat()}), encoding="utf-8")
        assert fl.main(["--now"], broker=broker, journal=j, clock=clock, state_dir=state,
                       sleep=clock.step) == 0  # the backstop: run_day is not watching
    assert sim.positions == {}


def test_paper_false_is_refused(monkeypatch, capsys):
    from dataclasses import replace
    monkeypatch.setattr(tc, "load_limits", lambda path=None: replace(LIMITS, paper=False))
    assert fl.main(["--check"], broker=object()) == 2
    assert "paper must be true" in capsys.readouterr().err
