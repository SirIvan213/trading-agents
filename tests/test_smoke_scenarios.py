"""smoke_debate --scenario. Offline: make_client is a tripwire, run_analysts and asyncio are
stubbed, the client is a fake. Nothing is billed, no key is read, no socket is opened."""
from types import SimpleNamespace

import pytest

import pipeline.order_flow as of
from agents.schemas import AgentRunRecord, AnalystReport, DecisionPacket, Evidence
from journal import log as journal_log
from pipeline import smoke_debate as S
from pipeline.ledger import PlanLedger
from pipeline.scenarios import ScenarioData, load_scenario, scenario_names
from tests.debate_helpers import bear_json, bull_json, reply
from tests.fakes import FakeClient
from tests.order_helpers import plan
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tools.config import CONFIG_DIR, load_limits

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
# The level plan (entry = last close, level = VWAP, 2R) each scenario's real tools produce.
PLANS = {
    "clean-buy": dict(symbol="BRKO", entry="80.42", stop="80.26", target="80.74",
                      risk_per_share="0.16", reward_to_risk="2.00"),
    "red-flag": dict(symbol="VNTR", entry="92.42", stop="92.27", target="92.72",
                     risk_per_share="0.15", reward_to_risk="2.00"),
    "tsla": dict(symbol="TSLA"),
}
SHARES = {"clean-buy": 37, "red-flag": 32, "tsla": 8}


def tripwire(*a, **k):
    pytest.fail("make_client / run_analysts / a billed call was reached")


def packet_for(symbol: str, plans, as_of: str) -> DecisionPacket:
    agents = ("technical", "sentiment", "fundamentals")
    reports = {a: AnalystReport(
        agent=a, symbol=symbol, stance="bullish", confidence=0.6,
        evidence=[Evidence(point="breakout above the opening range", source="get_bars")])
        for a in agents}
    runs = {a: AgentRunRecord(agent=a, requested_model="m", effort="low", ok=True, duration_ms=1)
            for a in agents}
    return DecisionPacket(symbol=symbol, as_of=as_of, reports=reports, runs=runs, all_ok=True,
                          exit_plans=plans)


# ---------------- unbilled (no client): a dry plan, exit 3 ----------------

@pytest.fixture
def unbilled(monkeypatch):
    monkeypatch.setattr(S, "make_client", tripwire)
    monkeypatch.setattr(S, "run_analysts", tripwire)
    monkeypatch.setattr(S, "run_debate", tripwire)
    monkeypatch.setattr(S, "run_trader", tripwire)


@pytest.mark.parametrize("name,symbol,expected", [
    ("clean-buy", "BRKO", "expected: BUY"),
    ("red-flag", "VNTR", "expected: PASS"),
    ("tsla", "TSLA", "expected: none (baseline)"),
])
def test_unbilled_run_names_the_scenario_and_exits_3(unbilled, capsys, name, symbol, expected):
    assert S.main(["--no-dotenv", "--scenario", name]) == 3
    out = capsys.readouterr().out
    assert f"scenario {name}: symbol {symbol}" in out
    assert expected in out
    assert "NOTHING was sent" in out and "Re-run with --billed" in out


def test_unbilled_default_scenario_is_tsla(unbilled, capsys):
    assert S.main(["--no-dotenv"]) == 3
    assert "scenario tsla: symbol TSLA" in capsys.readouterr().out


