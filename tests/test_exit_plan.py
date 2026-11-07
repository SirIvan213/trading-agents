"""Tests for tools.exit_plan, including the bars -> exit_plan -> size -> risk chain."""
import json
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tools import bars as B
from tools.config import CONFIG_DIR, limits_from_dict, load_limits
from tools.exit_plan import exit_plan, to_plan
from tools.risk_check import DayState, check_order
from tools.size import Plan, size_position

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
FIX = Path(__file__).parent / "fixtures"
NY = ZoneInfo("America/New_York")
OR = {"high": "102.00", "low": "99.70", "complete": True, "missing_minutes": 0}


# ---------- happy paths ----------

def test_orb_happy_path_exact():
    r = exit_plan("orb", D("102.50"), LIMITS, opening_range=OR, atr=D("0.80"))
    assert r["ok"]
    # buffer 0.10*0.80 = 0.08 -> stop 99.62; risk 2.88; R=1.0 -> target 105.38
    assert r["stop"] == "99.62"
    assert r["target"] == "105.38"
    assert r["risk_per_share"] == "2.88"
    assert r["reward_to_risk"] == "1.00"
    assert r["entry"] == "102.50"
    assert r["setup"] == "orb"


def test_orb_without_atr_notes_and_no_buffer():
    r = exit_plan("orb", D("102.50"), LIMITS, opening_range=OR)
    assert r["ok"] and r["stop"] == "99.70"
    assert any("no atr" in n for n in r["notes"])


def test_level_happy_path_exact():
    r = exit_plan("level", D("52.00"), LIMITS, level=D("50.00"), reward_multiple=D("2"))
    assert r["ok"]
    assert r["stop"] == "50.00" and r["risk_per_share"] == "2.00"
    assert r["target"] == "56.00" and r["reward_to_risk"] == "2.00"
    assert r["setup"] == "level"


def test_level_accepts_numeric_types():
    r = exit_plan("level", 52, LIMITS, level=50.0, atr=0)
    assert r["ok"] and r["stop"] == "50.00"


# ---------- rounding ----------

def test_stop_rounds_down_to_cent():
    # 99.70 - 0.10*0.35 = 99.665 -> floor 99.66 (not 99.67)
    r = exit_plan("orb", D("101.00"), LIMITS, opening_range=OR, atr=D("0.35"))
    assert r["stop"] == "99.66"
    assert r["risk_per_share"] == "1.34"


def test_stop_rounds_down_even_for_tiny_fraction():
    # 99.70 - 0.0001 = 99.6999 -> 99.69
    r = exit_plan("level", D("101"), LIMITS, level=D("99.70"), atr=D("0.001"))
    assert r["stop"] == "99.69"


def test_target_rounds_up_to_cent():
    # entry 101.00, stop 99.66, risk 1.34, R 1.33 -> 101 + 1.7822 = 102.7822 -> 102.79
    r = exit_plan("orb", D("101.00"), LIMITS, opening_range=OR, atr=D("0.35"),
                  reward_multiple=D("1.33"))
    assert r["target"] == "102.79"
    assert r["reward_to_risk"] == "1.33"


def test_reward_to_risk_meets_minimum_after_rounding():
    limits = replace(LIMITS, min_reward_to_risk=D("1.5"))
    r = exit_plan("orb", D("100.33"), limits, opening_range=OR, atr=D("0.37"))
    # stop floor(99.663)=99.66, risk 0.67, target ceil(100.33+1.005)=101.34
    assert r["stop"] == "99.66" and r["target"] == "101.34"
    assert D(r["reward_to_risk"]) >= limits.min_reward_to_risk


@pytest.mark.parametrize("entry,atr", [("100.01", "0.33"), ("103.37", "0.77"), ("100.99", "0.05")])
def test_rr_never_below_limit(entry, atr):
    limits = replace(LIMITS, min_reward_to_risk=D("1.5"))
    r = exit_plan("orb", D(entry), limits, opening_range=OR, atr=D(atr))
    assert r["ok"]
    assert D(r["reward_to_risk"]) >= D("1.5")
    real = (D(r["target"]) - D(r["entry"])) / (D(r["entry"]) - D(r["stop"]))
    assert real >= D("1.5")
    assert D(r["stop"]) < D(r["entry"]) < D(r["target"])


