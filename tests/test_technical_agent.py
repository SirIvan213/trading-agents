"""Technical Analyst loop, config and client factory. Fake client only: no API, no network."""
import json
from datetime import datetime

import anthropic
import pytest

import agents.technical as tech
from agents.llm import AgentConfig, config_from_env, make_client
from agents.schemas import AnalystReport
from agents.tool_registry import TOOLS
from pipeline.technical import run_technical
from tests.fakes import (
    NOW,
    FakeClient,
    FakeMarketData,
    api_error,
    report_json,
    response,
    text,
    tool_use,
)

SENTINEL = "sk-ant-SENTINEL-1234567890"
REFUSAL = "opening_range and atr come from get_bars, not from the model"


def run(queue, *, config=None, symbol="AAPL", now=NOW, data=None):
    client = FakeClient(queue)
    report = tech.run_technical_analyst(
        symbol, now, data or FakeMarketData(), client=client, config=config
    )
    return report, client


def assert_neutral(report, symbol="AAPL"):
    assert report.agent == "technical"
    assert report.symbol == symbol
    assert report.stance == "neutral"
    assert report.confidence == 0.0
    assert len(report.evidence) == 1
    assert report.evidence[0].source == "technical_analyst"
    assert report.evidence[0].point.startswith("technical analysis unavailable: ")


def good(**over):
    return response([text(report_json(**over))], "end_turn")


def bad():
    return response([text("not json")], "end_turn")


# ---- happy path ----

def happy_queue():
    return [
        response([tool_use("t1", "get_bars", {"symbol": "AAPL"})], "tool_use"),
        response(
            [tool_use("t2", "plan_exit", {"setup": "orb", "entry": "102.50"})], "tool_use"
        ),
        good(),
    ]


def test_happy_path_report():
    report, client = run(happy_queue())
    assert report == AnalystReport(
        agent="technical", symbol="AAPL", stance="bullish", confidence=0.7,
        evidence=[{"point": "above vwap", "source": "get_bars"}], catalysts=[],
    )
    assert len(client.calls) == 3
    assert client.queue == []


def test_happy_path_request_kwargs():
    _, client = run(happy_queue())
    for kw in client.calls:
        assert kw["model"] == "claude-opus-5-5"
        assert kw["max_tokens"] == 16000
        assert kw["output_config"]["effort"] == "high"
        assert kw["output_config"]["format"]["type"] == "json_schema"
        assert kw["output_config"]["format"]["schema"] == tech.OUTPUT_SCHEMA
        assert kw["betas"] == ["server-side-fallback-2026-07-01"]
        assert kw["fallbacks"] == "default"
        assert kw["tools"] == TOOLS
        assert kw["system"] == [
            {"type": "text", "text": tech.PROMPT, "cache_control": {"type": "ephemeral"}}
        ]
        for forbidden in ("thinking", "temperature", "tool_choice", "top_p", "top_k"):
            assert forbidden not in kw
        assert set(kw) == {
            "model", "max_tokens", "system", "tools", "messages", "output_config",
            "betas", "fallbacks",
        }


def test_system_prompt_is_technical_md():
    from pathlib import Path

    path = Path(tech.__file__).parent / "prompts" / "technical.md"
    assert tech.PROMPT == path.read_text(encoding="utf-8")
    assert tech.PROMPT.strip()


def test_history_is_append_only():
    _, client = run(happy_queue())
    msgs = [c["messages"] for c in client.calls]
    assert [len(m) for m in msgs] == [1, 3, 5]
    for prev, nxt in zip(msgs, msgs[1:], strict=False):
        assert nxt[: len(prev)] == prev
    assert msgs[0][0]["role"] == "user"
    assert "Analyse AAPL" in msgs[0][0]["content"]
    assert "2026-11-03T10:30:00-05:00" in msgs[0][0]["content"]


def test_tool_results_roundtrip_and_ids():
    _, client = run(happy_queue())
    m2 = client.calls[1]["messages"]
    assert [m["role"] for m in m2] == ["user", "assistant", "user"]
    (r1,) = m2[2]["content"]
    assert r1["type"] == "tool_result" and r1["tool_use_id"] == "t1"
    assert r1["is_error"] is False
    assert json.loads(r1["content"])["session_low"] == "99.7"
    m3 = client.calls[2]["messages"]
    (r2,) = m3[4]["content"]
    assert r2["tool_use_id"] == "t2" and r2["is_error"] is False
    plan = json.loads(r2["content"])
    assert plan["ok"] is True and plan["stop"] == "99.61"


