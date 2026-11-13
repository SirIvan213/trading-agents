"""Decision journal: one JSON object per line in journal/decisions.jsonl (git-ignored).

Phase 4 sends nothing to a broker. An approved order is written here with status
"approved_not_sent"; sending is Phase 5. Today's DayState for risk_check is read back from
these entries, fail-closed: an unreadable journal blocks the order instead of resetting the
day's count.

Entries hold the order, the verdict and ids. They never hold analyst or trader text or scores.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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


LOCK_TIMEOUT_S = 10.0


def _try_lock(fh) -> bool:  # noqa: ANN001 - a binary file object
    """Take an exclusive, non-blocking OS lock on `fh`. True if taken."""
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh) -> None:  # noqa: ANN001
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class Journal:
    """Append-only JSONL journal."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._tx_lock = threading.Lock()
        self._holder: int | None = None  # thread ident holding the lock through this object

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def exclusive(self, timeout_s: float | None = None) -> Iterator[None]:
        """Hold the journal's cross-process lock (#107) for a read-check-append.

        An OS file lock on `<journal>.lock` (msvcrt on Windows, flock elsewhere), so two
        processes, or two Journal objects on one file, cannot interleave an approval's
        day_state re-check and its append, nor two appends. Re-entrant for the thread that
        already holds it through this object (append() inside an approval's exclusive block).
        Raises JournalError if the lock cannot be taken within `timeout_s` (default
        LOCK_TIMEOUT_S, read at call time), so a contended or broken lock means no write and no
        approval. Re-entrancy is by thread id, so never hold it across a yield or an await.
        """
        me = threading.get_ident()
        if self._holder == me:
            yield  # nested: the outer block releases the lock
            return
        if timeout_s is None:
            timeout_s = LOCK_TIMEOUT_S
        deadline = time.monotonic() + timeout_s
        if not self._tx_lock.acquire(timeout=max(timeout_s, 0)):
            raise JournalError("journal lock timed out")
        try:
            try:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                fh = self.lock_path.open("a+b")
            except OSError as exc:
                raise JournalError(f"journal lock failed ({type(exc).__name__})") from None
            try:
                while not _try_lock(fh):
                    if time.monotonic() >= deadline:
                        raise JournalError("journal lock timed out")
                    time.sleep(0.01)
                self._holder = me
                try:
                    yield
                finally:
                    self._holder = None
                    try:
                        _unlock(fh)
                    except OSError:
                        pass  # closing fh below releases the OS lock anyway
            finally:
                fh.close()
        finally:
            self._tx_lock.release()

    def append(self, entry: dict) -> None:
        """Append one entry under the cross-process lock, so concurrent writers (other
        processes or Journal objects) can never overwrite each other's line. Raises
        JournalError if it cannot be written or the lock cannot be taken."""
        line = json.dumps(entry, sort_keys=True, default=str)
        with self.exclusive():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                raise JournalError(
                    f"journal write failed ({type(exc).__name__})") from None

    def entries(self) -> list[dict]:
        """Every entry, oldest first. Raises JournalError for an unreadable or corrupt file.

        Reads under the journal's lock (#128), like every write, so a reader never sees
        another process's half-written last line. A last line WITHOUT its newline is
        therefore not a write in progress but one a crash cut off: it is refused (fail
        closed), and `python -m journal.repair` moves it aside after a human looks."""
        with self.exclusive():
            return self._read_entries()

    def _read_entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise JournalError(f"journal unreadable ({type(exc).__name__})") from None
        if text and not text.endswith("\n"):
            raise JournalError(
                "journal's last line was cut off (no newline): a write did not finish. "
                "Run `python -m journal.repair` to move it aside")
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
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise JournalError("now must be timezone-aware")
        try:
            today = now.astimezone(NY).date()
        except (OverflowError, ValueError):  # #127: an aware clock at the date-range limits
            raise JournalError("now is outside the supported date range") from None
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
                day = when.astimezone(NY).date()
            except (KeyError, ValueError, InvalidOperation, OverflowError):
                raise JournalError("journal has a malformed approved entry") from None
            if day == today:
                trades += 1
                risk += max_loss
        return DayState(trades_today=trades, risk_used_today=risk)
