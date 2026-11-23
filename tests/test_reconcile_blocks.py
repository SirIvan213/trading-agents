"""The reconciler's blocks (Phase 5 PR 4, #155, #189): journal entries that stop every new
entry (DayState.reconcile_blocks, risk_check) until a human resolves each with
python -m journal.resolve."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import journal.resolve as resolve
from journal.log import Journal, JournalError, unresolved_blocks
from tests.order_helpers import sized_order
from tools.config import CONFIG_DIR, load_limits
from tools.risk_check import DayState, check_order

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 24, 10, 15, tzinfo=NY)
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")


def test_a_block_stops_every_new_entry_until_resolved(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    assert j.day_state(NOW).reconcile_blocks == ()
    bid = j.add_block("unprotected", "unprotected:ta-1", "SPY has no working stop", NOW,
                      symbol="SPY")
    state = j.day_state(NOW)
    assert state.reconcile_blocks == (f"{bid} unprotected: SPY has no working stop",)
    from tests.order_helpers import clear_gate
    order = sized_order()
    v = check_order(order, LIMITS, state, NOW, gate=clear_gate(order["symbol"], NOW))
    assert v["verdict"] == "BLOCK"
    assert any("the reconciler blocked new entries (1 unresolved" in r
               and "python -m journal.resolve" in r for r in v["reasons"])
    j.resolve_block(bid, "checked the dashboard: flat", NOW)
    assert j.day_state(NOW).reconcile_blocks == ()


def test_the_same_open_block_is_written_once_and_again_after_it_is_resolved(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    first = j.add_block("orphan", "orphan-position:SPY", "a position not in the journal", NOW)
    assert j.add_block("orphan", "orphan-position:SPY", "again", NOW) is None
    j.resolve_block(first, "closed by hand", NOW)
    second = j.add_block("orphan", "orphan-position:SPY", "it is back", NOW)
    assert second and second != first
    assert [b["block_id"] for b in unresolved_blocks(j.entries())] == [second]


@pytest.mark.parametrize("call", [
    lambda j: j.add_block("made_up", "k", "r", NOW),
    lambda j: j.add_block("orphan", "k", "r", datetime(2026, 11, 24, 10, 0)),
    lambda j: j.resolve_block("rb-999999", "note", NOW),
    lambda j: j.resolve_block("rb-000001", "  ", NOW),
])
def test_blocked_bad_blocks_and_resolutions(tmp_path, call):
    j = Journal(tmp_path / "j.jsonl")
    j.add_block("orphan", "k", "r", NOW)
    with pytest.raises(JournalError):
        call(j)


def test_a_block_without_an_id_never_reads_as_resolved(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append({"status": "reconcile_block", "kind": "conflict", "reason": "odd"})
    j.append({"status": "reconcile_block_resolved", "block_id": None, "note": "x"})
    assert len(j.day_state(NOW).reconcile_blocks) == 1


@pytest.mark.parametrize("bad", [["a"], ("a", 1), "a", None])
def test_blocked_a_day_state_with_malformed_blocks(bad):
    order = sized_order()
    v = check_order(order, LIMITS, DayState(reconcile_blocks=bad), NOW)
    assert v["verdict"] == "BLOCK"
    assert "day state reconcile_blocks must be a tuple of texts" in v["reasons"]


def test_resolve_lists_and_resolves_with_a_note(tmp_path, capsys):
    j = Journal(tmp_path / "j.jsonl")
    assert resolve.main([], journal=j, now=NOW) == 0
    assert "no open blocks" in capsys.readouterr().out
    bid = j.add_block("fill_above_limit", "fill-above-limit:ta-1", "filled at 101 > 100", NOW)
    assert resolve.main([], journal=j, now=NOW) == 0
    out = capsys.readouterr().out
    assert "1 open block(s)" in out and bid in out and "fill_above_limit" in out
    assert resolve.main([bid], journal=j, now=NOW) == 2  # a note is required
    assert "--note is required" in capsys.readouterr().err
    assert resolve.main([bid, "--note", "checked: a broker rounding"], journal=j,
                        now=NOW) == 0
    assert "no open blocks left" in capsys.readouterr().out
    assert j.day_state(NOW).reconcile_blocks == ()
    assert resolve.main([bid, "--note", "again"], journal=j, now=NOW) == 1  # already done
