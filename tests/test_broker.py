"""The Alpaca PAPER broker client (Phase 5 PR 2, #154, with #136). Fake openers and synthetic
fixtures in Alpaca's documented shapes; no network."""
from __future__ import annotations

import http.client
import inspect
import io
import json
import urllib.error
import urllib.parse
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

import tools.broker as broker_mod
from tests.order_helpers import sized_order
from tools.broker import (
    PAPER_BASE_URL,
    AlpacaPaperBroker,
    BrokerError,
    bracket_request,
    parse_order,
)
from tools.config import CONFIG_DIR, load_limits

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
FIX = Path(__file__).parent / "fixtures" / "alpaca_broker"
KEY, SECRET = "SENTINEL-PAPER-KEY-ID", "SENTINEL-PAPER-SECRET"
CID = "ta-0123456789abcdef0123456789abcdef"


def fx(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", KEY)
    monkeypatch.setenv("ALPACA_SECRET_KEY", SECRET)


class Resp:
    def __init__(self, status, body, url):
        self.status, self._b, self._url = status, body, url

    def read(self, n=None):
        return self._b if n is None else self._b[:n]

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeAlpaca:
    """An opener answering by (method, path): a (status, body) pair, an exception, or a
    callable(req) -> either. Records every request."""

    def __init__(self, routes=None):
        self.routes = {("GET", "/v2/account"): (200, fx("account.json")), **(routes or {})}
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        parts = urllib.parse.urlsplit(req.full_url)
        key = (req.get_method(), parts.path)
        if key not in self.routes:
            raise AssertionError(f"unexpected request {key}")
        out = self.routes[key]
        if callable(out):
            out = out(req)
        if isinstance(out, Resp):
            return out
        if isinstance(out, BaseException):
            raise out
        status, body = out
        raw = body if isinstance(body, bytes) else (b"" if body is None
                                                   else json.dumps(body).encode())
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(raw))
        return Resp(status, raw, req.full_url)


def client(routes=None, *, verified=True):
    fake = FakeAlpaca(routes)
    b = AlpacaPaperBroker(LIMITS, opener=fake)
    if verified:
        b.verify_paper_account()
    return b, fake


def order_for(**over):
    """A real risk-checked order: TSLA 8 shares, entry 364.18, stop 358.32, target 370.04,
    allowance 0.37 (tests.order_helpers.sized_order)."""
    o = dict(sized_order())
    o.update(over)
    return o


# ======================= paper only =======================

def test_the_base_url_is_the_paper_host_and_cannot_be_passed_in():
    assert PAPER_BASE_URL == "https://paper-api.alpaca.markets"
    params = inspect.signature(AlpacaPaperBroker).parameters
    assert set(params) == {"limits", "opener", "timeout"}
    src = Path(broker_mod.__file__).read_text(encoding="utf-8")
    assert '"https://api.alpaca.markets' not in src  # no live host anywhere in the code


@pytest.mark.parametrize("paper", [False, None, "true", 1])
def test_blocked_the_client_refuses_to_start_unless_paper_is_true(paper):
    with pytest.raises(BrokerError, match="paper only: limits.paper must be true"):
        AlpacaPaperBroker(replace(LIMITS, paper=paper), opener=FakeAlpaca())


