"""Decision-quality changes #125, #123, #108, #113, #122. Offline: scenario fixtures, fakes."""
from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from agents.bear import unavailable_bear
from agents.debate_common import build_payload
from agents.money_guard import (
    allowed_values,
    context_ratios,
    money_violations,
    quoted_values,
)
from agents.schemas import DebateRecord, ExitPlanRecord, PlanContext, PlanLabels
from agents.tool_registry import TOOLS, price_levels
from agents.trader import PROMPT as TRADER_PROMPT
from agents.trader import build_trader_payload, run_trader
from tests.debate_helpers import make_packet, make_plan, reply
from tests.fakes import FakeClient
from tests.test_plan_labels import labels
from tests.test_scenario_review import session
from tests.test_trader import debate as make_debate
from tests.test_trader import decision
from tools.plan_labels import STRUCTURAL_LEVELS

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
PROMPT_NAMES = ["technical", "bull", "bear", "trader"]
DEFINITION = ('`stop_within_max_width` true means the stop is no wider than the configured '
              'maximum (`max_stop_atr_5m` five-minute ATRs from entry): a good thing, never a '
              'sign the stop is "too tight".')


def prompt(name: str) -> str:
    return (AGENTS_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8")


def clean_vwap(s, **extra):
    lv = price_levels(s.summary)
    return s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                   "level": lv["vwap"], **extra})


# ===================== #125: stop_within_max_width =====================

def test_plan_labels_output_key_is_stop_within_max_width():
    out = labels()
    assert "stop_within_max_width" in out and "tight_stop" not in out


def test_plan_labels_model_field_renamed():
    assert "stop_within_max_width" in PlanLabels.model_fields
    assert "tight_stop" not in PlanLabels.model_fields
    with pytest.raises(ValueError):  # the old name is an extra field and is refused
        PlanLabels(extended=False, tight_stop=True, suggested_multiple="1",
                   atr_5m_provisional=False)


def test_plan_exit_result_and_record_carry_the_new_label_only():
    s = session("clean-buy")
    res, err = clean_vwap(s)
    assert err is False and res["labels"]["stop_within_max_width"] is True
    assert "tight_stop" not in res["labels"]
    rec = s.exit_plans[0]
    assert rec.labels.stop_within_max_width is True
    assert "tight_stop" not in rec.labels.model_dump()


def test_tight_stop_appears_in_no_prompt_schema_or_tool_description():
    for name in PROMPT_NAMES + ["sentiment", "fundamentals"]:
        assert "tight_stop" not in prompt(name), name
    assert "tight_stop" not in (AGENTS_DIR / "schemas.py").read_text(encoding="utf-8")
    assert "tight_stop" not in json.dumps(TOOLS)
    plan_exit = next(t for t in TOOLS if t["name"] == "plan_exit")
    assert "stop_within_max_width" in plan_exit["description"]


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_each_prompt_has_the_one_line_definition(name):
    t = prompt(name)
    assert DEFINITION in t
    assert "never a sign the stop is" in t


@pytest.mark.parametrize("name", ["sentiment", "fundamentals"])
def test_prompts_without_plans_do_not_carry_the_definition(name):
    assert "never a sign the stop is" not in prompt(name)  # negative control


def test_bear_prompt_forbids_arguing_the_label_means_too_tight():
    assert "Never argue that `stop_within_max_width` true makes a stop" in prompt("bear")
    assert "Never argue that `stop_within_max_width`" not in prompt("bull")  # bear-only line


def test_stop_within_max_width_is_false_just_past_the_maximum():
    assert labels(risk_per_share=D("0.50"))["stop_within_max_width"] is True
    assert labels(risk_per_share=D("0.51"))["stop_within_max_width"] is False


# ===================== #123: structural_stop dropped =====================

def test_no_structural_stop_anywhere_in_labels():
    assert "structural_stop" not in labels()
    assert "structural_stop" not in PlanLabels.model_fields
    s = session("clean-buy")
    res, _ = clean_vwap(s)
    assert "structural_stop" not in res["labels"]
    assert "structural_stop" not in s.exit_plans[0].labels.model_dump()
    for name in PROMPT_NAMES:
        assert "structural_stop" not in prompt(name), name


