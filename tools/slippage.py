"""Slippage allowance for market entries (issue #68).

Orders are market orders, so they fill near the ask when sent, not at the plan's entry.
Sizing therefore adds a per-share allowance to the risk, and an order is refused when the live
ask is already above entry + allowance (the price moved further than the allowance covers):

    allowance = ceil_cent(max(ask - bid, limits.slippage_pct% x entry, 0.01))

The quote must be fresh (at most limits.max_order_quote_age_s old, at most 5 s in the future),
positive and not crossed. Pure function: the caller fetches the quote (injected, like the
clock) and nothing here reads model text.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from tools.config import Limits

CENT = Decimal("0.01")
FUTURE_SKEW = timedelta(seconds=5)


def minimum_allowance(entry: Decimal, limits: Limits) -> Decimal:
    """The allowance floor without a quote: ceil_cent(max(slippage_pct% x entry, 0.01)).
    risk_check BLOCKs an order whose slippage is below this."""
    return max(entry * limits.slippage_pct / 100, CENT).quantize(CENT, rounding=ROUND_CEILING)


def _price(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


def _text(d: Decimal) -> str:
    """A price as text: whole cents with two decimals ("103.10", not "103.1"), sub-penny kept."""
    cents = d.quantize(CENT)
    return str(cents) if cents == d else str(d)


def _ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    return dt if dt.tzinfo is not None and dt.utcoffset() is not None else None


def slippage_from_quote(entry: Decimal, quote: object, limits: Limits, now: datetime) -> dict:
    """Return {"ok": True, "allowance", "bid", "ask", "spread", "quote_t", "age_s"} (prices as
    strings) or {"ok": False, "reason"}.

    `quote` is an Alpaca latestQuote object: {"ap": ask, "bp": bid, "t": RFC3339}. Refused when
    it is missing or malformed, crossed (bid > ask), older than limits.max_order_quote_age_s,
    more than 5 s in the future, or when ask > entry + allowance (price moved)."""
    if now.tzinfo is None or now.utcoffset() is None:
        return {"ok": False, "reason": "now must be timezone-aware"}
    if not isinstance(quote, dict):
        return {"ok": False, "reason": "no live quote"}
    ask, bid, t = _price(quote.get("ap")), _price(quote.get("bp")), _ts(quote.get("t"))
    if ask is None or bid is None:
        return {"ok": False, "reason": "live quote has no valid bid and ask"}
    if bid > ask:
        return {"ok": False, "reason": f"live quote is crossed (bid {bid} > ask {ask})"}
    if t is None:
        return {"ok": False, "reason": "live quote has no valid timestamp"}
    age = now - t
    if age > timedelta(seconds=limits.max_order_quote_age_s):
        return {"ok": False, "reason": (
            f"live quote is {int(age.total_seconds())}s old "
            f"(max {limits.max_order_quote_age_s}s)")}
    if age < -FUTURE_SKEW:
        return {"ok": False, "reason": "live quote is dated in the future"}
    spread = ask - bid
    allowance = max(spread, entry * limits.slippage_pct / 100, CENT).quantize(
        CENT, rounding=ROUND_CEILING)
    if ask > entry + allowance:
        return {"ok": False, "reason": (
            f"price moved: ask {ask} is above entry {entry} + slippage allowance {allowance}")}
    return {
        "ok": True, "allowance": str(allowance), "bid": _text(bid), "ask": _text(ask),
        "spread": _text(spread), "quote_t": t.isoformat(), "age_s": int(age.total_seconds()),
    }
