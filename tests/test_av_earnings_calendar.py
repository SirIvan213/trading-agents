"""Alpha Vantage earnings calendar: fetch, parse, cache and the whole chain to the gate (#167).

A fake opener stands in for the network; the fixtures in tests/fixtures/alphavantage/ are
SYNTHETIC (see the README there). Nothing here reads a real key or makes a request. Every
failure goes all the way: fetch -> AvCalendarCache -> events_from -> event_gate -> check_order.
Alpha Vantage is the PRIMARY calendar, so every failure and every malformed reply must end
UNKNOWN and BLOCK, and only a good reply may be CLEAR.
"""
import io
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.events import AV_RETRY_AFTER, AvCalendarCache, _failure, events_from
from tests.fakes import av_csv, fresh_halt_feed
from tests.order_helpers import NY, submissions_202, verdict_for
from tools.earnings_calendar import AV_MAX_BYTES, fetch_av_earnings_calendar, parse_av_calendar

KEY = "SENTINEL-AV-KEY-7c1d"
NOW = datetime(2026, 11, 9, 10, 30, tzinfo=NY)  # a Monday
START, END = date(2026, 11, 9), date(2026, 11, 16)
AV = "https://www.alphavantage.co/query"
FIX = Path(__file__).parent / "fixtures" / "alphavantage"


def good_text() -> str:
    """The good fixture with LF line ends (the file is CRLF; both are tested below)."""
    return (FIX / "av_earnings_calendar_good.csv").read_bytes().decode("utf-8").replace(
        "\r\n", "\n")


def rate_limit_text() -> str:
    return (FIX / "av_rate_limit.json").read_text(encoding="utf-8")


