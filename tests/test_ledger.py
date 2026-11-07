"""PlanLedger (issue #64): the only source of plans the order path will size. Offline."""
import asyncio
import threading

import pytest

import agents.orchestrator as orch
import agents.tool_registry as reg
from agents.schemas import ExitPlanRecord
from agents.tool_registry import ToolSession
from pipeline.ledger import LedgerError, PlanLedger
from tests.fakes import NOON, FakeAllData, FakeClient, FakeMarketData, response, text
from tests.fakes import NOW as NOW_AAPL
from tests.order_helpers import plan, sealed_ledger
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tests.test_phase4_gate import abcd_plan_queue, abcd_report, other_agent_script
from tools.config import load_limits
from tools.scan import ScanCriteria


def led() -> PlanLedger:
    return PlanLedger("TSLA")


# ---------------- record / seal / get ----------------

def test_record_seal_get_roundtrip():
    ledger = led()
    p = plan()
    ledger.record(p)
    ledger.seal(accept=True)
    assert ledger.sealed is True
    assert ledger.get("plan-1") == p
    assert ledger.plan_ids() == ["plan-1"]


def test_symbol_is_normalised_and_run_id_is_random():
    a, b = PlanLedger(" tsla "), PlanLedger("TSLA")
    assert a.symbol == "TSLA"
    assert a.run_id != b.run_id and len(a.run_id) == 16


def test_plan_ids_keeps_recording_order():
    ledger = sealed_ledger([plan("plan-2"), plan("plan-1"), plan("plan-3")])
    assert ledger.plan_ids() == ["plan-2", "plan-1", "plan-3"]


def test_get_returns_a_fresh_copy_each_time():
    ledger = sealed_ledger([plan()])
    a, b = ledger.get("plan-1"), ledger.get("plan-1")
    assert a == b and a is not b


def test_mutating_the_original_after_record_does_not_change_the_ledger():
    p = plan()
    ledger = led()
    ledger.record(p)
    ledger.seal(accept=True)
    # records are frozen; force the mutation anyway, as a forger with object.__setattr__ would
    object.__setattr__(p, "stop", "1.00")
    object.__setattr__(p, "entry", "0.01")
    assert ledger.get("plan-1").stop == "358.32" and ledger.get("plan-1").entry == "364.18"


def test_mutating_a_returned_copy_does_not_change_the_ledger():
    ledger = sealed_ledger([plan()])
    got = ledger.get("plan-1")
    object.__setattr__(got, "target", "999.99")
    object.__setattr__(got, "stop", "1.00")
    again = ledger.get("plan-1")
    assert again.target == "370.04" and again.stop == "358.32"


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_model_construct_record_is_validated_on_the_way_in():
    forged = ExitPlanRecord.model_construct(**{**plan().model_dump(), "stop": 1.0})
    with pytest.raises(ValueError):  # a non-string stop fails validation; nothing stored
        led().record(forged)


# ---------------- refusals ----------------

def test_unsealed_ledger_gives_nothing():
    ledger = led()
    ledger.record(plan())
    assert ledger.sealed is False and ledger.plan_ids() == []
    with pytest.raises(KeyError, match="unknown plan_id"):
        ledger.get("plan-1")


def test_sealed_not_accepted_is_emptied():
    ledger = led()
    ledger.record(plan())
    ledger.seal(accept=False)
    assert ledger.sealed is True and ledger.plan_ids() == []
    with pytest.raises(KeyError, match="unknown plan_id"):
        ledger.get("plan-1")


def test_record_after_seal_is_refused_accepted_or_not():
    for accept in (True, False):
        ledger = led()
        ledger.seal(accept=accept)
        with pytest.raises(LedgerError, match="sealed"):
            ledger.record(plan())
        assert ledger.plan_ids() == []


def test_duplicate_id_is_refused_and_first_plan_kept():
    ledger = led()
    ledger.record(plan())
    with pytest.raises(LedgerError, match="already recorded"):
        ledger.record(plan(entry="365.00", stop="359.00", risk_per_share="6.00"))
    ledger.seal(accept=True)
    assert ledger.get("plan-1").entry == "364.18"


def test_another_symbol_is_refused():
    with pytest.raises(LedgerError, match="symbol"):
        led().record(plan(symbol="AAPL"))


def test_sealing_twice_keeps_the_first_outcome():
    ledger = led()
    ledger.record(plan())
    ledger.seal(accept=True)
    ledger.seal(accept=False)  # must not empty an accepted ledger
    assert ledger.plan_ids() == ["plan-1"]
    other = led()
    other.record(plan())
    other.seal(accept=False)
    other.seal(accept=True)  # must not resurrect an emptied ledger
    assert other.plan_ids() == []


