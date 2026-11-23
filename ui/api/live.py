"""GET /api/live: the Live run page (UI-5, #218). Read-only, built fresh on every request.

Sources, each read without a lock:
  - the heartbeat (journal/state/run_day.heartbeat.json), through read_state_file: its age,
    and stale when more than HEARTBEAT_STALE_S old or dated in the future;
  - today's budget (journal/state/budget-YYYY-MM-DD.json): ONE read_state_file call per
    request (one short open_shared read, closed before anything else is built). It is never
    held open: a budget write blocked past its 0.5 s retry stops run_day's day;
  - the journal, through the app's JournalReader. The day's totals come from
    SnapshotJournal.day_state, the code risk_check's DayState comes from, so "money at risk",
    "open positions" and "trades today" are the numbers risk_check uses;
  - the limits (tools.config.load_limits) and run_day's caps (pipeline.budget's
    load_run_day_config, next to CEILINGS), through ui/api/readers/config.py;
  - the earnings caches (journal/cache/): only whether today's file is in the folder's
    listing. No cache file is ever opened.

Status is one sentence from a fixed template, never model text:
  - not_running: run_day has not started today (no run_day_start today and no heartbeat
    from today). Outside market hours this is the calm, ordinary state;
  - blocked: run_day started today and something stops new entries: a stale or future
    heartbeat, a run_day_end reason other than a normal end, an exhausted or unreadable
    budget, the kill switch, a held day, a journal that does not replay, an earnings calendar
    that can no longer be fetched today;
  - idle: run_day started today but the market is closed, the scan has not run yet, or the
    day ended normally;
  - normal: running, nothing stops new entries;
  - order_waiting needs the approval ticket in the record (#212): never returned yet, and
    `ticket` is always null.
A reason that is not the status (for example the kill switch while run_day is not running)
is a problem, so nothing is hidden.

Money values are passed through as recorded or as DayState holds them (Decimal, shown with
str()). The "reached" flags and the bar widths compare recorded numbers with their limits,
for display only: nothing here decides money.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from journal.log import JournalError
from journal.orders import (
    APPROVED,
    SEND_UNKNOWN,
    SUBMITTED,
    SUBMITTING,
    OrderError,
    client_order_id,
)
from tools.market_calendar import CalendarError, closed_reason, session_hours
from ui.api.detail import cap
from ui.api.history import decisions
from ui.api.readers.config import Loaded, ceilings
from ui.api.readers.journal_snapshot import JournalSnapshot, SnapshotJournal
from ui.api.readers.state import (
    HEARTBEAT_STALE_S,
    StateFile,
    budget_path,
    heartbeat_age_s,
    heartbeat_path,
    read_state_file,
)

NY = ZoneInfo("America/New_York")
DEFAULT_SCAN = time(9, 40)  # pipeline.budget.RunDayConfig.scan_time's default
# run_day_end reasons of a normal day: pipeline/run_day.py _the_day's literals (a test reads
# them there, so a reworded reason fails it)
NORMAL_ENDS = ("entry window closed", "no session (")
CAPS_KEYS = ("decisions", "tokens_used", "fmp_requests", "av_calls")
SYMBOL_PHASE = re.compile(r"deciding ([A-Z][A-Z0-9.\-]{0,9})")  # Day.beat(f"deciding {sym}")
HEARTBEAT_SKEW_S = 2  # the clock is read before the heartbeat: allow this much "future"
NOT_FILLED = frozenset({SUBMITTING, SUBMITTED, SEND_UNKNOWN})
TEXT = 300


def cache_dir(journal: Path) -> Path:
    """pipeline.events.CACHE_DIR, next to the journal (not imported: pipeline.events loads
    the network modules)."""
    return Path(journal).parent / "cache"


# ---- the budget --------------------------------------------------------------------------

@dataclass(frozen=True)
class Budget:
    """Today's budget as read once: `data` when valid, else why not."""

    exists: bool | None
    data: dict | None
    error: str | None


