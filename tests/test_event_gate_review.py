"""Regression tests for the code review of the event gate (#112): every case below used to
read as CLEAR (fail-open) or latch UNKNOWN for the day. Offline: fakes only, no network."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from pipeline.events import FmpCalendarCache
from tests.fakes import background_rows
from tests.order_helpers import (
    calendar_ok,
    clear_gate,
    filings_ok,
    fmp_row,
    gate_for,
    news_item,
    news_ok,
    submissions_202,
)
from tools.config import CONFIG_DIR, load_limits
from tools.earnings_calendar import FMP_ROW_CAP, parse_fmp_calendar
from tools.event_gate import halt_status, norm_ticker
from tools.risk_check import DayState, check_order
from tools.size import Plan, size_position
from tools.slippage import minimum_allowance

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 9, 10, 30, tzinfo=NY)  # a Monday
TODAY = NOW.date()
END = TODAY + timedelta(days=14)
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")


def order(symbol="TSLA"):
    plan = Plan(symbol, Decimal("20.00"), Decimal("19.00"), Decimal("21.00"))
    slip = minimum_allowance(plan.entry, LIMITS)
    return size_position(plan, LIMITS, slippage=slip)["order"]


# ============================ B1: empty / cut-short FMP replies ============================

def test_blocked_an_empty_reply_is_not_proof_that_nobody_reports():
    cal = parse_fmp_calendar([], NOW, TODAY, END)
    assert cal == {"ok": False, "reason": "earnings calendar reply is empty"}
    gate = gate_for("TSLA", NOW, calendar=cal)
    assert gate["status"] == "UNKNOWN"
    assert "UNKNOWN (fmp): earnings source unavailable: earnings calendar reply is empty" \
        in gate["reason"]


def test_blocked_a_reply_at_the_row_cap_may_be_cut_short():
    rows = [fmp_row(f"Z{i:04d}", TODAY.isoformat()) for i in range(FMP_ROW_CAP)]
    cal = parse_fmp_calendar(rows, NOW, TODAY, END)
    assert cal["ok"] is False and "may be cut short" in cal["reason"]
    assert gate_for("TSLA", NOW, calendar=cal)["status"] == "UNKNOWN"


def test_one_row_under_the_cap_is_accepted():
    rows = [fmp_row(f"Z{i:04d}", END.isoformat()) for i in range(FMP_ROW_CAP - 1)]
    assert parse_fmp_calendar(rows, NOW, TODAY, END)["ok"] is True


@pytest.mark.parametrize("day", [TODAY - timedelta(days=1), END + timedelta(days=1)],
                         ids=["before", "after"])
def test_blocked_a_row_dated_outside_the_requested_range_is_malformed(day):
    rows = [*background_rows(TODAY, END), fmp_row("AXLE", day.isoformat())]
    cal = parse_fmp_calendar(rows, NOW, TODAY, END)
    assert cal["ok"] is False and "outside the requested" in cal["reason"]


def test_blocked_rows_that_stop_before_the_needed_day_are_unknown():
    # Monday 11-09: the 3rd trading day after is Thursday 11-12; rows stop on 11-10
    rows = background_rows(TODAY, TODAY + timedelta(days=1))
    gate = gate_for("TSLA", NOW, calendar=calendar_ok(NOW, rows, background=False))
    assert gate["status"] == "UNKNOWN"
    assert "rows stop at 2026-11-10, before 2026-11-12" in gate["reason"]


def test_blocked_rows_that_start_after_today_are_unknown():
    """A reply cut at its start (newest-first truncation, a later range served) could have
    dropped today's row for the symbol."""
    rows = [*background_rows(TODAY + timedelta(days=2), END),
            fmp_row("TSLA", TODAY.isoformat(), "amc")]  # today's TSLA row, dropped below
    rows = [r for r in rows if r["date"] != TODAY.isoformat()]
    gate = gate_for("TSLA", NOW, calendar=calendar_ok(NOW, rows, background=False))
    assert gate["status"] == "UNKNOWN"
    assert "rows start at 2026-11-11, after today 2026-11-09" in gate["reason"]


@pytest.mark.parametrize("tickers", [None, "GOOG", 5, {"a": 1}, [None, 5, {}, "", "  "]],
                         ids=["none", "string", "int", "dict", "junk-list"])
def test_a_malformed_tickers_list_never_raises_or_clears_a_listed_event(tickers):
    subs = submissions_202(NOW, 30, symbol="GOOG")
    subs["tickers"] = tickers
    cal = calendar_ok(NOW, [fmp_row("GOOG", TODAY.isoformat(), "amc")])
    gate = gate_for("GOOG", NOW, calendar=cal, filings={"ok": True, "submissions": subs})
    assert gate["status"] == "EVENT"


