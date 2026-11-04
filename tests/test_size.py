from dataclasses import replace
from decimal import Decimal as D

from tools.config import load_limits
from tools.size import Plan, size_position

LIMITS = load_limits()  # config/limits.example.json: $5,000 account, $50 max loss
TSLA = Plan("tsla", entry=D("364.18"), stop=D("358.32"), target=D("370.04"))


def test_sizes_to_max_loss():
    r = size_position(TSLA, LIMITS)
    assert r["ok"]
    assert r["shares"] == 8  # 50 / 5.86 = 8.53 -> 8
    assert r["max_loss"] == "46.88"
    assert r["order"]["symbol"] == "TSLA"
    assert r["order"]["order_class"] == "bracket"


def test_position_cap_decides_when_tighter():
    limits = replace(LIMITS, max_position_value=D("1000"))
    r = size_position(TSLA, limits)
    assert r["shares"] == 2  # 1000 / 364.18 = 2.7
    assert r["decided_by"] == "max_position_value"


def test_stop_too_far_is_a_refusal():
    wide = replace(TSLA, stop=D("300.00"))  # 64.18 a share > $50
    r = size_position(wide, LIMITS)
    assert not r["ok"]
    assert "max_loss_per_trade" in r["reason"]


def test_rejects_stop_at_or_above_entry():
    assert not size_position(replace(TSLA, stop=D("364.18")), LIMITS)["ok"]
    assert not size_position(replace(TSLA, stop=D("365.00")), LIMITS)["ok"]


def test_rejects_target_at_or_below_entry():
    assert not size_position(replace(TSLA, target=D("364.18")), LIMITS)["ok"]
