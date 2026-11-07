"""LLM client configuration. Never logs or echoes the API key."""
from __future__ import annotations

import os
from dataclasses import dataclass

EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
DEFAULT_MODELS: dict[str, str] = {
    "technical": "claude-sonnet-5-5",
    "sentiment": "claude-sonnet-5-5",
    "fundamentals": "claude-sonnet-5-5",
    "bull": "claude-sonnet-5-5",
    "bear": "claude-sonnet-5-5",
    "trader": "claude-opus-5-5",
}


@dataclass(frozen=True)
class AgentConfig:
    """Settings for one agent run."""

    model: str = "claude-sonnet-5-5"
    effort: str = "high"
    max_tokens: int = 16000
    max_tool_rounds: int = 8
    request_timeout_s: float = 120.0  # per API request; keep below the orchestrator timeout_s
    fallbacks: bool = True  # server-side refusal fallback (beta header + fallbacks="default")


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    if not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _valid_effort(name: str, value: str) -> str:
    value = value.lower()
    if value not in EFFORTS:
        raise ValueError(f"{name} must be one of {sorted(EFFORTS)}")
    return value


def config_from_env() -> AgentConfig:
    """Build an AgentConfig, overriding model/effort from TRADING_AGENTS_MODEL / _EFFORT.

    Raises ValueError for an unknown effort or a blank model.
    """
    kwargs: dict[str, str] = {}
    model = _env("TRADING_AGENTS_MODEL")
    if model is not None:
        kwargs["model"] = model
    effort = _env("TRADING_AGENTS_EFFORT")
    if effort is not None:
        kwargs["effort"] = _valid_effort("TRADING_AGENTS_EFFORT", effort)
    return AgentConfig(**kwargs)


def config_for(agent: str) -> AgentConfig:
    """AgentConfig for one agent ("technical", "sentiment", "fundamentals", "bull", "bear" or
    "trader").

    Priority: TRADING_AGENTS_<AGENT>_MODEL / _EFFORT, then TRADING_AGENTS_MODEL / _EFFORT,
    then DEFAULT_MODELS / the AgentConfig default effort. Raises ValueError for an unknown
    agent, a blank model or an unknown effort.
    """
    if agent not in DEFAULT_MODELS:
        raise ValueError(f"unknown agent {agent!r}")
    up = agent.upper()
    model = _env(f"TRADING_AGENTS_{up}_MODEL") or _env("TRADING_AGENTS_MODEL")
    effort_name = f"TRADING_AGENTS_{up}_EFFORT"
    effort = _env(effort_name)
    if effort is None:
        effort_name = "TRADING_AGENTS_EFFORT"
        effort = _env(effort_name)
    kwargs: dict[str, str] = {"model": model or DEFAULT_MODELS[agent]}
    if effort is not None:
        kwargs["effort"] = _valid_effort(effort_name, effort)
    return AgentConfig(**kwargs)


def make_client():  # noqa: ANN201 - returns anthropic.Anthropic
    """Return anthropic.Anthropic using ANTHROPIC_API_KEY from the environment."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not key.strip():
        raise RuntimeError("ANTHROPIC_API_KEY must be set in the environment")
    import anthropic

    return anthropic.Anthropic(api_key=key)