def _valid_budget(s: dict, today: date) -> bool:
    """The same checks as pipeline.budget._valid_state (not importable: ui/api imports only
    load_run_day_config and CEILINGS from pipeline.budget)."""
    if s.get("day") != today.isoformat():
        return False
    for k in CAPS_KEYS:
        v = s.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return False
    res = s.get("reservations")
    if not isinstance(res, dict) or any(
            isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in res.values()):
        return False
    syms = s.get("symbols")
    return (isinstance(syms, list) and all(isinstance(x, str) for x in syms)
            and (s.get("exhausted") is None or isinstance(s.get("exhausted"), str)))


def read_budget(state_dir: Path, today: date) -> Budget:
    """ONE read_state_file call: one open_shared read, closed before this returns."""
    f = read_state_file(budget_path(state_dir, today))
    if not f.exists:
        return Budget(False, None, None)
    if f.error or f.data is None:
        return Budget(True, None, f"today's budget file is {f.error or 'unusable'}")
    if not _valid_budget(f.data, today):
        return Budget(True, None, "today's budget file is invalid")
    return Budget(True, f.data, None)


# ---- the journal's day notes -------------------------------------------------------------

def _ny(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text)
        return when.astimezone(NY) if when.tzinfo is not None else None
    except (ValueError, OverflowError):  # OverflowError: a time at the date limits
        return None


@dataclass
class DayNotes:
    """run_day's journal notes for today: the latest start, a later end, the latest scan."""

    started: dict | None = None
    ended: dict | None = None
    scan: dict | None = None
    held_alert: bool = False


def day_notes(entries: tuple[dict, ...], today: date) -> DayNotes:
    notes = DayNotes()
    iso = today.isoformat()
    for e in entries:
        status = e.get("status")
        if not (isinstance(status, str) and status.startswith("run_day_")):
            continue
        at = _ny(e.get("at"))
        on_today = e.get("day") == iso if "day" in e else (at is not None and at.date() == today)
        if not on_today:
            continue
        if status == "run_day_start":
            # a restart: only a later end counts, and its own held check decides again
            notes.started, notes.ended, notes.held_alert = e, None, False
        elif status == "run_day_end":
            notes.ended = e  # also an end with no start (the budget closed at opening)
        elif status == "run_day_scan":
            notes.scan = e
        elif status == "run_day_alert" and e.get("orders"):
            notes.held_alert = True
    return notes


# ---- limits ------------------------------------------------------------------------------

def _limit(key: str, label: str, unit: str, used: object, limit: object,
           note: str | None = None, reached: bool | None = None) -> dict:
    """One limit as shown: used and limit as strings exactly as recorded ("—" not as None:
    null means not known), reached and the bar's width from those numbers (display only)."""
    pct = None
    if isinstance(used, (int, Decimal)) and isinstance(limit, (int, Decimal)) and limit > 0:
        if reached is None:
            reached = used >= limit
        pct = int(min(Decimal(100), Decimal(used) * 100 / Decimal(limit)))
    return {"key": key, "label": label, "unit": unit,
            "used": None if used is None else str(used),
            "limit": None if limit is None else str(limit),
            "reached": bool(reached), "pct": pct, "note": note}


def build_limits(day_state: Any, limits: Any, caps: Any, budget: dict | None
                 ) -> tuple[list[dict], list[dict]]:
    b = budget or {}
    ds = day_state
    key = [
        _limit("decisions", "AI decisions today", "count", b.get("decisions"),
               caps.max_decisions if caps else None),
        _limit("money_at_risk", "Money at risk today", "usd",
               ds.risk_used_today if ds else None, limits.daily_loss_limit if limits else None),
        _limit("open_positions", "Open positions", "count", ds.open_positions if ds else None,
               limits.max_open_positions if limits else None),
    ]
    reserved = sum(b["reservations"].values()) if budget else 0
    tokens_reached = (budget is not None and caps is not None
                      and b["tokens_used"] + reserved >= caps.max_tokens)
    more = [
        _limit("tokens", "AI usage (tokens)", "tokens", b.get("tokens_used"),
               caps.max_tokens if caps else None,
               note=f"{reserved} more reserved for calls in progress" if reserved else None,
               reached=tokens_reached),
        _limit("av_calls", "Earnings calendar calls (Alpha Vantage)", "count",
               b.get("av_calls"), caps.max_av_calls if caps else None),
        _limit("fmp_requests", "FMP data calls", "count", b.get("fmp_requests"),
               caps.max_fmp_requests if caps else None),
        _limit("trades", "Trades today", "count", ds.trades_today if ds else None,
               limits.max_trades_per_day if limits else None),
    ]
    return key, more


