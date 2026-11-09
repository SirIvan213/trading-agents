"""The gate's plausibility rule for the primary calendar (#167).

A calendar that answers but lists fewer than MIN_CALENDAR_ROWS reports for today..end, or reports
on fewer than MIN_CALENDAR_TRADING_DAYS distinct trading days, proves nothing: it is UNKNOWN,
and check_order BLOCKs UNKNOWN. The live case behind it: FMP's free tier listed 3 reports for a
week of earnings season and the gate said CLEAR. Dates are Monday 2026-11-09 unless noted.
"""
from datetime import date, datetime, timedelta

import pytest

import tools.event_gate as eg
from tests.fakes import av_csv
from tests.order_helpers import (
    NY,
    calendar_ok,
    filings_ok,
    fmp_row,
    gate_for,
    verdict_for,
)
from tools.earnings_calendar import parse_av_calendar
from tools.event_gate import MIN_CALENDAR_ROWS, MIN_CALENDAR_TRADING_DAYS

NOW = datetime(2026, 11, 9, 10, 30, tzinfo=NY)  # a Monday
MON, TUE, THU = "2026-11-09", "2026-11-10", "2026-11-12"
SAT, SUN = "2026-11-14", "2026-11-15"


def rows_on(days, n, prefix="ZP"):
    """n other companies' FMP rows, spread over `days` in turn."""
    return [fmp_row(f"{prefix}{i:03d}", days[i % len(days)], "amc", False) for i in range(n)]


def gate_with(rows, now=NOW, symbol="MSFT", **cal_kw):
    cal = calendar_ok(now, rows, background=False, **cal_kw)
    assert cal["ok"] is True, cal
    return gate_for(symbol, now, calendar=cal, filings=filings_ok(now, symbol=symbol))


def test_the_thresholds_are_the_documented_ones():
    assert MIN_CALENDAR_ROWS == 50 and MIN_CALENDAR_TRADING_DAYS == 3


# ---- the #167 regression ----

def test_blocked_the_167_case_three_reports_for_the_week_is_unknown_and_blocks():
    """MSFT, an item 2.02 filed 30 days ago, FMP-shaped calendar for 2026-11-09..16 with only
    3 rows: it used to read CLEAR."""
    rows = rows_on([MON, TUE, THU], 3)
    gate = gate_with(rows, end=date(2026, 11, 16))
    assert gate["status"] == "UNKNOWN", gate["reason"]
    assert "too sparse" in gate["reason"] and "only 3 reports" in gate["reason"]
    assert "UNKNOWN (fmp)" in gate["reason"]
    verdict = verdict_for(gate, NOW, "MSFT")
    assert verdict["verdict"] == "BLOCK"


def test_the_167_regression_fails_if_the_floor_is_removed(monkeypatch):
    """Proves the test above can fail: with the floor at 0 the same 3-row calendar is CLEAR."""
    monkeypatch.setattr(eg, "MIN_CALENDAR_ROWS", 0)
    gate = gate_with(rows_on([MON, TUE, THU], 3), end=date(2026, 11, 16))
    assert gate["status"] == "CLEAR"


# ---- the row boundary ----

def test_blocked_exactly_49_rows_is_unknown():
    gate = gate_with(rows_on([MON, TUE, THU], 49))
    assert gate["status"] == "UNKNOWN" and "only 49 reports" in gate["reason"]
    assert verdict_for(gate, NOW, "MSFT")["verdict"] == "BLOCK"


def test_exactly_50_rows_is_clear_and_passes():
    gate = gate_with(rows_on([MON, TUE, THU], 50))
    assert gate["status"] == "CLEAR", gate["reason"]
    assert verdict_for(gate, NOW, "MSFT") == {"verdict": "PASS", "reasons": []}


def test_no_rows_at_all_is_unknown_and_never_clear():
    cal = calendar_ok(NOW, [], background=False)
    assert cal["ok"] is False  # the parser refuses an empty reply before the gate sees it
    gate = gate_for("MSFT", NOW, calendar=cal, filings=filings_ok(NOW, symbol="MSFT"))
    assert gate["status"] == "UNKNOWN"


# ---- the trading-day rule ----

def test_blocked_fifty_rows_on_only_two_trading_days_is_unknown():
    # Mon + Tue, then the weekend: the range checks pass (rows start today, run past the 3rd
    # trading day), but only 2 distinct trading days carry reports
    gate = gate_with(rows_on([MON, TUE, SAT, SUN], 60))
    assert gate["status"] == "UNKNOWN", gate["reason"]
    assert "only 2 trading day(s)" in gate["reason"] and "too sparse" in gate["reason"]
    assert verdict_for(gate, NOW, "MSFT")["verdict"] == "BLOCK"


def test_three_trading_days_among_weekend_rows_is_clear():
    gate = gate_with(rows_on([MON, TUE, THU, SAT, SUN], 60))
    assert gate["status"] == "CLEAR", gate["reason"]


