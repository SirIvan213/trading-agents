"""From the Trader's plan_id to an approved, journaled order (issues #64, #65, #85).

This module is the only caller of build_order and check_order in the decision pipeline. Its
inputs are deliberately narrow: a plan_id, the pipeline's PlanLedger, the journal, a clock
and (for approval) a function that reads the human's answer. It never receives a
DecisionPacket, a DebateRecord or a TraderDecision, so no model text or score can reach
sizing or the risk gate (CLAUDE.md rule 2). Risk limits come only from load_checked_limits().

Flow:
  1. propose(): look the plan up in the ledger, build_order at the clock's `now`, read
     today's DayState from the journal, check_order. Any failure means no order.
  2. render_ticket(): the order in plain words.
  3. approve(): show the ticket and require the typed word "yes" for that exact order. Then
     re-check at a NEW wall-clock `now` (freshness, build_order, check_order). Stale, blocked
     or changed means no order. An approved order is journaled "approved_not_sent"; nothing
     is sent to a broker in Phase 4.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from agents.orchestrator import load_checked_limits
from agents.schemas import ExitPlanRecord
from journal.log import APPROVED, Journal, JournalError
from pipeline.ledger import PlanLedger
from pipeline.market import NY
from pipeline.orders import build_order, select_plan
from tools.risk_check import check_order

Clock = Callable[[], datetime]
Ask = Callable[[str], str]


def wall_clock() -> datetime:
    """The real time, timezone-aware. The default clock everywhere in the order path."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class Proposal:
    """A sized and risk-checked order for one plan, or why there is none.

    `stage` names the step that refused it: "limits", "plan", "size", "journal" or "risk";
    None when ok.
    """

    ok: bool
    stage: str | None
    reason: str | None
    run_id: str
    plan_id: str | None
    plan: ExitPlanRecord | None
    sized: dict | None
    verdict: dict | None
    checked_at: str

    @property
    def order(self) -> dict | None:
        return self.sized["order"] if self.ok and self.sized else None


@dataclass(frozen=True)
class Outcome:
    """What happened at approval. status is one of: "approved_not_sent", "declined",
    "refused_after_approval", "changed_after_approval", "no_order"."""

    status: str
    reason: str | None
    proposal: Proposal
    recheck: Proposal | None = None
    approved_at: str | None = None
    plan_age_s: int | None = None


def _iso(now: datetime) -> str:
    return now.astimezone(NY).isoformat()


def propose(
    ledger: PlanLedger, plan_id: str | None, *, journal: Journal, clock: Clock = wall_clock
) -> Proposal:
    """Size and risk-check the ledger plan `plan_id` at `clock()`. Never raises."""
    now = clock()
    run_id = ledger.run_id if isinstance(ledger, PlanLedger) else ""

    def refuse(
        stage: str, reason: str, plan: ExitPlanRecord | None = None,
        sized: dict | None = None, verdict: dict | None = None,
    ) -> Proposal:
        return Proposal(False, stage, reason, run_id, plan_id, plan, sized, verdict, _iso(now))

    if now.tzinfo is None or now.utcoffset() is None:
        return refuse("plan", "clock must be timezone-aware")
    try:
        limits = load_checked_limits()
    except ValueError:
        return refuse("limits", "limits config is invalid")
    try:
        plan = select_plan(ledger, plan_id)  # type: ignore[arg-type]
    except KeyError:
        return refuse("plan", "unknown plan_id")
    sized = build_order(plan, limits, now)
    if sized.get("ok") is not True:
        return refuse("size", str(sized.get("reason", "order could not be built")), plan, sized)
    try:
        state = journal.day_state(now)
    except JournalError as exc:
        return refuse("journal", str(exc), plan, sized)
    verdict = check_order(sized["order"], limits, state, now)
    if verdict.get("verdict") != "PASS":
        return refuse("risk", "; ".join(verdict.get("reasons", [])) or "blocked",
                      plan, sized, verdict)
    return Proposal(True, None, None, run_id, plan_id, plan, sized, verdict, _iso(now))


