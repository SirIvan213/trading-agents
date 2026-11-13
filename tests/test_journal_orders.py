"""The order lifecycle in the journal (Phase 5 PR 1, #109): deterministic client_order_id,
the allowed steps, field checks, replay and Journal.record_order_event. Offline."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from journal.log import Journal, JournalError
from journal.orders import (
    APPROVED,
    CANCELLED,
    CLOSED,
    EXPIRED,
    FILLED,
    NEXT,
    ORDER_EVENTS,
    PARTIALLY_FILLED,
    REJECTED,
    SEND_UNKNOWN,
    SUBMITTED,
    SUBMITTING,
    OrderError,
    apply_event,
    client_order_id,
    from_approved,
    replay,
)

NY = ZoneInfo("America/New_York")
T = datetime(2026, 11, 16, 10, 0, tzinfo=NY)  # a Monday


def approved(run_id="run-1", plan_id="plan-1", *, qty=8, max_loss="49.84", symbol="TSLA",
             at=T, with_id=True, **over):
    e = {"status": APPROVED, "run_id": run_id, "plan_id": plan_id, "symbol": symbol,
         "order": {"symbol": symbol, "side": "buy", "qty": qty}, "max_loss": max_loss,
         "approved_at": at.isoformat()}
    if with_id:
        e["client_order_id"] = client_order_id(run_id, plan_id)
    e.update(over)
    return e


CID = client_order_id("run-1", "plan-1")


def ev(status, at=T, cid=CID, **fields):
    return {"status": status, "client_order_id": cid, "at": at.isoformat(), **fields}


def state_after(*events, **approved_over):
    return replay([approved(**approved_over), *events])[CID]


# Minimal valid fields for each event, given the state before it
def fields_for(status, before):
    if status == PARTIALLY_FILLED:
        return {"filled_qty": max(before.filled_qty, 1) if before.filled_qty < before.qty - 1
                else before.filled_qty, "avg_price": "364.20"}
    if status == FILLED:
        return {"filled_qty": before.qty, "avg_price": "364.20"}
    if status == CLOSED:
        return {"closed_by": "target", "realized_pnl": "12.00"}
    if status in (CANCELLED, EXPIRED):
        return {"filled_qty": before.filled_qty}
    return {}


# A path into each state, so every step out of it can be tried
PATHS = {
    APPROVED: [],
    SUBMITTING: [ev(SUBMITTING)],
    SUBMITTED: [ev(SUBMITTING), ev(SUBMITTED)],
    SEND_UNKNOWN: [ev(SUBMITTING), ev(SEND_UNKNOWN)],
    PARTIALLY_FILLED: [ev(SUBMITTING), ev(SUBMITTED),
                       ev(PARTIALLY_FILLED, filled_qty=3, avg_price="364.20")],
    FILLED: [ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=8, avg_price="364.20")],
    CANCELLED: [ev(SUBMITTING), ev(SUBMITTED),
                ev(PARTIALLY_FILLED, filled_qty=3, avg_price="364.20"),
                ev(CANCELLED, filled_qty=3)],
    EXPIRED: [ev(SUBMITTING), ev(SUBMITTED),
              ev(PARTIALLY_FILLED, filled_qty=3, avg_price="364.20"), ev(EXPIRED, filled_qty=3)],
    REJECTED: [ev(SUBMITTING), ev(REJECTED)],
    CLOSED: [ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=8, avg_price="364.20"),
             ev(CLOSED, closed_by="stop", realized_pnl="-48.00")],
}


# ======================= client_order_id =======================

def test_client_order_id_is_deterministic_and_distinct():
    assert client_order_id("run-1", "plan-1") == client_order_id("run-1", "plan-1")
    assert client_order_id("run-1", "plan-1") != client_order_id("run-1", "plan-2")
    assert client_order_id("run-1", "plan-1") != client_order_id("run-2", "plan-1")
    assert client_order_id("ab", "c") != client_order_id("a", "bc")  # separator matters
    cid = client_order_id("run-1", "plan-1")
    assert cid.startswith("ta-") and len(cid) == 35 and cid[3:].isalnum()


@pytest.mark.parametrize("run_id,plan_id", [("", "p"), ("r", ""), (None, "p"), ("r", 7),
                                            ("  ", "p")])
def test_client_order_id_needs_both_parts(run_id, plan_id):
    with pytest.raises(OrderError):
        client_order_id(run_id, plan_id)


# ======================= the allowed steps =======================

def test_every_state_has_a_path_and_the_table_is_complete():
    assert set(PATHS) == set(NEXT) == ORDER_EVENTS | {APPROVED}
    for status, path in PATHS.items():
        assert state_after(*path).status == status


@pytest.mark.parametrize("before,after", [(b, a) for b in NEXT for a in sorted(ORDER_EVENTS)],
                         ids=[f"{b}->{a}" for b in NEXT for a in sorted(ORDER_EVENTS)])
def test_each_step_is_allowed_exactly_when_the_table_says(before, after):
    state = state_after(*PATHS[before])
    event = ev(after, **fields_for(after, state))
    if after in NEXT[before]:
        assert apply_event(state, event).status == after
    else:
        with pytest.raises(OrderError, match=f"cannot go from {before} to {after}"):
            apply_event(state, event)


def test_a_full_trade_stopped_out():
    s = state_after(*PATHS[CLOSED])
    assert (s.status, s.filled_qty, s.avg_price, s.closed_by, s.realized_pnl) == (
        CLOSED, 8, D("364.20"), "stop", D("-48.00"))
    assert s.stopped_out and not s.open and not s.released
    assert s.realized_loss == D("48.00") and s.open_risk == 0  # #188: its loss counts
    assert s.last_at == T


def test_a_gap_through_the_stop_is_its_whole_realized_loss():
    s = state_after(ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=8, avg_price="364"),
                    ev(CLOSED, closed_by="stop", realized_pnl="-75.50"))
    assert s.realized_loss == D("75.50") and s.open_risk == 0


def test_a_closed_winner_carries_no_risk_and_no_loss():
    """#188: once closed, a trade counts only its realized loss; a profit counts nothing."""
    s = state_after(ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=8, avg_price="364"),
                    ev(CLOSED, closed_by="target", realized_pnl="47.00"))
    assert s.open_risk == 0 and s.realized_loss == 0 and not s.stopped_out


