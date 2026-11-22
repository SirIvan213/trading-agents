"""GET /api/decisions/{run_id}: the Decision detail page (UI-4, #217).

Two sources, both read-only:
  - the journal (JournalReader, lock-free): the decision's outcome entry, its order events, a
    run_day approval timeout and the run_day scan that picked the symbol. This is what History
    shows, and what the order ticket and the approval steps are built from;
  - the decision record (pipeline/records.py writes it; #211):
    journal/records/YYYY-MM-DD/<run_id>.json, read with open_shared as one short read and
    parsed here as JSON. ui/api never imports pipeline.records. The record holds the
    coverage check, the analysts, the plans, the debate and the Trader. A missing, unreadable
    or malformed record (or an unknown schema) is a visible problem, and the record-backed
    steps then say "not recorded for this decision", never empty or guessed content.

The 8 steps follow the code's order (pipeline/decide.py and pipeline/order_flow.py):
  1 Scan            the run_day scan that listed the symbol (journal)
  2 Event check     the reporting check only (8-K item 2.02) before any AI (record, or the
                    journal's skip)
  3 Analysts  4 Plans  5 Debate  6 Trader    (record; the Trader's action also from the journal)
  7 Order ticket    sizing, the risk check and the earnings and halt gate (journal)
  8 Approve and log the gate rechecked at approval, the approval and the order events (journal)

Record text (analyst evidence, debate turns, the Trader's reasoning) is untrusted model text:
returned as plain strings, length-capped, never parsed for numbers. "In short" is a fixed
template over the recorded outcome and numbers. Numbers are shown exactly as recorded; the
limit price is never computed (a dash when it isn't recorded).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from journal.orders import APPROVED, ORDER_EVENTS, client_order_id
from ui.api.history import decisions
from ui.api.readers.journal_snapshot import JournalSnapshot
from ui.api.readers.shared_open import read_bytes_shared

RUN_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"  # no dot, slash or backslash: safe in a path
RUN_ID = re.compile(RUN_ID_PATTERN)
SCHEMA = 1
TEXT_CAP = 600
SHORT_CAP = 240
EVENT_FIELDS = ("limit_price", "filled_qty", "avg_price", "closed_by", "realized_pnl",
                "reason", "broker_reason")
STEPS = (
    (1, "scan", "Scan", "code"), (2, "event_check", "Event check", "code"),
    (3, "analysts", "Analysts", "ai"), (4, "plans", "Plans", "code"),
    (5, "debate", "Debate", "ai"), (6, "trader", "Trader", "ai"),
    (7, "order_ticket", "Order ticket", "code"), (8, "approve_and_log", "Approve and log", "you"),
)
NOT_RECORDED = "Not recorded for this decision."
AGENT_NAME = {"technical": "Chart", "sentiment": "News", "fundamentals": "Finances"}
FORCED_TEXT = {
    "fatal_bear": "the Bear raised a fatal objection",
    "analyst_failed": "an analyst failed",
    "no_plans": "no trade plan could be made",
    "stale_before_trader": "too little time was left to approve a ticket",
    "debate_mismatch": "the debate did not match this stock's analysis",
    "config_error": "an agent's settings were invalid",
}
SKIP_TEXT = {"analyst_failed": "an analyst failed, so the Trader passes without a call"}

# The record fields the page reads, and their types: the contract with pipeline/records.py
# (ui/tests/test_detail.py checks the golden records in tests/fixtures/records/ against it).
RECORD_FIELDS: dict[str, tuple[type, ...]] = {
    "schema": (int,), "run_id": (str,), "symbol": (str,), "checked_at": (str,),
    "coverage": (dict, type(None)), "analysts": (dict,), "exit_plans": (list,),
    "debate": (dict, type(None)), "trader": (dict, type(None)),
    "proposal": (dict, type(None)), "outcome": (dict,), "error": (dict, type(None)),
}


def cap(text: object, limit: int = TEXT_CAP) -> str | None:
    """Untrusted text, as a plain string, at most `limit` characters ("…" when cut)."""
    if text is None:
        return None
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def validate_record(obj: object, run_id: str) -> str | None:
    """Why `obj` is not a usable record for `run_id`, or None."""
    if not isinstance(obj, dict):
        return "not a JSON object"
    if obj.get("schema") != SCHEMA:
        return f"unknown schema {str(obj.get('schema'))[:12]!r} (this page reads {SCHEMA})"
    for key, types in RECORD_FIELDS.items():
        if key not in obj:
            return f"missing field {key!r}"
        if not isinstance(obj[key], types) or isinstance(obj[key], bool) and int in types:
            return f"field {key!r} has the wrong type"
    if obj["run_id"] != run_id:
        return "it is for another decision"
    if not isinstance(obj["outcome"].get("status"), str | type(None)):
        return "field 'outcome.status' has the wrong type"
    return None


def records_dir(journal_path: Path) -> Path:
    """journal/records next to the journal (the rule pipeline.records.records_dir follows;
    ui/tests checks the two agree)."""
    return Path(journal_path).parent / "records"


def _record_path(directory: Path, run_id: str, day_hint: str | None) -> Path | None:
    """The record's path, found by the folder listing (no file is opened): the hinted day,
    the day before it, then every day folder."""
    days: list[str] = []
    if day_hint:
        d = date.fromisoformat(day_hint)
        days += [d.isoformat(), (d - timedelta(days=1)).isoformat()]
    try:
        folders = sorted((p.name for p in Path(directory).iterdir() if p.is_dir()),
                         reverse=True)
    except OSError:
        folders = []
    for day in [*days, *(f for f in folders if f not in days)]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        path = Path(directory) / day / f"{run_id}.json"
        if path.is_file():
            return path
    return None


def read_record(directory: Path, run_id: str, day_hint: str | None
                ) -> tuple[dict | None, dict | None]:
    """(record, problem). The record is read with open_shared as one short read and checked
    with validate_record; a problem says why there is none."""
    path = _record_path(directory, run_id, day_hint)
    if path is None:
        return None, {"level": "warn", "text": "no decision record was found for this "
                      "decision: the analysts, plans, debate and Trader are not shown"}
    try:
        raw = read_bytes_shared(path)
    except OSError as exc:
        return None, {"level": "error",
                      "text": f"the decision record could not be read ({type(exc).__name__})"}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {"level": "error", "text": "the decision record is not valid JSON"}
    why = validate_record(obj, run_id)
    if why:
        return None, {"level": "error", "text": f"the decision record is unusable: {why}"}
    return obj, None


# ---------------------------------------------------------------------------------------

def _step(n: int, key: str, title: str, actor: str, state: str, line: str,
          details: dict | None = None) -> dict:
    return {"n": n, "key": key, "title": title, "actor": actor, "state": state,
            "line": line, "details": details}


def _hhmm(iso: str | None) -> str | None:
    m = re.match(r"^\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2})", iso or "")
    return f"{m.group(1)}:{m.group(2)} ET" if m else None


def decision_detail(snap: JournalSnapshot, now: datetime, run_id: str, directory: Path,
                    reasons: dict) -> dict | None:
    """The page's data, or None when neither the journal nor a record knows `run_id`."""
    if not RUN_ID.match(run_id):
        return None
    listing = decisions(snap, now, limit=10**9, reasons=reasons)
    row = next((i for i in listing["items"] if i["run_id"] == run_id), None)
    problems = list(listing["problems"])
    record, rec_problem = (None, None)
    if not run_id.startswith("entry-"):
        record, rec_problem = read_record(directory, run_id, row["day"] if row else None)
    if row is None and record is None:
        return None
    if rec_problem:
        problems.append(rec_problem)

    entry, timeout, events = _journal_parts(snap, run_id, row)
    symbol = (row or {}).get("symbol") or (record or {}).get("symbol")
    order = entry.get("order") if isinstance(entry.get("order"), dict) else None
    limit = _limit_price(record, events)
    numbers = None
    if order is not None:
        numbers = {
            "shares": order.get("qty") if isinstance(order.get("qty"), int) else None,
            "entry": _str(order.get("entry")), "limit_price": limit,
            "stop": _str(order.get("stop_loss")), "target": _str(order.get("take_profit")),
            "max_loss": _str(entry.get("max_loss")),
        }
    steps = [
        _scan_step(snap, symbol, row),
        _event_step(record, row),
        _analysts_step(record, row),
        _plans_step(record, row),
        _debate_step(record, row),
        _trader_step(record, row, entry),
        _ticket_step(row, entry, limit),
        _approval_step(row, entry, timeout, events),
    ]
    return {
        "run_id": run_id,
        "symbol": symbol,
        "at": (row or {}).get("at") or (record or {}).get("checked_at"),
        "day": (row or {}).get("day"),
        "outcome": (row or {}).get("outcome"),
        "why": (row or {}).get("why"),
        "order_status": (row or {}).get("order_status"),
        "in_short": _in_short(row, numbers, symbol),
        "numbers": numbers,
        "steps": steps,
        "order_events": events,
        "record_available": record is not None,
        "problems": problems,
    }


