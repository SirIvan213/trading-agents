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
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from journal.orders import (
    APPROVED,
    CLOSED,
    EXPIRED,
    OrderError,
    OrderState,
    apply_event,
    replay,
)
from tools.risk_check import DayState

NY = ZoneInfo("America/New_York")
# The reconciler's notes (Phase 5 PR 4, #155). A block stops every new entry (DayState.
# reconcile_blocks) until a human appends its resolution (python -m journal.resolve).
RECONCILE_BLOCK = "reconcile_block"
RECONCILE_BLOCK_RESOLVED = "reconcile_block_resolved"
RECONCILE_ALERT = "reconcile_alert"
BLOCK_KINDS = frozenset({"conflict", "unreadable_order", "orphan", "unprotected",
                         "fill_above_limit"})
DEFAULT_PATH = Path(__file__).resolve().parent / "decisions.jsonl"
__all__ = ["APPROVED", "DEFAULT_PATH", "Journal", "JournalError", "JournalLockTimeout",
           "LOCK_TIMEOUT_S"]


class JournalError(RuntimeError):
    """The journal could not be read or written."""


class JournalLockTimeout(JournalError):
    """The journal's lock could not be taken in time (another writer holds it)."""


CUT_OFF = ("journal's last line was cut off (no newline): a write did not finish. "
           "Run `python -m journal.repair` to move it aside")


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
        # #191: the order replay, kept only while this object holds the lock (nobody else can
        # write then), so one locked operation replays the journal once. Dropped on release.
        self._replayed: dict[str, OrderState] | None = None

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
            raise JournalLockTimeout("journal lock timed out")
        try:
            try:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                fh = self.lock_path.open("a+b")
            except OSError as exc:
                raise JournalError(f"journal lock failed ({type(exc).__name__})") from None
            try:
                while not _try_lock(fh):
                    if time.monotonic() >= deadline:
                        raise JournalLockTimeout("journal lock timed out")
                    time.sleep(0.01)
                self._holder = me
                self._replayed = None
                try:
                    yield
                finally:
                    self._holder = None
                    self._replayed = None
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
        JournalError if it cannot be written or the lock cannot be taken, and (#128) if the
        journal's last line was cut off by a crash: appending would glue the new entry onto
        that fragment, making a bad line mid-file that journal.repair can no longer move."""
        line = json.dumps(entry, sort_keys=True, default=str)
        with self.exclusive():
            self._replayed = None  # the held replay no longer matches the file
            if self._ends_cut_off():
                raise JournalError(CUT_OFF)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                raise JournalError(
                    f"journal write failed ({type(exc).__name__})") from None

    def _missing(self) -> bool:
        """True only when the journal file does not exist (FileNotFoundError). Any other
        error reading its status (a permission error, an unreachable share, a broken link)
        raises JournalError: an unreadable journal never reads as empty (fail closed)."""
        try:
            os.stat(self.path)
        except FileNotFoundError:
            return True
        except (OSError, ValueError) as exc:
            raise JournalError(f"journal unreadable ({type(exc).__name__})") from None
        return False

    def _ends_cut_off(self) -> bool:
        """True when the journal exists, is not empty and does not end with a newline."""
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, 2)
                if fh.tell() == 0:
                    return False
                fh.seek(-1, 2)
                return fh.read(1) != b"\n"
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise JournalError(f"journal unreadable ({type(exc).__name__})") from None

    def entries(self) -> list[dict]:
        """Every entry, oldest first. Raises JournalError for an unreadable or corrupt file.

        Reads under the journal's lock (#128), like every write, so a reader never sees
        another process's half-written last line. A last line WITHOUT its newline is
        therefore not a write in progress but one a crash cut off: it is refused (fail
        closed), and `python -m journal.repair` moves it aside after a human looks.

        A journal that does not exist yet reads as empty WITHOUT taking the lock (#192), so a
        read never creates the journal's directory or its lock file. Only a missing file
        counts: one whose status cannot be read raises JournalError."""
        if self._holder != threading.get_ident() and self._missing():
            return []
        with self.exclusive():
            return self._read_entries()

    def _read_entries(self) -> list[dict]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            raise JournalError(f"journal unreadable ({type(exc).__name__})") from None
        if text and not text.endswith("\n"):
            raise JournalError(CUT_OFF)
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
        lifecycle.

        Inside exclusive() the replay is done once and reused until the lock is released or
        an entry is appended (#191); record_order_event keeps it current. Outside the lock,
        every call replays the file."""
        held = self._holder == threading.get_ident()
        if held and self._replayed is not None:
            return dict(self._replayed)
        try:
            out = replay(self.entries())
        except OrderError as exc:
            raise JournalError(f"journal order history is invalid: {exc}") from None
        if held:
            self._replayed = dict(out)
        return out

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
            known = self.orders()
            current = known.get(client_order_id)
            if current is None:
                raise JournalError(f"no approved order {str(client_order_id)[:40]}")
            try:
                new = apply_event(current, entry)
            except OrderError as exc:
                raise JournalError(f"order event refused: {exc}") from None
            self.append(entry)
            # the replay of the file now: the same orders, this one advanced by `entry` (#191)
            known[client_order_id] = new
            self._replayed = known
        return new

    def add_block(self, kind: str, key: str, reason: str, at: datetime,
                  **fields: object) -> str | None:
        """Append a reconcile_block unless an unresolved one with the same `key` exists.
        Returns the new block id, or None when the same block is already open. Under the
        lock; raises JournalError if it cannot be read or written."""
        if kind not in BLOCK_KINDS:
            raise JournalError(f"unknown block kind {kind!r}")
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise JournalError("block time must be timezone-aware")
        with self.exclusive():
            open_ = unresolved_blocks(self.entries())
            if any(b.get("key") == key for b in open_):
                return None
            used = {e.get("block_id") for e in self.entries()}
            block_id = f"rb-{secrets.token_hex(4)}"
            while block_id in used:  # never reuse an id, even from a rotated journal
                block_id = f"rb-{secrets.token_hex(4)}"
            self.append({"status": RECONCILE_BLOCK, "block_id": block_id, "kind": kind,
                         "key": str(key)[:200], "reason": str(reason)[:400],
                         "at": at.astimezone(NY).isoformat(), **fields})
            return block_id

    def resolve_block(self, block_id: str, note: str, at: datetime) -> dict:
        """Append the resolution of one open block (a human's decision). Returns the block.
        Raises JournalError for an unknown or already resolved block, or an empty note."""
        if not isinstance(note, str) or not note.strip():
            raise JournalError("a resolution needs a note saying what was done")
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise JournalError("resolution time must be timezone-aware")
        with self.exclusive():
            open_ = {b["block_id"]: b for b in unresolved_blocks(self.entries())}
            if block_id not in open_:
                raise JournalError(f"no open block {block_id!r}")
            self.append({"status": RECONCILE_BLOCK_RESOLVED, "block_id": block_id,
                         "note": note.strip()[:400], "at": at.astimezone(NY).isoformat()})
            return open_[block_id]

    def expire_unsent(self, now: datetime) -> list[str]:
        """Journal every approval that was never sent and whose plan has expired at `now`
        as "expired" (filled_qty 0), releasing its position slot and its risk (Ivan's
        decision on #109). Returns the client_order_ids expired. Under the lock; raises
        JournalError if the journal cannot be read or written. The journal is replayed once
        for all of them (#191), instead of once per expired approval. A journal that does not
        exist has nothing to expire."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise JournalError("now must be timezone-aware")
        if self._holder != threading.get_ident() and self._missing():
            return []
        done: list[str] = []
        with self.exclusive():
            for cid, o in self.orders().items():
                if o.status == APPROVED and not o.live_unsent(now):
                    self.record_order_event(cid, EXPIRED, now, filled_qty=0,
                                            reason="plan expired before the order was sent")
                    done.append(cid)
        return done

    def day_state(self, now: datetime, *, exclude: str | None = None) -> DayState:
        """Today's trades, risk, open positions, realized losses and stop-outs (New York
        date of `now`), replayed from the order lifecycle (journal/orders.py, #109):

          - trades_today: every order approved today that was SENT (reached submitting),
            whatever happened to it afterwards; an approval never sent takes no slot;
          - risk_used_today (#188, Ivan's decision): the realized losses of every trade
            CLOSED today, whenever it was opened, plus the sized worst-case max_loss of
            everything open or pending, from any day (a live unsent approval included). A
            confirmed zero fill releases an order; a profit adds nothing;
          - open_positions: orders that may hold a position now, from ANY day (an overnight
            one blocks too). An approval not sent yet counts (open, with its risk) only until
            its plan expires (OrderState.live_unsent), whether or not its "expired" event is
            written yet;
          - realized_loss_today: the losses of trades CLOSED today, whenever approved (an
            overnight position closed this morning counts: the daily loss stop);
          - stopped_out_symbols: symbols whose trade was closed by its stop today.

        `exclude`: the client_order_id of an UNSENT approval to leave out, for the sender's
        re-check at sending (#187). It is ignored once that order has been sent: a sent
        order's slot and risk always count.

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
            if exclude is not None and o.client_order_id == exclude and o.status == APPROVED:
                # the sender re-checks the day without the approval it is about to send; a
                # sent order is never left out (its slot and risk always count)
                continue
            day = o.approved_at.astimezone(NY).date()
            if o.status == APPROVED:
                if o.live_unsent(now):
                    opened += 1
                    risk += o.open_risk
                continue
            if o.open:
                opened += 1
                risk += o.open_risk
            if o.status == CLOSED and o.last_at is not None and \
                    o.last_at.astimezone(NY).date() == today:
                lost += o.realized_loss
                risk += o.realized_loss
                if o.stopped_out:
                    stopped.add(o.symbol)
            if day == today:
                trades += 1 if o.sent else 0
        blocks = tuple(f"{b.get('block_id')} {b.get('kind')}: {b.get('reason')}"
                       for b in unresolved_blocks(self.entries()))
        return DayState(trades_today=trades, risk_used_today=risk, open_positions=opened,
                        reconcile_blocks=blocks,
                        realized_loss_today=lost, stopped_out_symbols=frozenset(stopped))


def unresolved_blocks(entries: list[dict]) -> list[dict]:
    """The reconcile_block entries without a later resolution, oldest first. A block entry
    without a usable block_id still counts (it can only be cleared by repairing the journal),
    so a malformed block never reads as resolved."""
    resolved = {e.get("block_id") for e in entries
                if e.get("status") == RECONCILE_BLOCK_RESOLVED}
    out = []
    for e in entries:
        if e.get("status") != RECONCILE_BLOCK:
            continue
        bid = e.get("block_id")
        if isinstance(bid, str) and bid in resolved:
            continue
        out.append(e if isinstance(bid, str) else {**e, "block_id": "(no id)"})
    return out