def test_rows_reaching_the_needed_day_prove_clear():
    rows = background_rows(TODAY, date(2026, 11, 12))
    assert gate_for("TSLA", NOW, calendar=calendar_ok(NOW, rows, background=False))[
        "status"] == "CLEAR"


def test_a_long_server_symbol_is_cut_in_the_reason():
    rows = [{"symbol": "X" * 500, "date": "bad", "time": None}]
    cal = parse_fmp_calendar(rows, NOW, TODAY, END)
    assert cal["ok"] is False and "X" * 17 not in cal["reason"]


# ============================ B2 + S1 + S4: halts ============================

def test_blocked_a_halt_headline_repeated_from_yesterday_is_still_today():
    """The digest keeps a repeated headline's EARLIEST copy; the halt scan reads every item."""
    same = "Trading halted in TSLA pending news"
    items = [news_item("TSLA", same, NOW - timedelta(days=1)),
             news_item("TSLA", same, NOW - timedelta(hours=1))]
    h = halt_status("TSLA", NOW, news_ok(items))
    assert h["status"] == "EVENT"
    assert h["findings"][0]["reason"] == f"trading halt today: {same!r}"


@pytest.mark.parametrize("bad", [
    {"headline": "Trading halted", "created_at": "2026-11-09T15:00:00Z"},           # no symbols
    {"headline": "Trading halted", "symbols": "TSLA", "created_at": "2026-11-09T15:00:00Z"},
    "not an object",
], ids=["no-symbols", "symbols-not-a-list", "not-a-dict"])
def test_blocked_an_unreadable_item_cannot_read_as_no_halt(bad):
    h = halt_status("TSLA", NOW, news_ok([bad]))
    assert h["status"] == "UNKNOWN"
    assert "1 news item(s) unreadable: cannot rule out a halt today" in h["findings"][0]["reason"]


@pytest.mark.parametrize("headline", [
    "TSLA trading paused for volatility (LULD)",
    "Exchange issues LULD pause in TSLA",
    "Nasdaq suspends trading in TSLA",
    "TSLA trading pause lifted after five minutes",
])
def test_blocked_pause_and_suspension_wording_is_a_halt(headline):
    h = halt_status("TSLA", NOW, news_ok([news_item("TSLA", headline, NOW - timedelta(hours=1))]))
    assert h["status"] == "EVENT", headline


@pytest.mark.parametrize("symbols", [[], [None], ["RIVN"], ["TSLAX"]],
                         ids=["empty", "none", "other", "other-spelling"])
def test_blocked_an_item_that_does_not_list_this_symbol_is_unknown(symbols):
    item = news_item("TSLA", "Trading halted", NOW - timedelta(hours=1))
    item["symbols"] = symbols
    assert halt_status("TSLA", NOW, news_ok([item]))["status"] == "UNKNOWN"


@pytest.mark.parametrize("listed", ["BRKB", "BRK B", "brk-b", "BRK/B"])
def test_blocked_a_halt_listed_under_another_spelling_of_the_symbol_is_found(listed):
    item = news_item("BRK.B", "Trading halted in Berkshire B", NOW - timedelta(hours=1))
    item["symbols"] = [listed]
    assert halt_status("BRK.B", NOW, news_ok([item]))["status"] == "EVENT"


def test_production_pause_is_not_a_trading_halt():
    item = news_item("TSLA", "Tesla paused Cybertruck production", NOW - timedelta(hours=1))
    assert halt_status("TSLA", NOW, news_ok([item]))["status"] == "CLEAR"


# ============================ S2: item 2.02 today, whatever its time ============================

def test_blocked_a_time_estimated_202_from_today_is_an_event():
    subs = submissions_202(NOW, 30)
    recent = subs["filings"]["recent"]
    recent["accessionNumber"].append("0000012345-26-000002")
    recent["filingDate"].append(TODAY.isoformat())
    recent["acceptanceDateTime"].append("")      # missing: parsed as 23:59:59 ET, after now
    recent["form"].append("8-K")
    recent["items"].append("2.02,9.01")
    recent["primaryDocument"].append("8k-today.htm")
    gate = gate_for("TSLA", NOW, filings={"ok": True, "submissions": subs})
    assert gate["status"] == "EVENT"
    assert "EVENT (sec): 8-K item 2.02 (results of operations) filed today" in gate["reason"]


def test_blocked_a_202_accepted_minutes_after_now_is_an_event():
    subs = submissions_202(NOW, days_ago=-30 / (24 * 60))  # 30 minutes ahead of our clock
    gate = gate_for("TSLA", NOW, filings={"ok": True, "submissions": subs})
    assert gate["status"] == "EVENT"


# ============================ S3: share classes and separators ============================

