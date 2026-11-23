"""Decision records (P5-UI-1, #211): one display-only JSON file per decision, written however
run_decision ends, best effort and with no effect on the decision. Offline: the scripted fake
clients of test_phase4b_proofs, the network blocked, no billed call.

The golden records in tests/fixtures/records/ are written by these runs, with the volatile
values normalized (the random run_id, and every duration in ms set to 0). UI-4 renders them.
Regenerate after a deliberate change:
    UPDATE_GOLDEN=1 .venv/Scripts/python.exe -m pytest -q tests/test_decision_records.py
"""
from __future__ import annotations

import ast
import errno
import json
import logging
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest

import pipeline.decide as decide
import pipeline.records as records
from journal.log import APPROVED, Journal
from pipeline.order_flow import limit_price
from tests.debate_helpers import bear_json
from tests.fakes import NOON, FakeAllData, response, text
from tests.order_helpers import QuotingData, StepClock
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse)
from tests.test_phase4b_proofs import (  # noqa: F401 - autouse fixtures
    SYMBOL,
    Router,
    fixed_limits,
    go,
    isolated_agent_env,
    scripts,
    trader_json,
)

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "fixtures" / "records"


def record_files(tmp_path):
    return sorted((tmp_path / "records").rglob("*.json"))


def only_record(tmp_path):
    files = record_files(tmp_path)
    assert len(files) == 1, files
    return files[0], json.loads(files[0].read_text(encoding="utf-8"))


