"""Issues #66 (minimum risk per share) and #67 (plan freshness at order time). Offline."""
import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

import pipeline.orders as orders
from agents.schemas import ExitPlanRecord
from agents.tool_registry import ToolSession
from pipeline.orders import build_order
from tests.fakes import NOW, FakeMarketData
from tests.order_helpers import fresh_quote, gate_for_order
from tools.config import CONFIG_DIR, Limits, limits_from_dict, load_limits, validate_limits
from tools.exit_plan import exit_plan
from tools.risk_check import DayState, check_order
from tools.scan import ScanCriteria
from tools.size import Plan, size_position

NY = ZoneInfo("America/New_York")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
EXAMPLE = json.loads((CONFIG_DIR / "limits.example.json").read_text())
MON_1015 = datetime(2026, 10, 19, 10, 15, tzinfo=NY)


# ===================== #66: config fields =====================

def cfg(**over):
    d = copy.deepcopy(EXAMPLE)
    d.update(over)
    return d


def test_example_config_ships_the_new_limits():
    assert EXAMPLE["min_risk_per_share"] == "0.10"
    assert EXAMPLE["min_risk_atr_multiple"] == "1.0"
    assert EXAMPLE["max_plan_age_s"] == 300
    assert LIMITS.min_risk_per_share == D("0.10")
    assert LIMITS.min_risk_atr_multiple == D("1.0")
    assert LIMITS.max_plan_age_s == 300
    assert LIMITS.paper is True


def test_missing_keys_use_the_defaults():
    d = cfg()
    for k in ("min_risk_per_share", "min_risk_atr_multiple", "max_plan_age_s"):
        del d[k]
    lim = limits_from_dict(d)
    assert lim.min_risk_per_share == D("0.10")
    assert lim.min_risk_atr_multiple == D("1.0")
    assert lim.max_plan_age_s == 300


def test_dataclass_defaults():
    assert Limits.__dataclass_fields__["min_risk_per_share"].default == D("0.10")
    assert Limits.__dataclass_fields__["min_risk_atr_multiple"].default == D("1.0")
    assert Limits.__dataclass_fields__["max_plan_age_s"].default == 300


def test_custom_values_load():
    lim = limits_from_dict(cfg(min_risk_per_share="0.25", min_risk_atr_multiple="2",
                               max_plan_age_s=60))
    assert (lim.min_risk_per_share, lim.min_risk_atr_multiple, lim.max_plan_age_s) == (
        D("0.25"), D("2"), 60)


@pytest.mark.parametrize("bad", ["-0.01", "-1", "0", "0.00", "NaN", "Infinity", "-Infinity",
                                 "abc", ""])
def test_bad_min_risk_per_share_rejected(bad):
    with pytest.raises(ValueError, match="min_risk_per_share"):
        limits_from_dict(cfg(min_risk_per_share=bad))


@pytest.mark.parametrize("bad", ["-0.01", "-1", "0", "0.00", "NaN", "Infinity", "abc"])
def test_bad_min_risk_atr_multiple_rejected(bad):
    with pytest.raises(ValueError, match="min_risk_atr_multiple"):
        limits_from_dict(cfg(min_risk_atr_multiple=bad))


def test_zero_atr_multiple_is_rejected():
    # #76: a zero multiple is a disabled limit, not a setting
    with pytest.raises(ValueError, match="min_risk_atr_multiple must be greater than zero"):
        limits_from_dict(cfg(min_risk_atr_multiple="0"))


@pytest.mark.parametrize("bad", [0, -1, 901, 3600, True, False, 1.5, "abc", None, "1.5", [],
                                 "-5"])
def test_bad_max_plan_age_rejected(bad):
    with pytest.raises(ValueError, match="max_plan_age_s"):
        limits_from_dict(cfg(max_plan_age_s=bad))


@pytest.mark.parametrize("good", [1, 300, 900, 300.0, "300"])
def test_good_max_plan_age_accepted(good):
    assert limits_from_dict(cfg(max_plan_age_s=good)).max_plan_age_s == int(good)


