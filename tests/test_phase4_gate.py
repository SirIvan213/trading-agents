"""Phase 4 risk gate: issues #39 (exact get_bars prices), #40 (no money from report text),
#50 (confidence never reaches sizing or risk_check). Offline: fixtures and fakes only."""
import ast
import asyncio
import dataclasses
import inspect
import re
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import agents.orchestrator as orch
import agents.technical as tech
import agents.tool_registry as reg
import pipeline.orders as orders
from agents.runner import AnalystRun, neutral_report
from agents.schemas import (
    AgentRunRecord,
    AnalystReport,
    Catalyst,
    DecisionPacket,
    Evidence,
    ExitPlanRecord,
    TraderDecision,
)
from agents.tool_registry import ToolSession, price_levels
from pipeline.ledger import LedgerError, PlanLedger
from pipeline.orders import build_order, select_plan
from tests.fakes import (
    NOON,
    FakeAllData,
    FakeClient,
    FakeMarketData,
    report_json,
    response,
    text,
    tool_use,
)
from tests.fakes import NOW as NOW_AAPL
from tests.order_helpers import fresh_quote, gate_for_order
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tools.config import CONFIG_DIR, Limits, load_limits
from tools.exit_plan import exit_plan as real_exit_plan
from tools.risk_check import ORDER_FIELDS, DayState, check_order
from tools.scan import ScanCriteria
from tools.size import Plan, size_position

ROOT = Path(__file__).resolve().parent.parent
NY = ZoneInfo("America/New_York")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
MON_1015 = datetime(2026, 10, 19, 10, 15, tzinfo=NY)
FRESH = datetime(2026, 10, 19, 10, 15, 30, tzinfo=NY)  # 30 s after plan_record()'s as_of
# A live TSLA quote at FRESH (#68): ask = entry, 2-cent spread, so the allowance is 0.37
# (0.10% of 364.18 rounded up to the cent beats the spread) and the order is 8 shares.
QUOTE = fresh_quote("364.18", FRESH)
SLIPPAGE = D("0.37")
QUOTE_DETAILS = {"bid": "364.16", "ask": "364.18", "spread": "0.02",
                 "quote_t": (FRESH - timedelta(seconds=1)).isoformat(), "age_s": 1}

AAPL_LEVELS = {
    "session_low": "99.70", "session_high": "103.20", "last_close": "103.10",
    "or_low": "99.70", "or_high": "102.00", "vwap": "101.60",
}
NAMES = "session_low, session_high, last_close, or_low, or_high, vwap"
ENTRY_MSG = ("entry must be the last_close value from get_bars price_levels "
             "(orders are market orders)")
LEVEL_MSG = f"level must be one of get_bars price_levels: {NAMES}"


def make(summary: dict | None = None) -> ToolSession:
    s = ToolSession(
        symbol="AAPL", now=NOW_AAPL, data=FakeMarketData(),
        limits=load_limits(), criteria=ScanCriteria(),
    )
    if summary is not None:
        s.summary = summary
    return s


def loaded() -> ToolSession:
    s = make()
    _, err = s.execute("get_bars", {"symbol": "AAPL"})
    assert err is False
    return s


@pytest.fixture
def spy(monkeypatch):
    calls = []

    def _spy(*a, **k):
        calls.append((a, k))
        return real_exit_plan(*a, **k)

    monkeypatch.setattr(reg, "exit_plan", _spy)
    return calls


def spellings(v: str) -> list[str]:
    """Equivalent decimal spellings of a 2-decimal price: '102.00' -> 102.00, 102.0, 102."""
    out = [v, v + "0", v.rstrip("0"), v.rstrip("0").rstrip(".")]
    assert all(D(x) == D(v) for x in out)
    return out


ALL_SPELLINGS = [(n, sp) for n, v in AAPL_LEVELS.items() for sp in spellings(v)]


# ===================== #39: price_levels =====================

def test_get_bars_returns_expected_price_levels():
    res, _ = make().execute("get_bars", {"symbol": "AAPL"})
    assert res["price_levels"] == AAPL_LEVELS
    assert res["vwap"] == "101.6001"  # the raw value is still there, and not a valid price


def test_price_levels_floor_never_round_up():
    assert price_levels({"vwap": "101.6001"})["vwap"] == "101.60"
    assert price_levels({"vwap": "101.5999"})["vwap"] == "101.59"
    assert price_levels({"vwap": "101.5900001"})["vwap"] == "101.59"
    assert price_levels({"last_close": "103.1999", "session_high": "103.1999"}) == {
        "session_high": "103.19", "last_close": "103.19"}
    s = {"opening_range": {"low": "99.6999", "high": "101.9999"}}
    assert price_levels(s) == {"or_low": "99.69", "or_high": "101.99"}


def test_price_levels_none_values_skipped():
    s = {"session_low": None, "session_high": "11", "last_close": None, "vwap": None,
         "opening_range": None}
    assert price_levels(s) == {"session_high": "11.00"}
    assert price_levels({}) == {}


@pytest.mark.parametrize("opening", [None, "missing"])
def test_price_levels_no_opening_range_has_no_or_keys(opening):
    s = {"session_low": "9.5", "session_high": "11", "last_close": "10", "vwap": "10.25"}
    if opening is None:
        s["opening_range"] = None
    levels = price_levels(s)
    assert "or_low" not in levels and "or_high" not in levels
    assert set(levels) == {"session_low", "session_high", "last_close", "vwap"}


def test_price_levels_opening_range_with_none_edges_skipped():
    levels = price_levels({"opening_range": {"low": None, "high": "12"}})
    assert levels == {"or_high": "12.00"}


@pytest.mark.parametrize("raw", ["5", "5.1", "5.10", "5.123456", "0.5", "1234.5", "103.2"])
def test_price_levels_always_two_decimal_strings(raw):
    s = {"session_low": raw, "session_high": raw, "last_close": raw, "vwap": raw,
         "opening_range": {"low": raw, "high": raw}}
    levels = price_levels(s)
    assert len(levels) == 6
    for v in levels.values():
        assert isinstance(v, str) and re.fullmatch(r"\d+\.\d{2}", v), v


# ===================== #39: entry =====================

def assert_last_close_only(spy, s, res, name, setup):
    """Issue #118: orders are market orders, so only last_close reaches exit_plan; any other
    returned price is refused with a non-error reason and nothing is recorded."""
    if name == "last_close":
        assert spy  # the gate let it through (a labelling call, then the chosen multiple)
        assert spy[0][0][0] == setup
        assert all(c[0][1] == D(AAPL_LEVELS[name]) for c in spy)
    else:
        assert res["ok"] is False and res["reason"].startswith("entry must be last_close")
        assert spy == [] and s.exit_plans == []


@pytest.mark.parametrize(("name", "entry"), ALL_SPELLINGS)
def test_each_allowed_value_as_entry_reaches_exit_plan_orb(spy, name, entry):
    s = loaded()
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": entry})
    assert err is False and "error" not in res
    assert_last_close_only(spy, s, res, name, "orb")


