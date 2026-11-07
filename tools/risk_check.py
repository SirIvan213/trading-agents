"""Risk Manager: the hard gate between a proposal and the broker.

Pure function of (order, limits, today's state, clock). Returns PASS or BLOCK
with every reason. No agent can override a BLOCK.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from tools.config import MARKET_CLOSE, MARKET_OPEN, Limits, validate_limits

NY = ZoneInfo("America/New_York")

# Exactly the keys of tools.size.size_position's "order" dict.
ORDER_FIELDS = frozenset(
    {"symbol", "side", "qty", "type", "order_class", "entry", "stop_loss", "take_profit"}
)


@dataclass(frozen=True)
class DayState:
    trades_today: int = 0
    risk_used_today: Decimal = Decimal("0")  # sum of max-loss-at-stop of today's entries


def _state_problem(state: object) -> str | None:
    """Why `state` is not a usable DayState (#31), or None. trades_today must be a
    non-negative int (bool rejected); risk_used_today a finite, non-negative Decimal."""
    if not isinstance(state, DayState):
        return "day state must be a DayState"
    trades = state.trades_today
    if isinstance(trades, bool) or not isinstance(trades, int) or trades < 0:
        return "day state trades_today must be a non-negative whole number"
    risk = state.risk_used_today
    if not isinstance(risk, Decimal) or not risk.is_finite() or risk < 0:
        return "day state risk_used_today must be a finite, non-negative Decimal"
    return None


def check_order(order: dict, limits: Limits, state: DayState, now: datetime) -> dict:
    """Return {"verdict": "PASS"|"BLOCK", "reasons": [...]}.

    `order` is the bracket order written by tools.size.size_position.
    `now` must be timezone-aware; it is converted to New York time. A naive or non-datetime
    `now`, or a DayState whose trades_today is not a non-negative int or whose
    risk_used_today is not a finite, non-negative Decimal, is an early BLOCK (#31).
    `limits.trading_enabled` other than True (the kill switch, #32) and `limits.paper` other
    than True each add a BLOCK reason.
    Minimum stop distance: entry - stop below limits.min_risk_per_share is blocked,
    because a stop a few cents from entry is a noise stop that sizing pushes to the
    position cap. No ATR is available here, so only the absolute minimum is enforced.
    Only the 8 fields in ORDER_FIELDS are accepted (symbol, side, qty, type,
    order_class, entry, stop_loss, take_profit). Any other key adds a BLOCK reason, and a
    non-dict order is blocked outright. Analyst confidence and other analyst output must
    never reach the gate or influence money decisions (CLAUDE.md rule 2).
    An early BLOCK (invalid limits, non-dict order, or bad prices) returns without
    evaluating later rules.
    """
    reasons: list[str] = []

    if getattr(limits, "paper", None) is not True:
        reasons.append("live trading is disabled in this phase (paper must be true)")
    # Kill switch (#32): anything but exactly True blocks, with the paper check.
    if getattr(limits, "trading_enabled", None) is not True:
        reasons.append("trading is disabled (kill switch: trading_enabled is not true)")

    # Fail closed on the clock and the day's state (#31): block, never raise.
    bad_input = False
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        reasons.append("now must be a timezone-aware datetime")
        bad_input = True
    state_problem = _state_problem(state)
    if state_problem is not None:
        reasons.append(state_problem)
        bad_input = True
    if not isinstance(limits, Limits):
        reasons.append("limits must be a Limits")
        bad_input = True
    if bad_input:
        return {"verdict": "BLOCK", "reasons": reasons}
    try:
        ny = now.astimezone(NY)
    except (OverflowError, ValueError):  # an aware datetime at the edge of the date range
        reasons.append("now must be a timezone-aware datetime")
        return {"verdict": "BLOCK", "reasons": reasons}

    # Fail closed: hand-built limits (NaN, Infinity, floats...) must never pass an order.
    try:
        validate_limits(limits)
    except ValueError as exc:
        msg = str(exc).removeprefix("invalid limits: ")
        reasons.append(f"limits invalid: {msg}")
        return {"verdict": "BLOCK", "reasons": reasons}

    if not isinstance(order, dict):
        reasons.append("order must be an object")
        return {"verdict": "BLOCK", "reasons": reasons}
    extra = sorted(str(k) for k in order if k not in ORDER_FIELDS)
    if extra:
        reasons.append(f"order has unexpected field(s): {', '.join(extra)}")

    raw_symbol = order.get("symbol")
    symbol = raw_symbol.strip().upper() if isinstance(raw_symbol, str) else ""
    if not symbol:
        reasons.append("order has no symbol")
    if symbol in limits.blocked_symbols:
        reasons.append(f"{symbol} is on the blocked list")
    if order.get("side") != "buy":
        reasons.append("long entries only in this phase")

    qty = order.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        reasons.append("quantity must be a whole number of at least 1")
        qty = 0

    try:
        raw = (order["entry"], order["stop_loss"], order["take_profit"])
        if any(isinstance(v, bool) for v in raw):
            raise TypeError("bool price")
        entry, stop, target = (Decimal(v) for v in raw)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        reasons.append("order needs entry, stop_loss and take_profit")
        return {"verdict": "BLOCK", "reasons": reasons}
    if not (entry.is_finite() and stop.is_finite() and target.is_finite()):
        reasons.append("order prices must be finite numbers")
        return {"verdict": "BLOCK", "reasons": reasons}
    if entry <= 0 or stop <= 0:
        reasons.append("entry and stop must be positive")
        return {"verdict": "BLOCK", "reasons": reasons}

    try:
        _numeric_rules(entry, stop, target, qty, limits, state, reasons)
    except ArithmeticError:
        reasons.append("order arithmetic failed")
        return {"verdict": "BLOCK", "reasons": reasons}

    if state.trades_today >= limits.max_trades_per_day:
        reasons.append(
            f"already {state.trades_today} trades today (max {limits.max_trades_per_day})"
        )

    if ny.weekday() >= 5 or not (MARKET_OPEN <= ny.time() < MARKET_CLOSE):
        reasons.append("market is closed")
    elif not (limits.entry_start <= ny.time() <= limits.entry_end):
        reasons.append(
            f"outside entry window {limits.entry_start:%H:%M}-{limits.entry_end:%H:%M} ET"
        )

    return {"verdict": "BLOCK" if reasons else "PASS", "reasons": reasons}


def _numeric_rules(
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    qty: int,
    limits: Limits,
    state: DayState,
    reasons: list[str],
) -> None:
    """Price/size rules, in their original order. May raise ArithmeticError."""
    if stop >= entry:
        reasons.append("stop must be below entry")
    if target <= entry:
        reasons.append("target must be above entry")

    risk_per_share = entry - stop
    if risk_per_share > 0 and qty:
        loss_at_stop = risk_per_share * qty
        if loss_at_stop > limits.max_loss_per_trade:
            reasons.append(
                f"loss at stop {loss_at_stop:.2f} exceeds max loss per trade "
                f"{limits.max_loss_per_trade}"
            )
        if state.risk_used_today + loss_at_stop > limits.daily_loss_limit:
            reasons.append(
                f"today's risk would reach {state.risk_used_today + loss_at_stop:.2f}, "
                f"over the daily limit {limits.daily_loss_limit}"
            )
        reward_to_risk = (target - entry) / risk_per_share
        if reward_to_risk < limits.min_reward_to_risk:
            reasons.append(
                f"reward:risk {reward_to_risk:.2f} below minimum {limits.min_reward_to_risk}"
            )
        if entry * qty > limits.max_position_value:
            reasons.append(f"position value {entry * qty:.2f} over cap {limits.max_position_value}")

    if risk_per_share > 0:
        reward = target - entry
        if reward > limits.max_reward_multiple * risk_per_share + Decimal("0.01"):
            rr = reward / risk_per_share
            reasons.append(
                f"reward:risk {rr:.2f} over cap {limits.max_reward_multiple} "
                f"(beyond one-cent rounding tolerance)"
            )

    if 0 < risk_per_share < limits.min_risk_per_share:
        reasons.append(
            f"risk per share {risk_per_share} below minimum {limits.min_risk_per_share}"
        )
