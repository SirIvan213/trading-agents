# trading-agents

A multi-agent intraday trading research system. Specialist LLM agents analyze a
stock, argue for and against a trade, and propose BUY, SELL or PASS.
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

## Status

- [x] Phase 0: repo, rules, subagents, CI
- [ ] Phase 1: tools (`bars`, `scan`, `news`, `exit_plan`, `size` ✅, `risk_check` ✅)
- [ ] Phase 2: first agent (Technical Analyst)
- [ ] Phase 3: parallel analysts + orchestrator
- [ ] Phase 4: bull/bear debate + trader
- [ ] Phase 5: paper trading, scheduling, decision journal
- [ ] Phase 6: evaluation and optimization

## Working on it with Claude Code

Project rules are in [`CLAUDE.md`](CLAUDE.md). Specialist subagents live in
`.claude/agents/`: `tools-engineer`, `agents-engineer`, `qa-engineer`,
`code-reviewer`.
