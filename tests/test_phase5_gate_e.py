"""Phase 5 gate E (#144): the Nasdaq trade-halt feed as a required halt source of the event
gate. Every halt case is judged through check_order (BLOCK names "event gate EVENT" or
"event gate UNKNOWN"); the passing twin of each is next to it. Fakes only, no network."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import pipeline.decide as decide
import pipeline.order_flow as of
from journal.log import APPROVED
from pipeline.events import events_from, gate_inputs, halts_input
from pipeline.scenarios import ScenarioData, load_scenario, scenario_names
from tests.fakes import (
    NOON,
    FakeAllData,
    asset_reply,
    av_csv,
    background_rows,
    fresh_halt_feed,
    halt_feed_xml,
)
from tests.fakes import halt_item_xml as hi
from tests.order_helpers import (
    T0,
    QuietGateData,
    calendar_ok,
    filings_ok,
    gate_for,
    halts_ok,
    listing_ok,
    news_item,
    news_ok,
    raw_lines,
    verdict_for,
)
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tests.test_phase5_gate_d import LIMITS, run_scenario
from tools.event_gate import (
    MAX_HALT_FEED_AGE,
    MAX_HALT_FEED_LAG,
    compact_ticker,
    event_gate,
    halt_status,
)
from tools.halt_feed import parse_halt_feed

TODAY = "10/19/2026"        # T0 is Monday 2026-10-19 10:15:30 ET
YESTERDAY = "10/16/2026"    # the Friday before
TOMORROW = "10/20/2026"


@pytest.fixture(autouse=True)
def fixed_limits(monkeypatch):
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)
    monkeypatch.setattr(decide, "load_checked_limits", lambda *a, **k: LIMITS)


def run(items=(), symbol="TSLA", *, halts=None, news=None, **kw):
    """(gate, check_order verdict) at T0 with a clean feed holding `items` unless `halts` given."""
    gate = gate_for(symbol, T0, halts=halts if halts is not None else halts_ok(T0, items),
                    news=news, **kw)
    return gate, verdict_for(gate, T0, symbol)


def assert_blocked(verdict, kind, *needles):
    assert verdict["verdict"] == "BLOCK", verdict
    (reason,) = verdict["reasons"]
    assert reason.startswith(f"event gate {kind}:"), reason
    for needle in needles:
        assert needle in reason, (needle, reason)


def assert_passes(gate, verdict):
    assert gate["status"] == "CLEAR", gate["reason"]
    assert verdict == {"verdict": "PASS", "reasons": []}


# ============================ A. a halt for the symbol today ============================

def test_clean_feed_and_clean_news_is_clear_and_passes():
    gate, v = run()
    assert_passes(gate, v)
    assert gate["halt"]["findings"] == [] and gate["halt"]["status"] == "CLEAR"
    assert gate["reason"].endswith("no halt today")


@pytest.mark.parametrize("code,desc", [
    ("LUDP", "volatility trading pause (LULD)"),
    ("LUDS", "volatility trading pause, straddle condition (LULD)"),
    ("T1", "news pending"), ("H10", "SEC trading suspension"), ("M", "volatility trading pause"),
    ("ZZ99", "halt"),
])
def test_blocked_a_halt_today_whatever_the_code(code, desc):
    gate, v = run([hi("TSLA", code, halt_date=TODAY, halt_time="10:05:00.214")])
    assert gate["status"] == "EVENT" and gate["halt"]["status"] == "EVENT"
    assert_blocked(v, "EVENT", "EVENT (nasdaq): Nasdaq trade halt today:",
                   f"{code} ({desc}) at 10:05:00 ET")
    (finding,) = [f for f in gate["findings"] if f["check"] == "halt"]
    assert finding["source"] == "nasdaq" and finding["status"] == "EVENT"


def test_blocked_a_halt_today_with_an_empty_reason_code():
    gate, v = run([hi("TSLA", "", halt_date=TODAY)])
    assert gate["status"] == "EVENT"
    assert_blocked(v, "EVENT", "Nasdaq trade halt today: no code (halt)")


def test_blocked_a_halt_today_that_already_resumed_is_still_an_event():
    gate, v = run([hi("TSLA", "LUDP", halt_date=TODAY, resumed=(TODAY, "10:10:00"))])
    assert_blocked(v, "EVENT", "Nasdaq trade halt today: LUDP")


@pytest.mark.parametrize("listed", ["BRK-B", "BRKB", "brk.b", "BRK B"])
def test_blocked_a_dotted_symbol_is_found_under_another_spelling_in_the_feed(listed):
    assert compact_ticker("BRK.B") == compact_ticker(listed)
    gate, v = run([hi(listed, "T1", halt_date=TODAY)], "BRK.B")
    assert gate["status"] == "EVENT"
    assert_blocked(v, "EVENT", "Nasdaq trade halt today: T1")


def test_a_dotted_symbol_with_no_halt_listed_passes():
    gate, v = run([hi("BRKA", "T1", halt_date=TODAY)], "BRK.B")
    assert_passes(gate, v)


def test_another_symbols_halt_today_does_not_block():
    gate, v = run([hi("AAPL", "LUDP", halt_date=TODAY), hi("TSLAX", "T1", halt_date=TODAY)])
    assert_passes(gate, v)


def test_blocked_one_listed_halt_among_other_symbols_is_found():
    gate, v = run([hi("AAPL", "T1", halt_date=TODAY), hi("TSLA", "LUDS", halt_date=TODAY),
                   hi("MSFT", "T1", halt_date=TODAY)])
    assert_blocked(v, "EVENT", "LUDS")


# ---- older and future dated items ----

def test_blocked_an_older_halt_with_no_resumption_listed():
    gate, v = run([hi("TSLA", "H10", halt_date="08/14/2025", halt_time="09:30:00")])
    assert_blocked(v, "EVENT", "halted since 2025-08-14 with no resumption listed:",
                   "H10 (SEC trading suspension)")


def test_blocked_an_older_halt_with_a_resumption_date_but_no_trade_time():
    gate, v = run([hi("TSLA", "T1", halt_date=YESTERDAY, resumed=(YESTERDAY, ""))])
    assert_blocked(v, "EVENT", "halted since 2026-10-16 with no resumption listed")


def test_an_older_halt_resumed_before_today_passes():
    gate, v = run([hi("TSLA", "T1", halt_date=YESTERDAY, resumed=(YESTERDAY, "09:45:00"))])
    assert_passes(gate, v)


def test_blocked_an_older_halt_that_resumes_today_is_still_listed_as_open():
    gate, v = run([hi("TSLA", "T1", halt_date=YESTERDAY, resumed=(TODAY, "09:45:00"))])
    assert_blocked(v, "EVENT", "halted since 2026-10-16")


def test_blocked_a_future_dated_halt():
    gate, v = run([hi("TSLA", "T1", halt_date=TOMORROW)])
    assert_blocked(v, "EVENT", "Nasdaq trade halt dated 2026-10-20: T1")


def test_blocked_a_halt_dated_today_just_after_midnight_new_york_is_today():
    gate, v = run([hi("TSLA", "T1", halt_date=TODAY, halt_time="00:00:00")])
    assert_blocked(v, "EVENT", "halt today", "at 00:00:00 ET")


# ============================ B. the feed itself is unusable: UNKNOWN ============================

def test_blocked_no_halts_input_at_all_is_unknown_never_no_halts():
    gate = event_gate("TSLA", T0, T0.replace(hour=15, minute=55), calendar=calendar_ok(T0),
                      filings=filings_ok(T0), news=news_ok())
    assert gate["status"] == "UNKNOWN"
    v = verdict_for(gate, T0)
    assert_blocked(v, "UNKNOWN", "UNKNOWN (nasdaq): Nasdaq halt feed unavailable: "
                   "no halt feed result")


def test_blocked_a_failed_halts_input_keeps_its_reason():
    gate, v = run(halts={"ok": False, "reason": "HTTP 503 from /rss.aspx"})
    assert_blocked(v, "UNKNOWN", "Nasdaq halt feed unavailable: HTTP 503 from /rss.aspx")


@pytest.mark.parametrize("bad", ["ok", ["ok"], 7, {"ok": "yes"}, {"fetched_at": "x"}],
                         ids=["str", "list", "int", "ok-not-true", "no-ok"])
def test_blocked_a_forged_halts_input_is_unknown(bad):
    gate, v = run(halts=bad)
    assert gate["status"] == "UNKNOWN"
    assert_blocked(v, "UNKNOWN", "Nasdaq halt feed unavailable")


def test_blocked_a_feed_that_failed_parsing_is_unusable():
    halts = {"ok": True, "fetched_at": (T0 - timedelta(seconds=5)).isoformat(),
             "feed": {"ok": False, "reason": "halt feed is not valid XML"}}
    gate, v = run(halts=halts)
    assert_blocked(v, "UNKNOWN", "Nasdaq halt feed unusable: halt feed is not valid XML")


@pytest.mark.parametrize("feed", [
    {"ok": True, "items": []},                                  # no pub_date
    {"ok": True, "pub_date": "garbage", "items": []},
    {"ok": True, "pub_date": "2026-10-19T14:14:00+00:00", "items": "x"},
    {"ok": True, "pub_date": "2026-10-19T14:14:00+00:00"},
    {"ok": True, "pub_date": "2026-10-19T14:14:00", "items": []},  # naive
], ids=["no-pub", "bad-pub", "items-not-list", "no-items", "naive-pub"])
def test_blocked_a_malformed_parsed_feed_is_unknown(feed):
    halts = {"ok": True, "fetched_at": (T0 - timedelta(seconds=5)).isoformat(), "feed": feed}
    gate, v = run(halts=halts)
    assert gate["status"] == "UNKNOWN"
    assert_blocked(v, "UNKNOWN", "Nasdaq halt feed result is malformed")


def test_blocked_a_bad_item_in_the_parsed_feed_is_unknown():
    feed = parse_halt_feed(halt_feed_xml(T0 - timedelta(seconds=60), [hi("TSLA", "T1")]))
    feed["items"][0]["halt_date"] = "not-a-date"
    halts = {"ok": True, "fetched_at": (T0 - timedelta(seconds=5)).isoformat(), "feed": feed}
    gate, v = run(halts=halts)
    assert_blocked(v, "UNKNOWN", "Nasdaq halt feed result is malformed")


def test_blocked_an_unparsable_fetched_at_is_unknown():
    halts = halts_ok(T0)
    halts["fetched_at"] = "yesterday"
    assert_blocked(run(halts=halts)[1], "UNKNOWN", "Nasdaq halt feed result is malformed")


# ---- freshness boundaries ----

def test_a_feed_fetched_exactly_three_minutes_ago_is_fresh():
    assert MAX_HALT_FEED_AGE == timedelta(minutes=3)
    gate, v = run(halts=halts_ok(T0, fetched_ago_s=180, pub_ago_s=200))
    assert_passes(gate, v)


def test_blocked_a_feed_fetched_just_over_three_minutes_ago_is_stale():
    gate, v = run(halts=halts_ok(T0, fetched_ago_s=181, pub_ago_s=200))
    assert_blocked(v, "UNKNOWN", "Nasdaq halt feed is stale (fetched 10:12:29 ET)")


def test_a_pubdate_exactly_ten_minutes_before_the_fetch_is_acceptable():
    assert MAX_HALT_FEED_LAG == timedelta(minutes=10)
    gate, v = run(halts=halts_ok(T0, fetched_ago_s=5, pub_ago_s=605))
    assert_passes(gate, v)


def test_blocked_a_pubdate_over_ten_minutes_before_the_fetch_means_not_updated():
    gate, v = run(halts=halts_ok(T0, fetched_ago_s=5, pub_ago_s=606))
    assert_blocked(v, "UNKNOWN", "Nasdaq halt feed not updated since 10:05:24 ET")


def test_a_fetch_five_minutes_ahead_is_within_the_clock_skew():
    gate, v = run(halts=halts_ok(T0, fetched_ago_s=-300, pub_ago_s=-290))
    assert_passes(gate, v)


def test_blocked_a_fetch_dated_over_five_minutes_in_the_future():
    gate, v = run(halts=halts_ok(T0, fetched_ago_s=-301, pub_ago_s=-290))
    assert_blocked(v, "UNKNOWN", "Nasdaq halt feed fetch is dated in the future")


def test_blocked_a_stale_feed_that_lists_a_halt_today_is_still_an_event_first():
    halts = halts_ok(T0, [hi("TSLA", "T1", halt_date=TODAY)], fetched_ago_s=400, pub_ago_s=410)
    gate, v = run(halts=halts)
    # the stale feed is UNKNOWN and it stops there: its items are not trusted either way
    assert gate["status"] == "UNKNOWN"
    assert_blocked(v, "UNKNOWN", "stale")


# ============================ C. the two halt sources together ============================

def halt_headline(symbol="TSLA"):
    return news_item(symbol, f"Trading halted in {symbol} pending news", T0 - timedelta(hours=1))


def test_blocked_clean_news_and_a_feed_halt_is_an_event():
    gate, v = run([hi("TSLA", "LUDP", halt_date=TODAY)], news=news_ok())
    assert_blocked(v, "EVENT", "EVENT (nasdaq)")


def test_blocked_a_news_halt_with_a_clean_feed_is_still_an_event():
    gate, v = run(news=news_ok([halt_headline()]))
    assert_blocked(v, "EVENT", "EVENT (news): trading halt today")


def test_blocked_both_sources_showing_the_halt_name_both():
    gate, v = run([hi("TSLA", "LUDP", halt_date=TODAY)], news=news_ok([halt_headline()]))
    assert gate["status"] == "EVENT"
    (reason,) = v["reasons"]
    assert "EVENT (nasdaq)" in reason and "EVENT (news)" in reason
    assert {f["source"] for f in gate["halt"]["findings"]} == {"nasdaq", "news"}


def test_blocked_a_failed_news_fetch_with_a_clean_feed_is_unknown():
    gate, v = run(news={"ok": False, "reason": "HTTP 500 from /v1beta1/news"})
    assert gate["status"] == "UNKNOWN"
    assert_blocked(v, "UNKNOWN", "UNKNOWN (news): news unavailable (HTTP 500")
    assert "nasdaq" not in v["reasons"][0]


def test_blocked_a_failed_feed_with_clean_news_is_unknown_and_names_only_nasdaq():
    gate, v = run(halts={"ok": False, "reason": "request to /rss.aspx timed out"}, news=news_ok())
    assert_blocked(v, "UNKNOWN", "UNKNOWN (nasdaq): Nasdaq halt feed unavailable: request to")
    assert "(news)" not in v["reasons"][0]


def test_blocked_a_feed_event_beats_a_news_failure():
    halts = halts_ok(T0, [hi("TSLA", "T1", halt_date=TODAY)])
    gate, v = run(halts=halts, news={"ok": False, "reason": "x"})
    assert gate["status"] == "EVENT"
    assert_blocked(v, "EVENT")


def test_halt_status_is_feed_findings_then_news_findings():
    h = halt_status("TSLA", T0, news_ok([halt_headline()]),
                    halts_ok(T0, [hi("TSLA", "T1", halt_date=TODAY)]), listing_ok("TSLA"))
    assert h["status"] == "EVENT"
    assert [f["source"] for f in h["findings"]] == ["nasdaq", "news"]


def test_halt_status_without_halts_is_unknown_not_clear():
    assert halt_status("TSLA", T0, news_ok())["status"] == "UNKNOWN"
    assert halt_status("TSLA", T0, news_ok(), None)["status"] == "UNKNOWN"


def test_a_clear_gate_is_json_serializable():
    json.dumps(run()[0])


# ============================ D. through events_from / halts_input ============================

def data_for(now, **over):
    """A clean data object (calendars, SEC, news, fresh halt feed); parts replaced via kwargs."""
    d = dict(
        av_earnings_calendar=lambda s, e: {
            "fetched_at": (now - timedelta(minutes=1)).isoformat(), "from": s.isoformat(),
            "to": e.isoformat(), "reply": av_csv(start=s, end=e)},
        earnings_calendar=lambda s, e: {"fetched_at": (now - timedelta(minutes=1)).isoformat(),
                                        "from": s.isoformat(), "to": e.isoformat(),
                                        "reply": background_rows(s, e)},
        submissions=lambda sym: filings_ok(now, symbol=sym)["submissions"],
        news=lambda syms, a, b: {"news": [], "next_page_token": None},
        halt_feed=lambda: fresh_halt_feed(now),
        listing=lambda sym: asset_reply(sym))
    d.update(over)
    return SimpleNamespace(**d)


WIN = T0.replace(hour=15, minute=55, second=0)


def gate_of(data):
    return events_from(data)("TSLA", T0, WIN)


def test_events_from_with_a_clean_feed_is_clear_and_passes():
    gate = gate_of(data_for(T0))
    assert gate["status"] == "CLEAR", gate["reason"]
    assert verdict_for(gate, T0) == {"verdict": "PASS", "reasons": []}


def test_blocked_events_from_a_listed_halt_today_is_an_event():
    feed = lambda: fresh_halt_feed(T0, [hi("TSLA", "LUDP", halt_date=TODAY)])  # noqa: E731
    gate = gate_of(data_for(T0, halt_feed=feed))
    assert gate["status"] == "EVENT"
    assert_blocked(verdict_for(gate, T0), "EVENT", "Nasdaq trade halt today: LUDP")


def test_blocked_data_without_a_halt_feed_is_unknown_no_halt_feed_source():
    data = data_for(T0)
    del data.halt_feed
    gate = gate_of(data)
    assert gate["status"] == "UNKNOWN"
    assert_blocked(verdict_for(gate, T0), "UNKNOWN", "Nasdaq halt feed unavailable: "
                   "no halt feed source")


@pytest.mark.parametrize("exc,why", [
    (RuntimeError("request to /rss.aspx timed out"), "request to /rss.aspx timed out"),
    (RuntimeError("HTTP 503 from /rss.aspx"), "HTTP 503 from /rss.aspx"),
    (KeyError("secret-token"), "KeyError"),
    (ValueError("secret-token"), "ValueError"),
], ids=["timeout", "503", "keyerror", "valueerror"])
def test_blocked_a_raising_feed_is_unknown_with_the_safe_reason(exc, why):
    def boom():
        raise exc
    gate = gate_of(data_for(T0, halt_feed=boom))
    assert gate["status"] == "UNKNOWN"
    assert_blocked(verdict_for(gate, T0), "UNKNOWN", f"Nasdaq halt feed unavailable: {why}")
    assert "secret-token" not in gate["reason"]


@pytest.mark.parametrize("body,why", [
    (b"<rss><channel>", "halt feed is not valid XML"),
    (b"<html/>", "halt feed must have exactly one rss channel"),
    ("text, not bytes", "halt feed body is not bytes"),
    (halt_feed_xml(T0, [hi("TSLA", "T1", halt_date=TODAY)], num_items=9),
     "lists 1 items but numItems is 9"),
], ids=["bad-xml", "wrong-root", "not-bytes", "cut-short"])
def test_blocked_a_bad_feed_body_is_unknown_with_the_parse_reason(body, why):
    gate = gate_of(data_for(T0, halt_feed=lambda: {"fetched_at": T0.isoformat(), "body": body}))
    assert gate["status"] == "UNKNOWN"
    assert_blocked(verdict_for(gate, T0), "UNKNOWN", "Nasdaq halt feed unavailable:", why)


def test_blocked_a_cut_short_feed_hides_a_halt_so_the_gate_must_not_read_clear():
    body = halt_feed_xml(T0 - timedelta(seconds=30), [hi("TSLA", "T1", halt_date=TODAY)],
                         num_items=2)
    gate = gate_of(data_for(T0, halt_feed=lambda: {"fetched_at": T0.isoformat(), "body": body}))
    assert gate["status"] != "CLEAR"


def test_blocked_a_stale_fetch_through_events_from_is_unknown():
    old = lambda: fresh_halt_feed(T0 - timedelta(minutes=4))  # noqa: E731
    gate = gate_of(data_for(T0, halt_feed=old))
    assert_blocked(verdict_for(gate, T0), "UNKNOWN", "Nasdaq halt feed is stale")


def test_halts_input_success_shape():
    out = halts_input(data_for(T0))
    assert out["ok"] is True and out["feed"]["ok"] is True and out["feed"]["items"] == []
    assert out["fetched_at"] == (T0 - timedelta(seconds=5)).isoformat()


def test_halts_input_missing_body_key_is_a_failure_by_type_name():
    out = halts_input(SimpleNamespace(halt_feed=lambda: {"fetched_at": "x"}))
    assert out == {"ok": False, "reason": "KeyError"}


def test_gate_inputs_passes_shared_halts_and_calendar_through():
    sentinel_h = {"ok": False, "reason": "shared-h"}
    sentinel_c = {"ok": False, "reason": "shared-c"}
    sentinel_f = {"ok": False, "reason": "shared-f"}
    calls = []
    data = data_for(T0, halt_feed=lambda: calls.append(1),
                    av_earnings_calendar=lambda s, e: calls.append(2),
                    earnings_calendar=lambda s, e: calls.append(3))
    inputs = gate_inputs(data, "TSLA", T0, calendar=sentinel_c, halts=sentinel_h,
                         fmp_calendar=sentinel_f)
    assert inputs["halts"] is sentinel_h and inputs["calendar"] is sentinel_c
    assert inputs["fmp_calendar"] is sentinel_f and calls == []
    assert set(inputs) == {"calendar", "fmp_calendar", "filings", "news", "halts", "listing"}


def test_gate_inputs_reads_the_feed_once_per_call_for_each_symbol_when_not_shared():
    calls = []
    data = data_for(T0, halt_feed=lambda: calls.append(1) or fresh_halt_feed(T0))
    gate_inputs(data, "TSLA", T0)
    gate_inputs(data, "AAPL", T0)
    assert len(calls) == 2  # the 60 s cache lives in LiveData, not here


# the shared test fakes carry a clean, fresh feed too
def test_fake_all_data_reports_a_clean_fresh_feed_at_the_gate_clock():
    gate = events_from(FakeAllData())("ABCD", NOON, NOON.replace(hour=15, minute=55))
    assert not [f for f in gate["findings"] if f["source"] == "nasdaq"], gate["reason"]
    window = NOON.replace(hour=15, minute=55)
    quiet = events_from(QuietGateData(FakeAllData()))("ABCD", NOON, window)
    assert quiet["status"] == "CLEAR", quiet["reason"]


def test_blocked_fake_all_data_with_a_listed_halt_is_an_event():
    fake = FakeAllData()
    fake.halt_items = (hi("ABCD", "LUDP", halt_date="11/03/2026"),)
    gate = events_from(QuietGateData(fake))("ABCD", NOON, NOON.replace(hour=15, minute=55))
    assert gate["status"] == "EVENT" and "Nasdaq trade halt today: LUDP" in gate["reason"]


# ============================ E. the saved scenarios ============================

def scenario_gate(name):
    data = ScenarioData(load_scenario(name))
    now = data.as_of()
    return data, now, events_from(data)(data.symbol, now, now.replace(hour=15, minute=55))


def test_there_are_still_four_scenarios():
    assert len(scenario_names()) == 4


@pytest.mark.parametrize("name", ["clean-buy", "extended-chase", "tsla"])
def test_clean_scenarios_have_a_clear_halt_status_and_a_clear_gate(name):
    data, now, gate = scenario_gate(name)
    assert gate["status"] == "CLEAR", (name, gate["reason"])
    assert gate["halt"]["findings"] == []


def test_blocked_red_flag_lists_the_nasdaq_ludp_pause_at_ten_oh_five():
    data, now, gate = scenario_gate("red-flag")
    assert data.symbol == "VNTR" and gate["status"] == "EVENT"
    nasdaq = [f for f in gate["findings"] if f["source"] == "nasdaq"]
    assert len(nasdaq) == 1 and nasdaq[0]["status"] == "EVENT"
    assert "Nasdaq trade halt today: LUDP" in nasdaq[0]["reason"]
    assert "at 10:05:00 ET" in nasdaq[0]["reason"]
    assert any(f["source"] == "news" and f["status"] == "EVENT" for f in gate["findings"])


def test_other_issues_in_a_scenario_feed_do_not_block_the_scenario_symbol():
    # the red-flag feed also lists QZXA (open since 2025), WYLD, PLMX; clean-buy's lists others
    data = ScenarioData(load_scenario("clean-buy"))
    feed = parse_halt_feed(data.halt_feed()["body"])
    assert feed["ok"] and feed["items"], "the clean-buy feed should list other issues"
    assert data.symbol not in {i["symbol"] for i in feed["items"]}


def test_scenario_halt_feed_is_fetched_thirty_seconds_before_the_clock():
    for name in scenario_names():
        data = ScenarioData(load_scenario(name))
        out = data.halt_feed()
        assert out["fetched_at"] == (data.as_of() - timedelta(seconds=30)).isoformat()
        assert isinstance(out["body"], bytes) and parse_halt_feed(out["body"])["ok"]


def test_the_tsla_fixture_feed_parses_and_does_not_list_tsla():
    from pathlib import Path
    body = (Path(__file__).parent / "fixtures" / "nasdaq_halts_tsla.xml").read_bytes()
    feed = parse_halt_feed(body)
    assert feed["ok"] and "TSLA" not in {i["symbol"] for i in feed["items"]}


# ============================ F. #144 end to end, every model says BUY ============================

def test_blocked_clean_buy_whose_only_problem_is_a_ludp_pause_in_the_nasdaq_feed(
        monkeypatch, tmp_path):
    """The case the issue is about: no headline mentions the pause, only the Nasdaq feed does."""
    def with_pause(self):
        now = self.as_of()
        item = hi("BRKO", "LUDP", halt_date="11/03/2026", halt_time="10:58:10.500")
        return fresh_halt_feed(now - timedelta(seconds=25), [item])

    monkeypatch.setattr(ScenarioData, "halt_feed", with_pause)
    result, journal, asked, client = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.trader.decision.action == "BUY"  # the Trader did say BUY
    p = result.proposal
    assert p.ok is False and p.stage == "risk"
    assert "event gate EVENT:" in p.reason
    assert "EVENT (nasdaq): Nasdaq trade halt today: LUDP" in p.reason
    assert "at 10:58:10 ET" in p.reason
    assert "(news)" not in p.reason and "earnings" not in p.reason.replace("no earnings", "")
    assert p.gate["status"] == "EVENT" and p.verdict["verdict"] == "BLOCK"
    assert result.outcome.status == "no_order" and asked == []
    assert result.ticket.startswith("NO ORDER for plan plan-1: event gate EVENT:")
    assert "ORDER TICKET" not in result.ticket
    (entry,) = raw_lines(journal)
    assert entry["status"] == "no_order" and entry["order"] is None
    assert entry["gate"]["status"] == "EVENT" and "Nasdaq trade halt" in entry["gate"]["reason"]


def test_clean_buy_unchanged_still_passes_with_the_scenarios_own_feed(monkeypatch, tmp_path):
    result, journal, asked, _ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.proposal.ok, result.proposal.reason
    assert result.proposal.gate["status"] == "CLEAR"
    assert result.outcome.status == APPROVED and len(asked) == 1


def test_blocked_clean_buy_is_refused_unknown_when_the_halt_feed_fetch_fails(
        monkeypatch, tmp_path):
    def boom(self):
        raise RuntimeError("HTTP 503 from /rss.aspx")

    monkeypatch.setattr(ScenarioData, "halt_feed", boom)
    result, journal, asked, _ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    p = result.proposal
    assert p.ok is False and p.gate["status"] == "UNKNOWN"
    assert ("event gate UNKNOWN: UNKNOWN (nasdaq): Nasdaq halt feed unavailable: "
            "HTTP 503 from /rss.aspx") in p.reason
    assert result.outcome.status == "no_order" and asked == []


def test_blocked_clean_buy_is_refused_when_the_feed_is_cut_short(monkeypatch, tmp_path):
    def cut(self):
        now = self.as_of()
        other = hi("QQQQ", "T1", halt_date="11/03/2026")
        body = halt_feed_xml(now - timedelta(seconds=60), [other], num_items=4)
        return {"fetched_at": (now - timedelta(seconds=5)).isoformat(), "body": body}

    monkeypatch.setattr(ScenarioData, "halt_feed", cut)
    result, *_ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.proposal.ok is False
    assert "event gate UNKNOWN:" in result.proposal.reason and "cut short" in result.proposal.reason


def test_blocked_clean_buy_refused_when_the_feed_went_stale_before_the_recheck(
        monkeypatch, tmp_path):
    """A feed that was fresh when proposed but is 4 minutes old at the approval re-check
    cannot be the basis of a yes: the re-check asks the gate again."""
    def old(self):
        return fresh_halt_feed(self.as_of() - timedelta(minutes=4))

    monkeypatch.setattr(ScenarioData, "halt_feed", old)
    result, *_ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.proposal.ok is False
    assert "Nasdaq halt feed is stale" in result.proposal.reason


def test_the_nasdaq_halt_is_independent_of_trader_confidence(monkeypatch, tmp_path):
    def with_pause(self):
        return fresh_halt_feed(self.as_of(), [hi("BRKO", "T1", halt_date="11/03/2026")])

    monkeypatch.setattr(ScenarioData, "halt_feed", with_pause)
    for conf in (0.01, 0.99):
        sub = tmp_path / str(conf)
        sub.mkdir()
        result, *_ = run_scenario(monkeypatch, sub, "clean-buy", trader_confidence=conf)
        assert result.proposal.ok is False
        assert "Nasdaq trade halt today: T1" in result.proposal.reason


# ============================ code review (#144) ============================

def test_blocked_a_pubdate_in_the_future_is_unknown_not_fresh():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
    from tests.order_helpers import gate_for, halts_ok
    now = datetime(2026, 11, 3, 11, 0, tzinfo=NY)
    halts = halts_ok(now, pub_ago_s=-24 * 3600)  # pubDate a day ahead
    gate = gate_for("TSLA", now, halts=halts)
    assert gate["status"] == "UNKNOWN"
    assert "Nasdaq halt feed pubDate" in gate["reason"] and "is in the future" in gate["reason"]


def test_a_pubdate_a_few_minutes_ahead_is_within_the_skew():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
    from tests.order_helpers import gate_for, halts_ok
    now = datetime(2026, 11, 3, 11, 0, tzinfo=NY)
    assert gate_for("TSLA", now, halts=halts_ok(now, pub_ago_s=-60))["status"] == "CLEAR"


def test_an_attribute_error_inside_the_fetch_is_not_reported_as_no_source():
    from types import SimpleNamespace

    from pipeline.events import halts_input

    def broken():
        raise AttributeError("inside the fetch")

    out = halts_input(SimpleNamespace(halt_feed=broken))
    assert out["ok"] is False and out["reason"] == "AttributeError"
    assert halts_input(SimpleNamespace())["reason"] == "no halt feed source"
