"""Phase 5 gate C: the 5-minute ATR minimum stop distance (#115) and the 2028 NYSE calendar with
its coverage warning (#132). Offline: fixtures and fakes only, nothing billed."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, time
from decimal import Decimal as D

import pytest

from agents.tool_registry import price_levels
from tests.test_phase5_gate_b import LIMITS as GB_LIMITS
from tests.test_phase5_gate_b import at, check
from tests.test_scenario_review import LIMITS, session
from tools import market_calendar as cal
from tools.exit_plan import exit_plan

# ============================ #115 exit_plan ============================

ENTRY = D("100.00")


def plan(level="98.00", atr="0.10", atr_5m=None, provisional=False, limits=LIMITS):
    """A level plan: atr 0.10 gives buffer 0.01, so level 98.00 gives stop 97.99, risk 2.01."""
    return exit_plan("level", ENTRY, limits, level=D(level),
                     atr=None if atr is None else D(atr),
                     atr_5m=None if atr_5m is None else D(atr_5m),
                     atr_5m_provisional=provisional)


def test_baseline_plan_has_risk_2_01():
    r = plan()
    assert r["ok"] is True and r["stop"] == "97.99" and r["risk_per_share"] == "2.01"


def test_5m_rule_risk_exactly_at_multiple_times_atr_5m_passes():
    r = plan(atr_5m="2.01")
    assert r["ok"] is True and r["risk_per_share"] == "2.01"


def test_5m_rule_one_cent_below_is_refused_with_the_5_minute_message():
    r = plan(atr_5m="2.02")
    assert r == {"ok": False,
                 "reason": "risk per share 2.01 is below 1.0 x 5-minute ATR 2.02"}


def test_5m_rule_scales_with_the_multiple():
    two = replace(LIMITS, min_risk_atr_multiple=D("2"))
    assert plan(atr_5m="1.005", limits=two)["ok"] is True       # 2.01 == 2 x 1.005
    r = plan(atr_5m="1.01", limits=two)                         # 2 x 1.01 = 2.02 > 2.01
    assert r == {"ok": False, "reason": "risk per share 2.01 is below 2 x 5-minute ATR 1.01"}


def test_provisional_5m_atr_passes_at_the_boundary_and_has_its_own_message():
    assert plan(atr_5m="2.01", provisional=True)["ok"] is True
    r = plan(atr_5m="2.02", provisional=True)
    assert r == {"ok": False,
                 "reason": "risk per share 2.01 is below 1.0 x provisional 5-minute ATR 2.02"}


def test_1_minute_rule_binds_when_larger_and_keeps_its_message():
    # atr 3.00: buffer 0.30, stop 97.70, risk 2.30 < 3.00; the smaller 5-minute 2.00 is no help
    r = plan(atr="3.00", atr_5m="2.00")
    assert r == {"ok": False, "reason": "risk per share 2.30 is below 1.0 x ATR 3.00"}
    # alone it behaves the same
    assert plan(atr="3.00") == r


def test_1_minute_and_5_minute_both_pass_when_risk_covers_the_larger():
    r = plan(atr="2.00", atr_5m="2.10")  # buffer 0.20, stop 97.80, risk 2.20 >= 2.10
    assert r["ok"] is True and r["risk_per_share"] == "2.20"
    r = plan(atr="2.00", atr_5m="2.21")
    assert r["ok"] is False and "5-minute ATR 2.21" in r["reason"]


def test_only_the_1_minute_atr_given_applies_the_1_minute_rule_only():
    assert plan(atr_5m=None)["ok"] is True
    r = plan(atr="3.00", atr_5m=None)
    assert r["ok"] is False and r["reason"].endswith("1.0 x ATR 3.00")


def test_atr_5m_zero_adds_no_constraint():
    r = plan(atr_5m="0")
    assert r == plan(atr_5m=None) and r["ok"] is True


def test_min_risk_atr_multiple_zero_disables_both_atr_rules():
    off = replace(LIMITS, min_risk_atr_multiple=D("0"))
    r = plan(atr="3.00", atr_5m="9.00", limits=off)
    assert r["ok"] is True and r["risk_per_share"] == "2.30"


def test_5m_rule_applies_without_a_1_minute_atr():
    ok = exit_plan("level", ENTRY, LIMITS, level=D("98.00"), atr_5m=D("2.00"))
    assert ok["ok"] is True
    risk = D(ok["risk_per_share"])
    assert exit_plan("level", ENTRY, LIMITS, level=D("98.00"), atr_5m=risk)["ok"] is True
    refused = exit_plan("level", ENTRY, LIMITS, level=D("98.00"), atr_5m=risk + D("0.01"))
    assert refused == {"ok": False,
                       "reason": f"risk per share {risk} is below 1.0 x 5-minute ATR "
                                 f"{risk + D('0.01')}"}


def test_buffer_and_stop_are_identical_with_and_without_atr_5m():
    base = plan()
    for five in ("0.50", "2.01", "0"):
        with_five = plan(atr_5m=five)
        assert with_five == base
    # a big 5-minute ATR changes the verdict, never the stop: it refuses, it does not re-buffer
    big = plan(atr_5m="50")
    assert big["ok"] is False


@pytest.mark.parametrize("bad,reason", [
    (D("-0.01"), "atr_5m must not be negative"),
    ("abc", "atr_5m is not a number"),
    (D("NaN"), "atr_5m must be a finite number"),
    (D("Infinity"), "atr_5m must be a finite number"),
])
def test_bad_atr_5m_fails_closed(bad, reason):
    r = exit_plan("level", ENTRY, LIMITS, level=D("98.00"), atr=D("0.10"), atr_5m=bad)
    assert r == {"ok": False, "reason": reason}


# ============================ #115 through plan_exit ============================

def test_vwap_level_plan_passes_both_atrs_on_clean_buy():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    res, err = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                       "level": lv["vwap"], "reward_multiple": "2"})
    assert err is False and res["ok"] is True
    assert res["risk_per_share"] == "0.34" and res["atr_5m"] == "0.1868"
    assert D(res["risk_per_share"]) >= D(s.summary["atr_5m"]) >= D(s.summary["atr"])


def test_multiple_2_refuses_on_the_5_minute_atr_while_the_1_minute_would_pass():
    strict = replace(LIMITS, min_risk_atr_multiple=D("2.0"))
    s = session("clean-buy")
    s.limits = strict
    lv = price_levels(s.summary)
    # 2 x 0.1301 = 0.2602 < 0.34: the 1-minute rule alone passes
    assert D(s.summary["atr"]) * 2 < D("0.34")
    res, err = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                       "level": lv["vwap"]})
    assert err is False and res["ok"] is False
    assert res["reason"] == "risk per share 0.34 is below 2.0 x 5-minute ATR 0.1868"
    assert not s.exit_plans
    # 2 x 0.1868 = 0.3736 > 0.34 is what binds; with the multiple at 1.8 it passes (0.3362)
    s2 = session("clean-buy")
    s2.limits = replace(LIMITS, min_risk_atr_multiple=D("1.8"))
    res, _ = s2.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                      "level": lv["vwap"]})
    assert res["ok"] is True and res["risk_per_share"] == "0.34"


def test_plan_exit_passes_atr_5m_and_the_provisional_flag_to_exit_plan(monkeypatch):
    import agents.tool_registry as reg
    calls = []
    real = reg.exit_plan

    def spy(*a, **kw):
        calls.append(kw)
        return real(*a, **kw)
    monkeypatch.setattr(reg, "exit_plan", spy)
    s = session("clean-buy")
    lv = price_levels(s.summary)
    s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"], "level": lv["vwap"]})
    s.execute("plan_exit", {"setup": "orb", "entry": lv["last_close"]})
    assert len(calls) >= 2
    for kw in calls:
        assert D(str(kw["atr_5m"])) == D("0.1868") and kw["atr_5m_provisional"] is False
        assert D(str(kw["atr"])) == D("0.1301")


def early_session(n_bars: int):
    s = session("clean-buy")
    s.data._bars["bars"] = s.data._bars["bars"][:n_bars]
    _, err = s.execute("get_bars", {"symbol": s.data.symbol})
    assert err is False
    return s


def with_last_close(s, price: str) -> None:
    s.summary = {**s.summary, "price_levels": {**s.summary["price_levels"], "last_close": price}}


def test_early_session_provisional_atr_5m_binds_through_plan_exit():
    s = early_session(45)  # 09:30-10:14: nine finished 5-minute bars, so provisional
    assert s.summary["atr"] == "0.1363"
    assert s.summary["atr_5m"] == "0.2050" and s.summary["atr_5m_provisional"] is True
    # level 80.25 (vwap) gives stop floor(80.25 - 0.01363) = 80.23
    with_last_close(s, "80.44")  # risk 0.21 >= provisional 5-minute 0.2050: passes
    passing, _ = s.execute("plan_exit", {"setup": "level", "entry": "80.44", "level": "80.25"})
    assert passing["ok"] is True and passing["risk_per_share"] == "0.21"
    with_last_close(s, "80.43")  # risk 0.20: clears the 1-minute 0.1363, not the 5-minute 0.2050
    refused, err = s.execute("plan_exit", {"setup": "level", "entry": "80.43", "level": "80.25"})
    assert err is False
    assert refused == {"ok": False, "reason": ("risk per share 0.20 is below 1.0 x provisional "
                                               "5-minute ATR 0.2050")}
    assert D("0.20") >= D(s.summary["atr"])  # the 1-minute rule alone would have passed it
    assert len(s.exit_plans) == 1


def test_early_session_with_no_5m_atr_falls_back_to_the_1_minute_rule_only():
    s = early_session(45)
    s.summary = {**s.summary, "atr_5m": None, "atr_5m_provisional": False}
    with_last_close(s, "80.43")  # the same risk 0.20 now passes: only the 1-minute 0.1363 applies
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "80.43", "level": "80.25"})
    assert res["ok"] is True and res["risk_per_share"] == "0.20"
    with_last_close(s, "80.37")  # risk 0.14 >= 0.1363 passes; 0.13 would not
    assert s.execute("plan_exit", {"setup": "level", "entry": "80.37",
                                   "level": "80.25"})[0]["ok"] is True
    with_last_close(s, "80.36")
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "80.36", "level": "80.25"})
    assert res == {"ok": False, "reason": "risk per share 0.13 is below 1.0 x ATR 0.1363"}


# ============================ #132 calendar 2028 ============================

HOL_2028 = {
    date(2028, 1, 17): "Martin Luther King, Jr. Day",
    date(2028, 2, 21): "Washington's Birthday",
    date(2028, 4, 14): "Good Friday",
    date(2028, 5, 29): "Memorial Day",
    date(2028, 6, 19): "Juneteenth National Independence Day",
    date(2028, 7, 4): "Independence Day",
    date(2028, 9, 4): "Labor Day",
    date(2028, 11, 23): "Thanksgiving Day",
    date(2028, 12, 25): "Christmas Day",
}
EARLY_2028 = [date(2028, 7, 3), date(2028, 11, 24)]


@pytest.mark.parametrize("day,name", sorted(HOL_2028.items()), ids=str)
def test_2028_holiday_is_closed_and_blocks_orders(day, name):
    assert cal.covers(day) is True
    assert cal.session_hours(day) is None
    assert cal.closed_reason(day) == name
    assert check(at(day.year, day.month, day.day, 11, 0)) == {
        "verdict": "BLOCK", "reasons": [f"market is closed ({name})"]}


@pytest.mark.parametrize("day", EARLY_2028, ids=str)
def test_2028_early_close_ends_at_13(day):
    assert cal.session_hours(day) == (time(9, 30), time(13, 0))
    assert cal.day_times(day, GB_LIMITS) == (time(9, 45), time(12, 30), time(12, 55))


def test_2028_01_03_monday_is_a_normal_day_and_01_01_a_weekend():
    assert cal.session_hours(date(2028, 1, 3)) == (time(9, 30), time(16, 0))
    assert check(at(2028, 1, 3, 11, 0)) == {"verdict": "PASS", "reasons": []}
    assert date(2028, 1, 1) not in cal.FULL_HOLIDAYS
    assert cal.session_hours(date(2028, 1, 1)) is None
    assert cal.closed_reason(date(2028, 1, 1)) == "weekend"


def test_2028_12_24_is_a_sunday_and_12_22_a_normal_day():
    assert cal.session_hours(date(2028, 12, 24)) is None
    assert cal.closed_reason(date(2028, 12, 24)) == "weekend"
    assert date(2028, 12, 24) not in cal.EARLY_CLOSES
    assert cal.session_hours(date(2028, 12, 22)) == (time(9, 30), time(16, 0))
    assert check(at(2028, 12, 22, 11, 0))["verdict"] == "PASS"


def test_2028_07_03_risk_check_pass_then_block_after_early_entry_end():
    assert check(at(2028, 7, 3, 11, 0)) == {"verdict": "PASS", "reasons": []}
    assert check(at(2028, 7, 3, 12, 30))["verdict"] == "PASS"  # boundary is inclusive
    assert check(at(2028, 7, 3, 12, 31)) == {
        "verdict": "BLOCK", "reasons": ["outside entry window 09:45-12:30 ET (early close)"]}
    assert check(at(2028, 7, 3, 13, 30)) == {"verdict": "BLOCK", "reasons": ["market is closed"]}


def test_2028_07_04_blocked_as_independence_day():
    assert check(at(2028, 7, 4, 11, 0)) == {
        "verdict": "BLOCK", "reasons": ["market is closed (Independence Day)"]}


def test_2028_11_24_early_close_blocks_after_1230():
    assert check(at(2028, 11, 24, 11, 0))["verdict"] == "PASS"
    assert check(at(2028, 11, 24, 12, 31))["reasons"] == [
        "outside entry window 09:45-12:30 ET (early close)"]
    assert check(at(2028, 11, 24, 13, 0))["reasons"] == ["market is closed"]


def test_2029_is_uncovered_and_blocks():
    assert cal.covers(date(2029, 1, 2)) is False
    assert check(at(2029, 1, 2, 11, 0)) == {
        "verdict": "BLOCK", "reasons": ["market calendar does not cover this date"]}


# ============================ #132 coverage_warning ============================

@pytest.mark.parametrize("day", [date(2026, 11, 7), date(2026, 12, 15), date(2027, 12, 15),
                                 date(2028, 1, 3), date(2028, 11, 30)], ids=str)
def test_no_warning_while_the_next_year_is_covered_or_before_december(day):
    assert cal.coverage_warning(day) is None


@pytest.mark.parametrize("day", [date(2028, 12, 1), date(2028, 12, 31)], ids=str)
def test_warning_from_december_of_the_last_covered_year(day):
    w = cal.coverage_warning(day)
    assert w is not None
    assert "ends on December 31, 2028" in w and "2029" in w
    assert "market_calendar" in w


def test_warning_when_today_itself_is_uncovered():
    w = cal.coverage_warning(date(2029, 1, 2))
    assert w is not None and "does not cover 2029" in w and "blocked" in w
    assert cal.coverage_warning(date(2025, 6, 2)) is not None


def test_warning_boundary_is_december_first():
    assert cal.coverage_warning(date(2028, 11, 30)) is None
    assert cal.coverage_warning(date(2028, 12, 1)) is not None


def _report(capsys, as_of: str) -> str:
    from agents.debate import run_debate
    from agents.llm import AgentConfig
    from pipeline import smoke_debate as S
    from tests.debate_helpers import bear_json, bull_json, reply
    from tests.fakes import FakeClient
    from tests.test_issue87 import PKT, run_record

    deb = run_debate(PKT, clients=FakeClient([reply(bull_json()), reply(bear_json())]),
                     configs={"bull": AgentConfig(), "bear": AgentConfig()}, rounds=1)
    reports = {a: PKT.reports["technical"].model_copy(update={"agent": a})
               for a in ("technical", "sentiment", "fundamentals")}
    runs = {a: run_record(a) for a in reports}
    pkt = PKT.model_copy(update={"reports": reports, "runs": runs, "all_ok": True,
                                 "as_of": as_of})
    cfgs = {a: AgentConfig() for a in (*reports, "bull", "bear")}
    S.report(pkt, deb, cfgs)
    return capsys.readouterr().out


def test_smoke_debate_report_prints_the_warning_line_when_the_year_is_uncovered(capsys):
    out = _report(capsys, "2029-01-02T10:30:00-05:00")
    lines = out.splitlines()
    assert lines[0].startswith("== Analysts + debate smoke run:")
    assert lines[1].startswith("WARNING: market calendar does not cover 2029")


def test_smoke_debate_report_prints_the_december_warning(capsys):
    out = _report(capsys, "2028-12-05T10:30:00-05:00")
    assert any(ln.startswith("WARNING: market calendar ends on December 31, 2028")
               for ln in out.splitlines())


def test_smoke_debate_report_prints_no_warning_in_a_covered_year(capsys):
    assert "WARNING" not in _report(capsys, "2026-11-05T10:30:00-05:00")