def test_extra_structural_stop_field_is_refused():
    with pytest.raises(ValueError):
        PlanLabels(extended=False, stop_within_max_width=True, structural_stop=True,
                   suggested_multiple="2", atr_5m_provisional=False)


@pytest.mark.parametrize("name", sorted(STRUCTURAL_LEVELS))
def test_structural_level_with_other_conditions_gets_good_multiple(name):
    assert labels(structure_level=name)["suggested_multiple"] == "2"


@pytest.mark.parametrize("name", ["last_close", "session_high", "", "bogus", None])
def test_non_structural_structure_level_still_gets_the_minimum(name):
    out = labels(structure_level=name)
    assert out["suggested_multiple"] == "1"
    assert out["stop_within_max_width"] is True and out["extended"] is False


def test_plan_record_structure_level_names_the_anchor():
    s = session("clean-buy")
    res, _ = clean_vwap(s)
    assert s.exit_plans[0].structure_level == "vwap"
    assert res["labels"]["suggested_multiple"] == "2"


def test_technical_prompt_points_to_structure_level_for_the_anchor():
    assert "structure_level" in prompt("technical")


# ===================== #108: analyst_failed forced PASS =====================

def _packet(**kw):
    return make_packet(**kw)


def _failed(**kw):
    return make_packet(**kw).model_copy(update={"all_ok": False})


def _debate(packet, **over):
    return make_debate().model_copy(update={"as_of": packet.as_of, **over})


def test_analyst_failed_forces_pass_and_never_calls_the_client():
    client = FakeClient([])
    packet = _failed()
    r = run_trader(packet, _debate(packet), client=client)
    assert client.calls == []
    assert r.ok is False and r.forced_pass == "analyst_failed"
    assert r.decision.action == "PASS" and r.decision.plan_id is None
    assert r.decision.confidence == 0.0
    assert r.run.ok is False and r.run.reason == "analyst_failed"
    assert r.run.reason_code is None and r.run.calls == []


def test_all_ok_with_a_good_debate_calls_the_client():
    client = FakeClient([reply(decision())])
    packet = _packet()
    r = run_trader(packet, _debate(packet), client=client)
    assert len(client.calls) == 1 and r.ok and r.forced_pass is None


def test_debate_mismatch_beats_analyst_failed():
    client = FakeClient([])
    packet = _failed()
    r = run_trader(packet, _debate(packet, symbol="MSFT"), client=client)
    assert r.forced_pass == "debate_mismatch" and client.calls == []


def test_analyst_failed_beats_fatal_bear():
    client = FakeClient([])
    packet = _failed()
    deb = _debate(packet, fatal=True, final_bear=unavailable_bear("AAPL", "api_error"))
    r = run_trader(packet, deb, client=client)
    assert r.forced_pass == "analyst_failed" and client.calls == []


def test_analyst_failed_beats_no_plans():
    client = FakeClient([])
    packet = _failed(plans=[])
    r = run_trader(packet, _debate(packet), client=client)
    assert r.forced_pass == "analyst_failed" and client.calls == []


def test_fatal_bear_and_no_plans_still_apply_when_all_ok():
    packet = _packet()
    r = run_trader(packet, _debate(packet, fatal=True), client=FakeClient([]))
    assert r.forced_pass == "fatal_bear"
    packet = _packet(plans=[])
    r = run_trader(packet, _debate(packet), client=FakeClient([]))
    assert r.forced_pass == "no_plans"


def test_trader_prompt_says_code_forces_pass_when_all_ok_is_false():
    assert "`all_ok` is false (an analyst failed; code also forces this PASS" in TRADER_PROMPT


# ===================== #113: plan context =====================

CONTEXT_FIELDS = ("target_vs_session_high", "risk_atr_5m", "entry_vs_vwap_atr_5m",
                  "session_high_vs_entry_atr_5m", "atr_5m")


