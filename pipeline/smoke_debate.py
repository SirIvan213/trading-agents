"""Smoke run: the three analysts and then the Bull/Bear debate, once, on saved TSLA fixture
data, against the REAL Claude API.

Not part of the test suite (it lives outside tests/ and is never imported by it). It makes
real, billed Claude API calls; it makes NO Alpaca or SEC calls. Every input is a fixture in
tests/fixtures/ (synthetic data in the real API shapes, for a run "as of" 2026-11-03 11:00 ET):

    alpaca_bars_tsla.json, alpaca_snapshot_tsla.json   bars and snapshot (technical)
    alpaca_news_tsla.json, sec_submissions_tsla.json   headlines and 8-K filings (sentiment)
    sec_companyfacts_tsla.json                         XBRL quarterly facts (fundamentals)

Nothing is sent unless --billed is given (CLAUDE.md rule 6); without it the script prints
what it would run and exits 3. The API key is read from ANTHROPIC_API_KEY, loaded from .env
in the repo root (or --env-file PATH; --no-dotenv skips it) if present (a non-empty exported
variable wins). The key is never printed. Without flags each agent uses
its own configured model and effort (config_for: TRADING_AGENTS_<AGENT>_MODEL / _EFFORT,
then TRADING_AGENTS_MODEL / _EFFORT, then the defaults); --model / --effort override all five
agents (technical, sentiment, fundamentals, bull, bear). Debate rounds come from
TRADING_AGENTS_DEBATE_ROUNDS (default 2). Risk limits are loaded and validated as in a real run.

Exit codes: 0 = all three analysts and every debate turn produced validated output (a
server-side MODEL fallback, where another model answered, still counts as success and is
shown per call); 1 = an agent produced no validated output (a neutral analyst report, a
missing Bull case or a fail-closed Bear) or no API call succeeded; 2 = no API key,
unreadable or missing .env/--env-file, or invalid config/limits; 3 = --billed not given,
nothing was sent.

Usage:
    python -m pipeline.smoke_debate                          # dry plan only, nothing sent
    python -m pipeline.smoke_debate --billed
    python -m pipeline.smoke_debate --billed --model claude-sonnet-5-5
    python -m pipeline.smoke_debate --billed --effort medium --no-fallbacks
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime
from typing import Any

from agents.debate import resolve_rounds, run_debate
from agents.llm import EFFORTS, AgentConfig, config_for, make_client
from agents.orchestrator import AGENTS, run_analysts
from agents.schemas import CallRecord, DebateRecord, DecisionPacket
from pipeline.smoke_common import ROOT, add_safety_flags, key_hint, load_env, refuse_unbilled
from pipeline.smoke_technical import (
    FIXTURES,
    SYMBOL,
    FixtureMarketData,
    token_totals,
)

NEWS_FIXTURE = FIXTURES / "alpaca_news_tsla.json"
SUBMISSIONS_FIXTURE = FIXTURES / "sec_submissions_tsla.json"
COMPANYFACTS_FIXTURE = FIXTURES / "sec_companyfacts_tsla.json"
DEBATERS = ("bull", "bear")


class FixtureData(FixtureMarketData):
    """Market, news and filings data for TSLA from saved fixtures. Makes no network calls."""

    def __init__(self) -> None:
        super().__init__()
        self._news = json.loads(NEWS_FIXTURE.read_text(encoding="utf-8"))
        self._subs = json.loads(SUBMISSIONS_FIXTURE.read_text(encoding="utf-8"))
        self._facts = json.loads(COMPANYFACTS_FIXTURE.read_text(encoding="utf-8"))

    def news(self, symbols: list[str], start: datetime, end: datetime) -> dict:
        wanted = {s.upper() for s in symbols}
        items = [n for n in self._news["news"] if wanted & {s.upper() for s in n["symbols"]}]
        return {"news": items, "next_page_token": None}

    def submissions(self, symbol: str) -> dict | None:
        return self._subs if symbol.upper() == SYMBOL else None

    def company_facts(self, symbol: str) -> dict | None:
        return self._facts if symbol.upper() == SYMBOL else None


def _configs(model: str | None, effort: str | None, fallbacks: bool) -> dict[str, AgentConfig]:
    """config_for(agent) for all five agents, with any command-line overrides applied."""
    out = {}
    for agent in (*AGENTS, *DEBATERS):
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
    for agent in AGENTS:
        calls = packet.runs[agent].calls
        all_calls += calls
        _print_calls(agent, calls)
    for turn in debate.turns:
        all_calls += turn.run.calls
        _print_calls(f"r{turn.round} {turn.side}", turn.run.calls)
    served = sorted({c.serving_model for c in all_calls})
    print(f"serving model(s): {', '.join(served) or 'none (no API response)'}")
    print(f"fallback ran: {'yes' if any(c.fallback_ran for c in all_calls) else 'no'}")

    t = token_totals(all_calls)
    print(f"\n-- Tokens (sum over all {len(all_calls)} API calls) --")
    print(f"input {t['input_tokens']}  output {t['output_tokens']}  "
          f"cache write {t['cache_creation_input_tokens']}  "
          f"cache read {t['cache_read_input_tokens']}  total processed {sum(t.values())}")


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the three analysts and the Bull/Bear debate once on saved TSLA "
                    "fixtures against the real Claude API (billed; no Alpaca or SEC calls)."
    )
    parser.add_argument("--model", default=None,
                        help="Claude model ID for all five agents (default: each agent's "
                             "configured model, normally claude-sonnet-5-5)")
    parser.add_argument("--effort", default=None, choices=sorted(EFFORTS),
                        help="effort for all five agents (default: each agent's configured effort)")
    parser.add_argument("--no-fallbacks", action="store_true",
                        help="turn off server-side refusal fallback")
    add_safety_flags(parser)
    args = parser.parse_args(argv)

    if client is None:  # .env first: it may hold the key and TRADING_AGENTS_* settings
        err = load_env(args, ROOT)
        if err is not None:
            return err
    try:
        configs = _configs(args.model, args.effort, not args.no_fallbacks)
        rounds = resolve_rounds(None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if client is None and not args.billed:
        return refuse_unbilled("Analysts + debate smoke run", [
            f"symbol {SYMBOL} on saved fixtures (no Alpaca or SEC calls)",
            *(f"{a:<12} model {c.model}  effort {c.effort}  "
              f"fallbacks {'on' if c.fallbacks else 'off'}" for a, c in configs.items()),
            f"debate rounds {rounds}",
            f"Claude API calls: typically {7 + 2 * rounds}-{7 + 4 * rounds}, at most "
            f"{sum(2 * (configs[a].max_tool_rounds + 1) for a in AGENTS) + 4 * rounds} "
            "(analysts: 2 attempts x tool rounds; debate: 2 attempts per turn)",
        ])
    if client is None:
        try:
            client = make_client()
        except RuntimeError as exc:
            print(f"error: {exc} ({key_hint(args)})", file=sys.stderr)
            return 2

    data = FixtureData()
    now = data.as_of()
    try:
        packet = asyncio.run(run_analysts(
            SYMBOL, now, data=data, clients=client,
            configs={a: configs[a] for a in AGENTS},
        ))
    except ValueError as exc:  # e.g. "limits config is invalid", before any analyst starts
        print(f"error: {exc}", file=sys.stderr)
        return 2
    debate = run_debate(packet, clients=client,
                        configs={a: configs[a] for a in DEBATERS}, rounds=rounds)

    report(packet, debate, configs)

    calls = [c for r in packet.runs.values() for c in r.calls]
    calls += [c for t in debate.turns for c in t.run.calls]
    if not calls or not packet.all_ok or not debate.all_ok:
        failed = [a for a in AGENTS if not packet.runs[a].ok]
        failed += [f"r{t.round} {t.side}" for t in debate.turns if not t.run.ok]
        print(f"\nsmoke run FAILED: {', '.join(failed) or 'no API response'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
