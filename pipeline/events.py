"""Event-gate inputs for the order path (issues #13, #112).

`events_from(data)` builds the EventSource that propose() and the approval re-check call:
(symbol, now, window_end) -> tools.event_gate.event_gate(...) result. It reads three sources
from `data` and turns every failure into a {"ok": False, "reason"} input, so the gate says
UNKNOWN instead of anything raising or reading as empty:
  data.earnings_calendar(start, end) -> {"fetched_at", "from", "to", "reply"}  (FMP, primary)
  data.submissions(symbol)                                                    (SEC coverage)
  data.news([symbol], start, end)                                             (halts, signal)

`FmpCalendarCache` fetches the FMP calendar once per New York day for [today, today + 7
days] and keeps it on disk (journal/cache/, git-ignored). A failed fetch is not cached, so the
next proposal tries again (no latching for the day).
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
from tools.earnings_calendar import fetch_fmp_earnings_calendar, parse_fmp_calendar
from tools.event_gate import CALENDAR_HORIZON_DAYS, NEWS_LOOKBACK_HOURS, event_gate

CACHE_DIR = Path(__file__).resolve().parent.parent / "journal" / "cache"

EventSource = Callable[[str, datetime, datetime], dict]


def _failure(exc: BaseException) -> str:
    """A reason safe to journal: our own RuntimeError text (paths only, never URLs with
    queries), otherwise the exception type name. A server's error text echoed by
    tools.http ("error payload from <path>: <detail>") is cut to the path, and the FMP key is
    masked if it appears anywhere, so nothing a server sends back can put a secret in the
    journal or the ticket (CLAUDE.md rule 4)."""
    if not isinstance(exc, RuntimeError):
        return type(exc).__name__
    text = str(exc)
    if text.startswith("error payload from "):
        text = text.split(":", 1)[0]
    key = os.environ.get("FMP_API_KEY")
    if key:
        text = text.replace(key, "***")
    return text[:200]


class FmpCalendarCache:
    """The FMP earnings calendar, fetched at most once per New York day (on success)."""

    def __init__(self, cache_dir: Path = CACHE_DIR,
                 fetch: Callable[[date, date], list] = fetch_fmp_earnings_calendar,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.fetch = fetch
        self.clock = clock

    def _path(self, today: date) -> Path:
        return self.cache_dir / f"fmp-earnings-{today.isoformat()}.json"

    def get(self, start: date, end: date) -> dict:
        """{"fetched_at", "from", "to", "reply"} for today's cached range, fetching it if there
        is none (or it is unreadable or too short). Raises on a failed fetch."""
        path = self._path(start)
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(cached, dict) and cached.get("from") == start.isoformat()
                    and isinstance(cached.get("to"), str) and cached["to"] >= end.isoformat()
                    and isinstance(cached.get("fetched_at"), str) and "reply" in cached):
                return cached
        except (OSError, ValueError, TypeError):
            pass  # unreadable or odd: fetch again rather than latch UNKNOWN for the day
        reply = self.fetch(start, end)
        now = (self.clock or (lambda: datetime.now(NY)))()
        record = {"fetched_at": now.astimezone(NY).isoformat(), "from": start.isoformat(),
                  "to": end.isoformat(), "reply": reply}
        # Only a reply that validates is cached: a bad one would hold UNKNOWN all day.
        if parse_fmp_calendar(reply, now, start, end).get("ok") is True:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(record), encoding="utf-8")
                os.replace(tmp, path)  # atomic: a reader never sees half a file
            except OSError:
                pass  # a cache that cannot be written only costs another fetch
        return record


def calendar_input(data: Any, today: date) -> dict:
    """The gate's calendar input: parse_fmp_calendar(...) or {"ok": False, "reason"}."""
    end = today + timedelta(days=CALENDAR_HORIZON_DAYS)
    try:
        raw = data.earnings_calendar(today, end)
        fetched = datetime.fromisoformat(str(raw["fetched_at"]))
        start_d = date.fromisoformat(str(raw["from"]))
        end_d = date.fromisoformat(str(raw["to"]))
        return parse_fmp_calendar(raw["reply"], fetched, start_d, end_d)
    except Exception as exc:  # noqa: BLE001 - every failure is an UNKNOWN input
        return {"ok": False, "reason": _failure(exc)}


def events_from(data: Any) -> EventSource:
    """An EventSource reading `data` (LiveData in production, ScenarioData in the smoke run)."""
    def gate(symbol: str, now: datetime, window_end: datetime) -> dict:
        today = now.astimezone(NY).date()
        calendar = calendar_input(data, today)
        try:
            filings: dict = {"ok": True, "submissions": data.submissions(symbol)}
        except Exception as exc:  # noqa: BLE001
            filings = {"ok": False, "reason": _failure(exc)}
        try:
            start = now - timedelta(hours=NEWS_LOOKBACK_HOURS)
            news: dict = {"ok": True, "news": data.news([symbol], start, now)}
        except Exception as exc:  # noqa: BLE001
            news = {"ok": False, "reason": _failure(exc)}
        return event_gate(symbol, now, window_end, calendar=calendar, filings=filings, news=news)
    return gate
