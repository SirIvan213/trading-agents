# Scheduling `run_day` with Windows Task Scheduler

`pipeline.run_day` runs one trading day of decisions. CLAUDE.md rule 6 pre-approves its billed
calls (Claude, FMP, Alpha Vantage) only when **this task**, the one Ivan sets up, launches it,
with `paper: true` and all four daily caps enforced. A Claude Code session never creates or
changes this task, and never starts `run_day`.

Times below are **Pacific**, the PC's clock. New York and Pacific change to and from daylight
time on the same dates, so 6:30 AM Pacific is always 9:30 AM New York.

## Before the first run

1. Copy `config\run_day.example.json` to `config\run_day.json` and set `universe` to your
   symbols. Leave the caps as they are: a cap above CLAUDE.md's value is refused, and raising
   one needs Ivan's explicit approval.
2. Check `config\limits.json` has `"paper": true` (run_day refuses otherwise).
3. Check `.env` has the keys (`ANTHROPIC_API_KEY`, Alpaca, FMP, Alpha Vantage, SEC user agent;
   see `.env.example`).
4. Dry run from a terminal in the repo folder. It prints the day's plan and calls nothing:

   ```
   .venv\Scripts\python.exe -m pipeline.run_day
   ```

## Keep the PC awake

The PC must not sleep while the task runs (6:30 AM to 1:30 PM Pacific on weekdays). A sleeping
PC stops the decision loop, leaves tickets unanswered, and stops the flat-by: positions would
stay open with no one watching them.

- **Settings, System, Power**: set "When plugged in, put my device to sleep after" to
  **Never** (at least on trading days).
- Leave the PC plugged in and signed in. The task runs only while you are signed in, so its
  console window is visible: that window is where you approve tickets.

## Create the task

Open **Task Scheduler**, then **Create Task...** (not "Create Basic Task").

**General**
- Name: `trading-agents run_day`
- **Run only when user is logged on** (so the console window is visible for approvals).
- Leave "Run with highest privileges" unchecked.

**Triggers**: New...
- Begin the task: **On a schedule**, **Weekly**, start **6:30:00 AM**.
- Recur every 1 week on **Monday, Tuesday, Wednesday, Thursday, Friday**.
- Check **Stop task if it runs longer than**, and type **7 hours** (6:30 AM to 1:30 PM Pacific,
  which is 1:30 PM to 4:30 PM New York: after the close).
- Enabled: checked.

`run_day` itself waits for the 9:40 AM New York scan (6:40 AM Pacific), makes no decisions
after the day's entry end (12:30 PM New York, 9:30 AM Pacific, on an early close), and exits on
a weekend or market holiday, so the weekday trigger does not need a holiday list. With
`--send`, it keeps running after the entry window until the paper account is flat or the
market closes (4:00 PM New York, 1:00 PM Pacific; 1:00 PM New York on an early close), so the
7 hours always cover it.

**Actions**: New...
- Action: **Start a program**.
- Program/script: `C:\Users\ivanp\trading-agents\.venv\Scripts\python.exe`
- Add arguments: `-m pipeline.run_day --scheduled --send`
- Start in: `C:\Users\ivanp\trading-agents`

