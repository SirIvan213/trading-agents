"""Structured outputs every agent must return. Validated with Pydantic."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Evidence(BaseModel):
    point: str = Field(description="One concrete observation")
    source: str = Field(description="Tool or data the point came from")


ReasonCode = Literal[
    "api_error", "timeout", "cancelled", "refusal", "max_tokens",
    "invalid_output", "tool_rounds", "config_error", "exception",
]


class Catalyst(BaseModel):
    name: str = Field(
        description=(
            "copied exactly from the cited tool's `catalysts` or `red_flags` list "
            "(e.g. earnings, officer_change, halt)"
        )
    )
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
    plan_id: str | None = Field(
        default=None,
        description="the packet plan this case argues for, or null; must be one of "
        "packet.exit_plans",
    )
    argument: str
    key_points: list[str] = Field(min_length=1)


class BearCase(BaseModel):
    symbol: str
    plan_id: str | None = Field(
        default=None,
        description="the packet plan this case challenges, or null; must be one of "
        "packet.exit_plans",
    )
    objections: list[str] = Field(min_length=3, description="At least three concrete objections")
    fatal: bool = Field(description="True if any objection alone should stop the trade")


class TraderDecision(BaseModel):
    """The Trader's choice. Long only: BUY one tool-produced plan by id, or PASS.

    Deliberately has NO price, stop, target, size or setup field (issues #65, #85): the order
    is built only from the pipeline's plan ledger by plan_id. `confidence` and `reasoning` are
    advisory and never reach sizing or risk_check (CLAUDE.md rule 2).
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    action: Literal["BUY", "PASS"]
    plan_id: str | None = Field(
        default=None, description="plan_id from the packet's exit_plans; required for BUY"
    )
    confidence: float = Field(ge=0, le=1)
    reasoning: str

    @model_validator(mode="after")
    def _buy_needs_plan(self) -> TraderDecision:
        if self.action == "BUY" and not (self.plan_id or "").strip():
            raise ValueError("BUY requires a plan_id")
        return self


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


class RetryRecord(BaseModel):
    """Why one attempt was rejected and retried (issue #87).

    `reason` is the stored reason, the same text a final failure would store. For the debate
    money check it is count-only and never quotes a rejected value (the detailed hint goes
    only to the model); an analyst reason may name the source or catalyst it rejected.
    """

    attempt: int = Field(description="1-based attempt that failed and was retried")
    reason: str
    reason_code: ReasonCode


class AgentRunRecord(BaseModel):
    """Audit record of one analyst run (all of its API calls)."""

    agent: str
    requested_model: str
    effort: str
    ok: bool
    reason: str | None = None
    reason_code: ReasonCode | None = None  # None when ok
    duration_ms: int
    calls: list[CallRecord] = Field(default_factory=list)
    retries: list[RetryRecord] = Field(
        default_factory=list, description="attempts rejected before the final one"
    )


class ExitPlanRecord(BaseModel):
    """One successful exit_plan result, stored by the plan_exit executor.

    The ONLY sanctioned source of order prices. Produced by the plan_exit executor from tool
    output, never by a model and never parsed from report text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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


class DebateTurn(BaseModel):
    """One side's turn in the debate, with its audit record."""

    round: int
    side: Literal["bull", "bear"]
    case: BullCase | BearCase
    run: AgentRunRecord

    @model_validator(mode="after")
    def _side_matches_case(self) -> DebateTurn:
        """A bull turn holds a BullCase and a bear turn a BearCase."""
        expected = BullCase if self.side == "bull" else BearCase
        if not isinstance(self.case, expected):
            raise ValueError(f"side {self.side!r} requires a {expected.__name__}")
        return self


class DebateRecord(BaseModel):
    """Outcome of the bull/bear debate. Advisory text only; never feeds sizing or orders."""

    symbol: str
    as_of: str
    rounds_planned: int
    rounds_run: int
    turns: list[DebateTurn]
    stopped_early: bool
    fatal: bool
    requested_same_model: bool = Field(description="bull and bear were configured with one model")
    same_model: bool = Field(
        description="both sides requested and were served by the same model"
    )
    final_bull: BullCase | None
    final_bear: BearCase | None
    all_ok: bool
