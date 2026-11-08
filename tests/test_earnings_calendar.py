"""FMP earnings calendar fetch and parse, and how every transport failure ends (#13, #112).

A fake opener stands in for the network; nothing here reads a real key or makes a request. Each
failure goes all the way: fetch -> FmpCalendarCache -> events_from -> event_gate -> check_order,
and must end UNKNOWN (never "no earnings") and a BLOCK, with its own distinct reason.
"""
import io
import json
import ssl
import urllib.error
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.events import FmpCalendarCache, events_from
from tests.fakes import fresh_halt_feed, load_fixture
from tests.order_helpers import NY, fmp_row, submissions_202, verdict_for
from tools.earnings_calendar import fetch_fmp_earnings_calendar, parse_fmp_calendar

KEY = "SENTINEL-FMP-KEY-9f3a"
NOW = datetime(2026, 11, 9, 10, 30, tzinfo=NY)  # a Monday
START, END = date(2026, 11, 9), date(2026, 11, 23)
FMP = "https://financialmodelingprep.com/stable/earnings-calendar"


def fx(name: str):
    path = Path(__file__).parent / "fixtures" / "fmp" / name
    return json.loads(path.read_text(encoding="utf-8"))


class Resp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(body):
    seen = []

    def op(req, timeout=None):
        seen.append(req)
        return Resp(body)

    op.seen = seen
    return op


def opener_raising(exc):
    def op(req, timeout=None):
        raise exc
    return op


def http_error(code):
    return urllib.error.HTTPError(f"{FMP}?apikey={KEY}", code, "x", {}, io.BytesIO(b""))