class Resp:
    def __init__(self, body: bytes, sizes: list):
        self._b, self._sizes = body, sizes

    def read(self, n=None):
        self._sizes.append(n)
        return self._b if n is None else self._b[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(body):
    """An opener that serves `body` (bytes or str), recording requests and read sizes."""
    raw = body.encode("utf-8") if isinstance(body, str) else body
    seen, sizes = [], []

    def op(req, timeout=None):
        seen.append(req)
        return Resp(raw, sizes)

    op.seen, op.sizes = seen, sizes
    return op


def opener_raising(exc):
    seen = []

    def op(req, timeout=None):
        seen.append(req)
        raise exc

    op.seen = seen
    return op


def http_error(code):
    return urllib.error.HTTPError(f"{AV}?apikey={KEY}", code, "x", {}, io.BytesIO(b""))


@pytest.fixture(autouse=True)
def av_key(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", KEY)


# ======================= fetch: the request =======================

def test_the_request_asks_for_the_whole_market_three_month_csv_with_the_key():
    op = opener_returning(good_text())
    fetch_av_earnings_calendar(opener=op)
    assert len(op.seen) == 1
    parts = urllib.parse.urlsplit(op.seen[0].full_url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AV
    assert urllib.parse.parse_qs(parts.query) == {
        "function": ["EARNINGS_CALENDAR"], "horizon": ["3month"], "apikey": [KEY]}


def test_a_symbol_is_never_requested():
    op = opener_returning(good_text())
    fetch_av_earnings_calendar(opener=op)
    assert "symbol" not in urllib.parse.urlsplit(op.seen[0].full_url).query


def test_the_body_is_returned_as_text():
    text = fetch_av_earnings_calendar(opener=opener_returning(good_text()))
    assert text == good_text() and text.startswith("symbol,name,reportDate")


def test_a_leading_utf8_bom_is_stripped_once():
    text = fetch_av_earnings_calendar(opener=opener_returning(b"\xef\xbb\xbf" + b"symbol\n"))
    assert text == "symbol\n"
    twice = fetch_av_earnings_calendar(opener=opener_returning(b"\xef\xbb\xbf\xef\xbb\xbfx"))
    assert twice == "﻿x"  # only one is stripped; the parser refuses what is left


def test_blocked_a_missing_key_raises_and_makes_no_request(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY")
    op = opener_returning(good_text())
    with pytest.raises(RuntimeError, match="ALPHA_VANTAGE_API_KEY must be set"):
        fetch_av_earnings_calendar(opener=op)
    assert op.seen == []


@pytest.mark.parametrize("value", [""])
def test_blocked_an_empty_key_raises_and_makes_no_request(monkeypatch, value):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", value)
    op = opener_returning(good_text())
    with pytest.raises(RuntimeError, match="ALPHA_VANTAGE_API_KEY must be set"):
        fetch_av_earnings_calendar(opener=op)
    assert op.seen == []


FAILURES = [
    ("timeout", lambda: TimeoutError("slow"), "timed out"),
    ("url-timeout", lambda: urllib.error.URLError(TimeoutError("slow")), "timed out"),
    ("http-429", lambda: http_error(429), "HTTP 429"),
    ("http-500", lambda: http_error(500), "HTTP 500"),
    ("http-503", lambda: http_error(503), "HTTP 503"),
    ("tls", lambda: urllib.error.URLError(ssl.SSLError("bad cert")), "SSLError"),
    ("refused", lambda: ConnectionRefusedError("no"), "ConnectionRefusedError"),
]
FAIL_PARAMS = [pytest.param(m, w, id=i) for i, m, w in FAILURES]


@pytest.mark.parametrize("make,message", FAIL_PARAMS)
def test_blocked_a_transport_failure_raises_naming_the_path_never_the_key(make, message):
    with pytest.raises(RuntimeError) as ei:
        fetch_av_earnings_calendar(opener=opener_raising(make()))
    text = str(ei.value)
    assert message in text and "/query" in text
    assert KEY not in text and "apikey" not in text


def test_blocked_a_body_that_is_not_utf8_raises():
    with pytest.raises(RuntimeError, match="not UTF-8"):
        fetch_av_earnings_calendar(opener=opener_returning(b"symbol\n\xff\xfe\x00\n"))


def test_blocked_a_body_over_the_size_cap_raises_and_is_read_only_one_byte_past_it():
    op = opener_returning(b"a" * (AV_MAX_BYTES + 50))
    with pytest.raises(RuntimeError, match="too large"):
        fetch_av_earnings_calendar(opener=op)
    assert op.sizes == [AV_MAX_BYTES + 1]


def test_a_body_exactly_at_the_size_cap_is_accepted():
    text = fetch_av_earnings_calendar(opener=opener_returning(b"a" * AV_MAX_BYTES))
    assert len(text) == AV_MAX_BYTES


# ======================= parse: a good reply =======================

def test_a_good_reply_is_ok_with_estimated_rows_only_in_range():
    cal = parse_av_calendar(good_text(), NOW, START, END)
    assert cal["ok"] is True and cal["source"] == "alphavantage"
    assert cal["from"] == "2026-11-09" and cal["to"] == "2026-11-16"
    assert cal["fetched_at"] == NOW.isoformat()
    assert cal["columns"] == ["symbol", "name", "reportDate", "fiscalDateEnding", "estimate",
                              "currency"]
    assert len(cal["rows"]) == 53 and cal["total_rows"] == 68 and cal["rows_after_end"] == 15
    assert all(r["time"] is None and r["confirmed"] is False for r in cal["rows"])
    assert all("2026-11-09" <= r["date"] <= "2026-11-16" for r in cal["rows"])
    assert {"symbol": "TSLA", "date": "2026-11-13", "time": None, "confirmed": False} \
        in cal["rows"]


def test_rows_after_the_range_are_counted_then_dropped():
    cal = parse_av_calendar(good_text(), NOW, START, date(2026, 11, 12))
    assert cal["ok"] is True and cal["rows_after_end"] == 16  # TSLA's 11-13 row is now after
    assert max(r["date"] for r in cal["rows"]) == "2026-11-12"


def test_an_extra_time_of_day_column_is_accepted_and_ignored():
    text = (FIX / "av_earnings_calendar_time_of_day.csv").read_bytes().decode("utf-8")
    cal = parse_av_calendar(text, NOW, START, END)
    assert cal["ok"] is True and cal["columns"][-1] == "timeOfTheDay"
    assert len(cal["rows"]) == 53
    assert all(r["time"] is None and r["confirmed"] is False for r in cal["rows"])


def test_crlf_and_lf_line_ends_parse_the_same():
    lf = good_text()
    crlf = lf.replace("\n", "\r\n")
    assert "\r\n" not in lf
    assert parse_av_calendar(lf, NOW, START, END) == parse_av_calendar(crlf, NOW, START, END)
    assert parse_av_calendar(crlf, NOW, START, END)["ok"] is True


def test_a_quoted_comma_in_a_name_parses_as_one_field():
    text = good_text()
    assert '"Synthetic 5, Holdings Inc."' in text
    cal = parse_av_calendar(text, NOW, START, END)
    assert cal["ok"] is True and any(r["symbol"] == "SYN005" for r in cal["rows"])


def test_a_leading_bom_and_blank_lines_are_tolerated_and_symbols_upper_cased():
    text = "﻿" + good_text().replace("TSLA,", " tsla ,") + "\n\n"
    cal = parse_av_calendar(text, NOW, START, END)
    assert cal["ok"] is True and any(r["symbol"] == "TSLA" for r in cal["rows"])


# ======================= parse: malformed replies =======================

HEAD = "symbol,name,reportDate\n"
IN_RANGE = "AAA,A Inc,2026-11-10\n"
AFTER = "BBB,B Inc,2026-12-01\n"
GOOD_MIN = HEAD + IN_RANGE + AFTER

MALFORMED = [
    ("json-message", rate_limit_text(), "message instead of the calendar"),
    ("json-list", "[]", "message instead of the calendar"),
    ("empty", "", "is empty"),
    ("blank", " \n \n", "is empty"),
    ("nul", GOOD_MIN + "\x00", "NUL"),
    ("not-text-none", None, "not text"),
    ("not-text-bytes", GOOD_MIN.encode(), "not text"),
    ("not-text-list", [GOOD_MIN], "not text"),
    ("bad-csv", 'symbol,name,reportDate\n"AAA,A Inc,2026-11-10\n', "not valid CSV"),
    ("no-symbol-column", "ticker,name,reportDate\n" + IN_RANGE + AFTER, "no symbol column"),
    ("no-date-column", "symbol,name,date\n" + IN_RANGE + AFTER, "no reportDate column"),
    ("repeated-column", "symbol,name,symbol,reportDate\nA,B,C,2026-11-10\n", "repeats a column"),
    ("too-few-fields", HEAD + "AAA,2026-11-10\n" + AFTER, "has 2 fields, the header has 3"),
    ("too-many-fields", HEAD + "AAA,A,2026-11-10,x\n" + AFTER, "has 4 fields"),
    ("blank-symbol", HEAD + ",A Inc,2026-11-10\n" + AFTER, "has no symbol"),
    ("space-symbol", HEAD + "  ,A Inc,2026-11-10\n" + AFTER, "has no symbol"),
    ("date-text", HEAD + "AAA,A,tomorrow\n" + AFTER, "no valid report date"),
    ("date-month", HEAD + "AAA,A,2026-13-40\n" + AFTER, "no valid report date"),
    ("date-us-format", HEAD + "AAA,A,11/10/2026\n" + AFTER, "no valid report date"),
    ("date-empty", HEAD + "AAA,A,\n" + AFTER, "no valid report date"),
    ("row-before-start", HEAD + IN_RANGE + "OLD,O Inc,2026-11-06\n" + AFTER,
     "before 2026-11-09"),
    ("no-row-in-range", HEAD + AFTER, "lists no reports in 2026-11-09..2026-11-16"),
    ("header-only", HEAD, "lists no reports in"),
    ("no-row-after-end", HEAD + IN_RANGE, "lists no reports after 2026-11-16"),
]
MAL_PARAMS = [pytest.param(m[1], m[2], id=m[0]) for m in MALFORMED]


def test_the_minimal_good_reply_is_ok():
    assert parse_av_calendar(GOOD_MIN, NOW, START, END)["ok"] is True


@pytest.mark.parametrize("reply,why", MAL_PARAMS)
def test_a_malformed_reply_is_not_ok_and_names_why(reply, why):
    cal = parse_av_calendar(reply, NOW, START, END)
    assert cal["ok"] is False and why in cal["reason"]
    assert "rows" not in cal


def test_one_bad_row_among_many_good_ones_makes_the_whole_reply_malformed():
    cal = parse_av_calendar(good_text() + "BAD,Bad Inc,not-a-date,2026-09-30,,USD\n", NOW,
                            START, END)
    assert cal["ok"] is False and "no valid report date" in cal["reason"]


def test_the_rate_limit_message_is_not_ok_and_its_text_is_not_echoed():
    cal = parse_av_calendar(rate_limit_text(), NOW, START, END)
    assert cal["ok"] is False
    assert "25 requests" not in cal["reason"] and "SYNTHETIC" not in cal["reason"]
    assert "premium" not in cal["reason"]


def test_a_long_server_symbol_is_cut_in_the_reason():
    sym = "X" * 60
    for text in (HEAD + f"{sym},A,not-a-date\n", HEAD + f"{sym},A,2026-11-01\n"):
        cal = parse_av_calendar(text + AFTER, NOW, START, END)
        assert cal["ok"] is False
        assert "X" * 16 in cal["reason"] and "X" * 17 not in cal["reason"]


def test_a_naive_fetch_time_is_not_ok():
    cal = parse_av_calendar(good_text(), datetime(2026, 11, 9, 8, 30), START, END)
    assert cal["ok"] is False and "timezone-aware" in cal["reason"]


# ======================= the chain: fetch -> cache -> gate -> check_order =======================

def make_data(cache):
    return SimpleNamespace(
        av_earnings_calendar=cache.get,
        submissions=lambda sym: submissions_202(NOW, 30, symbol=sym),
        halt_feed=lambda: fresh_halt_feed(NOW),
        news=lambda syms, s, e: {"news": [], "next_page_token": None})


def chain(tmp_path, opener, symbol="TSLA"):
    """data -> AvCalendarCache(fetch via the fake opener) -> events_from -> the gate result."""
    cache = AvCalendarCache(tmp_path, lambda: fetch_av_earnings_calendar(opener=opener),
                            lambda: NOW)
    return events_from(make_data(cache))(symbol, NOW, NOW.replace(hour=15, minute=55))


UNAVAILABLE = "UNKNOWN (alphavantage): earnings source unavailable:"


@pytest.mark.parametrize("make,message", FAIL_PARAMS)
def test_blocked_a_fetch_failure_ends_unknown_and_is_not_cached(tmp_path, make, message):
    gate = chain(tmp_path, opener_raising(make()))
    assert gate["status"] == "UNKNOWN" and UNAVAILABLE in gate["reason"]
    assert message in gate["reason"]
    assert KEY not in gate["reason"] and "apikey" not in gate["reason"]
    assert list(tmp_path.iterdir()) == []  # a failure is never cached
    assert verdict_for(gate, NOW)["verdict"] == "BLOCK"


def test_blocked_a_missing_key_ends_unknown_and_makes_no_request(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY")
    op = opener_returning(good_text())
    gate = chain(tmp_path, op)
    assert gate["status"] == "UNKNOWN" and UNAVAILABLE in gate["reason"]
    assert "ALPHA_VANTAGE_API_KEY must be set" in gate["reason"] and op.seen == []
    assert verdict_for(gate, NOW)["verdict"] == "BLOCK"


TEXT_MAL_PARAMS = [pytest.param(m[1], m[2], id=m[0]) for m in MALFORMED
                   if isinstance(m[1], str)]  # the fetch always returns text


@pytest.mark.parametrize("reply,why", TEXT_MAL_PARAMS)
def test_blocked_a_malformed_reply_ends_unknown_and_is_not_cached(tmp_path, reply, why):
    gate = chain(tmp_path, opener_returning(reply))
    assert gate["status"] == "UNKNOWN" and UNAVAILABLE in gate["reason"]
    assert why in gate["reason"] and KEY not in gate["reason"]
    assert list(tmp_path.iterdir()) == []
    assert verdict_for(gate, NOW)["verdict"] == "BLOCK"


def test_the_rate_limit_message_through_the_chain_is_unknown_and_not_echoed(tmp_path):
    gate = chain(tmp_path, opener_returning(rate_limit_text()))
    assert gate["status"] == "UNKNOWN" and UNAVAILABLE in gate["reason"]
    assert "25 requests" not in gate["reason"]


def test_a_good_reply_is_clear_names_alpha_vantage_and_passes(tmp_path):
    gate = chain(tmp_path, opener_returning(good_text()))
    assert gate["status"] == "CLEAR", gate["reason"]
    assert "(Alpha Vantage)" in gate["reason"] and "(FMP)" not in gate["reason"]
    assert gate["earnings"]["calendar_source"] == "alphavantage"
    assert verdict_for(gate, NOW) == {"verdict": "PASS", "reasons": []}
    assert len(list(tmp_path.glob("av-earnings-2026-11-09.json"))) == 1


def with_tsla_on(day: str) -> str:
    return good_text().replace("TSLA,Tesla Inc,2026-11-13", f"TSLA,Tesla Inc,{day}")


def test_blocked_a_tsla_row_two_trading_days_ahead_is_an_estimated_event(tmp_path):
    gate = chain(tmp_path, opener_returning(with_tsla_on("2026-11-11")))
    assert gate["status"] == "EVENT"
    assert "EVENT (alphavantage): estimated earnings date 2026-11-11 within 3 trading days" \
        in gate["reason"]
    assert verdict_for(gate, NOW)["verdict"] == "BLOCK"


def test_blocked_a_tsla_row_exactly_three_trading_days_ahead_still_blocks(tmp_path):
    gate = chain(tmp_path, opener_returning(with_tsla_on("2026-11-12")))
    assert gate["status"] == "EVENT" and "estimated earnings date 2026-11-12" in gate["reason"]


def test_blocked_a_tsla_row_today_blocks(tmp_path):
    gate = chain(tmp_path, opener_returning(with_tsla_on("2026-11-09")))
    assert gate["status"] == "EVENT" and "earnings today 2026-11-09" in gate["reason"]


@pytest.mark.parametrize("day", ["2026-11-13", "2026-11-16"])
def test_a_tsla_row_four_or_five_trading_days_ahead_is_clear(tmp_path, day):
    gate = chain(tmp_path, opener_returning(with_tsla_on(day)))
    assert gate["status"] == "CLEAR", gate["reason"]
    assert verdict_for(gate, NOW)["verdict"] == "PASS"


def test_blocked_a_key_inside_an_error_never_reaches_the_gate_reason(tmp_path):
    cache = AvCalendarCache(tmp_path, lambda: (_ for _ in ()).throw(
        RuntimeError(f"boom {KEY}")), lambda: NOW)
    gate = events_from(make_data(cache))("TSLA", NOW, NOW.replace(hour=15, minute=55))
    assert gate["status"] == "UNKNOWN" and KEY not in gate["reason"] and "***" in gate["reason"]


# ======================= AvCalendarCache =======================

class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def counting(reply="good", exc=None):
    calls = []

    def fetch():
        calls.append(1)
        if exc is not None:
            raise exc
        return good_text() if reply == "good" else reply

    fetch.calls = calls
    return fetch


def test_the_retry_wait_is_fifteen_minutes():
    assert AV_RETRY_AFTER == timedelta(minutes=15)


def test_one_fetch_per_day_written_atomically_to_the_days_file(tmp_path):
    fetch = counting()
    cache = AvCalendarCache(tmp_path, fetch, Clock(NOW))
    first = cache.get(START, END)
    second = cache.get(START, END)
    assert len(fetch.calls) == 1 and first == second
    assert first["reply"] == good_text() and first["from"] == "2026-11-09"
    assert first["to"] == "2026-11-16" and first["fetched_at"] == NOW.isoformat()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["av-earnings-2026-11-09.json"]  # no .tmp left behind
    assert json.loads((tmp_path / files[0]).read_text(encoding="utf-8")) == first


def test_a_new_cache_object_reads_the_days_file_without_fetching(tmp_path):
    AvCalendarCache(tmp_path, counting(), Clock(NOW)).get(START, END)
    fetch = counting()
    again = AvCalendarCache(tmp_path, fetch, Clock(NOW)).get(START, END)
    assert fetch.calls == [] and again["reply"] == good_text()


def test_a_different_day_fetches_again_into_its_own_file(tmp_path):
    calls = []
    replies = [good_text(), av_csv(start=date(2026, 11, 10), end=date(2026, 11, 17))]

    def fetch():
        calls.append(1)
        return replies[len(calls) - 1]

    clock = Clock(NOW)
    cache = AvCalendarCache(tmp_path, fetch, clock)
    cache.get(START, END)
    clock.advance(days=1)
    cache.get(date(2026, 11, 10), date(2026, 11, 17))
    assert len(calls) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "av-earnings-2026-11-09.json", "av-earnings-2026-11-10.json"]


def test_blocked_a_failure_is_served_from_memory_until_the_retry_window_ends(tmp_path):
    fetch, clock = counting(exc=RuntimeError("HTTP 503 from /query")), Clock(NOW)
    cache = AvCalendarCache(tmp_path, fetch, clock)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        cache.get(START, END)
    assert len(fetch.calls) == 1
    clock.advance(minutes=14, seconds=59)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        cache.get(START, END)
    assert len(fetch.calls) == 1  # still no new fetch
    clock.advance(seconds=1)  # exactly 15:00 after the failure
    with pytest.raises(RuntimeError, match="HTTP 503"):
        cache.get(START, END)
    assert len(fetch.calls) == 2
    assert list(tmp_path.iterdir()) == []


def test_a_recovery_after_the_retry_window_is_cached(tmp_path):
    state, clock = {"fail": True}, Clock(NOW)

    def fetch():
        state.setdefault("n", 0)
        state["n"] += 1
        if state["fail"]:
            raise RuntimeError("HTTP 503 from /query")
        return good_text()

    cache = AvCalendarCache(tmp_path, fetch, clock)
    with pytest.raises(RuntimeError):
        cache.get(START, END)
    state["fail"] = False
    clock.advance(minutes=15)
    assert cache.get(START, END)["reply"] == good_text()
    cache.get(START, END)
    assert state["n"] == 2 and len(list(tmp_path.glob("av-earnings-*.json"))) == 1


def test_a_served_failure_carries_a_safe_message_for_any_error_type(tmp_path):
    clock = Clock(NOW)
    cache = AvCalendarCache(tmp_path, counting(exc=ValueError(f"secret {KEY}")), clock)
    with pytest.raises(RuntimeError) as first:  # the first failure is masked too
        cache.get(START, END)
    assert KEY not in str(first.value) and "ValueError" in str(first.value)
    clock.advance(minutes=1)
    with pytest.raises(RuntimeError) as ei:
        cache.get(START, END)
    assert KEY not in str(ei.value) and "ValueError" in str(ei.value)


def test_blocked_an_invalid_reply_is_not_written_and_is_served_for_the_retry_window(tmp_path):
    fetch, clock = counting(reply=rate_limit_text()), Clock(NOW)
    cache = AvCalendarCache(tmp_path, fetch, clock)
    first = cache.get(START, END)
    assert first["reply"] == rate_limit_text()
    assert list(tmp_path.iterdir()) == []
    clock.advance(minutes=59, seconds=59)  # AV_INVALID_RETRY_AFTER: 60 minutes
    assert cache.get(START, END) == first and len(fetch.calls) == 1
    clock.advance(seconds=1)
    cache.get(START, END)
    assert len(fetch.calls) == 2


@pytest.mark.parametrize("junk", ["not json at all", "[]", "{}", '{"from": "2026-11-09"}', ""])
def test_a_junk_cache_file_causes_a_refetch_and_is_replaced(tmp_path, junk):
    (tmp_path / "av-earnings-2026-11-09.json").write_text(junk, encoding="utf-8")
    fetch = counting()
    got = AvCalendarCache(tmp_path, fetch, Clock(NOW)).get(START, END)
    assert len(fetch.calls) == 1 and got["reply"] == good_text()
    stored = json.loads((tmp_path / "av-earnings-2026-11-09.json").read_text(encoding="utf-8"))
    assert stored["reply"] == good_text()


def test_two_threads_calling_get_at_once_trigger_one_fetch(tmp_path):
    calls, gate = [], threading.Barrier(2)

    def fetch():
        calls.append(1)
        time.sleep(0.15)
        return good_text()

    cache = AvCalendarCache(tmp_path, fetch, Clock(NOW))
    out = []

    def worker():
        gate.wait(timeout=5)
        out.append(cache.get(START, END))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(calls) == 1 and len(out) == 2 and out[0] == out[1]


# ======================= secrets =======================

def test_failure_masks_the_alpha_vantage_key(monkeypatch):
    assert "***" in _failure(RuntimeError(f"bad {KEY} here"))
    assert KEY not in _failure(RuntimeError(f"bad {KEY} here"))


def test_failure_masks_the_fmp_key_too(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "SENTINEL-FMP-KEY-1")
    text = _failure(RuntimeError(f"a {KEY} b SENTINEL-FMP-KEY-1"))
    assert KEY not in text and "SENTINEL-FMP-KEY-1" not in text


def test_failure_cuts_a_server_error_payload_to_the_path():
    text = _failure(RuntimeError(f"error payload from /query: your key {KEY} is bad"))
    assert text == "error payload from /query"


def test_failure_names_only_the_type_of_a_non_runtime_error():
    assert _failure(ValueError(f"secret {KEY}")) == "ValueError"
