"""run_day: one trading day of decisions, launched by Windows Task Scheduler (Phase 5 PR 6, #156).

    python -m pipeline.run_day               # dry run: prints the day's plan, calls nothing
    python -m pipeline.run_day --scheduled   # what the scheduled task runs (billed calls)

CLAUDE.md rule 6 pre-approves run_day's Claude, FMP and Alpha Vantage calls only when the task
Ivan set up launched it, `paper` is true, and all four daily caps are enforced in code. So:
  - billed calls happen only with --scheduled, which only the task passes; without it run_day
    prints its plan and exits (NOT_SCHEDULED), touching no data source and no client;
  - it refuses to start (REFUSED) for a missing or invalid config/run_day.json or cap
    (pipeline/budget.py), an invalid limits config, or `paper` not true;
  - a broker (--send, Alpaca PAPER only) is allowed only while the reconciler (#155) and the
    flat-by job (#135) both declare RUN_DAY_READY (`broker_allowed`, enforced in code).

The day (New York time, the NYSE calendar in tools/market_calendar.py):
  1. start: the calendar coverage warning (#141), the single-instance lock, the daily budget
     (continued if run_day restarts the same day), a `run_day_start` journal entry;
  2. a weekend, holiday or uncovered year: journaled, no decisions;
  3. start-of-day check (decision 8): an order sent on an earlier day that may still hold a
     position blocks, alerts and holds the day (HELD): no decisions, nothing closed. With a
     broker (#209), so does any position or open order AT THE BROKER that no order sent
     today explains, and a broker that cannot be checked (fail closed);
  4. the scan at scan_time (09:40 by default, #131: trades and quotes are fresh by then, so
     the 120 s staleness rule applies unchanged), over the configured universe, with the
     listing (#168) and quarterly-reporting (#170) checks;
  5. a decision cycle every cycle_minutes from the day's entry start until its entry_end
     (an early close moves entry_end, tools.market_calendar.day_times): a fresh scan, then at
     most candidates_per_cycle decisions, one at a time, one per symbol per day;
  6. it stops making decisions when a cap is reached (or the budget cannot be trusted);
  7. with a broker (#248): it keeps the reconciler running until the account is flat or the
     market closes; at the flat-by (15:55 ET, 12:55 on an early close) the reconciler
     cancels today's working entries and flattens today's positions (#135). Not flat 3
     minutes before the close alerts; then a final pass, and exiting with anything still
     open alerts. It journals `run_day_end` with the day's counts.
A decision counts when it starts (run_decision's before_analysts hook, after the free SEC
check). Each ticket is asked in the watched terminal until the moment the decision was
counted + max_plan_age_s (never after the plan's expiry); no answer by then is declined.

A heartbeat file (journal/state/run_day.heartbeat.json) is rewritten at every step and, by a
background thread, at least every HEARTBEAT_S seconds of real time, during decisions too: a
heartbeat older than that means run_day stopped. Alerts are a console line and a journal
entry (desktop notifications are a follow-up).

With a broker, the reconciler (pipeline/reconcile.py) runs in a thread every reconcile.POLL_S
seconds and once before each decision, and writes its last-pass time to the heartbeat
("reconciled_at"). If that is more than reconcile.STALE_S old, or the thread died, run_day
starts no decision and refuses every send (the broker's paper-account check fails first),
and alerts. Every send is also refused from the flat-by on. One in-process lock
(tools.broker.ACTION_LOCK) wraps every broker action, so the reconciler and a decision never
act at once.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from agents.orchestrator import load_checked_limits
from journal.log import Journal, JournalError
from journal.orders import APPROVED, SEND_UNKNOWN, SUBMITTING
from pipeline.budget import (
    STATE_DIR,
    AlreadyDecided,
    CapReached,
    DailyBudget,
    RunDayConfig,
    RunDayConfigError,
    load_run_day_config,
)
from pipeline.decide import SymbolSkipped, run_decision
from pipeline.events import listing_input, reporting_check
from pipeline.filelock import LockTimeout, exclusive_file_lock
from pipeline.flatten import flat_by_reached, still_open
from pipeline.market import NY, run_scan
from pipeline.order_flow import wall_clock
from pipeline.reconcile import POLL_S as RECONCILE_POLL_S
from pipeline.reconcile import STALE_S as RECONCILE_STALE_S
from pipeline.reconcile import Reconciler
from pipeline.terminal import WatchedTerminal
from tools.atomic_write import atomic_write_text
from tools.broker import ACTION_LOCK, BrokerError
from tools.market_calendar import (
    CalendarError,
    closed_reason,
    coverage_warning,
    day_times,
    session_hours,
)
from tools.scan import ScanCriteria

OK = 0
REFUSED = 2
NOT_SCHEDULED = 3
ALREADY_RUNNING = 4
HELD = 5

HEARTBEAT_S = 30.0
CLOSE_WARNING = timedelta(minutes=3)  # not flat this long before the close: alert (#135)
# The reconciler (#155) and the flat-by job (#135): run_day refuses a broker unless both
# declare RUN_DAY_READY = True (the #199 runner-ordering note on #156).
SENDING_PREREQUISITES = ("pipeline.reconcile", "pipeline.flatten")
# Stop starting decisions when fewer tokens than this are left: a decision could not even
# make one call at the default max_tokens (16,000) doubled for a fallback.
MIN_TOKENS_TO_START = 32_000


class BrokerNotAllowed(RuntimeError):
    """run_day was given a broker before the reconciler and the flatten job are merged."""


def broker_allowed() -> tuple[bool, str]:
    """(True, "") only when every SENDING_PREREQUISITES module exists and declares
    RUN_DAY_READY = True; otherwise (False, why). The modules' own PRs set the flag."""
    for name in SENDING_PREREQUISITES:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            return False, f"{name} is not merged yet (#155 reconciler, #135 flatten job)"
        if getattr(mod, "RUN_DAY_READY", None) is not True:
            return False, f"{name} does not declare RUN_DAY_READY = True"
    return True, ""


