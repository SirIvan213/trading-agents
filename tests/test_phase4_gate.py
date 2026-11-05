"""Phase 4 risk gate: issues #39 (exact get_bars prices), #40 (no money from report text),
#50 (confidence never reaches sizing or risk_check). Offline: fixtures and fakes only."""
import ast
import asyncio
import dataclasses
import inspect
import re
import threading
from dataclasses import replace
from datetime import datetime
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

AAPL_LEVELS = {
    "session_low": "99.70", "session_high": "103.20", "last_close": "103.10",
    "or_low": "99.70", "or_high": "102.00", "vwap": "101.60",
}
NAMES = "session_low, session_high, last_close, or_low, or_high, vwap"
ENTRY_MSG = f"entry must be one of get_bars price_levels: {NAMES}"
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

@pytest.mark.parametrize(("name", "entry"), ALL_SPELLINGS)
def test_each_allowed_value_as_entry_reaches_exit_plan_orb(spy, name, entry):
    s = loaded()
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": entry})
    assert err is False and "error" not in res
    assert len(spy) == 1  # exit_plan may refuse structurally, but the gate let it through
    assert spy[0][0][0] == "orb" and spy[0][0][1] == D(AAPL_LEVELS[name])


@pytest.mark.parametrize(("name", "entry"), ALL_SPELLINGS)
def test_each_allowed_value_as_entry_reaches_exit_plan_level(spy, name, entry):
    s = loaded()
    res, err = s.execute("plan_exit", {"setup": "level", "entry": entry, "level": "99.70"})
    assert err is False and "error" not in res
    assert len(spy) == 1 and spy[0][0][1] == D(AAPL_LEVELS[name])


@pytest.mark.parametrize("name", ["or_high", "session_high", "last_close", "vwap"])
def test_allowed_entry_above_structure_gives_ok_plan(name):
    res, err = loaded().execute("plan_exit", {"setup": "orb", "entry": AAPL_LEVELS[name]})
    assert err is False and res["ok"] is True


def test_entry_spellings_of_or_high_all_equal():
    for sp in ("102", "102.0", "102.00", "102.000"):
        res, err = loaded().execute("plan_exit", {"setup": "orb", "entry": sp})
        assert err is False and res["ok"] is True
        assert D(res["entry"]) == D("102.00")  # exit_plan echoes the spelling it was given


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
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "103.20", "level": level})
    assert err is False and "error" not in res
    assert len(spy) == 1
    assert spy[0][1]["level"] == D(AAPL_LEVELS[name])


@pytest.mark.parametrize("level", IN_RANGE_NOT_RETURNED + OUT_OF_RANGE)
def test_level_not_a_returned_price_is_refused(spy, level):
    s = loaded()
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": level})
    assert (res, err) == ({"error": LEVEL_MSG}, True)
    assert spy == [] and s.exit_plans == []


def test_valid_entry_does_not_excuse_invalid_level(spy):
    res, err = loaded().execute(
        "plan_exit", {"setup": "level", "entry": "102.00", "level": "102.50"})
    assert err is True and res == {"error": LEVEL_MSG}
    assert spy == []


def test_refusal_lists_available_level_names():
    res, _ = loaded().execute("plan_exit", {"setup": "orb", "entry": "102.50"})
    for name in AAPL_LEVELS:
        assert name in res["error"]
    res, _ = loaded().execute(
        "plan_exit", {"setup": "level", "entry": "102.00", "level": "102.50"})
    for name in AAPL_LEVELS:
        assert name in res["error"]


def test_refusal_names_exclude_or_levels_without_opening_range(spy):
    summary = {"session_low": "10", "session_high": "11", "last_close": "10.5", "vwap": "10.4",
               "atr": None, "opening_range": None}
    summary["price_levels"] = price_levels(summary)
    s = make(summary)
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "10.75"})
    assert err is True
    assert res == {"error": "entry must be one of get_bars price_levels: "
                            "session_low, session_high, last_close, vwap"}
    assert "or_low" not in res["error"] and "or_high" not in res["error"]
    assert spy == []


def test_plan_exit_without_price_levels_in_summary_refuses(spy):
    s = make({"session_low": "99.70", "session_high": "103.20", "opening_range": None})
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "100.00"})
    assert err is True and res["error"].startswith("entry must be one of get_bars price_levels")
    assert spy == []


# ===================== #39: ledger =====================

def test_ok_plan_adds_record_with_sequential_ids_and_result_has_plan_id():
    s = loaded()
    r1, _ = s.execute("plan_exit", {"setup": "orb", "entry": "102.00"})
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
    s.execute("plan_exit", {"setup": "orb", "entry": "102"})
    (rec,) = s.exit_plans
    assert rec.entry == "102.00"
    direct = real_exit_plan(
        "orb", D("102.00"), s.limits, opening_range=s.summary["opening_range"],
        atr=D(s.summary["atr"]))
    for f in ("entry", "stop", "target", "risk_per_share", "reward_to_risk", "time_exit"):
        assert getattr(rec, f) == direct[f], f


