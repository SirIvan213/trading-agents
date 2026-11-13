"""#182: the halt check skips a news item whose timestamp parses to a time before today's New
York midnight (it cannot be today's halt), however malformed the rest of it is. An item whose
timestamp does not parse still makes the halt check UNKNOWN. The earnings-news window from #169
is unchanged. Offline; no live calls."""
from datetime import UTC, datetime, timedelta

import pytest

from tests.order_helpers import (
    NY,
    gate_for,
    halts_ok,
    listing_ok,
    news_item,
    news_ok,
    verdict_for,
)
from tools.event_gate import halt_status

MON = datetime(2026, 11, 9, 10, 30, tzinfo=NY)   # a Monday; the fetch reaches Friday 16:00
FRI_EVENING = datetime(2026, 11, 6, 18, 0, tzinfo=NY)


def halt(news_items, now=MON):
    return halt_status("TSLA", now, news_ok(news_items), halts_ok(now), listing_ok("TSLA"))


def stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def friday(**over):
    item = news_item("TSLA", "Tesla shares rise", FRI_EVENING)
    item.update(over)
    return item


# ======================= old items with a valid timestamp are skipped =======================

@pytest.mark.parametrize("over", [
    {"headline": None},
    {"headline": "   "},
    {"symbols": None},
    {"symbols": []},
    {"symbols": ["NOT-TSLA"]},
], ids=["no-headline", "blank-headline", "no-symbols", "empty-symbols", "other-symbol"])
def test_a_malformed_item_from_friday_is_skipped_on_monday(over):
    """The #182 case: before #169 a 24-hour fetch on Monday never saw Friday evening."""
    assert halt([friday(**over)]) == {"status": "CLEAR", "findings": []}


def test_the_gate_is_clear_with_only_an_old_malformed_item():
    gate = gate_for("TSLA", MON, news=news_ok([friday(headline=None)]))
    assert gate["status"] == "CLEAR", gate["reason"]
    assert verdict_for(gate, MON) == {"verdict": "PASS", "reasons": []}


def test_an_old_halt_headline_is_not_todays_halt():
    item = news_item("TSLA", "Trading halted in TSLA pending news", FRI_EVENING)
    assert halt([item])["status"] == "CLEAR"


def test_one_second_before_midnight_is_skipped():
    late_sunday = datetime(2026, 11, 8, 23, 59, 59, tzinfo=NY)
    assert halt([news_item("TSLA", "Trading halted in TSLA", late_sunday)])["status"] == "CLEAR"


def test_midnight_uses_the_new_york_offset_after_the_clock_change():
    """Monday 2026-11-02 (EST since 11-01): 23:30 EST on Sunday is skipped, 00:30 counts."""
    mon = datetime(2026, 11, 2, 10, 0, tzinfo=NY)
    sun_night = news_item("TSLA", "Trading halted in TSLA", datetime(2026, 11, 1, 23, 30,
                                                                     tzinfo=NY))
    assert halt([sun_night], mon)["status"] == "CLEAR"
    early = news_item("TSLA", "Trading halted in TSLA", datetime(2026, 11, 2, 0, 30, tzinfo=NY))
    assert halt([early], mon)["status"] == "EVENT"


# ======================= an unreadable timestamp still blocks =======================

@pytest.mark.parametrize("created_at", [None, "", "yesterday", "2026-11-06T18:00:00",
                                        "2026-13-40T10:00:00Z", 1730930400],
                         ids=["missing", "empty", "text", "naive", "impossible", "number"])
def test_blocked_an_item_with_an_unreadable_timestamp_still_makes_the_halt_check_unknown(
        created_at):
    item = friday(headline=None, created_at=created_at)
    h = halt([item])
    assert h["status"] == "UNKNOWN"
    assert h["findings"] == [{"check": "halt", "source": "news", "status": "UNKNOWN",
                              "reason": "1 news item(s) unreadable: cannot rule out a halt "
                                        "today"}]
    gate = gate_for("TSLA", MON, news=news_ok([item]))
    assert gate["status"] == "UNKNOWN" and verdict_for(gate, MON)["verdict"] == "BLOCK"


@pytest.mark.parametrize("item", [None, "TSLA halted", ["x"]], ids=["none", "string", "list"])
def test_blocked_an_item_that_is_not_an_object_still_blocks(item):
    assert halt([item])["status"] == "UNKNOWN"


def test_blocked_a_malformed_item_from_today_still_blocks():
    item = news_item("TSLA", "x", MON - timedelta(hours=2))
    item["headline"] = None
    assert halt([item])["status"] == "UNKNOWN"


def test_blocked_a_malformed_item_from_after_now_still_blocks():
    item = news_item("TSLA", "x", MON + timedelta(minutes=5))
    item["symbols"] = []
    assert halt([item])["status"] == "UNKNOWN"


# ======================= today's halts still flag =======================

def test_blocked_a_halt_headline_from_this_morning_still_flags():
    item = news_item("TSLA", "Trading halted in TSLA pending news",
                     datetime(2026, 11, 9, 7, 0, tzinfo=NY))
    h = halt([item])
    assert h["status"] == "EVENT"
    assert "trading halt today: 'Trading halted in TSLA pending news'" in h["findings"][0][
        "reason"]
    gate = gate_for("TSLA", MON, news=news_ok([item]))
    assert gate["status"] == "EVENT" and verdict_for(gate, MON)["verdict"] == "BLOCK"


def test_a_halt_this_morning_still_flags_next_to_skipped_old_items():
    items = [friday(headline=None), friday(symbols=[]),
             news_item("TSLA", "Trading halted in TSLA", datetime(2026, 11, 9, 9, 45, tzinfo=NY))]
    h = halt(items)
    assert h["status"] == "EVENT" and len(h["findings"]) == 1


# ======================= the earnings-news window (#169) is unchanged =======================

def test_blocked_fridays_evening_earnings_headline_still_counts_on_monday():
    item = news_item("TSLA", "Tesla to report third-quarter earnings Monday", FRI_EVENING)
    gate = gate_for("TSLA", MON, news=news_ok([item]))
    assert gate["status"] == "EVENT"
    assert "EVENT (news): earnings headline in the news" in gate["reason"]
    assert gate["halt"]["status"] == "CLEAR"


# ======================= review: timestamps at the edge of the date range =======================

@pytest.mark.parametrize("created_at", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"],
                         ids=["year-1", "year-9999"])
@pytest.mark.parametrize("headline", [None, "Trading halted in TSLA"], ids=["malformed", "halt"])
def test_blocked_an_overflowing_timestamp_is_unreadable_not_an_exception(created_at, headline):
    item = friday(headline=headline, created_at=created_at)
    h = halt([item])
    assert h["status"] == "UNKNOWN"
    assert "1 news item(s) unreadable" in h["findings"][0]["reason"]
    gate = gate_for("TSLA", MON, news=news_ok([item]))
    assert gate["status"] == "UNKNOWN" and verdict_for(gate, MON)["verdict"] == "BLOCK"


def test_a_far_past_but_parseable_timestamp_is_trusted_and_skipped():
    """The accepted trade-off (#182): a parseable timestamp is trusted, as it already was for
    readable items; the Nasdaq halt feed still covers a real halt."""
    assert halt([friday(headline=None, created_at="1970-01-01T00:00:00Z")])["status"] == "CLEAR"
