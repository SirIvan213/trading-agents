"""Phase 5 gate D: the earnings and halt event gate in the order path (#112, #13).

Groups: (A) check_order's `gate` input, (B) propose/approve with an events source, (C) the FMP
calendar cache and LiveData, (D) get_news' earnings_calendar field, (E) the #112 end-to-end proof:
every model says BUY on the red-flag scenario and the deterministic gate still refuses it.
No network, no Claude API; every model is a fake.
"""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import pipeline.decide as decide
import pipeline.order_flow as of
from agents.sentiment_tools import SentimentSession
from journal.log import APPROVED, Journal
from pipeline.data import LiveData
from pipeline.decide import run_decision
from pipeline.events import FmpCalendarCache, events_from
from pipeline.order_flow import approve, propose, render_ticket
from pipeline.scenarios import ScenarioData, load_scenario
from tests.debate_helpers import reply
from tests.fakes import NOON, FakeAllData, FakeClient
from tests.order_helpers import (
    NY,
    T0,
    StepClock,
    calendar_ok,
    clear_gate,
    events_clear,
    events_of,
    filings_ok,
    fmp_row,
    gate_for,
    news_item,
    news_ok,
    plan,
    quotes_at,
    raw_lines,
    sealed_ledger,
    verdict_for,
)
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tests.test_smoke_scenarios import PLANS, SHARES, debaters, smoke, tail, trader  # noqa: F401
from tools.config import CONFIG_DIR, load_limits
from tools.risk_check import DayState, check_order

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
MAX_AGE = LIMITS.max_plan_age_s  # 300


@pytest.fixture(autouse=True)
def fixed_limits(monkeypatch):
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)
    monkeypatch.setattr(decide, "load_checked_limits", lambda *a, **k: LIMITS)


def order():
    from tests.order_helpers import sized_order
    return sized_order("TSLA")


def check(gate, now=T0, **kw):
    return check_order(order(), LIMITS, DayState(), now, gate=gate, **kw)


def gate_reasons(verdict):
    return [r for r in verdict["reasons"] if r.startswith("event gate")]


# ============================ A. check_order: the gate input ============================

def test_a_fresh_clear_gate_for_the_symbol_passes():
    assert check(clear_gate("TSLA", T0)) == {"verdict": "PASS", "reasons": []}


def test_blocked_no_gate_at_all():
    v = check(None)
    assert v["verdict"] == "BLOCK"
    assert v["reasons"] == ["event gate result missing (earnings and halts not checked)"]


def test_blocked_default_gate_argument_is_none_and_blocks():
    v = check_order(order(), LIMITS, DayState(), T0)
    assert v["verdict"] == "BLOCK" and gate_reasons(v) == [
        "event gate result missing (earnings and halts not checked)"]


@pytest.mark.parametrize("gate", ["CLEAR", ["CLEAR"], 1, True, ()], ids=repr)
def test_blocked_a_gate_that_is_not_a_dict(gate):
    assert gate_reasons(check(gate)) == [
        "event gate result missing (earnings and halts not checked)"]


def test_blocked_gate_for_another_symbol():
    v = check(clear_gate("AAPL", T0))
    assert v["verdict"] == "BLOCK"
    assert gate_reasons(v) == ["event gate result is for another symbol"]


def test_the_gate_symbol_is_compared_after_trim_and_upper_case():
    gate = {**clear_gate("TSLA", T0), "symbol": " tsla "}
    assert check(gate) == {"verdict": "PASS", "reasons": []}


def test_blocked_stale_gate_older_than_max_plan_age():
    old = clear_gate("TSLA", T0 - timedelta(seconds=MAX_AGE + 1))
    v = check(old)
    assert v["verdict"] == "BLOCK" and gate_reasons(v) == ["event gate result is stale"]


def test_a_gate_exactly_max_plan_age_old_is_still_fresh():
    ok = clear_gate("TSLA", T0 - timedelta(seconds=MAX_AGE))
    assert check(ok) == {"verdict": "PASS", "reasons": []}


def test_blocked_a_gate_from_the_future_beyond_the_skew():
    ahead = clear_gate("TSLA", T0 + timedelta(seconds=6))
    assert gate_reasons(check(ahead)) == ["event gate result is stale"]


def test_a_gate_five_seconds_ahead_is_within_the_skew():
    ahead = clear_gate("TSLA", T0 + timedelta(seconds=5))
    assert check(ahead) == {"verdict": "PASS", "reasons": []}


def test_blocked_a_gate_from_another_new_york_day_is_stale_even_if_recent():
    yesterday = datetime(2026, 11, 8, 23, 59, 30, tzinfo=NY)
    gate = gate_for("TSLA", yesterday)
    assert gate["status"] == "CLEAR"
    v = check_order(order(), LIMITS, DayState(), yesterday + timedelta(seconds=40), gate=gate)
    assert "event gate result is stale" in v["reasons"] and v["verdict"] == "BLOCK"


