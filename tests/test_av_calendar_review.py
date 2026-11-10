"""#167 review fixes: a cut-short Alpha Vantage reply, the cache's memory, key masking in
parse reasons, and the plausibility count's bounds. Offline: fake openers and fetches only."""
import http.client
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from pipeline.data import LiveData
from pipeline.events import AV_INVALID_RETRY_AFTER, AvCalendarCache, calendar_input, events_from
from tests.fakes import asset_reply, av_csv, fresh_halt_feed
from tests.order_helpers import NY, calendar_ok, fmp_row, gate_for, submissions_202, verdict_for
from tools.earnings_calendar import fetch_av_earnings_calendar, parse_av_calendar
from tools.http import get_bytes

KEY = "SENTINELAVKEY16X"  # 16 characters, like a free Alpha Vantage key
NOW = datetime(2026, 11, 9, 10, 30, tzinfo=NY)  # a Monday
START, END = date(2026, 11, 9), date(2026, 11, 16)
URL = "https://www.alphavantage.co/query?apikey=" + KEY


@pytest.fixture(autouse=True)
def av_key(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", KEY)


class Resp:
    def __init__(self, body: bytes, headers: dict | None = None, exc: Exception | None = None):
        self._b, self.headers, self._exc = body, headers or {}, exc

    def read(self, n=None):
        if self._exc is not None:
            raise self._exc
        return self._b if n is None else self._b[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(body: bytes, headers: dict | None = None, exc: Exception | None = None):
    return lambda req, timeout=None: Resp(body, headers, exc)


def symbol_sorted_csv(cut_before: str | None = None) -> str:
    """A whole-market CSV sorted by SYMBOL (as Alpha Vantage may serve it), with MSFT on
    2026-11-10. `cut_before` drops that row and everything after it, at a row boundary."""
    rows = [(f"A{n:04d}", (START + timedelta(days=n % 30)).isoformat()) for n in range(400)]
    rows.append(("MSFT", "2026-11-10"))
    rows += [(f"Z{n:04d}", (START + timedelta(days=n % 30)).isoformat()) for n in range(400)]
    text = av_csv(rows, background=False)
    if cut_before is not None:
        text = text[:text.index(f"\r\n{cut_before},") + 2]
    return text


def gate_of(reply: str):
    data = SimpleNamespace(
        av_earnings_calendar=lambda s, e: {"fetched_at": NOW.replace(hour=8).isoformat(),
                                           "from": s.isoformat(), "to": e.isoformat(),
                                           "reply": reply},
        submissions=lambda sym: submissions_202(NOW, 30, symbol=sym),
        halt_feed=lambda: fresh_halt_feed(NOW),
        listing=lambda sym: asset_reply(sym),
        news=lambda syms, s, e: {"news": [], "next_page_token": None})
    return events_from(data)("MSFT", NOW, NOW.replace(hour=15, minute=55))


# ======================= B1: a cut-short reply =======================

def test_blocked_the_whole_symbol_sorted_reply_lists_msft():
    gate = gate_of(symbol_sorted_csv())
    assert gate["status"] == "EVENT" and "EVENT (alphavantage)" in gate["reason"]


def test_a_cut_at_a_row_boundary_still_parses_so_the_transport_must_catch_it():
    """The limitation the transport check exists for: this reply parses and is CLEAR."""
    cut = symbol_sorted_csv(cut_before="MSFT")
    assert parse_av_calendar(cut, NOW, START, END)["ok"] is True
    assert gate_of(cut)["status"] == "CLEAR"


def test_blocked_a_body_shorter_than_its_content_length_is_refused():
    whole = symbol_sorted_csv().encode()
    cut = symbol_sorted_csv(cut_before="MSFT").encode()
    with pytest.raises(RuntimeError, match=r"incomplete response from /query \(\d+ of \d+ bytes\)"):
        fetch_av_earnings_calendar(opener=opener(cut, {"Content-Length": str(len(whole))}))


def test_blocked_a_short_body_ends_unknown_through_the_cache_and_the_gate(tmp_path):
    whole = symbol_sorted_csv().encode()
    cut = symbol_sorted_csv(cut_before="MSFT").encode()
    cache = AvCalendarCache(
        tmp_path, lambda: fetch_av_earnings_calendar(
            opener=opener(cut, {"Content-Length": str(len(whole))})), lambda: NOW)
    data = SimpleNamespace(av_earnings_calendar=cache.get,
                           submissions=lambda sym: submissions_202(NOW, 30, symbol=sym),
                           halt_feed=lambda: fresh_halt_feed(NOW),
                           listing=lambda sym: asset_reply(sym),
                           news=lambda syms, s, e: {"news": [], "next_page_token": None})
    gate = events_from(data)("MSFT", NOW, NOW.replace(hour=15, minute=55))
    assert gate["status"] == "UNKNOWN"
    assert "UNKNOWN (alphavantage): earnings source unavailable: incomplete response from " \
        "/query" in gate["reason"]
    assert KEY not in gate["reason"] and list(tmp_path.iterdir()) == []
    assert verdict_for(gate, NOW, "MSFT")["verdict"] == "BLOCK"


def test_a_body_matching_its_content_length_is_accepted():
    body = symbol_sorted_csv().encode()
    assert get_bytes(URL, {}, opener=opener(body, {"Content-Length": str(len(body))})) == body


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "abc"}, {"Content-Length": ""}],
                         ids=["absent", "not-a-number", "empty"])
