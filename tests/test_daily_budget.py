"""The daily-budget batch: token totals with usage.iterations (#58), no debate after a failed
analyst (#163), no decision spent on a symbol that is not proven to report quarterly (#170),
and the analyst's scan limited to the session symbol plus a few peers (#43; its tool tests are
in tests/test_tool_registry.py). Offline: SDK types built locally, fakes, no network."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from anthropic.types.beta import BetaMessage

import pipeline.decide as decide
from agents.debate import run_debate
from agents.llm import AgentConfig
from agents.runner import call_record
from agents.schemas import CallRecord, IterationRecord, UsageRecord
from agents.trader import run_trader
from pipeline.events import reporting_check
from pipeline.smoke_technical import token_totals
from tests.debate_helpers import bear_json, bull_json, reply
from tests.fakes import FakeClient
from tests.order_helpers import submissions_202
from tests.test_issue87 import DEBATED, PKT
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tests.test_phase5_gate_d import run_scenario
from tools.config import load_limits
from tools.event_gate import COVERAGE_DAYS, reporting_coverage
from tools.scan import ScanCriteria, scan

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 3, 10, 30, tzinfo=NY)


# ======================= #58: token totals with usage.iterations =======================

def sdk_response(*, iterations=True, top=None, extra_iterations=()):
    """A real SDK object (anthropic BetaMessage) for a server-side fallback: Opus 5.5 declined
    after 900 in / 50 out (and a 300-token cache write); Opus 4.8 served the message. The
    top-level usage covers ONLY the serving attempt, as the API documents."""
    top = top or {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 500}
    usage = dict(top)
    if iterations:
        usage["iterations"] = [
            {"type": "message", "input_tokens": 900, "output_tokens": 50,
             "cache_creation_input_tokens": 300, "cache_read_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1000,
             "output_tokens": 200, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 500},
            *extra_iterations,
        ]
    return BetaMessage.model_validate({
        "id": "msg_fixture", "type": "message", "role": "assistant",
        "model": "claude-opus-4-8", "stop_reason": "end_turn", "stop_sequence": None,
        "content": [
            {"type": "fallback", "from": {"model": "claude-opus-5-5"},
             "to": {"model": "claude-opus-4-8"},
             "trigger": {"type": "refusal", "category": "cyber"}},
            {"type": "text", "text": "{}"},
        ],
        "usage": usage,
    })


def test_58_a_fallback_response_counts_the_declined_hop():
    rec = call_record(sdk_response(), "claude-opus-5-5")
    assert rec.fallback_ran and rec.fallback_from == "claude-opus-5-5"
    assert rec.usage.input_tokens == 1000  # top level: the serving attempt only
    billed = rec.billed_usage()
    assert billed == UsageRecord(input_tokens=1900, output_tokens=250,
                                 cache_creation_input_tokens=300, cache_read_input_tokens=500)
    assert token_totals([rec]) == {"input_tokens": 1900, "output_tokens": 250,
                                   "cache_creation_input_tokens": 300,
                                   "cache_read_input_tokens": 500}
    assert sum(token_totals([rec]).values()) == 2950  # what the daily token cap counts


def test_58_without_iterations_the_top_level_usage_is_the_total():
    rec = call_record(sdk_response(iterations=False), "claude-opus-4-8")
    assert rec.iterations == [] and rec.billed_usage() == rec.usage
    assert sum(token_totals([rec]).values()) == 1700


def test_58_every_iteration_type_is_counted():
    compaction = {"type": "compaction", "input_tokens": 4000, "output_tokens": 600,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    rec = call_record(sdk_response(extra_iterations=[compaction]), "claude-opus-5-5")
    assert rec.billed_usage().input_tokens == 5900
    assert rec.billed_usage().output_tokens == 850


def test_58_blocked_iterations_that_under_report_never_count_less_than_the_top_level():
    """A response whose iterations sum below its top-level usage counts the top level:
    the cap errs toward counting more."""
    rec = CallRecord(
        requested_model="m", serving_model="m", fallback_ran=False,
        usage=UsageRecord(input_tokens=500, output_tokens=100),
        iterations=[IterationRecord(type="message", usage=UsageRecord(input_tokens=200,
                                                                      output_tokens=300))],
    )
    assert rec.billed_usage() == UsageRecord(input_tokens=500, output_tokens=300)


def test_58_totals_add_up_across_calls():
    a = call_record(sdk_response(), "claude-opus-5-5")
    b = call_record(sdk_response(iterations=False), "claude-opus-4-8")
    assert sum(token_totals([a, b]).values()) == 2950 + 1700


# ======================= #163: no debate after a failed analyst =======================

def test_163_a_failed_analyst_skips_the_debate_with_no_call():
    client = FakeClient([])  # any call would fail: nothing is scripted
    assert PKT.all_ok is False
    rec = run_debate(PKT, clients=client, configs={"bull": AgentConfig(), "bear": AgentConfig()},
                     rounds=2)
    assert client.calls == []
    assert rec.skipped and "analyst_failed" in rec.skip_reason
    assert rec.turns == [] and rec.rounds_run == 0 and rec.rounds_planned == 2
    assert rec.final_bull is None and rec.final_bear is None
    assert rec.all_ok is False and rec.fatal is False and rec.stopped_early is False
    assert rec.symbol == PKT.symbol and rec.as_of == PKT.as_of
    # the record keeps its shape: it round-trips through JSON like any debate
    assert type(rec).model_validate_json(rec.model_dump_json()) == rec


def test_163_the_trader_then_passes_without_a_call():
    rec = run_debate(PKT, clients=FakeClient([]), rounds=1,
                     configs={"bull": AgentConfig(), "bear": AgentConfig()})
    trader_client = FakeClient([])
    run = run_trader(PKT, rec, client=trader_client, config=AgentConfig())
    assert run.decision.action == "PASS" and run.forced_pass == "analyst_failed"
    assert trader_client.calls == []


def test_163_the_smoke_report_shows_a_skipped_debate_as_skipped(capsys):
    from pipeline import smoke_debate as S
    from tests.test_issue87 import run_record
    reports = {a: PKT.reports["technical"].model_copy(update={"agent": a})
               for a in ("technical", "sentiment", "fundamentals")}
    runs = {a: run_record(a, ok=(a != "fundamentals")) for a in reports}
    pkt = PKT.model_copy(update={"reports": reports, "runs": runs, "all_ok": False})
    rec = run_debate(pkt, clients=FakeClient([]), rounds=2,
                     configs={"bull": AgentConfig(), "bear": AgentConfig()})
    cfgs = {a: AgentConfig() for a in (*reports, "bull", "bear")}
    S.report(pkt, rec, cfgs)
    out = capsys.readouterr().out.split("-- Debate --")[1]
    assert "SKIPPED (no Bull or Bear call): analyst_failed" in out
    assert "rounds planned 2, run 0 (debate skipped)" in out
    assert "stopped early: n/a, the debate was skipped" in out
    assert "stopped early: no" not in out


@pytest.mark.parametrize("over", [
    {"all_ok": True}, {"fatal": True}, {"rounds_run": 1}, {"skip_reason": None},
    {"stopped_early": True},
], ids=["ok", "fatal", "ran", "no-reason", "stopped"])
def test_163_blocked_a_skipped_record_cannot_look_like_a_debate(over):
    from pydantic import ValidationError
    rec = run_debate(PKT, clients=FakeClient([]), rounds=1,
                     configs={"bull": AgentConfig(), "bear": AgentConfig()})
    with pytest.raises(ValidationError, match="skipped debate"):
        type(rec).model_validate({**rec.model_dump(), **over})


def test_163_blocked_a_healthy_packet_is_still_debated():
    client = FakeClient([reply(bull_json()), reply(bear_json())])
    rec = run_debate(DEBATED, clients=client, rounds=1,
                     configs={"bull": AgentConfig(), "bear": AgentConfig()})
    assert not rec.skipped and rec.skip_reason is None
    assert len(client.calls) == 2 and [t.side for t in rec.turns] == ["bull", "bear"]


def test_163_a_bad_rounds_value_still_raises_before_the_skip():
    with pytest.raises(ValueError):
        run_debate(PKT, clients=FakeClient([]), rounds=0)


# ======================= #170: reporting coverage before a decision =======================

def foreign_filer(now: datetime, symbol: str = "OMH") -> dict:
    """A foreign private issuer: 6-K and 20-F, never an 8-K item 2.02."""
    stamp = (now - timedelta(days=20)).astimezone(ZoneInfo("UTC")).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    return {"cik": "0000099999", "name": f"{symbol} Holdings Ltd", "tickers": [symbol],
            "filings": {"recent": {
                "accessionNumber": ["a1", "a2"], "filingDate": [stamp[:10], stamp[:10]],
                "acceptanceDateTime": [stamp, stamp], "form": ["6-K", "20-F"],
                "items": ["", ""], "primaryDocument": ["6k.htm", "20f.htm"]}, "files": []}}


def test_170_a_recent_item_202_is_covered():
    out = reporting_coverage("TSLA", NOW, {"ok": True, "submissions": submissions_202(NOW, 30)})
    assert out["covered"] is True and out["reason"] is None
    assert out["last_item_202"] == (NOW - timedelta(days=30)).date().isoformat()


def test_170_an_item_202_filed_today_counts_as_reporting():
    """Reporting is proven; whether today's filing blocks (EVENT) stays the gate's call."""
    out = reporting_coverage("TSLA", NOW, {"ok": True,
                                           "submissions": submissions_202(NOW, 0.01)})
    assert out["covered"] is True


@pytest.mark.parametrize("filings,why", [
    ({"ok": True, "submissions": None}, "no SEC filer found"),
    ({"ok": False, "reason": "HTTP 503"}, "SEC filings unavailable: HTTP 503"),
    ({"ok": True, "submissions": {"filings": "garbage"}}, "malformed"),
    (None, "SEC filings unavailable"),
], ids=["no-filer", "sec-down", "malformed", "missing"])
def test_170_blocked_unproven_reporting_is_not_covered(filings, why):
    out = reporting_coverage("OMH", NOW, filings)
    assert out["covered"] is False and why in out["reason"]


def test_170_blocked_a_foreign_private_issuer_is_not_covered():
    out = reporting_coverage("OMH", NOW, {"ok": True, "submissions": foreign_filer(NOW)})
    assert out["covered"] is False
    assert f"no 8-K item 2.02 in the last {COVERAGE_DAYS} days" in out["reason"]


def test_170_blocked_an_item_202_older_than_100_days_is_not_covered():
    out = reporting_coverage("TSLA", NOW, {"ok": True,
                                           "submissions": submissions_202(NOW, 101)})
    assert out["covered"] is False


def test_170_blocked_an_8k_without_item_202_is_not_covered():
    out = reporting_coverage("TSLA", NOW, {"ok": True,
                                           "submissions": submissions_202(NOW, 10, "8.01")})
    assert out["covered"] is False


def test_170_an_item_202_stamped_later_today_than_now_counts_as_reporting():
    """The only 2.02 in 100 days is today's, stamped after `now` (an estimated 23:59:59 or a
    clock a little behind EDGAR): the gate's EVENT finding, so reporting is proven."""
    later_today = submissions_202(NOW, -0.01)  # 14 minutes after NOW, same New York day
    out = reporting_coverage("TSLA", NOW, {"ok": True, "submissions": later_today})
    assert out["covered"] is True and out["reason"] is None