@pytest.mark.parametrize(("field", "bad"), [
    ("min_risk_per_share", D("0")), ("min_risk_per_share", D("-0.1")),
    ("min_risk_per_share", D("NaN")), ("min_risk_per_share", 0.1),
    ("min_risk_atr_multiple", D("-1")), ("min_risk_atr_multiple", D("NaN")),
    ("min_risk_atr_multiple", 1.0),
    ("max_plan_age_s", 0), ("max_plan_age_s", True), ("max_plan_age_s", 1.5),
    ("max_plan_age_s", -5),
])
def test_validate_limits_rejects_hand_built_bad_values(field, bad):
    with pytest.raises(ValueError, match=field):
        validate_limits(replace(LIMITS, **{field: bad}))


def test_validate_limits_accepts_the_shipped_values():
    assert validate_limits(LIMITS) is None
    with pytest.raises(ValueError, match="min_risk_atr_multiple"):
        validate_limits(replace(LIMITS, min_risk_atr_multiple=D("0")))


# ===================== #66: exit_plan =====================

ENTRY = D("100.00")
# Isolates the absolute floor: the ATR multiple (0 is no longer valid, #76) and the percent-of-entry
# floor (#77, 0.25% of 100.00 = 0.25) are made too small to bind at these prices.
NOISE = replace(LIMITS, min_risk_per_share=D("0.10"), min_risk_atr_multiple=D("0.01"),
                min_risk_pct=D("0.01"))


def plan_level(level, limits=LIMITS, atr=None, entry=ENTRY):
    return exit_plan("level", entry, limits, level=D(level), atr=atr)


def test_exit_plan_risk_exactly_at_the_minimum_passes():
    r = plan_level("99.90", NOISE)
    assert r["ok"] is True and r["risk_per_share"] == "0.10" and r["stop"] == "99.90"


def test_exit_plan_risk_one_cent_below_the_minimum_is_refused():
    r = plan_level("99.91", NOISE)
    assert r == {"ok": False,
                 "reason": "risk per share 0.09 is below min_risk_per_share 0.10"}


@pytest.mark.parametrize("level", ["99.99", "99.95", "99.00"])
def test_exit_plan_min_risk_boundary_is_strict_per_cent(level):
    r = plan_level(level, replace(NOISE, min_risk_per_share=D("0.50")))
    assert (r["ok"], r.get("risk_per_share")) == (level == "99.00", "1.00" if level == "99.00"
                                                    else None)


ATRL = replace(LIMITS, min_risk_per_share=D("0.01"), min_risk_atr_multiple=D("1.0"))
# ATR 2.00: buffer 0.10 x 2.00 = 0.20, so stop = level - 0.20 and risk = entry - level + 0.20


def test_exit_plan_atr_rule_exactly_at_the_multiple_passes():
    r = plan_level("98.20", ATRL, atr=D("2.00"))  # stop 98.00, risk 2.00 == 1.0 x ATR
    assert r["ok"] is True and r["risk_per_share"] == "2.00" and r["stop"] == "98.00"


def test_exit_plan_atr_rule_just_below_is_refused():
    r = plan_level("98.21", ATRL, atr=D("2.00"))  # stop 98.01, risk 1.99
    assert r == {"ok": False, "reason": "risk per share 1.99 is below 1.0 x ATR 2.00"}


def test_exit_plan_atr_multiple_scales():
    two = replace(ATRL, min_risk_atr_multiple=D("2"))
    assert plan_level("96.20", two, atr=D("2.00"))["ok"] is True   # risk 4.00 == 2 x 2.00
    r = plan_level("96.21", two, atr=D("2.00"))                    # risk 3.99
    assert r == {"ok": False, "reason": "risk per share 3.99 is below 2 x ATR 2.00"}


def test_exit_plan_tiny_atr_multiple_barely_binds():
    # zero is no longer a valid multiple (#76); 0.01 x ATR 2.00 = 0.02 is far below the risk
    off = replace(ATRL, min_risk_atr_multiple=D("0.01"))
    r = plan_level("99.50", off, atr=D("2.00"))  # stop 99.30, risk 0.70 < ATR 2.00
    assert r["ok"] is True and r["risk_per_share"] == "0.70"


def test_exit_plan_no_atr_means_no_atr_rule():
    assert plan_level("99.50", ATRL, atr=None)["ok"] is True


def test_exit_plan_absolute_minimum_applies_even_when_atr_rule_is_tiny():
    off = replace(LIMITS, min_risk_atr_multiple=D("0.01"))
    assert plan_level("99.95", off, atr=D("0.01"))["ok"] is False