def render_ticket(p: Proposal) -> str:
    """The order in plain words (or why there is none). Every number comes from tools."""
    if not p.ok or p.plan is None or p.sized is None:
        return (f"NO ORDER for plan {p.plan_id or '(none)'}: {p.reason} "
                f"(refused at {p.stage}, checked {p.checked_at}).")
    o, plan, sized = p.sized["order"], p.plan, p.sized
    qty = o["qty"]
    return "\n".join([
        "ORDER TICKET (paper account; an approved order is journaled, NOT sent to a broker)",
        f"  Buy {qty} shares of {o['symbol']} at market, as a bracket order.",
        f"  Stop-loss: sell all {qty} if the price falls to {o['stop_loss']}.",
        f"  Take-profit: sell all {qty} if the price reaches {o['take_profit']}.",
        f"  Planned entry {o['entry']}; risk per share {sized['risk_per_share']}; "
        f"maximum loss at the stop {sized['max_loss']}.",
        f"  Size decided by the limit: {sized['decided_by']}.",
        f"  Plan {plan.plan_id} ({plan.setup} setup), made {plan.as_of}; "
        f"flat by {plan.time_exit} ET.",
        f"  Risk check: {p.verdict['verdict'] if p.verdict else 'n/a'}. "
        f"Checked {p.checked_at}. Run {p.run_id}.",
    ])


def approval_prompt(p: Proposal) -> str:
    """The question asked under the ticket; names the exact order being approved."""
    o = p.order or {}
    return (f"Type yes to approve exactly this order (BUY {o.get('qty')} {o.get('symbol')}, "
            f"stop {o.get('stop_loss')}, target {o.get('take_profit')}); "
            "anything else declines: ")


def _entry(p: Proposal, status: str, reason: str | None, **extra: object) -> dict:
    o = p.order if p.ok else None
    return {
        "status": status, "reason": reason, "run_id": p.run_id, "plan_id": p.plan_id,
        "symbol": (p.plan.symbol if p.plan else None), "order": o,
        "max_loss": (p.sized or {}).get("max_loss") if p.ok else None,
        "decided_by": (p.sized or {}).get("decided_by") if p.ok else None,
        "verdict": p.verdict, "checked_at": p.checked_at, **extra,
    }


def _plan_age_s(plan: ExitPlanRecord, now: datetime) -> int | None:
    try:
        made = datetime.fromisoformat(plan.as_of)
        return int((now.astimezone(UTC) - made.astimezone(UTC)).total_seconds())
    except (ValueError, TypeError):
        return None


def approve(
    p: Proposal, ledger: PlanLedger, *, journal: Journal, ask: Ask = input,
    clock: Clock = wall_clock,
) -> Outcome:
    """Ask a human to approve `p`, re-check it, and journal the result. Never sends an order.

    A proposal that is not ok is journaled as "no_order" (with its stage) and never asked.

    Only the exact answer "yes" (surrounding spaces ignored, lower case) approves. After the
    answer the plan is re-sized and re-checked at a new `clock()` reading; if that fails, or
    the re-built order differs in any field from the one shown, there is no order.
    Raises JournalError only if the approved order cannot be journaled (then nothing counts
    as approved).
    """
    if not p.ok:
        try:  # audit trail for a refused BUY; a broken journal must not hide the refusal
            journal.append(_entry(p, "no_order", p.reason, stage=p.stage))
        except JournalError:
            pass
        return Outcome("no_order", p.reason, p)
    try:
        answer = ask(render_ticket(p) + "\n" + approval_prompt(p))
    except EOFError:
        answer = ""
    if not isinstance(answer, str) or answer.strip() != "yes":
        journal.append(_entry(p, "declined", "not approved"))
        return Outcome("declined", "not approved", p)

    again = propose(ledger, p.plan_id, journal=journal, clock=clock)
    if not again.ok:
        reason = f"re-check failed at {again.stage}: {again.reason}"
        journal.append(_entry(p, "refused_after_approval", reason, recheck_at=again.checked_at))
        return Outcome("refused_after_approval", reason, p, again)
    if again.sized != p.sized or again.plan != p.plan:
        reason = "the re-built order differs from the approved ticket"
        journal.append(_entry(p, "changed_after_approval", reason, recheck_at=again.checked_at))
        return Outcome("changed_after_approval", reason, p, again)

    approved_at = datetime.fromisoformat(again.checked_at)
    age = _plan_age_s(again.plan, approved_at) if again.plan else None
    journal.append(_entry(again, APPROVED, None, approved_at=again.checked_at, plan_age_s=age))
    return Outcome(APPROVED, None, p, again, again.checked_at, age)
