"""Event-gate inputs for the order path (issues #13, #112).

`events_from(data)` builds the EventSource that propose() and the approval re-check call:
(symbol, now, window_end) -> tools.event_gate.event_gate(...) result. It reads five sources
from `data` and turns every failure into a {"ok": False, "reason"} input, so the gate says
UNKNOWN (or, for a secondary source, adds nothing) instead of anything raising or reading as
empty:
  data.av_earnings_calendar(start, end) -> {"fetched_at", "from", "to", "reply": CSV text}
                                                      (Alpha Vantage, primary calendar, #167)
  data.earnings_calendar(start, end) -> {"fetched_at", "from", "to", "reply": FMP list}
                                                      (FMP, block-only secondary, #145)
  data.submissions(symbol)                                                    (SEC coverage)
  data.news([symbol], start, end)                                             (halts, signal)
  data.halt_feed() -> {"fetched_at", "body"}         (Nasdaq trade-halt feed, halts, #144)
  data.listing(symbol) -> Alpaca asset reply           (listing exchange, #168)

`AvCalendarCache` and `FmpCalendarCache` fetch their calendar once per New York day and keep
it on disk (journal/cache/, git-ignored). A failed fetch or a reply that does not validate is
not cached. FMP is tried again on the next use; Alpha Vantage (25 calls a day on a free key)
only after AV_RETRY_AFTER.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.market import NY
from tools.earnings_calendar import (
    fetch_av_earnings_calendar_typed,
    fetch_fmp_earnings_calendar,
    parse_av_calendar,
    parse_fmp_calendar,
)
from tools.event_gate import CALENDAR_HORIZON_DAYS, NEWS_LOOKBACK_HOURS, event_gate
from tools.halt_feed import parse_halt_feed
from tools.listing import parse_asset

CACHE_DIR = Path(__file__).resolve().parent.parent / "journal" / "cache"
# After a failed Alpha Vantage fetch, wait this long before asking again: a free key allows
# 25 calls a day, and every proposal reads the gate. A reply that arrives but does not
# validate is likely the same all day, so it waits longer.
AV_RETRY_AFTER = timedelta(minutes=15)
AV_INVALID_RETRY_AFTER = timedelta(minutes=60)
SECRET_ENV = ("FMP_API_KEY", "ALPHA_VANTAGE_API_KEY")

EventSource = Callable[[str, datetime, datetime], dict]


def _failure(exc: BaseException) -> str:
    """A reason safe to journal: our own RuntimeError text (paths only, never URLs with
    queries), otherwise the exception type name. A server's error text echoed by
    tools.http ("error payload from <path>: <detail>") is cut to the path, and the FMP and
    Alpha Vantage keys are masked if they appear anywhere, so nothing a server sends back can
    put a secret in the journal or the ticket (CLAUDE.md rule 4)."""
    if not isinstance(exc, RuntimeError):
        return type(exc).__name__
    text = str(exc)
    if text.startswith("error payload from "):
        text = text.split(":", 1)[0]
    return _mask(text)[:200]


def _mask(text: str) -> str:
    """`text` with every data-source key (SECRET_ENV) replaced by ***."""
    for name in SECRET_ENV:
        key = (os.environ.get(name) or "").strip()
        if key:
            text = text.replace(key, "***")
    return text


class _DailyCalendarCache:
    """A whole-market earnings calendar, fetched at most once per New York day (on success)
    and kept on disk. Subclasses name the file, fetch, and validate. After a failure the same
    outcome is served from memory before the source is asked again: for `retry_after` after a
    fetch that raises, `invalid_retry_after` after a reply that does not validate (zero: ask
    on every use). With `remember`, a good record is also kept in memory for the day, so a
    cache file that cannot be written does not cost a fetch on every use."""

    prefix = ""
    retry_after = timedelta(0)
    invalid_retry_after = timedelta(0)
    remember = False

    def __init__(self, cache_dir: Path = CACHE_DIR, fetch: Callable | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.fetch = fetch
        self.clock = clock
        self._lock = threading.Lock()
        self._failed: tuple[datetime, date, dict | BaseException] | None = None
        self._good: dict | None = None
        self._extra: dict = {}  # extra record fields from the latest _fetch (under the lock)

    def _path(self, today: date) -> Path:
        return self.cache_dir / f"{self.prefix}-{today.isoformat()}.json"

    def _fetch(self, start: date, end: date) -> object:
        raise NotImplementedError

    def _valid(self, reply: object, now: datetime, start: date, end: date) -> bool:
        raise NotImplementedError

    def get(self, start: date, end: date) -> dict:
        """{"fetched_at", "from", "to", "reply"} for today's cached range, fetching it if there
        is none (or it is unreadable or too short). Raises on a failed fetch."""
        path = self._path(start)
        with self._lock:  # one fetch at a time: threads that miss together fetch once
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if (isinstance(cached, dict) and cached.get("from") == start.isoformat()
                        and isinstance(cached.get("to"), str)
                        and cached["to"] >= end.isoformat()
                        and isinstance(cached.get("fetched_at"), str) and "reply" in cached):
                    return cached
            except (OSError, ValueError, TypeError):
                pass  # unreadable or odd: fetch again rather than latch UNKNOWN for the day
            good = self._good
            if (good is not None and good.get("from") == start.isoformat()
                    and good.get("to", "") >= end.isoformat()):
                return good
            now = (self.clock or (lambda: datetime.now(NY)))()
            failed = self._failed
            wait = (self.retry_after if failed is not None
                    and isinstance(failed[2], BaseException) else self.invalid_retry_after)
            if (failed is not None and failed[1] == start
                    and failed[0] <= now < failed[0] + wait):
                outcome = failed[2]
                if isinstance(outcome, BaseException):
                    raise RuntimeError(_failure(outcome))
                return outcome
            self._extra: dict = {}
            try:
                reply = self._fetch(start, end)
            except Exception as exc:
                self._failed = (now, start, exc)
                # masked like a served failure: no caller can leak a key from the fetch error
                raise RuntimeError(_failure(exc)) from None
            record = {"fetched_at": now.astimezone(NY).isoformat(),
                      "from": start.isoformat(), "to": end.isoformat(), "reply": reply,
                      **self._extra}
            # Only a reply that validates is cached: a bad one would hold UNKNOWN all day.
            if not self._valid(reply, now, start, end):
                self._failed = (now, start, record)
                return record
            self._failed = None
            if self.remember:
                self._good = record
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(record), encoding="utf-8")
                os.replace(tmp, path)  # atomic: a reader never sees half a file
            except OSError:
                pass  # a cache that cannot be written only costs another fetch
            return record


class FmpCalendarCache(_DailyCalendarCache):
    """The FMP earnings calendar (secondary, #145), fetched at most once per New York day
    (on success). Billed per call on FMP's side."""

    prefix = "fmp-earnings"

    def __init__(self, cache_dir: Path = CACHE_DIR,
                 fetch: Callable[[date, date], list] = fetch_fmp_earnings_calendar,
                 clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(cache_dir, fetch, clock)

    def _fetch(self, start: date, end: date) -> object:
        return self.fetch(start, end)

    def _valid(self, reply: object, now: datetime, start: date, end: date) -> bool:
        return parse_fmp_calendar(reply, now, start, end).get("ok") is True


class AvCalendarCache(_DailyCalendarCache):
    """The Alpha Vantage earnings calendar (primary, #167): one 3-month CSV per New York day
    (on success), kept for [start, end] on disk and in memory. A failed fetch is served from
    memory for AV_RETRY_AFTER, an invalid reply for AV_INVALID_RETRY_AFTER, so a bad day
    cannot spend the free key's 25 daily calls in a morning."""

    prefix = "av-earnings"
    retry_after = AV_RETRY_AFTER
    invalid_retry_after = AV_INVALID_RETRY_AFTER
    remember = True

    def __init__(self, cache_dir: Path = CACHE_DIR,
                 fetch: Callable[[], object] = fetch_av_earnings_calendar_typed,
                 clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(cache_dir, fetch, clock)

    def _fetch(self, start: date, end: date) -> object:
        """The CSV text. A fetch that returns (text, content_type) also records the reply's
        Content-Type in the cache record (#172: smoke_events prints it)."""
        out = self.fetch()
        if isinstance(out, tuple) and len(out) == 2:
            text, ctype = out
            self._extra = {"content_type": ctype if isinstance(ctype, str) else None}
            return text
        return out

    def _valid(self, reply: object, now: datetime, start: date, end: date) -> bool:
        return parse_av_calendar(reply, now, start, end).get("ok") is True


def _read_calendar(data: Any, method: str, source: str, today: date,
                   parse: Callable[[object, datetime, date, date], dict]) -> dict:
    """parse(...) of data.<method>(today, today + CALENDAR_HORIZON_DAYS), or {"ok": False,
    "source", "reason"}. A failure keeps the source name, so the gate's reason says which
    calendar failed."""
    end = today + timedelta(days=CALENDAR_HORIZON_DAYS)
    fetch = getattr(data, method, None)
    if not callable(fetch):
        return {"ok": False, "source": source, "reason": f"no {method} source"}
    try:
        raw = fetch(today, end)
        fetched = datetime.fromisoformat(str(raw["fetched_at"]))
        start_d = date.fromisoformat(str(raw["from"]))
        end_d = date.fromisoformat(str(raw["to"]))
        result = parse(raw["reply"], fetched, start_d, end_d)
    except Exception as exc:  # noqa: BLE001 - every failure is an UNKNOWN input
        return {"ok": False, "source": source, "reason": _failure(exc)}
    if result.get("ok") is not True:
        # parse reasons quote a few characters of server text: mask keys like _failure does
        result = {"ok": False, "source": source, "reason": _mask(str(result.get("reason")))}
    if isinstance(raw, dict) and "content_type" in raw:
        ctype = raw["content_type"]
        result["content_type"] = _mask(ctype[:100]) if isinstance(ctype, str) else None
    return result


def calendar_input(data: Any, today: date) -> dict:
    """The gate's PRIMARY calendar input (Alpha Vantage, #167): parse_av_calendar(...) or
    {"ok": False, "source", "reason"} (no source, a fetch error, a malformed reply)."""
    return _read_calendar(data, "av_earnings_calendar", "alphavantage", today,
                          parse_av_calendar)


def fmp_calendar_input(data: Any, today: date) -> dict:
    """The gate's secondary FMP calendar input (#145): parse_fmp_calendar(...) or
    {"ok": False, "source", "reason"}. Block-only: a failure here never blocks on its own."""
    return _read_calendar(data, "earnings_calendar", "fmp", today, parse_fmp_calendar)


def halts_input(data: Any) -> dict:
    """The gate's halt-feed input: {"ok": True, "fetched_at", "feed": parse_halt_feed(...)} or
    {"ok": False, "reason"} (no source, a fetch error, a malformed feed)."""
    if not callable(getattr(data, "halt_feed", None)):
        return {"ok": False, "reason": "no halt feed source"}
    try:
        raw = data.halt_feed()
        feed = parse_halt_feed(raw["body"])
        if feed.get("ok") is not True:
            return {"ok": False, "reason": str(feed.get("reason"))}
        return {"ok": True, "fetched_at": str(raw["fetched_at"]), "feed": feed}
    except Exception as exc:  # noqa: BLE001 - every failure is an UNKNOWN input
        return {"ok": False, "reason": _failure(exc)}


def listing_input(data: Any, symbol: str) -> dict:
    """The symbol's listing (#168): tools.listing.parse_asset(data.listing(symbol)) or
    {"ok": False, "reason"} (no source, a fetch error, a malformed reply). A lookup that
    raised also carries "fetch_failed": True, so a caller looking up many symbols can stop."""
    fetch = getattr(data, "listing", None)
    if not callable(fetch):
        return {"ok": False, "reason": "no listing source", "fetch_failed": True}
    try:
        reply = fetch(symbol)
    except Exception as exc:  # noqa: BLE001 - every failure is an UNKNOWN input
        return {"ok": False, "reason": _failure(exc), "fetch_failed": True}
    return parse_asset(reply, symbol)


def gate_inputs(data: Any, symbol: str, now: datetime, *, calendar: dict | None = None,
                halts: dict | None = None, fmp_calendar: dict | None = None) -> dict:
    """The six gate inputs for `symbol` at `now`, each {"ok": True, ...} or {"ok": False,
    "reason"}: calendar (Alpha Vantage), fmp_calendar (FMP, secondary), filings (SEC), news
    (Alpaca, 24 h), halts (Nasdaq feed) and listing (Alpaca asset, #168). `calendar`,
    `fmp_calendar` and `halts` may be passed in when shared across symbols (smoke_events)."""
    today = now.astimezone(NY).date()
    try:
        filings: dict = {"ok": True, "submissions": data.submissions(symbol)}
    except Exception as exc:  # noqa: BLE001
        filings = {"ok": False, "reason": _failure(exc)}
    try:
        start = now - timedelta(hours=NEWS_LOOKBACK_HOURS)
        news: dict = {"ok": True, "news": data.news([symbol], start, now)}
    except Exception as exc:  # noqa: BLE001
        news = {"ok": False, "reason": _failure(exc)}
    return {
        "calendar": calendar if calendar is not None else calendar_input(data, today),
        "fmp_calendar": (fmp_calendar if fmp_calendar is not None
                         else fmp_calendar_input(data, today)),
        "filings": filings, "news": news,
        "halts": halts if halts is not None else halts_input(data),
        "listing": listing_input(data, symbol),
    }


def events_from(data: Any) -> EventSource:
    """An EventSource reading `data` (LiveData in production, ScenarioData in the smoke run)."""
    def gate(symbol: str, now: datetime, window_end: datetime) -> dict:
        return event_gate(symbol, now, window_end, **gate_inputs(data, symbol, now))
    return gate