# ------------------------------------------------------------------------------------------
# The day's state, heartbeat and journal notes
# ------------------------------------------------------------------------------------------

@dataclass
class Day:
    config: RunDayConfig
    data: Any
    clients: Any
    journal: Journal
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    terminal: WatchedTerminal
    state_dir: Path
    budget: DailyBudget | None = None
    broker: Any | None = None
    write: Callable[[str], None] = print
    phase: str = "starting"
    pulse_s: float | None = None  # the background heartbeat's period (None: HEARTBEAT_S)
    listings: dict[str, dict] = field(default_factory=dict)
    reporting: dict[str, dict] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)  # SymbolSkipped today (#170): not retried
    reconciler: Any | None = None  # with a broker: pipeline.reconcile.Reconciler (#155)
    # the reconciler's own client (verified every pass), so a decision's verify never blinds
    # it; None: it shares `broker` (tests)
    reconcile_broker: Any | None = None
    reconciled_at: datetime | None = None  # the end of its last completed pass
    reconcile_thread: Any | None = None
    pass_started_at: datetime | None = None  # a reconcile pass is under way since then
    limits: Any | None = None  # the checked limits (the flat-by guard on sends, #135)
    closed_out: bool = False  # the reconciler's final pass ran and what is open was checked

    @property
    def heartbeat_path(self) -> Path:
        return self.state_dir / "run_day.heartbeat.json"

    def note(self, status: str, **fields: Any) -> None:
        """A journal entry for the day (best effort: run_day never stops on a note)."""
        try:
            at = self.clock().astimezone(NY).isoformat()
        except Exception:  # noqa: BLE001
            at = None
        try:
            self.journal.append({"status": status, "at": at, **fields})
        except JournalError as exc:
            self.write(f"journal note failed ({status}): {exc}")

    def alert(self, message: str, **fields: Any) -> None:
        """A console ALERT line and a journal entry (decision 11; desktop: a follow-up)."""
        self.write(f"\aALERT: {message}")
        self.note("run_day_alert", message=message, **fields)

    def beat(self, phase: str | None = None) -> None:
        """Rewrite the heartbeat file (best effort)."""
        if phase:
            self.phase = phase
        counts: dict[str, Any] = {}
        if self.budget is not None:
            try:
                s = self.budget.snapshot()
                counts = {k: s[k] for k in ("decisions", "tokens_used", "fmp_requests",
                                            "av_calls")}
                counts["tokens_reserved"] = sum(s["reservations"].values())
            except Exception as exc:  # noqa: BLE001 - a heartbeat never stops on the budget
                counts = {"budget": str(exc) if isinstance(exc, CapReached)
                          else f"unreadable ({type(exc).__name__})"}
        body = {"pid": os.getpid(), "at": self.clock().astimezone(NY).isoformat(),
                "phase": self.phase, **counts}
        if getattr(self, "reconciler", None) is not None:  # with a broker (#155)
            last = getattr(self, "reconciled_at", None)
            body["reconciled_at"] = last.astimezone(NY).isoformat() if last else None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.heartbeat_path, json.dumps(body, sort_keys=True))  # #213
        except OSError:
            pass  # best effort: the next beat follows; no temp file is left behind

    def wait_until(self, when: datetime, phase: str) -> None:
        """Sleep until `when`, rewriting the heartbeat at least every HEARTBEAT_S."""
        self.beat(phase)
        while True:
            left = (when - self.clock()).total_seconds()
            if left <= 0:
                return
            self.sleep(min(left, HEARTBEAT_S))
            self.beat()


