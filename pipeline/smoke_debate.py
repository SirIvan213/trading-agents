"""Smoke run: the three analysts, the Bull/Bear debate and the Trader, once, on a saved
scenario, against the REAL Claude API. For a BUY it prints the order ticket, sized from the
plan ledger at the fixture's clock; there is NO approval step and nothing is journaled or sent.
At the end it prints the scenario's expected decision next to the actual one.

Not part of the test suite (it lives outside tests/). It makes real, billed Claude API calls;
it makes NO Alpaca or SEC calls. Every input is a fixture (synthetic data in the real API
shapes, for a run "as of" 2026-11-03 11:00 ET). --scenario NAME picks one of
tests/fixtures/scenarios/<NAME>/scenario.json (see pipeline/scenarios.py):

    tsla       the original TSLA fixtures; baseline, no expected decision (default)
    clean-buy  BRKO: orderly push off VWAP, tight stop, room for 2R, positive news -> BUY
    red-flag   VNTR: similar chart, a trading halt and earnings inside the trade -> PASS

Nothing is sent unless --billed is given (CLAUDE.md rule 6); without it the script prints
what it would run and exits 3. Every retried attempt's rejection reason is printed under its
agent's API calls (a debate money-check reason is count-only and never quotes the rejected
value; an analyst reason may name the field or catalyst the model got wrong). After the
report, a DEBUG section lists each money-check rejection's offending tokens, the pattern that
matched and an excerpt; it is printed only and never stored in the debate record. The API key
is read from ANTHROPIC_API_KEY, loaded from .env in the repo root (or --env-file PATH;
--no-dotenv skips it) if present (a non-empty exported variable wins). The key is never
printed. Without flags each agent uses its own configured model and effort (config_for:
TRADING_AGENTS_<AGENT>_MODEL / _EFFORT, then TRADING_AGENTS_MODEL / _EFFORT, then the
defaults); --model / --effort override all six agents (technical, sentiment, fundamentals,
bull, bear, trader). Debate rounds come from TRADING_AGENTS_DEBATE_ROUNDS (default 2). Risk limits
are loaded and validated as in a real run.

Exit codes: 0 = all three analysts and every debate turn produced validated output (a
server-side MODEL fallback, where another model answered, still counts as success and is
shown per call); 1 = an agent produced no validated output (a neutral analyst report, a
missing Bull case or a fail-closed Bear) or no API call succeeded; 2 = no API key,
unreadable or missing .env/--env-file, or invalid config/limits; 3 = --billed not given,
nothing was sent.

Usage:
    python -m pipeline.smoke_debate                          # dry plan only, nothing sent
    python -m pipeline.smoke_debate --billed
    python -m pipeline.smoke_debate --billed --scenario clean-buy
    python -m pipeline.smoke_debate --billed --scenario red-flag
    python -m pipeline.smoke_debate --billed --model claude-sonnet-5-5
    python -m pipeline.smoke_debate --billed --effort medium --no-fallbacks
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from agents.debate import resolve_rounds, run_debate
from agents.debate_common import MONEY_REASON, money_debug
from agents.llm import EFFORTS, AgentConfig, config_for, make_client
from agents.money_guard import ViolationDetail
from agents.orchestrator import AGENTS, load_checked_limits, run_analysts
from agents.schemas import CallRecord, DebateRecord, DecisionPacket, RetryRecord
from agents.trader import TraderRun, run_trader
from journal.log import Journal
from pipeline.ledger import PlanLedger
from pipeline.order_flow import Proposal, propose, render_ticket
from pipeline.scenarios import DEFAULT as DEFAULT_SCENARIO
from pipeline.scenarios import ScenarioData, compare, load_scenario, scenario_names
from pipeline.smoke_common import ROOT, add_safety_flags, key_hint, load_env, refuse_unbilled
from pipeline.smoke_technical import token_totals

DEBATERS = ("bull", "bear")
TRADER = "trader"
# Forced-PASS codes that mean the Trader itself failed (a fatal Bear or no plans is a
# legitimate PASS, not a failure).
TRADER_FAILURES = frozenset({"invalid_output", "api_error", "exception", "config_error",
                             "cancelled", "debate_mismatch"})


class FixtureData(ScenarioData):
    """The TSLA scenario's data (kept for callers of the pre-scenario API)."""

    def __init__(self) -> None:
        super().__init__(load_scenario(DEFAULT_SCENARIO))