# ---------- reward multiple vs limit ----------

def test_reward_multiple_below_limit_is_raised_with_note():
    limits = replace(LIMITS, min_reward_to_risk=D("1.5"))
    r = exit_plan("level", D("52.00"), limits, level=D("50.00"), reward_multiple=D("1.0"))
    assert r["ok"]
    assert r["target"] == "55.00" and r["reward_to_risk"] == "1.50"
    assert any("below limit" in n for n in r["notes"])


def test_reward_multiple_at_limit_has_no_raise_note():
    r = exit_plan("level", D("52.00"), LIMITS, level=D("50.00"), reward_multiple=D("1.0"))
    assert not any("below limit" in n for n in r["notes"])


def test_reward_multiple_above_limit_is_kept():
    r = exit_plan("level", D("52.00"), LIMITS, level=D("50.00"), reward_multiple=D("3"))
    assert r["target"] == "58.00" and r["reward_to_risk"] == "3.00"


def test_default_multiple_uses_limit():
    limits = replace(LIMITS, min_reward_to_risk=D("2"))
    r = exit_plan("level", D("52.00"), limits, level=D("50.00"))
    assert r["target"] == "56.00"


def test_multiple_never_below_one_even_if_limit_is_lower():
    # exit_plan has its own 1.0R floor that a lower configured limit must not undercut.
    limits = replace(LIMITS, min_reward_to_risk=D("0.5"))
    r = exit_plan("level", D("52.00"), limits, level=D("50.00"), reward_multiple=D("0.5"))
    assert r["target"] == "54.00" and r["reward_to_risk"] == "1.00"


# ---------- refusals ----------

def refused(*a, **k):
    r = exit_plan(*a, **k)
    assert r["ok"] is False and r["reason"]
    return r


def test_refuse_unknown_setup():
    refused("breakout", D("100"), LIMITS, level=D("90"))


def test_refuse_orb_missing_opening_range():
    refused("orb", D("100"), LIMITS)
    refused("orb", D("100"), LIMITS, opening_range={})
    refused("orb", D("100"), LIMITS, opening_range={"high": "101"})


def test_refuse_orb_non_numeric_low():
    refused("orb", D("100"), LIMITS, opening_range={"low": "abc", "complete": True})


def test_refuse_level_missing():
    refused("level", D("100"), LIMITS)


def test_refuse_entry_zero_and_negative():
    refused("level", D("0"), LIMITS, level=D("-1"))
    r = refused("level", D("-1"), LIMITS, level=D("-2"))
    assert "positive" in r["reason"]


def test_refuse_entry_at_or_below_structure():
    refused("level", D("50.00"), LIMITS, level=D("50.00"))  # equal
    refused("level", D("49.99"), LIMITS, level=D("50.00"))
    refused("orb", D("99.70"), LIMITS, opening_range=OR)


def test_refuse_negative_atr():
    refused("orb", D("102"), LIMITS, opening_range=OR, atr=D("-0.1"))


def test_refuse_negative_buffer_atr():
    refused("orb", D("102"), LIMITS, opening_range=OR, atr=D("1"), buffer_atr=D("-0.1"))


def test_refuse_stop_zero_or_negative():
    refused("level", D("1.00"), LIMITS, level=D("0.05"), atr=D("1"))  # 0.05 - 0.10 < 0
    refused("level", D("1.00"), LIMITS, level=D("0.10"), atr=D("1"))  # stop exactly 0
    refused("level", D("1.00"), LIMITS, level=D("0"))


def test_refuse_non_finite():
    refused("level", D("NaN"), LIMITS, level=D("50"))
    refused("level", D("100"), LIMITS, level=D("Infinity"))


# ---------- time exit and shape ----------

def test_time_exit_equals_flat_by():
    r = exit_plan("level", D("52"), LIMITS, level=D("50"))
    assert r["time_exit"] == "15:55"
    other = replace(LIMITS, flat_by=time(15, 30))
    assert exit_plan("level", D("52"), other, level=D("50"))["time_exit"] == "15:30"


def test_result_is_json_serializable():
    json.dumps(exit_plan("orb", D("102.5"), LIMITS, opening_range=OR, atr=D("0.8")))


