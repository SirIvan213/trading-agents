"""Metering of run_day's billed calls (#157, #156): Claude through MeteredClient, FMP and Alpha
Vantage through metered_fetch. Offline: SDK objects built locally, fake inner clients and
fetches, tmp_path budgets. No network, no billed call."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import httpx2
import pytest

import tools.earnings_calendar as ec
from pipeline.budget import CapReached, Caps, DailyBudget
from pipeline.events import calendar_input, fmp_calendar_input
from pipeline.metering import (
    FALLBACK_FACTOR,
    MeteredClient,
    metered_fetch,
    metered_live_data,
    worst_case_tokens,
)
from tests.test_daily_budget import sdk_response
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)

NY = ZoneInfo("America/New_York")
DAY = date(2026, 11, 23)
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
KW = {"model": "claude-opus-5-5", "max_tokens": 1000, "system": "s" * 200,
      "messages": [{"role": "user", "content": "m" * 400}], "tools": []}


def status_error(code: int) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(f"HTTP {code}", response=httpx2.Response(code, request=REQ),
                                    body=None)


def no_answer() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=REQ)


class Inner:
    """A fake Anthropic client: `outcomes` are returned or raised in order."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.options: list[dict] = []
        self.beta = type("B", (), {})()
        self.beta.messages = type("M", (), {"create": self._create})()

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    def with_options(self, **options):
        self.options.append(options)
        return self


def budget(tmp_path, max_tokens=600_000):
    b = DailyBudget(Caps(10, max_tokens, 3, 5), DAY, tmp_path)
    b.open_day(started_today=False)
    return b


def client(inner, b):
    return MeteredClient(inner, b, sleep=lambda s: None)


# ======================= the worst case =======================

def test_the_worst_case_covers_input_output_and_a_fallback():
    base = worst_case_tokens(KW)
    assert base >= 1000 + 300  # max_tokens + (600+ chars / 2)
    assert worst_case_tokens({**KW, "fallbacks": "default"}) >= base * FALLBACK_FACTOR
    assert worst_case_tokens({**KW, "max_tokens": 2000}) == base + 1000
    bigger = {**KW, "messages": [{"role": "user", "content": "m" * 4000}]}
    assert worst_case_tokens(bigger) > base


def test_non_ascii_text_never_counts_for_less_than_its_escape():
    plain = {**KW, "messages": [{"role": "user", "content": "a" * 100}]}
    emoji = {**KW, "messages": [{"role": "user", "content": "\U0001F680" * 100}]}
    assert worst_case_tokens(emoji) - worst_case_tokens(plain) >= 500  # ~12 chars each


def test_every_request_field_counts_toward_the_worst_case():
    more = {**KW, "tool_choice": {"type": "auto"}, "extra_body": {"x": "y" * 1000}}
    assert worst_case_tokens(more) >= worst_case_tokens(KW) + 500


def test_the_fallback_factor_covers_the_documented_chain():
    """Two supported fallback targets today: up to three attempts per call."""
    assert FALLBACK_FACTOR == 3


def test_the_worst_case_over_estimates_a_real_response():
    """sdk_response's call billed 2950 tokens; a request that short reserves far more."""
    response = sdk_response()
    assert worst_case_tokens({**KW, "max_tokens": 16000, "fallbacks": "default"}) > 2950
    assert response.usage.input_tokens == 1000


# ======================= Claude calls =======================

def test_a_call_settles_to_its_billed_tokens_iterations_included(tmp_path):
    b = budget(tmp_path)
    inner = Inner(sdk_response())  # a fallback: 2950 billed, 1700 at the top level
    out = client(inner, b).beta.messages.create(**KW)
    assert out is not None and len(inner.calls) == 1
    s = b.snapshot()
    assert s["tokens_used"] == 2950 and s["reservations"] == {}


def test_blocked_a_call_whose_worst_case_would_pass_the_cap_is_never_made(tmp_path):
    b = budget(tmp_path, max_tokens=worst_case_tokens(KW) - 1)
    inner = Inner(sdk_response())
    with pytest.raises(CapReached, match="token cap"):
        client(inner, b).beta.messages.create(**KW)
    assert inner.calls == []


@pytest.mark.parametrize("code", [400, 401, 403, 404, 413])
def test_a_definite_error_is_not_retried_and_nothing_is_counted(tmp_path, code):
    b = budget(tmp_path)
    inner = Inner(status_error(code))
    with pytest.raises(anthropic.APIStatusError):
        client(inner, b).beta.messages.create(**KW)
    assert len(inner.calls) == 1
    s = b.snapshot()
    assert s["tokens_used"] == 0 and s["reservations"] == {}


