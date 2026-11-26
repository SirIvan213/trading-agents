"""pipeline/scan_replay.py: rebuilding snapshots, the per-criterion report, the CLI. Fixtures
only; the repo's network guard (tests/conftest.py) is in force, and FakeData stands in for
Alpaca's bars, trades, quotes and asset endpoints.

tests/fixtures/scan_replay/day.json is a synthetic IEX day (2026-11-25, 09:40/10:30/12:00):
  GAPR  +3% gap, tight quote; 51,000 IEX shares by 09:40 (fails volume), 151,000 by 10:30
        (a candidate); a 03:00 bar (before 04:00 ET) never counts
  FLAT  +0.5% gap: fails gap only
  THIN  +3% gap, 30,000 shares all day: fails volume; a 300 s old trade at 12:00
  WIDE  +4% gap, a 10.00 x 10.10 quote: fails spread only
  DOWN  -3% gap: wrong direction
  NOBAR no bar on the date, no trade or quote: stale daily bar, trade and quote (and its
        stale bar's 0% gap)
"""
from __future__ import annotations

import copy
import dataclasses
import inspect
import io
import json
import urllib.error
import urllib.parse
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import pipeline.run_day as run_day
import pipeline.scan_replay as sr
from pipeline.data import LiveData
from tools.config import load_limits
from tools.scan import ScanCriteria, fetch_snapshots, scan

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
FIX = json.loads((Path(__file__).parent / "fixtures" / "scan_replay" / "day.json")
                 .read_text(encoding="utf-8"))
DAY = date(2026, 11, 25)
TIMES = FIX["times"]
SYMS = ["GAPR", "FLAT", "THIN", "WIDE", "DOWN", "NOBAR"]
LIMITS = load_limits(ROOT / "config" / "limits.example.json")
AFTER_CLOSE = datetime(2026, 11, 25, 17, 0, tzinfo=NY)


class FakeData:
    """The ReplayData methods over the fixture. `fail` maps (method, symbol) to an exception;
    a (method, symbol, "HH:MM") key fails only at that time."""

    def __init__(self, fix: dict | None = None, fail: dict | None = None):
        self.fix = fix or FIX
        self.fail = fail or {}
        self.calls: list[tuple] = []

    def _check(self, method: str, symbol: str, at: datetime | None = None) -> dict:
        self.calls.append((method, symbol, at))
        for key in ((method, symbol), (method, symbol, at and f"{at:%H:%M}")):
            if key in self.fail:
                raise self.fail[key]
        return self.fix["symbols"][symbol]

    def daily_bars(self, symbol, start, end):
        return {"bars": copy.deepcopy(self._check("daily_bars", symbol)["daily"])}

    def minute_bars(self, symbol, start, end):
        return {"bars": copy.deepcopy(self._check("minute_bars", symbol)["minutes"])}

    def last_trade(self, symbol, at):
        return copy.deepcopy(self._check("last_trade", symbol, at)["trades"][f"{at:%H:%M}"])

    def last_quote(self, symbol, at):
        return copy.deepcopy(self._check("last_quote", symbol, at)["quotes"][f"{at:%H:%M}"])

    def listing(self, symbol):
        return copy.deepcopy(self._check("listing", symbol)["asset"])


def _report(data=None, symbols=SYMS):
    return sr.replay(data or FakeData(), DAY, TIMES, symbols, LIMITS)


def _rows(report, sym):
    return {r["time"]: r for p in report["symbols"] if p["symbol"] == sym for r in p["rows"]}


def _sym(report, sym):
    return next(p for p in report["symbols"] if p["symbol"] == sym)


# --- the replay reads what the live scan reads ---------------------------------------------

def test_feed_is_the_live_scans_feed():
    assert inspect.signature(fetch_snapshots).parameters["feed"].default == sr.FEED
    # LiveData passes no feed, so the default above is the one run_day reads
    assert "fetch_snapshots(symbols)" in inspect.getsource(LiveData.snapshots)


def test_criteria_are_run_days():
    src = inspect.getsource(run_day.scan_universe)
    assert "ScanCriteria()" in src  # defaults: the replay uses ScanCriteria() too
    assert "avg_volume" not in src  # no average: rvol is vs the previous day, as replayed


# --- verdicts and values -------------------------------------------------------------------

def test_gapr_fails_volume_at_0940_then_passes():
    rows = _rows(_report(), "GAPR")
    assert rows["09:40"]["result"] == sr.REJECTED
    assert rows["09:40"]["failed"] == ["volume"]
    # 10 x 5,000 + the 08:00 bar's 1,000; the 03:00 bar is before 04:00 ET and never counts
    assert rows["09:40"]["volume"] == 51_000
    assert rows["09:40"]["volume_of_min_pct"] == 51
    assert rows["10:30"]["result"] == sr.CANDIDATE and rows["10:30"]["failed"] == []
    assert rows["10:30"]["volume"] == 151_000  # the 10:29 bar ended at 10:30: it counts
    assert rows["12:00"]["volume"] == 151_100
    r = rows["10:30"]
    assert (r["gap_pct"], r["gap_of_min_pct"], r["price"]) == ("3.00", 150, "103.4")
    assert (r["trade_age_s"], r["quote_age_s"]) == (10, 5)
    assert r["rvol"] == str((Decimal(151_000) / Decimal(900_000)).quantize(Decimal("0.01")))


