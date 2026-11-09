"""Nasdaq Trader trade-halt RSS feed (issue #144).

The free, official feed lists current trading halts and pauses for Nasdaq-listed AND other
exchange-listed securities, including the five-minute LULD volatility pauses (code LUDP) that
rarely make a news headline:

    <rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>
      <pubDate>Sat, 07 Nov 2026 11:39:58 GMT</pubDate><ttl>1</ttl><ndaq:numItems>23</ndaq:numItems>
      <item><title>STKH</title>
        <ndaq:HaltDate>11/06/2026</ndaq:HaltDate><ndaq:HaltTime>19:50:00.000</ndaq:HaltTime>
        <ndaq:IssueSymbol>STKH</ndaq:IssueSymbol><ndaq:IssueName>...</ndaq:IssueName>
        <ndaq:Market>NASDAQ</ndaq:Market><ndaq:ReasonCode>T1</ndaq:ReasonCode>
        <ndaq:PauseThresholdPrice /><ndaq:ResumptionDate /><ndaq:ResumptionQuoteTime />
        <ndaq:ResumptionTradeTime /></item> ...

Halt dates and times are Eastern Time (HaltTime HH:MM:SS with optional .mmm). Field and code
definitions: https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltCodes.

Terms (THRSSFeedTermsCond.pdf, linked from https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltRSS):
the feed is free, no registration; using it accepts the terms; subscribers must not edit,
modify or misrepresent the feed; Nasdaq disclaims accuracy and timeliness. The RSS page asks:
"Please do not query the data more than once a minute." HaltFeedCache enforces that (failures
included) and keeps the feed in memory only (the site-wide notice restricts storing copies).
We only read the feed to decide internally whether to block our own paper orders; nothing is
redistributed. Test fixtures are synthetic XML in the feed's shape, not copies of the feed.

This module fetches, validates and caches; tools.event_gate decides.
"""
from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from tools.http import get_bytes

NY = ZoneInfo("America/New_York")
# HTTPS: a tampered plain-HTTP reply could hide a halt. tools.http refuses redirects, so if
# the host only serves HTTP the fetch fails and the gate reads UNKNOWN (fails closed).
HALT_FEED_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NS = {"ndaq": "http://www.nasdaqtrader.com/"}
MIN_FETCH_INTERVAL = timedelta(seconds=60)  # the feed's own polling rule
MAX_BODY_BYTES = 2_000_000
_HALT_DATE = re.compile(r"\d{2}/\d{2}/\d{4}")
_HALT_TIME = re.compile(r"\d{2}:\d{2}:\d{2}(\.\d{1,6})?")

# Reason codes (https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltCodes). Descriptions are
# for the ticket only: ANY code, known or not, for the symbol is a halt.
HALT_CODES = {
    "T1": "news pending", "T2": "news released", "T3": "news and resumption times",
    "T5": "single stock trading pause", "T6": "extraordinary market activity",
    "T7": "trading pause / quotation-only period", "T8": "exchange-traded fund",
    "T12": "additional information requested", "H4": "non-compliance", "H9": "not current",
    "H10": "SEC trading suspension", "H11": "regulatory concern",
    "O1": "operations halt", "IPO1": "IPO not yet trading", "M1": "corporate action",
    "M2": "quotation not available", "LUDP": "volatility trading pause (LULD)",
    "LUDS": "volatility trading pause, straddle condition (LULD)",
    "M": "volatility trading pause", "MWC0": "market-wide circuit breaker",
    "MWC1": "market-wide circuit breaker level 1", "MWC2": "market-wide circuit breaker level 2",
    "MWC3": "market-wide circuit breaker level 3",
}


def fetch_halt_feed(opener: Callable | None = None) -> bytes:
    """GET the halt feed once (no key). Raises RuntimeError on transport errors. Callers go
    through HaltFeedCache, which enforces the once-a-minute polling rule."""
    return get_bytes(HALT_FEED_URL, {"Accept": "application/rss+xml, text/xml"}, opener=opener,
                     max_bytes=MAX_BODY_BYTES)


def _text(item: ET.Element, name: str) -> str:
    return (item.findtext(f"ndaq:{name}", default="", namespaces=NS) or "").strip()


