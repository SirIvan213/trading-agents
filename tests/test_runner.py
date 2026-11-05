"""Generic analyst runner: CallRecord (#46), ok signal (#48), catalyst validation, schema.

Fake clients and real anthropic SDK types only; no API, no network.
"""
import copy
import json
from types import SimpleNamespace

import pytest
from anthropic.types.beta import BetaMessage

from agents import runner as R
from agents.llm import AgentConfig
from agents.runner import NEUTRAL_SOURCE, AnalystSpec, call_record, output_schema, run_analyst
from agents.schemas import AnalystReport
from tests.fakes import NOW, FakeClient, api_error, report_json, response, text, tool_use

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
SPEC = AnalystSpec(name="sentiment", prompt="PROMPT", tools=[{"name": "get_news"}],
                   output_schema={"type": "object"})
CFG = AgentConfig()


class ScriptSession:
    """ToolExecutor returning scripted (result, is_error) per tool name."""

    def __init__(self, results=None):
        self.results = results or {"get_news": (
            {"headlines": [], "catalysts": ["earnings"], "red_flags": ["bankruptcy"]}, False)}
        self.calls = []

    def execute(self, name, tool_input):
        self.calls.append((name, tool_input))
        return copy.deepcopy(self.results.get(name, ({"error": "unknown tool"}, True)))


def rep(**over):
    base = {"agent": "sentiment", "symbol": "ABCD",
            "evidence": [{"point": "p", "source": "get_news"}]}
    base.update(over)
    return report_json(**base)


def end(**over):
    return response([text(rep(**over))], "end_turn")


def call_news(id_="t1"):
    return response([tool_use(id_, "get_news", {"symbol": "ABCD"})], "tool_use")


def go(queue, session=None, config=CFG, symbol="ABCD"):
    client = FakeClient(queue)
    run = run_analyst(SPEC, symbol, NOW, session or ScriptSession(), client=client, config=config)
    return run, client


CAT = [{"name": "earnings", "source": "get_news"}]


# ---------------- CallRecord from SDK types (#46) ----------------

def sdk_message(**over):
    d = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": OPUS,
        "content": [{"type": "text", "text": "{}"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 300, "output_tokens": 70,
                  "cache_creation_input_tokens": 5, "cache_read_input_tokens": 10},
    }
    d.update(over)
    return BetaMessage.model_validate(d)


