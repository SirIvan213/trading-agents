"""Sentiment and Fundamentals analysts end to end with FakeClient. No API, no network."""
import json
from pathlib import Path

import pytest

import agents.fundamentals as fund_agent
import agents.sentiment as sent_agent
from agents.fundamentals_tools import TOOLS as F_TOOLS
from agents.llm import AgentConfig
from agents.runner import NEUTRAL_SOURCE
from agents.sentiment_tools import TOOLS as S_TOOLS
from tests.fakes import NOON, FakeClient, FakeFilingsData, report_json, response, text, tool_use

PROMPTS = Path(sent_agent.__file__).parent / "prompts"
BETAS = ["server-side-fallback-2026-07-01"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for agent in ("", "TECHNICAL_", "SENTIMENT_", "FUNDAMENTALS_"):
        for kind in ("MODEL", "EFFORT"):
            monkeypatch.delenv(f"TRADING_AGENTS_{agent}{kind}", raising=False)


def rep(agent, **over):
    return response([text(report_json(agent=agent, symbol="ABCD", **over))], "end_turn")


def sent_queue(**over):
    over.setdefault("evidence", [{"point": "trading halted", "source": "get_news"}])
    return [
        response([tool_use("n1", "get_news", {"symbol": "ABCD"})], "tool_use"),
        rep("sentiment", **over),
    ]


def fund_queue(**over):
    over.setdefault("evidence", [{"point": "revenue up", "source": "get_fundamentals"}])
    return [
        response([tool_use("f1", "get_fundamentals", {"symbol": "ABCD"})], "tool_use"),
        rep("fundamentals", **over),
    ]


def tool_result(client, call_index=1):
    (res,) = client.calls[call_index]["messages"][-1]["content"]
    return res, json.loads(res["content"])


# ---------------- sentiment ----------------

def test_sentiment_happy_path():
    client = FakeClient(sent_queue(
        stance="bearish", confidence=0.6, catalysts=[{"name": "halt", "source": "get_news"}]))
    run = sent_agent.run_sentiment("ABCD", NOON, FakeFilingsData(), client=client)
    assert run.ok is True and run.reason is None
    assert run.report.agent == "sentiment" and run.report.stance == "bearish"
    assert [(c.name, c.source) for c in run.report.catalysts] == [("halt", "get_news")]
    assert len(client.calls) == 2 and client.queue == []
    assert len(run.calls) == 2


def test_sentiment_request_kwargs():
    client = FakeClient(sent_queue())
    sent_agent.run_sentiment("ABCD", NOON, FakeFilingsData(), client=client)
    for kw in client.calls:
        assert kw["model"] == "claude-sonnet-5-5"
        assert kw["max_tokens"] == 16000
        assert kw["betas"] == BETAS and kw["fallbacks"] == "default"
        assert kw["system"] == [{
            "type": "text", "text": (PROMPTS / "sentiment.md").read_text(encoding="utf-8"),
            "cache_control": {"type": "ephemeral"},
        }]
        assert kw["tools"] == S_TOOLS
        assert [t["name"] for t in kw["tools"]] == ["get_news"]
        assert kw["output_config"]["effort"] == "high"
        assert kw["output_config"]["format"] == {
            "type": "json_schema", "schema": sent_agent.OUTPUT_SCHEMA}
        assert set(kw) == {"model", "max_tokens", "system", "tools", "messages",
                           "output_config", "betas", "fallbacks"}
    props = sent_agent.OUTPUT_SCHEMA["properties"]
    assert props["agent"]["enum"] == ["sentiment"]
    assert "Analyse ABCD" in client.calls[0]["messages"][0]["content"]


def test_sentiment_red_flag_headline_reaches_the_model_in_tool_result():
    client = FakeClient(sent_queue())
    sent_agent.run_sentiment("ABCD", NOON, FakeFilingsData(), client=client)
    res, payload = tool_result(client)
    assert res["tool_use_id"] == "n1" and res["is_error"] is False
    title = "Trading halted in ABCD pending news"
    halted = [h for h in payload["headlines"] if h["headline"] == title]
    assert len(halted) == 1 and halted[0]["flags"] == ["halt"]
    assert "halt" in payload["red_flags"] and "bankruptcy" in payload["red_flags"]
    assert payload["counts"]["headlines"] == 7


def test_sentiment_prompt_matches_file_and_names_the_tool():
    assert sent_agent.PROMPT == (PROMPTS / "sentiment.md").read_text(encoding="utf-8")
    assert "get_news" in sent_agent.PROMPT and "sentiment" in sent_agent.PROMPT


def test_sentiment_tool_failure_with_catalyst_is_rejected_then_neutral():
    d = FakeFilingsData(news_raises=RuntimeError("down"))
    cat = [{"name": "halt", "source": "get_news"}]
    q = [
        response([tool_use("n1", "get_news", {"symbol": "ABCD"})], "tool_use"),
        rep("sentiment", evidence=[{"point": "p", "source": "get_news"}], catalysts=cat),
        rep("sentiment", evidence=[{"point": "p", "source": "get_news"}], catalysts=cat),
    ]
    client = FakeClient(q)
    run = sent_agent.run_sentiment("ABCD", NOON, d, client=client)
    res, payload = tool_result(client)
    assert res["is_error"] is True and payload == {"error": "get_news failed (RuntimeError)"}
    assert run.ok is False
    assert run.report.evidence[0].source == NEUTRAL_SOURCE


def test_sentiment_report_citing_a_failed_tool_is_rejected_even_without_catalysts():
    d = FakeFilingsData(news_raises=RuntimeError("down"))
    q = sent_queue(stance="neutral", confidence=0.1) + [
        rep("sentiment", evidence=[{"point": "p", "source": "get_news"}])]
    run = sent_agent.run_sentiment("ABCD", NOON, d, client=FakeClient(q))
    assert run.ok is False
    assert run.reason == "evidence source get_news is not a tool that returned data in this run"


def test_sentiment_survives_an_sec_failure():
    d = FakeFilingsData(subs_raises=RuntimeError("sec down"))
    client = FakeClient(sent_queue(stance="neutral", confidence=0.1))
    run = sent_agent.run_sentiment("ABCD", NOON, d, client=client)
    assert run.ok is True
    _, payload = tool_result(client)
    assert payload["note"] == "filings unavailable (RuntimeError)" and payload["filings"] == []


def test_sentiment_config_from_env(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_SENTIMENT_MODEL", "claude-test-sent")
    monkeypatch.setenv("TRADING_AGENTS_SENTIMENT_EFFORT", "low")
    client = FakeClient(sent_queue())
    sent_agent.run_sentiment("ABCD", NOON, FakeFilingsData(), client=client)
    assert client.calls[0]["model"] == "claude-test-sent"
    assert client.calls[0]["output_config"]["effort"] == "low"


def test_sentiment_explicit_config_wins(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_SENTIMENT_MODEL", "from-env")
    client = FakeClient(sent_queue())
    cfg = AgentConfig(model="explicit", fallbacks=False)
    sent_agent.run_sentiment("ABCD", NOON, FakeFilingsData(), client=client, config=cfg)
    kw = client.calls[0]
    assert kw["model"] == "explicit" and "betas" not in kw and "fallbacks" not in kw


def test_sentiment_wrong_agent_in_output_is_rejected():
    bad = rep("technical", evidence=[{"point": "p", "source": "get_news"}])
    run = sent_agent.run_sentiment(
        "ABCD", NOON, FakeFilingsData(), client=FakeClient([bad, bad]))
    assert run.ok is False and run.reason == "agent must be 'sentiment'"


def test_sentiment_bad_inputs_raise():
    from datetime import datetime
    with pytest.raises(ValueError):
        sent_agent.run_sentiment("ABCD", datetime(2026, 11, 3, 12), FakeFilingsData(),
                                 client=FakeClient())
    for bad in ("", "AB CD", None):
        with pytest.raises(ValueError):
            sent_agent.run_sentiment(bad, NOON, FakeFilingsData(), client=FakeClient())


# ---------------- fundamentals ----------------

def test_fundamentals_happy_path():
    client = FakeClient(fund_queue(stance="bullish", confidence=0.4))
    run = fund_agent.run_fundamentals("ABCD", NOON, FakeFilingsData(), client=client)
    assert run.ok is True
    assert run.report.agent == "fundamentals" and run.report.symbol == "ABCD"
    assert len(run.calls) == len(client.calls) == 2


def test_fundamentals_request_kwargs():
    client = FakeClient(fund_queue())
    fund_agent.run_fundamentals("ABCD", NOON, FakeFilingsData(), client=client)
    for kw in client.calls:
        assert kw["model"] == "claude-sonnet-5-5"
        assert kw["system"] == [{
            "type": "text", "text": (PROMPTS / "fundamentals.md").read_text(encoding="utf-8"),
            "cache_control": {"type": "ephemeral"},
        }]
        assert kw["tools"] == F_TOOLS
        assert [t["name"] for t in kw["tools"]] == ["get_fundamentals"]
        assert kw["output_config"]["format"] == {
            "type": "json_schema", "schema": fund_agent.OUTPUT_SCHEMA}
        assert kw["betas"] == BETAS and kw["fallbacks"] == "default"
    assert fund_agent.OUTPUT_SCHEMA["properties"]["agent"]["enum"] == ["fundamentals"]
    assert fund_agent.PROMPT == (PROMPTS / "fundamentals.md").read_text(encoding="utf-8")


def test_fundamentals_tool_result_contains_point_in_time_facts():
    client = FakeClient(fund_queue())
    fund_agent.run_fundamentals("ABCD", NOON, FakeFilingsData(), client=client)
    res, payload = tool_result(client)
    assert res["is_error"] is False and payload["ok"] is True
    assert payload["as_of"] == "2026-11-03"
    assert payload["growth"]["revenue_yoy_pct"] == "15.00"
    assert "2026-11-10" not in res["content"]  # nothing from the future


def test_fundamentals_no_cik_is_error_and_catalyst_citation_rejected():
    cat = [{"name": "recent_filing", "source": "get_fundamentals"}]
    ev = [{"point": "p", "source": "get_fundamentals"}]
    q = [
        response([tool_use("f1", "get_fundamentals", {"symbol": "ABCD"})], "tool_use"),
        rep("fundamentals", evidence=ev, catalysts=cat),
        rep("fundamentals", evidence=ev, catalysts=cat),
    ]
    client = FakeClient(q)
    run = fund_agent.run_fundamentals(
        "ABCD", NOON, FakeFilingsData(no_cik=True), client=client)
    res, payload = tool_result(client)
    assert res["is_error"] is True and payload == {"error": "no SEC filer found for symbol"}
    assert run.ok is False and "get_fundamentals" in run.reason


def test_fundamentals_ok_false_result_cannot_back_a_catalyst():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    early = datetime(2025, 1, 1, 12, tzinfo=ZoneInfo("America/New_York"))  # nothing filed yet
    cat = [{"name": "recent_filing", "source": "get_fundamentals"}]
    ev = [{"point": "p", "source": "get_fundamentals"}]
    q = [
        response([tool_use("f1", "get_fundamentals", {"symbol": "ABCD"})], "tool_use"),
        rep("fundamentals", evidence=ev, catalysts=cat),
        rep("fundamentals", evidence=ev, catalysts=[]),
    ]
    client = FakeClient(q)
    run = fund_agent.run_fundamentals("ABCD", early, FakeFilingsData(), client=client)
    res, payload = tool_result(client)
    assert res["is_error"] is False and payload["ok"] is False
    # an ok:false result is not data: neither the catalyst nor the evidence can cite the tool
    assert run.ok is False
    assert run.reason == (
        "evidence source get_fundamentals is not a tool that returned data in this run")
    assert len(client.calls) == 3


def test_fundamentals_config_from_env(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_FUNDAMENTALS_MODEL", "claude-test-fund")
    client = FakeClient(fund_queue())
    fund_agent.run_fundamentals("ABCD", NOON, FakeFilingsData(), client=client)
    assert client.calls[0]["model"] == "claude-test-fund"
    assert client.calls[0]["output_config"]["effort"] == "high"
