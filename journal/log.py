"""Decision journal: one JSON object per line in journal/decisions.jsonl (git-ignored).

Phase 4 sends nothing to a broker. An approved order is written here with status
"approved_not_sent"; sending is Phase 5. Today's DayState for risk_check is read back from
these entries, fail-closed: an unreadable journal blocks the order instead of resetting the
day's count.

Entries hold the order, the verdict and ids. They never hold analyst or trader text or scores.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.risk_check import DayState

NY = ZoneInfo("America/New_York")
DEFAULT_PATH = Path(__file__).resolve().parent / "decisions.jsonl"
APPROVED = "approved_not_sent"


class JournalError(RuntimeError):
    """The journal could not be read or written."""


class Journal:
    """Append-only JSONL journal."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._lock = threading.Lock()

    def append(self, entry: dict) -> None:
        """Append one entry. Raises JournalError if it cannot be written."""
        line = json.dumps(entry, sort_keys=True, default=str)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            raise JournalError(f"journal write failed ({type(exc).__name__})") from None

    def entries(self) -> list[dict]:
        """Every entry, oldest first. Raises JournalError for an unreadable or corrupt file."""
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise JournalError(f"journal unreadable ({type(exc).__name__})") from None
        out = []
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                raise JournalError(f"journal line {n} is not JSON") from None
            if not isinstance(obj, dict):
                raise JournalError(f"journal line {n} is not an object")
            out.append(obj)
        return out

    def day_state(self, now: datetime) -> DayState:
        """Trades and risk already committed today (New York date of `now`).

        Counts every "approved_not_sent" entry approved today, even though nothing was sent:
        fail-closed, an approval uses up the day's trade and risk budget. Raises JournalError
        for a corrupt journal or a malformed approved entry.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise JournalError("now must be timezone-aware")
        today = now.astimezone(NY).date()
        trades, risk = 0, Decimal("0")
        for e in self.entries():
            if e.get("status") != APPROVED:
                continue
            try:
                when = datetime.fromisoformat(str(e["approved_at"]))
                if when.tzinfo is None:
                    raise ValueError
                max_loss = Decimal(str(e["max_loss"]))
                if not max_loss.is_finite() or max_loss < 0:
                    raise ValueError
            except (KeyError, ValueError, InvalidOperation):
                raise JournalError("journal has a malformed approved entry") from None
            if when.astimezone(NY).date() == today:
                trades += 1
                risk += max_loss
        return DayState(trades_today=trades, risk_used_today=risk)
