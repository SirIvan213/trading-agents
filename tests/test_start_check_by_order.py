"""#257: run_day's start-of-day broker check matches the broker to today's journal BY ORDER
(client_order_id and bracket leg ids; for a position, the shares today's filled entries still
hold by the broker's own record), never by symbol. An earlier day's leftover in a symbol also
traded today holds the day, blocks and alerts like any other, on a mid-day restart too, and
nothing is cancelled or closed. Against the simulated Alpaca PAPER account
(tests/test_reconcile.py). No network."""
from __future__ import annotations

import copy
from datetime import timedelta
from decimal import Decimal

import pytest

import pipeline.run_day as rd
from journal.orders import CLOSED, FILLED, SUBMITTED, OrderState
from tests.test_reconcile import blocks, keys, sent  # noqa: F401  (keys: autouse)
from tests.test_run_day import LIMITS
from tests.test_run_day_flatten import TUESDAY, alerts, paper_day

RESTART = TUESDAY.replace(hour=11, minute=0)  # mid-day: run_day started again
SENT_AT = RESTART - timedelta(hours=1)  # today's order, sent before the restart


def today_trade(sim, day, *, qty=1, filled=None):
    """An order sent today and filled at the broker (all of it unless `filled`). The journal
    still says "submitted": the fill was never recorded before run_day stopped."""
    cid = sent(day.journal, qty=qty, at=SENT_AT)
    sim.add_entry(cid, qty=qty)
    sim.fill(cid, qty=filled)
    return cid


def snapshot(sim):
    return (dict(sim.positions),
            {cid: (e["status"], [g["status"] for g in e["legs"]])
             for cid, e in sim.entries.items()})


def assert_held_untouched(sim, day, dec, before):
    """Held: no decision, no reconciler, and nothing cancelled or closed at the broker."""
    assert sim.writes == []
    assert snapshot(sim) == before
    assert dec.calls == [] and day.reconcile_thread is None


def check(day):
    return rd.broker_start_check(day, RESTART.date())


# ================= an earlier day's leftover in a symbol traded today =================

def _leftover_shares(sim):
    sim.positions["SPY"] += 5  # an earlier day's 5 SPY shares, unknown to the journal


def _leftover_bracket(sim):
    sim.add_entry("yesterdays-spy", symbol="SPY")  # an earlier day's filled bracket: a
    sim.fill("yesterdays-spy")  # share, and its take-profit leg listed


def _leftover_entry(sim):
    sim.add_entry("yesterdays-spy", symbol="SPY")  # an earlier day's entry, still working


@pytest.mark.parametrize("leave, keys_", [
    (_leftover_shares, ["start-of-day:SPY"]),
    (_leftover_bracket, ["start-of-day-order:{leg}", "start-of-day:SPY"]),
    (_leftover_entry, ["start-of-day-order:{entry}"]),
], ids=["shares", "filled-bracket", "working-entry"])
def test_a_restart_with_an_earlier_days_leftover_in_a_symbol_traded_today_holds(
        tmp_path, monkeypatch, leave, keys_):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    today_trade(sim, day)  # today's SPY: 1 share, its take-profit leg listed
    leave(sim)
    before = snapshot(sim)
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert_held_untouched(sim, day, dec, before)
    left = sim.entries.get("yesterdays-spy")
    want = sorted(k.format(entry=left and left["id"],
                           leg=left and sim.leg("yesterdays-spy", "limit")["id"])
                  for k in keys_)
    got = blocks(day.journal)
    assert sorted(b["key"] for b in got) == want
    assert all(b["kind"] == "orphan" and "not closed automatically" in b["reason"]
               for b in got)
    msgs = alerts(day)
    assert any("at the broker, not from an order sent today" in m for m in msgs)
    # SPY holds today's share AND an earlier day's: flatten --now (by symbol) is not advised
    (unwatched,) = [m for m in msgs if "will not watch or flatten" in m]
    assert "SPY position" in unwatched and "Do NOT run python -m pipeline.flatten --now" in (
        unwatched) and unwatched.endswith("SPY")


