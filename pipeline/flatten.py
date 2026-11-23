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

This module does not declare RUN_DAY_READY: run_day refuses a broker until PR 5 (the flat-by
job, #135) adds it here.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from tools.broker import ACTION_LOCK, FINAL_STATUSES, BrokerError, is_close_order

CONFIRM_WAIT_S = 15.0
POLL_S = 1.0

FLAT = "flat"  # no position in the symbol
CLOSING = "closing"  # a close is working (ours, or one we just placed)
WAITING = "waiting"  # not done this pass (orders not gone yet, legs still holding shares)
SKIPPED_UNREADABLE = "skipped_unreadable"  # an unreadable order may be a working close
FAILED = "failed"  # a read failed: nothing was closed blindly; the next pass tries again


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
        if closing:
            return FlattenResult(CLOSING, f"a close of {symbol} is already working", actions)
        try:
            order = broker.close_position(symbol)
            actions.append(f"close {symbol} ({order['id']})")
            return FlattenResult(CLOSING, f"close of {symbol} placed", actions)
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
