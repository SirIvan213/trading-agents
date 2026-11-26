"""Replay run_day's scan for a past session: why did each symbol pass or fail, and how close
did it come?

    python -m pipeline.scan_replay --date 2026-11-25
    python -m pipeline.scan_replay --date 2026-11-25 --times 09:40 11:00 --symbols AMD TSLA
    python -m pipeline.scan_replay --date 2026-11-25 --json

Only Ivan runs it. READ-ONLY and FREE data only (CLAUDE.md rule 6): Alpaca market data on the
IEX feed, the feed the live scan reads (daily bars, minute bars, historical trades and quotes),
and Alpaca's asset endpoint for the listing check. No Claude, FMP, Alpha Vantage or SEC call,
no broker, no order, and nothing is written (no journal, no state, no config).

For each symbol in config/run_day.json (or --symbols) and each time (default 09:40, 10:30,
12:00, 14:00, 15:20 ET), it rebuilds the snapshot the live scan would have read at that time
and runs the scan's own code on it: pipeline.market.run_scan, with run_day's criteria
(ScanCriteria() defaults) and config/limits.json's blocked symbols, so every verdict and
reason is the scan's own. The per-criterion values come from the scan's own metric function.

How a snapshot is rebuilt at time T (Alpaca's snapshot has four parts):
  - prevDailyBar: the IEX daily bar of the session before the date;
  - dailyBar: the date's IEX daily bar for its open and date stamp, with volume = the sum of
    the date's IEX minute bars that ended by T (every minute bar from 04:00 ET, so pre-market
    prints count), and close = the price at T;
  - latestTrade / latestQuote: the last IEX trade and quote at or before T (one request each,
    newest first, looking back LOOKBACK; a reply that is not newest first is an ERROR).
That volume is meant to be at least the live dailyBar volume, so that a volume rejection in
the replay is a real one. It assumes minute bars carry every share the daily bar counts; the
report checks this per symbol (the date's daily-bar volume against its minute bars' sum), and
the summary counts the symbols whose daily bar holds more.
A symbol with no IEX daily bar on the date gets the previous one as its dailyBar, as a live
snapshot would, and the scan rejects it as a stale daily bar.

Not replayed: the quarterly-reporting check (it reads SEC EDGAR, not Alpaca); run
`python -m pipeline.universe_check` for it. The listing check uses the asset as it is now,
not as it was on the date. Neither changes intraday, but within one scan run_day stops asking
SEC (or the asset endpoint) after a failed lookup and rejects every remaining symbol: a replay
that finds candidates where the live scan found none points there.

It refuses to run during any session (until 15 minutes after today's close, whatever date is
replayed), because it shares the Alpaca key's rate limit with a live run_day.

Relative volume (rvol) is shown but is not a criterion: run_day passes no average volume, so
the scan computes it as today's IEX volume so far over the previous session's full IEX volume,
and uses it only to order candidates.

Requests: per symbol 1 daily-bars, 1 minute-bars and 1 asset request, plus a trade and a
quote request per time: about 13 per symbol with the default times (about 640 for 49
symbols), paced under Alpaca's free-plan limit of 200 a minute, so a full run takes a few
minutes. Progress goes to stderr.

Exit codes: 0 done; 2 refused before any request (bad date, time, option or config, or the
session not over yet); 4 done,
but at least one symbol or time could not be fetched (its rows say ERROR).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time as _time
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pipeline.budget import CONFIG_PATH, RunDayConfigError, load_run_day_config
from pipeline.events import _failure, listing_input
from pipeline.market import run_scan
from pipeline.smoke_common import load_env
from tools.bars import BASE_URL, fetch_bars
from tools.config import load_limits
from tools.http import get_json
from tools.listing import fetch_asset
from tools.market_calendar import CalendarError, session_hours
from tools.scan import ScanCriteria, _metrics, _ts
from tools.symbols import validate_symbols

NY = ZoneInfo("America/New_York")
OK, REFUSED, INCOMPLETE = 0, 2, 4

# The live scan's feed: tools.scan.fetch_snapshots' default, which pipeline.data.LiveData uses
# (a test keeps the two equal).
FEED = "iex"
DEFAULT_TIMES = ("09:40", "10:30", "12:00", "14:00", "15:20")
LOOKBACK = timedelta(hours=1)  # how far back the last trade / quote is looked for
DAILY_LOOKBACK = timedelta(days=14)  # calendar days: finds the previous session's bar
DAY_START = time(4, 0)  # first minute bar counted toward the day's volume
DAY_END = time(20, 0)  # minute bars are fetched to here, for the volume check
HHMM = re.compile(r"\d\d:\d\d", re.ASCII)
AFTER_CLOSE = timedelta(minutes=15)  # run_day may still be finishing just after the close
MIN_INTERVAL_S = 0.35  # between requests: about 170 a minute, under the free plan's 200
RETRY_WAIT_S = 10.0  # one retry after an HTTP 429

# Criterion keys, in the order the report lists them (also the tie-break for "most often").
CRITERIA = ("gap", "volume", "spread", "trade_age", "quote_age", "price", "daily_bar",
            "listing", "blocked", "reporting", "data", "symbol", "other")
LABELS = {"gap": "gap", "volume": "volume", "spread": "spread", "trade_age": "trade age",
          "quote_age": "quote age", "price": "price", "daily_bar": "daily bar",
          "listing": "listing", "blocked": "blocked", "reporting": "reporting",
          "data": "bad data", "symbol": "symbol", "other": "other"}

CANDIDATE, REJECTED, ERROR = "candidate", "rejected", "ERROR"


def reason_key(reason: str) -> str:
    """The criterion a tools.scan rejection reason belongs to (one of CRITERIA). Matches the
    fixed start of every reason tools.scan writes; anything else is "other" (a test checks that
    every reason the scan can write has a key of its own)."""
    r = str(reason)
    prefixes = (
        ("gap ", "gap"), ("volume ", "volume"), ("spread ", "spread"),
        ("stale trade", "trade_age"), ("stale quote", "quote_age"), ("price ", "price"),
        ("stale daily bar", "daily_bar"), ("listing exchange unknown", "listing"),
        ("listed on ", "listing"), ("blocked symbol", "blocked"),
        ("quarterly reporting not proven", "reporting"), ("malformed snapshot", "data"),
        ("null or malformed snapshot", "data"), ("invalid symbol", "symbol"),
        ("duplicate symbol", "symbol"),
    )
    for prefix, key in prefixes:
        if r.startswith(prefix):
            return key
    return "other"


# ------------------------------------------------------------------------------------------
# Data
# ------------------------------------------------------------------------------------------

class ReplayData(Protocol):
    """Raw Alpaca replies the replay reads (LiveReplayData; fakes in the tests)."""

    def daily_bars(self, symbol: str, start: datetime, end: datetime) -> dict: ...
    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> dict: ...
    def last_trade(self, symbol: str, at: datetime) -> dict | None: ...
    def last_quote(self, symbol: str, at: datetime) -> dict | None: ...
    def listing(self, symbol: str) -> dict: ...


def _utc(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


class LiveReplayData:
    """Alpaca market data (IEX feed) and the asset endpoint, paced and with one retry after an
    HTTP 429. Keys from ALPACA_API_KEY / ALPACA_SECRET_KEY, never printed."""

    def __init__(self, opener: Callable | None = None, sleep: Callable[[float], None] = _time.sleep,
                 clock: Callable[[], float] = _time.monotonic,
                 min_interval_s: float = MIN_INTERVAL_S) -> None:
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval_s
        self._last: float | None = None

    def _call(self, fn: Callable[[], Any]) -> Any:
        for attempt in (1, 2):
            if self._last is not None:
                wait = self._min_interval - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait)
            self._last = self._clock()
            try:
                return fn()
            except RuntimeError as exc:
                if attempt == 1 and str(exc).startswith("HTTP 429"):
                    self._sleep(RETRY_WAIT_S)
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    def daily_bars(self, symbol: str, start: datetime, end: datetime) -> dict:
        return self._call(lambda: fetch_bars(symbol, start, end, timeframe="1Day", feed=FEED,
                                             opener=self._opener))

    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> dict:
        return self._call(lambda: fetch_bars(symbol, start, end, timeframe="1Min", feed=FEED,
                                             opener=self._opener))

    def _latest(self, kind: str, symbol: str, at: datetime) -> dict | None:
        key = (os.environ.get("ALPACA_API_KEY") or "").strip()
        secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
        if not key or not secret:
            raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the "
                               "environment")
        params = {"start": _utc(at - LOOKBACK), "end": _utc(at), "limit": "2",
                  "sort": "desc", "feed": FEED}
        url = (f"{BASE_URL}/{urllib.parse.quote(symbol.upper(), safe='.')}/{kind}?"
               f"{urllib.parse.urlencode(params)}")
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                   "Accept": "application/json"}
        body = self._call(lambda: get_json(url, headers, opener=self._opener,
                                           expect_keys=[kind]))
        if not isinstance(body, dict):
            raise RuntimeError(f"unexpected {kind} response shape")
        return latest_item(body.get(kind), kind, at)

    def last_trade(self, symbol: str, at: datetime) -> dict | None:
        return self._latest("trades", symbol, at)

    def last_quote(self, symbol: str, at: datetime) -> dict | None:
        return self._latest("quotes", symbol, at)

    def listing(self, symbol: str) -> dict:
        return self._call(lambda: fetch_asset(symbol, opener=self._opener))


def latest_item(items: object, kind: str, at: datetime) -> dict | None:
    """The newest of the (at most two) trades or quotes Alpaca returned newest first
    (sort=desc), or None for none. RuntimeError otherwise: an item that is not an object, a
    missing or unparseable "t", a "t" outside [at - LOOKBACK, at], or items not newest first
    (an ignored sort would hand back the OLDEST one in the window and fake a stale trade)."""
    if items is None:
        return None
    if not isinstance(items, list):
        raise RuntimeError(f"unexpected {kind} response shape")
    stamps = []
    for item in items:
        t = _ts(item.get("t")) if isinstance(item, dict) else None
        if t is None:
            raise RuntimeError(f"a {kind} item has no usable timestamp")
        if not at - LOOKBACK <= t <= at:
            raise RuntimeError(f"a {kind} item is outside the requested window")
        stamps.append(t)
    if any(a < b for a, b in zip(stamps, stamps[1:], strict=False)):
        raise RuntimeError(f"{kind} are not newest first")
    return items[0] if items else None


# ------------------------------------------------------------------------------------------
# Rebuilding a snapshot
# ------------------------------------------------------------------------------------------

def _ny_day(t: object) -> date | None:
    dt = _ts(t)
    return None if dt is None else dt.astimezone(NY).date()


def _bar_list(reply: object, what: str) -> list[dict]:
    items = reply.get("bars") if isinstance(reply, dict) else None
    if items is None:
        return []
    if not isinstance(items, list) or not all(isinstance(b, dict) for b in items):
        raise ValueError(f"{what} reply is malformed")
    return items


def snapshot_at(day: date, at: datetime, daily: list[dict], minutes: list[dict],
                trade: dict | None, quote: dict | None) -> dict:
    """The snapshot Alpaca would have served at `at`, in its own shape, from the date's raw
    IEX bars and the last trade and quote at or before `at` (see the module docstring).
    ValueError when no daily bar on or before `day` exists, or a bar is malformed."""
    dated = []
    for b in daily:
        d = _ny_day(b.get("t"))
        if d is None:
            raise ValueError("a daily bar has no usable timestamp")
        if d <= day:  # a bar after the date is not what the scan saw
            dated.append((d, b))
    dated.sort(key=lambda x: x[0])
    if not dated:
        raise ValueError(f"no IEX daily bar on or before {day}")
    last_day, last = dated[-1]
    prev = dated[-2][1] if len(dated) > 1 else None
    if last_day != day:  # no bar on the date: a live snapshot shows the older one (stale)
        return {"dailyBar": dict(last), "prevDailyBar": dict(prev) if prev else None,
                "latestTrade": trade, "latestQuote": quote}
    start = datetime.combine(day, DAY_START, tzinfo=NY)
    volume = 0
    close: object = None
    done = []
    for b in minutes:
        ts = _ts(b.get("t"))
        if ts is None:
            raise ValueError("a minute bar has no usable timestamp")
        if start <= ts and ts + timedelta(minutes=1) <= at:  # the minute has ended by `at`
            done.append((ts, b))
    for _, b in sorted(done, key=lambda x: x[0]):
        v = b.get("v")
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError("a minute bar has no usable volume")
        volume += v
        close = b.get("c")
    if isinstance(trade, dict) and trade.get("p") is not None:
        close = trade.get("p")
    bar = {"t": last.get("t"), "o": last.get("o"), "c": close if close is not None
           else last.get("o"), "v": volume}
    return {"dailyBar": bar, "prevDailyBar": dict(prev) if prev else None,
            "latestTrade": trade, "latestQuote": quote}


# ------------------------------------------------------------------------------------------
# Replay
# ------------------------------------------------------------------------------------------

def _pct(value: Decimal | None, of: Decimal) -> int | None:
    """value as a whole percent of `of` (rounded toward zero), None without both."""
    if value is None or not of:
        return None
    return int(value / of * 100)


def _age_s(t: object, at: datetime) -> int | None:
    dt = _ts(t)
    return None if dt is None else max(0, math.ceil((at - dt).total_seconds()))


def _why(exc: BaseException) -> str:
    """A failure for the report: our own ValueError text (it names no secret), else
    pipeline.events._failure (paths only, keys masked)."""
    return str(exc)[:200] if isinstance(exc, ValueError) else _failure(exc)


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _volume_check(day: date, daily: list[dict], minutes: list[dict]) -> dict:
    """{"daily_bar", "minute_sum"}: the date's IEX daily-bar volume (None without one) and the
    sum of its IEX minute bars from DAY_START, so a reader can see whether the replay's volume
    can fall short of the live dailyBar's."""
    bar = next((b for b in daily if _ny_day(b.get("t")) == day), None)
    start = datetime.combine(day, DAY_START, tzinfo=NY)
    total = 0
    for b in minutes:
        ts = _ts(b.get("t"))
        if ts is not None and ts >= start and ts.astimezone(NY).date() == day:
            total += _int(b.get("v")) or 0
    return {"daily_bar": _int(bar.get("v")) if bar else None, "minute_sum": total}


