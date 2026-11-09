"""Event gate: earnings and trading halts (issues #13, #112). Pure: no network.

Status is CLEAR, EVENT or UNKNOWN, and ONLY CLEAR lets an order through (risk_check BLOCKs
EVENT, UNKNOWN and a missing result). No setting changes that.

Earnings (`earnings_status`):
  CLEAR needs positive proof from both required sources:
    - the primary calendar (`calendar`): a fresh, well-formed, PLAUSIBLE whole-market reply
      covering the window, with no event for the symbol in it. In production this is Alpha
      Vantage (#167), whose dates are all estimates; the check itself does not depend on the
      source, and findings carry the calendar's own "source" ("alphavantage" or "fmp").
      Plausible means at least MIN_CALENDAR_ROWS reports dated from today to the end of the
      range, on at least MIN_CALENDAR_TRADING_DAYS different trading days: a near-empty
      calendar (FMP's free tier listed 3 reports for a week of earnings season) proves
      nothing; and
    - SEC (coverage): an 8-K item 2.02 (results of operations) filed in the last
      COVERAGE_DAYS days, proof the company reports quarterly so the calendar's silence
      means something (an empty calendar for a company it does not cover proves nothing).
  EVENT: earnings dated today (any hour, confirmed or estimated, with or without a report
    time); earnings inside the window; an ESTIMATED date within ESTIMATE_TRADING_DAYS
    trading days (dates move); an item 2.02 filed today; or a secondary source's EVENT
    (an earnings headline in the news digest, or a report on FMP's calendar).
  UNKNOWN: calendar transport errors, malformed, stale or sparse replies, a reply that does
    not cover the window; SEC unavailable or malformed; no item 2.02 in the last
    COVERAGE_DAYS days.
  Secondary sources (SECONDARY_EARNINGS_SIGNALS) can only ADD an EVENT: their failure never
  blocks on its own and never clears. FMP's calendar (`fmp_calendar`) is one (#145). Add
  another by appending (name, fn) where fn(symbol, now, window_end, sources) -> list of EVENT
  reasons; `sources` holds calendar, fmp_calendar, filings and news.

Halts (`halt_status`), from two REQUIRED sources (#112, #144):
  - the Nasdaq Trader trade-halt feed (tools.halt_feed): any halt or pause for the symbol
    dated today (incl. LULD LUDP / LUDS), any older halt still listed without a resumption,
    or a future-dated one, is EVENT. A failed, malformed, cut-short or stale feed is UNKNOWN.
  - news headlines: a headline flagged "halt" dated today is EVENT; a failed or malformed news
    fetch, or any item that cannot be read, is UNKNOWN ("cannot rule out a halt").
  CLEAR only when BOTH load cleanly and neither shows a halt.

`event_gate` combines both: EVENT if any EVENT, CLEAR only if both are CLEAR, else UNKNOWN.
Each finding keeps its own status and source, so EVENT and UNKNOWN reasons stay distinct in
the ticket and the journal. Inputs are tool output only, never model text (CLAUDE.md rule 2).

Input shapes (each fetched by the caller; a failed fetch is {"ok": False, "reason"}):
  calendar  tools.earnings_calendar.parse_av_calendar(...) (or parse_fmp_calendar) result
  fmp_calendar  tools.earnings_calendar.parse_fmp_calendar(...) result (secondary)
  filings   {"ok": True, "submissions": <EDGAR submissions dict or None>}
  news      {"ok": True, "news": <Alpaca news reply {"news": [...]}>}
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from tools.halt_feed import HALT_CODES, resumed_before
from tools.market_calendar import CalendarError, session_hours
from tools.news import digest, headline_flags, parse_8k_filings

NY = ZoneInfo("America/New_York")
CLEAR, EVENT, UNKNOWN = "CLEAR", "EVENT", "UNKNOWN"

COVERAGE_DAYS = 100           # an item 2.02 in this many days proves quarterly reporting
ESTIMATE_TRADING_DAYS = 3     # an estimated date this close blocks
MAX_CALENDAR_AGE = timedelta(hours=20)  # fetched once per day, before the open
CALENDAR_HORIZON_DAYS = 7     # callers fetch [today, today + 7 days]: covers the 3rd
                              # trading day after a long weekend, stays far under FMP's row cap
# Plausibility of the primary calendar (#167): a whole-market calendar lists many reports
# over [today, today + 7]. Fewer rows, or rows on fewer trading days, is UNKNOWN. A starting
# floor, to be recalibrated from live counts (pipeline.smoke_events prints rows per day); a
# quiet week (late December) may fall below it, which fails closed.
MIN_CALENDAR_ROWS = 50
MIN_CALENDAR_TRADING_DAYS = 3
CALENDAR_SOURCES = {"alphavantage": "Alpha Vantage", "fmp": "FMP"}
FUTURE_SKEW = timedelta(minutes=5)
NEWS_LOOKBACK_HOURS = 24
MAX_HALT_FEED_AGE = timedelta(minutes=3)   # our fetch of the feed (cached for 60 s)
MAX_HALT_FEED_LAG = timedelta(minutes=10)  # the feed's own pubDate vs our fetch

Finding = dict  # {"check": "earnings"|"halt", "source", "status": EVENT|UNKNOWN, "reason"}


def _finding(check: str, source: str, status: str, reason: str) -> Finding:
    return {"check": check, "source": source, "status": status, "reason": reason}


def _aware(dt: object) -> bool:
    return isinstance(dt, datetime) and dt.tzinfo is not None and dt.utcoffset() is not None


def _trading_days_after(day: date, n: int) -> date:
    """The n-th trading day after `day` (NYSE calendar; weekdays where it has no coverage)."""
    d, found, guessed = day, 0, False
    while found < n:
        d += timedelta(days=1)
        try:
            open_ = session_hours(d) is not None
        except CalendarError:
            open_, guessed = d.weekday() < 5, True
        if open_:
            found += 1
    if guessed:
        # A weekday the calendar does not cover may be a holiday (e.g. Jan 1): one more
        # weekday of slack keeps the horizon from coming out short (the unsafe direction).
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return d


def _is_trading_day(day: date) -> bool:
    """NYSE session that day; a weekday where the calendar has no coverage (as
    _trading_days_after guesses). risk_check blocks every order on an uncovered date anyway
    ("market calendar does not cover this date")."""
    try:
        return session_hours(day) is not None
    except CalendarError:
        return day.weekday() < 5


def _compact(text: object) -> str:
    """A ticker with every separator removed ("BRK.B", "BRK B", "BRKB" -> "BRKB"): used where
    matching more items only ever over-blocks (the halt scan)."""
    return "".join(ch for ch in norm_ticker(text) if ch.isalnum())


def compact_ticker(text: object) -> str:
    """Public name for the separator-free ticker form used by the halt checks."""
    return _compact(text)


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
# Earnings: primary calendar, SEC item 2.02 (coverage), secondary signals (EVENT only)
# --------------------------------------------------------------------------------------

def calendar_source(calendar: object) -> str:
    """The calendar's source name for findings: "alphavantage", "fmp", or "calendar"."""
    src = calendar.get("source") if isinstance(calendar, dict) else None
    return src if isinstance(src, str) and src in CALENDAR_SOURCES else "calendar"