def test_no_usable_content_length_leaves_the_body_alone(headers):
    assert get_bytes(URL, {}, opener=opener(b"abc", headers)) == b"abc"


def test_an_oversized_read_under_max_bytes_is_left_to_the_caller():
    body = b"x" * 50
    got = get_bytes(URL, {}, opener=opener(body, {"Content-Length": "50"}), max_bytes=10)
    assert got == b"x" * 11  # max_bytes + 1: the caller refuses the size itself


def test_blocked_an_incomplete_chunked_read_is_a_safe_runtime_error():
    exc = http.client.IncompleteRead(b"partial " + KEY.encode())
    with pytest.raises(RuntimeError) as err:
        get_bytes(URL, {}, opener=opener(b"", exc=exc))
    assert str(err.value) == "incomplete response from /query" and KEY not in str(err.value)


def test_blocked_a_cut_inside_the_last_field_is_refused_by_the_parser():
    text = symbol_sorted_csv()
    cut = text[:text.index("MSFT,") + 40]  # mid-row: no final line break
    cal = parse_av_calendar(cut, NOW, START, END)
    assert cal["ok"] is False and "does not end with a line break" in cal["reason"]


def test_blocked_a_reply_cut_mid_word_in_its_last_column_is_refused():
    text = symbol_sorted_csv()
    cal = parse_av_calendar(text[:-3], NOW, START, END)  # "USD\r\n" -> "US"
    assert cal["ok"] is False and "may be cut short" in cal["reason"]


@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_either_line_ending_counts_as_complete(ending):
    text = av_csv(start=START, end=END).replace("\r\n", ending)
    assert parse_av_calendar(text, NOW, START, END)["ok"] is True


def test_the_parse_reports_whether_rows_are_sorted_by_date():
    assert parse_av_calendar(av_csv(start=START, end=END), NOW, START, END)[
        "sorted_by_date"] is True
    assert parse_av_calendar(symbol_sorted_csv(), NOW, START, END)["sorted_by_date"] is False


# ======================= S1, S2: the cache's memory =======================