@pytest.mark.parametrize("bad", [None, 1, b"plan-1", ["plan-1"], "", "PLAN-1", "plan-1 "])
def test_get_unknown_or_non_string_id(bad):
    with pytest.raises(KeyError, match="unknown plan_id"):
        sealed_ledger().get(bad)  # type: ignore[arg-type]


def test_plan_ids_is_a_copy():
    ledger = sealed_ledger()
    ledger.plan_ids().append("plan-9")
    assert ledger.plan_ids() == ["plan-1"]


# ---------------- thread safety ----------------

def test_concurrent_records_all_land_once():
    ledger = led()
    errors: list[Exception] = []
    start = threading.Barrier(8)

    def worker(n: int) -> None:
        start.wait()
        for i in range(25):
            try:
                ledger.record(plan(f"plan-{n}-{i}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    ledger.seal(accept=True)
    assert errors == [] and len(ledger.plan_ids()) == 200


def test_concurrent_duplicate_id_is_recorded_exactly_once():
    ledger = led()
    results: list[str] = []
    start = threading.Barrier(8)

    def worker() -> None:
        start.wait()
        try:
            ledger.record(plan("plan-1"))
            results.append("ok")
        except LedgerError:
            results.append("dup")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == ["dup"] * 7 + ["ok"]


def test_records_racing_a_seal_are_either_kept_or_refused_never_lost_silently():
    ledger = led()
    outcomes: list[bool] = []
    start = threading.Barrier(5)

    def worker(n: int) -> None:
        start.wait()
        for i in range(50):
            try:
                ledger.record(plan(f"plan-{n}-{i}"))
                outcomes.append(True)
            except LedgerError:
                outcomes.append(False)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    start.wait()
    ledger.seal(accept=True)
    for t in threads:
        t.join(timeout=10)
    assert len(ledger.plan_ids()) == sum(outcomes)  # every accepted record is in the ledger


# ---------------- the plan_exit executor records into the ledger ----------------

def session(ledger) -> ToolSession:
    s = ToolSession(symbol="AAPL", now=NOW_AAPL, data=FakeMarketData(), limits=load_limits(),
                    criteria=ScanCriteria(), ledger=ledger)
    _, err = s.execute("get_bars", {"symbol": "AAPL"})
    assert err is False
    return s


def test_plan_exit_records_the_same_record_it_appends():
    ledger = PlanLedger("AAPL")
    s = session(ledger)
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "103.10"})
    assert err is False and res["plan_id"] == "plan-1"
    ledger.seal(accept=True)
    assert s.exit_plans == [ledger.get("plan-1")]
    assert ledger.plan_ids() == [p.plan_id for p in s.exit_plans]


def test_plan_exit_refused_or_not_ok_records_nothing():
    ledger = PlanLedger("AAPL")
    s = session(ledger)
    s.execute("plan_exit", {"setup": "orb", "entry": "102.50"})  # not a price level
    s.execute("plan_exit", {"setup": "orb", "entry": "99.70"})  # ok False: entry is not last_close
    ledger.seal(accept=True)
    assert ledger.plan_ids() == [] and s.exit_plans == []


def test_plan_exit_without_a_ledger_still_works():
    s = session(None)
    res, _ = s.execute("plan_exit", {"setup": "orb", "entry": "103.10"})
    assert res["ok"] is True and len(s.exit_plans) == 1


def test_plan_exit_after_seal_is_an_error_and_appends_nothing():
    """A late tool call from a timed-out worker after the seal: refused, nothing leaks."""
    ledger = PlanLedger("AAPL")
    s = session(ledger)
    ledger.seal(accept=True)
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "103.10"})
    assert err is True and "error" in res
    assert s.exit_plans == [] and ledger.plan_ids() == []


def test_plan_exit_for_another_symbol_ledger_is_an_error():
    ledger = PlanLedger("TSLA")
    s = session(ledger)
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "103.10"})
    assert err is True and "error" in res
    assert s.exit_plans == []


# ---------------- run_analysts seals the ledger ----------------

def clients_for(technical_queue):
    return {
        "technical": FakeClient(technical_queue),
        "sentiment": FakeClient(other_agent_script("sentiment")),
        "fundamentals": FakeClient(other_agent_script("fundamentals")),
    }


def run(ledger, technical_queue, timeout_s=5):
    return asyncio.run(orch.run_analysts(
        "ABCD", NOON, data=FakeAllData(), clients=clients_for(technical_queue),
        timeout_s=timeout_s, ledger=ledger))


