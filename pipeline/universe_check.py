"""Check candidate tickers against the strategy's criteria and write the passing ones into
config/run_day.json's `universe` (a stop-gap list; the whole-market scan, #205, replaces it).

    python -m pipeline.universe_check            # print the pass/fail table; change nothing
    python -m pipeline.universe_check --write    # also back up run_day.json, then replace
                                                 # ONLY its "universe"

Only Ivan runs it. FREE sources only (CLAUDE.md rule 6): Alpaca paper data (the asset
endpoint and daily bars) and SEC EDGAR. No FMP, Alpha Vantage or Claude, no order, no broker.
Each criterion reuses existing code, and no new client is written:

  - primary listing on NASDAQ, NYSE or NYSE American: pipeline.events.listing_input
    (tools.listing.fetch_asset / parse_asset, Alpaca's asset endpoint, the scan's own rule);
  - reports earnings, an 8-K item 2.02 in the last COVERAGE_DAYS (100) days:
    pipeline.events.reporting_check (tools.event_gate.reporting_coverage on SEC EDGAR
    submissions; SEC_USER_AGENT from the environment);
  - last close between $10 and $500, and 20-day average daily volume of at least 2,000,000
    shares: Alpaca daily bars (tools.bars.fetch_bars, timeframe 1Day) over the last 20
    COMPLETED sessions (today's partial bar is never used).

Volume feed. Alpaca's IEX feed carries only the IEX venue's share of volume (a few percent of
the consolidated tape), so a 2M-share threshold would fail nearly every symbol on it. The
default is therefore the consolidated "sip" feed: historical SIP bars older than 15 minutes
are within Alpaca's free plan (the end of the request is 16 minutes ago, and a daily bar
ends at yesterday's close). If your key cannot read SIP the request fails and the symbol
shows as ERROR, not as a failed criterion; --feed iex exists but its volumes are not
comparable with the 2M threshold.

A symbol is PASS, FAIL (a criterion failed: the reasons are listed) or ERROR (a data request
failed: nothing is known). With --write, any ERROR refuses the write (exit 4, nothing
changed): a symbol must never drop out of the list because a request hiccuped. Run it again.
The scan logic (tools/scan.py, pipeline/market.py) is not touched, and nothing here is used
by the scan.

Exit codes: 0 done (a clean table; with --write, written); 1 refused: nothing passed; 2
refused before any request (bad config or option); 4 at least one ERROR (with --write:
nothing written).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.budget import CONFIG_PATH, RunDayConfigError, config_from_dict
from pipeline.events import _failure, listing_input, reporting_check
from pipeline.smoke_common import load_env
from tools.atomic_write import atomic_write_text
from tools.bars import fetch_bars
from tools.listing import ALLOWED_LISTINGS
from tools.symbols import validate_symbols

NY = ZoneInfo("America/New_York")
OK, NOTHING_PASSED, REFUSED, INCOMPLETE = 0, 1, 2, 4

CANDIDATES = (
    "AMD MU INTC MRVL SMCI ON DELL ANET PLTR SNOW NET DDOG MDB RBLX ROKU PINS U AI SOUN "
    "SOFI HOOD COIN AFRM UPST PYPL XYZ MARA RIOT MSTR TSLA RIVN LCID UBER LYFT "
    "IONQ RGTI RKLB ASTS OKLO SMR JOBY ACHR HIMS CVNA CHWY DKNG LULU NKE TGT CCL "
    "AAL UAL W ETSY ENPH FSLR RUN OXY MRNA VKTX NVDA META"
).split()

MIN_CLOSE = Decimal("10")
MAX_CLOSE = Decimal("500")
MIN_AVG_VOLUME = 2_000_000
VOLUME_DAYS = 20
BARS_LOOKBACK_DAYS = 50  # calendar days: 20 sessions plus weekends and holidays
SIP_DELAY = timedelta(minutes=16)  # the free plan's SIP data must be older than 15 minutes

PASS, FAIL, ERROR = "PASS", "FAIL", "ERROR"


def _bar_day(bar: dict) -> Any:
    """The NY date a daily bar belongs to (Alpaca stamps a daily bar at the session's start)."""
    when = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
    return when.astimezone(NY).date()


def daily_stats(bars: object, today: Any) -> dict:
    """{"close", "avg_volume", "days"} from daily bars: the last VOLUME_DAYS COMPLETED
    sessions (a bar dated today is partial and never used). "close" is the latest completed
    session's close (Decimal), "avg_volume" the mean volume (Decimal), "days" how many
    sessions were used. Raises ValueError for a malformed bar."""
    if not isinstance(bars, list):
        raise ValueError("bars are not a list")
    done = []
    for bar in bars:
        if not isinstance(bar, dict):
            raise ValueError("a bar is not an object")
        if _bar_day(bar) < today:
            done.append(bar)
    done.sort(key=_bar_day)
    last = done[-VOLUME_DAYS:]
    if not last:
        return {"close": None, "avg_volume": None, "days": 0}
    try:
        close = Decimal(str(last[-1]["c"]))
        volumes = [Decimal(str(b["v"])) for b in last]
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("a bar has no usable close or volume") from exc
    if not close.is_finite() or any(not v.is_finite() or v < 0 for v in volumes):
        raise ValueError("a bar has no usable close or volume")
    return {"close": close, "avg_volume": sum(volumes) / len(volumes), "days": len(last)}


def check_symbol(data: Any, fetch_daily: Any, symbol: str, now: datetime, feed: str) -> dict:
    """One row: {"symbol", "status", "reasons", "exchange", "last_item_202", "close",
    "avg_volume"}. All three criteria run, so a FAIL lists every reason it can. A request
    that failed (not a criterion that failed) makes the row ERROR."""
    reasons: list[str] = []
    errors: list[str] = []
    row: dict = {"symbol": symbol, "exchange": None, "last_item_202": None, "close": None,
                 "avg_volume": None}

    listing = listing_input(data, symbol)
    if listing.get("ok") is True:
        row["exchange"] = listing["exchange"]
        if listing["exchange"] not in ALLOWED_LISTINGS:
            reasons.append(f"primary listing is {listing['exchange']}, not NASDAQ, NYSE or "
                           "AMEX")
    elif str(listing.get("reason", "")).startswith("HTTP 404"):
        reasons.append("not an Alpaca asset (HTTP 404)")
    elif listing.get("fetch_failed"):
        errors.append(f"listing request failed: {listing.get('reason')}")
    else:
        reasons.append(f"no usable listing: {listing.get('reason')}")

    reporting = reporting_check(data, symbol, now)
    row["last_item_202"] = reporting.get("last_item_202")
    if reporting.get("covered") is not True:
        if reporting.get("fetch_failed"):
            errors.append(f"SEC request failed: {reporting.get('reason')}")
        else:
            reasons.append(f"no 8-K item 2.02 in the last 100 days ({reporting.get('reason')})")

    try:
        start = (now - timedelta(days=BARS_LOOKBACK_DAYS)).replace(microsecond=0)
        end = (now - SIP_DELAY).replace(microsecond=0)
        reply = fetch_daily(symbol, start, end, feed)
        stats = daily_stats(reply.get("bars") if isinstance(reply, dict) else None,
                            now.astimezone(NY).date())
    except Exception as exc:  # noqa: BLE001 - every failure is "unknown", never a pass
        errors.append(f"daily bars request failed: {_failure(exc)}")
        stats = None
    if stats is not None:
        row["close"], row["avg_volume"] = stats["close"], stats["avg_volume"]
        if stats["days"] < VOLUME_DAYS:
            reasons.append(f"only {stats['days']} completed daily bars (need {VOLUME_DAYS})")
        else:
            if not MIN_CLOSE <= stats["close"] <= MAX_CLOSE:
                reasons.append(f"last close {stats['close']} is outside ${MIN_CLOSE}-"
                               f"${MAX_CLOSE}")
            if stats["avg_volume"] < MIN_AVG_VOLUME:
                reasons.append(f"20-day average volume {int(stats['avg_volume']):,} is below "
                               f"{MIN_AVG_VOLUME:,}")

    row["status"] = ERROR if errors else FAIL if reasons else PASS
    row["reasons"] = errors + reasons
    return row


def render(rows: list[dict], feed: str, now: datetime) -> str:
    """The pass/fail table, as text."""
    lines = [f"universe_check at {now:%Y-%m-%d %H:%M} ET, daily bars feed: {feed}",
             f"criteria: NASDAQ/NYSE/AMEX listing; 8-K item 2.02 in 100 days; last close "
             f"${MIN_CLOSE}-${MAX_CLOSE}; {VOLUME_DAYS}-day average volume >= "
             f"{MIN_AVG_VOLUME:,}", "",
             f"{'SYMBOL':<7}{'RESULT':<7}{'EXCH':<8}{'CLOSE':>9}{'AVG VOLUME':>13}  REASON"]
    for r in rows:
        close = "-" if r["close"] is None else f"{r['close']:.2f}"
        vol = "-" if r["avg_volume"] is None else f"{int(r['avg_volume']):,}"
        lines.append(f"{r['symbol']:<7}{r['status']:<7}{(r['exchange'] or '-'):<8}{close:>9}"
                     f"{vol:>13}  {'; '.join(r['reasons'])}")
    count = {s: sum(1 for r in rows if r["status"] == s) for s in (PASS, FAIL, ERROR)}
    lines += ["", f"{count[PASS]} pass, {count[FAIL]} fail, {count[ERROR]} error "
                  f"(of {len(rows)})"]
    return "\n".join(lines)


def write_universe(config_path: Path, symbols: list[str]) -> Path:
    """Back up `config_path` to <name>.bak, then replace ONLY its "universe". Returns the
    backup. The new file is validated with run_day's own validator before it is written, and
    written atomically; the caps, times and every other key are kept as they are. Raises
    RunDayConfigError (nothing changed) for a missing or invalid config."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RunDayConfigError(f"{config_path.name} not found: copy run_day.example.json "
                                "first") from None
    except (OSError, ValueError, UnicodeDecodeError):
        raise RunDayConfigError(f"{config_path.name} is unreadable or not JSON") from None
    if not isinstance(raw, dict):
        raise RunDayConfigError(f"{config_path.name} must be a JSON object")
    new = {**raw, "universe": list(symbols)}  # every other key, in its place
    config_from_dict(new)  # caps, ceilings, universe size: run_day's own rules
    backup = config_path.with_name(config_path.name + ".bak")
    shutil.copy2(config_path, backup)
    atomic_write_text(config_path, json.dumps(new, indent=2) + "\n")
    return backup


def live_fetch_daily(symbol: str, start: datetime, end: datetime, feed: str) -> dict:
    return fetch_bars(symbol, start, end, timeframe="1Day", feed=feed)


def main(argv: list[str] | None = None, *, data: Any = None, fetch_daily: Any = None,
         now: datetime | None = None, config_path: Path | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline.universe_check", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--write", action="store_true",
                   help="back up config/run_day.json to run_day.json.bak, then replace only "
                        "its universe with the passing symbols")
    p.add_argument("--feed", choices=("sip", "iex"), default="sip",
                   help="daily bars feed (default sip: IEX volumes are a few percent of the "
                        "consolidated tape)")
    p.add_argument("--symbols", nargs="+", metavar="SYM", default=None,
                   help="check these instead of the built-in candidate list")
    env = p.add_mutually_exclusive_group()
    env.add_argument("--env-file", type=Path, default=None, metavar="PATH")
    env.add_argument("--no-dotenv", action="store_true")
    args = p.parse_args(argv)
    try:
        symbols = validate_symbols(args.symbols if args.symbols else CANDIDATES)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    path = config_path or CONFIG_PATH
    if args.write and not path.is_file():  # fail before any request, not after
        print(f"refused: {path.name} not found: copy run_day.example.json first",
              file=sys.stderr)
        return REFUSED
    if data is None:
        code = load_env(args)
        if code is not None:
            return code
        from pipeline.data import LiveData  # only listing, submissions and the CIK map run
        data = LiveData()
    fetch_daily = fetch_daily or live_fetch_daily
    clock = now or datetime.now(NY)
    rows = [check_symbol(data, fetch_daily, s, clock, args.feed) for s in symbols]
    print(render(rows, args.feed, clock))
    errors = [r["symbol"] for r in rows if r["status"] == ERROR]
    passing = [r["symbol"] for r in rows if r["status"] == PASS]
    if errors:
        print(f"\n{len(errors)} symbol(s) could not be checked ({', '.join(errors)}): "
              "run it again." + (" Nothing was written." if args.write else ""),
              file=sys.stderr)
        return INCOMPLETE
    if not args.write:
        print("\n(dry run: nothing was changed; add --write to update the universe)")
        return OK
    if not passing:
        print("\nrefused: no symbol passed, so the universe is left as it is",
              file=sys.stderr)
        return NOTHING_PASSED
    try:
        backup = write_universe(path, passing)
    except RunDayConfigError as exc:
        print(f"\nrefused: {exc}. Nothing was written.", file=sys.stderr)
        return REFUSED
    print(f"\nwrote {len(passing)} symbols to {path.name} (backup: {backup.name})")
    return OK


if __name__ == "__main__":
    sys.exit(main())
