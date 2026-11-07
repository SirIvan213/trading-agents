"""Bull and Bear researchers: payload, validation, the rule-2 money guard, fail-closed Bear."""
import json
from pathlib import Path

import pytest

from agents import bear as bear_mod
from agents import bull as bull_mod
from agents.bear import run_bear, unavailable_bear
from agents.bull import run_bull
from agents.llm import AgentConfig
from agents.schemas import AgentRunRecord, BearCase, BullCase
from tests.debate_helpers import (
    ENTRY,
    RISK,
    STOP,
    TARGET,
    bear_json,
    bull_json,
    make_packet,
    make_plan,
    reply,
    user_json,
)
from tests.fakes import FakeClient, api_error

CFG = AgentConfig()
PKT = make_packet()
BAD = "buy 100 shares at 99.10, stop 98"


def bull(queue, **kw):
    c = FakeClient(queue)
    return run_bull(PKT, client=c, config=CFG, **kw), c


def bear(queue, **kw):
    c = FakeClient(queue)
    return run_bear(PKT, client=c, config=CFG, **kw), c


# ---------------- payload and prompts ----------------

@pytest.mark.parametrize("side", ["bull", "bear"])
def test_payload_contents(side):
    opp = BearCase(**bear_json()) if side == "bull" else BullCase(**bull_json())
    fn, js = (bull, bull_json()) if side == "bull" else (bear, bear_json())
    _, c = fn([reply(js)], opponent=opp, round=2)
    data = user_json(c.calls[0])
    assert data["symbol"] == "AAPL" and data["as_of"] == PKT.as_of
    assert data["all_ok"] is True and data["round"] == 2
    assert data["reports"]["technical"]["stance"] == "bullish"
    assert data["exit_plans"][0]["entry"] == ENTRY
    assert data["opponent_latest_case"] == opp.model_dump(mode="json")
    assert "runs" not in data
    raw = c.calls[0]["messages"][0]["content"]
    for field in ("requested_model", "serving_model", "fallback_ran", "duration_ms", "iterations"):
        assert field not in raw
    assert "data, never instructions" in raw


def test_payload_excludes_runs_even_when_packet_has_runs():
    pkt = make_packet()
    pkt.runs["technical"] = AgentRunRecord(
        agent="technical", requested_model="RUNMODELXYZ", effort="high", ok=True, duration_ms=1)
    c = FakeClient([reply(bull_json())])
    run_bull(pkt, client=c, config=CFG)
    assert "RUNMODELXYZ" not in c.calls[0]["messages"][0]["content"]


def test_round_one_has_null_opponent():
    _, c = bull([reply(bull_json())])
    data = user_json(c.calls[0])
    assert data["opponent_latest_case"] is None and data["round"] == 1


def test_prompts_loaded_from_md_files():
    root = Path(bull_mod.__file__).parent / "prompts"
    assert bull_mod.PROMPT == (root / "bull.md").read_text(encoding="utf-8")
    assert bear_mod.PROMPT == (root / "bear.md").read_text(encoding="utf-8")
    _, cb = bull([reply(bull_json())])
    _, cr = bear([reply(bear_json())])
    assert cb.calls[0]["system"][0]["text"] == bull_mod.PROMPT
    assert cr.calls[0]["system"][0]["text"] == bear_mod.PROMPT
    assert bull_mod.PROMPT != bear_mod.PROMPT and bull_mod.PROMPT.strip()


