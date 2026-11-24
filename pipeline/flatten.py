"""Flatten one symbol on the Alpaca PAPER account: cancel what is working, confirm it is
gone, then close the position at market (Phase 5 PR 4, #155; PR 5's flat-by job reuses it,
#135). Paper only: it acts through tools.broker.AlpacaPaperBroker, which is fixed to the
paper host.

The steps, all under tools.broker.ACTION_LOCK so no decision acts at the same time:
  1. find everything working in the symbol: each journaled entry's lookup WITH ITS LEGS (the
     only place a held stop shows, #196) and the symbol's readable open orders, except a
     close that is already working;
  2. cancel each one. A cancel whose outcome is unclear (may_have_acted) is never repeated
     blindly: the next lookup says what happened;
  3. confirm they are gone, by looking them up again (up to CONFIRM_WAIT_S). A leg that
     FILLED meanwhile is the bracket closing the position itself;
  4. close: read the position; if shares remain and no close is working, close_position.
     A 403 (40310000) means something still holds the shares: the next pass starts over.
     An unclear close is followed by check_close, never by a second close.

An unreadable open order that may be in the symbol (#246) could be a working close, so the
pass does not close ("skipped_unreadable"). That never becomes "never close": the caller
(pipeline.reconcile) repeats the alert every pass and, 60 s later or at the flat-by time,
raises a louder alert asking for manual action in the Alpaca paper dashboard.

The flat-by job (PR 5, #135) is the reconciler's flat-by mode (pipeline.reconcile): at and
after the day's flat-by time (flat_by_at: 15:55 ET, 12:55 on an early close, from the NYSE
calendar), every reconcile pass cancels today's working entries and flattens today's
positions with flatten_symbol, until the account is flat. run_day keeps the reconciler
running until then (#248). A position from an earlier day is never closed automatically
(decision 8): it is blocked and alerted, and waits for Ivan.

    python -m pipeline.flatten --check   # read only: today's flat-by, and what --now would do
    python -m pipeline.flatten --now     # flatten today's positions now (Ivan, or a backstop)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools.broker import ACTION_LOCK, FINAL_STATUSES, BrokerError, is_close_order
from tools.market_calendar import CalendarError, closed_reason, day_times, session_hours

# The flat-by job is here (#135): with pipeline.reconcile's own flag, run_day accepts a broker
# (pipeline.run_day.broker_allowed), so approved orders can reach the PAPER account.
RUN_DAY_READY = True

NY = ZoneInfo("America/New_York")
CONFIRM_WAIT_S = 15.0
POLL_S = 1.0

FLAT = "flat"  # no position in the symbol
CLOSING = "closing"  # a close is working (ours, or one we just placed)
WAITING = "waiting"  # not done this pass (orders not gone yet, legs still holding shares)
SKIPPED_UNREADABLE = "skipped_unreadable"  # an unreadable order may be a working close
FAILED = "failed"  # a read failed: nothing was closed blindly; the next pass tries again


def flat_by_at(day: date, limits: Any) -> datetime | None:
    """`day`'s flat-by as a New York datetime: limits.flat_by (15:55), or 12:55 on an early
    close (tools.market_calendar.day_times, #135). None on a day with no session. Raises
    CalendarError for a year the calendar does not cover."""
    if session_hours(day) is None:
        return None
    return datetime.combine(day, day_times(day, limits)[2], tzinfo=NY)


def flat_by_reached(now: datetime, limits: Any) -> bool:
    """`now` is at or after today's flat-by. A year the calendar does not cover counts as
    reached (fails safe: flatten, never wait; risk_check already refuses every entry then).
    A day with no session: False (nothing trades)."""
    now = now.astimezone(NY)
    try:
        at = flat_by_at(now.date(), limits)
    except CalendarError:
        return True
    return at is not None and now >= at


def still_open(broker: Any) -> list[str] | None:
    """What the broker still holds: one line per position, open order and unreadable open
    order ([] when the account is flat). None when it cannot be read. Read only."""
    try:
        with ACTION_LOCK:
            positions = broker.list_positions()
            listing = broker.list_open_orders_checked()
    except BrokerError:
        return None
    return ([f"{p['symbol']} position ({p.get('qty')} shares)" for p in positions]
            + [f"open {o['type']} {o['side']} order in {o['symbol']}"
               for o in listing["orders"]]
            + [f"an open order that cannot be read (id {u.get('id')}, symbol "
               f"{u.get('symbol')})" for u in listing["unreadable"]])


@dataclass
class FlattenResult:
    state: str
    detail: str
    actions: list[str] = field(default_factory=list)


def _working_ids(broker: Any, symbol: str, cids: Iterable[str]) -> tuple[dict, bool, bool]:
    """({order id: what it is}, a close is working, an unreadable order may be in the
    symbol). Raises BrokerError when a read fails."""
    ids: dict[str, str] = {}
    for cid in cids:
        entry = broker.get_order_by_client_id(cid)
        if entry is None:
            continue
        if entry["status"] not in FINAL_STATUSES:
            ids[entry["id"]] = f"entry {cid}"
        for leg in entry.get("legs") or []:
            if leg["status"] not in FINAL_STATUSES:
                ids[leg["id"]] = f"{leg['type']} leg of {cid}"
    listing = broker.list_open_orders_checked()
    closing = False
    for o in listing["orders"]:
        if o["symbol"] != symbol:
            continue
        if is_close_order(o):
            closing = True
        else:
            ids.setdefault(o["id"], f"open {o['type']} {o['side']} order")
    unreadable = any(u["symbol"] in (None, symbol) for u in listing["unreadable"])
    return ids, closing, unreadable


def _leg_filled(broker: Any, cids: Iterable[str]) -> bool:
    for cid in cids:
        entry = broker.get_order_by_client_id(cid)
        if entry and any(leg["status"] == "filled" for leg in entry.get("legs") or []):
            return True
    return False


def flatten_symbol(broker: Any, symbol: str, cids: Iterable[str] = (), *,
                   sleep: Callable[[float], None] = time.sleep,
                   monotonic: Callable[[], float] = time.monotonic) -> FlattenResult:
    """One flatten pass for `symbol` (`cids`: its journaled entries' client_order_ids). Never
    raises a BrokerError: the result says where it got to."""
    cids = list(cids)
    actions: list[str] = []
    with ACTION_LOCK:
        try:
            ids, closing, unreadable = _working_ids(broker, symbol, cids)
        except BrokerError as exc:
            return FlattenResult(FAILED, f"could not read {symbol}'s orders ({exc})", actions)
        for oid, what in ids.items():
            try:
                broker.cancel_order(oid)
                actions.append(f"cancel {what} ({oid})")
            except BrokerError as exc:
                # a refusal (404, 422) usually means it is already final; an unclear one is
                # never repeated blindly: the confirm step looks it up
                actions.append(f"cancel {what} ({oid}) failed: {exc}")
        if ids:
            deadline = monotonic() + CONFIRM_WAIT_S
            while True:
                try:
                    left, closing, unreadable = _working_ids(broker, symbol, cids)
                    if not left or _leg_filled(broker, cids):
                        break
                except BrokerError as exc:
                    return FlattenResult(FAILED, f"could not confirm the cancels ({exc})",
                                         actions)
                if monotonic() >= deadline:
                    return FlattenResult(WAITING, f"{len(left)} order(s) in {symbol} are not "
                                         "gone yet", actions)
                sleep(POLL_S)
        if unreadable:
            return FlattenResult(SKIPPED_UNREADABLE, f"an open order that may be in {symbol} "
                                 "cannot be read; it could be a working close, so this pass "
                                 "does not close", actions)
        try:
            position = next((p for p in broker.list_positions() if p["symbol"] == symbol),
                            None)
        except BrokerError as exc:
            return FlattenResult(FAILED, f"could not read the positions ({exc})", actions)
        if position is None:
            return FlattenResult(FLAT, f"no position in {symbol}", actions)
        if closing:  # already placed: the next pass looks again (no wait every pass)
            return FlattenResult(CLOSING, f"a close of {symbol} is already working", actions)
        try:
            order = broker.close_position(symbol)
            actions.append(f"close {symbol} ({order['id']})")
            return _await_flat(broker, symbol, f"close of {symbol} placed", actions, sleep,
                               monotonic)
        except BrokerError as exc:
            actions.append(f"close {symbol} failed: {exc}")
            if exc.may_have_acted:  # never close again blindly: find out
                try:
                    state = broker.check_close(symbol)["state"]
                except BrokerError as check_exc:
                    return FlattenResult(FAILED, f"the close was unclear and could not be "
                                         f"checked ({check_exc})", actions)
                actions.append(f"check_close {symbol}: {state}")
                return FlattenResult({"closed": FLAT, "closing": CLOSING}.get(state, WAITING),
                                     f"the close was unclear; check_close says {state}",
                                     actions)
            if exc.status == 403:
                return FlattenResult(WAITING, "something still holds the shares (403): the "
                                     "next pass cancels again", actions)
            return FlattenResult(FAILED, f"the close was refused ({exc})", actions)


def _await_flat(broker: Any, symbol: str, detail: str, actions: list[str],
                sleep: Callable[[float], None], monotonic: Callable[[], float]
                ) -> FlattenResult:
    """A market close usually fills in seconds: wait up to CONFIRM_WAIT_S for the position
    to go (FLAT), else CLOSING (the next pass looks again). Reads only."""
    deadline = monotonic() + CONFIRM_WAIT_S
    while True:
        try:
            if not any(p["symbol"] == symbol for p in broker.list_positions()):
                return FlattenResult(FLAT, f"{detail}; {symbol} is flat", actions)
        except BrokerError:
            return FlattenResult(CLOSING, detail, actions)  # the next pass reads again
        if monotonic() >= deadline:
            return FlattenResult(CLOSING, detail, actions)
        sleep(POLL_S)


# ---- the CLI: python -m pipeline.flatten --check | --now --------------------------------

LOCK_REFUSED = 4  # run_day is running and watching: it flattens at the flat-by itself
MARKET_CLOSED = 5  # a market close would wait for the next open: nothing done
NOT_FLAT = 1


def flat_by_line(today: date, limits: Any) -> str:
    """Today's flat-by, for a person."""
    try:
        at = flat_by_at(today, limits)
    except CalendarError:
        return (f"the market calendar does not cover {today.year}: every entry is blocked "
                "(risk_check); --now still flattens")
    if at is None:
        return f"no session today ({closed_reason(today)})"
    hours = session_hours(today)
    early = hours is not None and hours[1].hour < 16
    return (f"flat-by today ({today.isoformat()}): {at:%H:%M} ET"
            + (" (early close at 13:00)" if early else ""))


def market_open(now: datetime) -> bool | None:
    """The NYSE session is open at `now` (None: the calendar does not cover the year)."""
    now = now.astimezone(NY)
    try:
        hours = session_hours(now.date())
    except CalendarError:
        return None
    return hours is not None and hours[0] <= now.time() < hours[1]


def _run_day_watching(state_dir: Path, now: datetime, stale_s: float) -> str | None:
    """run_day holds its lock: is its reconciler watching (a pass in the last stale_s)?
    A reason to refuse, or None (it is not watching: --now acts as the backstop)."""
    beat =_read_heartbeat(state_dir / "run_day.heartbeat.json")
    last = beat.get("reconciled_at") if beat else None
    try:
        when = datetime.fromisoformat(last) if isinstance(last, str) else None
    except ValueError:
        when = None
    if when is None or when.tzinfo is None:
        return None
    age = (now - when).total_seconds()
    if age <= stale_s:
        return (f"run_day is running and its reconciler made a pass {int(age)} s ago: it "
                "flattens at the flat-by itself. Use --check to look, or stop run_day first")
    return None


def _read_heartbeat(path: Path) -> dict | None:
    """One short read (run_day rewrites the file atomically, tools/atomic_write.py)."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _today_left(broker: Any, earlier_day: set[str]) -> tuple[list[str] | None, list[str]]:
    """(what is still open outside an earlier day's symbols, or None when unreadable;
    what is left alone because an earlier day's order may hold it, decision 8)."""
    try:
        with ACTION_LOCK:
            positions = broker.list_positions()
            listing = broker.list_open_orders_checked()
    except BrokerError:
        return None, []
    left, alone = [], []
    for line, sym in ([(f"{p['symbol']} position ({p.get('qty')} shares)", p["symbol"])
                       for p in positions]
                      + [(f"open {o['type']} {o['side']} order in {o['symbol']}", o["symbol"])
                         for o in listing["orders"]]):
        (alone if sym in earlier_day else left).append(line)
    left += [f"an open order that cannot be read (id {u.get('id')})"
             for u in listing["unreadable"]]
    return left, alone


def flatten_now(rec: Any, broker: Any, *, clock: Callable[[], datetime],
                sleep: Callable[[float], None], poll_s: float,
                write: Callable[[str], None] = print) -> int:
    """Flat-by passes (rec has flat_now) every poll_s until everything of today's is flat
    (0), or the session ends (NOT_FLAT, after a final pass). An earlier day's position is
    left alone, blocked and alerted (NOT_FLAT: Ivan checks it)."""
    while True:
        with ACTION_LOCK:
            report = rec.pass_once()
        for a in report.get("actions", []):
            write(f"  {a}")
        left, alone = _today_left(broker, rec.earlier_day)
        if left == []:
            if alone:
                write("today's positions are flat; left alone (an earlier day's, decision 8, "
                      "check them in the Alpaca paper dashboard): " + "; ".join(alone))
                return NOT_FLAT
            write("flat: no position and no open order")
            return 0
        write("not flat yet: " + ("the broker cannot be read" if left is None
                                  else "; ".join(left)))
        if market_open(clock()) is False:
            with ACTION_LOCK:
                rec.pass_once(final=True)
            write("the market has closed with something still open: MANUAL ACTION NEEDED in "
                  "the Alpaca paper dashboard")
            return NOT_FLAT
        sleep(poll_s)


def main(argv: list[str] | None = None, *, broker: Any = None, journal: Any = None,
         clock: Callable[[], datetime] | None = None, state_dir: Path | None = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    from journal.log import Journal
    from pipeline.reconcile import POLL_S, STALE_S, ReadOnlyBroker, Reconciler
    from tools.config import load_limits

    p = argparse.ArgumentParser(prog="python -m pipeline.flatten", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="read only: print today's flat-by and what --now would do")
    mode.add_argument("--now", action="store_true",
                      help="flatten today's positions on the PAPER account now (Ivan only)")
    env = p.add_mutually_exclusive_group()
    env.add_argument("--env-file", type=Path, default=None, metavar="PATH")
    env.add_argument("--no-dotenv", action="store_true")
    args = p.parse_args(argv)
    clock = clock or (lambda: datetime.now(NY))
    try:
        limits = load_limits()
    except (ValueError, OSError) as exc:
        print(f"refused: the limits config cannot be used ({exc})", file=sys.stderr)
        return 2
    if limits.paper is not True:
        print("refused: paper must be true (CLAUDE.md rule 1)", file=sys.stderr)
        return 2
    now = clock().astimezone(NY)
    print(flat_by_line(now.date(), limits))
    if args.now and market_open(now) is False:
        print("refused: the market is closed, so a market close would wait for the next "
              "open. Nothing was done; use the Alpaca paper dashboard", file=sys.stderr)
        return MARKET_CLOSED
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
        rec = Reconciler(journal or Journal(), ReadOnlyBroker(broker), clock=clock,
                         limits=limits, check_only=True, flat_now=True)
        report = rec.pass_once()
        print("--now would:" if report["would"] else "--now would do nothing: nothing of "
              "today's is open")
        for w in report["would"]:
            print(f"  {w}")
        for a in report["alerts"]:
            print(f"  alert: {a}")
        print(json.dumps({k: report[k] for k in ("positions", "orders", "unreadable",
                                                 "read_errors")}, indent=1, default=str))
        return 0 if report.get("complete") else NOT_FLAT
    from pipeline.budget import STATE_DIR
    from pipeline.filelock import LockTimeout, exclusive_file_lock
    folder = state_dir or STATE_DIR
    rec = Reconciler(journal or Journal(), broker, clock=clock, limits=limits, flat_now=True)
    run = lambda: flatten_now(rec, broker, clock=clock, sleep=sleep,  # noqa: E731
                              poll_s=POLL_S)
    try:
        # run_day's own lock: no acting beside a run_day that is watching
        with exclusive_file_lock(folder / "run_day.lock", timeout_s=0):
            return run()
    except LockTimeout:
        why = _run_day_watching(folder, clock(), STALE_S)
        if why is not None:
            print(f"refused: {why}", file=sys.stderr)
            return LOCK_REFUSED
        print("run_day holds its lock but its reconciler is not watching (no pass in the "
              f"last {STALE_S} s): flattening anyway, as the backstop", file=sys.stderr)
        return run()


if __name__ == "__main__":
    sys.exit(main())