def _str(v: object) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v)


def _journal_parts(snap: JournalSnapshot, run_id: str, row: dict | None
                   ) -> tuple[dict, dict | None, list[dict]]:
    """The decision's outcome entry, its approval-timeout note and its order events."""
    entry: dict = {}
    timeout = None
    if row is not None:
        entry = snap.entries[row["seq"] - 1]
    for e in snap.entries:
        if e.get("status") == "run_day_approval_timeout" and e.get("run_id") == run_id:
            timeout = e
    events: list[dict] = []
    if entry.get("status") == APPROVED and isinstance(entry.get("plan_id"), str):
        cid = entry.get("client_order_id") or client_order_id(run_id, entry["plan_id"])
        events = [{"status": e.get("status"), "at": _str(e.get("at")),
                   "fields": {k: _str(e[k]) for k in EVENT_FIELDS if e.get(k) is not None}}
                  for e in snap.entries
                  if e.get("status") in ORDER_EVENTS and e.get("client_order_id") == cid]
    return entry, timeout, events


def _limit_price(record: dict | None, events: list[dict]) -> str | None:
    """The limit price as recorded: the record's (the tool's value), else the one the
    submitting event recorded. Never computed."""
    prop = (record or {}).get("proposal") or {}
    for key in ("approved_limit_price", "limit_price"):
        if isinstance(prop.get(key), str):
            return prop[key]
    for e in events:
        if e["status"] == "submitting" and e["fields"].get("limit_price"):
            return e["fields"]["limit_price"]
    return None


