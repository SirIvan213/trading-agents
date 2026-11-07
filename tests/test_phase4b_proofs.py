"""Phase 4B end-to-end proofs (#85, #65) through pipeline.decide.run_decision.

All six agents are fakes behind one router client keyed on the system prompt. The technical
analyst really calls get_bars and plan_exit through its fake, so the plan ledger is filled by
plan_exit. No network, no Claude API, no billed call.
"""
import json
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

import agents.bear as bear_agent
import agents.bull as bull_agent
import agents.fundamentals as fund_agent
import agents.orchestrator as orch
import agents.sentiment as sent_agent
import agents.technical as tech_agent
import agents.trader as trader_agent
import pipeline.decide as decide
import pipeline.order_flow as of
from agents.schemas import TraderDecision
from agents.trader import TraderRun
from journal.log import APPROVED, Journal
from pipeline.decide import run_decision
from tests.debate_helpers import bear_json, bull_json, reply
from tests.fakes import NOON, FakeAllData, report_json, response, text, tool_use
from tests.order_helpers import StepClock, raw_lines
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tools.config import CONFIG_DIR, load_limits

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
SYMBOL = "ABCD"
PROMPT_TO_AGENT = {
    tech_agent.PROMPT: "technical", sent_agent.PROMPT: "sentiment",
    fund_agent.PROMPT: "fundamentals", bull_agent.PROMPT: "bull", bear_agent.PROMPT: "bear",
    trader_agent.PROMPT: "trader",
}
FAKE_WORDS = ["stop 1.00", "target 999.99", "buy 10000 shares", "entry 0.01"]
FAKE_TEXT = "; ".join(FAKE_WORDS)


@pytest.fixture(autouse=True)
def isolated_agent_env(monkeypatch):
    """The shell's TRADING_AGENTS_* settings must not change these tests."""
    import os
    for name in list(os.environ):
        if name.startswith("TRADING_AGENTS_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def fixed_limits(monkeypatch):
    """The example limits for the analysts and the order flow, whatever config/ holds."""
    monkeypatch.setattr(orch, "load_limits", lambda *a, **k: LIMITS)
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)


class Router:
    """One shared client: pops the next scripted reply for whichever agent is calling."""

    def __init__(self, scripts: dict[str, list]) -> None:
        assert len(PROMPT_TO_AGENT) == 6  # six distinct system prompts, one per agent
        self.scripts = {a: list(q) for a, q in scripts.items()}
        self.calls: list[str] = []
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        agent = PROMPT_TO_AGENT[kwargs["system"][0]["text"]]
        with self._lock:
            self.calls.append(agent)
            self.requests.append({"agent": agent, **kwargs})
            queue = self.scripts.get(agent) or []
            if not queue:
                raise AssertionError(f"unexpected API call by {agent}")
            return queue.pop(0)


def tech_script(point="breakout above the opening range"):
    """get_bars, then plan_exit (orb at the opening-range high), then the report."""
    return [
        response([tool_use("b", "get_bars", {"symbol": SYMBOL})], "tool_use"),
        response([tool_use("p", "plan_exit", {"setup": "orb", "entry": "103.10"})], "tool_use"),
        response([text(report_json(
            agent="technical", symbol=SYMBOL,
            evidence=[{"point": point, "source": "get_bars"}]))], "end_turn"),
    ]


def other_script(agent, tool, point):
    return [
        response([tool_use("u", tool, {"symbol": SYMBOL})], "tool_use"),
        response([text(report_json(
            agent=agent, symbol=SYMBOL, evidence=[{"point": point, "source": tool}]))],
            "end_turn"),
    ]


def trader_json(confidence=0.7, reasoning="technical is bullish; the bear's objections are met",
                action="BUY", plan_id="plan-1"):
    return {"symbol": SYMBOL, "action": action, "plan_id": plan_id, "confidence": confidence,
            "reasoning": reasoning}


def scripts(*, tech_point="breakout above the opening range", sent_point="two positive headlines",
            bull=None, bear=None, trader=None):
    return {
        "technical": tech_script(tech_point),
        "sentiment": other_script("sentiment", "get_news", sent_point),
        "fundamentals": other_script("fundamentals", "get_fundamentals", "revenue grew"),
        "bull": [reply(bull if bull is not None else bull_json(plan_id="plan-1", symbol=SYMBOL))],
        "bear": [reply(bear if bear is not None else bear_json(plan_id="plan-1", symbol=SYMBOL))],
        "trader": [reply(trader if trader is not None else trader_json())],
    }


