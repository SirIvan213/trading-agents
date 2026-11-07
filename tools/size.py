"""Position sizing: the stop decides the size.

Finds the largest whole number of shares that fits every limit and says which
limit decided it. A stop too far away for the max loss is a refusal, never a
smaller-than-one-share order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from tools.config import Limits


@dataclass(frozen=True)
class Plan:
    symbol: str
    entry: Decimal
    stop: Decimal
    target: Decimal


def _floor_shares(amount: Decimal, per_share: Decimal) -> int:
    if per_share <= 0:
        return 0
    return int((amount / per_share).to_integral_value(rounding=ROUND_FLOOR))


def size_position(plan: Plan, limits: Limits, slippage: Decimal = Decimal("0")) -> dict:
    """Return {"ok", "shares", "decided_by", "risk_per_share", "slippage", "max_loss", "order"}
    or a refusal.

    `slippage` (issue #68) is the per-share allowance for a market fill above plan.entry
    (tools/slippage.py). The max-loss cap uses (entry - stop) + slippage per share, and the
    position-value and cash caps use entry + slippage, so a fill up to entry + slippage stays
    within every limit. "max_loss" is that worst case; "risk_per_share" stays the planned
    entry - stop. The order carries "slippage" so risk_check can re-check it.
    """
    symbol = plan.symbol.strip().upper() if isinstance(plan.symbol, str) else ""
    if not symbol:
        return {"ok": False, "reason": "symbol is required"}
    if plan.entry <= 0 or plan.stop <= 0:
        return {"ok": False, "reason": "entry and stop must be positive"}
    if plan.stop >= plan.entry:
        return {"ok": False, "reason": "stop must be below entry (long only)"}
    if plan.target <= plan.entry:
        return {"ok": False, "reason": "target must be above entry"}
    if (isinstance(slippage, bool) or not isinstance(slippage, Decimal)
            or not slippage.is_finite() or slippage < 0):
        return {"ok": False, "reason": "slippage must be a finite, non-negative Decimal"}

    risk_per_share = plan.entry - plan.stop
    worst_risk = risk_per_share + slippage    # loss per share at the stop after a worst fill
    worst_entry = plan.entry + slippage       # price per share after a worst fill
    usable_cash = limits.account_cash * (Decimal(100) - limits.cash_buffer_pct) / Decimal(100)

    caps = {
        "max_loss_per_trade": _floor_shares(limits.max_loss_per_trade, worst_risk),
        "max_position_value": _floor_shares(limits.max_position_value, worst_entry),
        "cash_after_buffer": _floor_shares(usable_cash, worst_entry),
    }
    decided_by = min(caps, key=caps.get)
    shares = caps[decided_by]

    if shares < 1:
        return {
            "ok": False,
            "reason": f"no whole share fits {decided_by}",
            "risk_per_share": str(risk_per_share),
        }

    max_loss = (worst_risk * shares).quantize(Decimal("0.01"))
    return {
        "ok": True,
        "shares": shares,
        "decided_by": decided_by,
        "risk_per_share": str(risk_per_share),
        "slippage": str(slippage),
        "max_loss": str(max_loss),
        "order": {
            "symbol": symbol,
            "side": "buy",
            "qty": shares,
            "type": "market",
            "order_class": "bracket",
            "entry": str(plan.entry),
            "stop_loss": str(plan.stop),
            "take_profit": str(plan.target),
            "slippage": str(slippage),
        },
    }
