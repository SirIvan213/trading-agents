"""#169: the event gate's news earnings signal. A headline adds EVENT only when one clause of
it names the company (SEC or Alpha Vantage name, or ticker) AND a report-event phrase, and it
was published since the previous regular session's close. Halt matching is unchanged. Offline;
no live calls."""
from datetime import date, datetime, timedelta

import pytest

from tests.fakes import av_csv
from tests.order_helpers import (
    NY,
    calendar_ok,
    filings_ok,
    gate_for,
    halts_ok,
    news_item,
    news_ok,
    submissions_202,
    verdict_for,
)
from tools.earnings_calendar import parse_av_calendar
from tools.event_gate import (
    NEWS_FALLBACK_WINDOW,
    company_names,
    halt_status,
    news_earnings_headlines,
    previous_session_close,
)
from tools.news import earnings_event_headline, headline_flags, name_forms

NOW = datetime(2026, 11, 9, 10, 30, tzinfo=NY)  # a Monday
WIN = NOW.replace(hour=15, minute=55)

# The live headline from #169: a market roundup tagged with many symbols.
DOW_ROUNDUP = "Dow Jumps Over 600 Points; Constellation Energy Posts Upbeat Earnings"


# ======================= names =======================

@pytest.mark.parametrize("name,forms", [
    ("Apple Inc.", [("apple",)]),
    ("OneMain Holdings, Inc.", [("onemain",)]),
    ("Ohmyhome Ltd", [("ohmyhome",)]),
    ("TURKCELL ILETISIM HIZMETLERI A S", [("turkcell", "iletisim", "hizmetleri", "a", "s"),
                                         ("turkcell",)]),
    ("TESLA, INC.", [("tesla",)]),
    ("Constellation Energy Corp", [("constellation", "energy"), ("constellation",)]),
    ("General Motors Co", [("general", "motors")]),        # "general" alone is too common
    ("First Solar, Inc.", [("first", "solar")]),
    ("The Walt Disney Company", [("walt", "disney"), ("walt",)]),
    ("Johnson & Johnson", [("johnson", "and", "johnson"), ("johnson",)]),
    ("Inc.", []),
    (None, []),
])
def test_name_forms(name, forms):
    assert name_forms(name) == forms


# ======================= the matcher =======================

YES = [
    ("Apple to report fourth-quarter earnings on Thursday", ["AAPL"], ["Apple Inc."]),
    ("Apple reports Q4 results, iPhone sales rise", ["AAPL"], ["Apple Inc."]),
    ("Tesla Q3 earnings beat estimates", ["TSLA"], ["Tesla, Inc."]),
    ("TSLA slips ahead of earnings call", ["TSLA"], []),
    ("Microsoft posts record quarterly profit", ["MSFT"], ["MICROSOFT CORP"]),
    ("OneMain misses analyst expectations", ["OMF"], ["OneMain Holdings, Inc."]),
    ("OneMain Holdings to announce third quarter 2026 results", ["OMF"],
     ["OneMain Holdings, Inc."]),
    ("General Motors reports third-quarter results", ["GM"], ["General Motors Co"]),
    ("Berkshire posts quarterly earnings ($BRK.B)", ["BRK.B"], []),
    ("ON Semiconductor misses estimates", ["ON"], ["ON Semiconductor Corp"]),
    ("Quarterly results due from $ON after the close", ["ON"], []),
    ("Tesla earnings release set for Wednesday", ["TSLA"], ["Tesla, Inc."]),
    ("Walt Disney fiscal 2026 results top forecasts", ["DIS"], ["The Walt Disney Company"]),
]
NO = [
    # #169's live roundup: another company's report, tagged with OMH (Ohmyhome)
    (DOW_ROUNDUP, ["OMH"], ["Ohmyhome Ltd"]),
    # a story naming the company with the bare word only
    ("Apple faces EU tax ruling, earnings in focus", ["AAPL"], ["Apple Inc."]),
    ("Big earnings week ahead for tech", ["AAPL"], ["Apple Inc."]),
    # another company's report in another clause of a roundup
    ("Apple, Microsoft gain; Tesla reports Q3 results", ["AAPL"], ["Apple Inc."]),
    # a report phrase without the company
    ("Chipmaker reports record Q3 results", ["AAPL"], ["Apple Inc."]),
    # a 1-2 letter ticker as a plain word does not count
    ("Stocks ON the move as earnings season reports beat estimates", ["ON"], []),
    # a common first word alone is not the company
    ("General Mills reports quarterly results", ["GM"], ["General Motors Co"]),
    # no name known and no ticker in the headline
    ("Other Corp posts earnings beat", ["OTHR"], []),
    ("", ["AAPL"], ["Apple Inc."]),
]