def _in_short(row: dict | None, numbers: dict | None, symbol: str | None) -> str:
    """A fixed template over the recorded outcome and numbers: never model text."""
    sym = symbol or "this stock"
    if row is None:
        return f"The journal has no outcome for {sym}; only the decision record was found."
    why = row["why"]["text"]
    outcome = row["outcome"]
    if outcome == "SKIPPED":
        return f"{sym} was skipped before any AI ran. {why}."
    if outcome == "PASS":
        return f"No trade in {sym}. {why}."
    if outcome == "FAILED":
        return f"The decision on {sym} did not finish. {why}."
    shares = (numbers or {}).get("shares")
    status = (row.get("order_status") or {}).get("text")
    lead = (f"The agents proposed buying {shares} shares of {sym}." if shares is not None
            else f"The Trader chose a plan to buy {sym}.")
    tail = f" Order: {status}." if status else ""
    loss = (numbers or {}).get("max_loss")
    risk = f" Worst case at the stop: ${loss}." if loss and row["why"]["code"] == "approved" \
        else ""
    return f"{lead} {why}.{tail}{risk}"


def _scan_step(snap: JournalSnapshot, symbol: str | None, row: dict | None) -> dict:
    n, key, title, actor = STEPS[0]
    at = (row or {}).get("at")
    found = None
    for e in snap.entries:
        if e.get("status") != "run_day_scan" or not isinstance(e.get("candidates"), list):
            continue
        when = e.get("at")
        if symbol in e["candidates"] and isinstance(when, str) and at and when <= at \
                and when[:10] == at[:10]:
            found = when
    if found:
        return _step(n, key, title, actor, "done",
                     f"Picked by the run_day scan at {_hhmm(found)}.", {"scan_at": found})
    return _step(n, key, title, actor, "not_recorded",
                 "No run_day scan picked it (a manual or test run), or the scan is not "
                 "recorded.")


def _event_step(record: dict | None, row: dict | None) -> dict:
    n, key, title, actor = STEPS[1]
    cov = (record or {}).get("coverage")
    if isinstance(cov, dict):
        if cov.get("covered") is True:
            last = cov.get("last_item_202")
            return _step(n, key, title, actor, "done",
                         "Proven to report earnings (last SEC 8-K item 2.02 filed "
                         f"{last}). The earnings and halt check itself is at step 7.",
                         {"coverage": cov})
        reason = cap(cov.get("reason"), SHORT_CAP) or "quarterly reporting not proven"
        state = "failed" if cov.get("fetch_failed") else "blocked"
        return _step(n, key, title, actor, state,
                     f"Not proven to report earnings ({reason}), so no AI time was spent.",
                     {"coverage": cov})
    if row is not None and row["outcome"] == "SKIPPED":
        return _step(n, key, title, actor, "blocked",
                     f"Not proven to report earnings ({row['why']['detail'] or 'see why'}), so "
                     "no AI time was spent.")
    if row is not None and row["outcome"] in ("BUY", "PASS"):
        return _step(n, key, title, actor, "done",
                     "Passed: the decision went on to the analysts. The details are not "
                     "recorded.")
    return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)


