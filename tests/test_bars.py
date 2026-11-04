"""Tests for tools.bars. Offline only: saved fixtures and a fake opener."""
import json
import urllib.parse
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tools import bars as B

FIX = Path(__file__).parent / "fixtures"
NY = ZoneInfo("America/New_York")


def load(name):
    return json.loads((FIX / name).read_text())


def load_dec(name):
    """Raw fixture with floats as Decimal, for first-principles expectations."""
    return json.loads((FIX / name).read_text(), parse_float=D)


def raw_bar(t="2026-11-03T15:00:00Z", o="100", h="101", l="99", c="100.5", v=1000):  # noqa: E741 - Alpaca field name
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def merged_fixture():
    return {"bars": load("alpaca_bars_page1.json")["bars"] + load("alpaca_bars_page2.json")["bars"]}


def merged_dec():
    return load_dec("alpaca_bars_page1.json")["bars"] + load_dec("alpaca_bars_page2.json")["bars"]


def regular_raw():
    """Raw bars with 14:30Z <= t < 21:00Z (09:30 <= ET < 16:00 on 2026-11-03, EST)."""
    return [b for b in merged_dec() if "2026-11-03T14:30:00Z" <= b["t"] < "2026-11-03T21:00:00Z"]


ALL = B.parse_bars(merged_fixture())
REG = B.regular_session(ALL)


# ---------- parse ----------

def test_parse_fixture_types_and_tz():
    assert len(ALL) == 29
    b = ALL[1]
    assert isinstance(b.open, D) and isinstance(b.high, D)
    assert b.ts.tzinfo is not None
    assert b.ts.utcoffset().total_seconds() == -5 * 3600  # EST
    assert (b.ts.hour, b.ts.minute) == (9, 30)
    assert b.open == D("100.0") and b.volume == 1000


def test_parse_dst_day_offset():
    bars = B.parse_bars(load("alpaca_bars_dst.json"))
    assert bars[1].ts.utcoffset().total_seconds() == -4 * 3600  # EDT
    assert (bars[1].ts.hour, bars[1].ts.minute) == (9, 30)  # 13:30Z
    assert (bars[0].ts.hour, bars[0].ts.minute) == (9, 29)


def test_dst_day_regular_session():
    bars = B.parse_bars(load("alpaca_bars_dst.json"))
    reg = B.regular_session(bars)
    # 20:00Z = 16:00 EDT is excluded
    assert [(b.ts.hour, b.ts.minute) for b in reg] == [(9, 30), (15, 59)]


def test_parse_sorts_and_dedupes():
    a = raw_bar("2026-11-03T15:01:00Z", c="100.5")
    b = raw_bar("2026-11-03T15:00:00Z", c="100.4")
    dup = raw_bar("2026-11-03T15:00:00Z", c="100.6")
    out = B.parse_bars({"bars": [a, b, dup]})
    assert [x.ts.minute for x in out] == [0, 1]
    assert len(out) == 2


def test_parse_empty_and_null():
    assert B.parse_bars({"bars": []}) == []
    assert B.parse_bars({"bars": None}) == []
    assert B.parse_bars({}) == []


def test_parse_single_symbol_dict_shape():
    out = B.parse_bars({"bars": {"AAPL": [raw_bar()]}})
    assert len(out) == 1


def test_parse_multi_symbol_rejected():
    with pytest.raises(ValueError):
        B.parse_bars({"bars": {"AAPL": [raw_bar()], "MSFT": [raw_bar()]}})


@pytest.mark.parametrize("bad", [None, [], "x"])
def test_parse_non_object_rejected(bad):
    with pytest.raises(ValueError):
        B.parse_bars(bad)


@pytest.mark.parametrize("key", ["t", "o", "h", "l", "c", "v"])
def test_parse_missing_key(key):
    item = raw_bar()
    del item[key]
    with pytest.raises(ValueError, match="missing"):
        B.parse_bars({"bars": [item]})


def test_parse_high_below_low():
    with pytest.raises(ValueError):
        B.parse_bars({"bars": [raw_bar(h="98", l="99", o="98.5", c="98.5")]})


def test_parse_open_close_outside_range():
    with pytest.raises(ValueError):
        B.parse_bars({"bars": [raw_bar(c="102")]})  # close above high
    with pytest.raises(ValueError):
        B.parse_bars({"bars": [raw_bar(o="98")]})  # open below low


@pytest.mark.parametrize("field,val", [("o", "0"), ("h", "-1"), ("l", "0"), ("c", "-5"), ("o", 0)])
def test_parse_non_positive_price(field, val):
    item = raw_bar()
    item[field] = val
    with pytest.raises(ValueError):
        B.parse_bars({"bars": [item]})


