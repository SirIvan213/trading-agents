"""Journal (issue #65): append-only JSONL, and the fail-closed DayState read back from it."""
import json
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from journal.log import APPROVED, DEFAULT_PATH, Journal, JournalError
from tests.order_helpers import (
    NY,
    T0,
    approved_entry,
    closed_trade,
    journal_with,
    raw_lines,
    sent_order,
)


@pytest.fixture
def j(tmp_path):
    return Journal(tmp_path / "sub" / "decisions.jsonl")


def test_default_path_is_the_git_ignored_decisions_file():
    assert DEFAULT_PATH.name == "decisions.jsonl" and DEFAULT_PATH.parent.name == "journal"
    assert Journal().path == DEFAULT_PATH


def test_append_creates_parents_and_entries_roundtrip(j):
    assert j.entries() == []  # a missing file is an empty journal
    j.append({"status": "declined", "n": 1})
    j.append({"status": "pass", "n": 2})
    assert [e["n"] for e in j.entries()] == [1, 2]
    assert len(raw_lines(j)) == 2


def test_append_serialises_unknown_types_with_str(j):
    j.append({"status": "x", "when": T0, "amount": D("1.50")})
    (e,) = j.entries()
    assert e["amount"] == "1.50" and e["when"].startswith("2026-10-19")


def test_append_failure_is_a_journal_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    # The lock file sits next to the journal, so its creation fails first (#107).
    with pytest.raises(JournalError, match="journal (write|lock) failed"):
        Journal(blocker / "decisions.jsonl").append({"status": "x"})


def test_blank_lines_are_ignored(j):
    j.append({"status": "x"})
    with j.path.open("a", encoding="utf-8") as fh:
        fh.write("\n   \n")
    assert len(j.entries()) == 1


@pytest.mark.parametrize("junk", ["not json", "[1, 2]", '"text"', "123", "{broken"])
def test_corrupt_line_raises_instead_of_being_skipped(j, junk):
    j.append({"status": "x"})
    with j.path.open("a", encoding="utf-8") as fh:
        fh.write(junk + "\n")
    with pytest.raises(JournalError):
        j.entries()
    with pytest.raises(JournalError):
        j.day_state(T0)


def test_non_utf8_file_raises(j):
    j.path.parent.mkdir(parents=True)
    j.path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(JournalError, match="unreadable"):
        j.entries()


# ---------------- day_state ----------------

def test_empty_journal_is_an_empty_day(j):
    s = j.day_state(T0)
    assert (s.trades_today, s.risk_used_today) == (0, D("0"))


def test_counts_only_todays_approvals_and_sums_max_loss(j):
    yesterday = T0 - timedelta(days=1)
    tomorrow = T0 + timedelta(days=1)
    for e in [
        *sent_order(T0 - timedelta(hours=1), "46.88"),
        *sent_order(T0, "10.00"),
        *closed_trade(yesterday, "99.00"),  # another day, closed: not counted
        *closed_trade(tomorrow, "99.00"),
        {"status": "declined", "approved_at": T0.isoformat(), "max_loss": "77"},
        {"status": "refused_after_approval", "max_loss": "77"},
        {"status": "changed_after_approval", "max_loss": "77"},
        {"status": "pass"},
    ]:
        j.append(e)
    s = j.day_state(T0)
    assert s.trades_today == 2 and s.risk_used_today == D("56.88")


def test_day_boundary_uses_new_york_date_not_utc(j):
    late = datetime(2026, 10, 19, 23, 30, tzinfo=NY)  # 03:30Z on the 20th
    for e in sent_order(late, "5.00"):
        j.append(e)
    same_ny_day = datetime(2026, 10, 19, 9, 0, tzinfo=NY)
    next_ny_day = datetime(2026, 10, 20, 0, 5, tzinfo=NY)
    assert j.day_state(same_ny_day).trades_today == 1
    assert j.day_state(next_ny_day).trades_today == 0
    same_instant_utc = datetime.fromisoformat("2026-10-20T03:30:00+00:00")
    assert j.day_state(same_instant_utc).trades_today == 1


def test_naive_now_is_refused(j):
    with pytest.raises(JournalError, match="timezone-aware"):
        j.day_state(datetime(2026, 10, 19, 10, 15))


def _broken(**over):
    """A full approved entry for T0's day with one field broken (or removed: value None)."""
    e = approved_entry(datetime(2026, 10, 19, 10, 15, tzinfo=NY), "1.00")
    for k, v in over.items():
        if v is _DROP:
            e.pop(k, None)
        else:
            e[k] = v
    return e


_DROP = object()
MALFORMED = [
    pytest.param(_broken(approved_at=_DROP), id="no-approved_at"),
    pytest.param(_broken(max_loss=_DROP), id="no-max_loss"),
    pytest.param(_broken(approved_at="2026-10-19T10:15:00"), id="naive-approved_at"),
    pytest.param(_broken(approved_at="yesterday"), id="bad-approved_at"),
    pytest.param(_broken(max_loss="abc"), id="max_loss-text"),
    pytest.param(_broken(max_loss=None), id="max_loss-null"),
    pytest.param(_broken(max_loss="-1.00"), id="max_loss-negative"),
    pytest.param(_broken(max_loss="NaN"), id="max_loss-nan"),
    pytest.param(_broken(max_loss="Infinity"), id="max_loss-inf"),
    pytest.param(_broken(approved_at=None), id="approved_at-null"),
    pytest.param(_broken(order=_DROP), id="no-order"),
    pytest.param(_broken(run_id=_DROP), id="no-run_id"),
]


@pytest.mark.parametrize("entry", MALFORMED)
def test_malformed_approved_entry_fails_closed(j, entry):
    j.append(approved_entry(T0, "1.00"))
    j.append(entry)
    with pytest.raises(JournalError, match="order history is invalid"):
        j.day_state(T0)


def test_malformed_approved_entry_from_another_day_still_fails_closed(j):
    j.append({"status": APPROVED, "approved_at": "garbage", "max_loss": "1.00"})
    with pytest.raises(JournalError):
        j.day_state(T0 + timedelta(days=3))


def test_zero_max_loss_is_allowed_and_counts_as_a_trade(j):
    for e in sent_order(T0, "0"):
        j.append(e)
    s = j.day_state(T0)
    assert s.trades_today == 1 and s.risk_used_today == D("0")


def test_state_is_re_read_from_disk_every_call(tmp_path):
    path = tmp_path / "d.jsonl"
    a, b = Journal(path), Journal(path)  # two handles on one file
    assert a.day_state(T0).trades_today == 0
    for e in sent_order(T0):
        b.append(e)
    assert a.day_state(T0).trades_today == 1


def test_entries_file_is_one_json_object_per_line(tmp_path):
    j = journal_with(tmp_path / "x.jsonl", [{"b": 1, "a": 2}, {"c": 3}])
    lines = j.path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [{"a": 2, "b": 1}, {"c": 3}]
    assert lines[0] == '{"a": 2, "b": 1}'  # sorted keys
