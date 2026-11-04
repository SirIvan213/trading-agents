# trading-agents — project rules for Claude Code

A multi-agent intraday trading research system. LLM agents analyze a stock,
debate it, and propose BUY / SELL / PASS. Deterministic code sizes the order,
checks it against hard risk limits, and asks a human before anything reaches a
**paper** trading account.

## Non-negotiable rules

1. **Paper trading only.** `config/limits.json` ships with `"paper": true`. Never
   change the default, never add a code path that trades live without an
   explicit `--live` flag AND `paper: false` in config.
2. **Agents never do arithmetic that decides money.** Position size, stops,
   targets and risk verdicts come from functions in `tools/`, never from an LLM.
3. **Never weaken a risk limit to make something pass.** If a test or a trade
   fails `risk_check`, fix the cause. Any diff touching `tools/risk_check.py`,
   `tools/size.py`, broker code, or the paper/live switch must be called out in
   the PR description.
4. **No secrets in code, prompts, logs or tests.** Keys come from environment
   variables (see `.env.example`).
5. **Tests never hit the network.** Every tool is tested against saved fixtures
   in `tests/fixtures/`.

## Layout

```
agents/      orchestrator, analysts, bull, bear, trader (LLM layer)
  prompts/   one system prompt per agent (markdown)
  schemas.py Pydantic models for every agent output
tools/       deterministic tools: data, math, risk, broker
pipeline/    runs one decision end to end; rules-only baseline
journal/     decision log and outcomes
evals/       saved scenarios + scoring
config/      *.example.json only; real config is git-ignored
tests/       pytest + fixtures
```

## Conventions

- Python 3.11+, type hints everywhere, standard library first.
- Each tool is a plain function with a docstring and a JSON-serializable
  return value, so it can be registered as a Claude tool later.
- Money is computed in `Decimal` or integer cents; round stops down, never up.
- Times are US/Eastern (`America/New_York`) for anything market-related.
- Run `pytest -q` before saying work is done.

## Working with subagents

Specialists live in `.claude/agents/`. Split work by folder so agents don't edit
the same files: tools-engineer → `tools/`, agents-engineer → `agents/`,
qa-engineer → `tests/`, code-reviewer is read-only.

## Git workflow

- **Never commit to `main`.** Every change goes on a branch.
- **One branch per task**, created from an up-to-date `main`. Don't pile
  unrelated work onto an open PR's branch.
- **Before pushing:** `pytest -q` and `ruff check .` must both pass, then run
  the code-reviewer subagent on the diff and fix any blockers it reports.
- **Open the PR with `gh pr create`** against `main`. The description has
  these sections:
  - **Summary**: what changed and why.
  - **Risk-sensitive changes**: anything touching sizing, stops/targets,
    `risk_check`, limits, broker code or the paper/live switch, with the
    lines quoted; say "None" if there are none.
  - **Follow-ups**: known gaps and reviewer suggestions not done in this PR.
  - **Test plan**: commands run and their results.
- **Never merge.** Ivan merges after CI passes.
- **Only the main session commits, pushes or opens PRs.** Subagents do their
  task and report back; they never run `git commit`, `git push`, or any `gh`
  command that changes GitHub (`gh pr create`, `gh pr merge`, `gh pr comment`,
  ...). Read-only commands (e.g. `git status`, `git diff`, `git log`, `gh pr view`,
  `gh pr diff`) are fine. The same rule is repeated in each
  `.claude/agents/*.md` prompt, since subagents may not see this file.

## Current phase

Phase 1: tools (`bars`, `scan`, `news`, `exit_plan`, `size`, `risk_check`) with
fixture tests. `size` and `risk_check` exist as a starting pattern.
