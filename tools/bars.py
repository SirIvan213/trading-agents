"""Bars: parse, filter and summarize intraday OHLCV data.

All logic takes already-fetched data so it can be tested offline. The only
network code is `fetch_bars`, which accepts an injectable opener.
"""
from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from tools.config import MARKET_CLOSE, MARKET_OPEN
from tools.http import get_json

NY = ZoneInfo("America/New_York")
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


def resample(bars: list[Bar], minutes: int = 5) -> list[Bar]:
    """Aggregate 1-minute bars into `minutes`-minute bars aligned to the New York clock
    (09:30, 09:35, ...): first open, max high, min low, last close, summed volume. A bucket
    with missing minutes (e.g. a halt) still counts; a bucket with no bars at all is skipped,
    never invented. A trailing bucket that has not reached its last minute yet is dropped, so
    the series holds only finished bars. Bars within a bucket are ordered by time; bars from
    different days never share a bucket. `minutes` must divide 60 (1, 2, 3, 5, ..., 30), so
    buckets line up with the 09:30 open."""
    if minutes < 1 or 60 % minutes != 0 or minutes == 60:
        raise ValueError("minutes must divide 60 (1 to 30)")
    buckets: dict[datetime, list[Bar]] = {}
    for b in bars:
        ny = b.ts.astimezone(NY)
        start = ny.replace(minute=ny.minute - ny.minute % minutes, second=0, microsecond=0)
        buckets.setdefault(start, []).append(b)
    out: list[Bar] = []
    for start in sorted(buckets):
        group = sorted(buckets[start], key=lambda g: g.ts)
        out.append(Bar(
            ts=start, open=group[0].open, high=max(g.high for g in group),
            low=min(g.low for g in group), close=group[-1].close,
            volume=sum(g.volume for g in group),
        ))
    if out:
        last_start = max(buckets)
        last_minute = max(b.ts.astimezone(NY) for b in buckets[last_start])
        if last_minute < last_start + timedelta(minutes=minutes - 1):
            out.pop()  # unfinished bucket
    return out


def atr_5m(bars: list[Bar], n: int = 14) -> Decimal | None:
    """Wilder ATR over 5-minute bars resampled from 1-minute `bars` (see `resample`).
    None until n finished 5-minute bars exist (n=14: 70 minutes)."""
    return atr(resample(bars, 5), n)


# Finished 5-minute bars needed before a provisional 5-minute ATR is reported (issue #117):
# three bars (09:30, 09:35, 09:40) exist at 09:45, when the entry window opens.
PROVISIONAL_MIN_BARS = 3


def atr_5m_seeded(
    bars: list[Bar], n: int = 14, min_bars: int = PROVISIONAL_MIN_BARS
) -> tuple[Decimal | None, bool]:
    """(5-minute ATR, provisional) for plan CONTEXT and labels only (issue #117).

    With n or more finished 5-minute bars this is atr_5m (Wilder), provisional False. With
    min_bars to n-1 it is the simple mean of their true ranges (first bar TR = high-low),
    provisional True. Fewer than min_bars: (None, False). Opening bars are usually wide, so a
    provisional value tends to overstate the ATR. Never used for the stop buffer or a limit.
    """
    if min_bars < 1 or min_bars > n:
        raise ValueError("min_bars must be between 1 and n")
    five = resample(bars, 5)
    if len(five) >= n:
        return atr(five, n), False
    if len(five) < min_bars:
        return None, False
    trs: list[Decimal] = []
    for i, b in enumerate(five):
        if i == 0:
            trs.append(b.high - b.low)
        else:
            pc = five[i - 1].close
            trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return (sum(trs) / len(trs)).quantize(Decimal("0.0001")), True


def summarize(bars: list[Bar], or_minutes: int = 15, atr_n: int = 14) -> dict:
    """JSON-serializable summary of a bar series (Decimals as strings)."""
    if not bars:
        return {
            "bars": 0, "first": None, "last": None, "last_close": None,
            "session_high": None, "session_low": None, "volume": 0,
            "vwap": None, "atr": None, "atr_5m": None, "atr_5m_provisional": False,
            "opening_range": None,
        }
    v = vwap(bars)
    a = atr(bars, atr_n)
    a5, provisional = atr_5m_seeded(bars, atr_n, min(PROVISIONAL_MIN_BARS, atr_n))
    return {
        "bars": len(bars),
        "first": bars[0].ts.isoformat(),
        "last": bars[-1].ts.isoformat(),
        "last_close": str(bars[-1].close),
        "session_high": str(max(b.high for b in bars)),
        "session_low": str(min(b.low for b in bars)),
        "volume": sum(b.volume for b in bars),
        "vwap": None if v is None else str(v),
        "atr": None if a is None else str(a),  # 1-minute ATR (stop buffer, risk limits)
        # 5-minute ATR (context and plan labels only); provisional before n 5-minute bars
        "atr_5m": None if a5 is None else str(a5),
        "atr_5m_provisional": provisional,
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
        page = get_json(url, headers, opener=opener, expect_keys=["bars"])
        if not isinstance(page, dict):
            raise RuntimeError("unexpected bars response shape")
        items = page.get("bars") or []
        if not isinstance(items, (list, dict)):
            raise RuntimeError("unexpected bars response shape")
        if isinstance(items, dict):
            items = items.get(sym, [])
        merged.extend(items)
        token = page.get("next_page_token")
        if not token:
            break
    else:
        raise RuntimeError(f"bars pagination exceeded {MAX_PAGES} pages")
    return {"bars": merged, "symbol": sym, "next_page_token": None}