def _row_finding(row: object, symbol: str, today: date, window_day: date, horizon: date,
                 aliases: frozenset[str], source: str) -> Finding | None:
    """The EVENT for one calendar row of this company, or None. Raises ValueError when the
    row is this company's but has no valid date."""
    if not isinstance(row, dict) or norm_ticker(row.get("symbol")) not in aliases:
        return None
    day = date.fromisoformat(str(row.get("date")))
    when = row.get("time") or "no report time given"
    listed = norm_ticker(row.get("symbol"))
    under = "" if listed == norm_ticker(symbol) else f", listed as {listed}"
    kind = "confirmed" if row.get("confirmed") is True else "estimated"
    if day == today:
        return _finding("earnings", source, EVENT,
                        f"earnings today {day} ({when}, {kind}{under})")
    if today < day <= window_day:
        return _finding("earnings", source, EVENT,
                        f"earnings {day} inside the trade window ({when}, {kind}{under})")
    if kind == "estimated" and today < day <= horizon:
        return _finding("earnings", source, EVENT,
                        f"estimated earnings date {day} within {ESTIMATE_TRADING_DAYS} trading "
                        f"days (estimated dates move{under})")
    return None


def _calendar_findings(symbol: str, now: datetime, window_end: datetime, calendar: object,
                       aliases: frozenset[str]) -> tuple[list[Finding], bool]:
    """(findings, proven_clear) from the primary earnings calendar."""
    source = calendar_source(calendar)

    def unknown(reason: str) -> tuple[list[Finding], bool]:
        return [_finding("earnings", source, UNKNOWN, reason)], False

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
        dates = [date.fromisoformat(str(r.get("date"))) for r in rows if isinstance(r, dict)]
        earliest, latest = min(dates), max(dates)
    except ValueError:
        return unknown("earnings calendar has no dated rows")
    if earliest > today:
        # a reply cut at its START (newest-first truncation, a later range served) could drop
        # today's row; a whole-market calendar lists someone reporting on a trading day
        return unknown(f"earnings calendar rows start at {earliest}, after today {today}: "
                       "the reply may be cut short")
    if latest < need_to:
        return unknown(f"earnings calendar rows stop at {latest}, before {need_to}: "
                       "the reply may be cut short")
    # Plausibility (#167): the source answered, but a near-empty answer proves nothing. FMP's
    # free tier listed 3 reports for a week of earnings season, and the gate said CLEAR.
    # Counted over [today, today + CALENDAR_HORIZON_DAYS] whatever range the reply covers (a
    # wider one must not spread the floor over months), one per (symbol, date): repeated rows
    # do not pad it.
    upto = min(end, today + timedelta(days=CALENDAR_HORIZON_DAYS))
    ahead = {(norm_ticker(r.get("symbol")), str(r.get("date"))) for r in rows
             if isinstance(r, dict) and today.isoformat() <= str(r.get("date")) <= upto.isoformat()}
    if len(ahead) < MIN_CALENDAR_ROWS:
        return unknown(f"earnings calendar lists only {len(ahead)} reports for {today}..{upto} "
                       f"(at least {MIN_CALENDAR_ROWS} needed): too sparse to prove a symbol "
                       "clear")
    trading = sum(1 for d in {date.fromisoformat(d) for _, d in ahead} if _is_trading_day(d))
    if trading < MIN_CALENDAR_TRADING_DAYS:
        return unknown(f"earnings calendar lists reports on only {trading} trading day(s) in "
                       f"{today}..{upto} (at least {MIN_CALENDAR_TRADING_DAYS} needed): too "
                       "sparse to prove a symbol clear")

    findings: list[Finding] = []
    window_day = window_end.astimezone(NY).date()
    for row in rows:
        try:
            found = _row_finding(row, symbol, today, window_day, horizon, aliases, source)
        except ValueError:
            return unknown("earnings calendar is malformed")
        if found is not None:
            findings.append(found)
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