def _configs(model: str | None, effort: str | None, fallbacks: bool) -> dict[str, AgentConfig]:
    """config_for(agent) for all six agents, with any command-line overrides applied."""
    out = {}
    for agent in (*AGENTS, *DEBATERS, TRADER):
        cfg = config_for(agent)
        changes: dict[str, Any] = {"fallbacks": fallbacks}
        if model:
            changes["model"] = model
        if effort:
            changes["effort"] = effort
        out[agent] = replace(cfg, **changes)
    return out


def _print_calls(label: str, calls: list[CallRecord]) -> None:
    for i, c in enumerate(calls, 1):
        line = f"  {label} call {i}: served by {c.serving_model}  stop_reason={c.stop_reason}"
        if c.fallback_ran:
            declined = f" (declined by {c.fallback_from})" if c.fallback_from else ""
            line += f"  fallback ran{declined}"
        if c.refusal_category:
            line += f"  refusal_category={c.refusal_category}"
        print(line)
        for it in c.iterations:  # per-hop usage; matters on fallback calls (see #58)
            print(f"     iteration {it.type} model={it.model} "
                  f"in={it.usage.input_tokens} out={it.usage.output_tokens}")


def _print_retries(label: str, retries: list[RetryRecord]) -> None:
    # Money-check reasons are count-only (never the rejected value); analyst reasons may
    # name a source or catalyst, as the final `reason` already does.
    for r in retries:
        print(f"  {label} RETRY after attempt {r.attempt}: {r.reason_code}: {r.reason}")


Rejection = tuple[str, list[tuple[str, ViolationDetail]]]  # (side, [(field, detail)])


def print_money_debug(rejections: list[Rejection]) -> None:
    """Console-only detail of every money-check rejection: token, pattern, excerpt.

    This is debugging output for this script alone. It is printed and dropped: it is never
    written into the DebateRecord, a retry reason, the model hint or anything the Trader reads.
    """
    print("\n-- Money-check rejections (DEBUG, console only, not stored) --")
    if not rejections:
        print("none")
        return
    seen: dict[str, int] = {}
    for side, items in rejections:
        seen[side] = seen.get(side, 0) + 1
        print(f"\n{side} rejected attempt #{seen[side]}: {len(items)} offending mention(s)")
        for field_name, d in items:
            note = "  (equals a plan value; rejected as a size)" if d.plan_value else ""
            print(f"  {field_name}: token {d.number!r} in {d.mention!r}{note}")
            print(f"    pattern: {d.pattern}")
            print(f"    excerpt: {d.excerpt}")


