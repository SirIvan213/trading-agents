"""Risk Manager: the hard gate between a proposal and the broker.

Pure function of (order, limits, today's state, clock). Returns PASS or BLOCK
with every reason. No agent can override a BLOCK.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from tools.config import MARKET_CLOSE, Limits, validate_limits
from tools.market_calendar import CalendarError, closed_reason, day_times, session_hours
from tools.slippage import minimum_allowance

NY = ZoneInfo("America/New_York")

# Exactly the keys of tools.size.size_position's "order" dict.
ORDER_FIELDS = frozenset(
    {"symbol", "side", "qty", "type", "order_class", "entry", "stop_loss", "take_profit",
     "slippage"}
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


GATE_STATUSES = ("CLEAR", "EVENT", "UNKNOWN")
GATE_SKEW = timedelta(seconds=5)


def _gate_problem(gate: object, symbol: str, limits: Limits, now: datetime) -> str | None:
    """Why the event gate result does not let this order through (#112), or None when it is a
    fresh CLEAR for this symbol. A missing result is a BLOCK, so a caller that forgets to wire
    the gate cannot open it. EVENT and UNKNOWN keep their own label and reason."""
    if not isinstance(gate, dict):
        return "event gate result missing (earnings and halts not checked)"
    status = gate.get("status")
    if status not in GATE_STATUSES:
        return "event gate result is malformed"
    if str(gate.get("symbol", "")).strip().upper() != symbol:
        return "event gate result is for another symbol"
    try:
        checked = datetime.fromisoformat(str(gate.get("checked_at")))
        if checked.tzinfo is None or checked.utcoffset() is None:
            raise ValueError
    except (ValueError, OverflowError):
        return "event gate result is malformed"
    age = now.astimezone(UTC) - checked.astimezone(UTC)
    if (age > timedelta(seconds=limits.max_plan_age_s) or age < -GATE_SKEW
            or checked.astimezone(NY).date() != now.astimezone(NY).date()):
        return "event gate result is stale"
    if status != "CLEAR":
        return f"event gate {status}: {gate.get('reason') or 'no reason given'}"
    findings = gate.get("findings", [])
    if not isinstance(findings, list) or any(
            not isinstance(f, dict) or f.get("status") != "CLEAR" for f in findings):
        # defence in depth: a CLEAR that still carries EVENT / UNKNOWN findings is not clear
        return "event gate result is inconsistent (CLEAR with open findings)"
    return None


def check_order(
    order: dict, limits: Limits, state: DayState, now: datetime, gate: object = None
) -> dict:
    """Return {"verdict": "PASS"|"BLOCK", "reasons": [...]}.

    `order` is the bracket order written by tools.size.size_position.
    `now` must be timezone-aware; it is converted to New York time. A naive or non-datetime
    `now`, or a DayState whose trades_today is not a non-negative int or whose
    risk_used_today is not a finite, non-negative Decimal, is an early BLOCK (#31).
    `limits.trading_enabled` other than True (the kill switch, #32) and `limits.paper` other
    than True each add a BLOCK reason.
    `gate` is the event-gate result (tools.event_gate.event_gate, #112). Only a fresh CLEAR for
    the order's symbol passes; EVENT, UNKNOWN, a stale or malformed result and a MISSING one
    (None, the default) each add a BLOCK reason. No setting changes that.
    Market hours come from the NYSE calendar (tools.market_calendar, #11): a full holiday or
    weekend, a time outside that day's session (13:00 close on an early close), a time outside
    the day's entry window (it ends before the 12:55 flat-by on an early close) or a year the
    calendar does not cover BLOCK.
    Minimum stop distance: entry - stop below limits.min_risk_per_share is blocked,
    because a stop a few cents from entry is a noise stop that sizing pushes to the
    position cap. No ATR is available here, so only the absolute minimum is enforced.
    Only the 9 fields in ORDER_FIELDS are accepted (symbol, side, qty, type,
    order_class, entry, stop_loss, take_profit, slippage). Any other key adds a BLOCK reason, and a
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

    # Slippage allowance (#68): required, and at least the no-quote floor
    # max(slippage_pct% of entry, 0.01), so an order sized without a live quote cannot pass.
    raw_slip = order.get("slippage")
    try:
        if raw_slip is None or isinstance(raw_slip, bool):
            raise TypeError("no slippage")
        slippage = Decimal(raw_slip)
        if not slippage.is_finite() or slippage < 0:
            raise ValueError("bad slippage")
    except (TypeError, ValueError, ArithmeticError):
        reasons.append("order needs a finite, non-negative slippage allowance")
        return {"verdict": "BLOCK", "reasons": reasons}
    try:
        floor = minimum_allowance(entry, limits)
        if slippage < floor:
            reasons.append(f"slippage allowance {slippage} below minimum {floor}")
        _numeric_rules(entry, stop, target, qty, limits, state, reasons, slippage)
    except ArithmeticError:
        reasons.append("order arithmetic failed")
        return {"verdict": "BLOCK", "reasons": reasons}

    gate_problem = _gate_problem(gate, symbol, limits, now)
    if gate_problem is not None:
        reasons.append(gate_problem)

    if state.trades_today >= limits.max_trades_per_day:
        reasons.append(
            f"already {state.trades_today} trades today (max {limits.max_trades_per_day})"
        )

    # NYSE calendar (#11): holidays, early closes (13:00, entry window ending before the
    # 12:55 flat-by) and an uncovered year all BLOCK.
    day = ny.date()
    try:
        hours = session_hours(day)
    except CalendarError:
        reasons.append("market calendar does not cover this date")
    else:
        if hours is None:
            reasons.append(f"market is closed ({closed_reason(day)})")
        elif not (hours[0] <= ny.time() < hours[1]):
            reasons.append("market is closed")
        else:
            start, end, _flat = day_times(day, limits)
            if not (start <= ny.time() <= end):
                early = " (early close)" if hours[1] != MARKET_CLOSE else ""
                reasons.append(f"outside entry window {start:%H:%M}-{end:%H:%M} ET{early}")

    return {"verdict": "BLOCK" if reasons else "PASS", "reasons": reasons}


def _numeric_rules(
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    qty: int,
    limits: Limits,
    state: DayState,
    reasons: list[str],
    slippage: Decimal = Decimal("0"),
) -> None:
    """Price/size rules, in their original order. May raise ArithmeticError.

    Loss at stop, today's risk and position value assume the worst allowed fill, entry +
    slippage (#68). Reward:risk (minimum and cap) and the minimum stop distance are judged on
    the PLANNED entry, stop and target: slippage there would fail every 1R plan."""
    if stop >= entry:
        reasons.append("stop must be below entry")
    if target <= entry:
        reasons.append("target must be above entry")

    risk_per_share = entry - stop
    if risk_per_share > 0 and qty:
        loss_at_stop = (risk_per_share + slippage) * qty
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
        worst_value = (entry + slippage) * qty
        if worst_value > limits.max_position_value:
            reasons.append(
                f"position value {worst_value:.2f} over cap {limits.max_position_value}")

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