def _s(v: object) -> str | None:
    return None if v is None else str(v)


def _row(sym: str, hhmm: str, at: datetime, snap: dict, verdict: dict | None,
         criteria: ScanCriteria) -> dict:
    """One symbol at one time: the scan's verdict (its rejected entry, or None when the scan
    listed it as a candidate) and the value of every criterion."""
    m, _reason = _metrics(snap, None, sym)
    reasons = list(verdict["reasons"]) if verdict else []
    failed = []
    for r in reasons:
        k = reason_key(r)
        if k not in failed:
            failed.append(k)
    row: dict = {"time": hhmm, "result": REJECTED if reasons else CANDIDATE,
                 "failed": failed, "reasons": reasons, "price": None, "gap_pct": None,
                 "gap_of_min_pct": None, "volume": None, "volume_of_min_pct": None,
                 "spread_pct": None, "spread_of_max_pct": None, "trade_age_s": None,
                 "quote_age_s": None, "rvol": None}
    if m is None:
        return row
    row.update({
        "price": _s(m["price"]), "gap_pct": _s(m["gap_pct"]),
        "gap_of_min_pct": _pct(m["gap_raw"], criteria.min_gap_pct),
        "volume": m["volume"],
        "volume_of_min_pct": _pct(Decimal(m["volume"]), Decimal(criteria.min_volume)),
        "spread_pct": _s(m["spread_pct"]),
        "spread_of_max_pct": _pct(m["spread_raw"], criteria.max_spread_pct),
        "trade_age_s": _age_s(m["trade_t"], at), "quote_age_s": _age_s(m["quote_t"], at),
        "rvol": _s(m["rvol"]),
    })
    return row