@pytest.mark.parametrize("events", [
    [ev(SUBMITTING), ev(REJECTED)],
    [ev(SUBMITTING), ev(SUBMITTED), ev(CANCELLED, filled_qty=0)],
    [ev(SUBMITTING), ev(SUBMITTED), ev(EXPIRED, filled_qty=0)],
    [ev(SUBMITTING), ev(SEND_UNKNOWN), ev(CANCELLED, filled_qty=0)],
], ids=["rejected", "cancelled", "expired", "send-unknown-cancelled"])
def test_risk_is_released_only_by_a_confirmed_zero_fill(events):
    s = state_after(*events)
    assert s.released and not s.open and s.open_risk == 0


@pytest.mark.parametrize("status", [APPROVED, SUBMITTING, SUBMITTED, SEND_UNKNOWN,
                                    PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED])
def test_risk_stays_counted_and_the_order_stays_open_until_closed_or_released(status):
    s = state_after(*PATHS[status])
    assert s.open and not s.released and s.open_risk == D("49.84")


def test_partial_fills_accumulate_then_fill():
    s = state_after(ev(SUBMITTING), ev(SUBMITTED),
                    ev(PARTIALLY_FILLED, filled_qty=2, avg_price="364.10"),
                    ev(PARTIALLY_FILLED, filled_qty=5, avg_price="364.15"),
                    ev(FILLED, filled_qty=8, avg_price="364.18"))
    assert s.status == FILLED and s.filled_qty == 8 and s.avg_price == D("364.18")


def test_a_cancelled_remainder_after_a_partial_fill_keeps_the_position_open():
    s = state_after(*PATHS[CANCELLED])
    assert s.filled_qty == 3 and s.open and not s.released
    closed = apply_event(s, ev(CLOSED, closed_by="flatten", realized_pnl="-1.20"))
    assert closed.status == CLOSED and not closed.open


def test_a_lost_send_resolved_as_filled():
    s = state_after(ev(SUBMITTING), ev(SEND_UNKNOWN),
                    ev(FILLED, filled_qty=8, avg_price="364.21"))
    assert s.status == FILLED and s.open