@pytest.mark.parametrize("change", [
    {"status": "MAYBE"}, {"status": None}, {"status": "clear"}, {"status": ["CLEAR"]},
    {"status": "CLEAR", "checked_at": None}, {"checked_at": "yesterday"},
    {"checked_at": "2026-10-19T10:15:30"},  # naive
    {"checked_at": 1760883330},
], ids=repr)
def test_blocked_malformed_gate_results(change):
    gate = {**clear_gate("TSLA", T0), **change}
    v = check(gate)
    assert v["verdict"] == "BLOCK"
    assert gate_reasons(v) == ["event gate result is malformed"]


def test_blocked_a_gate_with_no_checked_at():
    gate = {k: v for k, v in clear_gate("TSLA", T0).items() if k != "checked_at"}
    assert gate_reasons(check(gate)) == ["event gate result is malformed"]


def test_blocked_a_hand_forged_bare_clear_has_no_symbol_and_no_time():
    v = check({"status": "CLEAR"})
    assert v["verdict"] == "BLOCK"
    assert gate_reasons(v) == ["event gate result is for another symbol"]


def test_blocked_a_forged_clear_with_a_symbol_but_no_time():
    v = check({"status": "CLEAR", "symbol": "TSLA"})
    assert v["verdict"] == "BLOCK" and gate_reasons(v) == ["event gate result is malformed"]


def test_blocked_event_and_unknown_keep_their_own_label_and_reason():
    ev = {**clear_gate("TSLA", T0), "status": "EVENT", "reason": "EVENT (fmp): earnings today"}
    un = {**clear_gate("TSLA", T0), "status": "UNKNOWN", "reason": "UNKNOWN (sec): no filer"}
    assert gate_reasons(check(ev)) == ["event gate EVENT: EVENT (fmp): earnings today"]
    assert gate_reasons(check(un)) == ["event gate UNKNOWN: UNKNOWN (sec): no filer"]
    assert check(ev)["verdict"] == check(un)["verdict"] == "BLOCK"


def test_blocked_an_event_without_a_reason_still_blocks_and_says_so():
    ev = {**clear_gate("TSLA", T0), "status": "EVENT", "reason": ""}
    assert gate_reasons(check(ev)) == ["event gate EVENT: no reason given"]


def test_a_blocked_gate_does_not_hide_other_reasons():
    bad = {**order(), "qty": 0}
    v = check_order(bad, LIMITS, DayState(), T0, gate=None)
    assert v["verdict"] == "BLOCK"
    assert any("whole number" in r for r in v["reasons"])
    assert gate_reasons(v) == ["event gate result missing (earnings and halts not checked)"]


def test_a_clear_gate_does_not_unblock_other_rules():
    v = check_order({**order(), "side": "sell"}, LIMITS, DayState(), T0, gate=clear_gate("TSLA",
        T0))
    assert v["verdict"] == "BLOCK" and gate_reasons(v) == []


def test_a_clear_gate_cannot_open_a_closed_market():
    sat = datetime(2026, 10, 17, 11, 0, tzinfo=NY)
    v = check_order(order(), LIMITS, DayState(), sat, gate=clear_gate("TSLA", sat))
    assert v["verdict"] == "BLOCK" and any("market is closed" in r for r in v["reasons"])


@pytest.mark.parametrize("lim", [replace(LIMITS, trading_enabled=False),
                                 replace(LIMITS, paper=False)], ids=["kill-switch", "live"])
def test_a_clear_gate_cannot_override_the_switches(lim):
    v = check_order(order(), lim, DayState(), T0, gate=clear_gate("TSLA", T0))
    assert v["verdict"] == "BLOCK"


def test_no_limit_setting_lets_an_unknown_gate_through():
    un = {**clear_gate("TSLA", T0), "status": "UNKNOWN", "reason": "x"}
    for lim in (LIMITS, replace(LIMITS, max_plan_age_s=3600), replace(LIMITS,
        max_trades_per_day=99)):
        assert check_order(order(), lim, DayState(), T0, gate=un)["verdict"] == "BLOCK"


def test_the_result_from_a_real_event_gate_is_json_serializable():
    json.dumps(clear_gate("TSLA", T0))
    json.dumps(gate_for("TSLA", T0, calendar={"ok": False, "reason": "x"}))


# ============================ B. propose / approve with an events source ===========

@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "j.jsonl")


def propose_with(journal, events, *, now=T0, ledger=None):
    return propose(ledger or sealed_ledger(), "plan-1", journal=journal, clock=lambda: now,
                   quotes=quotes_at(now), events=events)


def test_clean_proposal_carries_the_gate_and_the_ticket_names_it(journal):
    ev = events_clear()
    p = propose_with(journal, ev)
    assert p.ok and p.gate["status"] == "CLEAR" and p.gate["symbol"] == "TSLA"
    assert "Event gate: CLEAR (no earnings through" in render_ticket(p)
    assert p.verdict == {"verdict": "PASS", "reasons": []}
    assert len(ev.calls) == 1


