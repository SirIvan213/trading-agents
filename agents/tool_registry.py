"""Tools the Technical Analyst may call. Executors never raise.

Numbers come from tools/ only. In particular `plan_exit` never accepts an opening
range or ATR from the model (issue #10): they are read from this session's
get_bars summary, which is built from bars fetched by the pipeline.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from agents.schemas import ExitPlanRecord, PlanLabels
from pipeline.data import MarketData, session_window
from pipeline.ledger import PlanLedger
from pipeline.market import NY, ny_today, run_scan
from tools.bars import parse_bars, regular_session, summarize
from tools.config import REWARD_MULTIPLES, Limits
from tools.exit_plan import exit_plan, opening_range_gap_error
from tools.plan_labels import plan_labels
from tools.scan import ScanCriteria, validate_symbols

MAX_SCAN_SYMBOLS = 50
PLAN_EXIT_KEYS = frozenset({"setup", "entry", "level", "reward_multiple"})
# REWARD_MULTIPLES (tools.config) is the only menu the model may pick from. exit_plan computes
# the target; values above the plan's suggested_multiple are refused here.

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_bars",
        "description": (
            "Fetch today's 1-minute bars for the symbol and return a regular-session "
            "summary: last_close, session_high/low, volume, vwap, atr (1-minute), atr_5m "
            "(5-minute bars; atr_5m_provisional true when built from fewer than 14 of them), "
            "opening_range, "
            "and price_levels (the only prices plan_exit accepts). Call this first."
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
            "Compute stop, target and reward:risk for a long entry at market. Opening range "
            "and ATR come from get_bars automatically; do not supply them. Requires get_bars "
            "first. entry must be the get_bars price_levels last_close (orders are market "
            "orders); level must be copied exactly from price_levels. The result's `labels` "
            "are tool-computed from configured thresholds: extended, tight_stop, "
            "structural_stop, suggested_multiple and atr_5m_provisional. reward_multiple "
            "(optional) picks the target multiple of risk from a fixed menu; it defaults to "
            "suggested_multiple and may not exceed it. The result also says whether the "
            "target is below the session high (target_vs_session_high) and gives, in "
            "5-minute ATRs, risk_atr_5m, entry_vs_vwap_atr_5m and "
            "session_high_vs_entry_atr_5m. "
            "Returns {ok: false, reason} if the setup is refused."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "setup": {"type": "string", "enum": ["orb", "level"]},
                "entry": {"type": "string", "description": "Entry price: copy the "
                    "last_close value from get_bars price_levels exactly"},
                "level": {
                    "type": "string",
                    "description": "Stop structure (setup 'level' only): copy exactly one "
                    "value from get_bars price_levels",
                },
                "reward_multiple": {
                    "type": "string",
                    "enum": list(REWARD_MULTIPLES),
                    "description": "Target as a multiple of risk per share; defaults to the "
                    "plan's suggested_multiple and may not exceed it",
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
    exit_plans: list[ExitPlanRecord] = field(default_factory=list)
    # The pipeline's plan ledger (issue #64): each plan is recorded the moment exit_plan
    # produces it. None outside a pipeline run (tests, the technical smoke script).
    ledger: PlanLedger | None = None

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


CENT = Decimal("0.01")


def price_levels(summary: dict) -> dict[str, str]:
    """The exact prices plan_exit may use, as strings with 2 decimals (rounded down).

    session_low, session_high, last_close, vwap, and or_low / or_high when an opening range
    exists. Values that are None are skipped.

    or_low and or_high are included even when the opening range is incomplete, and are valid
    only as `level` values: the orb setup itself requires a complete range in exit_plan.
    """
    opening = summary.get("opening_range")
    raw = {
        "session_low": summary.get("session_low"),
        "session_high": summary.get("session_high"),
        "last_close": summary.get("last_close"),
        "or_low": opening.get("low") if isinstance(opening, dict) else None,
        "or_high": opening.get("high") if isinstance(opening, dict) else None,
        "vwap": summary.get("vwap"),
    }
    return {
        name: str(Decimal(str(v)).quantize(CENT, ROUND_FLOOR))
        for name, v in raw.items() if v is not None
    }


def _level_name(levels: dict[str, str], value: Decimal) -> str | None:
    for name, text in levels.items():
        if Decimal(text) == value:
            return name
    return None


def _get_bars(s: ToolSession, inp: dict) -> tuple[dict, bool]:
    sym = inp.get("symbol")
    if not isinstance(sym, str) or sym.strip().upper() != s.symbol:
        return _err(f"symbol must be {s.symbol}")
    start, end = session_window(s.now)
    raw = s.data.bars(s.symbol, start, end)
    bars = regular_session(parse_bars(raw), day=ny_today(s.now))
    summary = summarize(bars)
    summary["price_levels"] = price_levels(summary)
    s.summary = summary
    return summary, False


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


def _summary_atr_5m(summary: dict) -> Decimal | None:
    raw = summary.get("atr_5m")
    return None if raw is None else Decimal(raw)


def _atr_context(result: dict, levels: dict[str, str], atr_5m: Decimal | None) -> dict:
    """Plan context in units of the 5-MINUTE ATR (labelled _atr_5m), to the cent (rounded
    down), so the model compares tool numbers instead of dividing prices:
      risk_atr_5m                   risk_per_share / atr_5m
      entry_vs_vwap_atr_5m          (entry - vwap) / atr_5m
      session_high_vs_entry_atr_5m  (session_high - entry) / atr_5m
    The 1-minute ATR is too small for this: a normal stop reads as 6+ ATR. It still sets
    the stop buffer and limits.min_risk_atr_multiple inside exit_plan, unchanged.
    atr_5m may be provisional early in the session (summary atr_5m_provisional, issue #117).
    Each field is None when atr_5m is missing or zero, or the level it needs is missing.
    Rounding down can understate a ratio by at most 0.01 ATR."""
    keys = ("risk_atr_5m", "entry_vs_vwap_atr_5m", "session_high_vs_entry_atr_5m")
    if atr_5m is None or atr_5m <= 0:
        return dict.fromkeys(keys)
    entry = Decimal(result["entry"])

    def per_atr(value: Decimal | None) -> str | None:
        return None if value is None else str((value / atr_5m).quantize(CENT, ROUND_FLOOR))

    vwap, high = levels.get("vwap"), levels.get("session_high")
    return {
        "risk_atr_5m": per_atr(Decimal(result["risk_per_share"])),
        "entry_vs_vwap_atr_5m": per_atr(None if vwap is None else entry - Decimal(vwap)),
        "session_high_vs_entry_atr_5m": per_atr(None if high is None else Decimal(high) - entry),
    }


def _vs_session_high(target: str, levels: dict[str, str]) -> str:
    """"below" when the target sits under today's session high (room before it matters),
    "at_or_above" otherwise, "unknown" without a session high. Compared in Decimal."""
    high = levels.get("session_high")
    if high is None:
        return "unknown"
    return "below" if Decimal(target) < Decimal(high) else "at_or_above"


def _labels(s: ToolSession, result: dict, structure_level: str | None) -> dict:
    """tools.plan_labels for a successful exit_plan result, from this session's summary."""
    summary = s.summary or {}
    vwap = (summary.get("price_levels") or {}).get("vwap")
    return plan_labels(
        entry=Decimal(result["entry"]),
        risk_per_share=Decimal(result["risk_per_share"]),
        vwap=None if vwap is None else Decimal(vwap),
        atr_5m=_summary_atr_5m(summary),
        atr_5m_provisional=summary.get("atr_5m_provisional") is True,
        structure_level=structure_level,
        limits=s.limits,
    )


def _plan_exit(s: ToolSession, inp: dict) -> tuple[dict, bool]:
    if set(inp) - PLAN_EXIT_KEYS:
        return _err("opening_range and atr come from get_bars, not from the model")
    if s.summary is None:
        return _err("call get_bars first")
    setup = inp.get("setup")
    if setup not in ("orb", "level"):
        return _err("setup must be 'orb' or 'level'")
    levels: dict[str, str] = s.summary.get("price_levels") or {}
    names = ", ".join(levels)
    entry = _decimal(inp.get("entry"), "entry")
    if _level_name(levels, entry) is None:
        return _err(f"entry must be one of get_bars price_levels: {names}")
    # Issue #118: orders are MARKET orders, so the entry is always last_close. Another price
    # level may be a stop structure (`level`), never the entry.
    last_close = levels.get("last_close")
    if last_close is None or entry != Decimal(last_close):
        return {"ok": False, "reason": (
            "entry must be last_close: orders are market orders. Use other price levels "
            "only as the `level` (stop structure) of a level setup"
        )}, False
    # Use the canonical 2-decimal string get_bars returned, not the model's spelling
    # ("102" -> "102.00"), so plan records and orders always carry cent prices.
    entry_level = "last_close"
    entry = Decimal(last_close)
    atr = _summary_atr(s.summary)
    # #115: the minimum stop distance also uses the 5-minute ATR (provisional or not); the
    # stop buffer stays on the 1-minute `atr`.
    atr_5m_kw = {"atr_5m": _summary_atr_5m(s.summary),
                 "atr_5m_provisional": s.summary.get("atr_5m_provisional") is True}
    requested: Decimal | None = None
    if inp.get("reward_multiple") is not None:
        if inp["reward_multiple"] not in REWARD_MULTIPLES:
            return _err(f"reward_multiple must be one of {', '.join(REWARD_MULTIPLES)}")
        requested = Decimal(inp["reward_multiple"])
    day = ny_today(s.now)  # NYSE calendar: flat-by on early closes, refusal on holidays
    structure_level: str | None
    if setup == "orb":
        structure_level = "or_low"

        def run(multiple: Decimal | None) -> dict:
            return exit_plan(
                "orb", entry, s.limits, opening_range=s.summary.get("opening_range"),
                atr=atr, reward_multiple=multiple, session_day=day, **atr_5m_kw,
            )
    else:
        if inp.get("level") is None:
            return _err("level setup needs a level")
        level = _decimal(inp["level"], "level")
        structure_level = _level_name(levels, level)
        if structure_level is None:
            return _err(f"level must be one of get_bars price_levels: {names}")
        level = Decimal(levels[structure_level])
        or_prices = {Decimal(levels[n]) for n in ("or_low", "or_high") if n in levels}
        if level in or_prices:
            # #14: an opening-range price from a gappy range is refused, as the orb setup is.
            # Matched by price, not name: or_low often equals session_low.
            gap = opening_range_gap_error(s.summary.get("opening_range"), s.limits)
            if gap:
                return {"ok": False, "reason": gap}, False

        def run(multiple: Decimal | None) -> dict:
            return exit_plan(
                "level", entry, s.limits, level=level, atr=atr, reward_multiple=multiple,
                session_day=day, **atr_5m_kw,
            )
    # The stop (and so the labels) does not depend on the multiple: plan once at the
    # minimum to label it, then at the chosen multiple, which may not exceed the suggestion.
    result = run(None)
    labels: dict | None = None
    if result.get("ok") is True:
        labels = _labels(s, result, structure_level)
        ceiling = Decimal(labels["suggested_multiple"])
        if requested is not None and requested > ceiling:
            return {"ok": False, "labels": labels, "reason": (
                f"reward_multiple {inp['reward_multiple']} is above this plan's "
                f"suggested_multiple {labels['suggested_multiple']}; choose it or lower"
            )}, False
        result = run(ceiling if requested is None else requested)
    if result.get("ok") is True and labels is not None:
        record = ExitPlanRecord(
            plan_id=f"plan-{len(s.exit_plans) + 1}",
            symbol=s.symbol,
            setup=setup,
            entry=result["entry"],
            stop=result["stop"],
            target=result["target"],
            risk_per_share=result["risk_per_share"],
            reward_to_risk=result["reward_to_risk"],
            time_exit=result["time_exit"],
            entry_level=entry_level,
            structure_level=structure_level,
            as_of=s.now.astimezone(NY).isoformat(),
            last_close=last_close,
            labels=PlanLabels(**labels),
        )
        if s.ledger is not None:
            s.ledger.record(record)  # LedgerError -> a tool error via ToolSession.execute
        s.exit_plans.append(record)
        result = {**result, "plan_id": record.plan_id,
                  "target_vs_session_high": _vs_session_high(result["target"], levels),
                  **_atr_context(result, levels, _summary_atr_5m(s.summary)),
                  "atr_5m": s.summary.get("atr_5m"),
                  "labels": labels}
    return result, False


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
