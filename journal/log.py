"""Decision journal: one JSON object per line in journal/decisions.jsonl (git-ignored).

An approved order is written here with status "approved_not_sent" and its deterministic
client_order_id; from Phase 5 everything that happens to it afterwards (submitting,
submitted, fills, closed ...) is appended as an order event, and its state is derived by
replaying them (journal/orders.py, #109). Today's DayState for risk_check is read back from
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
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from journal.orders import APPROVED, OrderError, OrderState, apply_event, replay
from tools.risk_check import DayState

NY = ZoneInfo("America/New_York")
DEFAULT_PATH = Path(__file__).resolve().parent / "decisions.jsonl"
__all__ = ["APPROVED", "DEFAULT_PATH", "Journal", "JournalError", "LOCK_TIMEOUT_S"]


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

    def orders(self) -> dict[str, OrderState]:
        """Every order in the journal, by client_order_id, as its entries replay
        (journal/orders.py). Raises JournalError for a corrupt journal or an impossible
        lifecycle."""
        try:
            return replay(self.entries())
        except OrderError as exc:
            raise JournalError(f"journal order history is invalid: {exc}") from None

    def record_order_event(self, client_order_id: str, status: str, at: datetime,
                           **fields: object) -> OrderState:
        """Append one lifecycle event for an approved order and return its new state. Under
        the lock, the journal is replayed and the event checked first (a known order, a
        possible step, valid fields), so an impossible event is never written. Raises
        JournalError otherwise, or if the journal cannot be read, locked or written."""
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise JournalError("order event time must be timezone-aware")
        try:
            stamp = at.astimezone(NY).isoformat()
        except (OverflowError, ValueError):
            raise JournalError("order event time is outside the supported date range") from None
        entry = {**fields, "status": status, "client_order_id": client_order_id, "at": stamp}
        with self.exclusive():
            current = self.orders().get(client_order_id)
            if current is None:
                raise JournalError(f"no approved order {str(client_order_id)[:40]}")
            try:
                new = apply_event(current, entry)
            except OrderError as exc:
                raise JournalError(f"order event refused: {exc}") from None
            self.append(entry)
        return new

    def day_state(self, now: datetime) -> DayState:
        """Today's trades, risk, open positions, realized losses and stop-outs (New York
        date of `now`), replayed from the order lifecycle (journal/orders.py, #109):

          - trades_today: every order approved today, whatever happened to it (a submitted
            order uses a trade slot);
          - risk_used_today: each of today's orders at its sized worst-case max_loss (or its
            realized loss, if larger), released only when the broker confirmed a zero fill;
          - open_positions: orders that may hold a position now, from ANY day (an overnight
            one blocks too). An order approved but never sent counts only on its approval
            day: it can never be sent later, its plan having expired;
          - realized_loss_today: the losses of today's closed trades (the daily loss stop);
          - stopped_out_symbols: symbols whose trade today was closed by its stop.

        Fail-closed: raises JournalError for a corrupt journal, a malformed approved entry or
        an impossible order history, so no order passes on totals that cannot be trusted.
        """
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise JournalError("now must be timezone-aware")
        try:
            today = now.astimezone(NY).date()
        except (OverflowError, ValueError):  # #127: an aware clock at the date-range limits
            raise JournalError("now is outside the supported date range") from None
        trades, risk, lost, opened = 0, Decimal("0"), Decimal("0"), 0
        stopped: set[str] = set()
        for o in self.orders().values():
            day = o.approved_at.astimezone(NY).date()
            if o.open and (o.status != APPROVED or day == today):
                opened += 1
            if day != today:
                continue
            trades += 1
            risk += o.committed_risk
            lost += o.realized_loss
            if o.stopped_out:
                stopped.add(o.symbol)
        return DayState(trades_today=trades, risk_used_today=risk, open_positions=opened,
                        realized_loss_today=lost, stopped_out_symbols=frozenset(stopped))