# ---- data sources ------------------------------------------------------------------------

def _cache_names(folder: Path) -> tuple[set[str] | None, str | None]:
    """The names in journal/cache/, from the listing only: no cache file is opened."""
    try:
        with os.scandir(folder) as it:
            return {e.name for e in it}, None
    except FileNotFoundError:
        return set(), None
    except OSError as exc:
        return None, f"the cache folder cannot be listed ({type(exc).__name__})"


def _calendar(name: str, prefix: str, used: int | None, limit: int | None,
              names: set[str] | None, today: date, *, blocks: bool) -> dict:
    have = None if names is None else f"{prefix}-{today.isoformat()}.json" in names
    of = f"{used} of {limit} calls today" if used is not None and limit else None
    if have:
        return {"name": name, "ok": True,
                "detail": "today's calendar is in" + (f" ({of})" if of else "")}
    if used is None or names is None:
        return {"name": name, "ok": None, "detail": "not known: "
                + ("the budget cannot be read" if used is None else "the cache folder "
                   "cannot be listed")}
    if used == 0:
        return {"name": name, "ok": None, "detail": "not called yet today"}
    if limit is not None and used >= limit:
        why = ("the event check cannot clear, so every new entry is blocked today" if blocks
               else "it only ever blocks, so its absence clears nothing")
        return {"name": name, "ok": False, "detail": f"no valid calendar today, and all "
                f"{limit} calls are used: {why}"}
    return {"name": name, "ok": False, "detail": f"no valid calendar yet ({of})"}


def build_sources(notes: DayNotes, budget: dict | None, caps: Any,
                  names: set[str] | None, today: date) -> dict:
    scan = notes.scan
    if scan is None:
        alpaca = {"name": "Prices (Alpaca)", "ok": None, "detail": "no scan yet today"}
    elif scan.get("ok") is True:
        at = _ny(scan.get("at"))
        alpaca = {"name": "Prices (Alpaca)", "ok": True,
                  "detail": "last scan ok" + (f" at {at:%H:%M} ET" if at else "")}
    else:
        alpaca = {"name": "Prices (Alpaca)", "ok": False,
                  "detail": "last scan failed: " + (cap(scan.get("reason"), TEXT)
                                                    or "no reason recorded")}
    b = budget or {}
    items = [
        alpaca,
        _calendar("Earnings calendar (Alpha Vantage)", "av-earnings", b.get("av_calls"),
                  caps.max_av_calls if caps else None, names, today, blocks=True),
        _calendar("Earnings calendar (FMP, block-only)", "fmp-earnings",
                  b.get("fmp_requests"), caps.max_fmp_requests if caps else None, names,
                  today, blocks=False),
    ]
    failed = [i for i in items if i["ok"] is False]
    if failed:
        names = " and ".join(i["name"] for i in failed)
        summary = f"Not working: {names}. Details below."
    elif all(i["ok"] is True for i in items):
        summary = f"All {len(items)} sources OK"
    else:
        used = sum(1 for i in items if i["ok"] is True)
        summary = f"{used} of {len(items)} sources used today, none failed"
    return {"ok": not failed, "summary": summary, "expanded": bool(failed), "items": items}


# ---- positions ---------------------------------------------------------------------------

