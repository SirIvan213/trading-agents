"""run_structured: tool-less, validated, one retry, never raises. Fake clients only."""
import json
import threading

import pytest

from agents.llm import AgentConfig
from agents.runner import BETAS, model_output_schema, run_structured
from agents.schemas import BearCase, BullCase
from tests.debate_helpers import bull_json, reply
from tests.fakes import FakeClient, api_error, response
from tests.test_runner import fallback_message

CFG = AgentConfig(model="claude-sonnet-5-5", effort="medium")
SENTINEL = "sk-ant-SENTINEL-SECRET"


def run(queue, validate=lambda v: None, cfg=CFG, cancel=None, client=None):
    client = client or FakeClient(queue)
    result = run_structured(
        "bull", "SYSTEM PROMPT", "USER PAYLOAD", BullCase, validate,
        client=client, config=cfg, cancel=cancel,
    )
    return result, client


def test_valid_first_reply():
    r, c = run([reply(bull_json())])
    assert r.ok and isinstance(r.value, BullCase) and r.reason is None and r.reason_code is None
    assert len(c.calls) == 1 and len(r.calls) == 1


def test_invalid_json_then_valid_retries_with_append_only_history():
    r, c = run([reply("not json {"), reply(bull_json())])
    assert r.ok and len(c.calls) == 2 and len(r.calls) == 2
    first, second = c.calls[0]["messages"], c.calls[1]["messages"]
    assert second[: len(first)] == first  # append-only
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert "Your previous attempt failed" in second[-1]["content"]
    assert "Return only the corrected JSON object" in second[-1]["content"]


def test_invalid_twice_fails_with_no_third_call():
    r, c = run([reply("nope"), reply("still nope"), reply(bull_json())])
    assert not r.ok and r.value is None and r.reason_code == "invalid_output"
    assert len(c.calls) == 2 and len(r.calls) == 2
    assert len(c.queue) == 1  # the third reply was never requested


def test_validator_rejection_retries_then_fails():
    r, c = run([reply(bull_json()), reply(bull_json())], validate=lambda v: "bad thing")
    assert not r.ok and r.reason == "bad thing" and r.reason_code == "invalid_output"
    assert len(c.calls) == 2
    assert "bad thing" in c.calls[1]["messages"][-1]["content"]


def test_schema_violation_names_location():
    r, c = run([reply({"symbol": "AAPL"}), reply({"symbol": "AAPL"})])
    assert not r.ok and "failed validation" in r.reason


def test_refusal_gives_refusal_code():
    r, c = run([reply("", stop="refusal"), reply("", stop="refusal")])
    assert not r.ok and r.reason_code == "refusal" and len(c.calls) == 2


def test_max_tokens_gives_max_tokens_code():
    r, c = run([reply("{", stop="max_tokens"), reply("{", stop="max_tokens")])
    assert not r.ok and r.reason_code == "max_tokens" and len(c.calls) == 2


def test_refusal_then_valid_recovers():
    r, c = run([response([], "refusal"), reply(bull_json())])
    assert r.ok and len(c.calls) == 2
    # empty content is not appended as an assistant turn
    assert [m["role"] for m in c.calls[1]["messages"]] == ["user", "user"]


def test_missing_text_block_is_invalid_output():
    r, _ = run([response([], "end_turn"), response([], "end_turn")])
    assert not r.ok and r.reason == "no text output"


def test_api_error_records_type_name_only():
    r, c = run([api_error(SENTINEL)])
    assert not r.ok and r.reason_code == "api_error"
    assert r.reason == "APIConnectionError"
    assert SENTINEL not in repr(r) and SENTINEL not in repr(r.reason)
    assert len(c.calls) == 1


def test_non_api_exception_is_exception_code_no_text():
    r, _ = run([RuntimeError(SENTINEL)])
    assert r.reason_code == "exception" and r.reason == "RuntimeError"
    assert SENTINEL not in repr(r)


def test_validator_raising_never_escapes():
    def boom(_):
        raise ValueError(SENTINEL)

    r, _ = run([reply(bull_json())], validate=boom)
    assert not r.ok and r.reason == "ValueError" and SENTINEL not in repr(r)


def test_cancel_before_run_makes_no_call():
    ev = threading.Event()
    ev.set()
    r, c = run([reply(bull_json())], cancel=ev)
    assert not r.ok and r.reason_code == "cancelled" and r.reason == "cancelled"
    assert c.calls == [] and r.calls == []


def test_cancel_between_attempts():
    ev = threading.Event()

    def cancel_after_first(_):
        ev.set()
        return "reject"

    r, c = run([reply(bull_json()), reply(bull_json())], validate=cancel_after_first, cancel=ev)
    assert r.reason_code == "cancelled" and len(c.calls) == 1


def test_one_call_record_per_response():
    r, _ = run([reply("bad"), reply(bull_json())])
    assert len(r.calls) == 2
    assert all(c.requested_model == CFG.model and c.serving_model for c in r.calls)


def test_real_sdk_fallback_response_gives_serving_model_from_iteration():
    msg = fallback_message()
    msg.content[-1].text = json.dumps(bull_json())
    r, _ = run([msg])
    assert r.ok and len(r.calls) == 1
    call = r.calls[0]
    assert call.fallback_ran is True
    assert call.serving_model == "claude-sonnet-5-5"
    assert call.requested_model == CFG.model


def test_request_kwargs_shape():
    r, c = run([reply(bull_json())])
    kw = c.calls[0]
    for forbidden in ("tools", "thinking", "temperature", "tool_choice"):
        assert forbidden not in kw
    assert kw["model"] == CFG.model and kw["max_tokens"] == CFG.max_tokens
    assert kw["betas"] == BETAS and kw["fallbacks"] == "default"
    assert kw["output_config"]["effort"] == "medium"
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] == model_output_schema(BullCase)
    assert kw["system"] == [
        {"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"][0] == {"role": "user", "content": "USER PAYLOAD"}


def test_no_fallbacks_when_disabled():
    cfg = AgentConfig(fallbacks=False)
    _, c = run([reply(bull_json())], cfg=cfg)
    assert "betas" not in c.calls[0] and "fallbacks" not in c.calls[0]


def test_with_options_timeout_applied():
    seen = {}
    inner = FakeClient([reply(bull_json())])

    class Wrapped:
        def with_options(self, **kw):
            seen.update(kw)
            return inner

    cfg = AgentConfig(request_timeout_s=7.5)
    r = run_structured("bull", "P", "U", BullCase, lambda v: None, client=Wrapped(), config=cfg)
    assert r.ok and seen == {"timeout": 7.5} and len(inner.calls) == 1


@pytest.mark.parametrize("model", [BullCase, BearCase])
def test_output_schema_is_strict_compatible(model):
    schema = model_output_schema(model)

    def walk(node):
        if isinstance(node, dict):
            assert "anyOf" not in node and "$ref" not in node and "default" not in node
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert node["required"] == list(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    assert schema["properties"]["plan_id"]["type"] == ["string", "null"]
    assert set(schema["required"]) == set(schema["properties"])
    assert "plan_id" in schema["required"]