def test_record_context_equals_the_tool_result_fields():
    s = session("clean-buy")
    res, err = clean_vwap(s, reward_multiple="2")
    assert err is False
    ctx = s.exit_plans[0].context
    assert isinstance(ctx, PlanContext)
    for f in CONTEXT_FIELDS:
        assert getattr(ctx, f) == res[f], f
    assert (ctx.target_vs_session_high, ctx.risk_atr_5m, ctx.entry_vs_vwap_atr_5m,
            ctx.atr_5m) == ("at_or_above", "1.82", "1.71", "0.1868")


def test_orb_plan_context_differs_from_vwap_plan_context():
    s = session("clean-buy")
    clean_vwap(s)
    lv = price_levels(s.summary)
    s.execute("plan_exit", {"setup": "orb", "entry": lv["last_close"]})
    vwap_plan, orb_plan = s.exit_plans
    assert vwap_plan.context.risk_atr_5m == "1.82" and orb_plan.context.risk_atr_5m == "4.55"


def test_two_multiples_of_one_setup_share_entry_and_stop_but_context_differs():
    s = session("clean-buy")
    s.summary["price_levels"]["session_high"] = "81.20"  # between the 1x and 2x targets
    r1, _ = clean_vwap(s, reward_multiple="1")
    r2, _ = clean_vwap(s, reward_multiple="2")
    p1, p2 = s.exit_plans
    assert (p1.entry, p1.stop) == (p2.entry, p2.stop)
    assert (r1["target"], r2["target"]) == ("81.06", "81.40")
    assert p1.context.target_vs_session_high == "below"
    assert p2.context.target_vs_session_high == "at_or_above"
    assert p1.context != p2.context


def _data(payload: str) -> dict:
    return json.loads(payload.split("<packet_data>")[1].split("</packet_data>")[0])


def test_context_is_in_the_debate_and_trader_payloads():
    s = session("clean-buy")
    clean_vwap(s)
    packet = make_packet(plans=list(s.exit_plans))
    expected = s.exit_plans[0].context.model_dump()
    assert _data(build_payload(packet, None, 1))["exit_plans"][0]["context"] == expected
    deb = DebateRecord(symbol="AAPL", as_of=packet.as_of, rounds_planned=1, rounds_run=1,
                       turns=[], stopped_early=False, fatal=False, requested_same_model=True,
                       same_model=True, final_bull=None, final_bear=None, all_ok=True)
    assert _data(build_trader_payload(packet, deb))["exit_plans"][0]["context"] == expected


def test_money_guard_allows_a_quoted_context_ratio_only_when_it_is_in_the_packet():
    s = session("clean-buy")
    clean_vwap(s)
    with_ctx = make_packet(plans=list(s.exit_plans))
    without = make_packet(plans=[s.exit_plans[0].model_copy(update={"context": None})])
    text = "The plan risks risk_atr_5m 1.82 and entry_vs_vwap_atr_5m 1.71 of ATR."
    assert {"1.82", "1.71"} <= context_ratios(with_ctx)
    assert "0.1868" not in context_ratios(with_ctx)       # the ATR is a dollar value
    assert "1.82" not in quoted_values(with_ctx)           # ratios are NOT general quotes
    assert money_violations(text, allowed_values(with_ctx), quoted_values(with_ctx),
                            context_ratios(with_ctx)) == []
    assert money_violations(text, allowed_values(without), quoted_values(without),
                            context_ratios(without)) != []


@pytest.mark.parametrize("text", [
    "stop 1.82", "target 1.82", "$1.82", "buy 1.82", "buy 1.82 shares", "1.82 shares",
    "1.82 per share", "entry 1.71", "price 1.71",
])
def test_a_context_ratio_never_passes_as_a_price_stop_target_or_size(text):
    s = session("clean-buy")
    clean_vwap(s)
    packet = make_packet(plans=list(s.exit_plans))
    assert money_violations(text, allowed_values(packet), quoted_values(packet),
                            context_ratios(packet)) != [], text