def _not_reached(record: dict | None, row: dict | None) -> bool:
    """True when the decision stopped before the analysts (a skip, or a cap before them)."""
    if row is not None and row["outcome"] == "SKIPPED":
        return True
    status = ((record or {}).get("outcome") or {}).get("status")
    return status in ("skipped", "not_started")


def _analysts_step(record: dict | None, row: dict | None) -> dict:
    n, key, title, actor = STEPS[2]
    if _not_reached(record, row):
        return _step(n, key, title, actor, "not_reached", "Not reached.")
    if record is None:
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    out, failed = [], []
    for agent in ("technical", "sentiment", "fundamentals"):
        a = record["analysts"].get(agent) or {}
        rep = a.get("report") or {}
        run = a.get("run") or {}
        ok = run.get("ok") is not False
        if not ok:
            failed.append(f"{AGENT_NAME[agent]} ({run.get('reason_code') or 'failed'})")
        out.append({
            "agent": agent, "name": AGENT_NAME[agent], "ok": ok,
            "reason_code": run.get("reason_code"),
            "stance": rep.get("stance") if ok else None,
            "confidence": rep.get("confidence") if ok else None,
            "evidence": ([{"point": cap(e.get("point")), "source": cap(e.get("source"), 60)}
                          for e in (rep.get("evidence") or [])[:10] if isinstance(e, dict)]
                         if ok else []),
        })
    if not record["analysts"]:
        return _step(n, key, title, actor, "not_reached", "No analyst ran.")
    if failed:
        return _step(n, key, title, actor, "failed",
                     f"Failed: {', '.join(failed)}. A failed analyst forces a PASS.",
                     {"analysts": out})
    stances = ", ".join(f"{a['name']} {a['stance']}" for a in out)
    return _step(n, key, title, actor, "done", f"{stances}.", {"analysts": out})


def _plans_step(record: dict | None, row: dict | None) -> dict:
    n, key, title, actor = STEPS[3]
    if _not_reached(record, row):
        return _step(n, key, title, actor, "not_reached", "Not reached.")
    if record is None:
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    chosen = (record.get("trader") or {}).get("plan_id")
    plans = [{"plan_id": p.get("plan_id"), "setup": p.get("setup"),
              "entry": _str(p.get("entry")), "stop": _str(p.get("stop")),
              "target": _str(p.get("target")), "reward_to_risk": _str(p.get("reward_to_risk")),
              "chosen": p.get("plan_id") == chosen}
             for p in record["exit_plans"] if isinstance(p, dict)]
    if not plans:
        return _step(n, key, title, actor, "skipped", "No plan was made.", {"plans": []})
    word = "plan" if len(plans) == 1 else "plans"
    pick = f" The Trader picked {chosen}." if chosen else ""
    return _step(n, key, title, actor, "done",
                 f"{len(plans)} {word} worked out by code.{pick}", {"plans": plans})


def _debate_step(record: dict | None, row: dict | None) -> dict:
    n, key, title, actor = STEPS[4]
    if _not_reached(record, row):
        return _step(n, key, title, actor, "not_reached", "Not reached.")
    if record is None:
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    d = record.get("debate")
    if not isinstance(d, dict):
        return _step(n, key, title, actor, "not_reached", "Not reached.")
    if d.get("skipped"):
        code = str(d.get("skip_reason") or "").split(":", 1)[0]
        text = SKIP_TEXT.get(code, code or "skipped")
        return _step(n, key, title, actor, "skipped", f"Skipped: {text}.",
                     {"skip_code": code})
    bull = d.get("final_bull") or {}
    bear = d.get("final_bear") or {}
    turns = []
    for t in (d.get("turns") or [])[:12]:
        case = t.get("case") or {}
        turns.append({
            "round": t.get("round"), "side": t.get("side"),
            "argument": cap(case.get("argument")),
            "points": [cap(p, 300) for p in (case.get("key_points") or
                                              case.get("objections") or [])[:8]],
            "fatal": case.get("fatal") if t.get("side") == "bear" else None,
        })
    details = {
        "rounds_run": d.get("rounds_run"), "rounds_planned": d.get("rounds_planned"),
        "fatal": bool(d.get("fatal")), "stopped_early": bool(d.get("stopped_early")),
        "bull": cap(bull.get("argument"), 300),
        "bear": cap(((bear.get("objections") or [None])[0]), 300),
        "turns": turns,
    }
    rounds = f"{d.get('rounds_run')} round{'s' if d.get('rounds_run') != 1 else ''}"
    if d.get("fatal"):
        return _step(n, key, title, actor, "blocked",
                     f"{rounds}. The Bear raised a fatal objection, which forces a PASS.",
                     details)
    return _step(n, key, title, actor, "done",
                 f"{rounds}. The Bear found no deal-breaker.", details)


