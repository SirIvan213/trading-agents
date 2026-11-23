"""pipeline/reconcile.py and pipeline/flatten.py (Phase 5 PR 4, #155), end to end against a
simulated Alpaca PAPER account: the real tools.broker client over a fake opener whose order
shapes are the probe's recorded replies (tests/fixtures/broker/, #196), and which behaves as
the probe found: legs nested on a lookup, the held stop missing from the open-orders listing,
a 403 (40310000) on closing while the legs hold the shares, a group cancel. No network."""
from __future__ import annotations

import copy
import io
import itertools
import json
import threading
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import pipeline.reconcile as rc
from journal.log import Journal
from journal.orders import client_order_id
from tests.test_broker import Resp
from tools.broker import ACTION_LOCK, AlpacaPaperBroker
from tools.config import CONFIG_DIR, load_limits

NY = ZoneInfo("America/New_York")
REC = Path(__file__).parent / "fixtures" / "broker"
SYN = Path(__file__).parent / "fixtures" / "alpaca_broker"
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
T0 = datetime(2026, 11, 24, 10, 0, tzinfo=NY)  # a Tuesday


def rec(name):
    return json.loads((REC / name).read_text(encoding="utf-8"))["body"]


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "SENTINEL-KEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SENTINEL-SECRET")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def step(self, seconds):
        self.t += timedelta(seconds=seconds)