def test_events_are_asked_for_the_plan_symbol_now_and_todays_flat_by(journal):
    ev = events_clear()
    propose_with(journal, ev)
    ((symbol, now, window_end),) = ev.calls
    assert symbol == "TSLA" and now == T0
    assert window_end == datetime(2026, 10, 19, 15, 55, tzinfo=NY)


def test_the_window_ends_at_the_early_close_flat_by(journal):
    day = datetime(2026, 11, 27, 10, 0, tzinfo=NY)  # the day after Thanksgiving: 13:00 close
    ev = events_clear()
    led = sealed_ledger([plan(as_of=day.isoformat())])
    propose(led, "plan-1", journal=journal, clock=lambda: day + timedelta(seconds=30),
            quotes=quotes_at(day + timedelta(seconds=30)), events=ev)
    assert [c[2] for c in ev.calls] == [datetime(2026, 11, 27, 12, 55, tzinfo=NY)]


def test_blocked_without_an_events_source_the_proposal_is_refused_at_risk(journal):
    p = propose(sealed_ledger(), "plan-1", journal=journal, clock=lambda: T0, quotes=quotes_at(T0))
    assert p.ok is False and p.stage == "risk" and p.gate is None
    assert p.reason == "event gate result missing (earnings and halts not checked)"
    assert render_ticket(p).startswith("NO ORDER for plan plan-1: event gate result missing")


def test_blocked_an_events_source_that_raises_is_refused_at_stage_events(journal):
    def boom(symbol, now, window_end):
        raise RuntimeError("secret key abc123 in the message")
    p = propose_with(journal, boom)
    assert p.ok is False and p.stage == "events"
    assert p.reason == "event gate failed (RuntimeError)"  # the type only, never the message
    assert "abc123" not in render_ticket(p)


def test_blocked_an_event_proposal_keeps_its_distinct_reason(journal):
    ev = events_of(calendar=calendar_ok(T0, [fmp_row("TSLA", "2026-10-19", "amc", True)]))
    p = propose_with(journal, ev)
    assert p.ok is False and p.stage == "risk" and p.gate["status"] == "EVENT"
    assert p.reason.startswith("event gate EVENT: EVENT (fmp): earnings today 2026-10-19")
    assert "UNKNOWN" not in p.reason
    assert p.verdict["verdict"] == "BLOCK"
    assert "NO ORDER" in render_ticket(p) and "event gate EVENT" in render_ticket(p)


def test_blocked_an_unknown_proposal_keeps_its_distinct_reason(journal):
    ev = events_of(calendar={"ok": False, "reason": "HTTP 429 from /stable/earnings-calendar"})
    p = propose_with(journal, ev)
    assert p.ok is False and p.stage == "risk" and p.gate["status"] == "UNKNOWN"
    assert p.reason.startswith(
        "event gate UNKNOWN: UNKNOWN (fmp): earnings source unavailable: HTTP 429")
    assert "EVENT (" not in p.reason


def test_blocked_a_gate_for_another_symbol_from_the_source(journal):
    p = propose_with(journal, lambda s, n, w: clear_gate("AAPL", n))
    assert p.ok is False and p.reason == "event gate result is for another symbol"


def test_blocked_a_source_returning_a_stale_gate(journal):
    p = propose_with(journal, lambda s, n, w: clear_gate(s, n - timedelta(seconds=MAX_AGE + 1)))
    assert p.ok is False and p.reason == "event gate result is stale"


def test_blocked_a_source_returning_nothing(journal):
    p = propose_with(journal, lambda s, n, w: None)
    assert p.ok is False and p.reason.startswith("event gate result missing")


def test_blocked_refused_proposals_journal_the_gate_status_and_reason(journal):
    unknown = events_of(calendar={"ok": False, "reason": "timeout"})
    event = events_of(calendar=calendar_ok(T0, [fmp_row("TSLA", "2026-10-19", None, False)]))
    for ev in (unknown, event):
        led = sealed_ledger()
        p = propose_with(journal, ev, ledger=led)
        out = approve(p, led, journal=journal, ask=lambda _: pytest.fail("asked"), clock=lambda: T0,
                      quotes=quotes_at(T0), events=ev)
        assert out.status == "no_order"
    first, second = raw_lines(journal)
    assert first["gate"]["status"] == "UNKNOWN"
    assert "earnings source unavailable: timeout" in first["gate"]["reason"]
    assert second["gate"]["status"] == "EVENT"
    assert "no report time given, estimated" in second["gate"]["reason"]
    assert first["status"] == second["status"] == "no_order" and first["stage"] == "risk"


def test_an_approved_entry_journals_a_clear_gate(journal):
    led = sealed_ledger()
    p = propose_with(journal, events_clear(), ledger=led)
    out = approve(p, led, journal=journal, ask=lambda _: "yes",
        clock=lambda: T0 + timedelta(seconds=10),
                  quotes=quotes_at(T0 + timedelta(seconds=10)), events=events_clear())
    assert out.status == APPROVED
    (entry,) = raw_lines(journal)
    assert entry["gate"]["status"] == "CLEAR" and entry["gate"]["reason"].startswith("no earnings")
    assert entry["verdict"] == {"verdict": "PASS", "reasons": []}