def test_each_symbol_fails_what_it_was_built_to_fail():
    rep = _report()
    assert {r["time"]: r["failed"] for r in _rows(rep, "FLAT").values()} == {
        t: ["gap"] for t in TIMES}
    assert _rows(rep, "FLAT")["09:40"]["gap_of_min_pct"] == 25
    assert _rows(rep, "WIDE")["10:30"]["failed"] == ["spread"]
    assert _rows(rep, "WIDE")["10:30"]["spread_of_max_pct"] == 199
    down = _rows(rep, "DOWN")["09:40"]
    assert down["failed"] == ["gap"] and "wrong direction" in down["reasons"][0]
    thin = _rows(rep, "THIN")
    assert thin["09:40"]["failed"] == ["volume"]
    assert thin["12:00"]["failed"] == ["trade_age", "volume"]
    assert thin["12:00"]["trade_age_s"] == 300
    nobar = _rows(rep, "NOBAR")["09:40"]
    assert set(nobar["failed"]) == {"daily_bar", "trade_age", "quote_age", "gap", "spread"}
    assert any("2026-11-24 is not 2026-11-25" in x for x in nobar["reasons"])


def test_most_often_per_symbol():
    rep = _report()
    got = {p["symbol"]: (p["most_often"], p["most_often_count"]) for p in rep["symbols"]}
    assert got["FLAT"] == ("gap", 3)
    assert got["THIN"] == ("volume", 3)
    assert got["WIDE"] == ("spread", 3)
    assert got["GAPR"] == ("volume", 1)
    # a four-way tie (its stale bar's gap is 0%, too): CRITERIA order decides
    assert got["NOBAR"] == ("gap", 3)


def test_summary_counts_symbols():
    s = _report()["summary"]
    assert s["symbols"] == 6 and s["symbols_with_errors"] == 0
    assert s["candidates"] == {"09:40": 0, "10:30": 1, "12:00": 1}
    assert s["failed_at"]["09:40"]["volume"] == 2  # GAPR and THIN
    assert s["failed_at"]["10:30"]["volume"] == 1
    assert s["failed_any_time"]["gap"] == 3  # FLAT, DOWN, NOBAR
    assert s["failed_any_time"]["volume"] == 2
    assert s["failed_every_time"]["volume"] == 1  # THIN only
    assert s["only_failure"]["volume"] == 2  # GAPR at 09:40, THIN at 09:40
    assert s["only_failure"]["spread"] == 1
    assert s["most_often"]["gap"] == 3 and s["most_often"]["volume"] == 2


def test_verdicts_equal_the_scans_own():
    """The replay adds no rule of its own: the same snapshots through tools.scan.scan (with
    today, now and the listings, as run_scan passes them) give the same reasons."""
    data = FakeData()
    rep = _report(data)
    at = datetime(2026, 11, 25, 10, 30, tzinfo=NY)
    snaps, listings = {}, {}
    for s in SYMS:
        f = FIX["symbols"][s]
        snaps[s] = sr.snapshot_at(DAY, at, f["daily"], f["minutes"], f["trades"]["10:30"],
                                  f["quotes"]["10:30"])
        listings[s] = {"ok": True, "symbol": s, "exchange": f["asset"]["exchange"]}
    direct = scan(snaps, ScanCriteria(), LIMITS, today=DAY, now=at, listings=listings)
    want = {r["symbol"]: r["reasons"] for r in direct["rejected"]}
    for s in SYMS:
        assert _rows(rep, s)["10:30"]["reasons"] == want.get(s, [])


# --- snapshot_at -----------------------------------------------------------------------------

def test_snapshot_needs_a_daily_bar():
    with pytest.raises(ValueError, match="no daily bar on or before"):
        sr.snapshot_at(DAY, datetime(2026, 11, 25, 10, 0, tzinfo=NY), [], [], None, None)


def test_snapshot_counts_only_minutes_ended_by_at():
    f = FIX["symbols"]["GAPR"]
    at = datetime(2026, 11, 25, 9, 35, tzinfo=NY)
    snap = sr.snapshot_at(DAY, at, f["daily"], f["minutes"], None, None)
    assert snap["dailyBar"]["v"] == 1_000 + 5 * 5_000  # 09:30-09:34; the 09:35 bar has not ended
    assert snap["dailyBar"]["c"] == 103.0  # without a trade: the last ended minute's close
    assert snap["dailyBar"]["o"] == 103.0 and snap["prevDailyBar"]["c"] == 100.0


def test_snapshot_refuses_a_bad_minute_volume():
    f = copy.deepcopy(FIX["symbols"]["GAPR"])
    f["minutes"][2]["v"] = "lots"
    with pytest.raises(ValueError, match="volume"):
        sr.snapshot_at(DAY, datetime(2026, 11, 25, 10, 0, tzinfo=NY), f["daily"], f["minutes"],
                       None, None)


# --- reason_key covers every reason the scan writes ------------------------------------------