# ---------- to_plan ----------

def test_to_plan_builds_size_plan():
    r = exit_plan("orb", D("102.50"), LIMITS, opening_range=OR, atr=D("0.80"))
    p = to_plan(r, "aapl")
    assert p == Plan("AAPL", D("102.50"), D("99.62"), D("105.38"))


def test_to_plan_raises_on_refusal():
    r = exit_plan("orb", D("102.50"), LIMITS)
    with pytest.raises(ValueError):
        to_plan(r, "AAPL")


# ---------- end to end ----------

def fixture_bars():
    names = ("alpaca_bars_page1.json", "alpaca_bars_page2.json")
    pages = [json.loads((FIX / n).read_text()) for n in names]
    return B.regular_session(B.parse_bars({"bars": pages[0]["bars"] + pages[1]["bars"]}))


def chain(now):
    summary = B.summarize(fixture_bars())
    r = exit_plan("orb", D("102.50"), LIMITS, opening_range=summary["opening_range"],
                  atr=D(summary["atr"]))
    assert r["ok"], r
    plan = to_plan(r, "AAPL")
    sized = size_position(plan, LIMITS)
    assert sized["ok"], sized
    return summary, r, sized, check_order(sized["order"], LIMITS, DayState(), now)


def test_chain_passes_in_entry_window():
    now = datetime(2026, 11, 3, 10, 30, tzinfo=NY)  # Tuesday
    summary, r, sized, verdict = chain(now)
    assert D(summary["opening_range"]["low"]) == D("99.7")
    assert D(r["stop"]) < D("99.7")  # buffered below OR low
    assert sized["shares"] >= 1
    assert D(sized["max_loss"]) <= LIMITS.max_loss_per_trade
    assert verdict == {"verdict": "PASS", "reasons": []}


def test_chain_blocked_outside_entry_window():
    now = datetime(2026, 11, 3, 9, 35, tzinfo=NY)  # before 09:45
    *_, verdict = chain(now)
    assert verdict["verdict"] == "BLOCK"
    assert any("entry window" in x for x in verdict["reasons"])


def test_chain_blocked_when_market_closed():
    now = datetime(2026, 11, 3, 17, 0, tzinfo=NY)
    *_, verdict = chain(now)
    assert verdict["verdict"] == "BLOCK"
    assert any("closed" in x for x in verdict["reasons"])


# ---------- non-numeric / non-finite input regression ----------

BAD_VALUES = [
    "abc", "", "NaN", "Infinity", "-Infinity",
    D("NaN"), D("Infinity"), float("nan"), True,
]


def _call_with(field, bad):
    kw = dict(level=D("50.00"), atr=D("0.5"), reward_multiple=D("2"))
    entry = D("52.00")
    setup = "level"
    if field == "entry":
        entry = bad
    elif field == "opening_range low":
        setup = "orb"
        orr = {"low": bad, "complete": True}
        kw = dict(opening_range=orr, atr=D("0.5"), reward_multiple=D("2"))
    elif field == "buffer_atr":
        kw["buffer_atr"] = bad
    else:
        kw[field] = bad
    return exit_plan(setup, entry, LIMITS, **kw)


@pytest.mark.parametrize("bad", BAD_VALUES, ids=repr)
@pytest.mark.parametrize(
    "field", ["entry", "level", "opening_range low", "atr", "reward_multiple", "buffer_atr"]
)
def test_non_numeric_or_non_finite_is_refused_not_raised(field, bad):
    r = _call_with(field, bad)
    assert r["ok"] is False
    assert field in r["reason"]


# ---------- item 1: opening range must be complete ----------

def test_orb_refused_when_incomplete():
    cases = (
        {"low": "99.70", "complete": False},
        {"low": "99.70"},
        {"low": "99.70", "complete": "yes"},
    )
    for orr in cases:
        r = refused("orb", D("102.50"), LIMITS, opening_range=orr, atr=D("0.8"))
        assert "opening range incomplete" in r["reason"]


def test_orb_passes_when_complete_from_fixture_chain():
    s = B.summarize(fixture_bars())
    assert s["opening_range"]["complete"] is True
    assert exit_plan("orb", D("102.50"), LIMITS, opening_range=s["opening_range"])["ok"]


