"""Phase 5 PR 1: the journal's order lifecycle and day totals (#109), and the journal hardening
it needs now that a reconciler will write alongside the approval path: an out-of-range clock
(#127), reads under the lock (#128), a lock timeout after "yes" (#129). Offline."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import pipeline.order_flow as of
from journal.log import APPROVED, Journal, JournalError
from pipeline.order_flow import propose
from tests.order_helpers import (
    T0,
    approved_entry,
    events_clear,
    journal_with,
    quotes_at,
    sealed_ledger,
)
from tools.config import CONFIG_DIR, load_limits

NY = ZoneInfo("America/New_York")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")

# Aware clocks too close to the date-range limits to convert to New York time (#126, #127)
EDGE_CLOCKS = [
    datetime(1, 1, 1, tzinfo=UTC),
    datetime(1, 1, 1, 3, tzinfo=timezone(timedelta(hours=14))),
    datetime(9999, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-12))),
]
EDGE_IDS = ["min-utc", "min-east", "max-west"]


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)
    return LIMITS


# ======================= #127: an out-of-range clock =======================

@pytest.mark.parametrize("edge", EDGE_CLOCKS, ids=EDGE_IDS)
def test_127_propose_refuses_an_out_of_range_clock_at_stage_plan(limits, tmp_path, edge):
    p = propose(sealed_ledger(), "plan-1", journal=Journal(tmp_path / "j.jsonl"),
                clock=lambda: edge, quotes=quotes_at(T0), events=events_clear())
    assert p.ok is False and p.stage == "plan"
    assert p.reason == "clock is outside the supported date range"
    assert p.checked_at == str(edge)  # a refusal still carries a timestamp


@pytest.mark.parametrize("bad", [None, "2026-10-19T10:15:00-04:00", 1760883300],
                         ids=["none", "string", "number"])
def test_127_propose_refuses_a_clock_that_is_not_a_datetime(limits, tmp_path, bad):
    p = propose(sealed_ledger(), "plan-1", journal=Journal(tmp_path / "j.jsonl"),
                clock=lambda: bad, quotes=quotes_at(T0), events=events_clear())
    assert p.ok is False and p.stage == "plan"
    assert p.reason == "clock must be timezone-aware"


def test_127_a_normal_clock_still_proposes(limits, tmp_path):
    p = propose(sealed_ledger(), "plan-1", journal=Journal(tmp_path / "j.jsonl"),
                clock=lambda: T0, quotes=quotes_at(T0), events=events_clear())
    assert p.ok, p.reason
    assert p.checked_at == T0.astimezone(NY).isoformat()


@pytest.mark.parametrize("edge", EDGE_CLOCKS, ids=EDGE_IDS)
def test_127_day_state_raises_journal_error_for_an_out_of_range_clock(tmp_path, edge):
    j = journal_with(tmp_path / "j.jsonl", [approved_entry(T0)])
    with pytest.raises(JournalError, match="outside the supported date range"):
        j.day_state(edge)


def test_127_day_state_rejects_an_approved_entry_stamped_at_the_date_range_edge(tmp_path):
    entry = approved_entry(T0)
    entry["approved_at"] = "9999-12-31T23:59:00-12:00"
    j = journal_with(tmp_path / "j.jsonl", [entry])
    with pytest.raises(JournalError, match="malformed approved entry"):
        j.day_state(T0)


@pytest.mark.parametrize("bad", [None, "2026-10-19", datetime(2026, 10, 19, 10, 15)],
                         ids=["none", "string", "naive"])
def test_127_day_state_rejects_a_clock_that_is_not_aware(tmp_path, bad):
    with pytest.raises(JournalError, match="timezone-aware"):
        Journal(tmp_path / "j.jsonl").day_state(bad)


def test_127_day_state_counts_today_as_before(tmp_path):
    j = journal_with(tmp_path / "j.jsonl",
                     [approved_entry(T0, "10.00"), approved_entry(T0 - timedelta(days=1))])
    state = j.day_state(T0)
    assert state.trades_today == 1 and str(state.risk_used_today) == "10.00"


def test_127_journal_lines_are_plain_json(tmp_path):
    """Sanity: the helper writes what the journal reads."""
    j = journal_with(tmp_path / "j.jsonl", [approved_entry(T0)])
    (line,) = j.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["status"] == APPROVED
