"""tools/protection.py and tools/realized.py (Phase 5 PR 4, #155): pure, no I/O. The entry
shapes come from the paper probe's recorded replies (tests/fixtures/broker/, #196)."""
from __future__ import annotations

import copy
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from tools.broker import parse_order
from tools.protection import EXECUTING, PENDING, PROTECTED, UNPROTECTED, stop_status
from tools.realized import RealizedError, realized_pnl

REC = Path(__file__).parent / "fixtures" / "broker"


def recorded(name: str) -> dict:
    return parse_order(json.loads((REC / name).read_text(encoding="utf-8"))["body"])


FILLED = recorded("lookup_with_legs_filled.json")  # entry filled; take-profit new, stop held
NEW = recorded("lookup_with_legs_new.json")  # entry new; both legs held


def entry(base=FILLED, *, status=None, filled=None, stop=None, tp=None, legs=None):
    e = copy.deepcopy(base)
    if status is not None:
        e["status"] = status
    if filled is not None:
        e["filled_qty"] = filled
    for leg in e["legs"]:
        if leg["type"] == "stop" and stop:
            leg.update(stop)
        if leg["type"] == "limit" and tp:
            leg.update(tp)
    if legs is not None:
        e["legs"] = legs(e["legs"])
    return e


def state(e, qty="1", **kw):
    return stop_status(e, qty, **kw)["state"]


# ---- protection -------------------------------------------------------------------------

def test_the_recorded_filled_bracket_is_protected():
    """#196: after the fill the take-profit is new and the stop held: a working stop."""
    assert state(FILLED, stop_price="714.52") == PROTECTED


def test_the_entry_can_still_fill_so_never_flatten():
    assert state(NEW) == PENDING
    assert state(entry(status="partially_filled", filled="1")) == PENDING


@pytest.mark.parametrize("tp_status, want", [
    ("new", PROTECTED), ("accepted", PROTECTED), ("pending_new", PROTECTED),
    ("partially_filled", PROTECTED),
    ("held", UNPROTECTED), ("canceled", UNPROTECTED),
    ("filled", EXECUTING),  # the take-profit filled: the bracket is closing it
    ("expired", UNPROTECTED),
])
def test_a_held_stop_works_only_with_a_live_take_profit(tp_status, want):
    assert state(entry(tp={"status": tp_status})) == want


@pytest.mark.parametrize("stop_status_, want", [
    ("held", PROTECTED),
    ("new", EXECUTING), ("accepted", EXECUTING), ("pending_new", EXECUTING),
    ("partially_filled", EXECUTING),
    ("canceled", UNPROTECTED), ("expired", UNPROTECTED), ("rejected", UNPROTECTED),
    ("replaced", UNPROTECTED), ("done_for_day", UNPROTECTED), ("suspended", UNPROTECTED),
    ("pending_cancel", UNPROTECTED), ("stopped", UNPROTECTED),
    ("filled", EXECUTING),  # the stop filled: the bracket is closing it
])
def test_every_stop_status(stop_status_, want):
    assert state(entry(stop={"status": stop_status_})) == want


def test_a_partial_fill_with_its_remainder_cancelled_is_judged_by_what_the_broker_shows():
    """Ivan's change: no assumption about Alpaca's legs after a partial fill. Legs kept and
    covering the filled shares: protected. Legs cancelled with the remainder: unprotected."""
    kept = entry(status="canceled", filled="1")
    assert state(kept) == PROTECTED
    gone = entry(status="canceled", filled="1", stop={"status": "canceled"},
                 tp={"status": "canceled"})
    assert state(gone) == UNPROTECTED


@pytest.mark.parametrize("change, why", [
    (dict(legs=lambda legs: [g for g in legs if g["type"] != "stop"]), "no stop leg"),
    (dict(legs=lambda legs: legs + [dict(legs[1])]), "more than one stop leg"),
    (dict(legs=lambda legs: [g for g in legs if g["type"] != "limit"]), "no take-profit leg"),
    (dict(stop={"qty": "1", "filled_qty": "1", "status": "held"}), "covers 0 of 1"),
])
def test_unprotected_shapes(change, why):
    v = stop_status(entry(**change), "1")
    assert v["state"] == UNPROTECTED and why in v["reason"]


def test_a_stop_for_fewer_shares_than_held_is_unprotected():
    assert state(FILLED, qty="2") == UNPROTECTED  # the stop is for 1, the position is 2


def test_a_stop_at_another_price_than_the_plans_is_unprotected():
    assert state(FILLED, stop_price="700.00") == UNPROTECTED


@pytest.mark.parametrize("qty", ["0", "-1", "abc", None])
def test_an_unreadable_position_quantity_is_unprotected(qty):
    assert state(FILLED, qty=qty) == UNPROTECTED


def test_an_entry_with_no_fill_holding_a_position_is_unprotected():
    assert state(entry(status="canceled", filled="0")) == UNPROTECTED


def test_the_listing_is_not_an_input():
    """stop_status takes the entry with its legs; there is no way to pass the open-orders
    listing (which hides a held stop, #196)."""
    import inspect
    assert list(inspect.signature(stop_status).parameters) == ["entry", "position_qty",
                                                               "stop_price"]


# ---- realized P&L -----------------------------------------------------------------------

def test_a_target_exit():
    assert realized_pnl(1, "718.32", [(1, "721.72")]) == D("3.40")


def test_a_stop_exit_is_a_loss():
    assert realized_pnl(1, "718.32", [(1, "714.52")]) == D("-3.80")


def test_a_partial_target_then_the_stop():
    assert realized_pnl(10, "100.00", [(4, "102.00"), (6, "98.50")]) == D("-1.00")


def test_a_loss_is_rounded_down_never_understated():
    # 3 x (100.333 - 100.00) = 0.999 -> 0.99; 3 x (99.997 - 100) = -0.009 -> -0.01
    assert realized_pnl(3, "100.00", [(3, "100.333")]) == D("0.99")
    assert realized_pnl(3, "100.00", [(3, "99.997")]) == D("-0.01")


@pytest.mark.parametrize("exits", [[], [(1, "100")], [(3, "100")], [(2, "0")], [(2, "x")],
                                   [(0, "100"), (2, "100")]])
def test_blocked_exits_that_are_not_one_closed_trade(exits):
    with pytest.raises(RealizedError):
        realized_pnl(2, "100.00", exits)


@pytest.mark.parametrize("qty, avg", [(0, "100"), (2, "0"), (2, None), ("x", "100")])
def test_blocked_a_bad_entry(qty, avg):
    with pytest.raises(RealizedError):
        realized_pnl(qty, avg, [(2, "100")])
