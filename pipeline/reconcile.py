"""The reconciler (Phase 5 PR 4, #155): keeps the journal in line with the Alpaca PAPER
broker, and enforces Ivan's protective-stop rule. Plan: the approved comment on #155
(2026-11-23) with Ivan's answers.

    python -m pipeline.reconcile --check   # read only: what it sees and what it WOULD do
    python -m pipeline.reconcile --once    # one acting pass (Ivan only)

run_day runs it in a thread every POLL_S seconds and once before each decision; it writes
its last-pass time to the heartbeat, and run_day stops new entries when that is more than
STALE_S old or the thread died.

One pass:
  1. read the journal's orders, the broker's open orders (one by one, #246) and positions;
  2. follow every journaled order not final: look it up by client_order_id WITH ITS LEGS
     and record what changed (submitted, partially_filled, filled, cancelled / expired with
     the broker's filled_qty, rejected), stamped with LOCAL receipt time (#202). A fill
     above the order's limit price blocks;
  3. an entry unfilled UNFILLED_CANCEL_S after it was submitted is cancelled (#136). A
     partial fill gets one poll of grace (PARTIAL_GRACE_S); then the remainder is cancelled
     and confirmed. It never flattens while the entry can still fill;
  4. every position must have a working stop for its full quantity, judged ONLY from the
     entry's lookup with legs (tools.protection; the listing hides a held stop, #196).
     A stop that triggered and is selling gets EXECUTING_WAIT_S; then alert and flatten.
     Anything unprotected is blocked, alerted and flattened (pipeline.flatten), except
     (Ivan's decision on #251) a partial fill whose legs Alpaca cancelled with the unfilled
     remainder: flattened with an alert, and blocked only if the close is not confirmed;
  5. a trade whose position is gone is closed in the journal with its realized P&L
     (tools.realized) from the broker's exit fills;
  6. orphans (a position or order not traceable to the journal; the paper account is
     dedicated to this project, no hand-placed orders) and unreadable orders block new
     entries and alert; orphans are flattened, an unreadable order is never acted on.

The flat-by (PR 5, #135): at and after the day's flat-by time (pipeline.flatten.flat_by_at:
15:55 ET, 12:55 on an early close), or with flat_now (python -m pipeline.flatten --now),
every pass also cancels each of today's working entries (and confirms it), and flattens each
of today's positions with pipeline.flatten.flatten_symbol, whatever its stop. Legs the
flat-by cancelled itself are no reason for an "unprotected" block. Not flat yet: the alert
repeats every pass, and after LOUD_S it says MANUAL ACTION NEEDED. An earlier day's position
is never closed: blocked and alerted (decision 8). With today_only (pipeline.flatten's CLI,
which has no start-of-day broker check), anything in a symbol with no order sent today counts
as an earlier day's. After the close (or on a day with no session) nothing is cancelled or
closed: a market close would wait for the next open and fill with nobody deciding (#135
review); it alerts MANUAL ACTION NEEDED instead.

Blocks (journal "reconcile_block", #189) stop every new entry (DayState.reconcile_blocks,
risk_check) until a human resolves each one: `python -m journal.resolve`.

After any unclear broker reply (may_have_acted), the state is read again before acting:
nothing is resent, re-cancelled or re-closed blindly. Paper only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from journal.log import (
    RECONCILE_ALERT,
    Journal,
    JournalError,
    JournalLockTimeout,
    unresolved_blocks,
)
from journal.orders import (
    APPROVED,
    CANCELLED,
    CLOSED,
    EXPIRED,
    FILLED,
    PARTIALLY_FILLED,
    REJECTED,
    SEND_UNKNOWN,
    SUBMITTED,
    SUBMITTING,
    OrderError,
    OrderState,
    client_order_id,
    replay,
)
from pipeline.flatten import (
    CLOSING,
    FLAT,
    SESSION_OVER,
    SKIPPED_UNREADABLE,
    FlattenResult,
    flat_by_reached,
    flatten_symbol,
)
from tools.broker import ACTION_LOCK, FINAL_STATUSES, BrokerError, is_close_order
from tools.market_calendar import CalendarError, day_times, session_hours
from tools.protection import (
    EXECUTING,
    PENDING,
    PROTECTED,
    UNPROTECTED,
    entry_can_fill,
    stop_status,
)
from tools.realized import RealizedError, realized_pnl

# The reconciler is ready for run_day (with pipeline.flatten's flag: run_day.broker_allowed).
RUN_DAY_READY = True
FINAL = FINAL_STATUSES
DONE_THIS_PASS = "done this pass"  # _flatten: the symbol was already flattened this pass

NY = ZoneInfo("America/New_York")
POLL_S = 15
STALE_S = 45
UNFILLED_CANCEL_S = 60
PARTIAL_GRACE_S = 15
EXECUTING_WAIT_S = 60
LOUD_S = 60  # a flatten not done this long: MANUAL ACTION NEEDED
LEGS_WITH_REMAINDER_S = 5  # legs ended this close to the entry: cancelled with it (#251)
LOST_SEND_GRACE_S = 120
RETRY_ALERT_S = 120
EARLIER_DAY = ("an order from an earlier day: not closed automatically (decision 8); "
               "check it in the Alpaca paper dashboard")
FOLLOW = frozenset({SUBMITTING, SUBMITTED, SEND_UNKNOWN, PARTIALLY_FILLED, FILLED})
BEFORE_PREVIOUS = "before the order's previous event"  # journal/orders.py (#194): retryable
MANUAL = ("MANUAL ACTION NEEDED in the Alpaca paper dashboard: cancel {symbol}'s orders and "
          "close the {symbol} position")


def _alert_print(message: str) -> None:
    print(f"ALERT: {message}", file=sys.stderr)


def _dec(value: object) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _when(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


class Reconciler:
    """One reconcile pass at a time (pass_once). `check_only`: read only, never writes the
    journal and never sends, cancels or closes; the report lists what it WOULD do."""

    def __init__(self, journal: Journal, broker: Any, *, clock: Callable[[], datetime],
                 limits: Any = None, check_only: bool = False, flat_now: bool = False,
                 today_only: bool = False,
                 alert: Callable[[str], None] = _alert_print,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.journal, self.broker, self.clock, self.limits = journal, broker, clock, limits
        self.check_only = check_only
        # flat_now: every pass is a flat-by pass, whatever the time (pipeline.flatten --now)
        self.flat_now = flat_now
        # today_only: a position or order in a symbol with no order sent today is an earlier
        # day's (decision 8), never an orphan to flatten (pipeline.flatten's CLI, #135 review)
        self.today_only = today_only
        self.cancel_sent: set[str] = set()  # entries whose cancel was sent in this pass
        self.flat_by_mode = False  # this pass flattens today's positions (#135)
        self._print_alert, self.sleep, self.monotonic = alert, sleep, monotonic
        # timers that span passes (in memory: a restart only waits longer, never less)
        self.not_found: dict[str, int] = {}  # cid -> consecutive 404 lookups
        self.partial_since: dict[str, datetime] = {}
        self.executing_since: dict[str, datetime] = {}
        self.activating_since: dict[str, datetime] = {}
        # Ivan's decision on #251: a partial fill whose legs Alpaca cancelled with the
        # remainder is flattened with an alert, and blocked only if that flatten is not
        # CONFIRMED (cid -> when the flatten started, its symbol, the reason)
        self.partial_flatten: dict[str, tuple[datetime, str, str]] = {}
        self.final = False  # this pass is the last (pass_once(final=True))
        self.stuck_since: dict[str, datetime] = {}  # symbol -> a flatten not done since
        self.retry_since: dict[str, datetime] = {}
        self.latest: dict[str, dict | None] = {}  # a lookup made after acting (a cancel)
        self.flattened: set[str] = set()  # symbols flattened in this pass
        self.limits_by_cid: dict[str, Decimal] = {}
        self.earlier_day: set[str] = set()  # symbols an earlier day's order may hold
        self.known_cids: set[str] = set()
        self.report: dict[str, Any] = {}

    # ---- outputs ----------------------------------------------------------------------
    def _now(self) -> datetime:
        return self.clock().astimezone(NY)

    def _alert(self, message: str, *, symbol: str | None = None) -> None:
        self.report["alerts"].append(message)
        if self.check_only:
            return
        self._print_alert(message)
        try:
            self.journal.append({"status": RECONCILE_ALERT, "at": self._now().isoformat(),
                                 "symbol": symbol, "message": message[:400]})
        except JournalError as exc:
            self._print_alert(f"the alert could not be journaled ({exc})")

    def _would(self, what: str) -> None:
        self.report["would"].append(what)

    def _block(self, kind: str, key: str, reason: str, **fields: object) -> bool:
        """Open a block (once per key). True when it is open: written now, or already open.
        False when it could not be written (the caller retries next pass)."""
        if self.check_only:
            self._would(f"block new entries ({kind}): {reason}")
            return False
        try:
            block_id = self.journal.add_block(kind, key, reason, self._now(), **fields)
        except JournalError as exc:
            self._alert(f"could not journal a block ({kind}: {reason}): {exc}")
            return False
        if block_id is not None:
            self.report["blocks"].append(block_id)
            self._alert(f"new entries blocked ({block_id} {kind}): {reason}. Resolve with "
                        f"python -m journal.resolve {block_id}", symbol=fields.get("symbol"))
        return True

    def _block_open(self, key: str) -> bool | None:
        """An unresolved block with this key is in the journal (None: cannot tell). Read
        without the journal's lock: never waits on it while holding ACTION_LOCK."""
        try:
            entries = self.journal._read_entries()  # noqa: SLF001 - read only, lock-free
        except JournalError:
            return None
        return any(b.get("key") == key for b in unresolved_blocks(entries))

    def _event(self, o: OrderState, status: str, **fields: object) -> bool:
        """Journal one order event at local receipt time. False when it was not written."""
        what = f"{o.client_order_id} {o.symbol}: {status}"
        if self.check_only:
            self._would(f"journal {what} {fields}")
            return False
        try:
            self.journal.record_order_event(o.client_order_id, status, self._now(), **fields)
        except JournalLockTimeout:  # busy (an approval, the sender): the next pass retries
            first = self.retry_since.setdefault(f"lock:{o.client_order_id}", self._now())
            if self._now() - first >= timedelta(seconds=RETRY_ALERT_S):
                self._alert(f"{what}: the journal has been busy since {first:%H:%M:%S}",
                            symbol=o.symbol)
            return False
        except JournalError as exc:
            if BEFORE_PREVIOUS in str(exc):  # a clock step (#202): retry; alert if it lasts
                first = self.retry_since.setdefault(o.client_order_id, self._now())
                if self._now() - first >= timedelta(seconds=RETRY_ALERT_S):
                    self._alert(f"{what} keeps being refused as before the order's previous "
                                f"event since {first:%H:%M:%S}: check the PC's clock",
                                symbol=o.symbol)
                return False
            if self._moved_on(o):
                # the journal changed since this pass read it (the sender recorded the
                # same broker fact first): not a conflict; the next pass reads it fresh
                self.report["recorded"].append(f"{what}: already recorded elsewhere")
                return False
            self._block("conflict", f"conflict:{o.client_order_id}:{status}",
                        f"the journal refused the broker's {status} for {o.client_order_id} "
                        f"({o.symbol}): {exc}", symbol=o.symbol,
                        client_order_id=o.client_order_id, broker_fields=_safe(fields))
            return False
        self.retry_since.pop(o.client_order_id, None)
        self.retry_since.pop(f"lock:{o.client_order_id}", None)
        self.report["recorded"].append(what)
        return True

    def _persisting(self, key: str, message: str, symbol: str | None = None) -> None:
        """A failure that the next pass retries: recorded, and alerted once it has lasted
        RETRY_ALERT_S (run_day never shows the report, so nothing may stay quiet)."""
        self.report["errors"].append(message)
        first = self.retry_since.setdefault(key, self._now())
        if self._now() - first >= timedelta(seconds=RETRY_ALERT_S):
            self._alert(f"{message} (since {first:%H:%M:%S})", symbol=symbol)

    def _moved_on(self, o: OrderState) -> bool:
        """The order's journal state differs from this pass's snapshot of it."""
        try:
            now_state = self.journal.orders().get(o.client_order_id)
        except JournalError:
            return True  # cannot tell: retry next pass rather than block on a guess
        return now_state != o

    def _cancel(self, order_id: str, what: str) -> None:
        if self.check_only:
            self._would(f"cancel {what} ({order_id})")
            return
        try:
            self.broker.cancel_order(order_id)
            self.report["actions"].append(f"cancel {what} ({order_id})")
            self.retry_since.pop(f"cancel:{order_id}", None)
        except BrokerError as exc:  # never repeated blindly: the next lookup says
            self._persisting(f"cancel:{order_id}", f"cancel {what} ({order_id}) failed: {exc}")

    # ---- one pass ---------------------------------------------------------------------
    def pass_once(self, *, final: bool = False) -> dict:
        """One pass. `final`: no later pass will confirm anything (a --once pass, run_day's
        last pass): a partial-fill flatten still unconfirmed at its end blocks."""
        self.final = final
        return self._pass()

    def _pass(self) -> dict:
        now = self._now()
        self.flat_by_mode = self.flat_now or (self.limits is not None
                                              and flat_by_reached(now, self.limits))
        self.report = {"at": now.isoformat(), "mode": "check" if self.check_only else "act",
                       "flat_by": self.flat_by_mode, "complete": False, "orders": [],
                       "positions": [], "unreadable": [], "recorded": [], "actions": [],
                       "would": [], "blocks": [], "alerts": [], "errors": [], "read_errors": []}
        try:
            self._run(now)
        finally:
            if not self.check_only:  # a failed read or an error never skips it
                self._confirm_partial_flattens(now, final=self.final)
        reads = self.report["read_errors"]
        self.report["complete"] = not reads
        if reads:  # a pass that could not see everything is not a pass (run_day's health)
            self._alert(f"the reconcile pass could not read everything ({len(reads)}: "
                        f"{reads[0]}); positions may be unwatched")
        return self.report

    def _read_journal(self) -> tuple[dict[str, OrderState], list[dict]]:
        """The journal's entries and orders. --check reads without the journal's lock, so it
        never contends with run_day (a cut-off last line still raises)."""
        if self.check_only:
            entries = self.journal._read_entries()  # noqa: SLF001 - read only, lock-free
            try:
                return replay(entries), entries
            except OrderError as exc:
                raise JournalError(f"journal order history is invalid: {exc}") from None
        return self.journal.orders(), self.journal.entries()

    def _run(self, now: datetime) -> None:
        self.flattened: set[str] = set()
        self.cancel_sent = set()
        reads = self.report["read_errors"]
        try:  # the reconciler's own client, verified every pass (#155 review)
            self.broker.verify_paper_account()
        except BrokerError as exc:
            reads.append(f"the paper account check failed: {exc}")
            return
        try:
            orders, entries = self._read_journal()
        except JournalError as exc:
            reads.append(f"the journal cannot be read: {exc}")
            return
        today = now.date()
        # before anything can act: the symbols an earlier day's order may hold (decision 8)
        # sent and still open, as run_day's held check (held_from_earlier_days): an order the
        # journal released (cancelled or expired with no fill) holds nothing (#155 review)
        self.earlier_day = {o.symbol for o in orders.values()
                            if o.sent and o.open
                            and o.approved_at.astimezone(NY).date() < today}
        approved = _approved_orders(entries)
        self.limits_by_cid = _limit_prices(entries)
        listing = positions = None
        try:
            listing = self.broker.list_open_orders_checked()
        except BrokerError as exc:
            reads.append(f"open orders: {exc}")
        try:
            positions = {p["symbol"]: p for p in self.broker.list_positions()}
            self.report["positions"] = [{k: p.get(k) for k in ("symbol", "side", "qty",
                                                               "qty_available")}
                                        for p in positions.values()]
        except BrokerError as exc:
            reads.append(f"positions: {exc}")
        if self.today_only:
            sent_today = {o.symbol for o in orders.values()
                          if o.sent and o.approved_at.astimezone(NY).date() == today}
            seen = set(positions or {}) | {o["symbol"] for o in (listing or {}).get("orders", [])}
            self.earlier_day |= seen - sent_today

        # unreadable orders: block and alert; never act on them (#246)
        for u in (listing or {}).get("unreadable", []):
            self.report["unreadable"].append(u)
            self._block("unreadable_order",
                        f"unreadable:{u.get('id') or u.get('symbol') or u.get('why')}",
                        f"an open order cannot be read (id {u.get('id')}, symbol "
                        f"{u.get('symbol')}: {u.get('why')}); the account is dedicated to "
                        "this project, so nothing should be there", symbol=u.get("symbol"))

        # follow the journal's orders
        looked: dict[str, dict | None] = {}
        lookup_failed = False
        for o in orders.values():
            if not _followed(o, now):
                continue
            try:
                found = self.broker.get_order_by_client_id(o.client_order_id)
            except BrokerError as exc:
                lookup_failed = True
                reads.append(f"lookup {o.client_order_id}: {exc}")
                continue
            looked[o.client_order_id] = found
            self.report["orders"].append({"client_order_id": o.client_order_id,
                                          "symbol": o.symbol, "journal": o.status,
                                          "broker": found["status"] if found else None,
                                          "filled_qty": found["filled_qty"] if found else None})
            self._settle(o, found, now, self.limits_by_cid.get(o.client_order_id))
            if self.flat_by_mode:  # #135: no new entries from the flat-by on
                current = self.latest.get(o.client_order_id, found)
                if (current is not None and entry_can_fill(current)
                        and o.client_order_id not in self.cancel_sent):
                    self._cancel_entry(o, current, "the flat-by time: no new entries (#135)")
            # after a cancel, judge by what the broker shows now, not before it
            looked[o.client_order_id] = self.latest.pop(o.client_order_id, found)

        if not self.check_only:
            try:
                orders = self.journal.orders()  # after this pass's events
            except JournalError as exc:
                reads.append(f"the journal cannot be read after recording: {exc}")
                return
        holding: dict[str, list[OrderState]] = {}  # recorded fills, not closed
        owners: dict[str, list[OrderState]] = {}  # every sent order that may hold shares
        for o in orders.values():
            if o.filled_qty > 0 and o.status in (PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED):
                holding.setdefault(o.symbol, []).append(o)
            # a fill the journal has not recorded yet (a clock step, a lock timeout) still
            # owns its position: never flattened as an orphan for the journal lagging
            if o.sent and o.status not in (CLOSED, REJECTED) and (
                    o.filled_qty > 0 or o.status in (SUBMITTING, SUBMITTED, SEND_UNKNOWN)):
                owners.setdefault(o.symbol, []).append(o)

        self.earlier_day |= {o.symbol for trades in owners.values() for o in trades
                             if o.approved_at.astimezone(NY).date() < today}
        self.known_cids = set(orders)
        if positions is not None:
            for sym in list(self.stuck_since):
                if sym not in positions:
                    self.stuck_since.pop(sym, None)  # flat now: a later flatten starts fresh
            held_cids = {o.client_order_id for trades in owners.values() for o in trades
                         if o.symbol in positions}
            for cid in list(self.activating_since):
                if cid not in held_cids:
                    self.activating_since.pop(cid, None)
            for sym, pos in positions.items():
                self._protect(sym, pos, owners.get(sym, []), looked, approved, now, today)
            for sym, trades in holding.items():
                if sym not in positions:
                    for o in trades:
                        self._close(o, looked.get(o.client_order_id), now)

        # (the partial-fill check runs in _pass, whatever stops _run)

        # orphan orders: open, readable, and not ours (skipped when a lookup failed: an
        # order we could not look up would read as an orphan)
        if listing is not None and not lookup_failed:
            known = set()
            for found in looked.values():
                if found:
                    known.add(found["id"])
                    known.update(leg["id"] for leg in found.get("legs") or [])
            for o in listing["orders"]:
                # our own close, also for a fill the journal has not recorded yet
                if (o["id"] in known or o.get("client_order_id") in self.known_cids
                        or (is_close_order(o) and o["symbol"] in owners)):
                    continue
                self._block("orphan", f"orphan-order:{o['id']}",
                            f"an open {o['type']} {o['side']} order in {o['symbol']} is not "
                            "in the journal", symbol=o["symbol"])
                self._flatten(o["symbol"], [x.client_order_id
                                            for x in owners.get(o["symbol"], [])],
                              "an orphan order", now)

    # ---- lifecycle --------------------------------------------------------------------
    def _settle(self, o: OrderState, found: dict | None, now: datetime,
                limit_price: Decimal | None) -> None:
        cid = o.client_order_id
        if found is None:
            if o.status in (SUBMITTING, SEND_UNKNOWN):
                n = self.not_found[cid] = self.not_found.get(cid, 0) + 1
                since = o.last_at or o.approved_at
                if n >= 2 and now - since >= timedelta(seconds=LOST_SEND_GRACE_S):
                    self._event(o, REJECTED, reason="not found at the broker after "
                                f"{LOST_SEND_GRACE_S} s and {n} lookups")
            elif o.status != REJECTED:
                self._block("conflict", f"missing:{cid}",
                            f"the journal says {cid} ({o.symbol}) is {o.status}, but the "
                            "broker has no such order", symbol=o.symbol, client_order_id=cid)
            return
        self.not_found.pop(cid, None)
        b_status = found["status"]
        b_fill = _dec(found["filled_qty"]) or Decimal(0)
        avg = found.get("filled_avg_price")
        if o.status == REJECTED:
            if b_status in ("rejected", "canceled", "expired") and b_fill == 0:
                return  # the broker never filled it either: nothing to do
            self._block("conflict", f"rejected-held:{cid}",
                        f"the journal says {cid} ({o.symbol}) was rejected, but the broker "
                        f"holds it ({b_status})", symbol=o.symbol, client_order_id=cid)
            self._flatten(o.symbol, [cid], "an order the journal calls rejected", now)
            return
        status = o.status
        if status in (SUBMITTING, SEND_UNKNOWN):
            if self._event(o, SUBMITTED, broker_order_id=found["id"],
                           broker_status=b_status):
                status = SUBMITTED
            elif not self.check_only:
                return
            else:
                status = SUBMITTED
        if b_fill > o.filled_qty and status in (SUBMITTED, PARTIALLY_FILLED):
            if limit_price is not None and _dec(avg) is not None and _dec(avg) > limit_price:
                self._block("fill_above_limit", f"fill-above-limit:{cid}",
                            f"{cid} ({o.symbol}) filled at {avg}, above its limit price "
                            f"{limit_price}", symbol=o.symbol, client_order_id=cid)
            kind = FILLED if b_fill == o.qty else PARTIALLY_FILLED
            if self._event(o, kind, filled_qty=int(b_fill), avg_price=avg,
                           broker_filled_at=found.get("filled_at")):
                status = kind
        if b_status in ("canceled", "expired", "done_for_day") and status in (
                SUBMITTED, PARTIALLY_FILLED):
            # done for the day: no more fills today (a day order), recorded as expired
            self._event(o, CANCELLED if b_status == "canceled" else EXPIRED,
                        filled_qty=int(b_fill), broker_status=b_status,
                        broker_at=found.get("canceled_at") or found.get("expired_at"))
            self.partial_since.pop(cid, None)
            return
        if b_status in FINAL and b_status not in ("filled", "rejected") and status in (
                SUBMITTED, PARTIALLY_FILLED):
            self._persisting(f"final:{cid}", f"{cid} ({o.symbol}) is {b_status} at the broker, "
                             f"which the journal cannot follow from {status}: check it",
                             o.symbol)
            return
        if b_status == "rejected" and status == SUBMITTED and b_fill == 0:
            self._event(o, REJECTED, reason="rejected by the broker")
            return
        if b_status == "rejected" or (b_status == "filled" and b_fill != o.qty):
            # the broker and the journal disagree on what filled: never silent
            self._block("conflict", f"qty:{cid}:{b_status}",
                        f"{cid} ({o.symbol}) is {b_status} at the broker with {b_fill} of "
                        f"{o.qty} shares filled, which the journal cannot follow from "
                        f"{status}", symbol=o.symbol, client_order_id=cid)
            return
        if not entry_can_fill(found):
            self.partial_since.pop(cid, None)
            return
        # the entry is still working
        if b_fill == 0:
            sent = o.last_at or o.approved_at
            if status == SUBMITTED and now - sent >= timedelta(seconds=UNFILLED_CANCEL_S):
                self._cancel_entry(o, found, "unfilled after 60 s (#136)")
            return
        first = self.partial_since.setdefault(cid, now)
        if now - first >= timedelta(seconds=PARTIAL_GRACE_S):
            self._cancel_entry(o, found, f"partly filled ({b_fill} of {o.qty}) after one "
                               "poll of grace: cancel the remainder")

    def _cancel_entry(self, o: OrderState, found: dict, why: str) -> None:
        """Cancel the entry (Alpaca cancels the group), then confirm by lookup and record."""
        if o.symbol in self.earlier_day:
            self._block("unprotected", f"earlier-day-symbol:{o.symbol}",
                        f"{o.symbol}: would cancel an entry ({why}), but an order from an "
                        f"earlier day may hold it: {EARLIER_DAY}", symbol=o.symbol)
            return
        self.cancel_sent.add(o.client_order_id)
        self._cancel(found["id"], f"entry {o.client_order_id} ({o.symbol}): {why}")
        if self.check_only:
            return
        deadline = self.monotonic() + 15
        while True:
            try:
                again = self.broker.get_order_by_client_id(o.client_order_id)
            except BrokerError as exc:
                self._persisting(f"confirm:{o.client_order_id}",
                                 f"the cancel of {o.client_order_id} ({o.symbol}) could not be "
                                 f"confirmed: {exc}", o.symbol)
                return
            legs_done = all(g["status"] in FINAL for g in (again or {}).get("legs") or [])
            nothing_filled = again is None or not (_dec(again["filled_qty"]) or 0) > 0
            # nothing filled: the entry and both legs must be gone; a partial fill: the entry
            # is done, and its legs are judged by what the broker shows (tools.protection)
            if again is None or (not entry_can_fill(again)
                                 and (legs_done or not nothing_filled)):
                break
            if self.monotonic() >= deadline:
                self._persisting(f"confirm:{o.client_order_id}",
                                 f"the cancel of {o.client_order_id} ({o.symbol}) is not "
                                 "confirmed yet", o.symbol)
                return
            self.sleep(1.0)
        self.retry_since.pop(f"confirm:{o.client_order_id}", None)
        self.latest[o.client_order_id] = again
        if again is not None:
            try:
                current = self.journal.orders()[o.client_order_id]
            except (JournalError, KeyError):
                return
            self._settle(current, again, self._now(),
                         self.limits_by_cid.get(o.client_order_id))

    # ---- protection -------------------------------------------------------------------
    def _protect(self, sym: str, pos: dict, trades: list[OrderState],
                 looked: dict[str, dict | None], approved: dict[str, dict],
                 now: datetime, today) -> None:
        cids = [o.client_order_id for o in trades]
        for o in trades:  # the position is (still) there: no close is due
            self.retry_since.pop(f"close:{o.client_order_id}", None)
        if pos.get("side") != "long":
            self._block("orphan", f"short:{sym}", f"a short position in {sym}: this system "
                        "is long only", symbol=sym)
            self._flatten(sym, cids, "a short position", now)
            return
        if not trades:
            self._block("orphan", f"orphan-position:{sym}",
                        f"a position in {sym} ({pos.get('qty')} shares) is not in the journal",
                        symbol=sym)
            self._flatten(sym, [], "an orphan position", now)
            return
        if len(trades) > 1:
            self._block("conflict", f"two-trades:{sym}", f"more than one open trade in {sym} "
                        "in the journal", symbol=sym)
            self._flatten(sym, cids, "more than one open trade", now)
            return
        if self.flat_by_mode:
            # #135: every position of today's is closed at the flat-by, whatever its stop.
            # The legs the flat-by cancels itself are no reason for an "unprotected" block
            # (it would stop tomorrow's entries). An earlier day's position: _flatten blocks
            # it and acts on nothing (decision 8).
            self._flatten(sym, cids, "the flat-by time (#135)", now)
            return
        o = trades[0]
        cid = o.client_order_id
        found = looked.get(cid)
        if found is None:
            if cid in looked and o.status not in (SUBMITTING, SEND_UNKNOWN):
                # the journal holds a sent order the broker does not know (already a
                # conflict block, _settle); a lost send waits out its grace instead
                self._flatten(sym, cids, "its entry is unknown to the broker", now)
            return  # a failed lookup, or a lost send in its grace: the next pass judges it
        stop = ((approved.get(cid) or {}).get("order") or {}).get("stop_loss")
        verdict = stop_status(found, pos.get("qty"), stop_price=stop)
        state = verdict["state"]
        if state == UNPROTECTED:
            # read again before acting: a normal exit between this pass's reads must never
            # read as "no stop" (the position read came before the lookup)
            try:
                fresh = next((p for p in self.broker.list_positions() if p["symbol"] == sym),
                             None)
                found = self.broker.get_order_by_client_id(cid) if fresh else found
            except BrokerError as exc:
                self.report["read_errors"].append(f"re-read {sym}: {exc}")
                return
            if fresh is None or found is None:
                return  # gone meanwhile: the next pass records the close
            verdict = stop_status(found, fresh.get("qty"), stop_price=stop)
            state = verdict["state"]
        if verdict.get("activating"):
            first = self.activating_since.setdefault(cid, now)
            if now - first < timedelta(seconds=PARTIAL_GRACE_S):
                # Alpaca may show filled/held/held for a moment: one poll of grace
                self.report["errors" if not self.check_only else "would"].append(
                    f"{sym}: the legs are still activating; judged again next pass")
                return
            verdict = {"state": UNPROTECTED, "reason": verdict["reason"] + " after one poll"}
            state = UNPROTECTED
        else:
            self.activating_since.pop(cid, None)
        if state in (PROTECTED, PENDING):
            self.executing_since.pop(cid, None)
            return
        if o.approved_at.astimezone(NY).date() < today:
            # decision 8 and the PR 5 morning rule: an earlier day's position is never closed
            # automatically. Block and alert; Ivan checks it.
            self._block("unprotected", f"earlier-day:{cid}",
                        f"{sym} ({pos.get('qty')} shares) is from an earlier day and has no "
                        f"working stop ({verdict['reason']}): {EARLIER_DAY}", symbol=sym,
                        client_order_id=cid)
            return
        if state == EXECUTING:
            first = self.executing_since.setdefault(cid, now)
            if now - first < timedelta(seconds=EXECUTING_WAIT_S):
                return
            self._alert(f"{sym}: {verdict['reason']} for {EXECUTING_WAIT_S} s and the "
                        "position is still there: cancelling the stop and closing",
                        symbol=sym)
            self._flatten(sym, cids, "a triggered stop that did not finish", now)
            return
        why = (f"{sym} has no working stop for its {pos.get('qty')} shares: "
               f"{verdict['reason']}")
        if (_legs_cancelled_with_remainder(found, o)
                and self._block_open(f"unprotected:{cid}") is False):
            # Ivan's decision on #251: alert and flatten; block only if not confirmed
            if self.check_only:
                self._would(f"flatten {sym} ({why}): a partial fill whose legs were cancelled "
                            "with the remainder; it blocks only if the close is not "
                            "confirmed")
                return
            if cid not in self.partial_flatten:
                self._alert(f"{why}. A partial fill whose legs were cancelled with the "
                            "remainder: flattening it; no block if the close is confirmed",
                            symbol=sym)
                self.partial_flatten[cid] = (now, sym, why)
            result = self._flatten(sym, cids, verdict["reason"], now)
            if result.state == FLAT:
                # confirm in this same pass: the close is recorded with matching quantities
                # and check_close says closed (else the timeout or the final pass blocks)
                try:
                    again = self.broker.get_order_by_client_id(cid)
                except BrokerError as exc:
                    self.report["read_errors"].append(f"re-read {cid}: {exc}")
                    return
                self._close(o, again, now)
            elif result.state not in (CLOSING, DONE_THIS_PASS):
                self._unconfirmed(cid, sym, why, f"the flatten did not complete "
                                  f"({result.state}: {result.detail})")
            return
        self._block("unprotected", f"unprotected:{cid}", why, symbol=sym,
                    client_order_id=cid)
        self._flatten(sym, cids, verdict["reason"], now)

    def _unconfirmed(self, cid: str, sym: str, why: str, what: str) -> bool:
        """The partial fill's flatten was not confirmed: block, as for any unprotected
        position (Ivan's decision on #251). Forgotten only once the block is open, so a
        block that could not be written is retried next pass."""
        if self._block("unprotected", f"unprotected:{cid}",
                       f"{why}; its flatten was not confirmed: {what}", symbol=sym,
                       client_order_id=cid):
            self.partial_flatten.pop(cid, None)
            return True
        return False

    def _confirm_partial_flattens(self, now: datetime, *, final: bool = False) -> None:
        """A pending partial-fill flatten still unconfirmed after LOUD_S, or at the end of a
        final pass (nothing later will confirm it), blocks."""
        for cid, (since, sym, why) in list(self.partial_flatten.items()):
            if final or now - since >= timedelta(seconds=LOUD_S):
                self._unconfirmed(cid, sym, why, "no confirmed close "
                                  + ("before the last pass ended" if final else
                                     f"within {LOUD_S} s")
                                  + " (the close did not complete, or its quantities do "
                                  "not match)")

    def _flatten(self, sym: str, cids: list[str], why: str, now: datetime) -> FlattenResult:
        if sym in self.flattened:  # once per symbol per pass, whatever asked for it
            return FlattenResult(DONE_THIS_PASS, "already flattened in this pass")
        self.flattened.add(sym)
        if sym in self.earlier_day:
            # decision 8 and the PR 5 morning rule: nothing an earlier day's order may hold
            # is closed or cancelled automatically, whatever the path (#155 review)
            self._block("unprotected", f"earlier-day-symbol:{sym}",
                        f"{sym} would be flattened ({why}), but an order from an earlier day "
                        f"may hold it: {EARLIER_DAY}", symbol=sym)
            return FlattenResult("held", "an earlier day's position: not acted on")
        # #135 review: after the close a market close would wait for the next open and fill
        # then, with nobody deciding: cancel nothing, close nothing; a person acts. Judged by
        # the time NOW (a pass can run long), and again inside flatten_symbol before each
        # cancel and before the close.
        why_not = ("the market is closed, so a market close would wait for the next open: "
                   "nothing was cancelled or closed")
        if self._session_over(self._now()):
            if self.check_only:
                self._would(f"flatten {sym} ({why}): not now, {why_not}")
                return FlattenResult("check", "check only")
            self._alert(MANUAL.format(symbol=sym) + f" ({why}; {why_not})", symbol=sym)
            self.stuck_since.setdefault(sym, now)
            return FlattenResult(SESSION_OVER, why_not)
        if self.check_only:
            self._would(f"flatten {sym} ({why}): cancel its legs and orders, confirm, close")
            return FlattenResult("check", "check only")
        result = flatten_symbol(self.broker, sym, cids, sleep=self.sleep,
                                monotonic=self.monotonic,
                                may_act=lambda: not self._session_over(self._now()))
        self.report["actions"].extend(result.actions)
        if result.state == SESSION_OVER:
            self._alert(MANUAL.format(symbol=sym) + f" ({why}; {result.detail})", symbol=sym)
            self.stuck_since.setdefault(sym, now)
            return result
        if result.state == FLAT:
            self.stuck_since.pop(sym, None)
            return result
        if result.state == CLOSING and not self._market_open(now):
            # a market close queued for the open: say so once, never escalate before then
            if sym not in self.stuck_since:
                self._alert(f"{sym}: {result.detail}; it fills when the market opens",
                            symbol=sym)
            self.stuck_since.setdefault(sym, now)
            return result
        # not flat yet: alert every pass, never quiet; loud after LOUD_S, or at the flat-by
        # for a skipped close (it never closes by itself) or a flatten stuck since before it.
        # A flatten the flat-by itself started gets its LOUD_S like any other (#135).
        first = self.stuck_since.setdefault(sym, now)
        flat_by = self._flat_by(now)
        what = ("the close was skipped this pass" if result.state == SKIPPED_UNREADABLE
                else f"flatten not done ({result.state})")
        at_flat_by = flat_by is not None and now >= flat_by and (
            result.state == SKIPPED_UNREADABLE or first < flat_by)
        if now - first >= timedelta(seconds=LOUD_S) or at_flat_by:
            self._alert(MANUAL.format(symbol=sym) + f" ({what}: {result.detail}; unresolved "
                        f"since {first:%H:%M:%S})", symbol=sym)
        else:
            self._alert(f"{sym}: {what} ({result.detail}); the next pass tries again",
                        symbol=sym)
        return result

    @staticmethod
    def _session_over(now: datetime) -> bool:
        """Today's session has ended, or there is none today. Unknown (an uncovered year):
        False, so nothing is held back."""
        try:
            hours = session_hours(now.date())
        except CalendarError:
            return False
        return hours is None or now.time() >= hours[1]

    @staticmethod
    def _market_open(now: datetime) -> bool:
        try:
            hours = session_hours(now.date())
        except CalendarError:
            return True  # unknown: never hold back an alert
        return hours is not None and hours[0] <= now.time() < hours[1]

    def _flat_by(self, now: datetime) -> datetime | None:
        if self.limits is None:
            return None
        try:
            flat = day_times(now.date(), self.limits)[2]
        except CalendarError:
            return None
        return datetime.combine(now.date(), flat, tzinfo=NY)

    # ---- closing ----------------------------------------------------------------------
    def _close(self, o: OrderState, found: dict | None, now: datetime) -> None:
        """The broker shows no position: record the trade's close with its realized P&L,
        from the broker's exit fills (the take-profit and stop legs, our flatten's close)."""
        if o.status == PARTIALLY_FILLED or found is None:
            return  # the entry may still fill, or it was not looked up this pass
        partial_case = _legs_cancelled_with_remainder(found, o)
        if partial_case and not self.check_only:
            # remembered until confirmed or blocked, also after a restart (#251 review)
            self.partial_flatten.setdefault(o.client_order_id,
                                            (now, o.symbol,
                                             f"{o.symbol}: a partial fill's flatten"))
        exits = []
        for leg in found.get("legs") or []:
            if _dec(leg["filled_qty"]) and _dec(leg["filled_qty"]) > 0:
                exits.append({"by": "target" if leg["type"] == "limit" else "stop",
                              "qty": leg["filled_qty"], "avg": leg["filled_avg_price"],
                              "at": leg.get("filled_at")})
        try:
            history = self.broker.list_orders_checked(o.symbol)
        except BrokerError as exc:
            self._persisting(f"history:{o.symbol}", f"{o.symbol}'s recent orders cannot be "
                             f"read, so its close is not recorded yet: {exc}", o.symbol)
            return
        self.retry_since.pop(f"history:{o.symbol}", None)
        for u in history["unreadable"]:
            self._block("unreadable_order",
                        f"unreadable:{u.get('id') or u.get('symbol') or u.get('why')}",
                        f"an order in {o.symbol}'s recent history cannot be read (id "
                        f"{u.get('id')}: {u.get('why')}); the close is recorded from the "
                        "readable fills", symbol=o.symbol)
        closes = history["orders"]
        opened = _when(found.get("submitted_at"))
        for c in closes:
            if not is_close_order(c) or not (_dec(c["filled_qty"]) or 0) > 0:
                continue
            if opened is not None and (_when(c.get("submitted_at")) or opened) < opened:
                continue  # an earlier trade's close
            exits.append({"by": "flatten", "qty": c["filled_qty"], "avg": c["filled_avg_price"],
                          "at": c.get("filled_at")})
        try:
            pnl = realized_pnl(o.filled_qty, o.avg_price,
                               [(x["qty"], x["avg"]) for x in exits])
        except RealizedError as exc:
            first = self.retry_since.setdefault(f"close:{o.client_order_id}", now)
            if partial_case and (now - first >= timedelta(seconds=LOUD_S)
                                 or self.final):
                # the decided case (#251): quantities that do not match block
                self._unconfirmed(o.client_order_id, o.symbol,
                                  f"{o.symbol}: a partial fill's flatten",
                                  f"its exit fills do not add up ({exc})")
            elif now - first >= timedelta(seconds=RETRY_ALERT_S):
                self._alert(f"{o.symbol}: the position is gone but its exit fills do not add "
                            f"up ({exc}) since {first:%H:%M:%S}", symbol=o.symbol)
            return
        self.retry_since.pop(f"close:{o.client_order_id}", None)
        earliest = datetime.min.replace(tzinfo=NY)
        last = max(exits, key=lambda x: _when(x["at"]) or earliest)
        confirmed = False
        if partial_case:
            # The decided case (#251): check_close FIRST, before the close is recorded, so
            # the order stays open in the journal (followed by every later pass, also after
            # a restart) until it is either confirmed or blocked: never neither.
            cid, why = o.client_order_id, f"{o.symbol}: a partial fill's flatten"
            try:
                state = self.broker.check_close(o.symbol)["state"]
                problem = None if state == "closed" else f"check_close says {state}"
            except BrokerError as exc:
                problem = f"check_close failed ({exc})"
            if problem is not None:
                # blocked first; the close is then recorded (its exits add up). A position that
                # reappears later is caught as an orphan, blocked and flattened.
                if not self._unconfirmed(cid, o.symbol, why, problem):
                    return  # no block written yet: nothing recorded, the next pass retries
            else:
                confirmed = True
        recorded = self._event(o, CLOSED, closed_by=last["by"], realized_pnl=str(pnl),
                               exits=exits, history_unreadable=bool(history["unreadable"]))
        if partial_case and confirmed and recorded:
            # confirmed: check_close says closed AND the journal recorded the close with
            # matching quantities (realized_pnl)
            cid = o.client_order_id
            self.partial_flatten.pop(cid, None)
            standing = self._block_open(f"unprotected:{cid}")
            self._alert(f"{o.symbol}: the partial fill's flatten is confirmed (check_close: "
                        f"closed; {o.filled_qty} shares closed, realized {pnl}); "
                        + ("its earlier block stays until resolved" if standing
                           else "no block" if standing is False
                           else "the block state cannot be read: check journal.resolve"),
                        symbol=o.symbol)


