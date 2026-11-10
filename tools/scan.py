"""Scan: filter a universe of Alpaca snapshots down to gap/volume candidates.

All logic takes already-fetched data so it can be tested offline. The only
network code is `fetch_snapshots`, which accepts an injectable opener.
"""
from __future__ import annotations

import math
import os
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from tools.config import Limits
from tools.http import get_json
from tools.listing import listing_problem
from tools.symbols import SYMBOL_RE as _SHARED_SYMBOL_RE
from tools.symbols import normalize_symbol

NY = ZoneInfo("America/New_York")
SNAPSHOTS_URL = "https://data.alpaca.markets/v2/stocks/snapshots"
MAX_SYMBOLS_PER_REQUEST = 100
SYMBOL_RE = _SHARED_SYMBOL_RE  # tools.symbols: 1-5 letters, optional ".X"/".XY" (#34)
CENT = Decimal("0.01")
RVOL_NOTE = "rvol vs previous day"


@dataclass(frozen=True)
class ScanCriteria:
    min_price: Decimal = Decimal("2.00")
    max_price: Decimal = Decimal("500.00")
    min_volume: int = 100_000
    max_spread_pct: Decimal = Decimal("0.50")
    min_gap_pct: Decimal = Decimal("2.0")
    direction: str = "up"  # "up" (long-only phase) or "both"
    # Staleness (#12): with `now`, a latestTrade or latestQuote older than this is rejected,
    # so a halted stock cannot look like a live gap. 120 s catches a 5-minute LULD halt
    # within two minutes; liquid names on the IEX feed trade and quote far more often.
    max_quote_age_s: int = 120

    def __post_init__(self) -> None:
        money = ("min_price", "max_price", "max_spread_pct", "min_gap_pct")
        for name in money:
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not v.is_finite():
                raise ValueError(f"{name} must be a finite Decimal, got {v!r}")
            if v < 0:
                raise ValueError(f"{name} must not be negative, got {v}")
        if self.min_price > self.max_price:
            raise ValueError("min_price must not exceed max_price")
        if isinstance(self.min_volume, bool) or not isinstance(self.min_volume, int):
            raise ValueError(f"min_volume must be an integer, got {self.min_volume!r}")
        if self.min_volume < 0:
            raise ValueError("min_volume must not be negative")
        age = self.max_quote_age_s
        if isinstance(age, bool) or not isinstance(age, int) or age < 1:
            raise ValueError(f"max_quote_age_s must be an integer of at least 1, got {age!r}")
        if self.direction not in ("up", "both"):
            raise ValueError(f"direction must be 'up' or 'both', got {self.direction!r}")


def criteria_from_dict(d: dict) -> ScanCriteria:
    """Build ScanCriteria from a dict; missing keys use defaults, unknown keys raise ValueError."""
    if not isinstance(d, dict):
        raise ValueError("criteria must be an object")
    names = {f.name for f in fields(ScanCriteria)}
    unknown = set(d) - names
    if unknown:
        raise ValueError(f"unknown criteria keys: {', '.join(sorted(unknown))}")
    kwargs: dict = {}
    try:
        for k, v in d.items():
            if k in ("min_volume", "max_quote_age_s"):
                if isinstance(v, bool):
                    raise ValueError(f"{k} must be an integer")
                dv = Decimal(str(v))
                if not dv.is_finite() or dv != dv.to_integral_value():
                    raise ValueError(f"{k} must be a whole number, got {v!r}")
                kwargs[k] = int(dv)
            elif k == "direction":
                kwargs[k] = str(v)
            else:
                kwargs[k] = Decimal(str(v))
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ValueError(f"bad criteria value: {exc}") from exc
    return ScanCriteria(**kwargs)


def _criteria_dict(c: ScanCriteria) -> dict:
    return {
        "min_price": str(c.min_price),
        "max_price": str(c.max_price),
        "min_volume": c.min_volume,
        "max_spread_pct": str(c.max_spread_pct),
        "min_gap_pct": str(c.min_gap_pct),
        "direction": c.direction,
        "max_quote_age_s": c.max_quote_age_s,
    }


def validate_symbols(symbols: list[str]) -> list[str]:
    """Normalise with tools.symbols (1-5 ASCII letters, optional one share-class suffix
    like ".B") and de-duplicate (order kept). Raises ValueError for an invalid symbol."""
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        if not isinstance(s, str):
            raise ValueError(f"symbol must be a string, got {s!r}")
        up = normalize_symbol(s)
        if up is None:
            raise ValueError(f"invalid symbol {s!r}")
        if up not in seen:
            seen.add(up)
            out.append(up)
    return out