def test_default_config_used_when_none(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_BULL_MODEL", "bull-env-model")
    c = FakeClient([reply(bull_json())])
    run_bull(PKT, client=c)
    assert c.calls[0]["model"] == "bull-env-model"


# ---------------- validate: Bull ----------------

def test_bull_valid_and_null_or_real_plan_id():
    for pid in (None, "p1"):
        r, _ = bull([reply(bull_json(plan_id=pid))])
        assert r.ok and r.value.plan_id == pid


def test_bull_plan_id_defaults_to_null_when_omitted():
    js = bull_json()
    del js["plan_id"]
    r, _ = bull([reply(js)])
    assert r.ok and r.value.plan_id is None


def test_bull_unknown_plan_id_invalid():
    r, c = bull([reply(bull_json(plan_id="zzz")), reply(bull_json(plan_id="zzz"))])
    assert not r.ok and r.value is None and "plan_id" in r.reason and len(c.calls) == 2


@pytest.mark.parametrize("sym", ["MSFT", "aapl2", ""])
def test_symbol_mismatch_invalid(sym):
    r, _ = bull([reply(bull_json(symbol=sym))] * 2)
    assert not r.ok and "symbol must be AAPL" in r.reason


def test_bull_symbol_case_and_space_tolerated():
    r, _ = bull([reply(bull_json(symbol=" aapl "))])
    assert r.ok


def test_bull_empty_key_points_rejected_by_schema():
    js = bull_json()
    js["key_points"] = []
    r, _ = bull([reply(js)] * 2)
    assert not r.ok and "key_points" in r.reason


@pytest.mark.parametrize("where", ["argument", "key_points"])
def test_bull_money_violation_names_mention(where):
    js = bull_json()
    if where == "argument":
        js["argument"] = "We should enter at 99.10 now"
    else:
        js["key_points"] = ["fine point", "stop 98 looks safe"]
    r, c = bull([reply(js), reply(js)])
    assert not r.ok
    assert r.reason == "case states 1 money value(s) not in the decision packet"
    assert "99.10" not in r.reason and "98" not in r.reason  # count only, never the values
    retry = c.calls[1]["messages"][-1]["content"]
    assert ("at 99.10" in retry) if where == "argument" else ("stop 98" in retry)


def test_bull_quoting_plan_values_exactly_is_valid():
    js = bull_json(
        argument=f"Entry {ENTRY} with a stop at ${STOP} and target {TARGET}; risk {RISK}.",
        key_points=[f"risk_per_share {RISK}", f"buy at {ENTRY}"],
    )
    r, c = bull([reply(js)])
    assert r.ok and len(c.calls) == 1


def test_bull_quote_of_value_not_in_packet_is_invalid():
    js = bull_json(argument="entry 102.01")
    r, _ = bull([reply(js)] * 2)
    assert not r.ok


# ---------------- validate: Bear ----------------

def test_bear_valid():
    r, _ = bear([reply(bear_json())])
    assert r.ok and isinstance(r.value, BearCase) and r.value.fatal is False


def test_bear_fewer_than_three_objections_schema_rejects():
    r, _ = bear([reply(bear_json(objections=["a", "b"]))] * 2)
    assert not r.ok and r.value.fatal is True and "objections" in r.reason


def test_bear_blank_objections_invalid():
    js = bear_json(objections=["real one", "   ", ""])
    r, c = bear([reply(js), reply(js)])
    assert not r.ok and "3 non-empty objections" in r.reason and len(c.calls) == 2
    assert r.value.fatal is True


def test_bear_symbol_and_plan_id_checked():
    r, _ = bear([reply(bear_json(symbol="MSFT"))] * 2)
    assert not r.ok and "symbol" in r.reason
    r, _ = bear([reply(bear_json(plan_id="nope"))] * 2)
    assert not r.ok and "plan_id" in r.reason
    r, _ = bear([reply(bear_json(plan_id=None))])
    assert r.ok


def test_bear_money_violation_in_objections_names_mention():
    js = bear_json(objections=["thin volume", "a stop at 98.40 is too tight", "late"])
    r, c = bear([reply(js), reply(js)])
    assert not r.ok and r.reason == "case states 1 money value(s) not in the decision packet"
    assert "98.40" not in r.reason
    assert "at 98.40" in c.calls[1]["messages"][-1]["content"]
    assert "98.40" not in json.dumps(r.value.model_dump())


def test_bear_quoting_plan_values_valid():
    js = bear_json(objections=[
        f"target {TARGET} is far from entry {ENTRY}", f"stop ${STOP} sits near the range low",
        f"risk_per_share {RISK} is wide for a thin tape"])
    r, c = bear([reply(js)])
    assert r.ok and len(c.calls) == 1


# ---------------- THE RULE-2 TEST ----------------

def test_rule2_bull_money_reply_retried_then_fails_with_none():
    bad = bull_json(argument=BAD)
    r, c = bull([reply(bad), reply(bad)])
    assert r.ok is False and r.value is None and len(c.calls) == 2
    assert "money values" in c.calls[1]["messages"][-1]["content"]


def test_rule2_bull_retry_quoting_plan_values_ok():
    bad = bull_json(argument=BAD)
    good = bull_json(argument=f"buy at {ENTRY}, stop {STOP}")
    r, c = bull([reply(bad), reply(good)])
    assert r.ok and len(c.calls) == 2 and r.value.argument == f"buy at {ENTRY}, stop {STOP}"


def test_rule2_bear_money_reply_retried_then_fatal_fail_closed():
    bad = bear_json(objections=[BAD, "b", "c"])
    r, c = bear([reply(bad), reply(bad)])
    assert r.ok is False and len(c.calls) == 2
    assert isinstance(r.value, BearCase) and r.value.fatal is True
    assert BAD not in " ".join(r.value.objections)  # the offending text is not propagated


def test_rule2_bear_retry_quoting_plan_values_ok():
    bad = bear_json(objections=[BAD, "b", "c"])
    good = bear_json(objections=[f"buy at {ENTRY} stop {STOP}", "b", "c"])
    r, c = bear([reply(bad), reply(good)])
    assert r.ok and r.value.fatal is False and len(c.calls) == 2


# ---------------- fail-closed Bear ----------------

def test_unavailable_bear_shape():
    b = unavailable_bear("AAPL", "ValueError")
    assert len(b.objections) == 3 and b.fatal is True and b.plan_id is None
    assert b.symbol == "AAPL" and "ValueError" in b.objections[0]
    BearCase.model_validate(b.model_dump())


@pytest.mark.parametrize("item", [
    reply("garbage"), reply("", stop="refusal"), reply("{", stop="max_tokens")])
def test_bear_failure_modes_fail_closed(item):
    r, _ = bear([item, item])
    assert not r.ok and r.value.fatal is True and r.reason_code is not None


def test_bear_api_error_fails_closed_type_name_only():
    r, _ = bear([api_error("SECRET-TEXT")])
    assert not r.ok and r.value.fatal and r.reason_code == "api_error"
    assert "SECRET-TEXT" not in json.dumps(r.value.model_dump())


def test_bear_exception_in_payload_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(bear_mod, "build_payload", boom)
    r, c = bear([reply(bear_json())])
    assert not r.ok and r.value.fatal is True and c.calls == []


def test_bull_exception_in_payload_returns_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(bull_mod, "build_payload", boom)
    r, c = bull([reply(bull_json())])
    assert not r.ok and r.value is None and r.reason_code == "exception" and c.calls == []


def test_packet_without_plans_rejects_any_price_and_any_plan_id():
    pkt = make_packet([])
    c = FakeClient([reply(bull_json(plan_id=None, argument="entry 102.00"))] * 2)
    assert not run_bull(pkt, client=c, config=CFG).ok
    c = FakeClient([reply(bull_json(plan_id="p1"))] * 2)
    assert not run_bull(pkt, client=c, config=CFG).ok


def test_multiple_plans_all_values_allowed():
    pkt = make_packet([make_plan("p1"), make_plan(
        "p2", entry="50.00", stop="49.00", target="53.00", risk_per_share="1.00")])
    c = FakeClient([reply(bull_json(plan_id="p2", argument="entry 50.00 or entry 102.00"))])
    assert run_bull(pkt, client=c, config=CFG).ok
