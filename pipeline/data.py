"""Market data access behind a Protocol so agents can be tested without a network."""
from __future__ import annotations

from datetime import datetime, time
from typing import Protocol

from pipeline.market import NY, ny_today
from tools.bars import fetch_bars
from tools.scan import fetch_snapshots


class MarketData(Protocol):
    """Source of raw Alpaca responses."""

    def bars(self, symbol: str, start: datetime, end: datetime) -> dict:
        """Raw Alpaca bars response for one symbol."""
        ...

    def snapshots(self, symbols: list[str]) -> dict:
        """Raw Alpaca snapshots, {symbol: snapshot|None}."""
        ...


class AlpacaMarketData:
    """Live implementation over tools.bars.fetch_bars / tools.scan.fetch_snapshots."""

    def bars(self, symbol: str, start: datetime, end: datetime) -> dict:
        return fetch_bars(symbol, start, end)

    def snapshots(self, symbols: list[str]) -> dict:
        return fetch_snapshots(symbols)


def session_window(now: datetime) -> tuple[datetime, datetime]:
    """(04:00 ET, now) on now's New York date, both aware. ValueError if now is naive."""
    day = ny_today(now)
    return datetime.combine(day, time(4, 0), tzinfo=NY), now
