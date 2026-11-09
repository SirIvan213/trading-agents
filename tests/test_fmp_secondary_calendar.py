"""FMP as a block-only secondary earnings signal next to a CLEAR Alpha Vantage primary (#145, #167).

FMP can only ADD an EVENT: a report for the symbol today, inside the trade window, or on an
estimated date within 3 trading days. A failed, missing or malformed FMP calendar adds nothing,
and it can never turn an UNKNOWN primary into CLEAR. Monday 2026-11-09 10:30 ET.
"""
from datetime import date, datetime, timedelta

import pytest

from tests.fakes import av_csv
from tests.order_helpers import (
    NY,
    calendar_ok,
    filings_ok,
    fmp_row,
    gate_for,
    submissions_202,
    verdict_for,
)
from tools.earnings_calendar import parse_av_calendar

NOW = datetime(2026, 11, 9, 10, 30, tzinfo=NY)  # a Monday
TODAY = date(2026, 11, 9)


def av_primary(now=NOW) -> dict:
    """A plausible, CLEAR-able Alpha Vantage calendar with no row for any test symbol."""
    cal = parse_av_calendar(av_csv(start=TODAY, end=TODAY + timedelta(days=7)),
                            now - timedelta(minutes=1), TODAY, TODAY + timedelta(days=7))
    assert cal["ok"] is True
    return cal


def fmp_with(*rows, background=True, **kw) -> dict:
    return calendar_ok(NOW, list(rows), background=background, **kw)


def gate(fmp, symbol="TSLA", **kw):
    kw.setdefault("filings", filings_ok(NOW, symbol=symbol))
    return gate_for(symbol, NOW, calendar=av_primary(), fmp_calendar=fmp, **kw)


def test_the_primary_alone_is_clear_so_the_blocks_below_come_from_fmp():
    g = gate(None)
    assert g["status"] == "CLEAR", g["reason"]
    assert verdict_for(g, NOW) == {"verdict": "PASS", "reasons": []}


def test_a_clean_fmp_calendar_leaves_the_primary_clear():
    g = gate(fmp_with(fmp_row("OTHER", "2026-11-10", "amc", True)))
    assert g["status"] == "CLEAR", g["reason"]
    assert "(Alpha Vantage)" in g["reason"]


def test_blocked_fmp_lists_the_symbol_today():
    g = gate(fmp_with(fmp_row("TSLA", "2026-11-09", "amc", True)))
    assert g["status"] == "EVENT"
    assert "EVENT (fmp): FMP calendar: earnings today 2026-11-09 (amc, confirmed)" \
        in g["reason"]
    assert verdict_for(g, NOW)["verdict"] == "BLOCK"
    assert [f["source"] for f in g["findings"] if f["status"] == "EVENT"] == ["fmp"]


def test_blocked_fmp_lists_the_symbol_inside_the_trade_window():
    window_end = datetime(2026, 11, 10, 15, 55, tzinfo=NY)
    g = gate_for("TSLA", NOW, calendar=av_primary(), window_end=window_end,
                 filings=filings_ok(NOW),
                 fmp_calendar=fmp_with(fmp_row("TSLA", "2026-11-10", "bmo", True)))
    assert g["status"] == "EVENT"
    assert "EVENT (fmp): FMP calendar: earnings 2026-11-10 inside the trade window" \
        in g["reason"]


def test_blocked_an_estimated_fmp_date_within_three_trading_days():
    g = gate(fmp_with(fmp_row("TSLA", "2026-11-12", None, False)))
    assert g["status"] == "EVENT"
    assert "EVENT (fmp): FMP calendar: estimated earnings date 2026-11-12 within 3 trading " \
        "days" in g["reason"]


def test_an_estimated_fmp_date_four_trading_days_ahead_adds_nothing():
    g = gate(fmp_with(fmp_row("TSLA", "2026-11-13", None, False)))
    assert g["status"] == "CLEAR", g["reason"]


def test_a_confirmed_fmp_date_outside_the_window_adds_nothing():
    """Two trading days ahead is outside the same-day window and a confirmed date does not
    move, so the secondary stays silent (the primary's estimate rule does not apply to it)."""
    g = gate(fmp_with(fmp_row("TSLA", "2026-11-11", "amc", True)))
    assert g["status"] == "CLEAR", g["reason"]
    assert not [f for f in g["findings"] if f["source"] == "fmp"]


def test_blocked_a_share_class_sibling_from_the_sec_tickers_is_matched():
    subs = submissions_202(NOW, 30, symbol="GOOGL")
    subs["tickers"] = ["GOOG", "GOOGL"]
    g = gate_for("GOOGL", NOW, calendar=av_primary(),
                 filings={"ok": True, "submissions": subs},
                 fmp_calendar=fmp_with(fmp_row("GOOG", "2026-11-09", "amc", True)))
    assert g["status"] == "EVENT"
    assert "FMP calendar: earnings today 2026-11-09" in g["reason"] and "listed as GOOG" \
        in g["reason"]


