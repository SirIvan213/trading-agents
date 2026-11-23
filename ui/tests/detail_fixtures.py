"""Decision detail fixtures: for each golden decision record in tests/fixtures/records/ (written
by pipeline/records.py through the real decision path, #211), a journal written by the
journal's own code with the entries that decision leaves, and the record copied unchanged into
journal/records/<day>/RUN_ID.json. The "missing" case is the approved BUY without its record.

The golden records' run_id is "RUN_ID" (tests/test_decision_records.py normalizes it), and
their clock is 2026-11-03 12:00 New York.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from journal.log import Journal
from journal.orders import APPROVED, client_order_id

NY = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
RECORDS = REPO / "tests" / "fixtures" / "records"
RUN_ID = "RUN_ID"
NOON = datetime(2026, 11, 3, 12, 0, tzinfo=NY)
DETAIL_NOW = NOON + timedelta(minutes=1)  # the approval's plan has not expired yet
CASES = {  # case -> the golden record it reads (None: no record)
    "buy_approved": "buy_approved",
    "pass_fatal_bear": "pass_fatal_bear",
    "pass_analyst_failed": "pass_analyst_failed",
    "skipped": "skipped",
    "missing_record": None,
    "declined": "buy_declined",
    "not_answered": "buy_declined",
}


def _scan(journal: Journal) -> None:
    journal.append({"status": "run_day_scan", "at": (NOON - timedelta(seconds=20)).isoformat(),
                    "ok": True, "candidates": ["ABCD"], "rejected": 3})


def write_case(case: str, journal_path: Path) -> None:
    """The journal for `case`, and its record (if any) next to it."""
    j = Journal(journal_path)
    _scan(j)
    if case in ("buy_approved", "missing_record"):
        at = NOON + timedelta(seconds=40)
        j.append({
            "status": APPROVED, "reason": None, "run_id": RUN_ID, "plan_id": "plan-1",
            "symbol": "ABCD",
            "order": {"symbol": "ABCD", "side": "buy", "qty": 13, "entry": "103.10",
                      "stop_loss": "99.61", "take_profit": "106.59", "slippage": "0.11"},
            "max_loss": "46.80", "decided_by": "max_loss_per_trade", "quote": None,
            "gate": {"status": "CLEAR", "reason": "no earnings through 2026-11-10; no halt"},
            "verdict": {"verdict": "PASS", "reasons": []},
            "checked_at": at.isoformat(), "approved_at": at.isoformat(), "plan_age_s": 40,
            "client_order_id": client_order_id(RUN_ID, "plan-1"),
            "expires_at": (NOON + timedelta(minutes=5)).isoformat(),
        })
    elif case in ("declined", "not_answered"):
        at = NOON + timedelta(seconds=40)
        j.append({
            "status": "declined", "reason": "not approved", "run_id": RUN_ID,
            "plan_id": "plan-1", "symbol": "ABCD",
            "order": {"symbol": "ABCD", "side": "buy", "qty": 13, "entry": "103.10",
                      "stop_loss": "99.61", "take_profit": "106.59", "slippage": "0.11"},
            "max_loss": "46.80", "decided_by": "max_loss_per_trade", "quote": None,
            "gate": {"status": "CLEAR", "reason": "no earnings through 2026-11-10; no halt"},
            "verdict": {"verdict": "PASS", "reasons": []}, "checked_at": at.isoformat(),
        })
        if case == "not_answered":  # run_day's note: the terminal's deadline passed
            j.append({"status": "run_day_approval_timeout", "at": at.isoformat(),
                      "symbol": "ABCD", "run_id": RUN_ID})
    elif case == "pass_fatal_bear":
        j.append({"status": "pass", "run_id": RUN_ID, "symbol": "ABCD",
                  "forced_pass": "fatal_bear",
                  "checked_at": (NOON + timedelta(seconds=20)).isoformat()})
    elif case == "pass_analyst_failed":
        j.append({"status": "pass", "run_id": RUN_ID, "symbol": "ABCD",
                  "forced_pass": "analyst_failed",
                  "checked_at": (NOON + timedelta(seconds=20)).isoformat()})
    elif case == "skipped":
        j.append({"status": "skipped", "symbol": "ABCD", "run_id": RUN_ID,
                  "reason": "no 8-K item 2.02 in the last 100 days",
                  "checked_at": NOON.isoformat()})
    else:
        raise ValueError(case)
    name = CASES[case]
    if name is not None:
        folder = journal_path.parent / "records" / NOON.date().isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(RECORDS / f"{name}.json", folder / f"{RUN_ID}.json")