def test_reason_key_covers_every_scan_reason():
    now = datetime(2026, 11, 25, 10, 30, tzinfo=NY)
    good = sr.snapshot_at(DAY, now, FIX["symbols"]["GAPR"]["daily"],
                          FIX["symbols"]["GAPR"]["minutes"],
                          FIX["symbols"]["GAPR"]["trades"]["10:30"],
                          FIX["symbols"]["GAPR"]["quotes"]["10:30"])

    def variant(**parts):
        s = copy.deepcopy(good)
        for k, v in parts.items():
            s[k] = v if v is None else {**(s[k] or {}), **v}
        return s

    snaps = {
        "AAA": variant(latestTrade={"p": 1.0}),  # price below
        "AAB": variant(latestTrade={"p": 900.0}),  # price above
        "AAC": variant(dailyBar={"v": 5}),  # volume
        "AAD": variant(latestQuote=None),  # spread unavailable
        "AAE": variant(latestQuote={"bp": 100.0, "ap": 110.0}),  # spread above
        "AAF": variant(dailyBar={"o": 100.5}),  # gap below
        "AAG": variant(dailyBar={"o": 90.0}),  # gap wrong direction
        "AAH": variant(dailyBar={"t": "2026-11-24T05:00:00Z"},
                       latestTrade={"t": "2026-11-25T14:00:00Z"},
                       latestQuote={"t": None}),  # stale bar, trade, quote
        "AAI": None,  # null snapshot
        "AAJ": {"dailyBar": {}},  # malformed
        "BLK": good, "OTC": good, "NOL": good, "NOR": good,
        "bad sym!": good, "aaa": good,  # invalid, duplicate
    }
    listings = {s: {"ok": True, "symbol": s, "exchange": "NASDAQ"} for s in snaps}
    listings["OTC"] = {"ok": True, "symbol": "OTC", "exchange": "OTC"}
    del listings["NOL"]
    reporting = {s.upper(): {"covered": True} for s in snaps}
    del reporting["NOR"]
    limits = dataclasses.replace(LIMITS, blocked_symbols=("BLK",))
    out = scan(snaps, ScanCriteria(), limits, today=DAY, now=now, listings=listings,
               reporting=reporting)
    reasons = [x for r in out["rejected"] for x in r["reasons"]]
    keys = {sr.reason_key(x) for x in reasons}
    assert "other" not in keys, [x for x in reasons if sr.reason_key(x) == "other"]
    assert set(sr.CRITERIA) - {"other"} == keys
    assert sr.reason_key("something new") == "other"


# --- failures --------------------------------------------------------------------------------

def test_a_failed_bars_request_is_an_error_row_not_a_pass():
    data = FakeData(fail={("minute_bars", "FLAT"): RuntimeError("HTTP 500 from /v2/x")})
    rep = _report(data)
    rows = _rows(rep, "FLAT")
    assert all(r["result"] == sr.ERROR for r in rows.values())
    assert "bars request failed: HTTP 500 from /v2/x" in rows["09:40"]["reasons"][0]
    assert rep["summary"]["symbols_with_errors"] == 1
    assert rep["summary"]["candidates"]["10:30"] == 1  # the others still ran
    assert not any(c[0] == "last_trade" and c[1] == "FLAT" for c in data.calls)


def test_a_failed_quote_at_one_time_is_an_error_at_that_time_only():
    data = FakeData(fail={("last_quote", "GAPR", "10:30"): RuntimeError("HTTP 500 from /q")})
    rows = _rows(_report(data), "GAPR")
    assert rows["10:30"]["result"] == sr.ERROR
    assert rows["12:00"]["result"] == sr.CANDIDATE


def test_listing_404_is_a_listing_rejection_but_a_listing_outage_is_an_error():
    rep = _report(FakeData(fail={("listing", "WIDE"): RuntimeError("HTTP 404 from /a")}))
    assert "listing" in _rows(rep, "WIDE")["09:40"]["failed"]
    rep = _report(FakeData(fail={("listing", "WIDE"): RuntimeError("HTTP 503 from /a")}))
    assert _rows(rep, "WIDE")["09:40"]["result"] == sr.ERROR


# --- text --------------------------------------------------------------------------------------

def test_render_lists_values_thresholds_and_summary():
    text = sr.render(_report())
    assert "IEX volume so far >= 100,000" in text and "gap >= 2.0%" in text
    assert "FLAT: most often rejected by gap (3 of 3 times)" in text
    assert "0.50 (25%)" in text  # FLAT's gap as a percent of the minimum
    assert "51,000 (51%)" in text  # GAPR's 09:40 volume
    assert "stale daily bar (2026-11-24 is not 2026-11-25)" in text
    summary = text[text.index("SUMMARY"):]
    assert "  gap" in summary and "  candidates" in summary
    assert "quarterly reporting" in text  # says what it does not replay


# --- CLI ---------------------------------------------------------------------------------------

def _main(argv, capsys, data=None, now=AFTER_CLOSE, **kw):
    code = sr.main(argv, data=data if data is not None else FakeData(), now=now,
                   limits=LIMITS, **kw)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.parametrize("argv, why", [
    (["--date", "2026-13-01"], "not YYYY-MM-DD"),
    (["--date", "2026-11-26"], "in the future"),
    (["--date", "2026-11-22"], "no NYSE session"),  # a Sunday
    (["--date", "2026-11-26", "--symbols", "AMD"], "in the future"),
    (["--date", "2026-11-25", "--times", "9:40"], "not HH:MM"),
    (["--date", "2026-11-25", "--times", "09:40:00"], "not HH:MM"),
    (["--date", "2026-11-25", "--times", "0940Z"], "not HH:MM"),  # a zone: was a TypeError
    (["--date", "2026-11-25", "--times", "T0940"], "not HH:MM"),
    (["--date", "2026-11-25", "--times", "08:00", "16:30"], "no time left"),
    (["--date", "2026-11-25", "--symbols", "bad sym!"], "invalid symbol"),
])
def test_refusals_make_no_request(argv, why, capsys):
    data = FakeData()
    code, _out, err = _main(argv, capsys, data=data,
                            now=datetime(2026, 11, 25, 17, 0, tzinfo=NY))
    assert code == sr.REFUSED and why in err
    assert data.calls == []