@pytest.mark.parametrize("val", ["NaN", "Infinity", "abc"])
def test_parse_non_finite_or_non_numeric_price(val):
    with pytest.raises(ValueError):
        B.parse_bars({"bars": [raw_bar(c=val)]})


def test_parse_negative_volume():
    with pytest.raises(ValueError):
        B.parse_bars({"bars": [raw_bar(v=-1)]})


@pytest.mark.parametrize("v", [1.5, True, None, [1]])
def test_parse_bad_volume(v):
    with pytest.raises(ValueError):
        B.parse_bars({"bars": [raw_bar(v=v)]})


def test_parse_zero_volume_allowed():
    assert B.parse_bars({"bars": [raw_bar(v=0)]})[0].volume == 0


@pytest.mark.parametrize("t", ["2026-11-03T15:00:00", "garbage", 12345, None])
def test_parse_bad_timestamp(t):
    with pytest.raises(ValueError):
        B.parse_bars({"bars": [raw_bar(t=t)]})


def test_parse_non_dict_bar():
    with pytest.raises(ValueError):
        B.parse_bars({"bars": ["nope"]})


# ---------- regular_session ----------

def test_regular_session_filters_fixture():
    # premarket, 21:00Z (16:00) and 21:30Z dropped
    assert len(REG) == 26
    assert REG[0].ts.astimezone(NY).strftime("%H:%M") == "09:30"  # 09:30 inclusive
    assert REG[-1].ts.astimezone(NY).strftime("%H:%M") == "15:59"
    assert all(b.ts.astimezone(NY).strftime("%H:%M") != "16:00" for b in REG)


def test_regular_session_boundaries_explicit():
    items = [
        raw_bar("2026-11-03T14:29:00Z"),  # 09:29 out
        raw_bar("2026-11-03T14:30:00Z"),  # 09:30 in
        raw_bar("2026-11-03T20:59:00Z"),  # 15:59 in
        raw_bar("2026-11-03T21:00:00Z"),  # 16:00 out
    ]
    out = B.regular_session(B.parse_bars({"bars": items}))
    assert [b.ts.strftime("%H:%M") for b in out] == ["09:30", "15:59"]


def test_regular_session_day_filter():
    items = [raw_bar("2026-11-03T15:00:00Z"), raw_bar("2026-11-04T15:00:00Z")]
    bars = B.parse_bars({"bars": items})
    assert len(B.regular_session(bars)) == 2
    only = B.regular_session(bars, date(2026, 11, 4))
    assert len(only) == 1 and only[0].ts.day == 4


def test_regular_session_empty():
    assert B.regular_session([]) == []


# ---------- opening_range ----------

def test_opening_range_values_from_first_principles():
    raw = regular_raw()
    window = [b for b in raw if b["t"] < "2026-11-03T14:45:00Z"]
    assert len(window) == 15
    exp_high = max(b["h"] for b in window)
    exp_low = min(b["l"] for b in window)
    orr = B.opening_range(ALL)
    assert D(orr["high"]) == exp_high == D("102.0")  # bar 5 spike
    assert D(orr["low"]) == exp_low == D("99.7")
    assert orr["bars"] == 15
    assert orr["start"] == "2026-11-03T09:30:00-05:00"
    assert orr["end"] == "2026-11-03T09:45:00-05:00"


def test_opening_range_excludes_premarket():
    orr = B.opening_range(ALL)
    assert D(orr["low"]) > D("98.5")  # premarket low must not leak in


def test_opening_range_custom_minutes():
    orr = B.opening_range(ALL, minutes=5)
    assert orr["bars"] == 5
    assert D(orr["high"]) == max(b["h"] for b in regular_raw()[:5])


def test_opening_range_dst_day():
    orr = B.opening_range(B.parse_bars(load("alpaca_bars_dst.json")), minutes=15)
    assert orr["start"] == "2026-07-06T09:30:00-04:00"
    assert orr["bars"] == 1 and D(orr["high"]) == D("50.4") and D(orr["low"]) == D("50.0")


def test_opening_range_none_when_empty():
    assert B.opening_range([]) is None


def test_opening_range_none_when_only_premarket():
    pre = B.parse_bars({"bars": [raw_bar("2026-11-03T14:25:00Z")]})
    assert B.opening_range(pre) is None


