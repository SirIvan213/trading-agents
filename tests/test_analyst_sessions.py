"""SentimentSession and FundamentalsSession against saved fixtures. No network.

Sentiment clock NOON = 2026-11-03 12:00 NY = 17:00Z, so the 24h window is
[2026-11-02 17:00Z, 2026-11-03 17:00Z]. ABCD items in the news fixtures (UTC):
  inside: 1001 16:30 (dup of 1008 at 16:45, earliest kept), 1002 15:10, 1003 14:05,
          1006 13:00, 1007 12:00, 1012 11:00, 1010 Nov-2 17:00 (exactly the window start)
  outside: 1005 17:30 (future), 1009 Nov-2 16:59 (one minute too old)
  skipped: 1011 (bad timestamp)   not ABCD: 1004 (OTHR)
  => news_for_symbol 11, headlines 7, duplicates 1, outside 2, malformed 1.
8-K filings accepted since 2026-11-02 17:00Z: -000005 (no acceptance time, estimated
end of 11-03 NY), -000010, -000009 => 3.  (-000008/-000003 are older; -000004 is 10-15.)
"""
from datetime import UTC, datetime, timedelta

import pytest

from agents.fundamentals_tools import TOOLS as F_TOOLS
from agents.fundamentals_tools import FundamentalsSession
from agents.sentiment_tools import TOOLS as S_TOOLS
from agents.sentiment_tools import SentimentSession
from tests.fakes import NOON, NY, FakeFilingsData

SENTINEL = "sk-ant-SENTINEL-98765"


def sent(data=None, now=NOON, symbol="ABCD"):
    d = data or FakeFilingsData()
    return SentimentSession(symbol, now, d), d


def fund(data=None, now=NOON, symbol="ABCD"):
    d = data or FakeFilingsData()
    return FundamentalsSession(symbol, now, d), d


# ---------------- sentiment ----------------

def test_get_news_digest_counts_and_window():
    s, d = sent()
    out, err = s.execute("get_news", {"symbol": "ABCD"})
    assert err is False
    assert out["symbol"] == "ABCD"
    assert out["counts"] == {
        "news_for_symbol": 11, "headlines": 7, "duplicates_removed": 1,
        "outside_window": 2, "malformed_skipped": 1, "filings": 3,
    }
    assert out["window_start"] == "2026-11-02T12:00:00-05:00"
    assert out["now"] == "2026-11-03T12:00:00-05:00"
    assert out["headlines"][0]["time"] == "2026-11-03T11:30:00-05:00"  # 16:30Z, id 1001
    heads = [h["headline"] for h in out["headlines"]]
    assert len(heads) == 7
    assert "ABCD announces $50 million public offering of common stock" in heads
    assert not any("Public Offering of Common Stock!" in h for h in heads)  # later dup dropped
    assert "ABCD exactly at window start update" in heads        # boundary is inclusive
    assert "ABCD guidance reiterated" not in heads               # 1 minute outside
    assert "ABCD to be acquired in $2B merger" not in heads      # in the future
    assert "Other Corp posts earnings beat" not in heads         # other symbol


def test_get_news_requests_24h_window_ending_now():
    s, d = sent()
    s.execute("get_news", {"symbol": "abcd"})
    kind, symbols, start, end = d.calls[0]
    assert (kind, symbols) == ("news", ["ABCD"])
    assert end == NOON
    assert start == NOON.astimezone(UTC) - timedelta(hours=24)
    assert ("submissions", "ABCD") in d.calls


def test_get_news_filings_parsed_since_24h_before_now():
    s, _ = sent()
    out, _ = s.execute("get_news", {"symbol": "ABCD"})
    assert [f["accession"] for f in out["filings"]] == [
        "0000012345-26-000005", "0000012345-26-000010", "0000012345-26-000009"]
    first = out["filings"][0]
    assert first["time_estimated"] is True and first["red_flags"] == ["bankruptcy"]
    # -000008 (8-K/A, 10-30) and -000003 (8-K/A 11-02 15:00Z) are before the window start
    assert all(f["accession"] not in ("0000012345-26-000008", "0000012345-26-000003")
               for f in out["filings"])


def test_get_news_red_flags_and_flagged_headline():
    s, _ = sent()
    out, _ = s.execute("get_news", {"symbol": "ABCD"})
    assert out["red_flags"] == ["bankruptcy", "halt"]
    halted = next(h for h in out["headlines"] if "halted" in h["headline"])
    assert halted["flags"] == ["halt"]
    near_miss = next(h for h in out["headlines"] if "halter" in h["headline"])
    assert near_miss["flags"] == []
    assert "earnings" in out["catalysts"] and "offering" in out["catalysts"]


