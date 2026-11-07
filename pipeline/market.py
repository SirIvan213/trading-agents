"""Market-time helpers and the one sanctioned path to `scan` (issue #9)."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from tools.config import Limits
from tools.scan import ScanCriteria, scan

NY = ZoneInfo("America/New_York")


def ny_today(now: datetime) -> date:
    """The New York calendar date of `now`. ValueError if `now` is naive."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(NY).date()


def run_scan(
    snapshots: dict,
    criteria: ScanCriteria,
    limits: Limits,
    now: datetime,
    avg_volume: dict[str, int] | None = None,
) -> dict:
    """Run tools.scan.scan with today=ny_today(now) and `now`, so stale daily bars and stale
    trades or quotes (criteria.max_quote_age_s, #12) are rejected.

    This is the ONLY sanctioned way for the pipeline and agents to scan. Never call
    tools.scan.scan directly elsewhere: without `today`, a pre-market scan ranks
    gaps against yesterday's daily bar.
    """
    return scan(snapshots, criteria, limits, avg_volume=avg_volume, today=ny_today(now),
                now=now)