def report(packet: DecisionPacket, debate: DebateRecord, configs: dict[str, AgentConfig]) -> None:
    """Print the analysts, the debate, every call's serving model and the token totals."""
    print(f"== Analysts + debate smoke run: {packet.symbol} as of {packet.as_of} ==")
    for agent, cfg in configs.items():
        print(f"{agent:<12} requested {cfg.model}  effort {cfg.effort}  "
              f"fallbacks {'on' if cfg.fallbacks else 'off'}")

    print("\n-- Analyst reports --")
    for agent in AGENTS:
        run = packet.runs[agent]
        status = "ok" if run.ok else f"FELL BACK ({run.reason_code}: {run.reason})"
        print(f"\n[{agent}] {status}")
        print(packet.reports[agent].model_dump_json(indent=2))
    print(f"\nexit plans in packet: {len(packet.exit_plans)} "
          f"({', '.join(p.plan_id for p in packet.exit_plans) or 'none'})")

    print("\n-- Debate --")
    for turn in debate.turns:
        # run.reason never quotes money values (count-only by design), so it is safe to print.
        status = "ok" if turn.run.ok else f"FAILED ({turn.run.reason_code}: {turn.run.reason})"
        print(f"\n[round {turn.round}] {turn.side.upper()} {status}")
        print(turn.case.model_dump_json(indent=2))
    print(f"\nrounds planned {debate.rounds_planned}, run {debate.rounds_run}")
    last_bear = next((t for t in reversed(debate.turns) if t.side == "bear"), None)
    bear_failed = last_bear is not None and not last_bear.run.ok
    if debate.stopped_early and bear_failed:
        print("stopped early: YES, the Bear failed and the debate failed closed (counted as fatal)")
    elif debate.stopped_early:
        print("stopped early: YES, the Bear marked an objection fatal")
    else:
        print("stopped early: no")
    print(f"fatal objection: {'yes' if debate.fatal else 'no'}")
    print(f"same model: requested {debate.requested_same_model}, "
          f"requested and served {debate.same_model}")

    print("\n-- API calls (serving model per call) --")
    all_calls: list[CallRecord] = []
    all_retries: list[RetryRecord] = []
    for agent in AGENTS:
        run = packet.runs[agent]
        all_calls += run.calls
        all_retries += run.retries
        _print_calls(agent, run.calls)
        _print_retries(agent, run.retries)
    for turn in debate.turns:
        label = f"r{turn.round} {turn.side}"
        all_calls += turn.run.calls
        all_retries += turn.run.retries
        _print_calls(label, turn.run.calls)
        _print_retries(label, turn.run.retries)
    served = sorted({c.serving_model for c in all_calls})
    print(f"serving model(s): {', '.join(served) or 'none (no API response)'}")
    print(f"fallback ran: {'yes' if any(c.fallback_ran for c in all_calls) else 'no'}")
    print(f"retries: {len(all_retries)} "
          f"({sum(MONEY_REASON in r.reason for r in all_retries)} from the money check)")

    t = token_totals(all_calls)
    print(f"\n-- Tokens (sum over all {len(all_calls)} API calls) --")
    print(f"input {t['input_tokens']}  output {t['output_tokens']}  "
          f"cache write {t['cache_creation_input_tokens']}  "
          f"cache read {t['cache_read_input_tokens']}  total processed {sum(t.values())}")


def print_latency(stage_s: dict[str, float], max_age_s: int) -> None:
    """Real wall time per stage against the plan-age budget (issue #65). The ticket above
    uses the fixture clock; these timings are real."""
    total = sum(stage_s.values())
    parts = ", ".join(f"{k} {v:.1f}s" for k, v in stage_s.items())
    print("\n-- Latency vs plan age budget (real wall time) --")
    print(f"{parts}: {total:.1f}s of max_plan_age_s {max_age_s}s")
    left = max_age_s - total
    if left <= 0:
        print("OVER BUDGET: in a real run every plan would be stale before approval")
    else:
        print(f"left for human approval: {left:.0f}s")