def test_blocked_the_recheck_asks_the_gate_again_and_an_event_after_yes_refuses(journal):
    led = sealed_ledger()
    later = T0 + timedelta(seconds=20)
    reading = {"n": 0}

    def flips(symbol, now, window_end):
        reading["n"] += 1
        if reading["n"] == 1:
            return gate_for(symbol, now, window_end=window_end)
        halt = news_item(symbol, "Trading halted in TSLA pending news", now - timedelta(minutes=1))
        return gate_for(symbol, now, window_end=window_end, news=news_ok([halt]))

    p = propose_with(journal, flips, ledger=led)
    assert p.ok and reading["n"] == 1
    out = approve(p, led, journal=journal, ask=lambda _: "yes", clock=lambda: later,
                  quotes=quotes_at(later), events=flips)
    assert reading["n"] == 2
    assert out.status == "refused_after_approval"
    assert ("re-check failed at risk: event gate EVENT: EVENT (news): trading halt today"
            in out.reason)
    (entry,) = raw_lines(journal)
    assert entry["status"] == "refused_after_approval" and "approved_at" not in entry


def test_blocked_an_events_source_that_fails_at_the_recheck_refuses_after_approval(journal):
    led = sealed_ledger()
    p = propose_with(journal, events_clear(), ledger=led)

    def boom(symbol, now, window_end):
        raise TimeoutError
    out = approve(p, led, journal=journal, ask=lambda _: "yes", clock=lambda: T0,
                  quotes=quotes_at(T0), events=boom)
    assert out.status == "refused_after_approval"
    assert out.reason == "re-check failed at events: event gate failed (TimeoutError)"


def test_blocked_approve_without_events_refuses_after_yes(journal):
    led = sealed_ledger()
    p = propose_with(journal, events_clear(), ledger=led)
    out = approve(p, led, journal=journal, ask=lambda _: "yes", clock=lambda: T0,
                  quotes=quotes_at(T0))
    assert out.status == "refused_after_approval"
    assert "event gate result missing" in out.reason


def test_a_decline_does_not_ask_the_gate_again(journal):
    led = sealed_ledger()
    ev = events_clear()
    p = propose_with(journal, ev, ledger=led)
    approve(p, led, journal=journal, ask=lambda _: "no", clock=lambda: T0, quotes=quotes_at(T0),
            events=ev)
    assert len(ev.calls) == 1


# ============================ C. the FMP calendar cache ============================

DAY = date(2026, 11, 9)
RANGE_END = date(2026, 11, 23)


class Fetch:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, start, end):
        self.calls.append((start, end))
        out = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(out, Exception):
            raise out
        return out


def cache_with(tmp_path, fetch, now=datetime(2026, 11, 9, 8, 30, tzinfo=NY)):
    return FmpCalendarCache(tmp_path, fetch, lambda: now)


def test_the_cache_fetches_once_per_new_york_day(tmp_path):
    fetch = Fetch([fmp_row("AXLE", "2026-11-09")])
    cache = cache_with(tmp_path, fetch)
    first = cache.get(DAY, RANGE_END)
    second = cache.get(DAY, RANGE_END)
    assert len(fetch.calls) == 1 and first == second
    assert first["from"] == "2026-11-09" and first["to"] == "2026-11-23"
    assert first["fetched_at"] == "2026-11-09T08:30:00-05:00"
    assert first["reply"] == [fmp_row("AXLE", "2026-11-09")]
    assert (tmp_path / "fmp-earnings-2026-11-09.json").exists()


def test_a_second_cache_object_reads_the_same_file(tmp_path):
    fetch = Fetch([])
    cache_with(tmp_path, fetch).get(DAY, RANGE_END)
    cache_with(tmp_path, fetch).get(DAY, RANGE_END)
    assert len(fetch.calls) == 1


def test_a_failed_fetch_is_not_cached_and_the_next_get_fetches_again(tmp_path):
    fetch = Fetch(RuntimeError("HTTP 503 from /stable/earnings-calendar"), [])
    cache = cache_with(tmp_path, fetch)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        cache.get(DAY, RANGE_END)
    assert list(tmp_path.glob("*.json")) == []
    assert cache.get(DAY, RANGE_END)["reply"] == []
    assert len(fetch.calls) == 2


@pytest.mark.parametrize("junk", ["", "not json", "[]", '{"from": "2026-11-09"}', "null",
                                  '{"from": "2026-11-09", "to": "2026-11-23"}'],
                         ids=["empty", "text", "list", "no-reply", "null", "no-reply-2"])
def test_an_unreadable_or_incomplete_cache_file_refetches_and_is_rewritten(tmp_path, junk):
    (tmp_path / "fmp-earnings-2026-11-09.json").write_text(junk, encoding="utf-8")
    fetch = Fetch([fmp_row("AXLE", "2026-11-09")])
    cache = cache_with(tmp_path, fetch)
    assert cache.get(DAY, RANGE_END)["reply"] == [fmp_row("AXLE", "2026-11-09")]
    assert len(fetch.calls) == 1
    cache.get(DAY, RANGE_END)
    assert len(fetch.calls) == 1  # the rewritten file is good


