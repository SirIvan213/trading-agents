"""smoke_debate now runs the Trader and prints the order ticket. Offline: an injected fake client,
run_analysts and asyncio stubbed, so no key is read, no socket opened and nothing is billed."""
from types import SimpleNamespace

import pytest

import pipeline.order_flow as of
from agents.schemas import AgentRunRecord, AnalystReport, DecisionPacket, Evidence
from journal import log as journal_log
from pipeline import smoke_debate as S
from pipeline.ledger import PlanLedger
from tests.debate_helpers import bear_json, bull_json, reply
from tests.fakes import FakeClient, api_error
from tests.order_helpers import plan
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tools.config import CONFIG_DIR, load_limits

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
SYMBOL = "TSLA"
BULL = bull_json(plan_id="plan-1", symbol=SYMBOL)
BEAR = bear_json(plan_id="plan-1", symbol=SYMBOL)


def trader_json(**over):
    d = {"symbol": SYMBOL, "action": "BUY", "plan_id": "plan-1", "confidence": 0.8,
         "reasoning": "technical is bullish; the bear's objections are met"}
    d.update(over)
    return d


def make_packet(plans) -> DecisionPacket:
    agents = ("technical", "sentiment", "fundamentals")
    reports = {a: AnalystReport(
        agent=a, symbol=SYMBOL, stance="bullish", confidence=0.6,
        evidence=[Evidence(point="breakout above the opening range", source="get_bars")])
        for a in agents}
    runs = {a: AgentRunRecord(agent=a, requested_model="m", effort="low", ok=True, duration_ms=1)
            for a in agents}
    return DecisionPacket(symbol=SYMBOL, as_of="2026-11-03T11:00:00-05:00", reports=reports,
                          runs=runs, all_ok=True, exit_plans=plans)


@pytest.fixture
def smoke(monkeypatch, tmp_path):
    """Stub the analysts (they record one plan into the ledger main() passes, then seal it),
    asyncio, make_client and the real journal path. Returns a record of what happened."""
    now = S.FixtureData().as_of()
    rec = SimpleNamespace(now=now, journals=[], analyst_calls=[])
    real_default = journal_log.DEFAULT_PATH
    rec.real_default = real_default
    rec.real_before = (real_default.exists(),
                       real_default.stat().st_size if real_default.exists() else None)
    fake_default = tmp_path / "must_not_be_written" / "decisions.jsonl"
    monkeypatch.setattr(journal_log, "DEFAULT_PATH", fake_default)
    rec.fake_default = fake_default

    def fake_analysts(*a, **k):
        rec.analyst_calls.append(k)
        ledger = k["ledger"]
        p = plan(as_of=now.isoformat())
        ledger.record(p)
        ledger.seal(accept=True)
        return make_packet([p])

    class SpyJournal(journal_log.Journal):
        def __init__(self, path=None):
            super().__init__(path)
            rec.journals.append(self.path)

    monkeypatch.setattr(S, "run_analysts", fake_analysts)
    monkeypatch.setattr(S, "asyncio", SimpleNamespace(run=lambda value: value))
    monkeypatch.setattr(S, "Journal", SpyJournal)
    monkeypatch.setattr(S, "make_client", lambda *a, **k: pytest.fail("make_client called"))
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)
    monkeypatch.setenv("TRADING_AGENTS_DEBATE_ROUNDS", "1")
    return rec


def assert_real_journal_untouched(rec):
    assert not rec.fake_default.exists() and not rec.fake_default.parent.exists()
    assert rec.real_default not in rec.journals
    now_state = (rec.real_default.exists(),
                 rec.real_default.stat().st_size if rec.real_default.exists() else None)
    assert now_state == rec.real_before


