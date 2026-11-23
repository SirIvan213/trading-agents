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
     Anything unprotected is blocked, alerted and flattened (pipeline.flatten);
  5. a trade whose position is gone is closed in the journal with its realized P&L
     (tools.realized) from the broker's exit fills;
  6. orphans (a position or order not traceable to the journal; the paper account is
     dedicated to this project, no hand-placed orders) and unreadable orders block new
     entries and alert; orphans are flattened, an unreadable order is never acted on.

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

from journal.log import RECONCILE_ALERT, Journal, JournalError, JournalLockTimeout
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
    SKIPPED_UNREADABLE,
    FlattenResult,
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

# The reconciler is ready for run_day. run_day still refuses a broker until pipeline.flatten
# (PR 5, the flat-by job, #135) declares RUN_DAY_READY too (pipeline.run_day.broker_allowed).
RUN_DAY_READY = True
FINAL = FINAL_STATUSES

NY = ZoneInfo("America/New_York")
POLL_S = 15
STALE_S = 45
UNFILLED_CANCEL_S = 60
PARTIAL_GRACE_S = 15
EXECUTING_WAIT_S = 60
LOUD_S = 60  # a flatten not done this long: MANUAL ACTION NEEDED
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
                 limits: Any = None, check_only: bool = False,
                 alert: Callable[[str], None] = _alert_print,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.journal, self.broker, self.clock, self.limits = journal, broker, clock, limits
        self.check_only = check_only
        self._print_alert, self.sleep, self.monotonic = alert, sleep, monotonic
        # timers that span passes (in memory: a restart only waits longer, never less)
        self.not_found: dict[str, int] = {}  # cid -> consecutive 404 lookups
        self.partial_since: dict[str, datetime] = {}
        self.executing_since: dict[str, datetime] = {}
        self.activating_since: dict[str, datetime] = {}
        self.stuck_since: dict[str, datetime] = {}  # symbol -> a flatten not done since
        self.retry_since: dict[str, datetime] = {}
        self.latest: dict[str, dict | None] = {}  # a lookup made after acting (a cancel)
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

    def _block(self, kind: str, key: str, reason: str, **fields: object) -> None:
        if self.check_only:
            self._would(f"block new entries ({kind}): {reason}")
            return
        try:
            block_id = self.journal.add_block(kind, key, reason, self._now(), **fields)
        except JournalError as exc:
            self._alert(f"could not journal a block ({kind}: {reason}): {exc}")
            return
        if block_id is not None:
            self.report["blocks"].append(block_id)
            self._alert(f"new entries blocked ({block_id} {kind}): {reason}. Resolve with "
                        f"python -m journal.resolve {block_id}", symbol=fields.get("symbol"))

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
    def pass_once(self) -> dict:
        now = self._now()
        self.report = {"at": now.isoformat(), "mode": "check" if self.check_only else "act",
                       "complete": False, "orders": [], "positions": [], "unreadable": [],
                       "recorded": [], "actions": [], "would": [], "blocks": [],
                       "alerts": [], "errors": [], "read_errors": []}
        self._run(now)
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
            for sym, pos in positions.items():
                self._protect(sym, pos, owners.get(sym, []), looked, approved, now, today)
            for sym, trades in holding.items():
                if sym not in positions:
                    for o in trades:
                        self._close(o, looked.get(o.client_order_id), now)

        # orphan orders: open, readable, and not ours (skipped when a lookup failed: an
        # order we could not look up would read as an orphan)
        if listing is not None and not lookup_failed:
            known = set()
            for found in looked.values():
                if found:
                    known.add(found["id"])
                    known.update(leg["id"] for leg in found.get("legs") or [])
            flattened: set[str] = set()
            for o in listing["orders"]:
                # our own close, also for a fill the journal has not recorded yet
                if (o["id"] in known or o.get("client_order_id") in self.known_cids
                        or (is_close_order(o) and o["symbol"] in owners)):
                    continue
                self._block("orphan", f"orphan-order:{o['id']}",
                            f"an open {o['type']} {o['side']} order in {o['symbol']} is not "
                            "in the journal", symbol=o["symbol"])
                if o["symbol"] in flattened:
                    continue
                flattened.add(o["symbol"])
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
        if b_status == "rejected" and status == SUBMITTED:
            if b_fill == 0:
                self._event(o, REJECTED, reason="rejected by the broker")
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
                return  # Alpaca may show filled/held/held for a moment: one poll of grace
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
        self._block("unprotected", f"unprotected:{cid}",
                    f"{sym} has no working stop for its {pos.get('qty')} shares: "
                    f"{verdict['reason']}", symbol=sym, client_order_id=cid)
        self._flatten(sym, cids, verdict["reason"], now)

    def _flatten(self, sym: str, cids: list[str], why: str, now: datetime) -> FlattenResult:
        if sym in self.earlier_day:
            # decision 8 and the PR 5 morning rule: nothing an earlier day's order may hold
            # is closed or cancelled automatically, whatever the path (#155 review)
            self._block("unprotected", f"earlier-day-symbol:{sym}",
                        f"{sym} would be flattened ({why}), but an order from an earlier day "
                        f"may hold it: {EARLIER_DAY}", symbol=sym)
            return FlattenResult("held", "an earlier day's position: not acted on")
        if self.check_only:
            self._would(f"flatten {sym} ({why}): cancel its legs and orders, confirm, close")
            return FlattenResult("check", "check only")
        result = flatten_symbol(self.broker, sym, cids, sleep=self.sleep,
                                monotonic=self.monotonic)
        self.report["actions"].extend(result.actions)
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
        # not flat yet: alert every pass, never quiet; loud after LOUD_S or at the flat-by
        first = self.stuck_since.setdefault(sym, now)
        flat_by = self._flat_by(now)
        what = ("the close was skipped this pass" if result.state == SKIPPED_UNREADABLE
                else f"flatten not done ({result.state})")
        if now - first >= timedelta(seconds=LOUD_S) or (flat_by is not None
                                                         and now >= flat_by):
            self._alert(MANUAL.format(symbol=sym) + f" ({what}: {result.detail}; unresolved "
                        f"since {first:%H:%M:%S})", symbol=sym)
        else:
            self._alert(f"{sym}: {what} ({result.detail}); the next pass tries again",
                        symbol=sym)
        return result

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
            if now - first >= timedelta(seconds=RETRY_ALERT_S):
                self._alert(f"{o.symbol}: the position is gone but its exit fills do not add "
                            f"up ({exc}) since {first:%H:%M:%S}", symbol=o.symbol)
            return
        self.retry_since.pop(f"close:{o.client_order_id}", None)
        earliest = datetime.min.replace(tzinfo=NY)
        last = max(exits, key=lambda x: _when(x["at"]) or earliest)
        self._event(o, CLOSED, closed_by=last["by"], realized_pnl=str(pnl), exits=exits,
                    history_unreadable=bool(history["unreadable"]))


# ---- helpers --------------------------------------------------------------------------

def _followed(o: OrderState, now: datetime) -> bool:
    """Orders the reconciler looks up: not final, or a fill not closed yet, or rejected
    today (re-checked: the broker might hold it after all)."""
    if o.status in FOLLOW:
        return True
    if o.status in (CANCELLED, EXPIRED) and o.filled_qty > 0:
        return True
    return o.status == REJECTED and o.approved_at.astimezone(NY).date() == now.date()


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
                    report = rec.pass_once()
        except LockTimeout:
            print("refused: run_day is running (its reconciler is already acting); use "
                  "--check to look", file=sys.stderr)
            return 4
    print(json.dumps(report, indent=1, default=str))
    return 0 if report.get("complete") else 1


if __name__ == "__main__":
    sys.exit(main())