@pytest.mark.parametrize("headline,tickers,names", YES, ids=[y[0][:40] for y in YES])
def test_a_company_report_headline_counts(headline, tickers, names):
    assert earnings_event_headline(headline, tickers, names) is True


@pytest.mark.parametrize("headline,tickers,names", NO, ids=[n[0][:40] or "empty" for n in NO])
def test_not_the_company_reporting(headline, tickers, names):
    assert earnings_event_headline(headline, tickers, names) is False


def test_the_digest_flag_for_the_analyst_is_unchanged():
    """The analyst's digest still flags the bare word; only the gate's signal is narrowed."""
    assert "earnings" in headline_flags(DOW_ROUNDUP)


# ======================= the gate signal =======================

def sources(news, *, symbol="OMH", calendar=None, filings=None):
    return {"news": news_ok(news), "filings": filings or filings_ok(NOW, symbol=symbol),
            "calendar": calendar, "fmp_calendar": None}


def test_blocked_the_live_roundup_no_longer_flags_omh():
    item = news_item("OMH", DOW_ROUNDUP, NOW - timedelta(hours=1))
    assert news_earnings_headlines("OMH", NOW, WIN, sources([item])) == []
    gate = gate_for("OMH", NOW, news=news_ok([item]))
    assert gate["status"] == "CLEAR", gate["reason"]


def test_blocked_the_companys_own_report_still_blocks():
    item = news_item("OMF", "OneMain Holdings to report third-quarter results after the bell",
                     NOW - timedelta(hours=1))
    gate = gate_for("OMF", NOW, news=news_ok([item]))
    assert gate["status"] == "EVENT"
    assert ("EVENT (news): earnings headline in the news: 'OneMain Holdings to report "
            "third-quarter results after the bell'") in gate["reason"]
    assert verdict_for(gate, NOW, "OMF")["verdict"] == "BLOCK"


def test_the_window_opens_at_the_previous_session_close():
    """NOW is Monday 10:30: Friday 16:00 is the last close. A weekend story counts; Friday's
    15:59 story does not; 16:01 does."""
    assert previous_session_close(NOW) == datetime(2026, 11, 6, 16, 0, tzinfo=NY)
    for when, count in [(datetime(2026, 11, 6, 15, 59, tzinfo=NY), 0),
                        (datetime(2026, 11, 6, 16, 1, tzinfo=NY), 1),
                        (datetime(2026, 11, 8, 18, 0, tzinfo=NY), 1)]:
        item = news_item("OMF", "OneMain reports Q3 results", when)
        got = news_earnings_headlines("OMF", NOW, WIN, sources([item], symbol="OMF"))
        assert len(got) == count, when


def test_blocked_an_evening_preview_still_counts_the_next_morning():
    """Ivan's case: Wednesday 18:00 "to report ... Thursday" blocks on Thursday."""
    thu = datetime(2026, 11, 12, 10, 0, tzinfo=NY)
    item = news_item("OMF", "OneMain to report Q3 earnings Thursday",
                     datetime(2026, 11, 11, 18, 0, tzinfo=NY))
    src = sources([item], symbol="OMF")
    assert len(news_earnings_headlines("OMF", thu, thu.replace(hour=15, minute=55), src)) == 1


