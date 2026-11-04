"""Tools the Technical Analyst may call. Executors never raise.

Numbers come from tools/ only. In particular `plan_exit` never accepts an opening
range or ATR from the model (issue #10): they are read from this session's
get_bars summary, which is built from bars fetched by the pipeline.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pipeline.data import MarketData, session_window
from pipeline.market import ny_today, run_scan
from tools.bars import parse_bars, regular_session, summarize
from tools.config import Limits
from tools.exit_plan import exit_plan
from tools.scan import ScanCriteria, validate_symbols

MAX_SCAN_SYMBOLS = 50
PLAN_EXIT_KEYS = frozenset({"setup", "entry", "level"})

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_bars",
        "description": (
            "Fetch today's 1-minute bars for the symbol and return a regular-session "
            "summary: last_close, session_high/low, volume, vwap, atr, opening_range. "
            "Call this first."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker, e.g. AAPL"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "plan_exit",
        "description": (
            "Compute stop, target and reward:risk for a long entry. Opening range and ATR "
            "come from get_bars automatically; do not supply them. Requires get_bars first. "
            "Returns {ok: false, reason} if the setup is refused."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "setup": {"type": "string", "enum": ["orb", "level"]},
                "entry": {"type": "string", "description": "Entry price as a decimal string"},
                "level": {
                    "type": "string",
                    "description": "Support level as a decimal string (setup 'level' only); "
                    "must lie within today's session range",
                },
            },
            "required": ["setup", "entry"],
            "additionalProperties": False,
        },
    },
    {
        "name": "scan",
        "description": (
            "Check how this symbol (optionally with a few peers) passes the gap/volume "
            "pre-filter; returns candidates (ranked) and rejected symbols with reasons."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"At most {MAX_SCAN_SYMBOLS} symbols",
                },
            },
            "required": ["symbols"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class ToolSession:
    """State for one analyst run. `summary` is set only by get_bars."""

    symbol: str
    now: datetime
    data: MarketData
    limits: Limits
    criteria: ScanCriteria
    summary: dict | None = None

    def execute(self, name: str, tool_input: Any) -> tuple[dict, bool]:
        """Run a tool; return (result, is_error). Never raises."""
        try:
            handler = _HANDLERS.get(name)
            if handler is None:
                return {"error": f"unknown tool {name!r}"}, True
            if not isinstance(tool_input, dict):
                return {"error": "tool input must be an object"}, True
            return handler(self, tool_input)
        except _Refusal as exc:  # message built by this module, safe to show
            return {"error": str(exc)}, True
        except Exception as exc:  # noqa: BLE001 - executors must never raise
            # Exception text never reaches the model: it may contain secrets.
            return {"error": f"{name} failed ({type(exc).__name__})"}, True


def execute(session: ToolSession, name: str, tool_input: Any) -> tuple[dict, bool]:
    """Module-level alias for ToolSession.execute."""
    return session.execute(name, tool_input)


class _Refusal(ValueError):
    """A deliberate refusal whose message is constructed here, never from a library."""


def _err(msg: str) -> tuple[dict, bool]:
    return {"error": msg}, True


def _get_bars(s: ToolSession, inp: dict) -> tuple[dict, bool]:
    sym = inp.get("symbol")
    if not isinstance(sym, str) or sym.strip().upper() != s.symbol:
        return _err(f"symbol must be {s.symbol}")
    start, end = session_window(s.now)
    raw = s.data.bars(s.symbol, start, end)
    bars = regular_session(parse_bars(raw), day=ny_today(s.now))
    s.summary = summarize(bars)
    return s.summary, False


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str):
        raise _Refusal(f"{name} must be a decimal string")
    try:
        d = Decimal(value.strip())
    except ArithmeticError:
        raise _Refusal(f"{name} is not a number") from None
    if not d.is_finite():
        raise _Refusal(f"{name} must be finite")
    return d


def _summary_atr(summary: dict) -> Decimal | None:
    raw = summary.get("atr")
    return None if raw is None else Decimal(raw)


def _plan_exit(s: ToolSession, inp: dict) -> tuple[dict, bool]:
    if set(inp) - PLAN_EXIT_KEYS:
        return _err("opening_range and atr come from get_bars, not from the model")
    if s.summary is None:
        return _err("call get_bars first")
    setup = inp.get("setup")
    if setup not in ("orb", "level"):
        return _err("setup must be 'orb' or 'level'")
    entry = _decimal(inp.get("entry"), "entry")
    lo, hi = s.summary.get("session_low"), s.summary.get("session_high")
    if lo is None or hi is None or not (Decimal(lo) <= entry <= Decimal(hi)):
        return _err("entry must be within today's session range")
    atr = _summary_atr(s.summary)
    if setup == "orb":
        return exit_plan(
            "orb", entry, s.limits, opening_range=s.summary.get("opening_range"), atr=atr
        ), False
    if inp.get("level") is None:
        return _err("level setup needs a level")
    level = _decimal(inp["level"], "level")
    if not (Decimal(lo) <= level <= Decimal(hi)):
        return _err("level must be within today's session range")
    return exit_plan("level", entry, s.limits, level=level, atr=atr), False


def _scan(s: ToolSession, inp: dict) -> tuple[dict, bool]:
    symbols = inp.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        return _err("symbols must be a non-empty array of strings")
    if len(symbols) > MAX_SCAN_SYMBOLS:
        return _err(f"at most {MAX_SCAN_SYMBOLS} symbols")
    try:
        syms = validate_symbols(symbols)
    except ValueError:
        raise _Refusal("symbols must be valid tickers (letters and dots)") from None
    snaps = s.data.snapshots(syms)
    return run_scan(snaps, s.criteria, s.limits, s.now), False


_HANDLERS: dict[str, Callable[[ToolSession, dict], tuple[dict, bool]]] = {
    "get_bars": _get_bars,
    "plan_exit": _plan_exit,
    "scan": _scan,
}