def build_positions(entries: tuple[dict, ...], orders: dict, reasons: dict) -> list[dict]:
    """Orders that were sent and may still hold a position (any day), newest first. Fills
    are recorded by the reconciler (#155): until then a sent order is "sent, fill not
    recorded". Unrealized P&L is always null: the UI has no live quotes."""
    # an approved entry without client_order_id (Phase 4) has the one its run and plan derive,
    # as journal/orders.py from_approved gives it
    approved: dict[str, dict] = {}
    for e in entries:
        if e.get("status") == APPROVED:
            cid = e.get("client_order_id")
            if not isinstance(cid, str):
                try:
                    cid = client_order_id(e.get("run_id"), e.get("plan_id"))
                except (OrderError, ValueError, TypeError):
                    continue
            approved[cid] = e
    out = []
    for cid, o in orders.items():
        if not (o.sent and o.open):
            continue
        order = (approved.get(cid) or {}).get("order")
        order = order if isinstance(order, dict) else {}
        if o.status in NOT_FILLED and o.filled_qty == 0:
            text = _sent_text(o.status)
        else:
            text = reasons["order_status"].get(o.status, o.status)
        out.append({
            "symbol": o.symbol, "run_id": o.run_id, "client_order_id": cid,
            "approved_at": o.approved_at.astimezone(NY).isoformat(),
            "qty": o.qty, "filled_qty": o.filled_qty,
            "avg_price": None if o.avg_price is None else str(o.avg_price),
            "stop": order.get("stop_loss") if isinstance(order.get("stop_loss"), str) else None,
            "target": (order.get("take_profit") if isinstance(order.get("take_profit"), str)
                       else None),
            "max_loss": str(o.max_loss),
            "status": {"code": o.status, "text": text},
            "unrealized": None,
        })
    out.sort(key=lambda p: p["approved_at"], reverse=True)
    return out


# ---- the page ----------------------------------------------------------------------------

def _market(today: date, now: datetime, problems: list[dict]) -> dict:
    try:
        hours = session_hours(today)
    except CalendarError:
        problems.append({"level": "error", "text": "the market calendar does not cover "
                         "today: run_day cannot trade"})
        return {"state": "unknown", "text": "Market hours unknown", "open": None,
                "opens": None, "closes": None}
    if hours is None:
        return {"state": "closed", "text": f"Market closed today ({closed_reason(today)})",
                "open": None, "opens": None, "closes": None}
    o, c = hours
    t = now.time()
    state = "open" if o <= t < c else "closed"
    text = ("Market open" if state == "open" else
            "Market opens at " + _hm(o) if t < o else "Market closed for the day")
    return {"state": state, "text": text, "open": state == "open", "opens": o.strftime("%H:%M"),
            "closes": c.strftime("%H:%M")}


def _hm(t: time) -> str:
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'} ET"


