"""Issue #52: a stable reason_code vocabulary on AnalystRun and AgentRunRecord. Offline."""
import asyncio
import json
import threading
from typing import get_args

import pytest

import agents.orchestrator as orch
from agents.llm import AgentConfig
from agents.runner import AnalystRun, AnalystSpec, neutral_report, run_analyst
from agents.schemas import AgentRunRecord, DecisionPacket, ReasonCode
from tests.fakes import (
    NOON,
    NOW,
    FakeAllData,
    FakeClient,
    api_error,
    report_json,
    response,
    text,
    tool_use,
)
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tests.test_runner import ScriptSession

SPEC = AnalystSpec(name="sentiment", prompt="PROMPT", tools=[{"name": "get_news"}],
                   output_schema={"type": "object"})
SECRET = "sk-ant-SENTINEL-reason"


def tool_round():
    return response([tool_use("t1", "get_news", {"symbol": "ABCD"})], "tool_use")


def good():
    return response([text(report_json(
        agent="sentiment", symbol="ABCD", evidence=[{"point": "p", "source": "get_news"}]))],
        "end_turn")


def junk():
    return response([text("not json")], "end_turn")


def run(queue, config=None, cancel=None, session=None):
    return run_analyst(
        SPEC, "ABCD", NOW, session or ScriptSession(), client=FakeClient(queue),
        config=config or AgentConfig(), cancel=cancel)


ALL_CODES = {
    "api_error", "timeout", "cancelled", "refusal", "max_tokens", "invalid_output",
    "tool_rounds", "config_error", "exception",
}


def test_vocabulary_is_exactly_the_documented_codes():
    assert set(get_args(ReasonCode)) == ALL_CODES


def test_ok_run_has_no_reason_code():
    r = run([tool_round(), good()])
    assert r.ok is True and r.reason is None and r.reason_code is None


def test_ok_after_a_failed_first_attempt_has_no_reason_code():
    r = run([tool_round(), junk(), good()])
    assert r.ok is True and r.reason_code is None


def test_api_error():
    r = run([api_error(f"leak {SECRET}")])
    assert (r.ok, r.reason_code, r.reason) == (False, "api_error", "APIConnectionError")
    assert SECRET not in r.reason and SECRET not in r.report.model_dump_json()


def test_exception():
    r = run([RuntimeError(f"boom {SECRET}")])
    assert (r.ok, r.reason_code, r.reason) == (False, "exception", "RuntimeError")
    assert SECRET not in r.report.model_dump_json()


def test_exception_from_a_tool_session():
    class Boom:
        def execute(self, name, tool_input):
            raise KeyError(SECRET)

    r = run([tool_round()], session=Boom())
    assert r.ok is False and r.reason_code == "exception" and r.reason == "KeyError"


@pytest.mark.parametrize(("stop", "code"), [("refusal", "refusal"), ("max_tokens", "max_tokens")])
def test_stop_reason_codes(stop, code):
    r = run([response([], stop), response([], stop)])
    assert (r.ok, r.reason_code, r.reason) == (False, code, f"model stopped with {stop}")


@pytest.mark.parametrize("stop", ["stop_sequence", "something_new", "context_exceeded"])
def test_unexpected_stop_reason_is_invalid_output(stop):
    r = run([response([], stop), response([], stop)])
    assert (r.ok, r.reason_code) == (False, "invalid_output")
    assert r.reason == f"model stopped with {stop}"


def test_endless_pause_turn_is_tool_rounds():
    r = run([response([], "pause_turn")] * 6, config=AgentConfig(max_tool_rounds=1))
    assert (r.ok, r.reason_code) == (False, "tool_rounds")


def test_unparseable_report_is_invalid_output():
    r = run([tool_round(), junk(), junk()])
    assert (r.ok, r.reason_code) == (False, "invalid_output")


def test_rejected_catalyst_is_invalid_output():
    bad = response([text(report_json(
        agent="sentiment", symbol="ABCD", evidence=[{"point": "p", "source": "get_news"}],
        catalysts=[{"name": "moon_landing", "source": "get_news"}]))], "end_turn")
    r = run([tool_round(), bad, bad])
    assert (r.ok, r.reason_code) == (False, "invalid_output")


def test_code_comes_from_the_last_attempt():
    r = run([response([], "refusal"), junk()])  # first attempt refused, retry gave junk
    assert r.reason_code == "invalid_output"
    r = run([junk(), response([], "max_tokens")])
    assert r.reason_code == "max_tokens"


