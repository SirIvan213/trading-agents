"""Nasdaq trade-halt feed (#144): get_bytes, fetch_halt_feed, parse_halt_feed, HaltFeedCache,
LiveData.halt_feed. Synthetic XML only (the feed's terms restrict storing real copies); fake
openers and clocks; nothing touches the network or the disk."""
from __future__ import annotations

import io
import threading
import time
import urllib.error
from datetime import date, datetime, timedelta

import pytest

import pipeline.data as data_mod
from pipeline.data import LiveData
from tests.fakes import NY, halt_feed_xml, halt_item_xml
from tools.halt_feed import (
    HALT_CODES,
    HALT_FEED_URL,
    MAX_BODY_BYTES,
    MIN_FETCH_INTERVAL,
    HaltFeedCache,
    fetch_halt_feed,
    parse_halt_feed,
    resumed_before,
)
from tools.http import get_bytes, get_json

PUB = datetime(2026, 11, 3, 10, 59, tzinfo=NY)
URL = "https://feed.example.test/rss.aspx?feed=tradehalts&k=SECRET-QUERY"


class Resp:
    def __init__(self, body: bytes, final_url: str | None = None) -> None:
        self._b, self._u = body, final_url

    def read(self) -> bytes:
        return self._b

    def geturl(self) -> str:
        return self._u or URL

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_of(body: bytes, final_url: str | None = None):
    seen = []

    def op(req, timeout=None):
        seen.append((req, timeout))
        return Resp(body, final_url)

    op.seen = seen
    return op


def raising(exc: BaseException):
    def op(req, timeout=None):
        raise exc
    return op


# ============================ get_bytes ============================

def test_get_bytes_returns_the_raw_body_and_sends_headers_and_timeout():
    op = opener_of(b"<x/>\xff")
    assert get_bytes(URL, {"Accept": "text/xml"}, opener=op, timeout=4) == b"<x/>\xff"
    req, timeout = op.seen[0]
    assert timeout == 4 and req.full_url == URL
    assert dict(req.header_items())["Accept"] == "text/xml"


def test_get_json_still_decodes_through_get_bytes():
    assert get_json(URL, {}, opener=opener_of(b'{"a": 1}')) == {"a": 1}


def _err(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "x", {}, io.BytesIO(b"SECRET-QUERY"))


@pytest.mark.parametrize("exc,needle", [
    (_err(503), "HTTP 503"), (_err(500), "HTTP 500"), (_err(429), "HTTP 429"),
    (_err(302), "redirect refused .HTTP 302."),
    (TimeoutError(), "timed out"),
    (urllib.error.URLError(TimeoutError()), "timed out"),
    (urllib.error.URLError(ConnectionRefusedError()), "network error"),
    (ConnectionResetError(), "network error"),
], ids=["503", "500", "429", "redirect", "timeout", "urlerror-timeout", "urlerror", "oserror"])
def test_blocked_get_bytes_failures_are_runtime_errors_naming_the_path_only(exc, needle):
    with pytest.raises(RuntimeError, match=needle) as ei:
        get_bytes(URL, {}, opener=raising(exc))
    msg = str(ei.value)
    assert "/rss.aspx" in msg and "SECRET-QUERY" not in msg and "feed=" not in msg


def test_blocked_get_bytes_response_from_another_origin():
    with pytest.raises(RuntimeError, match="different origin") as ei:
        get_bytes(URL, {}, opener=opener_of(b"x", "https://evil.example.test/rss.aspx"))
    assert "evil" not in str(ei.value)


def test_get_bytes_same_origin_final_url_passes():
    assert get_bytes(URL, {}, opener=opener_of(b"ok", "https://feed.example.test/other")) == b"ok"


# ============================ fetch_halt_feed ============================

def test_fetch_halt_feed_uses_the_https_feed_url_accept_header_and_the_opener():
    op = opener_of(b"<rss/>", HALT_FEED_URL)
    assert fetch_halt_feed(op) == b"<rss/>"
    ((req, timeout),) = op.seen
    assert req.full_url == HALT_FEED_URL
    assert HALT_FEED_URL == "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
    assert "xml" in dict(req.header_items())["Accept"] and timeout == 10


def test_blocked_fetch_halt_feed_error_is_a_runtime_error_without_the_query():
    with pytest.raises(RuntimeError, match="HTTP 503") as ei:
        fetch_halt_feed(raising(_err(503)))
    assert "/rss.aspx" in str(ei.value) and "tradehalts" not in str(ei.value)


def test_blocked_fetch_halt_feed_refuses_a_redirect():
    with pytest.raises(RuntimeError, match="redirect refused"):
        fetch_halt_feed(raising(_err(301)))


# ============================ parse_halt_feed ============================

