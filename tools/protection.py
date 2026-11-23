"""Is an open position protected by a working stop? (Phase 5 PR 4, #155; Ivan's rule of
2026-11-17 on #196, refined by the paper probe.) Pure: no I/O, no clock.

The rule: every open position must always have a working stop covering its full quantity;
anything else is flattened at market and alerted. What the broker shows decides, never an
assumption about how Alpaca handles legs after a partial fill.

The input is the entry order AS LOOKED UP WITH ITS LEGS (tools.broker.get_order_by_client_id,
nested=true). The open-orders listing is never used: it does not show a held stop (#196).

A stop leg is WORKING when it is "held" (Alpaca monitors it and triggers it at its price)
and all of these hold:
  - the entry can no longer fill: "filled", or cancelled / expired / done for the day with
    a fill (a partial fill whose remainder is gone);
  - the sibling take-profit leg is live (new, accepted, pending_new, partially_filled);
  - the stop is a sell at the plan's stop price, and both legs are sized to the entry's
    fill, with exactly the position's quantity left (never fewer shares, never more).
A filled leg, or a stop that triggered and is selling, is EXECUTING: the bracket is closing
the position.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from tools.broker import FINAL_STATUSES

PROTECTED = "protected"  # a working stop covers the whole position
EXECUTING = "executing"  # the stop has triggered and is selling: the bracket is closing it
PENDING = "pending"  # the entry can still fill: never flatten while it can
UNPROTECTED = "unprotected"  # anything else: flatten at market and alert

LIVE = frozenset({"new", "accepted", "pending_new", "partially_filled"})
STOP_TYPES = frozenset({"stop", "stop_limit"})


def _dec(value: object) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def entry_can_fill(entry: dict) -> bool:
    """The entry order may still fill more shares (it is not in a final status)."""
    return entry.get("status") not in FINAL_STATUSES


def stop_status(entry: dict, position_qty: object, *,
                stop_price: object = None) -> dict:
    """{"state": PROTECTED | EXECUTING | PENDING | UNPROTECTED, "reason": str}.

    `entry`: the parsed entry order with its legs. `position_qty`: the shares the broker
    shows held in the symbol. `stop_price`: the plan's stop (the approved order's
    stop_loss); when given, a stop at another price is not the plan's stop."""
    held = _dec(position_qty)
    if held is None or held <= 0:
        return {"state": UNPROTECTED, "reason": "the position quantity is not readable"}
    if entry_can_fill(entry):
        # A partial fill with the entry still working: the reconciler waits one poll, then
        # cancels the remainder, confirms it, and decides from what the broker shows then.
        return {"state": PENDING, "reason": "the entry can still fill"}
    filled = _dec(entry.get("filled_qty") or "0")
    if filled is None or filled <= 0:
        return {"state": UNPROTECTED, "reason": "the entry shows no fill"}
    legs = entry.get("legs") or []
    stops = [g for g in legs if g.get("type") in STOP_TYPES and g.get("side") == "sell"]
    targets = [g for g in legs if g.get("type") == "limit" and g.get("side") == "sell"]
    if not stops:
        return {"state": UNPROTECTED, "reason": "no stop leg"}
    if len(stops) > 1:
        return {"state": UNPROTECTED, "reason": "more than one stop leg"}
    stop = stops[0]
    status = stop.get("status")
    target = targets[0] if len(targets) == 1 else None
    if status in LIVE or status == "filled" or (
            target is not None and target.get("status") == "filled"):
        # a bracket stop sits "held" until it triggers: live means it is selling now, and
        # a filled leg means the bracket is closing the position (the broker's position
        # may lag): give it time, never read it as "no stop"
        return {"state": EXECUTING, "reason": f"the bracket is closing it (stop {status}, "
                f"take-profit {target.get('status') if target else None})"}
    if status != "held":
        return {"state": UNPROTECTED, "reason": f"the stop is {status}"}
    if stop_price is not None and _dec(stop.get("stop_price")) != _dec(stop_price):
        return {"state": UNPROTECTED, "reason": "the stop is not at the plan's stop price"}
    if target is None or target.get("status") not in LIVE:
        why = ("no take-profit leg" if not targets else "more than one take-profit leg"
               if len(targets) > 1 else f"the take-profit leg is {target.get('status')}")
        return {"state": UNPROTECTED, "reason": f"the held stop is not working: {why}"}
    # Both legs sized to the entry's fill, and what is left equals the position exactly: a
    # stop for fewer shares leaves some unprotected, one for more could oversell (legs not
    # resized after a partial fill's remainder was cancelled).
    sq, tq = _dec(stop.get("qty")), _dec(target.get("qty"))
    sf, tf = _dec(stop.get("filled_qty") or "0"), _dec(target.get("filled_qty") or "0")
    if None in (sq, tq, sf, tf):
        return {"state": UNPROTECTED, "reason": "a leg's quantity is not readable"}
    if sq != filled or tq != filled:
        return {"state": UNPROTECTED, "reason": f"the legs are for {sq} and {tq} shares, "
                f"the entry filled {filled}"}
    left = filled - sf - tf
    if left != held:
        return {"state": UNPROTECTED,
                "reason": f"the stop covers {left} of {held} shares held"}
    return {"state": PROTECTED, "reason": "a held stop covers the position"}