def test_the_hold_holds_again_on_the_next_restart_with_the_same_block(tmp_path, monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    today_trade(sim, day)
    _leftover_shares(sim)
    before = snapshot(sim)
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert_held_untouched(sim, day, dec, before)
    assert [b["key"] for b in blocks(day.journal)] == ["start-of-day:SPY"]  # never twice


# ================= partial explanations count as unexplained =================

def test_a_position_larger_than_todays_fills_holds(tmp_path, monkeypatch):
    """Today's entry of 10 filled only 4 (still working): 4 shares are explained, not 10.
    A position of 6 is blocked in full."""
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    today_trade(sim, day, qty=10, filled=4)
    sim.positions["SPY"] += 2
    before = snapshot(sim)
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert_held_untouched(sim, day, dec, before)
    (b,) = blocks(day.journal)
    assert b["key"] == "start-of-day:SPY"
    assert "(6 shares)" in b["reason"] and "explain only 4 of them" in b["reason"]


def test_what_todays_legs_sold_is_not_explained_again(tmp_path, monkeypatch):
    """10 filled, the take-profit sold 4: today's order holds 6. An earlier day's 3 make 9,
    fewer than the 10 the entry filled, and still hold the day."""
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    cid = today_trade(sim, day, qty=10)
    sim.leg(cid, "limit").update(status="partially_filled", filled_qty="4",
                                 filled_avg_price="721.72")
    sim.positions["SPY"] = 6 + 3
    found, todays, mixed = check(day)
    assert len(found) == 1 and "explain only 6 of them" in found[0]
    assert mixed == ["SPY"]
    assert sim.writes == []


def test_a_trade_the_journal_closed_explains_no_shares(tmp_path, monkeypatch):
    """Today's trade was flattened and journaled closed (its legs cancelled, nothing sold by
    them): a SPY position at the broker now is not today's."""
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    cid = today_trade(sim, day)
    for g in sim.entries[cid]["legs"]:
        g["status"] = "canceled"
    j = day.journal
    j.record_order_event(cid, "filled", SENT_AT + timedelta(seconds=5), filled_qty=1,
                         avg_price="718.32")
    j.record_order_event(cid, "closed", SENT_AT + timedelta(minutes=30), closed_by="flatten",
                         realized_pnl="0.47")
    before = snapshot(sim)  # SPY 1 still there: an earlier day's
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert_held_untouched(sim, day, dec, before)
    (b,) = blocks(day.journal)
    assert b["key"] == "start-of-day:SPY" and "not from an order sent today" in b["reason"]


def test_an_order_sent_today_the_broker_does_not_know_explains_nothing(tmp_path, monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    sent(day.journal, at=SENT_AT)  # journaled as sent; the broker has no such order
    sim.positions["SPY"] = 1
    before = snapshot(sim)
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert_held_untouched(sim, day, dec, before)
    assert [b["key"] for b in blocks(day.journal)] == ["start-of-day:SPY"]


# ================= everything explained: no hold =================

def test_a_restart_where_everything_is_explained_is_not_held(tmp_path, monkeypatch):
    """Today's filled SPY (journal lagging: it still says submitted), with its take-profit
    leg listed; today's QQQ entry still working, legs nested. Nothing blocked; the day runs
    and its reconciler starts."""
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch)
    today_trade(sim, day)
    qqq = sent(day.journal, symbol="QQQ", at=TUESDAY - timedelta(hours=1))
    sim.add_entry(qqq, symbol="QQQ")
    found, todays, mixed = check(day)
    assert found == [] and mixed == []
    assert todays == ["SPY position (1 shares)", "QQQ entry order (new)"]
    assert sim.writes == []
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert not any(b["key"].startswith("start-of-day") for b in blocks(day.journal))


def test_a_position_smaller_than_todays_fills_is_explained(tmp_path, monkeypatch):
    """Exits only shrink a position: 10 filled, the take-profit sold 4, 6 left."""
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    cid = today_trade(sim, day, qty=10)
    sim.leg(cid, "limit").update(status="partially_filled", filled_qty="4",
                                 filled_avg_price="721.72")
    sim.positions["SPY"] = 6
    assert check(day) == ([], ["SPY position (6 shares)"], [])
    assert blocks(day.journal) == []


# ================= reads, and their order =================

def test_todays_orders_are_looked_up_before_the_positions_are_read(tmp_path, monkeypatch):
    """A fill landing between the reads can then only make a position look larger (a hold),
    never hide a leftover."""
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    today_trade(sim, day)
    calls = []
    for name in ("get_order_by_client_id", "list_positions", "list_open_orders_checked"):
        real = getattr(day.broker, name)
        monkeypatch.setattr(day.broker, name,
                            lambda *a, _n=name, _r=real: calls.append(_n) or _r(*a))
    check(day)
    assert calls == ["get_order_by_client_id", "list_positions", "list_open_orders_checked"]


def test_a_lookup_that_fails_holds_the_day(tmp_path, monkeypatch):
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    today_trade(sim, day)
    faulty.add("GET", "/v2/orders:by_client_order_id", "500")
    before = snapshot(sim)
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert_held_untouched(sim, day, dec, before)
    msgs = alerts(day)
    assert any("start-of-day broker check failed" in m for m in msgs)
    assert any("any position of today's (the broker could not be read)" in m for m in msgs)


def test_an_unmixed_hold_still_advises_flatten_now(tmp_path, monkeypatch):
    """The leftover is in another symbol: --now would close only today's SPY."""
    sim, faulty, day, clock, dec, out = paper_day(tmp_path, monkeypatch, start=RESTART)
    today_trade(sim, day)
    sim.positions["MSFT"] = 5
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    (unwatched,) = [m for m in alerts(day) if "will not watch or flatten" in m]
    assert "or run python -m pipeline.flatten --now" in unwatched
    assert "Do NOT" not in unwatched


# ================= held_by_today (pure) =================

def _state(cid, symbol="SPY", status=SUBMITTED, filled=0):
    return OrderState(client_order_id=cid, symbol=symbol, run_id="r", plan_id="p",
                      approved_at=RESTART, qty=10, max_loss=Decimal("1"), status=status,
                      filled_qty=filled, sent=True)


def _found(cid, symbol="SPY", filled="10", legs=("0", "0")):
    return {"id": f"ord-{cid}", "client_order_id": cid, "symbol": symbol,
            "filled_qty": filled,
            "legs": [{"id": f"leg-{cid}-{n}", "filled_qty": q} for n, q in enumerate(legs)]}


def test_held_by_today():
    sent_today = {
        "a": _state("a"),  # the broker's fill counts, whatever the journal says
        "b": _state("b", status=CLOSED, filled=10),  # closed: holds none, legs still known
        "c": _state("c", symbol="QQQ"),  # the broker shows it in another symbol: nothing
        "d": _state("d", symbol="IWM"),  # the broker does not know it: nothing
        "e": _state("e", symbol="DIA", status=FILLED, filled=10),  # legs sold all 10
    }
    looked = {"a": _found("a", legs=("4", "0")), "b": _found("b"),
              "c": _found("c", symbol="SPY"), "d": None,
              "e": _found("e", symbol="DIA", legs=("10", "0"))}
    held, legs = rd.held_by_today(sent_today, copy.deepcopy(looked))
    assert held == {"SPY": Decimal("6")}
    assert legs == {"leg-a-0", "leg-a-1", "leg-b-0", "leg-b-1", "leg-e-0", "leg-e-1"}
