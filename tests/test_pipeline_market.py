"""Issue #9: the pipeline always passes the New York date to scan."""
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import pipeline.market as market
from tests.fakes import load_fixture
from tools.config import load_limits
from tools.scan import ScanCriteria

ROOT = Path(__file__).resolve().parent.parent


def test_ny_today_premarket_utc():
    assert market.ny_today(datetime(2026, 11, 4, 13, 0, tzinfo=UTC)) == date(2026, 11, 4)


def test_ny_today_evening_et_is_previous_utc_day():
    # 02:30Z on Nov 5 is 21:30 EST on Nov 4
    assert market.ny_today(datetime(2026, 11, 5, 2, 30, tzinfo=UTC)) == date(2026, 11, 4)


def test_ny_today_summer_edt():
    # 02:30Z on Jul 2 is 22:30 EDT on Jul 1
    assert market.ny_today(datetime(2026, 7, 2, 2, 30, tzinfo=UTC)) == date(2026, 7, 1)
    assert market.ny_today(datetime(2026, 7, 2, 4, 30, tzinfo=UTC)) == date(2026, 7, 2)


def test_ny_today_naive_raises():
    with pytest.raises(ValueError):
        market.ny_today(datetime(2026, 11, 4, 8, 0))


def test_run_scan_passes_ny_date(monkeypatch):
    seen = {}

    def spy(snapshots, criteria, limits, avg_volume=None, today=None, now=None):
        seen.update(today=today, avg=avg_volume, now=now)
        return {"candidates": [], "rejected": [], "criteria": {}}

    monkeypatch.setattr(market, "scan", spy)
    now = datetime(2026, 11, 5, 2, 30, tzinfo=UTC)
    market.run_scan({}, ScanCriteria(), load_limits(), now, avg_volume={"A": 1})
    assert seen == {"today": date(2026, 11, 4), "avg": {"A": 1}, "now": now}


def _snap(day_t: str) -> dict:
    snap = load_fixture("alpaca_snapshots.json")["GAPA"]
    snap["dailyBar"]["t"] = day_t
    # fresh trade/quote relative to PREMARKET (13:00Z) so only the daily-bar rule is in play
    snap["latestTrade"]["t"] = "2026-11-04T12:59:00Z"
    snap["latestQuote"]["t"] = "2026-11-04T12:59:00Z"
    return snap


PREMARKET = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)  # 08:00 ET Nov 4


def test_run_scan_end_to_end_rejects_yesterdays_bar_premarket():
    snaps = {"GAPA": _snap("2026-11-03T05:00:00Z")}  # Nov 3 ET
    out = market.run_scan(snaps, ScanCriteria(), load_limits(), PREMARKET)
    assert out["candidates"] == []
    (rej,) = out["rejected"]
    assert rej["symbol"] == "GAPA"
    assert any("stale daily bar" in r for r in rej["reasons"])


def test_run_scan_end_to_end_accepts_todays_bar_premarket():
    snaps = {"GAPA": _snap("2026-11-04T05:00:00Z")}
    out = market.run_scan(snaps, ScanCriteria(), load_limits(), PREMARKET)
    assert [c["symbol"] for c in out["candidates"]] == ["GAPA"]
    assert out["rejected"] == []


def test_scan_only_called_from_pipeline_market():
    """Text check: no .py under agents/ or pipeline/ other than pipeline/market.py may
    import `scan` from tools.scan or call `scan(`/`tools.scan.scan(`; they must use run_scan.
    """
    offenders = []
    for folder in ("agents", "pipeline"):
        for path in (ROOT / folder).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel == "pipeline/market.py":
                continue
            src = path.read_text(encoding="utf-8")
            lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
            body = "\n".join(lines)
            if re.search(r"(?<![\w.])scan\(", body) or "scan.scan(" in body:
                offenders.append(rel)
            if re.search(r"from tools\.scan import[^\n]*\bscan\b(?!_)", body):
                offenders.append(rel + " (imports scan)")
    assert offenders == []
    market_src = (ROOT / "pipeline" / "market.py").read_text(encoding="utf-8")
    assert "today=ny_today(now)" in market_src