# ---- helpers --------------------------------------------------------------------------

def _followed(o: OrderState, now: datetime) -> bool:
    """Orders the reconciler looks up: not final, or a fill not closed yet, or rejected
    today (re-checked: the broker might hold it after all)."""
    if o.status in FOLLOW:
        return True
    if o.status in (CANCELLED, EXPIRED) and o.filled_qty > 0:
        return True
    return o.status == REJECTED and o.approved_at.astimezone(NY).date() == now.date()


def _legs_cancelled_with_remainder(entry: dict, o: OrderState) -> bool:
    """A partial fill whose stop and take-profit Alpaca cancelled (or expired) together with
    the entry's unfilled remainder: the case Ivan decided on #251."""
    filled = _dec(entry.get("filled_qty")) or 0
    if entry.get("status") not in ("canceled", "expired") or not 0 < filled < o.qty:
        return False
    legs = entry.get("legs") or []
    if not legs or not all(g.get("status") in ("canceled", "expired") for g in legs):
        return False
    # together WITH the remainder: each leg ended within a few seconds of the entry. Legs
    # cancelled later (by a flatten, or by hand) are not this case: those block at once.
    ended = _when(entry.get("canceled_at") or entry.get("expired_at"))
    if ended is None:
        return False
    for g in legs:
        at = _when(g.get("canceled_at") or g.get("expired_at"))
        if at is None or abs((at - ended).total_seconds()) > LEGS_WITH_REMAINDER_S:
            return False
    return True


