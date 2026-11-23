"""Sending an approved order to the paper broker (Phase 5 PR 3: #187, #136, #148).

Offline: a sealed ledger, a tmp_path journal, a fake clock and a fake paper broker (or the real
tools.broker client behind a fake opener). No network, no billed call.
"""
import json
import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

import pipeline.order_flow as of
from journal.log import Journal, JournalError, JournalLockTimeout
from journal.orders import APPROVED, client_order_id
from pipeline.order_flow import approval_prompt, approve, propose, render_ticket
from pipeline.send import ALREADY_SENT, NOT_SENT, send_approved
from tests.order_helpers import (
    AS_OF,
    BROKER_FIXTURES,
    T0,
    FakePaperBroker,
    StepClock,
    approved_entry,
    closed_trade,
    events_clear,
    quotes_at,
    raw_lines,
    sealed_ledger,
    sent_order,
)
from tools.broker import AlpacaPaperBroker, BrokerError, bracket_request
from tools.config import CONFIG_DIR, load_limits

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
T1 = T0 + timedelta(seconds=2)  # sent two seconds after the approval
EXPIRY = AS_OF + timedelta(seconds=LIMITS.max_plan_age_s)  # the plan's expiry


@pytest.fixture(autouse=True)
def fixed_limits(monkeypatch):
    """The example limits, whatever config/limits.json holds on this machine."""
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "decisions.jsonl")


@pytest.fixture
def ledger():
    return sealed_ledger()


def at(t):
    return lambda: t


def yes(_prompt):
    return "yes"


def approved(ledger, journal, t=T0):
    p = propose(ledger, "plan-1", journal=journal, clock=at(t), quotes=quotes_at(t),
                events=events_clear())
    assert p.ok, p.reason
    out = approve(p, ledger, journal=journal, ask=yes, clock=at(t), quotes=quotes_at(t),
                  events=events_clear(), sends=True)
    assert out.status == APPROVED, out.reason
    return out


def cid_of(out):
    return client_order_id(out.recheck.run_id, out.recheck.plan_id)


def statuses(journal):
    return [e["status"] for e in raw_lines(journal)]


def send(out, journal, broker, t=T1):
    return send_approved(out, journal=journal, broker=broker, clock=at(t))


# ===================== the happy path =====================

