"""Tests for tools.news. Fixtures, fake openers, fake clock; never the network."""
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tools import news as N

FIX = Path(__file__).parent / "fixtures"
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=NY)  # 17:00Z
START = datetime(2026, 11, 2, 12, 0, tzinfo=NY)
SINCE = datetime(2026, 11, 1, 0, 0, tzinfo=NY)
UA = "Ivan Test research@acme-research.org"


def load(name):
    return json.loads((FIX / name).read_text())


def all_news():
    return {"news": load("alpaca_news_page1.json")["news"] + load("alpaca_news_page2.json")["news"]}


class Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return Resp(self.pages.pop(0))


def query(req):
    return urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)


def headers(req):
    return {k.lower(): v for k, v in req.header_items()}


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def limiter(clock=None, **kw):
    c = clock or FakeClock()
    return N.RateLimiter(clock=c.now, sleep=c.sleep, **kw), c


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "dummy-key-id")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "dummy-secret")


@pytest.fixture
def ua(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", UA)


# ---------- fetch_news ----------

def test_fetch_news_paginates_and_merges(keys):
    p1, p2 = load("alpaca_news_page1.json"), load("alpaca_news_page2.json")
    op = Opener([p1, p2])
    out = N.fetch_news(["abcd"], START, NOW, opener=op)
    assert len(op.requests) == 2
    assert len(out["news"]) == len(p1["news"]) + len(p2["news"]) == 12
    q1, q2 = query(op.requests[0]), query(op.requests[1])
    assert "page_token" not in q1
    assert q2["page_token"] == [p1["next_page_token"]]
    assert q1["symbols"] == ["ABCD"]
    assert q1["start"] == ["2026-11-02T17:00:00Z"] and q1["end"] == ["2026-11-03T17:00:00Z"]
    assert q1["limit"] == ["50"] and q1["sort"] == ["desc"]


def test_fetch_news_auth_headers(keys):
    op = Opener([{"news": [], "next_page_token": None}])
    N.fetch_news(["ABCD"], START, NOW, opener=op)
    h = headers(op.requests[0])
    assert h["apca-api-key-id"] == "dummy-key-id" and h["apca-api-secret-key"] == "dummy-secret"


def test_fetch_news_multiple_symbols_joined(keys):
    op = Opener([{"news": [], "next_page_token": None}])
    N.fetch_news(["abcd", "BIOX", "ABCD"], START, NOW, opener=op)
    assert query(op.requests[0])["symbols"] == ["ABCD,BIOX"]


def test_fetch_news_missing_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    op = Opener([])
    with pytest.raises(RuntimeError):
        N.fetch_news(["ABCD"], START, NOW, opener=op)
    assert op.requests == []


def test_fetch_news_naive_datetimes_rejected(keys):
    op = Opener([])
    with pytest.raises(ValueError):
        N.fetch_news(["ABCD"], datetime(2026, 11, 2, 12), NOW, opener=op)
    with pytest.raises(ValueError):
        N.fetch_news(["ABCD"], START, datetime(2026, 11, 3, 12), opener=op)
    assert op.requests == []


@pytest.mark.parametrize("limit", [1, 25, 50])
def test_fetch_news_limit_in_bounds(keys, limit):
    op = Opener([{"news": [], "next_page_token": None}])
    N.fetch_news(["ABCD"], START, NOW, limit=limit, opener=op)
    assert query(op.requests[0])["limit"] == [str(limit)]


@pytest.mark.parametrize("limit", [0, -1, 51, 1.5, "10", True, None])
def test_fetch_news_limit_out_of_bounds(keys, limit):
    op = Opener([])
    with pytest.raises(ValueError):
        N.fetch_news(["ABCD"], START, NOW, limit=limit, opener=op)
    assert op.requests == []


def test_fetch_news_requires_symbol_and_valid_symbols(keys):
    with pytest.raises(ValueError):
        N.fetch_news([], START, NOW, opener=Opener([]))
    with pytest.raises(ValueError):
        N.fetch_news(["AB CD"], START, NOW, opener=Opener([]))


def test_fetch_news_pagination_guard(keys):
    page = {"news": [], "next_page_token": "again"}
    op = Opener([page] * (N.MAX_NEWS_PAGES + 1))
    with pytest.raises(RuntimeError, match="pagination"):
        N.fetch_news(["ABCD"], START, NOW, opener=op)
    assert len(op.requests) == N.MAX_NEWS_PAGES


def test_fetch_news_error_payload(keys):
    with pytest.raises(RuntimeError, match="error payload"):
        N.fetch_news(["ABCD"], START, NOW, opener=Opener([{"message": "forbidden"}]))


def test_fetch_news_null_news_page(keys):
    out = N.fetch_news(["ABCD"], START, NOW, opener=Opener([{"news": None}]))
    assert out == {"news": []}


def test_fetch_news_list_body_rejected(keys):
    with pytest.raises(RuntimeError, match="unexpected"):
        N.fetch_news(["ABCD"], START, NOW, opener=Opener([[1]]))


def test_fetch_news_without_opener_hits_guard(keys):
    with pytest.raises(RuntimeError, match="network access in tests"):
        N.fetch_news(["ABCD"], START, NOW)


# ---------- SEC user agent ----------

BAD_UAS = [None, "", "   ", "Your Name you@example.com", "X YOU@EXAMPLE.COM"]


@pytest.mark.parametrize("value", BAD_UAS)
def test_sec_user_agent_invalid_raises(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    else:
        monkeypatch.setenv("SEC_USER_AGENT", value)
    lim, clock = limiter()
    op = Opener([])
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        N.fetch_cik_map(op, limiter=lim)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        N.fetch_submissions("12345", op, limiter=lim)
    assert op.requests == []


def test_sec_user_agent_valid_sent_stripped(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", f"  {UA}  ")
    op = Opener([load("sec_company_tickers.json")])
    N.fetch_cik_map(op, limiter=limiter()[0])
    assert headers(op.requests[0])["user-agent"] == UA


def test_sec_user_agent_not_in_error_messages(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "SENTINEL-UA-999 sentinel@acme-research.org")
    with pytest.raises(RuntimeError) as ei:
        N.fetch_cik_map(Opener([{"message": "Undeclared Automated Tool"}]), limiter=limiter()[0])
    assert "SENTINEL-UA-999" not in str(ei.value)


# ---------- fetch_cik_map / fetch_submissions ----------

def test_cik_map_zero_pads(ua):
    op = Opener([load("sec_company_tickers.json")])
    out = N.fetch_cik_map(op, limiter=limiter()[0])
    assert out == {
        "AAPL": "0000320193",
        "MSFT": "0000789019",
        "TSLA": "0001318605",
        "ABCD": "0000012345",
    }
    assert all(len(v) == 10 for v in out.values())
    assert op.requests[0].full_url == N.CIK_MAP_URL


def test_cik_map_skips_malformed_and_keeps_first_duplicate(ua):
    body = {
        "0": {"cik_str": 1, "ticker": "dup", "title": "first"},
        "1": {"cik_str": 2, "ticker": "DUP", "title": "second"},
        "2": {"cik_str": "abc", "ticker": "BAD1"},
        "3": {"cik_str": -5, "ticker": "BAD2"},
        "4": {"cik_str": 12345678901, "ticker": "BAD3"},
        "5": {"cik_str": 7},
        "6": {"cik_str": 8, "ticker": "  "},
        "7": "not a dict",
        "8": {"cik_str": 9, "ticker": "good"},
    }
    out = N.fetch_cik_map(Opener([body]), limiter=limiter()[0])
    assert out == {"DUP": "0000000001", "GOOD": "0000000009"}


def test_cik_map_error_payload(ua):
    with pytest.raises(RuntimeError, match="error payload"):
        N.fetch_cik_map(Opener([{"message": "rate limited"}]), limiter=limiter()[0])


def test_cik_map_list_body_rejected(ua):
    with pytest.raises(RuntimeError, match="unexpected"):
        N.fetch_cik_map(Opener([[1]]), limiter=limiter()[0])


@pytest.mark.parametrize("cik", [12345, "12345", "0000012345"])
def test_fetch_submissions_pads_cik_in_url(ua, cik):
    op = Opener([load("sec_submissions_8k.json")])
    out = N.fetch_submissions(cik, op, limiter=limiter()[0])
    assert op.requests[0].full_url == "https://data.sec.gov/submissions/CIK0000012345.json"
    assert out["name"] == "ABCD Holdings Inc"


@pytest.mark.parametrize("cik", ["abc", 0, "-5", "", 12345678901, None, "1;2", "../1"])
def test_fetch_submissions_invalid_cik(ua, cik):
    op = Opener([])
    with pytest.raises(ValueError):
        N.fetch_submissions(cik, op, limiter=limiter()[0])
    assert op.requests == []


def test_fetch_submissions_list_body_rejected(ua):
    with pytest.raises(RuntimeError, match="unexpected"):
        N.fetch_submissions("1", Opener([[1]]), limiter=limiter()[0])


def test_sec_calls_use_module_limiter_by_default(ua, monkeypatch):
    lim, clock = limiter()
    monkeypatch.setattr(N, "SEC_LIMITER", lim)
    N.fetch_cik_map(Opener([load("sec_company_tickers.json")]))
    assert len(lim._calls) == 1


def test_sec_call_without_opener_hits_guard(ua):
    with pytest.raises(RuntimeError, match="network access in tests"):
        N.fetch_cik_map(limiter=limiter()[0])


# ---------- rate limiter ----------

def test_ten_calls_in_a_second_do_not_sleep():
    lim, clock = limiter()
    for _ in range(10):
        lim.wait()
    assert clock.sleeps == []


def test_eleventh_call_within_a_second_sleeps():
    lim, clock = limiter()
    for _ in range(10):
        lim.wait()
    lim.wait()
    assert clock.sleeps == [pytest.approx(1.0)]


def test_eleventh_call_sleeps_only_the_remainder():
    lim, clock = limiter()
    for i in range(10):
        clock.t = i * 0.05
        lim.wait()  # last at 0.45
    clock.t = 0.5
    lim.wait()
    assert clock.sleeps == [pytest.approx(0.5)]  # oldest call at t=0 expires at 1.0


def test_eleventh_call_after_window_does_not_sleep():
    lim, clock = limiter()
    for _ in range(10):
        lim.wait()
    clock.t = 1.0  # exactly one period later: oldest expired
    lim.wait()
    assert clock.sleeps == []


def test_calls_spread_over_time_never_sleep():
    lim, clock = limiter()
    for i in range(40):
        clock.t = i * 0.15  # at most 7 calls per second
        lim.wait()
    assert clock.sleeps == []


def test_sustained_burst_is_throttled_to_ten_per_second():
    lim, clock = limiter()
    for _ in range(30):
        lim.wait()
    assert clock.t >= 2.0 - 1e-9  # 30 calls cannot finish in under two sleeps
    assert len(clock.sleeps) >= 2


def test_custom_limit():
    lim, clock = limiter(max_calls=2, period=5.0)
    lim.wait()
    lim.wait()
    lim.wait()
    assert clock.sleeps == [pytest.approx(5.0)]


# ---------- parse_8k_filings ----------

def filings(since=SINCE):
    return N.parse_8k_filings(load("sec_submissions_8k.json"), since)


def by_acc(rows):
    return {r["accession"][-6:]: r for r in rows}


def test_8k_form_filter_and_since_filter():
    rows = filings()
    assert [r["accession"][-6:] for r in rows] == ["000005", "000010", "000009", "000003"]
    assert {r["form"] for r in rows} == {"8-K", "8-K/A"}  # no 10-Q, no Form 4


def test_8k_since_earlier_includes_old_filings():
    rows = filings(datetime(2026, 10, 1, tzinfo=NY))
    accs = {r["accession"][-6:] for r in rows}
    assert accs == {"000010", "000009", "000008", "000005", "000004", "000003"}


def test_8k_since_is_inclusive_at_boundary():
    at = datetime(2026, 11, 2, 16, 30, tzinfo=NY)  # 000009 accepted 21:30Z
    assert "000009" in by_acc(filings(at))
    assert "000009" not in by_acc(filings(at + timedelta(seconds=1)))


def test_8k_item_to_catalyst_map():
    r = by_acc(filings())
    a = r["000010"]
    assert a["items"] == ["2.02", "9.01"]
    assert a["catalysts"] == ["earnings"]  # 9.01 (exhibits) is not a catalyst (#87)
    assert a["red_flags"] == []
    b = r["000009"]
    assert b["items"] == ["3.02"] and b["catalysts"] == ["unregistered_equity_sale"]
    assert b["red_flags"] == []  # dilution is a catalyst, not a listed red flag
    assert r["000003"]["catalysts"] == ["other_event"] and r["000003"]["form"] == "8-K/A"


def test_8k_red_flags_and_unknown_code():
    r = by_acc(filings())["000005"]
    assert r["items"] == ["1.03", "99.99"]
    assert r["catalysts"] == ["bankruptcy", "item_99.99"]
    assert r["red_flags"] == ["bankruptcy"]


def test_8k_all_red_flag_codes():
    expected = {
        "1.03": "bankruptcy",
        "3.01": "delisting_notice",
        "4.02": "non_reliance_restatement",
    }
    for code, name in expected.items():
        sub = {
            "cik": "5",
            "filings": {"recent": {
                "accessionNumber": ["0000000005-26-000001"], "filingDate": ["2026-11-03"],
                "acceptanceDateTime": ["2026-11-03T15:00:00.000Z"], "form": ["8-K"],
                "items": [code], "primaryDocument": ["x.htm"],
            }},
        }
        row = N.parse_8k_filings(sub, SINCE)[0]
        assert row["red_flags"] == [name], code


def test_8k_url_construction_strips_dashes_and_leading_zero_cik():
    a = by_acc(filings())["000010"]
    assert a["url"] == "https://www.sec.gov/Archives/edgar/data/12345/000001234526000010/abcd-8k.htm"
    assert a["primary_document"] == "abcd-8k.htm"


def test_8k_filed_at_is_new_york_time():
    a = by_acc(filings())["000010"]
    assert a["filed_at"] == "2026-11-03T07:05:12-05:00"  # 12:05:12Z in EST
    assert a["time_estimated"] is False


def test_8k_empty_acceptance_falls_back_to_end_of_filing_date():
    r = by_acc(filings())["000005"]
    assert r["time_estimated"] is True
    assert r["filed_at"] == "2026-11-03T23:59:59-05:00"


def test_8k_sorted_newest_first():
    instants = [datetime.fromisoformat(r["filed_at"]).astimezone(UTC) for r in filings()]
    assert len(instants) == 4
    assert instants == sorted(instants, reverse=True)


def sub_with(**overrides):
    recent = {
        "accessionNumber": ["0000000005-26-000001"], "filingDate": ["2026-11-03"],
        "acceptanceDateTime": ["2026-11-03T15:00:00.000Z"], "form": ["8-K"],
        "items": ["8.01"], "primaryDocument": ["x.htm"],
    }
    recent.update(overrides)
    return {"cik": "5", "filings": {"recent": recent}}


def test_8k_unequal_arrays_raise():
    with pytest.raises(ValueError, match="unequal"):
        N.parse_8k_filings(sub_with(items=["8.01", "2.02"]), SINCE)


def test_8k_missing_array_raises():
    sub = sub_with()
    del sub["filings"]["recent"]["items"]
    with pytest.raises(ValueError):
        N.parse_8k_filings(sub, SINCE)


@pytest.mark.parametrize("bad", [None, [], "x", {}, {"filings": None}, {"filings": {}},
                                 {"filings": {"recent": []}}])
def test_8k_malformed_submissions_raise(bad):
    with pytest.raises(ValueError):
        N.parse_8k_filings(bad, SINCE)


def test_8k_missing_cik_raises():
    sub = sub_with()
    del sub["cik"]
    with pytest.raises(ValueError, match="cik"):
        N.parse_8k_filings(sub, SINCE)


def test_8k_naive_since_raises():
    with pytest.raises(ValueError):
        N.parse_8k_filings(sub_with(), datetime(2026, 11, 1))


def test_8k_entry_missing_accession_or_document_raises():
    with pytest.raises(ValueError):
        N.parse_8k_filings(sub_with(accessionNumber=[""]), SINCE)
    with pytest.raises(ValueError):
        N.parse_8k_filings(sub_with(primaryDocument=[""]), SINCE)


def test_8k_no_usable_time_raises():
    with pytest.raises(ValueError):
        N.parse_8k_filings(sub_with(acceptanceDateTime=[""], filingDate=["garbage"]), SINCE)


def test_8k_ignores_bad_rows_that_are_not_8k():
    sub = sub_with(
        accessionNumber=["", "0000000005-26-000001"], filingDate=["bad", "2026-11-03"],
        acceptanceDateTime=["", "2026-11-03T15:00:00.000Z"], form=["4", "8-K"],
        items=["", "8.01"], primaryDocument=["", "x.htm"],
    )
    assert len(N.parse_8k_filings(sub, SINCE)) == 1


def test_8k_empty_items_gives_no_catalysts():
    row = N.parse_8k_filings(sub_with(items=[""]), SINCE)[0]
    assert row["items"] == [] and row["catalysts"] == []


def test_8k_sort_across_dst_fallback_hour():
    # 05:30Z = 01:30 EDT (older); 06:10Z = 01:10 EST (newer).
    # Regression: a sort on the local ISO strings put the older one first,
    # because "01:30-04:00" > "01:10-05:00". Sorting must use real instants.
    sub = sub_with(
        accessionNumber=["0000000005-26-000001", "0000000005-26-000002"],
        filingDate=["2026-11-01", "2026-11-01"],
        acceptanceDateTime=["2026-11-01T05:30:00.000Z", "2026-11-01T06:10:00.000Z"],
        form=["8-K", "8-K"], items=["8.01", "8.01"], primaryDocument=["a.htm", "b.htm"],
    )
    rows = N.parse_8k_filings(sub, datetime(2026, 10, 1, tzinfo=NY))
    assert [r["accession"][-1] for r in rows] == ["2", "1"]  # newest (06:10Z) first


# ---------- headline_flags ----------

@pytest.mark.parametrize(
    "text,flag",
    [
        ("ABCD announces public offering", "offering"),
        ("Company prices registered direct deal", "offering"),
        ("Firm files $100M ATM program", "offering"),
        ("Trading halted in ABCD", "halt"),
        ("Nasdaq halt on ABCD", "halt"),
        ("FDA clears device", "fda"),
        ("Gets CRL from regulator", "fda"),
        ("Raises full-year guidance", "guidance"),
        ("Updates outlook", "guidance"),
        ("Analyst downgrade to sell", "analyst"),
        ("Upgrade to buy", "analyst"),
        ("Q3 earnings beat", "earnings"),
        ("EPS of $1.20", "earnings"),
        ("Reports quarterly results", "earnings"),
        ("Announces merger agreement", "m_and_a"),
        ("To acquire rival", "m_and_a"),
        ("Completes acquisition", "m_and_a"),
        ("Buyout offer received", "m_and_a"),
        ("Files for bankruptcy", "bankruptcy"),
        ("Enters Chapter 11", "bankruptcy"),
        ("Nasdaq to delist shares", "delisting"),
        ("Delisting notice received", "delisting"),
        ("Opens investigation", "legal"),
        ("SEC probe disclosed", "legal"),
        ("Shareholder lawsuit filed", "legal"),
    ],
)
def test_headline_flag_groups(text, flag):
    assert flag in N.headline_flags(text)


def test_headline_flags_case_insensitive():
    assert N.headline_flags("TRADING HALTED") == N.headline_flags("trading halted") == ["halt"]
    assert "fda" in N.headline_flags("fda approval")
    assert "offering" in N.headline_flags("Public OFFERING")


def test_headline_flags_whitespace_in_phrase():
    assert "offering" in N.headline_flags("public   offering")
    assert "bankruptcy" in N.headline_flags("chapter\t11 filing")


@pytest.mark.parametrize(
    "text",
    ["halter top recall", "thalt study", "atmosphere report", "ABCD halt2",
     "xFDA", "unacquire", "pre-approvals", ""],
)
def test_headline_flags_whole_word_only(text):
    assert N.headline_flags(text) == []


def test_headline_flags_punctuation_adjacent_matches():
    assert N.headline_flags("(halt)") == ["halt"]
    assert N.headline_flags("FDA-approved") == ["fda"]  # hyphen is not a letter/digit


def test_headline_flags_sorted_and_multi():
    flags = N.headline_flags("FDA approval and public offering after halt")
    assert flags == ["fda", "halt", "offering"]


@pytest.mark.parametrize("bad", [None, 5, [], b"halt"])
def test_headline_flags_non_string(bad):
    assert N.headline_flags(bad) == []


# ---------- digest ----------

_DEFAULT = object()


def digest(symbol="ABCD", news=_DEFAULT, filings_=None, now=NOW, **kw):
    return N.digest(symbol, all_news() if news is _DEFAULT else news, filings_ or [], now, **kw)


def test_digest_headline_order_and_ny_times():
    d = digest()
    assert [h["time"] for h in d["headlines"]] == [
        "2026-11-03T11:30:00-05:00",
        "2026-11-03T10:10:00-05:00",
        "2026-11-03T09:05:00-05:00",
        "2026-11-03T08:00:00-05:00",
        "2026-11-03T07:00:00-05:00",
        "2026-11-03T06:00:00-05:00",
        "2026-11-02T12:00:00-05:00",
    ]
    assert d["now"] == "2026-11-03T12:00:00-05:00"
    assert d["window_start"] == "2026-11-02T12:00:00-05:00"
    assert d["symbol"] == "ABCD"


def test_digest_dedupe_keeps_earliest_and_counts():
    d = digest()
    offering = [h for h in d["headlines"] if "offering" in h["flags"]]
    assert len(offering) == 1
    assert offering[0]["time"] == "2026-11-03T11:30:00-05:00"  # 16:30Z original, not 16:45Z copy
    assert offering[0]["headline"] == "ABCD announces $50 million public offering of common stock"
    assert d["counts"] == {
        "news_for_symbol": 11,
        "headlines": 7,
        "duplicates_removed": 1,
        "outside_window": 2,  # future item and the one 1 minute before the window
        "malformed_skipped": 1,  # bad created_at
        "filings": 0,
    }


def test_digest_dedupe_is_order_independent():
    items = all_news()["news"]
    d1 = digest(news={"news": items})
    d2 = digest(news={"news": list(reversed(items))})
    assert d1["headlines"] == d2["headlines"]


def test_digest_symbol_filter():
    d = digest("BIOX")
    assert [h["headline"] for h in d["headlines"]] == ["ABCD receives FDA approval for lead drug"]
    assert d["counts"]["news_for_symbol"] == 1
    assert digest("OTHR")["counts"]["news_for_symbol"] == 1
    assert digest("NONE")["headlines"] == []


def test_digest_symbol_case_insensitive():
    assert digest(" abcd ")["symbol"] == "ABCD"
    assert len(digest("abcd")["headlines"]) == 7


def test_digest_window_excludes_future_and_old_and_includes_boundary():
    heads = [h["headline"] for h in digest()["headlines"]]
    assert not any("acquired in $2B merger" in h for h in heads)  # future
    assert not any("guidance reiterated" in h for h in heads)  # 17:00Z - 1 minute
    assert any("exactly at window start" in h for h in heads)  # exactly 24h before


def test_digest_item_at_now_included_one_second_after_excluded():
    def item(created):
        return {"headline": "ABCD thing happened", "symbols": ["ABCD"], "created_at": created}

    inc = digest(news={"news": [item("2026-11-03T17:00:00Z")]})
    exc = digest(news={"news": [item("2026-11-03T17:00:01Z")]})
    assert len(inc["headlines"]) == 1
    assert exc["headlines"] == [] and exc["counts"]["outside_window"] == 1


def test_digest_lookback_hours():
    d = digest(lookback_hours=2)  # window 10:00-12:00 ET
    assert [h["time"][11:16] for h in d["headlines"]] == ["11:30", "10:10"]
    assert d["window_start"] == "2026-11-03T10:00:00-05:00"


def test_digest_flags_catalysts_and_red_flags_from_headlines():
    d = digest()
    assert d["catalysts"] == ["earnings", "fda", "halt", "m_and_a", "offering"]
    assert d["red_flags"] == ["halt"]
    halted = [h for h in d["headlines"] if "halt" in h["flags"]]
    assert len(halted) == 1 and halted[0]["source"] == "benzinga"


def test_digest_near_miss_headline_has_no_flags():
    d = digest()
    near = [h for h in d["headlines"] if "halter" in h["headline"]]
    assert near and near[0]["flags"] == []


def test_digest_unions_in_filing_catalysts_and_red_flags():
    rows = N.parse_8k_filings(load("sec_submissions_8k.json"), SINCE)
    d = digest(filings_=rows)
    assert d["counts"]["filings"] == 4 and d["filings"] == rows
    from_filings = {"bankruptcy", "unregistered_equity_sale", "other_event", "item_99.99"}
    assert from_filings <= set(d["catalysts"])
    assert {"fda", "halt", "offering", "earnings"} <= set(d["catalysts"])  # headline flags kept
    assert d["red_flags"] == ["bankruptcy", "halt"]
    assert d["catalysts"] == sorted(d["catalysts"])


def test_digest_headline_red_flags_bankruptcy_delisting():
    news = {"news": [
        {"headline": "ABCD files for bankruptcy", "symbols": ["ABCD"],
         "created_at": "2026-11-03T15:00:00Z"},
        {"headline": "ABCD receives delisting notice", "symbols": ["ABCD"],
         "created_at": "2026-11-03T14:00:00Z"},
        {"headline": "ABCD opens investigation", "symbols": ["ABCD"],
         "created_at": "2026-11-03T13:00:00Z"},
    ]}
    d = digest(news=news)
    assert d["red_flags"] == ["bankruptcy", "delisting"]  # legal is a catalyst, not a red flag
    assert "legal" in d["catalysts"]


def test_digest_equal_time_duplicate_keeps_first_seen():
    a = {"headline": "ABCD Big News!", "symbols": ["ABCD"], "created_at": "2026-11-03T15:00:00Z",
         "source": "first"}
    b = {"headline": "abcd big news", "symbols": ["ABCD"], "created_at": "2026-11-03T15:00:00Z",
         "source": "second"}
    d = digest(news={"news": [a, b]})
    assert len(d["headlines"]) == 1 and d["headlines"][0]["source"] == "first"
    assert d["counts"]["duplicates_removed"] == 1


def test_digest_skips_malformed_items_without_raising():
    news = {"news": [
        "junk",
        None,
        {"headline": "ABCD no symbols field", "created_at": "2026-11-03T15:00:00Z"},
        {"headline": "ABCD symbols wrong type", "symbols": "ABCD",
         "created_at": "2026-11-03T15:00:00Z"},
        {"headline": None, "symbols": ["ABCD"], "created_at": "2026-11-03T15:00:00Z"},
        {"headline": "   !!! ", "symbols": ["ABCD"], "created_at": "2026-11-03T15:00:00Z"},
        {"headline": "ABCD naive time", "symbols": ["ABCD"], "created_at": "2026-11-03T15:00:00"},
        {"headline": "ABCD missing time", "symbols": ["ABCD"]},
        {"headline": "ABCD good one", "symbols": ["ABCD"], "created_at": "2026-11-03T15:00:00Z"},
    ]}
    d = digest(news=news)
    assert [h["headline"] for h in d["headlines"]] == ["ABCD good one"]
    # skipped: "junk", None, null headline, punctuation-only headline, naive time, missing time.
    # Items without a symbols list are filtered out silently and not counted.
    assert d["counts"]["malformed_skipped"] == 6


@pytest.mark.parametrize("bad", [None, [], "x", {"news": None}, {"news": "x"}, {}])
def test_digest_bad_news_container_gives_empty(bad):
    d = digest(news=bad)
    assert d["headlines"] == [] and d["counts"]["news_for_symbol"] == 0


def test_digest_non_list_filings_ignored():
    d = N.digest("ABCD", {"news": []}, "nope", NOW)
    assert d["filings"] == [] and d["counts"]["filings"] == 0
    d = N.digest("ABCD", {"news": []}, [{"catalysts": ["x"]}, "junk", None], NOW)
    assert d["counts"]["filings"] == 1 and d["catalysts"] == ["x"]


def test_digest_naive_now_raises():
    with pytest.raises(ValueError):
        N.digest("ABCD", {"news": []}, [], datetime(2026, 11, 3, 12, 0))


def test_digest_converts_utc_now_to_ny():
    d = N.digest("ABCD", all_news(), [], datetime(2026, 11, 3, 17, 0, tzinfo=ZoneInfo("UTC")))
    assert d["now"] == "2026-11-03T12:00:00-05:00"
    assert len(d["headlines"]) == 7


def test_digest_is_json_serializable():
    rows = N.parse_8k_filings(load("sec_submissions_8k.json"), SINCE)
    json.dumps(digest(filings_=rows))


# ---------- DST: fall-back (2026-11-01) and spring-forward (2026-03-08) ----------

def z(*args):
    return datetime(*args, tzinfo=UTC)


def item(headline, created, symbols=("ABCD",)):
    return {"headline": headline, "symbols": list(symbols), "created_at": created, "source": "s"}


FALL_NOW = z(2026, 11, 1, 7, 0)  # 02:00 EST, after the repeated 01:00-02:00 hour


def test_digest_orders_by_real_instant_in_fall_back_hour():
    # 05:30Z = 01:30 EDT (older); 06:10Z = 01:10 EST (newer). Wall clock would reverse them.
    news = {"news": [item("ABCD one", "2026-11-01T05:30:00Z"),
                     item("ABCD two", "2026-11-01T06:10:00Z")]}
    d = N.digest("ABCD", news, [], FALL_NOW)
    assert [h["headline"] for h in d["headlines"]] == ["ABCD two", "ABCD one"]
    assert [h["time"] for h in d["headlines"]] == [
        "2026-11-01T01:10:00-05:00",
        "2026-11-01T01:30:00-04:00",
    ]
    assert d["now"] == "2026-11-01T02:00:00-05:00"


def test_digest_dedupe_keeps_earliest_real_instant_in_fall_back_hour():
    # The earlier copy shows a LATER wall-clock (01:30) than the newer copy (01:10).
    news = {"news": [item("abcd halted again", "2026-11-01T06:10:00Z"),
                     item("ABCD Halted Again!", "2026-11-01T05:30:00Z"),
                     item("ABCD later item", "2026-11-01T06:40:00Z")]}
    d = N.digest("ABCD", news, [], FALL_NOW)
    assert d["counts"]["duplicates_removed"] == 1
    assert [h["time"] for h in d["headlines"]] == [
        "2026-11-01T01:40:00-05:00",
        "2026-11-01T01:30:00-04:00",  # the 05:30Z copy survived
    ]
    assert d["headlines"][1]["headline"] == "ABCD Halted Again!"


def test_digest_item_after_now_in_fall_back_hour_excluded():
    # now = 05:40Z (01:40 EDT). An item at 06:10Z (01:10 EST) is later in real time.
    news = {"news": [item("ABCD early", "2026-11-01T05:30:00Z"),
                     item("ABCD future", "2026-11-01T06:10:00Z")]}
    d = N.digest("ABCD", news, [], z(2026, 11, 1, 5, 40))
    assert [h["headline"] for h in d["headlines"]] == ["ABCD early"]
    assert d["counts"]["outside_window"] == 1


def fall_sub(accepted):
    n = len(accepted)
    return {"cik": "5", "filings": {"recent": {
        "accessionNumber": [f"0000000005-26-{i:06d}" for i in range(1, n + 1)],
        "filingDate": ["2026-11-01"] * n,
        "acceptanceDateTime": accepted,
        "form": ["8-K"] * n, "items": ["8.01"] * n,
        "primaryDocument": [f"d{i}.htm" for i in range(1, n + 1)],
    }}}


OLD = datetime(2026, 10, 1, tzinfo=NY)


def test_filings_ordered_by_real_instant_in_fall_back_hour():
    rows = N.parse_8k_filings(
        fall_sub(["2026-11-01T05:30:00.000Z", "2026-11-01T06:10:00.000Z"]), OLD
    )
    assert [r["filed_at"] for r in rows] == [
        "2026-11-01T01:10:00-05:00",  # 06:10Z, newest
        "2026-11-01T01:30:00-04:00",  # 05:30Z
    ]
    assert [r["accession"][-1] for r in rows] == ["2", "1"]


def test_since_filter_uses_real_instant_in_fall_back_hour():
    since = z(2026, 11, 1, 5, 30).astimezone(NY)  # 01:30 EDT
    sub = fall_sub(["2026-11-01T06:10:00.000Z", "2026-11-01T05:29:00.000Z"])
    rows = N.parse_8k_filings(sub, since)
    # 06:10Z is 01:10 EST: earlier on the wall clock than since, but later in real time -> kept.
    # 05:29Z is 01:29 EDT: one real minute before since -> dropped.
    assert [r["accession"][-1] for r in rows] == ["1"]


def test_since_filter_inclusive_at_instant_and_excludes_after():
    sub = fall_sub(["2026-11-01T06:10:00.000Z"])
    assert len(N.parse_8k_filings(sub, z(2026, 11, 1, 6, 10).astimezone(NY))) == 1
    assert len(N.parse_8k_filings(sub, z(2026, 11, 1, 6, 11).astimezone(NY))) == 0


def test_since_filter_with_ambiguous_wall_clock_since():
    # since built from the EST side of the repeated hour (01:10 EST = 06:10Z).
    since = datetime(2026, 11, 1, 1, 10, tzinfo=NY, fold=1)
    sub = fall_sub(["2026-11-01T05:30:00.000Z", "2026-11-01T06:15:00.000Z"])
    assert [r["accession"][-1] for r in N.parse_8k_filings(sub, since)] == ["2"]


def lookback_items(now, minutes_before):
    """One item per offset (minutes before `now`), headline carries the offset."""
    return {"news": [
        item(f"ABCD item {m}", (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for m in minutes_before
    ]}


def test_lookback_24h_is_24_real_hours_across_fall_back():
    # 12:00 EST on Nov 1; the wall-clock span back to Oct 31 12:00 would be 25 real hours.
    now = z(2026, 11, 1, 17, 0)
    news = lookback_items(now, [24 * 60 - 1, 24 * 60, 24 * 60 + 1])  # 23h59m, 24h, 24h01m
    d = N.digest("ABCD", news, [], now.astimezone(NY))
    assert {h["headline"] for h in d["headlines"]} == {"ABCD item 1439", "ABCD item 1440"}
    assert d["counts"]["outside_window"] == 1
    assert d["window_start"] == "2026-10-31T13:00:00-04:00"  # 17:00Z on Oct 31


def test_lookback_24h_is_24_real_hours_across_spring_forward():
    now = z(2026, 3, 8, 16, 0)  # 12:00 EDT; 24 real hours earlier is 11:00 EST on Mar 7
    news = lookback_items(now, [24 * 60 - 1, 24 * 60, 24 * 60 + 1])
    d = N.digest("ABCD", news, [], now.astimezone(NY))
    assert {h["headline"] for h in d["headlines"]} == {"ABCD item 1439", "ABCD item 1440"}
    assert d["counts"]["outside_window"] == 1
    assert d["window_start"] == "2026-03-07T11:00:00-05:00"


def test_lookback_hours_param_across_spring_forward_gap():
    now = z(2026, 3, 8, 7, 30)  # 03:30 EDT, one real hour after 01:30 EST
    news = lookback_items(now, [59, 60, 61])
    d = N.digest("ABCD", news, [], now.astimezone(NY), lookback_hours=1)
    assert {h["headline"] for h in d["headlines"]} == {"ABCD item 59", "ABCD item 60"}
    assert d["window_start"] == "2026-03-08T01:30:00-05:00"


# ---------- headline keyword inflections ----------

@pytest.mark.parametrize(
    "text,flag",
    [
        ("Nasdaq halts trading in ABCD", "halt"),
        ("Trading halted", "halt"),
        ("Regulator halting sales", "halt"),
        ("Exchange to delist ABCD", "delisting"),
        ("ABCD shares delisted", "delisting"),
        ("ABCD delisting looms", "delisting"),
        ("ABCD to acquire WidgetCo", "m_and_a"),
        ("ABCD acquires WidgetCo", "m_and_a"),
        ("ABCD acquired by BigCo", "m_and_a"),
        ("ABCD acquiring WidgetCo", "m_and_a"),
        ("Analyst downgrade of ABCD", "analyst"),
        ("Analyst downgrades ABCD", "analyst"),
        ("Analyst upgrade of ABCD", "analyst"),
        ("Analyst upgrades ABCD", "analyst"),
        ("ABCD prices offering", "offering"),
        ("ABCD prices offerings", "offering"),
        ("Two mergers announced", "m_and_a"),
        ("Merger talks", "m_and_a"),
        ("ABCD faces lawsuit", "legal"),
        ("ABCD faces lawsuits", "legal"),
    ],
)
def test_headline_inflections_flag(text, flag):
    assert flag in N.headline_flags(text)


@pytest.mark.parametrize(
    "text",
    ["Shareholder approval of plan", "Board approval granted", "halter top", "thalt", "xFDA",
     "unacquire", "acquirer named", "offeringsx", "merge soon"],
)
def test_headline_non_keywords_stay_unflagged(text):
    assert N.headline_flags(text) == []


def test_shareholder_approval_is_not_fda_but_fda_approval_still_is():
    assert "fda" not in N.headline_flags("Shareholder approval")
    assert "fda" in N.headline_flags("FDA approval granted")
    assert "fda" in N.headline_flags("Receives CRL")


def test_digest_red_flags_for_halts_and_delisted():
    news = {"news": [item("Nasdaq halts ABCD", "2026-11-03T15:00:00Z"),
                     item("ABCD shares delisted", "2026-11-03T14:00:00Z")]}
    d = digest(news=news)
    assert d["red_flags"] == ["delisting", "halt"]
    assert d["catalysts"] == ["delisting", "halt"]


def test_digest_no_red_flags_for_non_red_inflections():
    news = {"news": [item("Analyst upgrades ABCD", "2026-11-03T15:00:00Z"),
                     item("ABCD acquires WidgetCo", "2026-11-03T14:00:00Z")]}
    d = digest(news=news)
    assert d["red_flags"] == []
    assert d["catalysts"] == ["analyst", "m_and_a"]


# ---------- RateLimiter robustness ----------

def test_short_sleep_never_lets_eleven_calls_into_one_second():
    t = [0.0]
    stamps = []

    def clock():
        return t[0]

    def short_sleep(s):  # wakes early: only advances 60% of the request
        t[0] += s * 0.6

    lim = N.RateLimiter(max_calls=10, clock=clock, sleep=short_sleep)
    for _ in range(40):
        lim.wait()
        stamps.append(t[0])
    for i, now in enumerate(stamps):
        in_window = [s for s in stamps[: i + 1] if now - s < 1.0]
        assert len(in_window) <= 10, (i, in_window)


def test_short_sleep_blocks_until_slot_really_opens():
    t = [0.0]

    def clock():
        return t[0]

    def short_sleep(s):
        t[0] += s / 2

    lim = N.RateLimiter(clock=clock, sleep=short_sleep)
    for _ in range(10):
        lim.wait()
    lim.wait()
    assert t[0] >= 1.0  # the 11th call could only happen once the first expired


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "10", None])
def test_limiter_rejects_bad_max_calls(bad):
    with pytest.raises(ValueError):
        N.RateLimiter(max_calls=bad)


@pytest.mark.parametrize("bad", [0, -1, -0.5, float("nan")])
def test_limiter_rejects_bad_period(bad):
    with pytest.raises(ValueError):
        N.RateLimiter(period=bad)


def test_limiter_minimum_valid_settings():
    lim, clock = limiter(max_calls=1, period=0.5)
    lim.wait()
    lim.wait()
    assert clock.sleeps == [pytest.approx(0.5)]


def test_module_sec_limiter_allows_nine_per_second():
    assert N.SEC_LIMITER.max_calls == 9
    assert N.SEC_LIMITER.period == 1.0
    assert N.SEC_MAX_CALLS_PER_SECOND == 10  # the SEC ceiling it stays under
    clock = FakeClock()
    twin = N.RateLimiter(max_calls=N.SEC_LIMITER.max_calls, period=N.SEC_LIMITER.period,
                         clock=clock.now, sleep=clock.sleep)
    for _ in range(9):
        twin.wait()
    assert clock.sleeps == []
    twin.wait()  # the tenth call within the second must wait
    assert clock.sleeps == [pytest.approx(1.0)]


# ---------- CIK validation ----------

BAD_CIKS = ["١٢٣", "１２３", "-5", 0, "0", "00000", -320193,
            "12345678901", " ", "1e5", "+5"]


@pytest.mark.parametrize("cik", BAD_CIKS, ids=ascii)
def test_fetch_submissions_rejects_bad_cik_without_request(ua, cik):
    op = Opener([])
    with pytest.raises(ValueError):
        N.fetch_submissions(cik, op, limiter=limiter()[0])
    assert op.requests == []


def test_cik_map_skips_unicode_digit_and_nonpositive_ciks(ua):
    body = {
        "0": {"cik_str": "١٢٣", "ticker": "ARAB"},
        "1": {"cik_str": "１２", "ticker": "FULL"},
        "2": {"cik_str": -1, "ticker": "NEG"},
        "3": {"cik_str": 0, "ticker": "ZERO"},
        "4": {"cik_str": "320193", "ticker": "AAPL"},
    }
    assert N.fetch_cik_map(Opener([body]), limiter=limiter()[0]) == {"AAPL": "0000320193"}


@pytest.mark.parametrize("cik", ["320193", 320193, " 320193 "])
def test_cik_pads_to_ten_digits(ua, cik):
    op = Opener([load("sec_submissions_8k.json")])
    N.fetch_submissions(cik, op, limiter=limiter()[0])
    assert op.requests[0].full_url.endswith("/CIK0000320193.json")


def test_submissions_cik_field_must_be_ascii_digits():
    sub = sub_with()
    sub["cik"] = "١٢٣"
    with pytest.raises(ValueError, match="cik"):
        N.parse_8k_filings(sub, SINCE)
    sub["cik"] = "0000000005"
    assert N.parse_8k_filings(sub, SINCE)[0]["url"].startswith(
        "https://www.sec.gov/Archives/edgar/data/5/"
    )


# ---------- argument validation ----------

@pytest.mark.parametrize("hours", [0, -1, -24, True, 1.5, "24", None])
def test_digest_rejects_bad_lookback(hours):
    with pytest.raises(ValueError):
        N.digest("ABCD", {"news": []}, [], NOW, lookback_hours=hours)


def test_digest_minimum_lookback_ok():
    d = N.digest("ABCD", {"news": []}, [], NOW, lookback_hours=1)
    assert d["window_start"] == "2026-11-03T11:00:00-05:00"


def test_fetch_news_start_after_end_rejected(keys):
    op = Opener([])
    with pytest.raises(ValueError, match="start"):
        N.fetch_news(["ABCD"], NOW, START, opener=op)
    assert op.requests == []


def test_fetch_news_start_equal_end_allowed(keys):
    op = Opener([{"news": [], "next_page_token": None}])
    N.fetch_news(["ABCD"], NOW, NOW, opener=op)
    assert len(op.requests) == 1


def test_fetch_news_start_after_end_compared_in_utc(keys):
    # The same instant in two zones is equal, not "after".
    op = Opener([{"news": [], "next_page_token": None}])
    N.fetch_news(["ABCD"], NOW.astimezone(UTC), NOW, opener=op)
    assert len(op.requests) == 1