@pytest.mark.parametrize("unset", ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"])
def test_blocked_missing_keys_refuse_without_a_request(monkeypatch, unset):
    monkeypatch.setenv(unset, " ")
    fake = FakeAlpaca()
    with pytest.raises(BrokerError, match="must be set"):
        AlpacaPaperBroker(LIMITS, opener=fake)
    assert fake.requests == []


@pytest.mark.parametrize("call", [
    lambda b: b.submit_bracket(order_for(), CID), lambda b: b.get_order_by_client_id(CID),
    lambda b: b.list_open_orders(), lambda b: b.cancel_order("ord-1"),
    lambda b: b.list_positions(), lambda b: b.close_position("TSLA"), lambda b: b.close_all(),
    lambda b: b.account(), lambda b: b.clock(),
], ids=["submit", "get", "open", "cancel", "positions", "close", "close_all", "account",
        "clock"])
def test_blocked_every_call_refuses_before_the_paper_account_is_verified(call):
    b, fake = client(verified=False)
    with pytest.raises(BrokerError, match="verify_paper_account"):
        call(b)
    assert fake.requests == []


def test_verify_reads_the_account_from_the_paper_host_with_the_key_headers():
    b, fake = client(verified=False)
    assert b.verify_paper_account() == {"account_number": "PA0SYNTHETIC", "status": "ACTIVE",
                                        "currency": "USD", "cash": "100000.00",
                                        "buying_power": "200000.00"}
    (req,) = fake.requests
    assert req.full_url == "https://paper-api.alpaca.markets/v2/account"
    assert req.get_header("Apca-api-key-id") == KEY
    assert req.get_header("Apca-api-secret-key") == SECRET


@pytest.mark.parametrize("over,why", [
    ({"status": "ACCOUNT_CLOSED"}, "not ACTIVE"), ({"status": "PAPER_ONLY"}, "not ACTIVE"),
    ({"trading_blocked": True}, "blocked"), ({"account_blocked": True}, "blocked"),
    ({"trading_blocked": None}, "blocked"),
], ids=["closed", "paper-only-status", "trading-blocked", "account-blocked", "blocked-null"])
def test_blocked_an_account_that_is_not_active_and_unblocked_is_refused(over, why):
    fake = FakeAlpaca({("GET", "/v2/account"): (200, {**fx("account.json"), **over})})
    b = AlpacaPaperBroker(LIMITS, opener=fake)
    with pytest.raises(BrokerError, match=why):
        b.verify_paper_account()
    with pytest.raises(BrokerError, match="verify_paper_account"):
        b.list_positions()


def test_blocked_live_keys_on_the_paper_host_fail_the_verification():
    """Alpaca keeps separate paper and live keys: the paper host refuses live keys."""
    fake = FakeAlpaca({("GET", "/v2/account"): (401, {"code": 40110000,
                                                     "message": "request is not authorized"})})
    b = AlpacaPaperBroker(LIMITS, opener=fake)
    with pytest.raises(BrokerError, match=r"^GET /v2/account failed: HTTP 401$") as exc:
        b.verify_paper_account()
    assert exc.value.status == 401 and exc.value.code == 40110000


# ======================= the bracket request (#136) =======================

def test_the_request_is_a_limit_bracket_at_entry_plus_allowance():
    o = order_for()
    assert (o["entry"], o["stop_loss"], o["take_profit"], o["slippage"]) == (
        "364.18", "358.32", "370.04", "0.37")
    assert bracket_request(o, CID) == {
        "symbol": "TSLA", "qty": "8", "side": "buy", "type": "limit",
        "limit_price": "364.55", "time_in_force": "day", "order_class": "bracket",
        "take_profit": {"limit_price": "370.04"}, "stop_loss": {"stop_price": "358.32"},
        "client_order_id": CID,
    }


def test_internal_fields_are_never_forwarded():
    body = json.dumps(bracket_request(order_for(), CID))
    for internal in ("slippage", "entry", "market", "0.37", "364.18"):
        assert internal not in body, internal


def test_an_extra_order_field_is_not_forwarded_either():
    req = bracket_request(order_for(confidence=0.99, note="x"), CID)
    assert "confidence" not in json.dumps(req) and "note" not in json.dumps(req)


@pytest.mark.parametrize("over,why", [
    ({"side": "sell"}, "only long bracket"), ({"order_class": "simple"}, "only long bracket"),
    ({"qty": 0}, "qty"), ({"qty": True}, "qty"), ({"qty": "8"}, "qty"), ({"qty": 8.0}, "qty"),
    ({"symbol": "tsla"}, "canonical"), ({"symbol": "TS LA"}, "canonical"),
    ({"entry": "364.185"}, "whole-cent"), ({"stop_loss": "0"}, "whole-cent"),
    ({"take_profit": None}, "not a price"), ({"entry": True}, "not a price"),
    ({"slippage": None}, "slippage"), ({"slippage": "-0.01"}, "slippage"),
    ({"slippage": "0.005"}, "slippage"), ({"slippage": "NaN"}, "slippage"),
    ({"entry": "0.90", "stop_loss": "0.80", "take_profit": "1.20", "slippage": "0.01"},
     "below 1.00"),
    ({"stop_loss": "364.55"}, "at least 0.01 below"),
    ({"stop_loss": "364.56"}, "at least 0.01 below"),
    ({"take_profit": "364.55"}, "target must be above the entry limit price"),
    ({"take_profit": "364.20"}, "target must be above the entry limit price"),
], ids=lambda x: x if isinstance(x, str) else None)
def test_blocked_an_order_alpaca_would_reject_or_this_phase_never_sends(over, why):
    with pytest.raises(BrokerError, match=why):
        bracket_request(order_for(**over), CID)


@pytest.mark.parametrize("cid", ["", "x" * 129, "ta id", "ta/1", None, 7])
def test_blocked_a_bad_client_order_id(cid):
    with pytest.raises(BrokerError, match="client_order_id"):
        bracket_request(order_for(), cid)


def test_a_stop_one_cent_under_the_limit_price_is_allowed():
    assert bracket_request(order_for(stop_loss="364.54", take_profit="370.04"),
                           CID)["stop_loss"] == {"stop_price": "364.54"}


def test_blocked_a_non_object_order():
    with pytest.raises(BrokerError, match="order must be an object"):
        bracket_request(["TSLA"], CID)  # type: ignore[arg-type]


# ======================= submit and look up =======================

def test_submit_posts_exactly_the_bracket_request_and_parses_the_reply():
    b, fake = client({("POST", "/v2/orders"): (200, fx("order_bracket_new.json"))})
    out = b.submit_bracket(order_for(), CID)
    req = fake.requests[-1]
    assert req.get_method() == "POST" and req.full_url == PAPER_BASE_URL + "/v2/orders"
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == bracket_request(order_for(), CID)
    assert out["client_order_id"] == CID and out["status"] == "accepted"
    assert [leg["stop_price"] for leg in out["legs"]] == [None, "358.32"]


def test_blocked_a_refused_order_is_a_broker_error_without_alpacas_message():
    reply = {"code": 40310000, "message": f"insufficient buying power {KEY}"}
    b, _ = client({("POST", "/v2/orders"): (403, reply)})
    with pytest.raises(BrokerError) as exc:
        b.submit_bracket(order_for(), CID)
    assert str(exc.value) == "POST /v2/orders failed: HTTP 403"
    assert exc.value.status == 403 and exc.value.code == 40310000
    assert KEY not in str(exc.value) and "insufficient" not in str(exc.value)


def test_blocked_an_invalid_bracket_is_never_sent():
    b, fake = client({("POST", "/v2/orders"): (200, fx("order_bracket_new.json"))})
    with pytest.raises(BrokerError):
        b.submit_bracket(order_for(take_profit="364.20"), CID)
    assert [r.get_method() for r in fake.requests] == ["GET"]  # only the account check


def test_get_by_client_order_id_queries_it_and_parses():
    b, fake = client({("GET", "/v2/orders:by_client_order_id"):
                      (200, fx("order_bracket_filled.json"))})
    out = b.get_order_by_client_id(CID)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.requests[-1].full_url).query)
    assert q == {"client_order_id": [CID], "nested": ["true"]}
    assert out["status"] == "filled" and out["filled_qty"] == "8"
    assert out["filled_avg_price"] == "364.20"