def item(sym="TSLA", code="LUDP", **kw):
    return halt_item_xml(sym, code, **kw)


def feed(items=(), **kw) -> bytes:
    return halt_feed_xml(PUB, items, **kw)


def test_a_valid_feed_parses_every_field():
    body = feed([
        item("TSLA", "LUDP", halt_time="10:05:00.214", resumed=("11/03/2026", "10:10:00")),
        item("qzxa", "h10", halt_date="08/14/2025", halt_time="09:30:00"),
    ])
    out = parse_halt_feed(body)
    assert out["ok"] is True
    assert out["pub_date"] == "2026-11-03T15:59:00+00:00"
    a, b = out["items"]
    assert a == {"symbol": "TSLA", "halt_date": "2026-11-03", "halt_time": "10:05:00",
                 "market": "NASDAQ", "reason": "LUDP", "resumption_date": "11/03/2026",
                 "resumption_trade_time": "10:10:00"}
    assert b["symbol"] == "QZXA" and b["reason"] == "H10" and b["halt_time"] == "09:30:00"
    assert b["resumption_date"] == "" and b["resumption_trade_time"] == ""
    assert b["halt_date"] == "2025-08-14"


def test_halt_time_with_and_without_milliseconds_both_parse():
    out = parse_halt_feed(feed([item("AAA", halt_time="10:05:00"),
                                item("BBB", halt_time="10:05:00.5"),
                                item("CCC", halt_time="10:05:00.123456")]))
    assert out["ok"] and [i["halt_time"] for i in out["items"]] == ["10:05:00"] * 3


def test_cdata_description_and_extra_elements_are_ignored():
    extra = "<description><![CDATA[a <b> note]]></description></item>"
    assert parse_halt_feed(feed([item("AAA").replace("</item>", extra)]))["ok"] is True


def test_an_empty_feed_with_zero_items_is_ok_with_no_items():
    assert parse_halt_feed(feed([])) == {"ok": True, "pub_date": "2026-11-03T15:59:00+00:00",
                                         "items": []}


def test_the_halt_code_table_has_the_volatility_pauses():
    for code in ("LUDP", "LUDS", "M", "T1", "H10"):
        assert code in HALT_CODES


BAD_ITEM_CASES = {
    "iso-date": (item("AAA", halt_date="2026-11-03"), "no valid halt time"),
    "short-time": (item("AAA", halt_time="10:05"), "no valid halt time"),
    "impossible-date": (item("AAA", halt_date="13/45/2026"), "no valid halt date"),
    "empty-date": (item("AAA", halt_date=""), "no valid halt time"),
    "empty-time": (item("AAA", halt_time=""), "no valid halt time"),
}


@pytest.mark.parametrize("name", sorted(BAD_ITEM_CASES))
def test_blocked_one_bad_item_makes_the_whole_feed_not_ok(name):
    bad, why = BAD_ITEM_CASES[name]
    out = parse_halt_feed(feed([item("GOOD"), bad]))
    assert out["ok"] is False and why in out["reason"]


def test_blocked_item_without_a_symbol_element():
    bad = item("AAA").replace("<ndaq:IssueSymbol>AAA</ndaq:IssueSymbol>", "")
    assert parse_halt_feed(feed([bad])) == {"ok": False, "reason": "halt feed item has no symbol"}


def test_blocked_item_with_a_blank_symbol():
    bad = item("AAA").replace("<ndaq:IssueSymbol>AAA</ndaq:IssueSymbol>",
                              "<ndaq:IssueSymbol>  </ndaq:IssueSymbol>")
    assert parse_halt_feed(feed([bad]))["reason"] == "halt feed item has no symbol"


@pytest.mark.parametrize("body", ["<rss/>", None, 5, ["x"]])
def test_blocked_not_bytes(body):
    assert parse_halt_feed(body) == {"ok": False, "reason": "halt feed body is not bytes"}


def test_blocked_too_large():
    big = feed([]) + b" " * MAX_BODY_BYTES
    assert parse_halt_feed(big)["reason"] == "halt feed body is too large"


def test_a_body_exactly_at_the_size_limit_is_not_refused_for_size():
    body = feed([])
    body = body + b" " * (MAX_BODY_BYTES - len(body))
    assert len(body) == MAX_BODY_BYTES and parse_halt_feed(body)["ok"] is True


def _after_decl(good: bytes) -> bytes:
    return good.split(b"?>", 1)[1]


def test_blocked_doctype_in_the_head():
    body = b'<?xml version="1.0"?><!DOCTYPE rss [<!ELEMENT rss ANY>]>' + _after_decl(feed([]))
    assert "DOCTYPE or entities" in parse_halt_feed(body)["reason"]


def test_blocked_lowercase_doctype_is_still_refused():
    body = b'<?xml version="1.0"?><!doctype rss>' + _after_decl(feed([]))
    assert "DOCTYPE or entities" in parse_halt_feed(body)["reason"]


