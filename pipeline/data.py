"""Market data access behind a Protocol so agents can be tested without a network."""
from __future__ import annotations

import threading
from datetime import date, datetime, time
from typing import Protocol

from pipeline.events import AvCalendarCache, FmpCalendarCache
from pipeline.market import NY, ny_today
from tools.bars import fetch_bars
from tools.earnings import cik_for_symbol, fetch_company_facts
from tools.halt_feed import HaltFeedCache
from tools.listing import fetch_asset, parse_asset
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
    """Sources of the earnings calendars, for the event gate: Alpha Vantage (primary, #167)
    and FMP (block-only secondary, #145)."""

    def av_earnings_calendar(self, start: date, end: date) -> dict:
        """{"fetched_at": NY ISO, "from", "to", "reply": Alpha Vantage's CSV text}."""
        ...

    def earnings_calendar(self, start: date, end: date) -> dict:
        """{"fetched_at": NY ISO, "from", "to", "reply": FMP's raw JSON list}."""
        ...


# One cache per process: every LiveData shares it, so the once-a-minute rule holds.
_SHARED_HALT_CACHE = HaltFeedCache()
# Likewise for Alpha Vantage, so its retry wait after a failure holds across LiveData objects.
_SHARED_AV_CACHE = AvCalendarCache()


class HaltData(Protocol):
    """Source of the Nasdaq Trader trade-halt feed, for the event gate (#144)."""

    def halt_feed(self) -> dict:
        """{"fetched_at": NY ISO, "body": the feed's raw XML bytes}."""
        ...


class LiveData:
    """Live MarketData + NewsData + FilingsData + EarningsData over the tools/ fetchers."""

    def __init__(self, calendar_cache: FmpCalendarCache | None = None,
                 halt_cache: HaltFeedCache | None = None,
                 av_cache: AvCalendarCache | None = None) -> None:
        self._cik_map: dict[str, str] | None = None
        self._lock = threading.Lock()  # analysts run in threads; fetch the map once
        self._calendar = calendar_cache or FmpCalendarCache()
        self._av_calendar = av_cache or _SHARED_AV_CACHE
        self._assets: dict[str, dict] = {}  # listing per symbol, for this object's life
        self._halts = halt_cache or _SHARED_HALT_CACHE

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

    def listing(self, symbol: str) -> dict:
        """The Alpaca asset for `symbol` (its listing exchange, #168). A reply that parses
        ok is kept for this object's life (a listing does not change intraday); a failure or
        a reply that does not parse is not."""
        sym = symbol.strip().upper()
        with self._lock:
            cached = self._assets.get(sym)
        if cached is not None:
            return cached
        asset = fetch_asset(sym)
        if parse_asset(asset, sym).get("ok") is True:
            with self._lock:
                self._assets[sym] = asset
        return asset

    def halt_feed(self) -> dict:
        """The Nasdaq Trader trade-halt feed {"fetched_at", "body"}: fetched at most once a
        minute per process (the feed's polling rule), held in memory only (#144)."""
        return self._halts.get()

    def earnings_calendar(self, start: date, end: date) -> dict:
        """FMP calendar (secondary), fetched at most once per NY day (FmpCalendarCache).
        Billed on FMP's side: a live call needs Ivan's approval (CLAUDE.md rule 6)."""
        return self._calendar.get(start, end)

    def av_earnings_calendar(self, start: date, end: date) -> dict:
        """Alpha Vantage calendar (primary), fetched at most once per NY day
        (AvCalendarCache). The first live call needs Ivan's approval (CLAUDE.md rule 6)."""
        return self._av_calendar.get(start, end)


class AlpacaMarketData(LiveData):
    """Backwards-compatible name for LiveData."""


def session_window(now: datetime) -> tuple[datetime, datetime]:
    """(04:00 ET, now) on now's New York date, both aware. ValueError if now is naive."""
    day = ny_today(now)
    return datetime.combine(day, time(4, 0), tzinfo=NY), now