def fmp_calendar_events(symbol: str, now: datetime, window_end: datetime,
                        sources: dict) -> list[str]:
    """Secondary signal (#145): FMP's calendar lists this company (any share class) today,
    inside the trade window, or on an estimated date within ESTIMATE_TRADING_DAYS trading
    days. Block-only: it never clears (the primary calendar decides CLEAR). A failed or
    malformed FMP calendar adds nothing and a row that cannot be read is skipped; a sparse or
    stale one still adds the rows it lists for this company, which can only over-block."""
    cal = sources.get("fmp_calendar")
    rows = cal.get("rows") if isinstance(cal, dict) and cal.get("ok") is True else None
    if not isinstance(rows, list):
        return []
    today = now.astimezone(NY).date()
    horizon = _trading_days_after(today, ESTIMATE_TRADING_DAYS)
    window_day = window_end.astimezone(NY).date()
    aliases = _aliases(symbol, sources.get("filings"))
    reasons = []
    for row in rows:
        try:
            found = _row_finding(row, symbol, today, window_day, horizon, aliases, "fmp")
        except ValueError:
            continue
        if found is not None:
            reasons.append(f"FMP calendar: {found['reason']}")
    return reasons


SecondarySignal = Callable[[str, datetime, datetime, dict], list[str]]
SECONDARY_EARNINGS_SIGNALS: tuple[tuple[str, SecondarySignal], ...] = (
    ("news", news_earnings_headlines),
    ("fmp", fmp_calendar_events),
)


