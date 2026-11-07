"""One decision end to end: analysts -> debate -> Trader -> order flow (Phase 4).

The only thing that crosses from the LLM side to the money side is the Trader's `plan_id`
string (issues #65, #85). The order flow gets that id, the pipeline's PlanLedger (filled by
plan_exit as plans were made, #64), the journal and the clock. It never sees the packet,
the debate, the Trader's text or any confidence.

`now` comes from the clock (the wall clock by default), never from packet.as_of or a plan's
as_of. Timings are recorded so the end-to-end latency can be compared with
limits.max_plan_age_s: a plan older than that at approval time is refused.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from agents.debate import run_debate
from agents.llm import AgentConfig
from agents.orchestrator import run_analysts
from agents.schemas import DebateRecord, DecisionPacket
from agents.trader import TraderRun, run_trader
from journal.log import Journal
from pipeline.ledger import PlanLedger
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

    @property
    def ticket(self) -> str | None:
        return render_ticket(self.proposal) if self.proposal is not None else None


def _client_for(clients: dict[str, Any] | Any, agent: str) -> Any:
    return clients.get(agent) if isinstance(clients, dict) else clients


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
    timeout_s: float = 300,
) -> DecisionResult:
    """Run one decision. `ask=None` stops after the ticket (no approval, nothing journaled
    as approved). A PASS, or a BUY that cannot be sized or fails the risk check, ends with
    no order. Raises ValueError (before any API call) for a bad symbol, naive clock or an
    invalid limits config, as run_analysts does."""
    configs = configs or {}
    journal = journal if journal is not None else Journal()
    ledger = PlanLedger(symbol)
    timings: dict[str, int] = {}

    t0 = time.monotonic()
    packet = asyncio.run(run_analysts(
        symbol, clock(), data=data, clients=clients,
        configs={a: c for a, c in configs.items() if a in ("technical", "sentiment",
                                                           "fundamentals")},
        timeout_s=timeout_s, ledger=ledger,
    ))
    timings["analysts"] = int((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    debate = run_debate(packet, clients=clients,
                        configs={a: c for a, c in configs.items() if a in ("bull", "bear")},
                        rounds=rounds)
    timings["debate"] = int((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    trader = run_trader(packet, debate, client=_client_for(clients, "trader"),
                        config=configs.get("trader"))
    timings["trader"] = int((time.monotonic() - t0) * 1000)

    decision = trader.decision
    result = DecisionResult(packet, debate, trader, ledger.run_id, None, None, timings)
    if decision.action != "BUY" or not decision.plan_id:
        journal.append({
            "status": "pass", "run_id": ledger.run_id, "symbol": packet.symbol,
            "forced_pass": trader.forced_pass, "checked_at": clock().isoformat(),
        })
        return result

    plan_id: str = decision.plan_id  # the ONLY value that crosses to the order path
    result.proposal = propose(ledger, plan_id, journal=journal, clock=clock)
    if ask is None:
        return result
    t0 = time.monotonic()
    result.outcome = approve(result.proposal, ledger, journal=journal, ask=ask, clock=clock)
    timings["approval"] = int((time.monotonic() - t0) * 1000)
    return result