def test_a_404_lookup_is_none_and_other_errors_raise():
    b, _ = client({("GET", "/v2/orders:by_client_order_id"): (404, {"code": 40410000})})
    assert b.get_order_by_client_id(CID) is None
    b, _ = client({("GET", "/v2/orders:by_client_order_id"): (500, {"code": 50010000})})
    with pytest.raises(BrokerError, match="HTTP 500"):
        b.get_order_by_client_id(CID)


def test_blocked_a_bad_client_order_id_is_never_looked_up():
    b, fake = client()
    with pytest.raises(BrokerError, match="client_order_id"):
        b.get_order_by_client_id("ta id&x=1")
    assert len(fake.requests) == 1


def test_list_open_orders():
    b, fake = client({("GET", "/v2/orders"): (200, fx("orders_open.json"))})
    (o,) = b.list_open_orders()
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.requests[-1].full_url).query)
    assert q == {"status": ["open"], "nested": ["true"], "limit": ["500"]}
    assert o["id"] == "ord-1" and len(o["legs"]) == 2


def test_cancel_order_accepts_204_and_refuses_a_bad_id():
    b, fake = client({("DELETE", "/v2/orders/ord-1"): (204, None)})
    assert b.cancel_order("ord-1") is None
    with pytest.raises(BrokerError, match="order id"):
        b.cancel_order("../v2/positions")
    assert [r.get_method() for r in fake.requests] == ["GET", "DELETE"]


