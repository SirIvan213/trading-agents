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
PC stops the decision loop and leaves tickets unanswered.

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
a weekend or market holiday, so the weekday trigger does not need a holiday list.

**Actions**: New...
- Action: **Start a program**.
- Program/script: `C:\Users\ivanp\trading-agents\.venv\Scripts\python.exe`
- Add arguments: `-m pipeline.run_day --scheduled`
- Start in: `C:\Users\ivanp\trading-agents`

Only the task passes `--scheduled`. Never add `--send`: it is refused until the reconciler
(#155) and the flatten job (#135) are merged.

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

Once sending is enabled (#155, #135), the reconciler (`pipeline/reconcile.py`) runs inside
run_day every 15 seconds and before each decision. It records fills and closes, cancels an
entry still unfilled after 60 seconds, and makes sure every position has a working stop for
all of its shares. A position without one is flattened at market and alerted. Its last pass
time is in the heartbeat (`reconciled_at`). If that is more than 45 seconds old, or the
reconciler stopped, run_day starts no decision and sends nothing until it recovers.

**Check what it sees, without acting** (works with the market closed; it never sends,
cancels or closes anything, and writes nothing):

```
.venv\Scripts\python.exe -m pipeline.reconcile --check
```

**Blocks.** When the reconciler finds something it cannot square, it writes a block and
alerts, and **no new entry is allowed until you resolve every block**:

| Kind | What it means |
|---|---|
| `conflict` | The broker reported something the journal refuses (a fill after a close, an order the journal calls rejected that the broker holds ...). |
| `unreadable_order` | An open order the broker client cannot read (for example, one placed by hand). Nothing acts on it. |
| `orphan` | A position or order that is not in the journal. It is flattened, except in a symbol an earlier day's order may hold. |
| `unprotected` | A position without a working stop for all its shares. It is flattened, except an earlier day's position: that is never closed automatically (check it in the dashboard). A partial fill whose stop and take-profit Alpaca cancelled with the unfilled rest is flattened with an alert and blocks only if that close is not confirmed (closed at Alpaca and recorded with matching quantities within 60 s). |
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

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The day ended: the entry window closed, a cap was reached, or there was no session. |
| 2 | Refused to start: config, a cap, limits, `paper`, or `--send`. |
| 3 | Dry run (no `--scheduled`): nothing was called. |
| 4 | Another run_day is already running. |
| 5 | Held: an order from an earlier day may still hold a position. Nothing was closed. |

Task Scheduler shows the last exit code as "Last Run Result" (0x0, 0x2 ...).