def fallback_message(response_model=OPUS):
    return sdk_message(
        model=response_model,
        content=[
            {"type": "fallback", "from": {"model": OPUS}, "to": {"model": SONNET},
             "trigger": {"type": "refusal", "category": "cyber"}},
            {"type": "text", "text": "{}"},
        ],
        usage={
            "input_tokens": 300, "output_tokens": 70,
            "cache_creation_input_tokens": 5, "cache_read_input_tokens": 10,
            "iterations": [
                {"type": "message", "model": OPUS, "input_tokens": 100, "output_tokens": 10,
                 "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7},
                {"type": "fallback_message", "model": SONNET, "input_tokens": 200,
                 "output_tokens": 60, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 3},
            ],
        },
    )


@pytest.mark.parametrize("response_model", [OPUS, SONNET, "claude-haiku-5-5"])
def test_fallback_response_records_serving_model_from_iteration(response_model):
    msg = fallback_message(response_model)
    rec = call_record(msg, OPUS)
    assert rec.requested_model == OPUS
    # serving model is the fallback_message iteration's model even if response.model differs
    assert rec.serving_model == SONNET
    assert rec.fallback_ran is True
    assert rec.stop_reason == "end_turn"
    assert rec.usage.input_tokens == 300 and rec.usage.output_tokens == 70
    assert [(i.type, i.model) for i in rec.iterations] == [
        ("message", OPUS), ("fallback_message", SONNET)]
    first, second = (i.usage for i in rec.iterations)
    assert (first.input_tokens, first.output_tokens) == (100, 10)
    assert (first.cache_creation_input_tokens, first.cache_read_input_tokens) == (5, 7)
    assert (second.input_tokens, second.output_tokens) == (200, 60)
    assert (second.cache_creation_input_tokens, second.cache_read_input_tokens) == (0, 3)
    json.dumps(rec.model_dump(mode="json"))


def test_refusal_response_records_category():
    msg = sdk_message(
        content=[], stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": None},
        usage={"input_tokens": 40, "output_tokens": 0},
    )
    rec = call_record(msg, OPUS)
    assert rec.refusal_category == "cyber"
    assert rec.stop_reason == "refusal"
    assert rec.fallback_ran is False
    assert rec.serving_model == OPUS
    assert rec.iterations == []


def test_plain_response_has_no_fallback():
    msg = sdk_message(model=SONNET)
    rec = call_record(msg, OPUS)
    assert rec.fallback_ran is False
    assert rec.serving_model == SONNET  # == response.model
    assert rec.requested_model == OPUS
    assert rec.refusal_category is None
    assert rec.stop_reason == "end_turn"
    assert (rec.usage.cache_creation_input_tokens, rec.usage.cache_read_input_tokens) == (5, 10)


def test_plain_response_with_only_message_iteration():
    msg = sdk_message(usage={
        "input_tokens": 9, "output_tokens": 3,
        "iterations": [{"type": "message", "model": OPUS, "input_tokens": 9, "output_tokens": 3,
                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}],
    })
    rec = call_record(msg, OPUS)
    assert rec.fallback_ran is False and rec.serving_model == OPUS
    assert len(rec.iterations) == 1


def test_bare_namespace_response_is_tolerated():
    rec = call_record(response([text("x")], "end_turn"), SONNET)
    assert (rec.requested_model, rec.serving_model, rec.fallback_ran) == (SONNET, SONNET, False)
    assert rec.usage.input_tokens == 0 and rec.refusal_category is None


def test_non_int_usage_values_become_zero():
    resp = SimpleNamespace(
        content=[], stop_reason="end_turn", model=OPUS,
        usage=SimpleNamespace(input_tokens=True, output_tokens="9", iterations=None),
    )
    rec = call_record(resp, OPUS)
    assert rec.usage.input_tokens == 0 and rec.usage.output_tokens == 0


# ---------------- records on every call ----------------

def test_one_record_per_response_including_tool_rounds():
    run, client = go([call_news(), end()])
    assert run.ok is True
    assert len(client.calls) == 2 and len(run.calls) == 2
    assert [c.stop_reason for c in run.calls] == ["tool_use", "end_turn"]
    assert all(c.requested_model == CFG.model for c in run.calls)


def test_retry_attempt_is_recorded_too():
    bad = response([text("not json")], "end_turn")
    run, client = go([call_news(), bad, end()])
    assert run.ok is True
    assert len(client.calls) == 3 and len(run.calls) == 3


def test_two_failed_attempts_both_recorded():
    bad = response([text("not json")], "end_turn")
    run, client = go([bad, bad])
    assert run.ok is False
    assert len(run.calls) == len(client.calls) == 2


def test_fallback_records_flow_through_a_run():
    good = fallback_message()
    good.content[1].text = rep()
    run, _ = go([call_news(), good])
    assert run.ok is True
    c = run.calls[1]
    assert c.fallback_ran is True and c.serving_model == SONNET


def test_calls_sink_receives_records_and_run_shares_them():
    sink = []
    client = FakeClient([call_news(), end()])
    run = run_analyst(SPEC, "ABCD", NOW, ScriptSession(), client=client, config=CFG,
                      calls_sink=sink)
    assert len(sink) == 2 and run.calls == sink


def test_api_error_keeps_records_made_before_it():
    run, _ = go([call_news(), api_error("down")])
    assert run.ok is False and run.reason == "APIConnectionError"
    assert len(run.calls) == 1


# ---------------- ok flag and neutral source (#48) ----------------

def test_neutral_source_constant():
    assert NEUTRAL_SOURCE == "analyst_unavailable"
    n = R.neutral_report("sentiment", "ABCD", "why")
    assert n.stance == "neutral" and n.confidence == 0.0
    assert [e.source for e in n.evidence] == [NEUTRAL_SOURCE]
    assert n.evidence[0].point == "sentiment analysis unavailable: why"


FAILURES = {
    "invalid_twice": lambda: [response([text("nope")], "end_turn")] * 2,
    "refusal": lambda: [response([text("")], "refusal")] * 2,
    "max_tokens": lambda: [response([text("")], "max_tokens")] * 2,
    "api_error": lambda: [api_error()],
    "wrong_symbol": lambda: [end(symbol="ZZZZ")] * 2,
    "wrong_agent": lambda: [end(agent="technical")] * 2,
}


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_ok_false_exactly_when_neutral(name):
    run, _ = go(FAILURES[name]())
    assert run.ok is False
    assert run.reason
    assert run.report.confidence == 0.0 and run.report.stance == "neutral"
    assert [e.source for e in run.report.evidence] == [NEUTRAL_SOURCE]
    assert run.report.evidence[0].point.endswith(run.reason)


def test_ok_true_for_validated_report_even_with_zero_confidence():
    run, _ = go([call_news(), end(confidence=0.0, stance="neutral")])
    assert run.ok is True and run.reason is None
    assert run.report.confidence == 0.0
    assert run.report.evidence[0].source == "get_news"  # the model's own evidence, not neutral


def test_evidence_citing_the_neutral_source_is_invalid():
    ev = [{"point": "p", "source": NEUTRAL_SOURCE}]
    bad = end(confidence=0.0, stance="neutral", evidence=ev)
    run, _ = go([call_news(), bad, bad])
    assert run.ok is False
    assert run.reason == (
        f"evidence source {NEUTRAL_SOURCE} is not a tool that answered in this run")


def test_too_many_tool_rounds_is_not_ok():
    loop = call_news()
    run, _ = go([loop] * 6, config=AgentConfig(max_tool_rounds=1))
    assert run.ok is False and run.reason == "too many tool rounds"


def test_unexpected_exception_in_session_tool_becomes_neutral_with_type_name_only():
    class Boom:
        def execute(self, name, tool_input):
            raise KeyError("secret-sentinel-xyz")

    run, _ = go([call_news(), end()], session=Boom())
    assert run.ok is False and run.reason == "KeyError"
    assert "secret-sentinel-xyz" not in run.report.model_dump_json()


def test_duration_ms_is_int():
    run, _ = go([end()])
    assert isinstance(run.duration_ms, int) and run.duration_ms >= 0


# ---------------- catalyst and evidence validation ----------------

def sess(result, is_error=False, tool="get_news"):
    return ScriptSession({tool: (result, is_error)})


def tool_call(name, id_="t1"):
    return response([tool_use(id_, name, {"symbol": "ABCD"})], "tool_use")


def evid(source):
    return [{"point": "p", "source": source}]


def reason_of(run):
    assert run.ok is False
    return run.reason


def test_catalyst_in_digest_catalysts_passes():
    run, client = go([call_news(), end(catalysts=CAT)])  # digest lists "earnings"
    assert run.ok is True
    assert [(c.name, c.source) for c in run.report.catalysts] == [("earnings", "get_news")]
    assert len(client.calls) == 2


def test_catalyst_name_must_be_in_the_digest_lists():
    made_up = [{"name": "moon_landing", "source": "get_news"}]
    run, client = go([call_news(), end(catalysts=made_up), end(catalysts=made_up)])
    assert reason_of(run) == "catalyst moon_landing is not in get_news's output"
    assert [e.source for e in run.report.evidence] == [NEUTRAL_SOURCE]
    assert len(client.calls) == 3  # tool round, bad report, retry


def test_made_up_name_then_correct_retry_succeeds():
    made_up = [{"name": "moon_landing", "source": "get_news"}]
    run, client = go([call_news(), end(catalysts=made_up), end(catalysts=CAT)])
    assert run.ok is True and run.report.catalysts[0].name == "earnings"
    retry = client.calls[2]["messages"][-1]["content"]
    msg = next(b["text"] for b in retry if b["type"] == "text")
    assert "catalyst moon_landing is not in get_news's output" in msg


def test_red_flags_entry_is_an_allowed_catalyst_name():
    run, _ = go([call_news(), end(catalysts=[{"name": "bankruptcy", "source": "get_news"}])])
    assert run.ok is True


ITEM_SESSION = ScriptSession({"get_news": (
    {"headlines": [], "catalysts": ["earnings", "item_9.01"], "red_flags": ["bankruptcy"]},
    False)})


@pytest.mark.parametrize(("given", "stored"), [
    ("Earnings", "earnings"),
    (" earnings ", "earnings"),
    ("EARNINGS", "earnings"),
    ("earnings", "earnings"),
    ("item 9.01", "item_9.01"),
    ("Item-9.01", "item_9.01"),
    ("item__9.01", "item_9.01"),
    ("ITEM - 9.01", "item_9.01"),
    ("  item_9.01  ", "item_9.01"),
    ("Bankruptcy", "bankruptcy"),
])
def test_catalyst_name_variants_validate_and_store_the_tools_spelling(given, stored):
    """Issue #53: case, surrounding space and runs of space/hyphen/underscore are ignored."""
    run, client = go(
        [call_news(), end(catalysts=[{"name": given, "source": "get_news"}])],
        session=ScriptSession(copy.deepcopy(ITEM_SESSION.results)),
    )
    assert run.ok is True
    assert [c.name for c in run.report.catalysts] == [stored]
    assert len(client.calls) == 2  # no retry was spent


def test_stored_spelling_is_the_tools_even_when_the_tool_uses_spaces():
    session = ScriptSession({"get_news": (
        {"headlines": [], "catalysts": ["Item 9.01"], "red_flags": []}, False)})
    run, _ = go([call_news(), end(catalysts=[{"name": "item_9.01", "source": "get_news"}])],
                session=session)
    assert run.ok is True and run.report.catalysts[0].name == "Item 9.01"


@pytest.mark.parametrize("invented", ["earning", "item 9.02", "earnings!", "ear nings",
                                      "item9.01", "moon landing", "", "   "])
def test_invented_catalyst_names_are_still_rejected(invented):
    bad = end(catalysts=[{"name": invented, "source": "get_news"}])
    run, client = go([call_news(), bad, bad],
                     session=ScriptSession(copy.deepcopy(ITEM_SESSION.results)))
    assert run.ok is False
    assert run.reason == f"catalyst {invented} is not in get_news's output"
    assert [e.source for e in run.report.evidence] == [NEUTRAL_SOURCE]
    assert len(client.calls) == 3


def test_normalisation_does_not_cross_sources():
    session = ScriptSession({
        "get_news": ({"catalysts": ["earnings"], "red_flags": []}, False),
        "get_fundamentals": ({"catalysts": ["recent_10q"], "red_flags": []}, False),
    })
    # "Recent 10q" is a get_fundamentals catalyst, cited here against get_news
    wrong = end(catalysts=[{"name": "Recent 10q", "source": "get_news"}])
    q = [
        response([tool_use("a", "get_news", {"symbol": "ABCD"}),
                  tool_use("b", "get_fundamentals", {"symbol": "ABCD"})], "tool_use"),
        wrong, wrong,
    ]
    run, _ = go(q, session=session)
    assert run.ok is False
    assert run.reason == "catalyst Recent 10q is not in get_news's output"


def test_catalyst_description_says_copy_exactly_from_the_tool_lists():
    from agents.schemas import Catalyst
    desc = Catalyst.model_fields["name"].description
    assert "copied exactly" in desc
    assert "`catalysts`" in desc and "`red_flags`" in desc
    assert "8-K item name" not in desc  # the old free-text nudge is gone
    schema_desc = Catalyst.model_json_schema()["properties"]["name"]["description"]
    assert schema_desc == desc


def test_names_from_all_successful_calls_accumulate():
    class TwoCalls:
        n = 0

        def execute(self, name, tool_input):
            self.n += 1
            return ({"catalysts": ["earnings"] if self.n == 1 else ["halt"]}, False)

    cats = [{"name": "earnings", "source": "get_news"}, {"name": "halt", "source": "get_news"}]
    run, _ = go([call_news(), call_news("t2"), end(catalysts=cats)], session=TwoCalls())
    assert run.ok is True


def test_catalyst_naming_uncalled_tool_retries_then_succeeds():
    bad = end(catalysts=[{"name": "recent_10q", "source": "get_fundamentals"}])
    run, client = go([call_news(), bad, end(catalysts=[])])
    assert run.ok is True and run.report.catalysts == []
    assert len(client.calls) == 3
    retry = client.calls[2]["messages"][-1]["content"]
    msg = next(b["text"] for b in retry if b["type"] == "text")
    assert "catalyst source get_fundamentals is not a tool that returned data" in msg


def test_catalyst_naming_uncalled_tool_twice_is_neutral():
    bad = end(catalysts=[{"name": "recent_10q", "source": "get_fundamentals"}])
    run, client = go([call_news(), bad, bad])
    assert reason_of(run) == (
        "catalyst source get_fundamentals is not a tool that returned data in this run")
    assert [e.source for e in run.report.evidence] == [NEUTRAL_SOURCE]
    assert run.report.catalysts == []
    assert len(client.calls) == 3


def test_catalyst_source_of_errored_tool_is_invalid():
    # get_news errored; only a different tool returned data, and the report cites get_news
    s2 = ScriptSession({
        "get_news": ({"error": "x"}, True), "get_other": ({"catalysts": ["earnings"]}, False)})
    ok_ev = end(evidence=evid("get_other"), catalysts=CAT)
    run, _ = go([call_news(), tool_call("get_other", "t2"), ok_ev, ok_ev], session=s2)
    assert reason_of(run) == "catalyst source get_news is not a tool that returned data in this run"


def test_catalyst_source_of_ok_false_result_is_invalid():
    s2 = ScriptSession({
        "get_news": ({"ok": False, "reason": "no data", "catalysts": ["earnings"]}, False),
        "get_other": ({"catalysts": []}, False)})
    ok_ev = end(evidence=evid("get_other"), catalysts=CAT)
    run, _ = go([call_news(), tool_call("get_other", "t2"), ok_ev, ok_ev], session=s2)
    assert reason_of(run) == "catalyst source get_news is not a tool that returned data in this run"


def test_ok_false_result_does_not_block_a_later_good_call():
    class Flaky:
        n = 0

        def execute(self, name, tool_input):
            self.n += 1
            if self.n == 1:
                return {"ok": False, "reason": "x", "catalysts": ["halt"]}, False
            return {"ok": True, "catalysts": ["earnings"]}, False

    run, _ = go([call_news(), call_news("t2"), end(catalysts=CAT)], session=Flaky())
    assert run.ok is True
    # names from the refused first call are NOT allowed
    bad = end(catalysts=[{"name": "halt", "source": "get_news"}])
    run, _ = go([call_news(), call_news("t2"), bad, bad], session=Flaky())
    assert reason_of(run) == "catalyst halt is not in get_news's output"


def test_result_without_ok_key_counts_as_data():
    run, _ = go([call_news(), end(catalysts=CAT)], session=sess(
        {"headlines": [], "catalysts": ["earnings"], "ok": True}))
    assert run.ok is True


@pytest.mark.parametrize("val", [None, 0, "false", ""])
def test_only_literal_false_marks_a_result_as_refusal(val):
    s = sess({"ok": val, "headlines": [], "catalysts": ["earnings"]})
    run, _ = go([call_news(), end(catalysts=CAT)], session=s)
    assert run.ok is True


def test_plain_string_catalysts_are_invalid():
    q = [call_news(), end(catalysts=["earnings"]), end(catalysts=["earnings"])]
    run, _ = go(q)
    assert reason_of(run).startswith("output failed validation at 'catalysts.0'")


def test_catalyst_object_missing_source_is_invalid():
    bad = end(catalysts=[{"name": "earnings"}])
    run, _ = go([call_news(), bad, bad])
    assert "catalysts.0.source" in reason_of(run)


def test_failed_attempt_then_valid_catalyst_uses_tools_from_whole_run():
    bad = response([text("garbage")], "end_turn")
    run, _ = go([call_news(), bad, end(catalysts=CAT)])
    assert run.ok is True


def test_catalysts_with_no_tool_called_at_all_are_invalid():
    run, _ = go([end(catalysts=CAT), end(catalysts=CAT)])
    assert reason_of(run).startswith("evidence source get_news is not a tool")


def test_technical_catalysts_are_always_invalid():
    # the technical tools return no catalysts/red_flags lists, so no name can ever match
    from agents.technical import SPEC as TECH
    from agents.tool_registry import ToolSession
    from tests.fakes import FakeMarketData
    from tools.config import load_limits
    from tools.scan import ScanCriteria

    def session():
        return ToolSession(symbol="AAPL", now=NOW, data=FakeMarketData(),
                           limits=load_limits(), criteria=ScanCriteria())

    def tech_report(**over):
        d = {"agent": "technical", "symbol": "AAPL", "stance": "bullish", "confidence": 0.5,
             "evidence": [{"point": "p", "source": "get_bars"}], "catalysts": []}
        d.update(over)
        return response([text(json.dumps(d))], "end_turn")

    bars = response([tool_use("b", "get_bars", {"symbol": "AAPL"})], "tool_use")
    for name in ("earnings", "gap", "halt", ""):
        cat = [{"name": name, "source": "get_bars"}]
        client = FakeClient([bars, tech_report(catalysts=cat), tech_report(catalysts=cat)])
        run = run_analyst(TECH, "AAPL", NOW, session(), client=client, config=CFG)
        assert run.ok is False
        assert run.reason == f"catalyst {name} is not in get_bars's output"
    client = FakeClient([bars, tech_report()])
    assert run_analyst(TECH, "AAPL", NOW, session(), client=client, config=CFG).ok is True


# ---- evidence sources ----

def test_evidence_source_never_called_is_invalid():
    bad = end(evidence=evid("get_fundamentals"))
    run, _ = go([call_news(), bad, bad])
    assert reason_of(run) == (
        "evidence source get_fundamentals is not a tool that answered in this run")


def test_evidence_source_of_errored_tool_is_invalid():
    s = sess({"error": "boom"}, True)
    run, _ = go([call_news(), end(), end()], session=s)
    assert reason_of(run).startswith("evidence source get_news is not a tool")


def test_evidence_source_of_ok_false_tool_is_valid():
    s = sess({"ok": False, "reason": "none"})
    run, client = go([call_news(), end()], session=s)
    assert run.ok is True and len(client.calls) == 2


def test_catalyst_citing_an_ok_false_tool_is_still_invalid():
    s = sess({"ok": False, "reason": "none", "catalysts": ["earnings"]})
    run, client = go([call_news(), end(catalysts=CAT), end(catalysts=CAT)], session=s)
    assert reason_of(run) == "catalyst source get_news is not a tool that returned data in this run"
    assert len(client.calls) == 3


def test_ok_false_evidence_with_no_catalysts_has_no_retry():
    s = sess({"ok": False, "reason": "none"})
    run, client = go([call_news(), end(confidence=0.0, stance="neutral")], session=s)
    assert run.ok is True and run.report.confidence == 0.0 and len(client.calls) == 2


def test_evidence_source_neutral_marker_is_invalid():
    bad = end(evidence=evid(NEUTRAL_SOURCE))
    run, _ = go([call_news(), bad, bad])
    assert reason_of(run).startswith(f"evidence source {NEUTRAL_SOURCE} is not a tool")


def test_every_evidence_item_is_checked():
    ev = [{"point": "a", "source": "get_news"}, {"point": "b", "source": "made_up"}]
    bad = end(evidence=ev)
    run, _ = go([call_news(), bad, bad])
    assert reason_of(run) == "evidence source made_up is not a tool that answered in this run"


def test_no_tool_called_means_no_valid_report():
    run, client = go([end(), end()])
    assert run.ok is False and len(client.calls) == 2


# ---------------- inputs ----------------

def test_naive_now_and_bad_symbol_raise():
    from datetime import datetime
    with pytest.raises(ValueError):
        run_analyst(SPEC, "ABCD", datetime(2026, 11, 3, 10), ScriptSession(),
                    client=FakeClient(), config=CFG)
    for bad in ("", "AB CD", None, 5):
        with pytest.raises(ValueError, match="valid ticker"):
            run_analyst(SPEC, bad, NOW, ScriptSession(), client=FakeClient(), config=CFG)


def test_symbol_is_normalised_in_report():
    run, _ = go([call_news(), end(symbol="abcd")], symbol=" abcd ")
    assert run.ok is True and run.report.symbol == "ABCD"


# ---------------- output_schema ----------------

ALLOWED = {"type", "enum", "properties", "required", "additionalProperties", "items"}


def walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for k, v in node.get("properties", {}).items():
            yield from walk(v, f"{path}.{k}")
        if "items" in node:
            yield from walk(node["items"], f"{path}[]")


@pytest.mark.parametrize("name", ["technical", "sentiment", "fundamentals"])
def test_output_schema_is_strict_compatible(name):
    schema = output_schema(name)
    nodes = list(walk(schema))
    assert len(nodes) >= 10
    for path, node in nodes:
        assert set(node) <= ALLOWED, (path, set(node) - ALLOWED)
        assert "type" in node or "enum" in node, path
        if node.get("type") == "object":
            assert node["additionalProperties"] is False, path
            assert node["required"] == list(node["properties"]), path
    text_ = json.dumps(schema)
    for banned in ("$ref", "$defs", "anyOf", "allOf", "oneOf", "minLength", "minItems",
                   "minimum", "maximum", "default", "title", "description", "const"):
        assert f'"{banned}"' not in text_


@pytest.mark.parametrize("name", ["technical", "sentiment", "fundamentals"])
def test_output_schema_pins_agent(name):
    props = output_schema(name)["properties"]
    assert props["agent"] == {"type": "string", "enum": [name]}
    assert set(props) == set(AnalystReport.model_fields)


def test_output_schema_catalysts_items_are_objects():
    cat = output_schema("sentiment")["properties"]["catalysts"]
    assert cat["type"] == "array"
    items = cat["items"]
    assert items["type"] == "object"
    assert list(items["properties"]) == ["name", "source"]
    assert items["required"] == ["name", "source"]
    assert items["additionalProperties"] is False
    assert all(p == {"type": "string"} for p in items["properties"].values())


def test_output_schema_evidence_items_and_stance_enum():
    props = output_schema("technical")["properties"]
    assert props["evidence"]["items"]["required"] == ["point", "source"]
    assert props["stance"]["enum"] == ["bullish", "bearish", "neutral"]
    assert props["confidence"] == {"type": "number"}


def test_output_schema_does_not_mutate_between_calls():
    a = output_schema("technical")
    output_schema("sentiment")
    assert a["properties"]["agent"]["enum"] == ["technical"]


# ---------------- request kwargs ----------------

def test_request_contains_spec_prompt_tools_schema():
    _, client = go([end()])
    kw = client.calls[0]
    assert kw["model"] == SONNET
    assert kw["system"][0]["text"] == "PROMPT"
    assert kw["tools"] == SPEC.tools
    assert kw["output_config"]["format"]["schema"] == SPEC.output_schema


# ---------------- refusal_category and fallback_from ----------------

def fb_block(frm, to, category):
    return {"type": "fallback", "from": {"model": frm}, "to": {"model": to},
            "trigger": {"type": "refusal", "category": category}}


def test_fallback_turn_records_category_and_declining_model():
    rec = call_record(fallback_message(), OPUS)
    assert rec.refusal_category == "cyber"
    assert rec.fallback_from == OPUS
    assert rec.stop_reason == "end_turn"


def test_plain_response_has_no_category_or_fallback_from():
    rec = call_record(sdk_message(), OPUS)
    assert rec.refusal_category is None and rec.fallback_from is None


def test_direct_refusal_category_from_stop_details():
    msg = sdk_message(
        content=[], stop_reason="refusal",
        stop_details={"type": "refusal", "category": "bio", "explanation": None},
        usage={"input_tokens": 4, "output_tokens": 0},
    )
    rec = call_record(msg, OPUS)
    assert rec.refusal_category == "bio" and rec.fallback_from is None


def test_last_fallback_block_wins():
    msg = sdk_message(content=[
        fb_block(OPUS, SONNET, "cyber"),
        fb_block(SONNET, "claude-haiku-5-5", "bio"),
        {"type": "text", "text": "{}"},
    ])
    rec = call_record(msg, OPUS)
    assert rec.refusal_category == "bio"
    assert rec.fallback_from == SONNET