def test_after_todays_close_the_window_starts_at_todays_close():
    evening = datetime(2026, 11, 9, 17, 0, tzinfo=NY)
    assert previous_session_close(evening) == datetime(2026, 11, 9, 16, 0, tzinfo=NY)
    morning = news_item("OMF", "OneMain reports Q3 results", datetime(2026, 11, 9, 9, 0,
                                                                         tzinfo=NY))
    assert news_earnings_headlines("OMF", evening, evening, sources([morning],
                                                                   symbol="OMF")) == []


def test_an_early_close_and_a_holiday_set_the_window():
    """Monday 2026-11-30: the last session is Friday 11-27, an early close (13:00)."""
    mon = datetime(2026, 11, 30, 10, 0, tzinfo=NY)
    assert previous_session_close(mon) == datetime(2026, 11, 27, 13, 0, tzinfo=NY)
    # Friday 2026-11-27 morning: Thursday is Thanksgiving, so Wednesday 16:00
    fri = datetime(2026, 11, 27, 9, 0, tzinfo=NY)
    assert previous_session_close(fri) == datetime(2026, 11, 25, 16, 0, tzinfo=NY)


@pytest.mark.parametrize("now,close", [
    # exactly at a close: that close has not ended "before now", so the earlier one (wider)
    (datetime(2026, 11, 9, 16, 0, tzinfo=NY), datetime(2026, 11, 6, 16, 0, tzinfo=NY)),
    (datetime(2026, 11, 9, 16, 0, 0, 1, tzinfo=NY), datetime(2026, 11, 9, 16, 0, tzinfo=NY)),
    # before the open
    (datetime(2026, 11, 10, 7, 0, tzinfo=NY), datetime(2026, 11, 9, 16, 0, tzinfo=NY)),
    # across the clock changes: Friday's close keeps its own offset (EDT, then EST)
    (datetime(2026, 11, 2, 10, 0, tzinfo=NY), datetime(2026, 10, 30, 16, 0, tzinfo=NY)),
    (datetime(2026, 3, 9, 10, 0, tzinfo=NY), datetime(2026, 3, 6, 16, 0, tzinfo=NY)),
    # across a year end, both years covered
    (datetime(2027, 1, 4, 10, 0, tzinfo=NY), datetime(2026, 12, 31, 16, 0, tzinfo=NY)),
], ids=["at-close", "just-after-close", "pre-open", "after-dst-end", "after-dst-start",
        "year-end"])
def test_previous_session_close_boundaries(now, close):
    got = previous_session_close(now)
    assert got == close
    assert got.utcoffset() == close.utcoffset()


def test_previous_session_close_utc_offsets_across_the_clock_change():
    from datetime import UTC
    assert previous_session_close(datetime(2026, 11, 2, 15, 0, tzinfo=UTC)).astimezone(
        UTC) == datetime(2026, 10, 30, 20, 0, tzinfo=UTC)  # 16:00 EDT
    assert previous_session_close(datetime(2026, 3, 9, 15, 0, tzinfo=UTC)).astimezone(
        UTC) == datetime(2026, 3, 6, 21, 0, tzinfo=UTC)  # 16:00 EST


def test_a_walk_back_into_an_uncovered_year_falls_back():
    early = datetime(2026, 1, 2, 10, 0, tzinfo=NY)  # 2025 is not covered
    assert previous_session_close(early) == early - NEWS_FALLBACK_WINDOW


def test_gate_inputs_fetches_news_back_to_the_fallback_in_an_uncovered_year():
    from types import SimpleNamespace

    from pipeline.events import gate_inputs
    late = datetime(2031, 3, 4, 10, 0, tzinfo=NY)
    seen = []
    data = SimpleNamespace(news=lambda syms, a, b: seen.append((a, b)) or {"news": []},
                           submissions=lambda s: None)
    gate_inputs(data, "TSLA", late, calendar={"ok": False}, halts={"ok": False},
                fmp_calendar={"ok": False})
    assert seen == [(late - NEWS_FALLBACK_WINDOW, late)]