# ------------------------------------------------------------------------------------------
# Scan and decisions
# ------------------------------------------------------------------------------------------

def scan_universe(day: Day, limits: Any) -> dict:
    """run_scan over the universe at clock(), with the listing (#168) and quarterly-reporting
    (#170) maps. Both are cached for the day: neither changes intraday."""
    now = day.clock()
    syms = list(day.config.universe)
    try:
        snaps = day.data.snapshots(syms)
    except Exception as exc:  # noqa: BLE001 - a data error is an empty scan, never a crash
        day.note("run_day_scan", ok=False, reason=f"snapshots failed ({type(exc).__name__})")
        return {"candidates": [], "rejected": []}
    listings, reporting = {}, {}
    listing_down = sec_down = False  # after one failed request, ask no more this scan
    for s in syms:
        if s in day.listings:
            listings[s] = day.listings[s]
        elif listing_down:
            listings[s] = {"ok": False, "reason": "listing lookups stopped for this scan"}
        else:
            listings[s] = listing_input(day.data, s)
            if listings[s].get("fetch_failed") and not str(
                    listings[s].get("reason", "")).startswith("HTTP 404"):
                listing_down = True  # a failed lookup is asked again at the next scan
            elif not listings[s].get("fetch_failed"):
                day.listings[s] = listings[s]
        if s in day.reporting:
            reporting[s] = day.reporting[s]
        elif sec_down:
            reporting[s] = {"covered": False, "reason": "SEC lookups stopped for this scan"}
        else:
            reporting[s] = reporting_check(day.data, s, now)
            if reporting[s].get("fetch_failed"):
                sec_down = True
            else:
                day.reporting[s] = reporting[s]
    out = run_scan(snaps, ScanCriteria(), limits, now, listings=listings,
                   reporting=reporting)
    day.note("run_day_scan", ok=True, candidates=[c["symbol"] for c in out["candidates"]],
             rejected=len(out["rejected"]))
    return out


def _stop_reason(budget: DailyBudget) -> str | None:
    """Why no further decision may start, or None."""
    try:
        s = budget.snapshot()
    except CapReached as exc:
        return str(exc)
    if s["decisions"] >= budget.caps.max_decisions:
        return f"decision cap reached ({budget.caps.max_decisions} today)"
    left = budget.caps.max_tokens - budget.tokens_committed(s)
    if left < MIN_TOKENS_TO_START:
        return f"token cap: {left} tokens left, fewer than one call needs"
    return None


def decide_one(day: Day, symbol: str, limits: Any) -> str | None:
    """One decision for `symbol`. Returns a reason to stop the day (a cap), or None."""
    assert day.budget is not None
    start = day.clock()
    age = timedelta(seconds=limits.max_plan_age_s)
    # The plan-age clock starts after the free SEC check, when the decision is counted: the
    # ticket's deadline counts from there, never later than the plan's expiry.
    due = {"at": start + age}
    ask = day.terminal.asker(lambda: due["at"], day.clock)
    budget = day.budget
    day.terminal.last_timed_out = False  # S4: never carried over from the last decision

    def count_it() -> None:
        budget.start_decision(symbol)  # CapReached: no billed call is made
        due["at"] = day.clock() + age
        day.beat(f"deciding {symbol}")

    day.write(f"\n=== decision: {symbol} at {start.astimezone(NY):%H:%M:%S} ET ===")
    try:
        result = run_decision(symbol, data=day.data, clients=day.clients, journal=day.journal,
                              clock=day.clock, ask=ask,
                              broker=GuardedBroker(day) if day.broker is not None else None,
                              before_analysts=count_it)
    except SymbolSkipped as exc:  # #170: not a decision, nothing counted, not retried today
        day.skipped.add(symbol)
        day.write(f"{symbol} skipped: {exc.reason}")
        return None
    except AlreadyDecided:
        return None
    except CapReached as exc:
        day.note("run_day_cap", reason=str(exc))
        return str(exc)
    except ValueError as exc:  # a config or validation problem: stop the day, safely
        day.alert(f"decision for {symbol} stopped ({type(exc).__name__}): {exc}")
        return f"stopped: {type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001 - one decision's crash never ends the day
        day.alert(f"decision for {symbol} failed ({type(exc).__name__})", symbol=symbol)
        return None
    if day.terminal.last_timed_out:
        day.note("run_day_approval_timeout", symbol=symbol, run_id=result.ledger_run_id)
    if getattr(result, "record_error", None):
        day.alert(f"{symbol}: {result.record_error}", symbol=symbol)
    outcome = getattr(result, "outcome", None)
    if outcome is not None:
        day.write(f"{symbol}: {outcome.status}" + (f" ({outcome.reason})"
                                                   if outcome.reason else ""))
    return None


