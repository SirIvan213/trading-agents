"""tools.event_gate: every way the earnings and halt gate can end, judged through check_order
(#112).

Rule for each case: the gate status is the one named, its reason carries the distinct text for
the cause, and check_order BLOCKs with that reason. Passing cases sit next to the blocked ones
at each boundary. Inputs are small hand-built dicts plus the saved FMP fixtures; no network.
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tests.order_helpers import (
    NY,
    calendar_ok,
    filings_ok,
    fmp_row,
    gate_for,
    news_item,
    news_ok,
    submissions_202,
    verdict_for,
)
from tools.event_gate import (
    CALENDAR_HORIZON_DAYS,
    COVERAGE_DAYS,
    ESTIMATE_TRADING_DAYS,
    MAX_CALENDAR_AGE,
    SECONDARY_EARNINGS_SIGNALS,
    earnings_status,
    event_gate,
    halt_status,
)

NOW = datetime(2026, 11, 9, 10, 30, tzinfo=NY)  # a Monday, in the entry window
FIXTURES = Path(__file__).parent / "fixtures" / "fmp"


def fx(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def run(now=NOW, *, rows=(), symbol="TSLA", filings=None, news=None, calendar=None, **kw):
    """(gate, check_order verdict) for TSLA with a calendar made from `rows`."""
    gate = gate_for(symbol, now, calendar=calendar if calendar is not None
                    else calendar_ok(now, rows), filings=filings, news=news, **kw)
    return gate, verdict_for(gate, now, symbol)


def assert_blocked(gate, verdict, status, *needles):
    assert gate["status"] == status, gate["reason"]
    assert verdict["verdict"] == "BLOCK"
    reasons = [r for r in verdict["reasons"] if r.startswith(f"event gate {status}: ")]
    assert len(reasons) == 1, verdict["reasons"]
    for n in needles:
        assert n in reasons[0], (n, reasons[0])
    # the other kind of status never appears for this block
    other = "UNKNOWN" if status == "EVENT" else "EVENT"
    assert not any(r.startswith(f"event gate {other}") for r in verdict["reasons"])


def assert_clear(gate, verdict):
    assert gate["status"] == "CLEAR", gate["reason"]
    assert verdict == {"verdict": "PASS", "reasons": []}


# ============================ the passing baseline ============================

def test_clear_needs_fmp_proof_sec_coverage_and_no_halt_and_passes_check_order():
    gate, v = run(rows=fx("earnings_calendar_others.json"))
    assert_clear(gate, v)
    assert gate["findings"] == []
    assert gate["symbol"] == "TSLA" and gate["checked_at"] == NOW.isoformat()
    assert "no earnings through 2026-11-23 (FMP)" in gate["reason"]
    assert "last 8-K item 2.02 filed 2026-10-10" in gate["reason"]
    assert "no halt today" in gate["reason"]


def test_constants_are_the_documented_ones():
    assert (COVERAGE_DAYS, ESTIMATE_TRADING_DAYS, CALENDAR_HORIZON_DAYS) == (100, 3, 7)
    assert MAX_CALENDAR_AGE == timedelta(hours=20)


# ============================ FMP: earnings today ============================

@pytest.mark.parametrize("name,needle", [
    ("earnings_calendar_tsla_today_amc.json", "(amc, confirmed)"),
    ("earnings_calendar_tsla_today_bmo.json", "(bmo, estimated)"),
    ("earnings_calendar_tsla_today_no_time.json", "(no report time given, confirmed)"),
])
def test_blocked_earnings_dated_today_is_an_event_whatever_the_hour(name, needle):
    gate, v = run(rows=fx(name))
    assert_blocked(gate, v, "EVENT", "EVENT (fmp): earnings today 2026-11-09", needle)


def test_blocked_bmo_today_is_still_an_event_even_after_the_morning_report():
    late = NOW.replace(hour=14)  # well after a before-market-open report
    gate, v = run(late, rows=[fmp_row("TSLA", "2026-11-09", "bmo", True)])
    assert_blocked(gate, v, "EVENT", "earnings today", "(bmo, confirmed)")


def test_blocked_estimated_date_today():
    gate, v = run(rows=[fmp_row("TSLA", "2026-11-09", "amc", False)])
    assert_blocked(gate, v, "EVENT", "earnings today 2026-11-09 (amc, estimated)")


def test_blocked_a_row_with_the_symbol_in_lower_case_still_matches():
    gate, v = run(rows=[{**fmp_row("TSLA", "2026-11-09", "amc", True), "symbol": " tsla"}])
    assert_blocked(gate, v, "EVENT", "earnings today")


def test_another_company_reporting_today_does_not_block():
    gate, v = run(rows=[fmp_row("AAPL", "2026-11-09", "amc", True)])
    assert_clear(gate, v)


# ============================ FMP: estimated dates move ============================

# (now, estimated date, expected EVENT): 3 trading days after `now` is the last blocked day
ESTIMATES = [
    # plain week: Mon 2026-11-09 -> Tue 10, Wed 11, Thu 12 are trading days 1..3, Fri 13 is 4
    (NOW, "2026-11-10", True), (NOW, "2026-11-11", True), (NOW, "2026-11-12", True),
    (NOW, "2026-11-13", False),
    # across a weekend: Thu 11-19 -> Fri 20 (1), Mon 23 (2), Tue 24 (3); Wed 25 is 4
    (datetime(2026, 11, 19, 10, 30, tzinfo=NY), "2026-11-20", True),
    (datetime(2026, 11, 19, 10, 30, tzinfo=NY), "2026-11-23", True),
    (datetime(2026, 11, 19, 10, 30, tzinfo=NY), "2026-11-24", True),
    (datetime(2026, 11, 19, 10, 30, tzinfo=NY), "2026-11-25", False),
    # across a weekend and Thanksgiving (Thu 2026-11-26): Tue 11-24 -> Wed 25 (1), Fri 27 (2),
    # Mon 30 (3); Tue Dec 1 is 4
    (datetime(2026, 11, 24, 10, 30, tzinfo=NY), "2026-11-25", True),
    (datetime(2026, 11, 24, 10, 30, tzinfo=NY), "2026-11-27", True),
    (datetime(2026, 11, 24, 10, 30, tzinfo=NY), "2026-11-30", True),
    (datetime(2026, 11, 24, 10, 30, tzinfo=NY), "2026-12-01", False),
]


@pytest.mark.parametrize("now,day,blocked", ESTIMATES, ids=[f"{n:%m-%d}->{d}" for n, d,
    _ in ESTIMATES])
def test_estimated_date_within_three_trading_days(now, day, blocked):
    gate, v = run(now, rows=[fmp_row("TSLA", day, "amc", False)])
    if blocked:
        assert_blocked(gate, v, "EVENT", f"estimated earnings date {day} within 3 trading days",
                       "(estimated dates move)")
    else:
        assert_clear(gate, v)


def test_the_holiday_really_is_skipped_so_the_second_day_is_friday():
    # Thu 2026-11-26 is Thanksgiving: an estimate on it would not be a trading day, but the
    # window still counts it as inside the three days (2 trading days away: Wed, Fri).
    now = datetime(2026, 11, 24, 10, 30, tzinfo=NY)
    gate, v = run(now, rows=[fmp_row("TSLA", "2026-11-26", "bmo", False)])
    assert_blocked(gate, v, "EVENT", "estimated earnings date 2026-11-26")


def test_confirmed_date_two_days_ahead_outside_the_window_is_clear():
    gate, v = run(rows=fx("earnings_calendar_tsla_confirmed_in_two_days.json"))
    assert_clear(gate, v)


def test_blocked_the_same_date_estimated_is_an_event():
    gate, v = run(rows=fx("earnings_calendar_tsla_estimated_in_two_days.json"))
    assert_blocked(gate, v, "EVENT", "estimated earnings date 2026-11-11")


def test_a_confirmed_row_far_ahead_and_an_estimated_one_beyond_three_days_are_clear():
    rows = [fmp_row("TSLA", "2026-11-18", "amc", True), fmp_row("TSLA", "2026-11-23", "bmo", False)]
    assert_clear(*run(rows=rows))


def test_blocked_a_confirmed_row_inside_a_window_that_runs_past_today():
    # a window ending tomorrow 15:55 (not the intraday default) makes tomorrow's report an event
    end = (NOW + timedelta(days=1)).replace(hour=15, minute=55)
    gate, v = run(rows=[fmp_row("TSLA", "2026-11-10", "amc", True)], window_end=end)
    assert_blocked(gate, v, "EVENT", "earnings 2026-11-10 inside the trade window (amc, confirmed)")


# ============================ FMP: stale, future and short replies ============================

def test_blocked_a_calendar_fetched_yesterday_is_stale():
    cal = calendar_ok(NOW, fetched_at=NOW - timedelta(days=1))
    gate, v = run(calendar=cal)
    assert_blocked(gate, v, "UNKNOWN",
                   "UNKNOWN (fmp): earnings calendar is stale (fetched 2026-11-08 10:30 ET)")


def test_blocked_a_calendar_fetched_21_hours_ago_is_stale():
    late = NOW.replace(hour=23, minute=0)  # 21 h before is 02:00 the same New York day
    cal = calendar_ok(late, fetched_at=late - timedelta(hours=21))
    gate, v = run(late, calendar=cal)
    assert gate["status"] == "UNKNOWN"
    stale = "UNKNOWN (fmp): earnings calendar is stale (fetched 2026-11-09 02:00 ET)"
    assert stale in gate["reason"]
    assert v["verdict"] == "BLOCK"
    assert any(r.startswith("event gate UNKNOWN: ") for r in v["reasons"])


def test_a_calendar_just_inside_20_hours_is_fresh_at_the_gate():
    late = NOW.replace(hour=23, minute=0)
    cal = calendar_ok(late, fetched_at=late - timedelta(hours=19, minutes=59))
    gate = gate_for("TSLA", late, calendar=cal)
    assert gate["status"] == "CLEAR", gate["reason"]


def test_blocked_a_calendar_exactly_20h_and_one_minute_old_is_stale():
    late = NOW.replace(hour=23, minute=30)
    cal = calendar_ok(late, fetched_at=late - timedelta(hours=20, minutes=1))
    assert gate_for("TSLA", late, calendar=cal)["status"] == "UNKNOWN"


def test_blocked_a_calendar_fetched_in_the_future_is_unknown():
    cal = calendar_ok(NOW, fetched_at=NOW + timedelta(minutes=10))
    gate, v = run(calendar=cal)
    assert_blocked(gate, v, "UNKNOWN", "UNKNOWN (fmp): earnings calendar is dated in the future")


def test_a_calendar_fetched_four_minutes_ahead_is_within_the_clock_skew():
    cal = calendar_ok(NOW, fetched_at=NOW + timedelta(minutes=4))
    assert_clear(*run(calendar=cal))


def test_blocked_a_calendar_that_starts_after_today_does_not_cover_today():
    cal = calendar_ok(NOW, start=NOW.date() + timedelta(days=1),
        end=NOW.date() + timedelta(days=14))
    gate, v = run(calendar=cal)
    assert_blocked(gate, v, "UNKNOWN",
        "UNKNOWN (fmp): earnings calendar covers 2026-11-10..2026-11-23, "
        "not 2026-11-09..2026-11-12")


def test_blocked_a_calendar_that_ends_before_the_third_trading_day_is_short():
    cal = calendar_ok(NOW, end=date(2026, 11, 11))  # third trading day is Thu 11-12
    gate, v = run(calendar=cal)
    assert_blocked(gate, v, "UNKNOWN",
        "earnings calendar covers 2026-11-09..2026-11-11, not 2026-11-09..2026-11-12")


def test_a_calendar_that_ends_exactly_on_the_third_trading_day_is_enough():
    assert_clear(*run(calendar=calendar_ok(NOW, end=date(2026, 11, 12))))


def test_blocked_a_calendar_must_reach_the_end_of_a_window_that_runs_past_the_horizon():
    end = NOW + timedelta(days=20)
    cal = calendar_ok(NOW, end=date(2026, 11, 23))
    gate, v = run(calendar=cal, window_end=end)
    assert_blocked(gate, v, "UNKNOWN",
        "earnings calendar covers 2026-11-09..2026-11-23, not 2026-11-09..2026-11-29")


def test_the_range_needed_skips_the_thanksgiving_holiday():
    now = datetime(2026, 11, 24, 10, 30, tzinfo=NY)
    short = calendar_ok(now, end=date(2026, 11, 27))  # a naive +3 calendar days would be enough
    assert gate_for("TSLA", now, calendar=short)["status"] == "UNKNOWN"
    assert gate_for("TSLA", now, calendar=calendar_ok(now, end=date(2026, 11,
        30)))["status"] == "CLEAR"


@pytest.mark.parametrize("calendar", [
    None, "garbage", {"ok": True}, {"ok": False}, {"ok": "yes", "rows": []},
    {"ok": True, "fetched_at": "x", "from": "2026-11-09", "to": "2026-11-23", "rows": []},
    {"ok": True, "fetched_at": NOW.isoformat(), "from": "soon", "to": "2026-11-23", "rows": []},
    {"ok": True, "fetched_at": NOW.isoformat(), "from": "2026-11-09", "to": "2026-11-23",
     "rows": "none"},
    {"ok": True, "fetched_at": NOW.replace(tzinfo=None).isoformat(), "from": "2026-11-09",
     "to": "2026-11-23", "rows": []},
])
def test_blocked_a_hand_forged_or_broken_calendar_input_is_unknown(calendar):
    gate = gate_for("TSLA", NOW, calendar=calendar if calendar is not None else {"ok": False})
    assert gate["status"] == "UNKNOWN"
    assert verdict_for(gate, NOW)["verdict"] == "BLOCK"


def test_blocked_a_failed_calendar_keeps_its_reason():
    gate, v = run(calendar={"ok": False, "source": "alphavantage",
                            "reason": "HTTP 429 from /query"})
    assert_blocked(gate, v, "UNKNOWN",
        "UNKNOWN (alphavantage): earnings source unavailable: HTTP 429 from /query")


def test_blocked_a_failed_calendar_without_a_source_is_labelled_calendar():
    gate, v = run(calendar={"ok": False, "reason": "x"})
    assert_blocked(gate, v, "UNKNOWN", "UNKNOWN (calendar): earnings source unavailable: x")


# ============================ SEC: an empty calendar proves nothing ============================

def test_blocked_no_item_202_in_100_days_means_fmp_silence_proves_nothing():
    gate, v = run(filings=filings_ok(NOW, days_ago=101))
    assert_blocked(gate, v, "UNKNOWN", "UNKNOWN (sec): no 8-K item 2.02 in the last 100 days",
                   "cannot prove TSLA reports quarterly")


def test_an_item_202_99_days_ago_covers_the_symbol():
    assert_clear(*run(filings=filings_ok(NOW, days_ago=99)))


def test_an_item_202_exactly_100_days_ago_still_covers_the_symbol():
    assert_clear(*run(filings=filings_ok(NOW, days_ago=100)))


def test_blocked_other_8k_items_do_not_count_as_coverage():
    gate, v = run(filings=filings_ok(NOW, days_ago=10, items="5.02,9.01"))
    assert_blocked(gate, v, "UNKNOWN", "no 8-K item 2.02")


def test_blocked_no_sec_filer_found():
    gate, v = run(filings={"ok": True, "submissions": None})
    assert_blocked(gate, v, "UNKNOWN", "UNKNOWN (sec): no SEC filer found for TSLA")


def test_blocked_sec_fetch_failure():
    gate, v = run(filings={"ok": False, "reason": "HTTP 503 from /submissions/CIK0000012345.json"})
    assert_blocked(gate, v, "UNKNOWN", "UNKNOWN (sec): SEC filings unavailable: HTTP 503 from")


@pytest.mark.parametrize("subs", [
    {"cik": "0000012345"},
    {"cik": "0000012345", "filings": {"recent": {"form": []}}},
    {"cik": "not-a-number", **{k: v for k, v in submissions_202(NOW).items() if k != "cik"}},
    "not a dict",
    [],
], ids=["no-filings", "missing-arrays", "bad-cik", "string", "list"])
def test_blocked_malformed_submissions(subs):
    gate, v = run(filings={"ok": True, "submissions": subs})
    assert_blocked(gate, v, "UNKNOWN", "UNKNOWN (sec): SEC filings are malformed")


def test_blocked_unequal_array_lengths_are_malformed():
    subs = submissions_202(NOW)
    subs["filings"]["recent"]["items"] = []
    assert_blocked(*run(filings={"ok": True, "submissions": subs}), "UNKNOWN",
                   "SEC filings are malformed")


@pytest.mark.parametrize("filings", [None, "x", {"ok": "yes", "submissions": {}}, {}])
def test_blocked_forged_filings_input(filings):
    gate = gate_for("TSLA", NOW, filings={"ok": False} if filings is None else filings)
    assert gate["status"] == "UNKNOWN"
    assert verdict_for(gate, NOW)["verdict"] == "BLOCK"


def test_blocked_an_item_202_filed_today_is_an_event_even_with_a_clear_calendar():
    gate, v = run(filings=filings_ok(NOW, days_ago=0.01))
    assert_blocked(gate, v, "EVENT",
        "EVENT (sec): 8-K item 2.02 (results of operations) filed today")


def test_an_item_202_dated_after_now_is_an_event_and_never_coverage():
    """#112 review: a 2.02 dated today or later (time estimated as 23:59:59 ET, clock skew, or
    forged) is EVENT; it never counts as the 100-day coverage proof."""
    subs = submissions_202(NOW, days_ago=-1)  # accepted tomorrow
    gate, v = run(filings={"ok": True, "submissions": subs})
    assert gate["status"] == "EVENT" and v["verdict"] == "BLOCK"
    reasons = [f["reason"] for f in gate["findings"] if f["source"] == "sec"]
    assert "8-K item 2.02 (results of operations) filed today" in reasons
    assert gate["earnings"]["last_item_202"] is None


# ============================ news: halts ============================

def test_blocked_a_halt_headline_today_is_an_event():
    item = news_item("TSLA", "Trading halted in TSLA pending news", NOW - timedelta(hours=1))
    gate, v = run(news=news_ok([item]))
    assert_blocked(gate, v, "EVENT",
        "EVENT (news): trading halt today: 'Trading halted in TSLA pending news'")
    assert gate["halt"]["status"] == "EVENT" and gate["earnings"]["status"] == "CLEAR"


def test_a_halt_headline_from_yesterday_is_not_a_halt_today():
    item = news_item("TSLA", "Trading halted in TSLA pending news", NOW - timedelta(hours=20))
    assert NOW.date() != (NOW - timedelta(hours=20)).date()
    assert_clear(*run(news=news_ok([item])))


def test_blocked_an_item_in_this_symbols_fetch_that_does_not_list_it_is_unknown():
    """#112 re-review: the news fetch is for TSLA alone, so an item listing only AAPL is a
    symbol-form mismatch we cannot read, not proof of "no halt"."""
    item = news_item("AAPL", "Trading halted in AAPL pending news", NOW - timedelta(hours=1))
    gate, v = run(news=news_ok([item]))
    assert gate["status"] == "UNKNOWN" and v["verdict"] == "BLOCK"
    assert "1 news item(s) unreadable: cannot rule out a halt today" in gate["reason"]


def test_a_look_alike_word_is_not_a_halt():
    item = news_item("TSLA", "TSLA shares exhalted by analysts as thalt study ends",
        NOW - timedelta(hours=1))
    assert_clear(*run(news=news_ok([item])))


def test_blocked_a_failed_news_fetch_is_unknown_never_no_halts():
    gate, v = run(news={"ok": False, "reason": "HTTP 500 from /v1beta1/news"})
    assert_blocked(gate, v, "UNKNOWN",
        "UNKNOWN (news): news unavailable (HTTP 500 from /v1beta1/news): "
        "cannot rule out a halt today")
    assert gate["halt"]["status"] == "UNKNOWN" and gate["earnings"]["status"] == "CLEAR"


@pytest.mark.parametrize("news", [
    {"ok": True, "news": None},
    {"ok": True, "news": {}},
    {"ok": True, "news": {"news": None}},
    {"ok": True, "news": {"news": "none"}},
    {"ok": True, "news": []},
    {"ok": True},
    {"ok": "yes", "news": {"news": []}},
], ids=["none", "empty-dict", "null-list", "string-list", "bare-list", "no-news", "forged-ok"])
def test_blocked_a_malformed_news_reply_is_unknown(news):
    gate, v = run(news=news)
    assert_blocked(gate, v, "UNKNOWN", "UNKNOWN (news): news unavailable")
    assert "cannot rule out a halt today" in gate["reason"]


def test_blocked_a_halt_headline_without_a_valid_time_cannot_read_as_no_halt():
    bad = news_item("TSLA", "Trading halted in TSLA", NOW)
    bad["created_at"] = "not-a-date"
    gate, v = run(news=news_ok([bad]))
    assert_blocked(gate, v, "UNKNOWN",
                   "UNKNOWN (news): 1 news item(s) unreadable: cannot rule out a halt today")


# ============================ news: earnings (a secondary source adds EVENT only) ============

def test_blocked_news_only_earnings_headline_is_an_event_when_sources_disagree():
    item = news_item("TSLA", "Tesla to report third-quarter earnings after the bell",
                     NOW - timedelta(hours=3))
    gate, v = run(rows=fx("earnings_calendar_others.json"), news=news_ok([item]))
    assert_blocked(gate, v, "EVENT",
        "EVENT (news): earnings headline in the news: "
        "'Tesla to report third-quarter earnings after the bell'")
    assert gate["earnings"]["findings"][0]["source"] == "news"
    assert gate["halt"]["status"] == "CLEAR"


def test_an_earnings_headline_older_than_24_hours_is_ignored():
    item = news_item("TSLA", "Tesla earnings beat", NOW - timedelta(hours=30))
    assert_clear(*run(news=news_ok([item])))


def test_a_news_failure_gives_no_earnings_finding_from_the_news_source():
    gate = gate_for("TSLA", NOW, news={"ok": False, "reason": "boom"})
    assert [f["source"] for f in gate["earnings"]["findings"]] == []
    assert [f["source"] for f in gate["halt"]["findings"]] == ["news"]  # the halt side blocks


def test_blocked_event_and_unknown_reasons_stay_distinct_and_event_comes_first():
    item = news_item("TSLA", "Trading halted in TSLA pending news", NOW - timedelta(hours=1))
    gate, v = run(rows=[fmp_row("TSLA", "2026-11-09", "amc", True)], news=news_ok([item]),
                  filings={"ok": True, "submissions": None})
    assert gate["status"] == "EVENT"
    parts = gate["reason"].split("; ")
    assert [p.split(" (")[0] for p in parts] == ["EVENT", "EVENT", "UNKNOWN"]
    assert parts[0].startswith("EVENT (fmp): earnings today")
    assert parts[1].startswith("EVENT (news)")
    assert parts[2].startswith("UNKNOWN (sec): no SEC filer")
    # check_order carries the one reason line, labelled EVENT, with the UNKNOWN part inside it
    assert sum(r.startswith("event gate ") for r in v["reasons"]) == 1
    assert v["reasons"][-1].startswith("event gate EVENT: EVENT (fmp)") or any(
        r.startswith("event gate EVENT: EVENT (fmp)") for r in v["reasons"])


def test_unknown_and_event_are_different_gate_statuses_and_check_order_labels():
    _, unknown = run(filings=filings_ok(NOW, days_ago=300))
    _, event = run(rows=[fmp_row("TSLA", "2026-11-09", "amc", True)])
    assert any(r.startswith("event gate UNKNOWN: ") for r in unknown["reasons"])
    assert any(r.startswith("event gate EVENT: ") for r in event["reasons"])
    assert not any("EVENT" in r.split(":")[0] for r in unknown["reasons"])


# ============================ secondary signals ============================

def test_a_secondary_that_raises_changes_nothing_clear_stays_clear():
    def boom(*a):
        raise RuntimeError("alpha vantage down")
    assert_clear(*run(secondary=(("av", boom),)))


def test_a_secondary_that_raises_cannot_clear_an_unknown():
    def boom(*a):
        raise RuntimeError("down")
    gate, v = run(filings={"ok": True, "submissions": None}, secondary=(("av", boom),))
    assert_blocked(gate, v, "UNKNOWN", "UNKNOWN (sec): no SEC filer")


@pytest.mark.parametrize("out", [None, [], (), [""][:0]], ids=["none", "empty-list", "tuple",
    "list"])
def test_a_secondary_that_returns_nothing_leaves_clear(out):
    assert_clear(*run(secondary=(("av", lambda *a: out),)))


def test_a_secondary_that_returns_nothing_cannot_clear_an_unknown():
    gate, v = run(calendar={"ok": False, "source": "alphavantage", "reason": "x"},
                  secondary=(("av", lambda *a: []),))
    assert_blocked(gate, v, "UNKNOWN", "UNKNOWN (alphavantage)")


def test_blocked_a_secondary_that_returns_a_reason_adds_an_event():
    gate, v = run(secondary=(("av", lambda *a: ["alpha vantage lists TSLA on 2026-11-09"]),))
    assert_blocked(gate, v, "EVENT", "EVENT (av): alpha vantage lists TSLA on 2026-11-09")


def test_blocked_a_custom_secondary_appended_to_the_default_tuple_works():
    seen = []

    def cross_check(symbol, now, window_end, sources):
        seen.append((symbol, sorted(sources)))
        return ["cross-check says earnings tomorrow"]

    gate, v = run(secondary=(*SECONDARY_EARNINGS_SIGNALS, ("alphavantage", cross_check)))
    assert_blocked(gate, v, "EVENT", "EVENT (alphavantage): cross-check says earnings tomorrow")
    assert seen == [("TSLA", ["calendar", "filings", "fmp_calendar", "news"])]


def test_the_default_tuple_is_unchanged_by_a_custom_one():
    assert [n for n, _ in SECONDARY_EARNINGS_SIGNALS] == ["news", "fmp"]


def test_an_empty_secondary_tuple_removes_the_news_cross_check_only():
    item = news_item("TSLA", "Tesla earnings beat", NOW - timedelta(hours=1))
    gate = gate_for("TSLA", NOW, news=news_ok([item]), secondary=())
    assert gate["earnings"]["status"] == "CLEAR"  # the halt side still reads the same news


# ============================ never raises ============================

@pytest.mark.parametrize("symbol,now,end", [
    ("", NOW, NOW), (None, NOW, NOW), ("TSLA", NOW.replace(tzinfo=None), NOW),
    ("TSLA", NOW, NOW.replace(tzinfo=None)), ("TSLA", None, NOW),
])
def test_blocked_an_unusable_symbol_or_clock_is_unknown_and_never_raises(symbol, now, end):
    e = earnings_status(symbol, now, end, calendar=calendar_ok(NOW), filings=filings_ok(NOW))
    assert e["status"] == "UNKNOWN"
    if not symbol or not isinstance(now, datetime) or now.tzinfo is None:
        assert halt_status(symbol, now, news_ok())["status"] == "UNKNOWN"
    g = event_gate(symbol, now, end, calendar=calendar_ok(NOW), filings=filings_ok(NOW),
                   news=news_ok())
    assert g["status"] == "UNKNOWN" and "called without" in g["reason"]


def test_blocked_a_gate_for_one_symbol_does_not_let_another_through():
    gate = gate_for("AAPL", NOW)
    assert gate["status"] == "CLEAR"
    v = verdict_for(gate, NOW, symbol="TSLA")
    assert v["verdict"] == "BLOCK" and "event gate result is for another symbol" in v["reasons"]