def test_exit_plan_orb_refused_below_the_minimum():
    orr = {"high": "100.00", "low": "99.96", "complete": True, "missing_minutes": 0}
    r = exit_plan("orb", D("100.05"), NOISE, opening_range=orr)
    assert r["ok"] is False and "below min_risk_per_share" in r["reason"]


# ===================== #66: plan_exit on the AAPL fixture =====================

def loaded() -> ToolSession:
    s = ToolSession(symbol="AAPL", now=NOW, data=FakeMarketData(), limits=load_limits(),
                    criteria=ScanCriteria())
    _, err = s.execute("get_bars", {"symbol": "AAPL"})
    assert err is False
    assert s.summary["atr"] == "0.8151"
    return s


def put_vwap(s, vwap):
    s.summary["price_levels"] = {**s.summary["price_levels"], "vwap": vwap}


def test_plan_exit_noise_stop_pair_is_refused_without_a_record():
    s = loaded()
    # entry last_close 103.10, a vwap level of 103.00 -> stop floor(103.00 - 0.08151) = 102.91
    # (#77: level 103.00 would be risk 0.19, now below 0.25% of entry, so use 102.80)
    # risk 0.39 passes the 0.10 minimum and 0.25% of entry (0.2578) but is below 1 x ATR 0.8151
    put_vwap(s, "102.80")
    res, err = s.execute("plan_exit",
                         {"setup": "level", "entry": "103.10", "level": "102.80"})
    assert err is False
    assert res == {"ok": False, "reason": "risk per share 0.39 is below 1.0 x ATR 0.8151"}
    assert s.exit_plans == []


def test_plan_exit_second_noise_pair_below_atr_is_refused():
    s = loaded()
    # the fixture's provisional 5-minute ATR (1.2000) would bind here (see the sibling test
    # below); drop it so this test isolates the 1-minute rule
    s.summary = {**s.summary, "atr_5m": None, "atr_5m_provisional": False}
    # entry last_close 103.10, level 101.60 (vwap) -> stop 101.51, risk 1.59 > ATR: accepted
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "101.60"})
    assert res["ok"] is True and res["plan_id"] == "plan-1"
    # entry 103.10, level 102.00 (or_high) -> stop 101.91, risk 1.19: accepted
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "102.00"})
    assert res["ok"] is True
    # entry 103.10, vwap level 102.50 -> stop 102.41, risk 0.69 < ATR 0.8151
    put_vwap(s, "102.50")
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "102.50"})
    assert res == {"ok": False, "reason": "risk per share 0.69 is below 1.0 x ATR 0.8151"}
    assert [p.plan_id for p in s.exit_plans] == ["plan-1", "plan-2"]


def test_plan_exit_provisional_5_minute_atr_binds_where_the_1_minute_atr_does_not():
    s = loaded()
    assert s.summary["atr_5m"] == "1.2000" and s.summary["atr_5m_provisional"] is True
    # risk 1.59 >= 1.2: accepted
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "101.60"})
    assert res["ok"] is True and res["plan_id"] == "plan-1"
    # risk 1.19 passes the 1-minute ATR 0.8151 but is below the provisional 5-minute 1.2000
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "102.00"})
    assert res == {"ok": False,
                   "reason": "risk per share 1.19 is below 1.0 x provisional 5-minute ATR 1.2000"}
    # risk 0.69 fails the 1-minute rule first, with the unchanged message
    put_vwap(s, "102.50")
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "102.50"})
    assert res == {"ok": False, "reason": "risk per share 0.69 is below 1.0 x ATR 0.8151"}
    assert [p.plan_id for p in s.exit_plans] == ["plan-1"]


def test_plan_exit_with_tiny_atr_multiple_accepts_the_same_pair():
    s = loaded()
    s.limits = replace(s.limits, min_risk_atr_multiple=D("0.01"))
    s.summary = {**s.summary, "atr_5m": None, "atr_5m_provisional": False}
    put_vwap(s, "102.80")
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "103.10", "level": "102.80"})
    assert res["ok"] is True and res["risk_per_share"] == "0.39"
    assert len(s.exit_plans) == 1


# ===================== #66: risk_check =====================

def order(entry="100.00", stop="99.90", target="100.10", qty=1):
    return {"symbol": "TSLA", "side": "buy", "qty": qty, "type": "market",
            "order_class": "bracket", "entry": entry, "stop_loss": stop, "take_profit": target,
            "slippage": "0.10"}  # the minimum allowance for a 100.00 entry (0.10%)


