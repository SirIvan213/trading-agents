"""Live check of the event gate for #146: print the gate's verdict and each source's raw input.

    python -m pipeline.smoke_events AAPL TSLA            # dry plan: prints what it would fetch
    python -m pipeline.smoke_events AAPL TSLA --live     # real requests (see below)

Without --live NOTHING is requested: the script prints the plan (which sources, how many
calls, which keys are set, never their values) and exits 3. With --live it makes real
requests:
  - FMP earnings calendar: ONE range call for [today, today + 7 days] (billed on FMP's side;
    FMP_API_KEY), then cached on disk for the New York day (journal/cache/);
  - Nasdaq Trader trade-halt feed: ONE fetch (free, no key; never more than once a minute);
  - per symbol: SEC EDGAR submissions (SEC_USER_AGENT; the CIK map once) and Alpaca news for
    the last 24 hours (ALPACA_API_KEY / ALPACA_SECRET_KEY).
No Claude call, no order, no journal write. Only Ivan runs it with --live (CLAUDE.md rule 6).

For #146 it also prints the calendar's row count, its first and last dates and rows per day,
so the FMP row cap and the "rows from today through the 3rd trading day" rule can be checked
against real replies.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.data import LiveData
from pipeline.events import calendar_input, gate_inputs, halts_input
from pipeline.market import NY
from pipeline.smoke_common import load_env
from tools.config import load_limits
from tools.earnings_calendar import FMP_ROW_CAP
from tools.event_gate import CALENDAR_HORIZON_DAYS, compact_ticker, event_gate, norm_ticker
from tools.halt_feed import HALT_FEED_URL
from tools.market_calendar import CalendarError, day_times
from tools.scan import validate_symbols

NOT_LIVE = 3  # exit code: --live not given, nothing was requested
KEYS = ("FMP_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "SEC_USER_AGENT")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m pipeline.smoke_events", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbols", nargs="+", help="tickers to check, e.g. AAPL TSLA")
    p.add_argument("--live", action="store_true",
                   help="make the real requests (FMP is billed). Without it the script only "
                        "prints the plan and exits 3")
    env = p.add_mutually_exclusive_group()
    env.add_argument("--env-file", type=Path, default=None,
                     metavar="PATH", help="load keys from PATH instead of the repo's .env")
    env.add_argument("--no-dotenv", action="store_true",
                     help="don't read any .env file; use only the exported environment")
    return p


def _window_end(now: datetime) -> datetime:
    """Today's flat-by in New York (as the order path computes it)."""
    try:
        flat = day_times(now.astimezone(NY).date(), load_limits())[2]
    except (CalendarError, ValueError, OSError):
        return now
    return datetime.combine(now.astimezone(NY).date(), flat, tzinfo=NY)


def plan_lines(symbols: list[str]) -> list[str]:
    """The dry plan: what --live would request. Names keys as set/missing, never values."""
    keys = ", ".join(f"{k} {'set' if os.environ.get(k, '').strip() else 'MISSING'}"
                     for k in KEYS)
    return [
        f"symbols: {', '.join(symbols)}",
        f"FMP earnings calendar: 1 range call for [today, today + {CALENDAR_HORIZON_DAYS} days]"
        " (billed on FMP's side), cached for the New York day in journal/cache/",
        f"Nasdaq trade-halt feed: 1 fetch of {HALT_FEED_URL} (free; at most once a minute)",
        f"SEC EDGAR: the ticker->CIK map once, then 1 submissions call per symbol "
        f"({len(symbols)})",
        f"Alpaca news: 1 call (or more pages) per symbol for the last 24 hours ({len(symbols)})",
        f"keys: {keys}",
        "No Claude call, no order, no journal write.",
    ]


def _calendar_lines(cal: dict) -> list[str]:
    if cal.get("ok") is not True:
        return [f"FMP calendar: NOT OK: {cal.get('reason')}"]
    rows = cal.get("rows") or []
    per_day = Counter(r.get("date") for r in rows)
    days = sorted(per_day)
    out = [f"FMP calendar: ok; fetched {cal.get('fetched_at')}; range {cal.get('from')}.."
           f"{cal.get('to')}; {len(rows)} rows (cap {FMP_ROW_CAP})"]
    if days:
        out.append(f"  first row {days[0]}, last row {days[-1]}")
        out.append("  rows per day: " + ", ".join(f"{d} {per_day[d]}" for d in days))
    return out