# ------------------------------------------------------------------------------------------
# The reconciler (#155): a thread, its health, and the guard on sends
# ------------------------------------------------------------------------------------------

def reconcile_pass(day: Day, *, final: bool = False) -> bool:
    """One reconcile pass under the broker's action lock; records its end time. False when it
    failed (the reconciler's own alerts say why; an unexpected error is alerted here)."""
    if day.reconciler is None:
        return True
    try:
        with ACTION_LOCK:
            day.pass_started_at = day.clock()
            try:
                report = day.reconciler.pass_once(final=final)
            finally:
                day.pass_started_at = None
    except Exception as exc:  # noqa: BLE001 - never kill the thread; staleness tells run_day
        day.alert(f"the reconcile pass failed ({type(exc).__name__}): {exc}")
        return False
    if not report.get("complete"):
        return False  # it could not see everything (it alerted): not a pass for health
    day.reconciled_at = day.clock()
    day.beat()
    return True


def reconcile_problem(day: Day) -> str | None:
    """Why new entries must stop because the reconciler is not watching, or None. No broker:
    nothing to watch."""
    if day.reconciler is None:
        return None
    thread = day.reconcile_thread
    if thread is not None and not thread.is_alive():
        return "the reconciler thread has stopped"
    last = day.reconciled_at
    if last is None:
        return "the reconciler has not completed a pass yet"
    age = (day.clock() - last).total_seconds()
    if age > RECONCILE_STALE_S:
        return f"the reconciler's last pass was {int(age)} s ago (more than {RECONCILE_STALE_S})"
    return None


class _Reconcile(threading.Thread):
    """Runs a reconcile pass every `period` (reconcile.POLL_S) seconds."""

    def __init__(self, day: Day, period: float = RECONCILE_POLL_S) -> None:
        super().__init__(daemon=True, name="run-day-reconcile")
        self.day, self.period = day, period
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            reconcile_pass(self.day)
            if self._stop_event.wait(self.period):
                return

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=30)


class GuardedBroker:
    """The broker a decision sends through: its paper-account check (which pipeline/send.py
    makes before any POST) refuses while the reconciler is not watching, so nothing is sent
    then (the order is journaled not sent). Everything else passes through."""

    def __init__(self, day: Day) -> None:
        self._day = day

    def verify_paper_account(self) -> dict:
        why = reconcile_problem(self._day)
        limits = self._day.limits
        if why is None and limits is not None and flat_by_reached(self._day.clock(), limits):
            why = "the flat-by time has passed: no new entries (#135)"
        if why is not None:
            self._day.alert(f"send refused: {why}")
            raise BrokerError(f"send refused: {why}")
        return self._day.broker.verify_paper_account()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._day.broker, name)


def held_from_earlier_days(day: Day, now: datetime) -> list[str]:
    """Start-of-day check (decision 8): client_order_ids of orders SENT on an earlier day
    that may still hold a position. Unsent approvals past their plan are expired first."""
    day.journal.expire_unsent(now)
    today = now.astimezone(NY).date()
    return [cid for cid, o in day.journal.orders().items()
            if o.open and o.sent and o.status != APPROVED
            and o.approved_at.astimezone(NY).date() < today]


EARLIER_DAY = ("from an earlier day: not closed automatically (decision 8). Check it in the "
               "Alpaca paper dashboard, then resolve the block: python -m journal.resolve")


def block(day: Day, kind: str, key: str, reason: str, **fields: Any) -> None:
    """Open a reconcile block (once per key): every new entry stops until Ivan resolves it
    (risk_check). This is decision 8's kill switch. A block that cannot be written alerts;
    the day is held either way."""
    try:
        day.journal.add_block(kind, key, reason, day.clock(), **fields)
    except JournalError as exc:
        day.alert(f"could not journal a block ({kind}: {reason}): {exc}")


def block_held(day: Day, held: list[str]) -> None:
    """Decision 8: block each order from an earlier day that may still hold a position (the
    same key the reconciler uses for one, so never twice)."""
    try:
        orders = day.journal.orders()
    except JournalError:
        orders = {}
    for cid in held:
        sym = orders[cid].symbol if cid in orders else None
        block(day, "unprotected", f"earlier-day:{cid}",
              f"{sym}: an order sent on an earlier day ({cid}) may still hold a position, "
              + EARLIER_DAY, symbol=sym, client_order_id=cid)