def test_a_sibling_row_without_the_sec_alias_is_not_matched():
    g = gate_for("GOOGL", NOW, calendar=av_primary(),
                 filings=filings_ok(NOW, symbol="GOOGL"),
                 fmp_calendar=fmp_with(fmp_row("GOOG", "2026-11-09", "amc", True)))
    assert g["status"] == "CLEAR", g["reason"]


def test_blocked_a_sparse_fmp_calendar_still_adds_its_event_for_the_symbol():
    sparse = fmp_with(fmp_row("TSLA", "2026-11-09", "bmo", True),
                      fmp_row("ZZ1", "2026-11-10", "amc", True),
                      fmp_row("ZZ2", "2026-11-12", "amc", True), background=False)
    assert len(sparse["rows"]) == 3
    g = gate(sparse)
    assert g["status"] == "EVENT" and "EVENT (fmp): FMP calendar: earnings today" in g["reason"]


def test_a_sparse_fmp_calendar_without_the_symbol_adds_nothing_and_is_not_unknown():
    sparse = fmp_with(fmp_row("ZZ1", "2026-11-10", "amc", True), background=False)
    g = gate(sparse)
    assert g["status"] == "CLEAR", g["reason"]


# ---- failures add nothing and cannot clear ----

NOT_OK = [
    pytest.param({"ok": False, "source": "fmp", "reason": "HTTP 503 from /stable/x"}, id="not-ok"),
    pytest.param(None, id="none"),
    pytest.param({}, id="empty-dict"),
    pytest.param("rows", id="not-a-dict"),
    pytest.param({"ok": True, "rows": None}, id="rows-none"),
    pytest.param({"ok": True, "rows": "TSLA"}, id="rows-not-a-list"),
    pytest.param({"ok": "yes", "rows": [fmp_row("TSLA", "2026-11-09")]}, id="ok-not-true"),
    pytest.param({"ok": True, "rows": [
        {"symbol": "TSLA", "date": "garbage"}, "TSLA", None, 7,
        {"symbol": None, "date": "2026-11-09"}, {"date": "2026-11-09"}]}, id="malformed-rows"),
]


@pytest.mark.parametrize("fmp", NOT_OK)
def test_a_failed_or_malformed_fmp_calendar_adds_nothing_and_the_primary_decides(fmp):
    g = gate(fmp)
    assert g["status"] == "CLEAR", g["reason"]
    assert verdict_for(g, NOW)["verdict"] == "PASS"
    assert "(fmp)" not in g["reason"]


def test_a_stale_fmp_calendar_without_the_symbol_adds_nothing():
    stale = calendar_ok(NOW, [], fetched_at=NOW - timedelta(days=2))
    g = gate(stale)
    assert g["status"] == "CLEAR", g["reason"]


@pytest.mark.parametrize("fmp", [
    pytest.param(None, id="none"),
    pytest.param({"ok": False, "reason": "x"}, id="not-ok"),
    pytest.param(calendar_ok(NOW, []), id="clean"),
    pytest.param(calendar_ok(NOW, [fmp_row("TSLA", "2026-11-13", None, False)]),
                 id="far-estimate"),
])
def test_blocked_fmp_can_never_clear_an_unknown_primary(fmp):
    failed_primary = {"ok": False, "source": "alphavantage", "reason": "HTTP 429 from /query"}
    g = gate_for("TSLA", NOW, calendar=failed_primary, fmp_calendar=fmp,
                 filings=filings_ok(NOW))
    assert g["status"] == "UNKNOWN"
    assert "UNKNOWN (alphavantage): earnings source unavailable: HTTP 429" in g["reason"]
    assert verdict_for(g, NOW)["verdict"] == "BLOCK"


def test_blocked_a_sparse_primary_is_not_rescued_by_a_full_fmp_calendar():
    sparse = parse_av_calendar(
        av_csv([("ZQ0", "2026-11-09"), ("ZQ1", "2026-11-10"), ("ZQ2", "2026-11-12"),
                ("AFT", "2026-11-17")], background=False),
        NOW - timedelta(minutes=1), TODAY, TODAY + timedelta(days=7))
    g = gate_for("TSLA", NOW, calendar=sparse, filings=filings_ok(NOW),
                 fmp_calendar=calendar_ok(NOW, []))
    assert g["status"] == "UNKNOWN" and "too sparse" in g["reason"]


def test_an_fmp_event_is_reported_even_when_the_primary_is_unknown():
    failed_primary = {"ok": False, "source": "alphavantage", "reason": "HTTP 429 from /query"}
    g = gate_for("TSLA", NOW, calendar=failed_primary, filings=filings_ok(NOW),
                 fmp_calendar=fmp_with(fmp_row("TSLA", "2026-11-09", "amc", True)))
    assert g["status"] == "EVENT"  # EVENT outranks UNKNOWN, and both reasons are shown
    assert "EVENT (fmp)" in g["reason"] and "UNKNOWN (alphavantage)" in g["reason"]
    assert g["reason"].index("EVENT (fmp)") < g["reason"].index("UNKNOWN (alphavantage)")
