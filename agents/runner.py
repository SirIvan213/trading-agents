"""Generic analyst runner: the manual tool loop shared by all three analysts.

The model may only call the tools of its AnalystSpec. Any failure (invalid JSON, refusal,
token limit, too many tool rounds, API error) ends in a neutral zero-confidence report and
`AnalystRun.ok == False`; the runner never raises except for a naive `now` or a bad symbol.
Every API response is recorded as a CallRecord (requested vs serving model, fallback, usage).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pydantic import ValidationError

from agents.llm import AgentConfig
from agents.schemas import (
    AnalystReport,
    CallRecord,
    Evidence,
    IterationRecord,
    UsageRecord,
)
from pipeline.market import NY, ny_today
from tools.scan import validate_symbols

BETAS = ["server-side-fallback-2026-07-01"]
NEUTRAL_SOURCE = "analyst_unavailable"  # evidence source of every neutral fallback report


class ToolExecutor(Protocol):
    """Anything with `.execute(name, input) -> (result, is_error)` that never raises."""

    def execute(self, name: str, tool_input: Any) -> tuple[dict, bool]:
        """Run one tool."""
        ...


@dataclass(frozen=True)
class AnalystSpec:
    """What differs between analysts: name, system prompt, tool schemas, output schema."""

    name: str  # "technical" | "sentiment" | "fundamentals"
    prompt: str
    tools: list[dict[str, Any]]
    output_schema: dict[str, Any]


@dataclass
class AnalystRun:
    """Result of one analyst run. `ok` is the explicit not-a-fallback signal (issue #48)."""

    report: AnalystReport
    ok: bool
    reason: str | None
    calls: list[CallRecord]
    duration_ms: int


# --------------------------------------------------------------------------
# Output schema
# --------------------------------------------------------------------------

_KEEP = ("type", "enum", "properties", "required", "additionalProperties", "items")


def _strict(node: Any, defs: dict[str, Any]) -> Any:
    """Reduce a pydantic JSON schema to type/enum/properties/required/additionalProperties."""
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _strict(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    out: dict[str, Any] = {}
    if "const" in node:
        out["enum"] = [node["const"]]
    for key in _KEEP:
        if key not in node:
            continue
        value = node[key]
        if key == "properties":
            out[key] = {k: _strict(v, defs) for k, v in value.items()}
        elif key == "items":
            out[key] = _strict(value, defs)
        else:
            out[key] = value
    if out.get("type") == "object":
        out["required"] = list(out.get("properties", {}))
        out["additionalProperties"] = False
    return out


def output_schema(agent_name: str) -> dict[str, Any]:
    """Strict-compatible JSON schema of AnalystReport with `agent` pinned to `agent_name`."""
    raw = AnalystReport.model_json_schema()
    schema = _strict(raw, raw.get("$defs", {}))
    schema["properties"]["agent"] = {"type": "string", "enum": [agent_name]}
    return schema


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage_record(obj: Any) -> UsageRecord:
    return UsageRecord(
        input_tokens=_int(getattr(obj, "input_tokens", 0)),
        output_tokens=_int(getattr(obj, "output_tokens", 0)),
        cache_creation_input_tokens=_int(getattr(obj, "cache_creation_input_tokens", 0)),
        cache_read_input_tokens=_int(getattr(obj, "cache_read_input_tokens", 0)),
    )


def call_record(response: Any, requested_model: str) -> CallRecord:
    """Build the CallRecord for one API response. Every field is read defensively."""
    usage = getattr(response, "usage", None)
    raw_iters = getattr(usage, "iterations", None) or []
    iterations = []
    for it in raw_iters:
        model = getattr(it, "model", None)
        iterations.append(IterationRecord(
            type=str(getattr(it, "type", "")),
            model=str(model) if model else None,
            usage=_usage_record(it),
        ))
    fallback_models = [i.model for i in iterations if i.type == "fallback_message" and i.model]
    resp_model = getattr(response, "model", None)
    serving = fallback_models[-1] if fallback_models else (str(resp_model) if resp_model else "")
    stop = getattr(response, "stop_reason", None)
    blocks = [
        b for b in (getattr(response, "content", None) or [])
        if getattr(b, "type", "") == "fallback"
    ]
    last_fb = blocks[-1] if blocks else None
    if stop == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
    else:
        category = getattr(getattr(last_fb, "trigger", None), "category", None)
    declined = getattr(getattr(last_fb, "from_", None), "model", None)
    return CallRecord(
        requested_model=requested_model,
        serving_model=serving or requested_model,
        fallback_ran=any(i.type == "fallback_message" for i in iterations),
        stop_reason=str(stop) if stop is not None else None,
        refusal_category=str(category) if category else None,
        fallback_from=str(declined) if declined else None,
        usage=_usage_record(usage),
        iterations=iterations,
    )


# --------------------------------------------------------------------------
# Loop
# --------------------------------------------------------------------------


@dataclass
class _Outcome:
    report: AnalystReport | None = None
    error: str = ""
    pending: list[dict] = field(default_factory=list)  # tool_results owed to the model


class _Cancelled(Exception):
    """Raised inside the loop when the run's cancel event is set."""


class _Tracked:
    """Session wrapper remembering what each successful tool returned in this run.

    `ok_tools` are the tools that returned data (not an error, not an `ok: false` refusal).
    `allowed[tool]` is the set of names in that tool's `catalysts` / `red_flags` lists.
    Also the cancellation point before every tool execution.
    """

    def __init__(self, inner: ToolExecutor, cancel: threading.Event | None = None) -> None:
        self.inner = inner
        self.cancel = cancel
        self.ok_tools: set[str] = set()
        self.allowed: dict[str, set[str]] = {}

    def check_cancel(self) -> None:
        """Raise _Cancelled if the cancel event is set."""
        if self.cancel is not None and self.cancel.is_set():
            raise _Cancelled

    def execute(self, name: str, tool_input: Any) -> tuple[dict, bool]:
        self.check_cancel()
        result, is_error = self.inner.execute(name, tool_input)
        refusal = isinstance(result, dict) and result.get("ok") is False
        if not is_error and not refusal:
            self.ok_tools.add(name)
            names = self.allowed.setdefault(name, set())
            if isinstance(result, dict):
                for key in ("catalysts", "red_flags"):
                    items = result.get(key)
                    if isinstance(items, list):
                        names.update(i for i in items if isinstance(i, str))
        return result, is_error


def _neutral(name: str, symbol: str, reason: str) -> AnalystReport:
    return AnalystReport(
        agent=name,  # type: ignore[arg-type]
        symbol=symbol,
        stance="neutral",
        confidence=0.0,
        evidence=[Evidence(point=f"{name} analysis unavailable: {reason}", source=NEUTRAL_SOURCE)],
    )


def neutral_report(name: str, symbol: str, reason: str) -> AnalystReport:
    """Public neutral report (zero confidence, evidence source NEUTRAL_SOURCE)."""
    return _neutral(name, symbol, reason)


def _btype(block: Any) -> str:
    return str(getattr(block, "type", ""))


def _request(client: Any, spec: AnalystSpec, config: AgentConfig, messages: list[dict]) -> Any:
    # with_options is the real SDK per-request timeout; fakes may not have it.
    if hasattr(client, "with_options"):
        client = client.with_options(timeout=config.request_timeout_s)
    kwargs: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": [{"type": "text", "text": spec.prompt, "cache_control": {"type": "ephemeral"}}],
        "tools": spec.tools,
        "messages": messages,
        "output_config": {
            "effort": config.effort,
            "format": {"type": "json_schema", "schema": spec.output_schema},
        },
    }
    if config.fallbacks:
        kwargs["betas"] = BETAS
        kwargs["fallbacks"] = "default"
    return client.beta.messages.create(**kwargs)


def _tool_results(content: Any, session: ToolExecutor, *, refuse: str | None = None) -> list[dict]:
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


def _parse(
    content: Any, name: str, symbol: str, tracked: _Tracked
) -> tuple[AnalystReport | None, str]:
    texts = [b.text for b in content if _btype(b) == "text" and getattr(b, "text", "")]
    if not texts:
        return None, "no text output"
    try:
        report = AnalystReport.model_validate_json(texts[-1])
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        return None, f"output failed validation at '{loc}': {first['msg']}"
    if report.agent != name:
        return None, f"agent must be '{name}'"
    if report.symbol.strip().upper() != symbol:
        return None, f"symbol must be {symbol}"
    ok_tools = tracked.ok_tools
    for ev in report.evidence:
        if ev.source not in ok_tools:
            return None, f"evidence source {ev.source} is not a tool that returned data in this run"
    for cat in report.catalysts:
        if cat.source not in ok_tools:
            return None, (
                f"catalyst source {cat.source} is not a tool that returned data in this run"
            )
        if cat.name not in tracked.allowed.get(cat.source, set()):
            return None, f"catalyst {cat.name} is not in {cat.source}'s output"
    return report.model_copy(update={"symbol": symbol}), ""


def _loop(
    client: Any,
    spec: AnalystSpec,
    config: AgentConfig,
    messages: list[dict],
    session: _Tracked,
    symbol: str,
    calls: list[CallRecord],
) -> _Outcome:
    """Run the manual tool loop. Appends to `messages`; never edits earlier entries."""
    for i in range(config.max_tool_rounds + 1):
        session.check_cancel()
        response = _request(client, spec, config, messages)
        calls.append(call_record(response, config.model))
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
            report, err = _parse(content, spec.name, symbol, session)
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


def run_analyst(
    spec: AnalystSpec,
    symbol: str,
    now: datetime,
    session: ToolExecutor,
    *,
    client: Any,
    config: AgentConfig,
    calls_sink: list[CallRecord] | None = None,
    cancel: threading.Event | None = None,
) -> AnalystRun:
    """Run one analyst and return an AnalystRun (neutral report, ok=False on any failure).

    `calls_sink`, if given, is the list CallRecords are appended to as they happen, so a
    caller that abandons the run (timeout) still sees the calls made so far.
    `cancel`, if given, is checked before every API request and tool execution; once set the
    run stops and returns the neutral report with reason "cancelled" (cooperative: an
    in-flight request is not interrupted, but the SDK timeout bounds it).
    Raises ValueError only for a naive `now` or an invalid symbol.
    """
    ny_today(now)  # ValueError for naive now
    try:
        validate_symbols([symbol])
    except (ValueError, AttributeError):
        raise ValueError("symbol must be a valid ticker") from None
    sym = symbol.strip().upper()
    calls: list[CallRecord] = calls_sink if calls_sink is not None else []
    started = time.monotonic()

    def finish(report: AnalystReport, ok: bool, reason: str | None) -> AnalystRun:
        ms = int((time.monotonic() - started) * 1000)
        return AnalystRun(report=report, ok=ok, reason=reason, calls=list(calls), duration_ms=ms)

    try:
        tracked = _Tracked(session, cancel)
        messages: list[dict] = [{
            "role": "user",
            "content": f"Analyse {sym}. Time (US/Eastern): {now.astimezone(NY).isoformat()}.",
        }]
        outcome = _loop(client, spec, config, messages, tracked, sym, calls)
        if outcome.report is None:
            retry_text = (
                f"Your previous attempt failed: {outcome.error}. "
                "Use the tools if needed and return only the JSON object."
            )
            messages.append({
                "role": "user",
                "content": [*outcome.pending, {"type": "text", "text": retry_text}],
            })
            outcome = _loop(client, spec, config, messages, tracked, sym, calls)
        if outcome.report is None:
            return finish(_neutral(spec.name, sym, outcome.error), False, outcome.error)
        return finish(outcome.report, True, None)
    except _Cancelled:
        return finish(_neutral(spec.name, sym, "cancelled"), False, "cancelled")
    except Exception as exc:  # noqa: BLE001 - must not raise; type name only, no secrets
        reason = type(exc).__name__
        return finish(_neutral(spec.name, sym, reason), False, reason)
