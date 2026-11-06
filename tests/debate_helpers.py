"""Shared offline helpers for the Phase 4A debate tests (no network, no Claude API)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from agents.schemas import AnalystReport, DecisionPacket, Evidence, ExitPlanRecord

ENTRY, STOP, TARGET, RISK = "102.00", "99.61", "104.39", "2.39"
AS_OF = "2026-11-05T10:30:00-05:00"


def make_plan(plan_id: str = "p1", **over) -> ExitPlanRecord:
    d = dict(
        plan_id=plan_id, symbol="AAPL", setup="orb", entry=ENTRY, stop=STOP, target=TARGET,
        risk_per_share=RISK, reward_to_risk="1.00", time_exit="15:45",
        entry_level="or_high", structure_level="or_low", as_of=AS_OF,
    )
    d.update(over)
    return ExitPlanRecord(**d)


def make_packet(plans: list[ExitPlanRecord] | None = None, symbol: str = "AAPL") -> DecisionPacket:
    rep = AnalystReport(
        agent="technical", symbol=symbol, stance="bullish", confidence=0.6,
        evidence=[Evidence(point="breakout above the opening range", source="plan_exit")],
    )
    return DecisionPacket(
        symbol=symbol, as_of=AS_OF, reports={"technical": rep}, runs={}, all_ok=True,
        exit_plans=[make_plan()] if plans is None else plans,
    )


def reply(obj, stop: str = "end_turn", model: str = "claude-sonnet-5-5"):
    body = obj if isinstance(obj, str) else json.dumps(obj)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=body)], stop_reason=stop, model=model,
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
            cache_read_input_tokens=0, iterations=None,
        ),
    )


def bull_json(argument: str = "The breakout holds above the opening range.",
              key_points=None, plan_id: str | None = "p1", symbol: str = "AAPL") -> dict:
    return {"symbol": symbol, "plan_id": plan_id, "argument": argument,
            "key_points": key_points or ["technical report is bullish"]}


def bear_json(objections=None, fatal: bool = False, plan_id: str | None = "p1",
              symbol: str = "AAPL") -> dict:
    return {"symbol": symbol, "plan_id": plan_id, "fatal": fatal,
            "objections": objections or ["thin volume", "late in the move", "one analyst only"]}


def user_json(call: dict) -> dict:
    """Parse the packet_data JSON block out of the FIRST user message of a recorded call."""
    content = call["messages"][0]["content"]
    start = content.index("<packet_data>") + len("<packet_data>")
    end = content.index("</packet_data>")
    return json.loads(content[start:end])
