"""New Phase 3 review tests: cancel, with_options, limits order, strict catalyst names (e2e)."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

import agents.fundamentals as fund_agent
import agents.orchestrator as orch
import agents.sentiment as sent_agent
import agents.technical as tech_agent
from agents.llm import AgentConfig
from agents.runner import AnalystSpec, run_analyst
from tests.fakes import (
    NOON,
    FakeAllData,
    FakeClient,
    FakeFilingsData,
    FakeMarketData,
    report_json,
    response,
    text,
    tool_use,
)
from tests.test_orchestrator import (  # noqa: F401  (fixtures + helpers)
    AGENTS,
    RouterClient,
    assert_neutral_failure,
    assert_ok,
    clean_env,
    loopback_only,
)

SPEC = AnalystSpec(name="sentiment", prompt="PROMPT", tools=[{"name": "get_news"}],
                   output_schema={"type": "object"})
CFG = AgentConfig()


class Session:
    def __init__(self, after_execute=None):
        self.calls = []
        self.after = after_execute

    def execute(self, name, tool_input):
        self.calls.append(name)
        if self.after:
            self.after()
        return {"headlines": [], "catalysts": ["earnings"]}, False


def news_call(id_="t1"):
    return response([tool_use(id_, "get_news", {"symbol": "ABCD"})], "tool_use")


def report(**over):
    d = {"agent": "sentiment", "symbol": "ABCD", "stance": "neutral", "confidence": 0.3,
         "evidence": [{"point": "p", "source": "get_news"}], "catalysts": []}
    d.update(over)
    return response([text(report_json(**d))], "end_turn")


# ---------------- C. cancel ----------------

def test_cancel_set_before_run_makes_no_api_call_and_no_tool_call():
    cancel = threading.Event()
    cancel.set()
    client, sess = FakeClient([report()]), Session()
    run = run_analyst(SPEC, "ABCD", NOON, sess, client=client, config=CFG, cancel=cancel)
    assert run.ok is False and run.reason == "cancelled"
    assert client.calls == [] and sess.calls == [] and run.calls == []
    assert run.report.evidence[0].source == "analyst_unavailable"
    assert run.report.evidence[0].point == "sentiment analysis unavailable: cancelled"


def test_cancel_set_during_create_stops_before_the_tool_call_and_keeps_records():
    cancel = threading.Event()
    queue = [news_call(), report()]

    class CancellingClient(FakeClient):
        def __init__(self):
            super().__init__(queue)
            inner = self.beta.messages.create

            def create(**kw):
                out = inner(**kw)
                cancel.set()  # the in-flight request finishes, then cancellation is seen
                return out

            self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    client, sess = CancellingClient(), Session()
    run = run_analyst(SPEC, "ABCD", NOON, sess, client=client, config=CFG, cancel=cancel)
    assert run.ok is False and run.reason == "cancelled"
    assert len(client.calls) == 1 and len(run.calls) == 1   # record of the finished request kept
    assert run.calls[0].stop_reason == "tool_use"
    assert sess.calls == []                                  # the tool never ran


def test_cancel_set_during_a_tool_call_stops_before_the_next_request():
    cancel = threading.Event()
    sess = Session(after_execute=cancel.set)
    client = FakeClient([news_call(), report()])
    run = run_analyst(SPEC, "ABCD", NOON, sess, client=client, config=CFG, cancel=cancel)
    assert run.reason == "cancelled" and run.ok is False
    assert sess.calls == ["get_news"]
    assert len(client.calls) == 1 and len(run.calls) == 1   # no second request was made


def test_cancel_during_retry_loop_also_stops():
    cancel = threading.Event()
    bad = response([text("not json")], "end_turn")

    class C(FakeClient):
        def __init__(self):
            super().__init__([bad, report()])
            inner = self.beta.messages.create
            self.beta = SimpleNamespace(messages=SimpleNamespace(
                create=lambda **kw: (lambda r: (cancel.set(), r)[1])(inner(**kw))))

    client = C()
    run = run_analyst(SPEC, "ABCD", NOON, Session(), client=client, config=CFG, cancel=cancel)
    assert run.reason == "cancelled" and len(client.calls) == 1


def test_cancel_none_is_the_normal_path():
    run = run_analyst(SPEC, "ABCD", NOON, Session(), client=FakeClient([news_call(), report()]),
                      config=CFG, cancel=None)
    assert run.ok is True


def test_unset_event_does_not_interfere():
    run = run_analyst(SPEC, "ABCD", NOON, Session(), client=FakeClient([news_call(), report()]),
                      config=CFG, cancel=threading.Event())
    assert run.ok is True


@pytest.mark.parametrize("runner", ["sentiment", "fundamentals", "technical"])
def test_each_run_function_accepts_cancel(runner):
    cancel = threading.Event()
    cancel.set()
    client = FakeClient()
    fn = {"sentiment": sent_agent.run_sentiment, "fundamentals": fund_agent.run_fundamentals,
          "technical": tech_agent.run_technical}[runner]
    data = FakeAllData()
    run = fn("ABCD", NOON, data, client=client, cancel=cancel)
    assert run.reason == "cancelled" and run.ok is False and client.calls == []
    assert data.calls == []


def test_orchestrator_timeout_sets_the_cancel_event_and_worker_observes_it(monkeypatch):
    started, finished = threading.Event(), threading.Event()
    seen = {}

    def hung(symbol, now, data, *, client, config, calls_sink, cancel=None):
        started.set()
        seen["observed"] = cancel.wait(timeout=10)   # returns True as soon as the event is set
        finished.set()
        raise AssertionError("result must be discarded")

    monkeypatch.setitem(orch._RUNNERS, "fundamentals", hung)
    packet = asyncio.run(orch.run_analysts(
        "ABCD", NOON, data=FakeAllData(), clients=RouterClient(), timeout_s=0.3))
    assert_neutral_failure(packet, "fundamentals", "timeout")
    assert_ok(packet, "technical", "sentiment")
    assert started.is_set()
    assert finished.wait(timeout=5)          # handshake: the worker woke up because of the cancel
    assert seen["observed"] is True


def test_orchestrator_sets_cancel_on_failure_and_after_completion(monkeypatch):
    events = {}

    def boom(symbol, now, data, *, client, config, calls_sink, cancel=None):
        events["sentiment"] = cancel
        assert not cancel.is_set()           # not set while the run is starting
        raise RuntimeError("x")

    monkeypatch.setitem(orch._RUNNERS, "sentiment", boom)
    packet = asyncio.run(orch.run_analysts(
        "ABCD", NOON, data=FakeAllData(), clients=RouterClient(), timeout_s=5))
    assert_neutral_failure(packet, "sentiment", "failed (RuntimeError)")
    assert events["sentiment"].is_set()


def test_each_agent_gets_its_own_event(monkeypatch):
    seen = {}

    def spy(name):
        def fn(symbol, now, data, *, client, config, calls_sink, cancel=None):
            seen[name] = cancel
            from agents.runner import AnalystRun, neutral_report
            return AnalystRun(neutral_report(name, symbol, "spy"), False, "spy", [], 0)
        return fn

    for a in AGENTS:
        monkeypatch.setitem(orch._RUNNERS, a, spy(a))
    asyncio.run(orch.run_analysts(
        "ABCD", NOON, data=FakeAllData(), clients=RouterClient(), timeout_s=5))
    assert len({id(e) for e in seen.values()}) == 3
    assert all(isinstance(e, threading.Event) for e in seen.values())


def test_cancelling_run_analysts_sets_the_events(monkeypatch):
    started, finished = threading.Event(), threading.Event()
    seen = {}

    def hung(symbol, now, data, *, client, config, calls_sink, cancel=None):
        started.set()
        seen["observed"] = cancel.wait(timeout=10)
        finished.set()
        raise AssertionError("discarded")

    monkeypatch.setitem(orch._RUNNERS, "technical", hung)

    async def main():
        task = asyncio.ensure_future(orch.run_analysts(
            "ABCD", NOON, data=FakeAllData(), clients=RouterClient(), timeout_s=30))
        for _ in range(200):                      # wait (bounded) until the worker is running
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert finished.wait(timeout=5) and seen["observed"] is True


# ---------------- D. with_options ----------------

class OptionsClient(FakeClient):
    def __init__(self, queue):
        super().__init__(queue)
        self.timeouts = []

    def with_options(self, **kw):
        self.timeouts.append(kw)
        return self


def test_default_request_timeout_is_120():
    assert AgentConfig().request_timeout_s == 120.0


def test_with_options_receives_the_configured_timeout_on_every_request():
    client = OptionsClient([news_call(), report()])
    run = run_analyst(SPEC, "ABCD", NOON, Session(), client=client, config=CFG)
    assert run.ok is True
    assert client.timeouts == [{"timeout": 120.0}, {"timeout": 120.0}]
    assert len(client.calls) == 2


def test_with_options_uses_a_custom_timeout():
    client = OptionsClient([news_call(), report()])
    cfg = AgentConfig(request_timeout_s=7.5)
    run_analyst(SPEC, "ABCD", NOON, Session(), client=client, config=cfg)
    assert client.timeouts[0] == {"timeout": 7.5}


def test_request_kwargs_do_not_contain_the_timeout():
    client = OptionsClient([news_call(), report()])
    run_analyst(SPEC, "ABCD", NOON, Session(), client=client, config=CFG)
    assert all("timeout" not in kw for kw in client.calls)


def test_client_without_with_options_still_works():
    client = FakeClient([news_call(), report()])
    assert not hasattr(client, "with_options")
    assert run_analyst(SPEC, "ABCD", NOON, Session(), client=client, config=CFG).ok is True


def test_with_options_failure_becomes_neutral_not_a_raise():
    class Bad(FakeClient):
        def with_options(self, **kw):
            raise RuntimeError("nope")

    run = run_analyst(SPEC, "ABCD", NOON, Session(), client=Bad([report()]), config=CFG)
    assert run.ok is False and run.reason == "RuntimeError"


# ---------------- E. limits order ----------------

SECRET = "LIMITS-FILE-CONTENTS-SENTINEL"


def boom_limits(*a, **kw):
    raise ValueError(f"bad json {SECRET}")


def test_invalid_symbol_raises_before_limits_are_loaded(monkeypatch):
    monkeypatch.setattr(tech_agent, "load_limits", boom_limits)
    for bad in ("", "AB CD", None, 5, "TOOLONGSYMBOLX"):
        with pytest.raises(ValueError, match="^symbol must be a valid ticker$"):
            tech_agent.run_technical(bad, NOON, FakeMarketData(), client=FakeClient())


def test_naive_now_raises_before_limits_are_loaded(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(tech_agent, "load_limits", boom_limits)
    with pytest.raises(ValueError) as ei:
        tech_agent.run_technical("AAPL", datetime(2026, 11, 3, 12), FakeMarketData(),
                                 client=FakeClient())
    assert "limits" not in str(ei.value)


def test_failing_load_limits_raises_generic_message_without_contents(monkeypatch):
    monkeypatch.setattr(tech_agent, "load_limits", boom_limits)
    client = FakeClient([report()])
    with pytest.raises(ValueError) as ei:
        tech_agent.run_technical("AAPL", NOON, FakeMarketData(), client=client)
    assert str(ei.value) == "limits config is invalid"
    assert SECRET not in str(ei.value) and SECRET not in repr(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__ is True
    assert client.calls == []                      # nothing was sent to the model


@pytest.mark.parametrize("exc", [FileNotFoundError("x"), OSError("y"), KeyError("z")])
def test_any_load_failure_is_reported_the_same_way(monkeypatch, exc):
    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(tech_agent, "load_limits", boom)
    with pytest.raises(ValueError, match="^limits config is invalid$"):
        tech_agent.run_technical("AAPL", NOON, FakeMarketData(), client=FakeClient())


def test_explicit_limits_skip_load_limits(monkeypatch):
    from tools.config import load_limits
    limits = load_limits()
    monkeypatch.setattr(tech_agent, "load_limits", boom_limits)
    client = FakeClient([response([tool_use("b", "get_bars", {"symbol": "AAPL"})], "tool_use"),
                         response([text(report_json())], "end_turn")])
    run = tech_agent.run_technical("AAPL", NOON, FakeMarketData(), client=client, limits=limits)
    assert run.ok is True


def test_orchestrator_technical_fails_others_ok_when_limits_are_broken(monkeypatch):
    monkeypatch.setattr(tech_agent, "load_limits", boom_limits)
    packet = asyncio.run(orch.run_analysts(
        "ABCD", NOON, data=FakeAllData(), clients=RouterClient(), timeout_s=5))
    assert packet.all_ok is False
    assert_neutral_failure(packet, "technical", "failed (ValueError)")
    assert_ok(packet, "sentiment", "fundamentals")
    assert SECRET not in packet.model_dump_json()
    assert packet.runs["technical"].calls == []


# ---------------- B. strict catalyst names end to end ----------------

def cat(name, source):
    return [{"name": name, "source": source}]


def sent_run(catalysts, extra=()):
    q = [news_call(), report(catalysts=catalysts), *extra]
    client = FakeClient(q)
    return sent_agent.run_sentiment("ABCD", NOON, FakeFilingsData(), client=client), client


def test_earnings_from_get_news_passes_when_digest_lists_it():
    run, client = sent_run(cat("earnings", "get_news"))
    assert run.ok is True and run.report.catalysts[0].name == "earnings"


def test_earnings_from_get_news_fails_when_digest_does_not_list_it():
    # no news and no filings => the digest has empty catalyst lists
    class Empty(FakeFilingsData):
        def news(self, symbols, start, end):
            return {"news": []}

        def submissions(self, symbol):
            return None

    q = [news_call(), report(catalysts=cat("earnings", "get_news")),
         report(catalysts=cat("earnings", "get_news"))]
    run = sent_agent.run_sentiment("ABCD", NOON, Empty(), client=FakeClient(q))
    assert run.ok is False and run.reason == "catalyst earnings is not in get_news's output"


def test_made_up_catalyst_retries_then_neutral():
    run, client = sent_run(cat("moon_landing", "get_news"),
                           extra=[report(catalysts=cat("moon_landing", "get_news"))])
    assert run.ok is False and len(client.calls) == 3
    assert run.reason == "catalyst moon_landing is not in get_news's output"


def test_made_up_catalyst_then_valid_retry_is_ok():
    run, _ = sent_run(cat("moon_landing", "get_news"), extra=[report(catalysts=[])])
    assert run.ok is True and run.report.catalysts == []


def test_red_flag_name_is_allowed():
    run, _ = sent_run(cat("bankruptcy", "get_news"))   # from the 8-K item 1.03 red flag
    assert run.ok is True
    run, _ = sent_run(cat("halt", "get_news"))         # from the halted headline
    assert run.ok is True


def fund_run(catalysts, facts_fixture, extra=()):
    d = FakeFilingsData(facts_fixture=facts_fixture)
    q = [response([tool_use("f1", "get_fundamentals", {"symbol": "ABCD"})], "tool_use"),
         report(agent="fundamentals", evidence=[{"point": "p", "source": "get_fundamentals"}],
                catalysts=catalysts), *extra]
    client = FakeClient(q)
    return fund_agent.run_fundamentals("ABCD", NOON, d, client=client), client


def fcat(name):
    return cat(name, "get_fundamentals")


def test_recent_10q_passes_when_earnings_facts_lists_it():
    run, client = fund_run(fcat("recent_10q"), "sec_companyfacts_abcd_realistic.json")
    assert run.ok is True and run.report.catalysts[0].name == "recent_10q"
    (res,) = client.calls[1]["messages"][-1]["content"]
    import json
    assert json.loads(res["content"])["catalysts"] == ["recent_10q", "restatement"]


def test_restatement_passes_with_the_default_fixture():
    run, _ = fund_run(fcat("restatement"), "sec_companyfacts_abcd.json")
    assert run.ok is True


def test_recent_10q_fails_when_earnings_facts_does_not_list_it():
    # default fixture at NOON: newest 10-Q was filed 2026-08-05, so only "restatement" is listed
    bad = report(agent="fundamentals", evidence=[{"point": "p", "source": "get_fundamentals"}],
                 catalysts=fcat("recent_10q"))
    run, client = fund_run(fcat("recent_10q"), "sec_companyfacts_abcd.json", extra=[bad])
    assert run.ok is False
    assert run.reason == "catalyst recent_10q is not in get_fundamentals's output"
    assert len(client.calls) == 3


def test_fundamentals_with_no_catalysts_is_fine():
    run, _ = fund_run([], "sec_companyfacts_abcd.json")
    assert run.ok is True


def test_fundamentals_may_not_use_a_sentiment_catalyst_name():
    bad = report(agent="fundamentals", evidence=[{"point": "p", "source": "get_fundamentals"}],
                 catalysts=fcat("halt"))
    run, _ = fund_run(fcat("halt"), "sec_companyfacts_abcd.json", extra=[bad])
    assert run.ok is False and run.reason == "catalyst halt is not in get_fundamentals's output"


# ---------------- evidence may cite any tool that answered (ok:false included) ----------------

def test_technical_evidence_may_cite_a_refused_plan_exit():
    # orb entry equal to the structure low is refused by exit_plan with {"ok": False, ...}
    data = FakeMarketData()
    q = [
        response([tool_use("b", "get_bars", {"symbol": "AAPL"})], "tool_use"),
        response([tool_use("p", "plan_exit", {"setup": "orb", "entry": "99.7"})], "tool_use"),
        response([text(report_json(
            stance="neutral", confidence=0.2,
            evidence=[{"point": "orb plan refused: entry must be above structure",
                       "source": "plan_exit"}]))], "end_turn"),
    ]
    client = FakeClient(q)
    run = tech_agent.run_technical("AAPL", NOON, data, client=client)
    (res,) = client.calls[2]["messages"][-1]["content"]
    import json
    payload = json.loads(res["content"])
    assert res["is_error"] is False and payload["ok"] is False
    assert run.ok is True and len(client.calls) == 3          # no retry
    assert run.report.evidence[0].source == "plan_exit"


def test_technical_catalyst_citing_the_refused_plan_exit_is_invalid():
    cat = [{"name": "x", "source": "plan_exit"}]
    bad = response([text(report_json(
        evidence=[{"point": "p", "source": "plan_exit"}], catalysts=cat))], "end_turn")
    q = [response([tool_use("b", "get_bars", {"symbol": "AAPL"})], "tool_use"),
         response([tool_use("p", "plan_exit", {"setup": "orb", "entry": "99.7"})], "tool_use"),
         bad, bad]
    run = tech_agent.run_technical("AAPL", NOON, FakeMarketData(), client=FakeClient(q))
    assert run.ok is False and run.reason.startswith("catalyst source plan_exit")


class NoTenQ(FakeFilingsData):
    def company_facts(self, symbol):
        self.calls.append(("company_facts", symbol))
        return {"cik": 12345, "entityName": "ABCD Holdings Inc", "facts": {"us-gaap": {}}}


def test_fundamentals_with_ok_false_facts_is_ok_with_zero_confidence_and_no_retry():
    ev = [{"point": "no 10-Q data available", "source": "get_fundamentals"}]
    q = [response([tool_use("f1", "get_fundamentals", {"symbol": "ABCD"})], "tool_use"),
         report(agent="fundamentals", stance="neutral", confidence=0.0, evidence=ev)]
    client = FakeClient(q)
    run = fund_agent.run_fundamentals("ABCD", NOON, NoTenQ(), client=client)
    (res,) = client.calls[1]["messages"][-1]["content"]
    import json
    assert res["is_error"] is False
    assert json.loads(res["content"])["ok"] is False
    assert run.ok is True and run.reason is None
    assert run.report.confidence == 0.0 and run.report.evidence[0].source == "get_fundamentals"
    assert len(client.calls) == 2 and client.queue == []      # no retry


def test_all_ok_unaffected_by_ok_false_facts():
    from tests.test_orchestrator import TOOL, first, good

    data = FakeAllData()
    data.company_facts = lambda symbol: {"cik": 12345, "facts": {"us-gaap": {}}}
    clients = {a: FakeClient([first(a), good(a)]) for a in AGENTS}
    packet = asyncio.run(orch.run_analysts(
        "ABCD", NOON, data=data, clients=clients, timeout_s=5))
    assert packet.all_ok is True
    assert packet.runs["fundamentals"].ok is True
    assert TOOL["fundamentals"] == "get_fundamentals"


def test_evidence_from_errored_or_never_called_tool_is_still_invalid():
    errored = Session()
    errored.execute = lambda name, tool_input: ({"error": "boom"}, True)
    bad = report()
    run = run_analyst(SPEC, "ABCD", NOON, errored, client=FakeClient([news_call(), bad, bad]),
                      config=CFG)
    assert run.ok is False
    assert run.reason == "evidence source get_news is not a tool that answered in this run"
    never = report(evidence=[{"point": "p", "source": "get_fundamentals"}])
    run = run_analyst(SPEC, "ABCD", NOON, Session(),
                      client=FakeClient([news_call(), never, never]), config=CFG)
    assert run.reason == "evidence source get_fundamentals is not a tool that answered in this run"
