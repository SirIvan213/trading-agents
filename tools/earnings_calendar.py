"""Earnings calendars: Alpha Vantage (primary, #167) and Financial Modeling Prep (#13, #112).

Alpha Vantage (`fetch_av_earnings_calendar`, `parse_av_calendar`) is the event gate's primary
calendar: with no symbol, its EARNINGS_CALENDAR endpoint returns "the full list of company
earnings scheduled" for a horizon of 3, 6 or 12 months, as CSV
(https://www.alphavantage.co/documentation/). We ask for 3 months, the shortest. The columns
are not documented officially; a third-party guide shows
symbol,name,reportDate,fiscalDateEnding,estimate,currency. Only `symbol` and `reportDate` are
required here. No report time or confirmed flag is documented, so every Alpha Vantage date is
treated as ESTIMATED (the gate blocks within 3 trading days of it). The key comes from
ALPHA_VANTAGE_API_KEY. Free keys allow 25 calls a day; the terms license personal,
non-commercial use, which includes "investment analysis, research, testing, monitoring" that
is "private and individual in nature" (Terms of Service, section 2.a).

FMP, below, is now a block-only secondary signal (#145): the free tier returned 3 rows for a
week of earnings season (#167), which proves nothing.

FMP: one range call per day: GET /stable/earnings-calendar?from=&to=&includeReportTimes=true&apikey=
returns a JSON list with one row per company report, e.g.

    {"symbol": "JW-B", "date": "2026-09-04", "time": "bmo", "confirmed": false,
     "epsActual": 0.23, "epsEstimated": 0.4, "revenueActual": 386361000,
     "revenueEstimated": 3800000000, "periodEnding": "2026-07-31", "fiscalPeriod": "Q1",
     "fiscalYear": 2027, "lastUpdated": "2026-09-06"}

`time` is "bmo", "amc" or null (no session given). `confirmed` is a boolean (false = an
estimated date). Shape taken from a merged contract fixture recorded against the live API
(tradermonty/claude-trading-skills PR #361, 2026-09-06); FMP's own docs pages refuse automated
fetches. `includeReportTimes` must be the STRING "true": anything else is HTTP 400.

The key comes from FMP_API_KEY and never appears in errors (tools.http reports the path only).
This module only fetches and validates; tools.event_gate decides.
"""
from __future__ import annotations

import csv
import io
import os
import re
import urllib.parse
from collections.abc import Callable
from datetime import date, datetime

from tools.http import get_bytes, get_json

FMP_EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings-calendar"
REPORT_TIMES = frozenset({"bmo", "amc"})
# FMP caps rows per request (believed 4000; unverified offline). A reply this long may be cut
# short, so it is not proof that a symbol is absent.
FMP_ROW_CAP = 4000
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

AV_URL = "https://www.alphavantage.co/query"
AV_HORIZON = "3month"  # the shortest the endpoint offers; covers [today, today + 7] easily
AV_MAX_BYTES = 8_000_000  # three months of the whole market is far smaller
AV_REQUIRED_COLUMNS = ("symbol", "reportDate")


def fetch_fmp_earnings_calendar(
    start: date, end: date, opener: Callable | None = None
) -> list:
    """Fetch the FMP earnings calendar for [start, end] (all symbols). Raises RuntimeError on a
    missing key, a transport error, an HTTP error status, non-JSON or an error payload, and on
    a reply that is not a JSON list. Billed per call on FMP's side: never call it from tests."""
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise RuntimeError("FMP_API_KEY must be set in the environment")
    if start > end:
        raise ValueError("start must not be after end")
    query = urllib.parse.urlencode({
        "from": start.isoformat(), "to": end.isoformat(),
        "includeReportTimes": "true",  # the string: a bool would be HTTP 400
        "apikey": key,
    })
    body = get_json(f"{FMP_EARNINGS_URL}?{query}", {"Accept": "application/json"},
                    opener=opener)
    if not isinstance(body, list):
        # FMP reports errors as a JSON object (e.g. {"Error Message": ...}); never echo it
        raise RuntimeError("unexpected earnings calendar response shape")
    return body