def test_orb_refused_on_truncated_fixture():
    cut = [b for b in fixture_bars() if b.ts.astimezone(NY).strftime("%H:%M") < "09:45"]
    orr = B.summarize(cut)["opening_range"]
    assert orr["complete"] is False
    r = refused("orb", D("102.50"), LIMITS, opening_range=orr)
    assert "incomplete" in r["reason"]


def test_level_setup_ignores_completeness():
    assert exit_plan("level", D("52.00"), LIMITS, level=D("50.00"))["ok"]


# ---------- item 2: entry in whole cents ----------

def test_entry_whole_cents_passes():
    for e in ("52.00", "52.01", "52", "52.0100"):
        assert exit_plan("level", D(e), LIMITS, level=D("50.00"))["ok"], e


@pytest.mark.parametrize("e", ["52.005", "52.001", "52.0099", "50.005"])
def test_sub_penny_entry_refused(e):
    r = refused("level", D(e), LIMITS, level=D("50.00"))
    assert "whole number of cents" in r["reason"]


def test_sub_penny_entry_refused_for_orb():
    r = refused("orb", D("102.505"), LIMITS, opening_range=OR)
    assert "whole number of cents" in r["reason"]


# ---------- item 3: reward multiple cap ----------

def test_reward_multiple_exactly_at_cap_passes():
    assert LIMITS.max_reward_multiple == D("5")
    r = exit_plan("level", D("52.00"), LIMITS, level=D("50.00"), reward_multiple=D("5"))
    assert r["ok"] and r["target"] == "62.00" and r["reward_to_risk"] == "5.00"


def test_reward_multiple_just_over_cap_refused():
    r = refused("level", D("52.00"), LIMITS, level=D("50.00"), reward_multiple=D("5.01"))
    assert "exceeds max_reward_multiple" in r["reason"]


@pytest.mark.parametrize("m", ["0", "-1", "-0.01"])
def test_reward_multiple_non_positive_refused(m):
    r = refused("level", D("52.00"), LIMITS, level=D("50.00"), reward_multiple=D(m))
    assert "must be positive" in r["reason"]


def test_custom_cap_respected():
    tight = replace(LIMITS, max_reward_multiple=D("2"))
    assert exit_plan("level", D("52.00"), tight, level=D("50"), reward_multiple=D("2"))["ok"]
    r = refused("level", D("52.00"), tight, level=D("50"), reward_multiple=D("2.01"))
    assert "exceeds max_reward_multiple" in r["reason"]


def test_config_error_when_min_floor_exceeds_cap():
    bad = replace(LIMITS, min_reward_to_risk=D("6"), max_reward_multiple=D("5"))
    for m in (None, D("3")):
        r = refused("level", D("52.00"), bad, level=D("50"), reward_multiple=m)
        assert "config error" in r["reason"]


def test_min_equal_to_cap_is_fine():
    ok = replace(LIMITS, min_reward_to_risk=D("5"), max_reward_multiple=D("5"))
    assert exit_plan("level", D("52.00"), ok, level=D("50"))["ok"]


def test_floor_note_when_limit_below_one():
    low = replace(LIMITS, min_reward_to_risk=D("0.5"))
    r = exit_plan("level", D("52.00"), low, level=D("50"))
    assert any("floored to 1.0" in n for n in r["notes"])
    assert r["target"] == "54.00"
    normal = exit_plan("level", D("52"), LIMITS, level=D("50"))
    assert not any("floored" in n for n in normal["notes"])


# ---------- limits_from_dict: max_reward_multiple ----------

def _limits_dict():
    return json.loads((CONFIG_DIR / "limits.example.json").read_text())


def test_example_config_has_cap_of_five():
    assert "max_reward_multiple" in _limits_dict()
    assert LIMITS.max_reward_multiple == D("5")


def test_limits_from_dict_defaults_cap_when_absent():
    d = _limits_dict()
    del d["max_reward_multiple"]
    assert limits_from_dict(d).max_reward_multiple == D("5")


def test_limits_from_dict_reads_cap_when_present():
    d = _limits_dict()
    d["max_reward_multiple"] = "3.5"
    assert limits_from_dict(d).max_reward_multiple == D("3.5")
