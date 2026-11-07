"""Market data access behind a Protocol so agents can be tested without a network."""
from __future__ import annotations

import threading
from datetime import date, datetime, time
from typing import Protocol

from pipeline.events import FmpCalendarCache
from pipeline.market import NY, ny_today
from tools.bars import fetch_bars
from tools.earnings import cik_for_symbol, fetch_company_facts
from tools.news import fetch_cik_map, fetch_news, fetch_submissions
from tools.scan import fetch_snapshots


class MarketData(Protocol):
    """Source of raw Alpaca responses."""

    def bars(self, symbol: str, start: datetime, end: datetime) -> dict:
        """Raw Alpaca bars response for one symbol."""
        ...

    def snapshots(self, symbols: list[str]) -> dict:
        """Raw Alpaca snapshots, {symbol: snapshot|None}."""
        ...


class NewsData(Protocol):
    """Source of Alpaca news."""

    def news(self, symbols: list[str], start: datetime, end: datetime) -> dict:
        """Alpaca news response, {"news": [...]}."""
        ...


class FilingsData(Protocol):
    """Source of SEC EDGAR documents, keyed by ticker."""

    def submissions(self, symbol: str) -> dict | None:
        """EDGAR submissions JSON, or None if the symbol has no CIK."""
        ...

    def company_facts(self, symbol: str) -> dict | None:
        """EDGAR companyfacts JSON, or None if the symbol has no CIK."""
        ...


class EarningsData(Protocol):
    """Source of the earnings calendar (FMP), for the event gate (#112)."""

    def earnings_calendar(self, start: date, end: date) -> dict:
        """{"fetched_at": NY ISO, "from", "to", "reply": FMP's raw JSON list}."""
        ...


class LiveData:
    """Live MarketData + NewsData + FilingsData + EarningsData over the tools/ fetchers."""

    def __init__(self, calendar_cache: FmpCalendarCache | None = None) -> None:
        self._cik_map: dict[str, str] | None = None
        self._lock = threading.Lock()  # analysts run in threads; fetch the map once
        self._calendar = calendar_cache or FmpCalendarCache()

    def _cik(self, symbol: str) -> str | None:
        with self._lock:
            if self._cik_map is None:
                self._cik_map = fetch_cik_map()
            cik_map = self._cik_map
        return cik_for_symbol(symbol, cik_map)

    def bars(self, symbol: str, start: datetime, end: datetime) -> dict:
        return fetch_bars(symbol, start, end)

    def snapshots(self, symbols: list[str]) -> dict:
        return fetch_snapshots(symbols)

    def news(self, symbols: list[str], start: datetime, end: datetime) -> dict:
        return fetch_news(symbols, start, end)

    def submissions(self, symbol: str) -> dict | None:
        cik = self._cik(symbol)
        return None if cik is None else fetch_submissions(cik)

    def company_facts(self, symbol: str) -> dict | None:
        cik = self._cik(symbol)
        return None if cik is None else fetch_company_facts(cik)

    def earnings_calendar(self, start: date, end: date) -> dict:
        """FMP calendar, fetched at most once per NY day (FmpCalendarCache). Billed on FMP's
        side: a live call needs Ivan's approval (CLAUDE.md rule 6)."""
        return self._calendar.get(start, end)


class AlpacaMarketData(LiveData):
    """Backwards-compatible name for LiveData."""


def session_window(now: datetime) -> tuple[datetime, datetime]:
    """(04:00 ET, now) on now's New York date, both aware. ValueError if now is naive."""
    day = ny_today(now)
    return datetime.combine(day, time(4, 0), tzinfo=NY), now