def live(paths: Any, snap: JournalSnapshot, now: datetime, *, limits: Loaded,
         run_cfg: Loaded, reasons: dict) -> dict:
    now = now.astimezone(NY)
    today = now.date()
    problems: list[dict] = list(snap.problems())

    # the budget first, read once and closed before anything else is built
    budget = read_budget(paths.state_dir, today)
    hb = read_state_file(heartbeat_path(paths.state_dir))

    lim = limits.value
    caps = run_cfg.value.caps if run_cfg.value is not None else None
    if limits.error:
        problems.append({"level": "error", "text": limits.error})
    if run_cfg.error:
        problems.append({"level": "error", "text": run_cfg.error})
    if budget.error:
        problems.append({"level": "error", "text": budget.error})
    if hb.error:
        problems.append({"level": "error", "text": f"{hb.name} is {hb.error}"})

    # the day's totals, by the journal's own code (what risk_check uses)
    day_state = None
    orders: dict | None = None
    journal_why: str | None = snap.error or ("the journal's last line is cut off"
                                              if snap.tail_stuck else None)
    if journal_why is None:
        try:
            sj = SnapshotJournal(snap.entries)
            orders = sj.orders()
            day_state = sj.day_state(now)
        except JournalError as exc:
            journal_why = str(exc)
            problems.append({"level": "error", "text": f"{exc}: day totals cannot be "
                             "trusted, and run_day refuses orders until it is fixed"})

    notes = day_notes(snap.entries, today)
    hb_view = _heartbeat(hb, now, today)
    # an end with no start is a run too: the budget closed when the day opened
    run_started = notes.started is not None or notes.ended is not None or hb_view["today"]
    ended = notes.ended
    live_now = hb_view["today"] and not hb_view["stale"] and ended is None

    names, cache_error = _cache_names(cache_dir(paths.journal))
    if cache_error:
        problems.append({"level": "warn", "text": cache_error})
    market = _market(today, now, problems)

    reasons_ = _blocked_reasons(limits=lim, limits_error=limits.error, run_cfg=run_cfg,
                                journal_why=journal_why, hb_view=hb_view, notes=notes,
                                ended=ended, budget=budget, caps=caps, names=names,
                                today=today, run_started=run_started)
    status = _status(reasons_, run_started=run_started, ended=ended, market=market,
                     now=now, scan_time=(run_cfg.value.scan_time if run_cfg.value is not None
                                         else DEFAULT_SCAN), notes=notes,
                     held_back=_held_back(day_state, lim))
    if status["kind"] != "blocked":  # never hidden: each reason is a problem instead
        for r in reasons_:
            problems.append({"level": "warn", "text": r["text"]})

    key, more = build_limits(day_state, lim, caps, budget.data)
    today_rows = _today(snap, now, today, budget.data, reasons, live_now=live_now,
                        problems=problems)
    positions = (build_positions(snap.entries, orders, reasons) if orders is not None
                 else None)

    current = None
    if hb_view["today"] and hb_view["phase"] is not None:
        m = SYMBOL_PHASE.fullmatch(str(hb.data.get("phase")))  # type: ignore[union-attr]
        current = {"symbol": m.group(1) if m else None, "step": None,
                   "line": hb_view["phase"], "analysts": None, "live": live_now}

    end_view = None
    if ended is not None:
        end_view = {"reason": cap(ended.get("reason"), TEXT),
                    "at": _iso(ended.get("at"))}
    scan_at = _iso(notes.scan.get("at")) if notes.scan else None
    return {
        "as_of": now.isoformat(),
        "et_date": today.isoformat(),
        "market": market,
        "status": status,
        "key_limits": key,
        "caps": more,
        "ceilings": ceilings(),
        "ticket": None,  # needs the approval ticket in the record (#212)
        "today": today_rows,
        "current": current,
        "positions": positions,
        "sources": build_sources(notes, budget.data, caps, names, today),
        "run_day": {
            "heartbeat_at": hb_view["at"], "age_s": hb_view["age_s"],
            "stale": hb_view["stale"], "future": hb_view["future"],
            "phase": hb_view["phase"], "pid": hb_view["pid"],
            "started_at": _iso(notes.started.get("at")) if notes.started else None,
            "last_scan_at": scan_at, "last_end": end_view,
        },
        "problems": _dedupe(problems),
    }


def _iso(text: object) -> str | None:
    when = _ny(text)
    return when.isoformat() if when else None


def _heartbeat(hb: StateFile, now: datetime, today: date) -> dict:
    age = heartbeat_age_s(hb, now)
    at = _ny(hb.data.get("at")) if hb.data is not None else None
    future = age is not None and age < -HEARTBEAT_SKEW_S
    pid = hb.data.get("pid") if hb.data is not None else None
    phase = hb.data.get("phase") if hb.data is not None else None
    return {
        "at": at.isoformat() if at else None,
        "age_s": age,
        "future": future,
        # stale: too old, dated in the future, or no usable time
        "stale": age is None or age > HEARTBEAT_STALE_S or future,
        "today": at is not None and at.date() == today and not future,
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        "phase": cap(phase, 120) if isinstance(phase, str) else None,
    }