def test_opening_range_none_when_window_has_no_bars():
    late = B.parse_bars({"bars": [raw_bar("2026-11-03T15:00:00Z")]})  # 10:00, after 09:45
    assert B.opening_range(late) is None


# ---------- vwap ----------

def test_vwap_from_first_principles():
    raw = regular_raw()
    num = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in raw)
    den = sum(b["v"] for b in raw)
    assert B.vwap(REG) == (num / den).quantize(D("0.0001"))


def test_vwap_simple_hand_value():
    bars = B.parse_bars({"bars": [
        raw_bar("2026-11-03T15:00:00Z", o="10", h="12", l="9", c="9", v=100),    # typical 10
        raw_bar("2026-11-03T15:01:00Z", o="10", h="14", l="10", c="10", v=300),  # typical 34/3
    ]})
    # (10*100 + (34/3)*300) / 400 = (1000 + 3400) / 400 = 11
    assert B.vwap(bars) == D("11.0000")


def test_vwap_none_on_zero_volume():
    bars = B.parse_bars({"bars": [raw_bar(v=0)]})
    assert B.vwap(bars) is None
    assert B.vwap([]) is None


# ---------- atr ----------

def wilder_atr(raw, n=14):
    trs = []
    for i, b in enumerate(raw):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = raw[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    val = sum(trs[:n]) / n
    for tr in trs[n:]:
        val = (val * (n - 1) + tr) / n
    return val.quantize(D("0.0001"))


def test_atr_from_first_principles():
    assert B.atr(REG) == wilder_atr(regular_raw())


def test_atr_constant_range_bars():
    # 14 bars, each h-l = 0.80 and close-to-next-extreme gaps 0.4: ATR = 0.8
    items = []
    for i in range(14):
        o = D("100") + D("0.1") * i
        items.append(raw_bar(f"2026-11-03T15:{i:02d}:00Z", o=str(o), h=str(o + D("0.5")),
                             l=str(o - D("0.3")), c=str(o + D("0.2")), v=100))
    assert B.atr(B.parse_bars({"bars": items})) == D("0.8000")


def test_atr_none_with_too_few_bars():
    assert B.atr(REG[:13]) is None
    assert B.atr(REG[:14]) is not None  # exactly n bars is enough
    assert B.atr([]) is None


def test_atr_n_must_be_positive():
    with pytest.raises(ValueError):
        B.atr(REG, 0)


# ---------- summarize ----------

def test_summarize_fixture():
    s = B.summarize(REG)
    raw = regular_raw()
    assert s["bars"] == 26
    assert s["first"] == "2026-11-03T09:30:00-05:00"
    assert s["last"] == "2026-11-03T15:59:00-05:00"
    assert D(s["last_close"]) == D("103.1")
    assert D(s["session_high"]) == max(b["h"] for b in raw)
    assert D(s["session_low"]) == min(b["l"] for b in raw) == D("99.7")
    assert s["volume"] == sum(b["v"] for b in raw)
    assert D(s["vwap"]) == B.vwap(REG)
    assert D(s["atr"]) == wilder_atr(raw)
    assert D(s["opening_range"]["low"]) == D("99.7")
    json.dumps(s)  # JSON-serializable


def test_summarize_empty():
    s = B.summarize([])
    assert s["bars"] == 0 and s["volume"] == 0
    assert s["vwap"] is None and s["atr"] is None and s["opening_range"] is None
    assert s["first"] is None and s["last_close"] is None
    json.dumps(s)


def test_summarize_few_bars_has_no_atr():
    s = B.summarize(REG[:5])
    assert s["atr"] is None and s["vwap"] is not None


# ---------- fetch_bars ----------

class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return FakeResp(self.pages.pop(0))


START = datetime(2026, 11, 3, 9, 30, tzinfo=NY)
END = datetime(2026, 11, 3, 16, 0, tzinfo=NY)


def query(req):
    return urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "dummy-key-id")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "dummy-secret")


def test_fetch_merges_pages_and_paginates(keys):
    p1, p2 = load("alpaca_bars_page1.json"), load("alpaca_bars_page2.json")
    op = FakeOpener([p1, p2])
    out = B.fetch_bars("aapl", START, END, opener=op)
    assert len(op.requests) == 2
    assert len(out["bars"]) == len(p1["bars"]) + len(p2["bars"]) == 29
    assert out["symbol"] == "AAPL" and out["next_page_token"] is None
    q1, q2 = query(op.requests[0]), query(op.requests[1])
    assert "page_token" not in q1
    assert q2["page_token"] == [p1["next_page_token"]]
    assert "/AAPL/bars" in op.requests[0].full_url
    assert q1["start"] == ["2026-11-03T14:30:00Z"] and q1["end"] == ["2026-11-03T21:00:00Z"]
    assert q1["timeframe"] == ["1Min"]
    assert len(B.parse_bars(out)) == 29  # merged output parses cleanly