def check(o, limits=LIMITS):
    return check_order(o, limits, DayState(), MON_1015, gate=gate_for_order(o, MON_1015))


def test_risk_check_risk_exactly_at_the_minimum_passes():
    # percent-of-entry floor (#77) loosened so only the absolute floor is under test
    assert check(order(stop="99.90", target="100.10"), NOISE) == {"verdict": "PASS",
                                                                   "reasons": []}


def test_risk_check_risk_below_the_minimum_blocks_with_exact_reason():
    r = check(order(stop="99.91", target="100.09"), NOISE)
    assert r == {"verdict": "BLOCK", "reasons": ["risk per share 0.09 below minimum 0.10"]}


def test_risk_check_minimum_follows_the_limit():
    lim = replace(LIMITS, min_risk_per_share=D("0.50"))
    assert check(order(stop="99.50", target="100.50"), lim)["verdict"] == "PASS"
    r = check(order(stop="99.51", target="100.49"), lim)
    assert r["reasons"] == ["risk per share 0.49 below minimum 0.50"]


def test_risk_check_inverted_order_gives_only_the_stop_reason():
    for stop in ("100.00", "100.05", "101.00"):
        r = check(order(stop=stop, target="102.00"))
        assert r == {"verdict": "BLOCK", "reasons": ["stop must be below entry"]}, stop


def test_risk_check_minimum_reason_is_appended_after_the_reward_cap():
    lim = replace(NOISE, max_reward_multiple=D("2"))
    r = check(order(stop="99.95", target="100.50"), lim)  # risk 0.05, reward 0.50 = 10x
    assert r["verdict"] == "BLOCK"
    assert r["reasons"][-1] == "risk per share 0.05 below minimum 0.10"
    assert any("over cap" in x for x in r["reasons"][:-1])


def test_risk_check_sizing_chain_for_a_valid_plan_still_passes():
    plan = size_position(Plan("TSLA", D("364.18"), D("358.32"), D("370.04")), LIMITS,
                         slippage=D("0.37"))
    assert check(plan["order"]) == {"verdict": "PASS", "reasons": []}


# ===================== #67: build_order freshness =====================

def plan_record(**over) -> ExitPlanRecord:
    d = {
        "plan_id": "plan-1", "symbol": "TSLA", "setup": "level", "entry": "364.18",
        "stop": "358.32", "target": "370.04", "risk_per_share": "5.86",
        "reward_to_risk": "1.00", "time_exit": "15:55", "entry_level": "last_close",
        "structure_level": "vwap", "as_of": "2026-10-19T10:15:00-04:00", "last_close": "364.18",
    }
    d.update(over)
    return ExitPlanRecord(**d)


MADE = datetime(2026, 10, 19, 10, 15, tzinfo=NY)  # == plan_record().as_of
REAL_PLAN = Plan("TSLA", D("364.18"), D("358.32"), D("370.04"))


def build_order_quoted(plan, limits, now):
    """build_order with a live quote (#68) fresh at `now`: ask = the plan's entry, 2c spread."""
    return build_order(plan, limits, now, fresh_quote(plan.entry, now))


@pytest.fixture
def size_spy(monkeypatch):
    calls = []
    real = orders.size_position

    def _spy(*a, **k):
        calls.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(orders, "size_position", _spy)
    return calls


def refused(reason, now, size_spy, plan=None, limits=LIMITS):
    before = len(size_spy)
    out = build_order(plan or plan_record(), limits, now)
    assert out == {"ok": False, "reason": reason}
    assert len(size_spy) == before  # size_position was not called


def test_fresh_plan_sizes_exactly_as_before(size_spy):
    now = MADE + timedelta(seconds=30)
    out = build_order(plan_record(), LIMITS, now, fresh_quote("364.18", now))
    direct = size_position(REAL_PLAN, LIMITS, slippage=D("0.37"))  # allowance 0.37 (#68)
    assert {k: v for k, v in out.items() if k != "quote"} == direct and out["ok"] is True
    assert out["quote"]["ask"] == "364.18" and out["quote"]["age_s"] == 1
    assert len(size_spy) == 1  # exactly one call, made by build_order


@pytest.mark.parametrize("age", [0, 1, 299, 300])
def test_age_up_to_the_maximum_passes(age, size_spy):
    assert build_order_quoted(plan_record(), LIMITS, MADE + timedelta(seconds=age))["ok"] is True


