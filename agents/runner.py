"""Generic analyst runner: the manual tool loop shared by all three analysts.

The model may only call the tools of its AnalystSpec. Any failure (invalid JSON, refusal,
token limit, too many tool rounds, API error) ends in a neutral zero-confidence report and
`AnalystRun.ok == False`; the runner never raises except for a naive `now` or a bad symbol.
Every API response is recorded as a CallRecord (requested vs serving model, fallback, usage).
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from agents.llm import AgentConfig
from agents.schemas import (
    AnalystReport,
    CallRecord,
    Evidence,
    ExitPlanRecord,
    IterationRecord,
    ReasonCode,
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
    # Plans the plan_exit executor recorded. Use them downstream ONLY when ok is True; when
    # ok is False run_analyst sets this to [] (the report did not validate).
    exit_plans: list[ExitPlanRecord] = field(default_factory=list)
    reason_code: ReasonCode | None = None  # None when ok (issue #52)


# --------------------------------------------------------------------------
# Output schema
# --------------------------------------------------------------------------

_KEEP = ("type", "enum", "properties", "required", "additionalProperties", "items")


def _strict(node: Any, defs: dict[str, Any], path: str = "") -> Any:
    """Reduce a pydantic JSON schema to type/enum/properties/required/additionalProperties.

    Raises ValueError (naming the field path) for any anyOf/oneOf/allOf: unions are not
    expressible as a simple strict type, so a model must not use them in an output schema.
    """
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _strict(defs[node["$ref"].rsplit("/", 1)[-1]], defs, path)
    for union in ("anyOf", "oneOf", "allOf"):
        if union in node:
            where = path or "<root>"
            raise ValueError(f"unsupported union ({union}) in output schema at '{where}'")
    out: dict[str, Any] = {}
    if "const" in node:
        out["enum"] = [node["const"]]
    for key in _KEEP:
        if key not in node:
            continue
        value = node[key]
        if key == "properties":
            out[key] = {k: _strict(v, defs, f"{path}.{k}".lstrip(".")) for k, v in value.items()}
        elif key == "items":
            out[key] = _strict(value, defs, f"{path}[]")
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
    code: ReasonCode = "invalid_output"
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
        self.answered_tools: set[str] = set()  # is_error False, including ok:false results
        self.allowed: dict[str, set[str]] = {}

    def check_cancel(self) -> None:
        """Raise _Cancelled if the cancel event is set."""
        if self.cancel is not None and self.cancel.is_set():
            raise _Cancelled

    def execute(self, name: str, tool_input: Any) -> tuple[dict, bool]:
        self.check_cancel()
        result, is_error = self.inner.execute(name, tool_input)
        refusal = isinstance(result, dict) and result.get("ok") is False
        if not is_error:
            self.answered_tools.add(name)
        if not is_error and not refusal:
            self.ok_tools.add(name)
            names = self.allowed.setdefault(name, set())
            if isinstance(result, dict):
                for key in ("catalysts", "red_flags"):
                    items = result.get(key)
                    if isinstance(items, list):
                        names.update(i for i in items if isinstance(i, str))
        return result, is_error


def _is_api_error(exc: BaseException) -> bool:
    """True for anthropic SDK errors (APIError family, incl. connection/timeout errors)."""
    return any(c.__module__.split(".")[0] == "anthropic" for c in type(exc).__mro__)


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


def _norm(name: str) -> str:
    """Casefold, strip, and collapse runs of spaces/hyphens/underscores to one "_"."""
    return re.sub(r"[\s_-]+", "_", name.casefold().strip())


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
        if ev.source not in tracked.answered_tools:
            return None, f"evidence source {ev.source} is not a tool that answered in this run"
    fixed = []
    for cat in report.catalysts:
        if cat.source not in ok_tools:
            return None, (
                f"catalyst source {cat.source} is not a tool that returned data in this run"
            )
        exact = {_norm(n): n for n in sorted(tracked.allowed.get(cat.source, set()))}
        canonical = exact.get(_norm(cat.name))
        if canonical is None:
            return None, f"catalyst {cat.name} is not in {cat.source}'s output"
        fixed.append(cat.model_copy(update={"name": canonical}))
    return report.model_copy(update={"symbol": symbol, "catalysts": fixed}), ""


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
                    error="too many tool rounds", code="tool_rounds",
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
            code="refusal" if stop == "refusal" else (
                "max_tokens" if stop == "max_tokens" else "invalid_output"
            ),
            pending=_tool_results(content, session, refuse="not executed"),
        )
    return _Outcome(error="too many tool rounds", code="tool_rounds")  # pragma: no cover


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

    def finish(
        report: AnalystReport, ok: bool, reason: str | None, code: ReasonCode | None = None
    ) -> AnalystRun:
        ms = int((time.monotonic() - started) * 1000)
        plans = list(getattr(session, "exit_plans", [])) if ok else []
        return AnalystRun(
            report=report, ok=ok, reason=reason, calls=list(calls), duration_ms=ms,
            exit_plans=plans, reason_code=None if ok else code,
        )

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
            return finish(
                _neutral(spec.name, sym, outcome.error), False, outcome.error, outcome.code
            )
        return finish(outcome.report, True, None)
    except _Cancelled:
        return finish(_neutral(spec.name, sym, "cancelled"), False, "cancelled", "cancelled")
    except Exception as exc:  # noqa: BLE001 - must not raise; type name only, no secrets
        reason = type(exc).__name__
        code: ReasonCode = "api_error" if _is_api_error(exc) else "exception"
        return finish(_neutral(spec.name, sym, reason), False, reason, code)


# --------------------------------------------------------------------------
# Generic structured runner (no tools): used by the debate agents
# --------------------------------------------------------------------------


@dataclass
class StructuredRun:
    """Result of run_structured. `value` is the validated model, or None when `ok` is False."""

    value: BaseModel | None
    ok: bool
    reason: str | None
    reason_code: ReasonCode | None
    calls: list[CallRecord]
    duration_ms: int


def model_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Strict-compatible JSON schema of any pydantic model (nullable fields become type lists)."""
    raw = model.model_json_schema()
    return _strict(raw, raw.get("$defs", {}))