# ======================= positions, closing, clock =======================

def test_list_positions():
    b, _ = client({("GET", "/v2/positions"): (200, fx("positions.json"))})
    assert b.list_positions() == [{"symbol": "TSLA", "side": "long", "qty": "8",
                                   "qty_available": "0", "avg_entry_price": "364.20",
                                   "current_price": "364.40", "market_value": "2915.20",
                                   "unrealized_pl": "1.60"}]


def test_close_position_deletes_that_symbol():
    b, fake = client({("DELETE", "/v2/positions/BRK.B"): (200,
                                                           fx("close_position_order.json"))})
    out = b.close_position("brk.b")
    assert fake.requests[-1].full_url == PAPER_BASE_URL + "/v2/positions/BRK.B"
    assert out["side"] == "sell" and out["type"] == "market"


def test_close_all_cancels_orders_and_reports_each_position():
    b, fake = client({("DELETE", "/v2/positions"): (207, fx("close_all_207.json"))})
    out = b.close_all()
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.requests[-1].full_url).query)
    assert q == {"cancel_orders": ["true"]}
    assert out[0]["symbol"] == "TSLA" and out[0]["order"]["id"] == "ord-close-1"
    assert out[1] == {"symbol": "NVDA", "status": 403, "error_code": 40310000}


def test_clock():
    b, _ = client({("GET", "/v2/clock"): (200, fx("clock.json"))})
    assert b.clock() == {"timestamp": "2026-11-16T10:30:00-05:00", "is_open": True,
                         "next_open": "2026-11-17T09:30:00-05:00",
                         "next_close": "2026-11-16T16:00:00-05:00"}


# ======================= errors fail closed and stay safe =======================

@pytest.mark.parametrize("exc,why", [
    (TimeoutError(KEY), "timed out"),
    (urllib.error.URLError(ConnectionRefusedError(KEY)), "network error (ConnectionRefusedError)"),
    (OSError(KEY), "network error (OSError)"),
], ids=["timeout", "refused", "os"])
def test_blocked_transport_failures_name_the_path_only(exc, why):
    b, _ = client({("GET", "/v2/positions"): exc})
    with pytest.raises(BrokerError) as err:
        b.list_positions()
    assert str(err.value).endswith(why), str(err.value)
    assert str(err.value).startswith("GET /v2/positions") and KEY not in str(err.value)


def test_blocked_a_reply_from_another_origin_is_refused():
    """A reply whose final URL is another host (e.g. the live API) is never used."""
    b, fake = client({("GET", "/v2/positions"): lambda req: Resp(
        200, b"[]", "https://api.alpaca.markets/v2/positions")})
    with pytest.raises(BrokerError, match="different origin"):
        b.list_positions()


