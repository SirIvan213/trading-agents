"""Console-only money-check debug path (smoke_debate only). Offline: fakes, no network.

The debug detail (offending tokens, pattern, excerpt) must reach the console and nothing else:
not the DebateRecord, not a retry reason, not the hint sent to the model, not case text.
"""
import json
from types import SimpleNamespace

import pytest

from agents import debate_common
from agents.debate import run_debate
from agents.debate_common import money_debug
from agents.llm import AgentConfig
from agents.money_guard import (
    PATTERN_NAMES,
    allowed_values,
    money_violations,
    quoted_values,
    violation_details,
)
from pipeline import smoke_debate as S
from tests.debate_helpers import bear_json, bull_json, reply
from tests.fakes import FakeClient
from tests.test_issue87 import PKT, run_record

CFGS = {"bull": AgentConfig(), "bear": AgentConfig()}
SECRET_TOKEN = "110.25"
BAD_BULL = bull_json(argument=f"pullback holds, target {SECRET_TOKEN} by close",
                     key_points=["buy 500 shares on the open"])
BAD_BEAR = bear_json(objections=["stop 97.13 is too tight", "b", "c"])


def queue():
    # bull: rejected then ok; bear: rejected twice (fails closed)
    return [reply(BAD_BULL), reply(bull_json()), reply(BAD_BEAR), reply(BAD_BEAR)]


def debate(listener=None):
    client = FakeClient(queue())
    if listener is None:
        rec = run_debate(PKT, clients=client, configs=CFGS, rounds=1)
    else:
        with money_debug(listener):
            rec = run_debate(PKT, clients=client, configs=CFGS, rounds=1)
    return rec, client


def without_timing(rec) -> dict:
    d = rec.model_dump(mode="json")
    for t in d["turns"]:
        t["run"].pop("duration_ms")
    return d


# ---------------- stored record is unchanged ----------------

def test_debug_listener_leaves_the_stored_record_unchanged():
    events = []
    plain, plain_client = debate()
    debugged, debug_client = debate(lambda side, items: events.append((side, items)))
    assert events, "listener should have been called"
    assert without_timing(debugged) == without_timing(plain)
    # the requests sent to the model (including the retry hint) are identical too
    assert debug_client.calls == plain_client.calls


def test_debug_detail_never_reaches_record_reasons_or_case_text():
    events = []
    rec, _ = debate(lambda side, items: events.append((side, items)))
    stored = json.dumps(rec.model_dump(mode="json"))
    for _, items in events:
        for _field, d in items:
            assert d.excerpt not in stored
            assert d.pattern not in stored
    assert SECRET_TOKEN not in stored and "500 shares" not in stored
    reasons = [r.reason for t in rec.turns for r in t.run.retries]
    assert reasons and all("debug" not in r.lower() and SECRET_TOKEN not in r for r in reasons)
    assert all(r.endswith(debate_common.MONEY_REASON) for r in reasons)


def test_listener_errors_do_not_change_the_run():
    def boom(side, items):
        raise RuntimeError("listener failed")
    plain, _ = debate()
    debugged, _ = debate(boom)
    assert without_timing(debugged) == without_timing(plain)


def test_listener_is_off_outside_the_block():
    events = []
    with money_debug(lambda side, items: events.append(side)):
        pass
    debate()
    assert events == [] and debate_common._money_debug.get() is None


# ---------------- listener content ----------------

def test_listener_gets_side_field_token_pattern_and_excerpt():
    events = []
    debate(lambda side, items: events.append((side, items)))
    assert [side for side, _ in events] == ["bull", "bear", "bear"]
    bull_items = events[0][1]
    assert [(f, d.number) for f, d in bull_items] == [
        ("argument", SECRET_TOKEN), ("key_points[0]", "500")]
    target, size = (d for _, d in bull_items)
    assert target.pattern == PATTERN_NAMES["c"] and f"target {SECRET_TOKEN}" in target.excerpt
    assert size.pattern == PATTERN_NAMES["e"] or size.pattern == PATTERN_NAMES["b"]
    assert "buy 500 shares" in size.excerpt
    assert events[1][1][0][0] == "objections[0]" and events[1][1][0][1].number == "97.13"


