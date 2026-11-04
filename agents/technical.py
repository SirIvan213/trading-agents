"""Technical Analyst: one symbol in, one validated AnalystReport out.

The model may only call the tools in agents/tool_registry.py. Any failure (invalid
JSON, refusal, token limit, too many tool rounds, API error) ends in a neutral
zero-confidence report; the agent never raises except for a naive `now`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agents.llm import AgentConfig
from agents.schemas import AnalystReport, Evidence
from agents.tool_registry import TOOLS, ToolSession
from pipeline.data import MarketData
from pipeline.market import NY, ny_today
from tools.config import Limits, load_limits
from tools.scan import ScanCriteria, validate_symbols

PROMPT = (Path(__file__).parent / "prompts" / "technical.md").read_text(encoding="utf-8")
BETAS = ["server-side-fallback-2026-07-01"]

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent": {"type": "string", "enum": ["technical"]},
        "symbol": {"type": "string"},
        "stance": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "confidence": {"type": "number"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"point": {"type": "string"}, "source": {"type": "string"}},
                "required": ["point", "source"],
                "additionalProperties": False,
            },
        },
        "catalysts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["agent", "symbol", "stance", "confidence", "evidence", "catalysts"],
    "additionalProperties": False,
}


@dataclass
class _Outcome:
    report: AnalystReport | None = None
    error: str = ""
    pending: list[dict] = field(default_factory=list)  # tool_results owed to the model


def _neutral(symbol: str, reason: str) -> AnalystReport:
    return AnalystReport(
        agent="technical",
        symbol=symbol,
        stance="neutral",
        confidence=0.0,
        evidence=[
            Evidence(point=f"technical analysis unavailable: {reason}", source="technical_analyst")
        ],
    )


def _btype(block: Any) -> str:
    return str(getattr(block, "type", ""))


def _request(client: Any, config: AgentConfig, messages: list[dict]) -> Any:
    kwargs: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": [{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}],
        "tools": TOOLS,
        "messages": messages,
        "output_config": {
            "effort": config.effort,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
    }
    if config.fallbacks:
        kwargs["betas"] = BETAS
        kwargs["fallbacks"] = "default"
    return client.beta.messages.create(**kwargs)


def _tool_results(content: Any, session: ToolSession, *, refuse: str | None = None) -> list[dict]:
    results = []
    for block in content:
        if _btype(block) != "tool_use":
            continue
        if refuse is not None:
            payload, is_error = {"error": refuse}, True
        else:
            payload, is_error = session.execute(block.name, block.input)
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": json.dumps(payload),
            "is_error": is_error,
        })
    return results


def _parse(content: Any, symbol: str) -> tuple[AnalystReport | None, str]:
    texts = [b.text for b in content if _btype(b) == "text" and getattr(b, "text", "")]
    if not texts:
        return None, "no text output"
    try:
        report = AnalystReport.model_validate_json(texts[-1])
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        return None, f"output failed validation at '{loc}': {first['msg']}"
    if report.agent != "technical":
        return None, "agent must be 'technical'"
    if report.symbol.strip().upper() != symbol:
        return None, f"symbol must be {symbol}"
    return report.model_copy(update={"symbol": symbol}), ""


def _loop(client: Any, config: AgentConfig, messages: list[dict], session: ToolSession) -> _Outcome:
    """Run the manual tool loop. Appends to `messages`; never edits earlier entries."""
    for i in range(config.max_tool_rounds + 1):
        response = _request(client, config, messages)
        content = list(response.content)
        stop = response.stop_reason
        if stop in ("tool_use", "pause_turn"):
            messages.append({"role": "assistant", "content": response.content})
            if i == config.max_tool_rounds:
                return _Outcome(
                    error="too many tool rounds",
                    pending=_tool_results(content, session, refuse="tool round limit reached"),
                )
            if stop == "tool_use":
                messages.append({"role": "user", "content": _tool_results(content, session)})
            continue
        if stop == "end_turn":
            report, err = _parse(content, session.symbol)
            if report is not None:
                return _Outcome(report=report)
            if content:
                messages.append({"role": "assistant", "content": response.content})
            return _Outcome(error=err)
        # refusal, max_tokens, anything unexpected
        if content:
            messages.append({"role": "assistant", "content": response.content})
        return _Outcome(
            error=f"model stopped with {stop}",
            pending=_tool_results(content, session, refuse="not executed"),
        )
    return _Outcome(error="too many tool rounds")  # pragma: no cover


def run_technical_analyst(
    symbol: str,
    now: datetime,
    data: MarketData,
    *,
    client: Any,
    config: AgentConfig | None = None,
    limits: Limits | None = None,
    criteria: ScanCriteria | None = None,
) -> AnalystReport:
    """Analyse one symbol and return an AnalystReport (neutral on any failure).

    Raises ValueError only for a naive `now` or an invalid symbol.
    """
    ny_today(now)  # ValueError for naive now
    try:
        validate_symbols([symbol])
    except (ValueError, AttributeError):
        raise ValueError("symbol must be a valid ticker") from None
    sym = symbol.strip().upper()
    try:
        cfg = config or AgentConfig()
        session = ToolSession(
            symbol=sym,
            now=now,
            data=data,
            limits=limits or load_limits(),
            criteria=criteria or ScanCriteria(),
        )
        messages: list[dict] = [{
            "role": "user",
            "content": f"Analyse {sym}. Time (US/Eastern): {now.astimezone(NY).isoformat()}.",
        }]
        outcome = _loop(client, cfg, messages, session)
        if outcome.report is None:
            retry_text = (
                f"Your previous attempt failed: {outcome.error}. "
                "Use the tools if needed and return only the JSON object."
            )
            messages.append({
                "role": "user",
                "content": [*outcome.pending, {"type": "text", "text": retry_text}],
            })
            outcome = _loop(client, cfg, messages, session)
        if outcome.report is None:
            return _neutral(sym, outcome.error)
        return outcome.report
    except Exception as exc:  # noqa: BLE001 - agents must not raise; type name only, no secrets
        return _neutral(sym, f"{type(exc).__name__}")