def test_an_uncovered_year_falls_back_to_a_wide_window():
    late = datetime(2031, 3, 4, 10, 0, tzinfo=NY)
    assert previous_session_close(late) == late - NEWS_FALLBACK_WINDOW


def test_the_alpha_vantage_name_is_used_when_sec_has_none():
    cal = parse_av_calendar(av_csv([("OTHR", "2026-12-01")], start=date(2026, 11, 9),
                                   end=date(2026, 11, 16)), NOW, date(2026, 11, 9),
                            date(2026, 11, 16))
    assert cal["names"]["OTHR"] == "OTHR Inc"
    item = news_item("OTHR", "OTHR Inc posts earnings beat", NOW - timedelta(hours=1))
    filings = {"ok": True, "submissions": None}
    src = sources([item], symbol="OTHR", calendar=cal, filings=filings)
    assert company_names("OTHR", src) == ["OTHR Inc"]
    assert len(news_earnings_headlines("OTHR", NOW, WIN, src)) == 1


def test_a_share_class_sibling_ticker_counts():
    subs = submissions_202(NOW, 30, symbol="GOOGL")
    subs["tickers"] = ["GOOGL", "GOOG"]
    subs["name"] = "Alphabet Inc."
    item = news_item("GOOGL", "GOOG to report Q3 earnings Tuesday", NOW - timedelta(hours=1))
    src = sources([item], symbol="GOOGL", filings={"ok": True, "submissions": subs})
    assert len(news_earnings_headlines("GOOGL", NOW, WIN, src)) == 1


def test_a_failed_news_fetch_still_adds_nothing():
    src = {"news": {"ok": False, "reason": "x"}, "filings": filings_ok(NOW, symbol="OMH")}
    assert news_earnings_headlines("OMH", NOW, WIN, src) == []


def test_the_signal_never_clears_an_unknown_primary():
    item = news_item("OMH", DOW_ROUNDUP, NOW - timedelta(hours=1))
    gate = gate_for("OMH", NOW, news=news_ok([item]),
                    calendar={"ok": False, "source": "alphavantage", "reason": "x"})
    assert gate["status"] == "UNKNOWN"


# ======================= halt matching is unchanged =======================

@pytest.mark.parametrize("headline,status", [
    ("Trading halted in OMH pending news", "EVENT"),
    (DOW_ROUNDUP, "CLEAR"),
])
def test_halt_headline_matching_is_unchanged(headline, status):
    from tests.order_helpers import listing_ok
    item = news_item("OMH", headline, NOW - timedelta(hours=1))
    assert halt_status("OMH", NOW, news_ok([item]), halts_ok(NOW),
                       listing_ok("OMH"))["status"] == status


def test_calendar_ok_still_clear_with_names_absent():
    """An FMP-parsed primary has no names map: the signal falls back to SEC and tickers."""
    assert "names" not in calendar_ok(NOW)
    assert gate_for("OMH", NOW)["status"] == "CLEAR"


# ======================= review fixes =======================

def test_blocked_a_headline_repeated_from_before_the_close_still_counts():
    """The digest keeps the earliest copy of a repeated headline (here Friday's, before the
    window); the signal scans every item, so today's copy counts."""
    same = "OneMain to report third-quarter results today"
    items = [news_item("OMF", same, datetime(2026, 11, 6, 15, 0, tzinfo=NY)),
             news_item("OMF", same, datetime(2026, 11, 9, 7, 0, tzinfo=NY))]
    assert news_earnings_headlines("OMF", NOW, WIN, sources(items, symbol="OMF")) == [
        f"earnings headline in the news: {same!r}"]


def test_a_headline_after_now_is_ignored():
    item = news_item("OMF", "OneMain reports Q3 results", NOW + timedelta(minutes=10))
    assert news_earnings_headlines("OMF", NOW, WIN, sources([item], symbol="OMF")) == []