@pytest.mark.parametrize(("name", "entry"), ALL_SPELLINGS)
def test_each_allowed_value_as_entry_reaches_exit_plan_level(spy, name, entry):
    s = loaded()
    res, err = s.execute("plan_exit", {"setup": "level", "entry": entry, "level": "99.70"})
    assert err is False and "error" not in res
    assert_last_close_only(spy, s, res, name, "level")


def test_last_close_entry_above_structure_gives_ok_plan():
    res, err = loaded().execute("plan_exit", {"setup": "orb", "entry": AAPL_LEVELS["last_close"]})
    assert err is False and res["ok"] is True


@pytest.mark.parametrize("name", ["or_high", "session_high", "vwap"])
def test_other_returned_prices_are_refused_as_entry_without_an_error(name):
    s = loaded()
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": AAPL_LEVELS[name]})
    assert err is False and res["ok"] is False and "last_close" in res["reason"]
    assert s.exit_plans == []


def test_entry_spellings_of_last_close_all_equal():
    for sp in ("103.1", "103.10", "103.100"):
        res, err = loaded().execute("plan_exit", {"setup": "orb", "entry": sp})
        assert err is False and res["ok"] is True
        assert res["entry"] == "103.10"  # exit_plan echoes the spelling it was given


IN_RANGE_NOT_RETURNED = ["102.50", "99.71", "103.19", "101.6001", "101.59", "101.61", "100",
                         "103.15"]
OUT_OF_RANGE = ["99.69", "103.21", "0.01", "500", "0", "-1", "-102.00"]


@pytest.mark.parametrize("setup", ["orb", "level"])
@pytest.mark.parametrize("entry", IN_RANGE_NOT_RETURNED + OUT_OF_RANGE)
def test_entry_not_a_returned_price_is_refused(spy, setup, entry):
    s = loaded()
    inp = {"setup": setup, "entry": entry}
    if setup == "level":
        inp["level"] = "99.70"
    res, err = s.execute("plan_exit", inp)
    assert (res, err) == ({"error": ENTRY_MSG}, True)
    assert spy == [] and s.exit_plans == []


def test_raw_vwap_is_refused_as_entry_and_level(spy):
    s = loaded()
    raw = s.summary["vwap"]
    assert raw == "101.6001"
    assert s.execute("plan_exit", {"setup": "orb", "entry": raw}) == ({"error": ENTRY_MSG}, True)
    res = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": raw})
    assert res == ({"error": LEVEL_MSG}, True)
    assert spy == []


# ===================== #39: level =====================

@pytest.mark.parametrize(("name", "level"), ALL_SPELLINGS)
def test_each_allowed_value_as_level_is_accepted(spy, name, level):
    s = loaded()
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": level})
    assert err is False and "error" not in res
    assert spy and all(c[1]["level"] == D(AAPL_LEVELS[name]) for c in spy)


@pytest.mark.parametrize("level", IN_RANGE_NOT_RETURNED + OUT_OF_RANGE)
def test_level_not_a_returned_price_is_refused(spy, level):
    s = loaded()
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": level})
    assert (res, err) == ({"error": LEVEL_MSG}, True)
    assert spy == [] and s.exit_plans == []


def test_valid_entry_does_not_excuse_invalid_level(spy):
    res, err = loaded().execute(
        "plan_exit", {"setup": "level", "entry": "103.10", "level": "102.50"})
    assert err is True and res == {"error": LEVEL_MSG}
    assert spy == []


def test_refusal_lists_available_level_names():
    res, _ = loaded().execute("plan_exit", {"setup": "orb", "entry": "102.50"})
    assert res == {"error": ENTRY_MSG}
    res, _ = loaded().execute(
        "plan_exit", {"setup": "level", "entry": "103.10", "level": "102.50"})
    for name in AAPL_LEVELS:
        assert name in res["error"]


def test_refusal_names_exclude_or_levels_without_opening_range(spy):
    summary = {"session_low": "10", "session_high": "11", "last_close": "10.5", "vwap": "10.4",
               "atr": None, "opening_range": None}
    summary["price_levels"] = price_levels(summary)
    s = make(summary)
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "10.75"})
    assert err is True
    assert res == {"error": ENTRY_MSG}
    assert "or_low" not in res["error"] and "or_high" not in res["error"]
    assert spy == []


def test_plan_exit_without_price_levels_in_summary_refuses(spy):
    s = make({"session_low": "99.70", "session_high": "103.20", "opening_range": None})
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "100.00"})
    assert err is True and res["error"] == ENTRY_MSG
    assert spy == []


# ===================== #39: ledger =====================

def test_ok_plan_adds_record_with_sequential_ids_and_result_has_plan_id():
    s = loaded()
    r1, _ = s.execute("plan_exit", {"setup": "orb", "entry": "103.10"})
    r2, _ = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "101.60"})
    assert r1["plan_id"] == "plan-1" and r2["plan_id"] == "plan-2"
    assert [p.plan_id for p in s.exit_plans] == ["plan-1", "plan-2"]
    assert all(isinstance(p, ExitPlanRecord) for p in s.exit_plans)


def test_record_fields_equal_exit_plan_output():
    s = loaded()
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "99.70"})
    (rec,) = s.exit_plans
    assert rec.symbol == "AAPL" and rec.setup == "level"
    for f in ("entry", "stop", "target", "risk_per_share", "reward_to_risk", "time_exit"):
        assert getattr(rec, f) == res[f], f
    assert (rec.entry, rec.stop, rec.target) == ("103.10", "99.61", "106.59")
    assert rec.as_of == "2026-11-03T10:30:00-05:00"


def test_record_matches_a_direct_exit_plan_call():
    s = loaded()
    # The model spells the entry "102"; the gate passes exit_plan the canonical
    # price_levels string ("102.00"), so the record always carries cent prices.
    s.execute("plan_exit", {"setup": "orb", "entry": "103.1"})
    (rec,) = s.exit_plans
    assert rec.entry == "103.10"
    direct = real_exit_plan(
        "orb", D("103.10"), s.limits, opening_range=s.summary["opening_range"],
        atr=D(s.summary["atr"]))
    for f in ("entry", "stop", "target", "risk_per_share", "reward_to_risk", "time_exit"):
        assert getattr(rec, f) == direct[f], f


def test_record_level_names():
    s = loaded()
    s.execute("plan_exit", {"setup": "orb", "entry": "103.1"})
    s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "101.6"})
    s.execute("plan_exit", {"setup": "level", "entry": "103.1", "level": "99.70"})
    orb, lvl, lvl2 = s.exit_plans
    assert (orb.entry_level, orb.structure_level) == ("last_close", "or_low")
    assert (lvl.entry_level, lvl.structure_level) == ("last_close", "vwap")
    assert lvl2.entry_level == "last_close"
    # 99.70 is both session_low and or_low; either name is a truthful match
    assert lvl2.structure_level in ("session_low", "or_low")