def _row_error(row: object, start: date, end: date) -> str | None:
    """Why a row is malformed, or None. Every row needs a symbol and a YYYY-MM-DD date; the
    report-time key must be present (requested with includeReportTimes) and be "bmo", "amc"
    or null; `confirmed`, when present, must be a boolean."""
    if not isinstance(row, dict):
        return "row is not an object"
    sym, day = row.get("symbol"), row.get("date")
    if not isinstance(sym, str) or not sym.strip():
        return "row has no symbol"
    sym = sym.strip()[:16]  # server text reaches the journal: keep it short
    if not isinstance(day, str) or not _DATE.fullmatch(day):
        return f"row for {sym} has no valid date"
    try:
        when = date.fromisoformat(day)
    except ValueError:
        return f"row for {sym} has no valid date"
    if not start <= when <= end:
        return f"row for {sym} is dated {day}, outside the requested {start}..{end}"
    if "time" not in row:
        return f"row for {sym} has no report time field"
    if row["time"] is not None and row["time"] not in REPORT_TIMES:
        return f"row for {sym} has an unknown report time"
    if "confirmed" in row and not isinstance(row["confirmed"], bool):
        return f"row for {sym} has a non-boolean confirmed flag"
    return None


def parse_fmp_calendar(reply: object, fetched_at: datetime, start: date, end: date) -> dict:
    """Validate an FMP reply. Returns {"ok": True, "fetched_at", "from", "to", "rows"} with rows
    {"symbol", "date", "time", "confirmed"} (confirmed False when absent), or
    {"ok": False, "reason"}. Any malformed row (incl. one dated outside [start, end]) makes the
    whole reply malformed: a reply that is partly wrong cannot prove a symbol clear. An empty
    reply, or one at FMP's row cap, is not ok either (it may be cut short)."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        return {"ok": False, "reason": "earnings calendar fetch time is not timezone-aware"}
    if not isinstance(reply, list):
        return {"ok": False, "reason": "earnings calendar reply is not a list"}
    if not reply:
        # A whole-market calendar is never empty for a two-week range: an empty reply is a
        # plan restriction or a glitch, and must not read as "nobody reports" (#112 review).
        return {"ok": False, "reason": "earnings calendar reply is empty"}
    if len(reply) >= FMP_ROW_CAP:
        return {"ok": False, "reason": (
            f"earnings calendar reply has {len(reply)} rows (cap {FMP_ROW_CAP}): may be cut short")}
    rows = []
    for row in reply:
        err = _row_error(row, start, end)
        if err:
            return {"ok": False, "reason": f"earnings calendar reply is malformed: {err}"}
        rows.append({
            "symbol": row["symbol"].strip().upper(),
            "date": row["date"],
            "time": row["time"],
            "confirmed": row.get("confirmed") is True,
        })
    return {"ok": True, "source": "fmp", "fetched_at": fetched_at.isoformat(),
            "from": start.isoformat(), "to": end.isoformat(), "rows": rows}


# --------------------------------------------------------------------------------------
# Alpha Vantage (primary, #167)
# --------------------------------------------------------------------------------------

def fetch_av_earnings_calendar(opener: Callable | None = None) -> str:
    """Fetch Alpha Vantage's whole-market earnings calendar (3-month horizon) as CSV text.
    Raises RuntimeError on a missing key, a transport error, an HTTP error status, an
    oversized body and a body that is not UTF-8. One leading UTF-8 byte-order mark is
    stripped. Error and rate-limit messages come back as JSON with HTTP 200; the parser
    refuses them. The first live call needs Ivan's approval (CLAUDE.md rule 6): never call
    it from tests."""
    key = (os.environ.get("ALPHA_VANTAGE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ALPHA_VANTAGE_API_KEY must be set in the environment")
    query = urllib.parse.urlencode({"function": "EARNINGS_CALENDAR", "horizon": AV_HORIZON,
                                    "apikey": key})
    body = get_bytes(f"{AV_URL}?{query}", {"Accept": "text/csv"}, opener=opener,
                     max_bytes=AV_MAX_BYTES)
    if len(body) > AV_MAX_BYTES:
        raise RuntimeError("Alpha Vantage earnings calendar reply is too large")
    raw = bytes(body)
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("Alpha Vantage earnings calendar reply is not UTF-8") from None


def parse_av_calendar(reply: object, fetched_at: datetime, start: date, end: date) -> dict:
    """Validate an Alpha Vantage CSV reply and keep the reports dated in [start, end].

    Returns {"ok": True, "source": "alphavantage", "fetched_at", "from", "to", "rows",
    "columns", "total_rows", "rows_after_end", "sorted_by_date"} with rows {"symbol", "date",
    "time": None, "confirmed": False} (no report time or confirmed flag is documented, so
    every date is an estimate), or {"ok": False, "reason"}.

    Strict, because a reply that is partly wrong cannot prove a symbol clear. Not ok: a
    body that is not text, is empty or holds a NUL byte; a JSON body (Alpha Vantage's error and
    rate-limit messages, never echoed: they are server text); a header without `symbol` or
    `reportDate`, or with a repeated column; ANY row with the wrong number of fields, no
    symbol or no valid YYYY-MM-DD date; a body that does not end with a line break (cut
    short); a row dated BEFORE `start` (the data may be stale);
    no row in [start, end]; and no row after `end` (a 3-month calendar always lists reports
    beyond the next week, so a reply without any may be cut short). Rows after `end` are
    counted, then dropped. Plausibility (enough rows, enough trading days) is the gate's
    check (tools.event_gate)."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        return {"ok": False, "reason": "earnings calendar fetch time is not timezone-aware"}
    if not isinstance(reply, str):
        return {"ok": False, "reason": "Alpha Vantage earnings calendar reply is not text"}
    if "\x00" in reply:
        return {"ok": False, "reason": "Alpha Vantage earnings calendar reply holds NUL bytes"}
    text = reply.removeprefix("\ufeff").strip()
    if not text:
        return {"ok": False, "reason": "Alpha Vantage earnings calendar reply is empty"}
    if not reply.endswith("\n") and text[0] not in "{[":
        # A cut-short CSV can still parse (a cut at a row boundary, or inside the last
        # field); the reply must end with its final line break (#167 review).
        return {"ok": False, "reason": (
            "Alpha Vantage earnings calendar reply does not end with a line break: it may be "
            "cut short")}
    if text[0] in "{[":
        return {"ok": False, "reason": (
            "Alpha Vantage returned a message instead of the calendar (rate limit, key or "
            "plan problem)")}
    try:
        records = list(csv.reader(io.StringIO(text), strict=True))
    except csv.Error:
        return {"ok": False, "reason": "Alpha Vantage earnings calendar reply is not valid CSV"}
    header = [h.strip() for h in records[0]]
    if len(set(header)) != len(header):
        return {"ok": False, "reason": "Alpha Vantage earnings calendar header repeats a column"}
    for col in AV_REQUIRED_COLUMNS:
        if col not in header:
            return {"ok": False, "reason": (
                f"Alpha Vantage earnings calendar header has no {col} column")}
    i_sym, i_day = header.index("symbol"), header.index("reportDate")
    rows: list[dict] = []
    total = after = 0
    previous: date | None = None
    sorted_by_date = True
    for n, rec in enumerate(records[1:], start=2):
        if not rec:
            continue  # a blank line
        total += 1
        bad = f"Alpha Vantage earnings calendar reply is malformed: line {n}"
        if len(rec) != len(header):
            return {"ok": False, "reason": (
                f"{bad} has {len(rec)} fields, the header has {len(header)}")}
        sym, day = rec[i_sym].strip(), rec[i_day].strip()
        if not sym:
            return {"ok": False, "reason": f"{bad} has no symbol"}
        try:
            if not _DATE.fullmatch(day):
                raise ValueError
            when = date.fromisoformat(day)
        except ValueError:
            return {"ok": False, "reason": f"{bad} ({sym[:16]!r}) has no valid report date"}
        if previous is not None and when < previous:
            sorted_by_date = False
        previous = when
        if when < start:
            return {"ok": False, "reason": (
                f"Alpha Vantage earnings calendar lists {sym[:16]!r} on {day}, before "
                f"{start}: the data may be stale")}
        if when > end:
            after += 1
            continue
        rows.append({"symbol": sym.upper(), "date": day, "time": None, "confirmed": False})
    if not rows:
        return {"ok": False, "reason": (
            f"Alpha Vantage earnings calendar lists no reports in {start}..{end}")}
    if not after:
        return {"ok": False, "reason": (
            f"Alpha Vantage earnings calendar lists no reports after {end}: it may be cut "
            "short")}
    return {"ok": True, "source": "alphavantage", "fetched_at": fetched_at.isoformat(),
            "from": start.isoformat(), "to": end.isoformat(), "rows": rows,
            "columns": header, "total_rows": total, "rows_after_end": after,
            "sorted_by_date": sorted_by_date}
