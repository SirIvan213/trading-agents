"""resample, atr_5m, the 5-minute plan_exit context and the clean-buy trend. Offline only."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import agents.tool_registry as reg
from agents.tool_registry import price_levels
from pipeline.orders import build_order
from tests.test_scenario_review import LIMITS, session
from tests.test_scenarios import Loaded
from tools import bars as B
from tools.bars import Bar, atr, atr_5m, resample, summarize
from tools.risk_check import DayState, check_order
from tools.scan import ScanCriteria
from tools.scan import scan as run_scan

NY = ZoneInfo("America/New_York")


def bar(day, hh, mm, o, h, lo, c, v=100):
    return Bar(ts=datetime(2026, 11, day, hh, mm, tzinfo=NY), open=D(o), high=D(h), low=D(lo),
               close=D(c), volume=v)


def flat(day, start_min, count, price="10"):
    """`count` consecutive 1-minute bars from 09:30 + start_min with a 1.00 range."""
    out = []
    for i in range(count):
        t = datetime(2026, 11, day, 9, 30, tzinfo=NY) + timedelta(minutes=start_min + i)
        p = D(price)
        out.append(Bar(ts=t, open=p, high=p + 1, low=p, close=p, volume=10))
    return out


def hhmm(bars):
    return [b.ts.strftime("%H:%M") for b in bars]


# ---------------- resample ----------------

def test_resample_exact_ohlcv():
    bars = [
        bar(3, 9, 30, "10", "11", "9.5", "10.5", 100),
        bar(3, 9, 31, "10.5", "12", "10", "11", 200),
        bar(3, 9, 32, "11", "11.5", "8", "9", 300),
        bar(3, 9, 33, "9", "10", "8.5", "9.5", 400),
        bar(3, 9, 34, "9.5", "10", "9", "9.8", 500),
        bar(3, 9, 35, "9.8", "10", "9.7", "9.9", 50),  # next bucket, unfinished
    ]
    out = resample(bars)
    assert len(out) == 1  # the 09:35 bucket is unfinished and dropped
    b = out[0]
    assert b.ts == datetime(2026, 11, 3, 9, 30, tzinfo=NY)
    assert (b.open, b.high, b.low, b.close, b.volume) == (D("10"), D("12"), D("8"), D("9.8"), 1500)


def test_resample_aligns_to_the_clock_when_the_series_starts_at_0931():
    out = resample(flat(3, 1, 9))  # 09:31 .. 09:39
    # 09:30 bucket holds 09:31-09:34 (finished); 09:35 bucket holds 09:35-09:39 (finished)
    assert hhmm(out) == ["09:30", "09:35"]
    assert [b.volume for b in out] == [40, 50]


def test_resample_halt_gap_inside_a_bucket_still_counts():
    bars = [b for b in flat(3, 0, 10) if b.ts.strftime("%H:%M") not in ("09:36", "09:37", "09:38")]
    out = resample(bars)
    assert hhmm(out) == ["09:30", "09:35"]
    assert out[1].volume == 20  # 09:35 and 09:39 only


def test_resample_drops_a_trailing_partial_bucket_and_keeps_a_complete_one():
    assert hhmm(resample(flat(3, 0, 9))) == ["09:30"]  # data to 09:38: 09:35 bucket unfinished
    assert hhmm(resample(flat(3, 0, 10))) == ["09:30", "09:35"]  # data to 09:39: finished
    # a bucket whose last present minute is 09:34 is finished even with 09:30-09:33 missing
    assert len(resample(flat(3, 4, 1))) == 1


def test_resample_never_merges_two_days():
    out = resample(flat(3, 0, 5) + flat(4, 0, 5))
    assert [b.ts.day for b in out] == [3, 4]
    assert [b.volume for b in out] == [50, 50]


@pytest.mark.parametrize("minutes", [0, -1, -5])
def test_resample_rejects_minutes_below_one(minutes):
    with pytest.raises(ValueError):
        resample(flat(3, 0, 5), minutes)


def test_resample_empty_list():
    assert resample([]) == []


def test_resample_one_minute_keeps_the_ohlcv():
    bars = flat(3, 0, 3)
    out = resample(bars, 1)
    assert [(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in out] == [
        (b.ts, b.open, b.high, b.low, b.close, b.volume) for b in bars]


def test_atr_5m_none_below_n_finished_bars_and_equals_atr_of_resample():
    short = flat(3, 0, 5 * 13)  # 13 finished bars
    assert atr_5m(short) is None
    bars = flat(3, 0, 5 * 14 + 2)  # 14 finished bars plus a partial 15th, which must not count
    assert len(resample(bars)) == 14
    assert atr_5m(bars) == atr(resample(bars)) == D("1.0000")
    assert atr_5m(flat(3, 0, 5 * 13 + 4)) is None  # 13 finished + a partial one
    assert atr_5m(short, n=13) == atr(resample(short), 13)


def test_atr_5m_rejects_n_below_one():
    with pytest.raises(ValueError):
        atr_5m(flat(3, 0, 10), n=0)


# ---------------- summarize ----------------

def test_summarize_atr_5m_present_and_equals_atr_5m():
    bars = flat(3, 0, 100)
    s = summarize(bars)
    assert s["atr_5m"] == str(atr_5m(bars)) and s["atr"] == str(atr(bars))


def test_summarize_atr_5m_none_for_short_or_empty():
    short = summarize(flat(3, 0, 30))
    assert short["atr_5m"] is None
    assert short["atr"] is not None  # the 1-minute ATR is unchanged
    empty = summarize([])
    assert "atr_5m" in empty and empty["atr_5m"] is None


def test_summarize_clean_buy_has_both_atrs():
    loaded = Loaded("clean-buy")
    assert loaded.summary["atr"] == "0.1301" and loaded.summary["atr_5m"] == "0.1868"
    assert D(loaded.summary["atr"]) == atr(loaded.bars)
    assert B.atr_5m(loaded.bars) == D(loaded.summary["atr_5m"])


# ---------------- plan_exit context ----------------

def test_clean_buy_plan_exit_context_exact_values():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    lvl, err = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                       "level": lv["vwap"], "reward_multiple": "2"})
    assert err is False
    assert (lvl["risk_per_share"], lvl["stop"], lvl["target"]) == ("0.34", "80.38", "81.40")
    assert lvl["atr_5m"] == "0.1868"
    assert lvl["risk_atr_5m"] == "1.82"             # 0.34 / 0.1868 = 1.820
    assert lvl["entry_vs_vwap_atr_5m"] == "1.71"    # 0.32 / 0.1868 = 1.713
    assert lvl["session_high_vs_entry_atr_5m"] == "0.26"  # 0.05 / 0.1868 = 0.267, floored
    orb, _ = s.execute("plan_exit", {"setup": "orb", "entry": lv["last_close"],
                                     "reward_multiple": "2"})
    assert orb["risk_per_share"] == "0.85" and orb["risk_atr_5m"] == "4.55"
    assert orb["entry_vs_vwap_atr_5m"] == "1.71" and orb["target_vs_session_high"] == "at_or_above"


@pytest.mark.parametrize("bad", [None, "0", "0.0000", "-1"])
def test_plan_exit_context_fields_are_none_when_atr_5m_missing_or_not_positive(bad):
    s = session("clean-buy")
    s.summary = {**s.summary, "atr_5m": bad}
    lv = price_levels(s.summary)
    res, err = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                       "level": lv["vwap"]})
    assert err is False and res["ok"] is True
    for k in ("risk_atr_5m", "entry_vs_vwap_atr_5m", "session_high_vs_entry_atr_5m"):
        assert k in res and res[k] is None
    assert res["atr_5m"] == bad


def test_plan_exit_context_none_when_a_level_is_missing():
    s = session("clean-buy")
    levels = {k: v for k, v in s.summary["price_levels"].items() if k != "vwap"}
    s.summary = {**s.summary, "price_levels": levels}
    res, _ = s.execute("plan_exit", {"setup": "orb", "entry": levels["last_close"]})
    assert res["ok"] is True
    assert res["entry_vs_vwap_atr_5m"] is None and res["risk_atr_5m"] is not None


@pytest.fixture
def spy(monkeypatch):
    calls = []
    real = reg.exit_plan

    def wrapper(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)
    monkeypatch.setattr(reg, "exit_plan", wrapper)
    return calls


@pytest.mark.parametrize("setup", ["orb", "level"])
def test_exit_plan_still_gets_the_1_minute_atr(spy, setup):
    s = session("clean-buy")
    lv = price_levels(s.summary)
    inp = {"setup": setup, "entry": lv["last_close"]}
    if setup == "level":
        inp["level"] = lv["vwap"]
    s.execute("plan_exit", inp)
    ((_, kw),) = spy
    assert kw["atr"] == D(s.summary["atr"]) == D("0.1301")
    assert kw["atr"] != D(s.summary["atr_5m"])


def test_stop_buffer_uses_the_1_minute_atr_not_atr_5m():
    s = session("clean-buy")
    lv = price_levels(s.summary)
    s.summary = {**s.summary, "atr_5m": "5.0000"}  # a huge 5-minute ATR must change nothing
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                     "level": lv["vwap"]})
    assert res["ok"] is True and res["stop"] == "80.38" and res["risk_per_share"] == "0.34"


def test_min_risk_atr_multiple_refusal_is_judged_on_the_1_minute_atr():
    assert LIMITS.min_risk_atr_multiple == D("1.0")
    s = session("clean-buy")
    # a vwap level of 80.61 puts the stop at 80.59: risk 0.13, above the 0.10 floor but just
    # under 1.0 x the 1-minute ATR 0.1301
    s.summary = {**s.summary, "price_levels": {**s.summary["price_levels"], "vwap": "80.61"}}
    inp = {"setup": "level", "entry": "80.72", "level": "80.61"}
    res, err = s.execute("plan_exit", inp)
    assert err is False and res["ok"] is False
    assert "ATR 0.1301" in res["reason"]
    assert not s.exit_plans
    # a tiny atr_5m cannot turn the refusal into a pass
    s.summary = {**s.summary, "atr_5m": "0.0001"}
    res, _ = s.execute("plan_exit", inp)
    assert res["ok"] is False and "ATR 0.1301" in res["reason"]
    # with a 1-minute ATR of 0.05 the same plan passes although 0.12 < atr_5m 0.1868:
    # the check follows the 1-minute ATR only
    s.summary = {**s.summary, "atr": "0.05", "atr_5m": "0.1868"}
    res, _ = s.execute("plan_exit", inp)
    assert res["ok"] is True and res["risk_per_share"] == "0.12"


# ---------------- clean-buy properties ----------------

@pytest.fixture(scope="module")
def clean() -> Loaded:
    return Loaded("clean-buy")


def vwap_running(bars):
    out, pv, vol = [], D(0), 0
    for b in bars:
        pv += (b.high + b.low + b.close) / 3 * b.volume
        vol += b.volume
        out.append(pv / vol)
    return out


def minute_of(clean, b):
    return int((b.ts - clean.bars[0].ts).total_seconds() // 60)


def test_clean_buy_is_a_trend(clean):
    lv = clean.levels
    assert D(lv["last_close"]) > D(lv["or_high"]) > D(lv["or_low"])
    assert D(lv["or_low"]) == D(lv["session_low"])  # never trades under the opening range
    assert D(lv["last_close"]) > D(lv["vwap"])


def test_clean_buy_pullback_lows_hold_the_running_vwap(clean):
    checked = 0
    for b, v in zip(clean.bars, vwap_running(clean.bars), strict=True):
        if 50 <= minute_of(clean, b) <= 72:
            checked += 1
            assert b.low > v, (b.ts, b.low, v)
    assert checked == 23


def test_clean_buy_pullback_is_on_lighter_volume(clean):
    def avg(lo, hi):
        vols = [b.volume for b in clean.bars if lo <= minute_of(clean, b) <= hi]
        return D(sum(vols)) / len(vols)
    assert avg(50, 72) < avg(0, 49)


def test_clean_buy_close_near_the_high(clean):
    gap = D(clean.levels["session_high"]) - D(clean.levels["last_close"])
    assert D("0") <= gap <= D("0.10")


def test_clean_buy_both_plans_valid_and_both_orders_pass(clean):
    s = session("clean-buy")
    lv = price_levels(s.summary)
    for setup, extra in (("level", {"level": lv["vwap"]}), ("orb", {})):
        res, err = s.execute("plan_exit", {"setup": setup, "entry": lv["last_close"],
                                           "reward_multiple": "2", **extra})
        assert err is False and res["ok"] is True and res["reward_to_risk"] == "2.00"
        sized = build_order(s.exit_plans[-1], LIMITS, s.now)
        assert sized["ok"] is True, sized
        assert check_order(sized["order"], LIMITS, DayState(), s.now) == {
            "verdict": "PASS", "reasons": []}
    assert [p.setup for p in s.exit_plans] == ["level", "orb"]


def test_clean_buy_rvol_above_one_and_no_red_flags(clean):
    res = run_scan(clean.data.snapshots([clean.symbol]), ScanCriteria(), LIMITS,
                   today=clean.now.date())
    (cand,) = res["candidates"]
    assert D(str(cand["rvol"])) > 1
    assert clean.digest()["red_flags"] == []


# ---------------- prompt ----------------

PROMPT = (Path(__file__).resolve().parents[1] / "agents" / "prompts" / "technical.md").read_text(
    encoding="utf-8")


def test_technical_prompt_uses_the_5_minute_fields():
    assert "atr_5m" in PROMPT
    assert "session_high_vs_entry_atr_5m" in PROMPT
    assert "risk_atr_5m" in PROMPT and "entry_vs_vwap_atr_5m" in PROMPT
    assert "`risk_atr`" not in PROMPT and "`entry_vs_vwap_atr`" not in PROMPT  # old names gone


def test_technical_prompt_asks_for_both_an_orb_and_a_vwap_level_plan():
    assert '"orb"' in PROMPT and '"level"' in PROMPT and "`vwap`" in PROMPT
    low = PROMPT.lower()
    assert "request both" in low and "trader can choose" in low


def test_technical_prompt_never_lets_the_model_write_prices():
    assert "never write stop or target numbers yourself" in PROMPT