class Sim:
    """A paper account: entries (with legs), close orders and positions, served through the
    endpoints tools.broker calls. Records every write."""

    def __init__(self):
        self.entries: dict[str, dict] = {}  # cid -> raw entry (legs nested)
        self.closes: list[dict] = []
        self.positions: dict[str, int] = {}
        self.extra_open: list = []  # raw orders the listing shows as they are
        self.writes: list[tuple[str, str]] = []
        self.ids = itertools.count(100)
        self.close_reply_broken = False  # the close works, its reply is unreadable (#196)
        self.close_fills = True
        self.minutes = itertools.count(1)

    def _stamp(self):
        """A cancel time: each cancel call a minute after the last (a group shares one)."""
        return f"2026-11-24T16:{next(self.minutes):02d}:00Z"

    # ---- building ----
    def add_entry(self, cid, *, symbol="SPY", qty=1, status="new"):
        e = copy.deepcopy(rec("bracket_submit.json"))
        e.update(id=f"ord-{next(self.ids)}", client_order_id=cid, symbol=symbol, qty=str(qty),
                 status=status)
        for leg in e["legs"]:
            leg.update(id=f"leg-{next(self.ids)}", symbol=symbol, qty=str(qty), status="held")
        self.entries[cid] = e
        return e

    def leg(self, cid, kind):
        return next(g for g in self.entries[cid]["legs"] if g["type"] == kind)

    def fill(self, cid, qty=None, avg="718.32"):
        e = self.entries[cid]
        n = int(e["qty"]) if qty is None else qty
        old = int(e["filled_qty"])
        e.update(filled_qty=str(n), filled_avg_price=avg,
                 status="filled" if n == int(e["qty"]) else "partially_filled",
                 filled_at="2026-11-24T15:00:05Z")
        if e["status"] == "filled":
            self.leg(cid, "limit")["status"] = "new"  # #196: take-profit new, stop held
        self.positions[e["symbol"]] = self.positions.get(e["symbol"], 0) + n - old

    def fill_leg(self, cid, kind, avg):
        e = self.entries[cid]
        leg = self.leg(cid, kind)
        leg.update(status="filled", filled_qty=e["filled_qty"], filled_avg_price=avg,
                   filled_at="2026-11-24T15:30:00Z")
        for other in e["legs"]:
            if other is not leg and other["status"] not in ("filled", "canceled"):
                other["status"] = "canceled"
        self.positions.pop(e["symbol"], None)

    # ---- serving ----
    def _all_orders(self):
        for e in self.entries.values():
            yield e
            yield from e["legs"]
        yield from self.closes

    def _open_listing(self):
        out = []
        for e in self.entries.values():
            if e["status"] in ("new", "accepted", "partially_filled", "pending_new"):
                out.append(e)  # its legs nested
            elif e["status"] == "filled":
                # #196: after the fill only the take-profit leg is listed; the held stop is not
                out.extend(g for g in e["legs"] if g["status"] in ("new", "accepted"))
        out.extend(c for c in self.closes if c["status"] in ("new", "pending_new",
                                                               "accepted"))
        return out + list(self.extra_open)

    def _cancel(self, oid):
        for e in self.entries.values():
            group = [e] + e["legs"]
            for o in group:
                if o["id"] == oid:
                    if o["status"] in ("filled", "canceled", "expired"):
                        return 422, {"code": 42210000}
                    stamp = self._stamp()
                    o.update(status="canceled", canceled_at=stamp)
                    # cancelling one order of a bracket cancels the rest of the group
                    for other in group:
                        if other["status"] not in ("filled", "canceled", "expired"):
                            other.update(status="canceled", canceled_at=stamp)
                    return 204, None
        return 404, {"code": 40410000}

    def _close(self, symbol):
        if symbol not in self.positions:
            return 404, {"code": 40410000}
        holding = any(o["status"] in ("new", "held", "accepted") and o.get("side") == "sell"
                      and o["symbol"] == symbol for o in self._all_orders())
        if holding:
            return 403, {"code": 40310000, "available": "0"}
        c = copy.deepcopy(rec("close_position_simple.json"))
        c.update(id=f"close-{next(self.ids)}", symbol=symbol,
                 qty=str(self.positions[symbol]), submitted_at="2026-11-24T16:00:00Z")
        self.closes.append(c)
        reply = copy.deepcopy(c)
        if self.close_fills:
            c.update(status="filled", filled_qty=c["qty"], filled_avg_price="718.79",
                     filled_at="2026-11-24T16:00:01Z")
            self.positions.pop(symbol, None)
        if self.close_reply_broken:
            reply["order_class"] = ["?"]
        return 200, reply

    def __call__(self, req, timeout=None):
        parts = urllib.parse.urlsplit(req.full_url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        method, path = req.get_method(), parts.path
        if method != "GET":
            self.writes.append((method, path))
        if path == "/v2/account":
            status, body = 200, json.loads((SYN / "account.json").read_text("utf-8"))
        elif path == "/v2/orders:by_client_order_id":
            e = self.entries.get(q["client_order_id"])
            status, body = (200, e) if e else (404, {"code": 40410000})
        elif path == "/v2/orders" and method == "GET":
            if q.get("status") == "open":
                status, body = 200, self._open_listing()
            else:
                sym = q["symbols"]
                status, body = 200, [o for o in reversed(list(self._all_orders()))
                                     if o["symbol"] == sym and o not in
                                     [g for e in self.entries.values() for g in e["legs"]]]
        elif path == "/v2/positions" and method == "GET":
            status, body = 200, [
                {"symbol": s, "side": "long", "qty": str(n), "avg_entry_price": "718.32",
                 "qty_available": "0"} for s, n in self.positions.items()]
        elif method == "DELETE" and path.startswith("/v2/orders/"):
            status, body = self._cancel(path.rsplit("/", 1)[1])
        elif method == "DELETE" and path.startswith("/v2/positions/"):
            status, body = self._close(path.rsplit("/", 1)[1])
        else:
            raise AssertionError(f"unexpected {method} {path}")
        raw = b"" if body is None else json.dumps(body).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(raw))
        return Resp(status, raw, req.full_url)


# ---- the journal side --------------------------------------------------------------------

RUNS = itertools.count(1)


def sent(j, *, symbol="SPY", qty=1, at=T0, status="submitted"):
    """An approval sent as pipeline/send.py journals it; returns its client_order_id."""
    run = f"run-{next(RUNS):04d}"
    cid = client_order_id(run, "plan-1")
    at = at - timedelta(seconds=10)  # sent before the reconciler's clock reads it
    j.append({"status": "approved_not_sent", "approved_at": at.isoformat(), "run_id": run,
              "plan_id": "plan-1", "symbol": symbol, "client_order_id": cid,
              "max_loss": "3.80", "expires_at": (at + timedelta(minutes=5)).isoformat(),
              "order": {"symbol": symbol, "side": "buy", "qty": qty, "entry": "718.34",
                        "slippage": "0.50", "stop_loss": "714.52", "take_profit": "721.72"}})
    j.record_order_event(cid, "submitting", at + timedelta(seconds=1), limit_price="718.84")
    if status != "submitting":
        j.record_order_event(cid, "submitted", at + timedelta(seconds=2),
                             broker_order_id="ord-x")
    return cid