def test_age_301_is_refused(size_spy):
    refused("plan is 301s old (max 300s)", MADE + timedelta(seconds=301), size_spy)


def test_age_300_point_9_truncates_to_300_and_passes(size_spy):
    now = MADE + timedelta(seconds=300, milliseconds=900)
    assert build_order_quoted(plan_record(), LIMITS, now)["ok"] is True


def test_age_301_point_1_is_refused_as_301(size_spy):
    refused("plan is 301s old (max 300s)", MADE + timedelta(seconds=301, milliseconds=100),
            size_spy)


def test_old_plan_message_reports_whole_seconds(size_spy):
    refused("plan is 4000s old (max 300s)", MADE + timedelta(seconds=4000), size_spy)


def test_max_age_follows_the_limit(size_spy):
    lim = replace(LIMITS, max_plan_age_s=10)
    assert build_order_quoted(plan_record(), lim, MADE + timedelta(seconds=10))["ok"] is True
    refused("plan is 11s old (max 10s)", MADE + timedelta(seconds=11), size_spy, limits=lim)


def test_future_plan_within_five_seconds_passes(size_spy):
    assert build_order_quoted(plan_record(), LIMITS, MADE - timedelta(seconds=5))["ok"] is True


def test_future_plan_six_seconds_ahead_is_refused(size_spy):
    refused("plan is dated in the future", MADE - timedelta(seconds=6), size_spy)


def test_future_plan_just_over_five_seconds_is_refused(size_spy):
    refused("plan is dated in the future", MADE - timedelta(seconds=5, milliseconds=1),
            size_spy)


def test_plan_from_the_previous_day_is_another_session_day(size_spy):
    plan = plan_record(as_of="2026-10-19T23:59:59-04:00")
    now = datetime(2026, 10, 20, 0, 0, 1, tzinfo=NY)  # two seconds later, across midnight ET
    refused("plan is from another session day", now, size_spy, plan=plan)


def test_midnight_boundary_is_judged_in_new_york_not_utc(size_spy):
    # 20:00 ET Oct 19 is 00:00 UTC Oct 20; 23:00 ET is the same ET day but a UTC "next" day
    plan = plan_record(as_of="2026-10-19T20:00:00-04:00")
    same_day = datetime(2026, 10, 19, 20, 0, 30, tzinfo=NY)
    assert build_order_quoted(plan, LIMITS, same_day)["ok"] is True
    # now expressed in UTC with a different calendar date but the same ET day
    utc_now = datetime(2026, 10, 20, 0, 0, 30, tzinfo=ZoneInfo("UTC"))
    assert build_order_quoted(plan, LIMITS, utc_now)["ok"] is True
    refused("plan is 10800s old (max 300s)",
            datetime(2026, 10, 19, 23, 0, 0, tzinfo=NY), size_spy, plan=plan)


def test_as_of_in_another_offset_is_compared_as_an_instant(size_spy):
    plan = plan_record(as_of="2026-10-19T14:15:00+00:00")  # == 10:15 ET
    assert build_order_quoted(plan, LIMITS, MADE + timedelta(seconds=30))["ok"] is True


def test_naive_now_is_refused(size_spy):
    refused("now must be timezone-aware", datetime(2026, 10, 19, 10, 15, 30), size_spy)


@pytest.mark.parametrize("as_of", ["", "yesterday", "2026-10-19T10:15:00", "not-a-date",
                                   "2026-13-45T10:15:00-04:00"])
def test_invalid_or_naive_as_of_is_refused(as_of, size_spy):
    refused("plan as_of is invalid", MADE + timedelta(seconds=30), size_spy,
            plan=plan_record(as_of=as_of))


def test_price_checks_come_before_freshness(size_spy):
    stale = MADE + timedelta(hours=5)
    bad = ExitPlanRecord.model_construct(**{**plan_record().model_dump(), "entry": "abc"})
    refused("plan prices must be finite whole cents", stale, size_spy, plan=bad)
    bad = ExitPlanRecord.model_construct(**{**plan_record().model_dump(), "symbol": "no way!"})
    refused("plan symbol is not a valid ticker", stale, size_spy, plan=bad)


