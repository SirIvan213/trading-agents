"""Approval in the watched terminal (#156 decision 1): no answer before the plan expires is
declined, and nothing typed before the question can approve it. Offline: a fake console.
Tests wait on conditions, never on fixed sleeps."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pipeline.order_flow import is_yes
from pipeline.terminal import NO_ANSWER, WatchedTerminal

NY = ZoneInfo("America/New_York")


class Console:
    """A fake console. Typed lines wait in `buffer` until the reader takes them, like a real
    console's input buffer; `flush()` empties it (FlushConsoleInputBuffer). `gate` can hold
    the reader back, to model a line typed but not yet read when a ticket appears."""

    def __init__(self, *, held: bool = False) -> None:
        self.buffer: list[str] = []
        self.cond = threading.Condition()
        self.gate = threading.Event()
        if not held:
            self.gate.set()
        self.closed = False
        self.out: list[str] = []
        self.reads = 0

    def readline(self) -> str:
        self.gate.wait()
        with self.cond:
            self.cond.wait_for(lambda: self.buffer or self.closed)
            self.reads += 1
            if self.buffer:
                return self.buffer.pop(0) + "\n"
            return ""

    def type(self, line: str) -> None:
        with self.cond:
            self.buffer.append(line)
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def flush(self) -> None:
        with self.cond:
            self.buffer.clear()

    def write(self, text: str) -> None:
        self.out.append(text)

    def text(self) -> str:
        return "".join(self.out)


def terminal(console):
    return WatchedTerminal(readline=console.readline, write=console.write,
                           flush=console.flush)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met")
        time.sleep(0.005)


def type_soon(console, line, after=0.05):
    threading.Timer(after, console.type, args=(line,)).start()


def test_an_answer_within_the_deadline_is_returned():
    c = Console()
    t = terminal(c)
    type_soon(c, "yes")
    assert t.ask("Type yes", 5.0) == "yes" and not t.last_timed_out
    assert "\a" in c.text() and "Type yes" in c.text()  # the bell rings for the ticket


def test_blocked_no_answer_before_the_deadline_is_declined():
    c = Console()
    t = terminal(c)
    started = time.monotonic()
    answer = t.ask("Type yes", 0.2, deadline_label="10:05:00 ET")
    assert answer == NO_ANSWER and not is_yes(answer) and t.last_timed_out
    assert time.monotonic() - started < 3
    assert "answer by 10:05:00 ET; no answer by then = declined" in c.text()
    assert "no answer before the plan expired: declined" in c.text()


def test_blocked_a_yes_typed_before_the_first_ticket_never_approves_it():
    """The review's B1: `yes` typed in the console before the run's FIRST ticket (no
    warm-up question). The reader starts with the terminal, reads it at once, and the
    ticket drops it as typed before the question."""
    c = Console()
    c.type("yes")  # typed before run_day even built the terminal
    t = terminal(c)
    wait_for(lambda: t._lines.qsize() >= 1)  # read and stamped, long before the ticket
    assert t.ask("first ticket", 0.3) == NO_ANSWER


def test_blocked_a_yes_typed_but_not_yet_read_is_flushed():
    """A line still in the console's input buffer when the ticket appears (the reader had
    not taken it yet) is flushed before the question, so it cannot answer it."""
    c = Console(held=True)
    t = terminal(c)
    c.type("yes")  # typed, not read: the reader is held back
    result = {}

    def ask():
        result["answer"] = t.ask("ticket", 0.5)

    th = threading.Thread(target=ask)
    th.start()
    wait_for(lambda: any("ticket" in x for x in c.out))  # the question was asked
    c.gate.set()  # now the reader may read: the buffer was already flushed
    th.join(5)
    assert result["answer"] == NO_ANSWER and c.buffer == []


def test_a_late_answer_to_one_ticket_does_not_approve_the_next():
    c = Console()
    t = terminal(c)
    assert t.ask("ticket 1", 0.1) == NO_ANSWER
    c.type("yes")  # too late for ticket 1
    wait_for(lambda: c.reads >= 1)
    assert t.ask("ticket 2", 0.2) == NO_ANSWER
    type_soon(c, "yes")
    assert t.ask("ticket 3", 5.0) == "yes"


def test_blocked_a_closed_console_declines_every_question():
    c = Console()
    t = terminal(c)
    c.close()
    assert t.ask("Type yes", 5.0) == NO_ANSWER
    assert t.ask("again", 5.0) == NO_ANSWER
    assert "console input is closed: declined" in c.text()


def test_blocked_no_time_left_declines_at_once():
    c = Console()
    t = terminal(c)
    started = time.monotonic()
    assert t.ask("Type yes", -5) == NO_ANSWER and t.last_timed_out
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("typed", ["y", "YES", "yes please", " no "])
def test_anything_but_yes_is_returned_as_typed_and_does_not_approve(typed):
    c = Console()
    t = terminal(c)
    type_soon(c, typed)
    assert not is_yes(t.ask("Type yes", 5.0))


def test_the_asker_counts_down_to_the_plan_deadline():
    c = Console()
    t = terminal(c)
    now = datetime(2026, 11, 23, 10, 0, tzinfo=NY)
    ask = t.asker(now + timedelta(seconds=0.2), clock=lambda: now)
    assert ask("ticket") == NO_ANSWER
    assert "answer by 10:00:00 ET" in c.text()
    expired = t.asker(now - timedelta(seconds=1), clock=lambda: now)
    assert expired("ticket") == NO_ANSWER  # already past the plan's expiry


def test_the_asker_reads_a_deadline_set_later():
    c = Console()
    t = terminal(c)
    now = datetime(2026, 11, 23, 10, 0, tzinfo=NY)
    box = {"due": now - timedelta(seconds=1)}
    ask = t.asker(lambda: box["due"], clock=lambda: now)
    box["due"] = now + timedelta(seconds=0.2)  # set when the decision started counting
    assert ask("ticket") == NO_ANSWER and "answer by 10:00:00 ET" in c.text()


def test_the_reader_starts_with_the_terminal():
    c = Console()
    t = terminal(c)
    assert t._reader.is_alive() and t._reader.daemon
