"""#139: build_order refuses a live quote that already breaks the plan. Offline."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from pipeline.orders import build_order
from tests.order_helpers import AS_OF, plan
from tools.config import CONFIG_DIR, load_limits

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
NOW = AS_OF + timedelta(seconds=30)


def quote(bid: str, ask: str) -> dict:
    return {"bp": float(D(bid)), "ap": float(D(ask)), "t": (NOW - timedelta(seconds=1)).isoformat()}


# The TSLA test plan: entry 364.18, stop 358.32, target 370.04 (risk 5.86, reward 5.86).
# Its allowance is max(spread, 0.10% of 364.18 = 0.37, 0.01) = 0.37 unless the spread is wider.

def test_a_normal_quote_passes():
    out = build_order(plan(), LIMITS, NOW, quote("364.16", "364.18"))
    assert out["ok"] is True, out


def test_blocked_a_bid_at_the_stop_breaks_the_plan():
    out = build_order(plan(), LIMITS, NOW, quote("358.32", "364.18"))
    assert out == {"ok": False, "reason": (
        "live bid 358.32 is at or below the stop 358.32: the plan is already broken")}


def test_blocked_a_bid_below_the_stop_breaks_the_plan():
    out = build_order(plan(), LIMITS, NOW, quote("350.00", "364.18"))
    assert out["ok"] is False and out["reason"].startswith("live bid 350.00 is at or below")


def test_a_bid_one_cent_above_the_stop_is_not_this_refusal():
    """5.85 spread: the allowance becomes 5.85, still under the 5.86 reward."""
    out = build_order(plan(), LIMITS, NOW, quote("358.33", "364.18"))
    assert out["ok"] is True, out


def test_blocked_an_allowance_equal_to_the_reward():
    """A 5.86 spread makes the allowance 5.86 = target - entry: a worst-case fill at 370.04
    would sit on the take-profit."""
    out = build_order(plan(), LIMITS, NOW, quote("358.33", "364.19"))
    assert out == {"ok": False, "reason": (
        "slippage allowance 5.86 reaches the reward 5.86: a worst-case fill at 370.04 would be "
        "at or above the target 370.04")}


def test_an_allowance_one_cent_below_the_reward_is_not_this_refusal():
    out = build_order(plan(), LIMITS, NOW, quote("358.33", "364.18"))  # spread 5.85
    assert out["ok"] is True and out["slippage"] == "5.85", out


def test_a_tight_target_with_the_minimum_allowance_is_refused():
    """entry 20.00, stop 19.00, target 20.02: the 0.02 minimum allowance reaches the reward."""
    p = plan(symbol="AAPL", entry="20.00", stop="19.00", target="20.02", risk_per_share="1.00",
             reward_to_risk="0.02", last_close="20.00")
    out = build_order(p, LIMITS, NOW, quote("19.99", "20.00"))
    assert out == {"ok": False, "reason": (
        "slippage allowance 0.02 reaches the reward 0.02: a worst-case fill at 20.02 would be "
        "at or above the target 20.02")}
