from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from tools.config import load_limits
from tools.risk_check import DayState, check_order
from tools.size import Plan, size_position

NY = ZoneInfo("America/New_York")
LIMITS = load_limits()
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