def test_a_negative_context_ratio_quoted_as_written_passes():
    plan = make_plan().model_copy(update={"context": PlanContext(
        target_vs_session_high="below", risk_atr_5m="1.20",
        entry_vs_vwap_atr_5m="-0.35", session_high_vs_entry_atr_5m="2.10", atr_5m="0.2000")})
    packet = make_packet(plans=[plan])
    text = "entry_vs_vwap_atr_5m -0.35: the entry is under VWAP."
    assert money_violations(text, allowed_values(packet), quoted_values(packet),
                            context_ratios(packet)) == []
    assert money_violations("entry_vs_vwap_atr_5m -0.36", allowed_values(packet),
                            quoted_values(packet), context_ratios(packet)) != []


def test_the_atr_itself_is_not_quotable_in_a_money_pattern():
    s = session("clean-buy")
    clean_vwap(s)
    packet = make_packet(plans=list(s.exit_plans))
    assert money_violations("stop 0.1868", allowed_values(packet), quoted_values(packet),
                            context_ratios(packet)) != []


def test_money_guard_still_rejects_an_unquoted_ratio_with_context_present():
    s = session("clean-buy")
    clean_vwap(s)
    packet = make_packet(plans=list(s.exit_plans))
    assert money_violations("risk_atr_5m 1.83", allowed_values(packet),
                            quoted_values(packet), context_ratios(packet)) != []


@pytest.mark.parametrize("name", ["bull", "bear", "trader"])
def test_debate_and_trader_prompts_never_call_a_plan_tight(name):
    import re
    text = (AGENTS_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    # "too tight" only appears inside the definition that rules that reading out
    leftovers = [m.start() for m in re.finditer(r"\btight\b", text)
                 if not text[:m.start()].endswith("too ")]
    assert leftovers == [], name


def test_bull_and_bear_may_repeat_context_ratios_but_never_as_money():
    for name in ("bull", "bear"):
        text = (AGENTS_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8")
        assert "a plan's `context` ratio" in text
        assert "never as a price, stop, target or size" in text


def test_trader_reads_the_higher_multiple_plans_context():
    assert "read the HIGHER-multiple plan's `context`" in TRADER_PROMPT


def test_record_without_context_validates_and_is_skipped_by_quoted_values():
    plan = make_plan()
    assert plan.context is None and isinstance(plan, ExitPlanRecord)
    packet = make_packet(plans=[plan])
    assert "1.82" not in quoted_values(packet) and "0.1868" not in quoted_values(packet)
    assert _data(build_payload(packet, None, 1))["exit_plans"][0]["context"] is None


def test_plan_context_is_frozen_and_forbids_extras():
    none = dict(risk_atr_5m=None, entry_vs_vwap_atr_5m=None,
                session_high_vs_entry_atr_5m=None, atr_5m=None)
    ctx = PlanContext(target_vs_session_high="unknown", **none)
    with pytest.raises(ValueError):
        ctx.atr_5m = "1"
    with pytest.raises(ValueError):
        PlanContext(target_vs_session_high="sideways", **none)
    with pytest.raises(ValueError):
        PlanContext(target_vs_session_high="below", bonus=1, **none)


# ===================== #122: entry error text =====================

NEW_ERR = ("entry must be the last_close value from get_bars price_levels "
           "(orders are market orders)")


def test_a_price_that_is_not_a_level_gets_exactly_the_new_error():
    s = session("clean-buy")
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "102.50"})
    assert err is True and res == {"error": NEW_ERR}
    assert s.exit_plans == []


def test_another_level_keeps_the_existing_non_error_refusal():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": lv["session_low"]})
    assert err is False and res["ok"] is False
    assert res["reason"].startswith("entry must be last_close")
    assert s.exit_plans == []


def test_last_close_entry_is_accepted():
    s = session("clean-buy")
    res, err = clean_vwap(s)
    assert err is False and res["ok"] is True
