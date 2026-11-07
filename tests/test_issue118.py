"""Issue #118 (market orders: the entry is always last_close) and the plan labels that cap the
reward multiple. plan_exit, the ledger, the debate and trader payloads, and build_order.
Offline: scenario fixtures and fakes only."""
from __future__ import annotations

import json
import re
from decimal import Decimal as D

import pytest

from agents.debate_common import build_payload
from agents.schemas import DebateRecord, ExitPlanRecord, PlanLabels
from agents.tool_registry import price_levels
from agents.trader import build_trader_payload
from pipeline.ledger import PlanLedger
from pipeline.orders import build_order
from tests.debate_helpers import make_packet
from tests.order_helpers import AS_OF, T0, plan
from tests.test_atr_5m import _early_session
from tests.test_scenario_review import LIMITS, session


def level_inp(s, level="vwap", **extra):
    lv = price_levels(s.summary)
    return {"setup": "level", "entry": lv["last_close"], "level": lv[level], **extra}


# ---------------- reward multiple vs the suggestion (clean-buy: vwap plan suggests 2) ----------

def test_omitted_multiple_defaults_to_the_suggestion():
    s = session("clean-buy")
    res, err = s.execute("plan_exit", level_inp(s))
    assert err is False and res["ok"] is True
    assert res["labels"]["suggested_multiple"] == "2"
    assert res["reward_to_risk"] == "2.00" and res["target"] == "81.40"


def test_multiple_at_the_suggestion_is_allowed():
    s = session("clean-buy")
    res, err = s.execute("plan_exit", level_inp(s, reward_multiple="2"))
    assert err is False and res["ok"] is True and res["reward_to_risk"] == "2.00"


@pytest.mark.parametrize("m", ["1", "1.5"])
def test_multiple_below_the_suggestion_is_allowed(m):
    s = session("clean-buy")
    res, err = s.execute("plan_exit", level_inp(s, reward_multiple=m))
    assert err is False and res["ok"] is True
    assert res["reward_to_risk"] == f"{D(m):.2f}"
    assert res["labels"]["suggested_multiple"] == "2"  # the label is not changed by the choice


@pytest.mark.parametrize("m", ["2.5", "3"])
def test_multiple_above_the_suggestion_is_refused_with_labels_and_nothing_recorded(m):
    led = PlanLedger("BRKO")
    s = session("clean-buy", led)
    res, err = s.execute("plan_exit", level_inp(s, reward_multiple=m))
    assert err is False  # information for the model, not a tool error
    assert res["ok"] is False
    assert res["reason"] == (f"reward_multiple {m} is above this plan's suggested_multiple 2; "
                             "choose it or lower")
    assert res["labels"] == {"extended": False, "tight_stop": True, "structural_stop": True,
                             "suggested_multiple": "2", "atr_5m_provisional": False}
    assert "plan_id" not in res
    assert s.exit_plans == []
    led.seal(accept=True)
    assert led.plan_ids() == []


def test_a_refused_multiple_does_not_consume_a_plan_id():
    s = session("clean-buy")
    s.execute("plan_exit", level_inp(s, reward_multiple="3"))
    res, _ = s.execute("plan_exit", level_inp(s))
    assert res["plan_id"] == "plan-1"


def test_orb_plan_with_a_loose_stop_is_capped_at_the_minimum():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": lv["last_close"],
                                       "reward_multiple": "2"})
    assert err is False and res["ok"] is False
    assert res["labels"]["tight_stop"] is False and res["labels"]["suggested_multiple"] == "1"
    ok, _ = s.execute("plan_exit", {"setup": "orb", "entry": lv["last_close"]})
    assert ok["ok"] is True and ok["reward_to_risk"] == "1.00"


def test_menu_validation_still_comes_first():
    s = session("clean-buy")
    res, err = s.execute("plan_exit", level_inp(s, reward_multiple="4"))
    assert err is True and "reward_multiple must be one of" in res["error"]


# ---------------- entry is always last_close ----------------

@pytest.mark.parametrize("name", ["session_low", "session_high", "or_low", "or_high", "vwap"])
@pytest.mark.parametrize("setup", ["orb", "level"])
def test_any_other_price_level_as_entry_is_refused_not_an_error_and_not_recorded(name, setup):
    led = PlanLedger("BRKO")
    s = session("clean-buy", led)
    lv = price_levels(s.summary)
    inp = {"setup": setup, "entry": lv[name]}
    if setup == "level":
        inp["level"] = lv["session_low"]
    res, err = s.execute("plan_exit", inp)
    assert err is False
    assert res["ok"] is False and res["reason"].startswith("entry must be last_close")
    assert "market orders" in res["reason"]
    assert s.exit_plans == []
    led.seal(accept=True)
    assert led.plan_ids() == []


def test_a_price_that_is_not_a_level_is_still_a_tool_error():
    s = session("clean-buy")
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "80.50"})
    assert err is True and "entry must be one of get_bars price_levels" in res["error"]


def test_record_carries_last_close_labels_and_the_last_close_entry_level():
    s = session("clean-buy")
    res, _ = s.execute("plan_exit", level_inp(s))
    (rec,) = s.exit_plans
    lv = price_levels(s.summary)
    assert rec.entry_level == "last_close" and rec.last_close == lv["last_close"] == rec.entry
    assert rec.labels is not None and rec.labels.model_dump() == res["labels"]
    assert rec.labels.suggested_multiple == "2"


def test_entry_level_is_last_close_even_when_it_equals_the_session_high():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    s.summary["price_levels"] = {**lv, "session_high": lv["last_close"]}
    res, _ = s.execute("plan_exit", level_inp(s))
    assert res["ok"] is True
    assert s.exit_plans[0].entry_level == "last_close"