def test_tool_rounds():
    r = run([tool_round()] * 6, config=AgentConfig(max_tool_rounds=1))
    assert (r.ok, r.reason_code, r.reason) == (False, "tool_rounds", "too many tool rounds")


def test_cancelled():
    ev = threading.Event()
    ev.set()
    r = run([good()], cancel=ev)
    assert (r.ok, r.reason_code, r.reason) == (False, "cancelled", "cancelled")


def test_default_reason_code_is_none():
    assert AnalystRun(neutral_report("sentiment", "X", "r"), True, None, [], 1).reason_code is None


# ---------- orchestrator-level codes: timeout, config_error, exception; packet JSON ----------

AGENT_TOOL = {"sentiment": "get_news", "fundamentals": "get_fundamentals"}


def script(agent):
    return [
        response([tool_use("u", AGENT_TOOL[agent], {"symbol": "ABCD"})], "tool_use"),
        response([text(report_json(
            agent=agent, symbol="ABCD",
            evidence=[{"point": "ok", "source": AGENT_TOOL[agent]}]))], "end_turn"),
    ]


def tech_script():
    return [
        response([tool_use("b", "get_bars", {"symbol": "ABCD"})], "tool_use"),
        response([text(report_json(
            agent="technical", symbol="ABCD",
            evidence=[{"point": "ok", "source": "get_bars"}]))], "end_turn"),
    ]


def packet(technical_queue=None, timeout_s=5):
    clients = {
        "technical": FakeClient(technical_queue or []),
        "sentiment": FakeClient(script("sentiment")),
        "fundamentals": FakeClient(script("fundamentals")),
    }
    return asyncio.run(orch.run_analysts(
        "ABCD", NOON, data=FakeAllData(), clients=clients, timeout_s=timeout_s))


def test_timeout(monkeypatch):
    release = threading.Event()

    def slow(*a, **k):
        release.wait(timeout=10)
        raise AssertionError("discarded")

    monkeypatch.setitem(orch._RUNNERS, "technical", slow)
    try:
        p = packet(timeout_s=0.3)
    finally:
        release.set()
    rec = p.runs["technical"]
    assert (rec.ok, rec.reason, rec.reason_code) == (False, "timeout", "timeout")


def test_config_error_from_a_value_error_in_a_worker(monkeypatch):
    def bad(*a, **k):
        raise ValueError(f"limits {SECRET}")

    monkeypatch.setitem(orch._RUNNERS, "technical", bad)
    rec = packet().runs["technical"]
    assert (rec.reason, rec.reason_code) == ("failed (ValueError)", "config_error")
    assert SECRET not in rec.model_dump_json()


def test_orchestrator_exception(monkeypatch):
    def bad(*a, **k):
        raise OSError(SECRET)

    monkeypatch.setitem(orch._RUNNERS, "technical", bad)
    rec = packet().runs["technical"]
    assert (rec.reason, rec.reason_code) == ("failed (OSError)", "exception")
    assert SECRET not in rec.model_dump_json()


def test_runner_codes_flow_into_the_agent_run_record():
    p = packet([api_error("x")])
    rec = p.runs["technical"]
    assert (rec.ok, rec.reason, rec.reason_code) == (False, "APIConnectionError", "api_error")
    assert p.runs["sentiment"].reason_code is None  # ok runs carry no code
    assert p.runs["fundamentals"].reason_code is None


def test_reason_code_is_in_the_packet_json():
    p = packet([api_error("x")])
    dumped = json.loads(p.model_dump_json())
    assert dumped["runs"]["technical"]["reason_code"] == "api_error"
    assert dumped["runs"]["sentiment"]["reason_code"] is None
    ok = packet(tech_script())
    assert all(r.reason_code is None for r in ok.runs.values())
    assert "reason_code" in json.loads(ok.model_dump_json())["runs"]["technical"]


def test_packet_schema_includes_the_reason_code_enum():
    schema = DecisionPacket.model_json_schema()
    props = schema["$defs"]["AgentRunRecord"]["properties"]
    assert "reason_code" in props
    enum_values = {v for branch in props["reason_code"]["anyOf"] for v in branch.get("enum", [])}
    assert enum_values == ALL_CODES
    assert "reason" in props  # free text stays beside the code


def test_agent_run_record_rejects_unknown_code():
    with pytest.raises(ValueError):
        AgentRunRecord(agent="x", requested_model="m", effort="e", ok=False,
                       reason_code="made_up", duration_ms=1)  # type: ignore[arg-type]
