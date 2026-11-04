"""Risk Manager: the hard gate between a proposal and the broker.

Pure function of (order, limits, today's state, clock). Returns PASS or BLOCK
with every reason. No agent can override a BLOCK.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from tools.config import Limits

NY = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


@dataclass(frozen=True)
class DayState:
    trades_today: int = 0
    risk_used_today: Decimal = Decimal("0")  # sum of max-loss-at-stop of today's entries


def check_order(order: dict, limits: Limits, state: DayState, now: datetime) -> dict:
    """Return {"verdict": "PASS"|"BLOCK", "reasons": [...]}.

    `order` is the bracket order written by tools.size.size_position.
    `now` must be timezone-aware; it is converted to New York time.
    """
    reasons: list[str] = []
    ny = now.astimezone(NY)

    if not limits.paper:
        reasons.append("live trading is disabled in this phase (paper must be true)")

    symbol = str(order.get("symbol", "")).upper()
    if not symbol:
        reasons.append("order has no symbol")
    if symbol in limits.blocked_symbols:
        reasons.append(f"{symbol} is on the blocked list")
    if order.get("side") != "buy":
        reasons.append("long entries only in this phase")

    qty = order.get("qty")
    if not isinstance(qty, int) or qty < 1:
        reasons.append("quantity must be a whole number of at least 1")
        qty = 0

    try:
        entry = Decimal(order["entry"])
        stop = Decimal(order["stop_loss"])
        target = Decimal(order["take_profit"])
    except (KeyError, TypeError, ArithmeticError):
        reasons.append("order needs entry, stop_loss and take_profit")
        return {"verdict": "BLOCK", "reasons": reasons}

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