def test_run_analysts_seals_and_accepts_when_technical_ok():
    ledger = PlanLedger("ABCD")
    packet = run(ledger, abcd_plan_queue(abcd_report()))
    assert ledger.sealed and ledger.plan_ids() == ["plan-1"]
    assert [p.plan_id for p in packet.exit_plans] == ["plan-1"]
    assert ledger.get("plan-1") == packet.exit_plans[0]


def test_run_analysts_seals_empty_when_technical_not_ok():
    bad = response([text("not json")], "end_turn")  # not parseable as a report
    ledger = PlanLedger("ABCD")
    packet = run(ledger, abcd_plan_queue(bad, bad))
    assert packet.runs["technical"].ok is False and packet.exit_plans == []
    assert ledger.sealed and ledger.plan_ids() == []
    with pytest.raises(KeyError):
        ledger.get("plan-1")  # the plan WAS made by the tool, but the technical run failed


def test_timed_out_technical_seals_without_accepting_and_late_record_is_refused(monkeypatch):
    release = threading.Event()
    seen: dict = {}

    def slow(symbol, now, data, *, client, config, calls_sink, cancel=None, limits=None,
             ledger=None):
        release.wait(timeout=10)
        try:  # the worker wakes up after the orchestrator sealed the ledger
            ledger.record(plan("plan-1", symbol="ABCD"))
        except LedgerError as exc:
            seen["late"] = str(exc)
        raise AssertionError("discarded")

    monkeypatch.setitem(orch._RUNNERS, "technical", slow)
    ledger = PlanLedger("ABCD")
    try:
        packet = run(ledger, [], timeout_s=0.3)
        assert packet.runs["technical"].reason == "timeout"
        assert ledger.sealed and ledger.plan_ids() == []
        release.set()
        for _ in range(100):
            if "late" in seen:
                break
            threading.Event().wait(0.05)
    finally:
        release.set()
    assert "sealed" in seen.get("late", "")
    assert ledger.plan_ids() == []
    with pytest.raises(KeyError):
        ledger.get("plan-1")


def test_timeout_after_a_plan_was_recorded_still_empties_the_ledger(monkeypatch):
    """The plan was recorded (real tool) but the technical run timed out: not accepted."""
    release = threading.Event()

    def slow(symbol, now, data, *, client, config, calls_sink, cancel=None, limits=None,
             ledger=None):
        ledger.record(plan("plan-1", symbol="ABCD"))
        release.wait(timeout=10)
        raise AssertionError("discarded")

    monkeypatch.setitem(orch._RUNNERS, "technical", slow)
    ledger = PlanLedger("ABCD")
    try:
        packet = run(ledger, [], timeout_s=0.3)
    finally:
        release.set()
    assert packet.runs["technical"].ok is False and packet.exit_plans == []
    assert ledger.sealed and ledger.plan_ids() == []
    with pytest.raises(KeyError):
        ledger.get("plan-1")


def test_run_analysts_refuses_a_ledger_for_another_symbol():
    with pytest.raises(ValueError, match="ledger must be an unsealed PlanLedger"):
        run(PlanLedger("TSLA"), abcd_plan_queue(abcd_report()))


def test_run_analysts_refuses_an_already_sealed_ledger():
    ledger = PlanLedger("ABCD")
    ledger.seal(accept=True)
    with pytest.raises(ValueError, match="ledger must be an unsealed PlanLedger"):
        run(ledger, abcd_plan_queue(abcd_report()))


def test_refused_ledger_means_no_analyst_started():
    clients = clients_for(abcd_plan_queue(abcd_report()))
    with pytest.raises(ValueError):
        asyncio.run(orch.run_analysts(
            "ABCD", NOON, data=FakeAllData(), clients=clients, ledger=PlanLedger("TSLA")))
    assert all(c.calls == [] for c in clients.values())


def test_run_analysts_without_a_ledger_is_unchanged():
    packet = run(None, abcd_plan_queue(abcd_report()))
    assert [p.plan_id for p in packet.exit_plans] == ["plan-1"]


def test_registry_module_records_via_the_session_ledger_only(monkeypatch):
    """plan_exit has no way to take a ledger from the model: tool input keys are fixed."""
    s = session(PlanLedger("AAPL"))
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "103.10", "ledger": "x"})
    assert err is True and "error" in res
    assert reg.PLAN_EXIT_KEYS and "ledger" not in reg.PLAN_EXIT_KEYS