def _approved_orders(entries: list[dict]) -> dict[str, dict]:
    out = {}
    for e in entries:
        if e.get("status") != APPROVED:
            continue
        cid = e.get("client_order_id")
        if not isinstance(cid, str):
            try:
                cid = client_order_id(e.get("run_id"), e.get("plan_id"))
            except ValueError:
                continue
        out[cid] = e
    return out


def _limit_prices(entries: list[dict]) -> dict[str, Decimal]:
    """Each sent order's limit price, from its "submitting" event (pipeline/send.py)."""
    out = {}
    for e in entries:
        if e.get("status") == SUBMITTING and isinstance(e.get("client_order_id"), str):
            price = _dec(e.get("limit_price"))
            if price is not None:
                out[e["client_order_id"]] = price
    return out


def _safe(fields: dict) -> dict:
    return {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
            for k, v in fields.items()}


# ---- read-only wrappers for --check ---------------------------------------------------

class ReadOnlyBroker:
    """The broker with every action refused: --check can only read."""

    WRITES = ("submit_bracket", "cancel_order", "close_position", "close_all")

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in self.WRITES:
            raise AttributeError(f"--check never calls {name}")
        return getattr(self._inner, name)


def main(argv: list[str] | None = None, *, broker: Any = None, journal: Journal | None = None,
         clock: Callable[[], datetime] | None = None, state_dir: Path | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline.reconcile", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="read only: print what the reconciler sees and would do")
    mode.add_argument("--once", action="store_true",
                      help="one acting pass on the PAPER account (Ivan only)")
    env = p.add_mutually_exclusive_group()
    env.add_argument("--env-file", type=Path, default=None, metavar="PATH")
    env.add_argument("--no-dotenv", action="store_true")
    args = p.parse_args(argv)
    from tools.config import load_limits
    try:
        limits = load_limits()
    except (ValueError, OSError) as exc:
        print(f"refused: the limits config cannot be used ({exc})", file=sys.stderr)
        return 2
    if limits.paper is not True:
        print("refused: paper must be true (CLAUDE.md rule 1)", file=sys.stderr)
        return 2
    if broker is None:
        from pipeline.smoke_common import load_env
        from tools.broker import AlpacaPaperBroker
        code = load_env(args)
        if code is not None:
            return code
        try:
            broker = AlpacaPaperBroker(limits)
            broker.verify_paper_account()
        except BrokerError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
    if args.check:
        broker = ReadOnlyBroker(broker)
    rec = Reconciler(journal or Journal(), broker, limits=limits, check_only=args.check,
                     clock=clock or (lambda: datetime.now(NY)))
    if args.check:
        report = rec.pass_once()
    else:
        # run_day's own lock: an acting pass never runs beside run_day's reconciler
        # (ACTION_LOCK is per process), and run_day cannot start during it
        from pipeline.budget import STATE_DIR
        from pipeline.filelock import LockTimeout, exclusive_file_lock
        try:
            with exclusive_file_lock((state_dir or STATE_DIR) / "run_day.lock", timeout_s=0):
                with ACTION_LOCK:
                    report = rec.pass_once(final=True)  # nothing later confirms
        except LockTimeout:
            print("refused: run_day is running (its reconciler is already acting); use "
                  "--check to look", file=sys.stderr)
            return 4
    print(json.dumps(report, indent=1, default=str))
    return 0 if report.get("complete") else 1


if __name__ == "__main__":
    sys.exit(main())
