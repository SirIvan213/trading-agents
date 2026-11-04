"""LLM client configuration. Never logs or echoes the API key."""
from __future__ import annotations

import os
from dataclasses import dataclass

EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass(frozen=True)
class AgentConfig:
    """Settings for one agent run."""

    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_tokens: int = 16000
    max_tool_rounds: int = 8
    fallbacks: bool = True  # server-side refusal fallback (beta header + fallbacks="default")


def config_from_env() -> AgentConfig:
    """Build an AgentConfig, overriding model/effort from TRADING_AGENTS_MODEL / _EFFORT.

    Raises ValueError for an unknown effort or a blank model.
    """
    kwargs: dict[str, str] = {}
    model = os.environ.get("TRADING_AGENTS_MODEL")
    if model is not None:
        if not model.strip():
            raise ValueError("TRADING_AGENTS_MODEL must be a non-empty string")
        kwargs["model"] = model.strip()
    effort = os.environ.get("TRADING_AGENTS_EFFORT")
    if effort is not None:
        effort = effort.strip().lower()
        if effort not in EFFORTS:
            raise ValueError(f"TRADING_AGENTS_EFFORT must be one of {sorted(EFFORTS)}")
        kwargs["effort"] = effort
    return AgentConfig(**kwargs)


def make_client():  # noqa: ANN201 - returns anthropic.Anthropic
    """Return anthropic.Anthropic using ANTHROPIC_API_KEY from the environment."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not key.strip():
        raise RuntimeError("ANTHROPIC_API_KEY must be set in the environment")
    import anthropic

    return anthropic.Anthropic(api_key=key)