@pytest.mark.parametrize("code", [408, 409, 429, 500, 529])
def test_a_retryable_error_is_retried_once_through_the_meter(tmp_path, code):
    b = budget(tmp_path)
    inner = Inner(status_error(code), sdk_response())
    client(inner, b).beta.messages.create(**KW)
    assert len(inner.calls) == 2
    s = b.snapshot()
    assert s["tokens_used"] == 2950 and s["reservations"] == {}  # the failed try billed 0


def test_blocked_a_second_failure_is_not_retried_again(tmp_path):
    b = budget(tmp_path)
    inner = Inner(status_error(500), status_error(500), sdk_response())
    with pytest.raises(anthropic.APIStatusError):
        client(inner, b).beta.messages.create(**KW)
    assert len(inner.calls) == 2  # one retry only


def test_a_call_with_no_answer_keeps_its_reservation_and_is_retried_once(tmp_path):
    b = budget(tmp_path)
    inner = Inner(no_answer(), sdk_response())
    client(inner, b).beta.messages.create(**KW)
    assert len(inner.calls) == 2
    s = b.snapshot()
    assert s["tokens_used"] == 2950
    assert list(s["reservations"].values()) == [worst_case_tokens(KW)]  # unknown: counted


def test_blocked_two_calls_with_no_answer_keep_both_reservations(tmp_path):
    b = budget(tmp_path)
    inner = Inner(no_answer(), no_answer())
    with pytest.raises(anthropic.APIConnectionError):
        client(inner, b).beta.messages.create(**KW)
    assert sorted(b.snapshot()["reservations"].values()) == [worst_case_tokens(KW)] * 2


def test_blocked_the_retry_reserves_its_own_worst_case_and_can_be_refused(tmp_path):
    """After a call with no answer, the cap has room for one worst case only: the retry's
    reservation is refused and no second request is made."""
    b = budget(tmp_path, max_tokens=worst_case_tokens(KW) + 10)
    inner = Inner(no_answer(), sdk_response())
    with pytest.raises(CapReached):
        client(inner, b).beta.messages.create(**KW)
    assert len(inner.calls) == 1


def test_another_exception_is_not_retried_and_stays_counted(tmp_path):
    b = budget(tmp_path)
    inner = Inner(ValueError("a bug"), sdk_response())
    with pytest.raises(ValueError):
        client(inner, b).beta.messages.create(**KW)
    assert len(inner.calls) == 1 and len(b.snapshot()["reservations"]) == 1


def test_with_options_stays_metered_and_forces_sdk_retries_off(tmp_path):
    b = budget(tmp_path)
    inner = Inner(sdk_response())
    timed = client(inner, b).with_options(timeout=30.0, max_retries=5)
    assert isinstance(timed, MeteredClient)
    timed.beta.messages.create(**KW)
    assert inner.options == [{"timeout": 30.0, "max_retries": 0}]
    assert b.snapshot()["tokens_used"] == 2950


def test_an_odd_usage_block_keeps_the_worst_case_counted(tmp_path):
    b = budget(tmp_path)

    class Odd:
        usage = None  # nothing to read: the call must not count as free

    inner = Inner(Odd())
    client(inner, b).beta.messages.create(**KW)
    s = b.snapshot()
    assert s["tokens_used"] == 0
    assert list(s["reservations"].values()) == [worst_case_tokens(KW)]


# ======================= FMP and Alpha Vantage =======================

def test_requests_are_counted_before_they_are_made(tmp_path):
    b = budget(tmp_path)
    seen = []

    def fetch(*a):
        seen.append(b.snapshot()["av_calls"])  # already counted when the request runs
        raise TimeoutError

    f = metered_fetch(b, "av", fetch)
    with pytest.raises(TimeoutError):
        f()
    assert seen == [1] and b.snapshot()["av_calls"] == 1  # a failed request counts


@pytest.mark.parametrize("source,cap", [("fmp", 3), ("av", 5)])
def test_blocked_past_the_cap_no_request_is_made(tmp_path, source, cap):
    b = budget(tmp_path)
    made = []
    f = metered_fetch(b, source, lambda *a: made.append(1))
    for _ in range(cap):
        f()
    with pytest.raises(CapReached):
        f()
    assert len(made) == cap