def test_excerpt_is_trimmed_and_marked():
    text = "x" * 100 + " stop 97.13 " + "y" * 100
    (d,) = violation_details(text, set(), context=10)
    assert d.excerpt.startswith("...") and d.excerpt.endswith("...")
    assert "stop 97.13" in d.excerpt and len(d.excerpt) < 40


def test_size_equal_to_plan_value_is_marked():
    allowed = allowed_values(PKT)
    (d,) = violation_details("104.39 shares", allowed)
    assert d.plan_value and d.pattern == PATTERN_NAMES["b"]


# violation_details reports exactly what money_violations rejects (read-only mirror).
CORPUS = [
    "buy 100", "size of 500", "go long 100", "sell short 50", "98 dollars", "trade 1000 USD",
    "position of 100 units", "entry 102.50", "stop 101", "target 105", "price 99", "$99",
    "shares: 100", "qty 50", "limit 99.5", "sell at $250", "CEO sold 50,000 shares",
    "2.40 per share", "stop item 98.50", "buy at 101:50", "entry 102.00, stop 99.61",
    "up 7.47% on 1.64 relative volume, EPS of 0.46", "risk 7.47 and target 2.54",
    "3,225,000,000 shares outstanding", "no money here", "8-K item 9.01 at 10:30",
]


@pytest.mark.parametrize("text", CORPUS)
def test_details_mirror_money_violations(text):
    allowed, quoted = allowed_values(PKT), quoted_values(PKT)
    assert [d.mention for d in violation_details(text, allowed, quoted)] == \
        money_violations(text, allowed, quoted)


# ---------------- smoke_debate prints it (console only) ----------------

def test_smoke_debate_main_prints_debug_section_and_record_is_clean(monkeypatch, capsys):
    reports = {a: PKT.reports["technical"].model_copy(update={"agent": a})
               for a in ("technical", "sentiment", "fundamentals")}
    pkt = PKT.model_copy(update={
        "reports": reports, "runs": {a: run_record(a) for a in reports}, "all_ok": True})
    captured = {}

    def fake_analysts(*a, **k):  # plain function: asyncio.run is stubbed below (no loop,
        return pkt                # so no loopback socketpair under the network guard)

    real_report = S.report

    def spy_report(packet, debate_rec, configs):
        captured["debate"] = debate_rec
        real_report(packet, debate_rec, configs)

    monkeypatch.setattr(S, "run_analysts", fake_analysts)
    monkeypatch.setattr(S, "asyncio", SimpleNamespace(run=lambda value: value))
    monkeypatch.setattr(S, "report", spy_report)
    monkeypatch.setattr(S, "make_client", lambda *a, **k: pytest.fail("make_client called"))
    monkeypatch.setenv("TRADING_AGENTS_DEBATE_ROUNDS", "1")
    assert S.main([], client=FakeClient(queue())) == 1  # the bear fails closed
    out = capsys.readouterr().out
    debug = out.split("-- Money-check rejections (DEBUG, console only, not stored) --")[1]
    assert "bull rejected attempt #1: 2 offending mention(s)" in debug
    assert f"argument: token '{SECRET_TOKEN}' in 'target {SECRET_TOKEN}'" in debug
    assert f"pattern: {PATTERN_NAMES['c']}" in debug
    assert f"excerpt: pullback holds, target {SECRET_TOKEN} by close" in debug
    assert "bear rejected attempt #2" in debug
    # nothing above the debug section (the report of the stored record) names the token
    assert SECRET_TOKEN not in out.split("-- Money-check rejections")[0].split("-- API calls")[1]
    assert SECRET_TOKEN not in json.dumps(captured["debate"].model_dump(mode="json"))


def test_print_money_debug_none(capsys):
    S.print_money_debug([])
    assert "none" in capsys.readouterr().out