@pytest.mark.parametrize("a, b", [("BRK.B", "BRK-B"), ("brk/b", "BRK.B"), (" goog ", "GOOG")])
def test_ticker_normalisation(a, b):
    assert norm_ticker(a) == norm_ticker(b)


def test_blocked_a_sibling_share_class_row_today_is_this_companys_event():
    subs = submissions_202(NOW, 30, symbol="GOOG")
    subs["tickers"] = ["GOOGL", "GOOG"]
    cal = calendar_ok(NOW, [fmp_row("GOOGL", TODAY.isoformat(), "amc")])
    gate = gate_for("GOOG", NOW, calendar=cal, filings={"ok": True, "submissions": subs})
    assert gate["status"] == "EVENT"
    assert "listed as GOOGL" in gate["reason"]


def test_blocked_a_dash_listed_row_matches_a_dot_order_symbol():
    subs = submissions_202(NOW, 30, symbol="BRK.B")
    subs["tickers"] = ["BRK-A", "BRK-B"]
    cal = calendar_ok(NOW, [fmp_row("BRK-B", TODAY.isoformat(), None)])
    gate = gate_for("BRK.B", NOW, calendar=cal, filings={"ok": True, "submissions": subs})
    assert gate["status"] == "EVENT"


def test_an_unrelated_company_row_does_not_block():
    subs = submissions_202(NOW, 30, symbol="GOOG")
    subs["tickers"] = ["GOOGL", "GOOG"]
    cal = calendar_ok(NOW, [fmp_row("GOOGX", TODAY.isoformat(), "amc")])
    gate = gate_for("GOOG", NOW, calendar=cal, filings={"ok": True, "submissions": subs})
    assert gate["status"] == "CLEAR", gate["reason"]


# ============================ S5: cache robustness ============================

def test_a_cache_record_with_a_non_string_to_refetches(tmp_path):
    calls = []

    def fetch(start, end):
        calls.append((start, end))
        return background_rows(start, end)

    (tmp_path / f"fmp-earnings-{TODAY}.json").write_text(json.dumps(
        {"fetched_at": NOW.isoformat(), "from": TODAY.isoformat(), "to": 5, "reply": []}))
    rec = FmpCalendarCache(tmp_path, fetch, lambda: NOW).get(TODAY, END)
    assert len(calls) == 1 and rec["reply"] == background_rows(TODAY, END)


@pytest.mark.parametrize("bad_reply", [[], [{"symbol": "X"}], {"Error Message": "x"}],
                         ids=["empty", "malformed", "error-object"])
def test_an_invalid_reply_is_returned_but_never_cached(tmp_path, bad_reply):
    calls = []

    def fetch(start, end):
        calls.append(1)
        return bad_reply

    cache = FmpCalendarCache(tmp_path, fetch, lambda: NOW)
    cache.get(TODAY, END)
    cache.get(TODAY, END)
    assert len(calls) == 2 and list(tmp_path.glob("*.json")) == []


def test_a_valid_reply_is_cached_atomically(tmp_path):
    cache = FmpCalendarCache(tmp_path, lambda s, e: background_rows(s, e), lambda: NOW)
    cache.get(TODAY, END)
    assert [p.name for p in tmp_path.iterdir()] == [f"fmp-earnings-{TODAY}.json"]


# ============================ NIT 5: a CLEAR with open findings ============================

def test_blocked_a_clear_gate_carrying_an_event_finding():
    gate = clear_gate("TSLA", NOW)
    forged = {**gate, "findings": [{"check": "halt", "source": "news", "status": "EVENT",
                                    "reason": "trading halt today"}]}
    v = check_order(order(), LIMITS, DayState(), NOW, forged)
    assert v["verdict"] == "BLOCK"
    assert "event gate result is inconsistent (CLEAR with open findings)" in v["reasons"]


@pytest.mark.parametrize("change", [
    {"findings": None}, {"earnings": {"status": "UNKNOWN"}}, {"halt": None}],
    ids=["no-findings", "earnings-unknown", "no-halt-part"])
def test_blocked_a_clear_gate_with_missing_or_open_parts(change):
    gate = {**clear_gate("TSLA", NOW), **change}
    if change == {"findings": None}:
        gate.pop("findings")
    v = check_order(order(), LIMITS, DayState(), NOW, gate)
    assert "event gate result is inconsistent (CLEAR with open findings)" in v["reasons"]


def test_a_real_clear_gate_passes():
    assert check_order(order(), LIMITS, DayState(), NOW, clear_gate("TSLA", NOW)) == {
        "verdict": "PASS", "reasons": []}


def test_filings_helper_still_clear():
    assert gate_for("TSLA", NOW, filings=filings_ok(NOW))["status"] == "CLEAR"