def go(tmp_path, scr, *, ask=lambda _: "yes", clock=None, name="j.jsonl"):
    router = Router(scr)
    journal = Journal(tmp_path / name)
    clock = clock or StepClock(NOON, NOON + timedelta(seconds=20), NOON + timedelta(seconds=40))
    result = run_decision(SYMBOL, data=FakeAllData(), clients=router, journal=journal,
                          clock=clock, ask=ask, rounds=1, timeout_s=10)
    return result, router, journal


def evidence_text(result):
    return " ".join(e.point for r in result.packet.reports.values() for e in r.evidence)


def case_text(result):
    out = []
    for t in result.debate.turns:
        out.append(t.case.model_dump_json())
    return " ".join(out)


def money(result):
    """The parts that must be identical whatever the models said."""
    o = result.outcome
    return {
        "order": o.recheck.order, "sized": o.recheck.sized, "verdict": o.recheck.verdict,
        "plan": o.recheck.plan.model_dump(), "status": o.status,
        "proposed": result.proposal.sized, "proposed_verdict": result.proposal.verdict,
    }


@pytest.fixture
def baseline(tmp_path):
    result, router, journal = go(tmp_path, scripts(), name="base.jsonl")
    assert result.outcome.status == APPROVED
    return result, journal


# ===================== baseline =====================

def test_baseline_buy_is_sized_from_the_tool_plan(baseline):
    result, journal = baseline
    assert result.trader.ok and result.trader.decision.plan_id == "plan-1"
    plan = result.outcome.recheck.plan
    assert (plan.entry, plan.setup, plan.symbol) == ("103.10", "orb", SYMBOL)
    assert plan.as_of == NOON.isoformat()
    o = result.outcome.recheck.order
    assert (o["entry"], o["stop_loss"], o["take_profit"]) == (plan.entry, plan.stop, plan.target)
    assert o["symbol"] == SYMBOL and o["side"] == "buy" and o["qty"] > 0
    assert result.outcome.recheck.verdict == {"verdict": "PASS", "reasons": []}
    (entry,) = raw_lines(journal)
    assert entry["status"] == APPROVED and entry["order"] == o
    assert entry["run_id"] == result.ledger_run_id and entry["plan_age_s"] == 40
    assert "Buy" in result.ticket and "NOT sent" in result.ticket


def test_the_ledger_was_filled_by_plan_exit_through_the_technical_fake(tmp_path):
    result, router, _ = go(tmp_path, scripts())
    tech_requests = [r for r in router.requests if r["agent"] == "technical"]
    assert len(tech_requests) == 3  # get_bars, plan_exit, report
    tool_names = [t["name"] for t in tech_requests[0]["tools"]]
    assert "get_bars" in tool_names and "plan_exit" in tool_names
    assert [p.plan_id for p in result.packet.exit_plans] == ["plan-1"]
    assert result.outcome.recheck.plan == result.packet.exit_plans[0]
    assert sorted(set(router.calls)) == sorted(PROMPT_TO_AGENT.values())


def test_timings_recorded(baseline):
    result, _ = baseline
    assert set(result.timings_ms) == {"analysts", "debate", "trader", "approval"}


# ===================== (a) debate text cannot change the order =====================

def test_a_tampered_debate_text_changes_nothing(tmp_path, baseline):
    clean, _ = baseline
    # The money guard allows a number a report's evidence already quotes, so put the fake
    # numbers in the sentiment evidence; then the debaters may repeat them and validate.
    quoted = "chatter: stop 1.00, target 999.99, buy 10000 shares, entry 0.01"
    tampered_bull = bull_json(
        argument=f"Hype says {FAKE_TEXT}.", key_points=FAKE_WORDS[:2] + ["technical is bullish"],
        plan_id="plan-1", symbol=SYMBOL)
    tampered_bear = bear_json(
        objections=[FAKE_WORDS[2], FAKE_WORDS[3], "late in the move"], plan_id="plan-1",
        symbol=SYMBOL)
    dirty, _, journal = go(tmp_path, scripts(
        sent_point=quoted, bull=tampered_bull, bear=tampered_bear), name="dirty.jsonl")

    # the tampered text really is in the stored debate, and the debate validated
    assert all(t.run.ok for t in dirty.debate.turns) and dirty.debate.all_ok
    assert all(w in case_text(dirty) for w in FAKE_WORDS)
    assert not any(w in case_text(clean) for w in FAKE_WORDS)
    # ... and the money side is byte-for-byte the same
    assert dirty.outcome.status == APPROVED
    assert money(dirty) == money(clean)
    (entry,) = raw_lines(journal)
    assert entry["order"] == money(clean)["order"] and entry["max_loss"] == money(clean)[
        "sized"]["max_loss"]


