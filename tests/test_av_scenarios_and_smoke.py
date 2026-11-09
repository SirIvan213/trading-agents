"""Alpha Vantage calendar in the data classes and in pipeline.smoke_events --live (#167).

ScenarioData serves the saved SYNTHETIC CSV of each scenario, FakeAllData builds one from
`av_rows`, LiveData goes through AvCalendarCache, and the smoke script prints both calendars.
No network, no key.
"""
from datetime import date, datetime, timedelta

import pytest

from pipeline.data import LiveData
from pipeline.events import AvCalendarCache, events_from
from pipeline.scenarios import ScenarioData, load_scenario, scenario_names
from pipeline.smoke_events import main
from tests.fakes import NOON, FakeAllData, av_csv
from tests.order_helpers import NY
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tools.earnings_calendar import parse_av_calendar

# ============================ ScenarioData ============================


def scenario_calendar(name):
    data = ScenarioData(load_scenario(name))
    today = data.as_of().date()
    raw = data.av_earnings_calendar(today, today + timedelta(days=7))
    cal = parse_av_calendar(raw["reply"], datetime.fromisoformat(raw["fetched_at"]),
                            date.fromisoformat(raw["from"]), date.fromisoformat(raw["to"]))
    return data, raw, cal


@pytest.mark.parametrize("name", ["clean-buy", "extended-chase", "red-flag", "tsla"])
def test_every_scenario_serves_an_alpha_vantage_calendar_that_parses_ok(name):
    data, raw, cal = scenario_calendar(name)
    assert name in scenario_names()
    assert cal["ok"] is True, cal
    assert cal["source"] == "alphavantage" and cal["rows_after_end"] > 0
    assert raw["fetched_at"].startswith(data.as_of().date().isoformat() + "T08:30")


@pytest.mark.parametrize("name,listed", [("clean-buy", False), ("extended-chase", False),
                                         ("tsla", False), ("red-flag", True)])
def test_only_red_flag_lists_the_scenario_symbol(name, listed):
    data, _, cal = scenario_calendar(name)
    symbols = {r["symbol"] for r in cal["rows"]}
    assert (data.symbol in symbols) is listed


def test_the_red_flag_symbol_is_reported_today_by_the_calendar():
    data, _, cal = scenario_calendar("red-flag")
    today = data.as_of().date().isoformat()
    assert any(r["symbol"] == data.symbol and r["date"] == today for r in cal["rows"])


def test_the_clean_buy_calendar_is_plausible_for_the_gate():
    data = ScenarioData(load_scenario("clean-buy"))
    now = data.as_of()
    gate = events_from(data)(data.symbol, now, now.replace(hour=15, minute=55))
    assert "calendar lists only" not in gate["reason"] and "too sparse" not in gate["reason"]
    assert gate["earnings"]["calendar_source"] == "alphavantage"


# ============================ FakeAllData and LiveData ============================

def test_blocked_a_fake_av_row_dated_today_gives_an_event():
    data = FakeAllData(av_rows=[("ABCD", "2026-11-03")])
    gate = events_from(data)("ABCD", NOON, NOON.replace(hour=15, minute=55))
    assert gate["status"] == "EVENT"
    assert "EVENT (alphavantage): earnings today 2026-11-03" in gate["reason"]


def test_a_fake_without_av_rows_for_the_symbol_has_no_alpha_vantage_event():
    data = FakeAllData()
    gate = events_from(data)("ABCD", NOON, NOON.replace(hour=15, minute=55))
    # (the fixture filings and news give ABCD other events today; only the calendar is asked)
    assert not [f for f in gate["findings"] if f["source"] == "alphavantage"]


def test_blocked_a_failing_fake_av_is_unknown_not_clear():
    data = FakeAllData(av_raises=RuntimeError("HTTP 429 from /query"))
    gate = events_from(data)("ABCD", NOON, NOON.replace(hour=15, minute=55))
    assert gate["status"] != "CLEAR"  # (the fixture filings also give ABCD an EVENT today)
    assert "UNKNOWN (alphavantage): earnings source unavailable: HTTP 429" in gate["reason"]


