"""Listing exchange of a symbol, from Alpaca's asset endpoint (#168).

The halt check relies on the Nasdaq Trader halt feed (tools.halt_feed). The live check for
#152 saw Nasdaq-, NYSE- and NYSE American-listed symbols in it, but no NYSE Arca or Cboe
listing, so a halt on those venues might never show up and would read as "no halt". Until a
live check sees an Arca or Cboe item in the feed, only symbols LISTED on Nasdaq, NYSE or NYSE
American are traded: the scan rejects the rest, and the event gate reads UNKNOWN for them
(the order path's backstop). The listing comes from a data source, never from the ticker.

Alpaca: GET https://paper-api.alpaca.markets/v2/assets/{symbol} returns the asset, e.g.

    {"id": "...", "class": "us_equity", "exchange": "NASDAQ", "symbol": "AAPL",
     "name": "Apple Inc. Common Stock", "status": "active", "tradable": true, ...}

`exchange` is one of AMEX, ARCA, BATS, NYSE, NASDAQ, NYSEARCA, OTC (Alpaca's docs; AMEX is
NYSE American, BATS is Cboe BZX). The paper host is used: this read-only call needs no live
account. Keys come from ALPACA_API_KEY / ALPACA_SECRET_KEY. Free; no billing.

This module fetches and validates; the scan and tools.event_gate decide.
"""
from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable

from tools.http import get_json
from tools.symbols import normalize_symbol

ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets"
# Alpaca exchange code -> the listing venue's name. Everything else is refused (#168) until
# a live check sees an NYSE Arca or Cboe item in the Nasdaq halt feed.
ALLOWED_LISTINGS = {"NASDAQ": "Nasdaq", "NYSE": "NYSE", "AMEX": "NYSE American"}
RESTRICTION_NOTE = ("only Nasdaq, NYSE and NYSE American listings until the halt feed is seen "
                    "to cover NYSE Arca and Cboe (#168)")


def fetch_asset(symbol: str, opener: Callable | None = None) -> dict:
    """GET the Alpaca asset for `symbol`. Raises RuntimeError on missing keys, a transport
    error, an HTTP error status (404 for an unknown symbol), non-JSON or a non-object reply.
    ValueError for an invalid symbol (no request is made)."""
    sym = normalize_symbol(symbol)
    if sym is None:
        raise ValueError("invalid symbol")
    key = (os.environ.get("ALPACA_API_KEY") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment")
    url = f"{ASSETS_URL}/{urllib.parse.quote(sym, safe='.')}"
    body = get_json(url, {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                          "Accept": "application/json"}, opener=opener,
                    expect_keys=("symbol", "exchange"))
    if not isinstance(body, dict):
        raise RuntimeError("unexpected asset response shape")
    return body


def parse_asset(reply: object, symbol: str) -> dict:
    """{"ok": True, "symbol", "exchange"} from an Alpaca asset reply, or {"ok": False,
    "reason"}. Not ok: not an object, a symbol other than the one asked for, no exchange, or a
    status other than "active". Server text in reasons is cut to 16 characters."""
    want = normalize_symbol(symbol)
    if want is None:
        return {"ok": False, "reason": "invalid symbol"}
    if not isinstance(reply, dict):
        return {"ok": False, "reason": "asset reply is not an object"}
    got = reply.get("symbol")
    if not isinstance(got, str) or got.strip().upper() != want:
        return {"ok": False, "reason": f"asset reply is for {str(got)[:16]!r}, not {want}"}
    exchange = reply.get("exchange")
    if not isinstance(exchange, str) or not exchange.strip():
        return {"ok": False, "reason": f"asset reply for {want} has no exchange"}
    if reply.get("status") != "active":
        return {"ok": False, "reason": f"{want} is not an active asset"}
    return {"ok": True, "symbol": want, "exchange": exchange.strip().upper()[:16]}


def _compact(text: object) -> str:
    """Upper case with every separator removed; "" for a non-string."""
    return "".join(ch for ch in text.upper() if ch.isalnum()) if isinstance(text, str) else ""


def listing_problem(listing: object, symbol: str) -> str | None:
    """Why `symbol` may not be traded under the #168 restriction, or None when it is listed
    on Nasdaq, NYSE or NYSE American. A missing, failed or mismatched listing is a problem:
    it cannot prove the venue is one the halt feed covers. Symbols are compared without
    separators ("BRK.B" == "BRK B"), as the halt checks compare them; whether the symbol is a
    valid ticker is risk_check's rule, not this one."""
    if not isinstance(listing, dict) or listing.get("ok") is not True:
        why = listing.get("reason") if isinstance(listing, dict) else None
        return f"listing exchange unknown ({why or 'no listing'})"
    want = _compact(symbol)
    if not want or _compact(listing.get("symbol")) != want:
        return "listing exchange unknown (listing is for another symbol)"
    exchange = listing.get("exchange")
    if exchange not in ALLOWED_LISTINGS:
        return f"listed on {str(exchange)[:16]}: {RESTRICTION_NOTE}"
    return None
