"""#256 (Ivan): outside the NYSE session nothing is cancelled or closed at the broker, by any
path. Before the open every position is an earlier day's (decision 8); after the close a
market close would wait for the next open and fill with nobody deciding. Whatever would have
been cancelled or closed is blocked ("outside-session:SYMBOL") and alerted MANUAL ACTION
NEEDED. The paths: the reconciler (run_day's thread, `pipeline.reconcile --once`), the
flat-by (`pipeline.flatten --now`) and run_day's start-up. Simulated paper account, network
blocked."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest

import pipeline.flatten as fl
import pipeline.reconcile as rc
import pipeline.run_day as rd
import tools.config as tc
from tests.test_flatten_job import Faulty
from tests.test_reconcile import (
    LIMITS,
    NY,
    Clock,
    Sim,
    blocks,
    keys,  # noqa: F401  (autouse: sentinel Alpaca keys)
    sent,
)
from tests.test_run_day import make_day
from tools.broker import AlpacaPaperBroker

OUTSIDE = {
    "before the open": datetime(2026, 11, 24, 9, 29, tzinfo=NY),  # 3 passes end by 09:29:30
    "at the close": datetime(2026, 11, 24, 16, 0, tzinfo=NY),
    "the evening": datetime(2026, 11, 24, 20, 0, tzinfo=NY),
    "a Saturday": datetime(2026, 11, 28, 11, 0, tzinfo=NY),
    "a holiday": datetime(2026, 11, 26, 11, 0, tzinfo=NY),  # Thanksgiving
    "an early close at 13:00": datetime(2026, 11, 27, 13, 0, tzinfo=NY),
    "an uncovered year": datetime(2031, 6, 2, 11, 0, tzinfo=NY),  # a Monday
}
INSIDE = datetime(2026, 11, 24, 10, 0, tzinfo=NY)


def world(tmp_path, at, **kw):
    sim = Sim()
    broker = AlpacaPaperBroker(LIMITS, opener=Faulty(sim))
    broker.verify_paper_account()
    from journal.log import Journal
    j = Journal(tmp_path / "decisions.jsonl")
    clock = Clock(at)
    alerts: list[str] = []
    r = rc.Reconciler(j, broker, clock=clock, limits=LIMITS, alert=alerts.append,
                      sleep=lambda s: None, monotonic=itertools.count().__next__, **kw)
    return sim, j, clock, r, alerts


def orphan_position(sim, j, clock):
    sim.positions["MSFT"] = 5  # not in the journal
    return "MSFT"


def unprotected_position(sim, j, clock):
    cid = sent(j, at=clock.t - timedelta(minutes=30))
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"
    return "SPY"


def flat_now_position(sim, j, clock):  # protected; flattened only because of flat_now
    cid = sent(j, at=clock.t - timedelta(minutes=30))
    sim.add_entry(cid)
    sim.fill(cid)
    return "SPY"


def working_entry(sim, j, clock):  # unfilled after 60 s: the #136 rule would cancel it
    cid = sent(j, at=clock.t - timedelta(minutes=2))
    sim.add_entry(cid)
    return "SPY"


def orphan_order(sim, j, clock):
    foreign = sim.add_entry("someone-elses", symbol="AAPL")
    sim.entries.pop("someone-elses")
    sim.extra_open.append(foreign)
    sim.entries["hidden"] = foreign  # so a cancel would find it
    return "AAPL"


SCENARIOS = {"an orphan position": orphan_position,
             "an unprotected position": unprotected_position,
             "a flat-now pass": flat_now_position,
             "an overdue working entry": working_entry,
             "an orphan order": orphan_order}


@pytest.mark.parametrize("when", list(OUTSIDE), ids=list(OUTSIDE))
@pytest.mark.parametrize("what", list(SCENARIOS), ids=list(SCENARIOS))
def test_outside_the_session_nothing_is_sent_it_blocks_and_alerts(tmp_path, when, what):
    sim, j, clock, r, alerts = world(tmp_path, OUTSIDE[when],
                                     flat_now=(what == "a flat-now pass"))
    sym = SCENARIOS[what](sim, j, clock)
    for _ in range(3):
        r.pass_once(final=True)
        clock.step(15)
    assert sim.writes == []  # no cancel, no close
    assert f"outside-session:{sym}" in [b["key"] for b in blocks(j)]
    assert any("MANUAL ACTION NEEDED" in a and sym in a for a in alerts)


@pytest.mark.parametrize("what", list(SCENARIOS), ids=list(SCENARIOS))
def test_inside_the_session_the_same_cases_act(tmp_path, what):
    """The control: the scenarios above are real, the reconciler acts on them at 10:00."""
    sim, j, clock, r, alerts = world(tmp_path, INSIDE, flat_now=(what == "a flat-now pass"))
    SCENARIOS[what](sim, j, clock)
    r.pass_once()
    assert sim.writes != []
    assert not any(b["key"].startswith("outside-session") for b in blocks(j))


def test_check_says_it_would_not_act(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path, OUTSIDE["before the open"], check_only=True)
    orphan_position(sim, j, clock)
    r.pass_once()
    assert sim.writes == [] and blocks(j) == []
    assert any(w.startswith("not act on MSFT") for w in r.report["would"])


@pytest.mark.parametrize("when", ["before the open", "the evening", "a Saturday"])
def test_reconcile_once_outside_the_session_sends_nothing(tmp_path, monkeypatch, when):
    monkeypatch.setattr(tc, "load_limits", lambda path=None: LIMITS)
    sim, j, clock, r, alerts = world(tmp_path, OUTSIDE[when])
    orphan_position(sim, j, clock)
    unprotected_position(sim, j, clock)
    assert rc.main(["--once"], broker=r.broker, journal=j, clock=clock,
                   state_dir=tmp_path / "state") in (0, 1)
    assert sim.writes == []
    keys_ = [b["key"] for b in blocks(j)]
    assert "outside-session:MSFT" in keys_ and "outside-session:SPY" in keys_


class NoBroker:
    def __getattr__(self, name):
        raise AssertionError(f"--now called the broker ({name}) outside the session")


@pytest.mark.parametrize("when", list(OUTSIDE), ids=list(OUTSIDE))
def test_flatten_now_refuses_outside_the_session_before_any_broker_call(tmp_path, monkeypatch,
                                                                        capsys, when):
    monkeypatch.setattr(tc, "load_limits", lambda path=None: LIMITS)
    code = fl.main(["--now"], broker=NoBroker(), journal=object(), clock=lambda: OUTSIDE[when],
                   state_dir=tmp_path / "state")
    assert code == fl.MARKET_CLOSED
    assert "Nothing was done" in capsys.readouterr().err


def test_run_day_starting_before_the_open_sends_nothing(tmp_path, monkeypatch):
    """run_day's start-up: a position no order sent today explains holds the day (#209); the
    reconciler never starts, and nothing is cancelled or closed."""
    sim = Sim()
    broker = AlpacaPaperBroker(LIMITS, opener=Faulty(sim))
    broker.verify_paper_account()
    sim.positions["SPY"] = 3
    sim.add_entry("yesterdays", symbol="AAPL")
    day, clock, dec, out = make_day(tmp_path, monkeypatch, broker=broker,
                                    start=datetime(2026, 11, 24, 8, 0, tzinfo=NY))
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert sim.writes == [] and day.reconcile_thread is None