def broker_start_check(day: Day, today: date) -> tuple[list[str], list[str]] | None:
    """#209: before the reconciler starts, what AT THE BROKER no order sent today explains:
    a position or an open order (from an earlier day, or placed by hand), or an open order
    that cannot be read. Each is blocked. Returns (those, today's positions), ([], ...) when
    clean; None when the broker cannot be checked (the caller holds the day: fail closed).
    Reads only: nothing is closed or cancelled (decision 8). The account flags are the
    paper-account check's.

    It matches by symbol: on a restart, something left from an earlier day in a symbol also
    traded today passes here; the reconciler's quantity and orphan checks judge it then."""
    broker = day.reconcile_broker or day.broker
    try:
        with ACTION_LOCK:
            broker.verify_paper_account()  # active, not blocked or suspended, no shorting
            positions = broker.list_positions()
            listing = broker.list_open_orders_checked()
        orders = day.journal.orders()
    except (BrokerError, JournalError) as exc:
        day.alert(f"the start-of-day broker check failed ({exc}): no decisions today "
                  "(fail closed)")
        return None
    sent_today = {cid: o for cid, o in orders.items()
                  if o.sent and o.approved_at.astimezone(NY).date() == today}
    symbols_today = {o.symbol for o in sent_today.values()}
    found: list[str] = []
    for pos in positions:
        if pos["symbol"] not in symbols_today:
            what = f"a {pos.get('side')} position in {pos['symbol']} ({pos.get('qty')} shares)"
            found.append(what)
            block(day, "orphan", f"start-of-day:{pos['symbol']}",
                  f"{what} at the start of the day, not from an order sent today: "
                  + EARLIER_DAY, symbol=pos["symbol"])
    for o in listing["orders"]:
        if o["symbol"] not in symbols_today and o.get("client_order_id") not in sent_today:
            what = f"an open {o['type']} {o['side']} order in {o['symbol']} ({o['id']})"
            found.append(what)
            block(day, "orphan", f"start-of-day-order:{o['id']}",
                  f"{what} at the start of the day, not from an order sent today: "
                  + EARLIER_DAY, symbol=o["symbol"])
    for u in listing["unreadable"]:
        what = f"an open order that cannot be read (id {u.get('id')}, symbol {u.get('symbol')})"
        found.append(what)
        block(day, "unreadable_order",
              f"unreadable:{u.get('id') or u.get('symbol') or u.get('why')}",
              f"{what} at the start of the day: " + EARLIER_DAY, symbol=u.get("symbol"))
    todays = [f"{p['symbol']} position ({p.get('qty')} shares)" for p in positions
              if p["symbol"] in symbols_today]
    return found, todays


def _held_unwatched(day: Day, todays: list[str] | None = None) -> None:
    """A held day starts no reconciler, so nothing watches or flattens today's positions (a
    restart): say so (#135 review). None: the broker could not be read, so say it may hold
    some."""
    if todays == []:
        return
    what = ("; ".join(todays) if todays else
            "any position of today's (the broker could not be read)")
    day.alert(f"held: run_day will not watch or flatten {what}. MANUAL ACTION NEEDED: check "
              "the Alpaca paper dashboard, or run python -m pipeline.flatten --now")


def _unsettled_sends(day: Day) -> list[str]:
    """Orders whose send may still reach the broker (submitting, send unknown): never flat
    while one is (the reconciler settles each within its lost-send grace). Unreadable
    journal: one line saying so."""
    try:
        orders = day.journal.orders()
    except JournalError:
        return ["the journal cannot be read"]
    return [f"a send of {o.symbol} not settled yet ({o.status})" for o in orders.values()
            if o.status in (SUBMITTING, SEND_UNKNOWN)]


def started_today(journal: Journal, today: date) -> bool:
    return any(e.get("status") == "run_day_start" and e.get("day") == today.isoformat()
               for e in journal.entries())


def skipped_today(journal: Journal, today: date) -> set[str]:
    """Symbols run_decision skipped today before any analyst (#170), from the journal, so a
    restarted run_day does not check them again."""
    out = set()
    for e in journal.entries():
        at = e.get("checked_at")
        if e.get("status") == "skipped" and isinstance(at, str) and isinstance(
                e.get("symbol"), str):
            try:
                if datetime.fromisoformat(at).astimezone(NY).date() == today:
                    out.add(e["symbol"])
            except ValueError:
                continue
    return out


# ------------------------------------------------------------------------------------------
# The day
# ------------------------------------------------------------------------------------------