def normalized(rec: dict) -> dict:
    """The record with its volatile values fixed: the run_id and every duration in ms."""
    def walk(x):
        if isinstance(x, dict):
            return {k: (0 if k == "duration_ms" else walk(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    out = walk(rec)
    out["run_id"] = "RUN_ID"
    out["timings_ms"] = {k: 0 for k in out["timings_ms"]}
    return out


def check_golden(name: str, rec: dict) -> None:
    path = GOLDEN / f"{name}.json"
    rec = normalized(rec)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert rec == json.loads(path.read_text(encoding="utf-8"))


# ---- written however the decision ends ----

def test_an_approved_buy_writes_the_whole_record(tmp_path):
    result, _, _ = go(tmp_path, scripts())
    path, rec = only_record(tmp_path)
    assert path.name == f"{result.ledger_run_id}.json" and rec["run_id"] == result.ledger_run_id
    assert path.parent.name == NOON.date().isoformat()
    assert rec["schema"] == 1 and rec["symbol"] == SYMBOL
    assert rec["coverage"]["covered"] is True
    assert set(rec["analysts"]) == {"technical", "sentiment", "fundamentals"}
    tech = rec["analysts"]["technical"]
    assert tech["report"]["stance"] and tech["report"]["evidence"][0]["point"]
    assert "usage" in tech["run"]["calls"][0] and "reason_code" in tech["run"]
    assert rec["exit_plans"] and rec["exit_plans"][0]["plan_id"] == "plan-1"
    assert rec["debate"]["turns"] and "fatal" in rec["debate"]
    assert {"stopped_early", "skipped", "final_bull", "final_bear"} <= set(rec["debate"])
    t = rec["trader"]
    assert (t["action"], t["plan_id"]) == ("BUY", "plan-1") and t["reasoning"]
    assert 0 <= t["confidence"] <= 1 and t["forced_pass"] is None
    assert rec["proposal"]["limit_price"] == limit_price(result.proposal)
    assert rec["outcome"]["status"] == "approved_not_sent" and rec["error"] is None
    assert set(rec["timings_ms"]) == set(result.timings_ms)
    assert rec["time_budget_s"] == {k: v for k, v in result.budget.items()}
    assert rec["checked_at"] == NOON.isoformat() and rec["outcome"]["approval"] == APPROVED
    assert rec["proposal"]["approved_limit_price"] == rec["proposal"]["limit_price"]
    check_golden("buy_approved", rec)


def test_a_declined_ticket_is_recorded(tmp_path):
    go(tmp_path, scripts(), ask=lambda _: "no")
    rec = only_record(tmp_path)[1]
    assert rec["outcome"]["status"] == "declined" and rec["outcome"]["approval"] == "declined"
    assert rec["proposal"]["approved_limit_price"] is None
    check_golden("buy_declined", rec)


def test_a_trader_pass_is_recorded(tmp_path):
    go(tmp_path, scripts(trader=trader_json(action="PASS", plan_id=None)))
    rec = only_record(tmp_path)[1]
    assert rec["outcome"]["status"] == "pass" and rec["proposal"] is None
    assert rec["trader"]["action"] == "PASS" and rec["trader"]["forced_pass"] is None


def test_a_fatal_bear_forced_pass_is_recorded(tmp_path):
    go(tmp_path, scripts(bear=bear_json(fatal=True, plan_id="plan-1", symbol=SYMBOL)))
    rec = only_record(tmp_path)[1]
    assert rec["trader"]["forced_pass"] == "fatal_bear" and rec["debate"]["fatal"] is True
    assert rec["outcome"]["status"] == "pass"
    check_golden("pass_fatal_bear", rec)


def test_a_stale_forced_pass_is_recorded(tmp_path):
    clock = StepClock(NOON, NOON, NOON + timedelta(seconds=290))
    go(tmp_path, scripts(), clock=clock)
    rec = only_record(tmp_path)[1]
    assert rec["trader"]["forced_pass"] == "stale_before_trader"
    assert rec["time_budget_s"]["age_before_trader_s"] == 290.0


def test_a_skip_is_recorded_before_any_analyst(tmp_path, monkeypatch):
    monkeypatch.setattr(decide, "reporting_check", lambda *a: {
        "covered": False, "reason": "no 8-K item 2.02 in the last 100 days",
        "last_item_202": None})
    with pytest.raises(decide.SymbolSkipped):
        go(tmp_path, scripts())
    rec = only_record(tmp_path)[1]
    assert rec["outcome"]["status"] == "skipped" and rec["error"] is None
    assert rec["coverage"] == {"covered": False, "last_item_202": None, "fetch_failed": False,
                               "reason": "no 8-K item 2.02 in the last 100 days"}
    assert rec["analysts"] == {} and rec["trader"] is None
    check_golden("skipped", rec)


def run_direct(tmp_path, scr=None, **kw):
    """run_decision with the fakes, keeping the router and the clock."""
    router = Router(scr or scripts())
    clock = kw.pop("clock", None) or StepClock(NOON, NOON, NOON + timedelta(seconds=20),
                                               NOON + timedelta(seconds=40))
    journal = Journal(tmp_path / "j.jsonl")
    result = decide.run_decision(
        SYMBOL, data=QuotingData(FakeAllData(), clock, SYMBOL, "103.10"), clients=router,
        journal=journal, clock=clock, rounds=1, timeout_s=10, **kw)
    return result, router, clock, journal


@pytest.mark.parametrize(("exc", "code"), [("CapReached", "cap_reached"),
                                           ("AlreadyDecided", "already_decided")])
def test_a_stop_before_the_analysts_is_not_started_and_recorded_by_type_only(tmp_path, exc,
                                                                              code):
    import pipeline.budget as budget

    def stop():
        raise getattr(budget, exc)("decision cap reached (10 today)")

    router = Router(scripts())
    with pytest.raises(budget.CapReached):
        clock = StepClock(NOON)
        decide.run_decision(SYMBOL, data=QuotingData(FakeAllData(), clock, SYMBOL, "103.10"),
                            clients=router, journal=Journal(tmp_path / "j.jsonl"), clock=clock,
                            ask=None, rounds=1, timeout_s=10, before_analysts=stop)
    assert router.calls == []
    rec = only_record(tmp_path)[1]
    assert rec["outcome"]["status"] == "not_started"  # never counted, no billed call
    assert rec["error"] == {"type": exc, "reason_code": code}
    assert "decision cap reached" not in json.dumps(rec)


def test_a_failed_analyst_skips_the_debate_and_records_its_reason_code_only(tmp_path):
    bad = [response([text("this is not a JSON report")], "end_turn") for _ in range(4)]
    scr = scripts()
    scr["technical"] = bad
    result, router, _, _ = run_direct(tmp_path, scr, ask=lambda _: "yes")
    rec = only_record(tmp_path)[1]
    tech = rec["analysts"]["technical"]
    # a failed analyst's report is the orchestrator's neutral placeholder, not model output
    assert (tech["report"]["stance"], tech["report"]["confidence"]) == ("neutral", 0.0)
    assert tech["run"]["ok"] is False and tech["run"]["reason_code"]
    assert "reason" not in tech["run"]
    assert all("reason" not in r for r in tech["run"]["retries"])
    assert rec["debate"]["skipped"] is True and rec["debate"]["skip_reason"]
    assert rec["trader"]["forced_pass"] == "analyst_failed"
    assert rec["outcome"]["status"] == "pass" and "trader" not in router.calls
    check_golden("pass_analyst_failed", rec)


def test_a_sent_order_is_recorded_after_the_send(tmp_path, monkeypatch):
    from tests.order_helpers import FakePaperBroker

    order = []
    broker = FakePaperBroker()
    real_send = decide.send_approved
    monkeypatch.setattr(decide, "send_approved",
                        lambda *a, **k: (order.append("send"), real_send(*a, **k))[1])
    real = records.write_record
    monkeypatch.setattr(decide, "write_record",
                        lambda *a, **k: (order.append("record"), real(*a, **k))[1])
    run_direct(tmp_path, ask=lambda _: "yes", broker=broker)
    rec = only_record(tmp_path)[1]
    assert order == ["send", "record"]
    assert rec["outcome"]["sent"] == "submitted"
    assert rec["outcome"]["status"] == rec["outcome"]["approval"] == APPROVED


def test_a_ticket_not_asked_is_recorded(tmp_path):
    run_direct(tmp_path, ask=None)
    rec = only_record(tmp_path)[1]
    assert rec["outcome"]["status"] == "not_asked" and rec["proposal"]["ok"] is True


def test_a_journal_lock_at_approval_is_recorded(tmp_path, monkeypatch):
    from journal.log import JournalLockTimeout

    def locked(*a, **k):
        raise JournalLockTimeout("journal lock timed out")

    monkeypatch.setattr(decide, "approve", locked)
    run_direct(tmp_path, ask=lambda _: "yes")
    rec = only_record(tmp_path)[1]
    assert rec["outcome"]["status"] == rec["outcome"]["approval"] == "journal_locked"


def test_a_pass_that_could_not_be_journaled_is_flagged(tmp_path, monkeypatch):
    from journal.log import JournalError

    monkeypatch.setattr(decide, "_append_with_retry",
                        lambda journal, entry: JournalError("journal write failed (OSError)"))
    run_direct(tmp_path, scripts(trader=trader_json(action="PASS", plan_id=None)),
               ask=lambda _: "yes")
    rec = only_record(tmp_path)[1]
    assert rec["outcome"]["status"] == "pass" and rec["outcome"]["record_error"] is True


def test_an_error_after_the_approval_keeps_the_approval(tmp_path, monkeypatch):
    from tests.order_helpers import FakePaperBroker

    def boom(*a, **k):
        raise RuntimeError("broker client crashed: token sk-SECRET")

    monkeypatch.setattr(decide, "send_approved", boom)
    with pytest.raises(RuntimeError):
        run_direct(tmp_path, ask=lambda _: "yes", broker=FakePaperBroker())
    path, rec = only_record(tmp_path)
    assert rec["outcome"]["status"] == "error" and rec["outcome"]["approval"] == APPROVED
    assert "SECRET" not in path.read_text(encoding="utf-8")


def test_a_failure_is_recorded_by_type_never_by_message(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("upstream said: x-api-key sk-ant-SECRET-123 rejected")

    monkeypatch.setattr(decide, "run_analysts", boom)
    with pytest.raises(RuntimeError):
        go(tmp_path, scripts())
    path, rec = only_record(tmp_path)
    assert rec["outcome"]["status"] == "error"
    assert rec["error"] == {"type": "RuntimeError", "reason_code": "exception"}
    assert "SECRET" not in path.read_text(encoding="utf-8")


def test_an_agent_runs_free_text_reason_is_never_recorded():
    from agents.schemas import AgentRunRecord, RetryRecord

    run = AgentRunRecord(agent="technical", requested_model="m", effort="low", ok=False,
                         reason="HTTP 401: invalid x-api-key sk-SECRET", reason_code="api_error",
                         duration_ms=5, retries=[RetryRecord(attempt=1, reason="sk-SECRET",
                                                             reason_code="api_error")])
    out = records._run(run)
    assert out["reason_code"] == "api_error" and "reason" not in out
    assert out["retries"] == [{"attempt": 1, "reason_code": "api_error"}]
    assert "SECRET" not in json.dumps(out)


# ---- best effort, with no effect on the decision ----

def journal_shape(journal, run_id):
    """The journal's entries with the random ids taken out, to compare two runs."""
    text = json.dumps(journal.entries(), sort_keys=True).replace(run_id, "RUN")
    return re.sub(r"ta-[0-9a-f]{32}", "CID", text)


def baseline_run(tmp_path):
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return "yes"

    result, router, clock, journal = run_direct(tmp_path, ask=ask)
    return (result, router, journal, [p.replace(result.ledger_run_id, "RUN") for p in prompts],
            clock.calls)


@pytest.mark.parametrize("failure", ["disk_full", "permission", "unserializable", "builder"])
def test_a_failed_record_changes_nothing(tmp_path, monkeypatch, caplog, failure):
    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()
    ok_result, ok_router, ok_journal, ok_prompts, ok_clock_calls = baseline_run(tmp_path / "ok")

    if failure == "disk_full":
        def fail(*a, **k):
            raise OSError(errno.ENOSPC, "No space left on device")
        monkeypatch.setattr(records, "atomic_write_text", fail)
    elif failure == "permission":
        def fail(*a, **k):
            raise PermissionError(errno.EACCES, "denied")
        monkeypatch.setattr(records, "atomic_write_text", fail)
    elif failure == "unserializable":
        real = records.build_record
        monkeypatch.setattr(records, "build_record",
                            lambda **k: {**real(**k), "bad": object()})
        monkeypatch.setattr(decide, "build_record", records.build_record)
    else:
        def broken(**k):
            raise AttributeError("a field went missing")
        monkeypatch.setattr(decide, "build_record", broken)

    with caplog.at_level(logging.WARNING, logger="pipeline.records"):
        result, router, journal, prompts, clock_calls = baseline_run(tmp_path / "bad")
    assert record_files(tmp_path / "bad") == []
    assert any("not written" in r.getMessage() for r in caplog.records)
    # the decision is exactly the same: outcome, journal entries, approval, calls, timings
    assert result.outcome.status == ok_result.outcome.status == "approved_not_sent"
    assert journal_shape(journal, result.ledger_run_id) == journal_shape(
        ok_journal, ok_result.ledger_run_id)
    # the analysts run in parallel, so their call order varies: compare the calls made
    assert prompts == ok_prompts and sorted(router.calls) == sorted(ok_router.calls)
    assert set(result.timings_ms) == set(ok_result.timings_ms)
    assert result.budget == ok_result.budget
    assert clock_calls == ok_clock_calls  # not one extra clock reading


def test_the_record_is_written_after_the_approval(tmp_path, monkeypatch):
    order = []
    real = records.write_record
    monkeypatch.setattr(decide, "write_record",
                        lambda *a, **k: (order.append("record"), real(*a, **k))[1])

    def ask(prompt):
        order.append("ask")
        return "yes"

    go(tmp_path, scripts(), ask=ask)
    assert order == ["ask", "record"]


def test_a_failing_record_writer_cannot_turn_a_failure_into_something_else(tmp_path,
                                                                            monkeypatch):
    async def boom(*a, **k):
        raise TimeoutError

    def fail(*a, **k):
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(decide, "run_analysts", boom)
    monkeypatch.setattr(records, "atomic_write_text", fail)
    with pytest.raises(TimeoutError):  # the original exception, unchanged
        go(tmp_path, scripts())


@pytest.mark.parametrize(("run_id", "day"), [("../x", "2026-11-16"), ("ok", "../2026")])
def test_an_unsafe_record_path_is_refused(tmp_path, run_id, day):
    assert records.write_record(tmp_path, day, run_id, lambda: {}) is None
    assert list(tmp_path.iterdir()) == []


# ---- display only: nothing on the money path reads the records ----

MONEY_PATH = [*sorted((REPO / "tools").glob("*.py")),
              *(REPO / "pipeline" / n for n in ("order_flow.py", "orders.py", "send.py",
                                                "budget.py", "events.py", "ledger.py")),
              REPO / "journal" / "log.py", REPO / "journal" / "orders.py"]
FORBIDDEN_MODULES = ("pipeline.records", "pipeline.decide")


RECORDS_PATH = re.compile(r"(^|[/\\])records([/\\]|$)")


def money_path_hits(source: str) -> list[str]:
    """Every way `source` could reach the decision records: an import of pipeline.records or
    pipeline.decide (any form, a dynamic import by its literal name included), a path with a
    `records` segment, or the records module's names."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            full = [f"{node.module}.{a.name}" for a in node.names] + [node.module or ""]
            hits += [m for m in full if m in FORBIDDEN_MODULES]
        elif isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name in FORBIDDEN_MODULES]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                RECORDS_PATH.search(node.value) or node.value in FORBIDDEN_MODULES):
            hits.append(repr(node.value))
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in ("records_dir", "build_record", "write_record"):
                hits.append(name)
    return hits


def test_no_money_path_module_reads_the_records():
    """So model text can never reach sizing, the risk check or an order (the journal's own
    rule: it holds no analyst or trader text)."""
    found = {str(f.relative_to(REPO)): hits for f in MONEY_PATH
             if (hits := money_path_hits(f.read_text(encoding="utf-8")))}
    assert len(MONEY_PATH) > 10 and found == {}


def test_the_records_module_only_writes():
    tree = ast.parse((REPO / "pipeline" / "records.py").read_text(encoding="utf-8"))
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & {"read_text", "read_bytes", "open", "load", "loads", "glob", "rglob",
                        "iterdir", "scandir", "listdir"}


def test_the_records_folder_is_git_ignored():
    text = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "journal/records/" in text.splitlines()


def test_the_money_path_checker_catches_each_form():
    """The read check is not vacuous: each way in is caught."""
    for src in ("from pipeline import records", "import pipeline.records",
                "from pipeline.records import write_record",
                "import importlib; importlib.import_module('pipeline.records')",
                "p = journal_dir / 'records' / day", "x = 'journal/records/2026-11-16'",
                "x = records_dir(p)", "from pipeline import decide"):
        assert money_path_hits(src), src
    assert money_path_hits("x = record_count + 1; y = 'record'") == []
