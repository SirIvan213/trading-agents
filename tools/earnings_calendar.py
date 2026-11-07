"""Earnings calendar from Financial Modeling Prep (issues #13, #112).

One range call per day: GET /stable/earnings-calendar?from=&to=&includeReportTimes=true&apikey=
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

import os
import re
import urllib.parse
from collections.abc import Callable
from datetime import date, datetime

from tools.http import get_json

FMP_EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings-calendar"
REPORT_TIMES = frozenset({"bmo", "amc"})
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


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


def _row_error(row: object) -> str | None:
    """Why a row is malformed, or None. Every row needs a symbol and a YYYY-MM-DD date; the
    report-time key must be present (requested with includeReportTimes) and be "bmo", "amc"
    or null; `confirmed`, when present, must be a boolean."""
    if not isinstance(row, dict):
        return "row is not an object"
    sym, day = row.get("symbol"), row.get("date")
    if not isinstance(sym, str) or not sym.strip():
        return "row has no symbol"
    if not isinstance(day, str) or not _DATE.fullmatch(day):
        return f"row for {sym} has no valid date"
    try:
        date.fromisoformat(day)
    except ValueError:
        return f"row for {sym} has no valid date"
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
    {"ok": False, "reason"}. Any malformed row makes the whole reply malformed: a reply that is
    partly wrong cannot prove a symbol clear."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        return {"ok": False, "reason": "earnings calendar fetch time is not timezone-aware"}
    if not isinstance(reply, list):
        return {"ok": False, "reason": "earnings calendar reply is not a list"}
    rows = []
    for row in reply:
        err = _row_error(row)
        if err:
            return {"ok": False, "reason": f"earnings calendar reply is malformed: {err}"}
        rows.append({
            "symbol": row["symbol"].strip().upper(),
            "date": row["date"],
            "time": row["time"],
            "confirmed": row.get("confirmed") is True,
        })
    return {"ok": True, "fetched_at": fetched_at.isoformat(), "from": start.isoformat(),
            "to": end.isoformat(), "rows": rows}
