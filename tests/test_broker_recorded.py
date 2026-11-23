"""tools/broker.py against REAL Alpaca paper replies, recorded by the probe on 2026-11-23
(#196; tests/fixtures/broker/README.md). No network: a fake opener serves the recorded body
with its recorded HTTP status."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.send import _matches
from tests.test_broker import client, keys  # noqa: F401 - keys: autouse, the env keys
from tools.broker import (
    BrokerError,
    bracket_legs_problem,
    bracket_request,
    parse_order,
)

REC = Path(__file__).parent / "fixtures" / "broker"
CID = "probe-000000000000000000000001"
BY_CID = ("GET", "/v2/orders:by_client_order_id")


def rec(name: str) -> dict:
    return json.loads((REC / name).read_text(encoding="utf-8"))


def served(name: str) -> tuple[int, object]:
    r = rec(name)
    return r["status"], r["body"]


def probe_order() -> dict:
    """The probe's request (recorded in probe.json): SPY, 1 share, limit 718.84, target
    721.72, stop 714.52."""
    return {"symbol": "SPY", "side": "buy", "qty": 1, "type": "market",
            "order_class": "bracket", "entry": "718.34", "slippage": "0.50",
            "stop_loss": "714.52", "take_profit": "721.72"}


def body() -> dict:
    return bracket_request(probe_order(), CID)


def test_the_recorded_request_is_what_bracket_request_builds():
    b = body()
    assert (b["limit_price"], b["take_profit"], b["stop_loss"]) == (
        "718.84", {"limit_price": "721.72"}, {"stop_price": "714.52"})


# ---- 2. the bracket reply -----------------------------------------------------------------

def test_the_recorded_bracket_reply_is_accepted_with_both_legs():
    b, _ = client({("POST", "/v2/orders"): served("bracket_submit.json")})
    out = b.submit_bracket(probe_order(), CID)
    assert out["status"] == "pending_new" and out["order_class"] == "bracket"
    assert [(leg["type"], leg["status"]) for leg in out["legs"]] == [("limit", "held"),
                                                                     ("stop", "held")]
    assert bracket_legs_problem(out, body()) is None and _matches(out, body())


@pytest.mark.parametrize("change, why", [
    (lambda r: r.update(legs=r["legs"][:1]), "both legs"),
    (lambda r: r.update(legs=None), "both legs"),
    (lambda r: r["legs"][1].update(stop_price="700.00"), "stop price"),
    (lambda r: r["legs"][0].update(limit_price="730.00"), "target price"),
    (lambda r: r["legs"][1].update(qty="2"), "full quantity"),
    (lambda r: r["legs"][1].update(side="buy"), "full quantity"),
    (lambda r: r["legs"][1].update(type="limit"), "stop leg"),
    (lambda r: r.update(order_class=""), "does not match"),
    (lambda r: r.update(order_class="simple"), "does not match"),
])
def test_blocked_an_entry_reply_without_both_correct_legs_fails_closed(change, why):
    """The bracket checks stay strict for our own entries: no legs, one leg, a leg at another
    price or size, or a simple order never reads as our bracket."""
    reply = rec("bracket_submit.json")["body"]
    change(reply)
    b, _ = client({("POST", "/v2/orders"): (200, reply)})
    with pytest.raises(BrokerError) as e:
        b.submit_bracket(probe_order(), CID)
    assert why in str(e.value) or "does not match" in str(e.value)
    assert e.value.may_have_acted is True  # Alpaca answered 200: the order may exist
    try:
        assert not _matches(parse_order(reply), body())  # send's lookup check agrees
    except BrokerError:
        pass  # (a simple order of this type is refused by the parser itself)


# ---- 3. lookups show the legs ---------------------------------------------------------------

def test_the_recorded_lookups_show_both_legs_before_and_after_the_fill():
    for name, entry, legs in (("lookup_with_legs_new.json", "new", ["held", "held"]),
                              ("lookup_with_legs_filled.json", "filled", ["new", "held"])):
        b, fake = client({BY_CID: served(name)})
        out = b.get_order_by_client_id(CID)
        assert "nested=true" in fake.requests[-1].full_url
        assert out["status"] == entry
        assert [leg["status"] for leg in out["legs"]] == legs
        # the legs carry Alpaca-made client_order_ids, not ours
        assert all(leg["client_order_id"] != CID for leg in out["legs"])
        assert _matches(out, body())


def test_after_the_fill_the_stop_is_held_and_the_take_profit_new():
    b, _ = client({BY_CID: served("lookup_with_legs_filled.json")})
    out = b.get_order_by_client_id(CID)
    stop = next(leg for leg in out["legs"] if leg["type"] == "stop")
    tp = next(leg for leg in out["legs"] if leg["type"] == "limit")
    assert (stop["status"], stop["stop_price"]) == ("held", "714.52")
    assert (tp["status"], tp["limit_price"]) == ("new", "721.72")
    assert out["filled_qty"] == "1" and out["filled_avg_price"] == "718.32"


def test_the_recorded_unknown_lookup_is_none():
    b, _ = client({BY_CID: served("lookup_unknown_404.json")})
    assert b.get_order_by_client_id("probe-unknown-0000000000000000") is None


# ---- 4. the resend ------------------------------------------------------------------------

def test_the_recorded_resend_is_a_422_with_a_general_code_and_did_not_act():
    b, _ = client({("POST", "/v2/orders"): served("resend_422.json")})
    with pytest.raises(BrokerError) as e:
        b.submit_bracket(probe_order(), CID)
    assert (e.value.status, e.value.code, e.value.may_have_acted) == (422, 40010001, False)
    assert "unique" not in str(e.value)  # Alpaca's message text is never passed on


# ---- 5. the open-orders listing misses the held stop ---------------------------------------

def test_the_recorded_open_orders_listing_has_no_held_stop():
    """After the fill, GET /v2/orders?status=open lists only the take-profit leg: a stop must
    never be looked for there (the reconciler looks up the entry with its legs)."""
    b, _ = client({("GET", "/v2/orders"): served("open_orders_missing_held_stop.json")})
    orders = b.list_open_orders()
    assert [(o["type"], o["status"], o["order_class"]) for o in orders] == [
        ("limit", "new", "bracket")]
    assert not any(o["type"] == "stop" for o in orders)
    assert not any(leg["type"] == "stop" for o in orders for leg in o["legs"])


def test_the_recorded_position_with_legs_open_has_nothing_available():
    b, _ = client({("GET", "/v2/positions"): served("positions_legs_open.json")})
    (p,) = b.list_positions()
    assert (p["symbol"], p["qty"], p["qty_available"]) == ("SPY", "1", "0")


# ---- 6. closing ---------------------------------------------------------------------------

def test_the_recorded_close_with_legs_open_is_a_403_that_did_not_act():
    b, _ = client({("DELETE", "/v2/positions/SPY"): served("close_with_legs_403.json")})
    with pytest.raises(BrokerError) as e:
        b.close_position("SPY")
    assert (e.value.status, e.value.code, e.value.may_have_acted) == (403, 40310000, False)


def test_the_recorded_simple_close_reply_is_accepted():
    """order_class "" (#196 finding 7): the close FILLED at Alpaca while this reply was
    refused here."""
    b, _ = client({("DELETE", "/v2/positions/SPY"): served("close_position_simple.json")})
    out = b.close_position("SPY")
    assert (out["order_class"], out["type"], out["side"], out["status"]) == (
        "simple", "market", "sell", "pending_new")
    assert out["legs"] == []


def test_the_recorded_listing_with_the_close_working_is_read():
    """The cleanup's listing failed on this reply (#196 finding 7)."""
    b, _ = client({("GET", "/v2/orders"): served("open_orders_with_close.json")})
    (o,) = b.list_open_orders()
    assert (o["order_class"], o["type"], o["side"]) == ("simple", "market", "sell")


@pytest.mark.parametrize("cls", [None, "", "simple"])
@pytest.mark.parametrize("kind", ["market", "limit"])
def test_a_simple_market_or_limit_order_is_read_as_simple(cls, kind):
    reply = rec("close_position_simple.json")["body"]
    reply.update(type=kind, limit_price="718.00" if kind == "limit" else None)
    if cls is None:
        reply.pop("order_class")
    else:
        reply["order_class"] = cls
    assert parse_order(reply)["order_class"] == "simple"


@pytest.mark.parametrize("change", [
    {"order_class": "", "type": "stop"}, {"order_class": None, "type": "trailing_stop"},
    {"order_class": 3}, {"order_class": ["bracket"]},
])
def test_blocked_an_odd_simple_order_or_class_is_refused(change):
    reply = {**rec("close_position_simple.json")["body"], **change}
    with pytest.raises(BrokerError):
        parse_order(reply)


# ---- an unclear close: find out, never retry blindly ----------------------------------------

def unreadable_close():
    """The recorded close reply with a field this parser cannot read: Alpaca answered 200
    and the close is working, but the reply is refused here (as #196 step 6 was)."""
    body_ = rec("close_position_simple.json")["body"]
    body_["status"] = "some_new_status"
    return 200, body_


def test_an_unreadable_close_reply_is_may_have_acted_never_a_plain_failure():
    b, fake = client({("DELETE", "/v2/positions/SPY"): unreadable_close()})
    with pytest.raises(BrokerError) as e:
        b.close_position("SPY")
    assert e.value.may_have_acted is True and e.value.status == 200
    assert "the close may have been placed" in str(e.value)
    assert sum(r.get_method() == "DELETE" for r in fake.requests) == 1


@pytest.mark.parametrize("change", [{"symbol": "QQQ"}, {"side": "buy"},
                                    {"order_class": "bracket"}])
def test_a_close_reply_that_is_not_a_simple_sell_in_the_symbol_may_have_acted(change):
    body_ = {**rec("close_position_simple.json")["body"], **change}
    b, _ = client({("DELETE", "/v2/positions/SPY"): (200, body_)})
    with pytest.raises(BrokerError) as e:
        b.close_position("SPY")
    assert e.value.may_have_acted is True


@pytest.mark.parametrize("positions, orders, state", [
    ([], "open_orders_with_close.json", "closed"),
    ("positions_legs_open.json", "open_orders_with_close.json", "closing"),
    ("positions_legs_open.json", "open_orders_missing_held_stop.json", "open"),
])
def test_check_close_reads_the_position_and_the_orders_and_sends_nothing(positions, orders,
                                                                        state):
    pos = (200, []) if positions == [] else served(positions)
    b, fake = client({("GET", "/v2/positions"): pos, ("GET", "/v2/orders"): served(orders)})
    out = b.check_close("SPY")
    assert out["state"] == state
    q = fake.requests[-1].full_url
    assert "status=all" in q and "symbols=SPY" in q and "nested=true" in q
    assert all(r.get_method() == "GET" for r in fake.requests)  # read only


def test_a_filled_close_reads_as_closed_not_open():
    filled = rec("close_position_simple.json")["body"]
    filled.update(status="filled", filled_qty="1", filled_avg_price="718.79")
    b, _ = client({("GET", "/v2/positions"): (200, []), ("GET", "/v2/orders"): (200, [filled])})
    out = b.check_close("SPY")
    assert out["state"] == "closed" and out["close_orders"][0]["status"] == "filled"


def test_after_an_unclear_close_the_code_checks_and_does_not_close_again():
    """The pattern for every caller (the probe today, flatten in PR 5): an unclear close is
    followed by check_close, which finds the close working, and no second DELETE."""
    routes = {("DELETE", "/v2/positions/SPY"): unreadable_close(),
              ("GET", "/v2/positions"): served("positions_legs_open.json"),
              ("GET", "/v2/orders"): served("open_orders_with_close.json")}
    b, fake = client(routes)
    try:
        b.close_position("SPY")
        state = "sent"
    except BrokerError as exc:
        assert exc.may_have_acted
        state = b.check_close("SPY")["state"]
    assert state == "closing"
    assert [r.get_method() for r in fake.requests].count("DELETE") == 1


@pytest.mark.parametrize("stopped", ["stopped", "suspended", "pending_new", "held"])
def test_a_close_in_an_unsure_status_counts_as_working(stopped):
    working = {**rec("close_position_simple.json")["body"], "status": stopped}
    b, _ = client({("GET", "/v2/positions"): served("positions_legs_open.json"),
                   ("GET", "/v2/orders"): (200, [working])})
    assert b.check_close("SPY")["state"] == "closing"


# ---- may_have_acted on the transport ---------------------------------------------------------

@pytest.mark.parametrize("answer, acted", [
    ((403, {"code": 40310000}), False), ((422, {"code": 40010001}), False),
    ((404, {"code": 40410000}), False),
    ((500, {"code": 50010000}), True), ((503, None), True),
    ((201, {}), True),  # an unexpected success status
    ((200, b"<html>"), True),  # an unreadable 2xx reply
    (TimeoutError(), True), (OSError("reset"), True),
])
def test_only_a_4xx_refusal_says_the_request_did_not_act(answer, acted):
    b, _ = client({("DELETE", "/v2/positions/SPY"): answer})
    with pytest.raises(BrokerError) as e:
        b.close_position("SPY")
    assert e.value.may_have_acted is acted


def test_a_refusal_before_any_request_did_not_act():
    b, fake = client()
    with pytest.raises(BrokerError) as e:
        b.close_position("not a symbol!")
    assert e.value.may_have_acted is False
    assert all(r.get_method() == "GET" for r in fake.requests)
