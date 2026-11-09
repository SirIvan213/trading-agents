"""#172 live check: Alpha Vantage answers Accept: text/csv with HTTP 406. The fetch sends
Accept: */*, the body is still validated as CSV whatever it is labelled, and the reply's
Content-Type is carried to smoke_events. Offline: fake openers only, no live call."""
import io
import urllib.error
from datetime import date, datetime

import pytest

from pipeline.events import AvCalendarCache, calendar_input
from pipeline.scenarios import ScenarioData, load_scenario
from pipeline.smoke_events import run_live
from tests.fakes import av_csv
from tests.order_helpers import NY
from tools.earnings_calendar import (
    fetch_av_earnings_calendar,
    fetch_av_earnings_calendar_typed,
    parse_av_calendar,
)
from tools.http import get_bytes

KEY = "SENTINEL-AV-KEY-406"
NOW = datetime(2026, 11, 9, 8, 30, tzinfo=NY)
START, END = date(2026, 11, 9), date(2026, 11, 16)
CSV = av_csv(start=START, end=END)
URL = "https://www.alphavantage.co/query"


@pytest.fixture(autouse=True)
def av_key(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", KEY)


class Resp:
    def __init__(self, body: bytes, headers: dict):
        self._b, self.headers = body, headers

    def read(self, n=None):
        return self._b if n is None else self._b[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def picky_server(body: str = CSV, content_type: str | None = "application/x-download"):
    """Like the live server: HTTP 406 for Accept: text/csv, the body for anything else.
    Records the Accept header of every request."""
    seen: list = []

    def op(req, timeout=None):
        accept = req.get_header("Accept")
        seen.append(accept)
        if accept == "text/csv":
            raise urllib.error.HTTPError(f"{URL}?apikey={KEY}", 406, "Not Acceptable", {},
                                         io.BytesIO(b""))
        headers = {} if content_type is None else {"Content-Type": content_type}
        return Resp(body.encode("utf-8"), headers)

    op.seen = seen
    return op


def test_the_fake_server_really_refuses_text_csv():
    """The double behaves like the live check: so the next test would fail with text/csv."""
    with pytest.raises(RuntimeError, match=r"^HTTP 406 from /query$"):
        get_bytes(f"{URL}?apikey={KEY}", {"Accept": "text/csv"}, opener=picky_server())


def test_the_fetch_sends_accept_any_and_gets_the_csv():
    op = picky_server()
    assert fetch_av_earnings_calendar(opener=op) == CSV
    assert op.seen == ["*/*"]


def test_the_typed_fetch_returns_the_reply_content_type():
    text, ctype = fetch_av_earnings_calendar_typed(opener=picky_server())
    assert text == CSV and ctype == "application/x-download"


def test_no_content_type_header_is_none():
    assert fetch_av_earnings_calendar_typed(opener=picky_server(content_type=None))[1] is None


def test_a_long_content_type_is_cut_to_100_characters():
    _, ctype = fetch_av_earnings_calendar_typed(opener=picky_server(content_type="x" * 500))
    assert ctype == "x" * 100


@pytest.mark.parametrize("ctype", ["text/csv", "application/json", "application/x-download",
                                   None])
def test_a_csv_body_validates_whatever_it_is_labelled(ctype):
    text, _ = fetch_av_earnings_calendar_typed(opener=picky_server(content_type=ctype))
    assert parse_av_calendar(text, NOW, START, END)["ok"] is True


def test_blocked_a_json_message_labelled_csv_is_still_refused():
    body = '{"Information": "rate limit"}\n'
    text, _ = fetch_av_earnings_calendar_typed(opener=picky_server(body, "text/csv"))
    cal = parse_av_calendar(text, NOW, START, END)
    assert cal["ok"] is False and "message instead of the calendar" in cal["reason"]


def test_the_cache_records_the_content_type_and_keeps_it_on_disk(tmp_path):
    cache = AvCalendarCache(tmp_path, lambda: fetch_av_earnings_calendar_typed(
        opener=picky_server()), lambda: NOW)
    record = cache.get(START, END)
    assert record["reply"] == CSV and record["content_type"] == "application/x-download"
    again = AvCalendarCache(tmp_path, lambda: pytest.fail("refetched"), lambda: NOW)
    assert again.get(START, END)["content_type"] == "application/x-download"


def test_a_plain_text_fetch_records_no_content_type(tmp_path):
    record = AvCalendarCache(tmp_path, lambda: CSV, lambda: NOW).get(START, END)
    assert "content_type" not in record


def test_calendar_input_carries_the_content_type_on_success_and_failure(tmp_path):
    good = AvCalendarCache(tmp_path / "a", lambda: (CSV, "text/plain"), lambda: NOW)
    cal = calendar_input(type("D", (), {"av_earnings_calendar": staticmethod(good.get)}),
                         START)
    assert cal["ok"] is True and cal["content_type"] == "text/plain"
    bad = AvCalendarCache(tmp_path / "b", lambda: ('{"Note": "x"}\n', "application/json"),
                          lambda: NOW)
    cal = calendar_input(type("D", (), {"av_earnings_calendar": staticmethod(bad.get)}),
                         START)
    assert cal["ok"] is False and cal["content_type"] == "application/json"


def test_a_key_in_the_content_type_is_masked(tmp_path):
    cache = AvCalendarCache(tmp_path, lambda: (CSV, f"text/csv; x={KEY}"), lambda: NOW)
    cal = calendar_input(type("D", (), {"av_earnings_calendar": staticmethod(cache.get)}),
                         START)
    assert KEY not in cal["content_type"] and "***" in cal["content_type"]


def test_the_production_cache_uses_the_typed_fetch():
    """Without it the live smoke run would silently print no Content-Type line (#172)."""
    import pipeline.data
    assert AvCalendarCache().fetch is fetch_av_earnings_calendar_typed
    assert pipeline.data._SHARED_AV_CACHE.fetch is fetch_av_earnings_calendar_typed


def test_an_old_cache_file_without_a_content_type_is_still_served(tmp_path):
    import json
    (tmp_path / "av-earnings-2026-11-09.json").write_text(json.dumps(
        {"fetched_at": NOW.isoformat(), "from": START.isoformat(), "to": END.isoformat(),
         "reply": CSV}), encoding="utf-8")
    cache = AvCalendarCache(tmp_path, lambda: pytest.fail("refetched"), lambda: NOW)
    cal = calendar_input(type("D", (), {"av_earnings_calendar": staticmethod(cache.get)}),
                         START)
    assert cal["ok"] is True and "content_type" not in cal


def _scenario_with(ctype, reply=None):
    data = ScenarioData(load_scenario("clean-buy"))
    orig = data.av_earnings_calendar

    def av(s, e):
        raw = orig(s, e)
        if reply is not None:
            raw["reply"] = reply
        return {**raw, "content_type": ctype}

    data.av_earnings_calendar = av
    return data


def test_smoke_events_prints_the_content_type():
    data = _scenario_with("text/csv; charset=utf-8")
    out = "\n".join(run_live(["BRKO"], data, data.as_of()))
    assert "Alpha Vantage calendar: ok" in out
    assert "  Content-Type: 'text/csv; charset=utf-8'" in out


def test_smoke_events_prints_the_content_type_of_a_refused_reply():
    data = _scenario_with("application/json", reply='{"Information": "x"}\n')
    out = "\n".join(run_live(["BRKO"], data, data.as_of()))
    assert "Alpha Vantage calendar: NOT OK" in out
    assert "  Content-Type: 'application/json'" in out


def test_smoke_events_says_when_no_content_type_was_sent():
    data = _scenario_with(None)
    out = "\n".join(run_live(["BRKO"], data, data.as_of()))
    assert "  Content-Type: none sent" in out


def test_smoke_events_prints_no_content_type_line_when_the_source_records_none():
    data = ScenarioData(load_scenario("clean-buy"))
    out = "\n".join(run_live(["BRKO"], data, data.as_of()))
    assert "Content-Type" not in out