def test_blocked_one_trading_day_with_many_rows_is_unknown():
    gate = gate_with(rows_on([MON, SAT, SUN], 200))
    assert gate["status"] == "UNKNOWN" and "only 1 trading day(s)" in gate["reason"]


def test_a_market_holiday_is_not_a_trading_day():
    now = datetime(2026, 11, 25, 10, 30, tzinfo=NY)  # Wednesday before Thanksgiving
    holiday, wed, tue_after = "2026-11-26", "2026-11-25", "2026-12-01"
    gate = gate_with(rows_on([wed, holiday, tue_after], 60), now=now)
    assert gate["status"] == "UNKNOWN", gate["reason"]
    assert "only 2 trading day(s)" in gate["reason"]


def test_the_same_week_with_the_day_after_thanksgiving_has_three_trading_days():
    now = datetime(2026, 11, 25, 10, 30, tzinfo=NY)
    gate = gate_with(rows_on(["2026-11-25", "2026-11-27", "2026-12-01"], 60), now=now)
    assert gate["status"] == "CLEAR", gate["reason"]


# ---- rows before today do not count ----

def test_blocked_rows_dated_before_today_do_not_count_toward_the_floor():
    stale = rows_on(["2026-11-05", "2026-11-06"], 10, prefix="ZO")
    gate = gate_with([*stale, *rows_on([MON, TUE, THU], 49)], start=date(2026, 11, 5))
    assert gate["status"] == "UNKNOWN" and "only 49 reports" in gate["reason"]


def test_fifty_rows_from_today_on_clear_even_with_older_rows_present():
    older = rows_on(["2026-11-05", "2026-11-06"], 10, prefix="ZO")
    gate = gate_with([*older, *rows_on([MON, TUE, THU], 50)], start=date(2026, 11, 5))
    assert gate["status"] == "CLEAR", gate["reason"]


def test_blocked_rows_before_today_do_not_count_toward_the_trading_days():
    older = rows_on(["2026-11-05", "2026-11-06"], 20, prefix="ZO")
    gate = gate_with([*older, *rows_on([MON, TUE, SAT, SUN], 60)], start=date(2026, 11, 5))
    assert gate["status"] == "UNKNOWN" and "only 2 trading day(s)" in gate["reason"]


# ---- the same rule on an Alpha Vantage-parsed calendar ----

def av_cal(rows, after=(("AFT", "2026-11-17"),)):
    text = av_csv([*rows, *after], background=False)
    cal = parse_av_calendar(text, NOW - timedelta(minutes=1), date(2026, 11, 9),
                            date(2026, 11, 16))
    assert cal["ok"] is True, cal
    return cal


def av_rows(days, n):
    return [(f"ZQ{i:03d}", days[i % len(days)]) for i in range(n)]


def av_gate(cal):
    return gate_for("MSFT", NOW, calendar=cal, filings=filings_ok(NOW, symbol="MSFT"))


def test_blocked_a_sparse_alpha_vantage_calendar_is_unknown_and_labelled_alphavantage():
    gate = av_gate(av_cal(av_rows([MON, TUE, THU], 3)))
    assert gate["status"] == "UNKNOWN"
    assert "UNKNOWN (alphavantage): earnings calendar lists only 3 reports" in gate["reason"]
    assert verdict_for(gate, NOW, "MSFT")["verdict"] == "BLOCK"


def test_blocked_an_alpha_vantage_calendar_with_49_rows_is_unknown():
    gate = av_gate(av_cal(av_rows([MON, TUE, THU], 49)))
    assert gate["status"] == "UNKNOWN" and "only 49 reports" in gate["reason"]


def test_an_alpha_vantage_calendar_with_50_rows_is_clear_and_names_alpha_vantage():
    gate = av_gate(av_cal(av_rows([MON, TUE, THU], 50)))
    assert gate["status"] == "CLEAR", gate["reason"]
    assert "(Alpha Vantage)" in gate["reason"]
    assert verdict_for(gate, NOW, "MSFT")["verdict"] == "PASS"


def test_blocked_an_alpha_vantage_calendar_on_two_trading_days_is_unknown():
    gate = av_gate(av_cal(av_rows([MON, TUE, SAT, SUN], 60)))
    assert gate["status"] == "UNKNOWN" and "only 2 trading day(s)" in gate["reason"]
    assert "UNKNOWN (alphavantage)" in gate["reason"]


@pytest.mark.parametrize("n", [49, 50])
def test_the_row_count_boundary_is_the_same_for_both_sources(n):
    fmp = gate_with(rows_on([MON, TUE, THU], n))["status"]
    av = av_gate(av_cal(av_rows([MON, TUE, THU], n)))["status"]
    assert fmp == av == ("CLEAR" if n == 50 else "UNKNOWN")