def test_window_moves_with_now():
    # now 10:30 NY = 15:30Z: window starts Nov-2 15:30Z, so 1009 (16:59Z) is inside and
    # 1001/1008/1005 (after 15:30Z) are in the future.
    now = datetime(2026, 11, 3, 10, 30, tzinfo=NY)
    out, _ = sent(now=now)[0].execute("get_news", {"symbol": "ABCD"})
    heads = [h["headline"] for h in out["headlines"]]
    assert "ABCD guidance reiterated" in heads
    assert not any("public offering" in h.lower() for h in heads)
    # inside: 1002, 1003, 1006, 1007, 1012, 1010, 1009 = 7 ; future: 1008, 1005, 1001 = 3
    assert out["counts"]["headlines"] == 7
    assert out["counts"]["outside_window"] == 3
    assert out["counts"]["duplicates_removed"] == 0


def test_symbol_normalised_in_session_and_input():
    s, _ = sent(symbol=" abcd ")
    out, err = s.execute("get_news", {"symbol": " Abcd "})
    assert err is False and out["symbol"] == "ABCD"


def test_symbol_mismatch_refused_without_touching_data():
    s, d = sent()
    out, err = s.execute("get_news", {"symbol": "MSFT"})
    assert (out, err) == ({"error": "symbol must be ABCD"}, True)
    assert d.calls == []


@pytest.mark.parametrize("inp", [{}, {"symbol": None}, {"symbol": 5}, {"symbol": ["ABCD"]}])
def test_missing_or_non_string_symbol_refused(inp):
    s, d = sent()
    out, err = s.execute("get_news", inp)
    assert err is True and out == {"error": "symbol must be ABCD"}
    assert d.calls == []


@pytest.mark.parametrize("inp", [None, "ABCD", ["ABCD"], 5])
def test_non_object_input_refused(inp):
    out, err = sent()[0].execute("get_news", inp)
    assert (out, err) == ({"error": "tool input must be an object"}, True)


def test_unknown_tool_refused():
    out, err = sent()[0].execute("get_fundamentals", {"symbol": "ABCD"})
    assert err is True and "unknown tool" in out["error"]


def test_news_exception_gives_generic_error_without_text():
    d = FakeFilingsData(news_raises=RuntimeError(f"boom {SENTINEL}"))
    out, err = sent(d)[0].execute("get_news", {"symbol": "ABCD"})
    assert (out, err) == ({"error": "get_news failed (RuntimeError)"}, True)
    assert SENTINEL not in str(out)


def test_submissions_exception_keeps_the_news_and_adds_a_note():
    d = FakeFilingsData(subs_raises=RuntimeError(f"boom {SENTINEL}"))
    out, err = sent(d)[0].execute("get_news", {"symbol": "ABCD"})
    assert err is False
    assert out["note"] == "filings unavailable (RuntimeError)"
    assert out["filings"] == [] and out["counts"]["filings"] == 0
    assert out["counts"]["headlines"] == 7
    assert any(h["headline"] == "Trading halted in ABCD pending news" for h in out["headlines"])
    assert SENTINEL not in str(out)


def test_malformed_submissions_keeps_the_news_and_adds_a_note():
    d = FakeFilingsData()
    d.submissions = lambda symbol: {"cik": "1", "filings": {}}  # parse_8k_filings raises
    out, err = sent(d)[0].execute("get_news", {"symbol": "ABCD"})
    assert err is False
    assert out["note"] == "filings unavailable (ValueError)"
    assert out["counts"]["headlines"] == 7 and out["filings"] == []


def test_no_note_when_filings_are_fine():
    out, _ = sent()[0].execute("get_news", {"symbol": "ABCD"})
    assert "note" not in out


def test_news_failure_even_with_good_submissions_is_still_a_tool_error():
    d = FakeFilingsData(news_raises=RuntimeError("x"))
    assert sent(d)[0].execute("get_news", {"symbol": "ABCD"}) == (
        {"error": "get_news failed (RuntimeError)"}, True)
    assert ("submissions", "ABCD") not in d.calls  # news is fetched first


def test_no_cik_still_returns_news_with_zero_filings():
    out, err = sent(FakeFilingsData(no_cik=True))[0].execute("get_news", {"symbol": "ABCD"})
    assert err is False and out["filings"] == [] and out["counts"]["filings"] == 0
    assert out["counts"]["headlines"] == 7