def test_fetch_sends_auth_headers(keys):
    op = FakeOpener([load("alpaca_bars_page2.json")])
    B.fetch_bars("AAPL", START, END, opener=op)
    h = {k.lower(): v for k, v in op.requests[0].header_items()}
    assert h["apca-api-key-id"] == "dummy-key-id"
    assert h["apca-api-secret-key"] == "dummy-secret"


def test_fetch_missing_env_raises(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    op = FakeOpener([])
    with pytest.raises(RuntimeError):
        B.fetch_bars("AAPL", START, END, opener=op)
    assert op.requests == []  # nothing was sent


def test_fetch_one_key_missing_raises(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "dummy-key-id")
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        B.fetch_bars("AAPL", START, END, opener=FakeOpener([]))


def test_fetch_naive_datetimes_rejected(keys):
    op = FakeOpener([])
    with pytest.raises(ValueError):
        B.fetch_bars("AAPL", datetime(2026, 11, 3, 9, 30), END, opener=op)
    with pytest.raises(ValueError):
        B.fetch_bars("AAPL", START, datetime(2026, 11, 3, 16, 0), opener=op)
    assert op.requests == []


def test_fetch_utc_conversion_in_summer(keys):
    op = FakeOpener([{"bars": [], "next_page_token": None}])
    s = datetime(2026, 7, 6, 9, 30, tzinfo=NY)
    B.fetch_bars("SPY", s, s.replace(hour=16), opener=op)
    assert query(op.requests[0])["start"] == ["2026-07-06T13:30:00Z"]


def test_fetch_pagination_loop_guard(keys):
    page = {"bars": [], "next_page_token": "again"}
    op = FakeOpener([page] * (B.MAX_PAGES + 1))
    with pytest.raises(RuntimeError):
        B.fetch_bars("AAPL", START, END, opener=op)


def test_fetch_null_bars_page(keys):
    op = FakeOpener([{"bars": None, "next_page_token": None}])
    assert B.fetch_bars("AAPL", START, END, opener=op)["bars"] == []


# ---------- opening_range completeness (regression for item 1) ----------

def test_opening_range_complete_on_full_fixture():
    orr = B.opening_range(ALL)
    assert orr["complete"] is True
    assert orr["missing_minutes"] == 0


def test_opening_range_incomplete_when_truncated_before_0945():
    cut = [b for b in ALL if b.ts.astimezone(NY).strftime("%H:%M") < "09:45"]
    orr = B.opening_range(cut)
    assert orr["complete"] is False
    assert orr["bars"] == 15 and orr["missing_minutes"] == 0


def test_opening_range_incomplete_when_cut_mid_window():
    cut = [b for b in ALL if b.ts.astimezone(NY).strftime("%H:%M") < "09:40"]
    orr = B.opening_range(cut)
    assert orr["complete"] is False
    assert orr["bars"] == 10 and orr["missing_minutes"] == 5


def test_opening_range_missing_minutes_one_removed():
    sliced = [b for b in ALL if b.ts.astimezone(NY).strftime("%H:%M") != "09:35"]
    orr = B.opening_range(sliced)
    assert orr["missing_minutes"] == 1
    assert orr["bars"] == 14
    assert orr["complete"] is True  # later bars exist, window elapsed


def test_opening_range_complete_needs_same_day_bar():
    # first day has only 09:30; a bar on the next day does not complete day one's window
    items = [raw_bar("2026-11-03T14:30:00Z"), raw_bar("2026-11-04T15:00:00Z")]
    orr = B.opening_range(B.parse_bars({"bars": items}))
    assert orr["complete"] is False


def test_opening_range_bar_exactly_at_end_completes():
    items = [raw_bar("2026-11-03T14:30:00Z"), raw_bar("2026-11-03T14:45:00Z")]
    assert B.opening_range(B.parse_bars({"bars": items}))["complete"] is True


def test_summarize_exposes_completeness():
    assert B.summarize(REG)["opening_range"]["complete"] is True
    short = B.summarize(REG[:5])["opening_range"]
    assert short["complete"] is False and short["missing_minutes"] == 10


# ---------- network guard (item 5) ----------

def test_fetch_without_opener_hits_guard_not_network(keys):
    with pytest.raises(RuntimeError, match="network access in tests"):
        B.fetch_bars("AAPL", START, END)


def test_socket_connect_is_blocked():
    import socket

    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="network access in tests"):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()