@pytest.mark.parametrize("raw,why", [(b"<html>", "not JSON"), (b"x" * 2_000_001, "too large")],
                         ids=["html", "huge"])
def test_blocked_a_reply_that_is_not_usable_json(raw, why):
    b, _ = client({("GET", "/v2/positions"): (200, raw)})
    with pytest.raises(BrokerError, match=why):
        b.list_positions()


@pytest.mark.parametrize("over,why", [
    ({"status": "teleported"}, "unknown order status"), ({"id": None}, "no id"),
    ({"client_order_id": ""}, "no client_order_id"), ({"filled_qty": None}, "filled_qty"),
    ({"filled_qty": "NaN"}, "filled_qty"), ({"qty": True}, "qty"),
    ({"filled_avg_price": "abc"}, "filled_avg_price"), ({"legs": "x"}, "legs"),
], ids=lambda x: x if isinstance(x, str) else None)
def test_blocked_an_unexpected_order_reply_never_reads_as_a_fill(over, why):
    with pytest.raises(BrokerError, match=why):
        parse_order({**fx("order_bracket_filled.json"), **over})


def test_blocked_an_unexpected_position_or_clock_reply():
    b, _ = client({("GET", "/v2/positions"): (200, [{**fx("positions.json")[0], "side": "x"}]),
                   ("GET", "/v2/clock"): (200, {"is_open": "yes"})})
    with pytest.raises(BrokerError, match="side is unknown"):
        b.list_positions()
    with pytest.raises(BrokerError, match="not a clock"):
        b.clock()


def test_an_unexpected_success_status_is_refused():
    b, _ = client({("POST", "/v2/orders"): (202, fx("order_bracket_new.json"))})
    with pytest.raises(BrokerError, match="unexpected HTTP 202"):
        b.submit_bracket(order_for(), CID)


def test_the_example_account_cash_stays_the_sizing_limit():
    """Decision 14 (#154): the paper account holds more, but sizing uses account_cash."""
    b, _ = client()
    assert D(b.account()["cash"]) > LIMITS.account_cash == D("5000.00")


# ======================= review fixes =======================

@pytest.mark.parametrize("over,want", [
    ({"take_profit": "370.040"}, ("364.55", "358.32", "370.04")),
    ({"entry": "364.180", "slippage": "0.370"}, ("364.55", "358.32", "370.04")),
    ({"slippage": "0E-5"}, ("364.18", "358.32", "370.04")),
    ({"take_profit": "3.7E+2"}, ("364.55", "358.32", "370.00")),
], ids=["trailing-zero", "entry-trailing", "exponent-zero", "exponent"])
def test_prices_are_sent_as_canonical_two_decimal_strings(over, want):
    req = bracket_request(order_for(**over), CID)
    assert (req["limit_price"], req["stop_loss"]["stop_price"],
            req["take_profit"]["limit_price"]) == want


@pytest.mark.parametrize("over", [{"entry": "1E+30"}, {"slippage": "1E+30"},
                                  {"take_profit": "1E+999999"}], ids=["entry", "slip", "huge"])
def test_blocked_an_absurd_price_is_a_broker_error_not_a_decimal_error(over):
    with pytest.raises(BrokerError):
        bracket_request(order_for(**over), CID)


@pytest.mark.parametrize("exc", [http.client.IncompleteRead(b"par"),
                                 http.client.BadStatusLine("x")], ids=["incomplete", "badline"])
def test_blocked_a_broken_http_reply_is_a_broker_error(exc):
    b, _ = client({("GET", "/v2/positions"): exc})
    with pytest.raises(BrokerError, match=r"^GET /v2/positions: incomplete response"):
        b.list_positions()


def test_a_timeout_wrapped_in_url_error_is_reported_as_a_timeout():
    b, _ = client({("GET", "/v2/positions"): urllib.error.URLError(TimeoutError())})
    with pytest.raises(BrokerError, match=r"^GET /v2/positions timed out$"):
        b.list_positions()


