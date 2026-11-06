"""Bull/Bear debate loop: order, visibility, rounds, early stop, fail-closed, audit records."""
import json

import pytest

from agents import debate as debate_mod
from agents.debate import resolve_rounds, run_debate
from agents.llm import AgentConfig
from agents.schemas import BearCase, BullCase, DebateRecord
from pipeline import debate as pipeline_debate
from tests.debate_helpers import (
    ENTRY,
    STOP,
    bear_json,
    bull_json,
    make_packet,
    reply,
    user_json,
)
from tests.fakes import FakeClient, api_error
from tests.test_runner import fallback_message

PKT = make_packet()
BAD = "buy 100 shares at 99.10, stop 98"
BADBULL = bull_json(argument=BAD)
BADBEAR = bear_json(objections=[BAD, "b", "c"])
CFGS = {"bull": AgentConfig(model="bull-model"), "bear": AgentConfig(model="bear-model")}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ("TRADING_AGENTS_DEBATE_ROUNDS", "TRADING_AGENTS_MODEL", "TRADING_AGENTS_BULL_MODEL",
              "TRADING_AGENTS_BEAR_MODEL", "TRADING_AGENTS_EFFORT", "TRADING_AGENTS_BULL_EFFORT",
              "TRADING_AGENTS_BEAR_EFFORT"):
        monkeypatch.delenv(v, raising=False)


def bull_case(tag: str) -> dict:
    return bull_json(argument=f"bull argument {tag}", key_points=[f"bull point {tag}"])


def bear_case(tag: str, fatal: bool = False) -> dict:
    return bear_json(objections=[f"{tag} one", f"{tag} two", f"{tag} three"], fatal=fatal)


def go(queue, **kw):
    c = FakeClient(queue)
    kw.setdefault("configs", CFGS)
    return run_debate(PKT, clients=c, **kw), c


# ---------------- default flow and visibility ----------------

def test_default_two_rounds_order_and_visibility():
    rec, c = go([reply(bull_case("b1")), reply(bear_case("r1")),
                 reply(bull_case("b2")), reply(bear_case("r2"))])
    assert rec.rounds_planned == 2 and rec.rounds_run == 2
    assert [(t.round, t.side) for t in rec.turns] == [
        (1, "bull"), (1, "bear"), (2, "bull"), (2, "bear")]
    assert len(c.calls) == 4 and c.queue == []
    seen = [user_json(call) for call in c.calls]
    assert [s["round"] for s in seen] == [1, 1, 2, 2]
    assert seen[0]["opponent_latest_case"] is None                          # bull1
    assert seen[1]["opponent_latest_case"]["argument"] == "bull argument b1"  # bear1 <- bull1
    assert seen[2]["opponent_latest_case"]["objections"][0] == "r1 one"       # bull2 <- bear1
    assert seen[3]["opponent_latest_case"]["argument"] == "bull argument b2"  # bear2 <- bull2
    assert [call["system"][0]["text"] for call in c.calls][:2] == [
        debate_mod.run_bull.__globals__["PROMPT"], debate_mod.run_bear.__globals__["PROMPT"]]
    assert rec.all_ok and not rec.fatal and not rec.stopped_early
    assert rec.final_bull.argument == "bull argument b2"
    assert rec.final_bear.objections[0] == "r2 one"
    assert rec.symbol == "AAPL" and rec.as_of == PKT.as_of


def test_record_is_json_serializable_and_roundtrips():
    rec, _ = go([reply(bull_case("b")), reply(bear_case("r"))], rounds=1)
    data = rec.model_dump(mode="json")
    json.dumps(data)
    assert DebateRecord.model_validate(data) == rec


# ---------------- rounds ----------------

@pytest.mark.parametrize("n", [1, 2, 3])
def test_rounds_param(n):
    queue = [reply(bull_case("b")), reply(bear_case("r"))] * n
    rec, c = go(queue, rounds=n)
    assert rec.rounds_planned == n and rec.rounds_run == n and len(c.calls) == 2 * n


