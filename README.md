# trading-agents

[![CI](https://github.com/SirIvan213/trading-agents/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/SirIvan213/trading-agents/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Paper trading only](https://img.shields.io/badge/trading-paper%20only-orange)

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
Alpaca, Alpha Vantage and FMP keys and the SEC user agent, so only set one you trust.

## Configuration

Keys and settings come from environment variables (see `.env.example`); nothing is read
from code or prompts.

| Variable | Meaning | Default / allowed values |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key | required for billed runs |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca market data and news | required for live data |
| `SEC_USER_AGENT` | identifies you to SEC EDGAR | `"Your Name you@example.com"` |
| `ALPHA_VANTAGE_API_KEY` | Alpha Vantage earnings calendar, the primary (see below) | required for the event gate |
| `FMP_API_KEY` | FMP earnings calendar, a block-only secondary (see below) | optional: without it the gate still works |
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
- **UNKNOWN**: a source failed, was malformed, stale or too sparse, or could not prove the
  symbol clear (for example no 8-K item 2.02 in the last 100 days).

**Listing restriction (#168).** The halt feed is only proven to cover Nasdaq, NYSE and NYSE
American listings: a live check saw no NYSE Arca or Cboe item in it. Until one is seen, the
scan rejects any other listing (from Alpaca's asset endpoint, never guessed from the ticker),
and the gate reads UNKNOWN for it.

**Quarterly reporting before a decision (#170).** A symbol with no 8-K item 2.02 in the last
100 days (a foreign private issuer filing 6-K / 20-F, no SEC filer, or SEC unavailable) can
only ever read UNKNOWN. So `run_decision` checks the free SEC filings first and skips it
(`SymbolSkipped`, journaled `skipped`) before any billed analyst runs, and the scan rejects it
when given the same check (`run_scan(..., reporting=...)`). The gate's rule is unchanged.

**Budget savers.** A packet with a failed analyst is not debated (#163): the Trader passes
without a call anyway, so the Bull and Bear calls are skipped and the debate record says
`skipped`. The technical analyst's `scan` tool takes this session's symbol plus at most 4
peers, at most twice per run (#43).

**Counting tokens (#58).** On a call where a server-side fallback ran, the top-level `usage`
covers only the attempt that produced the message; `usage.iterations` lists every attempt
(the Claude API docs call it the per-attempt source of truth). A call's tokens are therefore
the sum of its iterations when they are present, else the top-level usage
(`CallRecord.billed_usage()`, taking the larger of the two per field; summed over calls by
`agents.schemas.token_totals`). `run_day`'s daily token cap counts input, output, cache
writes and cache reads this way (its meter settles each call to `billed_usage()`).

`risk_check` blocks EVENT and UNKNOWN. The reason names the kind and the source, e.g.
`event gate EVENT: EVENT (alphavantage): estimated earnings date ...` against
`event gate UNKNOWN: UNKNOWN (alphavantage): earnings source unavailable: ...`. The order
ticket shows an `Event gate: <STATUS> (<reason>)` line, and the decision journal entry
carries a `gate` field with the status and reason.

Two earnings calendars feed the gate (#167, #145):

- **[Alpha Vantage](https://www.alphavantage.co/documentation/) (primary).** Its
  `EARNINGS_CALENDAR` endpoint returns, per its docs, the full list of scheduled reports
  for the next 3 months as CSV (to be confirmed by the first live call). Only this calendar can prove CLEAR. It gives no report time and no confirmed flag,
  so every date counts as an estimate: a symbol is blocked from three trading days before
  its listed date.
  - The key goes in `.env` as `ALPHA_VANTAGE_API_KEY`. A free key allows 25 calls a day;
    the gate makes one.
  - The terms license personal, non-commercial use, which covers private investment
    research and testing. Use on behalf of a firm, or by someone affiliated with a broker,
    adviser or bank, counts as commercial (Terms of Service, section 2).
- **[Financial Modeling Prep](https://site.financialmodelingprep.com/) (secondary).** It
  can only add an EVENT, for example from its report times or confirmed dates. Its failure
  never blocks on its own, and it never proves CLEAR: the free tier listed 3 reports for a
  week of earnings season.
  - The key goes in `.env` as `FMP_API_KEY`. FMP bills per call on its side.

How the calendars are fetched and checked:

- **Plausibility.** The primary calendar must list at least 50 reports from today to a week
  ahead, on at least 3 different trading days, or the gate says UNKNOWN. That floor is a
  starting point. Recalibrate it from live counts: `python -m pipeline.smoke_events SYMBOL
  --live` prints rows per day. A quiet week, such as late December, may fall below it, which
  blocks trading.
- **Caching.** Each calendar is fetched once per New York day and cached in `journal/cache/`
  (git-ignored).
  - A failed or invalid FMP reply is not cached and is retried on the next use.
  - A failed Alpha Vantage fetch is retried only after 15 minutes, and a reply that does not
    validate (for example one cut short) after 60, to stay under the free key's
    25-calls-a-day limit.
  - Under `run_day` (CLAUDE.md rule 6), every request is also counted on disk before it is
    made, shared across processes: at most **3 FMP requests** and **5 Alpha Vantage calls**
    per New York day, retries and failures included. A valid calendar is reused for the day.
    After the FMP cap, run_day runs without FMP (block-only, so nothing is cleared). After 5
    Alpha Vantage calls without a valid calendar, the gate cannot say CLEAR, so every new
    entry is blocked.
- **Where they are called.** Both are read by the event gate in the order path and by the
  Sentiment analyst's `get_news` tool.
- **Approval.** The first live call to each needs Ivan's explicit approval (CLAUDE.md rule
  6). Tests never call either.

## Running a trading day (`run_day`)

`python -m pipeline.run_day --scheduled` runs one trading day. It is launched only by the
Windows Task Scheduler task Ivan sets up ([docs/run_day_task_scheduler.md](docs/run_day_task_scheduler.md)).
Without `--scheduled` it prints the day's plan and calls nothing.

- **The day:** a scan at 09:40 ET over `config/run_day.json`'s universe. Then a decision cycle
  every 5 minutes until the day's entry end (NYSE calendar, early closes included), with at
  most 2 decisions per cycle and one per symbol per day. Each ticket is approved in the
  watched console; no answer before the plan expires is declined.
- **Daily caps** (CLAUDE.md rule 6, `pipeline/budget.py`):
  - 10 decisions, counted when they start;
  - 600,000 tokens, every call reserving its worst case before it runs and settling to its
    billed usage after;
  - 3 FMP requests and 5 Alpha Vantage calls.

  They are kept in `journal/state/` and shared across processes. A cap that is missing,
  invalid or above CLAUDE.md's value refuses to start. A lost or unreadable budget file
  closes the day.
- **No orders are sent yet.** A broker is refused until the reconciler (#155) and the
  flatten job (#135) are merged.
- **Housekeeping:** a single-instance lock, a heartbeat file, the calendar coverage warning
  at every start, and a hold on the day if an order from an earlier day may still hold a
  position.

## Paper broker

`tools/broker.py` is the Alpaca **paper** trading client (Phase 5). Its base URL is fixed to
`paper-api.alpaca.markets`, and there is no live path. It refuses to start unless
`limits.paper` is true, and refuses every call until it has read an ACTIVE, unblocked account
from the paper host that is not suspended by the user and has shorting disabled (#197: the
system is long only, so a stray sell must never open a short; set "no shorting" in the paper
account's configuration). Use Alpaca paper keys: Alpaca issues separate paper and live keys,
and documents no paper flag on the account.

An approved order goes out as a bracket:

- **Entry:** a limit order at entry + slippage allowance, so the exchange enforces the price
  sizing assumed.
- **Legs:** the plan's take-profit and stop, time in force `day`.
- **Allow-list:** the request is built field by field, so internal fields such as the
  slippage allowance are never forwarded.

### Sending (`pipeline/send.py`)

An approved order is sent only when `run_decision` is given a broker; without one, nothing is
sent. The ticket then says the order is **sent to the Alpaca paper account** and shows the
limit price. The sender:

1. **Before the journal lock:** builds the request and checks the paper account (network).
2. **Under the lock:** the order must still be an unsent approval whose plan has not expired.
   Today's totals are re-read without this order and `risk_check` runs again, covering the
   kill switch, market hours, trades, open positions, risk, the loss stop, stop-outs and the
   event gate. A block journals the approval `expired`, so it can never be sent later. A pass
   journals `submitting` before any HTTP call. If the journal refuses that entry (the plan
   expired) or cannot write it, nothing is sent.
3. **After the lock:** POSTs once and **never resends**. A reply that matches is `submitted`.
   After any failure the order is looked up by its `client_order_id`:
   - found and matching: `submitted`;
   - found but not matching: cancelled, `send_unknown`, ALERT;
   - not found after a definite refusal (HTTP 400/401/403/404/422): `rejected`;
   - anything else: `send_unknown`, ALERT.

A `submitting` or `send_unknown` order keeps its slot and its risk until the reconciler (Phase
5 PR 4) settles it with the broker.

## Decision journal

Every decision is a line in `journal/decisions.jsonl` (git-ignored). An approved order is
written as `approved_not_sent` with a deterministic `client_order_id` (from the run id and the
plan id: one order per plan). From Phase 5, everything that happens to it is appended as an
order event; nothing is ever rewritten, and an order's state is the replay of its events
(`journal/orders.py`):

`approved_not_sent` → `submitting` → `submitted` / `rejected` / `send_unknown` →
`partially_filled` / `filled` → `closed` (by `stop`, `target` or `flatten`), plus `cancelled`
and `expired`.

`risk_check` reads the day's totals from that replay (`Journal.day_state`):

- **Trades:** every order sent today uses a slot, whatever happens to it
  (`max_trades_per_day`). An approval never sent takes none.
- **Risk** (`daily_loss_limit`): today's realized losses, counting every position closed
  today whenever it was opened, plus the sized worst-case `max_loss` of everything open or
  pending. A confirmed zero fill releases an order; a profit adds nothing.
- **Open positions:** an order that may hold a position, from any day, blocks a new entry
  while `max_open_positions` (1) are open. An approval not sent yet counts, with its risk,
  only until its plan expires (`as_of + max_plan_age_s`); it is then journaled `expired`.
- **Daily loss stop:** the realized losses of trades closed today (an overnight position
  included) reaching `daily_loss_limit` stop new entries. Profits don't offset them.
- **No re-entry:** a symbol stopped out today cannot be bought again that day.

A malformed entry or an impossible order history fails closed: no order passes. Reads and
writes take the journal's lock. A last line cut off by a crash blocks reading until a human
runs `python -m journal.repair` (a dry run; `--yes` moves the fragment to a side file). It
never creates a file, and it reports, but never changes, a corrupt line mid-file (exit 3): a
human fixes that one. An order event is never dated before the order's previous event
(#194). Reading a journal that does not exist yet creates nothing.

## Status

- [x] Phase 0: repo, rules, subagents, CI
- [x] Phase 1: tools (`bars`, `scan`, `news`, `exit_plan`, `size`, `risk_check`)
- [x] Phase 2: first agent (Technical Analyst)
- [x] Phase 3: parallel analysts + orchestrator
- [x] Phase 4: bull/bear debate + trader
- [ ] Phase 5: paper trading, scheduling, decision journal (in progress)
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
