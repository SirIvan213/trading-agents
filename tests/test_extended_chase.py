"""The extended-chase scenario, the label-aware prompts and the money guard's date rule.
Offline: saved scenario fixtures and prompt text only."""
from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from agents.money_guard import money_violations, quoted_values
from agents.schemas import AgentRunRecord, AnalystReport, DecisionPacket, Evidence
from agents.tool_registry import price_levels
from pipeline.orders import build_order
from pipeline.scenarios import ScenarioData, load_scenario, scenario_names
from tests.debate_helpers import AS_OF, make_plan
from tests.test_scenario_review import LIMITS, session
from tests.test_scenarios import Loaded

PROMPTS = Path(__file__).resolve().parents[1] / "agents" / "prompts"


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


# ---------------- scenario list ----------------

def test_there_are_four_scenarios():
    assert scenario_names() == ["clean-buy", "extended-chase", "red-flag", "tsla"]


# ---------------- extended-chase ----------------

def test_extended_chase_loads_and_expects_pass():
    sc = load_scenario("extended-chase")
    assert sc.expected.action == "PASS"
    data = ScenarioData(sc)
    assert data.symbol == "CHSW"
    assert data.as_of().isoformat() == "2026-11-03T11:00:00-05:00"


def test_extended_chase_context_numbers():
    s = session("extended-chase")
    lv = price_levels(s.summary)
    assert lv["last_close"] == "81.40" and lv["vwap"] == "80.47"
    assert s.summary["atr_5m"] == "0.2294"
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                     "level": lv["vwap"]})
    assert res["entry_vs_vwap_atr_5m"] == "4.05"


@pytest.mark.parametrize("inp_extra", [
    {"setup": "orb"}, {"setup": "level", "level": "vwap"}, {"setup": "level", "level": "or_high"},
    {"setup": "level", "level": "session_low"},
])
def test_every_extended_chase_plan_at_last_close_is_extended_with_the_minimum(inp_extra):
    s = session("extended-chase")
    lv = price_levels(s.summary)
    inp = {"entry": lv["last_close"], **inp_extra}
    if "level" in inp:
        inp["level"] = lv[inp["level"]]
    res, err = s.execute("plan_exit", inp)
    assert err is False and res["ok"] is True, res
    assert res["labels"]["extended"] is True
    assert res["labels"]["suggested_multiple"] == "1"
    assert res["reward_to_risk"] == "1.00"
    # a higher multiple is refused: the labels cap it
    over, _ = s.execute("plan_exit", {**inp, "reward_multiple": "2"})
    assert over["ok"] is False and over["labels"]["extended"] is True
    assert s.exit_plans[-1].labels.extended is True


def test_extended_chase_plan_is_still_a_sizable_market_order_so_only_judgement_says_pass():
    """The tools do not forbid the chase; the Trader's PASS rule on labels.extended does."""
    s = session("extended-chase")
    lv = price_levels(s.summary)
    s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"], "level": lv["vwap"]})
    assert build_order(s.exit_plans[0], LIMITS, s.now)["ok"] is True


def test_clean_buy_vwap_plan_is_not_extended_and_suggests_two():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                     "level": lv["vwap"]})
    assert res["labels"] == {"extended": False, "tight_stop": True, "structural_stop": True,
                             "suggested_multiple": "2", "atr_5m_provisional": False}
    assert res["target"] == "81.40" and res["reward_to_risk"] == "2.00"


def test_clean_buy_orb_plan_is_not_tight_so_suggests_one():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    res, _ = s.execute("plan_exit", {"setup": "orb", "entry": lv["last_close"]})
    assert res["labels"]["extended"] is False and res["labels"]["tight_stop"] is False
    assert res["labels"]["suggested_multiple"] == "1" and res["risk_atr_5m"] == "4.55"


def test_red_flag_vwap_plan_suggests_two():
    s = session("red-flag")
    lv = price_levels(s.summary)
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                     "level": lv["vwap"]})
    assert res["labels"]["suggested_multiple"] == "2" and res["labels"]["extended"] is False


