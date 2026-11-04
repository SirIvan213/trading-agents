import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from tools.config import CONFIG_DIR, limits_from_dict, load_limits
from tools.exit_plan import exit_plan
from tools.risk_check import DayState, check_order
from tools.size import Plan, size_position

NY = ZoneInfo("America/New_York")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
ORDER = size_position(
    Plan("TSLA", D("364.18"), D("358.32"), D("370.04")), LIMITS
)["order"]
MON_1015 = datetime(2026, 10, 19, 10, 15, tzinfo=NY)


def check(order=ORDER, limits=LIMITS, state=None, now=MON_1015):
    return check_order(order, limits, state or DayState(), now)


def test_good_order_passes():
    r = check()
    assert r == {"verdict": "PASS", "reasons": []}


def test_accepts_utc_clock():
    utc = MON_1015.astimezone(ZoneInfo("UTC"))
    assert check(now=utc)["verdict"] == "PASS"


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"qty": 20}, "max loss per trade"),
        ({"stop_loss": "365.00"}, "stop must be below entry"),
        ({"take_profit": "366.00"}, "reward:risk"),
        ({"side": "sell"}, "long entries only"),
        ({"qty": 0}, "whole number"),
    ],
)
def test_order_rules_block(change, expected):
    r = check(order={**ORDER, **change})
    assert r["verdict"] == "BLOCK"
    assert any(expected in reason for reason in r["reasons"])


def test_missing_stop_blocks():
    order = {k: v for k, v in ORDER.items() if k != "stop_loss"}
    assert check(order=order)["verdict"] == "BLOCK"


def test_daily_loss_limit_blocks():
    r = check(state=DayState(trades_today=2, risk_used_today=D("120")))
    assert r["verdict"] == "BLOCK"
    assert any("daily limit" in x for x in r["reasons"])


def test_daily_loss_limit_at_boundary_passes():
    # 103.12 used + 46.88 this order = exactly 150.00
    assert check(state=DayState(risk_used_today=D("103.12")))["verdict"] == "PASS"


def test_trade_count_blocks():
    assert check(state=DayState(trades_today=3))["verdict"] == "BLOCK"


@pytest.mark.parametrize(
    "when, expected",
    [
        (datetime(2026, 10, 19, 9, 35, tzinfo=NY), "entry window"),
        (datetime(2026, 10, 19, 15, 45, tzinfo=NY), "entry window"),
        (datetime(2026, 10, 19, 17, 0, tzinfo=NY), "market is closed"),
        (datetime(2026, 10, 17, 11, 0, tzinfo=NY), "market is closed"),  # Saturday
    ],
)
def test_clock_rules_block(when, expected):
    r = check(now=when)
    assert r["verdict"] == "BLOCK"
    assert any(expected in x for x in r["reasons"])


def test_blocked_symbol():
    limits = replace(LIMITS, blocked_symbols=frozenset({"TSLA"}))
    assert check(limits=limits)["verdict"] == "BLOCK"


def test_live_mode_is_blocked_in_this_phase():
    limits = replace(LIMITS, paper=False)
    r = check(limits=limits)
    assert r["verdict"] == "BLOCK"
    assert any("paper" in x for x in r["reasons"])


# ---------- reward cap (issue #7): risk_check is a second line of defence ----------

CAP5 = replace(LIMITS, max_reward_multiple=D("5"))
CAP2 = replace(LIMITS, max_reward_multiple=D("2"))


def cap_order(target, entry="100.00", stop="99.00", qty=10):
    return {
        "symbol": "AAPL", "side": "buy", "qty": qty, "type": "market",
        "order_class": "bracket", "entry": entry, "stop_loss": stop, "take_profit": target,
    }


def cap_reasons(r):
    return [x for x in r["reasons"] if "over cap" in x]


@pytest.mark.parametrize("target", ["105.00", "105.01", "104.99", "102.00"])
def test_reward_at_or_within_cap_plus_one_cent_passes(target):
    r = check(order=cap_order(target), limits=CAP5)
    assert r == {"verdict": "PASS", "reasons": []}


def test_reward_one_cent_over_tolerance_blocks_with_exact_reason():
    r = check(order=cap_order("105.02"), limits=CAP5)
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == [
        "reward:risk 5.02 over cap 5 (beyond one-cent rounding tolerance)"
    ]


def test_reward_far_over_cap_blocks_with_exact_reason():
    r = check(order=cap_order("106"), limits=CAP5)
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == [
        "reward:risk 6.00 over cap 5 (beyond one-cent rounding tolerance)"
    ]