# ======================= field checks =======================

BASE_PARTIAL = [ev(SUBMITTING), ev(SUBMITTED),
                ev(PARTIALLY_FILLED, filled_qty=3, avg_price="364.20")]


@pytest.mark.parametrize("events,why", [
    ([ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=7, avg_price="364")],
     "filled needs all 8"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=9, avg_price="364")],
     "between 0 and 8"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(PARTIALLY_FILLED, filled_qty=8, avg_price="364")],
     "filled, not partially_filled"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(PARTIALLY_FILLED, filled_qty=0, avg_price="364")],
     "needs a fill"),
    ([*BASE_PARTIAL, ev(PARTIALLY_FILLED, filled_qty=2, avg_price="364")], "between 3 and 8"),
    ([*BASE_PARTIAL, ev(CANCELLED, filled_qty=1)], "between 3 and 8"),
    # S2: a cancel or expiry without the broker's fill count is not a confirmed zero fill
    ([ev(SUBMITTING), ev(SUBMITTED), ev(CANCELLED)], "needs the broker's filled_qty"),
    ([ev(SUBMITTING), ev(SEND_UNKNOWN), ev(EXPIRED)], "needs the broker's filled_qty"),
    ([*BASE_PARTIAL, ev(CANCELLED)], "needs the broker's filled_qty"),
    # S3: a partial fill is closed only after its remainder is cancelled or expired
    ([*BASE_PARTIAL, ev(CLOSED, closed_by="stop", realized_pnl="-1")],
     "cannot go from partially_filled to closed"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=8)], "avg_price is not a number"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=8, avg_price="0")], "out of range"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=8, avg_price="NaN")],
     "out of range"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=8, avg_price=True)],
     "not a number"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty="8", avg_price="364")],
     "not a whole number"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(FILLED, filled_qty=True, avg_price="364")],
     "not a whole number"),
    ([*PATHS[FILLED], ev(CLOSED, closed_by="manual", realized_pnl="1")], "closed_by"),
    ([*PATHS[FILLED], ev(CLOSED, closed_by="stop")], "realized_pnl is not a number"),
    ([*PATHS[FILLED], ev(CLOSED, closed_by="stop", realized_pnl="Infinity")], "out of range"),
    ([ev(SUBMITTING), ev(SUBMITTED), ev(EXPIRED, filled_qty=0),
      ev(CLOSED, closed_by="stop", realized_pnl="0")],
     "only a filled position can be closed"),
    ([ev(SUBMITTING), ev(SEND_UNKNOWN), ev(PARTIALLY_FILLED, filled_qty=2, avg_price="9"),
      ev(REJECTED)], "cannot go from partially_filled to rejected"),
    ([ev(SUBMITTING, at=datetime(2026, 11, 16, 10, 0))], "not an aware ISO time"),
    ([{"status": SUBMITTING, "client_order_id": CID,
       "at": datetime(9999, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-12))).isoformat()}],
     "not an aware ISO time"),
    ([{"status": SUBMITTING, "client_order_id": CID}], "not an aware ISO time"),
], ids=lambda x: x if isinstance(x, str) else None)
def test_a_malformed_event_is_refused(events, why):
    with pytest.raises(OrderError, match=why):
        replay([approved(), *events])


def test_an_unknown_event_status_is_refused():
    with pytest.raises(OrderError, match="unknown order event"):
        apply_event(state_after(), ev("half_filled"))


# ======================= replay =======================

def test_an_event_for_an_unknown_order_is_refused():
    with pytest.raises(OrderError, match="event for an unknown order"):
        replay([approved(), ev(SUBMITTING, cid="ta-other")])
    with pytest.raises(OrderError, match="event for an unknown order"):
        replay([ev(SUBMITTING)])


def test_the_same_order_approved_twice_is_refused():
    with pytest.raises(OrderError, match="approved twice"):
        replay([approved(), approved()])


def test_a_phase4_entry_without_an_id_gets_the_derived_one():
    orders = replay([approved(with_id=False)])
    assert list(orders) == [CID] and orders[CID].symbol == "TSLA"