def run_day(day: Day, *, limits: Any | None = None) -> int:
    """Run one trading day. Returns an exit code (OK, REFUSED, ALREADY_RUNNING, HELD)."""
    if day.broker is not None:
        allowed, why = broker_allowed()
        if not allowed:
            day.write(f"refused: no broker until the reconciler and flatten job are merged: "
                      f"{why}")
            return REFUSED
    try:
        limits = load_checked_limits(limits)
    except ValueError as exc:
        day.write(f"refused: {exc}")
        return REFUSED
    if limits.paper is not True:
        day.write("refused: paper must be true (CLAUDE.md rule 6)")
        return REFUSED
    day.limits = limits
    now = day.clock()
    today = now.astimezone(NY).date()
    warning = coverage_warning(today)  # #141: on the real path, every run
    if warning:
        day.alert(f"calendar: {warning}")
    try:
        with exclusive_file_lock(day.state_dir / "run_day.lock", timeout_s=0):
            return _locked_day(day, limits, now, today)
    except LockTimeout:
        day.write("another run_day is already running: exiting")
        return ALREADY_RUNNING


def _locked_day(day: Day, limits: Any, now: datetime, today: date) -> int:
    # S5: ONE budget for the run. The live build made it for the metered client and data;
    # if the New York day changed since, refuse rather than count against two days.
    if day.budget is None:
        day.budget = DailyBudget(day.config.caps, today, day.state_dir)
    elif day.budget.day != today:
        day.write("refused: the New York day changed during start-up; start again")
        return REFUSED
    elif Path(day.budget.state_dir).resolve() != Path(day.state_dir).resolve():
        day.write("refused: the budget and the day use different state folders")
        return REFUSED
    budget = day.budget
    try:
        budget.open_day(started_today=started_today(day.journal, today))
    except CapReached as exc:
        day.alert(f"daily budget closed: {exc}")
        day.note("run_day_end", day=today.isoformat(), reason=str(exc))
        return OK
    except JournalError as exc:
        day.write(f"refused: journal unreadable: {exc}")
        return REFUSED
    try:
        day.skipped |= skipped_today(day.journal, today)
        # S6: the day-started marker is REQUIRED: without it, a lost budget file could
        # later read as a fresh day.
        day.journal.append({"status": "run_day_start", "day": today.isoformat(),
                            "at": day.clock().astimezone(NY).isoformat(), "pid": os.getpid(),
                            "caps": vars(day.config.caps),
                            "universe": list(day.config.universe)})
    except JournalError as exc:
        day.write(f"\aALERT: refused: the run_day_start entry could not be journaled: {exc}")
        return REFUSED
    pulse = _Pulse(day)
    pulse.start()
    if day.broker is not None:  # the thread starts after the held check (_the_day)
        # its alerts through run_day's console, with the bell (the reconciler journals
        # them itself as reconcile_alert)
        day.reconciler = Reconciler(day.journal, day.reconcile_broker or day.broker,
                                    clock=day.clock, limits=limits,
                                    alert=lambda m: day.write(f"\aALERT: {m}"))
    try:
        return _the_day(day, limits, now, today, budget)
    except CapReached as exc:  # S9: a budget doubt anywhere ends the day cleanly
        return _end(day, today, str(exc))
    except Exception as exc:  # noqa: BLE001 - S9: never exit without run_day_end
        day.alert(f"run_day stopped on an unexpected error ({type(exc).__name__})")
        return _end(day, today, f"error: {type(exc).__name__}")
    finally:
        close_out(day)  # never on a held day: the thread never started
        pulse.stop()


class _Pulse(threading.Thread):
    """Rewrites the heartbeat every day.pulse_s (default HEARTBEAT_S) seconds of real time,
    so a decision (analysts, debate, Trader, up to max_plan_age_s of approval) never looks
    like a stopped run."""

    def __init__(self, day: Day) -> None:
        super().__init__(daemon=True, name="run-day-heartbeat")
        self.day = day
        self._stop_event = threading.Event()

    def run(self) -> None:
        period = self.day.pulse_s if self.day.pulse_s is not None else HEARTBEAT_S
        while not self._stop_event.wait(period):
            self.day.beat()

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


