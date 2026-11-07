"""One decision end to end: analysts -> debate -> Trader -> order flow (Phase 4).

The only thing that crosses from the LLM side to the money side is the Trader's `plan_id`
string (issues #65, #85). The order flow gets that id, the pipeline's PlanLedger (filled by
plan_exit as plans were made, #64), the journal and the clock. It never sees the packet,
the debate, the Trader's text or any confidence.

`now` comes from the clock (the wall clock by default), never from packet.as_of or a plan's
as_of.

Latency budget (#65): a plan older than limits.max_plan_age_s is refused, so the analysts,
the debate, the Trader and the human together must fit inside it. Therefore:
  - the analysts' timeout must be below max_plan_age_s (default: half of it);
  - every agent config, the debate rounds and the limits are checked BEFORE any API call;
  - before the (billed) Trader call, the time elapsed since the analysts started is
    compared with max_plan_age_s; if it is already used up, the Trader is not called and
    the decision is a forced PASS ("stale_before_trader");
  - the ticket says when the plan expires, and the re-check after approval refuses a plan
    that went stale while the human read it.
Elapsed time is measured from the analysts' start, which is no later than any plan's as_of,
so the check is conservative. `DecisionResult.budget` records the ages at each step.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agents.debate import resolve_rounds, run_debate
from agents.llm import AgentConfig, config_for
from agents.orchestrator import AGENTS, load_checked_limits, run_analysts
from agents.schemas import DebateRecord, DecisionPacket
from agents.trader import TraderRun, forced_pass, run_trader
from journal.log import Journal
from pipeline.ledger import PlanLedger
from pipeline.market import NY
from pipeline.order_flow import (
    Ask,
    Clock,
    Outcome,
    Proposal,
    approve,
    propose,
    render_ticket,
    wall_clock,
)
from tools.scan import validate_symbols

ALL_AGENTS = (*AGENTS, "bull", "bear", "trader")


@dataclass
class DecisionResult:
    """Everything one decision produced. `outcome` is None when no approval was asked."""

    packet: DecisionPacket
    debate: DebateRecord
    trader: TraderRun
    ledger_run_id: str
    proposal: Proposal | None
    outcome: Outcome | None
    timings_ms: dict[str, int] = field(default_factory=dict)
    budget: dict[str, float | int | None] = field(default_factory=dict)

    @property
    def ticket(self) -> str | None:
        return render_ticket(self.proposal) if self.proposal is not None else None


def _client_for(clients: dict[str, Any] | Any, agent: str) -> Any:
    return clients.get(agent) if isinstance(clients, dict) else clients


def _elapsed_s(start: datetime, now: datetime) -> float:
    return round((now - start).total_seconds(), 3)


def run_decision(
    symbol: str,
    *,
    data: Any,
    clients: dict[str, Any] | Any,
    configs: dict[str, AgentConfig] | None = None,
    rounds: int | None = None,
    journal: Journal | None = None,
    clock: Clock = wall_clock,
    ask: Ask | None = input,
    timeout_s: float | None = None,
) -> DecisionResult:
    """Run one decision. `ask=None` stops after the ticket (no approval, nothing journaled
    as approved). A PASS, or a BUY that cannot be sized or fails the risk check, ends with
    no order.

    Raises ValueError before any API call for: a bad symbol, an invalid limits config, a bad
    agent config or debate rounds, a naive clock, or `timeout_s` not below
    limits.max_plan_age_s. `timeout_s` defaults to half of max_plan_age_s.
    """
    try:
        validate_symbols([symbol])
    except (ValueError, AttributeError, TypeError):
        raise ValueError("symbol must be a valid ticker") from None
    limits = load_checked_limits()
    max_age = limits.max_plan_age_s
    analyst_timeout = max_age / 2 if timeout_s is None else timeout_s
    if not 0 < analyst_timeout < max_age:
        raise ValueError(
            f"timeout_s must be above 0 and below limits.max_plan_age_s ({max_age} s), "
            "leaving time for the debate, the Trader and approval")
    cfgs = {a: (configs or {}).get(a) or config_for(a) for a in ALL_AGENTS}
    rounds = resolve_rounds(rounds)
    journal = journal if journal is not None else Journal()
    sym = symbol.strip().upper()
    ledger = PlanLedger(sym)
    timings: dict[str, int] = {}
    budget: dict[str, float | int | None] = {
        "max_plan_age_s": max_age, "analyst_timeout_s": analyst_timeout}

    started = clock()
    t0 = time.monotonic()
    packet = asyncio.run(run_analysts(
        sym, started, data=data, clients=clients, configs={a: cfgs[a] for a in AGENTS},
        timeout_s=analyst_timeout, limits=limits, ledger=ledger,
    ))
    timings["analysts"] = int((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    debate = run_debate(packet, clients=clients,
                        configs={a: cfgs[a] for a in ("bull", "bear")}, rounds=rounds)
    timings["debate"] = int((time.monotonic() - t0) * 1000)

    age = _elapsed_s(started, clock())
    budget["age_before_trader_s"] = age
    t0 = time.monotonic()
    if age > max_age:  # every plan is already stale: don't pay for a Trader call
        trader = forced_pass(packet.symbol, "stale_before_trader", cfgs["trader"])
    else:
        trader = run_trader(packet, debate, client=_client_for(clients, "trader"),
                            config=cfgs["trader"])
    timings["trader"] = int((time.monotonic() - t0) * 1000)

    decision = trader.decision
    result = DecisionResult(packet, debate, trader, ledger.run_id, None, None, timings, budget)
    if decision.action != "BUY" or not decision.plan_id:
        journal.append({
            "status": "pass", "run_id": ledger.run_id, "symbol": packet.symbol,
            "forced_pass": trader.forced_pass,
            "checked_at": clock().astimezone(NY).isoformat(),
        })
        return result

    plan_id: str = decision.plan_id  # the ONLY value that crosses to the order path
    result.proposal = propose(ledger, plan_id, journal=journal, clock=clock)
    budget["age_at_proposal_s"] = _elapsed_s(
        started, datetime.fromisoformat(result.proposal.checked_at))
    if ask is None:
        return result
    t0 = time.monotonic()
    result.outcome = approve(result.proposal, ledger, journal=journal, ask=ask, clock=clock)
    timings["approval"] = int((time.monotonic() - t0) * 1000)
    budget["plan_age_at_approval_s"] = result.outcome.plan_age_s
    return result