def test_blocked_entity_declaration_anywhere():
    body = feed([item("AAA")]).replace(b"</channel>", b"<!ENTITY x 'y'></channel>")
    assert "DOCTYPE or entities" in parse_halt_feed(body)["reason"]


def test_blocked_a_billion_laughs_style_body_is_never_expanded():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            b"<rss><channel>&b;</channel></rss>")
    assert parse_halt_feed(bomb)["ok"] is False


@pytest.mark.parametrize("body", [b"", b"not xml at all", b"<rss><channel></rss>"],
                         ids=["empty", "text", "unclosed"])
def test_blocked_not_xml(body):
    assert parse_halt_feed(body) == {"ok": False, "reason": "halt feed is not valid XML"}


def test_blocked_wrong_root():
    body = feed([]).replace(b"<rss ", b"<feed ").replace(b"</rss>", b"</feed>")
    assert parse_halt_feed(body) == {"ok": False, "reason": "halt feed has no rss channel"}


def test_blocked_no_channel():
    assert parse_halt_feed(b"<rss version='2.0'></rss>")["reason"] == "halt feed has no rss channel"


def _with_pub(pub: str) -> bytes:
    good = feed([]).decode()
    start = good.index("<pubDate>") + len("<pubDate>")
    return (good[:start] + pub + good[good.index("</pubDate>"):]).encode()


@pytest.mark.parametrize("pub", ["", "yesterday", "2026-11-03", "Tue, 03 Nov 2026 99:99:99 GMT"],
                         ids=["empty", "word", "iso", "bad-clock"])
def test_blocked_bad_pubdate(pub):
    assert parse_halt_feed(_with_pub(pub)) == {"ok": False,
                                               "reason": "halt feed has no valid pubDate"}


def test_blocked_missing_pubdate():
    good = feed([]).decode()
    body = good.replace(good[good.index("<pubDate>"):good.index("</pubDate>") + 10], "").encode()
    assert parse_halt_feed(body)["reason"] == "halt feed has no valid pubDate"


def test_blocked_pubdate_without_a_zone():
    # RFC-822 date with "-0000" parses as naive in Python: a time we cannot place is refused
    out = parse_halt_feed(_with_pub("Tue, 03 Nov 2026 15:59:00 -0000"))
    assert out == {"ok": False, "reason": "halt feed has no valid pubDate"}


def test_blocked_numitems_missing():
    body = feed([item("AAA")]).replace(b"<ndaq:numItems>1</ndaq:numItems>", b"")
    out = parse_halt_feed(body)
    assert out["ok"] is False and "numItems is missing" in out["reason"]
    assert "cut short" in out["reason"]


def test_blocked_numitems_not_a_number():
    out = parse_halt_feed(feed([item("AAA")], num_items="many"))
    assert out["ok"] is False and "numItems is many" in out["reason"]


def test_blocked_numitems_larger_than_the_items_is_a_cut_short_feed():
    out = parse_halt_feed(feed([item("AAA")], num_items=5))
    assert out["ok"] is False and "lists 1 items but numItems is 5" in out["reason"]


def test_blocked_numitems_smaller_than_the_items_is_also_refused():
    out = parse_halt_feed(feed([item("AAA"), item("BBB")], num_items=1))
    assert out["ok"] is False and "lists 2 items but numItems is 1" in out["reason"]


def test_blocked_numitems_zero_with_items_present():
    assert parse_halt_feed(feed([item("AAA")], num_items=0))["ok"] is False


# ---- resumed_before ----

D = date(2026, 11, 3)


def rb(rd, rt):
    return resumed_before({"resumption_date": rd, "resumption_trade_time": rt}, D)


def test_resumed_before_true_only_with_a_trade_time_and_an_earlier_date():
    assert rb("11/02/2026", "09:45:00") is True


@pytest.mark.parametrize("rd,rt", [
    ("", ""), ("11/02/2026", ""), ("", "09:45:00"), ("11/03/2026", "09:45:00"),
    ("11/04/2026", "09:45:00"), ("2026-11-02", "09:45:00"), ("13/45/2026", "09:45:00"),
], ids=["none", "no-time", "no-date", "same-day", "future", "iso", "impossible"])
def test_not_resumed_before_today(rd, rt):
    assert rb(rd, rt) is False


def test_resumed_before_tolerates_missing_keys():
    assert resumed_before({}, D) is False


# ============================ HaltFeedCache ============================

class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class Fetch:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> bytes:
        self.calls += 1
        o = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(o, BaseException):
            raise o
        return o


T = datetime(2026, 11, 3, 11, 0, tzinfo=NY)


