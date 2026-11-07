"""Review fixes on the scenario branch: a real breakout in clean-buy, Q3 not yet reported in
red-flag, ATR context from plan_exit, the not-evidence-of-edge caveat, malformed `expected`,
and a failed smoke run never reading as a plain MATCH. Offline: fixtures and fakes only."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

import pipeline.scenarios as sc
from agents.tool_registry import ToolSession, price_levels
from pipeline.ledger import PlanLedger
from pipeline.orders import build_order, select_plan
from pipeline.scenarios import ScenarioData, load_scenario
from tests.test_scenarios import tmp_scen  # noqa: F401  (fixture)
from tests.test_smoke_scenarios import debaters, reply, smoke, tail, trader  # noqa: F401
from tools.bars import parse_bars, regular_session, summarize
from tools.config import CONFIG_DIR, load_limits
from tools.earnings import earnings_facts
from tools.exit_plan import exit_plan
from tools.risk_check import DayState, check_order
from tools.scan import ScanCriteria

NY = ZoneInfo("America/New_York")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")


def summary(name: str) -> tuple[dict, dict, ScenarioData]:
    data = ScenarioData(load_scenario(name))
    bars = regular_session(parse_bars(data._bars))
    s = summarize(bars)
    return s, price_levels(s), data


def session(name: str, ledger: PlanLedger | None = None) -> ToolSession:
    data = ScenarioData(load_scenario(name))
    s = ToolSession(symbol=data.symbol, now=data.as_of(), data=data, limits=LIMITS,
                    criteria=ScanCriteria(), ledger=ledger)
    _, err = s.execute("get_bars", {"symbol": data.symbol})
    assert err is False
    return s


# ---------------- clean-buy is a breakout ----------------

@pytest.mark.parametrize("name", ["clean-buy", "red-flag"])
def test_the_close_is_above_the_opening_range_and_within_a_dime_of_the_high(name):
    s, lv, _ = summary(name)
    assert s["opening_range"]["complete"] is True
    assert D(lv["or_low"]) < D(lv["or_high"]) < D(lv["last_close"]) <= D(lv["session_high"])
    assert D(lv["session_high"]) - D(lv["last_close"]) <= D("0.10")


def test_clean_buy_breakout_plan_at_or_high_is_valid_at_2r_with_room_at_the_highs():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    res, err = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                       "level": lv["or_high"], "reward_multiple": "2"})
    assert err is False and res["ok"] is True
    # trending at its highs: the target is above the session high, which is not resistance
    assert res["target_vs_session_high"] == "at_or_above"
    assert D(res["session_high_vs_entry_atr_5m"]) < 1
    assert D(res["stop"]) < D(lv["or_high"])  # stop under the broken level


def test_clean_buy_3r_plan_also_passes_the_risk_check():
    led = PlanLedger("BRKO")
    s = session("clean-buy", led)
    lv = price_levels(s.summary)
    res, err = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                       "level": lv["vwap"], "reward_multiple": "3"})
    assert err is False and res["ok"] is True and res["reward_to_risk"] == "3.00"
    led.seal(accept=True)
    sized = build_order(select_plan(led, res["plan_id"]), LIMITS, s.now)
    assert sized["ok"] is True
    assert check_order(sized["order"], LIMITS, DayState(), s.now)["verdict"] == "PASS"


# ---------------- ATR context from plan_exit ----------------

def test_plan_exit_reports_atr_5m_multiples_from_the_tool():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    atr5 = D(s.summary["atr_5m"])
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                     "level": lv["vwap"]})

    def per(x):
        return str((x / atr5).quantize(D("0.01"), "ROUND_FLOOR"))
    assert res["risk_atr_5m"] == per(D(res["risk_per_share"]))
    assert res["entry_vs_vwap_atr_5m"] == per(D(res["entry"]) - D(lv["vwap"]))
    assert res["session_high_vs_entry_atr_5m"] == per(D(lv["session_high"]) - D(res["entry"]))
    assert "risk_atr" not in res and "entry_vs_vwap_atr" not in res  # old 1-minute fields gone
    assert D("1") <= D(res["risk_atr_5m"]) <= D("2")      # tight structural stop
    assert D("0") < D(res["entry_vs_vwap_atr_5m"]) <= D("2")  # above VWAP, not extended


def test_atr_context_is_none_without_atr_5m():
    s = session("clean-buy")
    s.summary = {**s.summary, "atr_5m": None}
    lv = price_levels(s.summary)
    res, err = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                       "level": lv["vwap"]})
    assert err is False and res["ok"] is True
    assert res["risk_atr_5m"] is None and res["entry_vs_vwap_atr_5m"] is None
    assert res["session_high_vs_entry_atr_5m"] is None


# ---------------- red-flag: Q3 not yet reported ----------------

def test_red_flag_latest_reported_quarter_precedes_the_announced_q3():
    data = ScenarioData(load_scenario("red-flag"))
    ef = earnings_facts(data._facts, as_of=date(2026, 11, 3))
    assert ef["ok"] is True
    assert max(q["end"] for q in ef["quarters"]) == "2026-06-30"
    assert ef["latest_filed"] < "2026-10-01"
    subs = data._subs["filings"]["recent"]
    assert all(f != "10-Q" or d < "2026-10-01" for f, d in zip(subs["form"], subs["filingDate"],
                                                                strict=True))
    assert any("third-quarter earnings" in n["headline"] for n in data._news["news"])


def test_clean_buy_10q_date_matches_its_facts():
    data = ScenarioData(load_scenario("clean-buy"))
    subs = data._subs["filings"]["recent"]
    q_dates = {d for f, d in zip(subs["form"], subs["filingDate"], strict=True) if f == "10-Q"}
    assert q_dates == {earnings_facts(data._facts, as_of=date(2026, 11, 3))["latest_filed"]}


# ---------------- exit_plan floors a low menu value ----------------

def test_menu_value_below_min_reward_to_risk_is_raised_to_the_minimum():
    import dataclasses
    lim = dataclasses.replace(LIMITS, min_reward_to_risk=D("1.5"))
    r = exit_plan("level", D("80.42"), lim, level=D("80.16"), atr=D("0.11"),
                  reward_multiple=D("1"))
    assert r["ok"] is True and r["reward_to_risk"] == "1.50"


# ---------------- prompts ----------------

def test_prompts_say_reward_to_risk_is_not_evidence_of_edge():
    from agents.bear import PROMPT as BEAR
    from agents.bull import PROMPT as BULL
    from agents.technical import PROMPT as TECH
    from agents.trader import PROMPT as TRADER
    assert "not evidence of edge" in TECH
    assert "risk_atr" in TECH and "entry_vs_vwap_atr" in TECH
    for p in (BULL, BEAR, TRADER):
        assert "it is not evidence of edge" in p


# ---------------- malformed expected ----------------

@pytest.mark.parametrize("bad", ["BUY", [1], 5])
def test_non_object_expected_is_a_value_error(tmp_scen, bad):  # noqa: F811
    tmp_scen("x", expected=bad)
    with pytest.raises(ValueError, match="expected must be an object"):
        sc.load_scenario("x")


# ---------------- a failed run is labelled ----------------

def test_failed_trader_run_is_labelled_not_a_plain_match(smoke, capsys):  # noqa: F811
    sym = "VNTR"
    bad = reply("not json at all")
    code, out, _, _ = smoke("red-flag", [*debaters(sym), bad, bad], capsys)
    assert code == 1
    _, line = tail(out, "red-flag")
    assert "-> MATCH" in line and line.endswith(
        "(run failed: the decision is a fail-closed default, not a judgment)")


def test_successful_run_has_no_failure_label(smoke, capsys):  # noqa: F811
    sym = "BRKO"
    code, out, _, _ = smoke("clean-buy", [*debaters(sym), reply(trader(sym))], capsys)
    assert code == 0 and "run failed" not in tail(out, "clean-buy")[1]


def test_scenario_as_of_is_eleven_et():
    for name in ("clean-buy", "red-flag"):
        assert ScenarioData(load_scenario(name)).as_of() == datetime(
            2026, 11, 3, 11, 0, tzinfo=NY)