def test_an_entry_whose_id_does_not_match_its_run_and_plan_is_refused():
    with pytest.raises(OrderError, match="does not match"):
        replay([approved(client_order_id="ta-forged")])


@pytest.mark.parametrize("over,why", [
    ({"order": None}, "no order"),
    ({"order": {"symbol": "TSLA", "qty": 0}}, "at least 1"),
    ({"order": {"symbol": "TSLA", "qty": True}}, "whole number"),
    ({"order": {"symbol": "TSLA", "qty": "8"}}, "whole number"),
    ({"order": {"symbol": " ", "qty": 8}}, "no symbol"),
    ({"max_loss": "-1"}, "out of range"),
    ({"max_loss": None}, "not a number"),
    ({"approved_at": "2026-11-16T10:00:00"}, "not an aware"),
    ({"run_id": None, "client_order_id": CID}, "run id and a plan id"),
], ids=lambda x: x if isinstance(x, str) else None)
def test_a_malformed_approved_entry_is_refused(over, why):
    with pytest.raises(OrderError, match=why):
        from_approved(approved(**over))


def test_other_entries_are_ignored():
    entries = [{"status": "pass", "run_id": "r"}, {"status": "declined"},
               {"status": "no_order"}, {"status": "journal_locked"}, approved()]
    assert list(replay(entries)) == [CID]


def test_the_entry_number_is_named_in_the_error():
    with pytest.raises(OrderError, match=r"^entry 3: "):
        replay([{"status": "pass"}, approved(), ev(FILLED, filled_qty=8, avg_price="1")])


# ======================= Journal.record_order_event and orders() =======================

def journal(tmp_path, *entries):
    j = Journal(tmp_path / "j.jsonl")
    for e in entries:
        j.append(e)
    return j


def test_record_order_event_appends_and_returns_the_new_state(tmp_path):
    j = journal(tmp_path, approved())
    s = j.record_order_event(CID, SUBMITTING, T + timedelta(seconds=1))
    assert s.status == SUBMITTING
    s = j.record_order_event(CID, SUBMITTED, T + timedelta(seconds=2), broker_order_id="b-1")
    assert j.orders()[CID].status == SUBMITTED
    last = json.loads(j.path.read_text(encoding="utf-8").splitlines()[-1])
    assert last == {"status": SUBMITTED, "client_order_id": CID, "broker_order_id": "b-1",
                    "at": (T + timedelta(seconds=2)).isoformat()}


@pytest.mark.parametrize("cid,status,fields,why", [
    (CID, FILLED, {"filled_qty": 8, "avg_price": "1"}, "cannot go from approved_not_sent"),
    ("ta-unknown", SUBMITTING, {}, "no approved order"),
    (CID, "teleported", {}, "unknown order event"),
], ids=["impossible-step", "unknown-order", "unknown-status"])
def test_record_order_event_refuses_without_writing(tmp_path, cid, status, fields, why):
    j = journal(tmp_path, approved())
    before = j.path.read_bytes()
    with pytest.raises(JournalError, match=why):
        j.record_order_event(cid, status, T, **fields)
    assert j.path.read_bytes() == before


@pytest.mark.parametrize("field", ["client_order_id", "status", "at"])
def test_record_order_event_cannot_override_the_key_fields(tmp_path, field):
    j = journal(tmp_path, approved())
    before = j.path.read_bytes()
    with pytest.raises(TypeError):
        j.record_order_event(CID, SUBMITTING, T, **{field: "forged"})
    assert j.path.read_bytes() == before


@pytest.mark.parametrize("at", [None, datetime(2026, 11, 16, 10, 0),
                                datetime(9999, 12, 31, 23, 59,
                                         tzinfo=timezone(timedelta(hours=-12)))],
                         ids=["none", "naive", "edge"])
def test_record_order_event_needs_a_usable_time(tmp_path, at):
    with pytest.raises(JournalError, match="time"):
        journal(tmp_path, approved()).record_order_event(CID, SUBMITTING, at)


def test_orders_wraps_an_invalid_history_as_a_journal_error(tmp_path):
    j = journal(tmp_path, approved(), ev(FILLED, filled_qty=8, avg_price="1"))
    with pytest.raises(JournalError, match="order history is invalid: entry 2"):
        j.orders()