def test_tolerance_is_one_cent_per_share_not_relative():
    # risk 0.50: cap reward = 2.50, plus 0.01 tolerance = 2.51
    ok = check(order=cap_order("102.51", stop="99.50"), limits=CAP5)
    assert ok["verdict"] == "PASS"
    bad = check(order=cap_order("102.52", stop="99.50"), limits=CAP5)
    assert bad["verdict"] == "BLOCK"
    assert bad["reasons"] == [
        "reward:risk 5.04 over cap 5 (beyond one-cent rounding tolerance)"
    ]


def test_test_local_cap_of_two():
    assert CAP2.max_reward_multiple == D("2")
    assert check(order=cap_order("102.01"), limits=CAP2)["verdict"] == "PASS"
    r = check(order=cap_order("102.02"), limits=CAP2)
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == [
        "reward:risk 2.02 over cap 2 (beyond one-cent rounding tolerance)"
    ]
    # the same order is fine under the shipped cap of 5
    assert check(order=cap_order("102.02"), limits=CAP5)["verdict"] == "PASS"


def test_cap_rule_does_not_hide_other_reasons():
    r = check(order=cap_order("106", qty=1000), limits=CAP5)
    assert r["verdict"] == "BLOCK"
    assert any("over cap" in x for x in r["reasons"])
    assert any("max loss per trade" in x for x in r["reasons"])


def test_cap_rule_skipped_when_stop_not_below_entry():
    r = check(order=cap_order("200", stop="100.00"), limits=CAP5)
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == ["stop must be below entry"]  # no division by zero, no cap text


GRID_ENTRIES = ["10.00", "33.33", "52.17", "101.01", "364.18"]
GRID_LEVEL_OFFSETS = [D("0.07"), D("0.50"), D("1.33"), D("2.01")]
GRID_ATRS = [None, D("0.37"), D("1.13"), D("0.01")]


@pytest.mark.parametrize("limits,multiple", [(CAP5, D("5")), (CAP2, D("2"))])
def test_exit_plan_at_the_cap_always_passes_the_cap_rule(limits, multiple):
    checked = 0
    for entry in GRID_ENTRIES:
        for off in GRID_LEVEL_OFFSETS:
            for atr in GRID_ATRS:
                level = D(entry) - off
                plan = exit_plan("level", D(entry), limits, level=level, atr=atr,
                                 reward_multiple=multiple)
                assert plan["ok"], (entry, off, atr, plan)
                order = cap_order(plan["target"], entry=plan["entry"], stop=plan["stop"], qty=1)
                r = check(order=order, limits=limits)
                assert cap_reasons(r) == [], (entry, off, atr, plan, r)
                risk = D(plan["entry"]) - D(plan["stop"])
                reward = D(plan["target"]) - D(plan["entry"])
                assert reward <= multiple * risk + D("0.01")
                checked += 1
    assert checked == 80


def test_exit_plan_above_cap_is_refused_before_risk_check_is_needed():
    plan = exit_plan("level", D("100.00"), CAP5, level=D("99.00"), reward_multiple=D("5.01"))
    assert plan["ok"] is False and "exceeds max_reward_multiple" in plan["reason"]


# ---------- fail closed: bad order values and hand-built limits ----------

def fc_order(**kw):
    base = cap_order("102.00")
    base.update(kw)
    return base


PRICE_FIELDS = ["entry", "stop_loss", "take_profit"]
NON_FINITE = ["NaN", "sNaN", float("nan"), "Infinity", "-Infinity"]


@pytest.mark.parametrize("field", PRICE_FIELDS)
@pytest.mark.parametrize("bad", NON_FINITE, ids=repr)
def test_non_finite_order_price_blocks_without_raising(field, bad):
    r = check(order=fc_order(**{field: bad}))
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == ["order prices must be finite numbers"]


@pytest.mark.parametrize("field", PRICE_FIELDS)
@pytest.mark.parametrize("bad", [[1], True, False, None, {"a": 1}], ids=repr)
def test_wrong_type_order_price_blocks_without_raising(field, bad):
    r = check(order=fc_order(**{field: bad}))
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == ["order needs entry, stop_loss and take_profit"]


def test_unparseable_string_price_blocks():
    r = check(order=fc_order(take_profit="abc"))
    assert r == {"verdict": "BLOCK", "reasons": ["order needs entry, stop_loss and take_profit"]}


