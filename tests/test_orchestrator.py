"""Orchestrator: concurrency, failure isolation, no secrets, clients, validation. No network."""
import _socket
import asyncio
import json
import socket
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

import agents.fundamentals as fund_agent
import agents.orchestrator as orch
import agents.sentiment as sent_agent
import agents.technical as tech_agent
from agents.llm import AgentConfig
from agents.runner import NEUTRAL_SOURCE, AnalystRun
from agents.schemas import CallRecord, DecisionPacket, UsageRecord
from pipeline.analysts import run_analysts_sync
from tests.fakes import (
    NOON,
    FakeAllData,
    FakeClient,
    api_error,
    report_json,
    response,
    text,
    tool_use,
)

SENTINEL = "sk-ant-SENTINEL-55555"
AGENTS = ("technical", "sentiment", "fundamentals")
PROMPT_TO_AGENT = {
    tech_agent.PROMPT: "technical",
    sent_agent.PROMPT: "sentiment",
    fund_agent.PROMPT: "fundamentals",
}


@pytest.fixture(autouse=True)
def loopback_only(_no_network, monkeypatch):
    """asyncio.run needs a loopback socketpair on Windows; allow 127.0.0.1/::1 and nothing else.

    The global guard in conftest.py stays in force for every other address.
    """
    guard_connect, guard_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def is_loopback(address):
        return isinstance(address, tuple) and address and address[0] in ("127.0.0.1", "::1")

    def connect(self, address):
        if is_loopback(address):
            return _socket.socket.connect(self, address)
        return guard_connect(self, address)

    def connect_ex(self, address):
        if is_loopback(address):
            return _socket.socket.connect_ex(self, address)
        return guard_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for agent in ("", "TECHNICAL_", "SENTIMENT_", "FUNDAMENTALS_"):
        for kind in ("MODEL", "EFFORT"):
            monkeypatch.delenv(f"TRADING_AGENTS_{agent}{kind}", raising=False)


TOOL = {"technical": "get_bars", "sentiment": "get_news", "fundamentals": "get_fundamentals"}


def first(agent, symbol="ABCD"):
    """The agent's first model turn: call its one data tool."""
    return response([tool_use("u1", TOOL[agent], {"symbol": symbol})], "tool_use")


def good(agent, symbol="ABCD"):
    """The final turn: a report citing the tool that returned data."""
    return response([text(report_json(
        agent=agent, symbol=symbol,
        evidence=[{"point": f"{agent} ok", "source": TOOL[agent]}]))], "end_turn")


def scripted(agent):
    """Replies for a complete successful run: tool call, then report."""
    return [first(agent), good(agent)]


class BarrierClient:
    """create() blocks until all three analysts are inside create() at the same time."""

    def __init__(self, agent, barrier, wait_s=2):
        self.agent, self.barrier, self.wait_s, self.calls = agent, barrier, wait_s, 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            # BrokenBarrierError unless the other two arrive too
            self.barrier.wait(timeout=self.wait_s)
            return first(self.agent)
        return good(self.agent)