MORE_YES = [
    ("AT&T reports Q3 results", ["T"], ["AT&T INC."]),
    ("M&T Bank reports third-quarter earnings", ["MTB"], ["M&T BANK CORP"]),
    ("McDonald's Q3 earnings beat", ["MCD"], ["MCDONALDS CORP"]),
    ("Kohl’s Q3 earnings top estimates", ["KSS"], ["KOHLS Corp"]),
    ("Apple's earnings beat estimates", ["AAPL"], ["Apple Inc."]),
    ("Acme Announces Fourth Quarter and Fiscal Year 2026 Financial Results", ["ACME"],
     ["Acme Corp"]),
    ("Acme Announces Financial Results for Third Quarter 2026", ["ACME"], ["Acme Corp"]),
    ("Acme Announces Results for the Third Quarter of Fiscal 2026", ["ACME"], ["Acme Corp"]),
    ("Apple earnings: iPhone sales top forecasts", ["AAPL"], ["Apple Inc."]),  # "top forecasts"
    ("Bank of America reports Q3 results", ["BAC"], ["BANK OF AMERICA CORP /DE/"]),
    ("Bank of New York Mellon Q3 earnings beat", ["BK"],
     ["Bank of New York Mellon Corp"]),
    ("eBay Q3 earnings beat", ["EBAY"], ["EBAY INC"]),
    ("iRobot to report Q3 results", ["IRBT"], ["IROBOT CORP"]),
    ("Pfizer Reports Third-Quarter 2026 Results", ["PFE"], ["PFIZER INC"]),
    ("Tesla stock falls after earnings", ["TSLA"], ["Tesla, Inc."]),
    ("Tesla results due Wednesday", ["TSLA"], ["Tesla, Inc."]),
    ("Acme Adj EPS $1.64 Beats $1.60 Estimate", ["ACME"], ["Acme Corp"]),
    ("Acme Earnings Lag Estimates", ["ACME"], ["Acme Corp"]),
    ("Nasdaq: ON shares slip after Q3 results", ["ON"], []),
    ("NYSE American: XYZ to report quarterly results", ["XYZ"], []),
]
MORE_NO = [
    ("Tesla stock posts biggest loss since March", ["TSLA"], ["Tesla, Inc."]),
    ("Analysts lower target ahead of Walmart Q3 earnings", ["TGT"], ["TARGET CORP"]),
    ("news corp rivals report Q3 results", ["NWSA"], ["NEWS CORP"]),
    ("Apple stock rises on earnings optimism", ["AAPL"], ["Apple Inc."]),
    ("Apple: the stock to own after earnings season", ["AAPL"], ["Apple Inc."]),
    ("Microsoft earnings: what it means for Apple", ["AAPL"], ["Apple Inc."]),
    ("Pfizer Announces Positive Topline Results from Phase 3 Trial", ["PFE"], ["PFIZER INC"]),
    ("Moderna Reports Positive Results From Late-Stage Trial", ["MRNA"], ["Moderna, Inc."]),
    ("Pfizer announces results of shareholder vote", ["PFE"], ["PFIZER INC"]),
    ("Tesla to report results of recall probe", ["TSLA"], ["Tesla, Inc."]),
]


@pytest.mark.parametrize("headline,tickers,names", MORE_YES, ids=[y[0][:40] for y in MORE_YES])
def test_review_wordings_count(headline, tickers, names):
    assert earnings_event_headline(headline, tickers, names) is True


@pytest.mark.parametrize("headline,tickers,names", MORE_NO, ids=[n[0][:40] for n in MORE_NO])
def test_review_false_positives_do_not_count(headline, tickers, names):
    assert earnings_event_headline(headline, tickers, names) is False


def test_av_names_edge_cases():
    start, end = date(2026, 11, 9), date(2026, 11, 16)
    no_col = "symbol,reportDate\nAAA,2026-11-10\nBBB,2026-12-01\n"
    assert parse_av_calendar(no_col, NOW, start, end)["names"] == {}
    text = ("symbol,name,reportDate\nAAA,,2026-11-10\nBBB,First Name,2026-11-11\n"
            "BBB,Second Name,2026-12-02\nCCC," + "X" * 300 + ",2026-12-01\n")
    names = parse_av_calendar(text, NOW, start, end)["names"]
    assert "AAA" not in names and names["BBB"] == "First Name"
    assert names["CCC"] == "X" * 120


