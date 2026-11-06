"""Money guard review fixes: per-share prices are prices, and exemptions no longer leak."""
from decimal import Decimal

import pytest

from agents.money_guard import money_violations

TABLE_ALLOWED = {Decimal(x) for x in ("2.39", "101.50", "1.00")}


@pytest.mark.parametrize("text", [
    "Risk is 2.39 per share", "2.39/share", "2.39 a share", "price per share 101.50",
    "reward_to_risk 1.00", "opened at 09:45", "8-K item 9.01",
])
def test_table_allowed(text):
    assert money_violations(text, TABLE_ALLOWED) == [], text


@pytest.mark.parametrize("text", [
    "2.40 per share", "3/share", "stop item 98.50", "stop :98.50", "stop at $98.50:00",
    "buy at 101:50", "CEO sold 50,000 shares",
])
def test_table_flagged(text):
    assert money_violations(text, TABLE_ALLOWED), text


def test_per_share_price_is_price_not_size_but_must_be_allowed():
    assert money_violations("2.39 per share", TABLE_ALLOWED) == []
    assert money_violations("2.39 per share", set())
    assert money_violations("2.39 shares", TABLE_ALLOWED)  # a plain count stays a size