def test_record_level_names():
    s = loaded()
    s.execute("plan_exit", {"setup": "orb", "entry": "102"})
    s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "101.6"})
    s.execute("plan_exit", {"setup": "level", "entry": "103.2", "level": "99.70"})
    orb, lvl, lvl2 = s.exit_plans
    assert (orb.entry_level, orb.structure_level) == ("or_high", "or_low")
    assert (lvl.entry_level, lvl.structure_level) == ("last_close", "vwap")
    assert lvl2.entry_level == "session_high"
    # 99.70 is both session_low and or_low; either name is a truthful match
    assert lvl2.structure_level in ("session_low", "or_low")


def test_refused_plan_adds_no_record():
    s = loaded()
    s.execute("plan_exit", {"setup": "orb", "entry": "102.50"})
    s.execute("plan_exit", {"setup": "level", "entry": "102.00", "level": "102.50"})
    s.execute("plan_exit", {"setup": "level", "entry": "102.00"})
    s.execute("plan_exit", {"setup": "bogus", "entry": "102.00"})
    s.execute("plan_exit", {"setup": "orb", "entry": "102.00", "atr": "0.01"})
    assert s.exit_plans == []


def test_ok_false_plan_adds_no_record_and_no_plan_id():
    s = loaded()
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "99.70"})  # entry at structure
    assert err is False and res["ok"] is False and "plan_id" not in res
    assert s.exit_plans == []
    res, _ = s.execute("plan_exit", {"setup": "orb", "entry": "102.00"})
    assert res["plan_id"] == "plan-1"  # failed attempts do not consume ids


def test_no_ledger_before_get_bars():
    s = make()
    s.execute("plan_exit", {"setup": "orb", "entry": "102.00"})
    assert s.exit_plans == []


# ===================== #40: AnalystRun / DecisionPacket =====================

def bars_then_plan(entry="102.00"):
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
    assert run.exit_plans[0].entry == "102.00" and run.exit_plans[0].symbol == "AAPL"


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
    assert run.exit_plans[0].entry == "102.00"


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
        response([tool_use("p", "plan_exit", {"setup": "orb", "entry": "102.00"})], "tool_use"),
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
        "structure_level": "vwap", "as_of": "2026-10-19T10:15:00-04:00",
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


def test_select_plan_returns_record_by_id():
    a, b = plan_record("plan-1"), plan_record("plan-2", entry="365.00")
    packet = packet_with([a, b], fake=False)
    assert select_plan(packet, "plan-1") is a
    assert select_plan(packet, "plan-2") is b


@pytest.mark.parametrize("bad", ["plan-3", "", "PLAN-1", "plan-1 ", "999", "0"])
def test_select_plan_unknown_id_raises_keyerror(bad):
    with pytest.raises(KeyError):
        select_plan(packet_with([plan_record("plan-1")], fake=False), bad)


def test_select_plan_on_empty_packet_raises_keyerror():
    with pytest.raises(KeyError):
        select_plan(packet_with([], fake=False), "plan-1")


def test_fake_price_text_has_no_effect_on_order_or_verdict():
    rec = plan_record()
    clean = packet_with([rec], fake=False)
    dirty = packet_with([rec], fake=True)
    # the fake text really is in the dirty packet and not in the clean one
    dumped = dirty.model_dump_json()
    assert all(t in dumped for t in FAKE_TEXT)
    assert not any(t in clean.model_dump_json() for t in FAKE_TEXT)

    # A TraderDecision whose free text carries the fake prices is also in play. The Trader
    # agent does not exist yet; the end-to-end Trader proof is a Phase 4 follow-up.
    decision = TraderDecision(
        symbol="TSLA", action="BUY", confidence=0.99, setup=rec.setup,
        reasoning="; ".join(FAKE_TEXT))
    assert decision.symbol == dirty.symbol and decision.action == "BUY"
    o_clean = build_order(select_plan(clean, "plan-1"), LIMITS, FRESH)
    o_dirty = build_order(select_plan(dirty, "plan-1"), LIMITS, FRESH)
    assert o_clean == o_dirty
    assert o_clean["ok"] is True
    assert o_clean["order"]["entry"] == "364.18" and o_clean["order"]["stop_loss"] == "358.32"
    assert o_clean["order"]["take_profit"] == "370.04"

    v_clean = check_order(o_clean["order"], LIMITS, DayState(), MON_1015)
    v_dirty = check_order(o_dirty["order"], LIMITS, DayState(), MON_1015)
    assert v_clean == v_dirty == {"verdict": "PASS", "reasons": []}


def test_build_order_is_sized_by_size_position_unchanged():
    rec = plan_record()
    direct = size_position(Plan("TSLA", D("364.18"), D("358.32"), D("370.04")), LIMITS)
    assert build_order(rec, LIMITS, FRESH) == direct


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
    assert "DecisionPacket" in src  # sanity: the checker is looking at the right file