def _the_day(day: Day, limits: Any, now: datetime, today: date, budget: DailyBudget) -> int:
    day.beat("started")
    try:
        hours = session_hours(today)
    except CalendarError as exc:
        day.alert(f"calendar: {exc}")
        return _end(day, today, "calendar does not cover today")
    if hours is None:
        day.write(f"no session today ({closed_reason(today)})")
        return _end(day, today, f"no session ({closed_reason(today)})")
    try:
        held = held_from_earlier_days(day, now)
    except JournalError as exc:
        day.alert(f"journal unreadable at the start of day: {exc}")
        return HELD
    if held:
        block_held(day, held)
        day.alert("an order from an earlier day may still hold a position: no decisions "
                  "today until Ivan checks it (nothing is closed automatically)", orders=held)
        _end(day, today, "held: position from an earlier day")
        return HELD
    if day.reconciler is not None:  # not held: check the broker, then watch (#209, #155)
        checked = broker_start_check(day, today)
        if checked is None:
            _held_unwatched(day)
            _end(day, today, "held: the start-of-day broker check failed")
            return HELD
        found, todays = checked
        if found:
            day.alert("at the broker, not from an order sent today: " + "; ".join(found)
                      + ". No decisions today until Ivan checks it (nothing is closed "
                      "automatically)")
            _held_unwatched(day, todays)
            _end(day, today, "held: the broker holds something from an earlier day")
            return HELD
        day.reconcile_thread = _Reconcile(day)
        day.reconcile_thread.start()
    entry_start, entry_end, _flat = day_times(today, limits)
    at = lambda t: datetime.combine(today, t, tzinfo=NY)  # noqa: E731
    scan_at, end_at = at(day.config.scan_time), at(entry_end)
    if day.clock() < scan_at:
        day.wait_until(scan_at, "waiting for the scan")
    if day.clock() < end_at:
        day.beat("scan")
        scan_universe(day, limits)  # #131: 09:40, trades and quotes fresh
    cycle = at(entry_start)
    step = timedelta(minutes=day.config.cycle_minutes)
    while cycle < end_at:
        if day.clock() < cycle:
            day.wait_until(cycle, "waiting for the next cycle")
        if day.clock() >= end_at:
            break
        stop = _stop_reason(budget)
        if stop:
            return _end(day, today, stop)
        day.beat("cycle scan")
        picked = [c["symbol"] for c in scan_universe(day, limits)["candidates"]
                  if c["symbol"] not in day.skipped
                  and not budget.decided(c["symbol"])][:day.config.candidates_per_cycle]
        for sym in picked:
            if day.clock() >= end_at:
                break
            if day.reconciler is not None:  # a pass before each decision, then its health
                reconcile_pass(day)
                why = reconcile_problem(day)
                if why is not None:
                    day.alert(f"no new decision: {why}")
                    break  # the next cycle tries again; the thread keeps watching
            stop = _stop_reason(budget) or decide_one(day, sym, limits)
            if stop:
                return _end(day, today, stop)
        while cycle <= day.clock():  # the next cycle after a long decision
            cycle += step
    return _end(day, today, "entry window closed")


def watch_until_flat(day: Day, today: date) -> None:
    """#248: after the decisions, keep the reconciler running (it flattens at the flat-by,
    #135) until the account is flat or the market closes, keeping its health rule (a pass
    older than STALE_S or a dead thread alerts; run_day then makes the pass itself). Not
    flat CLOSE_WARNING before the close alerts. Flat, or at the close: close_out (the final
    pass records the last closes before run_day_end is written). Never raises."""
    if day.reconcile_thread is None or day.closed_out:
        return
    try:
        hours = session_hours(today)
    except CalendarError:
        hours = None
    close_at = datetime.combine(today, hours[1], tzinfo=NY) if hours else None
    warned = False
    day.beat("watching until flat")
    while True:
        try:
            left = still_open(day.reconcile_broker or day.broker)
            if left is not None:
                left += _unsettled_sends(day)
            if left == []:
                day.write("the paper account is flat: no position and no open order")
                break
            now = day.clock()
            if close_at is None or now >= close_at:
                break
            if not warned and now >= close_at - CLOSE_WARNING:
                warned = True
                day.alert("not flat 3 minutes before the close: " + (
                    "; ".join(left) if left is not None else "the broker cannot be read"))
            why = reconcile_problem(day)
            running = day.pass_started_at
            if why is not None and running is not None and (
                    day.clock() - running).total_seconds() <= RECONCILE_STALE_S:
                why = None  # a long pass (a flatten waits for its fills) is not a stale one
            if why is not None:
                day.alert(f"after the entry window: {why}; run_day makes the pass itself")
                reconcile_pass(day)
            day.sleep(max(1.0, min(float(RECONCILE_POLL_S),
                                   (close_at - day.clock()).total_seconds())))
            day.beat()
        except Exception as exc:  # noqa: BLE001 - the watch never ends the day early
            day.alert(f"watching until flat: {type(exc).__name__}: {exc}")
            if close_at is None or day.clock() >= close_at:
                break  # never past the close, whatever keeps failing
            day.sleep(float(RECONCILE_POLL_S))
    close_out(day)