def _structured_request(
    client: Any, name: str, prompt: str, schema: dict[str, Any], config: AgentConfig,
    messages: list[dict],
) -> Any:
    """Same request shape as `_request` but with NO tools (and no thinking/temperature)."""
    spec = AnalystSpec(name=name, prompt=prompt, tools=[], output_schema=schema)
    if hasattr(client, "with_options"):
        client = client.with_options(timeout=config.request_timeout_s)
    kwargs: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": [{"type": "text", "text": spec.prompt, "cache_control": {"type": "ephemeral"}}],
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


def run_structured(
    name: str,
    prompt: str,
    user_payload: str,
    output_model: type[BaseModel],
    validate: Callable[[BaseModel], str | tuple[str, str] | None],
    *,
    client: Any,
    config: AgentConfig,
    cancel: threading.Event | None = None,
) -> StructuredRun:
    """One tool-less structured request, validated, with ONE retry; never raises.

    The JSON text is parsed into `output_model`, then `validate(value)` returns None when fine
    an error string, or a tuple (stored_reason, model_hint). Only `stored_reason` is ever kept
    in the result; `model_hint` (which may quote offending values) goes only to the model in the
    retry message. Invalid output, a refusal or max_tokens triggers one retry with a short
    error message appended (history is append-only). `cancel` is checked before each request.
    A CallRecord is kept for every response. API errors give reason_code "api_error" and
    reason = exception type name only.
    """
    calls: list[CallRecord] = []
    started = time.monotonic()

    def done(
        value: BaseModel | None, reason: str | None, code: ReasonCode | None
    ) -> StructuredRun:
        ms = int((time.monotonic() - started) * 1000)
        return StructuredRun(
            value=value, ok=value is not None, reason=reason,
            reason_code=None if value is not None else code, calls=list(calls), duration_ms=ms,
        )

    try:
        schema = model_output_schema(output_model)  # ValueError -> exception result
        messages: list[dict] = [{"role": "user", "content": user_payload}]
        error, hint, code = "", "", "invalid_output"
        for attempt in range(2):
            if cancel is not None and cancel.is_set():
                raise _Cancelled
            response = _structured_request(client, name, prompt, schema, config, messages)
            calls.append(call_record(response, config.model))
            content = list(response.content)
            stop = response.stop_reason
            hint = ""
            if stop == "end_turn":
                texts = [
                    b.text for b in content if _btype(b) == "text" and getattr(b, "text", "")
                ]
                code = "invalid_output"
                if not texts:
                    error = "no text output"
                else:
                    try:
                        value = output_model.model_validate_json(texts[-1])
                    except ValidationError as exc:
                        first = exc.errors()[0]
                        loc = ".".join(str(p) for p in first["loc"])
                        error = f"output failed validation at '{loc}': {first['msg']}"
                    else:
                        problem = validate(value)
                        if problem is None:
                            return done(value, None, None)
                        error, hint = (problem, problem) if isinstance(problem, str) else problem
            else:
                error = f"model stopped with {stop}"
                code = (
                    "refusal" if stop == "refusal"
                    else "max_tokens" if stop == "max_tokens" else "invalid_output"
                )
            if attempt == 0:
                if content:
                    messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your previous attempt failed: {hint or error}. "
                        "Return only the corrected JSON object."
                    ),
                })
        return done(None, error, code)  # type: ignore[arg-type]
    except _Cancelled:
        return done(None, "cancelled", "cancelled")
    except Exception as exc:  # noqa: BLE001 - must not raise; type name only, no secrets
        return done(None, type(exc).__name__, "api_error" if _is_api_error(exc) else "exception")
