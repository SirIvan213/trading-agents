"""The one order path: from a stored exit plan to a sized order.

Order prices come only from tool-produced ExitPlanRecords. Analyst text and analyst scores
never feed sizing or risk_check (CLAUDE.md rule 2; issues #40, #50). Nothing but the plan and
the risk limits is an input here.
"""
from __future__ import annotations

from decimal import Decimal

from agents.schemas import DecisionPacket, ExitPlanRecord
from tools.config import Limits
from tools.size import Plan, size_position


def select_plan(packet: DecisionPacket, plan_id: str) -> ExitPlanRecord:
    """Return the packet's plan with this id, or raise KeyError("unknown plan_id")."""
    for plan in packet.exit_plans:
        if plan.plan_id == plan_id:
            return plan
    raise KeyError("unknown plan_id")


def build_order(plan: ExitPlanRecord, limits: Limits) -> dict:
    """Size the plan with tools.size.size_position and return its result unchanged."""
    return size_position(
        Plan(plan.symbol, Decimal(plan.entry), Decimal(plan.stop), Decimal(plan.target)),
        limits,
    )