def close_out(day: Day) -> None:
    """Stop the reconcile thread, make one last (final) pass, and alert if anything is still
    open at the broker: run_day exits with it (MANUAL ACTION NEEDED). Once."""
    if day.reconcile_thread is None or day.closed_out:
        return
    day.closed_out = True
    try:
        day.reconcile_thread.stop()
        reconcile_pass(day, final=True)  # one last pass at the end of the day
        left = still_open(day.reconcile_broker or day.broker)
    except Exception as exc:  # noqa: BLE001 - the exit check is always made
        day.alert(f"the last reconcile pass failed ({type(exc).__name__}): {exc}")
        left = None
    if left:
        day.alert("run_day exits with this still open at the broker: " + "; ".join(left)
                  + ". MANUAL ACTION NEEDED in the Alpaca paper dashboard (tomorrow it is an "
                  "earlier day's position: held, never closed automatically)", open=left)
    elif left is None:
        day.alert("run_day exits and the broker cannot be read: check the Alpaca paper "
                  "dashboard for open positions and orders")


def _end(day: Day, today: date, reason: str) -> int:
    watch_until_flat(day, today)  # #248: with a broker, never before flat or the close
    counts: dict[str, Any] = {}
    if day.budget is not None:
        try:
            s = day.budget.snapshot()
            counts = {k: s[k] for k in ("decisions", "tokens_used", "fmp_requests", "av_calls")}
        except Exception as exc:  # noqa: BLE001 - the end entry is always written
            counts = {"budget": str(exc) if isinstance(exc, CapReached)
                      else f"unreadable ({type(exc).__name__})"}
    day.note("run_day_end", day=today.isoformat(), reason=reason, **counts)
    day.write(f"run_day ends: {reason} {counts}")
    day.beat("ended")
    return OK


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------

def _plan(config: RunDayConfig, now: datetime) -> str:
    c = config.caps
    return (f"run_day dry run ({now.astimezone(NY):%Y-%m-%d %H:%M} ET): scan at "
            f"{config.scan_time:%H:%M}, a cycle every {config.cycle_minutes} min, at most "
            f"{config.candidates_per_cycle} decisions per cycle; caps: {c.max_decisions} "
            f"decisions, {c.max_tokens} tokens, FMP {c.max_fmp_requests}, Alpha Vantage "
            f"{c.max_av_calls}; universe {len(config.universe)} symbols. Nothing was called. "
            "The scheduled task passes --scheduled.")


def main(argv: list[str] | None = None, *, clock: Callable[[], datetime] = wall_clock,
         build: Callable[..., Day] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline.run_day", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scheduled", action="store_true",
                   help="make billed calls (only the Windows Task Scheduler task passes it)")
    p.add_argument("--send", action="store_true",
                   help="send approved orders to the Alpaca PAPER account (refused unless the "
                        "reconciler and the flat-by job declare RUN_DAY_READY)")
    args = p.parse_args(argv)
    try:
        config = load_run_day_config()
    except RunDayConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    if args.send:
        allowed, why = broker_allowed()
        if not allowed:
            print(f"refused: --send needs the reconciler and the flatten job: {why}",
                  file=sys.stderr)
            return REFUSED
    if not args.scheduled:
        print(_plan(config, clock()))
        return NOT_SCHEDULED
    day = (build or _build_live)(config, clock=clock, send=args.send)
    return run_day(day)


def _build_live(config: RunDayConfig, *, clock: Callable[[], datetime], send: bool) -> Day:
    """The real day: env keys from .env, the metered client and data, the journal. Only
    reached with --scheduled (the task)."""
    from pipeline.metering import MeteredClient, metered_live_data
    from pipeline.smoke_common import ROOT, load_dotenv

    if (ROOT / ".env").is_file():
        load_dotenv(ROOT / ".env")  # keys only from the environment / .env, never logged
    today = clock().astimezone(NY).date()
    budget = DailyBudget(config.caps, today, STATE_DIR)
    from agents.llm import make_client
    client = MeteredClient(make_client(max_retries=0), budget)
    broker = reconcile_broker = None
    if send:  # main refuses --send unless broker_allowed()
        from tools.broker import AlpacaPaperBroker
        broker = AlpacaPaperBroker(load_checked_limits())
        reconcile_broker = AlpacaPaperBroker(load_checked_limits())  # its own client
    # ONE budget (S5): the metered client, the metered data and the day's loop share it
    return Day(config=config, data=metered_live_data(budget), clients=client,
               journal=Journal(), clock=clock, sleep=time.sleep, terminal=WatchedTerminal(),
               state_dir=STATE_DIR, broker=broker, budget=budget,
               reconcile_broker=reconcile_broker)


if __name__ == "__main__":
    sys.exit(main())