# The calendar caches stamp replies with the real clock, so these tests use the real day.
@pytest.fixture
def today():
    """The real New York day, read per test (a run across midnight stays consistent)."""
    return datetime.now(NY).date()


def test_blocked_after_5_av_calls_the_gate_input_is_unavailable(tmp_path, monkeypatch, today):
    """Five failed Alpha Vantage calls, then no more requests: the calendar input reads
    unavailable (the gate says UNKNOWN), and run_day never calls Alpha Vantage again."""
    calls = []

    def av_down():
        calls.append(1)
        raise TimeoutError

    monkeypatch.setattr(ec, "fetch_av_earnings_calendar_typed", av_down)
    monkeypatch.setattr(ec, "fetch_fmp_earnings_calendar", lambda s, e: [])
    b = budget(tmp_path / "state")
    for _ in range(8):  # a fresh LiveData each time: no in-memory retry wait helps
        data = metered_live_data(b, tmp_path / "cache")
        out = calendar_input(data, today)
        assert out["ok"] is False
    assert len(calls) == 5 and b.snapshot()["av_calls"] == 5


def test_a_valid_av_calendar_is_reused_for_the_day(tmp_path, monkeypatch, today):
    from tests.fakes import av_csv
    calls = []

    def av_ok():
        calls.append(1)
        from datetime import timedelta

        from pipeline.events import CALENDAR_HORIZON_DAYS
        return av_csv(start=today,
                      end=today + timedelta(days=CALENDAR_HORIZON_DAYS))

    monkeypatch.setattr(ec, "fetch_av_earnings_calendar_typed", av_ok)
    b = budget(tmp_path / "state")
    for _ in range(4):
        data = metered_live_data(b, tmp_path / "cache")
        assert calendar_input(data, today)["ok"] is True
    assert len(calls) == 1 and b.snapshot()["av_calls"] == 1


def test_blocked_after_3_fmp_requests_run_day_runs_without_fmp(tmp_path, monkeypatch, today):
    calls = []

    def fmp_bad(start, end):
        calls.append(1)
        return {"Error Message": "limit"}  # fails parse_fmp_calendar: a failed request

    monkeypatch.setattr(ec, "fetch_fmp_earnings_calendar", fmp_bad)
    b = budget(tmp_path / "state")
    for _ in range(6):
        data = metered_live_data(b, tmp_path / "cache")
        out = fmp_calendar_input(data, today)
        assert out["ok"] is False  # block-only: its absence clears nothing
    assert len(calls) == 3 and b.snapshot()["fmp_requests"] == 3


# ======================= end to end: a real decision through the meter =======================

def test_a_whole_decision_runs_through_the_meter_and_settles_every_call(tmp_path, monkeypatch):
    """The real run_decision (three analysts in parallel threads, the debate, the Trader)
    with a MeteredClient over the fake models: every create() reserves and settles, nothing
    stays reserved, and the day's tokens are exactly the calls' usage."""
    from types import SimpleNamespace

    import agents.orchestrator as orch
    import pipeline.order_flow as of
    from journal.log import Journal
    from pipeline.decide import run_decision
    from tests.fakes import FakeAllData
    from tests.order_helpers import QuotingData, StepClock
    from tests.test_phase4b_proofs import LIMITS, NOON, SYMBOL, Router, scripts

    monkeypatch.setattr(orch, "load_limits", lambda *a, **k: LIMITS)
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)
    router = Router(scripts())
    made = []

    class WithUsage:  # the fake models, each reply carrying usage like a real response
        def __init__(self):
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

        def create(self, **kw):
            r = router.beta.messages.create(**kw)
            r.usage = SimpleNamespace(input_tokens=100, output_tokens=50,
                                      cache_creation_input_tokens=0,
                                      cache_read_input_tokens=0, iterations=None)
            made.append(1)
            return r

    b = budget(tmp_path / "state")
    metered = MeteredClient(WithUsage(), b, sleep=lambda s: None)
    clock = StepClock(NOON, NOON, NOON + timedelta(seconds=20), NOON + timedelta(seconds=40))
    result = run_decision(SYMBOL, data=QuotingData(FakeAllData(), clock, SYMBOL, "103.10"),
                          clients=metered, journal=Journal(tmp_path / "j.jsonl"),
                          clock=clock, ask=lambda _: "no", rounds=1, timeout_s=10)
    assert result.trader.ok and len(made) == len(router.calls) >= 6
    s = b.snapshot()
    assert s["reservations"] == {}  # every call settled
    assert s["tokens_used"] == 150 * len(made)
