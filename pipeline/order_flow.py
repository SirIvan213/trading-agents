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
     or changed means no order. An approved order is journaled "approved_not_sent" with its
     deterministic client_order_id (journal/orders.py). The live quote and the event gate
     are fetched BEFORE the journal lock is taken (#148); their freshness is re-checked
     inside it (build_order, check_order).
  4. Sending is pipeline/send.py (Phase 5 PR 3): only when the caller passes a broker, and
     then the ticket says the approved order is sent to the Alpaca PAPER account.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agents.orchestrator import load_checked_limits
from agents.schemas import ExitPlanRecord
from journal.log import APPROVED, Journal, JournalError
from journal.orders import OrderError, client_order_id
from pipeline.events import EventSource
from pipeline.ledger import PlanLedger
from pipeline.market import NY
from pipeline.orders import build_order, select_plan
from tools.broker import BrokerError, bracket_request
from tools.market_calendar import CalendarError, day_times
from tools.risk_check import check_order

Clock = Callable[[], datetime]
Ask = Callable[[str], str]
# Live quote source (#68): symbol -> Alpaca latestQuote {"ap", "bp", "t"} or None. Injected like
# the clock, read in propose() and again in the approval re-check.
Quotes = Callable[[str], object]


def quotes_from(data: object) -> Quotes:
    """A Quotes source reading `data.snapshots([symbol])[symbol]["latestQuote"]`."""
    def read(symbol: str) -> object:
        snap = data.snapshots([symbol]).get(symbol)  # type: ignore[attr-defined]
        return snap.get("latestQuote") if isinstance(snap, dict) else None
    return read


def wall_clock() -> datetime:
    """The real time, timezone-aware. The default clock everywhere in the order path."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class Proposal:
    """A sized and risk-checked order for one plan, or why there is none.

    `stage` names the step that refused it: "limits", "plan", "quote", "size", "journal" or
    "risk";
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
    expires_at: str | None = None  # plan as_of + limits.max_plan_age_s (New York time)
    gate: dict | None = None  # event-gate result (#112): status CLEAR / EVENT / UNKNOWN

    @property
    def order(self) -> dict | None:
        return self.sized["order"] if self.ok and self.sized else None


@dataclass(frozen=True)
class Outcome:
    """What happened at approval. status is one of: "approved_not_sent", "declined",
    "refused_after_approval", "changed_after_approval", "no_order", and from
    pipeline.decide (#129) "journal_locked" / "journal_error" (nothing approved)."""

    status: str
    reason: str | None
    proposal: Proposal
    recheck: Proposal | None = None
    approved_at: str | None = None
    plan_age_s: int | None = None


def _iso(now: datetime) -> str:
    return now.astimezone(NY).isoformat()


def _checked_at(now: object) -> str:
    """`now` in New York time, or as given when it cannot be converted (naive, not a datetime,
    or too close to the date-range limits, #127): a refusal still needs a timestamp."""
    try:
        return _iso(now)  # type: ignore[arg-type]
    except (OverflowError, ValueError, TypeError, AttributeError):
        return str(now)


def _expires_at(plan: ExitPlanRecord, max_age_s: int) -> str | None:
    try:
        return _iso(datetime.fromisoformat(plan.as_of) + timedelta(seconds=max_age_s))
    except (ValueError, TypeError, OverflowError):
        return None


def _window_end(now: datetime, limits: object) -> datetime:
    """Today's flat-by in New York (12:55 on an early close): the end of an intraday trade.
    On a day the calendar cannot answer, `now` (risk_check blocks that day anyway)."""
    day = now.astimezone(NY).date()
    try:
        flat = day_times(day, limits)[2]  # type: ignore[arg-type]
    except CalendarError:
        return now
    return datetime.combine(day, flat, tzinfo=NY)


def propose(
    ledger: PlanLedger, plan_id: str | None, *, journal: Journal, clock: Clock = wall_clock,
    quotes: Quotes | None = None, events: EventSource | None = None,
) -> Proposal:
    """Size and risk-check the ledger plan `plan_id` at `clock()`. Never raises.

    The symbol's live quote is read from `quotes` after the clock (#68); without a quote source
    or a usable quote there is no order (stage "quote" when the source fails, "size" when the
    quote is missing, stale, crossed or the price moved past the slippage allowance).

    A plan that already has an order in the journal (its client_order_id, #109) is refused
    at stage "journal": one order per plan.

    The event gate (#112) is read from `events` (symbol, now, today's flat-by) and passed to
    check_order. Without an events source the result is missing and check_order BLOCKs; a
    source that raises is refused at stage "events". The result rides on the Proposal.

    A clock that is naive, or aware but too close to the date-range limits to convert to New
    York time (#127), is refused at stage "plan"."""
    now = clock()
    run_id = ledger.run_id if isinstance(ledger, PlanLedger) else ""
    checked_at = _checked_at(now)

    def refuse(
        stage: str, reason: str, plan: ExitPlanRecord | None = None,
        sized: dict | None = None, verdict: dict | None = None, gate: dict | None = None,
    ) -> Proposal:
        return Proposal(False, stage, reason, run_id, plan_id, plan, sized, verdict,
                        checked_at, gate=gate)

    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return refuse("plan", "clock must be timezone-aware")
    try:
        _iso(now)
    except (OverflowError, ValueError):
        return refuse("plan", "clock is outside the supported date range")
    try:
        limits = load_checked_limits()
    except ValueError:
        return refuse("limits", "limits config is invalid")
    try:
        plan = select_plan(ledger, plan_id)  # type: ignore[arg-type]
    except KeyError:
        return refuse("plan", "unknown plan_id")
    if quotes is None:
        return refuse("quote", "no live quote source", plan)
    try:
        quote = quotes(plan.symbol)
    except Exception as exc:  # noqa: BLE001 - a data error must never raise out of propose
        return refuse("quote", f"live quote unavailable ({type(exc).__name__})", plan)
    sized = build_order(plan, limits, now, quote)
    if sized.get("ok") is not True:
        return refuse("size", str(sized.get("reason", "order could not be built")), plan, sized)
    try:  # #136: an order the paper broker would refuse is never proposed
        bracket_request(sized["order"], client_order_id(run_id, plan.plan_id))
    except (BrokerError, OrderError) as exc:
        return refuse("size", f"the broker request cannot be built: {exc}", plan, sized)
    try:
        # #191: one lock hold for the three reads, so the journal is replayed once (the lock
        # is local file I/O only; the event gate below is fetched after it is released). A
        # journal that does not exist yet is read without creating it (#192).
        with journal.exclusive() if journal.path.exists() else nullcontext():
            # One order per plan (#109): the client_order_id is derived from the run and the
            # plan, so a second approval of the same plan would be a second order with the
            # same id.
            if client_order_id(run_id, plan.plan_id) in journal.orders():
                return refuse("journal", "this plan already has an order (one order per plan)",
                              plan, sized)
            journal.expire_unsent(now)  # unsent approvals past their plan's expiry (#109)
            state = journal.day_state(now)
    except (JournalError, OrderError) as exc:
        return refuse("journal", str(exc), plan, sized)
    gate: dict | None = None
    if events is not None:
        try:
            gate = events(plan.symbol, now, _window_end(now, limits))
        except Exception as exc:  # noqa: BLE001 - a data error must never raise out of propose
            return refuse("events", f"event gate failed ({type(exc).__name__})", plan, sized)
    verdict = check_order(sized["order"], limits, state, now, gate)
    if verdict.get("verdict") != "PASS":
        return refuse("risk", "; ".join(verdict.get("reasons", [])) or "blocked",
                      plan, sized, verdict, gate)
    return Proposal(True, None, None, run_id, plan_id, plan, sized, verdict, checked_at,
                    _expires_at(plan, limits.max_plan_age_s), gate)


def limit_price(p: Proposal) -> str | None:
    """The entry LIMIT price the broker request carries (tools.broker.bracket_request: the
    plan's entry + the slippage allowance, #136), or None when there is no order or the
    request cannot be built. The ticket shows exactly the price that is sent."""
    if p.order is None or p.plan_id is None:
        return None
    try:
        return bracket_request(p.order, client_order_id(p.run_id, p.plan_id))["limit_price"]
    except (BrokerError, OrderError):
        return None


def render_ticket(p: Proposal, *, sends: bool = False) -> str:
    """The order in plain words (or why there is none). Every number comes from tools.
    `sends` says whether approving SENDS the order to the Alpaca paper account (a caller
    with a broker) or only journals it."""
    if not p.ok or p.plan is None or p.sized is None:
        return (f"NO ORDER for plan {p.plan_id or '(none)'}: {p.reason} "
                f"(refused at {p.stage}, checked {p.checked_at}).")
    o, plan, sized = p.sized["order"], p.plan, p.sized
    qty = o["qty"]
    head = ("ORDER TICKET (Alpaca PAPER account; an approved order is SENT to the paper "
            "broker after a final risk re-check)" if sends else
            "ORDER TICKET (paper account; an approved order is journaled, NOT sent to a broker)")
    return "\n".join([
        head,
        f"  Buy {qty} shares of {o['symbol']} with a LIMIT order at {limit_price(p)} "
        f"(planned entry {o['entry']} + slippage allowance {o.get('slippage', 'n/a')}), "
        "as a bracket order, time in force day: it fills at that price or lower. Nothing "
        "cancels an unfilled entry yet: it stays working until the close.",
        f"  Stop-loss: sell all {qty} if the price falls to {o['stop_loss']}.",
        f"  Take-profit: sell all {qty} if the price reaches {o['take_profit']}.",
        f"  Planned risk per share {sized['risk_per_share']} "
        f"(live ask {(sized.get('quote') or {}).get('ask', 'n/a')}).",
        f"  Worst-case loss at the stop {sized['max_loss']}, for a fill at the limit price "
        "(a gap through the stop can lose more).",
        f"  Size decided by the limit: {sized['decided_by']}.",
        f"  Plan {plan.plan_id} ({plan.setup} setup), made {plan.as_of}; "
        f"flat by {plan.time_exit} ET.",
        f"  Event gate: {(p.gate or {}).get('status', 'n/a')} "
        f"({(p.gate or {}).get('reason', 'no result')}).",
        f"  Risk check: {p.verdict['verdict'] if p.verdict else 'n/a'}. "
        f"Checked {p.checked_at}. Run {p.run_id}.",
        f"  Approve before {p.expires_at or 'n/a'}: after that the plan is stale and the "
        "re-check refuses it.",
    ])


def approval_prompt(p: Proposal, *, sends: bool = False) -> str:
    """The question asked under the ticket; names the exact order being approved."""
    o = p.order or {}
    verb = "approve and SEND to the paper account" if sends else "approve"
    return (f"Type yes to {verb} exactly this order (BUY {o.get('qty')} {o.get('symbol')}, "
            f"LIMIT {limit_price(p)}, stop {o.get('stop_loss')}, "
            f"target {o.get('take_profit')}); anything else declines: ")


def _entry(p: Proposal, status: str, reason: str | None, **extra: object) -> dict:
    o = p.order if p.ok else None
    return {
        "status": status, "reason": reason, "run_id": p.run_id, "plan_id": p.plan_id,
        "symbol": (p.plan.symbol if p.plan else None), "order": o,
        "max_loss": (p.sized or {}).get("max_loss") if p.ok else None,
        "decided_by": (p.sized or {}).get("decided_by") if p.ok else None,
        "quote": (p.sized or {}).get("quote") if p.ok else None,
        # #112: the gate's status and reason, so EVENT and UNKNOWN blocks stay distinct facts
        "gate": ({"status": p.gate.get("status"), "reason": p.gate.get("reason")}
                 if isinstance(p.gate, dict) else None),
        "verdict": p.verdict, "checked_at": p.checked_at, **extra,
    }


def _order_part(sized: dict | None) -> dict | None:
    """The sized result without its live-quote details (time, age, bid/ask), which always
    differ between the ticket and the re-check. Shares, allowance, max loss and the order
    itself must match exactly."""
    return None if sized is None else {k: v for k, v in sized.items() if k != "quote"}


def _plan_age_s(plan: ExitPlanRecord, now: datetime) -> int | None:
    try:
        made = datetime.fromisoformat(plan.as_of)
        return int((now.astimezone(UTC) - made.astimezone(UTC)).total_seconds())
    except (ValueError, TypeError):
        return None


def check_before_send(p: Proposal, *, journal: Journal, now: datetime) -> dict:
    """check_order for an APPROVED order at the moment it is sent (#187), so this module
    stays the only caller of the risk gate. Today's DayState is re-read WITHOUT this order
    (its client_order_id), with the limits as they are now and the approval's event gate
    (stale once older than max_plan_age_s). The caller holds journal.exclusive().
    An invalid limits config or a proposal without an order is a BLOCK; raises JournalError
    when the journal cannot be read (the caller then sends nothing)."""
    if p.order is None or p.plan_id is None:
        return {"verdict": "BLOCK", "reasons": ["no approved order"]}
    try:
        limits = load_checked_limits()
    except ValueError:
        return {"verdict": "BLOCK", "reasons": ["limits config is invalid"]}
    try:
        cid = client_order_id(p.run_id, p.plan_id)
    except OrderError as exc:
        return {"verdict": "BLOCK", "reasons": [str(exc)]}
    state = journal.day_state(now, exclude=cid)
    return check_order(p.order, limits, state, now, p.gate)


def _raiser(exc: Exception) -> Callable[..., object]:
    def fail(*_: object) -> object:
        raise exc
    return fail


def _prefetch(fn: Callable[..., object], *args: object) -> Callable[..., object]:
    """Call `fn(*args)` now (outside the journal lock, #148) and return a stand-in that gives
    back its result, or raises its exception, when propose() calls it inside the lock."""
    try:
        value = fn(*args)
    except Exception as exc:  # noqa: BLE001 - re-raised inside propose, which refuses
        return _raiser(exc)
    return lambda *_: value


def _prefetched_sources(
    p: Proposal, clock: Clock, quotes: Quotes | None, events: EventSource | None,
) -> tuple[Quotes | None, EventSource | None]:
    """The quote and the event gate for `p`'s symbol, fetched before the lock (#148). Their
    freshness is checked again inside it: build_order refuses a stale quote, check_order a
    stale gate (older than max_plan_age_s, or from another New York day)."""
    symbol = p.plan.symbol if p.plan is not None else ""
    try:  # the clock first, then the sources, in propose's order
        now = clock()
        end = _window_end(now, load_checked_limits()) if events is not None else None
    except Exception as exc:  # noqa: BLE001 - propose refuses the clock or limits itself
        failed = _raiser(exc)
        return (failed if quotes is not None else None,  # type: ignore[return-value]
                failed if events is not None else None)
    q = _prefetch(quotes, symbol) if quotes is not None else None
    e = _prefetch(events, symbol, now, end) if events is not None else None
    return q, e  # type: ignore[return-value]


def approve(
    p: Proposal, ledger: PlanLedger, *, journal: Journal, ask: Ask = input,
    clock: Clock = wall_clock, quotes: Quotes | None = None,
    events: EventSource | None = None, sends: bool = False,
) -> Outcome:
    """Ask a human to approve `p`, re-check it, and journal the result. Never sends an order
    (pipeline/send.py does, after an approval); `sends` only changes the ticket's words.

    A proposal that is not ok is journaled as "no_order" (with its stage) and never asked.

    Only the exact answer "yes" (surrounding spaces ignored, lower case) approves. After the
    answer the plan is re-sized and re-checked at a new `clock()` reading; if that fails, or
    the re-built order differs in any field from the one shown, there is no order.
    Raises JournalError if an entry for a declined, refused, changed or approved outcome
    cannot be written; for an approval nothing then counts as approved. A not-ok proposal's
    "no_order" entry is best effort. The re-check and its journal entry run under
    journal.exclusive() (#107); if that lock cannot be taken, JournalError is raised and
    nothing is approved.
    The quote and the event gate for the re-check are fetched before the lock (#148).
    """
    if not p.ok:
        try:  # audit trail for a refused BUY; a broken journal must not hide the refusal
            journal.append(_entry(p, "no_order", p.reason, stage=p.stage))
        except JournalError:
            pass
        return Outcome("no_order", p.reason, p)
    try:
        answer = ask(render_ticket(p, sends=sends) + "\n" + approval_prompt(p, sends=sends))
    except EOFError:
        answer = ""
    if not isinstance(answer, str) or answer.strip() != "yes":
        journal.append(_entry(p, "declined", "not approved"))
        return Outcome("declined", "not approved", p)

    # #107: the re-check (which reads today's DayState from the journal) and the approved
    # append happen under the journal's cross-process lock, so two concurrent approvers can
    # never both fit into the last trade or the last of the daily loss budget. The network
    # reads happen before the lock (#148), so a slow source never holds it.
    quotes, events = _prefetched_sources(p, clock, quotes, events)
    with journal.exclusive():
        again = propose(ledger, p.plan_id, journal=journal, clock=clock, quotes=quotes,
                        events=events)
        if not again.ok:
            reason = f"re-check failed at {again.stage}: {again.reason}"
            journal.append(
                _entry(p, "refused_after_approval", reason, recheck_at=again.checked_at))
            return Outcome("refused_after_approval", reason, p, again)
        if _order_part(again.sized) != _order_part(p.sized) or again.plan != p.plan:
            reason = "the re-built order differs from the approved ticket"
            journal.append(
                _entry(p, "changed_after_approval", reason, recheck_at=again.checked_at))
            return Outcome("changed_after_approval", reason, p, again)

        approved_at = datetime.fromisoformat(again.checked_at)
        age = _plan_age_s(again.plan, approved_at) if again.plan else None
        journal.append(
            _entry(again, APPROVED, None, approved_at=again.checked_at, plan_age_s=age,
                   client_order_id=client_order_id(again.run_id, again.plan_id),
                   expires_at=again.expires_at))
        return Outcome(APPROVED, None, p, again, again.checked_at, age)