Only the task passes `--scheduled`. `--send` lets approved orders reach the Alpaca **paper**
account. It is accepted only while the reconciler (#155) and the flat-by job (#135) are both
in the code (`RUN_DAY_READY`). Without `--send`, run_day decides and journals each approval
as "approved, not sent".

**Conditions**
- Uncheck **Start the task only if the computer is on AC power** (or keep the PC plugged in).
- Check **Wake the computer to run this task**.

**Settings**
- Check **Allow task to be run on demand**.
- Check **Stop the task if it runs longer than: 8 hours** (a backstop to the trigger's 7).
- **If the task is already running, then the following rule applies: Do not start a new
  instance.** (run_day also has its own single-instance lock.)

## While it runs

- **Approving a ticket.** The console shows each order ticket, rings the bell, and says by
  when to answer. Type `yes` and Enter to approve. Anything else, or no answer before the
  plan expires, is declined. A `yes` typed before the ticket appears never counts.
  Type only after the ticket appears: characters typed earlier without Enter can stay in
  the console's line and be sent by your next Enter.
- **Heartbeat.** `journal\state\run_day.heartbeat.json` is rewritten at least every 30 seconds,
  with the phase and the day's counts. A stale heartbeat means run_day stopped.
- **Daily budget.** `journal\state\budget-YYYY-MM-DD.json` holds the day's counts (decisions,
  tokens, FMP requests, Alpha Vantage calls). Never edit or delete it: a budget file that is
  missing or unreadable after the day started closes the day (no more decisions).
- **Alerts.** An `ALERT:` line in the console and a `run_day_alert` entry in the journal
  (`reconcile_alert` for the reconciler's). Desktop notifications are a follow-up.

## The paper account is dedicated to this project

The Alpaca **paper** account belongs to this project alone. **Never place, change or cancel
an order in it by hand** (except when an alert below asks you to). The reconciler relies on
that: an order or position it cannot trace to the journal is an **orphan**, and an order it
cannot read is **unreadable**. Both block new entries; an orphan is also flattened. For the
same reason, never run the paper probe (`python -m pipeline.probe_broker --live`) while run_day
is running: its orders would be flattened as orphans.

## The reconciler and its blocks

With `--send`, the reconciler (`pipeline/reconcile.py`) runs inside run_day every 15 seconds
and before each decision. It records fills and closes, cancels an
entry still unfilled after 60 seconds, and makes sure every position has a working stop for
all of its shares. A position without one is flattened at market and alerted. Its last pass
time is in the heartbeat (`reconciled_at`). If that is more than 45 seconds old, or the
reconciler stopped, run_day starts no decision and sends nothing until it recovers.

**Check what it sees, without acting** (works with the market closed; it never sends,
cancels or closes anything, and writes nothing):

```
.venv\Scripts\python.exe -m pipeline.reconcile --check
```

`--check` and `--once` (one acting pass, run by hand) have no start-of-day broker check. So
anything at the broker that no order sent today explains counts as an earlier day's: they
block it and alert, and never close or cancel it. Inside run_day, the start-of-day check has
already held the day for anything like that. So run_day's own reconciler treats something
untraceable that turns up during the day as an orphan, and flattens it.

**Blocks.** When the reconciler finds something it cannot square, it writes a block and
alerts, and **no new entry is allowed until you resolve every block**:

| Kind | What it means |
|---|---|
| `conflict` | The broker reported something the journal refuses (a fill after a close, an order the journal calls rejected that the broker holds ...). |
| `unreadable_order` | An open order the broker client cannot read (for example, one placed by hand). Nothing acts on it. |
| `orphan` | A position or order that is not in the journal. During the session it is flattened, except in a symbol an earlier day's order may hold. |
| `unprotected` | A position without a working stop for all its shares. During the session it is flattened, except an earlier day's position: that is never closed automatically (check it in the dashboard). A partial fill whose stop and take-profit Alpaca cancelled with the unfilled rest is flattened with an alert and blocks only if that close is not confirmed (closed at Alpaca and recorded with matching quantities within 60 s, or by the end of the last pass). Cancelling a partly filled entry by hand in the dashboard looks the same to it. Key `outside-session:SYMBOL`: something the reconciler would have cancelled or closed outside the market session (below); nothing was sent. |
| `fill_above_limit` | An entry filled above its limit price. |

List the open blocks, check the Alpaca paper dashboard, fix the cause, then resolve each one
with a note saying what you did:

```
.venv\Scripts\python.exe -m journal.resolve
.venv\Scripts\python.exe -m journal.resolve rb-1a2b3c4d --note "cancelled the stray order by hand"
```

If the cause is still there, the next pass blocks again.

**Manual action needed.** If an open order in a symbol cannot be read, it could be a close
that is already working, so the reconciler skips closing that symbol and alerts on every pass.
If that lasts 60 seconds, or reaches the flat-by time, the alert says **MANUAL ACTION NEEDED**:
in the Alpaca paper dashboard, cancel that symbol's orders and close the position.

## The flat-by and the end of the day

Nothing is held overnight. Each day has a **flat-by** time from the NYSE calendar:

- **3:55 PM New York** (12:55 PM Pacific) on a normal day;
- **12:55 PM New York** (9:55 AM Pacific) on an early close (13:00), for example the day after
  Thanksgiving or Christmas Eve.

From the flat-by on, no order is sent. Every reconcile pass, every 15 seconds:

1. cancels each of today's entries that is still working, and confirms it is gone;
2. for each of today's positions: cancels its stop and take-profit, confirms they are gone,
   closes the position at market, and checks the close again after any unclear reply (it
   never sends a second close blindly).

A symbol that is not flat yet alerts on every pass. After 60 seconds the alert says
**MANUAL ACTION NEEDED** in the Alpaca paper dashboard. If the account is still not flat
3 minutes before the close, run_day alerts again.

After the entry window, run_day keeps the reconciler running until the account is flat (no
position, no open order) or the market closes, then makes one last pass and exits. If it exits
with anything still open, it alerts: close it in the dashboard. Tomorrow it would be an
earlier day's position (below).

**A position from an earlier day is never closed automatically** (decision 8). At the start of
the day, run_day checks the journal and the broker. If it finds an order sent on an earlier
day that may still hold shares, or a position or open order at the broker that no order sent
today explains, it:

- writes a block, which works as a kill switch: no new entry until you resolve it;
- alerts;
- exits with code 5 (held), with no decisions that day.

The broker check matches **by order, never by symbol** (#257). An open order counts as
today's only if it is an order sent today or one of its bracket legs. A position counts as
today's only up to the shares today's filled entries still hold, by the broker's own record.
So a leftover in a symbol that was also traded today holds the day like any other, on a
restart too, and a position larger than today's fills explain holds it in full. A flat-by
close still working when run_day restarts holds the day as well, since nothing records its
id.

The same happens if the broker cannot be checked. Check the dashboard, close or cancel what is
there by hand, then resolve the block with `journal.resolve`.

### Look, or flatten by hand

Neither command uses a billed API. The Alpaca paper API is free.

```
.venv\Scripts\python.exe -m pipeline.flatten --check
.venv\Scripts\python.exe -m pipeline.flatten --now
```

- `--check` is read only: it prints today's flat-by time and what `--now` would cancel and
  close. It works while run_day runs.
- `--now` flattens today's positions immediately, repeating every 15 seconds until they are
  flat or the market closes.
- `--now` only closes what an order sent today explains. A position or order that no order
  sent today explains counts as an earlier day's, so `--now` blocks it, alerts and leaves it
  alone. So does an open order it cannot read. Close those in the dashboard yourself.
- **Caution:** `--now` still matches by symbol. If a symbol holds both today's shares and an
  earlier day's, `--now` would close or cancel both. Close that symbol in the dashboard
  instead. run_day's held alert says so when it finds such a symbol.
- `--now` refuses while run_day is running and watching (exit 4), because run_day flattens
  at the flat-by itself. To get out earlier, stop run_day first. If run_day is hung (no
  reconcile pass in the last 2 minutes), `--now` acts anyway.
- `--now` refuses outside the session (exit 5), and when the calendar does not cover the
  year. It does this before any broker call.

**Outside the market session nothing cancels or closes anything.** That covers:
- before 9:30 AM New York;
- at or after the close (4:00 PM, or 1:00 PM on an early close);
- weekends and holidays;
- a year the calendar does not cover.

It applies to every path: run_day's reconciler (its first passes if started early, and its
last pass), `pipeline.reconcile --once`, and `pipeline.flatten --now`. Before the open, every
position is an earlier day's (decision 8). After the close, a market close would fill at the
next open with nobody deciding.

Whatever the reconciler would have cancelled or closed gets a block (`outside-session:SYMBOL`)
and an alert, MANUAL ACTION NEEDED, repeated on every pass. Check the Alpaca paper dashboard,
deal with it during the session, then resolve the block.

| `--now` exit code | Meaning |
|---|---|
| 0 | Flat: no position and no open order. |
| 1 | Not flat: something is left (an earlier day's position, or the market closed first). Check the dashboard. |
| 2 | Refused: config, `paper`, keys, or the paper account check. |
| 4 | Refused: run_day is running and watching. |
| 5 | Refused: the market is closed. |

### Optional backstop task

If run_day dies in the afternoon, nothing flattens at the flat-by. You can create a second
task yourself that runs `--now` two minutes after the flat-by. A Claude Code session never
creates or changes it.

- **Create Task...**, name `trading-agents flatten backstop`, **Run only when user is logged
  on**.
- **Triggers**: Weekly, Monday to Friday, at **12:57:00 PM** Pacific (3:57 PM New York). For
  each early close, add a **One time** trigger at **9:57:00 AM** Pacific (12:57 PM New York):
  2026-11-27, 2026-12-24, 2027-11-26, 2028-07-03, 2028-11-24 (`tools/market_calendar.py`,
  `EARLY_CLOSES`). Check **Stop task if it runs longer than 10 minutes**.
- **Actions**: Program `C:\Users\ivanp\trading-agents\.venv\Scripts\python.exe`, arguments
  `-m pipeline.flatten --now`, start in `C:\Users\ivanp\trading-agents`.

On a normal day its "Last Run Result" is 0x4 (run_day was watching) or 0x0 (already flat).
0x1 means look at the dashboard. On a holiday it is 0x5.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The day ended: the entry window closed, a cap was reached, or there was no session. |
| 2 | Refused to start: config, a cap, limits, `paper`, or `--send`. |
| 3 | Dry run (no `--scheduled`): nothing was called. |
| 4 | Another run_day is already running. |
| 5 | Held: an order from an earlier day may still hold a position, the broker holds a position or order no order sent today explains, or the broker could not be checked. A block was written. Nothing was closed. |

Task Scheduler shows the last exit code as "Last Run Result" (0x0, 0x2 ...).

## First paper day checklist

**The day before**

1. `config\limits.json`: `"paper": true` and `"trading_enabled": true`. run_day refuses
   without `paper: true`.
2. `config\run_day.json`: `universe` is your list (check it with
   `python -m pipeline.universe_check`), and the caps are unchanged.
3. `.env` has the Alpaca **paper** keys, plus Anthropic, FMP, Alpha Vantage and the SEC user
   agent.
4. The Task Scheduler task (above):
   - **Run only when user is logged on**, so you see the console;
   - arguments `-m pipeline.run_day --scheduled --send`;
   - weekdays at 6:30 AM Pacific;
   - wake the computer to run it;
   - do not start a new instance.

   Optionally, add the backstop task.
5. The PC is plugged in, set never to sleep, and you are signed in.
6. In the Alpaca paper dashboard, there is no position and no open order. The account belongs
   to this project alone.

**Before the open** (in a terminal in the repo folder)

```
.venv\Scripts\python.exe -m journal.resolve              # no open blocks
.venv\Scripts\python.exe -m pipeline.reconcile --check   # sees the account, would do nothing
.venv\Scripts\python.exe -m pipeline.flatten --check     # today's flat-by; --now would do nothing
.venv\Scripts\python.exe -m pipeline.run_day             # dry run: the day's plan, calls nothing
```

Start the UI and keep **http://127.0.0.1:8765/#/live** open:
`.venv\Scripts\python.exe -m ui.api` (it only reads, and calls nothing; see `ui/README.md`).

**During the day**

- **Approve** in the run_day console window only. When a ticket appears (with a bell), read
  it and type `yes` and Enter before the deadline it shows. Anything else, or no answer, is
  declined.
- **An ALERT line** in the console (also a `run_day_alert` or `reconcile_alert` in the
  journal, and on the Live page) needs a look.
- **A block** stops every new entry. Run `python -m journal.resolve` to see it, then check
  the Alpaca paper dashboard and fix the cause (cancel the stray order, close the position).
  Then resolve it with a note:
  `python -m journal.resolve rb-1a2b3c4d --note "what you checked and did"`.
  See "The reconciler and its blocks" above.
- **MANUAL ACTION NEEDED**: in the Alpaca paper dashboard, cancel that symbol's orders and
  close its position.
- **At the flat-by** (12:55 PM Pacific, 9:55 AM on an early close), run_day flattens today's
  positions and exits once the account is flat. Check the dashboard after it exits: no
  position, no open order.

**Stop everything (the kill switch)**

1. **Stop new orders:** set `"trading_enabled": false` in `config\limits.json`. risk_check
   then refuses every order, from the next decision on and at the send.
2. **Stop run_day:** press Ctrl+C in its window, or End the task in Task Scheduler. Ctrl+C
   lets it make a last reconcile pass and alert if anything is still open. A stopped run_day
   flattens nothing at the flat-by.
3. **Close what is open,** right away: run `python -m pipeline.flatten --now` while the
   market is open, or close it in the Alpaca paper dashboard. Nothing is held overnight on
   purpose, so make sure the account is flat.
4. Before the next day, set `"trading_enabled": true` again and resolve any open block.
