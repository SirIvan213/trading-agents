"""Bars: parse, filter and summarize intraday OHLCV data.

All logic takes already-fetched data so it can be tested offline. The only
network code is `fetch_bars`, which accepts an injectable opener.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
BASE_URL = "https://data.alpaca.markets/v2/stocks"
MAX_PAGES = 100


@dataclass(frozen=True)
class Bar:
    ts: datetime  # tz-aware, America/New_York
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def _parse_ts(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"bar timestamp must be a string, got {value!r}")
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"bad bar timestamp {value!r}") from exc
    if ts.tzinfo is None:
        raise ValueError(f"bar timestamp {value!r} has no timezone")
    return ts.astimezone(NY)


def _parse_bar(item: object) -> Bar:
    if not isinstance(item, dict):
        raise ValueError(f"bar must be an object, got {item!r}")
    missing = [k for k in ("t", "o", "h", "l", "c", "v") if k not in item]
    if missing:
        raise ValueError(f"bar missing keys: {', '.join(missing)}")
    try:
        o, h, lo, c = (Decimal(str(item[k])) for k in ("o", "h", "l", "c"))
    except ArithmeticError as exc:
        raise ValueError(f"bar has non-numeric price: {item!r}") from exc
    if not all(p.is_finite() for p in (o, h, lo, c)):
        raise ValueError(f"bar has non-finite price: {item!r}")
    if min(o, h, lo, c) <= 0:
        raise ValueError(f"bar prices must be positive: {item!r}")
    if h < lo:
        raise ValueError(f"bar high below low: {item!r}")
    if h < max(o, c) or lo > min(o, c):
        raise ValueError(f"bar open/close outside high-low range: {item!r}")
    v = item["v"]
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"bar volume invalid: {item!r}")
    try:
        vol_dec = Decimal(str(v))
    except ArithmeticError as exc:
        raise ValueError(f"bar volume invalid: {item!r}") from exc
    if not vol_dec.is_finite() or vol_dec != vol_dec.to_integral_value():
        raise ValueError(f"bar volume must be a whole number: {item!r}")
    if vol_dec < 0:
        raise ValueError(f"bar volume is negative: {item!r}")
    return Bar(ts=_parse_ts(item["t"]), open=o, high=h, low=lo, close=c, volume=int(vol_dec))


def parse_bars(raw: dict) -> list[Bar]:
    """Parse an Alpaca market-data v2 bars response into sorted, de-duplicated Bars.

    Accepts {"bars": [...]} and the multi-symbol shape {"bars": {"AAPL": [...]}}
    when it holds exactly one symbol. Raises ValueError on malformed input.
    """
    if not isinstance(raw, dict):
        raise ValueError("bars response must be an object")
    items = raw.get("bars")
    if items is None:
        return []
    if isinstance(items, dict):
        if len(items) > 1:
            raise ValueError("multi-symbol bars response not supported; fetch one symbol")
        items = next(iter(items.values()), [])
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("'bars' must be a list")
    parsed = sorted((_parse_bar(i) for i in items), key=lambda b: b.ts)
    out: list[Bar] = []
    for b in parsed:
        if out and out[-1].ts == b.ts:
            continue
        out.append(b)
    return out


def regular_session(bars: list[Bar], day: date | None = None) -> list[Bar]:
    """Keep bars with 09:30 <= time < 16:00 ET (only on `day` if given)."""
    out = []
    for b in bars:
        ny = b.ts.astimezone(NY)
        if day is not None and ny.date() != day:
            continue
        if MARKET_OPEN <= ny.time() < MARKET_CLOSE:
            out.append(b)
    return out


def opening_range(bars: list[Bar], minutes: int = 15) -> dict | None:
    """High/low of the first `minutes` of the first regular session day, or None.

    Also returns "complete" (True only if at least one regular-session bar on that
    day has ts >= the window end, i.e. the window has fully elapsed) and
    "missing_minutes" (whole minutes in [09:30, end) with no bar; informational,
    assumes 1-minute bars).
    """
    session = regular_session(bars)
    if not session:
        return None
    first_day = min(b.ts.astimezone(NY).date() for b in session)
    start = datetime.combine(first_day, MARKET_OPEN, tzinfo=NY)
    end = start + timedelta(minutes=minutes)
    window = [b for b in session if b.ts.astimezone(NY).date() == first_day and b.ts < end]
    if not window:
        return None
    present = {b.ts.astimezone(NY).replace(second=0, microsecond=0) for b in window}
    missing = sum(
        1 for i in range(minutes) if (start + timedelta(minutes=i)) not in present
    )
    complete = any(
        b.ts >= end and b.ts.astimezone(NY).date() == first_day for b in session
    )
    return {
        "high": str(max(b.high for b in window)),
        "low": str(min(b.low for b in window)),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bars": len(window),
        "complete": complete,
        "missing_minutes": missing,
    }


def vwap(bars: list[Bar]) -> Decimal | None:
    """Volume-weighted typical price (h+l+c)/3, quantized to 0.0001; None if no volume."""
    total = sum(b.volume for b in bars)
    if total == 0:
        return None
    num = sum(((b.high + b.low + b.close) / 3) * b.volume for b in bars)
    return (num / total).quantize(Decimal("0.0001"))


def atr(bars: list[Bar], n: int = 14) -> Decimal | None:
    """Wilder ATR over true range (first bar TR = high-low); None if fewer than n bars."""
    if n < 1:
        raise ValueError("n must be at least 1")
    if len(bars) < n:
        return None
    trs: list[Decimal] = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    value = sum(trs[:n]) / n
    for tr in trs[n:]:
        value = (value * (n - 1) + tr) / n
    return value.quantize(Decimal("0.0001"))


def summarize(bars: list[Bar], or_minutes: int = 15, atr_n: int = 14) -> dict:
    """JSON-serializable summary of a bar series (Decimals as strings)."""
    if not bars:
        return {
            "bars": 0, "first": None, "last": None, "last_close": None,
            "session_high": None, "session_low": None, "volume": 0,
            "vwap": None, "atr": None, "opening_range": None,
        }
    v = vwap(bars)
    a = atr(bars, atr_n)
    return {
        "bars": len(bars),
        "first": bars[0].ts.isoformat(),
        "last": bars[-1].ts.isoformat(),
        "last_close": str(bars[-1].close),
        "session_high": str(max(b.high for b in bars)),
        "session_low": str(min(b.low for b in bars)),
        "volume": sum(b.volume for b in bars),
        "vwap": None if v is None else str(v),
        "atr": None if a is None else str(a),
        "opening_range": opening_range(bars, or_minutes),
    }


def _utc_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("start/end must be timezone-aware")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe: str = "1Min",
    feed: str = "iex",
    opener: Callable | None = None,
) -> dict:
    """Fetch bars from Alpaca (the only network code), following pagination.

    Keys come from ALPACA_API_KEY / ALPACA_SECRET_KEY; RuntimeError if missing.
    Returns {"bars": [...], "symbol": symbol, "next_page_token": None}.
    """
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment")
    open_fn = opener or urllib.request.urlopen
    sym = symbol.upper()
    base_params = {
        "timeframe": timeframe,
        "start": _utc_rfc3339(start),
        "end": _utc_rfc3339(end),
        "limit": "10000",
        "feed": feed,
        "adjustment": "raw",
    }
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
    merged: list = []
    token: str | None = None
    for _ in range(MAX_PAGES):
        params = dict(base_params)
        if token:
            params["page_token"] = token
        url = f"{BASE_URL}/{urllib.parse.quote(sym)}/bars?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers)
        with open_fn(req, timeout=10) as resp:
            page = json.loads(resp.read().decode("utf-8"))
        items = page.get("bars") or []
        if isinstance(items, dict):
            items = items.get(sym, [])
        merged.extend(items)
        token = page.get("next_page_token")
        if not token:
            break
    else:
        raise RuntimeError(f"bars pagination exceeded {MAX_PAGES} pages")
    return {"bars": merged, "symbol": sym, "next_page_token": None}