def test_a_tampered_debate_the_guard_rejects_fails_closed_and_orders_nothing(tmp_path):
    """Fake prices the evidence does NOT quote never reach the Trader: the Bear fails closed."""
    bad_bear = bear_json(objections=FAKE_WORDS[:3], plan_id="plan-1", symbol=SYMBOL)
    scr = scripts(bear=bad_bear)
    scr["bear"] = [reply(bad_bear), reply(bad_bear)]  # rejected, retried, rejected again
    result, router, journal = go(tmp_path, scr, ask=lambda _: pytest.fail("asked"))
    assert result.debate.fatal and not result.debate.all_ok
    assert FAKE_WORDS[0] not in case_text(result)
    assert result.trader.forced_pass == "fatal_bear" and "trader" not in router.calls
    assert result.proposal is None and result.outcome is None
    assert [e["status"] for e in raw_lines(journal)] == ["pass"]


# ===================== (b) analyst evidence text cannot change the order =====================

def test_b_tampered_analyst_evidence_changes_nothing(tmp_path, baseline):
    clean, _ = baseline
    dirty_point = f"{FAKE_TEXT}, risk 50000"
    dirty, _, journal = go(tmp_path, scripts(tech_point=dirty_point, sent_point=dirty_point))
    assert dirty_point in evidence_text(dirty) and dirty_point not in evidence_text(clean)
    assert dirty.packet.runs["technical"].ok
    assert dirty.outcome.status == APPROVED
    assert money(dirty) == money(clean)


def test_b_tampered_evidence_is_what_the_trader_sees_but_not_what_the_order_uses(tmp_path):
    dirty_point = FAKE_TEXT
    result, router, _ = go(tmp_path, scripts(tech_point=dirty_point, sent_point=dirty_point))
    trader_request = next(r for r in router.requests if r["agent"] == "trader")
    assert "stop 1.00" in json.dumps(trader_request["messages"])  # it was in the model's input
    o = result.outcome.recheck.order
    assert "1.00" not in (o["stop_loss"], o["entry"]) and o["entry"] == "103.10"
    assert o["take_profit"] != "999.99" and o["qty"] != 10000


# ===================== (c) Trader confidence and text cannot change the order ==============

@pytest.mark.parametrize("confidence", [0.1, 0.99])
def test_c_trader_confidence_and_fake_prices_change_nothing(tmp_path, baseline, confidence):
    clean, _ = baseline
    reasoning = f"I would {FAKE_TEXT}, size up, risk 50000"
    dirty, _, journal = go(tmp_path, scripts(trader=trader_json(
        confidence=confidence, reasoning=reasoning)))
    assert dirty.trader.ok and dirty.trader.decision.confidence == confidence
    assert FAKE_WORDS[0] in dirty.trader.decision.reasoning
    assert dirty.outcome.status == APPROVED
    assert money(dirty) == money(clean)
    text_ = journal.path.read_text(encoding="utf-8")
    assert "confidence" not in text_ and "999.99" not in text_


def test_c_low_and_high_confidence_runs_are_identical_to_each_other(tmp_path):
    lo, _, _ = go(tmp_path, scripts(trader=trader_json(confidence=0.1)), name="lo.jsonl")
    hi, _, _ = go(tmp_path, scripts(trader=trader_json(confidence=0.99)), name="hi.jsonl")
    assert lo.trader.decision.confidence != hi.trader.decision.confidence
    assert money(lo) == money(hi)