def test_refused_plan_adds_no_record():
    s = loaded()
    s.execute("plan_exit", {"setup": "orb", "entry": "102.50"})
    s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "102.50"})
    s.execute("plan_exit", {"setup": "level", "entry": "103.10"})
    s.execute("plan_exit", {"setup": "bogus", "entry": "103.10"})
    s.execute("plan_exit", {"setup": "orb", "entry": "103.10", "atr": "0.01"})
    s.execute("plan_exit", {"setup": "orb", "entry": "102.00"})  # valid price, not last_close
    assert s.exit_plans == []


def test_ok_false_plan_adds_no_record_and_no_plan_id():
    s = loaded()
    # the level equals the entry: no stop under it, so the plan is not ok
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "103.10"})
    assert err is False and res["ok"] is False and "plan_id" not in res
    assert s.exit_plans == []
    res, _ = s.execute("plan_exit", {"setup": "orb", "entry": "103.10"})
    assert res["plan_id"] == "plan-1"  # failed attempts do not consume ids


def test_no_ledger_before_get_bars():
    s = make()
    s.execute("plan_exit", {"setup": "orb", "entry": "103.10"})
    assert s.exit_plans == []


# ===================== #40: AnalystRun / DecisionPacket =====================

def bars_then_plan(entry="103.10"):
    return [
        response([tool_use("b", "get_bars", {"symbol": "AAPL"})], "tool_use"),
        response([tool_use("p", "plan_exit", {"setup": "orb", "entry": entry})], "tool_use"),
    ]


def good_report():
    return response([text(report_json())], "end_turn")


def run_tech(queue, cancel=None):
    return tech.run_technical(
        "AAPL", NOW_AAPL, FakeMarketData(), client=FakeClient(queue), cancel=cancel)


def test_analyst_run_exit_plans_filled_when_ok():
    run = run_tech([*bars_then_plan(), good_report()])
    assert run.ok is True
    assert [p.plan_id for p in run.exit_plans] == ["plan-1"]
    assert run.exit_plans[0].entry == "103.10" and run.exit_plans[0].symbol == "AAPL"


def test_analyst_run_exit_plans_empty_when_neutral_fallback():
    bad = response([text("not json")], "end_turn")
    run = run_tech([*bars_then_plan(), bad, bad])
    assert run.ok is False and run.report.stance == "neutral"
    assert run.exit_plans == []


def test_plan_survives_when_first_report_rejected_and_retry_validates():
    """Retry policy: plan-1 was made by the tool on attempt 1; only the REPORT was rejected.
    The retry validates, the run is ok, and the tool-made plan stays in exit_plans."""
    rejected = response([text(report_json(stance="sideways"))], "end_turn")
    run = run_tech([*bars_then_plan(), rejected, good_report()])
    assert run.ok is True and run.reason is None
    assert [p.plan_id for p in run.exit_plans] == ["plan-1"]
    assert run.exit_plans[0].entry == "103.10"


def test_plan_survives_when_first_report_has_invalid_catalyst_and_retry_validates():
    rejected = response([text(report_json(
        catalysts=[{"name": "earnings", "source": "get_news"}]))], "end_turn")
    run = run_tech([*bars_then_plan(), rejected, good_report()])
    assert run.ok is True
    assert [p.plan_id for p in run.exit_plans] == ["plan-1"]


def test_analyst_run_exit_plans_empty_when_run_raised():
    run = run_tech([*bars_then_plan(), RuntimeError("boom")])
    assert run.ok is False and run.reason == "RuntimeError"
    assert run.exit_plans == []


def test_analyst_run_exit_plans_empty_when_cancelled(monkeypatch):
    cancel = threading.Event()
    made = []

    def cancelling(*a, **k):
        out = real_exit_plan(*a, **k)
        made.append(out)
        cancel.set()  # cancelled right after the plan was computed and recorded
        return out

    monkeypatch.setattr(reg, "exit_plan", cancelling)
    run = run_tech([*bars_then_plan(), good_report()], cancel=cancel)
    assert made and made[0]["ok"] is True  # the plan really was produced
    assert run.ok is False and run.reason == "cancelled"
    assert run.exit_plans == []


def test_analyst_run_default_exit_plans_is_empty_list():
    run = AnalystRun(neutral_report("technical", "AAPL", "r"), False, "r", [], 1)
    assert run.exit_plans == []


AGENT_TOOL = {"sentiment": "get_news", "fundamentals": "get_fundamentals"}


def other_agent_script(agent):
    return [
        response([tool_use("u", AGENT_TOOL[agent], {"symbol": "ABCD"})], "tool_use"),
        response([text(report_json(
            agent=agent, symbol="ABCD",
            evidence=[{"point": "ok", "source": AGENT_TOOL[agent]}]))], "end_turn"),
    ]


def packet_via_orchestrator(technical_queue, timeout_s=5):
    clients = {
        "technical": FakeClient(technical_queue),
        "sentiment": FakeClient(other_agent_script("sentiment")),
        "fundamentals": FakeClient(other_agent_script("fundamentals")),
    }
    return asyncio.run(orch.run_analysts(
        "ABCD", NOON, data=FakeAllData(), clients=clients, timeout_s=timeout_s))


def abcd_plan_queue(*tail):
    return [
        response([tool_use("b", "get_bars", {"symbol": "ABCD"})], "tool_use"),
        response([tool_use("p", "plan_exit", {"setup": "orb", "entry": "103.10"})], "tool_use"),
        *tail,
    ]


def abcd_report():
    return response([text(report_json(
        agent="technical", symbol="ABCD",
        evidence=[{"point": "ok", "source": "get_bars"}]))], "end_turn")


def test_packet_exit_plans_come_from_technical_when_ok():
    packet = packet_via_orchestrator(abcd_plan_queue(abcd_report()))
    assert packet.runs["technical"].ok is True
    assert [p.plan_id for p in packet.exit_plans] == ["plan-1"]
    assert packet.exit_plans[0].symbol == "ABCD"


def test_packet_exit_plans_empty_when_technical_not_ok():
    bad = response([text("not json")], "end_turn")
    packet = packet_via_orchestrator(abcd_plan_queue(bad, bad))
    assert packet.runs["technical"].ok is False
    assert packet.exit_plans == []
    assert packet.runs["sentiment"].ok and packet.runs["fundamentals"].ok


def test_packet_exit_plans_empty_when_technical_times_out(monkeypatch):
    release = threading.Event()

    def slow(symbol, now, data, *, client, config, calls_sink, cancel=None, limits=None):
        release.wait(timeout=10)
        raise AssertionError("discarded")

    monkeypatch.setitem(orch._RUNNERS, "technical", slow)
    try:
        packet = packet_via_orchestrator([], timeout_s=0.3)
    finally:
        release.set()
    assert packet.runs["technical"].reason == "timeout" and packet.exit_plans == []