def print_trader(trader: TraderRun, proposal: Proposal | None) -> None:
    """The Trader's decision, its API calls and the order ticket (no approval step)."""
    d = trader.decision
    print("\n-- Trader --")
    status = "ok" if trader.ok else f"FORCED PASS ({trader.forced_pass})"
    print(f"[trader] {status}  requested {trader.run.requested_model}  "
          f"effort {trader.run.effort}")
    print(d.model_dump_json(indent=2))
    _print_calls("trader", trader.run.calls)
    _print_retries("trader", trader.run.retries)
    t = token_totals(trader.run.calls)
    print(f"trader tokens: input {t['input_tokens']}  output {t['output_tokens']}  "
          f"cache write {t['cache_creation_input_tokens']}  "
          f"cache read {t['cache_read_input_tokens']}")
    print("\n-- Order ticket (smoke: NO approval step, nothing journaled or sent) --")
    if proposal is None:
        print("no ticket: the Trader did not choose BUY")
        return
    print("clock: the fixture's as_of (smoke only; a real run uses the wall clock)")
    print(render_ticket(proposal))


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the analysts, the Bull/Bear debate and the Trader once on a saved "
                    "scenario against the real Claude API (billed; no Alpaca or SEC calls)."
    )
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, choices=scenario_names(),
                        help="saved scenario under tests/fixtures/scenarios "
                             f"(default {DEFAULT_SCENARIO})")
    parser.add_argument("--model", default=None,
                        help="Claude model ID for all six agents (default: each agent's "
                             "configured model, normally claude-sonnet-5-5)")
    parser.add_argument("--effort", default=None, choices=sorted(EFFORTS),
                        help="effort for all six agents (default: each agent's configured effort)")
    parser.add_argument("--no-fallbacks", action="store_true",
                        help="turn off server-side refusal fallback")
    add_safety_flags(parser)
    args = parser.parse_args(argv)

    if client is None:  # .env first: it may hold the key and TRADING_AGENTS_* settings
        err = load_env(args, ROOT)
        if err is not None:
            return err
    try:
        scenario = load_scenario(args.scenario)
        configs = _configs(args.model, args.effort, not args.no_fallbacks)
        rounds = resolve_rounds(None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if client is None and not args.billed:
        return refuse_unbilled("Analysts + debate + trader smoke run", [
            f"scenario {scenario.name}: symbol {scenario.symbol} on saved fixtures "
            "(no Alpaca or SEC calls)",
            f"expected: {scenario.expected.action or 'none (baseline)'}",
            *(f"{a:<12} model {c.model}  effort {c.effort}  "
              f"fallbacks {'on' if c.fallbacks else 'off'}" for a, c in configs.items()),
            f"debate rounds {rounds}",
            f"Claude API calls: typically {8 + 2 * rounds}-{9 + 4 * rounds}, at most "
            f"{sum(2 * (configs[a].max_tool_rounds + 1) for a in AGENTS) + 4 * rounds + 2} "
            "(analysts: 2 attempts x tool rounds; debate: 2 attempts per turn; "
            "trader: up to 2)",
            "then the order ticket for a BUY (no approval step, nothing journaled or sent)",
        ])
    if client is None:
        try:
            client = make_client()
        except RuntimeError as exc:
            print(f"error: {exc} ({key_hint(args)})", file=sys.stderr)
            return 2

    data = ScenarioData(scenario)
    symbol = scenario.symbol
    now = data.as_of()
    ledger = PlanLedger(symbol)
    stage_s: dict[str, float] = {}
    t0 = time.monotonic()
    try:
        packet = asyncio.run(run_analysts(
            symbol, now, data=data, clients=client,
            configs={a: configs[a] for a in AGENTS}, ledger=ledger,
        ))
    except ValueError as exc:  # e.g. "limits config is invalid", before any analyst starts
        print(f"error: {exc}", file=sys.stderr)
        return 2
    stage_s["analysts"] = time.monotonic() - t0
    rejections: list[Rejection] = []
    t0 = time.monotonic()
    try:
        with money_debug(lambda side, items: rejections.append((side, items))):
            debate = run_debate(packet, clients=client,
                                configs={a: configs[a] for a in DEBATERS}, rounds=rounds)
    except BaseException:
        print_money_debug(rejections)  # still show what was collected before the crash
        raise

    stage_s["debate"] = time.monotonic() - t0
    t0 = time.monotonic()
    trader = run_trader(packet, debate, client=client, config=configs[TRADER])
    stage_s["trader"] = time.monotonic() - t0
    proposal = None
    if trader.decision.action == "BUY" and trader.decision.plan_id:
        # The ticket is sized from the ledger at the fixture's clock (fixture plans are dated
        # 2026-11-03). A throwaway journal: the smoke run never writes the real one.
        with tempfile.TemporaryDirectory() as tmp:
            proposal = propose(ledger, trader.decision.plan_id,
                               journal=Journal(Path(tmp) / "smoke.jsonl"), clock=lambda: now)

    report(packet, debate, configs)
    print_trader(trader, proposal)
    print_latency(stage_s, load_checked_limits().max_plan_age_s)
    print_money_debug(rejections)
    print(f"\n-- Expected vs actual ({scenario.name}) --")
    verdict = compare(scenario.expected, trader.decision.action, debate.fatal)[0]
    if trader.forced_pass in TRADER_FAILURES or not packet.all_ok or not debate.all_ok:
        verdict += "  (run failed: the decision is a fail-closed default, not a judgment)"
    print(verdict)

    calls = [c for r in packet.runs.values() for c in r.calls]
    calls += [c for t in debate.turns for c in t.run.calls]
    trader_failed = trader.forced_pass in TRADER_FAILURES
    if not calls or not packet.all_ok or not debate.all_ok or trader_failed:
        failed = [a for a in AGENTS if not packet.runs[a].ok]
        failed += [f"r{t.round} {t.side}" for t in debate.turns if not t.run.ok]
        failed += [f"trader ({trader.forced_pass})"] if trader_failed else []
        print(f"\nsmoke run FAILED: {', '.join(failed) or 'no API response'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
