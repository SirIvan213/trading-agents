"""pipeline/probe_broker.py (#196): the paper-account probe Ivan runs during market hours.
A stateful fake broker; no network, no real broker."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

import pipeline.probe_broker as pb
from tools.broker import BrokerError, bracket_request
from tools.config import CONFIG_DIR, load_limits

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
NY = ZoneInfo("America/New_York")


class FakePaper:
    """A tiny paper account: open orders, positions, client_order_id lookups. Behaviour
    switches: the market clock, fills, and what Alpaca does with a resend or an early close."""

    def __init__(self, *, is_open=True, fill=True, dup_refused=True, close_with_legs_fails=True,
                 positions=None, orders=None, close_always_fails=False, minutes_left=120):
        self.is_open, self.fill, self.dup_refused = is_open, fill, dup_refused
        self.close_with_legs_fails, self.close_always_fails = close_with_legs_fails, \
            close_always_fails
        self.positions = dict(positions or {})  # symbol -> qty
        self.orders: dict[str, dict] = {o["id"]: o for o in (orders or [])}
        self.by_cid: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.n = 0
        self.minutes_left = minutes_left

    def verify_paper_account(self):
        self.calls.append(("verify",))
        return {"account_number": "PA0FAKE", "status": "ACTIVE"}

    def clock(self):
        now = datetime(2026, 11, 17, 14, 0, tzinfo=NY)
        return {"is_open": self.is_open, "timestamp": now.isoformat(),
                "next_open": (now + timedelta(days=1)).isoformat(),
                "next_close": (now + timedelta(minutes=self.minutes_left)).isoformat()}

    def list_positions(self):
        return [{"symbol": s, "qty": str(q), "side": "long"} for s, q in self.positions.items()]

    def list_open_orders(self):
        return [o for o in self.orders.values() if o["status"] in ("new", "held", "accepted")]

    def _leg(self, parent, kind):
        self.n += 1
        leg = {"id": f"leg-{self.n}", "symbol": parent["symbol"], "status": "held",
               "type": kind, "legs": []}
        return leg

    def submit_bracket(self, order, cid):
        bracket_request(order, cid)  # the real mapping must accept the probe order
        self.calls.append(("submit", cid))
        if cid in self.by_cid:
            if self.dup_refused:
                raise BrokerError("POST /v2/orders failed: HTTP 422", status=422, code=40010001)
        self.n += 1
        o = {"id": f"ord-{self.n}", "client_order_id": cid, "symbol": order["symbol"],
             "status": "accepted", "type": "limit", "legs": []}
        o["legs"] = [self._leg(o, "limit"), self._leg(o, "stop")]
        self.orders[o["id"]] = o
        self.by_cid[cid] = o["id"]
        if self.fill:
            o["status"] = "filled"
            self.positions[o["symbol"]] = self.positions.get(o["symbol"], 0) + 1
            for leg in o["legs"]:
                leg["status"] = "new"
                self.orders[leg["id"]] = leg
        return dict(o)

    def get_order_by_client_id(self, cid):
        self.calls.append(("lookup", cid))
        oid = self.by_cid.get(cid)
        return None if oid is None else dict(self.orders[oid])

    def cancel_order(self, oid):
        self.calls.append(("cancel", oid))
        o = self.orders.get(oid)
        if o is None:
            raise BrokerError(f"DELETE /v2/orders/{oid} failed: HTTP 404", status=404)
        o["status"] = "canceled"
        for leg in o.get("legs") or []:
            leg["status"] = "canceled"

    def close_position(self, symbol):
        self.calls.append(("close", symbol))
        legs_open = any(o["symbol"] == symbol for o in self.list_open_orders())
        if self.close_always_fails or (legs_open and self.close_with_legs_fails):
            raise BrokerError(f"DELETE /v2/positions/{symbol} failed: HTTP 403", status=403,
                              code=40310000)
        self.positions.pop(symbol, None)
        return {"id": "close-1", "symbol": symbol, "status": "accepted"}

    def check_close(self, symbol):
        self.calls.append(("check_close", symbol))
        working = [o for o in self.list_open_orders()
                   if o["symbol"] == symbol and o.get("order_class") == "simple"]
        state = ("closed" if symbol not in self.positions
                 else "closing" if working else "open")
        return {"state": state, "position": None, "close_orders": working}


def run(fake, *args):
    return pb.main(["--live", "--no-dotenv", *args], broker=fake, quote=lambda s: D("600.00"),
                   sleep=lambda s: None, monotonic=iter(range(10_000)).__next__)


def report(capsys):
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):])


# ======================= dry run and refusals =======================

class Tripwire:
    def __getattr__(self, name):
        raise AssertionError(f"the dry run touched the broker ({name})")


def test_dry_run_prints_the_plan_requests_nothing_and_exits_3(capsys):
    assert pb.main([], broker=Tripwire()) == pb.DRY_RUN
    out = capsys.readouterr().out
    assert "dry plan only, NOTHING was requested" in out and "1 share" in out
    assert "PAPER account only" in out and "--live" in out


def test_blocked_a_closed_market_refuses_before_any_order(capsys):
    fake = FakePaper(is_open=False)
    assert run(fake) == pb.REFUSED
    assert not [c for c in fake.calls if c[0] in ("submit", "close", "cancel")]
    assert "market is closed" in capsys.readouterr().err


@pytest.mark.parametrize("fake,why", [
    (FakePaper(positions={"SPY": 3}), "already holds SPY"),
    (FakePaper(orders=[{"id": "x", "symbol": "SPY", "status": "new", "legs": []}]),
     "open orders in SPY"),
], ids=["position", "open-order"])
def test_blocked_an_existing_position_or_order_in_the_symbol_refuses(capsys, fake, why):
    assert run(fake) == pb.REFUSED
    assert not [c for c in fake.calls if c[0] in ("submit", "close", "cancel")]
    assert why in capsys.readouterr().err


def test_blocked_limits_with_paper_false_refuse_before_any_request(monkeypatch, capsys):
    monkeypatch.setattr(pb, "load_limits", lambda: replace(LIMITS, paper=False))
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert pb.main(["--live", "--no-dotenv"]) == pb.REFUSED
    assert "paper only" in capsys.readouterr().err


def test_blocked_a_bad_symbol(capsys):
    assert pb.main(["--symbol", "S P Y"], broker=Tripwire()) == pb.REFUSED


# ======================= the probe =======================

def test_the_probe_order_is_a_valid_one_share_bracket():
    o = pb.probe_order("SPY", D("600.00"))
    assert o == {"symbol": "SPY", "side": "buy", "qty": 1, "type": "market",
                 "order_class": "bracket", "entry": "600.00", "stop_loss": "597.00",
                 "take_profit": "603.00", "slippage": "0.60"}
    req = bracket_request(o, "probe-x")
    assert req["limit_price"] == "600.60" and req["qty"] == "1"


def test_a_full_run_records_every_answer_and_ends_flat(capsys):
    fake = FakePaper()
    assert run(fake) == pb.OK
    r = report(capsys)
    assert r["1_unknown_lookup"]["result"] is None
    assert "HTTP 404" in r["1_unknown_lookup"]["meaning"]
    assert r["2_request"]["qty"] == "1" and r["2_sent"]["status"] == "filled"
    assert r["3_legs_shown"] is True
    assert r["4_resend"] == {"accepted": False, "error": "POST /v2/orders failed: HTTP 422",
                             "status": 422, "code": 40010001}
    assert r["5_entry_status"] == "filled" and len(r["5_legs_before_close"]) == 2
    assert r["5_position_before_close"]["qty"] == "1"
    assert len(r["5_open_orders_listing"]) == 2  # what the listing shows with legs open
    assert r["5_close_with_legs_open"]["closed"] is False
    assert r["5_close_with_legs_open"]["status"] == 403
    assert r["6_legs_gone"] is True and r["6_close_after_legs"]["closed"] is True
    assert r["cleanup"] == {"flat": True, "notes": []}
    assert fake.positions == {}


def test_an_accepted_resend_is_recorded_and_its_extra_order_cancelled(capsys):
    fake = FakePaper(dup_refused=False)
    assert run(fake) in (pb.OK, pb.INCOMPLETE)  # the lookup may now find the duplicate
    r = report(capsys)
    assert r["4_resend"]["accepted"] is True and r["4_resend_cancel"] == {"canceled": True}
    assert fake.positions == {}  # the duplicate's share was closed too


def test_an_entry_that_does_not_fill_skips_the_closing_checks_and_cleans_up(capsys):
    fake = FakePaper(fill=False)
    assert run(fake) == pb.INCOMPLETE  # flat, but not every step ran
    r = report(capsys)
    assert r["5_entry_status"] == "accepted" and "skipped" in r["5_note"]
    assert "5_close_with_legs_open" not in r
    assert r["cleanup"]["flat"] is True and not fake.list_open_orders()


def test_a_failure_mid_run_still_cleans_up(capsys):
    fake = FakePaper()
    real = fake.get_order_by_client_id
    calls = {"n": 0}

    def flaky(cid):
        calls["n"] += 1
        if calls["n"] == 3:  # the first poll of the step 5 fill wait fails
            raise BrokerError("GET /v2/orders:by_client_order_id failed: HTTP 500", status=500)
        return real(cid)

    fake.get_order_by_client_id = flaky
    assert run(fake) == pb.INCOMPLETE
    r = report(capsys)
    assert "HTTP 500" in r["stopped"] and r["cleanup"]["flat"] is True
    assert fake.positions == {}


def test_not_flat_after_cleanup_exits_1_with_an_alert(capsys):
    fake = FakePaper(close_always_fails=True)
    assert run(fake) == pb.NOT_FLAT
    captured = capsys.readouterr()
    assert "ALERT: the paper account is NOT flat in SPY" in captured.err
    r = json.loads(captured.out[captured.out.index("{"):])
    assert r["cleanup"]["flat"] is False and r["cleanup"]["notes"]


def test_the_probe_never_touches_another_symbol(capsys):
    other = {"id": "aapl-1", "symbol": "AAPL", "status": "new", "legs": []}
    fake = FakePaper(positions={"AAPL": 5}, orders=[other])
    assert run(fake) == pb.OK
    assert fake.positions == {"AAPL": 5} and fake.orders["aapl-1"]["status"] == "new"
    assert ("cancel", "aapl-1") not in fake.calls and ("close", "AAPL") not in fake.calls


def test_out_writes_the_report_as_json(tmp_path, capsys):
    out = tmp_path / "probe.json"
    assert run(FakePaper(), "--out", str(out)) == pb.OK
    assert json.loads(out.read_text(encoding="utf-8"))["cleanup"]["flat"] is True


# ======================= review fixes =======================

def test_blocked_the_probe_refuses_close_to_the_market_close(capsys):
    fake = FakePaper(minutes_left=9)
    assert run(fake) == pb.REFUSED
    assert "closes in 9 minutes" in capsys.readouterr().err
    assert not [c for c in fake.calls if c[0] in ("submit", "close", "cancel")]


def test_ctrl_c_during_the_run_still_cleans_up(capsys):
    fake = FakePaper()
    real = fake.get_order_by_client_id
    calls = {"n": 0}

    def interrupt(cid):
        calls["n"] += 1
        if calls["n"] == 3:  # during the fill wait
            raise KeyboardInterrupt
        return real(cid)

    fake.get_order_by_client_id = interrupt
    assert run(fake) == pb.INCOMPLETE
    r = report(capsys)
    assert "Ctrl-C" in r["stopped"] and r["cleanup"]["flat"] is True
    assert fake.positions == {}


def test_a_transient_error_in_cleanup_is_retried(capsys):
    """The run stops before step 6 with a share and its legs open; the cleanup's first
    listing and first close then fail once (503, 500). The next pass gets it flat."""
    fake = FakePaper()
    real_run, real_list, real_close = pb.Probe.run, fake.list_open_orders, fake.close_position
    fail = {"list": 1, "close": 1}

    def stop_after_fill(self):
        self.report["stopped"] = "simulated stop"  # the share and its legs stay open
        self.b.submit_bracket(pb.probe_order("SPY", D("600.00")), self.cid)

    def flaky_list():
        if fail["list"]:
            fail["list"] -= 1
            raise BrokerError("GET /v2/orders failed: HTTP 503", status=503)
        return real_list()

    def flaky_close(symbol):
        if fail["close"]:
            fail["close"] -= 1
            raise BrokerError("DELETE /v2/positions/SPY failed: HTTP 500", status=500)
        return real_close(symbol)

    pb.Probe.run = stop_after_fill
    try:
        fake.list_open_orders = flaky_list
        fake.close_position = flaky_close
        fail["list"], fail["close"] = 0, 0
        # arm the failures only once the cleanup starts
        real_cleanup = pb.Probe.cleanup

        def armed(self):
            fail["list"], fail["close"] = 1, 1
            return real_cleanup(self)

        pb.Probe.cleanup = armed
        try:
            assert run(fake) == pb.INCOMPLETE
        finally:
            pb.Probe.cleanup = real_cleanup
    finally:
        pb.Probe.run = real_run
    r = report(capsys)
    assert r["cleanup"]["flat"] is True and fake.positions == {}
    assert any("HTTP 503" in n for n in r["cleanup"]["notes"])
    assert any("HTTP 500" in n for n in r["cleanup"]["notes"])


def test_a_close_that_succeeds_with_the_legs_open_cancels_the_legs_at_once(capsys):
    fake = FakePaper(close_with_legs_fails=False)
    assert run(fake) == pb.OK
    r = report(capsys)
    assert r["5_close_with_legs_open"]["closed"] is True
    assert all(v == {"canceled": True} for k, v in r.items() if k.startswith("6_cancel_leg"))
    assert r["6_close_after_legs"] == "no position left" and fake.positions == {}


def test_a_short_left_behind_is_closed_by_the_cleanup(capsys):
    fake = FakePaper()
    real_run = pb.Probe.run

    def run_then_short(self):
        real_run(self)
        fake.positions["SPY"] = -1  # a leg sold after the close: a 1-share short

    pb.Probe.run = run_then_short
    try:
        assert run(fake) == pb.OK
    finally:
        pb.Probe.run = real_run
    assert report(capsys)["cleanup"]["flat"] is True and fake.positions == {}


def test_a_resend_error_without_an_http_status_is_unknown_not_refused(capsys):
    fake = FakePaper()
    real = fake.submit_bracket
    sent = {"n": 0}

    def submit(order, cid):
        sent["n"] += 1
        if sent["n"] == 2:
            raise BrokerError("broker reply does not match the order sent")
        return real(order, cid)

    fake.submit_bracket = submit
    run(fake)
    assert report(capsys)["4_resend"]["accepted"] == "unknown"


NOW_UTC = datetime(2026, 11, 17, 19, 0, tzinfo=UTC)


def snap(ap, bp, age_s=1):
    return {"SPY": {"latestQuote": {"ap": ap, "bp": bp,
                                    "t": (NOW_UTC - timedelta(seconds=age_s)).isoformat()}}}


@pytest.mark.parametrize("reply,ok", [
    (snap(600.01, 599.99), True),
    (snap(600.01, 599.99, age_s=60), False),
    (snap(599.0, 600.0), False),
    (snap(0.5, 0.4), False),
    (snap(True, 599.0), False),
    (snap("abc", 599.0), False),
    ({"SPY": {"latestQuote": {}}}, False),
    ({"SPY": None}, False),
], ids=["fresh", "stale", "crossed", "penny", "bool", "text", "empty", "none"])
def test_live_quote_must_be_fresh_uncrossed_and_at_least_a_dollar(monkeypatch, reply, ok):
    monkeypatch.setattr(pb, "fetch_snapshots", lambda symbols: reply)
    if ok:
        assert pb.live_quote("SPY", now=lambda: NOW_UTC) == D("600.01")
    else:
        with pytest.raises(BrokerError):
            pb.live_quote("SPY", now=lambda: NOW_UTC)


def test_the_recording_opener_keeps_raw_replies_and_redacts_the_account_number():
    import io as _io

    class R:
        status = 200

        def __init__(self, body):
            self.b = body

        def read(self, n=None):
            return self.b

        def geturl(self):
            return "https://paper-api.alpaca.markets/v2/account"

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = json.dumps({"id": "acct-uuid-1", "account_number": "PA123",
                       "status": "ACTIVE"}).encode()

    def inner(req, timeout=None):
        if req.full_url.endswith("/v2/orders"):
            raise urllib.error.HTTPError(req.full_url, 422, "x", {},
                                         _io.BytesIO(b'{"code": 42210000}'))
        return R(body)

    rec = pb.RecordingOpener(inner)
    with rec(urllib.request.Request("https://paper-api.alpaca.markets/v2/account")) as r:
        assert r.read() == body
    with pytest.raises(urllib.error.HTTPError) as exc:
        rec(urllib.request.Request("https://paper-api.alpaca.markets/v2/orders", data=b"{}"))
    assert exc.value.read() == b'{"code": 42210000}'  # the broker can still read it
    assert rec.replies == [
        {"method": "GET", "path": "/v2/account", "status": 200,
         "body": {"id": "REDACTED", "account_number": "REDACTED", "status": "ACTIVE"}},
        {"method": "POST", "path": "/v2/orders", "status": 422, "body": {"code": 42210000}},
    ]


def test_the_report_never_shows_the_account_number(capsys):
    run(FakePaper())
    assert report(capsys)["account"]["account_number"] == "REDACTED"


# ======================= the paper probe's findings (#196, 2026-11-23) =======================

def unclear(symbol):
    """The close was placed (and fills) but its reply could not be read (#196 finding 7)."""
    return BrokerError(f"DELETE /v2/positions/{symbol}: broker reply has no order_class; "
                       "the close may have been placed", status=200, may_have_acted=True)


def test_a_step_error_makes_the_run_incomplete_even_when_flat(capsys):
    """#196 finding 8: step 6 errored and the probe still exited 0."""
    fake = FakePaper()
    real_close = fake.close_position
    n = {"close": 0}

    def close(symbol):
        n["close"] += 1
        if n["close"] == 2:  # step 6: the close is placed, its reply unreadable
            real_close(symbol)
            raise unclear(symbol)
        return real_close(symbol)

    fake.close_position = close
    assert run(fake) == pb.INCOMPLETE
    captured = capsys.readouterr()
    r = json.loads(captured.out[captured.out.index("{"):])
    assert r["cleanup"]["flat"] is True and fake.positions == {}
    assert any(e.startswith("6_close_after_legs:") for e in r["errors"])
    assert "incomplete" in captured.err


def test_an_unclear_close_is_checked_and_never_sent_again_in_that_step(capsys):
    fake = FakePaper()
    real_close = fake.close_position
    n = {"close": 0}

    def close(symbol):
        n["close"] += 1
        if n["close"] == 2:
            real_close(symbol)  # it worked at the broker
            raise unclear(symbol)
        return real_close(symbol)

    fake.close_position = close
    run(fake)
    r = report(capsys)
    assert r["6_close_after_legs"]["check"]["state"] == "closed"
    closes = [c for c in fake.calls if c[0] == "close"]
    assert len(closes) == 2  # step 5 (refused, legs open) and step 6: never a blind third
    assert ("check_close", "SPY") in fake.calls


def test_an_unclear_close_that_did_not_take_is_left_to_the_cleanup_after_a_check(capsys):
    fake = FakePaper()
    real_close = fake.close_position
    n = {"close": 0}

    def close(symbol):
        n["close"] += 1
        if n["close"] == 2:
            raise unclear(symbol)  # nothing happened at the broker
        return real_close(symbol)

    fake.close_position = close
    assert run(fake) == pb.INCOMPLETE
    r = report(capsys)
    assert r["6_close_after_legs"]["check"]["state"] == "open"
    assert r["cleanup"]["flat"] is True and fake.positions == {}  # the cleanup closed it


def test_a_definite_refusal_is_a_finding_not_an_error(capsys):
    """The 403 on closing with the legs open and the 422 on the resend are what the probe
    exists to record: a clean run with both still exits 0."""
    assert run(FakePaper()) == pb.OK
    r = report(capsys)
    assert r["errors"] == []
    assert r["4_resend"]["status"] == 422 and r["5_close_with_legs_open"]["status"] == 403


def test_a_failed_unknown_lookup_or_leg_cancel_is_an_error(capsys):
    fake = FakePaper()
    real_lookup = fake.get_order_by_client_id

    def lookup(cid):
        if cid.startswith("probe-unknown-"):
            raise BrokerError("GET /v2/orders:by_client_order_id failed: HTTP 500", status=500,
                              may_have_acted=True)
        return real_lookup(cid)

    fake.get_order_by_client_id = lookup
    assert run(fake) == pb.INCOMPLETE
    assert report(capsys)["errors"][0].startswith("1_unknown_lookup:")


def test_a_resend_that_may_have_acted_is_unknown_and_an_error(capsys):
    fake = FakePaper()
    real = fake.submit_bracket
    sent = {"n": 0}

    def submit(order, cid):
        sent["n"] += 1
        if sent["n"] == 2:
            raise BrokerError("POST /v2/orders failed: HTTP 503", status=503,
                              may_have_acted=True)
        return real(order, cid)

    fake.submit_bracket = submit
    assert run(fake) == pb.INCOMPLETE
    r = report(capsys)
    assert r["4_resend"]["accepted"] == "unknown"
    assert any(e.startswith("4_resend:") for e in r["errors"])


def test_a_cleanup_error_makes_the_run_incomplete(capsys):
    fake = FakePaper()
    real_run = pb.Probe.run
    real_list = fake.list_open_orders
    fail = {"n": 0}

    def flaky_list():
        if fail["n"]:
            fail["n"] -= 1
            raise BrokerError("GET /v2/orders failed: HTTP 503", status=503,
                              may_have_acted=True)
        return real_list()

    def run_then_arm(self):
        real_run(self)
        fail["n"] = 1  # the cleanup's first listing fails (#196: it did, on the parser)

    fake.list_open_orders = flaky_list
    pb.Probe.run = run_then_arm
    try:
        assert run(fake) == pb.INCOMPLETE
    finally:
        pb.Probe.run = real_run
    r = report(capsys)
    assert r["errors"] == [] and r["cleanup"]["flat"] is True
    assert any("HTTP 503" in x for x in r["cleanup"]["notes"])


def test_the_cleanup_finds_a_held_stop_the_listing_does_not_show(capsys):
    """#196 finding 5: the open-orders listing shows the take-profit but not the held stop.
    The cleanup cancels the entry's legs found by lookup."""
    fake = FakePaper()
    real_list = fake.list_open_orders

    def listing_without_held():
        return [o for o in real_list() if o["status"] != "held"]

    def stop_after_fill(self):
        self.report["stopped"] = "simulated stop"
        self.b.submit_bracket(pb.probe_order("SPY", D("600.00")), self.cid)
        entry = self.b.get_order_by_client_id(self.cid)
        entry_legs = fake.orders[entry["id"]]["legs"]
        next(leg for leg in entry_legs if leg["type"] == "stop")["status"] = "held"

    fake.list_open_orders = listing_without_held
    real_run = pb.Probe.run
    pb.Probe.run = stop_after_fill
    try:
        run(fake)
    finally:
        pb.Probe.run = real_run
    stop = next(o for o in fake.orders.values() if o.get("type") == "stop")
    assert stop["status"] == "canceled"
    assert ("cancel", stop["id"]) in fake.calls
    assert report(capsys)["cleanup"]["flat"] is True


def test_the_cleanup_leaves_a_working_close_alone_and_waits(capsys):
    fake = FakePaper()
    real_run = pb.Probe.run

    def run_with_a_close_working(self):
        real_run(self)
        fake.positions["SPY"] = 1  # a close is working: it fills on the next position read
        fake.orders["close-9"] = {"id": "close-9", "symbol": "SPY", "status": "new",
                                  "side": "sell", "order_class": "simple", "type": "market",
                                  "legs": []}

    real_positions = fake.list_positions
    reads = {"n": 0}

    def positions():
        reads["n"] += 1
        if "close-9" in fake.orders and reads["n"] > 1:
            fake.orders["close-9"]["status"] = "filled"
            fake.positions.pop("SPY", None)
        return real_positions()

    fake.list_positions = positions
    pb.Probe.run = run_with_a_close_working
    try:
        run(fake)
    finally:
        pb.Probe.run = real_run
    assert ("cancel", "close-9") not in fake.calls
    # step 5 (refused, legs open) and step 6 only: no close while one is working
    assert [c for c in fake.calls if c[0] == "close"] == [("close", "SPY")] * 2
    assert report(capsys)["cleanup"]["flat"] is True


# ======================= review round 1 =======================

@pytest.mark.parametrize("status, code", [(429, None), (401, 40110000), (422, 42210000)])
def test_an_unexpected_answer_to_the_resend_is_an_error_not_a_finding(capsys, status, code):
    fake = FakePaper()
    real = fake.submit_bracket
    sent = {"n": 0}

    def submit(order, cid):
        sent["n"] += 1
        if sent["n"] == 2:
            raise BrokerError(f"POST /v2/orders failed: HTTP {status}", status=status,
                              code=code, may_have_acted=status == 429)
        return real(order, cid)

    fake.submit_bracket = submit
    assert run(fake) == pb.INCOMPLETE
    assert any(e.startswith("4_resend:") for e in report(capsys)["errors"])


@pytest.mark.parametrize("status, code", [(403, 40310001), (429, None), (404, 40410000)])
def test_an_unexpected_answer_to_the_close_with_legs_is_an_error(capsys, status, code):
    fake = FakePaper()
    real_close = fake.close_position
    n = {"close": 0}

    def close(symbol):
        n["close"] += 1
        if n["close"] == 1:
            raise BrokerError(f"DELETE /v2/positions/{symbol} failed: HTTP {status}",
                              status=status, code=code, may_have_acted=status == 429)
        return real_close(symbol)

    fake.close_position = close
    assert run(fake) == pb.INCOMPLETE
    assert any(e.startswith("5_close_with_legs_open:") for e in report(capsys)["errors"])


def test_step_6_does_not_close_again_while_step_5s_close_is_working(capsys):
    """Step 5's close was unclear and check_close found it working: step 6 never closes."""
    fake = FakePaper()
    n = {"close": 0}

    def close(symbol):
        n["close"] += 1
        if n["close"] == 1:  # placed, reply unreadable, still working
            fake.orders["close-7"] = {"id": "close-7", "symbol": symbol, "status": "new",
                                      "side": "sell", "order_class": "simple",
                                      "type": "market", "legs": []}
            raise unclear(symbol)
        raise AssertionError("closed again while a close was working")

    real_positions = fake.list_positions
    reads = {"n": 0}

    def positions():
        reads["n"] += 1
        if reads["n"] > 6 and "close-7" in fake.orders:  # it fills later
            fake.orders["close-7"]["status"] = "filled"
            fake.positions.pop("SPY", None)
        return real_positions()

    fake.close_position = close
    fake.list_positions = positions
    run(fake)
    r = report(capsys)
    assert r["5_close_with_legs_open"]["check"]["state"] == "closing"
    assert r["6_close_after_legs"] in ("a close is already working: not closed again",
                                       "no position left")
    assert n["close"] == 1 and r["cleanup"]["flat"] is True
