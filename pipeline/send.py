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
from tools.broker import BrokerError, bracket_request

# HTTP statuses after which Alpaca has definitely not accepted the order. Anything else
# (no status, 408, 429, 5xx) may have created it, so it is looked up, never resent.
DEFINITE_REFUSALS = frozenset({400, 401, 403, 404, 422})

NOT_SENT = "not_sent"  # nothing reached the broker (the approval is journaled expired)
JOURNAL_RECORD_ATTEMPTS = 2  # a post-send journal entry is written with one retry


@dataclass(frozen=True)
class SendResult:
    """What happened to one approved order.

    status: "submitted", "rejected", "send_unknown" (the broker may hold the order; PR 4's
    reconciler settles it) or "not_sent" (nothing was POSTed). `alert` is True when a human
    must look now: the outcome at the broker is unknown or does not match, or a post-send
    journal entry could not be written. `journaled` is False when the journal entry for this
    result could not be written (the journal then still reads "submitting", which keeps the
    order's slot and risk counted)."""

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
    limit entry and limit price."""
    try:
        qty = str(Decimal(str(reply.get("qty"))).normalize())
        limit = Decimal(str(reply.get("limit_price")))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return (reply.get("client_order_id") == body["client_order_id"]
            and reply.get("symbol") == body["symbol"] and reply.get("side") == "buy"
            and qty == body["qty"] and reply.get("order_class") == "bracket"
            and reply.get("type") == "limit" and limit == Decimal(body["limit_price"]))


def _alert(message: str) -> None:
    """Print an ALERT line for the human at the console (PR 6 adds the desktop notice)."""
    print(f"ALERT: {message}", file=sys.stderr)


def _now(clock: Clock) -> datetime:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise JournalError("clock must be timezone-aware")
    return now


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
            journal.record_order_event(cid, status, _now(clock), **fields)
            return result
        except JournalError as exc:
            last = exc
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
            if current is None or current.status != APPROVED:
                state = current.status if current is not None else "missing"
                return SendResult(NOT_SENT, f"the order is {state}, not an unsent approval",
                                  cid)
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
                          f"sent: {exc}", cid)

    # 3. The one POST, outside the lock. Never resent.
    try:
        reply = broker.submit_bracket(order, cid)
    except Exception as exc:  # noqa: BLE001 - every failure is resolved by lookup below
        return _after_failure(journal, broker, cid, body, clock, exc, verdict)
    if not isinstance(reply, dict) or not _matches(reply, body):
        return _after_failure(journal, broker, cid, body, clock,
                              BrokerError("the broker's reply does not match the request"),
                              verdict)
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