def _blocked_reasons(*, limits: Any, limits_error: str | None, run_cfg: Loaded,
                     journal_why: str | None, hb_view: dict, notes: DayNotes,
                     ended: dict | None, budget: Budget, caps: Any, names: set[str] | None,
                     today: date, run_started: bool) -> list[dict]:
    out: list[dict] = []

    def add(code: str, text: str) -> None:
        out.append({"code": code, "text": text})

    if limits_error or limits is None:
        add("limits", "the limits config cannot be used, so risk_check blocks every order")
    elif not limits.paper:
        add("paper", "the limits say paper: false, and run_day refuses to start")
    elif not limits.trading_enabled:
        add("kill_switch", "the kill switch is on (trading_enabled: false)")
    if run_cfg.error:
        add("run_day_config", "run_day's config cannot be used, so it refuses to start")
    if journal_why:
        add("journal", "the journal does not replay, so run_day refuses orders until it is "
            "fixed")
    if hb_view["future"]:
        add("heartbeat_future", "run_day's heartbeat is dated in the future: check the "
            "PC's clock")
    elif run_started and ended is None and hb_view["stale"]:
        # the time, not the age: the text (and so the ETag) stays the same between polls
        at = hb_view["at"]
        add("heartbeat_stale",
            f"run_day's last heartbeat was at {at[11:19]} ET, more than "
            f"{HEARTBEAT_STALE_S} s ago: it may have stopped" if at else
            "run_day's heartbeat is missing or unreadable: it may have stopped")
    reason = ended.get("reason") if ended is not None else None
    held = notes.held_alert or (isinstance(reason, str) and reason.startswith("held:"))
    if held:
        add("held", "a position from an earlier day may still be open: no decisions today "
            "until you check it")
    elif isinstance(reason, str) and not reason.startswith(NORMAL_ENDS):
        add("run_day_end", f"run_day ended: {cap(reason, TEXT)}")
    elif ended is not None and not isinstance(reason, str):
        add("run_day_end", "run_day ended with no reason recorded")
    if budget.error:
        add("budget_unreadable", f"{budget.error}: run_day counts every cap as reached")
    elif budget.data is not None:
        b = budget.data
        if b["exhausted"]:
            add("budget_exhausted", f"the daily budget is closed: {cap(b['exhausted'], TEXT)}")
        elif caps is not None:
            if b["decisions"] >= caps.max_decisions:
                add("budget_exhausted", f"the decision cap is reached ({caps.max_decisions} "
                    "today)")
            if b["tokens_used"] + sum(b["reservations"].values()) >= caps.max_tokens:
                add("budget_exhausted", f"the token cap is reached ({caps.max_tokens} today)")
    elif run_started and budget.exists is False:
        add("budget_missing", "today's budget file is missing although run_day started: "
            "every cap counts as reached")
    if (budget.data is not None and caps is not None and names is not None
            and budget.data["av_calls"] >= caps.max_av_calls
            and f"av-earnings-{today.isoformat()}.json" not in names):
        add("event_calendar", "the earnings calendar could not be fetched today, so the "
            "event check blocks every new entry")
    elif (budget.data is not None and caps is not None and names is not None
            and budget.data["av_calls"] > 0
            and f"av-earnings-{today.isoformat()}.json" not in names):
        add("event_calendar", "the earnings calendar has not come in yet today "
            f"({budget.data['av_calls']} of {caps.max_av_calls} calls used), so the event "
            "check blocks new entries until it does")
    return out


def _held_back(day_state: Any, limits: Any) -> str | None:
    """Why risk_check refuses every new entry now although nothing is wrong: a limit the day
    reached (tools/risk_check.py). Display only, from DayState and the limits."""
    if day_state is None or limits is None:
        return None
    if day_state.open_positions >= limits.max_open_positions:
        n = day_state.open_positions
        return (f"{n} position{'s are' if n != 1 else ' is'} open (at most "
                f"{limits.max_open_positions} at once)")
    if day_state.trades_today >= limits.max_trades_per_day:
        return f"all {limits.max_trades_per_day} of today's trades are used"
    if day_state.risk_used_today >= limits.daily_loss_limit:
        return f"today's money-at-risk limit (${limits.daily_loss_limit}) is reached"
    return None