# ===================== #50: signatures =====================

def _params(fn):
    sig = inspect.signature(fn)
    kinds = {p.kind for p in sig.parameters.values()}
    assert inspect.Parameter.VAR_KEYWORD not in kinds, f"{fn.__name__} takes **kwargs"
    assert inspect.Parameter.VAR_POSITIONAL not in kinds, f"{fn.__name__} takes *args"
    assert kinds <= {inspect.Parameter.POSITIONAL_OR_KEYWORD}
    assert all(p.default is inspect.Parameter.empty for p in sig.parameters.values())
    return list(sig.parameters)


def test_size_position_signature_pinned():
    assert _params(size_position) == ["plan", "limits"]


def test_plan_fields_pinned():
    assert tuple(f.name for f in dataclasses.fields(Plan)) == (
        "symbol", "entry", "stop", "target")


def test_check_order_signature_pinned():
    assert _params(check_order) == ["order", "limits", "state", "now"]


def test_build_order_signature_pinned():
    # Deliberately changed in issue #67: the order path now takes the clock.
    assert _params(build_order) == ["plan", "limits", "now"]


def test_select_plan_signature_pinned():
    assert _params(select_plan) == ["packet", "plan_id"]


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
ORDER = size_position(REAL_PLAN, LIMITS)["order"]


def check(order, limits=LIMITS, now=MON_1015):
    return check_order(order, limits, DayState(), now)


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
    # NOTE: the Trader agent does not exist yet. The end-to-end proof (a real Trader run feeding
    # select_plan -> build_order) is a Phase 4 follow-up; here each decision is carried
    # alongside its packet and the order is built in that decision's context.
    rec = plan_record()
    lo = packet_with([rec], fake=False, confidence=0.1, stance="bearish")
    hi = packet_with([rec], fake=False, confidence=0.99, stance="bullish")
    assert lo.reports["technical"].confidence == 0.1
    assert hi.reports["technical"].confidence == 0.99
    d_lo = TraderDecision(
        symbol="TSLA", action="BUY", confidence=0.1, reasoning="r", setup=rec.setup)
    d_hi = TraderDecision(
        symbol="TSLA", action="BUY", confidence=0.99, reasoning="r", setup=rec.setup)
    assert d_lo.confidence != d_hi.confidence
    for d in (d_lo, d_hi):  # the decision matches the plan it will be executed with
        assert d.action == "BUY" and d.symbol == rec.symbol and d.setup == rec.setup

    def order_for(decision, packet):
        assert decision.symbol == packet.symbol
        return build_order(select_plan(packet, "plan-1"), LIMITS, FRESH)

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
    with pytest.raises(TypeError):
        size_position(REAL_PLAN, LIMITS, 0.99)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_order(plan_record(), LIMITS, FRESH, confidence=0.99)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Plan("TSLA", D("1"), D("1"), D("2"), confidence=0.9)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        check_order(ORDER, LIMITS, DayState(), MON_1015, confidence=0.9)  # type: ignore[call-arg]


def test_build_order_passes_size_position_exactly_plan_and_limits(monkeypatch):
    seen = []
    real = orders.size_position

    def strict(*args, **kwargs):
        seen.append((args, kwargs))
        if kwargs or len(args) != 2 or not isinstance(args[0], Plan) \
                or not isinstance(args[1], Limits):
            raise AssertionError(f"size_position got extra input: {args!r} {kwargs!r}")
        assert tuple(dataclasses.asdict(args[0])) == ("symbol", "entry", "stop", "target")
        return real(*args)

    monkeypatch.setattr(orders, "size_position", strict)
    out = build_order(plan_record(), LIMITS, FRESH)
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
        "reward_to_risk", "time_exit", "entry_level", "structure_level", "as_of")


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
    out = build_order(plan_record(), LIMITS, FRESH)
    direct = size_position(REAL_PLAN, LIMITS)
    assert out == direct and out["ok"] is True
    assert len(size_spy) == 1
    assert check(out["order"]) == {"verdict": "PASS", "reasons": []}


def test_build_order_model_construct_valid_record_also_sizes(size_spy):
    assert build_order(raw_plan(), LIMITS, FRESH) == size_position(REAL_PLAN, LIMITS)
    assert len(size_spy) == 1


# ===================== select_plan symbol check =====================

def test_select_plan_symbol_mismatch_raises_keyerror():
    aapl_packet = packet_with([plan_record(symbol="TSLA")], fake=False)
    aapl_packet = aapl_packet.model_copy(update={"symbol": "AAPL"})
    with pytest.raises(KeyError, match="plan symbol does not match packet"):
        select_plan(aapl_packet, "plan-1")


def test_select_plan_matching_symbol_still_returned():
    packet = packet_with([plan_record(symbol="TSLA")], fake=False)
    assert select_plan(packet, "plan-1").symbol == "TSLA"
