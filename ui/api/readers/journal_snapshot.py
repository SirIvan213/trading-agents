"""A lock-free, incremental reader of the decision journal (journal/decisions.jsonl).

The journal's own `Journal.entries()` reads under the cross-process lock that approvals write
under, so a UI polling it could make a real approval time out ("journal_locked", nothing
approved). This reader never takes that lock and never writes anything:
  - every read opens the file with open_shared (it never blocks a writer, on Windows too);
  - only complete lines (ending in a newline) are parsed. An incomplete last line is a write
    in progress; if it is still there, unchanged, after TAIL_GRACE_S it is reported as cut
    off (the journal itself then refuses every read: `python -m journal.repair`);
  - a line that is not a JSON object is reported, and nothing after it is read: the day's
    totals cannot be trusted (the journal fails closed the same way);
  - a file that shrank (journal.repair truncates a cut-off tail in place) or was replaced is
    read again from the start.

SnapshotJournal gives the journal's own read code (orders, day_state) over a snapshot, so the
totals the UI shows are computed by exactly the code risk_check's DayState comes from. It
refuses every lock and every write.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from journal.log import Journal, JournalError
from journal.orders import OrderState
from ui.api.readers.shared_open import open_shared

TAIL_GRACE_S = 5.0


@dataclass(frozen=True)
class JournalSnapshot:
    """The journal's complete lines as of one read. The entry dicts are shared with the
    reader's cache and with later snapshots: treat them as read-only (copy before changing
    one)."""

    entries: tuple[dict, ...]
    exists: bool
    tail_bytes: int = 0  # length of an incomplete last line (0: none)
    tail_stuck: bool = False  # that line has not changed for TAIL_GRACE_S
    error: str | None = None  # unreadable or a bad line: totals cannot be trusted

    @property
    def trusted(self) -> bool:
        """The journal's own reader would accept this file: no error, no cut-off line."""
        return self.error is None and not self.tail_stuck

    def problems(self) -> list[dict]:
        out = []
        if self.error:
            out.append({"level": "error", "text": f"{self.error}: day totals cannot be "
                        "trusted, and run_day refuses orders until it is fixed"})
        if self.tail_stuck:
            out.append({"level": "error", "text": "the journal's last line is cut off (a "
                        "write did not finish): run `python -m journal.repair`"})
        return out


class JournalReader:
    """Reads the journal at `path` without its lock, appending only what was added since the
    last read. Thread-safe (FastAPI runs sync endpoints in a thread pool)."""

    def __init__(self, path: Path, *, monotonic: Callable[[], float] = time.monotonic,
                 grace_s: float = TAIL_GRACE_S) -> None:
        self.path = Path(path)
        self._monotonic = monotonic
        self._grace_s = grace_s
        self._mutex = threading.Lock()
        self._reset(None)

    def _reset(self, ident: tuple[int, int] | None) -> None:
        self._ident = ident
        self._offset = 0  # bytes of complete, parsed lines
        self._line_no = 0  # lines consumed (blank lines included, as the journal counts)
        self._entries: list[dict] = []
        self._error: str | None = None
        self._tail: tuple[int, int] | None = None  # (offset, length) of an incomplete line
        self._tail_since = 0.0

    def read(self) -> JournalSnapshot:
        with self._mutex:
            return self._read()

    def _read(self) -> JournalSnapshot:
        try:
            fh = open_shared(self.path)
        except FileNotFoundError:
            self._reset(None)
            return JournalSnapshot((), exists=False)
        except OSError as exc:
            return JournalSnapshot(tuple(self._entries), exists=True,
                                   error=f"journal unreadable ({type(exc).__name__})")
        with fh:
            try:
                st = os.fstat(fh.fileno())
                ident = (st.st_dev, st.st_ino)
                if ident != self._ident or st.st_size < self._offset:
                    self._reset(ident)  # replaced or truncated: read it all again
                fh.seek(self._offset)
                data = fh.read()
            except OSError as exc:
                return JournalSnapshot(tuple(self._entries), exists=True,
                                       error=f"journal unreadable ({type(exc).__name__})")
        self._error = None
        end = data.rfind(b"\n") + 1  # complete lines end here
        pos = 0
        while pos < end:
            nl = data.index(b"\n", pos)
            line = data[pos:nl + 1]
            n = self._line_no + 1
            if line.strip():
                problem = self._parse(line, n)
                if problem:
                    self._error = problem  # stop here; read again from this line next time
                    break
            self._line_no = n
            self._offset += len(line)
            pos = nl + 1
        tail = len(data) - end if self._error is None else 0
        stuck = False
        if tail:
            key = (self._offset, tail)
            now = self._monotonic()
            if key != self._tail:
                self._tail, self._tail_since = key, now
            stuck = now - self._tail_since >= self._grace_s
        else:
            self._tail = None
        return JournalSnapshot(tuple(self._entries), exists=True, tail_bytes=tail,
                               tail_stuck=stuck, error=self._error)

    def _parse(self, line: bytes, n: int) -> str | None:
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"journal line {n} is not JSON"
        if not isinstance(obj, dict):
            return f"journal line {n} is not an object"
        self._entries.append(obj)
        return None


class SnapshotJournal(Journal):
    """The journal's own read code (orders(), day_state()) over a snapshot's entries. It never
    opens, locks or writes any file: every lock and write raises JournalError."""

    def __init__(self, entries: tuple[dict, ...] | list[dict]) -> None:
        super().__init__(Path(os.devnull))  # never opened
        self._snapshot = list(entries)

    def entries(self) -> list[dict]:
        return list(self._snapshot)

    # The file-reading internals are refused too, so a future Journal method that reads the
    # file directly fails loudly instead of quietly reading os.devnull.
    def _missing(self) -> bool:
        raise JournalError("read-only snapshot: no file")

    def _ends_cut_off(self) -> bool:
        raise JournalError("read-only snapshot: no file")

    def _read_entries(self) -> list[dict]:
        raise JournalError("read-only snapshot: no file")

    @contextmanager
    def exclusive(self, timeout_s: float | None = None) -> Iterator[None]:
        raise JournalError("read-only snapshot: the UI never takes the journal lock")
        yield  # pragma: no cover - makes this a generator, like Journal.exclusive

    def append(self, entry: dict) -> None:
        raise JournalError("read-only snapshot: the UI never writes the journal")

    def record_order_event(self, client_order_id: str, status: str, at: datetime,
                           **fields: object) -> OrderState:
        raise JournalError("read-only snapshot: the UI never writes the journal")

    def expire_unsent(self, now: datetime) -> list[str]:
        raise JournalError("read-only snapshot: the UI never writes the journal")