def test_all_three_tampered_at_once_changes_nothing(tmp_path, baseline):
    clean, _ = baseline
    quoted = "chatter: stop 1.00, target 999.99, buy 10000 shares, entry 0.01"
    dirty, _, _ = go(tmp_path, scripts(
        tech_point=quoted, sent_point=quoted,
        bull=bull_json(argument=f"Hype says {FAKE_TEXT}.", plan_id="plan-1", symbol=SYMBOL),
        bear=bear_json(objections=[FAKE_WORDS[0], FAKE_WORDS[1], "thin volume"],
                       plan_id="plan-1", symbol=SYMBOL),
        trader=trader_json(confidence=0.99, reasoning=FAKE_TEXT)))
    assert dirty.outcome.status == APPROVED and money(dirty) == money(clean)


# ===================== refusals through run_decision =====================

def test_fatal_bear_forces_pass_with_no_proposal(tmp_path):
    scr = scripts(bear=bear_json(fatal=True, plan_id="plan-1", symbol=SYMBOL))
    scr["trader"] = []  # the Trader must not even be called
    result, router, journal = go(tmp_path, scr, ask=lambda _: pytest.fail("asked"))
    assert result.debate.fatal
    assert result.trader.forced_pass == "fatal_bear" and result.trader.ok is False
    assert result.trader.decision.action == "PASS" and "trader" not in router.calls
    assert result.proposal is None and result.outcome is None and result.ticket is None
    (entry,) = raw_lines(journal)
    assert entry["status"] == "pass" and entry["forced_pass"] == "fatal_bear"
    assert journal.day_state(NOON).trades_today == 0


def test_failed_bear_fails_closed_to_pass(tmp_path):
    scr = scripts()
    scr["bear"] = [reply("not json"), reply("still not json")]
    result, router, journal = go(tmp_path, scr, ask=lambda _: pytest.fail("asked"))
    assert result.debate.fatal and result.trader.forced_pass == "fatal_bear"
    assert result.proposal is None and "trader" not in router.calls


def test_trader_pass_gives_no_proposal(tmp_path):
    scr = scripts(trader=trader_json(action="PASS", plan_id="", confidence=0.4))
    result, _, journal = go(tmp_path, scr, ask=lambda _: pytest.fail("asked"))
    assert result.trader.ok and result.trader.decision.action == "PASS"
    assert result.proposal is None and result.outcome is None
    assert [e["status"] for e in raw_lines(journal)] == ["pass"]


def test_trader_plan_id_not_in_the_packet_is_a_forced_pass(tmp_path):
    scr = scripts()
    scr["trader"] = [reply(trader_json(plan_id="plan-99")), reply(trader_json(plan_id="plan-99"))]
    result, _, journal = go(tmp_path, scr, ask=lambda _: pytest.fail("asked"))
    assert result.trader.forced_pass == "invalid_output"
    assert result.proposal is None and result.outcome is None
    assert APPROVED not in [e["status"] for e in raw_lines(journal)]


def test_trader_plan_id_not_in_the_ledger_gives_no_order(tmp_path, monkeypatch):
    """Bypass the Trader's own validator: a BUY naming an id the ledger never recorded."""
    def rogue_trader(packet, debate, *, client, config=None, cancel=None):
        d = TraderDecision(symbol=SYMBOL, action="BUY", plan_id="plan-99", confidence=1.0,
                           reasoning=FAKE_TEXT)
        return TraderRun(d, True, None, SimpleNamespace(calls=[], retries=[]))

    monkeypatch.setattr(decide, "run_trader", rogue_trader)
    scr = scripts()
    scr["trader"] = []
    result, _, journal = go(tmp_path, scr, ask=lambda _: pytest.fail("asked"))
    assert result.proposal.ok is False and result.proposal.stage == "plan"
    assert result.proposal.reason == "unknown plan_id" and result.proposal.order is None
    assert result.outcome.status == "no_order"
    assert APPROVED not in [e["status"] for e in raw_lines(journal)]
    assert "NO ORDER" in result.ticket


def test_technical_failure_empties_the_ledger_and_forces_pass(tmp_path):
    scr = scripts()
    scr["technical"] = scr["technical"][:2] + [response([text("not json")], "end_turn")] * 2
    scr["trader"] = []
    result, router, journal = go(tmp_path, scr, ask=lambda _: pytest.fail("asked"))
    assert result.packet.runs["technical"].ok is False and result.packet.exit_plans == []
    assert result.trader.forced_pass in ("fatal_bear", "no_plans")
    assert result.proposal is None and "trader" not in router.calls
    assert APPROVED not in [e["status"] for e in raw_lines(journal)]


