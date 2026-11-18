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
    compared with max_plan_age_s; if less than MIN_APPROVAL_S (30 s) would be left for the
    human, the Trader is not called and the decision is a forced PASS
    ("stale_before_trader");
  - the ticket says when the plan expires, and the re-check after approval refuses a plan
    that went stale while the human read it.
Elapsed time is measured from the analysts' start, which is no later than any plan's as_of,
so the check is conservative. `DecisionResult.budget` records the ages at each step.

Sending (Phase 5 PR 3): only when the caller passes a `broker` (tools.broker's paper client).
The ticket then says the approved order is SENT to the Alpaca paper account, and an approval
goes to pipeline.send.send_approved (re-check under the lock, write-ahead "submitting", one
POST, never resent). Without a broker nothing is sent, as in Phase 4.
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
from journal.log import Journal, JournalError, JournalLockTimeout
from journal.orders import APPROVED
from pipeline.events import events_from, reporting_check
from pipeline.ledger import PlanLedger
from pipeline.market import NY
from pipeline.order_flow import (
    Ask,
    Clock,
    Outcome,
    Proposal,
    approve,
    is_yes,
    propose,
    quotes_from,
    render_ticket,
    wall_clock,
)
from pipeline.send import SendResult, send_approved
from tools.scan import validate_symbols

ALL_AGENTS = (*AGENTS, "bull", "bear", "trader")
# Seconds the human must have left to read the ticket; with less, the Trader is not called.
MIN_APPROVAL_S = 30


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
    record_error: str | None = None  # a PASS that could not be journaled (#190)
    sends: bool = False  # a broker was given: an approval is sent to the paper account
    sent: SendResult | None = None  # what happened at the broker (None: nothing sent)

    @property
    def ticket(self) -> str | None:
        return (render_ticket(self.proposal, sends=self.sends)
                if self.proposal is not None else None)


def _client_for(clients: dict[str, Any] | Any, agent: str) -> Any:
    return clients.get(agent) if isinstance(clients, dict) else clients


def _elapsed_s(start: datetime, now: datetime) -> float:
    return round((now - start).total_seconds(), 3)


class SymbolSkipped(ValueError):  # noqa: N818 - a skip, not an error
    """The symbol was skipped before any billed call (#170): it is not proven to report
    quarterly (no 8-K item 2.02 in the last 100 days), so the event gate could only block it.
    No decision was spent. `reason` says why."""

    def __init__(self, symbol: str, reason: str) -> None:
        super().__init__(f"{symbol} skipped before any billed call: {reason}")
        self.symbol = symbol
        self.reason = reason


JOURNAL_LOCKED = "journal_locked"
JOURNAL_ERROR = "journal_error"
RECORD_ATTEMPTS = 2  # the record of a failed approval is written with one retry (#129)


def _append_with_retry(journal: Journal, entry: dict) -> JournalError | None:
    """Append `entry`, retried once (#129, #190). None when written, else the last error."""
    last: JournalError | None = None
    for _ in range(RECORD_ATTEMPTS):
        try:
            journal.append(entry)
            return None
        except JournalError as exc:
            last = exc
    return last


def _journal_failed(proposal: Proposal, journal: Journal, exc: JournalError,
                    clock: Clock, *, said_yes: bool) -> Outcome:
    """The outcome when approve() raised JournalError (#129): "journal_locked" for a lock
    timeout, else "journal_error". Nothing is approved. The attempt is journaled, retried
    once; if both writes fail, the outcome's reason says it was not recorded. The reason says
    whether the human approved or declined (#192)."""
    status = JOURNAL_LOCKED if isinstance(exc, JournalLockTimeout) else JOURNAL_ERROR
    answer = "after a yes" if said_yes else "recording a decline"
    reason = f"journal unavailable {answer}, nothing approved: {exc}"
    try:
        at = clock().astimezone(NY).isoformat()
    except Exception:  # noqa: BLE001 - the record must not fail on the clock
        at = None
    entry = {"status": status, "reason": reason, "run_id": proposal.run_id,
             "plan_id": proposal.plan_id,
             "symbol": proposal.plan.symbol if proposal.plan else None,
             "proposal_checked_at": proposal.checked_at, "checked_at": at,
             "answer": "yes" if said_yes else "declined"}
    if _append_with_retry(journal, entry) is not None:
        reason += " (and the attempt could not be journaled)"
    return Outcome(status, reason, proposal)


def _recording(ask: Ask, answers: list[object]) -> Ask:
    """`ask`, also keeping each answer in `answers` (an EOFError keeps none: a decline)."""
    def recorded(prompt: str) -> str:
        answer = ask(prompt)
        answers.append(answer)
        return answer
    return recorded


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
    broker: Any | None = None,
) -> DecisionResult:
    """Run one decision. `ask=None` stops after the ticket (no approval, nothing journaled
    as approved). A PASS, or a BUY that cannot be sized or fails the risk check, ends with
    no order. If the journal cannot be locked or written during approval (#129), the outcome
    is "journal_locked" (or "journal_error"), nothing is approved, and the attempt is
    journaled (one retry).

    With a `broker` (the Alpaca paper client), an approved order is sent once through
    pipeline.send.send_approved; `result.sent` says what happened. Without one nothing is
    sent.

    Raises ValueError before any API call for: a bad symbol, an invalid limits config, a bad
    agent config or debate rounds, a naive clock, or `timeout_s` not below
    limits.max_plan_age_s. `timeout_s` defaults to half of max_plan_age_s.

    Raises SymbolSkipped (a ValueError) before any billed call when `data`'s SEC filings do
    not show an 8-K item 2.02 in the last 100 days (#170): a foreign private issuer, no SEC
    filer, or SEC unavailable. The event gate could only call it UNKNOWN, so no decision is
    spent on it. The skip is journaled (best effort).
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
    # #170: free SEC check before the first billed call
    coverage = reporting_check(data, sym, started)
    if coverage.get("covered") is not True:
        reason = str(coverage.get("reason") or "quarterly reporting not proven")
        _append_with_retry(journal, {
            "status": "skipped", "symbol": sym, "run_id": ledger.run_id, "reason": reason,
            "checked_at": started.astimezone(NY).isoformat(),
        })
        raise SymbolSkipped(sym, reason)
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
    if age > max_age - MIN_APPROVAL_S:  # no time left to approve: skip the billed call
        trader = forced_pass(packet.symbol, "stale_before_trader", cfgs["trader"])
    else:
        trader = run_trader(packet, debate, client=_client_for(clients, "trader"),
                            config=cfgs["trader"])
    timings["trader"] = int((time.monotonic() - t0) * 1000)

    decision = trader.decision
    result = DecisionResult(packet, debate, trader, ledger.run_id, None, None, timings, budget,
                            sends=broker is not None)
    if decision.action != "BUY" or not decision.plan_id:
        # #190: a PASS record that cannot be written (lock timeout, cut-off journal) is
        # retried once and reported in the result; it never raises out of the decision.
        try:
            checked_at: str | None = clock().astimezone(NY).isoformat()
        except Exception:  # noqa: BLE001 - the record must not fail on the clock
            checked_at = None
        failed = _append_with_retry(journal, {
            "status": "pass", "run_id": ledger.run_id, "symbol": packet.symbol,
            "forced_pass": trader.forced_pass, "checked_at": checked_at,
        })
        if failed is not None:
            result.record_error = f"the PASS could not be journaled: {failed}"
        return result

    plan_id: str = decision.plan_id  # the ONLY value that crosses to the order path
    quotes = quotes_from(data)  # live quote, read again in the approval re-check (#68)
    events = events_from(data)  # earnings and halt gate, read again at the re-check (#112)
    result.proposal = propose(ledger, plan_id, journal=journal, clock=clock, quotes=quotes,
                              events=events)
    budget["age_at_proposal_s"] = _elapsed_s(
        started, datetime.fromisoformat(result.proposal.checked_at))
    if ask is None:
        return result
    t0 = time.monotonic()
    answers: list[object] = []
    ask = _recording(ask, answers)  # #192: tells a yes from a decline if the journal fails
    try:
        result.outcome = approve(result.proposal, ledger, journal=journal, ask=ask,
                                 clock=clock, quotes=quotes, events=events,
                                 sends=broker is not None)
    except JournalError as exc:
        # #129: the journal lock timed out (or a write failed) after the answer. Nothing is
        # approved, since an approval exists only once its entry is written, but the attempt
        # must still leave a record: one entry, written with one retry.
        said_yes = bool(answers) and is_yes(answers[-1])
        result.outcome = _journal_failed(result.proposal, journal, exc, clock,
                                         said_yes=said_yes)
    timings["approval"] = int((time.monotonic() - t0) * 1000)
    budget["plan_age_at_approval_s"] = result.outcome.plan_age_s
    if broker is not None and result.outcome.status == APPROVED:
        t0 = time.monotonic()
        result.sent = send_approved(result.outcome, journal=journal, broker=broker, clock=clock)
        timings["send"] = int((time.monotonic() - t0) * 1000)
    return result
