"""Run the three analysts concurrently and assemble a DecisionPacket.

Analysts propose only; nothing here sizes, limits or trades.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from agents.fundamentals import run_fundamentals
from agents.llm import AgentConfig, config_for
from agents.runner import NEUTRAL_SOURCE, AnalystRun, neutral_report
from agents.schemas import AgentRunRecord, CallRecord, DecisionPacket, ExitPlanRecord
from agents.sentiment import run_sentiment
from agents.technical import run_technical
from pipeline.market import NY, ny_today
from tools.config import Limits, load_limits, validate_limits
from tools.scan import validate_symbols

AGENTS = ("technical", "sentiment", "fundamentals")
_RUNNERS: dict[str, Callable[..., AnalystRun]] = {
    "technical": run_technical,
    "sentiment": run_sentiment,
    "fundamentals": run_fundamentals,
}

__all__ = ["AGENTS", "NEUTRAL_SOURCE", "run_analysts"]


def _client_for(clients: dict[str, Any] | Any, agent: str) -> Any:
    return clients.get(agent) if isinstance(clients, dict) else clients


def load_checked_limits(limits: Limits | None = None) -> Limits:
    """Load (if not given) and validate the risk limits, or raise.

    Raises ValueError("limits config is invalid") - never the file contents - for an
    unreadable, malformed or invalid config, or for invalid hand-built `limits`. Called once
    by run_analysts before any analyst starts, so a broken risk config stops the run instead
    of becoming a quiet neutral report inside one analyst.
    """
    try:
        loaded = load_limits() if limits is None else limits
        validate_limits(loaded)
    except Exception:  # noqa: BLE001 - message must not echo config contents
        raise ValueError("limits config is invalid") from None
    return loaded


# Extra keyword arguments per analyst. Only the technical analyst's tools (plan_exit, scan)
# read risk limits; sentiment and fundamentals take none.
def _extra_kwargs(agent: str, limits: Limits) -> dict[str, Any]:
    return {"limits": limits} if agent == "technical" else {}


async def _one(
    agent: str,
    symbol: str,
    now: datetime,
    data: Any,
    client: Any,
    config: AgentConfig,
    timeout_s: float,
    executor: ThreadPoolExecutor,
    cancel: threading.Event,
    extra: dict[str, Any],
) -> tuple[Any, AgentRunRecord, list[ExitPlanRecord]]:
    """Run one analyst; any exception or timeout becomes a neutral report, never a raise."""
    calls: list[CallRecord] = []
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    try:
        run: AnalystRun = await asyncio.wait_for(
            loop.run_in_executor(
                executor,
                lambda: _RUNNERS[agent](
                    symbol, now, data, client=client, config=config, calls_sink=calls,
                    cancel=cancel, **extra,
                ),
            ),
            timeout=timeout_s,
        )
        record = AgentRunRecord(
            agent=agent, requested_model=config.model, effort=config.effort, ok=run.ok,
            reason=run.reason, duration_ms=run.duration_ms, calls=run.calls,
        )
        return run.report, record, (list(run.exit_plans) if run.ok else [])
    except asyncio.CancelledError:
        cancel.set()  # the orchestrator itself was cancelled: stop the worker cooperatively
        raise
    except Exception as exc:  # noqa: BLE001 - includes TimeoutError; the run must not crash
        cancel.set()  # ask a still-running worker to stop at its next request/tool call
        reason = "timeout" if isinstance(exc, TimeoutError) else f"failed ({type(exc).__name__})"
        record = AgentRunRecord(
            agent=agent, requested_model=config.model, effort=config.effort, ok=False,
            reason=reason, duration_ms=int((time.monotonic() - started) * 1000),
            calls=list(calls),
        )
        return neutral_report(agent, symbol, reason), record, []


async def run_analysts(
    symbol: str,
    now: datetime,
    *,
    data: Any,
    clients: dict[str, Any] | Any,
    configs: dict[str, AgentConfig] | None = None,
    timeout_s: float = 300,
    limits: Limits | None = None,
) -> DecisionPacket:
    """Run the technical, sentiment and fundamentals analysts concurrently.

    `clients` is one client shared by all three, or a dict keyed by agent name. `configs`
    defaults to config_for(agent). Risk limits are loaded (unless `limits` is given) and
    validated ONCE, before any analyst starts; an invalid config raises
    ValueError("limits config is invalid") and no analyst runs. The checked limits are passed
    to the analysts that use them (technical). Each analyst runs in its own worker thread under
    `timeout_s`. If one raises or times out it gets a neutral report (evidence source
    NEUTRAL_SOURCE) and a run record with ok=False and reason "failed (<Type>)" or "timeout";
    the others still complete.

    A timed-out thread cannot be killed. Each analyst gets a cancel Event, set on timeout (and
    on cancellation of this coroutine); the worker checks it before every API request and tool
    call and then stops, but an in-flight request is not interrupted, so it may finish in the
    background with its result discarded. AgentConfig.request_timeout_s (per API request) should
    be below `timeout_s` to bound that. Raises ValueError for a naive `now`, an invalid
    symbol, a bad agent config, or an invalid limits config - all before any analyst starts.
    """
    ny_today(now)  # ValueError for naive now
    try:
        validate_symbols([symbol])
    except (ValueError, AttributeError):
        raise ValueError("symbol must be a valid ticker") from None
    sym = symbol.strip().upper()
    cfgs = {a: (configs or {}).get(a) or config_for(a) for a in AGENTS}
    checked = load_checked_limits(limits)  # raises before the worker pool exists

    cancels = {a: threading.Event() for a in AGENTS}
    executor = ThreadPoolExecutor(max_workers=len(AGENTS), thread_name_prefix="analyst")
    try:
        results = await asyncio.gather(*(
            _one(a, sym, now, data, _client_for(clients, a), cfgs[a], timeout_s, executor,
                 cancels[a], _extra_kwargs(a, checked))
            for a in AGENTS
        ))
    finally:
        for ev in cancels.values():  # also covers cancellation of run_analysts itself
            ev.set()
        executor.shutdown(wait=False)  # never block on a timed-out worker
    reports = {a: r for a, (r, _, _) in zip(AGENTS, results, strict=True)}
    runs = {a: rec for a, (_, rec, _) in zip(AGENTS, results, strict=True)}
    plans = {a: p for a, (_, _, p) in zip(AGENTS, results, strict=True)}
    return DecisionPacket(
        symbol=sym,
        as_of=now.astimezone(NY).isoformat(),
        reports=reports,
        runs=runs,
        all_ok=all(rec.ok for rec in runs.values()),
        exit_plans=(
            [p for p in plans["technical"] if p.symbol == sym] if runs["technical"].ok else []
        ),
    )
