# trading-agents

A multi-agent intraday trading research system. Specialist LLM agents analyze a
stock, argue for and against a trade, and propose BUY or PASS (long only).
Deterministic code then sizes the order, checks it against hard risk limits, and
asks a human before anything reaches a **paper** trading account.

> Research and portfolio project. Not financial advice. Paper trading only.

## How it works

```
pre-filter (scan + odds) ──▶ Orchestrator
                               ├─ Technical Analyst ─┐
                               ├─ Sentiment Analyst ─┼─▶ Bull ⇄ Bear ──▶ Trader
                               └─ Fundamentals ──────┘                    │
                                                                          ▼
                           size ──▶ risk_check (PASS/BLOCK) ──▶ your yes ──▶ Alpaca paper
```

- **Agents** (`agents/`) make judgments and return validated JSON.
- **Tools** (`tools/`) fetch data and do all money math; they are plain, tested Python.
- **Gates**: hard risk rules in code, paper trading by default, human approval per order.

Design inspired by [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                       # add your keys
cp config/limits.example.json config/limits.json
pytest -q
```

Data requests honour the `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` environment variables
(Python's default opener). A proxy sees the full request URLs and headers, including the
Alpaca and FMP keys and the SEC user agent, so only set one you trust.

## Configuration

Keys and settings come from environment variables (see `.env.example`); nothing is read
from code or prompts.

| Variable | Meaning | Default / allowed values |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key | required for billed runs |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca market data and news | required for live data |
| `SEC_USER_AGENT` | identifies you to SEC EDGAR | `"Your Name you@example.com"` |
| `FMP_API_KEY` | FMP earnings calendar (see below) | required for the event gate |
| `TRADING_AGENTS_MODEL` | model for every agent | `claude-sonnet-5-5` (`claude-opus-5-5` for the trader) |
| `TRADING_AGENTS_EFFORT` | effort for every agent | `low`, `medium`, `high`, `xhigh` or `max`; default `high` |
| `TRADING_AGENTS_<AGENT>_MODEL` | model for one agent; wins over `TRADING_AGENTS_MODEL` | `<AGENT>` is `TECHNICAL`, `SENTIMENT`, `FUNDAMENTALS`, `BULL`, `BEAR` or `TRADER` |
| `TRADING_AGENTS_<AGENT>_EFFORT` | effort for one agent; wins over `TRADING_AGENTS_EFFORT` | same values as `TRADING_AGENTS_EFFORT` |
| `TRADING_AGENTS_DEBATE_ROUNDS` | Bull/Bear debate rounds | whole number 1 to 5; default 2 |

A blank value, an unknown effort or an out-of-range number is a configuration error, not
a silent default. Risk limits are separate: `config/limits.json` (copy of
`config/limits.example.json`).

## Earnings calendar and the event gate

Before any order, the event gate (`tools/event_gate.py`) checks earnings and trading
halts. It returns one of three results, and **only CLEAR lets an order through**:

- **CLEAR**: every required source loaded cleanly and shows no earnings in the window
  and no halt today.
- **EVENT**: a source shows earnings today or in the trade window (or an estimated date
  within three trading days), an item 2.02 filed today, or a halt or pause today.
- **UNKNOWN**: a source failed, was malformed or stale, or could not prove the symbol
  clear (for example no 8-K item 2.02 in the last 100 days).

`risk_check` blocks EVENT and UNKNOWN. The reason names the kind and the source, e.g.
`event gate EVENT: EVENT (fmp): earnings today ...` against
`event gate UNKNOWN: UNKNOWN (fmp): earnings source unavailable: ...`. The order ticket
shows an `Event gate: <STATUS> (<reason>)` line, and the decision journal entry carries a
`gate` field with the status and reason.

The earnings calendar comes from [Financial Modeling Prep](https://site.financialmodelingprep.com/):

- The key goes in `.env` as `FMP_API_KEY`. FMP bills per call on its side.
- It is called from the event gate in the order path and from the Sentiment analyst's
  `get_news` tool.
- One range call per New York day is cached in `journal/cache/` (git-ignored). A failed
  fetch is not cached and is retried on the next use.
- The first live call needs Ivan's explicit approval (CLAUDE.md rule 6). Tests never call
  FMP.

## Status

- [x] Phase 0: repo, rules, subagents, CI
- [x] Phase 1: tools (`bars`, `scan`, `news`, `exit_plan`, `size`, `risk_check`)
- [x] Phase 2: first agent (Technical Analyst)
- [x] Phase 3: parallel analysts + orchestrator
- [ ] Phase 4: bull/bear debate + trader
- [ ] Phase 5: paper trading, scheduling, decision journal
- [ ] Phase 6: evaluation and optimization

## Follow-ups and known gaps

Tracked as [GitHub issues](https://github.com/SirIvan213/trading-agents/issues), labeled by when they must be done:

- [`before-phase-2`](https://github.com/SirIvan213/trading-agents/issues?q=is%3Aopen+label%3Abefore-phase-2): fix before the agents build on the tools ([Phase 2 milestone](https://github.com/SirIvan213/trading-agents/milestone/1))
- [`before-phase-4`](https://github.com/SirIvan213/trading-agents/issues?q=is%3Aopen+label%3Abefore-phase-4): fix before the trader consumes analyst output ([Phase 4 milestone](https://github.com/SirIvan213/trading-agents/milestone/3))
- [`before-phase-5`](https://github.com/SirIvan213/trading-agents/issues?q=is%3Aopen+label%3Abefore-phase-5): fix before any paper trading ([Phase 5 milestone](https://github.com/SirIvan213/trading-agents/milestone/2))
- [`hardening`](https://github.com/SirIvan213/trading-agents/issues?q=is%3Aopen+label%3Ahardening): real but low risk; fix anytime
- [`docs`](https://github.com/SirIvan213/trading-agents/issues?q=is%3Aopen+label%3Adocs): wording only

Issues that touch sizing, stops, `risk_check`, limits or the broker also carry
[`risk-sensitive`](https://github.com/SirIvan213/trading-agents/issues?q=is%3Aopen+label%3Arisk-sensitive).

## Working on it with Claude Code

Project rules are in [`CLAUDE.md`](CLAUDE.md). Specialist subagents live in
`.claude/agents/`: `tools-engineer`, `agents-engineer`, `qa-engineer`,
`code-reviewer`.
