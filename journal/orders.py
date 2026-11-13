"""The order lifecycle in the decision journal (Phase 5 PR 1, #109).

An approved order and everything that happens to it afterwards are append-only journal
entries keyed by a deterministic `client_order_id` (from the run id and the plan id, so a
retry can never create a second order). Its state is derived by REPLAYING those entries; no
entry is ever rewritten.

    approved_not_sent -> submitting -> submitted | rejected | send_unknown
    send_unknown      -> submitted | rejected | partially_filled | filled | cancelled | expired
    submitted         -> partially_filled | filled | cancelled | expired | rejected
    partially_filled  -> partially_filled | filled | cancelled | expired | closed
    filled            -> closed
    cancelled/expired -> closed (only when part of the order filled: the position remains)
    rejected, closed  -> nothing (final)

Event entries: {"status", "client_order_id", "at": aware ISO time, ...}.
  partially_filled / filled: "filled_qty" (cumulative, 1..qty, never decreasing; filled means
    all of qty) and "avg_price" (> 0).
  cancelled / expired: optional "filled_qty" (cumulative, never decreasing).
  rejected: no fill.
  closed: "closed_by" ("stop", "target" or "flatten") and "realized_pnl" (the whole trade's
    realized profit or loss, negative for a loss).
Risk (decisions on #109): an order's risk is its sized worst-case `max_loss`, counted from
approval until the broker confirms a ZERO fill (rejected, or cancelled/expired with nothing
filled). A closed trade counts the larger of max_loss and its realized loss.

Any malformed entry or impossible sequence raises JournalError: the day's totals cannot be
trusted, so the gate fails closed. Nothing here talks to a broker.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

APPROVED = "approved_not_sent"
SUBMITTING = "submitting"
SUBMITTED = "submitted"
REJECTED = "rejected"
SEND_UNKNOWN = "send_unknown"
PARTIALLY_FILLED = "partially_filled"
FILLED = "filled"
CANCELLED = "cancelled"
EXPIRED = "expired"
CLOSED = "closed"

ORDER_EVENTS = frozenset({SUBMITTING, SUBMITTED, REJECTED, SEND_UNKNOWN, PARTIALLY_FILLED,
                          FILLED, CANCELLED, EXPIRED, CLOSED})
NEXT: dict[str, frozenset[str]] = {
    APPROVED: frozenset({SUBMITTING}),
    SUBMITTING: frozenset({SUBMITTED, REJECTED, SEND_UNKNOWN}),
    SEND_UNKNOWN: frozenset({SUBMITTED, REJECTED, PARTIALLY_FILLED, FILLED, CANCELLED,
                             EXPIRED}),
    SUBMITTED: frozenset({PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, REJECTED}),
    PARTIALLY_FILLED: frozenset({PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, CLOSED}),
    FILLED: frozenset({CLOSED}),
    CANCELLED: frozenset({CLOSED}),
    EXPIRED: frozenset({CLOSED}),
    REJECTED: frozenset(),
    CLOSED: frozenset(),
}
CLOSED_BY = frozenset({"stop", "target", "flatten"})
ZERO = Decimal("0")


class OrderError(ValueError):
    """A malformed order entry or an impossible lifecycle step (the journal wraps it)."""


def client_order_id(run_id: object, plan_id: object) -> str:
    """The deterministic broker order id for one plan of one run: "ta-" + 32 hex characters
    of SHA-256 over the run id and the plan id. The same plan always gets the same id, so a
    resend can be found (and refused) by id. Raises OrderError for an empty or non-string
    part."""
    if not (isinstance(run_id, str) and run_id.strip() and isinstance(plan_id, str)
            and plan_id.strip()):
        raise OrderError("client_order_id needs a run id and a plan id")
    digest = hashlib.sha256(f"{run_id}\x1f{plan_id}".encode()).hexdigest()
    return f"ta-{digest[:32]}"


@dataclass(frozen=True)
class OrderState:
    """One order, as its journal entries replay. Times are aware datetimes."""

    client_order_id: str
    symbol: str
    run_id: str
    plan_id: str
    approved_at: datetime
    qty: int
    max_loss: Decimal
    status: str = APPROVED
    filled_qty: int = 0
    avg_price: Decimal | None = None
    closed_by: str | None = None
    realized_pnl: Decimal | None = None
    last_at: datetime | None = None

    @property
    def released(self) -> bool:
        """The broker confirmed a ZERO fill: rejected, or cancelled/expired with nothing
        filled. Only then does the order stop counting toward the day's risk."""
        return self.status in (REJECTED, CANCELLED, EXPIRED) and self.filled_qty == 0

    @property
    def open(self) -> bool:
        """The order may still hold, or come to hold, a position: anything not closed and
        not released (approved, submitting, submitted, send_unknown, any fill, or a cancelled
        remainder after a partial fill)."""
        return not self.released and self.status != CLOSED

    @property
    def realized_loss(self) -> Decimal:
        """The closed trade's realized loss as a positive amount (0 for a profit or while
        open)."""
        if self.status == CLOSED and self.realized_pnl is not None and self.realized_pnl < 0:
            return -self.realized_pnl
        return ZERO

    @property
    def committed_risk(self) -> Decimal:
        """Risk this order uses from the daily budget: 0 once released, otherwise its sized
        worst-case max_loss, or its realized loss when that was larger (a gap through the
        stop)."""
        if self.released:
            return ZERO
        return max(self.max_loss, self.realized_loss)

    @property
    def stopped_out(self) -> bool:
        return self.status == CLOSED and self.closed_by == "stop"


