"""Live run fixtures (UI-5, #218): one New York day as run_day leaves it, in temporary folders.

The journal is written by the journal's own code (Journal.append, record_order_event), the
configs are the repo's examples, and the budget and heartbeat have the shapes
pipeline.budget and pipeline.run_day write (their keys are checked against those writers in
test_live.py). The earnings cache files hold "{}": the UI only ever lists their names.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from journal.log import Journal
from journal.orders import client_order_id
from ui.api.paths import Paths
from ui.tests.journal_fixtures import approved_entry

NY = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parent.parent.parent
LIVE_NOW = datetime(2026, 11, 23, 10, 12, tzinfo=NY)  # a Monday, market open
DAY = LIVE_NOW.date().isoformat()
KINDS = ("normal", "before_scan", "not_running", "not_running_closed", "ended",
         "stale_heartbeat", "future_heartbeat", "corrupt_journal", "unreadable_budget",
         "budget_exhausted", "kill_switch", "held", "calendar_down", "run_end_cap")


def configs(paths: Paths, **limits_changes: object) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    limits = json.loads((REPO / "config" / "limits.example.json").read_text(encoding="utf-8"))
    limits.update(limits_changes)
    (paths.config_dir / "limits.json").write_text(json.dumps(limits), encoding="utf-8")
    shutil.copy(REPO / "config" / "run_day.example.json", paths.config_dir / "run_day.json")


def budget(paths: Paths, **changes: object) -> Path:
    """journal/state/budget-<day>.json, in pipeline.budget._empty's shape."""
    s = {"day": DAY, "decisions": 3, "symbols": ["NVDA", "KO", "AMD"], "tokens_used": 214000,
         "reservations": {"a1b2": 12000}, "fmp_requests": 1, "av_calls": 1, "exhausted": None}
    s.update(changes)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    p = paths.state_dir / f"budget-{DAY}.json"
    p.write_text(json.dumps(s, sort_keys=True), encoding="utf-8")
    return p


def heartbeat(paths: Paths, at: datetime, phase: str) -> None:
    """journal/state/run_day.heartbeat.json, in pipeline.run_day.Day.beat's shape."""
    body = {"pid": 4242, "at": at.isoformat(), "phase": phase, "decisions": 3,
            "tokens_used": 214000, "tokens_reserved": 12000, "fmp_requests": 1, "av_calls": 1}
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.state_dir / "run_day.heartbeat.json").write_text(json.dumps(body, sort_keys=True),
                                                           encoding="utf-8")


def caches(paths: Paths, *names: str) -> None:
    folder = paths.journal.parent / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / f"{n}-{DAY}.json").write_text("{}", encoding="utf-8")


def morning(j: Journal, *, scan: bool = True) -> None:
    t = lambda m: LIVE_NOW.replace(hour=9, minute=0) + timedelta(minutes=m)  # noqa: E731
    j.append({"status": "run_day_start", "day": DAY, "at": t(25).isoformat(), "pid": 4242,
              "caps": {"max_decisions": 10, "max_tokens": 600000, "max_fmp_requests": 3,
                       "max_av_calls": 5}, "universe": ["NVDA", "KO", "AMD", "DIS"]})
    if not scan:
        return
    j.append({"status": "run_day_scan", "at": t(40).isoformat(), "ok": True,
              "candidates": ["NVDA", "KO"], "rejected": 18})
    j.append(approved_entry("r-nvda", "p-nvda", t(45), symbol="NVDA", qty=6,
                            max_loss="15.91"))
    cid = client_order_id("r-nvda", "p-nvda")
    j.record_order_event(cid, "submitting", t(45) + timedelta(seconds=4),
                         limit_price="250.25")
    j.record_order_event(cid, "submitted", t(45) + timedelta(seconds=5))
    j.append({"status": "pass", "run_id": "r-ko", "symbol": "KO", "forced_pass": None,
              "checked_at": t(60).isoformat()})
    j.append({"status": "skipped", "symbol": "DIS", "run_id": "r-dis",
              "reason": "no 8-K item 2.02 in the last 100 days",
              "checked_at": t(65).isoformat()})


def write_live(paths: Paths, kind: str) -> datetime:
    """Write one Live run case. Returns the clock to read it at."""
    assert kind in KINDS, kind
    j = Journal(paths.journal)
    now = LIVE_NOW
    # two positions at once, so "normal" is all normal with one open; the reached limit has
    # its own test
    changes: dict = {"max_open_positions": 2}
    if kind == "kill_switch":
        changes["trading_enabled"] = False
    configs(paths, **changes)
    if kind in ("not_running", "not_running_closed"):
        # yesterday's heartbeat is still on disk; nothing ran today
        heartbeat(paths, LIVE_NOW - timedelta(days=3), "ended")
        if kind == "not_running_closed":
            now = LIVE_NOW.replace(hour=18)
        return now
    if kind == "before_scan":
        morning(j, scan=False)
        budget(paths, decisions=0, symbols=[], tokens_used=0, reservations={},
               fmp_requests=0, av_calls=0)
        now = LIVE_NOW.replace(hour=9, minute=35)
        heartbeat(paths, now - timedelta(seconds=10), "waiting for the scan")
        return now
    morning(j)
    caches(paths, "av-earnings", "fmp-earnings")
    if kind == "held":
        j.append({"status": "run_day_alert", "at": LIVE_NOW.replace(hour=9, minute=26)
                  .isoformat(), "message": "an order from an earlier day may still hold a "
                  "position", "orders": ["ta-0123"]})
        j.append({"status": "run_day_end", "day": DAY, "at": LIVE_NOW.replace(
            hour=9, minute=26).isoformat(), "reason": "held: position from an earlier day"})
    b = budget(paths)
    phase = "deciding AMD"
    hb_at = now - timedelta(seconds=12)
    if kind == "ended":
        now = LIVE_NOW.replace(hour=15, minute=45)
        budget(paths, reservations={})
        j.append({"status": "pass", "run_id": "r-amd", "symbol": "AMD", "forced_pass": None,
                  "checked_at": LIVE_NOW.replace(hour=10, minute=20).isoformat()})
        j.append({"status": "run_day_end", "day": DAY, "at": LIVE_NOW.replace(
            hour=15, minute=30).isoformat(), "reason": "entry window closed",
            "decisions": 3, "tokens_used": 214000, "fmp_requests": 1, "av_calls": 1})
        hb_at, phase = LIVE_NOW.replace(hour=15, minute=30), "ended"
    if kind == "run_end_cap":
        budget(paths, decisions=10, reservations={})
        j.append({"status": "run_day_end", "day": DAY, "at": (now - timedelta(minutes=1))
                  .isoformat(), "reason": "decision cap reached (10 today)", "decisions": 10})
        hb_at, phase = now - timedelta(minutes=1), "ended"
    if kind == "stale_heartbeat":
        hb_at = now - timedelta(seconds=95)
    if kind == "future_heartbeat":
        hb_at = now + timedelta(minutes=5)
    if kind == "unreadable_budget":
        b.write_text("{not json", encoding="utf-8")
    if kind == "budget_exhausted":
        budget(paths, exhausted="daily budget file could not be written")
    if kind == "calendar_down":
        os.remove(paths.journal.parent / "cache" / f"av-earnings-{DAY}.json")
        budget(paths, av_calls=5)
    if kind == "corrupt_journal":
        with open(paths.journal, "ab") as fh:
            fh.write(b"{this is not json\n")
    heartbeat(paths, hb_at, phase)
    return now
