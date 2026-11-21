"""Approval in the watched terminal (#156 decision 1): no answer before the plan expires is
declined, and nothing typed before the question can approve it. Offline: a fake console."""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pipeline.order_flow import is_yes
from pipeline.terminal import NO_ANSWER, WatchedTerminal

NY = ZoneInfo("America/New_York")


class Console:
    """A fake stdin: `type(line)` makes a line available; `close()` ends input."""

    def __init__(self) -> None:
        self.q: queue.Queue[str] = queue.Queue()
        self.out: list[str] = []

    def readline(self) -> str:
        return self.q.get()

    def type(self, line: str) -> None:
        self.q.put(line + "\n")

    def close(self) -> None:
        self.q.put("")

    def write(self, text: str) -> None:
        self.out.append(text)

    def text(self) -> str:
        return "".join(self.out)


def terminal(console):
    return WatchedTerminal(readline=console.readline, write=console.write)


def settle():
    time.sleep(0.05)  # let the reader thread move typed lines into its queue


def test_an_answer_within_the_deadline_is_returned():
    c = Console()
    t = terminal(c)
    threading.Timer(0.05, c.type, args=("yes",)).start()
    assert t.ask("Type yes", 2.0) == "yes" and not t.last_timed_out
    assert "\a" in c.text() and "Type yes" in c.text()  # the bell rings for the ticket


def test_blocked_no_answer_before_the_deadline_is_declined():
    c = Console()
    t = terminal(c)
    started = time.monotonic()
    answer = t.ask("Type yes", 0.2, deadline_label="10:05:00 ET")
    assert answer == NO_ANSWER and not is_yes(answer) and t.last_timed_out
    assert time.monotonic() - started < 1.5
    assert "answer by 10:05:00 ET; no answer by then = declined" in c.text()
    assert "no answer before the plan expired: declined" in c.text()


def test_blocked_a_yes_typed_before_the_question_never_approves():
    c = Console()
    t = terminal(c)
    t.ask("warm-up", 0.01)  # starts the reader
    c.type("yes")  # typed early, e.g. at a previous ticket
    settle()
    assert t.ask("Type yes", 0.2) == NO_ANSWER


def test_a_late_answer_to_one_ticket_does_not_approve_the_next():
    c = Console()
    t = terminal(c)
    assert t.ask("ticket 1", 0.1) == NO_ANSWER
    c.type("yes")  # too late for ticket 1
    settle()
    assert t.ask("ticket 2", 0.1) == NO_ANSWER
    threading.Timer(0.05, c.type, args=("yes",)).start()
    assert t.ask("ticket 3", 2.0) == "yes"


def test_blocked_a_closed_console_declines_every_question():
    c = Console()
    t = terminal(c)
    c.close()
    settle()
    assert t.ask("Type yes", 2.0) == NO_ANSWER
    assert t.ask("again", 2.0) == NO_ANSWER
    assert "console input is closed: declined" in c.text()


def test_blocked_no_time_left_declines_at_once():
    c = Console()
    t = terminal(c)
    started = time.monotonic()
    assert t.ask("Type yes", -5) == NO_ANSWER and t.last_timed_out
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("typed", ["y", "YES", "yes please", " no "])
def test_anything_but_yes_is_returned_as_typed_and_does_not_approve(typed):
    c = Console()
    t = terminal(c)
    threading.Timer(0.05, c.type, args=(typed,)).start()
    assert not is_yes(t.ask("Type yes", 2.0))


def test_the_asker_counts_down_to_the_plan_deadline():
    c = Console()
    t = terminal(c)
    now = datetime(2026, 11, 23, 10, 0, tzinfo=NY)
    ask = t.asker(now + timedelta(seconds=0.2), clock=lambda: now)
    assert ask("ticket") == NO_ANSWER
    assert "answer by 10:00:00 ET" in c.text()
    expired = t.asker(now - timedelta(seconds=1), clock=lambda: now)
    assert expired("ticket") == NO_ANSWER  # already past the plan's expiry


def test_the_reader_thread_starts_once():
    c = Console()
    t = terminal(c)
    t.ask("a", 0.01)
    first = t._reader
    t.ask("b", 0.01)
    assert t._reader is first and threading.active_count() >= 1