def _aware(text: object, what: str) -> datetime:
    try:
        when = datetime.fromisoformat(str(text))
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError
        when.astimezone(NY)  # an aware time at the date-range limits cannot be used
    except (ValueError, OverflowError, TypeError):
        raise OrderError(f"{what} is not an aware ISO time") from None
    return when


def _decimal(value: object, what: str, *, positive: bool = False,
             non_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise OrderError(f"{what} is not a number")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OrderError(f"{what} is not a number") from None
    if not d.is_finite() or (positive and d <= 0) or (non_negative and d < 0):
        raise OrderError(f"{what} is out of range")
    return d


def _whole(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrderError(f"{what} is not a whole number")
    return value


def from_approved(entry: dict) -> OrderState:
    """The OrderState an "approved_not_sent" entry opens. A Phase 4 entry without a
    client_order_id gets the one its run id and plan id derive; one that has an id must match
    it. Raises OrderError for a malformed entry."""
    run_id, plan_id = entry.get("run_id"), entry.get("plan_id")
    derived = client_order_id(run_id, plan_id)
    cid = entry.get("client_order_id", derived)
    if cid != derived:
        raise OrderError("approved entry's client_order_id does not match its run and plan")
    order = entry.get("order")
    if not isinstance(order, dict):
        raise OrderError("approved entry has no order")
    qty = _whole(order.get("qty"), "approved order qty")
    if qty < 1:
        raise OrderError("approved order qty must be at least 1")
    symbol = order.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise OrderError("approved order has no symbol")
    return OrderState(
        client_order_id=cid, symbol=symbol.strip().upper(), run_id=run_id, plan_id=plan_id,
        approved_at=_aware(entry.get("approved_at"), "approved_at"), qty=qty,
        max_loss=_decimal(entry.get("max_loss"), "max_loss", non_negative=True))


def apply_event(state: OrderState, entry: dict) -> OrderState:
    """`state` after one event entry, or OrderError for an impossible step or bad fields."""
    status = entry.get("status")
    if status not in ORDER_EVENTS:
        raise OrderError(f"unknown order event {str(status)[:32]!r}")
    if status not in NEXT[state.status]:
        raise OrderError(f"order {state.client_order_id} cannot go from {state.status} to "
                         f"{status}")
    at = _aware(entry.get("at"), f"{status} time")
    filled, avg = state.filled_qty, state.avg_price
    if status in (PARTIALLY_FILLED, FILLED) or (status in (CANCELLED, EXPIRED)
                                                and "filled_qty" in entry):
        filled = _whole(entry.get("filled_qty"), f"{status} filled_qty")
        if not state.filled_qty <= filled <= state.qty:
            raise OrderError(f"{status} filled_qty {filled} must be between "
                             f"{state.filled_qty} and {state.qty}")
        if status in (PARTIALLY_FILLED, FILLED):
            if filled < 1:
                raise OrderError(f"{status} needs a fill")
            avg = _decimal(entry.get("avg_price"), f"{status} avg_price", positive=True)
        if status == FILLED and filled != state.qty:
            raise OrderError(f"filled needs all {state.qty} shares, got {filled}")
        if status == PARTIALLY_FILLED and filled == state.qty:
            raise OrderError("a fill of every share is filled, not partially_filled")
    if status == REJECTED and state.filled_qty:
        raise OrderError("an order with a fill cannot be rejected")
    closed_by, pnl = state.closed_by, state.realized_pnl
    if status == CLOSED:
        if state.filled_qty < 1:
            raise OrderError("only a filled position can be closed")
        closed_by = entry.get("closed_by")
        if closed_by not in CLOSED_BY:
            raise OrderError(f"closed_by must be one of {sorted(CLOSED_BY)}")
        pnl = _decimal(entry.get("realized_pnl"), "realized_pnl")
    return replace(state, status=status, filled_qty=filled, avg_price=avg,
                   closed_by=closed_by, realized_pnl=pnl, last_at=at)


def replay(entries: Iterable[dict]) -> dict[str, OrderState]:
    """Every order in `entries` (oldest first), by client_order_id. Raises OrderError for a
    malformed approved or event entry, an event for an unknown order, an impossible step, or
    the same order approved twice. Entries that are neither (pass, declined, no_order ...)
    are ignored."""
    orders: dict[str, OrderState] = {}
    for n, entry in enumerate(entries, 1):
        status = entry.get("status")
        try:
            if status == APPROVED:
                state = from_approved(entry)
                if state.client_order_id in orders:
                    raise OrderError(f"order {state.client_order_id} approved twice")
                orders[state.client_order_id] = state
            elif status in ORDER_EVENTS:
                cid = entry.get("client_order_id")
                if not isinstance(cid, str) or cid not in orders:
                    raise OrderError(f"{status} event for an unknown order")
                orders[cid] = apply_event(orders[cid], entry)
        except OrderError as exc:
            raise OrderError(f"entry {n}: {exc}") from None
    return orders