def test_rounds_env(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_DEBATE_ROUNDS", "3")
    rec, c = go([reply(bull_case("b")), reply(bear_case("r"))] * 3)
    assert rec.rounds_planned == 3 and len(c.calls) == 6


def test_rounds_param_beats_env(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_DEBATE_ROUNDS", "3")
    rec, _ = go([reply(bull_case("b")), reply(bear_case("r"))], rounds=1)
    assert rec.rounds_planned == 1


def test_rounds_default_is_two():
    assert resolve_rounds(None) == 2
    assert resolve_rounds(5) == 5 and resolve_rounds(1) == 1


@pytest.mark.parametrize("bad", [0, 6, -1, True, False, 1.5, 2.0, "x", "2"])
def test_bad_rounds_param_raises_before_any_call(bad):
    c = FakeClient([reply(bull_case("b"))])
    with pytest.raises(ValueError):
        run_debate(PKT, clients=c, configs=CFGS, rounds=bad)
    assert c.calls == []


@pytest.mark.parametrize("bad", ["0", "6", "x", "", "1.5", " "])
def test_bad_rounds_env_raises_before_any_call(monkeypatch, bad):
    monkeypatch.setenv("TRADING_AGENTS_DEBATE_ROUNDS", bad)
    c = FakeClient([reply(bull_case("b"))])
    with pytest.raises(ValueError):
        run_debate(PKT, clients=c, configs=CFGS)
    assert c.calls == []


# ---------------- early stop ----------------

@pytest.mark.parametrize("planned", [2, 3])
def test_bear_fatal_in_round_one_stops_early(planned):
    rec, c = go([reply(bull_case("b1")), reply(bear_case("r1", fatal=True)),
                 reply(bull_case("b2")), reply(bear_case("r2"))], rounds=planned)
    assert rec.rounds_run == 1 and rec.stopped_early is True and rec.fatal is True
    assert len(c.calls) == 2 and len(rec.turns) == 2
    assert rec.final_bear.fatal is True
    assert rec.rounds_planned == planned


def test_bear_fatal_in_last_round_is_not_stopped_early():
    rec, c = go([reply(bull_case("b1")), reply(bear_case("r1")),
                 reply(bull_case("b2")), reply(bear_case("r2", fatal=True))], rounds=2)
    assert rec.rounds_run == 2 and rec.stopped_early is False and rec.fatal is True
    assert len(c.calls) == 4


def test_bear_fatal_in_middle_round_stops_there():
    rec, c = go([reply(bull_case("b1")), reply(bear_case("r1")),
                 reply(bull_case("b2")), reply(bear_case("r2", fatal=True)),
                 reply(bull_case("b3")), reply(bear_case("r3"))], rounds=3)
    assert rec.rounds_run == 2 and rec.stopped_early and rec.fatal and len(c.calls) == 4


# ---------------- failure handling ----------------

def test_failed_bull_turn_bear_sees_last_valid_bull_and_all_ok_false():
    rec, c = go([reply(bull_case("b1")), reply(bear_case("r1")),
                 reply(BADBULL), reply(BADBULL),                   # round 2 bull fails
                 reply(bear_case("r2"))], rounds=2)
    assert len(c.calls) == 5
    bear2 = user_json(c.calls[4])
    assert bear2["opponent_latest_case"]["argument"] == "bull argument b1"
    assert rec.final_bull.argument == "bull argument b1"
    assert rec.all_ok is False and rec.fatal is False and rec.rounds_run == 2
    t = rec.turns[2]
    assert t.side == "bull" and t.run.ok is False and t.run.reason_code == "invalid_output"
    assert len(t.run.calls) == 2


def test_failed_bull_first_round_bear_sees_none_and_final_bull_none():
    rec, c = go([reply(BADBULL), reply(BADBULL), reply(bear_case("r1"))], rounds=1)
    assert user_json(c.calls[2])["opponent_latest_case"] is None
    assert rec.final_bull is None and rec.all_ok is False
    placeholder = rec.turns[0].case
    assert isinstance(placeholder, BullCase) and "unavailable" in placeholder.argument
    assert BAD not in placeholder.argument


def test_failed_bull_placeholder_never_shown_to_opponent():
    rec, c = go([reply(BADBULL), reply(BADBULL), reply(bear_case("r1")),
                 reply(bull_case("b2")), reply(bear_case("r2"))], rounds=2)
    for call in c.calls:
        assert "bull case unavailable" not in call["messages"][0]["content"]


def test_failed_bear_turn_fails_closed_and_stops():
    rec, c = go([reply(bull_case("b1")), reply(BADBEAR), reply(BADBEAR),
                 reply(bull_case("b2")), reply(bear_case("r2"))], rounds=2)
    assert len(c.calls) == 3
    assert rec.fatal is True and rec.stopped_early is True and rec.rounds_run == 1
    assert rec.all_ok is False
    bear_turn = rec.turns[1]
    assert bear_turn.run.ok is False and bear_turn.case.fatal is True
    assert rec.final_bear.fatal is True and BAD not in " ".join(rec.final_bear.objections)


def test_failed_bear_in_last_round_is_fatal_not_early():
    rec, _ = go([reply(bull_case("b1")), reply(bear_case("r1")), reply(bull_case("b2")),
                 reply("junk"), reply("junk")], rounds=2)
    assert rec.fatal and not rec.stopped_early and rec.rounds_run == 2


def test_bear_api_error_fails_closed():
    rec, _ = go([reply(bull_case("b1")), api_error("SECRETXYZ")], rounds=2)
    assert rec.fatal and rec.stopped_early
    assert rec.turns[1].run.reason_code == "api_error"
    assert "SECRETXYZ" not in json.dumps(rec.model_dump(mode="json"))


def test_bull_api_error_does_not_stop_debate():
    rec, c = go([api_error("x"), reply(bear_case("r1"))], rounds=1)
    assert rec.turns[0].run.reason_code == "api_error" and rec.all_ok is False
    assert rec.fatal is False and len(c.calls) == 2


def test_exception_in_bull_turn_never_escapes(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SECRETBOOM")
    monkeypatch.setattr(debate_mod, "run_bull", boom)
    rec, c = go([reply(bear_case("r1"))], rounds=1)
    assert rec.turns[0].run.ok is False and rec.turns[0].run.reason_code == "exception"
    assert rec.turns[0].run.reason == "RuntimeError" and rec.all_ok is False
    assert rec.final_bull is None and len(c.calls) == 1
    assert "SECRETBOOM" not in json.dumps(rec.model_dump(mode="json"))


def test_exception_in_bear_turn_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SECRETBOOM")
    monkeypatch.setattr(debate_mod, "run_bear", boom)
    rec, c = go([reply(bull_case("b1")), reply(bull_case("b2"))], rounds=2)
    assert rec.fatal and rec.stopped_early and rec.rounds_run == 1
    assert rec.turns[1].run.reason_code == "exception" and rec.turns[1].case.fatal
    assert len(c.calls) == 1
    assert "SECRETBOOM" not in json.dumps(rec.model_dump(mode="json"))


def test_cancel_event_produces_failed_turns_without_calls():
    import threading
    ev = threading.Event()
    ev.set()
    c = FakeClient([])
    rec = run_debate(PKT, clients=c, configs=CFGS, rounds=2, cancel=ev)
    assert c.calls == [] and rec.fatal and rec.all_ok is False
    assert rec.turns[0].run.reason_code == "cancelled"


# ---------------- models, audit ----------------

def test_same_model_true_by_default():
    rec, c = go([reply(bull_case("b")), reply(bear_case("r"))], rounds=1, configs=None)
    assert rec.same_model is True
    assert [t.run.requested_model for t in rec.turns] == ["claude-sonnet-5-5"] * 2
    assert [call["model"] for call in c.calls] == ["claude-sonnet-5-5"] * 2


def test_same_model_false_when_bear_model_differs(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_BEAR_MODEL", "claude-opus-5-5")
    rec, c = go([reply(bull_case("b")), reply(bear_case("r"))], rounds=1, configs=None)
    assert rec.same_model is False
    assert rec.turns[0].run.requested_model == "claude-sonnet-5-5"
    assert rec.turns[1].run.requested_model == "claude-opus-5-5"
    assert [call["model"] for call in c.calls] == ["claude-sonnet-5-5", "claude-opus-5-5"]


def test_explicit_configs_drive_requested_model():
    rec, c = go([reply(bull_case("b")), reply(bear_case("r"))], rounds=1)
    assert rec.same_model is False
    assert [t.run.requested_model for t in rec.turns] == ["bull-model", "bear-model"]
    assert [t.run.agent for t in rec.turns] == ["bull", "bear"]
    assert [call["model"] for call in c.calls] == ["bull-model", "bear-model"]


def test_partial_configs_fall_back_to_config_for(monkeypatch):
    monkeypatch.setenv("TRADING_AGENTS_BEAR_MODEL", "env-bear")
    rec, _ = go([reply(bull_case("b")), reply(bear_case("r"))], rounds=1,
                configs={"bull": AgentConfig(model="given-bull")})
    assert [t.run.requested_model for t in rec.turns] == ["given-bull", "env-bear"]


def test_one_call_record_per_call_with_serving_model():
    rec, c = go([reply("junk"), reply(bull_case("b")), reply(bear_case("r"))], rounds=1)
    assert len(c.calls) == 3
    assert [len(t.run.calls) for t in rec.turns] == [2, 1]
    for t in rec.turns:
        for call in t.run.calls:
            assert call.requested_model == t.run.requested_model
            assert call.serving_model == "claude-sonnet-5-5"


def test_fallback_turn_records_serving_model_from_real_sdk_response():
    msg = fallback_message(response_model="claude-opus-5-5")
    msg.content[-1].text = json.dumps(bull_case("b"))
    cfgs = {"bull": AgentConfig(model="claude-opus-5-5"), "bear": AgentConfig(model="x-bear")}
    rec, _ = go([msg, reply(bear_case("r"))], rounds=1, configs=cfgs)
    call = rec.turns[0].run.calls[0]
    assert rec.turns[0].run.ok and call.fallback_ran is True
    assert call.requested_model == "claude-opus-5-5"
    assert call.serving_model == "claude-sonnet-5-5" != call.requested_model
    assert rec.turns[1].run.calls[0].fallback_ran is False


# ---------------- clients ----------------

def test_dict_of_clients_per_side():
    cb = FakeClient([reply(bull_case("b1")), reply(bull_case("b2"))])
    cr = FakeClient([reply(bear_case("r1")), reply(bear_case("r2"))])
    rec = run_debate(PKT, clients={"bull": cb, "bear": cr}, configs=CFGS)
    assert len(cb.calls) == 2 and len(cr.calls) == 2 and rec.all_ok
    assert all(c["model"] == "bull-model" for c in cb.calls)
    assert all(c["model"] == "bear-model" for c in cr.calls)


def test_single_shared_client():
    rec, c = go([reply(bull_case("b")), reply(bear_case("r"))], rounds=1)
    assert len(c.calls) == 2 and rec.all_ok


def test_no_tools_and_no_runs_in_any_debate_request():
    _, c = go([reply(bull_case("b")), reply(bear_case("r"))], rounds=1)
    for call in c.calls:
        assert "tools" not in call and "runs" not in user_json(call)


# ---------------- plan references / money ----------------

def test_debate_with_plan_quotes_ok_and_no_money_leaks():
    b = bull_json(argument=f"entry {ENTRY}, stop {STOP}")
    rec, _ = go([reply(b), reply(bear_case("r"))], rounds=1)
    assert rec.all_ok and rec.final_bull.argument == f"entry {ENTRY}, stop {STOP}"


def test_money_text_never_reaches_case_texts():
    # Regression: the failure reason (naming the offending mention, e.g. '100 shares;
    # at 99.10') used to be embedded in the placeholder Bull argument and in the
    # fail-closed Bear objections, so model-stated money reached case text the trader reads.
    from agents.money_guard import allowed_values, money_violations
    rec, _ = go([reply(BADBULL), reply(BADBULL), reply(BADBEAR), reply(BADBEAR)], rounds=2)
    allowed = allowed_values(PKT)
    texts = []
    for t in rec.turns:
        c = t.case
        texts += [c.argument, *c.key_points] if isinstance(c, BullCase) else list(c.objections)
    texts += list(rec.final_bear.objections)
    leaked = [m for x in texts for m in money_violations(x, allowed)]
    assert leaked == []


def test_offending_full_reply_text_is_not_stored():
    rec, _ = go([reply(BADBULL), reply(BADBULL), reply(BADBEAR), reply(BADBEAR)], rounds=2)
    assert BAD not in json.dumps(rec.model_dump(mode="json"))
    assert isinstance(rec.final_bear, BearCase) and rec.fatal


# ---------------- pipeline.debate.run_debate_sync ----------------

def test_run_debate_sync_returns_valid_debate_record_dict():
    c = FakeClient([reply(bull_case("b")), reply(bear_case("r"))])
    out = pipeline_debate.run_debate_sync(PKT, client=c, configs=CFGS, rounds=1)
    assert isinstance(out, dict)
    json.dumps(out)
    rec = DebateRecord.model_validate(out)
    assert rec.rounds_run == 1 and rec.symbol == "AAPL" and rec.all_ok


def test_run_debate_sync_default_configs_and_make_client(monkeypatch):
    c = FakeClient([reply(bull_case("b")), reply(bear_case("r"))])
    monkeypatch.setattr(pipeline_debate, "make_client", lambda: c)
    out = pipeline_debate.run_debate_sync(PKT, rounds=1)
    assert DebateRecord.model_validate(out).same_model is True
    assert len(c.calls) == 2


def test_run_debate_sync_bad_rounds_raises_and_makes_no_call():
    c = FakeClient([reply(bull_case("b"))])
    with pytest.raises(ValueError):
        pipeline_debate.run_debate_sync(PKT, client=c, configs=CFGS, rounds=0)
    assert c.calls == []
