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
   Orders are built only through `pipeline/orders.py` from tool-produced
   `ExitPlanRecord`s. Prices, stops, targets and sizes are never parsed from
   analyst or trader text, and analyst or trader `confidence` never reaches
   `size_position`, `risk_check` or the limits passed to them. The model picks
   among tool-produced plans; it never supplies or adjusts a price, stop, target
   or size.
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
- **Follow-ups become issues.** Right after opening a PR, file each item in its
  Follow-ups section as a GitHub issue with `gh issue create`, labeled
  `before-phase-N` (put it in that phase's milestone), `hardening` or `docs`,
  plus `risk-sensitive` when it touches sizing, stops/targets, `risk_check`,
  limits, broker code or the paper/live switch. List the new issue numbers in
  the PR. A PR that fixes an issue says `Fixes #N` in its description.
  Label a follow-up `before-phase-N` only if it could lose money or corrupt
  data in phase N; everything else is `hardening`. Work that phase N's own
  build will deliver stays in that phase's milestone without the
  `before-phase-N` label, with a comment saying so.
- **Phase gate.** Before starting phase N, check
  `gh issue list --label before-phase-N --state open`. If any are open, fix
  them first or tell Ivan which ones remain.
- **Only the main session changes the repo or GitHub.** Subagents may only run
  read-only git and gh commands (e.g. `git status`, `git diff`, `git log`,
  `git show`, `gh pr view`, `gh pr diff`, `gh pr checks`). Any git or gh command
  that changes the repo locally or on GitHub — including `git add`, `checkout`,
  `switch`, `reset`, `stash`, `commit`, `push`, and `gh api` writes — is the
  main session's job; subagents do their task and report back. The same rule
  is repeated in each `.claude/agents/*.md` prompt, since subagents may not
  see this file.

## Current phase

Phases 1–3 are done: the tools, the Technical, Sentiment and Fundamentals
analysts, and the orchestrator that runs them in parallel and returns a
`DecisionPacket`. Phase 4 (bull/bear debate + trader) is next. It is gated by
the open issues labeled `before-phase-4` (Phase 4 milestone): check
`gh issue list --label before-phase-4 --state open` and fix them first or tell
Ivan which remain.
