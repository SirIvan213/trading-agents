"""Event gate: earnings and trading halts (issues #13, #112). Pure: no network.

Status is CLEAR, EVENT or UNKNOWN, and ONLY CLEAR lets an order through (risk_check BLOCKs
EVENT, UNKNOWN and a missing result). No setting changes that.

Earnings (`earnings_status`):
  CLEAR needs positive proof from both required sources:
    - FMP (primary): a fresh, well-formed calendar reply covering the window, with no event
      for the symbol in it; and
    - SEC (coverage): an 8-K item 2.02 (results of operations) filed in the last
      COVERAGE_DAYS days, proof the company reports quarterly so FMP's silence means
      something (an empty calendar for a company FMP does not cover proves nothing).
  EVENT: earnings dated today (any hour, confirmed or estimated, with or without a report
    time); earnings inside the window; an ESTIMATED date within ESTIMATE_TRADING_DAYS
    trading days (dates move); an item 2.02 filed today; or a secondary source's EVENT
    (an earnings headline in the news digest).
  UNKNOWN: FMP transport errors, malformed or stale replies, a reply that does not cover the
    window; SEC unavailable or malformed; no item 2.02 in the last COVERAGE_DAYS days.
  Secondary sources (SECONDARY_EARNINGS_SIGNALS) can only ADD an EVENT: their failure never
  blocks on its own and never clears. Add one (e.g. a block-only Alpha Vantage cross-check) by
  appending (name, fn) where fn(symbol, now, window_end, sources) -> list of EVENT reasons.

Halts (`halt_status`): a headline flagged "halt" dated today is EVENT. A failed or malformed
news fetch, or a reply with any item the digest cannot read (no valid time or headline), is
UNKNOWN ("cannot rule out a halt"), never "no halts". News that loads cleanly with no halt
today is CLEAR.

`event_gate` combines both: EVENT if any EVENT, CLEAR only if both are CLEAR, else UNKNOWN.
Each finding keeps its own status and source, so EVENT and UNKNOWN reasons stay distinct in
the ticket and the journal. Inputs are tool output only, never model text (CLAUDE.md rule 2).

Input shapes (each fetched by the caller; a failed fetch is {"ok": False, "reason"}):
  calendar  tools.earnings_calendar.parse_fmp_calendar(...) result
  filings   {"ok": True, "submissions": <EDGAR submissions dict or None>}
  news      {"ok": True, "news": <Alpaca news reply {"news": [...]}>}
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from tools.market_calendar import CalendarError, session_hours
from tools.news import digest, headline_flags, parse_8k_filings

NY = ZoneInfo("America/New_York")
CLEAR, EVENT, UNKNOWN = "CLEAR", "EVENT", "UNKNOWN"

COVERAGE_DAYS = 100           # an item 2.02 in this many days proves quarterly reporting
ESTIMATE_TRADING_DAYS = 3     # an estimated date this close blocks
MAX_CALENDAR_AGE = timedelta(hours=20)  # fetched once per day, before the open
CALENDAR_HORIZON_DAYS = 14    # callers fetch [today, today + 14 days]
FUTURE_SKEW = timedelta(minutes=5)
NEWS_LOOKBACK_HOURS = 24

Finding = dict  # {"check": "earnings"|"halt", "source", "status": EVENT|UNKNOWN, "reason"}


def _finding(check: str, source: str, status: str, reason: str) -> Finding:
    return {"check": check, "source": source, "status": status, "reason": reason}


def _aware(dt: object) -> bool:
    return isinstance(dt, datetime) and dt.tzinfo is not None and dt.utcoffset() is not None


def _trading_days_after(day: date, n: int) -> date:
    """The n-th trading day after `day` (NYSE calendar; weekdays where it has no coverage)."""
    d, found = day, 0
    while found < n:
        d += timedelta(days=1)
        try:
            open_ = session_hours(d) is not None
        except CalendarError:
            open_ = d.weekday() < 5
        if open_:
            found += 1
    return d


def norm_ticker(text: object) -> str:
    """Upper case with ".", "-" and "/" share-class separators unified ("BRK-B" == "BRK.B")."""
    if not isinstance(text, str):
        return ""
    return text.strip().upper().replace("-", ".").replace("/", ".")


def _aliases(symbol: str, filings: object) -> frozenset[str]:
    """The symbol plus every ticker the SEC filer lists ("tickers" in its submissions), all
    normalised: an FMP row under a share-class sibling (GOOG / GOOGL) is the same company's
    report, so it must not read as "no earnings" for the other class (#112 review)."""
    out = {norm_ticker(symbol)}
    subs = filings.get("submissions") if isinstance(filings, dict) else None
    tickers = subs.get("tickers") if isinstance(subs, dict) else None
    if isinstance(tickers, list):
        out.update(norm_ticker(t) for t in tickers if isinstance(t, str) and t.strip())
    out.discard("")
    return frozenset(out)


def _parse_time(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if _aware(dt) else None


# --------------------------------------------------------------------------------------
# Earnings: FMP (primary), SEC item 2.02 (coverage), secondary signals (EVENT only)
# --------------------------------------------------------------------------------------

def _fmp_findings(symbol: str, now: datetime, window_end: datetime, calendar: object,
                  aliases: frozenset[str]) -> tuple[list[Finding], bool]:
    """(findings, proven_clear) from the FMP calendar."""
    def unknown(reason: str) -> tuple[list[Finding], bool]:
        return [_finding("earnings", "fmp", UNKNOWN, reason)], False

    if not isinstance(calendar, dict) or calendar.get("ok") is not True:
        why = calendar.get("reason") if isinstance(calendar, dict) else None
        return unknown(f"earnings source unavailable: {why or 'no calendar'}")
    fetched = _parse_time(calendar.get("fetched_at"))
    try:
        start = date.fromisoformat(str(calendar.get("from")))
        end = date.fromisoformat(str(calendar.get("to")))
    except ValueError:
        return unknown("earnings calendar has no valid date range")
    rows = calendar.get("rows")
    if fetched is None or not isinstance(rows, list):
        return unknown("earnings calendar is malformed")
    now_utc, fetched_utc = now.astimezone(UTC), fetched.astimezone(UTC)
    today = now.astimezone(NY).date()
    if fetched_utc > now_utc + FUTURE_SKEW:
        return unknown("earnings calendar is dated in the future")
    if fetched.astimezone(NY).date() != today or now_utc - fetched_utc > MAX_CALENDAR_AGE:
        stamp = f"{fetched.astimezone(NY):%Y-%m-%d %H:%M}"
        return unknown(f"earnings calendar is stale (fetched {stamp} ET)")
    horizon = _trading_days_after(today, ESTIMATE_TRADING_DAYS)
    need_to = max(window_end.astimezone(NY).date(), horizon)
    if start > today or end < need_to:
        return unknown(f"earnings calendar covers {start}..{end}, not {today}..{need_to}")
    # The requested range is not proof by itself: a reply that is cut short (row cap, a
    # shorter range served) has no rows near its end. A whole-market calendar always lists
    # someone reporting on or after the last day we rely on (#112 review).
    try:
        latest = max(date.fromisoformat(str(r.get("date"))) for r in rows
                     if isinstance(r, dict))
    except ValueError:
        return unknown("earnings calendar has no dated rows")
    if latest < need_to:
        return unknown(f"earnings calendar rows stop at {latest}, before {need_to}: "
                       "the reply may be cut short")

    findings: list[Finding] = []
    window_day = window_end.astimezone(NY).date()
    for row in rows:
        if not isinstance(row, dict) or norm_ticker(row.get("symbol")) not in aliases:
            continue
        try:
            day = date.fromisoformat(str(row.get("date")))
        except ValueError:
            return unknown("earnings calendar is malformed")
        when = row.get("time") or "no report time given"
        listed = norm_ticker(row.get("symbol"))
        under = "" if listed == norm_ticker(symbol) else f", listed as {listed}"
        kind = "confirmed" if row.get("confirmed") is True else "estimated"
        if day == today:
            findings.append(_finding("earnings", "fmp", EVENT,
                                     f"earnings today {day} ({when}, {kind}{under})"))
        elif today < day <= window_day:
            findings.append(_finding("earnings", "fmp", EVENT,
                                     f"earnings {day} inside the trade window "
                                     f"({when}, {kind}{under})"))
        elif kind == "estimated" and today < day <= horizon:
            findings.append(_finding(
                "earnings", "fmp", EVENT,
                f"estimated earnings date {day} within {ESTIMATE_TRADING_DAYS} trading days "
                f"(estimated dates move{under})"))
    return findings, not findings


def _sec_findings(symbol: str, now: datetime,
                  filings: object) -> tuple[list[Finding], bool, str | None]:
    """(findings, covered, last item 2.02 date) from EDGAR submissions."""
    def unknown(reason: str) -> tuple[list[Finding], bool, str | None]:
        return [_finding("earnings", "sec", UNKNOWN, reason)], False, None

    if not isinstance(filings, dict) or filings.get("ok") is not True:
        why = filings.get("reason") if isinstance(filings, dict) else None
        return unknown(f"SEC filings unavailable: {why or 'no filings'}")
    subs = filings.get("submissions")
    if subs is None:
        return unknown(f"no SEC filer found for {symbol}: cannot prove quarterly reporting")
    try:
        recent = parse_8k_filings(subs, now - timedelta(days=COVERAGE_DAYS))
    except ValueError:
        return unknown("SEC filings are malformed")
    today = now.astimezone(NY).date()
    now_utc = now.astimezone(UTC)
    results, today_or_later = [], False
    for f in recent:
        filed = _parse_time(f.get("filed_at"))
        if filed is None or "2.02" not in f.get("items", []):
            continue
        # A filing time estimated as 23:59:59 ET, or one a few minutes ahead of our clock, is
        # still today's results: the EVENT check ignores `now` (#112 review). Coverage counts
        # only filings already made.
        if filed.astimezone(NY).date() >= today:
            today_or_later = True
        if filed.astimezone(UTC) <= now_utc:
            results.append(filed)
    findings = []
    if today_or_later:
        findings.append(_finding("earnings", "sec", EVENT,
                                 "8-K item 2.02 (results of operations) filed today"))
    if not results:
        if findings:
            return findings, False, None
        return unknown(
            f"no 8-K item 2.02 in the last {COVERAGE_DAYS} days: cannot prove {symbol} "
            "reports quarterly, so an empty calendar proves nothing")
    last = max(results)
    return findings, True, last.astimezone(NY).date().isoformat()


def _digest(symbol: str, now: datetime, news: object) -> dict | None:
    """The news digest when the news fetch succeeded and is well formed, else None."""
    if not isinstance(news, dict) or news.get("ok") is not True:
        return None
    reply = news.get("news")
    if not isinstance(reply, dict) or not isinstance(reply.get("news"), list):
        return None
    try:
        return digest(symbol, reply, [], now, lookback_hours=NEWS_LOOKBACK_HOURS)
    except ValueError:
        return None


def news_earnings_headlines(symbol: str, now: datetime, window_end: datetime,
                            sources: dict) -> list[str]:
    """Secondary signal: headlines flagged "earnings" in the last 24 hours."""
    dg = _digest(symbol, now, sources.get("news"))
    if dg is None:
        return []  # a secondary source's failure never blocks on its own
    return [f"earnings headline in the news: {h['headline']!r}"
            for h in dg["headlines"] if "earnings" in h.get("flags", [])]


SecondarySignal = Callable[[str, datetime, datetime, dict], list[str]]
SECONDARY_EARNINGS_SIGNALS: tuple[tuple[str, SecondarySignal], ...] = (
    ("news", news_earnings_headlines),
)


def earnings_status(
    symbol: str, now: datetime, window_end: datetime, *, calendar: object, filings: object,
    news: object = None,
    secondary: tuple[tuple[str, SecondarySignal], ...] = SECONDARY_EARNINGS_SIGNALS,
) -> dict:
    """{"status": CLEAR|EVENT|UNKNOWN, "findings": [...], "clear_through", "last_item_202"}.
    See the module docstring for the rule. Never raises."""
    sym = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not sym or not _aware(now) or not _aware(window_end):
        return {"status": UNKNOWN, "findings": [_finding(
            "earnings", "gate", UNKNOWN, "event gate called without a symbol, now or window")],
            "clear_through": None, "last_item_202": None}
    fmp, fmp_clear = _fmp_findings(sym, now, window_end, calendar, _aliases(sym, filings))
    sec, covered, last_202 = _sec_findings(sym, now, filings)
    extra: list[Finding] = []
    sources = {"calendar": calendar, "filings": filings, "news": news}
    for name, signal in secondary:
        try:
            reasons = signal(sym, now, window_end, sources)
        except Exception:  # noqa: BLE001 - a secondary failure never blocks or clears
            continue
        extra.extend(_finding("earnings", name, EVENT, str(r)) for r in reasons or [])
    findings = fmp + sec + extra
    if any(f["status"] == EVENT for f in findings):
        status = EVENT
    elif fmp_clear and covered and not findings:
        status = CLEAR
    else:
        status = UNKNOWN
    clear_through = calendar.get("to") if status == CLEAR and isinstance(calendar, dict) else None
    return {"status": status, "findings": findings, "clear_through": clear_through,
            "last_item_202": last_202}


def halt_status(symbol: str, now: datetime, news: object) -> dict:
    """{"status": CLEAR|EVENT|UNKNOWN, "findings": [...]}. Never raises.

    Scans EVERY raw news item (not the de-duplicated digest, which keeps a repeated headline's
    earliest copy, so a halt that also ran yesterday would hide today's): an item for this
    symbol flagged "halt" (halt, paused, LULD, trading suspended ...) dated today in New York
    is EVENT. A failed or malformed fetch, or ANY item that cannot be read (not an object, no
    symbols list, no headline, no valid time), is UNKNOWN: the fetch was for this symbol, so an
    unreadable item might be today's halt. Clean news with no halt today is CLEAR."""
    sym = norm_ticker(symbol)
    if not sym or not _aware(now):
        return {"status": UNKNOWN, "findings": [_finding(
            "halt", "gate", UNKNOWN, "event gate called without a symbol or now")]}
    ok = isinstance(news, dict) and news.get("ok") is True
    reply = news.get("news") if ok else None
    items = reply.get("news") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        why = news.get("reason") if isinstance(news, dict) and not ok else "malformed news reply"
        return {"status": UNKNOWN, "findings": [_finding(
            "halt", "news", UNKNOWN, f"news unavailable ({why}): cannot rule out a halt today")]}
    today = now.astimezone(NY).date()
    findings: list[Finding] = []
    unreadable = 0
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("symbols"), list):
            unreadable += 1
            continue
        if sym not in {norm_ticker(x) for x in item["symbols"]}:
            continue
        headline, when = item.get("headline"), _parse_time(item.get("created_at"))
        if not isinstance(headline, str) or not headline.strip() or when is None:
            unreadable += 1
            continue
        if "halt" in headline_flags(headline) and when.astimezone(NY).date() == today:
            findings.append(_finding("halt", "news", EVENT,
                                     f"trading halt today: {headline.strip()[:160]!r}"))
    if unreadable:
        findings.append(_finding(
            "halt", "news", UNKNOWN,
            f"{unreadable} news item(s) unreadable: cannot rule out a halt today"))
    if any(f["status"] == EVENT for f in findings):
        status = EVENT
    elif findings:
        status = UNKNOWN
    else:
        status = CLEAR
    return {"status": status, "findings": findings}


def event_gate(
    symbol: str, now: datetime, window_end: datetime, *, calendar: object, filings: object,
    news: object,
    secondary: tuple[tuple[str, SecondarySignal], ...] = SECONDARY_EARNINGS_SIGNALS,
) -> dict:
    """The combined gate result risk_check requires: {"status", "symbol", "checked_at",
    "window_end", "earnings", "halt", "findings", "reason"}. EVENT if any EVENT finding,
    CLEAR only when earnings and halt are both CLEAR, else UNKNOWN. Never raises."""
    earn = earnings_status(symbol, now, window_end, calendar=calendar, filings=filings,
                           news=news, secondary=secondary)
    halt = halt_status(symbol, now, news)
    findings = earn["findings"] + halt["findings"]
    if any(f["status"] == EVENT for f in findings):
        status = EVENT
    elif earn["status"] == CLEAR and halt["status"] == CLEAR:
        status = CLEAR
    else:
        status = UNKNOWN
    if status == CLEAR:
        reason = (f"no earnings through {earn['clear_through']} (FMP); last 8-K item 2.02 "
                  f"filed {earn['last_item_202']}; no halt today")
    else:
        # EVENT findings first, then UNKNOWN: the two kinds stay distinct and labelled
        ordered = sorted(findings, key=lambda f: f["status"] != EVENT)
        reason = "; ".join(f"{f['status']} ({f['source']}): {f['reason']}" for f in ordered)
    sym = symbol.strip().upper() if isinstance(symbol, str) else ""
    return {
        "status": status, "symbol": sym,
        "checked_at": now.astimezone(NY).isoformat() if _aware(now) else None,
        "window_end": window_end.astimezone(NY).isoformat() if _aware(window_end) else None,
        "earnings": earn, "halt": halt, "findings": findings, "reason": reason,
    }