def test_two_gets_within_a_minute_fetch_once_and_return_the_first_fetch_time():
    clock, fetch = Clock(T), Fetch(b"one", b"two")
    cache = HaltFeedCache(fetch, clock)
    first = cache.get()
    clock.t = T + timedelta(seconds=59)
    second = cache.get()
    assert fetch.calls == 1 and first == second
    assert first == {"fetched_at": T.isoformat(), "body": b"one"}


def test_at_exactly_sixty_seconds_it_fetches_again():
    assert MIN_FETCH_INTERVAL == timedelta(seconds=60)
    clock, fetch = Clock(T), Fetch(b"one", b"two")
    cache = HaltFeedCache(fetch, clock)
    cache.get()
    clock.t = T + timedelta(seconds=60)
    out = cache.get()
    assert fetch.calls == 2 and out["body"] == b"two"
    assert out["fetched_at"] == (T + timedelta(seconds=60)).isoformat()


def test_blocked_a_failure_is_reraised_for_a_minute_without_refetching_then_retried():
    clock = Clock(T)
    fetch = Fetch(RuntimeError("HTTP 503 from /rss.aspx"), b"fine")
    cache = HaltFeedCache(fetch, clock)
    with pytest.raises(RuntimeError, match="HTTP 503 from /rss.aspx"):
        cache.get()
    clock.t = T + timedelta(seconds=30)
    with pytest.raises(RuntimeError, match="HTTP 503 from /rss.aspx"):
        cache.get()
    assert fetch.calls == 1  # the polling rule covers failures too
    clock.t = T + timedelta(seconds=60)
    assert cache.get()["body"] == b"fine" and fetch.calls == 2


def test_a_clock_going_backwards_refetches():
    clock, fetch = Clock(T), Fetch(b"one", b"two")
    cache = HaltFeedCache(fetch, clock)
    cache.get()
    clock.t = T - timedelta(seconds=1)
    assert cache.get()["body"] == b"two" and fetch.calls == 2


def test_blocked_runtime_error_text_is_kept_other_exceptions_become_the_type_name():
    clock = Clock(T)
    cache = HaltFeedCache(Fetch(KeyError("secret-token")), clock)
    with pytest.raises(RuntimeError) as ei:
        cache.get()
    assert str(ei.value) == "KeyError" and "secret-token" not in str(ei.value)
    cache2 = HaltFeedCache(Fetch(RuntimeError("request to /rss.aspx timed out")), clock)
    with pytest.raises(RuntimeError) as ei2:
        cache2.get()
    assert str(ei2.value) == "request to /rss.aspx timed out"


def test_a_long_runtime_error_text_is_cut():
    cache = HaltFeedCache(Fetch(RuntimeError("x" * 500)), Clock(T))
    with pytest.raises(RuntimeError) as ei:
        cache.get()
    assert len(str(ei.value)) == 200


def test_the_cache_is_in_memory_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    HaltFeedCache(Fetch(b"body"), Clock(T)).get()
    assert list(tmp_path.iterdir()) == []


def test_five_concurrent_gets_fetch_once():
    fetch = Fetch(b"body")

    def slow() -> bytes:
        time.sleep(0.05)
        return fetch()

    cache = HaltFeedCache(slow, Clock(T))
    results, errors = [], []

    def worker():
        try:
            results.append(cache.get())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert errors == [] and len(results) == 5 and fetch.calls == 1
    assert all(r == results[0] for r in results)


def test_default_cache_clock_is_new_york_time():
    cache = HaltFeedCache(Fetch(b"b"))
    assert cache.get()["fetched_at"].endswith(("-05:00", "-04:00"))


# ============================ LiveData.halt_feed ============================

class FakeCache:
    def __init__(self) -> None:
        self.calls = 0

    def get(self) -> dict:
        self.calls += 1
        return {"fetched_at": T.isoformat(), "body": b"x"}


def test_live_data_halt_feed_delegates_to_the_injected_cache():
    cache = FakeCache()
    out = LiveData(halt_cache=cache).halt_feed()
    assert out == {"fetched_at": T.isoformat(), "body": b"x"} and cache.calls == 1


def test_blocked_live_data_halt_feed_passes_the_cache_error_through():
    cache = HaltFeedCache(Fetch(RuntimeError("HTTP 429 from /rss.aspx")), Clock(T))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        LiveData(halt_cache=cache).halt_feed()


def test_two_live_data_objects_share_the_process_wide_cache(monkeypatch):
    fetch = Fetch(b"shared")
    shared = HaltFeedCache(fetch, Clock(T))
    monkeypatch.setattr(data_mod, "_SHARED_HALT_CACHE", shared)
    a, b = LiveData(), LiveData()
    assert a.halt_feed()["body"] == b"shared" and b.halt_feed()["body"] == b"shared"
    assert fetch.calls == 1
