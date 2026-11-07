"""NYSE trading calendar: full holidays and early closes (issue #11).

Source: the official NYSE "Holidays & Trading Hours" page,
https://www.nyse.com/markets/hours-calendars (read 2026-11-06). Update the tables below from
that page each year; a year that is not in COVERED_YEARS is not a trading day as far as
risk_check is concerned (it BLOCKs: "market calendar does not cover this date").

Early closes are at 13:00 ET (1:15 p.m. for eligible options, not relevant here). On an early
close the flat-by time moves to 12:55 ET and the entry window ends as many minutes before it
as the configured window ends before the configured flat-by (15:30 vs 15:55 -> 12:30).
All times are America/New_York.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from tools.config import MARKET_CLOSE, MARKET_OPEN, Limits

NYSE_HOLIDAYS_URL = "https://www.nyse.com/markets/hours-calendars"

FULL_HOLIDAYS: dict[date, str] = {
    # 2026
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King, Jr. Day",
    date(2026, 2, 16): "Washington's Birthday",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth National Independence Day",
    date(2026, 7, 3): "Independence Day (observed)",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    # 2027
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King, Jr. Day",
    date(2027, 2, 15): "Washington's Birthday",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth National Independence Day (observed)",
    date(2027, 7, 5): "Independence Day (observed)",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed)",
}

# 13:00 ET closes. NYSE lists none for Christmas Eve 2027 (a full holiday that year) and
# none for July 3 in 2026 or 2027.
EARLY_CLOSES: dict[date, str] = {
    date(2026, 11, 27): "Day after Thanksgiving",
    date(2026, 12, 24): "Christmas Eve",
    date(2027, 11, 26): "Day after Thanksgiving",
}

COVERED_YEARS = frozenset({2026, 2027})
EARLY_CLOSE = time(13, 0)
EARLY_CLOSE_FLAT_BY = time(12, 55)


class CalendarError(ValueError):
    """The calendar does not cover the requested date."""


def covers(day: date) -> bool:
    """True if the calendar tables include `day`'s year."""
    return day.year in COVERED_YEARS


def session_hours(day: date) -> tuple[time, time] | None:
    """(open, close) in ET for a trading day, or None for a weekend or full holiday.

    Raises CalendarError for a year not in COVERED_YEARS."""
    if not covers(day):
        raise CalendarError("market calendar does not cover this date")
    if day.weekday() >= 5 or day in FULL_HOLIDAYS:
        return None
    if day in EARLY_CLOSES:
        return MARKET_OPEN, EARLY_CLOSE
    return MARKET_OPEN, MARKET_CLOSE


def closed_reason(day: date) -> str:
    """Why `day` has no session ("weekend" or the holiday name). Assumes it has none."""
    return FULL_HOLIDAYS.get(day, "weekend")


def day_times(day: date, limits: Limits) -> tuple[time, time, time]:
    """(entry_start, entry_end, flat_by) for `day`.

    Normal days: the configured values. Early closes: flat_by = min(configured flat_by,
    12:55) and entry_end keeps the configured gap before flat_by (15:30/15:55 -> 12:30), never
    later than configured. If keeping the gap would end the window before entry_start, a
    configured entry_end already before the early flat_by is kept (entry_end 10:30 stays
    10:30); otherwise entry_end becomes entry_start (fails safe). Raises CalendarError
    for an uncovered year. The caller decides what a non-trading day means."""
    hours = session_hours(day)
    start, end, flat = limits.entry_start, limits.entry_end, limits.flat_by
    if hours is None or hours[1] == MARKET_CLOSE:
        return start, end, flat
    early_flat = min(flat, EARLY_CLOSE_FLAT_BY)
    gap = _minutes(flat) - _minutes(end)
    kept_gap = _minutes(early_flat) - gap
    if kept_gap >= _minutes(start):
        early_end = min(end, _from_minutes(kept_gap))
    elif end < early_flat:
        # The configured gap is wider than the early session allows but the configured end
        # is already before the early flat-by (e.g. entry_end 10:30, flat_by 15:55): keep it.
        early_end = end
    else:
        # No room to keep the gap: the window shrinks to entry_start (fails safe).
        early_end = start
    return start, early_end, early_flat


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _from_minutes(m: int) -> time:
    return (datetime.min + timedelta(minutes=m)).time()
