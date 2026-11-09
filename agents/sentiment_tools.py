"""The Sentiment Analyst's one tool: get_news. Numbers and flags come from tools.news."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agents.session_base import SingleSymbolSession, symbol_tool
from pipeline.events import calendar_input, fmp_calendar_input
from pipeline.market import NY
from tools.config import MARKET_CLOSE
from tools.event_gate import CALENDAR_SOURCES, earnings_status, fmp_calendar_events
from tools.news import digest, parse_8k_filings

LOOKBACK_HOURS = 24

TOOLS: list[dict[str, Any]] = [
    symbol_tool(
        "get_news",
        "Fetch the last 24 hours of news headlines and SEC 8-K filings for the symbol and "
        "return a digest: headlines (with flags), filings, catalysts, red_flags, counts and "
        "earnings_calendar (the earnings calendars' verdict for the symbol today; the "
        "catalyst earnings_scheduled when one lists a report). Call this first.",
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
        subs_input: dict
        try:  # an SEC failure must not hide the news
            subs = self.data.submissions(self.symbol)
            subs_input = {"ok": True, "submissions": subs}
            if subs is not None:
                filings = parse_8k_filings(subs, start)
        except Exception as exc:  # noqa: BLE001 - type name only
            filings, note = [], f"filings unavailable ({type(exc).__name__})"
            subs_input = {"ok": False, "reason": f"filings unavailable ({type(exc).__name__})"}
        result = digest(self.symbol, news, filings, end, lookback_hours=LOOKBACK_HOURS)
        if note is not None:
            result["note"] = note
        extra = self._earnings_calendar(end, subs_input)
        if extra.pop("earnings_scheduled", False):
            result["catalysts"] = sorted({*result["catalysts"], "earnings_scheduled"})
        result.update(extra)
        return result, False

    def _earnings_calendar(self, now: datetime, subs_input: dict) -> dict:
        """#13: the earnings calendars' verdict for this symbol as a digest field, and the
        catalyst "earnings_scheduled" when Alpha Vantage (primary) or FMP (secondary) lists a
        report today, within the window, or on an estimated date within 3 trading days.
        Context for the analyst only: the order path runs its own gate (tools.event_gate)."""
        if not (hasattr(self.data, "av_earnings_calendar")
                or hasattr(self.data, "earnings_calendar")):
            return {"earnings_calendar": {"status": "UNKNOWN",
                                          "reason": "no earnings calendar source"}}
        try:
            today = now.astimezone(NY).date()
            window_end = datetime.combine(today, MARKET_CLOSE, tzinfo=NY)
            status = earnings_status(self.symbol, now, window_end,
                                     calendar=calendar_input(self.data, today),
                                     fmp_calendar=fmp_calendar_input(self.data, today),
                                     filings=subs_input,
                                     secondary=(("fmp", fmp_calendar_events),))
        except Exception as exc:  # noqa: BLE001 - context must never break get_news
            return {"earnings_calendar": {"status": "UNKNOWN",
                                          "reason": f"calendar failed ({type(exc).__name__})"}}
        reasons = "; ".join(f"{f['status']} ({f['source']}): {f['reason']}"
                            for f in status["findings"]) or "no report listed in the window"
        out: dict = {"earnings_calendar": {"status": status["status"], "reason": reasons}}
        if any(f["source"] in CALENDAR_SOURCES and f["status"] == "EVENT"
               for f in status["findings"]):
            out["earnings_scheduled"] = True
        return out