def earnings_status(
    symbol: str, now: datetime, window_end: datetime, *, calendar: object, filings: object,
    news: object = None, fmp_calendar: object = None,
    secondary: tuple[tuple[str, SecondarySignal], ...] = SECONDARY_EARNINGS_SIGNALS,
) -> dict:
    """{"status": CLEAR|EVENT|UNKNOWN, "findings": [...], "clear_through", "calendar_source",
    "last_item_202"}. See the module docstring for the rule. Never raises."""
    sym = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not sym or not _aware(now) or not _aware(window_end):
        return {"status": UNKNOWN, "findings": [_finding(
            "earnings", "gate", UNKNOWN, "event gate called without a symbol, now or window")],
            "clear_through": None, "calendar_source": calendar_source(calendar),
            "last_item_202": None}
    cal, cal_clear = _calendar_findings(sym, now, window_end, calendar, _aliases(sym, filings))
    sec, covered, last_202 = _sec_findings(sym, now, filings)
    extra: list[Finding] = []
    sources = {"calendar": calendar, "fmp_calendar": fmp_calendar, "filings": filings,
               "news": news}
    for name, signal in secondary:
        try:
            reasons = signal(sym, now, window_end, sources)
        except Exception:  # noqa: BLE001 - a secondary failure never blocks or clears
            continue
        extra.extend(_finding("earnings", name, EVENT, str(r)) for r in reasons or [])
    findings = cal + sec + extra
    if any(f["status"] == EVENT for f in findings):
        status = EVENT
    elif cal_clear and covered and not findings:
        status = CLEAR
    else:
        status = UNKNOWN
    clear_through = calendar.get("to") if status == CLEAR and isinstance(calendar, dict) else None
    return {"status": status, "findings": findings, "clear_through": clear_through,
            "calendar_source": calendar_source(calendar), "last_item_202": last_202}


def _news_halt_findings(symbol: str, now: datetime, news: object) -> list[Finding]:
    """Halt findings from news. Scans EVERY raw news item (not the de-duplicated digest,
    which keeps a repeated headline's earliest copy, so a halt that also ran yesterday would
    hide today's): an item for this symbol flagged "halt" (halt, trading paused, LULD, trading
    suspended ...) dated today in New York is EVENT. A failed or malformed fetch, or ANY item
    that cannot be read (not an object, no symbols list, a symbols list without this symbol in
    any spelling, no headline, no valid time), is UNKNOWN: the fetch was for this symbol, so
    such an item might be today's halt. Clean news with no halt today adds nothing."""
    sym = _compact(symbol)
    ok = isinstance(news, dict) and news.get("ok") is True
    reply = news.get("news") if ok else None
    items = reply.get("news") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        why = news.get("reason") if isinstance(news, dict) and not ok else "malformed news reply"
        return [_finding("halt", "news", UNKNOWN,
                         f"news unavailable ({why}): cannot rule out a halt today")]
    today = now.astimezone(NY).date()
    findings: list[Finding] = []
    unreadable = 0
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("symbols"), list):
            unreadable += 1
            continue
        if sym not in {_compact(x) for x in item["symbols"]}:
            # The fetch was for this one symbol: an item that does not list it (empty list,
            # another spelling) is a mismatch we cannot read, not proof of "no halt".
            unreadable += 1
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
    return findings