def test_170_reporting_check_reads_the_data_source_and_never_raises():
    class Data:
        def submissions(self, symbol):
            return foreign_filer(NOW, symbol)

    class Down:
        def submissions(self, symbol):
            raise TimeoutError

    assert reporting_check(Data(), "OMH", NOW)["covered"] is False
    out = reporting_check(Down(), "OMH", NOW)
    assert out["covered"] is False and "SEC filings unavailable" in out["reason"]


def gap_snapshot(t: str = "2026-11-03T15:29:00Z") -> dict:
    return {"latestTrade": {"p": 25.0, "t": t},
            "latestQuote": {"ap": 25.01, "bp": 24.99, "t": t},
            "dailyBar": {"o": 25.0, "c": 25.0, "v": 2_000_000, "t": "2026-11-03T05:00:00Z"},
            "prevDailyBar": {"c": 23.0, "t": "2026-11-02T05:00:00Z"}}


LISTED = {s: {"ok": True, "symbol": s, "exchange": "NASDAQ"} for s in ("GAPA", "OMH", "TKC")}


def test_170_blocked_the_scan_rejects_a_symbol_without_reporting_with_its_reason():
    snaps = {"GAPA": gap_snapshot(), "OMH": gap_snapshot(), "TKC": gap_snapshot()}
    reporting = {"GAPA": {"covered": True, "reason": None},
                 "OMH": reporting_coverage("OMH", NOW, {"ok": True,
                                                        "submissions": foreign_filer(NOW)})}
    out = scan(snaps, ScanCriteria(), load_limits(), listings=LISTED, reporting=reporting)
    rejected = {r["symbol"]: r["reasons"] for r in out["rejected"]}
    assert "GAPA" not in rejected
    assert any(r.startswith("quarterly reporting not proven: no 8-K item 2.02")
               for r in rejected["OMH"])
    assert "quarterly reporting not proven: not checked" in rejected["TKC"]  # missing: rejected


