"""Send one approved order to the Alpaca PAPER account (Phase 5 PR 3: #187, #136, #148).

The only input is an approval Outcome from pipeline.order_flow.approve(): its re-checked
Proposal carries the tool-built order and the event-gate result. Nothing here computes a
price, a stop, a target or a size (CLAUDE.md rule 2); the broker request is the allow-list
mapping in tools.broker.bracket_request (a LIMIT entry at entry + slippage allowance).

Flow (#187):
  1. Before the lock: the request is built (a malformed order is never sent) and the paper
     account is verified. Both may refuse; the approval is then journaled "expired"
     (filled_qty 0, with the reason), so it can never be sent later.
  2. Under journal.exclusive(): the order must still be an unsent approval whose plan has not
     expired. Today's DayState is re-read WITHOUT this order and check_order runs again (kill
     switch, paper, market hours, max_trades_per_day, open positions, the day's risk, the
     daily loss stop, stop-outs, the event gate's freshness). A BLOCK journals "expired".
     A PASS journals "submitting" BEFORE any HTTP call (write-ahead); if the journal refuses
     it (a late send after the plan expired, #186) or cannot write it, nothing is sent.
  3. The lock is released and the bracket is POSTed once. The order is NEVER resent:
       - a reply that matches the request -> "submitted" (with the broker's order id);
       - any failure -> look the order up by its client_order_id:
           found and matching            -> "submitted";
           found but not matching        -> cancel it (best effort), "send_unknown", ALERT;
           not found after a definite refusal (HTTP 400/401/403/404/422) -> "rejected";
           anything else (a timeout, a network error, 5xx, 429, a failed lookup)
                                         -> "send_unknown", ALERT.
     A "submitting" or "send_unknown" order keeps its position slot and its worst-case risk
     in day_state until the reconciler (PR 4) settles it with the broker.

Not here (PR 4, the reconciler): the 60 s cancel of an unfilled entry (#136), fills and
positions (#155), and Ivan's protective-stop rule: every open position must always have a
working stop covering its full quantity, else it is flattened at market and alerted.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from journal.log import Journal, JournalError
from journal.orders import (
    APPROVED,
    EXPIRED,
    REJECTED,
    SEND_UNKNOWN,
    SUBMITTED,
    SUBMITTING,
    OrderError,
    client_order_id,
)
from pipeline.order_flow import Clock, Outcome, check_before_send, wall_clock
from tools.broker import (
    DEFINITE_REFUSALS,  # Alpaca definitely did not accept the order (else: look it up)
    BrokerError,
    bracket_legs_problem,
    bracket_request,
)

NOT_SENT = "not_sent"  # this call POSTed nothing (`journaled`: the approval was expired)
ALREADY_SENT = "already_sent"  # the order was sent before (the broker may hold it)
JOURNAL_RECORD_ATTEMPTS = 2  # a post-send journal entry is written with one retry


@dataclass(frozen=True)
class SendResult:
    """What happened to one approved order.

    status:
      - "submitted": the broker holds the order (PR 4's reconciler follows it from there,
        including an asynchronous reject or cancel);
      - "rejected": a definite refusal, and the lookup found no order;
      - "send_unknown": the broker may hold the order; the reconciler settles it;
      - "not_sent": THIS call POSTed nothing. `journaled` True: the approval is journaled
        expired and can never be sent; False: no entry was written (the journal failed, or
        the outcome does not match the journaled approval) and the approval stays unsent
        until its plan expires;
      - "already_sent": the journal shows the order was sent before (submitting or later), so
        nothing is POSTed again; the broker may hold it.
    `alert` is True when a human must look now: the outcome at the broker is unknown or does
    not match, or a post-send journal entry could not be written (the journal then still
    reads "submitting", which keeps the order's slot and risk counted)."""

    status: str
    reason: str | None
    client_order_id: str | None
    broker_order_id: str | None = None
    verdict: dict | None = None
    alert: bool = False
    journaled: bool = True
    details: dict[str, Any] = field(default_factory=dict)


def _matches(reply: dict, body: dict) -> bool:
    """The broker's order is the one in the request: id, symbol, side, quantity, bracket,
    limit entry and limit price, and both legs at the plan's target and stop
    (tools.broker.bracket_legs_problem): an entry without its stop never reads as sent."""
    try:
        qty = Decimal(str(reply.get("qty")))  # as a number: "10" may come back as "10.0"
        limit = Decimal(str(reply.get("limit_price")))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return (reply.get("client_order_id") == body["client_order_id"]
            and reply.get("symbol") == body["symbol"] and reply.get("side") == "buy"
            and qty == Decimal(body["qty"]) and reply.get("order_class") == "bracket"
            and reply.get("type") == "limit" and limit == Decimal(body["limit_price"])
            and bracket_legs_problem(reply, body) is None)


def _alert(message: str) -> None:
    """Print an ALERT line for the human at the console (PR 6 adds the desktop notice)."""
    print(f"ALERT: {message}", file=sys.stderr)


def _now(clock: Clock) -> datetime:
    """`clock()`, or JournalError for a clock that raises or is not timezone-aware."""
    try:
        now = clock()
    except Exception as exc:  # noqa: BLE001 - a broken clock must not raise out of the sender
        raise JournalError(f"clock failed ({type(exc).__name__})") from None
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise JournalError("clock must be timezone-aware")
    return now


def _event_time(clock: Clock) -> datetime:
    """The time for a post-send event: the clock, or the wall clock when the injected clock
    fails, so a sent order's broker id is never lost to a clock error."""
    try:
        return _now(clock)
    except JournalError:
        return wall_clock()


def _approved_order(journal: Journal, cid: str) -> object:
    """The order dict the human approved, as journaled for this client_order_id (an entry
    without one gets the id its run and plan derive, as journal.orders does)."""
    for e in journal.entries():
        if e.get("status") != APPROVED:
            continue
        try:
            own = e.get("client_order_id") or client_order_id(e.get("run_id"), e.get("plan_id"))
        except OrderError:
            continue
        if own == cid:
            return e.get("order")
    return None


def _not_sent(journal: Journal, cid: str, clock: Clock, reason: str,
              verdict: dict | None = None) -> SendResult:
    """Journal the unsent approval as expired (filled_qty 0: nothing was sent), so it can
    never be sent later, and say so. A journal failure here still means nothing was sent."""
    journaled = True
    try:
        journal.record_order_event(cid, EXPIRED, _now(clock), filled_qty=0,
                                   reason=f"not sent: {reason}")
    except JournalError as exc:
        journaled = False
        reason = f"{reason} (and the journal entry failed: {exc})"
    return SendResult(NOT_SENT, reason, cid, verdict=verdict, journaled=journaled)


def _record(journal: Journal, cid: str, status: str, clock: Clock, result: SendResult,
            **fields: object) -> SendResult:
    """Journal a post-send event (one retry). If it cannot be written, the journal still
    reads "submitting" (slot and risk counted), and the result raises an ALERT."""
    last: Exception | None = None
    for _ in range(JOURNAL_RECORD_ATTEMPTS):
        try:
            journal.record_order_event(cid, status, _event_time(clock), **fields)
            return result
        except JournalError as exc:
            last = exc
            # the reconciler (#155) follows the order too and may have recorded the
            # broker's answer first: then the journal no longer reads "submitting"
            try:
                current = journal.orders().get(cid)
            except JournalError:
                current = None
            if current is not None and current.status not in (SUBMITTING, APPROVED):
                if current.status == status:
                    return result
                reason = (f"{result.reason or status}; the journal already reads "
                          f"{current.status} (the reconciler recorded the broker's answer)")
                _alert(f"order {cid}: {reason}")
                return SendResult(current.status, reason, cid, result.broker_order_id,
                                  result.verdict, alert=True, details=result.details)
    reason = (f"{result.reason or status}; the {status} event could NOT be journaled "
              f"({last}), the journal still reads submitting")
    _alert(f"order {cid}: {reason}")
    return SendResult(result.status, reason, cid, result.broker_order_id, result.verdict,
                      alert=True, journaled=False, details=result.details)


def send_approved(outcome: Outcome, *, journal: Journal, broker: Any,
                  clock: Clock = wall_clock) -> SendResult:
    """Send the order `outcome` approved to the paper broker, at most once. Never raises for
    a broker, journal or clock failure: the result says what happened (see the module
    docstring for the flow and the statuses)."""
    p = outcome.recheck
    if outcome.status != APPROVED or p is None or p.order is None or p.plan_id is None:
        return SendResult(NOT_SENT, "nothing was approved", None)
    order = p.order
    try:
        cid = client_order_id(p.run_id, p.plan_id)
    except OrderError as exc:
        return SendResult(NOT_SENT, str(exc), None)

    # 1. Outside the lock: the request and the paper-account check (network, #148).
    try:
        body = bracket_request(order, cid)
    except BrokerError as exc:
        return _not_sent(journal, cid, clock, f"the broker request cannot be built: {exc}")
    try:
        broker.verify_paper_account()
    except Exception as exc:  # noqa: BLE001 - any failure means the account is not verified
        return _not_sent(journal, cid, clock,
                         f"the paper account check failed ({type(exc).__name__}: {exc})")

    # 2. Under the lock: re-check the day without this order, then write-ahead "submitting".
    try:
        with journal.exclusive():
            now = _now(clock)
            current = journal.orders().get(cid)
            if current is None:
                return SendResult(NOT_SENT, "no approval in the journal for this order", cid,
                                  journaled=False)
            if current.sent:
                return SendResult(ALREADY_SENT, f"the order was already sent (now "
                                  f"{current.status}); nothing is sent again", cid)
            if current.status != APPROVED:
                return SendResult(NOT_SENT, f"the order is {current.status}, not an unsent "
                                  "approval", cid, journaled=False)
            if _approved_order(journal, cid) != order:
                # only the exact order the human approved is ever sent
                return SendResult(NOT_SENT, "the order differs from the journaled approval",
                                  cid, journaled=False)
            if not current.live_unsent(now):
                return _not_sent(journal, cid, clock, "its plan expired before it was sent")
            verdict = check_before_send(p, journal=journal, now=now)
            if verdict.get("verdict") != "PASS":
                why = "; ".join(verdict.get("reasons", [])) or "blocked"
                return _not_sent(journal, cid, clock,
                                 f"the risk re-check at sending blocked it: {why}", verdict)
            journal.record_order_event(cid, SUBMITTING, now, limit_price=body["limit_price"])
    except JournalError as exc:
        # The write-ahead entry was refused (a late send, #186) or the journal failed:
        # without "submitting" on record, nothing is sent.
        return SendResult(NOT_SENT, f"journal refused or failed before sending, nothing "
                          f"sent: {exc}", cid, journaled=False)

    # 3. The one POST, outside the lock. Never resent.
    try:
        reply = broker.submit_bracket(order, cid)
    except Exception as exc:  # noqa: BLE001 - every failure is resolved by lookup below
        return _after_failure(journal, broker, cid, body, clock, exc, verdict)
    if not isinstance(reply, dict) or not _matches(reply, body):
        return _after_failure(journal, broker, cid, body, clock,
                              BrokerError("the broker's reply does not match the request"),
                              verdict)
    # A reply with status rejected or canceled (an asynchronous refusal) is still journaled
    # "submitted": its risk stays counted until the reconciler (PR 4) records what happened.
    result = SendResult(SUBMITTED, None, cid, reply.get("id"), verdict,
                        details={"broker_status": reply.get("status")})
    return _record(journal, cid, SUBMITTED, clock, result, broker_order_id=reply.get("id"),
                   broker_status=reply.get("status"))


def _after_failure(journal: Journal, broker: Any, cid: str, body: dict, clock: Clock,
                   exc: Exception, verdict: dict | None) -> SendResult:
    """The POST failed or its reply did not match: find out by client_order_id what the
    broker holds. Never resends."""
    status = exc.status if isinstance(exc, BrokerError) else None
    code = exc.code if isinstance(exc, BrokerError) else None
    what = f"{type(exc).__name__}: {exc}"
    try:
        found = broker.get_order_by_client_id(cid)
    except Exception as lookup_exc:  # noqa: BLE001
        reason = (f"send failed ({what}) and the lookup by client_order_id failed "
                  f"({type(lookup_exc).__name__}: {lookup_exc}); the broker may hold it")
        _alert(f"order {cid}: {reason}")
        return _record(journal, cid, SEND_UNKNOWN, clock,
                       SendResult(SEND_UNKNOWN, reason, cid, verdict=verdict, alert=True),
                       reason=reason)
    if isinstance(found, dict) and _matches(found, body):
        reason = f"send reported {what}, but the broker holds the order"
        return _record(journal, cid, SUBMITTED, clock,
                       SendResult(SUBMITTED, reason, cid, found.get("id"), verdict,
                                  details={"broker_status": found.get("status")}),
                       broker_order_id=found.get("id"), broker_status=found.get("status"),
                       reason=reason)
    if isinstance(found, dict):
        cancelled = False
        try:
            broker.cancel_order(found.get("id"))
            cancelled = True
        except Exception:  # noqa: BLE001 - best effort; the reconciler settles the rest
            pass
        reason = (f"the broker holds an order under this client_order_id that does not "
                  f"match the request (send: {what}); "
                  f"{'cancel requested' if cancelled else 'cancel FAILED'}")
        _alert(f"order {cid}: {reason}")
        return _record(journal, cid, SEND_UNKNOWN, clock,
                       SendResult(SEND_UNKNOWN, reason, cid, found.get("id"), verdict,
                                  alert=True), broker_order_id=found.get("id"), reason=reason)
    if found is None and status in DEFINITE_REFUSALS:
        reason = f"rejected by the broker ({what})"
        return _record(journal, cid, REJECTED, clock,
                       SendResult(REJECTED, reason, cid, verdict=verdict,
                                  details={"http_status": status, "error_code": code}),
                       http_status=status, error_code=code, reason=reason)
    reason = f"send failed ({what}) and the order was not found; the broker may still hold it"
    _alert(f"order {cid}: {reason}")
    return _record(journal, cid, SEND_UNKNOWN, clock,
                   SendResult(SEND_UNKNOWN, reason, cid, verdict=verdict, alert=True),
                   reason=reason)