def test_uncovered_year_is_refused(capsys):
    code, _out, err = _main(["--date", "2019-11-25"], capsys)
    assert code == sr.REFUSED and "does not cover" in err


@pytest.mark.parametrize("date_arg, now", [
    ("2026-11-25", datetime(2026, 11, 25, 11, 0, tzinfo=NY)),
    ("2026-11-25", datetime(2026, 11, 25, 15, 59, tzinfo=NY)),
    ("2026-11-25", datetime(2026, 11, 25, 7, 0, tzinfo=NY)),
    ("2026-11-25", datetime(2026, 11, 25, 16, 14, tzinfo=NY)),  # run_day may be finishing
    ("2026-11-24", datetime(2026, 11, 25, 11, 0, tzinfo=NY)),  # a past date, during today's
    ("2026-11-25", datetime(2026, 11, 27, 12, 59, tzinfo=NY)),  # an early close's session
])
def test_refused_during_todays_session_whatever_the_date(date_arg, now, capsys):
    """It shares the Alpaca key's rate limit with a live run_day."""
    data = FakeData()
    code, _out, err = _main(["--date", date_arg, "--symbols", "GAPR"], capsys, data=data,
                            now=now)
    assert code == sr.REFUSED and "today's session is not over" in err and data.calls == []


@pytest.mark.parametrize("now", [datetime(2026, 11, 25, 16, 15, tzinfo=NY),
                                 datetime(2026, 11, 27, 13, 15, tzinfo=NY),
                                 datetime(2026, 11, 28, 10, 0, tzinfo=NY),  # a Saturday
                                 datetime(2026, 11, 26, 11, 0, tzinfo=NY)])  # Thanksgiving
def test_runs_outside_a_session(now, capsys):
    code, _out, _err = _main(["--date", "2026-11-25", "--symbols", "GAPR", "--times",
                              "10:30"], capsys, now=now)
    assert code == sr.OK


def test_refused_when_the_calendar_does_not_cover_today(capsys):
    data = FakeData()
    code, _out, err = _main(["--date", "2026-11-25", "--symbols", "GAPR"], capsys, data=data,
                            now=datetime(2031, 1, 6, 18, 0, tzinfo=NY))
    assert code == sr.REFUSED and "does not cover today" in err and data.calls == []


def test_times_after_an_early_close_are_skipped(capsys):
    hours = sr.session_hours(date(2026, 11, 27))
    assert hours is not None and hours[1].hour == 13
    kept, notes = sr._times(list(sr.DEFAULT_TIMES), date(2026, 11, 27))
    assert kept == ["09:40", "10:30", "12:00"]
    assert notes == ["14:00 skipped: outside the session (09:30-13:00 ET)",
                     "15:20 skipped: outside the session (09:30-13:00 ET)"]


def test_main_text_and_json(capsys):
    code, out, _ = _main(["--date", "2026-11-25", "--symbols", *SYMS, "--times", *TIMES],
                         capsys)
    assert code == sr.OK and "SUMMARY: 6 symbols, 3 times" in out
    code, out, _ = _main(["--date", "2026-11-25", "--symbols", "FLAT", "--times", "09:40",
                          "--json"], capsys)
    rep = json.loads(out)
    assert code == sr.OK and rep["symbols"][0]["rows"][0]["failed"] == ["gap"]


def test_main_reads_the_universe_from_run_day_config(tmp_path, capsys):
    cfg = json.loads((ROOT / "config" / "run_day.example.json").read_text(encoding="utf-8"))
    cfg["universe"] = ["FLAT", "WIDE"]
    path = tmp_path / "run_day.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    data = FakeData()
    code, out, _ = _main(["--date", "2026-11-25", "--times", "10:30"], capsys, data=data,
                         config_path=path)
    assert code == sr.OK
    assert {c[1] for c in data.calls} == {"FLAT", "WIDE"}
    code, _out, err = _main(["--date", "2026-11-25"], capsys,
                            config_path=tmp_path / "missing.json")
    assert code == sr.REFUSED and "not found" in err


def test_main_exit_4_when_a_symbol_errors(capsys):
    data = FakeData(fail={("daily_bars", "FLAT"): RuntimeError("HTTP 500 from /b")})
    code, out, _ = _main(["--date", "2026-11-25", "--symbols", "FLAT", "GAPR", "--times",
                          "10:30"], capsys, data=data)
    assert code == sr.INCOMPLETE and "ERROR" in out


def test_main_refuses_without_keys(monkeypatch, capsys):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    code = sr.main(["--date", "2026-11-25", "--symbols", "AMD", "--no-dotenv"],
                   now=AFTER_CLOSE, limits=LIMITS)
    assert code == sr.REFUSED
    assert "ALPACA_API_KEY" in capsys.readouterr().err


# --- LiveReplayData, through an injected opener (no network) -----------------------------------

class FakeResp:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *_):
        return self.body