def test_packet_exit_plans_empty_when_technical_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("x")

    monkeypatch.setitem(orch._RUNNERS, "technical", boom)
    packet = packet_via_orchestrator([])
    assert packet.runs["technical"].ok is False and packet.exit_plans == []


def test_packet_ignores_plans_on_a_not_ok_technical_run(monkeypatch):
    rec = plan_record()

    def liar(symbol, now, data, *, client, config, calls_sink, cancel=None, limits=None):
        return AnalystRun(neutral_report("technical", symbol, "r"), False, "r", [], 1, [rec])

    monkeypatch.setitem(orch._RUNNERS, "technical", liar)
    assert packet_via_orchestrator([]).exit_plans == []


def test_other_analysts_plans_never_leak_into_packet(monkeypatch):
    rec = plan_record()

    def sentiment(symbol, now, data, *, client, config, calls_sink, cancel=None, limits=None):
        return AnalystRun(
            neutral_report("sentiment", symbol, "x").model_copy(update={"stance": "neutral"}),
            True, None, [], 1, [rec])

    monkeypatch.setitem(orch._RUNNERS, "sentiment", sentiment)
    packet = packet_via_orchestrator(abcd_plan_queue(abcd_report()))
    assert [p.plan_id for p in packet.exit_plans] == ["plan-1"]
    assert packet.exit_plans[0].symbol == "ABCD"


# ===================== #40: select_plan, build_order, fake-price proof =====================

def plan_record(plan_id="plan-1", **over) -> ExitPlanRecord:
    d = {
        "plan_id": plan_id, "symbol": "TSLA", "setup": "level", "entry": "364.18",
        "stop": "358.32", "target": "370.04", "risk_per_share": "5.86",
        "reward_to_risk": "1.00", "time_exit": "15:55", "entry_level": "last_close",
        "structure_level": "vwap", "as_of": "2026-10-19T10:15:00-04:00", "last_close": "364.18",
    }
    d.update(over)
    return ExitPlanRecord(**d)


FAKE_TEXT = [
    "stop 1.00", "target 999.99", "buy 10000 shares", "entry 0.01", "risk 50000",
]


def packet_with(plans, *, fake: bool, confidence=0.7, stance="bullish") -> DecisionPacket:
    reports, runs = {}, {}
    for agent in ("technical", "sentiment", "fundamentals"):
        if fake:
            ev = [Evidence(point=t, source="get_bars") for t in FAKE_TEXT]
            cats = [Catalyst(name=t, source="get_bars") for t in FAKE_TEXT]
        else:
            ev = [Evidence(point="above vwap", source="get_bars")]
            cats = []
        reports[agent] = AnalystReport(
            agent=agent, symbol="TSLA", stance=stance, confidence=confidence,
            evidence=ev, catalysts=cats)
        runs[agent] = AgentRunRecord(
            agent=agent, requested_model="m", effort="low", ok=True, duration_ms=1)
    return DecisionPacket(
        symbol="TSLA", as_of="2026-10-19T10:15:00-04:00", reports=reports, runs=runs,
        all_ok=True, exit_plans=plans)


def ledger_with(plans, symbol="TSLA", accept=True) -> PlanLedger:
    """A sealed ledger holding these plans, as the pipeline would record them (#64)."""
    led = PlanLedger(symbol)
    for p in plans:
        led.record(p)
    led.seal(accept=accept)
    return led


def test_select_plan_returns_record_by_id():
    a, b = plan_record("plan-1"), plan_record("plan-2", entry="365.00")
    led = ledger_with([a, b])
    assert select_plan(led, "plan-1") == a
    assert select_plan(led, "plan-2") == b
    assert select_plan(led, "plan-1") is not a  # a fresh validated copy, never the original


@pytest.mark.parametrize("bad", ["plan-3", "", "PLAN-1", "plan-1 ", "999", "0"])
def test_select_plan_unknown_id_raises_keyerror(bad):
    with pytest.raises(KeyError):
        select_plan(ledger_with([plan_record("plan-1")]), bad)


def test_select_plan_on_empty_ledger_raises_keyerror():
    with pytest.raises(KeyError):
        select_plan(ledger_with([]), "plan-1")


def test_select_plan_never_reads_a_packet():
    with pytest.raises(KeyError):
        select_plan(packet_with([plan_record("plan-1")], fake=False), "plan-1")  # type: ignore[arg-type]


def test_fake_price_text_has_no_effect_on_order_or_verdict():
    rec = plan_record()
    clean = packet_with([rec], fake=False)
    dirty = packet_with([rec], fake=True)
    # the fake text really is in the dirty packet and not in the clean one
    dumped = dirty.model_dump_json()
    assert all(t in dumped for t in FAKE_TEXT)
    assert not any(t in clean.model_dump_json() for t in FAKE_TEXT)

    # A TraderDecision whose free text carries the fake prices is also in play (the end-to-end
    # Trader proof through run_decision is in tests/test_phase4b_proofs.py).
    decision = TraderDecision(
        symbol="TSLA", action="BUY", confidence=0.99, plan_id=rec.plan_id,
        reasoning="; ".join(FAKE_TEXT))
    assert decision.symbol == dirty.symbol and decision.action == "BUY"
    led = ledger_with([rec])  # both packets carry the same plan; the order reads the ledger
    o_clean = build_order(select_plan(led, "plan-1"), LIMITS, FRESH, QUOTE)
    o_dirty = build_order(select_plan(led, "plan-1"), LIMITS, FRESH, QUOTE)
    assert o_clean == o_dirty
    assert o_clean["ok"] is True
    assert o_clean["order"]["entry"] == "364.18" and o_clean["order"]["stop_loss"] == "358.32"
    assert o_clean["order"]["take_profit"] == "370.04"

    v_clean = check_order(o_clean["order"], LIMITS, DayState(), MON_1015,
        gate=gate_for_order(o_clean["order"], MON_1015))
    v_dirty = check_order(o_dirty["order"], LIMITS, DayState(), MON_1015,
        gate=gate_for_order(o_dirty["order"], MON_1015))
    assert v_clean == v_dirty == {"verdict": "PASS", "reasons": []}


def test_build_order_is_sized_by_size_position_unchanged():
    rec = plan_record()
    direct = size_position(Plan("TSLA", D("364.18"), D("358.32"), D("370.04")), LIMITS,
                           slippage=SLIPPAGE)
    assert build_order(rec, LIMITS, FRESH, QUOTE) == {**direct, "quote": QUOTE_DETAILS}


# ---- AST guard ----

FORBIDDEN = {"confidence", "evidence", "point", "catalysts", "stance", "reports"}
FORBIDDEN_RE = re.compile(r"\b(" + "|".join(sorted(FORBIDDEN)) + r")\b", re.IGNORECASE)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def forbidden_uses(source: str) -> list[str]:
    """Names, attributes, args, keywords, imports or strings (not docstrings) naming analyst
    output."""
    tree = ast.parse(source)
    docs = _docstring_nodes(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN:
            out.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
            out.append(f"attribute {node.attr}")
        elif isinstance(node, ast.arg) and node.arg in FORBIDDEN:
            out.append(f"arg {node.arg}")
        elif isinstance(node, ast.keyword) and node.arg in FORBIDDEN:
            out.append(f"keyword {node.arg}")
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in FORBIDDEN:
            out.append(f"import {node.name}")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docs):
            m = FORBIDDEN_RE.search(node.value)
            if m:
                out.append(f"string {m.group(0)}")
    return out


