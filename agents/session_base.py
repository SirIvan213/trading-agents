"""Shared plumbing for single-tool analyst sessions. Executors never raise."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any


class Refusal(ValueError):
    """A deliberate refusal whose message is constructed in our code, never from a library."""


def symbol_tool(name: str, description: str) -> dict[str, Any]:
    """Strict tool schema for a tool whose only input is {symbol}."""
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker, e.g. AAPL"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    }


class SingleSymbolSession:
    """Base for sessions bound to one symbol and one `now` with a dict of handlers."""

    def __init__(self, symbol: str, now: datetime) -> None:
        self.symbol = symbol.strip().upper()
        self.now = now
        self._handlers: dict[str, Callable[[dict], tuple[dict, bool]]] = {}

    def _check_symbol(self, tool_input: dict) -> None:
        sym = tool_input.get("symbol")
        if not isinstance(sym, str) or sym.strip().upper() != self.symbol:
            raise Refusal(f"symbol must be {self.symbol}")

    def execute(self, name: str, tool_input: Any) -> tuple[dict, bool]:
        """Run a tool; return (result, is_error). Never raises."""
        try:
            handler = self._handlers.get(name)
            if handler is None:
                return {"error": f"unknown tool {name!r}"}, True
            if not isinstance(tool_input, dict):
                return {"error": "tool input must be an object"}, True
            return handler(tool_input)
        except Refusal as exc:
            return {"error": str(exc)}, True
        except Exception as exc:  # noqa: BLE001 - exception text may hold secrets
            return {"error": f"{name} failed ({type(exc).__name__})"}, True