def test_extended_chase_shares_the_clean_buy_opening_range_and_is_extended_late():
    chase, clean = Loaded("extended-chase"), Loaded("clean-buy")
    assert chase.levels["or_low"] == clean.levels["or_low"]
    assert chase.levels["or_high"] == clean.levels["or_high"]
    assert D(chase.levels["last_close"]) > D(chase.levels["vwap"])
    assert D(chase.levels["last_close"]) - D(chase.levels["vwap"]) > D("2") * chase.atr_5m


# ---------------- money guard: dates ----------------

ALLOWED = {D("101.50"), D("100.25")}


@pytest.mark.parametrize("text", [
    "8-K filed 2026-11-02, shares rose",
    "Filed 2026-11-02 shares",
    "the 8-K dated 2026-11-02 was filed",
    "reported on 2026-11-02 and again on 2025-12-31",
])
def test_numbers_inside_a_date_are_not_money_mentions(text):
    assert money_violations(text, ALLOWED) == [], text


@pytest.mark.parametrize("text,mention", [
    ("buy 300 shares on 2026-11-02", "300 shares"),
    ("$2026-11-02", "$2026"),
])
def test_dates_do_not_hide_real_violations(text, mention):
    bad = money_violations(text, ALLOWED)
    assert any(mention in b for b in bad), (text, bad)


def test_a_date_does_not_excuse_a_nearby_invented_price():
    assert money_violations("stop 99.10 as filed 2026-11-02", ALLOWED)
    assert money_violations("2026-11-02 target 150", ALLOWED)


def test_quoted_values_unchanged_dates_clocks_and_item_numbers_are_skipped():
    def run(ok):
        return AgentRunRecord(agent="technical", requested_model="m", effort="low", ok=ok,
                              duration_ms=1)
    rep = AnalystReport(
        agent="technical", symbol="AAPL", stance="bullish", confidence=0.6,
        evidence=[Evidence(point="item 9.01 filed 2026-11-02, bar at 10:15, volume 3,225",
                           source="get_bars")])
    pkt = DecisionPacket(symbol="AAPL", as_of=AS_OF, reports={"technical": rep},
                         runs={"technical": run(True)}, all_ok=True, exit_plans=[make_plan()])
    assert quoted_values(pkt) == frozenset({"3,225", "0.6"})
    failed = pkt.model_copy(update={"runs": {"technical": run(False)}})
    assert quoted_values(failed) == frozenset()


# ---------------- prompts ----------------

def test_technical_prompt_uses_labels_and_last_close_and_does_not_judge_extension():
    t = prompt("technical")
    for word in ("labels", "suggested_multiple", "last_close", "extended", "tight_stop",
                 "structural_stop", "atr_5m_provisional"):
        assert word in t, word
    assert "do NOT judge extension or stop tightness yourself" in t
    assert "any other entry is refused" in t
    assert "default and the highest multiple allowed" in t
    # the old self-judged extension guidance is gone
    assert "do not just default to the minimum" not in t.lower()
    assert "stay at the minimum multiple" not in t
    assert "`entry_vs_vwap_atr_5m` small" not in t


def test_bull_prompt_rebuts_each_objection_and_concedes_only_what_the_packet_proves():
    b = prompt("bull")
    assert "rebut EACH of its objections" in b
    assert "Concede only what the packet proves" in b


def test_bear_prompt_uses_labels_and_does_not_count_the_multiple_as_an_objection():
    b = prompt("bear")
    assert "labels" in b and "suggested_multiple" in b
    assert "momentum only" in b.lower()


def test_trader_prompt_decides_on_labels():
    t = prompt("trader")
    assert "momentum only" in t and "NOT by itself a reason to PASS" in t
    assert "minimum-multiple plan" in t
    assert "every plan has `labels.extended` true or null" in t
    assert "PASS when" in t