def _trader_step(record: dict | None, row: dict | None, entry: dict) -> dict:
    n, key, title, actor = STEPS[5]
    if _not_reached(record, row):
        return _step(n, key, title, actor, "not_reached", "Not reached.")
    t = (record or {}).get("trader")
    if isinstance(t, dict):
        forced = t.get("forced_pass")
        details = {"action": t.get("action"), "plan_id": t.get("plan_id"),
                   "confidence": t.get("confidence"), "forced_pass": forced,
                   "reasoning": None if forced else cap(t.get("reasoning"), 1200)}
        if forced:
            text = FORCED_TEXT.get(forced, forced)
            return _step(n, key, title, actor, "blocked",
                         f"Forced PASS without a Trader call: {text}.", details)
        if t.get("action") == "BUY":
            return _step(n, key, title, actor, "done",
                         f"BUY {t.get('plan_id')}, {t.get('confidence')} sure.", details)
        return _step(n, key, title, actor, "done",
                     f"PASS, {t.get('confidence')} sure.", details)
    # no record: the journal still says what the Trader chose
    if entry.get("status") == "pass":
        forced = entry.get("forced_pass")
        line = (f"Forced PASS: {FORCED_TEXT.get(forced, forced)}." if forced else "PASS.")
        return _step(n, key, title, actor, "blocked" if forced else "done",
                     f"{line} The reasoning is not recorded.")
    if row is not None and row["outcome"] == "BUY" and entry.get("plan_id"):
        return _step(n, key, title, actor, "done",
                     f"BUY {entry['plan_id']}. The reasoning is not recorded.")
    return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)


def _ticket_step(row: dict | None, entry: dict, limit: str | None) -> dict:
    n, key, title, actor = STEPS[6]
    status = entry.get("status")
    if row is None or row["outcome"] in ("SKIPPED", "PASS") or status in (None, "pass",
                                                                           "skipped"):
        state = "not_recorded" if row is None else "not_reached"
        return _step(n, key, title, actor, state,
                     NOT_RECORDED if row is None else "Not reached: no plan was chosen.")
    gate = row["gate"]
    verdict = entry.get("verdict") if isinstance(entry.get("verdict"), dict) else {}
    reasons = [cap(r, 300) for r in (verdict.get("reasons") or [])[:10]]
    details = {"gate": gate, "risk": verdict.get("verdict"), "risk_reasons": reasons,
               "stage": entry.get("stage"), "limit_price": limit,
               "decided_by": _str(entry.get("decided_by"))}
    if status == "no_order":
        stage = entry.get("stage")
        state = "failed" if stage == "events" else "blocked"
        return _step(n, key, title, actor, state, f"No order: {row['why']['text']}.", details)
    if status in ("journal_locked", "journal_error"):
        return _step(n, key, title, actor, "done", "The ticket was made and shown.", details)
    order = entry.get("order") if isinstance(entry.get("order"), dict) else {}
    lim = limit or "—"
    return _step(n, key, title, actor, "done",
                 f"Buy {order.get('qty')} shares with a limit of {lim}, stop "
                 f"{order.get('stop_loss')}, target {order.get('take_profit')}. Risk check: "
                 f"{verdict.get('verdict') or 'PASS'}; earnings and halt: {gate['status']}.",
                 details)


def _approval_step(row: dict | None, entry: dict, timeout: dict | None,
                   events: list[dict]) -> dict:
    n, key, title, actor = STEPS[7]
    status = entry.get("status")
    if row is None:
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    if row["outcome"] in ("SKIPPED", "PASS") or status == "no_order":
        return _step(n, key, title, actor, "not_reached", "Not reached: no ticket to approve.")
    details = {"status": status, "approved_at": _str(entry.get("approved_at")),
               "expires_at": _str(entry.get("expires_at")),
               "recheck_at": _str(entry.get("recheck_at")),
               "plan_age_s": entry.get("plan_age_s"), "timed_out": timeout is not None,
               "events": events}
    why = row["why"]["text"]
    if status == APPROVED:
        tail = f" Order: {row['order_status']['text']}." if row.get("order_status") else ""
        return _step(n, key, title, actor, "done",
                     f"Approved at {_hhmm(entry.get('approved_at'))} after the re-check "
                     f"(earnings and halt rechecked).{tail}", details)
    if status in ("journal_locked", "journal_error"):
        return _step(n, key, title, actor, "failed", f"{why}.", details)
    return _step(n, key, title, actor, "blocked", f"{why}.", details)