# ---------- fetch_bars through the shared safe-error helper (issue #8) ----------

SENTINEL_KEY = "SENTINEL-KEY-123"
SENTINEL_SECRET = "SENTINEL-SECRET-456"


@pytest.fixture
def sentinel_keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", SENTINEL_KEY)
    monkeypatch.setenv("ALPACA_SECRET_KEY", SENTINEL_SECRET)


class RawOpener:
    """Returns raw bytes for each request."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        body = self.bodies.pop(0)
        return FakeResp(body) if not isinstance(body, bytes) else RawResp(body)


class RawResp:
    def __init__(self, b):
        self._b = b

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def assert_path_only(exc_info):
    msg = str(exc_info.value)
    for secret in (SENTINEL_KEY, SENTINEL_SECRET, "timeframe", "adjustment", "limit=", "start="):
        assert secret not in msg, secret
    assert "/AAPL/bars" in msg


def test_fetch_error_payload_raises_runtime_error_with_path_only(sentinel_keys):
    op = FakeOpener([{"message": "forbidden"}])
    with pytest.raises(RuntimeError, match="error payload") as ei:
        B.fetch_bars("AAPL", START, END, opener=op)
    assert "forbidden" in str(ei.value)
    assert_path_only(ei)


def test_fetch_non_json_body_raises_runtime_error(sentinel_keys):
    with pytest.raises(RuntimeError, match="non-JSON") as ei:
        B.fetch_bars("AAPL", START, END, opener=RawOpener([b"<html>Bad gateway</html>"]))
    assert_path_only(ei)


def test_fetch_http_403_raises_runtime_error(sentinel_keys):
    import urllib.error

    def op(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    with pytest.raises(RuntimeError, match="HTTP 403") as ei:
        B.fetch_bars("AAPL", START, END, opener=op)
    assert_path_only(ei)


def test_fetch_error_on_second_page_raises(sentinel_keys):
    p1 = load("alpaca_bars_page1.json")
    op = FakeOpener([p1, {"message": "rate limit exceeded"}])
    with pytest.raises(RuntimeError, match="error payload"):
        B.fetch_bars("AAPL", START, END, opener=op)
    assert len(op.requests) == 2


def test_fetch_page_with_bars_key_and_message_is_not_an_error(sentinel_keys):
    op = FakeOpener([{"message": "note", "bars": [], "next_page_token": None}])
    assert B.fetch_bars("AAPL", START, END, opener=op)["bars"] == []


def test_fetch_pagination_still_merges_29_bars_through_get_json(sentinel_keys):
    op = FakeOpener([load("alpaca_bars_page1.json"), load("alpaca_bars_page2.json")])
    out = B.fetch_bars("AAPL", START, END, opener=op)
    assert len(op.requests) == 2 and len(out["bars"]) == 29
    token = load("alpaca_bars_page1.json")["next_page_token"]
    assert query(op.requests[1])["page_token"] == [token]
    h = {k.lower(): v for k, v in op.requests[1].header_items()}
    assert h["apca-api-key-id"] == SENTINEL_KEY  # still sent on every page


def test_fetch_multi_symbol_dict_shape_is_unwrapped(sentinel_keys):
    page = {"bars": {"AAPL": [raw_bar()]}, "next_page_token": None}
    out = B.fetch_bars("aapl", START, END, opener=FakeOpener([page]))
    assert out["bars"] == [raw_bar()]


def test_fetch_list_body_rejected(sentinel_keys):
    with pytest.raises(RuntimeError, match="unexpected"):
        B.fetch_bars("AAPL", START, END, opener=FakeOpener([[1, 2]]))


def test_fetch_bars_string_bars_value_rejected(sentinel_keys):
    with pytest.raises(RuntimeError, match="unexpected bars response shape"):
        B.fetch_bars("AAPL", START, END, opener=FakeOpener([{"bars": "abc"}]))


@pytest.mark.parametrize("bad", [5, True, 1.5])
def test_fetch_bars_other_non_container_bars_values_rejected(sentinel_keys, bad):
    with pytest.raises(RuntimeError, match="unexpected bars response shape"):
        B.fetch_bars("AAPL", START, END, opener=FakeOpener([{"bars": bad}]))


def test_fetch_bars_list_bars_value_accepted(sentinel_keys):
    out = B.fetch_bars("AAPL", START, END, opener=FakeOpener([{"bars": [raw_bar()]}]))
    assert out["bars"] == [raw_bar()]
