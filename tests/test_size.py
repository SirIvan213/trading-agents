from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from tests.order_helpers import gate_for_order
from tools.config import CONFIG_DIR, load_limits
from tools.risk_check import DayState, check_order
from tools.size import Plan, size_position
from tools.slippage import minimum_allowance

# Example config: $5,000 account, $50 max loss (explicit path, never a local limits.json).
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
TSLA = Plan("tsla", entry=D("364.18"), stop=D("358.32"), target=D("370.04"))


def test_sizes_to_max_loss():
    r = size_position(TSLA, LIMITS, slippage=D("0"))
    assert r["ok"]
    assert r["shares"] == 8  # 50 / 5.86 = 8.53 -> 8
    assert r["max_loss"] == "46.88"
    assert r["order"]["symbol"] == "TSLA"
    assert r["order"]["order_class"] == "bracket"


def test_position_cap_decides_when_tighter():
    limits = replace(LIMITS, max_position_value=D("1000"))
    r = size_position(TSLA, limits, slippage=D("0"))
    assert r["shares"] == 2  # 1000 / 364.18 = 2.7
    assert r["decided_by"] == "max_position_value"


def test_stop_too_far_is_a_refusal():
    wide = replace(TSLA, stop=D("300.00"))  # 64.18 a share > $50
    r = size_position(wide, LIMITS, slippage=D("0"))
    assert not r["ok"]
    assert "max_loss_per_trade" in r["reason"]


def test_rejects_stop_at_or_above_entry():
    assert not size_position(replace(TSLA, stop=D("364.18")), LIMITS, slippage=D("0"))["ok"]
    assert not size_position(replace(TSLA, stop=D("365.00")), LIMITS, slippage=D("0"))["ok"]


def test_rejects_target_at_or_below_entry():
    assert not size_position(replace(TSLA, target=D("364.18")), LIMITS, slippage=D("0"))["ok"]


# ---------- symbol handling (issue #5) ----------

def test_plan_symbol_is_stripped_and_uppercased():
    r = size_position(Plan(" gme ", D("20.00"), D("19.00"), D("21.00")), LIMITS, slippage=D("0"))
    assert r["ok"] and r["order"]["symbol"] == "GME"


@pytest.mark.parametrize("symbol", ["", "   ", "\n", None, 5, ["AAPL"]], ids=repr)
def test_missing_or_non_string_symbol_is_refused(symbol):
    r = size_position(Plan(symbol, D("20.00"), D("19.00"), D("21.00")), LIMITS, slippage=D("0"))
    assert r == {"ok": False, "reason": "symbol is required"}


def test_padded_symbol_chain_is_blocked_by_risk_check():
    limits = replace(LIMITS, blocked_symbols=frozenset({"GME"}))
    plan = Plan(" gme ", D("20.00"), D("19.00"), D("21.00"))
    sized = size_position(plan, limits, slippage=minimum_allowance(plan.entry, limits))
    assert sized["ok"]
    now = datetime(2026, 10, 19, 10, 15, tzinfo=ZoneInfo("America/New_York"))
    r = check_order(sized["order"], limits, DayState(), now,
        gate=gate_for_order(sized["order"], now))
    assert r == {"verdict": "BLOCK", "reasons": ["GME is on the blocked list"]}


def test_padded_other_symbol_chain_passes():
    limits = replace(LIMITS, blocked_symbols=frozenset({"GME"}))
    plan = Plan(" aapl ", D("20.00"), D("19.00"), D("21.00"))
    sized = size_position(plan, limits, slippage=minimum_allowance(plan.entry, limits))
    now = datetime(2026, 10, 19, 10, 15, tzinfo=ZoneInfo("America/New_York"))
    assert check_order(sized["order"], limits, DayState(), now,
        gate=gate_for_order(sized["order"], now)) == {
        "verdict": "PASS", "reasons": []}