def _error_row(hhmm: str, why: str) -> dict:
    return {"time": hhmm, "result": ERROR, "failed": [], "reasons": [why], "price": None,
            "gap_pct": None, "gap_of_min_pct": None, "volume": None,
            "volume_of_min_pct": None, "spread_pct": None, "spread_of_max_pct": None,
            "trade_age_s": None, "quote_age_s": None, "rvol": None}


def _most(rows: list[dict]) -> tuple[str | None, int]:
    counts = {k: 0 for k in CRITERIA}
    for r in rows:
        for k in r["failed"]:
            counts[k] += 1
    best = max(CRITERIA, key=lambda k: (counts[k], -CRITERIA.index(k)))
    return (best, counts[best]) if counts[best] else (None, 0)


def replay(data: ReplayData, day: date, times: list[str], symbols: list[str], limits: Any,
           criteria: ScanCriteria | None = None,
           progress: Callable[[str], None] | None = None,
           now: datetime | None = None) -> dict:
    """The replay report (JSON-serializable). `times` are "HH:MM" ET inside a finished
    session (main checks). The scan runs once per time over every symbol fetched without an
    error, through pipeline.market.run_scan. Minute bars are fetched to DAY_END (or `now`, if
    earlier) for the volume check; each snapshot uses only those ended by its time."""
    criteria = criteria or ScanCriteria()
    say = progress or (lambda _m: None)
    ats = {h: datetime.combine(day, time.fromisoformat(h), tzinfo=NY) for h in times}
    minutes_end = datetime.combine(day, DAY_END, tzinfo=NY)
    if now is not None:
        minutes_end = max(min(minutes_end, now), max(ats.values()))
    day_start = datetime.combine(day, time(0, 0), tzinfo=NY)
    sym_error: dict[str, str] = {}
    raw: dict[str, dict] = {}
    listings: dict[str, dict] = {}
    checks: dict[str, dict] = {}
    for i, sym in enumerate(symbols, 1):
        say(f"fetching {sym} ({i}/{len(symbols)})")
        try:
            daily = _bar_list(data.daily_bars(sym, day_start - DAILY_LOOKBACK,
                                              day_start + timedelta(days=1)), "daily bars")
            minutes = _bar_list(data.minute_bars(
                sym, datetime.combine(day, DAY_START, tzinfo=NY), minutes_end), "minute bars")
        except Exception as exc:  # noqa: BLE001 - a failed request is an ERROR row, never a pass
            sym_error[sym] = f"bars request failed: {_why(exc)}"
            continue
        listing = listing_input(data, sym)
        if listing.get("fetch_failed") and not str(listing.get("reason", "")).startswith(
                "HTTP 404"):
            sym_error[sym] = f"listing request failed: {listing.get('reason')}"
            continue
        checks[sym] = _volume_check(day, daily, minutes)
        listings[sym] = listing
        raw[sym] = {"daily": daily, "minutes": minutes, "at": {}}
        for hhmm, at in ats.items():
            try:
                trade, quote = data.last_trade(sym, at), data.last_quote(sym, at)
                raw[sym]["at"][hhmm] = snapshot_at(day, at, daily, minutes, trade, quote)
            except Exception as exc:  # noqa: BLE001
                raw[sym]["at"][hhmm] = f"data at {hhmm} failed: {_why(exc)}"

    rows: dict[str, list[dict]] = {s: [] for s in symbols}
    for hhmm, at in ats.items():
        snaps = {s: v["at"][hhmm] for s, v in raw.items() if isinstance(v["at"][hhmm], dict)}
        out = run_scan(snaps, criteria, limits, at, avg_volume=None, listings=listings,
                       reporting=None)
        verdicts = {r["symbol"]: r for r in out["rejected"]}
        passed = {c["symbol"] for c in out["candidates"]}
        for s in symbols:
            if s in sym_error:
                rows[s].append(_error_row(hhmm, sym_error[s]))
            elif not isinstance(raw[s]["at"][hhmm], dict):
                rows[s].append(_error_row(hhmm, raw[s]["at"][hhmm]))
            elif s in verdicts:
                rows[s].append(_row(s, hhmm, at, snaps[s], verdicts[s], criteria))
            elif s in passed:
                rows[s].append(_row(s, hhmm, at, snaps[s], None, criteria))
            else:  # never shown as a pass by default
                rows[s].append(_error_row(hhmm, "the scan returned no verdict for it"))

    per_symbol = []
    for s in symbols:
        most, n = _most(rows[s])
        per_symbol.append({"symbol": s, "most_often": most, "most_often_count": n,
                           "times_checked": sum(1 for r in rows[s] if r["result"] != ERROR),
                           "volume_check": checks.get(s), "rows": rows[s]})
    return {"date": day.isoformat(), "feed": FEED, "times": list(times),
            "criteria": {"min_price": str(criteria.min_price),
                         "max_price": str(criteria.max_price),
                         "min_volume": criteria.min_volume,
                         "max_spread_pct": str(criteria.max_spread_pct),
                         "min_gap_pct": str(criteria.min_gap_pct),
                         "direction": criteria.direction,
                         "max_quote_age_s": criteria.max_quote_age_s},
            "not_replayed": ["quarterly reporting (SEC EDGAR): see pipeline.universe_check"],
            "symbols": per_symbol, "summary": summarize(per_symbol, list(times))}