@pytest.mark.parametrize("reporting", [
    ["GAPA"], "GAPA", 0, {"gapa": {"covered": True}}],
    ids=["list", "text", "zero", "lower-case-key"])
def test_170_blocked_a_malformed_reporting_map_fails_closed_never_raises(reporting):
    out = scan({"GAPA": gap_snapshot()}, ScanCriteria(), load_limits(), listings=LISTED,
               reporting=reporting)
    assert out["candidates"] == []
    (rej,) = out["rejected"]
    assert "quarterly reporting not proven: not checked" in rej["reasons"]


def test_170_without_reporting_the_scan_does_not_check_it():
    out = scan({"OMH": gap_snapshot()}, ScanCriteria(), load_limits(), listings=LISTED)
    reasons = [r for x in out["rejected"] for r in x["reasons"]]
    assert not any("quarterly reporting" in r for r in reasons)


def test_170_blocked_run_decision_skips_a_symbol_without_reporting_before_any_billed_call(
        monkeypatch, tmp_path):
    """The red-flag scenario (VNTR) has no 8-K item 2.02: no analyst, debate or Trader runs.
    The guard below stays installed (stub_analysts=False), so an analyst run would fail."""
    ran = []

    def no_analysts(*a, **k):
        ran.append(1)
        raise AssertionError("an analyst ran: a decision was spent")

    monkeypatch.setattr(decide, "run_analysts", no_analysts)
    with pytest.raises(decide.SymbolSkipped) as exc:
        run_scenario(monkeypatch, tmp_path, "red-flag", stub_analysts=False)
    assert decide.run_analysts is no_analysts and ran == []  # the guard was live, never hit
    assert exc.value.symbol == "VNTR"
    assert f"no 8-K item 2.02 in the last {COVERAGE_DAYS} days" in exc.value.reason
    assert isinstance(exc.value, ValueError)  # run_decision's "before any API call" contract
    (entry,) = [json.loads(x) for x in (tmp_path / "red-flag.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert entry["status"] == "skipped" and entry["symbol"] == "VNTR"
    assert "no 8-K item 2.02" in entry["reason"]


def test_170_blocked_a_skip_whose_journal_write_fails_is_still_a_skip(monkeypatch, tmp_path):
    """The skip's journal entry is best effort: a locked or cut-off journal never turns the
    skip into a decision, and never raises anything but SymbolSkipped."""
    from journal.log import Journal, JournalLockTimeout
    tries = []

    def locked(self, entry):
        tries.append(entry["status"])
        raise JournalLockTimeout("journal lock timed out")

    def no_analysts(*a, **k):
        raise AssertionError("an analyst ran: a decision was spent")

    monkeypatch.setattr(Journal, "append", locked)
    monkeypatch.setattr(decide, "run_analysts", no_analysts)
    with pytest.raises(decide.SymbolSkipped):
        run_scenario(monkeypatch, tmp_path, "red-flag", stub_analysts=False)
    assert tries == ["skipped", "skipped"]  # one retry, then the skip stands


def test_170_the_guard_would_catch_an_analyst_run(monkeypatch, tmp_path):
    """The guard has teeth: with the SEC check passed, the same scenario reaches it."""
    def no_analysts(*a, **k):
        raise AssertionError("an analyst ran: a decision was spent")

    monkeypatch.setattr(decide, "run_analysts", no_analysts)
    with pytest.raises(AssertionError, match="an analyst ran"):
        run_scenario(monkeypatch, tmp_path, "red-flag", stub_analysts=False,
                     bypass_prescreen=True)


def test_170_a_symbol_that_reports_is_decided_as_before(monkeypatch, tmp_path):
    result, journal, asked, client = run_scenario(monkeypatch, tmp_path, "clean-buy")
    assert result.proposal.ok and result.outcome.status == "approved_not_sent"
    assert "skipped" not in [e["status"] for e in journal.entries()]


def test_170_the_skip_never_changes_the_gate_rule():
    """The gate itself still calls a symbol without coverage UNKNOWN (unchanged)."""
    from tests.order_helpers import gate_for
    from tools.event_gate import UNKNOWN
    gate = gate_for("TSLA", NOW, filings={"ok": True, "submissions": submissions_202(NOW, 101)})
    assert gate["status"] == UNKNOWN and "no 8-K item 2.02" in gate["reason"]