def test_huge_exponent_target_blocks_as_arithmetic_failure():
    r = check(order=fc_order(take_profit="1e999999999"))
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == ["order arithmetic failed"]


def test_ordinary_order_with_example_limits_still_passes():
    # control: validate_limits must not over-block the shipped limits
    assert check(order=fc_order())["verdict"] == "PASS"
    assert check(order=fc_order())["reasons"] == []


def limits_invalid(limits):
    r = check(order=fc_order(), limits=limits)
    assert r["verdict"] == "BLOCK"
    assert len(r["reasons"]) == 1 and r["reasons"][0].startswith("limits invalid:")
    return r["reasons"][0]


@pytest.mark.parametrize(
    "bad",
    [D("NaN"), D("sNaN"), D("0"), D("-1"), 5.0],
    ids=["nan", "snan", "zero", "negative", "float"],
)
def test_invalid_max_reward_multiple_blocks_every_order(bad):
    msg = limits_invalid(replace(LIMITS, max_reward_multiple=bad))
    assert "max_reward_multiple" in msg


def test_regression_infinite_reward_cap_used_to_pass_now_blocks():
    # An Infinity cap disabled the reward-cap rule, so a 100:1 order used to PASS.
    wild = cap_order("200.00")  # reward 100 on risk 1
    assert check(order=wild, limits=CAP5)["verdict"] == "BLOCK"  # sanity: the cap normally bites
    r = check(order=wild, limits=replace(LIMITS, max_reward_multiple=D("Infinity")))
    assert r["verdict"] == "BLOCK"
    assert r["reasons"][0].startswith("limits invalid:")
    assert "max_reward_multiple" in r["reasons"][0]


def test_invalid_min_reward_to_risk_blocks():
    assert "min_reward_to_risk" in limits_invalid(replace(LIMITS, min_reward_to_risk=D("NaN")))


def test_invalid_max_loss_per_trade_blocks():
    assert "max_loss_per_trade" in limits_invalid(replace(LIMITS, max_loss_per_trade=D("NaN")))


def test_limits_invalid_reason_has_no_double_prefix():
    msg = limits_invalid(replace(LIMITS, max_loss_per_trade=D("NaN")))
    assert not msg.startswith("limits invalid: invalid limits:")


def test_paper_false_still_reported_first_with_invalid_limits():
    r = check(order=fc_order(), limits=replace(LIMITS, paper=False, max_reward_multiple=D("NaN")))
    assert r["verdict"] == "BLOCK"
    assert r["reasons"][0].startswith("live trading is disabled")
    assert r["reasons"][1].startswith("limits invalid:")


# ---------- order symbol vs the blocklist (issue #5) ----------

GME_BLOCKED = replace(LIMITS, blocked_symbols=frozenset({"GME"}))
GME_REASON = ["GME is on the blocked list"]


@pytest.mark.parametrize("symbol", ["GME", "gme", " GME", "GME ", "GME\n", "\tgMe  "], ids=repr)
def test_padded_or_lowercase_order_symbol_cannot_bypass_blocklist(symbol):
    r = check(order=fc_order(symbol=symbol), limits=GME_BLOCKED)
    assert r == {"verdict": "BLOCK", "reasons": GME_REASON}


def test_regression_whitespace_padded_symbol_used_to_bypass_blocklist():
    # Before the fix " GME" was not stripped, so it slipped past a blocked GME.
    r = check(order=fc_order(symbol=" GME"), limits=GME_BLOCKED)
    assert r["verdict"] == "BLOCK" and r["reasons"] == GME_REASON


@pytest.mark.parametrize("symbol", ["AAPL", "G ME", " aapl "], ids=repr)
def test_other_symbols_pass_when_gme_is_blocked(symbol):
    r = check(order=fc_order(symbol=symbol), limits=GME_BLOCKED)
    assert r == {"verdict": "PASS", "reasons": []}


@pytest.mark.parametrize("symbol", [None, [], ["AAPL"], "", "   ", "\n", 5, {"s": 1}], ids=repr)
def test_missing_or_non_string_symbol_blocks(symbol):
    r = check(order=fc_order(symbol=symbol), limits=GME_BLOCKED)
    assert r == {"verdict": "BLOCK", "reasons": ["order has no symbol"]}


def test_absent_symbol_key_blocks():
    order = fc_order()
    del order["symbol"]
    assert check(order=order)["reasons"] == ["order has no symbol"]