def summarize(per_symbol: list[dict], times: list[str]) -> dict:
    """Counts of SYMBOLS per criterion: failing it at each time, at any time, at every time
    it was checked, most often, and as the only failed criterion at some time (a near miss).
    "candidates" counts symbols passing at each time."""
    def zero() -> dict:
        return {k: 0 for k in CRITERIA}

    at_time = {h: zero() for h in times}
    any_time, every_time, most, sole = zero(), zero(), zero(), zero()
    candidates = {h: 0 for h in times}
    errors = 0
    for p in per_symbol:
        checked = [r for r in p["rows"] if r["result"] != ERROR]
        if len(checked) < len(p["rows"]):
            errors += 1
        for r in checked:
            if r["result"] == CANDIDATE:
                candidates[r["time"]] += 1
            for k in r["failed"]:
                at_time[r["time"]][k] += 1
        for k in CRITERIA:
            hits = [r for r in checked if k in r["failed"]]
            if hits:
                any_time[k] += 1
                if len(hits) == len(checked):
                    every_time[k] += 1
            if any(r["failed"] == [k] for r in checked):
                sole[k] += 1
        if p["most_often"]:
            most[p["most_often"]] += 1
    short = sorted(p["symbol"] for p in per_symbol if (vc := p.get("volume_check"))
                   and vc["daily_bar"] is not None and vc["daily_bar"] > vc["minute_sum"])
    return {"symbols": len(per_symbol), "symbols_with_errors": errors,
            "candidates": candidates, "failed_at": at_time, "failed_any_time": any_time,
            "failed_every_time": every_time, "most_often": most, "only_failure": sole,
            "daily_bar_above_minute_sum": short}