def parse_halt_feed(body: object) -> dict:
    """Validate the feed. Returns {"ok": True, "pub_date": ISO, "items": [{"symbol",
    "halt_date" (ISO), "halt_time", "market", "reason", "resumption_date",
    "resumption_trade_time"}]} or {"ok": False, "reason"}.

    One leading UTF-8 byte-order mark is stripped (the live feed sends one).
    Strict: not UTF-8 (a UTF-16/UTF-32 BOM, a second BOM, another declared encoding, or bytes
    that do not decode), a
    DOCTYPE/ENTITY declaration anywhere, not XML, not exactly one rss channel, items outside
    that channel, no valid pubDate, a numItems that is missing or does not match the item
    count (a cut-short feed), or ANY item without a symbol, a MM/DD/YYYY halt date or an
    HH:MM:SS halt time makes the whole feed not ok."""
    if not isinstance(body, (bytes, bytearray)):
        return {"ok": False, "reason": "halt feed body is not bytes"}
    if len(body) > MAX_BODY_BYTES:
        return {"ok": False, "reason": "halt feed body is too large"}
    raw = bytes(body)
    # The live feed starts with a UTF-8 byte-order mark (EF BB BF): strip exactly one. Any
    # other BOM (UTF-16 or UTF-32, either byte order) is still refused: a UTF-16 body could
    # smuggle a DOCTYPE past the byte-level check below (#144 review).
    if raw.startswith((b"\x00\x00\xfe\xff", b"\xff\xfe", b"\xfe\xff")):
        return {"ok": False, "reason": "halt feed has a UTF-16 or UTF-32 byte-order mark "
                                       "(UTF-8 required)"}
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        if raw.startswith(b"\xef\xbb\xbf"):
            # a second BOM would hide the encoding declaration from the check below
            return {"ok": False, "reason": "halt feed has more than one byte-order mark"}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "reason": "halt feed is not UTF-8"}
    decl = re.match(r"\s*<\?xml[^>]*encoding=[\"']([^\"']+)[\"']", text)
    if decl and decl.group(1).strip().lower() not in ("utf-8", "utf8"):
        return {"ok": False, "reason": "halt feed declares a non-UTF-8 encoding"}
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        return {"ok": False, "reason": "halt feed declares a DOCTYPE or entities"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {"ok": False, "reason": "halt feed is not valid XML"}
    channels = root.findall("channel") if root.tag == "rss" else []
    if len(channels) != 1:
        return {"ok": False, "reason": "halt feed must have exactly one rss channel"}
    channel = channels[0]
    try:
        pub = parsedate_to_datetime((channel.findtext("pubDate") or "").strip())
        if pub.tzinfo is None or pub.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError, IndexError):
        return {"ok": False, "reason": "halt feed has no valid pubDate"}
    raw_items = channel.findall("item")
    if len(list(root.iter("item"))) != len(raw_items):
        return {"ok": False, "reason": "halt feed has items outside its channel"}
    num = (channel.findtext("ndaq:numItems", default="", namespaces=NS) or "").strip()
    if not num.isdigit() or int(num) != len(raw_items):
        return {"ok": False, "reason": (
            f"halt feed lists {len(raw_items)} items but numItems is {num or 'missing'}: "
            "it may be cut short")}
    items = []
    for it in raw_items:
        sym, hd, ht = _text(it, "IssueSymbol"), _text(it, "HaltDate"), _text(it, "HaltTime")
        if not sym:
            return {"ok": False, "reason": "halt feed item has no symbol"}
        if not _HALT_DATE.fullmatch(hd) or not _HALT_TIME.fullmatch(ht):
            return {"ok": False, "reason": f"halt feed item {sym[:16]} has no valid halt time"}
        try:
            day = datetime.strptime(hd, "%m/%d/%Y").date()
        except ValueError:
            return {"ok": False, "reason": f"halt feed item {sym[:16]} has no valid halt date"}
        items.append({
            "symbol": sym.upper(), "halt_date": day.isoformat(), "halt_time": ht[:8],
            "market": _text(it, "Market"), "reason": _text(it, "ReasonCode").upper(),
            "resumption_date": _text(it, "ResumptionDate"),
            "resumption_trade_time": _text(it, "ResumptionTradeTime"),
        })
    return {"ok": True, "pub_date": pub.isoformat(), "items": items}


def resumed_before(item: dict, day: date) -> bool:
    """True when the item shows a resumption trade on a date before `day` (an old halt that
    is over). An empty or unreadable resumption means NOT resumed."""
    rd, rt = item.get("resumption_date") or "", item.get("resumption_trade_time") or ""
    if not rt or not _HALT_DATE.fullmatch(rd):
        return False
    try:
        return datetime.strptime(rd, "%m/%d/%Y").date() < day
    except ValueError:
        return False


class HaltFeedCache:
    """The halt feed fetched at most once per MIN_FETCH_INTERVAL, in memory only. A failed
    fetch is also kept for the interval (the polling rule applies to failures too), so a
    caller in that minute gets the same error. Thread-safe."""

    def __init__(self, fetch: Callable[[], bytes] = fetch_halt_feed,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.fetch = fetch
        self.clock = clock or (lambda: datetime.now(NY))
        self._lock = threading.Lock()
        self._at: datetime | None = None
        self._outcome: bytes | BaseException | None = None

    def get(self) -> dict:
        """{"fetched_at": NY ISO, "body": bytes}. Raises the fetch's error (cached for the
        interval)."""
        with self._lock:
            now = self.clock()
            if self._at is None or now - self._at >= MIN_FETCH_INTERVAL or now < self._at:
                try:
                    self._outcome = self.fetch()
                except Exception as exc:  # noqa: BLE001 - kept and re-raised for the interval
                    self._outcome = exc
                self._at = now
            at, outcome = self._at, self._outcome
        if isinstance(outcome, BaseException):
            raise RuntimeError(str(outcome)[:200] if isinstance(outcome, RuntimeError)
                               else type(outcome).__name__)
        return {"fetched_at": at.astimezone(NY).isoformat(), "body": outcome}