def test_a_different_day_fetches_again_and_has_its_own_file(tmp_path):
    fetch = Fetch([])
    cache = cache_with(tmp_path, fetch)
    cache.get(DAY, RANGE_END)
    cache.get(date(2026, 11, 10), date(2026, 11, 24))
    assert [c[0] for c in fetch.calls] == [DAY, date(2026, 11, 10)]
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [
        "fmp-earnings-2026-11-09.json", "fmp-earnings-2026-11-10.json"]


def test_a_cached_range_that_is_shorter_than_requested_is_fetched_again(tmp_path):
    fetch = Fetch([])
    cache = cache_with(tmp_path, fetch)
    cache.get(DAY, date(2026, 11, 12))
    cache.get(DAY, RANGE_END)
    assert len(fetch.calls) == 2


def test_an_unwritable_cache_dir_only_costs_another_fetch(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    fetch = Fetch([])
    cache = FmpCalendarCache(blocker / "sub", fetch, lambda: NOON)
    cache.get(DAY, RANGE_END)
    cache.get(DAY, RANGE_END)
    assert len(fetch.calls) == 2


def test_live_data_earnings_calendar_goes_through_the_cache(tmp_path):
    fetch = Fetch([fmp_row("AXLE", "2026-11-09")])
    data = LiveData(calendar_cache=cache_with(tmp_path, fetch))
    one = data.earnings_calendar(DAY, RANGE_END)
    two = data.earnings_calendar(DAY, RANGE_END)
    assert one == two and len(fetch.calls) == 1 and one["reply"][0]["symbol"] == "AXLE"


def test_blocked_live_data_with_a_failing_fetch_gives_an_unknown_gate(tmp_path):
    fetch = Fetch(RuntimeError("request to /stable/earnings-calendar timed out"))
    data = LiveData(calendar_cache=cache_with(tmp_path, fetch))
    now = datetime(2026, 11, 9, 10, 30, tzinfo=NY)
    shim = SimpleNamespace(earnings_calendar=data.earnings_calendar,
                           submissions=lambda s: None,
                           news=lambda syms, a, b: {"news": [], "next_page_token": None})
    gate = events_from(shim)("TSLA", now, now.replace(hour=15, minute=55))
    assert gate["status"] == "UNKNOWN"
    why = "earnings source unavailable: request to /stable/earnings-calendar timed out"
    assert why in gate["reason"]
    assert verdict_for(gate, now)["verdict"] == "BLOCK"


# ============================ D. events_from: the closed paths ============================

def data_for(now, **over):
    """A data object that is clear unless a part is replaced."""
    d = dict(
        earnings_calendar=lambda s, e: {"fetched_at": (now - timedelta(minutes=1)).isoformat(),
                                        "from": s.isoformat(), "to": e.isoformat(), "reply": []},
        submissions=lambda sym: filings_ok(now, symbol=sym)["submissions"],
        news=lambda syms, a, b: {"news": [], "next_page_token": None})
    d.update(over)
    return SimpleNamespace(**d)


NOW9 = datetime(2026, 11, 9, 10, 30, tzinfo=NY)
WINDOW9 = NOW9.replace(hour=15, minute=55)


def test_events_from_with_clean_data_is_clear_and_passes_check_order():
    gate = events_from(data_for(NOW9))("TSLA", NOW9, WINDOW9)
    assert gate["status"] == "CLEAR", gate["reason"]
    assert verdict_for(gate, NOW9) == {"verdict": "PASS", "reasons": []}


@pytest.mark.parametrize("news_reply", [{"news": None}, {}, None, [], {"news": "x"}],
                         ids=["null-list", "empty-dict", "none", "bare-list", "string"])
def test_blocked_data_news_returning_a_null_or_malformed_reply_is_unknown(news_reply):
    data = data_for(NOW9, news=lambda syms, a, b: news_reply)
    gate = events_from(data)("TSLA", NOW9, WINDOW9)
    assert gate["status"] == "UNKNOWN"
    assert "UNKNOWN (news): news unavailable (malformed news reply): cannot rule out a halt today" \
        in gate["reason"]
    v = verdict_for(gate, NOW9)
    assert v["verdict"] == "BLOCK" and any(r.startswith("event gate UNKNOWN") for r in v["reasons"])


def test_blocked_data_news_raising_is_unknown_with_the_runtime_error_text():
    def boom(syms, a, b):
        raise RuntimeError("unexpected news response shape")
    gate = events_from(data_for(NOW9, news=boom))("TSLA", NOW9, WINDOW9)
    assert gate["status"] == "UNKNOWN"
    assert "news unavailable (unexpected news response shape)" in gate["reason"]


def test_blocked_a_non_runtime_exception_is_reported_by_type_name_only():
    def boom(sym):
        raise KeyError("CIK for TSLA secret-token")
    gate = events_from(data_for(NOW9, submissions=boom))("TSLA", NOW9, WINDOW9)
    assert gate["status"] == "UNKNOWN"
    assert "SEC filings unavailable: KeyError" in gate["reason"]
    assert "secret-token" not in gate["reason"]


def test_blocked_a_data_object_without_an_earnings_calendar_is_unknown():
    data = SimpleNamespace(submissions=lambda s: None, news=lambda a, b, c: {"news": []})
    gate = events_from(data)("TSLA", NOW9, WINDOW9)
    assert gate["status"] == "UNKNOWN"
    assert "UNKNOWN (fmp): earnings source unavailable:" in gate["reason"]


@pytest.mark.parametrize("raw", [None, {}, {"fetched_at": "x", "from": "2026-11-09",
                                           "to": "2026-11-23", "reply": []},
                                 {"fetched_at": "2026-11-09T09:00:00-05:00", "from": "x",
                                  "to": "y", "reply": []},
                                 {"fetched_at": "2026-11-09T09:00:00", "from": "2026-11-09",
                                  "to": "2026-11-23", "reply": []}],
                         ids=["none", "empty", "bad-time", "bad-dates", "naive-time"])
def test_blocked_a_broken_cache_record_is_unknown(raw):
    gate = events_from(data_for(NOW9, earnings_calendar=lambda s, e: raw))("TSLA", NOW9, WINDOW9)
    assert gate["status"] == "UNKNOWN"


def test_blocked_a_halt_in_the_data_news_is_an_event_through_events_from():
    halt = news_item("TSLA", "Trading halted in TSLA pending news", NOW9 - timedelta(hours=1))
    data = data_for(NOW9, news=lambda syms, a, b: {"news": [halt], "next_page_token": None})
    gate = events_from(data)("TSLA", NOW9, WINDOW9)
    assert gate["status"] == "EVENT" and "EVENT (news): trading halt today" in gate["reason"]


def test_events_from_reads_a_24_hour_news_window_for_the_symbol():
    seen = []
    data = data_for(NOW9, news=lambda syms, a, b: seen.append((syms, a, b)) or {"news": []})
    events_from(data)("TSLA", NOW9, WINDOW9)
    assert seen == [(["TSLA"], NOW9 - timedelta(hours=24), NOW9)]


# ============================ E. get_news: the earnings_calendar field ============================

def news_session(name):
    data = ScenarioData(load_scenario(name))
    return SentimentSession(data.symbol, data.as_of(), data), data


def test_get_news_red_flag_shows_an_earnings_event_and_the_scheduled_catalyst():
    s, data = news_session("red-flag")
    out, _ = s.execute("get_news", {"symbol": data.symbol})
    cal = out["earnings_calendar"]
    assert cal["status"] == "EVENT"
    assert ("EVENT (fmp): earnings today 2026-11-03 (no report time given, confirmed)"
            in cal["reason"])
    assert "earnings_scheduled" in out["catalysts"]
    assert out["catalysts"] == sorted(out["catalysts"])


def test_get_news_clean_buy_is_clear_without_the_scheduled_catalyst():
    s, data = news_session("clean-buy")
    out, _ = s.execute("get_news", {"symbol": data.symbol})
    assert out["earnings_calendar"] == {"status": "CLEAR",
        "reason": "no report listed in the window"}
    assert "earnings_scheduled" not in out["catalysts"]


def test_get_news_with_a_data_object_without_earnings_calendar_is_unknown_and_still_works():
    class NoCalendar:
        def __init__(self, inner):
            self.news, self.submissions = inner.news, inner.submissions
    s = SentimentSession("ABCD", NOON, NoCalendar(FakeAllData()))
    out, err = s.execute("get_news", {"symbol": "ABCD"})
    assert out["earnings_calendar"] == {"status": "UNKNOWN",
        "reason": "no earnings calendar source"}
    assert out["counts"] and "earnings_scheduled" not in out["catalysts"]


def test_get_news_with_a_failing_calendar_is_unknown_and_still_works():
    fake = FakeAllData(calendar_raises=RuntimeError("HTTP 429 from /stable/earnings-calendar"))
    out, _ = SentimentSession("ABCD", NOON, fake).execute("get_news", {"symbol": "ABCD"})
    cal = out["earnings_calendar"]
    assert cal["status"] != "CLEAR"  # (the fixture's item 2.02 filed today is a SEC EVENT too)
    assert "earnings_scheduled" not in out["catalysts"]
    why = "UNKNOWN (fmp): earnings source unavailable: HTTP 429 from /stable/earnings-calendar"
    assert why in cal["reason"]
    assert out["headlines"]  # the digest itself is intact


def test_get_news_with_a_failing_filings_source_still_reports_the_calendar():
    fake = FakeAllData(subs_raises=RuntimeError("boom"))
    out, _ = SentimentSession("ABCD", NOON, fake).execute("get_news", {"symbol": "ABCD"})
    assert out["earnings_calendar"]["status"] == "UNKNOWN"
    assert "SEC filings unavailable" in out["earnings_calendar"]["reason"]
    assert out["note"].startswith("filings unavailable")


def test_get_news_is_context_only_the_calendar_reply_is_not_passed_through():
    fake = FakeAllData(calendar_rows=[fmp_row("ABCD", "2026-11-03", "amc", True)])
    out, _ = SentimentSession("ABCD", NOON, fake).execute("get_news", {"symbol": "ABCD"})
    assert out["earnings_calendar"]["status"] == "EVENT"
    assert set(out["earnings_calendar"]) == {"status", "reason"}
    assert "epsEstimated" not in json.dumps(out)


# ============================ F. #112 end to end ============================

class RestampedScenario(ScenarioData):
    """The scenario's data with its saved quote re-stamped at the clock's latest reading, the
    way a live quote is (the saved one is minutes old and would be refused as stale, #68)."""

    def __init__(self, scenario, clock):
        super().__init__(scenario)
        self.clock = clock

    def snapshots(self, symbols):
        out = super().snapshots(symbols)
        times = self.clock.times
        now = times[max(0, min(self.clock.calls - 1, len(times) - 1))]
        fixed = {}
        for sym, snap in out.items():
            if isinstance(snap, dict) and isinstance(snap.get("latestQuote"), dict):
                snap = {**snap, "latestQuote": {**snap["latestQuote"], "t": now.isoformat()}}
            fixed[sym] = snap
        return fixed


def run_scenario(monkeypatch, tmp_path, name, *, trader_confidence=0.8, answers=None):
    """run_decision on a saved scenario with every model faked to BUY plan-1. The analysts are
    one stub that records the scenario's real plan; the debaters and the Trader are scripted."""
    scenario = load_scenario(name)
    base = ScenarioData(scenario)
    as_of, symbol = base.as_of(), base.symbol
    clock = StepClock(as_of, as_of + timedelta(seconds=20), as_of + timedelta(seconds=40))
    data = RestampedScenario(scenario, clock)

    async def fake_analysts(sym, at, **k):
        p = plan(as_of=as_of.isoformat(), **PLANS[name])
        k["ledger"].record(p)
        k["ledger"].seal(accept=True)
        from tests.test_smoke_scenarios import packet_for
        return packet_for(symbol, [p], as_of.isoformat())

    monkeypatch.setattr(decide, "run_analysts", fake_analysts)
    replies = [*debaters(symbol), reply(trader(symbol, confidence=trader_confidence))]
    client = FakeClient(replies)
    journal = Journal(tmp_path / f"{name}.jsonl")
    asked = []

    def ask(question):
        asked.append(question)
        return (answers or "yes")

    result = run_decision(symbol, data=data, clients=client, journal=journal, clock=clock,
                          ask=ask, rounds=1, timeout_s=10)
    return result, journal, asked, client


def test_blocked_red_flag_every_model_says_buy_and_the_gate_still_refuses(monkeypatch, tmp_path):
    result, journal, asked, client = run_scenario(monkeypatch, tmp_path, "red-flag")
    assert result.trader.decision.action == "BUY" and result.trader.decision.plan_id == "plan-1"
    p = result.proposal
    assert p.ok is False and p.stage == "risk"
    assert "event gate EVENT:" in p.reason
    assert "EVENT (fmp): earnings today 2026-11-03" in p.reason
    assert "EVENT (news): trading halt today: 'Ventara shares halted for volatility" in p.reason
    assert "earnings headline in the news" in p.reason
    assert "UNKNOWN (sec): no 8-K item 2.02 in the last 100 days" in p.reason
    assert p.verdict["verdict"] == "BLOCK" and p.sized is not None  # sized, then refused
    assert p.gate["status"] == "EVENT"
    assert result.outcome.status == "no_order" and asked == []
    assert result.ticket.startswith("NO ORDER for plan plan-1: event gate EVENT:")
    assert "ORDER TICKET" not in result.ticket and "Buy " not in result.ticket
    (entry,) = [e for e in map(json.loads, journal.path.read_text(encoding="utf-8").splitlines())]
    assert entry["status"] == "no_order" and entry["stage"] == "risk" and entry["order"] is None
    assert entry["gate"]["status"] == "EVENT" and "trading halt today" in entry["gate"]["reason"]


@pytest.mark.parametrize("confidence", [0.01, 0.99])
def test_blocked_trader_confidence_cannot_move_the_gate(monkeypatch, tmp_path, confidence):
    result, *_ = run_scenario(monkeypatch, tmp_path, "red-flag", trader_confidence=confidence)
    assert result.proposal.ok is False and "event gate EVENT:" in result.proposal.reason


def test_the_refusal_is_deterministic_across_runs(monkeypatch, tmp_path):
    a, *_ = run_scenario(monkeypatch, tmp_path, "red-flag")
    reasons_a = a.proposal.verdict["reasons"]
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    b, *_ = run_scenario(monkeypatch, tmp2, "red-flag")
    assert b.proposal.verdict["reasons"] == reasons_a


def test_clean_buy_with_the_same_fakes_gets_a_sized_ticket_and_a_clear_gate(monkeypatch, tmp_path):
    result, journal, asked, client = run_scenario(monkeypatch, tmp_path, "clean-buy")
    p = result.proposal
    assert p.ok, p.reason
    assert p.gate["status"] == "CLEAR" and p.verdict == {"verdict": "PASS", "reasons": []}
    assert p.order["symbol"] == "BRKO" and p.order["qty"] == SHARES["clean-buy"]
    assert ("Event gate: CLEAR (no earnings through 2026-11-17 (FMP); "
            "last 8-K item 2.02 filed 2026-10-29") in result.ticket
    assert result.outcome.status == APPROVED and len(asked) == 1
    (entry,) = raw_lines(journal)
    assert entry["gate"]["status"] == "CLEAR" and entry["order"] == p.order


def test_blocked_clean_buy_turns_into_a_refusal_when_the_calendar_fetch_fails(monkeypatch,
    tmp_path):
    scenario = load_scenario("clean-buy")
    monkeypatch.setattr(ScenarioData, "earnings_calendar", lambda self, s, e: (_ for _ in ()).throw(
        RuntimeError("HTTP 429 from /stable/earnings-calendar")))
    result, journal, asked, _ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert scenario.symbol == "BRKO"
    assert result.proposal.ok is False and result.proposal.gate["status"] == "UNKNOWN"
    assert ("event gate UNKNOWN: UNKNOWN (fmp): earnings source unavailable: HTTP 429"
            in result.proposal.reason)
    assert result.outcome.status == "no_order" and asked == []


def test_blocked_clean_buy_with_a_halt_headline_added_is_refused(monkeypatch, tmp_path):
    original = ScenarioData.news

    def with_halt(self, symbols, start, end):
        out = original(self, symbols, start, end)
        halt = news_item("BRKO", "Trading halted in BRKO pending news", end - timedelta(minutes=5))
        return {**out, "news": [*out["news"], halt]}

    monkeypatch.setattr(ScenarioData, "news", with_halt)
    result, *_ = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.proposal.ok is False
    assert ("EVENT (news): trading halt today: 'Trading halted in BRKO pending news'"
            in result.proposal.reason)


def test_extended_chase_and_tsla_scenarios_have_a_clear_gate_at_the_run_clock():
    for name in ("clean-buy", "extended-chase", "tsla"):
        data = ScenarioData(load_scenario(name))
        now = data.as_of()
        gate = events_from(data)(data.symbol, now, now.replace(hour=15, minute=55, second=0))
        assert gate["status"] == "CLEAR", (name, gate["reason"])
    red = ScenarioData(load_scenario("red-flag"))
    gate = events_from(red)(red.symbol, red.as_of(), red.as_of().replace(hour=15, minute=55))
    assert gate["status"] == "EVENT"


# ---- through the smoke script's output (pipeline.smoke_debate) ----

def test_smoke_clean_buy_ticket_names_a_clear_event_gate(smoke, capsys):  # noqa: F811
    sym = "BRKO"
    code, out, _, _ = smoke("clean-buy", [*debaters(sym), reply(trader(sym))], capsys)
    assert code == 0
    assert "Event gate: CLEAR (no earnings through" in out and "Buy 37 shares of BRKO" in out
    assert tail(out, "clean-buy")[1].endswith("-> MATCH")


@pytest.mark.parametrize("confidence", [0.2, 0.95])
def test_blocked_smoke_red_flag_buy_is_refused_naming_halt_and_earnings(
        smoke, capsys, confidence):  # noqa: F811
    sym = "VNTR"
    replies = [*debaters(sym), reply(trader(sym, confidence=confidence))]
    code, out, client, _ = smoke("red-flag", replies, capsys)
    assert code == 0 and len(client.calls) == 3  # the Trader did say BUY
    assert "NO ORDER for plan plan-1: event gate EVENT:" in out
    assert "earnings today 2026-11-03" in out and "trading halt today" in out
    assert "refused at risk" in out
    assert "ORDER TICKET" not in out and "Buy 32 shares" not in out
    assert tail(out, "red-flag")[1].endswith("actual BUY (fatal Bear: no) -> MISMATCH")



# ============================ review: server error text never reaches the journal ============

def test_failure_text_cuts_a_server_error_payload_and_masks_the_key(monkeypatch):
    from pipeline.events import _failure
    monkeypatch.setenv("FMP_API_KEY", "sekret-key-123")
    payload = RuntimeError(
        "error payload from /stable/earnings-calendar: bad apikey sekret-key-123")
    assert _failure(payload) == "error payload from /stable/earnings-calendar"
    other = RuntimeError("HTTP 401 from /stable/earnings-calendar?apikey=sekret-key-123")
    assert "sekret-key-123" not in _failure(other) and "***" in _failure(other)
    assert _failure(ValueError("sekret-key-123")) == "ValueError"