class Opener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.urls: list[str] = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        reply = self.replies.pop(0)
        if isinstance(reply, int):
            raise urllib.error.HTTPError(req.full_url, reply, "err", {}, io.BytesIO(b""))
        return FakeResp(reply)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")


def _live(opener):
    """A LiveReplayData whose clock never moves, so every request after the first waits the
    full MIN_INTERVAL_S; `slept` records each wait."""
    slept: list[float] = []
    live = sr.LiveReplayData(opener=opener, sleep=slept.append, clock=lambda: 0.0)
    return live, slept


def test_last_trade_asks_iex_newest_first_ending_at_at(keys):
    trade = FIX["symbols"]["GAPR"]["trades"]["10:30"]
    op = Opener([{"trades": [trade], "symbol": "GAPR", "next_page_token": None}])
    live, _ = _live(op)
    at = datetime(2026, 11, 25, 10, 30, tzinfo=NY)
    assert live.last_trade("GAPR", at) == trade
    url = urllib.parse.urlsplit(op.urls[0])
    q = dict(urllib.parse.parse_qsl(url.query))
    assert url.path == "/v2/stocks/GAPR/trades"
    assert q == {"start": "2026-11-25T14:30:00Z", "end": "2026-11-25T15:30:00Z", "limit": "2",
                 "sort": "desc", "feed": "iex"}


def test_last_quote_none_when_empty_or_null(keys):
    op = Opener([{"quotes": [], "symbol": "X"}, {"quotes": None, "symbol": "X"}])
    live, _ = _live(op)
    at = datetime(2026, 11, 25, 10, 30, tzinfo=NY)
    assert live.last_quote("X", at) is None
    assert live.last_quote("X", at) is None
    assert "/quotes?" in op.urls[0]


def test_bars_use_the_iex_feed(keys):
    op = Opener([{"bars": [], "symbol": "X"}, {"bars": [], "symbol": "X"}])
    live, _ = _live(op)
    at = datetime(2026, 11, 25, 10, 30, tzinfo=NY)
    live.daily_bars("X", at, at)
    live.minute_bars("X", at, at)
    qs = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query)) for u in op.urls]
    assert [(q["timeframe"], q["feed"]) for q in qs] == [("1Day", "iex"), ("1Min", "iex")]


def test_paced_and_one_retry_after_429(keys):
    op = Opener([429, {"quotes": [], "symbol": "X"}, 429, 429])
    live, slept = _live(op)
    at = datetime(2026, 11, 25, 10, 30, tzinfo=NY)
    assert live.last_quote("X", at) is None
    with pytest.raises(RuntimeError, match="HTTP 429"):
        live.last_quote("X", at)  # a second 429 in a row is raised
    gap, retry = sr.MIN_INTERVAL_S, sr.RETRY_WAIT_S
    assert slept == [retry, gap, gap, retry, gap]


def test_live_requests_go_to_alpaca_only(keys):
    hosts: set[str] = set()

    def opener(req, timeout=None):
        hosts.add(urllib.parse.urlsplit(req.full_url).hostname)
        path = urllib.parse.urlsplit(req.full_url).path
        if path.startswith("/v2/assets/"):
            return FakeResp(FIX["symbols"]["GAPR"]["asset"])
        if path.endswith("/bars"):
            return FakeResp({"bars": [], "symbol": "GAPR"})
        return FakeResp({path.rsplit("/", 1)[1]: [], "symbol": "GAPR"})

    live, _ = _live(opener)
    sr.replay(live, DAY, ["10:30"], ["GAPR"], LIMITS)
    assert hosts and hosts <= {"data.alpaca.markets", "paper-api.alpaca.markets"}


def test_lookback_covers_the_freshness_limit():
    # a trade missing from the lookback must still be stale, never fresh
    assert sr.LOOKBACK.total_seconds() > ScanCriteria().max_quote_age_s


# --- latest_item: an ignored sort or a malformed item is an ERROR, never a stale trade -------

AT = datetime(2026, 11, 25, 10, 30, tzinfo=NY)


def _t(hh, mm, ss=0):
    return {"t": datetime(2026, 11, 25, hh, mm, ss, tzinfo=NY).isoformat(), "p": 1.0}


def test_latest_item_takes_the_newest():
    assert sr.latest_item([_t(10, 29, 50), _t(10, 29, 10)], "trades", AT)["t"].startswith(
        "2026-11-25T10:29:50")
    assert sr.latest_item([_t(10, 0)], "trades", AT) is not None
    assert sr.latest_item([], "trades", AT) is None
    assert sr.latest_item(None, "trades", AT) is None


@pytest.mark.parametrize("items, why", [
    ([_t(10, 0), _t(10, 29)], "not newest first"),
    (["x"], "no usable timestamp"),
    ([{}], "no usable timestamp"),
    ([{"t": "soon"}], "no usable timestamp"),
    ([_t(10, 31)], "outside the requested window"),
    ([_t(9, 29)], "outside the requested window"),
    ({"t": "x"}, "unexpected trades response shape"),
])
def test_latest_item_refuses(items, why):
    with pytest.raises(RuntimeError, match=why):
        sr.latest_item(items, "trades", AT)