def imports_analyst_report(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and any(a.name in ("AnalystReport", "*")
                                                    for a in node.names):
            return True
        if isinstance(node, ast.Import) and any(a.name.endswith("AnalystReport")
                                                for a in node.names):
            return True
        if isinstance(node, ast.Attribute | ast.Name) and getattr(
                node, "attr", getattr(node, "id", None)) == "AnalystReport":
            return True
    return False


@pytest.mark.parametrize("ident", sorted(FORBIDDEN))
def test_ast_checker_catches_each_identifier(ident):
    assert forbidden_uses(f"x = {ident}") != []  # bare name
    assert forbidden_uses(f"x = obj.{ident}") != []  # attribute
    assert forbidden_uses(f"def f({ident}): pass") != []  # parameter
    assert forbidden_uses(f"f({ident}=1)") != []  # keyword
    assert forbidden_uses(f"x = '{ident}'") != []  # string constant
    assert forbidden_uses(f"x = 'the {ident.upper()} value'") != []


@pytest.mark.parametrize("src", [
    'x = packet.reports["x"].confidence',
    'x = getattr(r, "confidence")',
    'x = kw["confidence"]',
    'def f(p):\n    return p.reports["technical"].evidence[0].point',
    'from m import confidence',
])
def test_ast_checker_catches_evasions(src):
    assert forbidden_uses(src) != []


@pytest.mark.parametrize("src", [
    '"""Module mentions confidence and stance."""\nx = 1',
    'def f():\n    """Never uses evidence or reports."""\n    return 1',
    'class C:\n    """catalysts point"""\n    y = 2',
    'x = "reasons"\ny = obj.reasoning',
])
def test_ast_checker_ignores_docstrings_and_lookalikes(src):
    assert forbidden_uses(src) == []


def test_ast_checker_docstring_exclusion_is_only_first_statement():
    assert forbidden_uses('def f():\n    x = 1\n    "confidence"') != []


@pytest.mark.parametrize("src", [
    "from agents.schemas import AnalystReport",
    "from agents.schemas import DecisionPacket, AnalystReport as R",
    "import agents.schemas.AnalystReport",
    "import agents.schemas as s\nx = s.AnalystReport",
    "from agents.schemas import *",
])
def test_import_checker_catches_analyst_report(src):
    assert imports_analyst_report(src)


def test_import_checker_allows_other_schema_imports():
    assert not imports_analyst_report("from agents.schemas import DecisionPacket, ExitPlanRecord")


@pytest.mark.parametrize("rel", ["pipeline/orders.py", "tools/size.py", "tools/risk_check.py"])
def test_money_path_modules_never_touch_analyst_output(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert forbidden_uses(src) == []


def test_orders_module_does_not_import_analyst_report():
    src = (ROOT / "pipeline" / "orders.py").read_text(encoding="utf-8")
    assert not imports_analyst_report(src)
    assert "def select_plan" in src  # sanity: the checker is looking at the right file


# ===================== #50: signatures =====================

def _params(fn, defaults_ok=frozenset()):
    sig = inspect.signature(fn)
    kinds = {p.kind for p in sig.parameters.values()}
    assert inspect.Parameter.VAR_KEYWORD not in kinds, f"{fn.__name__} takes **kwargs"
    assert inspect.Parameter.VAR_POSITIONAL not in kinds, f"{fn.__name__} takes *args"
    assert kinds <= {inspect.Parameter.POSITIONAL_OR_KEYWORD}
    assert all(p.default is inspect.Parameter.empty for p in sig.parameters.values()
               if p.name not in defaults_ok)
    return list(sig.parameters)


def test_size_position_signature_pinned():
    # #68: the only addition is `slippage`, a tool-computed allowance (tools.slippage); #138
    # makes it a REQUIRED keyword-only argument (no default of 0)
    sig = inspect.signature(size_position)
    assert list(sig.parameters) == ["plan", "limits", "slippage"]
    plan_p, limits_p, slip_p = sig.parameters.values()
    assert plan_p.kind == limits_p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert slip_p.kind == inspect.Parameter.KEYWORD_ONLY
    assert all(p.default is inspect.Parameter.empty for p in sig.parameters.values())


def test_plan_fields_pinned():
    assert tuple(f.name for f in dataclasses.fields(Plan)) == (
        "symbol", "entry", "stop", "target")


def test_check_order_signature_pinned():
    # #112: the only addition is `gate`, the event-gate result (tools.event_gate); it defaults to
    # None, and a missing gate is a BLOCK (tests/test_phase5_gate_d.py), never a pass
    assert _params(check_order, defaults_ok={"gate"}) == ["order", "limits", "state", "now", "gate"]
    assert inspect.signature(check_order).parameters["gate"].default is None


def test_build_order_signature_pinned():
    # Deliberately changed in issue #67 (the clock) and #68 (the live quote): still no way to
    # pass a price, size or score.
    assert _params(build_order, defaults_ok={"quote"}) == ["plan", "limits", "now", "quote"]


def test_select_plan_signature_pinned():
    # Deliberately changed in issue #64: plans come from the pipeline's ledger, not the packet.
    assert _params(select_plan) == ["ledger", "plan_id"]


def test_signature_pin_detects_added_parameter():
    def sneaky(plan, limits, confidence=1.0):
        pass

    def kw(plan, limits, **extra):
        pass

    with pytest.raises(AssertionError):
        _params(sneaky)
    with pytest.raises(AssertionError):
        _params(kw)


# ===================== #50: ORDER_FIELDS and check_order rejections =====================

REAL_PLAN = Plan("TSLA", D("364.18"), D("358.32"), D("370.04"))
ORDER = size_position(REAL_PLAN, LIMITS, slippage=SLIPPAGE)["order"]


def check(order, limits=LIMITS, now=MON_1015):
    return check_order(order, limits, DayState(), now, gate=gate_for_order(order, now))


def test_order_fields_equal_size_position_order_keys():
    assert ORDER_FIELDS == frozenset(ORDER)
    assert isinstance(ORDER_FIELDS, frozenset)


def test_clean_order_passes():
    assert check(ORDER) == {"verdict": "PASS", "reasons": []}


def test_confidence_field_blocks():
    r = check({**ORDER, "confidence": 0.99})
    assert r["verdict"] == "BLOCK"
    assert "order has unexpected field(s): confidence" in r["reasons"]


def test_extra_fields_listed_sorted():
    r = check({**ORDER, "zeta": 1, "confidence": 0.99, "alpha": 2, "Beta": 3})
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == ["order has unexpected field(s): Beta, alpha, confidence, zeta"]


def test_extra_field_added_to_other_reasons_not_replacing():
    r = check({**ORDER, "confidence": 0.5, "qty": 10_000})
    assert r["verdict"] == "BLOCK"
    assert "order has unexpected field(s): confidence" in r["reasons"]
    assert len(r["reasons"]) > 1  # the real rule violations are still reported


@pytest.mark.parametrize("field", sorted(ORDER_FIELDS))
def test_known_field_removed_is_never_flagged_as_unexpected(field):
    order = {k: v for k, v in ORDER.items() if k != field}
    assert field not in order and len(order) == len(ORDER_FIELDS) - 1
    r = check(order)  # may block for missing data; must not call a known field unexpected
    assert not any("unexpected field" in x for x in r["reasons"]), field


@pytest.mark.parametrize("bad", [[], [ORDER], None, "buy 100 TSLA", 5, ("symbol",)])
def test_non_dict_order_blocks_with_only_object_reason(bad):
    assert check(bad) == {"verdict": "BLOCK", "reasons": ["order must be an object"]}


def test_non_dict_order_reports_after_paper_check():
    live = replace(LIMITS, paper=False)
    r = check(["x"], limits=live)
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == [
        "live trading is disabled in this phase (paper must be true)",
        "order must be an object",
    ]


def test_non_dict_order_with_invalid_limits_reports_limits_first():
    bad = replace(LIMITS, max_loss_per_trade=D("-1"))
    r = check(None, limits=bad)
    assert r["verdict"] == "BLOCK"
    assert r["reasons"][0].startswith("limits invalid:")
    assert "order must be an object" not in r["reasons"]


# ===================== #50: confidence cannot change the order =====================

def test_two_confidences_same_plan_same_order():
    # Unit-level check; the end-to-end Trader proof through run_decision is in
    # tests/test_phase4b_proofs.py. Here each decision is carried alongside its packet.
    rec = plan_record()
    lo = packet_with([rec], fake=False, confidence=0.1, stance="bearish")
    hi = packet_with([rec], fake=False, confidence=0.99, stance="bullish")
    assert lo.reports["technical"].confidence == 0.1
    assert hi.reports["technical"].confidence == 0.99
    d_lo = TraderDecision(
        symbol="TSLA", action="BUY", confidence=0.1, reasoning="r", plan_id=rec.plan_id)
    d_hi = TraderDecision(
        symbol="TSLA", action="BUY", confidence=0.99, reasoning="r", plan_id=rec.plan_id)
    assert d_lo.confidence != d_hi.confidence
    for d in (d_lo, d_hi):  # the decision matches the plan it will be executed with
        assert d.action == "BUY" and d.symbol == rec.symbol and d.plan_id == rec.plan_id

    def order_for(decision, packet):
        assert decision.symbol == packet.symbol
        return build_order(select_plan(ledger_with(packet.exit_plans), "plan-1"), LIMITS, FRESH,
                           QUOTE)

    o_lo = order_for(d_lo, lo)
    o_hi = order_for(d_hi, hi)
    # swapping which decision goes with which packet changes nothing either
    assert order_for(d_hi, lo) == order_for(d_lo, hi) == o_lo
    assert o_lo == o_hi and o_lo["ok"] is True
    assert (o_lo["order"]["qty"], o_lo["order"]["stop_loss"], o_lo["order"]["take_profit"]) == (
        o_hi["order"]["qty"], o_hi["order"]["stop_loss"], o_hi["order"]["take_profit"])
    assert check(o_lo["order"]) == check(o_hi["order"])


def test_size_position_has_no_way_to_receive_confidence():
    with pytest.raises(TypeError):
        size_position(REAL_PLAN, LIMITS, confidence=0.99)  # type: ignore[call-arg]
    # there is no third positional (#138): the allowance is keyword-only, so a confidence-like
    # float cannot be passed positionally, and as slippage it is not a Decimal, so refused
    with pytest.raises(TypeError):
        size_position(REAL_PLAN, LIMITS, 0.99)  # type: ignore[misc]
    refused = size_position(REAL_PLAN, LIMITS, slippage=0.99)  # type: ignore[arg-type]
    assert refused == {"ok": False, "reason": "slippage must be a finite, non-negative Decimal"}
    with pytest.raises(TypeError):
        build_order(plan_record(), LIMITS, FRESH, QUOTE, confidence=0.99)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Plan("TSLA", D("1"), D("1"), D("2"), confidence=0.9)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        check_order(ORDER, LIMITS, DayState(), MON_1015, confidence=0.9)  # type: ignore[call-arg]


def test_build_order_passes_size_position_exactly_plan_and_limits(monkeypatch):
    seen = []
    real = orders.size_position

    def strict(*args, **kwargs):
        seen.append((args, kwargs))
        # exactly (Plan, Limits) plus the tool-computed slippage allowance (#68) for QUOTE,
        # nothing else
        if (set(kwargs) != {"slippage"} or len(args) != 2 or not isinstance(args[0], Plan)
                or not isinstance(args[1], Limits) or not isinstance(kwargs["slippage"], D)
                or kwargs["slippage"] != SLIPPAGE):
            raise AssertionError(f"size_position got extra input: {args!r} {kwargs!r}")
        assert tuple(dataclasses.asdict(args[0])) == ("symbol", "entry", "stop", "target")
        return real(*args, **kwargs)

    monkeypatch.setattr(orders, "size_position", strict)
    out = build_order(plan_record(), LIMITS, FRESH, QUOTE)
    assert out["ok"] is True and len(seen) == 1


def test_wiring_guard_would_catch_an_extra_argument(monkeypatch):
    """Prove the guard above has teeth: a build_order that sneaks confidence in must fail it."""
    def strict(*args, **kwargs):
        if kwargs or len(args) != 2:
            raise AssertionError("extra input")
        return {}

    def leaky_build(plan, limits):
        return strict(Plan(plan.symbol, D(plan.entry), D(plan.stop), D(plan.target)), limits,
                      0.99)

    with pytest.raises(AssertionError):
        leaky_build(plan_record(), LIMITS)
    with pytest.raises(AssertionError):
        strict(Plan("T", D("1"), D("1"), D("2")), LIMITS, confidence=0.5)


# ===================== ExitPlanRecord shape =====================

def test_exit_plan_record_fields_pinned():
    assert tuple(ExitPlanRecord.model_fields) == (
        "plan_id", "symbol", "setup", "entry", "stop", "target", "risk_per_share",
        "reward_to_risk", "time_exit", "entry_level", "structure_level", "as_of", "last_close",
        "labels", "context")


def test_exit_plan_record_is_frozen():
    rec = plan_record()
    for field, value in (("stop", "1.00"), ("entry", "0.01"), ("symbol", "AAPL")):
        with pytest.raises(ValueError):  # pydantic ValidationError subclasses ValueError
            setattr(rec, field, value)
    assert rec.stop == "358.32" and rec.entry == "364.18"


def test_exit_plan_record_forbids_extra_fields():
    with pytest.raises(ValueError):
        plan_record(confidence=0.9)
    with pytest.raises(ValueError):
        plan_record(qty=10_000)


# ===================== build_order re-validation =====================

@pytest.fixture
def size_spy(monkeypatch):
    calls = []
    real = orders.size_position

    def _spy(*a, **k):
        calls.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(orders, "size_position", _spy)
    return calls


def raw_plan(**over) -> ExitPlanRecord:
    """Built with model_construct so validation is bypassed, like a record mutated later."""
    d = plan_record().model_dump()
    d.update(over)
    return ExitPlanRecord.model_construct(**d)


BAD_PRICE = "plan prices must be finite whole cents"
BAD_ORDER = "plan must have stop < entry < target"
BAD_RISK = "plan risk_per_share does not match entry - stop"
BAD_SYMBOL = "plan symbol is not a valid ticker"

REFUSALS = [
    # mutated-looking records (as the AAPL ledger would have produced: entry 102.00)
    pytest.param(dict(symbol="AAPL", entry="102.00", stop="101.99", target="102.02",
                      risk_per_share="2.39"), BAD_RISK, id="mutated-stop-target-risk-stale"),
    pytest.param(dict(entry="364.18", risk_per_share="5.00"), BAD_RISK, id="risk-mismatch"),
    pytest.param(dict(risk_per_share="abc"), BAD_RISK, id="risk-not-a-number"),
    pytest.param(dict(risk_per_share="NaN"), BAD_RISK, id="risk-nan"),
    pytest.param(dict(symbol="not a ticker!"), BAD_SYMBOL, id="bad-symbol"),
    pytest.param(dict(symbol=""), BAD_SYMBOL, id="empty-symbol"),
    pytest.param(dict(symbol="TOOLONGSYMBOL"), BAD_SYMBOL, id="long-symbol"),
    pytest.param(dict(entry="abc"), BAD_PRICE, id="entry-abc"),
    pytest.param(dict(stop="NaN"), BAD_PRICE, id="stop-nan"),
    pytest.param(dict(target="sNaN"), BAD_PRICE, id="target-snan"),
    pytest.param(dict(entry="Infinity"), BAD_PRICE, id="entry-inf"),
    pytest.param(dict(entry="364.185"), BAD_PRICE, id="entry-subcent"),
    pytest.param(dict(stop="358.321"), BAD_PRICE, id="stop-subcent"),
    pytest.param(dict(target="370.0401"), BAD_PRICE, id="target-subcent"),
    pytest.param(dict(entry="0"), BAD_PRICE, id="entry-zero"),
    pytest.param(dict(stop="-1.00"), BAD_PRICE, id="stop-negative"),
    pytest.param(dict(stop="364.18", risk_per_share="0.00"), BAD_ORDER, id="stop-equals-entry"),
    pytest.param(dict(stop="365.00", risk_per_share="-0.82"), BAD_ORDER, id="stop-above-entry"),
    pytest.param(dict(target="364.18"), BAD_ORDER, id="target-equals-entry"),
    pytest.param(dict(target="360.00"), BAD_ORDER, id="target-below-entry"),
]


@pytest.mark.parametrize(("over", "reason"), REFUSALS)
def test_build_order_refuses_invalid_plan_without_sizing(size_spy, over, reason):
    out = build_order(raw_plan(**over), LIMITS, FRESH)
    assert out == {"ok": False, "reason": reason}
    assert size_spy == []  # size_position was never reached


def test_build_order_valid_record_still_sizes_exactly_as_before(size_spy):
    out = build_order(plan_record(), LIMITS, FRESH, QUOTE)
    direct = size_position(REAL_PLAN, LIMITS, slippage=SLIPPAGE)
    assert out == {**direct, "quote": QUOTE_DETAILS} and out["ok"] is True
    assert len(size_spy) == 1
    assert check(out["order"]) == {"verdict": "PASS", "reasons": []}


def test_build_order_model_construct_valid_record_also_sizes(size_spy):
    assert build_order(raw_plan(), LIMITS, FRESH, QUOTE) == {
        **size_position(REAL_PLAN, LIMITS, slippage=SLIPPAGE), "quote": QUOTE_DETAILS}
    assert len(size_spy) == 1


# ===================== select_plan symbol check =====================

def test_ledger_refuses_a_plan_for_another_symbol():
    with pytest.raises(LedgerError, match="plan symbol does not match the ledger"):
        PlanLedger("AAPL").record(plan_record(symbol="TSLA"))


def test_select_plan_matching_symbol_still_returned():
    assert select_plan(ledger_with([plan_record(symbol="TSLA")]), "plan-1").symbol == "TSLA"

# ===================== #65: only pipeline/order_flow.py touches build_order / check_order =====

MONEY_FNS = frozenset({"build_order", "check_order"})
ORDER_PACKAGES = ("agents", "pipeline", "tools", "journal")
ORDER_FLOW = "pipeline/order_flow.py"
BANNED_TYPES = frozenset({"AnalystReport", "DecisionPacket", "DebateRecord", "TraderDecision"})


def money_fn_references(source: str) -> list[str]:
    """Calls to, or any other reference to (import, alias, attribute, bare name), build_order or
    check_order. A `def` of either name is a definition, not a reference."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in MONEY_FNS:
            out.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in MONEY_FNS:
            out.append(f"attribute {node.attr}")
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in MONEY_FNS:
            out.append(f"import {node.name}")
    return out


def referenced_types(source: str) -> set[str]:
    """Imported or referenced names from BANNED_TYPES (AST only; docstrings do not count)."""
    found: set[str] = set()
    tree = ast.parse(source)
    docs = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.alias):
            found |= {node.name.split(".")[-1]} & BANNED_TYPES
        elif isinstance(node, ast.Name) and node.id in BANNED_TYPES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_TYPES:
            found.add(node.attr)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docs):
            found |= {t for t in BANNED_TYPES if node.value.strip() == t}  # a string annotation
    return found


def project_sources() -> dict[str, str]:
    out = {}
    for pkg in ORDER_PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            out[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return out


def call_names(source: str) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            names.append(getattr(node.func, "id", getattr(node.func, "attr", None)))
    return names


def test_money_reference_scanner_has_teeth():
    assert money_fn_references("build_order(p, l, n)")
    assert money_fn_references("orders.build_order(p, l, n)")
    assert money_fn_references("x.check_order(o, l, s, n)")
    assert money_fn_references("from pipeline.orders import build_order as bo\nbo(1)")
    assert money_fn_references("import tools.risk_check.check_order")
    assert money_fn_references("f = check_order\nf(1)")
    assert money_fn_references("def build_order(p):\n    return 1") == []  # a definition only
    assert money_fn_references("def f():\n    '''build_order'''\n    return 'check_order'") == []
    assert "build_order" in call_names("a.b(); build_order(1)")


def test_banned_type_scanner_has_teeth():
    assert referenced_types("from agents.schemas import DecisionPacket") == {"DecisionPacket"}
    assert referenced_types("import agents.schemas as s\nx: s.DebateRecord") == {"DebateRecord"}
    assert referenced_types("from agents.schemas import TraderDecision as T") == {"TraderDecision"}
    assert referenced_types("def f(p: 'AnalystReport'): pass") == {"AnalystReport"}
    assert referenced_types("from agents.schemas import ExitPlanRecord") == set()
    assert referenced_types('"""DecisionPacket"""\nx = 1') == set()


def test_only_order_flow_references_build_order_or_check_order():
    sources = project_sources()
    assert ORDER_FLOW in sources and "tools/risk_check.py" in sources
    assert "pipeline/orders.py" in sources  # sanity: the scan sees the defining modules
    users = {rel for rel, src in sources.items() if money_fn_references(src)}
    assert users == {ORDER_FLOW}, users
    # the two definitions live where expected, as defs (not references)
    defs = {rel for rel, src in sources.items()
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name in MONEY_FNS}
    assert defs == {"pipeline/orders.py", "tools/risk_check.py"}


def test_order_flow_is_the_only_caller_and_calls_both():
    callers = {rel: {n for n in call_names(src) if n in MONEY_FNS}
               for rel, src in project_sources().items()}
    assert {rel: names for rel, names in callers.items() if names} == {
        ORDER_FLOW: {"build_order", "check_order"}}


def test_order_flow_never_touches_model_output():
    src = (ROOT / ORDER_FLOW).read_text(encoding="utf-8")
    assert forbidden_uses(src) == []
    assert not imports_analyst_report(src)
    assert referenced_types(src) == set()


def test_every_module_that_references_the_money_functions_is_clean():
    checked = 0
    for rel, src in project_sources().items():
        if money_fn_references(src):
            checked += 1
            assert forbidden_uses(src) == [], rel
            assert not imports_analyst_report(src), rel
            assert referenced_types(src) == set(), rel
    assert checked == 1  # exactly order_flow; a second module would fail the test above


def test_order_flow_imports_only_what_the_order_path_needs():
    tree = ast.parse((ROOT / ORDER_FLOW).read_text(encoding="utf-8"))
    from_imports = {n.module: {a.name for a in n.names}
                    for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert from_imports["agents.schemas"] == {"ExitPlanRecord"}
    assert from_imports["agents.orchestrator"] == {"load_checked_limits"}
    for banned in ("agents.trader", "agents.debate", "agents.debate_common", "agents.bull",
                   "agents.bear", "agents.technical", "agents.sentiment", "agents.fundamentals",
                   "agents.runner", "agents.llm", "pipeline.decide"):
        assert banned not in from_imports, banned


def _calls_named(source: str, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None)) == name]


def _names_in(node: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def test_decide_passes_only_plan_id_to_propose_and_approve():
    src = (ROOT / "pipeline" / "decide.py").read_text(encoding="utf-8")
    proposes, approves = _calls_named(src, "propose"), _calls_named(src, "approve")
    assert len(proposes) == 1 and len(approves) == 1
    (pr,), (ap,) = proposes, approves

    # propose(ledger, plan_id, journal=..., clock=..., quotes=...): `quotes` is the live market
    # quote source (#68), never anything the model produced
    assert [getattr(a, "id", None) for a in pr.args] == ["ledger", "plan_id"]
    # `events` is the event-gate source (#112), built from the same data object
    assert sorted(k.arg for k in pr.keywords) == ["clock", "events", "journal", "quotes"]
    assert _names_in(pr) <= {"propose", "ledger", "plan_id", "journal", "clock", "quotes",
                             "events"}

    # approve(<the Proposal>, ledger, journal=..., ask=..., clock=...)
    assert len(ap.args) == 2 and getattr(ap.args[1], "id", None) == "ledger"
    assert sorted(k.arg for k in ap.keywords) == ["ask", "clock", "events", "journal", "quotes"]
    assert _names_in(ap) <= {"approve", "result", "proposal", "ledger", "journal", "ask", "clock",
                             "quotes", "events"}

    # quotes is assigned exactly once, as quotes_from(data): live market data, nothing else
    q_assigns = [n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Assign) and "quotes" in _names_in(n.targets[0])]
    assert len(q_assigns) == 1
    q_value = q_assigns[0].value
    assert isinstance(q_value, ast.Call) and getattr(q_value.func, "id", None) == "quotes_from"
    assert [getattr(a, "id", None) for a in q_value.args] == ["data"] and not q_value.keywords

    # events is assigned exactly once, as events_from(data): earnings, filings and news read from
    # the data object (#112), nothing a model produced
    e_assigns = [n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Assign) and "events" in _names_in(n.targets[0])]
    assert len(e_assigns) == 1
    e_value = e_assigns[0].value
    assert isinstance(e_value, ast.Call) and getattr(e_value.func, "id", None) == "events_from"
    assert [getattr(a, "id", None) for a in e_value.args] == ["data"] and not e_value.keywords

    # plan_id is assigned exactly once, from the decision's plan_id and nothing else
    assigns = [n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.AnnAssign | ast.Assign)
               and "plan_id" in _names_in(
                   n.target if isinstance(n, ast.AnnAssign) else n.targets[0])]
    assert len(assigns) == 1
    value = assigns[0].value
    assert isinstance(value, ast.Attribute) and value.attr == "plan_id"
    assert isinstance(value.value, ast.Name) and value.value.id == "decision"


def test_decide_never_reads_model_scores_or_text():
    src = (ROOT / "pipeline" / "decide.py").read_text(encoding="utf-8")
    assert forbidden_uses(src) == []
    attrs = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
    assert not attrs & {"confidence", "reasoning", "evidence", "argument", "objections",
                        "key_points", "exit_plans", "entry", "stop", "target"}


def test_decide_scanner_would_catch_a_leaked_argument():
    """Teeth: a decide() that also passes the Trader's confidence must fail the same checks."""
    bad = ("def f(ledger, plan_id, journal, clock, decision):\n"
           "    propose(ledger, plan_id, journal=journal, clock=clock,"
           " size=decision.size)\n")
    (call,) = _calls_named(bad, "propose")
    assert sorted(k.arg for k in call.keywords) != ["clock", "journal", "quotes"]
    assert not _names_in(call) <= {"propose", "ledger", "plan_id", "journal", "clock", "quotes"}



# Modules that feed plans, sizing, the risk gate or DayState never reference model output.
@pytest.mark.parametrize("rel", [
    "pipeline/orders.py", "tools/size.py", "tools/risk_check.py", "pipeline/ledger.py",
    "journal/log.py", "pipeline/order_flow.py", "tools/slippage.py", "tools/exit_plan.py",
])
def test_money_feeding_modules_reference_no_model_output_types(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert referenced_types(src) == set(), rel
    assert forbidden_uses(src) == [], rel
