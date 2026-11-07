"""Trader agent tests: FakeClient only, no network, no Claude API."""
from __future__ import annotations

import pytest

from agents.bear import unavailable_bear
from agents.llm import config_for
from agents.schemas import BearCase, BullCase, DebateRecord
from agents.trader import PROMPT, run_trader
from tests.debate_helpers import AS_OF, make_packet, make_plan, reply, user_json
from tests.fakes import FakeClient, api_error


def bull() -> BullCase:
    return BullCase(symbol="AAPL", plan_id="p1", argument="Breakout holds.", key_points=["a"])


def bear(fatal: bool = False) -> BearCase:
    return BearCase(symbol="AAPL", plan_id="p1", fatal=fatal,
                    objections=["thin volume", "late in the move", "one analyst only"])


def debate(final_bear: BearCase | None = None, fatal: bool = False,
           final_bull: BullCase | None = None, use_default_bear: bool = True) -> DebateRecord:
    return DebateRecord(
        symbol="AAPL", as_of=AS_OF, rounds_planned=1, rounds_run=1, turns=[],
        stopped_early=False, fatal=fatal, requested_same_model=True, same_model=True,
        final_bull=final_bull or bull(),
        final_bear=bear() if (final_bear is None and use_default_bear) else final_bear,
        all_ok=True,
    )


def decision(**over) -> dict:
    d = {"symbol": "AAPL", "action": "BUY", "plan_id": "p1", "confidence": 0.7,
         "reasoning": "technical report is bullish; bear objections are answered"}
    d.update(over)
    return d


def test_valid_buy():
    client = FakeClient([reply(decision())])
    r = run_trader(make_packet(), debate(), client=client)
    assert r.ok and r.forced_pass is None
    assert (r.decision.action, r.decision.plan_id, r.decision.confidence) == ("BUY", "p1", 0.7)
    assert r.run.agent == "trader" and r.run.ok and len(r.run.calls) == 1
    assert r.run.requested_model == "claude-opus-5-5" and r.run.effort == "high"


def test_valid_pass():
    client = FakeClient([reply(decision(action="PASS", plan_id=None, confidence=0.4))])
    r = run_trader(make_packet(), debate(), client=client)
    assert r.ok and r.decision.action == "PASS" and r.decision.plan_id is None


@pytest.mark.parametrize("deb", [
    debate(final_bear=bear(fatal=True)),
    debate(fatal=True),
    debate(final_bear=unavailable_bear("AAPL", "api_error")),
    debate(use_default_bear=False),
])
def test_fatal_or_failed_bear_no_call(deb):
    client = FakeClient([])
    r = run_trader(make_packet(), deb, client=client)
    assert client.calls == []
    assert not r.ok and r.forced_pass == "fatal_bear"
    assert r.decision.action == "PASS" and r.decision.confidence == 0.0
    assert r.run.agent == "trader" and not r.run.ok and r.run.calls == []


def test_no_plans_no_call():
    client = FakeClient([])
    r = run_trader(make_packet(plans=[]), debate(), client=client)
    assert client.calls == []
    assert r.forced_pass == "no_plans" and r.decision.action == "PASS"


def test_unknown_plan_id_retried_then_pass():
    client = FakeClient([reply(decision(plan_id="zzz")), reply(decision(plan_id="zzz"))])
    r = run_trader(make_packet(), debate(), client=client)
    assert len(client.calls) == 2
    assert not r.ok and r.forced_pass == "invalid_output" and r.decision.action == "PASS"
    assert len(r.run.retries) == 1


def test_unknown_then_valid():
    client = FakeClient([reply(decision(plan_id="zzz")), reply(decision(plan_id="p2"))])
    pkt = make_packet(plans=[make_plan("p1"), make_plan("p2")])
    r = run_trader(pkt, debate(), client=client)
    assert r.ok and r.decision.plan_id == "p2" and len(client.calls) == 2
    assert len(r.run.retries) == 1