def _halt_lines(halts: dict) -> list[str]:
    if halts.get("ok") is not True:
        return [f"Nasdaq halt feed: NOT OK: {halts.get('reason')}"]
    feed = halts.get("feed") or {}
    items = feed.get("items") or []
    codes = Counter(i.get("reason") or "?" for i in items)
    return [f"Nasdaq halt feed: ok; fetched {halts.get('fetched_at')}; pubDate "
            f"{feed.get('pub_date')}; {len(items)} items; codes "
            + (", ".join(f"{c} {n}" for c, n in sorted(codes.items())) or "none")]


def _raw_for(symbol: str, inputs: dict) -> list[str]:
    """Each source's raw input for one symbol (no secrets: none of these hold keys)."""
    out = []
    cal = inputs["calendar"]
    rows = [r for r in (cal.get("rows") or []) if norm_ticker(r.get("symbol")) ==
            norm_ticker(symbol)] if cal.get("ok") else []
    out.append(f"  fmp rows for {symbol}: {rows if rows else 'none'}")
    fil = inputs["filings"]
    if fil.get("ok") is not True:
        out.append(f"  sec: NOT OK: {fil.get('reason')}")
    else:
        subs = fil.get("submissions")
        if subs is None:
            out.append("  sec: no filer found (no CIK)")
        else:
            recent = (subs.get("filings") or {}).get("recent") or {}
            pairs = [(d, i) for d, f, i in zip(recent.get("filingDate", []),
                                                recent.get("form", []),
                                                recent.get("items", []), strict=False)
                     if str(f).startswith("8-K") and "2.02" in str(i)]
            out.append(f"  sec: tickers {subs.get('tickers')}; 8-K item 2.02 dates "
                       f"{[d for d, _ in pairs[:4]] or 'none'}")
    news = inputs["news"]
    if news.get("ok") is not True:
        out.append(f"  news: NOT OK: {news.get('reason')}")
    else:
        items = (news.get("news") or {}).get("news")
        out.append(f"  news: {len(items) if isinstance(items, list) else 'malformed'} items")
        for it in (items or [])[:5]:
            if isinstance(it, dict):
                out.append(f"    {it.get('created_at')} {str(it.get('headline'))[:100]!r}")
    halts = inputs["halts"]
    mine = [i for i in ((halts.get("feed") or {}).get("items") or [])
            if compact_ticker(i.get("symbol")) == compact_ticker(symbol)] if halts.get("ok") else []
    out.append(f"  nasdaq halt items for {symbol}: {mine if mine else 'none'}")
    return out


def run_live(symbols: list[str], data: Any, now: datetime) -> list[str]:
    """Fetch shared inputs once, then each symbol's; return the report lines."""
    window_end = _window_end(now)
    today = now.astimezone(NY).date()
    calendar = calendar_input(data, today)
    halts = halts_input(data)
    lines = [f"== Event gate check (LIVE) as of {now.astimezone(NY):%Y-%m-%d %H:%M:%S} ET; "
             f"window end {window_end.astimezone(NY):%H:%M} ET =="]
    lines += _calendar_lines(calendar) + _halt_lines(halts)
    for sym in symbols:
        inputs = gate_inputs(data, sym, now, calendar=calendar, halts=halts)
        gate = event_gate(sym, now, window_end, **inputs)
        lines.append(f"\n[{sym}] verdict {gate['status']}")
        lines.append(f"  reason: {gate['reason']}")
        lines.append(f"  earnings {gate['earnings']['status']}; halt {gate['halt']['status']}")
        for f in gate["findings"]:
            lines.append(f"  - {f['status']} ({f['check']}/{f['source']}): {f['reason']}")
        lines += _raw_for(sym, inputs)
    return lines


def main(argv: list[str] | None = None, *, data: Any = None,
         clock: Any = None) -> int:
    """Entry point. `data` and `clock` may be injected (tests); then --live uses them."""
    args = _parser().parse_args(argv)
    try:
        symbols = validate_symbols(args.symbols)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    code = load_env(args)
    if code is not None:
        return code
    if not args.live:
        print("== smoke_events: dry plan only, NOTHING was requested ==")
        for line in plan_lines(symbols):
            print(line)
        print("\nRe-run with --live to make these requests (FMP is billed on FMP's side).")
        return NOT_LIVE
    now = (clock or (lambda: datetime.now(NY)))()
    for line in run_live(symbols, data if data is not None else LiveData(), now):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