def test_every_refusal_never_reaches_size_position(size_spy):
    for now, plan in [
        (MADE + timedelta(seconds=301), plan_record()),
        (MADE - timedelta(seconds=6), plan_record()),
        (datetime(2026, 10, 19, 10, 15, 30), plan_record()),
        (MADE, plan_record(as_of="junk")),
        (datetime(2026, 10, 20, 9, 30, tzinfo=NY), plan_record()),
    ]:
        assert build_order(plan, LIMITS, now)["ok"] is False
    assert size_spy == []


def test_refusal_reasons_carry_no_plan_text(size_spy):
    out = build_order(plan_record(as_of="sk-ant-SENTINEL"), LIMITS, MADE)
    assert "SENTINEL" not in json.dumps(out)


# ===================== #67: DST edges =====================

def test_fall_back_hour_counts_real_elapsed_seconds(size_spy):
    # 01:30 EDT (05:30Z) to 01:31 EST (06:31Z) is 3,660 real seconds, not 60
    plan = plan_record(as_of="2026-11-01T01:30:00-04:00")
    now = datetime(2026, 11, 1, 1, 31, tzinfo=NY, fold=1)  # the second 01:31, EST
    assert now.utcoffset() == timedelta(hours=-5)
    refused("plan is 3660s old (max 300s)", now, size_spy, plan=plan)


def test_fall_back_first_occurrence_is_fresh(size_spy):
    plan = plan_record(as_of="2026-11-01T01:30:00-04:00")
    now = datetime(2026, 11, 1, 1, 31, tzinfo=NY, fold=0)  # first 01:31, still EDT
    assert now.utcoffset() == timedelta(hours=-4)
    assert build_order_quoted(plan, LIMITS, now)["ok"] is True  # 60 s


def test_spring_forward_gap_counts_real_elapsed_seconds(size_spy):
    # 01:59:30 EST (06:59:30Z) to 03:00:30 EDT (07:00:30Z) is 60 s of real time, not 1 h 1 min
    plan = plan_record(as_of="2026-03-08T01:59:30-05:00")
    now = datetime(2026, 3, 8, 3, 0, 30, tzinfo=NY)
    assert now.utcoffset() == timedelta(hours=-4)
    assert build_order_quoted(plan, LIMITS, now)["ok"] is True
    late = datetime(2026, 3, 8, 3, 5, 31, tzinfo=NY)  # 361 s of real time
    refused("plan is 361s old (max 300s)", late, size_spy, plan=plan)


# ===================== #66: no ATR (fewer than 14 bars) =====================

class FewBars(FakeMarketData):
    def bars(self, symbol, start, end):
        out = super().bars(symbol, start, end)
        out["bars"] = out["bars"][:10]
        return out


def few_bars_session() -> ToolSession:
    s = ToolSession(symbol="AAPL", now=NOW, data=FewBars(), limits=load_limits(),
                    criteria=ScanCriteria())
    _, err = s.execute("get_bars", {"symbol": "AAPL"})
    assert err is False
    assert s.summary["atr"] is None and s.summary["bars"] < 14
    # same ATR-less summary, price levels chosen to sit 0.09 and 0.10 below last_close
    s.summary["price_levels"] = {
        "session_low": "99.90", "session_high": "100.50", "last_close": "100.00",
        "vwap": "99.91",
    }
    return s


def test_no_atr_only_the_absolute_floor_applies_refused_at_009():
    s = few_bars_session()
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "100.00", "level": "99.91"})
    assert err is False
    assert res == {"ok": False,
                   "reason": "risk per share 0.09 is below min_risk_per_share 0.10"}
    assert s.exit_plans == []


def test_no_atr_only_the_absolute_floor_applies_passes_at_010():
    s = few_bars_session()
    s.limits = replace(s.limits, min_risk_pct=D("0.01"))  # isolate the 0.10 floor from #77
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "100.00", "level": "99.90"})
    assert err is False and res["ok"] is True and res["risk_per_share"] == "0.10"
    assert res["plan_id"] == "plan-1" and len(s.exit_plans) == 1


def test_no_atr_large_multiple_does_not_matter():
    s = few_bars_session()
    s.limits = replace(s.limits, min_risk_atr_multiple=D("50"))
    put_vwap(s, "99.75")
    res, _ = s.execute("plan_exit", {"setup": "level", "entry": "100.00", "level": "99.75"})
    assert res["ok"] is True  # there is no ATR to multiply (risk 0.25 = the #77 floor)