def _feed_halt_findings(symbol: str, now: datetime, halts: object) -> list[Finding]:
    """Halt findings from the Nasdaq Trader halt feed (#144). `halts` is
    {"ok": True, "fetched_at", "feed": tools.halt_feed.parse_halt_feed(...)} or a failure."""
    def unknown(reason: str) -> list[Finding]:
        return [_finding("halt", "nasdaq", UNKNOWN, reason)]

    if not isinstance(halts, dict) or halts.get("ok") is not True:
        why = halts.get("reason") if isinstance(halts, dict) else None
        return unknown(f"Nasdaq halt feed unavailable: {why or 'no halt feed result'}")
    feed = halts.get("feed")
    if not isinstance(feed, dict) or feed.get("ok") is not True:
        why = feed.get("reason") if isinstance(feed, dict) else None
        return unknown(f"Nasdaq halt feed unusable: {why or 'malformed'}")
    fetched, pub = _parse_time(halts.get("fetched_at")), _parse_time(feed.get("pub_date"))
    items = feed.get("items")
    if fetched is None or pub is None or not isinstance(items, list):
        return unknown("Nasdaq halt feed result is malformed")
    now_utc = now.astimezone(UTC)
    if fetched.astimezone(UTC) > now_utc + FUTURE_SKEW:
        return unknown("Nasdaq halt feed fetch is dated in the future")
    if now_utc - fetched.astimezone(UTC) > MAX_HALT_FEED_AGE:
        return unknown(f"Nasdaq halt feed is stale (fetched {fetched.astimezone(NY):%H:%M:%S} ET)")
    if pub.astimezone(UTC) > fetched.astimezone(UTC) + FUTURE_SKEW:
        # a clock off by more than a few minutes would switch the lag check off
        return unknown(f"Nasdaq halt feed pubDate {pub.astimezone(NY):%Y-%m-%d %H:%M} ET is "
                       "in the future")
    if fetched.astimezone(UTC) - pub.astimezone(UTC) > MAX_HALT_FEED_LAG:
        return unknown(f"Nasdaq halt feed not updated since {pub.astimezone(NY):%H:%M:%S} ET")
    sym, today = _compact(symbol), now.astimezone(NY).date()
    findings: list[Finding] = []
    for it in items:
        if not isinstance(it, dict) or _compact(it.get("symbol")) != sym:
            continue
        try:
            day = date.fromisoformat(str(it.get("halt_date")))
        except ValueError:
            return unknown("Nasdaq halt feed result is malformed")
        code = str(it.get("reason") or "").strip() or "no code"
        what = f"{code} ({HALT_CODES.get(code, 'halt')})"
        at = f"{it.get('halt_time', '?')} ET"
        if day == today:
            findings.append(_finding("halt", "nasdaq", EVENT,
                                     f"Nasdaq trade halt today: {what} at {at}"))
        elif day > today:
            findings.append(_finding("halt", "nasdaq", EVENT,
                                     f"Nasdaq trade halt dated {day}: {what} at {at}"))
        elif not resumed_before(it, today):
            findings.append(_finding("halt", "nasdaq", EVENT,
                                     f"halted since {day} with no resumption listed: {what}"))
    return findings


def halt_status(symbol: str, now: datetime, news: object, halts: object = None) -> dict:
    """{"status": CLEAR|EVENT|UNKNOWN, "findings": [...]}. Never raises.

    Two required sources: the Nasdaq halt feed (`halts`, #144) and news headlines (`news`).
    EVENT if either shows a halt; CLEAR only when both load cleanly with none; otherwise
    UNKNOWN. A missing feed result (None) is UNKNOWN, never "no halts"."""
    if not _compact(symbol) or not _aware(now):
        return {"status": UNKNOWN, "findings": [_finding(
            "halt", "gate", UNKNOWN, "event gate called without a symbol or now")]}
    findings = _feed_halt_findings(symbol, now, halts) + _news_halt_findings(symbol, now, news)
    if any(f["status"] == EVENT for f in findings):
        status = EVENT
    elif findings:
        status = UNKNOWN
    else:
        status = CLEAR
    return {"status": status, "findings": findings}


def event_gate(
    symbol: str, now: datetime, window_end: datetime, *, calendar: object, filings: object,
    news: object, halts: object = None, fmp_calendar: object = None,
    secondary: tuple[tuple[str, SecondarySignal], ...] = SECONDARY_EARNINGS_SIGNALS,
) -> dict:
    """The combined gate result risk_check requires: {"status", "symbol", "checked_at",
    "window_end", "earnings", "halt", "findings", "reason"}. EVENT if any EVENT finding,
    CLEAR only when earnings and halt are both CLEAR, else UNKNOWN. Never raises."""
    earn = earnings_status(symbol, now, window_end, calendar=calendar, filings=filings,
                           news=news, fmp_calendar=fmp_calendar, secondary=secondary)
    halt = halt_status(symbol, now, news, halts)
    findings = earn["findings"] + halt["findings"]
    if any(f["status"] == EVENT for f in findings):
        status = EVENT
    elif earn["status"] == CLEAR and halt["status"] == CLEAR:
        status = CLEAR
    else:
        status = UNKNOWN
    if status == CLEAR:
        label = CALENDAR_SOURCES.get(earn["calendar_source"], "calendar")
        reason = (f"no earnings through {earn['clear_through']} ({label}); last 8-K item "
                  f"2.02 filed {earn['last_item_202']}; no halt today")
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
