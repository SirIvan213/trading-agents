"""The Sentiment Analyst's one tool: get_news. Numbers and flags come from tools.news."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agents.session_base import SingleSymbolSession, symbol_tool
from tools.news import digest, parse_8k_filings

LOOKBACK_HOURS = 24

TOOLS: list[dict[str, Any]] = [
    symbol_tool(
        "get_news",
        "Fetch the last 24 hours of news headlines and SEC 8-K filings for the symbol and "
        "return a digest: headlines (with flags), filings, catalysts, red_flags and counts. "
        "Call this first.",
    ),
]


class SentimentSession(SingleSymbolSession):
    """State for one Sentiment Analyst run."""

    def __init__(self, symbol: str, now: datetime, data: Any) -> None:
        super().__init__(symbol, now)
        self.data = data
        self._handlers = {"get_news": self._get_news}

    def _get_news(self, inp: dict) -> tuple[dict, bool]:
        self._check_symbol(inp)
        end = self.now
        start = end.astimezone(UTC) - timedelta(hours=LOOKBACK_HOURS)
        news = self.data.news([self.symbol], start, end)
        filings: list[dict] = []
        note: str | None = None
        try:  # an SEC failure must not hide the news
            subs = self.data.submissions(self.symbol)
            if subs is not None:
                filings = parse_8k_filings(subs, start)
        except Exception as exc:  # noqa: BLE001 - type name only
            filings, note = [], f"filings unavailable ({type(exc).__name__})"
        result = digest(self.symbol, news, filings, end, lookback_hours=LOOKBACK_HOURS)
        if note is not None:
            result["note"] = note
        return result, False