def test_a_decline_leaves_no_approved_entry_and_the_day_untouched(tmp_path):
    result, _, journal = go(tmp_path, scripts(), ask=lambda _: "y")
    assert result.proposal.ok and result.outcome.status == "declined"
    assert [e["status"] for e in raw_lines(journal)] == ["declined"]
    assert journal.day_state(NOON).trades_today == 0


def test_ask_none_stops_after_the_ticket(tmp_path):
    result, _, journal = go(tmp_path, scripts(), ask=None)
    assert result.proposal.ok and result.outcome is None and "Buy" in result.ticket
    assert not journal.path.exists()


def test_slow_pipeline_makes_the_plan_stale_before_approval(tmp_path):
    # readings: analysts start, before the Trader, proposal, re-check after approval
    clock = StepClock(NOON, NOON + timedelta(seconds=10), NOON + timedelta(seconds=20),
                      NOON + timedelta(seconds=301))
    result, _, journal = go(tmp_path, scripts(), clock=clock)
    assert result.proposal.ok
    assert result.outcome.status == "refused_after_approval"
    assert APPROVED not in [e["status"] for e in raw_lines(journal)]


def test_the_plan_age_limit_boundary_through_the_pipeline(tmp_path):
    # readings: analysts start, before the Trader, proposal, re-check after approval
    clock = StepClock(NOON, NOON + timedelta(seconds=10), NOON + timedelta(seconds=20),
                      NOON + timedelta(seconds=300))
    result, _, _ = go(tmp_path, scripts(), clock=clock)
    assert result.outcome.status == APPROVED and result.outcome.plan_age_s == 300


def test_market_closed_after_hours_gets_no_order(tmp_path):
    late = NOON.replace(hour=16, minute=30)  # the plan is fine, the entry window is closed
    result, _, journal = go(tmp_path, scripts(), clock=StepClock(late), ask=lambda _: pytest.fail(
        "asked"))
    assert result.proposal.ok is False and result.proposal.stage == "risk"
    assert result.outcome.status == "no_order"


def test_naive_clock_raises_before_any_api_call(tmp_path):
    router = Router(scripts())
    with pytest.raises(ValueError):
        run_decision(SYMBOL, data=FakeAllData(), clients=router,
                     journal=Journal(tmp_path / "j"), clock=lambda: NOON.replace(tzinfo=None),
                     ask=None)
    assert router.calls == []