def test_an_approved_order_is_sent_once_with_a_write_ahead_submitting(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    r = send(out, journal, broker)
    cid = cid_of(out)
    assert r.status == "submitted" and r.broker_order_id == "ord-1" and not r.alert
    assert r.client_order_id == cid and r.verdict == {"verdict": "PASS", "reasons": []}
    assert broker.calls == [("verify",), ("submit", cid)]
    lines = raw_lines(journal)
    assert [e["status"] for e in lines] == [APPROVED, "submitting", "submitted"]
    assert lines[1]["limit_price"] == "364.55" and lines[1]["at"] == T1.isoformat()
    assert lines[2]["broker_order_id"] == "ord-1" and lines[2]["broker_status"] == "accepted"
    state = journal.day_state(T1)
    assert state.trades_today == 1 and state.open_positions == 1
    assert state.risk_used_today == D("49.84")


def test_the_request_is_a_limit_at_entry_plus_allowance_and_nothing_internal(
        ledger, journal, monkeypatch):
    """The real paper client behind a fake opener: what is POSTed is bracket_request (#136)."""
    monkeypatch.setenv("ALPACA_API_KEY", "SENTINEL-KEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SENTINEL-SECRET")
    out = approved(ledger, journal)
    cid = cid_of(out)
    posted = []
    account = (BROKER_FIXTURES / "account.json").read_bytes()
    reply = json.loads((BROKER_FIXTURES / "order_bracket_new.json").read_text(encoding="utf-8"))
    reply["client_order_id"] = cid

    class Resp:
        status = 200

        def __init__(self, body, url):
            self.b, self.u = body, url

        def read(self, n=None):
            return self.b

        def geturl(self):
            return self.u

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        if req.full_url.endswith("/v2/account"):
            return Resp(account, req.full_url)
        assert req.get_method() == "POST" and req.full_url.endswith("/v2/orders")
        posted.append(json.loads(req.data))
        return Resp(json.dumps(reply).encode(), req.full_url)

    r = send(out, journal, AlpacaPaperBroker(LIMITS, opener=opener))
    assert r.status == "submitted", r.reason
    assert posted == [bracket_request(out.recheck.order, cid)]
    body = posted[0]
    assert body["type"] == "limit" and body["limit_price"] == "364.55"  # 364.18 + 0.37
    assert body["stop_loss"] == {"stop_price": "358.32"}
    assert body["take_profit"] == {"limit_price": "370.04"}
    assert not {"slippage", "entry"} & set(body)


# ===================== never resent =====================

def test_a_second_send_of_the_same_approval_sends_nothing(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    assert send(out, journal, broker).status == "submitted"
    again = send(out, journal, broker, T1 + timedelta(seconds=1))
    assert again.status == ALREADY_SENT and "already sent (now submitted)" in again.reason
    assert broker.names().count("submit") == 1
    assert statuses(journal) == [APPROVED, "submitting", "submitted"]


def test_an_outcome_that_is_not_an_approval_sends_nothing(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    for bad in (replace(out, status="declined"), replace(out, recheck=None)):
        r = send(bad, journal, broker)
        assert r.status == NOT_SENT and r.client_order_id is None
    assert broker.calls == [] and statuses(journal) == [APPROVED]


# ===================== the re-check under the lock (#187) =====================

def blocked(journal, r, why):
    """Not sent: the approval is journaled expired (zero fill), never reached submitting."""
    assert r.status == NOT_SENT and why in r.reason, r.reason
    last = raw_lines(journal)[-1]
    assert last["status"] == "expired" and last["filled_qty"] == 0
    assert last["client_order_id"] == r.client_order_id
    assert last["reason"].startswith("not sent: ")
    own = journal.orders()[r.client_order_id]
    assert own.status == "expired" and not own.sent and own.released


def test_max_trades_reached_by_another_process_after_the_approval_blocks_the_send(
        ledger, journal):
    out = approved(ledger, journal)
    for _ in range(LIMITS.max_trades_per_day):  # three trades sent and closed meanwhile
        for e in closed_trade(T0 - timedelta(hours=1)):
            journal.append(e)
    broker = FakePaperBroker()
    r = send(out, journal, broker)
    blocked(journal, r, "trades today")
    assert broker.names() == ["verify"]
    assert journal.day_state(T1).trades_today == 3  # the unsent order took no slot


def test_a_position_opened_by_another_process_blocks_the_send(ledger, journal):
    out = approved(ledger, journal)
    for e in sent_order(T0 + timedelta(seconds=1), symbol="AAPL"):
        journal.append(e)
    broker = FakePaperBroker()
    r = send(out, journal, broker)
    blocked(journal, r, "open position")
    assert "submit" not in broker.names()


def test_the_daily_loss_stop_hit_after_the_approval_blocks_the_send(ledger, journal):
    out = approved(ledger, journal)
    for e in closed_trade(T0 - timedelta(hours=1), "100.00", pnl="-150.00",
                          closed_by="flatten", symbol="AAPL"):
        journal.append(e)
    r = send(out, journal, FakePaperBroker())
    blocked(journal, r, "loss")


def test_the_order_itself_is_left_out_of_the_day_it_is_checked_against(ledger, journal):
    """Its own approval would fill max_open_positions (1): day_state(exclude=its id)."""
    out = approved(ledger, journal)
    assert journal.day_state(T1).open_positions == 1  # the live approval itself
    assert journal.day_state(T1, exclude=cid_of(out)).open_positions == 0
    assert send(out, journal, FakePaperBroker()).status == "submitted"


def test_the_kill_switch_flipped_after_the_approval_blocks_the_send(ledger, journal, monkeypatch):
    out = approved(ledger, journal)
    monkeypatch.setattr(of, "load_checked_limits",
                        lambda *a, **k: replace(LIMITS, trading_enabled=False))
    r = send(out, journal, FakePaperBroker())
    blocked(journal, r, "kill switch")


def test_an_invalid_limits_config_at_sending_blocks_it(ledger, journal, monkeypatch):
    out = approved(ledger, journal)

    def broken(*a, **k):
        raise ValueError("bad config")

    monkeypatch.setattr(of, "load_checked_limits", broken)
    blocked(journal, send(out, journal, FakePaperBroker()), "limits config is invalid")


def test_the_approvals_event_gate_is_the_one_checked(ledger, journal):
    out = approved(ledger, journal)
    gone = replace(out, recheck=replace(out.recheck, gate=None))
    blocked(journal, send(gone, journal, FakePaperBroker()), "event gate result missing")


def test_the_market_closed_by_the_time_of_sending_blocks_it(ledger, journal):
    out = approved(ledger, journal)
    late = T0.replace(hour=16, minute=1)
    r = send(out, journal, FakePaperBroker(), late)
    assert r.status == NOT_SENT  # the plan has expired by then too
    assert "submitting" not in statuses(journal)


def test_an_approval_past_its_plan_expiry_is_never_sent(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    r = send(out, journal, broker, EXPIRY)
    assert r.status == NOT_SENT and "plan expired before it was sent" in r.reason
    assert raw_lines(journal)[-1]["status"] == "expired"
    assert "submit" not in broker.names()
    state = journal.day_state(EXPIRY)
    assert state.open_positions == 0 and state.trades_today == 0


def test_a_send_one_second_before_expiry_goes_out(ledger, journal):
    out = approved(ledger, journal)
    assert send(out, journal, FakePaperBroker(), EXPIRY - timedelta(seconds=1)).status == \
        "submitted"


def test_a_journal_refusal_of_submitting_means_nothing_is_sent(ledger, journal, monkeypatch):
    out = approved(ledger, journal)
    real = journal.record_order_event

    def refuse_submitting(cid, status, at_, **f):
        if status == "submitting":
            raise JournalError("order event refused: its plan expired before it was sent")
        return real(cid, status, at_, **f)

    monkeypatch.setattr(journal, "record_order_event", refuse_submitting)
    broker = FakePaperBroker()
    r = send(out, journal, broker)
    assert r.status == NOT_SENT and "nothing sent" in r.reason
    assert "submit" not in broker.names() and statuses(journal) == [APPROVED]


def test_a_journal_lock_timeout_before_sending_sends_nothing(ledger, journal, monkeypatch):
    out = approved(ledger, journal)
    monkeypatch.setattr("journal.log.LOCK_TIMEOUT_S", 0.2)
    broker = FakePaperBroker()
    held, release = threading.Event(), threading.Event()

    def hold():
        with journal.exclusive():
            held.set()
            release.wait(5)

    th = threading.Thread(target=hold)
    th.start()
    held.wait(5)
    try:
        r = send(out, journal, broker)
    finally:
        release.set()
        th.join()
    assert r.status == NOT_SENT and "nothing sent" in r.reason
    assert "submit" not in broker.names()


def test_a_corrupt_journal_at_sending_sends_nothing(ledger, journal):
    out = approved(ledger, journal)
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write('{"status": "submitting", "client_order_id": "ta-unknown", "at": "x"}\n')
    broker = FakePaperBroker()
    r = send(out, journal, broker)
    assert r.status == NOT_SENT and "submit" not in broker.names()


# ===================== before the lock: the request and the account =====================

def test_a_failed_paper_account_check_sends_nothing(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    broker.verify = BrokerError("paper account is blocked")
    r = send(out, journal, broker)
    assert r.status == NOT_SENT and "paper account check failed" in r.reason
    assert broker.names() == ["verify"]
    assert raw_lines(journal)[-1]["status"] == "expired"  # never sendable later


def test_an_order_the_broker_request_cannot_carry_is_never_sent(ledger, journal):
    out = approved(ledger, journal)
    bad = dict(out.recheck.order, slippage="5.86")  # the limit would reach the target
    sized = dict(out.recheck.sized, order=bad)
    broken = replace(out, recheck=replace(out.recheck, sized=sized))
    broker = FakePaperBroker()
    r = send(broken, journal, broker)
    assert r.status == NOT_SENT and "broker request cannot be built" in r.reason
    assert broker.calls == []


def run_in_other_thread(journal):
    """A hook that, inside a broker call, takes the journal lock from another thread."""
    seen = []

    def hook(name):
        got = []

        def take():
            try:
                with journal.exclusive(timeout_s=0.5):
                    got.append(True)
            except JournalLockTimeout:
                got.append(False)

        th = threading.Thread(target=take)
        th.start()
        th.join()
        seen.append((name, got[0]))
    return seen, hook


def test_no_broker_call_holds_the_journal_lock(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    broker.submit = BrokerError("POST /v2/orders timed out")
    seen, broker.during_call = run_in_other_thread(journal)
    send(out, journal, broker)
    assert seen == [("verify", True), ("submit", True), ("lookup", True)]


# ===================== after the POST: rejected, found, unknown =====================

def test_a_definite_refusal_with_no_order_found_is_rejected(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    broker.submit = BrokerError("POST /v2/orders failed: HTTP 403", status=403, code=40310000)
    r = send(out, journal, broker)
    assert r.status == "rejected" and not r.alert
    assert broker.names() == ["verify", "submit", "lookup"]  # looked up, never resent
    last = raw_lines(journal)[-1]
    assert last["status"] == "rejected" and last["http_status"] == 403
    assert last["error_code"] == 40310000
    state = journal.day_state(T1)
    assert state.trades_today == 1  # it was sent: it took the slot
    assert state.open_positions == 0 and state.risk_used_today == 0  # a confirmed zero fill


def test_a_definite_refusal_but_the_order_exists_is_submitted(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    broker.submit = BrokerError("POST /v2/orders failed: HTTP 422", status=422)
    broker.hold_on_failure = True
    r = send(out, journal, broker)
    assert r.status == "submitted" and r.broker_order_id == "ord-1"
    assert "broker holds the order" in r.reason
    assert statuses(journal)[-1] == "submitted"


@pytest.mark.parametrize("exc", [
    BrokerError("POST /v2/orders timed out"),
    BrokerError("POST /v2/orders failed: network error (ConnectionResetError)"),
    BrokerError("POST /v2/orders failed: HTTP 503", status=503),
    BrokerError("POST /v2/orders failed: HTTP 429", status=429),
    RuntimeError("a bug in the client"),
], ids=["timeout", "network", "503", "429", "other"])
def test_an_ambiguous_failure_with_the_order_found_is_submitted(ledger, journal, exc):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    broker.submit, broker.hold_on_failure = exc, True
    r = send(out, journal, broker)
    assert r.status == "submitted" and r.broker_order_id == "ord-1" and not r.alert
    assert broker.names().count("submit") == 1


@pytest.mark.parametrize("exc", [
    BrokerError("POST /v2/orders timed out"),
    BrokerError("POST /v2/orders failed: HTTP 503", status=503),
    BrokerError("POST /v2/orders failed: HTTP 429", status=429),
    BrokerError("POST /v2/orders failed: HTTP 408", status=408),
], ids=["timeout", "503", "429", "408"])
def test_an_ambiguous_failure_with_nothing_found_is_send_unknown_and_alerts(
        ledger, journal, capsys, exc):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    broker.submit = exc
    r = send(out, journal, broker)
    assert r.status == "send_unknown" and r.alert
    assert "ALERT" in capsys.readouterr().err
    assert broker.names() == ["verify", "submit", "lookup"]
    assert statuses(journal) == [APPROVED, "submitting", "send_unknown"]
    state = journal.day_state(T1)  # the broker may hold it: slot and risk stay counted
    assert state.open_positions == 1 and state.risk_used_today == D("49.84")


def test_a_failed_lookup_after_a_failed_post_is_send_unknown(ledger, journal, capsys):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    broker.submit = BrokerError("POST /v2/orders failed: HTTP 403", status=403)
    broker.lookup = BrokerError("GET failed: HTTP 500", status=500)
    r = send(out, journal, broker)
    assert r.status == "send_unknown" and r.alert  # a refusal is not trusted unconfirmed
    assert "lookup by client_order_id failed" in r.reason
    assert "ALERT" in capsys.readouterr().err


def test_a_reply_that_does_not_match_is_cancelled_and_alerts(ledger, journal, capsys):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    body = bracket_request(out.recheck.order, cid_of(out))
    from tests.order_helpers import broker_reply
    wrong = broker_reply(body, qty="9")
    broker.submit, broker.lookup = wrong, wrong
    r = send(out, journal, broker)
    assert r.status == "send_unknown" and r.alert and "cancel requested" in r.reason
    assert ("cancel", "ord-1") in broker.calls
    assert broker.names().count("submit") == 1
    assert statuses(journal)[-1] == "send_unknown"
    assert "ALERT" in capsys.readouterr().err


def test_a_mismatch_whose_cancel_fails_says_so(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    body = bracket_request(out.recheck.order, cid_of(out))
    from tests.order_helpers import broker_reply
    wrong = broker_reply(body, limit_price="365.00")
    broker.submit, broker.lookup = wrong, wrong
    broker.cancel = BrokerError("DELETE failed: HTTP 500", status=500)
    r = send(out, journal, broker)
    assert r.status == "send_unknown" and "cancel FAILED" in r.reason


def test_a_submitted_event_that_cannot_be_journaled_alerts_and_keeps_submitting(
        ledger, journal, monkeypatch, capsys):
    out = approved(ledger, journal)
    real = journal.record_order_event

    def fail_after_send(cid, status, at_, **f):
        if status == "submitted":
            raise JournalError("disk full")
        return real(cid, status, at_, **f)

    monkeypatch.setattr(journal, "record_order_event", fail_after_send)
    r = send(out, journal, FakePaperBroker())
    assert r.status == "submitted" and r.alert and not r.journaled
    assert "could NOT be journaled" in r.reason and "ALERT" in capsys.readouterr().err
    assert statuses(journal) == [APPROVED, "submitting"]
    assert journal.day_state(T1).open_positions == 1  # still counted


def test_a_post_send_journal_write_is_retried_once(ledger, journal, monkeypatch):
    out = approved(ledger, journal)
    real = journal.record_order_event
    fails = {"n": 0}

    def flaky(cid, status, at_, **f):
        if status == "submitted" and fails["n"] == 0:
            fails["n"] += 1
            raise JournalError("lock timed out")
        return real(cid, status, at_, **f)

    monkeypatch.setattr(journal, "record_order_event", flaky)
    r = send(out, journal, FakePaperBroker())
    assert r.status == "submitted" and r.journaled and not r.alert
    assert statuses(journal)[-1] == "submitted"


def test_other_orders_in_the_journal_are_untouched(ledger, journal):
    other = approved_entry(T0 - timedelta(hours=2), symbol="AAPL")  # long expired
    journal.append(other)
    out = approved(ledger, journal)
    assert send(out, journal, FakePaperBroker()).status == "submitted"
    assert journal.orders()[other["client_order_id"]].status == "expired"  # by propose


# ===================== the ticket shows the limit price (#136) =====================

def test_the_ticket_shows_the_limit_price_and_says_it_is_sent(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=at(T0), quotes=quotes_at(T0),
                events=events_clear())
    sent = render_ticket(p, sends=True)
    assert sent.splitlines()[0] == ("ORDER TICKET (Alpaca PAPER account; an approved order is "
                                    "SENT to the paper broker after a final risk re-check)")
    assert ("Buy 8 shares of TSLA with a LIMIT order at 364.55 (planned entry 364.18 + "
            "slippage allowance 0.37)") in sent
    assert "fills at that price or lower" in sent
    # honest until the reconciler's 60 s cancel exists (PR 4)
    assert "Nothing cancels an unfilled entry yet: it stays working until the close." in sent
    assert "at market" not in sent and "NOT sent" not in sent
    kept = render_ticket(p)
    assert "NOT sent to a broker" in kept and "LIMIT order at 364.55" in kept
    prompt = approval_prompt(p, sends=True)
    assert "approve and SEND to the paper account" in prompt and "LIMIT 364.55" in prompt
    assert "LIMIT 364.55" in approval_prompt(p) and "SEND" not in approval_prompt(p)


def test_the_ticket_limit_is_exactly_the_price_the_request_carries(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=at(T0), quotes=quotes_at(T0),
                events=events_clear())
    body = bracket_request(p.order, client_order_id(p.run_id, p.plan_id))
    assert of.limit_price(p) == body["limit_price"] == "364.55"


def test_sends_only_changes_the_words_approve_still_sends_nothing(ledger, journal):
    out = approved(ledger, journal)  # sends=True
    assert statuses(journal) == [APPROVED]
    assert out.recheck.order == approved(sealed_ledger(), Journal(journal.path.with_name(
        "other.jsonl"))).recheck.order


# ===================== approve: network reads before the lock (#148) =====================

def test_the_quote_and_the_gate_are_fetched_outside_the_lock(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=at(T0), quotes=quotes_at(T0),
                events=events_clear())
    seen, hook = run_in_other_thread(journal)
    quotes, events = quotes_at(T0), events_clear()

    def q(symbol):
        hook("quote")
        return quotes(symbol)

    def e(*a):
        hook("events")
        return events(*a)

    out = approve(p, ledger, journal=journal, ask=yes, clock=at(T0), quotes=q, events=e)
    assert out.status == APPROVED
    assert seen == [("quote", True), ("events", True)]


def test_a_prefetched_quote_failure_still_refuses_at_quote(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=at(T0), quotes=quotes_at(T0),
                events=events_clear())

    def boom(symbol):
        raise TimeoutError

    out = approve(p, ledger, journal=journal, ask=yes, clock=at(T0), quotes=boom,
                  events=events_clear())
    assert out.status == "refused_after_approval" and "at quote" in out.reason


def test_a_prefetched_gate_failure_still_refuses_at_events(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=at(T0), quotes=quotes_at(T0),
                events=events_clear())

    def boom(*a):
        raise TimeoutError

    out = approve(p, ledger, journal=journal, ask=yes, clock=at(T0), quotes=quotes_at(T0),
                  events=boom)
    assert out.status == "refused_after_approval" and "at events" in out.reason


def test_a_quote_gone_stale_while_waiting_for_the_lock_is_refused(ledger, journal):
    """Fetched at T0 + 45 s; the re-check inside the lock runs at T0 + 65 s (max 15 s)."""
    p = propose(ledger, "plan-1", journal=journal, clock=at(T0), quotes=quotes_at(T0),
                events=events_clear())
    fetch = T0 + timedelta(seconds=45)
    clock = StepClock(fetch, fetch + timedelta(seconds=20))
    out = approve(p, ledger, journal=journal, ask=yes, clock=clock, quotes=quotes_at(fetch),
                  events=events_clear())
    assert out.status == "refused_after_approval" and "at size" in out.reason
    assert "old" in out.reason


# ===================== review fixes =====================

class Resp:
    def __init__(self, body, url, status=200):
        self.b, self.u, self.status = body, url, status

    def read(self, n=None):
        return self.b

    def geturl(self):
        return self.u

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def real_broker(monkeypatch, *, post, lookup=None):
    """tools.broker's paper client behind a fake opener. `post(body)` answers the POST
    (bytes, or raises); `lookup()` answers GET /v2/orders:by_client_order_id."""
    import urllib.error
    monkeypatch.setenv("ALPACA_API_KEY", "SENTINEL-KEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SENTINEL-SECRET")
    account = (BROKER_FIXTURES / "account.json").read_bytes()
    calls = []

    def opener(req, timeout=None):
        url = req.full_url
        if url.endswith("/v2/account"):
            return Resp(account, url)
        if req.get_method() == "POST":
            calls.append("POST")
            return Resp(post(json.loads(req.data)), url)
        if "by_client_order_id" in url:
            calls.append("LOOKUP")
            if lookup is None:
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)
            return Resp(lookup(), url)
        if req.get_method() == "DELETE":
            calls.append("DELETE")
            return Resp(b"", url, 204)
        raise AssertionError(url)

    return AlpacaPaperBroker(LIMITS, opener=opener), calls


def alpaca_reply(body):
    raw = json.loads((BROKER_FIXTURES / "order_bracket_new.json").read_text(encoding="utf-8"))
    raw.update({"client_order_id": body["client_order_id"], "symbol": body["symbol"],
                "qty": body["qty"], "limit_price": body["limit_price"]})
    for leg in raw["legs"]:
        leg.update({"symbol": body["symbol"], "qty": body["qty"]})
        if leg["type"] == "limit":
            leg["limit_price"] = body["take_profit"]["limit_price"]
        else:
            leg["stop_price"] = body["stop_loss"]["stop_price"]
    return json.dumps(raw).encode()


def approved_qty(journal, monkeypatch, qty):
    """An approval whose order is `qty` shares (the position cap sizes it), journaled."""
    led = sealed_ledger()
    value = (D("364.55") * qty + D("1")).quantize(D("0.01"))
    limits = replace(LIMITS, max_position_value=value, max_loss_per_trade=D("10000.00"),
                     account_cash=D("100000.00"), daily_loss_limit=D("10000.00"))
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: limits)
    out = approved(led, journal)
    assert out.recheck.order["qty"] == qty, out.recheck.order
    return out


@pytest.mark.parametrize("qty", [10, 100])
def test_b1_a_round_quantity_through_the_real_client_is_submitted(
        journal, monkeypatch, qty):
    """Decimal("10").normalize() is "1E+1": a 10-share order must still match."""
    out = approved_qty(journal, monkeypatch, qty)
    broker, calls = real_broker(monkeypatch, post=alpaca_reply)
    r = send(out, journal, broker)
    assert r.status == "submitted" and not r.alert, r.reason
    assert calls == ["POST"]  # no lookup, no cancel


def test_a_quantity_written_with_a_decimal_point_still_matches():
    from pipeline.send import _matches
    body = {"client_order_id": "ta-x", "symbol": "TSLA", "qty": "10", "limit_price": "364.55",
            "take_profit": {"limit_price": "370.04"}, "stop_loss": {"stop_price": "358.32"}}
    legs = [{"type": "limit", "side": "sell", "symbol": "TSLA", "qty": "10",
             "order_class": "bracket", "limit_price": "370.04"},
            {"type": "stop", "side": "sell", "symbol": "TSLA", "qty": "10.0",
             "order_class": "bracket", "stop_price": "358.32"}]
    reply = {**body, "side": "buy", "order_class": "bracket", "type": "limit", "legs": legs}
    assert _matches(dict(reply, qty="10.0"), body) and _matches(dict(reply, qty="10"), body)
    assert not _matches(dict(reply, qty="1E+2"), body)  # 100 is not 10
    assert not _matches(dict(reply, qty="9"), body)


@pytest.mark.parametrize("qty", [10, 100])
def test_b1_a_round_quantity_found_by_lookup_is_submitted(journal, monkeypatch, qty):
    import urllib.error

    def timeout(body):
        raise urllib.error.URLError(TimeoutError())

    out = approved_qty(journal, monkeypatch, qty)
    body = bracket_request(out.recheck.order, cid_of(out))
    broker, calls = real_broker(monkeypatch, post=timeout,
                                lookup=lambda: alpaca_reply(body))
    r = send(out, journal, broker)
    assert r.status == "submitted" and "broker holds the order" in r.reason
    assert calls == ["POST", "LOOKUP"]  # never resent, never cancelled


def test_the_real_client_on_a_timeout_with_nothing_found_is_send_unknown(
        ledger, journal, monkeypatch, capsys):
    import urllib.error

    def timeout(body):
        raise urllib.error.URLError(TimeoutError())

    out = approved(ledger, journal)
    broker, calls = real_broker(monkeypatch, post=timeout)  # the lookup answers 404
    r = send(out, journal, broker)
    assert r.status == "send_unknown" and r.alert and calls == ["POST", "LOOKUP"]
    assert "ALERT" in capsys.readouterr().err


def test_the_real_client_on_a_500_with_the_order_found_is_submitted(
        ledger, journal, monkeypatch):
    import urllib.error

    def server_error(body):
        raise urllib.error.HTTPError("u", 500, "x", {}, None)

    out = approved(ledger, journal)
    body = bracket_request(out.recheck.order, cid_of(out))
    broker, calls = real_broker(monkeypatch, post=server_error,
                                lookup=lambda: alpaca_reply(body))
    assert send(out, journal, broker).status == "submitted"
    assert calls == ["POST", "LOOKUP"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_every_definite_refusal_with_nothing_found_is_rejected(ledger, journal, status):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    broker.submit = BrokerError(f"POST /v2/orders failed: HTTP {status}", status=status)
    r = send(out, journal, broker)
    assert r.status == "rejected" and raw_lines(journal)[-1]["http_status"] == status


def test_a_lookup_that_returns_neither_an_order_nor_none_is_send_unknown(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    broker.submit = BrokerError("POST /v2/orders failed: HTTP 403", status=403)
    broker.lookup = ["not", "an", "order"]
    r = send(out, journal, broker)
    assert r.status == "send_unknown" and r.alert


def test_a_clock_that_raises_before_the_post_sends_nothing(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()

    def broken():
        raise RuntimeError("clock gone")

    r = send_approved(out, journal=journal, broker=broker, clock=broken)
    assert r.status == NOT_SENT and "submit" not in broker.names()


def test_a_clock_that_raises_after_the_post_still_journals_the_broker_id(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    reads = {"n": 0}

    def breaks_after_the_lock():
        reads["n"] += 1
        if reads["n"] > 1:
            raise RuntimeError("clock gone")
        return T1

    r = send_approved(out, journal=journal, broker=broker, clock=breaks_after_the_lock)
    assert r.status == "submitted" and r.journaled
    last = raw_lines(journal)[-1]
    assert last["status"] == "submitted" and last["broker_order_id"] == "ord-1"


def test_an_outcome_that_differs_from_the_journaled_approval_is_never_sent(ledger, journal):
    out = approved(ledger, journal)
    bigger = dict(out.recheck.order, qty=9)
    sized = dict(out.recheck.sized, order=bigger)
    forged = replace(out, recheck=replace(out.recheck, sized=sized))
    broker = FakePaperBroker()
    r = send(forged, journal, broker)
    assert r.status == NOT_SENT and "differs from the journaled approval" in r.reason
    assert not r.journaled and "submit" not in broker.names()
    assert statuses(journal) == [APPROVED]  # the real approval is untouched
    assert send(out, journal, broker).status == "submitted"


def test_day_state_never_leaves_out_a_sent_order(ledger, journal):
    out = approved(ledger, journal)
    send(out, journal, FakePaperBroker())
    cid = cid_of(out)
    assert journal.day_state(T1, exclude=cid) == journal.day_state(T1)
    assert journal.day_state(T1, exclude=cid).open_positions == 1


def test_the_cancel_of_a_mismatch_does_not_hold_the_lock(ledger, journal):
    out = approved(ledger, journal)
    broker = FakePaperBroker()
    from tests.order_helpers import broker_reply
    wrong = broker_reply(bracket_request(out.recheck.order, cid_of(out)), qty="9")
    broker.submit, broker.lookup = wrong, wrong
    seen, hook = run_in_other_thread(journal)
    real_cancel = broker.cancel_order

    def cancel(order_id):
        hook("cancel")
        return real_cancel(order_id)

    broker.cancel_order = cancel
    broker.during_call = hook
    send(out, journal, broker)
    assert ("cancel", True) in seen and all(ok for _, ok in seen)