@pytest.fixture(autouse=True)
def fmp_key(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", KEY)


# ======================= fetch: the request =======================

def test_the_request_has_the_range_the_report_times_flag_as_a_string_and_the_key():
    op = opener_returning(fx("earnings_calendar_others.json"))
    rows = fetch_fmp_earnings_calendar(START, END, opener=op)
    assert len(rows) == 4 and len(op.seen) == 1
    parts = urllib.parse.urlsplit(op.seen[0].full_url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == FMP
    q = urllib.parse.parse_qs(parts.query)
    assert q == {"from": ["2026-11-09"], "to": ["2026-11-23"], "includeReportTimes": ["true"],
                 "apikey": [KEY]}
    assert "includeReportTimes=true" in parts.query  # the string "true", never True or 1


def test_a_start_after_the_end_is_a_value_error():
    with pytest.raises(ValueError):
        fetch_fmp_earnings_calendar(END, START, opener=opener_returning([]))


def test_a_good_reply_passes_the_fetch_and_parses_to_rows():
    reply = fetch_fmp_earnings_calendar(START, END, opener=opener_returning(
        fx("earnings_calendar_tsla_today_amc.json")))
    cal = parse_fmp_calendar(reply, NOW, START, END)
    assert cal["ok"] is True and cal["from"] == "2026-11-09" and cal["to"] == "2026-11-23"
    assert cal["rows"][0] == {"symbol": "TSLA", "date": "2026-11-09", "time": "amc",
                              "confirmed": True}


# ======================= fetch: every failure, with a safe, distinct message =================

FAILURES = [
    ("timeout", lambda: opener_raising(TimeoutError("boom " + KEY)),
     "request to /stable/earnings-calendar timed out"),
    ("url-timeout", lambda: opener_raising(urllib.error.URLError(TimeoutError(KEY))),
     "request to /stable/earnings-calendar timed out"),
    ("http-429", lambda: opener_raising(http_error(429)),
     "HTTP 429 from /stable/earnings-calendar"),
    ("http-500", lambda: opener_raising(http_error(500)),
     "HTTP 500 from /stable/earnings-calendar"),
    ("http-503", lambda: opener_raising(http_error(503)),
     "HTTP 503 from /stable/earnings-calendar"),
    ("tls", lambda: opener_raising(urllib.error.URLError(ssl.SSLCertVerificationError(KEY))),
     "network error for /stable/earnings-calendar: SSLCertVerificationError"),
    ("refused", lambda: opener_raising(urllib.error.URLError(ConnectionRefusedError(KEY))),
     "network error for /stable/earnings-calendar: ConnectionRefusedError"),
    ("non-json", lambda: opener_returning(b"<html>Bad gateway</html>"),
     "non-JSON response from /stable/earnings-calendar"),
    ("error-message-object", lambda: opener_returning(fx("error_message.json")),
     "unexpected earnings calendar response shape"),
    ("error-key-object", lambda: opener_returning({"error": "limit reached"}),
     "error payload from /stable/earnings-calendar"),
    ("not-a-list", lambda: opener_returning(fx("not_a_list.json")),
     "unexpected earnings calendar response shape"),
    ("null-body", lambda: opener_returning(None),
     "unexpected earnings calendar response shape"),
]
FAIL_PARAMS = [pytest.param(f[1], f[2], id=f[0]) for f in FAILURES]


@pytest.mark.parametrize("make,message", FAIL_PARAMS)
def test_a_fetch_failure_raises_a_safe_runtime_error(make, message):
    with pytest.raises(RuntimeError) as exc:
        fetch_fmp_earnings_calendar(START, END, opener=make())
    assert message in str(exc.value)
    assert KEY not in str(exc.value) and "apikey" not in str(exc.value)


def test_a_missing_key_is_a_runtime_error_that_makes_no_request(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY")
    op = opener_returning([])
    with pytest.raises(RuntimeError, match="FMP_API_KEY must be set"):
        fetch_fmp_earnings_calendar(START, END, opener=op)
    assert op.seen == []


def test_an_empty_key_is_also_missing(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "")
    with pytest.raises(RuntimeError, match="FMP_API_KEY must be set"):
        fetch_fmp_earnings_calendar(START, END, opener=opener_returning([]))


# ======================= fetch failure -> gate UNKNOWN -> check_order BLOCK ==================

def chain(tmp_path, opener):
    """data -> FmpCalendarCache(fetch via the fake opener) -> events_from -> the gate result."""
    cache = FmpCalendarCache(
        tmp_path, lambda s, e: fetch_fmp_earnings_calendar(s, e, opener=opener), lambda: NOW)
    data = SimpleNamespace(
        earnings_calendar=cache.get,
        submissions=lambda sym: submissions_202(NOW, 30, symbol=sym),
        halt_feed=lambda: fresh_halt_feed(NOW),
        news=lambda syms, s, e: {"news": [], "next_page_token": None})
    return events_from(data)("TSLA", NOW, NOW.replace(hour=15, minute=55))


@pytest.mark.parametrize("make,message", FAIL_PARAMS)
def test_blocked_every_fmp_failure_is_unknown_and_blocks_the_order(tmp_path, make, message):
    gate = chain(tmp_path, make())
    assert gate["status"] == "UNKNOWN"
    assert f"UNKNOWN (fmp): earnings source unavailable: {message}" in gate["reason"]
    assert KEY not in gate["reason"]
    assert list(tmp_path.glob("fmp-earnings-*.json")) == []  # a failure is never cached
    v = verdict_for(gate, NOW)
    assert v["verdict"] == "BLOCK"
    assert any(r.startswith("event gate UNKNOWN: ") and message in r for r in v["reasons"])


def test_blocked_a_missing_fmp_key_is_unknown_and_blocks(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY")
    gate = chain(tmp_path, opener_returning([]))
    assert gate["status"] == "UNKNOWN"
    assert "earnings source unavailable: FMP_API_KEY must be set in the environment" \
        in gate["reason"]
    assert verdict_for(gate, NOW)["verdict"] == "BLOCK"


def test_a_good_reply_through_the_same_chain_is_clear_and_passes(tmp_path):
    gate = chain(tmp_path, opener_returning(fx("earnings_calendar_others.json")))
    assert gate["status"] == "CLEAR", gate["reason"]
    assert verdict_for(gate, NOW) == {"verdict": "PASS", "reasons": []}


# ======================= parse: malformed replies =======================

def good_row(**over):
    return {**fmp_row("TSLA", "2026-11-12", "amc", False), **over}


def without(row, key):
    return {k: v for k, v in row.items() if k != key}


MALFORMED = [
    ("not-a-list", {"Error Message": "x"}, "earnings calendar reply is not a list"),
    ("none", None, "earnings calendar reply is not a list"),
    ("row-not-a-dict", [good_row(), "TSLA"], "row is not an object"),
    ("row-none", [None], "row is not an object"),
    ("missing-symbol", [without(good_row(), "symbol")], "row has no symbol"),
    ("blank-symbol", [good_row(symbol="  ")], "row has no symbol"),
    ("symbol-not-str", [good_row(symbol=7)], "row has no symbol"),
    ("bad-date-text", [good_row(date="tomorrow")], "has no valid date"),
    ("bad-date-month", [good_row(date="2026-13-40")], "has no valid date"),
    ("bad-date-format", [good_row(date="11/12/2026")], "has no valid date"),
    ("missing-date", [without(good_row(), "date")], "has no valid date"),
    ("missing-time-key", [without(good_row(), "time")], "has no report time field"),
    ("time-tbd", [good_row(time="tbd")], "has an unknown report time"),
    ("time-empty", [good_row(time="")], "has an unknown report time"),
    ("confirmed-yes", [good_row(confirmed="yes")], "has a non-boolean confirmed flag"),
    ("confirmed-1", [good_row(confirmed=1)], "has a non-boolean confirmed flag"),
]
MAL_PARAMS = [pytest.param(m[1], m[2], id=m[0]) for m in MALFORMED]


@pytest.mark.parametrize("reply,why", MAL_PARAMS)
def test_a_malformed_reply_is_not_ok_and_names_why(reply, why):
    cal = parse_fmp_calendar(reply, NOW, START, END)
    assert cal["ok"] is False and why in cal["reason"]
    assert "rows" not in cal


@pytest.mark.parametrize("reply,why", MAL_PARAMS)
def test_blocked_a_malformed_reply_is_unknown_and_blocks(tmp_path, reply, why):
    gate = chain(tmp_path, opener_returning(reply))
    assert gate["status"] == "UNKNOWN"
    assert "UNKNOWN (fmp): earnings source unavailable:" in gate["reason"]
    v = verdict_for(gate, NOW)
    assert v["verdict"] == "BLOCK"
    assert any(r.startswith("event gate UNKNOWN: ") for r in v["reasons"])


def test_one_bad_row_among_good_ones_makes_the_whole_reply_malformed():
    reply = fx("earnings_calendar_others.json") + [good_row(time="later")]
    assert parse_fmp_calendar(reply, NOW, START, END)["ok"] is False


def test_a_naive_fetch_time_is_not_ok():
    cal = parse_fmp_calendar([], datetime(2026, 11, 9, 8, 30), START, END)
    assert cal["ok"] is False and "timezone-aware" in cal["reason"]


def test_confirmed_may_be_absent_and_then_means_estimated():
    cal = parse_fmp_calendar([without(good_row(), "confirmed")], NOW, START, END)
    assert cal["ok"] is True and cal["rows"][0]["confirmed"] is False


def test_symbols_are_normalised_and_extra_fields_dropped():
    cal = parse_fmp_calendar([good_row(symbol=" tsla ")], NOW, START, END)
    assert cal["rows"] == [{"symbol": "TSLA", "date": "2026-11-12", "time": "amc",
                            "confirmed": False}]


def test_fixture_files_are_in_the_shape_the_parser_accepts():
    for name in ("earnings_calendar_others.json", "earnings_calendar_tsla_today_amc.json",
                 "earnings_calendar_tsla_today_bmo.json",
                 "earnings_calendar_tsla_today_no_time.json",
                 "earnings_calendar_tsla_estimated_in_two_days.json",
                 "earnings_calendar_tsla_confirmed_in_two_days.json"):
        assert parse_fmp_calendar(fx(name), NOW, START, END)["ok"] is True, name
    assert load_fixture("fmp_earnings_calendar_tsla.json")  # the scenarios' shared reply