# ------------------------------------------------------------------------------------------
# Text
# ------------------------------------------------------------------------------------------

def _cell(value: object, pct: object = None) -> str:
    if value is None:
        return "-"
    return f"{value} ({pct}%)" if pct is not None else str(value)


def _vol(v: object) -> str:
    return "-" if v is None else f"{v:,}"


def _age(s: object) -> str:
    return "-" if s is None else f"{s}s"


def render(report: dict) -> str:
    c = report["criteria"]
    lines = [
        f"scan replay for {report['date']} (feed: {report['feed']}, run_day's ScanCriteria "
        "defaults), times ET: " + ", ".join(report["times"]),
        f"criteria: price {c['min_price']}-{c['max_price']}; IEX volume so far >= "
        f"{c['min_volume']:,}; spread <= {c['max_spread_pct']}% (IEX quote); gap >= "
        f"{c['min_gap_pct']}% ({c['direction']}, IEX open vs IEX previous close); last trade "
        f"and quote <= {c['max_quote_age_s']}s old; listing; blocked symbols",
        "not replayed: " + "; ".join(report["not_replayed"]),
        "(n%) is the value as a percent of its threshold: gap and volume need 100%+, "
        "spread must stay at or under 100%. rvol is not a criterion.",
        "If the replay finds candidates where the live scan found none, look at the SEC and "
        "listing lookups (one failure there rejects every remaining symbol for that scan), "
        "or at volume: the replay counts pre-market minute bars, which the live daily bar "
        "may leave out.",
    ]
    head = (f"  {'ET':<6}{'RESULT':<10}{'PRICE':>9}  {'GAP%':<14}{'IEX VOLUME':<20}"
            f"{'SPREAD%':<13}{'TRADE':>6}{'QUOTE':>6}{'RVOL':>6}  FAILED")
    for p in report["symbols"]:
        most = (f"most often rejected by {LABELS[p['most_often']]} "
                f"({p['most_often_count']} of {p['times_checked']} times)"
                if p["most_often"] else
                ("passed every time checked" if p["times_checked"] else "not checked"))
        lines += ["", f"{p['symbol']}: {most}"]
        vc = p.get("volume_check")
        if vc:
            lines.append(f"  IEX volume for the day: daily bar {_vol(vc['daily_bar'])}, "
                         f"minute bars from 04:00 ET {_vol(vc['minute_sum'])}")
        lines.append(head)
        for r in p["rows"]:
            if r["result"] == ERROR:
                lines.append(f"  {r['time']:<6}{ERROR:<10}{'':>9}  {'; '.join(r['reasons'])}")
                continue
            lines.append(
                f"  {r['time']:<6}{r['result']:<10}{_cell(r['price']):>9}  "
                f"{_cell(r['gap_pct'], r['gap_of_min_pct']):<14}"
                f"{_cell(_vol(r['volume']) if r['volume'] is not None else None, r['volume_of_min_pct']):<20}"  # noqa: E501
                f"{_cell(r['spread_pct'], r['spread_of_max_pct']):<13}"
                f"{_age(r['trade_age_s']):>6}{_age(r['quote_age_s']):>6}{_cell(r['rvol']):>6}  "
                + (", ".join(LABELS[k] for k in r["failed"]) or "-"))
            other = [x for x in r["reasons"] if reason_key(x) in ("other", "listing", "data",
                                                                  "daily_bar", "blocked")]
            for x in other:
                lines.append(f"  {'':<6}  {x}")
    s = report["summary"]
    times = report["times"]
    lines += ["", f"SUMMARY: {s['symbols']} symbols, {len(times)} times"
              + (f", {s['symbols_with_errors']} symbol(s) with errors"
                 if s["symbols_with_errors"] else ""),
              "symbols failing each criterion (a symbol can fail several):",
              f"  {'CRITERION':<11}" + "".join(f"{h:>7}" for h in times)
              + f"{'ANY TIME':>10}{'EVERY TIME':>12}{'MOST OFTEN':>12}{'ONLY ONE':>10}"]
    for k in CRITERIA:
        if not s["failed_any_time"][k]:
            continue
        lines.append(f"  {LABELS[k]:<11}" + "".join(f"{s['failed_at'][h][k]:>7}" for h in times)
                     + f"{s['failed_any_time'][k]:>10}{s['failed_every_time'][k]:>12}"
                     f"{s['most_often'][k]:>12}{s['only_failure'][k]:>10}")
    lines.append(f"  {'candidates':<11}" + "".join(f"{s['candidates'][h]:>7}" for h in times))
    lines += ["", "EVERY TIME: failed it at every time checked. MOST OFTEN: it was the "
              "symbol's most frequent failure. ONLY ONE: at some time it was the only "
              "criterion the symbol failed (a near miss)."]
    short = s["daily_bar_above_minute_sum"]
    lines.append("volume check: " + (
        f"the daily bar holds more volume than the minute bars for {len(short)} symbol(s) "
        f"({', '.join(short)}): their replayed volume may be lower than the live scan's"
        if short else "no symbol's daily bar holds more volume than its minute bars: no sign "
        "that the replayed volume is lower than the live scan's"))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------