def world(tmp_path, **kw):
    sim = Sim()
    broker = AlpacaPaperBroker(LIMITS, opener=sim)
    broker.verify_paper_account()
    j = Journal(tmp_path / "decisions.jsonl")
    clock = Clock()
    alerts: list[str] = []
    r = rc.Reconciler(j, broker, clock=clock, limits=LIMITS, alert=alerts.append,
                      sleep=lambda s: None, monotonic=itertools.count().__next__, **kw)
    return sim, j, clock, r, alerts


def events(j, cid):
    return [e for e in j.entries() if e.get("client_order_id") == cid
            and e["status"] not in ("approved_not_sent",)]


def statuses(j, cid):
    return [e["status"] for e in events(j, cid)]


def blocks(j):
    return [e for e in j.entries() if e["status"] == "reconcile_block"]


# ======================= fills, protection, closes =======================

def test_a_fill_is_journaled_at_receipt_time_with_the_brokers_time_as_a_field(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    clock.step(10)
    r.pass_once()
    filled = events(j, cid)[-1]
    assert filled["status"] == "filled" and filled["filled_qty"] == 1
    assert filled["avg_price"] == "718.32"
    assert filled["at"] == clock().isoformat()  # local receipt time (#202)
    assert filled["broker_filled_at"] == "2026-11-24T15:00:05Z"
    # protected (held stop, live take-profit): nothing else happens
    assert sim.writes == [] and blocks(j) == [] and alerts == []


def test_the_listing_hides_the_held_stop_but_the_position_is_still_protected(tmp_path):
    """#196 finding 5: stops are judged from the lookup with legs, never from the listing."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    listed = sim._open_listing()
    assert [o["type"] for o in listed] == ["limit"]  # no stop in the listing
    for _ in range(3):
        clock.step(15)
        r.pass_once()
    assert blocks(j) == [] and sim.writes == []


def test_an_unfilled_entry_is_cancelled_after_60_s_and_releases_its_risk(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    clock.step(30)
    r.pass_once()
    assert sim.writes == []  # not yet
    clock.step(31)
    r.pass_once()
    assert sim.writes == [("DELETE", f"/v2/orders/{sim.entries[cid]['id']}")]
    last = events(j, cid)[-1]
    assert last["status"] == "cancelled" and last["filled_qty"] == 0
    assert j.day_state(clock()).risk_used_today == 0


def test_a_partial_fill_gets_one_poll_then_the_remainder_is_cancelled(tmp_path):
    """Ivan's answer 2: one poll of grace; never flatten while the entry can still fill."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    clock.step(5)
    r.pass_once()
    assert statuses(j, cid)[-1] == "partially_filled" and sim.writes == []
    clock.step(10)  # 10 s: still inside the grace
    r.pass_once()
    assert sim.writes == []
    clock.step(6)  # past one poll (15 s)
    r.pass_once()
    assert sim.writes[0] == ("DELETE", f"/v2/orders/{sim.entries[cid]['id']}")
    assert "cancelled" in statuses(j, cid)


def test_after_a_partial_fill_legs_the_broker_kept_mean_protected(tmp_path):
    """Ivan's change: decide from what the broker shows. If Alpaca keeps the legs (sized to
    the filled shares) after the remainder is cancelled, the position is protected."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    e = sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    real_cancel = sim._cancel

    def keep_legs(oid):
        if oid == e["id"]:
            e["status"] = "canceled"
            for g in e["legs"]:
                g["qty"] = "4"
            sim.leg(cid, "limit")["status"] = "new"
            return 204, None
        return real_cancel(oid)

    sim._cancel = keep_legs
    r.pass_once()
    clock.step(16)
    r.pass_once()
    clock.step(15)
    r.pass_once()
    assert blocks(j) == []
    assert ("DELETE", "/v2/positions/SPY") not in sim.writes and sim.positions == {"SPY": 4}


def test_after_a_partial_fill_legs_the_broker_cancelled_mean_flatten(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=10)
    sim.add_entry(cid, qty=10)
    sim.fill(cid, qty=4)
    r.pass_once()
    clock.step(16)
    r.pass_once()  # cancels the remainder (the group goes with it), then flattens
    assert ("DELETE", "/v2/positions/SPY") in sim.writes and sim.positions == {}
    clock.step(15)
    r.pass_once()  # the close is recorded
    closed = events(j, cid)[-1]
    assert closed["status"] == "closed" and closed["closed_by"] == "flatten"
    assert closed["realized_pnl"] == "1.88"  # 4 x (718.79 - 718.32)
    # Ivan's decision on #251: a CONFIRMED flatten of this case alerts, never blocks
    assert blocks(j) == []
    assert any("flatten is confirmed" in a for a in alerts)


def test_a_missing_stop_is_blocked_alerted_and_flattened(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"  # e.g. cancelled by hand
    r.pass_once()
    (b,) = blocks(j)
    assert b["kind"] == "unprotected" and "the stop is canceled" in b["reason"]
    assert any("new entries blocked" in a for a in alerts)
    # cancel the take-profit, confirm it's gone, then close
    tp = sim.leg(cid, "limit")["id"]
    assert sim.writes == [("DELETE", f"/v2/orders/{tp}"), ("DELETE", "/v2/positions/SPY")]
    assert sim.positions == {}


def test_a_triggered_stop_gets_60_s_then_alert_cancel_and_close(tmp_path):
    """Ivan's answer 1: a stop that triggered and is selling is the stop doing its job."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    r.pass_once()
    sim.leg(cid, "stop")["status"] = "new"  # triggered, selling
    sim.leg(cid, "limit")["status"] = "canceled"
    clock.step(15)
    r.pass_once()
    clock.step(30)
    r.pass_once()
    assert sim.writes == [] and blocks(j) == []
    clock.step(31)
    r.pass_once()
    assert any("cancelling the stop and closing" in a for a in alerts)
    stop = sim.leg(cid, "stop")["id"]
    assert sim.writes == [("DELETE", f"/v2/orders/{stop}"), ("DELETE", "/v2/positions/SPY")]


def test_a_target_exit_is_closed_with_its_realized_pnl(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    r.pass_once()
    sim.fill_leg(cid, "limit", "721.72")
    clock.step(15)
    r.pass_once()
    closed = events(j, cid)[-1]
    assert (closed["status"], closed["closed_by"], closed["realized_pnl"]) == (
        "closed", "target", "3.40")
    assert closed["at"] == clock().isoformat()


def test_a_stop_exit_is_a_loss_counted_today(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    r.pass_once()
    sim.fill_leg(cid, "stop", "714.52")
    clock.step(15)
    r.pass_once()
    closed = events(j, cid)[-1]
    assert (closed["closed_by"], closed["realized_pnl"]) == ("stop", "-3.80")
    ds = j.day_state(clock())
    assert ds.realized_loss_today == D("3.80") and "SPY" in ds.stopped_out_symbols


def test_an_overnight_position_closed_this_morning_counts_today(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, at=T0 - timedelta(days=1))
    sim.add_entry(cid)
    sim.fill(cid)
    j.record_order_event(cid, "filled", T0 - timedelta(days=1, seconds=-10), filled_qty=1,
                         avg_price="718.32")
    sim.fill_leg(cid, "stop", "714.52")
    r.pass_once()
    assert events(j, cid)[-1]["at"].startswith("2026-11-24")
    assert j.day_state(clock()).realized_loss_today == D("3.80")


# ======================= unclear replies: check, never repeat =======================

def test_an_unclear_close_is_checked_and_never_sent_twice(tmp_path):
    """#196 finding 7: the close FILLED while its reply was refused. check_close finds it."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"
    sim.close_reply_broken = True
    r.pass_once()
    assert sim.writes.count(("DELETE", "/v2/positions/SPY")) == 1
    assert any("check_close SPY: closed" in a for a in r.report["actions"])
    clock.step(15)
    r.pass_once()
    assert sim.writes.count(("DELETE", "/v2/positions/SPY")) == 1
    assert events(j, cid)[-1]["status"] == "closed"


def test_a_close_refused_while_the_legs_hold_the_shares_waits_for_the_next_pass(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"
    real = sim._cancel
    sim._cancel = lambda oid: (204, None)  # the cancel is accepted but not done yet
    r.pass_once()
    assert ("DELETE", "/v2/positions/SPY") not in sim.writes  # not gone yet: no close
    assert any("not gone yet" in a for a in alerts)  # never quiet
    sim._cancel = real
    clock.step(15)
    r.pass_once()
    assert sim.positions == {}


# ======================= unreadable orders, orphans =======================

def stray(symbol="AAPL", **over):
    return {**rec("close_position_simple.json"), "id": "stray-1", "symbol": symbol,
            "type": "stop", "stop_price": "200.00", "order_class": "", "status": "new",
            **over}


def test_an_unreadable_order_blocks_and_alerts_but_everything_else_is_reconciled(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.extra_open.append(stray())
    r.pass_once()
    assert statuses(j, cid)[-1] == "filled"  # the rest was reconciled
    (b,) = blocks(j)
    assert b["kind"] == "unreadable_order" and "stray-1" in b["reason"]
    assert ("DELETE", "/v2/orders/stray-1") not in sim.writes  # never acted on
    from tests.order_helpers import clear_gate, sized_order
    from tools.risk_check import check_order
    o = sized_order()
    assert check_order(o, LIMITS, j.day_state(clock()), clock(),
                       gate=clear_gate(o["symbol"], clock()))["verdict"] == "BLOCK"


def test_an_unreadable_order_in_the_symbol_skips_the_close_and_never_goes_silent(tmp_path):
    """Ivan's rule on #155: the skip repeats its alert every pass; 60 s later the alert says
    manual action is needed in the Alpaca paper dashboard."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"
    sim.extra_open.append(stray(symbol="SPY"))
    seen = []
    for _ in range(6):  # 6 passes, 15 s apart
        before = len(alerts)
        r.pass_once()
        new = alerts[before:]
        assert any("SPY" in a and ("skipped" in a or "MANUAL ACTION" in a) for a in new), new
        seen.append(any("MANUAL ACTION NEEDED in the Alpaca paper dashboard: cancel SPY's "
                        "orders and close the SPY position" in a for a in new))
        clock.step(15)
    assert seen[:4] == [False] * 4 and seen[4] and seen[5]  # from 60 s on: loud, every pass
    assert ("DELETE", "/v2/positions/SPY") not in sim.writes


def test_the_skip_is_loud_at_the_flat_by_time_even_before_60_s(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    clock.t = T0.replace(hour=15, minute=55)  # the flat-by (limits.example.json)
    cid = sent(j, at=clock.t - timedelta(minutes=5))
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"
    sim.extra_open.append(stray(symbol="SPY"))
    r.pass_once()
    assert any("MANUAL ACTION NEEDED" in a for a in alerts)


def test_an_orphan_position_is_blocked_and_flattened(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    sim.positions["SPY"] = 3
    r.pass_once()
    (b,) = blocks(j)
    assert b["kind"] == "orphan" and "not in the journal" in b["reason"]
    assert sim.writes == [("DELETE", "/v2/positions/SPY")]


def test_an_orphan_order_is_blocked_and_cancelled(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    foreign = sim.add_entry("someone-elses")
    sim.entries.pop("someone-elses")
    sim.extra_open.append(foreign)
    sim.entries["hidden"] = foreign  # so a cancel finds it
    r.pass_once()
    assert [b["kind"] for b in blocks(j)] == ["orphan"]
    assert ("DELETE", f"/v2/orders/{foreign['id']}") in sim.writes


# ======================= the journal refusing, lost sends, 422 =======================

def test_a_broker_event_the_journal_refuses_is_a_conflict_block(tmp_path):
    """#189: a fill above the sized quantity cannot be journaled: block and alert."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, qty=1)
    e = sim.add_entry(cid, qty=1)
    e.update(status="filled", filled_qty="2", filled_avg_price="718.32")
    r.pass_once()
    kinds = [b["kind"] for b in blocks(j)]
    assert "conflict" in kinds
    assert any("refused the broker's" in b["reason"] for b in blocks(j))


def test_a_clock_step_refusal_is_retried_not_blocked(tmp_path):
    """#202: "before the order's previous event" is a clock problem: retried, alerted only
    if it lasts."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    clock.t = T0 - timedelta(minutes=5)  # the PC's clock went back
    r.pass_once()
    assert blocks(j) == [] and statuses(j, cid)[-1] == "submitted"
    clock.step(121)
    r.pass_once()
    assert any("check the PC's clock" in a for a in alerts)
    clock.t = T0 + timedelta(seconds=30)
    r.pass_once()
    assert statuses(j, cid)[-1] == "filled"


def test_a_stuck_submitting_found_at_the_broker_becomes_submitted(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, status="submitting")
    sim.add_entry(cid)
    r.pass_once()
    assert statuses(j, cid)[-1] == "submitted"


def test_a_lost_send_is_rejected_only_after_the_grace_and_two_404s(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, status="submitting")
    r.pass_once()
    clock.step(60)
    r.pass_once()  # two 404s, but inside 120 s
    assert statuses(j, cid)[-1] == "submitting"
    clock.step(61)
    r.pass_once()
    assert statuses(j, cid)[-1] == "rejected"
    assert j.day_state(clock()).risk_used_today == 0


def test_a_failed_lookup_is_never_a_rejection(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, status="submitting")
    real = sim.__call__

    def broken(req, timeout=None):
        if "by_client_order_id" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 503, "x", {}, io.BytesIO(b"{}"))
        return real(req, timeout)

    r.broker._open = broken
    for _ in range(5):
        clock.step(60)
        r.pass_once()
    assert statuses(j, cid)[-1] == "submitting"


def test_a_rejected_order_the_broker_holds_is_a_conflict_and_is_flattened(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j, status="submitting")
    j.record_order_event(cid, "rejected", T0 + timedelta(seconds=3), reason="422")
    sim.add_entry(cid)
    sim.fill(cid)
    r.pass_once()
    kinds = [b["kind"] for b in blocks(j)]
    assert "conflict" in kinds and sim.positions == {}


def test_a_fill_above_the_limit_price_blocks(tmp_path):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid, avg="719.00")  # the limit is 718.84
    r.pass_once()
    assert [b["kind"] for b in blocks(j)] == ["fill_above_limit"]
    assert statuses(j, cid)[-1] == "filled"  # still recorded: the broker's truth


# ======================= --check, the lock =======================

def test_check_makes_no_write_calls_and_writes_nothing(tmp_path, capsys):
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    sim.fill(cid)
    sim.leg(cid, "stop")["status"] = "canceled"  # it would flatten
    sim.extra_open.append(stray())  # it would block
    before = j.path.read_bytes()
    clock.t = T0.replace(hour=20)  # the same day, after the close: the market is closed
    code = rc.main(["--check"], broker=r.broker, journal=j, clock=clock)
    assert code == 0
    assert sim.writes == []  # never sends, cancels or closes
    assert j.path.read_bytes() == before  # never writes the journal
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "check"
    assert any(w.startswith("journal") and "filled" in w for w in report["would"])
    assert any(w.startswith("flatten SPY") for w in report["would"])
    assert any("unreadable_order" in w for w in report["would"])


def test_check_cannot_reach_a_write_method_even_by_mistake():
    class Inner:
        def cancel_order(self, oid):
            raise AssertionError("reached")

        def list_positions(self):
            return []

    ro = rc.ReadOnlyBroker(Inner())
    for name in ("submit_bracket", "cancel_order", "close_position", "close_all"):
        with pytest.raises(AttributeError):
            getattr(ro, name)
    assert ro.list_positions() == []


def test_one_lock_wraps_every_broker_action(tmp_path):
    """Ivan's answer 3: the reconciler and a decision never act at once."""
    sim, j, clock, r, alerts = world(tmp_path)
    cid = sent(j)
    sim.add_entry(cid)
    held, release, done = threading.Event(), threading.Event(), threading.Event()

    def reconciler_holds_it():
        with ACTION_LOCK:
            held.set()
            release.wait(5)

    t = threading.Thread(target=reconciler_holds_it)
    t.start()
    held.wait(5)

    def a_decision_cancels():
        r.broker.cancel_order(sim.entries[cid]["id"])
        done.set()

    d = threading.Thread(target=a_decision_cancels)
    d.start()
    assert not done.wait(0.3)  # blocked while the other side holds the lock
    release.set()
    assert done.wait(5)
    t.join()
    d.join()
    # reads never wait for it
    with ACTION_LOCK:
        assert r.broker.list_positions() == []