def test_an_unwritable_av_cache_dir_still_fetches_once_a_day(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    calls = []
    cache = AvCalendarCache(blocker / "sub", lambda: calls.append(1) or
                            av_csv(start=START, end=END), lambda: NOW)
    one, two = cache.get(START, END), cache.get(START, END)
    assert one == two and len(calls) == 1


def test_the_remembered_record_is_only_for_its_own_day(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    calls = []
    cache = AvCalendarCache(blocker / "sub", lambda: calls.append(1) or
                            av_csv(start=date(2026, 11, 10), end=date(2026, 11, 17)),
                            lambda: NOW)
    cache.get(START, END)
    cache.get(date(2026, 11, 10), date(2026, 11, 17))
    assert len(calls) == 2


def test_a_failed_fetch_waits_fifteen_minutes_and_an_invalid_reply_an_hour(tmp_path):
    assert AV_INVALID_RETRY_AFTER == timedelta(minutes=60)
    clock = SimpleNamespace(now=NOW)
    calls = []

    def fetch():
        calls.append(1)
        raise RuntimeError("HTTP 503 from /query")
    cache = AvCalendarCache(tmp_path, fetch, lambda: clock.now)
    with pytest.raises(RuntimeError):
        cache.get(START, END)
    clock.now += timedelta(minutes=15)
    with pytest.raises(RuntimeError):
        cache.get(START, END)
    assert len(calls) == 2  # a transport failure: 15 minutes


def test_a_failure_late_in_the_day_does_not_hold_the_next_day(tmp_path):
    clock = SimpleNamespace(now=datetime(2026, 11, 9, 23, 55, tzinfo=NY))
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("HTTP 503 from /query")
        return av_csv(start=date(2026, 11, 10), end=date(2026, 11, 17))
    cache = AvCalendarCache(tmp_path, fetch, lambda: clock.now)
    with pytest.raises(RuntimeError):
        cache.get(START, END)
    clock.now = datetime(2026, 11, 10, 0, 1, tzinfo=NY)
    assert cache.get(date(2026, 11, 10), date(2026, 11, 17))["from"] == "2026-11-10"
    assert len(calls) == 2


def test_a_clock_that_goes_backwards_after_a_failure_asks_again(tmp_path):
    clock = SimpleNamespace(now=NOW)
    calls = []

    def fetch():
        calls.append(1)
        raise RuntimeError("HTTP 503 from /query")
    cache = AvCalendarCache(tmp_path, fetch, lambda: clock.now)
    with pytest.raises(RuntimeError):
        cache.get(START, END)
    clock.now = NOW - timedelta(minutes=1)
    with pytest.raises(RuntimeError):
        cache.get(START, END)
    assert len(calls) == 2


def test_every_live_data_object_shares_one_av_cache():
    assert LiveData()._av_calendar is LiveData()._av_calendar


# ======================= S3: keys masked in parse reasons =======================

def test_a_key_echoed_into_a_symbol_is_masked_in_the_reason():
    reply = f"symbol,name,reportDate\n{KEY},x,not-a-date\n"
    data = SimpleNamespace(av_earnings_calendar=lambda s, e: {
        "fetched_at": NOW.isoformat(), "from": s.isoformat(), "to": e.isoformat(),
        "reply": reply})
    cal = calendar_input(data, START)
    assert cal["ok"] is False and cal["source"] == "alphavantage"
    assert KEY not in cal["reason"] and "***" in cal["reason"]


def test_an_fmp_key_echoed_into_a_reason_is_masked_too(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "SENTINEL-FMP-99")
    reply = "symbol,name,reportDate\nSENTINEL-FMP-99,x,bad\n"
    data = SimpleNamespace(av_earnings_calendar=lambda s, e: {
        "fetched_at": NOW.isoformat(), "from": s.isoformat(), "to": e.isoformat(),
        "reply": reply})
    assert "SENTINEL-FMP-99" not in calendar_input(data, START)["reason"]


# ======================= S4: the plausibility count's bounds =======================

def _spread(n: int, first: date, days: int, prefix: str = "P") -> list[dict]:
    return [fmp_row(f"{prefix}{i:03d}", (first + timedelta(days=i % days)).isoformat())
            for i in range(n)]


def test_blocked_a_wide_range_cannot_spread_the_floor_over_months():
    """49 reports this week plus hundreds later: still too sparse for the week we rely on."""
    week = _spread(49, START, 8)
    later = _spread(300, START + timedelta(days=8), 60, prefix="L")
    cal = calendar_ok(NOW, [*week, *later], background=False, end=START + timedelta(days=70))
    gate = gate_for("TSLA", NOW, calendar=cal)
    assert gate["status"] == "UNKNOWN" and "only 49 reports for 2026-11-09..2026-11-16" \
        in gate["reason"]


def test_fifty_reports_this_week_in_a_wide_range_are_enough():
    week = _spread(50, START, 8)
    later = _spread(10, START + timedelta(days=8), 30, prefix="L")
    cal = calendar_ok(NOW, [*week, *later], background=False, end=START + timedelta(days=40))
    assert gate_for("TSLA", NOW, calendar=cal)["status"] == "CLEAR"


def test_blocked_repeated_rows_do_not_pad_the_floor():
    rows = _spread(10, START, 8) * 6  # 60 rows, 10 distinct reports
    gate = gate_for("TSLA", NOW, calendar=calendar_ok(NOW, rows, background=False,
                                                      end=END))
    assert gate["status"] == "UNKNOWN" and "only 10 reports" in gate["reason"]