def fetch_snapshots(symbols: list[str], feed: str = "iex", opener: Callable | None = None) -> dict:
    """Fetch Alpaca snapshots for `symbols` (100 per request). Returns {symbol: snapshot|None}.

    Keys come from ALPACA_API_KEY / ALPACA_SECRET_KEY; RuntimeError if missing.
    ValueError on an invalid symbol.
    """
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment")
    syms = validate_symbols(symbols)
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
    merged: dict = {}
    for i in range(0, len(syms), MAX_SYMBOLS_PER_REQUEST):
        chunk = syms[i : i + MAX_SYMBOLS_PER_REQUEST]
        qs = urllib.parse.urlencode({"symbols": ",".join(chunk), "feed": feed}, safe=",")
        body = get_json(
            f"{SNAPSHOTS_URL}?{qs}", headers, opener=opener, expect_keys=chunk + ["snapshots"]
        )
        if not isinstance(body, dict):
            raise RuntimeError("unexpected snapshots response shape")
        if isinstance(body.get("snapshots"), dict):
            body = body["snapshots"]
        merged.update(body)
    return merged


def _dec(value: object) -> Decimal | None:
    """Positive finite Decimal or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d


def _vol(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    if not d.is_finite() or d < 0 or d != d.to_integral_value():
        return None
    return int(d)


def _get(obj: object, key: str) -> object:
    return obj.get(key) if isinstance(obj, dict) else None


def _metrics(snap: object, avg_volume: dict[str, int] | None, sym: str) -> tuple[dict | None, str]:
    """Compute metrics for one snapshot; (None, reason) if malformed or not computable."""
    try:
        return _metrics_unsafe(snap, avg_volume, sym)
    except ArithmeticError:
        return None, "malformed snapshot: numeric overflow or out-of-range values"


def _metrics_unsafe(
    snap: object, avg_volume: dict[str, int] | None, sym: str
) -> tuple[dict | None, str]:
    if not isinstance(snap, dict):
        return None, "null or malformed snapshot"
    daily = snap.get("dailyBar")
    prev = snap.get("prevDailyBar")
    d_open = _dec(_get(daily, "o"))
    d_close = _dec(_get(daily, "c"))
    prev_close = _dec(_get(prev, "c"))
    volume = _vol(_get(daily, "v"))
    if d_open is None or prev_close is None or volume is None:
        return None, "malformed snapshot: missing or invalid dailyBar/prevDailyBar"
    price = _dec(_get(snap.get("latestTrade"), "p")) or d_close
    if price is None:
        return None, "malformed snapshot: no price"
    gap_raw = (d_open - prev_close) / prev_close * 100
    gap = gap_raw.quantize(CENT)

    ask = _dec(_get(snap.get("latestQuote"), "ap"))
    bid = _dec(_get(snap.get("latestQuote"), "bp"))
    spread: Decimal | None = None
    spread_raw: Decimal | None = None
    if ask is not None and bid is not None and ask >= bid:
        spread_raw = (ask - bid) / ((ask + bid) / 2) * 100
        spread = spread_raw.quantize(CENT)

    avg = avg_volume.get(sym) if isinstance(avg_volume, dict) else None
    note: str | None = None
    denom: int | None
    if isinstance(avg, int) and not isinstance(avg, bool) and avg > 0:
        denom = avg
    else:
        denom = _vol(_get(prev, "v"))
        note = RVOL_NOTE
    rvol = (Decimal(volume) / Decimal(denom)).quantize(CENT) if denom else None
    return {
        "price": price, "gap_pct": gap, "spread_pct": spread,
        "gap_raw": gap_raw, "spread_raw": spread_raw, "bar_t": _get(daily, "t"),
        "volume": volume, "rvol": rvol, "rvol_note": note,
        "trade_t": _get(snap.get("latestTrade"), "t"),
        "quote_t": _get(snap.get("latestQuote"), "t"),
    }, ""


def _sort_key(c: dict) -> tuple:
    rvol = c["rvol"]
    return (rvol is None, -(rvol or Decimal(0)), -c["gap_pct"], c["symbol"])


def _ts(t: object) -> datetime | None:
    """An aware datetime from an RFC3339 timestamp, or None if absent/unparseable."""
    if not isinstance(t, str):
        return None
    try:
        dt = datetime.fromisoformat(t.strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def _bar_day(t: object) -> date | None:
    """ET calendar date of a dailyBar RFC3339 timestamp, or None if absent/unparseable."""
    dt = _ts(t)
    return None if dt is None else dt.astimezone(NY).date()


def _staleness(kind: str, t: object, now: datetime, max_age_s: int) -> str | None:
    """Rejection reason when the latest `kind` ("trade" or "quote") is missing or older than
    max_age_s at `now`; None when fresh. A timestamp after `now` (clock skew) counts as
    fresh."""
    dt = _ts(t)
    if dt is None:
        return f"stale {kind} (missing or invalid timestamp)"
    age = now - dt
    if age > timedelta(seconds=max_age_s):  # exact: 120.5 s is stale
        return f"stale {kind} ({math.ceil(age.total_seconds())}s old, max {max_age_s}s)"
    return None


def scan(
    snapshots: dict,
    criteria: ScanCriteria,
    limits: Limits,
    avg_volume: dict[str, int] | None = None,
    today: date | None = None,
    now: datetime | None = None,
    listings: dict | None = None,
) -> dict:
    """Filter snapshots into candidates; every rejection carries explicit reasons.

    Returns {"candidates": [...], "rejected": [{"symbol","reasons"}], "criteria": {...}}.
    Candidates are sorted by rvol desc, gap_pct desc, symbol asc. Decimals are
    returned as strings. Malformed input is rejected, never raised.

    Gap and spread are compared against the criteria unrounded; the 0.01-rounded
    values are for output only. Duplicate symbols after upper-casing: the first
    (in dict order) is evaluated, the rest are rejected as "duplicate symbol".

    Freshness: Alpaca's dailyBar is the latest session with data. Without `today`
    no freshness check is made, so a scan before 09:30 ET uses yesterday's bar as
    "today" (and its gap is stale). Pass `today` (ET date) to reject any snapshot
    whose dailyBar "t" is not on that date, or is missing, as "stale daily bar".
    Pass `now` (timezone-aware) to also reject a symbol whose latestTrade or latestQuote "t"
    is missing or older than criteria.max_quote_age_s, as "stale trade" / "stale quote"
    (issue #12: a halted stock stops trading and quoting).

    Listing (#168): pass `listings` ({symbol: tools.listing.parse_asset(...) result}) to reject
    any symbol that is not proven listed on Nasdaq, NYSE or NYSE American, with the reason
    from tools.listing.listing_problem (a symbol missing from `listings` is rejected too).
    pipeline.market.run_scan, the sanctioned path, always applies this check.
    """
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("now must be timezone-aware")
    blocked = {s.upper() for s in limits.blocked_symbols}
    avgs = None
    if isinstance(avg_volume, dict):
        avgs = {str(k).upper(): v for k, v in avg_volume.items()}
    candidates: list[dict] = []
    rejected: list[dict] = []
    seen: set[str] = set()
    items = snapshots.items() if isinstance(snapshots, dict) else []
    for raw_sym, snap in items:
        reasons: list[str] = []
        norm = normalize_symbol(raw_sym)  # ASCII check BEFORE upper-casing ("ß" is not "SS")
        if norm is None:
            rejected.append({"symbol": str(raw_sym), "reasons": ["invalid symbol"]})
            continue
        sym = norm
        if sym in seen:
            rejected.append({"symbol": sym, "reasons": ["duplicate symbol"]})
            continue
        seen.add(sym)
        if sym in blocked:
            reasons.append("blocked symbol")
        if listings is not None:
            problem = listing_problem(listings.get(sym), sym)
            if problem:
                reasons.append(problem)
        m, why = _metrics(snap, avgs, sym)
        if m is None:
            reasons.append(why)
            rejected.append({"symbol": sym, "reasons": reasons})
            continue
        if today is not None:
            bar_day = _bar_day(m["bar_t"])
            if bar_day is None:
                reasons.append("stale daily bar (missing or invalid timestamp)")
            elif bar_day != today:
                reasons.append(f"stale daily bar ({bar_day} is not {today})")
        if now is not None:
            for kind in ("trade", "quote"):
                why_stale = _staleness(kind, m[f"{kind}_t"], now, criteria.max_quote_age_s)
                if why_stale:
                    reasons.append(why_stale)
        if m["price"] < criteria.min_price:
            reasons.append(f"price {m['price']} below minimum {criteria.min_price}")
        elif m["price"] > criteria.max_price:
            reasons.append(f"price {m['price']} above maximum {criteria.max_price}")
        if m["volume"] < criteria.min_volume:
            reasons.append(f"volume {m['volume']} below minimum {criteria.min_volume}")
        if m["spread_raw"] is None:
            reasons.append("spread unavailable (quote missing or crossed)")
        elif m["spread_raw"] > criteria.max_spread_pct:
            reasons.append(f"spread {m['spread_pct']}% above maximum {criteria.max_spread_pct}%")
        gap = m["gap_pct"]
        if criteria.direction == "up" and m["gap_raw"] < 0:
            reasons.append(f"gap {gap}% is in the wrong direction (down)")
        elif abs(m["gap_raw"]) < criteria.min_gap_pct:
            reasons.append(f"gap {gap}% below minimum {criteria.min_gap_pct}%")
        if reasons:
            rejected.append({"symbol": sym, "reasons": reasons})
        else:
            candidates.append({"symbol": sym, **m})
    rejected.sort(key=lambda r: r["symbol"])
    candidates.sort(key=_sort_key)
    out_candidates = []
    for c in candidates:
        out_candidates.append({
            "symbol": c["symbol"],
            "price": str(c["price"]),
            "gap_pct": str(c["gap_pct"]),
            "spread_pct": None if c["spread_pct"] is None else str(c["spread_pct"]),
            "volume": c["volume"],
            "rvol": None if c["rvol"] is None else str(c["rvol"]),
            "rvol_note": c["rvol_note"],
        })
    return {
        "candidates": out_candidates,
        "rejected": rejected,
        "criteria": _criteria_dict(criteria),
    }


__all__ = [
    "ScanCriteria", "criteria_from_dict", "fetch_snapshots", "scan", "validate_symbols"
]
