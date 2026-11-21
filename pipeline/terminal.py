"""Approval in the watched terminal (#156, decision 1).

run_day shows each order ticket in the console Task Scheduler opened, and a human types
`yes` there. No answer before the plan expires means declined.

One daemon thread reads the console line by line into a queue for the whole run (a blocked
console read cannot be cancelled, so it is never started twice). Each question:
  - throws away every line typed BEFORE the question, so a `yes` typed early (or left over
    from an earlier ticket) can never approve this one;
  - rings the terminal bell and says by when it must be answered;
  - waits for one line until the deadline. No line by then (or the console closed) is the
    empty answer, which approve() treats as declined.
Only the exact answer `yes` approves (pipeline.order_flow.is_yes decides that).
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime

from pipeline.market import NY

NO_ANSWER = ""


class WatchedTerminal:
    """Asks questions on the console with a deadline. `readline` and `write` are injectable
    for tests (default: the process's stdin and stdout)."""

    def __init__(self, readline: Callable[[], str] | None = None,
                 write: Callable[[str], None] | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._readline = readline or sys.stdin.readline
        self._write = write or (lambda text: (sys.stdout.write(text), sys.stdout.flush()))
        self._monotonic = monotonic
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self.last_timed_out = False

    def _read_forever(self) -> None:
        while True:
            try:
                line = self._readline()
            except (OSError, ValueError, EOFError):
                line = ""
            if not line:  # end of input: the console is gone
                self._lines.put(None)
                return
            self._lines.put(line.rstrip("\r\n"))

    def _ensure_reader(self) -> None:
        with self._start_lock:
            if self._reader is None:
                self._reader = threading.Thread(target=self._read_forever, daemon=True,
                                                name="watched-terminal")
                self._reader.start()

    def _discard_typed_ahead(self) -> bool:
        """Drop every line typed before the question. True if the console has closed."""
        closed = False
        while True:
            try:
                item = self._lines.get_nowait()
            except queue.Empty:
                return closed
            if item is None:
                closed = True

    def ask(self, prompt: str, timeout_s: float, *, deadline_label: str = "") -> str:
        """Show `prompt` and return the first line typed within `timeout_s` seconds, or
        NO_ANSWER (declined) if none comes, the console has closed, or no time is left."""
        self._ensure_reader()
        self.last_timed_out = False
        closed = self._discard_typed_ahead()
        if closed:
            self._lines.put(None)  # keep the closed state for later questions
        when = f" (answer by {deadline_label}; no answer by then = declined)" \
            if deadline_label else ""
        self._write(f"\a\n{prompt}{when}\n")
        if closed:
            self._write("console input is closed: declined\n")
            return NO_ANSWER
        end = self._monotonic() + max(timeout_s, 0.0)
        while True:
            left = end - self._monotonic()
            if left <= 0:
                self.last_timed_out = True
                self._write("no answer before the plan expired: declined\n")
                return NO_ANSWER
            try:
                item = self._lines.get(timeout=left)
            except queue.Empty:
                continue  # the loop re-checks the clock and times out
            if item is None:
                self._lines.put(None)
                self._write("console input is closed: declined\n")
                return NO_ANSWER
            return item

    def asker(self, deadline: datetime, clock: Callable[[], datetime]) -> Callable[[str], str]:
        """An `ask` for run_decision that must be answered by `deadline` (aware). run_day
        passes the decision's start + max_plan_age_s, never later than the plan's expiry."""
        label = deadline.astimezone(NY).strftime("%H:%M:%S ET")

        def ask(prompt: str) -> str:
            left = (deadline - clock()).total_seconds()
            return self.ask(prompt, left, deadline_label=label)
        return ask