def test_invalid_limits_raise_before_any_api_call(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(orch, "load_limits", boom)
    router = Router(scripts())
    with pytest.raises(ValueError, match="limits config is invalid"):
        run_decision(SYMBOL, data=FakeAllData(), clients=router,
                     journal=Journal(tmp_path / "j"), clock=lambda: NOON, ask=None)
    assert router.calls == []


def test_each_run_has_its_own_ledger(tmp_path):
    a, _, _ = go(tmp_path, scripts(), name="a.jsonl")
    b, _, _ = go(tmp_path, scripts(), name="b.jsonl")
    assert a.ledger_run_id != b.ledger_run_id
    assert a.outcome.recheck.plan == b.outcome.recheck.plan  # same plan, separate ledgers



# ===================== latency budget (#65) =====================

def test_budget_is_recorded_against_max_plan_age(tmp_path):
    result, _, _ = go(tmp_path, scripts())
    b = result.budget
    assert b["max_plan_age_s"] == LIMITS.max_plan_age_s
    assert b["analyst_timeout_s"] == 10  # go() passes timeout_s=10
    assert b["age_before_trader_s"] == 20 and b["age_at_proposal_s"] == 40
    assert b["plan_age_at_approval_s"] == 40
    assert set(result.timings_ms) == {"analysts", "debate", "trader", "approval"}


@pytest.mark.parametrize("age", [
    LIMITS.max_plan_age_s - decide.MIN_APPROVAL_S + 1, LIMITS.max_plan_age_s + 1])
def test_plans_stale_before_the_trader_skip_the_trader_call(tmp_path, age):
    clock = StepClock(NOON, NOON + timedelta(seconds=age))
    result, router, journal = go(tmp_path, scripts(), clock=clock,
                                 ask=lambda _: pytest.fail("must not ask"))
    assert "trader" not in router.calls  # no billed Trader call
    assert result.trader.forced_pass == "stale_before_trader"
    assert result.trader.decision.action == "PASS" and result.proposal is None
    assert [e["status"] for e in raw_lines(journal)] == ["pass"]


def test_budget_boundary_still_calls_the_trader(tmp_path):
    # exactly MIN_APPROVAL_S left for the human: the Trader is still called
    clock = StepClock(NOON, NOON + timedelta(seconds=LIMITS.max_plan_age_s
                                             - decide.MIN_APPROVAL_S))
    result, router, _ = go(tmp_path, scripts(), clock=clock, ask=lambda _: "no")
    assert "trader" in router.calls and result.trader.ok


@pytest.mark.parametrize("bad", [0, -1, LIMITS.max_plan_age_s, LIMITS.max_plan_age_s + 60])
def test_analyst_timeout_must_leave_room_for_the_rest(tmp_path, bad):
    router = Router(scripts())
    with pytest.raises(ValueError, match="timeout_s must be above 0 and below"):
        run_decision(SYMBOL, data=FakeAllData(), clients=router,
                     journal=Journal(tmp_path / "j.jsonl"), clock=StepClock(NOON),
                     ask=lambda _: "yes", rounds=1, timeout_s=bad)
    assert router.calls == []


def test_default_analyst_timeout_is_half_the_plan_age(tmp_path, monkeypatch):
    seen = {}
    real = orch.run_analysts

    async def spy(*a, **k):
        seen["timeout_s"] = k["timeout_s"]
        return await real(*a, **k)

    monkeypatch.setattr(decide, "run_analysts", spy)
    run_decision(SYMBOL, data=FakeAllData(), clients=Router(scripts()),
                 journal=Journal(tmp_path / "j.jsonl"),
                 clock=StepClock(NOON, NOON + timedelta(seconds=20)), ask=lambda _: "no",
                 rounds=1)
    assert seen["timeout_s"] == LIMITS.max_plan_age_s / 2


@pytest.mark.parametrize("var", ["TRADING_AGENTS_TRADER_EFFORT", "TRADING_AGENTS_BEAR_EFFORT"])
def test_bad_agent_config_fails_before_any_api_call(tmp_path, monkeypatch, var):
    monkeypatch.setenv(var, "turbo")
    router = Router(scripts())
    with pytest.raises(ValueError):
        run_decision(SYMBOL, data=FakeAllData(), clients=router,
                     journal=Journal(tmp_path / "j.jsonl"), clock=StepClock(NOON),
                     ask=lambda _: "yes", rounds=1, timeout_s=10)
    assert router.calls == []


@pytest.mark.parametrize("bad", [None, 123, "", "toolongsymbol"])
def test_bad_symbol_is_a_value_error_before_any_call(tmp_path, bad):
    router = Router(scripts())
    with pytest.raises(ValueError, match="symbol must be a valid ticker"):
        run_decision(bad, data=FakeAllData(), clients=router,  # type: ignore[arg-type]
                     journal=Journal(tmp_path / "j.jsonl"), clock=StepClock(NOON),
                     ask=lambda _: "yes", rounds=1, timeout_s=10)
    assert router.calls == []


def test_ticket_says_when_the_plan_expires(tmp_path):
    result, _, _ = go(tmp_path, scripts(), ask=lambda _: "no")
    assert result.proposal.expires_at == (NOON + timedelta(
        seconds=LIMITS.max_plan_age_s)).isoformat()
    assert f"Approve before {result.proposal.expires_at}" in result.ticket


def test_ledger_is_sealed_unaccepted_when_run_analysts_raises(monkeypatch):
    import asyncio

    from pipeline.ledger import PlanLedger

    async def boom(*a, **k):
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(orch, "_one", boom)
    led = PlanLedger(SYMBOL)
    with pytest.raises(RuntimeError):
        asyncio.run(orch.run_analysts(SYMBOL, NOON, data=FakeAllData(), clients=Router({}),
                                      limits=LIMITS, ledger=led))
    assert led.sealed and led.plan_ids() == []
