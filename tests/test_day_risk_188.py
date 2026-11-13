"""#188 (Ivan's decision): today's risk used = the realized losses of every position closed
today, whenever it was opened, plus the worst-case risk of everything open or pending.
Offline."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

import pipeline.order_flow as of
from pipeline.order_flow import propose
from tests.order_helpers import (
    T0,
    approved_entry,
    closed_trade,
    events_clear,
    journal_with,
    quotes_at,
    sealed_ledger,
    sent_order,
)
from tools.config import CONFIG_DIR, load_limits

NY = ZoneInfo("America/New_York")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")  # daily_loss_limit 150.00
FRIDAY = datetime(2026, 10, 16, 10, 0, tzinfo=NY)  # T0 is Monday 2026-10-19 10:15:30


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)


def friday_position_closed_monday(pnl: str, *, symbol: str = "GAPA") -> list[dict]:
    """Approved and filled on Friday, closed by its stop on Monday at 09:50."""
    a = approved_entry(FRIDAY, "49.84", symbol=symbol)
    cid = a["client_order_id"]

    def ev(status, when, **fields):
        return {"status": status, "client_order_id": cid, "at": when.isoformat(), **fields}

    return [a, ev("submitting", FRIDAY), ev("submitted", FRIDAY),
            ev("filled", FRIDAY, filled_qty=8, avg_price="100.00"),
            ev("closed", T0.replace(hour=9, minute=50, second=0), closed_by="stop",
               realized_pnl=pnl)]


def propose_tsla(tmp_path, entries):
    j = journal_with(tmp_path / "j.jsonl", entries)
    return j, propose(sealed_ledger(), "plan-1", journal=j, clock=lambda: T0,
                      quotes=quotes_at(T0), events=events_clear())


def test_ivan_friday_position_closed_monday_at_minus_120_leaves_30_so_49_84_is_blocked(
        tmp_path):
    j, p = propose_tsla(tmp_path, friday_position_closed_monday("-120.00"))
    state = j.day_state(T0)
    assert state.risk_used_today == D("120.00")  # 150 - 120 = 30 left
    assert state.realized_loss_today == D("120.00") and state.open_positions == 0
    assert p.ok is False and p.stage == "risk"
    assert "today's risk would reach 169.84, over the daily limit 150.00" in p.reason
    assert "daily loss stop" not in p.reason  # 120 < 150: the stop itself has not fired


def test_a_smaller_overnight_loss_leaves_room_for_the_entry(tmp_path):
    _, p = propose_tsla(tmp_path, friday_position_closed_monday("-100.16"))
    assert p.ok, p.reason  # 100.16 + 49.84 = 150.00, exactly the limit


def test_open_risk_and_realized_losses_add_up(tmp_path):
    """A Friday position still open (49.84 worst case) plus a loss realized today (30)."""
    still_open = approved_entry(FRIDAY, "49.84", symbol="OPEN")
    cid = still_open["client_order_id"]
    events = [{"status": s, "client_order_id": cid, "at": FRIDAY.isoformat(), **f}
              for s, f in [("submitting", {}), ("submitted", {}),
                           ("filled", {"filled_qty": 8, "avg_price": "100"})]]
    entries = [still_open, *events,
               *closed_trade(T0 - timedelta(hours=1), "40", pnl="-30.00", closed_by="stop",
                             symbol="LOSS")]
    s = journal_with(tmp_path / "j.jsonl", entries).day_state(T0)
    assert s.risk_used_today == D("79.84") and s.open_positions == 1


def test_a_closed_winner_uses_no_budget(tmp_path):
    entries = closed_trade(T0 - timedelta(hours=1), "49.84", pnl="+80.00", symbol="WIN")
    j, p = propose_tsla(tmp_path, entries)
    assert j.day_state(T0).risk_used_today == 0
    assert p.ok, p.reason


def test_a_live_unsent_approval_and_a_pending_send_count_at_their_worst_case(tmp_path):
    live = approved_entry(T0 - timedelta(seconds=30), "20.00", symbol="LIVE")
    pending = sent_order(T0 - timedelta(minutes=2), "15.00", symbol="PEND")
    s = journal_with(tmp_path / "j.jsonl", [live, *pending]).day_state(T0)
    assert s.risk_used_today == D("35.00") and s.open_positions == 2


def test_a_loss_closed_yesterday_does_not_count_today(tmp_path):
    entries = closed_trade(T0 - timedelta(days=1), "49.84", pnl="-140.00", closed_by="stop")
    assert journal_with(tmp_path / "j.jsonl", entries).day_state(T0).risk_used_today == 0


def test_a_live_unsent_approval_from_late_yesterday_counts_after_midnight(tmp_path):
    """Approved 23:58, its plan expires 00:03: at 00:01 it still holds its worst case."""
    late = datetime(2026, 10, 18, 23, 58, tzinfo=NY)
    a = approved_entry(late, "49.84")  # expires 00:03
    j = journal_with(tmp_path / "j.jsonl", [a])
    s = j.day_state(datetime(2026, 10, 19, 0, 1, tzinfo=NY))
    assert s.risk_used_today == D("49.84") and s.open_positions == 1 and s.trades_today == 0
    assert j.day_state(datetime(2026, 10, 19, 0, 3, tzinfo=NY)).risk_used_today == 0