# ---------- qty ----------

@pytest.mark.parametrize("qty", [True, False], ids=repr)
def test_bool_qty_blocks(qty):
    r = check(order=fc_order(qty=qty))
    assert r == {"verdict": "BLOCK", "reasons": ["quantity must be a whole number of at least 1"]}


def test_qty_one_passes():
    assert check(order=fc_order(qty=1)) == {"verdict": "PASS", "reasons": []}


@pytest.mark.parametrize("qty", [0, -1, 1.0, "1", None], ids=repr)
def test_other_bad_qty_blocks(qty):
    r = check(order=fc_order(qty=qty))
    assert r["reasons"] == ["quantity must be a whole number of at least 1"]


# ---------- non-positive entry / stop ----------

@pytest.mark.parametrize(
    "entry,stop",
    [("0", "-1"), ("-0", "99.00"), ("100.00", "0"), ("100.00", "-0"), ("-5", "-6")],
    ids=repr,
)
def test_non_positive_entry_or_stop_blocks_with_exact_reason(entry, stop):
    r = check(order=fc_order(entry=entry, stop_loss=stop))
    assert r == {"verdict": "BLOCK", "reasons": ["entry and stop must be positive"]}


def test_tiny_positive_prices_are_evaluated_normally():
    r = check(order=fc_order(entry="0.02", stop_loss="0.01", take_profit="0.05"))
    assert r == {"verdict": "PASS", "reasons": []}
    # ...and still judged by the normal rules: 5x reward is over a cap of 2
    r2 = check(order=fc_order(entry="0.02", stop_loss="0.01", take_profit="0.07"),
               limits=replace(LIMITS, max_reward_multiple=D("2")))
    assert r2["verdict"] == "BLOCK"
    assert r2["reasons"] == [
        "reward:risk 5.00 over cap 2 (beyond one-cent rounding tolerance)"
    ]


# ---------- paper flag on hand-built limits ----------

LIVE_REASON = "live trading is disabled in this phase (paper must be true)"


@pytest.mark.parametrize("paper", ["false", "true", None, 0, 1, "", "yes"], ids=repr)
def test_non_bool_paper_blocks(paper):
    r = check(order=fc_order(), limits=replace(LIMITS, paper=paper))
    assert r["verdict"] == "BLOCK"
    assert r["reasons"][0] == LIVE_REASON


def test_paper_false_blocks_with_exact_reason():
    r = check(order=fc_order(), limits=replace(LIMITS, paper=False))
    assert r == {"verdict": "BLOCK", "reasons": [LIVE_REASON]}


def test_paper_true_passes():
    assert LIMITS.paper is True
    assert check(order=fc_order()) == {"verdict": "PASS", "reasons": []}


# ---------- blocked_symbols on hand-built limits ----------

@pytest.mark.parametrize(
    "bad",
    [None, ["GME"], "GME", frozenset({"gme"}), frozenset({" GME"}), frozenset({""}),
     frozenset({"GME", 5}), {"GME"}],
    ids=repr,
)
def test_hand_built_blocked_symbols_must_be_clean_frozenset(bad):
    r = check(order=fc_order(), limits=replace(LIMITS, blocked_symbols=bad))
    assert r["verdict"] == "BLOCK"
    assert len(r["reasons"]) == 1
    assert r["reasons"][0].startswith("limits invalid:") and "blocked_symbols" in r["reasons"][0]


def test_clean_frozenset_blocked_symbols_pass_validation():
    lim = replace(LIMITS, blocked_symbols=frozenset({"GME", "AMC"}))
    assert check(order=fc_order(symbol="AAPL"), limits=lim)["verdict"] == "PASS"
    assert check(order=fc_order(symbol="AMC"), limits=lim)["reasons"] == [
        "AMC is on the blocked list"
    ]


def test_loader_output_from_padded_entries_passes_validation_and_blocks_gme():
    d = json.loads((CONFIG_DIR / "limits.example.json").read_text())
    d["blocked_symbols"] = [" gme ", ""]
    lim = limits_from_dict(d)
    assert lim.blocked_symbols == frozenset({"GME"})
    assert check(order=fc_order(symbol="GME"), limits=lim) == {
        "verdict": "BLOCK", "reasons": GME_REASON}
    assert check(order=fc_order(symbol=" gme"), limits=lim)["reasons"] == GME_REASON
    assert check(order=fc_order(symbol="AAPL"), limits=lim) == {"verdict": "PASS", "reasons": []}