def test_buy_prints_trader_section_and_ticket_and_never_writes_the_real_journal(smoke, capsys):
    client = FakeClient([reply(BULL), reply(BEAR), reply(trader_json())])
    assert S.main([], client=client) == 0
    out = capsys.readouterr().out
    assert "-- Trader --" in out and "[trader] ok" in out
    assert '"action": "BUY"' in out and '"plan_id": "plan-1"' in out
    assert "-- Order ticket (smoke: NO approval step, nothing journaled or sent) --" in out
    assert "the fixture's as_of" in out
    ticket = out.split("-- Order ticket")[1]
    for part in ("ORDER TICKET", "Buy 8 shares of TSLA", "358.32", "370.04", "46.88",
                 "NOT sent to a broker", "plan-1"):
        assert part in ticket, part
    assert "Type yes" not in out  # no approval step
    assert isinstance(smoke.analyst_calls[0]["ledger"], PlanLedger)
    assert len(client.calls) == 3  # bull, bear, trader: nothing else was called
    assert_real_journal_untouched(smoke)


def test_buy_ticket_uses_a_throwaway_journal(smoke, capsys):
    S.main([], client=FakeClient([reply(BULL), reply(BEAR), reply(trader_json())]))
    capsys.readouterr()
    assert len(smoke.journals) == 1
    assert "smoke.jsonl" in smoke.journals[0].name
    assert not smoke.journals[0].exists()  # propose() never writes, and the temp dir is gone


def test_ticket_is_sized_from_the_ledger_at_the_fixture_clock(smoke, capsys, monkeypatch):
    """A stale plan relative to the fixture clock prints NO ORDER, proving the clock is used."""
    def stale_analysts(*a, **k):
        ledger = k["ledger"]
        p = plan(as_of=(smoke.now.replace(hour=9, minute=0)).isoformat())  # two hours old
        ledger.record(p)
        ledger.seal(accept=True)
        return make_packet([p])

    monkeypatch.setattr(S, "run_analysts", stale_analysts)
    assert S.main([], client=FakeClient([reply(BULL), reply(BEAR), reply(trader_json())])) == 0
    out = capsys.readouterr().out
    assert "NO ORDER for plan plan-1" in out and "old" in out
    assert "Buy 8 shares" not in out
    assert_real_journal_untouched(smoke)


def test_pass_prints_no_ticket(smoke, capsys):
    client = FakeClient([reply(BULL), reply(BEAR),
                         reply(trader_json(action="PASS", plan_id="", confidence=0.3))])
    assert S.main([], client=client) == 0
    out = capsys.readouterr().out
    assert "-- Trader --" in out and "no ticket: the Trader did not choose BUY" in out
    assert "ORDER TICKET" not in out
    assert smoke.journals == []
    assert_real_journal_untouched(smoke)


def test_fatal_bear_is_a_legitimate_pass_not_a_failure(smoke, capsys):
    client = FakeClient([reply(BULL), reply(bear_json(fatal=True, plan_id="plan-1",
                                                      symbol=SYMBOL))])
    assert S.main([], client=client) == 0
    out = capsys.readouterr().out
    assert "FORCED PASS (fatal_bear)" in out and "no ticket" in out
    assert len(client.calls) == 2  # the Trader was never called
    assert_real_journal_untouched(smoke)


def test_forced_pass_failure_code_gives_exit_1(smoke, capsys):
    bad = reply(trader_json(plan_id="plan-99"))  # not a packet plan: invalid, retried, invalid
    client = FakeClient([reply(BULL), reply(BEAR), bad, bad])
    assert S.main([], client=client) == 1
    cap = capsys.readouterr()
    assert "FORCED PASS (invalid_output)" in cap.out and "no ticket" in cap.out
    assert "trader (invalid_output)" in cap.err and "smoke run FAILED" in cap.err
    assert "ORDER TICKET" not in cap.out
    assert_real_journal_untouched(smoke)


def test_trader_api_error_gives_exit_1(smoke, capsys):
    client = FakeClient([reply(BULL), reply(BEAR), api_error()])
    assert S.main([], client=client) == 1
    assert "trader (api_error)" in capsys.readouterr().err


def test_trader_failure_codes_exclude_the_legitimate_passes():
    assert S.TRADER_FAILURES == {"invalid_output", "api_error", "exception", "config_error",
                                 "cancelled"}
