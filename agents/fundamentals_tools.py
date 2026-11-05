"""The Fundamentals Analyst's one tool: get_fundamentals. All numbers come from tools.earnings."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from agents.session_base import Refusal, SingleSymbolSession, symbol_tool
from pipeline.market import ny_today
from tools.earnings import earnings_facts

TOOLS: list[dict[str, Any]] = [
    symbol_tool(
        "get_fundamentals",
        "Return point-in-time quarterly facts from SEC filings for the symbol: recent quarters "
        "(revenue, net income, EPS), latest balance-sheet items, growth and the latest filing "
        "date. Returns {ok: false, reason} if there is not enough data. Call this first.",
    ),
]


class FundamentalsSession(SingleSymbolSession):
    """State for one Fundamentals Analyst run."""

    def __init__(self, symbol: str, now: datetime, data: Any) -> None:
        super().__init__(symbol, now)
        self.data = data
        self._handlers = {"get_fundamentals": self._get_fundamentals}

    def _get_fundamentals(self, inp: dict) -> tuple[dict, bool]:
        self._check_symbol(inp)
        facts = self.data.company_facts(self.symbol)
        if facts is None:
            raise Refusal("no SEC filer found for symbol")
        return earnings_facts(facts, as_of=ny_today(self.now)), False