class RouterClient:
    """One shared client: answers each analyst according to the system prompt it sent."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        agent = PROMPT_TO_AGENT[kwargs["system"][0]["text"]]
        with self._lock:
            self.calls.append(agent)
            n = self.calls.count(agent)
        return first(agent) if n == 1 else good(agent)


def run(coro_kwargs=None, **over):
    kwargs = {"data": FakeAllData(), "clients": RouterClient(), "timeout_s": 5}
    kwargs.update(coro_kwargs or {})
    kwargs.update(over)
    return asyncio.run(orch.run_analysts("ABCD", NOON, **kwargs))


def assert_neutral_failure(packet, agent, reason):
    rec, rep = packet.runs[agent], packet.reports[agent]
    assert rec.ok is False and rec.reason == reason
    assert rep.agent == agent and rep.symbol == "ABCD"
    assert rep.stance == "neutral" and rep.confidence == 0.0
    assert [e.source for e in rep.evidence] == [NEUTRAL_SOURCE]
    assert rep.evidence[0].point == f"{agent} analysis unavailable: {reason}"


def assert_ok(packet, *agents):
    for a in agents:
        assert packet.runs[a].ok is True and packet.runs[a].reason is None
        assert packet.reports[a].agent == a
        assert packet.reports[a].evidence[0].point == f"{a} ok"


# ---------------- happy path and shape ----------------

def test_happy_path_packet():
    client = RouterClient()
    packet = run(clients=client)
    assert isinstance(packet, DecisionPacket)
    assert packet.symbol == "ABCD" and packet.all_ok is True
    assert packet.as_of == "2026-11-03T12:00:00-05:00"
    assert set(packet.reports) == set(packet.runs) == set(AGENTS)
    assert_ok(packet, *AGENTS)
    assert sorted(client.calls) == sorted(AGENTS * 2)  # tool round + final report
    for a in AGENTS:
        rec = packet.runs[a]
        assert (rec.agent, rec.requested_model, rec.effort) == (a, "claude-sonnet-5-5", "high")
        assert len(rec.calls) == 2 and rec.calls[0].requested_model == "claude-sonnet-5-5"
        assert rec.duration_ms >= 0
    DecisionPacket.model_validate(json.loads(packet.model_dump_json()))


def test_symbol_is_normalised():
    packet = asyncio.run(orch.run_analysts(
        " abcd ", NOON, data=FakeAllData(), clients=RouterClient(), timeout_s=5))
    assert packet.symbol == "ABCD" and packet.reports["sentiment"].symbol == "ABCD"


def test_configs_override_and_default_fill_in():
    cfgs = {"sentiment": AgentConfig(model="m-sent", effort="low")}
    packet = run(configs=cfgs)
    assert (packet.runs["sentiment"].requested_model, packet.runs["sentiment"].effort) == (
        "m-sent", "low")
    assert packet.runs["technical"].requested_model == "claude-sonnet-5-5"
    assert packet.runs["fundamentals"].effort == "high"


def test_env_config_used_when_no_configs(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_FUNDAMENTALS_MODEL", "env-fund")
    packet = run()
    assert packet.runs["fundamentals"].requested_model == "env-fund"
    assert packet.runs["technical"].requested_model == "claude-sonnet-5-5"


# ---------------- concurrency ----------------

def test_all_three_run_concurrently():
    barrier = threading.Barrier(3)
    clients = {a: BarrierClient(a, barrier) for a in AGENTS}
    packet = run(clients=clients)
    # the barrier only releases when all three create() calls overlap in time
    assert packet.all_ok is True, {a: r.reason for a, r in packet.runs.items()}
    assert all(c.calls == 2 for c in clients.values())
    assert barrier.broken is False


def test_barrier_test_would_fail_if_sequential():
    # sanity check of the technique: with only two parties arriving, the barrier breaks.
    barrier = threading.Barrier(3)
    clients = {a: BarrierClient(a, barrier, wait_s=0.3) for a in AGENTS}
    replies = iter(scripted("sentiment"))
    clients["sentiment"] = SimpleNamespace(  # never waits on the barrier
        beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: next(replies))))
    packet = run(clients=clients)
    assert packet.all_ok is False
    assert packet.runs["sentiment"].ok is True
    # the two waiting analysts hit BrokenBarrierError, which the runner turns into neutral
    assert packet.runs["technical"].ok is False and packet.runs["fundamentals"].ok is False


# ---------------- failure isolation ----------------

def test_api_error_inside_runner_gives_neutral_and_others_ok():
    clients = {
        "technical": FakeClient(scripted("technical")),
        "sentiment": FakeClient([api_error(f"bad key {SENTINEL}")]),
        "fundamentals": FakeClient(scripted("fundamentals")),
    }
    packet = run(clients=clients)
    assert packet.all_ok is False
    assert_ok(packet, "technical", "fundamentals")
    # the runner already converts the error; its reason is the exception type name
    assert_neutral_failure(packet, "sentiment", "APIConnectionError")
    assert SENTINEL not in packet.model_dump_json()


def test_unexpected_runner_exception_gives_neutral_failed_reason(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError(f"explode {SENTINEL}")

    monkeypatch.setitem(orch._RUNNERS, "fundamentals", boom)
    packet = run()
    assert packet.all_ok is False
    assert_ok(packet, "technical", "sentiment")
    assert_neutral_failure(packet, "fundamentals", "failed (RuntimeError)")
    assert packet.runs["fundamentals"].calls == []
    assert SENTINEL not in packet.model_dump_json()


@pytest.mark.parametrize("victim", AGENTS)
def test_any_one_agent_can_fail(monkeypatch, victim):
    def boom(*a, **kw):
        raise ValueError("x")

    monkeypatch.setitem(orch._RUNNERS, victim, boom)
    packet = run()
    assert packet.all_ok is False
    assert_neutral_failure(packet, victim, "failed (ValueError)")
    assert_ok(packet, *[a for a in AGENTS if a != victim])


def test_timeout_gives_neutral_and_others_ok(monkeypatch):
    release = threading.Event()

    def slow(symbol, now, data, *, client, config, calls_sink, cancel=None):
        calls_sink.append(CallRecord(
            requested_model="m", serving_model="m", fallback_ran=False,
            usage=UsageRecord(input_tokens=3)))
        release.wait(timeout=10)  # simulates a hung API call; released in finally
        raise AssertionError("result must be discarded")

    monkeypatch.setitem(orch._RUNNERS, "sentiment", slow)
    try:
        packet = run(timeout_s=0.3)
    finally:
        release.set()
    assert packet.all_ok is False
    assert_ok(packet, "technical", "fundamentals")
    assert_neutral_failure(packet, "sentiment", "timeout")
    # calls made before the timeout are still reported
    assert [c.usage.input_tokens for c in packet.runs["sentiment"].calls] == [3]
    assert packet.runs["sentiment"].duration_ms >= 250


def test_two_failures_one_success(monkeypatch):
    def boom(*a, **kw):
        raise OSError("x")

    monkeypatch.setitem(orch._RUNNERS, "technical", boom)
    monkeypatch.setitem(orch._RUNNERS, "sentiment", boom)
    packet = run()
    assert packet.all_ok is False
    assert_neutral_failure(packet, "technical", "failed (OSError)")
    assert_neutral_failure(packet, "sentiment", "failed (OSError)")
    assert_ok(packet, "fundamentals")


def test_failed_analyst_returned_run_not_ok_makes_all_ok_false(monkeypatch):
    def not_ok(symbol, now, data, *, client, config, calls_sink, cancel=None):
        from agents.runner import neutral_report
        return AnalystRun(neutral_report("technical", symbol, "r"), False, "r", [], 1)

    monkeypatch.setitem(orch._RUNNERS, "technical", not_ok)
    packet = run()
    assert packet.all_ok is False and packet.runs["technical"].reason == "r"


def test_sec_failure_inside_session_does_not_fail_the_run():
    # an SEC failure only adds a note to the digest; the analyst still finishes ok
    packet = run(data=FakeAllData(subs_raises=RuntimeError("down")))
    assert packet.all_ok is True


def test_news_failure_fails_only_the_sentiment_analyst():
    # get_news errored, so no report can cite it: sentiment ends neutral, the rest are fine
    packet = run(data=FakeAllData(news_raises=RuntimeError("down")))
    assert packet.all_ok is False
    assert_ok(packet, "technical", "fundamentals")
    assert packet.runs["sentiment"].ok is False
    assert packet.runs["sentiment"].reason == (
        "evidence source get_news is not a tool that answered in this run")


# ---------------- no secrets ----------------

def test_exception_text_never_in_packet_json(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError(f"key={SENTINEL}")

    monkeypatch.setitem(orch._RUNNERS, "technical", boom)
    clients = {
        "technical": FakeClient(),
        "sentiment": FakeClient([api_error(f"leak {SENTINEL}")]),
        "fundamentals": FakeClient([response([text(SENTINEL)], "end_turn")] * 2),
    }
    packet = run(clients=clients)
    dumped = packet.model_dump_json()
    assert SENTINEL not in dumped
    assert SENTINEL not in json.dumps(packet.model_dump(mode="json"))
    assert packet.all_ok is False


def test_leaky_data_exception_not_in_packet(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    packet = run(data=FakeAllData(facts_raises=RuntimeError(SENTINEL)))
    assert SENTINEL not in packet.model_dump_json()


# ---------------- clients ----------------

def test_single_shared_client_serves_all_three():
    client = RouterClient()
    packet = run(clients=client)
    assert packet.all_ok is True
    assert sorted(client.calls) == sorted(AGENTS * 2)  # tool round + final report


def test_per_agent_client_dict_is_routed_by_name():
    clients = {a: FakeClient(scripted(a)) for a in AGENTS}
    packet = run(clients=clients)
    assert packet.all_ok is True
    for a, c in clients.items():
        assert len(c.calls) == 2 and c.calls[0]["system"][0]["text"] == {
            "technical": tech_agent.PROMPT, "sentiment": sent_agent.PROMPT,
            "fundamentals": fund_agent.PROMPT}[a]


def test_dict_missing_an_agent_gives_that_agent_a_neutral_report():
    clients = {a: FakeClient(scripted(a)) for a in ("technical", "sentiment")}
    packet = run(clients=clients)
    assert packet.all_ok is False
    assert_ok(packet, "technical", "sentiment")
    assert packet.runs["fundamentals"].ok is False
    assert packet.reports["fundamentals"].evidence[0].source == NEUTRAL_SOURCE


# ---------------- validation up front ----------------

@pytest.mark.parametrize("bad", ["", "AB CD", "   ", None, 5, "TOOLONGSYMBOLX"])
def test_invalid_symbol_raises_before_any_call(bad):
    client = FakeClient()
    with pytest.raises(ValueError):
        asyncio.run(orch.run_analysts(bad, NOON, data=FakeAllData(), clients=client))
    assert client.calls == []


def test_naive_now_raises_before_any_call():
    client = FakeClient()
    data = FakeAllData()
    with pytest.raises(ValueError):
        asyncio.run(orch.run_analysts(
            "ABCD", datetime(2026, 11, 3, 12, 0), data=data, clients=client))
    assert client.calls == [] and data.calls == []


def test_neutral_source_is_reexported():
    assert orch.NEUTRAL_SOURCE == NEUTRAL_SOURCE == "analyst_unavailable"
    assert orch.AGENTS == AGENTS


# ---------------- pipeline.analysts ----------------

def test_run_analysts_sync_returns_valid_json_packet():
    out = run_analysts_sync("ABCD", NOON, data=FakeAllData(), client=RouterClient())
    assert isinstance(out, dict)
    json.dumps(out)
    packet = DecisionPacket.model_validate(out)
    assert packet.all_ok is True and packet.symbol == "ABCD"
    assert set(out["reports"]) == set(AGENTS)


def test_run_analysts_sync_accepts_explicit_configs():
    cfgs = {a: AgentConfig(model="m-" + a, effort="medium") for a in AGENTS}
    out = run_analysts_sync("ABCD", NOON, data=FakeAllData(), client=RouterClient(), configs=cfgs)
    assert out["runs"]["technical"]["requested_model"] == "m-technical"
    assert out["runs"]["sentiment"]["effort"] == "medium"


def test_run_analysts_sync_failure_still_returns_packet(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setitem(orch._RUNNERS, "sentiment", boom)
    out = run_analysts_sync("ABCD", NOON, data=FakeAllData(), client=RouterClient())
    assert out["all_ok"] is False and out["runs"]["sentiment"]["reason"] == "failed (RuntimeError)"
    DecisionPacket.model_validate(out)


def test_run_analysts_sync_without_client_needs_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        run_analysts_sync("ABCD", NOON, data=FakeAllData())


def test_run_analysts_sync_invalid_input_raises():
    with pytest.raises(ValueError):
        run_analysts_sync("", NOON, data=FakeAllData(), client=RouterClient())
    with pytest.raises(ValueError):
        run_analysts_sync("ABCD", datetime(2026, 11, 3), data=FakeAllData(),
                          client=RouterClient())


def test_guard_still_blocks_non_loopback():
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="network access in tests"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()