def test_live_data_alpha_vantage_calendar_goes_through_the_cache_once(tmp_path):
    now = datetime(2026, 11, 9, 8, 30, tzinfo=NY)
    calls = []

    def fetch():
        calls.append(1)
        return av_csv(start=now.date(), end=now.date() + timedelta(days=7))

    data = LiveData(av_cache=AvCalendarCache(tmp_path, fetch, lambda: now))
    start, end = now.date(), now.date() + timedelta(days=7)
    one = data.av_earnings_calendar(start, end)
    two = data.av_earnings_calendar(start, end)
    assert one == two and len(calls) == 1
    assert one["reply"].startswith("symbol,name,reportDate")
    assert parse_av_calendar(one["reply"], now, start, end)["ok"] is True


# ============================ smoke_events --live ============================

def live(name, symbols, capsys, mutate=None):
    data = ScenarioData(load_scenario(name))
    if mutate:
        mutate(data)
    code = main([*symbols, "--live", "--no-dotenv"], data=data, clock=data.as_of)
    return code, capsys.readouterr().out


def test_live_prints_the_alpha_vantage_calendar_summary(capsys):
    code, out = live("clean-buy", ["BRKO"], capsys)
    assert code == 0
    assert "Alpha Vantage calendar: ok; fetched 2026-11-03T08:30:00-05:00; range " \
        "2026-11-03..2026-11-10;" in out
    assert "  columns: ['symbol', 'name', 'reportDate', 'fiscalDateEnding', 'estimate', " \
        "'currency']" in out
    assert "rows in range (plausible needs 50 on 3 trading days)" in out
    assert "rows in the reply" in out and "after the range" in out
    assert "rows per day: 2026-11-03 " in out


def test_live_still_prints_the_fmp_calendar_summary(capsys):
    _, out = live("clean-buy", ["BRKO"], capsys)
    assert "FMP calendar: ok;" in out and "rows (cap 4000)" in out


def test_live_prints_per_symbol_rows_for_both_calendars(capsys):
    _, out = live("red-flag", ["VNTR"], capsys)
    assert "alphavantage rows for VNTR: [{'symbol': 'VNTR', 'date': '2026-11-03'" in out
    assert "fmp rows for VNTR: [{'symbol': 'VNTR', 'date': '2026-11-03'" in out
    _, clean = live("clean-buy", ["BRKO"], capsys)
    assert "alphavantage rows for BRKO: none" in clean and "fmp rows for BRKO: none" in clean


def test_blocked_live_with_a_failing_alpha_vantage_prints_not_ok_and_an_unknown_verdict(capsys):
    def fail(data):
        def boom(start, end):
            raise RuntimeError("HTTP 429 from /query")
        data.av_earnings_calendar = boom

    code, out = live("clean-buy", ["BRKO"], capsys, mutate=fail)
    assert code == 0
    assert "Alpha Vantage calendar: NOT OK: HTTP 429 from /query" in out
    assert "FMP calendar: ok;" in out  # the other source is unaffected
    assert "[BRKO] verdict UNKNOWN" in out
    assert "UNKNOWN (alphavantage): earnings source unavailable: HTTP 429" in out


def test_live_fetches_the_alpha_vantage_calendar_once_for_many_symbols(capsys):
    calls = []

    def count(data):
        orig = data.av_earnings_calendar

        def wrapped(start, end):
            calls.append(1)
            return orig(start, end)

        data.av_earnings_calendar = wrapped

    _, out = live("clean-buy", ["BRKO", "VNTR", "AAPL"], capsys, mutate=count)
    assert len(calls) == 1 and out.count("verdict ") == 3


def test_live_output_never_contains_an_alpha_vantage_key_value(monkeypatch, capsys):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "SENTINEL-AV-KEY-555")
    _, out = live("red-flag", ["VNTR"], capsys)
    assert "SENTINEL-AV-KEY-555" not in out