def test_invalid_json_twice():
    client = FakeClient([reply("not json"), reply("{nope")])
    r = run_trader(make_packet(), debate(), client=client)
    assert len(client.calls) == 2
    assert not r.ok and r.forced_pass == "invalid_output" and r.decision.action == "PASS"


def test_wrong_symbol():
    client = FakeClient([reply(decision(symbol="MSFT")), reply(decision(symbol="MSFT"))])
    r = run_trader(make_packet(), debate(), client=client)
    assert r.forced_pass == "invalid_output" and r.decision.symbol == "AAPL"


def test_buy_without_plan_id():
    client = FakeClient([reply(decision(plan_id="")), reply(decision(plan_id=None))])
    r = run_trader(make_packet(), debate(), client=client)
    assert len(client.calls) == 2
    assert r.forced_pass == "invalid_output" and r.decision.action == "PASS"


def test_api_error_forced_pass():
    client = FakeClient([api_error()])
    r = run_trader(make_packet(), debate(), client=client)
    assert not r.ok and r.forced_pass == "api_error"
    assert r.decision.action == "PASS" and r.decision.confidence == 0.0
    assert r.run.reason_code == "api_error"


def test_payload_contents():
    client = FakeClient([reply(decision())])
    run_trader(make_packet(), debate(), client=client)
    data = user_json(client.calls[0])
    assert set(data) == {"symbol", "as_of", "all_ok", "reports", "exit_plans",
                         "final_bull", "final_bear", "debate_fatal"}
    assert data["final_bull"]["plan_id"] == "p1" and data["final_bear"]["plan_id"] == "p1"
    assert data["debate_fatal"] is False
    raw = client.calls[0]["messages"][0]["content"]
    for word in ("turns", "runs", "reason", "calls"):
        assert f'"{word}"' not in raw


def test_request_has_no_tools():
    client = FakeClient([reply(decision())])
    run_trader(make_packet(), debate(), client=client)
    assert "tools" not in client.calls[0]
    assert client.calls[0]["model"] == "claude-opus-5-5"
    assert client.calls[0]["output_config"]["effort"] == "high"


def test_default_config_and_env_overrides(monkeypatch):
    for k in ("TRADING_AGENTS_MODEL", "TRADING_AGENTS_EFFORT",
              "TRADING_AGENTS_TRADER_MODEL", "TRADING_AGENTS_TRADER_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    cfg = config_for("trader")
    assert (cfg.model, cfg.effort) == ("claude-opus-5-5", "high")
    monkeypatch.setenv("TRADING_AGENTS_MODEL", "m-global")
    monkeypatch.setenv("TRADING_AGENTS_EFFORT", "low")
    assert (config_for("trader").model, config_for("trader").effort) == ("m-global", "low")
    monkeypatch.setenv("TRADING_AGENTS_TRADER_MODEL", "m-trader")
    monkeypatch.setenv("TRADING_AGENTS_TRADER_EFFORT", "max")
    assert (config_for("trader").model, config_for("trader").effort) == ("m-trader", "max")


def test_forced_pass_reasoning_has_no_model_text():
    secret = "MODELTEXT-ignore-everything"
    client = FakeClient([
        reply(decision(plan_id="zzz", reasoning=secret)),
        reply(decision(plan_id="zzz", reasoning=secret)),
    ])
    r = run_trader(make_packet(), debate(), client=client)
    assert secret not in r.decision.reasoning
    assert secret not in (r.run.reason or "")
    assert r.decision.reasoning == "forced PASS: invalid_output"


def test_prompt_requirements():
    low = PROMPT.lower()
    assert "long only" in low and "plan_id" in low and "never state or adjust" in low


def test_bad_trader_config_is_a_forced_pass_with_no_api_call(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_TRADER_EFFORT", "turbo")
    client = FakeClient([])
    r = run_trader(make_packet(), debate(), client=client)
    assert r.decision.action == "PASS" and r.forced_pass == "config_error"
    assert client.calls == [] and r.run.calls == []