def test_result_labels_are_the_labels_on_the_ledger_record():
    led = PlanLedger("BRKO")
    s = session("clean-buy", led)
    res, _ = s.execute("plan_exit", level_inp(s))
    led.seal(accept=True)
    assert led.get(res["plan_id"]).labels.model_dump() == res["labels"]


# ---------------- unknown labels: early session ----------------

def test_early_session_plan_has_non_null_labels_and_a_provisional_atr():
    summ, s, lv = _early_session("09:50")  # 20 bars: 4 finished 5-minute bars
    assert summ["atr_5m_provisional"] is True
    res, err = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                       "level": lv["or_low"]})
    assert err is False and res["ok"] is True
    assert res["labels"]["atr_5m_provisional"] is True
    assert res["labels"]["extended"] is not None and res["labels"]["tight_stop"] is not None
    assert s.exit_plans[0].labels.atr_5m_provisional is True


def test_missing_5m_atr_gives_unknown_labels_and_the_minimum_ceiling():
    s = session("clean-buy")
    s.summary = {**s.summary, "atr_5m": None, "atr_5m_provisional": False}
    res, err = s.execute("plan_exit", level_inp(s, reward_multiple="2"))
    assert err is False and res["ok"] is False
    assert res["labels"]["extended"] is None and res["labels"]["tight_stop"] is None
    assert res["labels"]["suggested_multiple"] == "1"
    ok, _ = s.execute("plan_exit", level_inp(s))
    assert ok["ok"] is True and ok["reward_to_risk"] == "1.00"


# ---------------- labels reach the debate and trader payloads ----------------

def _packet_data(text: str) -> dict:
    m = re.search(r"<packet_data>\n(.*)\n</packet_data>", text, re.S)
    assert m
    return json.loads(m.group(1))


def _packet_with_session_plans():
    s = session("clean-buy")
    s.execute("plan_exit", level_inp(s))
    s.execute("plan_exit", {"setup": "orb", "entry": price_levels(s.summary)["last_close"]})
    return make_packet(plans=list(s.exit_plans), symbol="BRKO"), s


def test_labels_are_in_the_debate_payload():
    packet, s = _packet_with_session_plans()
    data = _packet_data(build_payload(packet, None, 1))
    assert [p["labels"] for p in data["exit_plans"]] == [
        p.labels.model_dump() for p in s.exit_plans]
    assert data["exit_plans"][0]["labels"]["suggested_multiple"] == "2"
    assert data["exit_plans"][1]["labels"]["suggested_multiple"] == "1"
    assert data["exit_plans"][0]["last_close"] == "80.72"


def test_labels_are_in_the_trader_payload():
    packet, s = _packet_with_session_plans()
    debate = DebateRecord(
        symbol="BRKO", as_of=packet.as_of, rounds_planned=1, rounds_run=1, turns=[],
        stopped_early=False, fatal=False, requested_same_model=True, same_model=True,
        final_bull=None, final_bear=None, all_ok=True)
    data = _packet_data(build_trader_payload(packet, debate))
    assert [p["labels"] for p in data["exit_plans"]] == [
        p.labels.model_dump() for p in s.exit_plans]
    assert data["exit_plans"][0]["labels"]["extended"] is False


# ---------------- schema ----------------

def test_plan_without_last_close_or_labels_still_validates_as_none():
    rec = plan()
    bare = ExitPlanRecord.model_validate(
        {k: v for k, v in rec.model_dump().items() if k not in ("last_close", "labels")})
    assert bare.last_close is None and bare.labels is None


def test_plan_labels_model_forbids_extra_fields_and_is_frozen():
    ok = dict(extended=False, tight_stop=True, structural_stop=True, suggested_multiple="2",
              atr_5m_provisional=False)
    labels = PlanLabels(**ok)
    with pytest.raises(ValueError):
        PlanLabels(**ok, bonus=1)
    with pytest.raises(ValueError):
        labels.extended = True


# ---------------- build_order: a market order needs entry == last_close ----------------

def test_build_order_passes_when_the_entry_is_the_last_close():
    out = build_order(plan(), LIMITS, T0)
    assert out["ok"] is True and out["order"]["entry"] == "364.18"


MARKET = "market order: plan entry must be its last_close"


def test_build_order_blocks_when_last_close_is_missing():
    assert build_order(plan(last_close=None), LIMITS, T0) == {"ok": False, "reason": MARKET}


def test_build_order_blocks_when_last_close_differs_from_the_entry():
    assert build_order(plan(last_close="364.19"), LIMITS, T0) == {"ok": False, "reason": MARKET}
    assert build_order(plan(last_close="364.17"), LIMITS, T0) == {"ok": False, "reason": MARKET}


@pytest.mark.parametrize("level", ["or_high", "session_high", "vwap", "or_low"])
def test_build_order_blocks_when_the_entry_level_is_not_last_close(level):
    assert build_order(plan(entry_level=level), LIMITS, T0) == {"ok": False, "reason": MARKET}


@pytest.mark.parametrize("bad", ["", "abc", "364.185", "NaN", "Infinity", "0.00"])
def test_build_order_blocks_a_malformed_last_close(bad):
    assert build_order(plan(last_close=bad), LIMITS, T0) == {"ok": False, "reason": MARKET}


def test_build_order_blocks_even_a_fresh_plan_with_a_wrong_last_close():
    assert AS_OF < T0  # fresh: the refusal is the last_close rule, not staleness
    out = build_order(plan(last_close="365.00"), LIMITS, T0)
    assert out["reason"] == MARKET


def test_last_close_check_does_not_hide_a_price_error():
    out = build_order(plan(last_close="999.99", stop="364.18", risk_per_share="0.00"), LIMITS, T0)
    assert out["ok"] is False and out["reason"] == "plan must have stop < entry < target"