def test_parallel_tool_calls_come_back_in_one_user_message():
    q = [
        response(
            [
                text("looking"),
                tool_use("a", "get_bars", {"symbol": "AAPL"}),
                tool_use("b", "scan", {"symbols": ["GAPA"]}),
            ],
            "tool_use",
        ),
        good(),
    ]
    _, client = run(q)
    msgs = client.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    results = msgs[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert all(r["type"] == "tool_result" for r in results)


def test_symbol_normalised():
    report, _ = run([good()], symbol=" aapl ")
    assert report.symbol == "AAPL"


def test_model_symbol_with_case_is_normalised():
    report, _ = run([good(symbol="aapl")])
    assert report.symbol == "AAPL"


# ---- issue #10 end to end ----

def test_model_supplied_opening_range_is_refused_in_next_request():
    q = [
        response([tool_use("t1", "get_bars", {"symbol": "AAPL"})], "tool_use"),
        response(
            [tool_use("t2", "plan_exit", {
                "setup": "orb", "entry": "102.50",
                "opening_range": {"low": "1.00", "high": "2.00", "complete": True},
            })],
            "tool_use",
        ),
        good(),
    ]
    report, client = run(q)
    assert report.stance == "bullish"
    (res,) = client.calls[2]["messages"][-1]["content"]
    assert res["tool_use_id"] == "t2"
    assert res["is_error"] is True
    assert json.loads(res["content"]) == {"error": REFUSAL}


# ---- retry and neutral ----

def test_retry_once_then_valid():
    report, client = run([bad(), good()])
    assert report.stance == "bullish"
    assert len(client.calls) == 2
    last = client.calls[1]["messages"][-1]
    assert last["role"] == "user"
    assert any(
        b["type"] == "text" and "Your previous attempt failed" in b["text"]
        for b in last["content"]
    )
    # the failed assistant turn is preserved before the retry message
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user", "assistant", "user"]


def test_invalid_twice_is_neutral_no_third_call():
    report, client = run([bad(), bad(), good()])
    assert_neutral(report)
    assert len(client.calls) == 2
    assert len(client.queue) == 1


@pytest.mark.parametrize("payload", [
    report_json(symbol="MSFT"),
    report_json(agent="sentiment"),
    report_json(confidence=1.5),
    report_json(evidence=[]),
    report_json(stance="long"),
    "",
])
def test_invalid_reports_are_neutral(payload):
    report, client = run([response([text(payload)], "end_turn")] * 3)
    assert_neutral(report)
    assert len(client.calls) == 2


def test_wrong_symbol_first_then_right_is_retried():
    report, client = run([good(symbol="MSFT"), good()])
    assert report.symbol == "AAPL" and report.stance == "bullish"
    assert len(client.calls) == 2


def test_boundary_confidences_valid():
    for c in (0, 1):
        report, _ = run([good(confidence=c)])
        assert report.confidence == c


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "stop_sequence"])
def test_bad_stop_reason_is_neutral(stop):
    r = response([text("")], stop)
    report, client = run([r, r, good()])
    assert_neutral(report)
    assert len(client.calls) == 2
    assert "model stopped with " + stop in report.evidence[0].point


def test_refusal_with_tool_use_blocks_gets_tool_results_not_executed():
    data = FakeMarketData()
    r = response([tool_use("x", "get_bars", {"symbol": "AAPL"})], "refusal")
    report, client = run([r, good()], data=data)
    assert report.stance == "bullish"  # retry succeeded
    assert data.calls == []  # tool never executed on a refusal
    last = client.calls[1]["messages"][-1]["content"]
    assert last[0]["type"] == "tool_result" and last[0]["tool_use_id"] == "x"
    assert last[0]["is_error"] is True


def test_exceeding_max_tool_rounds_is_neutral():
    cfg = AgentConfig(max_tool_rounds=1)
    loop = response([tool_use("t", "get_bars", {"symbol": "AAPL"})], "tool_use")
    report, client = run([loop] * 6, config=cfg)
    assert_neutral(report)
    assert "too many tool rounds" in report.evidence[0].point
    # first chain: rounds 0 and 1; retry chain: rounds 0 and 1; nothing more
    assert len(client.calls) == 4
    assert len(client.queue) == 2


def test_tool_round_limit_leaves_no_orphan_tool_use():
    cfg = AgentConfig(max_tool_rounds=1)
    loop = response([tool_use("t", "get_bars", {"symbol": "AAPL"})], "tool_use")
    _, client = run([loop] * 6, config=cfg)
    retry_req = client.calls[2]["messages"]
    assert retry_req[-1]["role"] == "user"
    kinds = [b["type"] for b in retry_req[-1]["content"]]
    assert kinds == ["tool_result", "text"]
    assert retry_req[-1]["content"][0]["is_error"] is True


def test_max_tool_rounds_exactly_enough_succeeds():
    cfg = AgentConfig(max_tool_rounds=1)
    q = [response([tool_use("t", "get_bars", {"symbol": "AAPL"})], "tool_use"), good()]
    report, client = run(q, config=cfg)
    assert report.stance == "bullish" and len(client.calls) == 2


