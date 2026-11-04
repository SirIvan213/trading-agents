"""Structured outputs every agent must return. Validated with Pydantic."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    point: str = Field(description="One concrete observation")
    source: str = Field(description="Tool or data the point came from")


class AnalystReport(BaseModel):
    agent: Literal["technical", "sentiment", "fundamentals"]
    symbol: str
    stance: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(min_length=1)
    catalysts: list[str] = Field(default_factory=list, description="e.g. earnings inside the trade")


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
