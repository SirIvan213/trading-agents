"""Structured outputs every agent must return. Validated with Pydantic."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    point: str = Field(description="One concrete observation")
    source: str = Field(description="Tool or data the point came from")


class Catalyst(BaseModel):
    name: str = Field(description='Short event label, e.g. "earnings" or an 8-K item name')
    source: str = Field(description="The tool that returned it")


class AnalystReport(BaseModel):
    agent: Literal["technical", "sentiment", "fundamentals"]
    symbol: str
    stance: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(min_length=1)
    catalysts: list[Catalyst] = Field(
        default_factory=list, description="Events from tool output, e.g. earnings inside the trade"
    )


class BullCase(BaseModel):
    symbol: str
    argument: str
    key_points: list[str] = Field(min_length=1)


class BearCase(BaseModel):
    symbol: str
    objections: list[str] = Field(min_length=3, description="At least three concrete objections")
    fatal: bool = Field(description="True if any objection alone should stop the trade")


class TraderDecision(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL", "PASS"]
    confidence: float = Field(ge=0, le=1)
    reasoning: str
    setup: str | None = Field(default=None, description="e.g. orb, level; needed for exit_plan")


class UsageRecord(BaseModel):
    """Token counts for one API response or one usage iteration."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class IterationRecord(BaseModel):
    """One entry of response.usage.iterations."""

    type: str
    model: str | None = None
    usage: UsageRecord


class CallRecord(BaseModel):
    """One API call: which model was asked, which actually served it, and what it cost."""

    requested_model: str
    serving_model: str
    fallback_ran: bool
    stop_reason: str | None = None
    refusal_category: str | None = None
    fallback_from: str | None = None  # model that declined, from the last fallback block
    usage: UsageRecord
    iterations: list[IterationRecord] = Field(default_factory=list)


class AgentRunRecord(BaseModel):
    """Audit record of one analyst run (all of its API calls)."""

    agent: str
    requested_model: str
    effort: str
    ok: bool
    reason: str | None = None
    duration_ms: int
    calls: list[CallRecord] = Field(default_factory=list)


class ExitPlanRecord(BaseModel):
    """One successful exit_plan result, stored by the plan_exit executor.

    The ONLY sanctioned source of order prices. Produced by the plan_exit executor from tool
    output, never by a model and never parsed from report text.
    """

    plan_id: str
    symbol: str
    setup: Literal["orb", "level"]
    entry: str
    stop: str
    target: str
    risk_per_share: str
    reward_to_risk: str
    time_exit: str
    entry_level: str = Field(description="price_levels name that entry matched")
    structure_level: str | None = Field(
        description='price_levels name used as level, or "or_low" for orb'
    )
    as_of: str = Field(description="New York ISO timestamp")


class DecisionPacket(BaseModel):
    """The three analyst reports for one symbol plus their run records."""

    symbol: str
    as_of: str = Field(description="New York ISO timestamp")
    reports: dict[str, AnalystReport] = Field(
        description="keys: technical, sentiment, fundamentals"
    )
    runs: dict[str, AgentRunRecord]
    all_ok: bool
    exit_plans: list[ExitPlanRecord] = Field(
        default_factory=list,
        description="Technical run's plans; only when that run's report validated",
    )