def _times(values: Iterable[str], day: date) -> tuple[list[str], list[str]]:
    """(kept, notes): the "HH:MM" times inside the date's session, sorted; the others are
    noted. ValueError for anything but HH:MM (no seconds, no zone)."""
    hours = session_hours(day)
    assert hours is not None  # main checked the date has a session
    kept, notes = set(), []
    for v in values:
        if not HHMM.fullmatch(v):
            raise ValueError(f"time {v!r} is not HH:MM")
        try:
            t = time.fromisoformat(v)
        except ValueError:
            raise ValueError(f"time {v!r} is not HH:MM") from None
        if not hours[0] <= t <= hours[1]:
            notes.append(f"{v} skipped: outside the session ({hours[0]:%H:%M}-"
                         f"{hours[1]:%H:%M} ET)")
        else:
            kept.add(v)
    return sorted(kept), notes


def _session_busy(clock: datetime) -> str | None:
    """Why no replay may run at `clock`, or None: TODAY's session (whatever date is replayed)
    has not closed AFTER_CLOSE ago, so a scheduled run_day may still be using the key. A
    year the calendar does not cover is refused too (it cannot tell)."""
    today = clock.date()
    try:
        hours = session_hours(today)
    except CalendarError:
        return f"the market calendar does not cover today ({today})"
    if hours is None:
        return None
    until = datetime.combine(today, hours[1], tzinfo=NY) + AFTER_CLOSE
    if clock < until:
        return (f"today's session is not over (it closes {hours[1]:%H:%M} ET; replays run from "
                f"{until:%H:%M} ET)")
    return None


