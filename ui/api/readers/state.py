"""run_day's state files, read without locks: the daily budget and the heartbeat.

Both are written by temp file + os.replace (pipeline/budget.py, pipeline/run_day.py), so a
reader always sees a whole file. The budget's own lock is never taken: a reader holding it
could make run_day's next reservation time out.

On Windows, os.replace (MoveFileExW) onto a file that ANY process holds open fails, even when
that handle shares delete (open_shared): a failed budget write is CapReached and stops the
day's decisions. A POSIX-semantics rename (FileRenameInfoEx) does succeed over an open_shared
reader, so until #213 makes the budget write use it (and retry), the app only checks that the
budget file EXISTS, from the folder's listing, and never opens it (budget_exists). The
heartbeat may be read: its write is best effort, and the next one follows within 30 s.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ui.api.readers.shared_open import read_bytes_shared

HEARTBEAT_NAME = "run_day.heartbeat.json"  # pipeline.run_day.Day.heartbeat_path
HEARTBEAT_STALE_S = 90  # run_day rewrites it at least every 30 s (pipeline.run_day.HEARTBEAT_S)


@dataclass(frozen=True)
class StateFile:
    """One JSON object file as read now. `data` is None when missing or unusable."""

    name: str
    exists: bool
    data: dict | None = None
    error: str | None = None

    def view(self) -> dict:
        return {"file": self.name, "exists": self.exists, "error": self.error}


def read_state_file(path: Path) -> StateFile:
    """Read one JSON object file. Never raises; an error never echoes the file's contents."""
    p = Path(path)
    try:
        raw = read_bytes_shared(p)
    except FileNotFoundError:
        return StateFile(p.name, exists=False)
    except OSError as exc:
        return StateFile(p.name, exists=True, error=f"unreadable ({type(exc).__name__})")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return StateFile(p.name, exists=True, error="not JSON")
    if not isinstance(data, dict):
        return StateFile(p.name, exists=True, error="not a JSON object")
    return StateFile(p.name, exists=True, data=data)


def budget_path(state_dir: Path, day: date) -> Path:
    """pipeline.budget.DailyBudget(...).path for `day`."""
    return Path(state_dir) / f"budget-{day.isoformat()}.json"


def budget_exists(state_dir: Path, day: date) -> bool:
    """Whether the day's budget file exists, from the folder's listing: the file itself is
    never opened (see the module docstring, #213)."""
    name = budget_path(state_dir, day).name
    try:
        with os.scandir(state_dir) as it:
            return any(e.name == name for e in it)
    except FileNotFoundError:
        return False


def heartbeat_path(state_dir: Path) -> Path:
    return Path(state_dir) / HEARTBEAT_NAME


def heartbeat_age_s(hb: StateFile, now: datetime) -> float | None:
    """Seconds since the heartbeat's `at`, or None when it has no usable time."""
    if hb.data is None:
        return None
    try:
        at = datetime.fromisoformat(str(hb.data.get("at")))
        if at.tzinfo is None:
            return None
        return round((now - at).total_seconds(), 1)
    except (ValueError, TypeError, OverflowError):
        return None
