"""Money guard (CLAUDE.md rule 2): debate text may quote plan values and nothing else."""
from decimal import Decimal

import pytest

from agents.money_guard import allowed_values, money_mentions, money_violations
from tests.debate_helpers import make_packet, make_plan

ALLOWED = {Decimal(x) for x in ("101.50", "100.25", "104.00", "1.25")}


@pytest.mark.parametrize("text", [
    "8-K item 9.01 filed today",
    "Item 2.02 and item_9.01 item-9.01",
    "at least 3 analysts are bullish",
    "at most 2",
    "price action over 15 minutes",
    "risk of 2 halts",
    "buy signal in 3 of 3 analysts",
    "opened at 09:45",
    "at 10:30",
    "revenue grew 15.00%",
    "1.25x reward",
    "entry 101.50",
    "buy at 101.50",
    "stop at $100.25",
    "buy at 101.5",
    "target 104",
])
def test_allowed(text):
    assert money_violations(text, ALLOWED) == [], text


@pytest.mark.parametrize("text", [
    "entry 102", "stop 101", "target 105", "price 99", "buy at 99.10", "stop at 101",
    "stop loss near 99", "$99", "buy 100 shares", "shares: 100", "qty 50",
    "100 shares at 101.50", "ratio 2.50", "limit 99.5", "sell at $250",
])
def test_flagged(text):
    assert money_violations(text, ALLOWED), text


def test_share_count_is_violation_even_when_price_allowed():
    bad = money_violations("100 shares at 101.50", ALLOWED)
    assert any("shares" in m for m in bad)
    assert not any(m.endswith("101.50") and "shares" not in m for m in bad)
    # a size is flagged even if the count equals an allowed value
    assert money_violations("104 shares", ALLOWED)
    assert money_violations("qty 104.00", ALLOWED)


def test_documented_false_positive_ratio():
    assert money_violations("ratio 2.50", ALLOWED)
    assert money_violations("ratio 2.50", {Decimal("2.5")}) == []  # unless it is a plan value


def test_commas_handled():
    assert money_violations("$1,000", ALLOWED)
    assert money_violations("$1,000", {Decimal("1000")}) == []
    assert money_violations("entry 1,000.50", {Decimal("1000.50")}) == []
    assert money_violations("1,000 shares", {Decimal("1000")})


def test_allowed_values_reads_exactly_five_fields_from_every_plan():
    p1 = make_plan("a", entry="10.10", stop="9.90", target="10.50", risk_per_share="0.20",
                   reward_to_risk="2.00", time_exit="15:45")
    p2 = make_plan("b", entry="20.00", stop="19.00", target="22.00", risk_per_share="1.00",
                   reward_to_risk="2.00")
    vals = allowed_values(make_packet([p1, p2]))
    assert vals == {Decimal(x) for x in (
        "10.10", "9.90", "10.50", "0.20", "20.00", "19.00", "22.00", "1.00", "2.00")}
    # entry, stop, target, risk_per_share and reward_to_risk; not time_exit or level names
    assert Decimal("15.45") not in vals and Decimal("1545") not in vals


def test_empty_packet_allows_nothing():
    pkt = make_packet([])
    assert allowed_values(pkt) == set()
    assert money_violations("entry 102.00", allowed_values(pkt))
    assert money_violations("no money here at all", allowed_values(pkt)) == []


def test_mentions_reports_all_regardless_of_allowed():
    assert money_mentions("entry 101.50") == ["entry 101.50"]


CORPUS = [
    "Three of the analysts lean bullish and the technical report cites a clean breakout.",
    "The sentiment analyst found an 8-K item 2.02 earnings release and item 9.01 exhibits.",
    "Revenue grew 15.00% year over year and gross margin expanded 3.5%.",
    "The stock opened at 09:45 above the range and held through 10:30 on rising volume.",
    "At least 2 catalysts are in play, and at most 1 red flag was found.",
    "Price action over the first 15 minutes shows buyers defending the opening range.",
    "There is a risk of 2 halts today according to the news report, which is thin.",
    "Fundamentals show 4 straight quarters of growth, but the filing is dated 2026-11-03.",
    "The reward is 1.25x the risk, per the exit plan, and the setup is an opening range break.",
    "Volume was 3 times the average, relative volume is elevated, and 2 of 3 analysts agree.",
]


@pytest.mark.parametrize("text", CORPUS)
def test_realistic_corpus_not_flagged(text):
    assert money_violations(text, set()) == [], text


# Regression: these sizes/prices once slipped past the guard (no $, decimals or keyword).
@pytest.mark.parametrize("text", [
    "buy 100", "size of 500", "go long 100", "sell short 50", "98 dollars", "trade 1000 USD",
    "position of 100 units",
])
def test_known_bypasses_are_flagged(text):
    assert money_violations(text, ALLOWED)


# The size/price keywords only match as whole words, not inside longer words.
@pytest.mark.parametrize("text", [
    "composition 100", "longest 3 sessions", "shortfall 2 quarters", "upsize 4 lanes",
    "3 of 3 analysts lean long", "long-term trend over 2 quarters", "buy signal in 3 of 3 analysts",
])
def test_keywords_inside_other_words_or_prose_counts_are_not_flagged(text):
    assert money_violations(text, ALLOWED) == []