def main(argv: list[str] | None = None, *, data: ReplayData | None = None,
         now: datetime | None = None, config_path: Path | None = None,
         limits: Any = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline.scan_replay", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", required=True, metavar="YYYY-MM-DD",
                   help="a finished NYSE session (today's only after the close)")
    p.add_argument("--times", nargs="+", metavar="HH:MM", default=list(DEFAULT_TIMES),
                   help="ET times to replay (default: " + " ".join(DEFAULT_TIMES) + ")")
    p.add_argument("--symbols", nargs="+", metavar="SYM", default=None,
                   help="replay these instead of config/run_day.json's universe")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    env = p.add_mutually_exclusive_group()
    env.add_argument("--env-file", type=Path, default=None, metavar="PATH")
    env.add_argument("--no-dotenv", action="store_true")
    args = p.parse_args(argv)
    clock = (now or datetime.now(NY)).astimezone(NY)

    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        print(f"refused: --date {args.date!r} is not YYYY-MM-DD", file=sys.stderr)
        return REFUSED
    if day > clock.date():
        print(f"refused: {day} is in the future", file=sys.stderr)
        return REFUSED
    try:
        hours = session_hours(day)
    except CalendarError:
        print(f"refused: the market calendar does not cover {day}", file=sys.stderr)
        return REFUSED
    if hours is None:
        print(f"refused: {day} has no NYSE session", file=sys.stderr)
        return REFUSED
    busy = _session_busy(clock)
    if busy:
        print(f"refused: {busy}. The replay shares the Alpaca key's rate limit with run_day: "
              "run it outside the session", file=sys.stderr)
        return REFUSED
    try:
        times, notes = _times(args.times, day)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    for n in notes:
        print(n, file=sys.stderr)
    if not times:
        print("refused: no time left to replay", file=sys.stderr)
        return REFUSED
    try:
        if args.symbols:
            symbols = validate_symbols(args.symbols)
        else:
            symbols = list(load_run_day_config(config_path or CONFIG_PATH).universe)
    except (ValueError, RunDayConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    if limits is None:
        try:
            limits = load_limits()
        except Exception as exc:  # noqa: BLE001
            print(f"refused: limits could not be loaded ({type(exc).__name__})",
                  file=sys.stderr)
            return REFUSED
    if data is None:
        code = load_env(args)
        if code is not None:
            return code
        missing = [k for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
                   if not (os.environ.get(k) or "").strip()]
        if missing:
            print(f"refused: set {', '.join(missing)} in the environment or .env",
                  file=sys.stderr)
            return REFUSED
        data = LiveReplayData()

    report = replay(data, day, times, symbols, limits,
                    progress=lambda m: print(m, file=sys.stderr), now=clock)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return INCOMPLETE if report["summary"]["symbols_with_errors"] else OK


if __name__ == "__main__":
    sys.exit(main())