def test_company_names_uses_dash_keyed_av_names_and_skips_a_failed_calendar():
    subs = submissions_202(NOW, 30, symbol="BRK.B")
    subs["name"] = "BERKSHIRE HATHAWAY INC"
    filings = {"ok": True, "submissions": subs}
    cal = {"ok": True, "names": {"BRK-B": "Berkshire Hathaway Inc"}}
    assert company_names("BRK.B", {"filings": filings, "calendar": cal}) == [
        "BERKSHIRE HATHAWAY INC", "Berkshire Hathaway Inc"]
    failed = {"ok": False, "names": {"BRK-B": "Something Else"}}
    assert company_names("BRK.B", {"filings": filings, "calendar": failed}) == [
        "BERKSHIRE HATHAWAY INC"]
    same = {"ok": True, "names": {"BRK.B": "BERKSHIRE HATHAWAY INC"}}
    assert company_names("BRK.B", {"filings": filings, "calendar": same}) == [
        "BERKSHIRE HATHAWAY INC"]


# ======================= Ivan's table of real headlines (#169) =======================
# (headline, symbol, expected earnings flag). Names come from tests.order_helpers.FILER_NAMES:
# OMH is Ohmyhome Ltd, CEG Constellation Energy Corp, TKC Turkcell (SEC name
# "TURKCELL ILETISIM HIZMETLERI A S"), VNTR Ventara Inc.

IVAN_TABLE = [
    ("Apple Discloses Tax Savings from Earnings Allocation to Singapore", "AAPL", False),
    ("Dow Jumps Over 600 Points; Constellation Energy Posts Upbeat Earnings", "OMH", False),
    ("Dow Jumps Over 600 Points; Constellation Energy Posts Upbeat Earnings", "CEG", True),
    ("Turkcell Iletisim Q3 2026 Earnings Call Transcript", "TKC", True),
    ("Ventara to report third-quarter earnings at 1:00 p.m. ET today", "VNTR", True),
    ("Ohmyhome Shares Halted On Circuit Breaker To The Upside, Stock Now Up 971.06%", "OMH",
     False),
]


@pytest.mark.parametrize("headline,symbol,flag", IVAN_TABLE,
                         ids=[f"{s}-{h[:30]}" for h, s, _ in IVAN_TABLE])
def test_ivan_table_through_the_gate(headline, symbol, flag):
    from tests.order_helpers import listing_ok
    item = news_item(symbol, headline, NOW - timedelta(hours=1))
    src = sources([item], symbol=symbol)
    got = news_earnings_headlines(symbol, NOW, WIN, src)
    assert got == ([f"earnings headline in the news: {headline!r}"] if flag else [])
    gate = gate_for(symbol, NOW, news=news_ok([item]), listing=listing_ok(symbol))
    assert ("EVENT (news): earnings headline" in gate["reason"]) is flag


def test_ivan_table_ohmyhome_halt_headline_still_flags_the_halt():
    from tests.order_helpers import listing_ok
    headline = IVAN_TABLE[-1][0]
    item = news_item("OMH", headline, NOW - timedelta(hours=1))
    halt = halt_status("OMH", NOW, news_ok([item]), halts_ok(NOW), listing_ok("OMH"))
    assert halt["status"] == "EVENT"
    gate = gate_for("OMH", NOW, news=news_ok([item]), listing=listing_ok("OMH"))
    assert gate["status"] == "EVENT" and "EVENT (news): trading halt today" in gate["reason"]
    assert "earnings headline" not in gate["reason"]


def test_ivan_table_old_vs_new_verdicts():
    """The old rule flagged every row with the bare word "earnings"; two of those change."""
    old = ["earnings" in headline_flags(h) for h, _, _ in IVAN_TABLE]
    assert old == [True, True, True, True, True, False]
    assert [flag for _, _, flag in IVAN_TABLE] == [False, False, True, True, True, False]