def test_unknown_scenario_is_an_argparse_error(unbilled, capsys):
    with pytest.raises(SystemExit) as exc:
        S.main(["--no-dotenv", "--scenario", "nope"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err and "clean-buy" in err


def test_scenario_choices_are_the_saved_scenarios():
    assert scenario_names() == ["clean-buy", "red-flag", "tsla"]
    assert S.DEFAULT_SCENARIO == "tsla"


# ---------------- injected fake client: the Expected vs actual section ----------------

@pytest.fixture
def smoke(monkeypatch, tmp_path):
    """Stub the analysts (one scenario-matching plan recorded then sealed), asyncio and the
    journal paths. Returns a function run(name, replies) -> (exit code, stdout, client, rec)."""
    real = journal_log.DEFAULT_PATH
    before = (real.exists(), real.stat().st_size if real.exists() else None)
    fake_default = tmp_path / "must_not_be_written" / "decisions.jsonl"
    monkeypatch.setattr(journal_log, "DEFAULT_PATH", fake_default)
    journals: list = []

    class SpyJournal(journal_log.Journal):
        def __init__(self, path=None):
            super().__init__(path)
            journals.append(self.path)

    monkeypatch.setattr(S, "Journal", SpyJournal)
    monkeypatch.setattr(S, "asyncio", SimpleNamespace(run=lambda value: value))
    monkeypatch.setattr(S, "make_client", tripwire)
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)
    monkeypatch.setenv("TRADING_AGENTS_DEBATE_ROUNDS", "1")

    def run(name, replies, capsys):
        data = ScenarioData(load_scenario(name))
        now, symbol = data.as_of(), data.symbol
        seen = []

        def fake_analysts(sym, at, **k):
            seen.append((sym, at, k["data"]))
            ledger: PlanLedger = k["ledger"]
            p = plan(as_of=now.isoformat(), **PLANS[name])
            ledger.record(p)
            ledger.seal(accept=True)
            return packet_for(symbol, [p], now.isoformat())

        monkeypatch.setattr(S, "run_analysts", fake_analysts)
        client = FakeClient(replies)
        code = S.main(["--no-dotenv", "--scenario", name], client=client)
        out = capsys.readouterr().out
        # the real journal file was never created or touched
        assert not fake_default.exists() and not fake_default.parent.exists()
        assert real not in journals
        assert (real.exists(), real.stat().st_size if real.exists() else None) == before
        return code, out, client, seen

    return run


def trader(symbol, **over):
    d = {"symbol": symbol, "action": "BUY", "plan_id": "plan-1", "confidence": 0.8,
         "reasoning": "technical is bullish; the bear's objections are met"}
    d.update(over)
    return d


def debaters(sym, fatal=False):
    return [reply(bull_json(plan_id="plan-1", symbol=sym)),
            reply(bear_json(plan_id="plan-1", symbol=sym, fatal=fatal))]


def tail(out: str, name: str) -> tuple[str, str]:
    """The header and verdict line of the final Expected vs actual section."""
    lines = out.rstrip("\n").splitlines()
    assert lines[-2] == f"-- Expected vs actual ({name}) --", lines[-3:]
    return lines[-2], lines[-1]


def test_clean_buy_with_a_trader_buy_matches(smoke, capsys):
    sym = "BRKO"
    replies = [*debaters(sym), reply(trader(sym))]
    code, out, client, seen = smoke("clean-buy", replies, capsys)
    assert code == 0
    _, line = tail(out, "clean-buy")
    assert line.startswith("expected BUY | actual BUY (fatal Bear: no)")
    assert line.endswith("-> MATCH")
    assert "Buy 37 shares of BRKO" in out and "80.26" in out and "80.74" in out
    assert seen[0][0] == "BRKO" and isinstance(seen[0][2], ScenarioData)
    assert seen[0][1].isoformat() == "2026-11-03T11:00:00-05:00"
    assert len(client.calls) == 3


def test_red_flag_with_a_fatal_bear_matches(smoke, capsys):
    sym = "VNTR"
    replies = [*debaters(sym, fatal=True)]
    code, out, client, _ = smoke("red-flag", replies, capsys)
    assert code == 0
    _, line = tail(out, "red-flag")
    assert line == ("expected PASS (ideally fatal Bear: yes) | actual PASS (fatal Bear: yes)"
                    " -> MATCH")
    assert "no ticket" in out and "ORDER TICKET" not in out
    assert len(client.calls) == 2  # the Trader never ran


def test_red_flag_pass_without_a_fatal_bear_matches_with_a_note(smoke, capsys):
    sym = "VNTR"
    replies = [*debaters(sym),
               reply(trader(sym, action="PASS", plan_id="", confidence=0.3))]
    code, out, _, _ = smoke("red-flag", replies, capsys)
    assert code == 0
    assert tail(out, "red-flag")[1].endswith("-> MATCH (decision matches; fatal Bear differs)")


def test_clean_buy_mismatch_when_the_trader_passes(smoke, capsys):
    sym = "BRKO"
    replies = [*debaters(sym),
               reply(trader(sym, action="PASS", plan_id="", confidence=0.3))]
    code, out, _, _ = smoke("clean-buy", replies, capsys)
    assert code == 0  # a MISMATCH is a label, not a failure of the run
    _, line = tail(out, "clean-buy")
    assert line == "expected BUY | actual PASS (fatal Bear: no) -> MISMATCH"
    assert "ORDER TICKET" not in out


def test_red_flag_mismatch_when_the_trader_buys(smoke, capsys):
    sym = "VNTR"
    replies = [*debaters(sym), reply(trader(sym))]
    code, out, _, _ = smoke("red-flag", replies, capsys)
    assert code == 0
    _, line = tail(out, "red-flag")
    assert line.endswith("actual BUY (fatal Bear: no) -> MISMATCH")
    assert "Buy 32 shares of VNTR" in out  # the ticket is only a ticket; nothing is sent


def test_tsla_baseline_has_no_verdict(smoke, capsys):
    sym = "TSLA"
    replies = [*debaters(sym), reply(trader(sym))]
    code, out, _, _ = smoke("tsla", replies, capsys)
    assert code == 0
    _, line = tail(out, "tsla")
    assert line == "expected: none (baseline) | actual BUY (fatal Bear: no)"
    assert "Buy 8 shares of TSLA" in out