def test_an_ignored_sort_is_an_error_row(keys):
    old, new = _t(9, 31), _t(10, 29, 50)

    def opener(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        f = FIX["symbols"]["GAPR"]
        if path.startswith("/v2/assets/"):
            return FakeResp(f["asset"])
        if path.endswith("/bars"):
            tf = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
            return FakeResp({"bars": f["daily"] if tf["timeframe"] == "1Day" else f["minutes"]})
        return FakeResp({path.rsplit("/", 1)[1]: [old, new]})  # oldest first

    live, _ = _live(opener)
    row = _rows(sr.replay(live, DAY, ["10:30"], ["GAPR"], LIMITS), "GAPR")["10:30"]
    assert row["result"] == sr.ERROR and "not newest first" in row["reasons"][0]


# --- more ERROR paths ------------------------------------------------------------------------

def test_an_unparseable_daily_bar_date_is_an_error_not_a_stale_bar():
    fix = copy.deepcopy(FIX)
    fix["symbols"]["GAPR"]["daily"][-1]["t"] = "yesterday"
    row = _rows(_report(FakeData(fix), ["GAPR"]), "GAPR")["10:30"]
    assert row["result"] == sr.ERROR
    assert "a daily bar has no usable timestamp" in row["reasons"][0]  # our text, not a type


def test_a_daily_bar_after_the_date_is_ignored():
    f = copy.deepcopy(FIX["symbols"]["GAPR"])
    f["daily"].append({**f["daily"][-1], "t": "2026-11-26T05:00:00Z", "o": 500.0})
    snap = sr.snapshot_at(DAY, AT, f["daily"], f["minutes"], None, None)
    assert snap["dailyBar"]["o"] == 103.0 and snap["prevDailyBar"]["c"] == 100.0


def test_no_verdict_from_the_scan_is_an_error_not_a_pass(monkeypatch):
    monkeypatch.setattr(sr, "run_scan", lambda *a, **k: {"candidates": [], "rejected": []})
    rows = _rows(_report(symbols=["GAPR"]), "GAPR")
    assert all(r["result"] == sr.ERROR for r in rows.values())


# --- the volume check ------------------------------------------------------------------------

def test_volume_check_flags_a_daily_bar_above_the_minute_sum():
    rep = _report()
    checks = {p["symbol"]: p["volume_check"] for p in rep["symbols"]}
    assert checks["GAPR"] == {"daily_bar": 151_100, "minute_sum": 151_100}  # 03:00 left out
    assert checks["FLAT"] == {"daily_bar": 900_000, "minute_sum": 500_000}
    assert checks["NOBAR"] == {"daily_bar": None, "minute_sum": 0}
    assert rep["summary"]["daily_bar_above_minute_sum"] == ["FLAT"]
    text = sr.render(rep)
    assert "daily bar 151,100, minute bars from 04:00 ET 151,100" in text
    assert "for 1 symbol(s) (FLAT)" in text


def test_minute_bars_are_fetched_to_the_evening_for_the_volume_check():
    seen = []

    class Spy(FakeData):
        def minute_bars(self, symbol, start, end):
            seen.append(end)
            return super().minute_bars(symbol, start, end)

    sr.replay(Spy(), DAY, ["10:30"], ["GAPR"], LIMITS)
    sr.replay(Spy(), DAY, ["10:30"], ["GAPR"], LIMITS,
              now=datetime(2026, 11, 25, 16, 5, tzinfo=NY))
    assert seen == [datetime(2026, 11, 25, 20, 0, tzinfo=NY),
                    datetime(2026, 11, 25, 16, 5, tzinfo=NY)]


# --- --compare-sip ---------------------------------------------------------------------------
# tests/fixtures/scan_replay/day_sip.json is the SIP side of day.json:
#   GAPR  40x the volume (passes at 09:40), a 1-cent quote: IEX/SIP spread 5x
#   FLAT  official open 51.25: SIP gap 2.50% vs IEX 0.50% (2 points apart); a SIP candidate
#   THIN  50x the volume and a fresh 12:00 trade: a SIP candidate every time
#   WIDE  NBBO 10.04 x 10.06: spread 0.20%, a SIP candidate (IEX/SIP 5x)
#   DOWN  the same -3% gap; NOBAR has the day's bar on SIP, but gap 0% and low volume

SIPFIX = json.loads((Path(__file__).parent / "fixtures" / "scan_replay" / "day_sip.json")
                    .read_text(encoding="utf-8"))


def _sip_report(sip=None, symbols=SYMS, data=None):
    return sr.replay(data or FakeData(), DAY, TIMES, symbols, LIMITS, now=AFTER_CLOSE,
                     sip=sip or FakeData(SIPFIX))


@pytest.mark.parametrize("now", [None, datetime(2026, 11, 25, 12, 15, tzinfo=NY)])
def test_replay_refuses_recent_sip_itself(now):
    """The free plan's 15-minute rule holds in replay(), not only in main()."""
    sip = FakeData(SIPFIX)
    with pytest.raises(ValueError, match="15 minutes"):
        sr.replay(FakeData(), DAY, TIMES, ["GAPR"], LIMITS, now=now, sip=sip)
    assert sip.calls == []


def test_replay_sip_boundary_is_16_minutes():
    with pytest.raises(ValueError, match="15 minutes"):
        sr.replay(FakeData(), DAY, ["12:00"], ["GAPR"], LIMITS,
                  now=datetime(2026, 11, 25, 12, 15, tzinfo=NY), sip=FakeData(SIPFIX))
    rep = sr.replay(FakeData(), DAY, ["12:00"], ["GAPR"], LIMITS,
                    now=datetime(2026, 11, 25, 12, 16, tzinfo=NY), sip=FakeData(SIPFIX))
    assert rep["compare_sip"] is True


def test_a_row_the_scan_could_not_evaluate_passes_nothing():
    sip = copy.deepcopy(SIPFIX)
    sip["symbols"]["GAPR"]["daily"] = [b for b in sip["symbols"]["GAPR"]["daily"]
                                       if b["t"].startswith("2026-11-25")]  # no previous bar
    rep = _sip_report(FakeData(sip), ["GAPR"])
    row = _rows(rep, "GAPR")["09:40"]["sip"]
    assert row["failed"] == ["data"]
    s = rep["sip_summary"]
    assert s["checked"]["09:40"]["sip"] == 1
    assert all(s["passing"]["09:40"][k]["sip"] == 0 for k in sr.MARKET_CRITERIA)


def test_a_sip_quote_failure_at_one_time():
    sip = FakeData(SIPFIX, fail={("last_quote", "WIDE", "10:30"): RuntimeError("HTTP 500 /q")})
    rows = _rows(_sip_report(sip, ["WIDE"]), "WIDE")
    assert rows["10:30"]["sip"]["result"] == sr.ERROR
    assert "data at 10:30 failed" in rows["10:30"]["sip"]["reasons"][0]
    assert rows["12:00"]["sip"]["result"] == sr.CANDIDATE


def test_main_exit_4_when_only_sip_errors(capsys):
    sip = FakeData(SIPFIX, fail={("daily_bars", "GAPR"): RuntimeError("HTTP 403 from /b")})
    code, _out, _err = _main(["--date", "2026-11-25", "--symbols", "GAPR", "--times", "10:30",
                              "--compare-sip"], capsys, sip_data=sip)
    assert code == sr.INCOMPLETE


def test_without_compare_sip_nothing_changes():
    rep = _report()
    assert rep["compare_sip"] is False and rep["sip_summary"] is None
    assert all("sip" not in r for p in rep["symbols"] for r in p["rows"])
    assert "IEX vs SIP" not in sr.render(rep)


def test_sip_rows_sit_beside_iex_rows():
    rep = _sip_report()
    gapr = _rows(rep, "GAPR")["09:40"]
    assert gapr["result"] == sr.REJECTED and gapr["failed"] == ["volume"]
    assert gapr["sip"]["result"] == sr.CANDIDATE and gapr["sip"]["volume"] == 2_040_000
    assert (gapr["prev_close"], gapr["open"]) == ("100.0", "103.0")
    assert (gapr["sip"]["prev_close"], gapr["sip"]["open"]) == ("100.0", "103.0")
    assert gapr["spread_ratio"] == "5.00"
    flat = _rows(rep, "FLAT")["10:30"]
    assert (flat["gap_pct"], flat["sip"]["gap_pct"]) == ("0.50", "2.50")
    assert flat["sip"]["open"] == "51.25" and flat["sip"]["result"] == sr.CANDIDATE
    wide = _rows(rep, "WIDE")["12:00"]
    assert wide["failed"] == ["spread"] and wide["sip"]["failed"] == []
    assert _rows(rep, "THIN")["12:00"]["sip"]["result"] == sr.CANDIDATE
    nobar = _rows(rep, "NOBAR")["10:30"]
    assert nobar["spread_ratio"] is None  # no IEX quote: no pair
    assert "daily_bar" not in nobar["sip"]["failed"]


def test_sip_summary():
    s = _sip_report()["sip_summary"]
    assert s["candidates"] == {"09:40": {"iex": 0, "sip": 4}, "10:30": {"iex": 1, "sip": 4},
                               "12:00": {"iex": 1, "sip": 4}}
    assert s["checked"]["09:40"] == {"iex": 6, "sip": 6}
    assert s["passing"]["09:40"]["gap"] == {"iex": 3, "sip": 4}  # FLAT passes on SIP
    assert s["passing"]["09:40"]["volume"] == {"iex": 4, "sip": 5}
    assert s["passing"]["12:00"]["trade_age"] == {"iex": 4, "sip": 6}
    assert set(s["passing"]["09:40"]) == set(sr.MARKET_CRITERIA)
    # 15 pairs: GAPR and WIDE 5x, DOWN 2x, FLAT and THIN about 1x; NOBAR has no IEX quote
    assert s["spread_ratio"] == {"pairs": 15, "not_comparable": 3, "median": "2.00",
                                 "max": "5.00", "max_at": {"symbol": "GAPR", "time": "09:40"}}
    assert s["gap_differs"] == [{"symbol": "FLAT", "iex_gap_pct": "0.50",
                                 "sip_gap_pct": "2.50", "difference": "-2.00"}]


def test_median_of_an_even_count():
    assert sr._median([Decimal("1"), Decimal("4"), Decimal("2"), Decimal("3")]) == Decimal(
        "2.50")
    assert sr._median([Decimal("3")]) == Decimal("3")


def test_a_sip_failure_is_a_sip_error_row_only():
    sip = FakeData(SIPFIX, fail={("minute_bars", "GAPR"): RuntimeError("HTTP 403 from /b")})
    rep = _sip_report(sip)
    rows = _rows(rep, "GAPR")
    assert rows["10:30"]["result"] == sr.CANDIDATE  # IEX unaffected
    assert rows["10:30"]["sip"]["result"] == sr.ERROR
    assert "SIP bars request failed: HTTP 403 from /b" in rows["10:30"]["sip"]["reasons"][0]
    assert rows["10:30"]["spread_ratio"] is None
    assert rep["sip_summary"]["checked"]["10:30"] == {"iex": 6, "sip": 5}


def test_an_iex_error_is_an_error_on_both_sides():
    data = FakeData(fail={("listing", "FLAT"): RuntimeError("HTTP 503 from /a")})
    sip = FakeData(SIPFIX)
    row = _rows(_sip_report(sip, ["FLAT"], data), "FLAT")["09:40"]
    assert row["result"] == sr.ERROR and row["sip"]["result"] == sr.ERROR
    assert sip.calls == []  # nothing is asked of SIP for a symbol that failed on IEX


def test_sip_requests_end_16_minutes_before_now():
    ends: dict[str, list] = {"daily": [], "minute": []}

    class Spy(FakeData):
        def daily_bars(self, symbol, start, end):
            ends["daily"].append(end)
            return super().daily_bars(symbol, start, end)

        def minute_bars(self, symbol, start, end):
            ends["minute"].append(end)
            return super().minute_bars(symbol, start, end)

    now = datetime(2026, 11, 25, 16, 30, tzinfo=NY)
    sr.replay(FakeData(), DAY, ["10:30"], ["GAPR"], LIMITS, now=now, sip=Spy(SIPFIX))
    assert ends == {"daily": [datetime(2026, 11, 25, 16, 14, tzinfo=NY)],
                    "minute": [datetime(2026, 11, 25, 16, 14, tzinfo=NY)]}
    ends["daily"].clear()
    ends["minute"].clear()
    later = datetime(2026, 11, 27, 9, 0, tzinfo=NY)  # a past date: nothing to cap
    sr.replay(FakeData(), DAY, ["10:30"], ["GAPR"], LIMITS, now=later, sip=Spy(SIPFIX))
    assert ends == {"daily": [datetime(2026, 11, 26, 0, 0, tzinfo=NY)],
                    "minute": [datetime(2026, 11, 25, 20, 0, tzinfo=NY)]}


def test_render_compare_sip():
    text = sr.render(_sip_report())
    assert "--compare-sip:" in text and "PREV CLOSE" in text and "IEX/SIP" in text
    assert "        SIP  candidate" in text
    assert "5.00x" in text
    assert "candidates       0/4      1/4      1/4" in text
    assert ("spread ratio IEX/SIP over 15 symbol-times: median 2.00x, max 5.00x (GAPR at "
            "09:40); 3 without a usable pair of spreads") in text
    assert "FLAT: IEX 0.50%, SIP 2.50% (IEX - SIP = -2.00)" in text
    assert "the live scan still reads IEX" in text


def test_main_compare_sip(capsys):
    code, out, _ = _main(["--date", "2026-11-25", "--symbols", *SYMS, "--times", *TIMES,
                          "--compare-sip"], capsys, sip_data=FakeData(SIPFIX))
    assert code == sr.OK and "IEX vs SIP" in out
    code, out, _ = _main(["--date", "2026-11-25", "--symbols", "FLAT", "--times", "10:30",
                          "--compare-sip", "--json"], capsys, sip_data=FakeData(SIPFIX))
    assert json.loads(out)["sip_summary"]["gap_differs"][0]["symbol"] == "FLAT"


def test_main_compare_sip_refuses_a_time_less_than_16_minutes_old(capsys):
    data, sip = FakeData(), FakeData(SIPFIX)
    code, _out, err = _main(["--date", "2026-11-25", "--symbols", "GAPR", "--times", "16:00",
                             "--compare-sip"], capsys, data=data, sip_data=sip,
                            now=datetime(2026, 11, 25, 16, 15, tzinfo=NY))
    assert code == sr.REFUSED and "run after 16:16 ET" in err
    assert data.calls == [] and sip.calls == []
    code, _out, _err = _main(["--date", "2026-11-25", "--symbols", "GAPR", "--times", "16:00",
                              "--compare-sip"], capsys, sip_data=FakeData(SIPFIX),
                             now=datetime(2026, 11, 25, 16, 16, tzinfo=NY))
    assert code == sr.INCOMPLETE  # it ran: the fixture just has no prints for 16:00


def test_main_compare_sip_needs_a_sip_source(capsys):
    code, _out, err = _main(["--date", "2026-11-25", "--symbols", "GAPR", "--compare-sip"],
                            capsys)
    assert code == sr.REFUSED and "no SIP feed" in err


def test_with_feed_asks_sip_and_shares_the_pacing(keys):
    op = Opener([{"bars": [], "symbol": "X"}, {"bars": [], "symbol": "X"},
                 {"quotes": [], "symbol": "X"}])
    iex, slept = _live(op)
    sip = iex.with_feed("sip")
    at = datetime(2026, 11, 25, 10, 30, tzinfo=NY)
    iex.daily_bars("X", at, at)
    sip.daily_bars("X", at, at)
    sip.last_quote("X", at)
    feeds = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))["feed"]
             for u in op.urls]
    assert feeds == ["iex", "sip", "sip"] and iex.feed == "iex" and sip.feed == "sip"
    assert slept == [sr.MIN_INTERVAL_S, sr.MIN_INTERVAL_S]  # one pace across both feeds
    with pytest.raises(ValueError, match="feed must be"):
        sr.LiveReplayData(feed="otc")