def test_sentiment_tool_schema_is_strict_single_symbol():
    (tool,) = S_TOOLS
    assert tool["name"] == "get_news" and tool["strict"] is True
    schema = tool["input_schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["symbol"] and list(schema["properties"]) == ["symbol"]


def test_extra_key_is_refused_by_schema():
    # The strict schema forbids unknown keys (server side). The executor itself only reads
    # "symbol" and ignores extras, so the schema is what refuses them.
    schema = S_TOOLS[0]["input_schema"]
    assert schema["additionalProperties"] is False
    assert "start" not in schema["properties"] and "hours" not in schema["properties"]
    assert F_TOOLS[0]["input_schema"]["additionalProperties"] is False


def test_extra_key_cannot_widen_the_window():
    s, d = sent()
    s.execute("get_news", {"symbol": "ABCD", "hours": 999, "start": "2020-01-01"})
    _, _, start, end = d.calls[0]
    assert end - start == timedelta(hours=24)


# ---------------- fundamentals ----------------

def test_get_fundamentals_returns_earnings_facts_as_of_ny_date():
    s, d = fund()
    out, err = s.execute("get_fundamentals", {"symbol": "ABCD"})
    assert err is False and out["ok"] is True
    assert out["as_of"] == "2026-11-03"
    assert d.calls == [("company_facts", "ABCD")]
    assert out["quarters"][0]["end"] == "2026-06-30"
    assert out["quarters"][0]["revenue"] == "1265000"           # amendment wins
    assert out["growth"]["revenue_yoy_pct"] == "15.00"
    assert out["latest_filed"] == "2026-08-05"                  # 2026-11-10 filing is hidden
    assert out["latest_instant"]["cash"]["value"] == "540000"


def test_as_of_uses_new_york_date_not_utc_date():
    # 2026-11-10 03:00Z is still Nov 9 in New York: the Nov 10 filing must not be visible.
    now = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
    out, _ = fund(now=now)[0].execute("get_fundamentals", {"symbol": "ABCD"})
    assert out["as_of"] == "2026-11-09"
    assert out["quarters"][0]["end"] == "2026-06-30"
    # later the same NY day Nov 10 the late filing appears
    now = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    out, _ = fund(now=now)[0].execute("get_fundamentals", {"symbol": "ABCD"})
    assert out["as_of"] == "2026-11-10"
    assert out["quarters"][0]["end"] == "2026-09-30"
    assert out["growth"]["revenue_yoy_pct"] == "16.67"


def test_no_data_by_as_of_is_ok_false_not_error():
    now = datetime(2025, 1, 1, 12, 0, tzinfo=NY)
    out, err = fund(now=now)[0].execute("get_fundamentals", {"symbol": "ABCD"})
    assert err is False
    assert out == {"ok": False, "reason": "no quarterly data filed on or before 2025-01-01"}


def test_no_cik_refused():
    s, _ = fund(FakeFilingsData(no_cik=True))
    out, err = s.execute("get_fundamentals", {"symbol": "ABCD"})
    assert (out, err) == ({"error": "no SEC filer found for symbol"}, True)


def test_fundamentals_data_exception_handled_without_text():
    d = FakeFilingsData(facts_raises=RuntimeError(f"boom {SENTINEL}"))
    out, err = fund(d)[0].execute("get_fundamentals", {"symbol": "ABCD"})
    assert (out, err) == ({"error": "get_fundamentals failed (RuntimeError)"}, True)
    assert SENTINEL not in str(out)


def test_fundamentals_symbol_mismatch_and_unknown_tool():
    s, d = fund()
    assert s.execute("get_fundamentals", {"symbol": "AAPL"}) == (
        {"error": "symbol must be ABCD"}, True)
    assert d.calls == []
    out, err = s.execute("get_news", {"symbol": "ABCD"})
    assert err is True and "unknown tool" in out["error"]
    assert s.execute("get_fundamentals", "ABCD")[1] is True


def test_fundamentals_tool_schema():
    (tool,) = F_TOOLS
    assert tool["name"] == "get_fundamentals" and tool["strict"] is True
    assert tool["input_schema"]["required"] == ["symbol"]


def test_fundamentals_naive_now_is_caught_as_error_not_raised():
    s = FundamentalsSession("ABCD", datetime(2026, 11, 3, 12, 0), FakeFilingsData())
    out, err = s.execute("get_fundamentals", {"symbol": "ABCD"})
    assert (out, err) == ({"error": "get_fundamentals failed (ValueError)"}, True)