def test_blocked_a_failed_re_verification_blocks_every_later_call():
    b, fake = client({("POST", "/v2/orders"): (200, fx("order_bracket_new.json"))})
    fake.routes[("GET", "/v2/account")] = (200, {**fx("account.json"), "trading_blocked": True})
    with pytest.raises(BrokerError, match="blocked"):
        b.account()
    with pytest.raises(BrokerError, match="verify_paper_account"):
        b.submit_bracket(order_for(), CID)
    assert [r.get_method() for r in fake.requests] == ["GET", "GET"]


@pytest.mark.parametrize("over", [
    {"client_order_id": "ta-ffffffffffffffffffffffffffffffff"}, {"symbol": "AAPL"},
    {"side": "sell"}, {"qty": "9"}, {"order_class": "simple"},
], ids=["cid", "symbol", "side", "qty", "class"])
def test_blocked_a_submit_reply_that_does_not_match_the_order(over):
    b, _ = client({("POST", "/v2/orders"): (200, {**fx("order_bracket_new.json"), **over})})
    with pytest.raises(BrokerError, match="does not match the order sent"):
        b.submit_bracket(order_for(), CID)


def test_blocked_a_lookup_reply_for_another_client_order_id():
    reply = {**fx("order_bracket_filled.json"), "client_order_id": "ta-other"}
    b, _ = client({("GET", "/v2/orders:by_client_order_id"): (200, reply)})
    with pytest.raises(BrokerError, match="another client_order_id"):
        b.get_order_by_client_id(CID)


def test_blocked_an_order_reply_with_an_unknown_side():
    with pytest.raises(BrokerError, match="unknown order side"):
        parse_order({**fx("order_bracket_new.json"), "side": "short"})


def test_the_order_reply_carries_the_timestamps_pr3_needs():
    out = parse_order(fx("order_bracket_filled.json"))
    assert out["time_in_force"] == "day" and out["updated_at"] and out["filled_at"]
    assert out["canceled_at"] is None and out["expired_at"] is None


def test_a_position_with_odd_display_fields_still_lists():
    """The exit path (listing positions to flatten them) never fails on a display field."""
    pos = {**fx("positions.json")[0], "market_value": None, "unrealized_pl": "abc",
           "current_price": None}
    b, _ = client({("GET", "/v2/positions"): (200, [pos])})
    (out,) = b.list_positions()
    assert out["qty"] == "8" and out["market_value"] is None and out["unrealized_pl"] is None


def test_close_all_keeps_every_result_when_one_order_reply_is_unreadable():
    results = fx("close_all_207.json")
    results[0]["body"]["status"] = "teleported"
    b, _ = client({("DELETE", "/v2/positions"): (207, results)})
    out = b.close_all()
    assert out[0]["symbol"] == "TSLA" and out[0]["order"] is None
    assert "unknown order status" in out[0]["error"]
    assert out[1] == {"symbol": "NVDA", "status": 403, "error_code": 40310000}


@pytest.mark.parametrize("body", [None, []], ids=["empty", "list"])
def test_close_all_with_nothing_to_close(body):
    b, _ = client({("DELETE", "/v2/positions"): (207, body)})
    assert b.close_all() == []


def test_a_not_cancelable_order_is_a_broker_error():
    b, _ = client({("DELETE", "/v2/orders/ord-1"): (422, {"code": 42210000})})
    with pytest.raises(BrokerError, match="HTTP 422") as exc:
        b.cancel_order("ord-1")
    assert exc.value.code == 42210000


def test_the_injected_timeout_reaches_the_opener():
    seen = []

    def opener(req, timeout=None):
        seen.append(timeout)
        return Resp(200, json.dumps(fx("account.json")).encode(), req.full_url)

    AlpacaPaperBroker(LIMITS, opener=opener, timeout=3.5).verify_paper_account()
    assert seen == [3.5]


def test_errors_carry_no_chained_exception():
    b, _ = client({("GET", "/v2/positions"): OSError(KEY)})
    with pytest.raises(BrokerError) as exc:
        b.list_positions()
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ is True
