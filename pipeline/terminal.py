"""Approval in the watched terminal (#156, decision 1).

run_day shows each order ticket in the console Task Scheduler opened, and a human types
`yes` there. No answer before the plan expires means declined.

A `yes` typed before a ticket appears must never approve it. Three layers make sure:
  1. one daemon thread reads the console for the whole run, started when the terminal is
     built (at the start of run_day), so whatever was typed earlier is read and queued long
     before the first ticket, not after it;
  2. every line is stamped with the time it arrived; a question accepts only lines that
     arrived after it was asked, and drops every earlier one;
  3. on Windows, unread console input is flushed just before each question
     (FlushConsoleInputBuffer), so a line typed but not yet read cannot reach it either.
Each question rings the bell, says by when it must be answered, and waits for one line
until the deadline. No line by then, or a closed console, is the empty answer, which
approve() treats as declined. Only the exact answer `yes` approves (order_flow.is_yes).
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime

from pipeline.market import NY

NO_ANSWER = ""


def flush_console_input() -> None:
    """Discard typed-but-unread console input (Windows console only; best effort)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        if handle and handle != -1:
            kernel32.FlushConsoleInputBuffer(handle)
    except Exception:  # noqa: BLE001 - not a console (redirected input): nothing to flush
        pass


class WatchedTerminal:
    """Asks questions on the console with a deadline. `readline`, `write`, `monotonic` and
    `flush` are injectable for tests (defaults: stdin, stdout, time.perf_counter and the
    Windows console flush). The reader thread starts here."""

    def __init__(self, readline: Callable[[], str] | None = None,
                 write: Callable[[str], None] | None = None,
                 monotonic: Callable[[], float] = time.perf_counter,
                 flush: Callable[[], None] | None = None) -> None:
        self._readline = readline or sys.stdin.readline
        self._write = write or (lambda text: (sys.stdout.write(text), sys.stdout.flush()))
        self._monotonic = monotonic
        self._flush = flush if flush is not None else (
            flush_console_input if readline is None else (lambda: None))
        self._lines: queue.Queue[tuple[float, str | None]] = queue.Queue()
        self._closed = False
        self.last_timed_out = False
        self._reader = threading.Thread(target=self._read_forever, daemon=True,
                                        name="watched-terminal")
        self._reader.start()

    def _read_forever(self) -> None:
        while True:
            try:
                line = self._readline()
            except (OSError, ValueError, EOFError):
                line = ""
            stamp = self._monotonic()
            if not line:  # end of input: the console is gone
                self._lines.put((stamp, None))
                return
            self._lines.put((stamp, line.rstrip("\r\n")))

    def ask(self, prompt: str, timeout_s: float, *, deadline_label: str = "") -> str:
        """Show `prompt` and return the first line typed after it, within `timeout_s`
        seconds, or NO_ANSWER (declined) if none comes, the console has closed, or no time
        is left."""
        self.last_timed_out = False
        self._flush()  # typed but unread: gone
        asked_at = self._monotonic()
        when = f" (answer by {deadline_label}; no answer by then = declined)" \
            if deadline_label else ""
        self._write(f"\a\n{prompt}{when}\n")
        end = asked_at + max(timeout_s, 0.0)
        while True:
            if self._closed:
                self._write("console input is closed: declined\n")
                return NO_ANSWER
            left = end - self._monotonic()
            if left <= 0:
                self.last_timed_out = True
                self._write("no answer before the plan expired: declined\n")
                return NO_ANSWER
            try:
                stamp, item = self._lines.get(timeout=left)
            except queue.Empty:
                continue  # the loop re-checks the clock and times out
            if item is None:
                self._closed = True
                continue
            if stamp <= asked_at:
                continue  # typed before the question: never an answer to it
            return item

    def asker(self, deadline: datetime | Callable[[], datetime],
              clock: Callable[[], datetime]) -> Callable[[str], str]:
        """An `ask` for run_decision that must be answered by `deadline` (aware, or a
        function returning it when the ticket is asked). run_day passes the decision's start
        + max_plan_age_s, never later than the plan's expiry."""
        def ask(prompt: str) -> str:
            due = deadline() if callable(deadline) else deadline
            left = (due - clock()).total_seconds()
            return self.ask(prompt, left,
                            deadline_label=due.astimezone(NY).strftime("%H:%M:%S ET"))
        return ask