def _status(reasons: list[dict], *, run_started: bool, ended: dict | None, market: dict,
            now: datetime, scan_time: time, notes: DayNotes,
            held_back: str | None = None) -> dict:
    if not run_started:
        tail = ("market closed" if market["state"] != "open"
                else "run_day has not started")
        return {"kind": "not_running", "sentence": f"Not running today · {tail}",
                "reasons": []}
    # Blocked while the run has not ended (a stale heartbeat before the open is still a run
    # that stopped), and while the market is open after a run that ended on a problem. After
    # a normal end, or once the market closed, the day is over: its reasons are problems.
    reason = ended.get("reason") if ended is not None else None
    normal_end = isinstance(reason, str) and reason.startswith(NORMAL_ENDS)
    if reasons and (ended is None or (not normal_end and market["state"] == "open")):
        first = reasons[0]["text"]
        more = f" (and {len(reasons) - 1} more)" if len(reasons) > 1 else ""
        return {"kind": "blocked",
                "sentence": f"New trades are blocked: {first}{more}", "reasons": reasons}
    if ended is not None:
        what = ("the entry window closed" if reason == "entry window closed"
                else "no session today" if isinstance(reason, str)
                and reason.startswith("no session (") else "run_day ended")
        return {"kind": "idle", "sentence": f"Done for today · {what}", "reasons": []}
    if market["state"] != "open":
        return {"kind": "idle", "sentence": f"Idle · {market['text']}", "reasons": []}
    if notes.scan is None and now.time() < scan_time:
        return {"kind": "idle", "sentence": f"Idle · waiting for the {_hm(scan_time)} scan",
                "reasons": []}
    if held_back:
        return {"kind": "normal", "sentence": f"Running · no new entries while {held_back}",
                "reasons": []}
    return {"kind": "normal", "sentence": "All normal · nothing needs you", "reasons": []}


def _today(snap: JournalSnapshot, now: datetime, today: date, budget: dict | None,
           reasons: dict, *, live_now: bool, problems: list[dict]) -> list[dict]:
    """Today's decisions, grouped as History groups them, newest first; then each symbol the
    budget counted with no outcome yet: Deciding while run_day is live, else no outcome."""
    listing = decisions(snap, now, day_from=today, day_to=today, limit=200, reasons=reasons)
    for p in listing["problems"]:
        problems.append(p)
    rows = [{
        "symbol": i["symbol"], "outcome": i["outcome"], "why": i["why"]["text"],
        "order_status": _unfilled(i["order_status"]), "run_id": i["run_id"], "at": i["at"],
    } for i in listing["items"] if i["day"] == today.isoformat()]
    if listing["next_cursor"] is not None:
        problems.append({"level": "warn", "text": "more than 200 decisions today: only the "
                         "newest 200 are listed"})
    seen = {str(r["symbol"]).upper() for r in rows if r["symbol"]}
    pending = []
    for sym in (budget or {}).get("symbols", []):
        if sym.upper() in seen:
            continue
        pending.append({
            "symbol": cap(sym, 12),
            "outcome": "DECIDING" if live_now else "NO_OUTCOME",
            "why": ("counted as today's decision; no outcome yet" if live_now else
                    "counted as today's decision, but no outcome was recorded"),
            "order_status": None, "run_id": None, "at": None,
        })
    return pending + rows


def _unfilled(status: dict | None) -> dict | None:
    """A sent order with no fill recorded, worded as on the position card: fills are
    recorded by the reconciler (#155), so "waiting for a fill" may not be true."""
    if status is None or status["code"] not in NOT_FILLED:
        return status
    return {"code": status["code"], "text": _sent_text(status["code"])}


def _sent_text(code: str) -> str:
    return ("sent, result unknown; fill not recorded" if code == SEND_UNKNOWN
            else "sent, fill not recorded")


def _dedupe(problems: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for p in problems:
        if p["text"] not in seen:
            seen.add(p["text"])
            out.append(p)
    return out
