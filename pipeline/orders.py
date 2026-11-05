"""The one order path: from a stored exit plan to a sized order.

Order prices come only from tool-produced ExitPlanRecords. Analyst text and analyst scores
never feed sizing or risk_check (CLAUDE.md rule 2; issues #40, #50). Nothing but the plan and
the risk limits is an input here.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from agents.schemas import DecisionPacket, ExitPlanRecord
from tools.config import Limits
from tools.scan import validate_symbols
from tools.size import Plan, size_position

_CENT = Decimal("0.01")


def select_plan(packet: DecisionPacket, plan_id: str) -> ExitPlanRecord:
    """Return the packet's plan with this id.

    Raises KeyError("unknown plan_id") if absent, and
    KeyError("plan symbol does not match packet") if its symbol is not the packet's.
    """
    for plan in packet.exit_plans:
        if plan.plan_id == plan_id:
            if plan.symbol != packet.symbol:
                raise KeyError("plan symbol does not match packet")
            return plan
    raise KeyError("unknown plan_id")


def _cents(text: object) -> Decimal | None:
    """A finite, positive, whole-cent Decimal, or None."""
    if not isinstance(text, str):
        return None
    try:
        d = Decimal(text.strip())
        if not d.is_finite() or d <= 0 or d % _CENT != 0:
            return None
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return d


def build_order(plan: ExitPlanRecord, limits: Limits) -> dict:
    """Re-validate the plan, then size it with tools.size.size_position (result unchanged).

    Never raises for a bad plan: returns {"ok": False, "reason": ...}.
    """
    try:
        validate_symbols([plan.symbol])
    except (ValueError, AttributeError, TypeError):
        return {"ok": False, "reason": "plan symbol is not a valid ticker"}
    entry, stop, target = _cents(plan.entry), _cents(plan.stop), _cents(plan.target)
    if entry is None or stop is None or target is None:
        return {"ok": False, "reason": "plan prices must be finite whole cents"}
    if not stop < entry < target:
        return {"ok": False, "reason": "plan must have stop < entry < target"}
    try:
        risk = Decimal(plan.risk_per_share)
        matches = risk.is_finite() and risk == entry - stop
    except (InvalidOperation, ValueError, ArithmeticError):
        matches = False
    if not matches:
        return {"ok": False, "reason": "plan risk_per_share does not match entry - stop"}
    return size_position(Plan(plan.symbol, entry, stop, target), limits)
