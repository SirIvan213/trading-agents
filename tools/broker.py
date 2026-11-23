"""Alpaca PAPER broker client (Phase 5 PR 2, #154). pipeline/send.py (PR 3) sends approved
orders through it.

PAPER ONLY (CLAUDE.md rule 1). The base URL is fixed to the paper API and cannot be changed
or passed in; there is no live path. The client refuses to start unless `limits.paper is
True`, and refuses every call until `verify_paper_account()` has read an ACTIVE, unblocked
account from the paper host.

What Alpaca documents (read 2026-11-13, docs.alpaca.markets), and what it does not:
  - Paper and live are separate hosts (paper-api.alpaca.markets vs api.alpaca.markets) with
    separate API keys ("Your paper trading account will have a different API key from your
    live account"). GET /v2/account has NO documented paper/live flag. So "the account is a
    paper account" rests on the fixed paper host: only paper keys work there, and live keys
    get an error. An undocumented account_number prefix is not used.
  - Bracket orders: `time_in_force` must be day or gtc; "take_profit.limit_price must be
    higher than stop_loss.stop_price for a buy bracket order"; the stop price "has to be at
    least $0.01 below ... the base price", which for a limit entry "is the limit price of the
    entry order". A take-profit-above-entry rule is not documented; #139 keeps the target
    above entry + allowance anyway.
  - Prices: limit and stop prices at or above $1.00 take at most 2 decimals (4 below $1).
    Prices and quantities are sent as strings. This client sends whole cents and refuses
    prices below $1.00.
  - client_order_id: at most 128 characters; allowed characters are not documented. Looked
    up with GET /v2/orders:by_client_order_id.

What the paper probe found (pipeline/probe_broker.py, 2026-11-23, #196; the recorded replies
are tests/fixtures/broker/):
  - An unknown client_order_id answers 404 (code 40410000): read as None.
  - A lookup with nested=true shows the bracket's two legs, each with an Alpaca-made
    client_order_id. Before the entry fills both legs are "held"; after it fills the
    take-profit is "new" and the stop stays "held" (Alpaca monitors it and triggers it at its
    price).
  - The open-orders listing (status=open, nested) then shows ONLY the take-profit leg: the
    held stop is missing. Find a position's stop by looking up its entry with the legs.
  - A resend of the same client_order_id answers 422 with code 40010001, a general
    validation code: confirm by lookup before calling it a duplicate.
  - Closing a position while its legs are open answers 403, code 40310000 (qty_available
    0); cancelling the legs first works.
  - The closing order (DELETE /v2/positions/{symbol}) is a simple market sell with
    order_class "": simple orders are read as order_class "simple".
  - Partial fills: the take-profit and stop-loss legs "won't be active until the first order
    is completely filled", and "if any one of the orders is canceled, any remaining open
    order in the group is canceled". How the legs are sized after a PARTIAL entry fill whose
    remainder is cancelled is not documented. Ivan's decision (#196): don't depend on it.
    Every open position must always have a working stop for its full quantity; the
    reconciler (PR 4) flattens one that doesn't, at market, and alerts.
  - Errors: {"code": number, "message": string}; POST /v2/orders documents 403 (buying power
    or shares not sufficient) and 422 (input parameters not recognized).
  - DELETE /v2/positions?cancel_orders=true answers 207 with one {"symbol", "status", "body"}
    per position; DELETE /v2/positions/{symbol} answers the closing order.

The entry is a marketable LIMIT order at entry + slippage allowance (#136), so the exchange
enforces the price that sizing assumed; the bracket legs are the plan's target and stop,
time in force `day` (#136 decision). The request is built from an explicit allow-list of
fields: `slippage` and every other internal field are never forwarded.

Errors (BrokerError) name the method, the URL path (never the query string), the HTTP status
and Alpaca's numeric error code: never keys, request bodies or Alpaca's message text. Keys
come from ALPACA_API_KEY / ALPACA_SECRET_KEY. Like every data request, the default opener
honours HTTPS_PROXY (tools/http.py): a proxy set there sees the key headers.

Replies are parsed strictly: an order reply must carry a known status and side and must match
the request (client_order_id, symbol and, for a submit, side, qty, the bracket class and both
legs at the plan's target and stop: bracket_legs_problem), so a surprise never reads as a fill
or as a protected entry. A simple order (order_class missing, "" or "simple") is accepted
only as a market or limit order: the closing order of close_position and what listings show.
Position fields that only describe the position (market_value, unrealized_pl, current_price,
qty_available) are optional, so the exit path (listing positions to flatten them) never fails
on them.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from tools.http import _default_open, _origin
from tools.symbols import normalize_symbol

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
MAX_BODY_BYTES = 2_000_000
CENT = Decimal("0.01")
MIN_PRICE = Decimal("1.00")  # sub-dollar prices take 4 decimals; not traded here
CLIENT_ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ORDER_STATUSES = frozenset({
    "new", "partially_filled", "filled", "done_for_day", "canceled", "expired", "replaced",
    "pending_cancel", "pending_replace", "accepted", "pending_new", "accepted_for_bidding",
    "stopped", "rejected", "suspended", "calculated", "held",
})
# HTTP statuses after which Alpaca has definitely not acted on the request. Anything else
# (no status, 408, 429, 5xx, an unexpected success) may have acted (pipeline/send.py uses
# the same set for a POST).
DEFINITE_REFUSALS = frozenset({400, 401, 403, 404, 422})
# Statuses after which an order does nothing more today. Anything else (stopped: a trade is
# guaranteed; suspended; pending_*; held) counts as still working, so a doubt never reads as
# "the close is done" and invites a second sell.
FINAL_STATUSES = frozenset({"filled", "canceled", "expired", "replaced", "rejected",
                            "done_for_day"})


class BrokerError(RuntimeError):
    """A broker call failed or was refused. `status` is the HTTP status (None for a network
    failure or a refusal before any call); `code` is Alpaca's numeric error code, if any.

    `may_have_acted` is True when the request may have reached Alpaca and been carried out
    although this call failed: a timeout, a network error, a 408, 429, 5xx or another
    unexpected status, or a reply that could not be read (the probe, #196: a close that FILLED
    while its reply was refused here). After such an error, check the broker's state before acting
    again (check_close for a close); never repeat the request blindly. A definite refusal
    (DEFINITE_REFUSALS: 400, 401, 403, 404, 422) and a refusal before any request are
    False."""

    def __init__(self, message: str, *, status: int | None = None,
                 code: int | None = None, may_have_acted: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.may_have_acted = may_have_acted


# ----------------------------------------------------------------------------------------
# The bracket request: an explicit allow-list mapping (#136)
# ----------------------------------------------------------------------------------------

def _cents(value: object, what: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise BrokerError(f"order {what} is not a price")
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d <= 0 or d != d.quantize(CENT):
            raise ValueError
        return d.quantize(CENT)  # canonical: "370.040" -> 370.04
    except (InvalidOperation, ValueError, ArithmeticError):
        raise BrokerError(f"order {what} must be a positive whole-cent price") from None


def bracket_request(order: dict, client_order_id: str) -> dict:
    """The POST /v2/orders body for a risk-checked order (tools.size.size_position's "order"):
    a BUY bracket whose entry is a LIMIT at entry + slippage (#136), take-profit at the plan's
    target, stop-loss at the plan's stop, time in force day. Built field by field, so nothing
    else in `order` (slippage, entry ...) is ever forwarded. Raises BrokerError for an order
    Alpaca would reject or this phase never sends: not a long bracket, a bad quantity, a price
    that is not whole cents or is below $1.00, a stop not at least $0.01 under the limit
    price, a target not above the limit price, or a bad client_order_id."""
    if not isinstance(order, dict):
        raise BrokerError("order must be an object")
    symbol = normalize_symbol(order.get("symbol"))
    if symbol is None or order.get("symbol") != symbol:
        raise BrokerError("order symbol is not a canonical ticker")
    if order.get("side") != "buy" or order.get("order_class") != "bracket":
        raise BrokerError("only long bracket orders are sent in this phase")
    qty = order.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        raise BrokerError("order qty must be a whole number of at least 1")
    entry = _cents(order.get("entry"), "entry")
    stop = _cents(order.get("stop_loss"), "stop_loss")
    target = _cents(order.get("take_profit"), "take_profit")
    slip = order.get("slippage")
    try:
        if isinstance(slip, bool) or slip is None:
            raise ValueError
        allowance = Decimal(str(slip))
        if not allowance.is_finite() or allowance < 0 or allowance != allowance.quantize(CENT):
            raise ValueError
        allowance = allowance.quantize(CENT)
    except (InvalidOperation, ValueError, ArithmeticError):
        raise BrokerError("order slippage must be a non-negative whole-cent allowance") from None
    limit = (entry + allowance).quantize(CENT)
    if min(limit, stop, target) < MIN_PRICE:
        raise BrokerError(f"prices below {MIN_PRICE} are not sent (sub-penny rules)")
    if stop > limit - CENT:
        raise BrokerError("stop must be at least 0.01 below the entry limit price")
    if target <= limit:
        raise BrokerError("target must be above the entry limit price (#139)")
    if not isinstance(client_order_id, str) or not CLIENT_ORDER_ID.fullmatch(client_order_id):
        raise BrokerError("client_order_id must be 1-128 letters, digits, '-' or '_'")
    return {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "limit",
        "limit_price": f"{limit:.2f}",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": f"{target:.2f}"},
        "stop_loss": {"stop_price": f"{stop:.2f}"},
        "client_order_id": client_order_id,
    }


# ----------------------------------------------------------------------------------------
# Response parsing: strict, so a surprise fails closed
# ----------------------------------------------------------------------------------------

def _dec(value: object, what: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise BrokerError(f"broker reply {what} is not a number")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BrokerError(f"broker reply {what} is not a number") from None
    if not d.is_finite():
        raise BrokerError(f"broker reply {what} is not a number")
    return d


def _text(value: object, what: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise BrokerError(f"broker reply has no {what}")
    return value[:128]


SIMPLE_CLASSES = (None, "", "simple")  # how Alpaca writes a simple order's class
SIMPLE_TYPES = frozenset({"market", "limit"})


def _order_class(reply: dict) -> str:
    """The order's class. A simple order is written with order_class missing, null, "" (the
    closing order of DELETE /v2/positions/{symbol}, seen on paper, #196) or "simple"; it is
    read as "simple" when it is a market or limit order, and refused otherwise. Any other
    class is passed through as written; the callers that need a bracket check for one."""
    cls = reply.get("order_class")
    if cls in SIMPLE_CLASSES:
        if reply.get("type") not in SIMPLE_TYPES:
            raise BrokerError("broker reply is a simple order of an unexpected type")
        return "simple"
    if not isinstance(cls, str):
        raise BrokerError("broker reply has an unknown order_class")
    return cls[:32]


def parse_order(reply: object) -> dict:
    """The fields of an Alpaca order this system uses, prices and quantities as Decimal
    strings. Raises BrokerError for a reply without an id, client_order_id, symbol, side,
    known status or quantities, so an unexpected shape never reads as a fill."""
    if not isinstance(reply, dict):
        raise BrokerError("broker reply is not an order")
    status = reply.get("status")
    if status not in ORDER_STATUSES:
        raise BrokerError("broker reply has an unknown order status")
    legs = reply.get("legs")
    if legs is not None and not isinstance(legs, list):
        raise BrokerError("broker reply legs is not a list")
    if reply.get("side") not in ("buy", "sell"):
        raise BrokerError("broker reply has an unknown order side")
    order_class = _order_class(reply)

    def money(key: str) -> str | None:
        v = _dec(reply.get(key), key, optional=True)
        return None if v is None else str(v)

    return {
        "id": _text(reply.get("id"), "id"),
        "client_order_id": _text(reply.get("client_order_id"), "client_order_id"),
        "symbol": _text(reply.get("symbol"), "symbol"),
        "side": _text(reply.get("side"), "side"),
        "status": status,
        "type": _text(reply.get("type"), "type"),
        "order_class": order_class,
        "qty": str(_dec(reply.get("qty"), "qty")),
        "filled_qty": str(_dec(reply.get("filled_qty"), "filled_qty")),
        "filled_avg_price": money("filled_avg_price"),
        "limit_price": money("limit_price"),
        "stop_price": money("stop_price"),
        "time_in_force": _text(reply.get("time_in_force"), "time_in_force", optional=True),
        "submitted_at": _text(reply.get("submitted_at"), "submitted_at", optional=True),
        "updated_at": _text(reply.get("updated_at"), "updated_at", optional=True),
        "filled_at": _text(reply.get("filled_at"), "filled_at", optional=True),
        "canceled_at": _text(reply.get("canceled_at"), "canceled_at", optional=True),
        "expired_at": _text(reply.get("expired_at"), "expired_at", optional=True),
        "failed_at": _text(reply.get("failed_at"), "failed_at", optional=True),
        "legs": [parse_order(leg) for leg in legs] if legs else [],
    }


def bracket_legs_problem(order: dict, body: dict) -> str | None:
    """Why a parsed entry `order` is not the bracket `body` (bracket_request) asked for, by
    its legs, or None. Exactly two legs, both bracket sells of the full quantity in the
    symbol: one LIMIT at the take-profit price, one STOP at the stop price. Their status is
    not checked here (they are "held" until the entry fills, #196)."""
    if not isinstance(order, dict):
        return "the entry reply is not an order"
    legs = order.get("legs")
    if order.get("order_class") != "bracket" or not isinstance(legs, list) or len(legs) != 2:
        return "the entry is not a bracket with both legs"
    by_type = {leg.get("type"): leg for leg in legs if isinstance(leg, dict)}
    tp, sl = by_type.get("limit"), by_type.get("stop")
    if tp is None or sl is None:
        return "the bracket does not have a take-profit limit leg and a stop leg"
    try:
        qty = Decimal(body["qty"])
        for leg in (tp, sl):
            if (leg.get("side") != "sell" or leg.get("symbol") != body["symbol"]
                    or leg.get("order_class") != "bracket"
                    or Decimal(str(leg.get("qty"))) != qty):
                return "a bracket leg is not a sell of the full quantity"
        if Decimal(str(tp.get("limit_price"))) != Decimal(body["take_profit"]["limit_price"]):
            return "the take-profit leg is not at the target price"
        if Decimal(str(sl.get("stop_price"))) != Decimal(body["stop_loss"]["stop_price"]):
            return "the stop leg is not at the stop price"
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return "a bracket leg has an unreadable price or quantity"
    return None


def parse_position(reply: object) -> dict:
    if not isinstance(reply, dict):
        raise BrokerError("broker reply is not a position")
    side = reply.get("side")
    if side not in ("long", "short"):
        raise BrokerError("broker reply position side is unknown")
    def optional(key: str) -> str | None:
        """Display-only fields: a missing or odd value is None, never a reason to fail the
        exit path (listing positions to flatten them)."""
        try:
            v = _dec(reply.get(key), key, optional=True)
        except BrokerError:
            return None
        return None if v is None else str(v)

    return {
        "symbol": _text(reply.get("symbol"), "symbol"),
        "side": side,
        "qty": str(_dec(reply.get("qty"), "qty")),
        "qty_available": optional("qty_available"),  # shares not held by open orders (legs)
        "avg_entry_price": str(_dec(reply.get("avg_entry_price"), "avg_entry_price")),
        "current_price": optional("current_price"),
        "market_value": optional("market_value"),
        "unrealized_pl": optional("unrealized_pl"),
    }


def _error_code(raw: bytes) -> int | None:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


# ----------------------------------------------------------------------------------------
# The client
# ----------------------------------------------------------------------------------------

class AlpacaPaperBroker:
    """The Alpaca paper trading API. Construct with the risk limits (paper must be True),
    then call verify_paper_account() once; every other method refuses until it has."""

    def __init__(self, limits: object, *, opener: Callable | None = None,
                 timeout: float = 10) -> None:
        if getattr(limits, "paper", None) is not True:
            raise BrokerError("the broker client is paper only: limits.paper must be true")
        key = (os.environ.get("ALPACA_API_KEY") or "").strip()
        secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
        if not key or not secret:
            raise BrokerError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                         "Accept": "application/json"}
        self._open = opener or _default_open
        self._timeout = timeout
        self._verified = False

    # ---- transport ----
    def _call(self, method: str, path: str, *, query: dict | None = None,
              body: dict | None = None, ok: tuple[int, ...] = (200,)) -> object:
        url = PAPER_BASE_URL + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = dict(self._headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        where = f"{method} {path}"
        try:
            with self._open(req, timeout=self._timeout) as resp:
                final = resp.geturl() if hasattr(resp, "geturl") else None
                if isinstance(final, str) and _origin(final) != _origin(url):
                    raise BrokerError(f"{where}: response came from a different origin")
                status = getattr(resp, "status", None) or resp.getcode()
                raw = resp.read(MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                raw_err = exc.read(4096) if exc.fp is not None else b""
            except OSError:
                raw_err = b""
            # a definite refusal did not act; a 408, 429 or 5xx may come after it acted
            raise BrokerError(f"{where} failed: HTTP {exc.code}", status=exc.code,
                              code=_error_code(raw_err),
                              may_have_acted=exc.code not in DEFINITE_REFUSALS) from None
        except TimeoutError:
            raise BrokerError(f"{where} timed out", may_have_acted=True) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise BrokerError(f"{where} timed out", may_have_acted=True) from None
            raise BrokerError(
                f"{where} failed: network error ({type(exc.reason).__name__})",
                may_have_acted=True) from None
        except http.client.HTTPException as exc:  # IncompleteRead, BadStatusLine: not OSError
            raise BrokerError(f"{where}: incomplete response ({type(exc).__name__})",
                              may_have_acted=True) from None
        except OSError as exc:
            raise BrokerError(f"{where} failed: network error ({type(exc).__name__})",
                              may_have_acted=True) from None
        if status not in ok:
            raise BrokerError(f"{where} failed: unexpected HTTP {status}", status=status,
                              may_have_acted=True)
        if len(raw) > MAX_BODY_BYTES:
            raise BrokerError(f"{where}: reply too large", status=status, may_have_acted=True)
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise BrokerError(f"{where}: reply is not JSON", status=status,
                              may_have_acted=True) from None

    def _require_verified(self) -> None:
        if not self._verified:
            raise BrokerError("call verify_paper_account() before any other broker call")

    # ---- the paper guard ----
    def verify_paper_account(self) -> dict:
        """Read GET /v2/account from the PAPER host and require an ACTIVE account that is not
        trading- or account-blocked. Only paper keys work on the paper host (Alpaca keeps
        separate paper and live keys), and Alpaca documents no paper flag on the account, so
        this is the check. Returns {"account_number", "status", "currency", "cash",
        "buying_power"}; raises BrokerError otherwise. Any failure, including on a later
        re-check, leaves the client unverified, so every other call refuses again."""
        self._verified = False
        reply = self._call("GET", "/v2/account")
        if not isinstance(reply, dict):
            raise BrokerError("broker reply is not an account")
        if reply.get("status") != "ACTIVE":
            raise BrokerError("paper account is not ACTIVE")
        if reply.get("trading_blocked") is not False or reply.get("account_blocked") is not False:
            raise BrokerError("paper account is blocked")
        if reply.get("trade_suspended_by_user") is not False:  # #197
            raise BrokerError("paper account trading is suspended by the user")
        if reply.get("shorting_enabled") is not False:  # #197: long only, no stray shorts
            raise BrokerError("paper account has shorting enabled: disable it (long only)")
        summary = {
            "account_number": _text(reply.get("account_number"), "account_number"),
            "status": "ACTIVE",
            "currency": _text(reply.get("currency"), "currency"),
            "cash": str(_dec(reply.get("cash"), "cash")),
            "buying_power": str(_dec(reply.get("buying_power"), "buying_power")),
        }
        self._verified = True
        return summary

    # ---- orders ----
    def submit_bracket(self, order: dict, client_order_id: str) -> dict:
        """POST /v2/orders with bracket_request(order, client_order_id); the parsed order."""
        self._require_verified()
        body = bracket_request(order, client_order_id)
        reply = self._call("POST", "/v2/orders", body=body)
        # Alpaca answered 200: from here on, any doubt means the order may exist
        try:
            out = parse_order(reply)
        except BrokerError as exc:
            raise BrokerError(str(exc), status=200, may_have_acted=True) from None
        expected = {"client_order_id": client_order_id, "symbol": body["symbol"], "side": "buy",
                    "qty": body["qty"], "order_class": "bracket"}
        got = {k: out[k] for k in expected}
        # compare the quantity as a number: Decimal("10").normalize() prints as "1E+1"
        if got["qty"] and Decimal(got["qty"]) == Decimal(body["qty"]):
            got["qty"] = body["qty"]
        if got != expected:
            raise BrokerError("broker reply does not match the order sent", status=200,
                              may_have_acted=True)
        problem = bracket_legs_problem(out, body)  # strict: our entry always has both legs
        if problem:
            raise BrokerError(f"broker reply does not match the order sent: {problem}",
                              status=200, may_have_acted=True)
        return out

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        """The order with this client_order_id, legs nested, or None when Alpaca answers 404
        ("no such order": confirmed on paper, #196)."""
        self._require_verified()
        if not isinstance(client_order_id, str) or not CLIENT_ORDER_ID.fullmatch(client_order_id):
            raise BrokerError("client_order_id must be 1-128 letters, digits, '-' or '_'")
        try:
            reply = self._call("GET", "/v2/orders:by_client_order_id",
                               query={"client_order_id": client_order_id, "nested": "true"})
        except BrokerError as exc:
            if exc.status == 404:
                return None
            raise
        out = parse_order(reply)
        if out["client_order_id"] != client_order_id:
            raise BrokerError("broker reply is for another client_order_id")
        return out

    def list_open_orders(self) -> list[dict]:
        self._require_verified()
        reply = self._call("GET", "/v2/orders",
                           query={"status": "open", "nested": "true", "limit": "500"})
        if not isinstance(reply, list):
            raise BrokerError("broker reply is not a list of orders")
        return [parse_order(o) for o in reply]

    def cancel_order(self, order_id: str) -> None:
        self._require_verified()
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", order_id):
            raise BrokerError("order id must be letters, digits or '-'")
        self._call("DELETE", f"/v2/orders/{order_id}", ok=(200, 204))

    # ---- positions ----
    def list_positions(self) -> list[dict]:
        self._require_verified()
        reply = self._call("GET", "/v2/positions")
        if not isinstance(reply, list):
            raise BrokerError("broker reply is not a list of positions")
        return [parse_position(p) for p in reply]

    def close_position(self, symbol: str) -> dict:
        """DELETE /v2/positions/{symbol}: the simple order closing the whole position (on
        paper, #196: a market sell with order_class ""). Alpaca refuses (403, 40310000,
        qty_available 0) while the position's bracket legs are open: cancel them first.

        A reply that cannot be read, or is not a simple sell in the symbol, raises BrokerError
        with may_have_acted=True: Alpaca answered 200, so the close may well be working (on
        paper it FILLED while its reply was refused here). Then call check_close; never send
        the close again without it."""
        self._require_verified()
        sym = normalize_symbol(symbol)
        if sym is None:
            raise BrokerError("symbol is not a valid ticker")
        path = f"/v2/positions/{urllib.parse.quote(sym, safe='.')}"
        reply = self._call("DELETE", path)
        try:
            out = parse_order(reply)
        except BrokerError as exc:
            raise BrokerError(f"DELETE {path}: {exc}; the close may have been placed",
                              status=200, may_have_acted=True) from None
        if out["symbol"] != sym or out["side"] != "sell" or out["order_class"] != "simple":
            raise BrokerError(f"DELETE {path}: the reply is not a simple sell in {sym}; the "
                              "close may have been placed", status=200, may_have_acted=True)
        return out

    def list_orders(self, symbol: str, *, limit: int = 50) -> list[dict]:
        """The symbol's most recent orders in any status, newest first, legs nested
        (GET /v2/orders?status=all&symbols=...). Read only."""
        self._require_verified()
        sym = normalize_symbol(symbol)
        if sym is None:
            raise BrokerError("symbol is not a valid ticker")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise BrokerError("limit must be a whole number from 1 to 500")
        reply = self._call("GET", "/v2/orders",
                           query={"status": "all", "symbols": sym, "nested": "true",
                                  "direction": "desc", "limit": str(limit)})
        if not isinstance(reply, list):
            raise BrokerError("broker reply is not a list of orders")
        return [o for o in (parse_order(x) for x in reply) if o["symbol"] == sym]

    def check_close(self, symbol: str) -> dict:
        """What became of a close whose outcome is unclear (a BrokerError with
        may_have_acted), from the broker's own state. Read only: it never sends anything.

          {"state": "closed" | "closing" | "open", "position": <parsed> | None,
           "close_orders": [the symbol's recent simple sell orders, newest first]}

        "closed": no position in the symbol. "closing": the position is still there and a
        simple sell is working (wait for it; do not close again). "open": the position is
        there and no simple sell is working, so the close did not take: a new close is an
        informed decision for the caller, never automatic here."""
        self._require_verified()
        sym = normalize_symbol(symbol)
        if sym is None:
            raise BrokerError("symbol is not a valid ticker")
        # orders first, then the position: a close that fills between the two reads then
        # shows as "closed" (position gone), never as "open" (a filled close, position seen)
        closes = [o for o in self.list_orders(sym)
                  if o["side"] == "sell" and o["order_class"] == "simple"]
        position = next((p for p in self.list_positions() if p["symbol"] == sym), None)
        working = [o for o in closes if o["status"] not in FINAL_STATUSES]
        state = "closed" if position is None else "closing" if working else "open"
        return {"state": state, "position": position, "close_orders": closes}

    def close_all(self) -> list[dict]:
        """DELETE /v2/positions?cancel_orders=true: cancel every open order, then close every
        position. One {"symbol", "status", "order"} or {"symbol", "status", "error_code"} per
        position (Alpaca answers 207, multi-status)."""
        self._require_verified()
        reply = self._call("DELETE", "/v2/positions", query={"cancel_orders": "true"},
                           ok=(200, 207))
        if reply is None:
            return []
        if not isinstance(reply, list):
            raise BrokerError("broker reply is not a list of results")
        out = []
        for item in reply:
            if not isinstance(item, dict) or not isinstance(item.get("status"), int):
                raise BrokerError("broker close-all result is malformed")
            sym = _text(item.get("symbol"), "symbol")
            if item["status"] == 200:
                # one unreadable order must not lose the other positions' results: the
                # positions are already being closed at the broker
                try:
                    out.append({"symbol": sym, "status": 200,
                                "order": parse_order(item.get("body"))})
                except BrokerError as exc:
                    out.append({"symbol": sym, "status": 200, "order": None,
                                "error": str(exc)[:120]})
            else:
                body = item.get("body")
                code = body.get("code") if isinstance(body, dict) else None
                out.append({"symbol": sym, "status": item["status"],
                            "error_code": code if isinstance(code, int) else None})
        return out

    # ---- account and clock ----
    def account(self) -> dict:
        self._require_verified()
        return self.verify_paper_account()

    def clock(self) -> dict:
        """GET /v2/clock: {"timestamp", "is_open", "next_open", "next_close"}."""
        self._require_verified()
        reply = self._call("GET", "/v2/clock")
        if not isinstance(reply, dict) or not isinstance(reply.get("is_open"), bool):
            raise BrokerError("broker reply is not a clock")
        return {"timestamp": _text(reply.get("timestamp"), "timestamp"),
                "is_open": reply["is_open"],
                "next_open": _text(reply.get("next_open"), "next_open"),
                "next_close": _text(reply.get("next_close"), "next_close")}