def test_api_error_is_neutral_and_not_retried():
    report, client = run([api_error("connection dropped"), good()])
    assert_neutral(report)
    assert len(client.calls) == 1
    assert "APIConnectionError" in report.evidence[0].point


def test_neutral_reason_never_contains_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    report, _ = run([api_error(f"bad key {SENTINEL}")])
    assert_neutral(report)
    assert SENTINEL not in report.model_dump_json()
    report2, _ = run([response([text(SENTINEL)], "end_turn")] * 2)
    assert_neutral(report2)
    assert SENTINEL not in report2.model_dump_json()


def test_tool_failure_does_not_abort_run():
    data = FakeMarketData(raises=RuntimeError("down"))
    q = [response([tool_use("t", "get_bars", {"symbol": "AAPL"})], "tool_use"), good()]
    report, client = run(q, data=data)
    assert report.stance == "bullish"
    (res,) = client.calls[1]["messages"][-1]["content"]
    assert res["is_error"] is True


def test_naive_now_raises():
    with pytest.raises(ValueError):
        tech.run_technical_analyst(
            "AAPL", datetime(2026, 11, 3, 10, 30), FakeMarketData(), client=FakeClient([good()])
        )


def test_pause_turn_is_continued():
    q = [response([text("thinking...")], "pause_turn"), good()]
    report, client = run(q)
    assert report.stance == "bullish"
    assert len(client.calls) == 2
    msgs = client.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


# ---- config ----

def test_defaults():
    c = AgentConfig()
    assert (c.model, c.effort, c.fallbacks) == ("claude-opus-5-5", "high", True)


def test_config_from_env_changes_request(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_MODEL", " claude-test-model ")
    monkeypatch.setenv("TRADING_AGENTS_EFFORT", "LOW")
    cfg = config_from_env()
    assert (cfg.model, cfg.effort) == ("claude-test-model", "low")
    _, client = run([good()], config=cfg)
    kw = client.calls[0]
    assert kw["model"] == "claude-test-model"
    assert kw["output_config"]["effort"] == "low"


def test_config_from_env_defaults(monkeypatch):
    monkeypatch.delenv("TRADING_AGENTS_MODEL", raising=False)
    monkeypatch.delenv("TRADING_AGENTS_EFFORT", raising=False)
    assert config_from_env() == AgentConfig()


@pytest.mark.parametrize("effort", ["extreme", "", "  ", "none"])
def test_bad_effort_raises(monkeypatch, effort):
    monkeypatch.setenv("TRADING_AGENTS_EFFORT", effort)
    with pytest.raises(ValueError):
        config_from_env()


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_every_valid_effort_accepted(monkeypatch, effort):
    monkeypatch.setenv("TRADING_AGENTS_EFFORT", effort)
    assert config_from_env().effort == effort


def test_blank_model_raises(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_MODEL", "   ")
    with pytest.raises(ValueError):
        config_from_env()


def test_fallbacks_false_omits_betas_and_fallbacks():
    _, client = run([good()], config=AgentConfig(fallbacks=False))
    kw = client.calls[0]
    assert "betas" not in kw and "fallbacks" not in kw


# ---- make_client ----

def test_make_client_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as ei:
        make_client()
    assert str(ei.value) == "ANTHROPIC_API_KEY must be set in the environment"


@pytest.mark.parametrize("blank", ["", "   "])
def test_make_client_blank_key(monkeypatch, blank):
    monkeypatch.setenv("ANTHROPIC_API_KEY", blank)
    with pytest.raises(RuntimeError) as ei:
        make_client()
    assert str(ei.value) == "ANTHROPIC_API_KEY must be set in the environment"


def test_make_client_with_dummy_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy-not-real")
    client = make_client()  # conftest guard would raise on any socket use
    assert isinstance(client, anthropic.Anthropic)


# ---- pipeline.technical ----

def test_run_technical_returns_valid_report_dict():
    client = FakeClient(happy_queue())
    out = run_technical(
        "AAPL", NOW, data=FakeMarketData(), client=client, config=AgentConfig()
    )
    assert isinstance(out, dict)
    json.dumps(out)
    assert AnalystReport.model_validate(out).symbol == "AAPL"
    assert out == {
        "agent": "technical", "symbol": "AAPL", "stance": "bullish", "confidence": 0.7,
        "evidence": [{"point": "above vwap", "source": "get_bars"}], "catalysts": [],
    }


def test_run_technical_neutral_dict_on_failure():
    out = run_technical(
        "AAPL", NOW, data=FakeMarketData(), client=FakeClient([api_error()]),
        config=AgentConfig(),
    )
    assert out["stance"] == "neutral" and out["confidence"] == 0.0
    AnalystReport.model_validate(out)
