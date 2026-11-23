"""GET /api/decisions/{run_id}: the Decision detail page (UI-4, #217).

Two sources, both read-only:
  - the journal (JournalReader, lock-free): the decision's outcome entry, its order events, a
    run_day approval timeout and the run_day scan that listed the symbol. This is what History
    shows, and what the order ticket and the approval steps are built from;
  - the decision record (pipeline/records.py writes it; #211):
    journal/records/YYYY-MM-DD/<run_id>.json, read with open_shared as one short read and
    parsed here as JSON. ui/api never imports pipeline.records. The record holds the
    coverage check, the analysts, the plans, the debate and the Trader. It is checked field
    by field (validate_record) before any of it is used; a missing, unreadable or malformed
    record (or an unknown schema) is a visible problem, and the record-backed steps then say
    "Not recorded for this decision", never empty or guessed content.

The 8 steps follow the code's order (pipeline/decide.py and pipeline/order_flow.py):
  1 Scan            a run_day scan that listed the symbol (journal)
  2 Event check     the reporting check only (8-K item 2.02) before any AI (record, or the
                    journal: a decision only reaches the analysts once it passed)
  3 Analysts  4 Plans  5 Debate  6 Trader    (record; the Trader's action also from the journal)
  7 Order ticket    sizing, the risk check and the earnings and halt gate (journal)
  8 Approve and log the gate rechecked at approval, the approval and the order events (journal)
A step is never given a state the sources don't support: what isn't recorded says so, and a
record whose decision stopped on an error marks where it stopped as failed.

Record text (analyst evidence, debate turns, the Trader's reasoning) is untrusted model text:
returned as plain strings, length-capped, with control and bidirectional-override characters
made visible, and never parsed for numbers. "In short" is a fixed template over the recorded
outcome and numbers. Numbers are shown exactly as recorded; the limit price is never computed
(a dash when it isn't recorded).
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from journal.orders import APPROVED, ORDER_EVENTS, client_order_id
from ui.api.history import decisions
from ui.api.readers.journal_snapshot import JournalSnapshot
from ui.api.readers.shared_open import read_bytes_shared

# No dot, slash or backslash: safe in a path. Stricter than pipeline/records.py's RUN_ID, which
# also allows dots; real run ids are secrets.token_hex(8) (pipeline/ledger.py). If the id
# format ever gains dots, widen this (and ui/web/src/router.ts) on purpose.
RUN_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)  # used with fullmatch
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
SCHEMA = 1
MAX_RECORD_BYTES = 2_000_000  # a record is tens of KB; anything this big is not one
SHAPE_ERRORS = (AttributeError, TypeError, KeyError, ValueError, IndexError)
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
# forced passes decided in code before any Trader call (agents/trader.py); the others
# (api_error, cancelled, exception, invalid_output) come after a call that failed
PRE_CALL = frozenset({"fatal_bear", "analyst_failed", "no_plans", "stale_before_trader",
                      "debate_mismatch", "config_error"})
SKIP_TEXT = {"analyst_failed": "an analyst failed, so the Trader passes without a call"}
# statuses whose journal entry carries the ticket's order fields (pipeline/order_flow._entry)
TICKET_STATUSES = frozenset({APPROVED, "declined", "refused_after_approval",
                             "changed_after_approval"})
# make text that could visually reorder or hide content visible (untrusted model text)
_INVISIBLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f"
                        r"\u202a-\u202e\u2066-\u2069\ud800-\udfff]")

# The record fields the page reads, and their types: the contract with pipeline/records.py
# (ui/tests/test_detail.py checks the golden records in tests/fixtures/records/ against it).
RECORD_FIELDS: dict[str, tuple[type, ...]] = {
    "schema": (int,), "run_id": (str,), "symbol": (str,), "checked_at": (str,),
    "coverage": (dict, type(None)), "analysts": (dict,), "exit_plans": (list,),
    "debate": (dict, type(None)), "trader": (dict, type(None)),
    "proposal": (dict, type(None)), "outcome": (dict,), "error": (dict, type(None)),
}
STR = (str, type(None))
NUM = (int, float, type(None))
BOOL = (bool, type(None))


def cap(text: object, limit: int = TEXT_CAP) -> str | None:
    """Untrusted text as a plain string: control and bidi-override characters shown as
    ⟨U+XXXX⟩ (newline and tab kept), at most `limit` characters ("…" when cut)."""
    if text is None:
        return None
    s = _INVISIBLE.sub(lambda m: f"⟨U+{ord(m.group()):04X}⟩", str(text))
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ---- the record contract ----------------------------------------------------------------

def _typed(obj: dict, key: str, types: tuple[type, ...], where: str) -> str | None:
    v = obj.get(key)
    if v is None and type(None) in types:
        return None
    if isinstance(v, bool) and bool not in types:
        return f"field {where}{key!r} has the wrong type"
    if not isinstance(v, types):
        return f"field {where}{key!r} has the wrong type"
    if isinstance(v, float) and not math.isfinite(v):
        return f"field {where}{key!r} is not a finite number"
    return None


def _strings(v: object) -> bool:
    """None, or a list of strings."""
    return v is None or (isinstance(v, list) and all(isinstance(x, str) for x in v))


def _check_nested(r: dict) -> str | None:
    """Every nested field the page reads, by type (the page must never 500 on a record)."""
    checks: list[tuple[dict, str, tuple[type, ...], str]] = []
    cov = r.get("coverage")
    if isinstance(cov, dict):
        checks += [(cov, "covered", BOOL, "coverage."), (cov, "reason", STR, "coverage."),
                   (cov, "last_item_202", STR, "coverage."),
                   (cov, "fetch_failed", BOOL, "coverage.")]
    for agent, a in r["analysts"].items():
        where = f"analysts.{str(agent)[:20]}."
        if not isinstance(agent, str) or not isinstance(a, dict):
            return f"analysts entry {str(agent)[:20]!r} is not an object"
        checks += [(a, "report", (dict, type(None)), where), (a, "run", (dict, type(None)), where)]
        rep, run = a.get("report"), a.get("run")
        if isinstance(rep, dict):
            checks += [(rep, "stance", STR, where), (rep, "confidence", NUM, where),
                       (rep, "evidence", (list, type(None)), where)]
            if not isinstance(rep.get("evidence"), list | None):
                return f"field {where}'evidence' has the wrong type"
            for e in rep.get("evidence") or []:
                if not isinstance(e, dict):
                    return f"{where}evidence has an item that is not an object"
                checks += [(e, "point", STR, where + "evidence."),
                           (e, "source", STR, where + "evidence.")]
        if isinstance(run, dict):
            checks += [(run, "ok", BOOL, where + "run."), (run, "reason_code", STR, where + "run.")]
    for p in r["exit_plans"]:
        if not isinstance(p, dict):
            return "exit_plans has an item that is not an object"
        checks += [(p, k, STR, "exit_plans.") for k in ("plan_id", "setup", "entry", "stop",
                                                         "target", "reward_to_risk")]
    d = r.get("debate")
    if isinstance(d, dict):
        checks += [(d, "rounds_run", NUM, "debate."), (d, "rounds_planned", NUM, "debate."),
                   (d, "fatal", BOOL, "debate."), (d, "stopped_early", BOOL, "debate."),
                   (d, "skipped", BOOL, "debate."), (d, "skip_reason", STR, "debate."),
                   (d, "all_ok", BOOL, "debate."), (d, "turns", (list, type(None)), "debate."),
                   (d, "final_bull", (dict, type(None)), "debate."),
                   (d, "final_bear", (dict, type(None)), "debate.")]
        if not isinstance(d.get("turns"), list | None):
            return "field debate.'turns' has the wrong type"
        for t in d.get("turns") or []:
            if not isinstance(t, dict):
                return "debate.turns has an item that is not an object"
            checks += [(t, "round", NUM, "debate.turns."), (t, "case", (dict, type(None)),
                                                            "debate.turns."),
                       (t, "run", (dict, type(None)), "debate.turns.")]
            if t.get("side") not in ("bull", "bear", None):
                return "field debate.turns.'side' has the wrong value"
            if isinstance(t.get("run"), dict):
                checks.append((t["run"], "ok", BOOL, "debate.turns.run."))
        for case in [*(t.get("case") for t in d.get("turns") or [] if isinstance(t, dict)),
                     d.get("final_bull"), d.get("final_bear")]:
            if isinstance(case, dict):
                checks += [(case, "argument", STR, "debate.case."),
                           (case, "fatal", BOOL, "debate.case.")]
                for key in ("key_points", "objections"):
                    if not _strings(case.get(key)):
                        return f"field debate.case.{key!r} has the wrong type"
    t = r.get("trader")
    if isinstance(t, dict):
        if t.get("action") not in ("BUY", "PASS"):
            return "field trader.'action' has the wrong value"
        checks += [(t, "plan_id", STR, "trader."), (t, "confidence", NUM, "trader."),
                   (t, "reasoning", STR, "trader."), (t, "forced_pass", STR, "trader.")]
    prop = r.get("proposal")
    if isinstance(prop, dict):
        checks += [(prop, k, STR, "proposal.") for k in ("limit_price", "approved_limit_price")]
    checks += [(r["outcome"], "status", STR, "outcome."), (r["outcome"], "approval", STR,
                                                           "outcome.")]
    err = r.get("error")
    if isinstance(err, dict):
        checks += [(err, "type", STR, "error."), (err, "reason_code", STR, "error.")]
    for obj, key, types, where in checks:
        why = _typed(obj, key, types, where)
        if why:
            return why
    return None


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
    return _check_nested(obj)


def records_dir(journal_path: Path) -> Path:
    """journal/records next to the journal (the rule pipeline.records.records_dir follows;
    ui/tests checks the two agree)."""
    return Path(journal_path).parent / "records"


def _record_path(directory: Path, run_id: str, day_hint: str | None) -> Path | None:
    """The record's path, found by the folder listing (no file is opened): the hinted day,
    the day before it, then every day folder. Only YYYY-MM-DD folder names are used."""
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
        if not DAY.fullmatch(day):
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
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None, {"level": "error", "text": "the decision record is too large to be "
                          "a record (over 2 MB): not read"}
        raw = read_bytes_shared(path)
    except OSError as exc:
        return None, {"level": "error",
                      "text": f"the decision record could not be read ({type(exc).__name__})"}
    if len(raw) > MAX_RECORD_BYTES:
        return None, {"level": "error", "text": "the decision record is too large to be "
                      "a record (over 2 MB): not read"}
    try:
        # ValueError covers JSONDecodeError, UnicodeDecodeError and Python's limit on the
        # digits of an integer
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None, {"level": "error", "text": "the decision record is not valid JSON"}
    try:
        why = validate_record(obj, run_id)
        if why is None:
            # a string JSON allows but UTF-8 cannot carry (an unpaired surrogate) would fail
            # when the response is sent: refuse it here
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        why = "it contains text that cannot be shown (an unpaired surrogate)"
    except (*SHAPE_ERRORS, RecursionError):
        why = "a field has an unexpected shape"
    if why:
        return None, {"level": "error", "text": f"the decision record is unusable: {why}"}
    return obj, None


# ---------------------------------------------------------------------------------------

def _step(n: int, key: str, title: str, actor: str, state: str, line: str,
          details: dict | None = None) -> dict:
    return {"n": n, "key": key, "title": title, "actor": actor, "state": state,
            "line": line, "details": details}


def _hhmm(iso: str | None) -> str | None:
    m = re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T([0-9]{2}):([0-9]{2})", iso or "")
    return f"{m.group(1)}:{m.group(2)} ET" if m else None


def decision_detail(snap: JournalSnapshot, now: datetime, run_id: str, directory: Path,
                    reasons: dict) -> dict | None:
    """The page's data, or None when neither the journal nor a record knows `run_id`."""
    if not RUN_ID.fullmatch(run_id):
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
    try:
        body = _build(snap, run_id, row, record, reasons)
    except SHAPE_ERRORS:
        # a record the contract let through but the page cannot use: shown as a problem,
        # and the page is built from the journal alone (never a 500)
        body = None
        if record is not None:
            problems.append({"level": "error", "text": "this decision could not be shown in "
                             "full: a field in its record or journal entry has an unexpected "
                             "shape, so the record is not used"})
            try:
                body = _build(snap, run_id, row, None, reasons)
            except SHAPE_ERRORS:
                body = None
        if body is None:
            problems.append({"level": "error", "text": "this decision's journal entry has an "
                             "unexpected shape: its steps are not shown"})
            body = _skeleton(run_id, row)
    body["problems"] = problems + body.pop("_problems")
    return body


def _skeleton(run_id: str, row: dict | None) -> dict:
    """The page when nothing could be built: every step not recorded, problems shown."""
    return {
        "run_id": run_id, "symbol": cap((row or {}).get("symbol"), 20),
        "at": (row or {}).get("at"), "day": (row or {}).get("day"),
        "outcome": (row or {}).get("outcome"), "why": (row or {}).get("why"),
        "order_status": (row or {}).get("order_status"),
        "in_short": "This decision could not be shown; see the problems.",
        "numbers": None,
        "steps": [_step(n, key, title, actor, "not_recorded", NOT_RECORDED)
                  for n, key, title, actor in STEPS],
        "order_events": [], "record_available": False, "_problems": [],
    }


def _build(snap: JournalSnapshot, run_id: str, row: dict | None, record: dict | None,
           reasons: dict) -> dict:
    extra: list[dict] = []
    status = ((record or {}).get("outcome") or {}).get("status")
    err = (record or {}).get("error")
    if status in ("error", "stopped", "not_started"):
        what = {"error": "stopped on an error", "stopped": "was stopped by a daily cap",
                "not_started": "did not start"}[status]
        why = (f"{cap(err.get('type'), 40)}, {cap(err.get('reason_code'), 40)}"
               if isinstance(err, dict) else "type not recorded")
        extra.append({"level": "error" if status == "error" else "warn",
                      "text": f"the decision {what} ({why})"})
    entry, timeout, events = _journal_parts(snap, run_id, row)
    symbol = cap((row or {}).get("symbol") or (record or {}).get("symbol"), 20)
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
        _trader_step(record, row, entry, reasons),
        _ticket_step(record, row, entry, limit),
        _approval_step(record, row, entry, timeout, events),
    ]
    return {
        "run_id": run_id,
        "symbol": symbol,
        "at": (row or {}).get("at") or (record or {}).get("checked_at"),
        "day": (row or {}).get("day"),
        "outcome": (row or {}).get("outcome"),
        "why": (row or {}).get("why"),
        "order_status": (row or {}).get("order_status"),
        "in_short": _in_short(row, numbers, symbol, record),
        "numbers": numbers,
        "steps": steps,
        "order_events": events,
        "record_available": record is not None,
        "_problems": extra,
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


def _in_short(row: dict | None, numbers: dict | None, symbol: str | None,
              record: dict | None) -> str:
    """A fixed template over the recorded outcome and numbers: never model text."""
    sym = symbol or "this stock"
    if row is None:
        status = ((record or {}).get("outcome") or {}).get("status")
        fixed = {"error": "it stopped on an error", "stopped": "a daily cap stopped it",
                 "not_started": "it stopped before the analysts (see the problems)",
                 "skipped": "it was skipped before any AI ran"}.get(str(status))
        tail = f" The record says {fixed}." if fixed else ""
        return f"The journal has no outcome for {sym}; only the decision record was found.{tail}"
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
    lead = (f"The ticket was for {shares} shares of {sym}." if shares is not None
            else f"The Trader chose a plan to buy {sym}.")
    tail = f" Order: {status}." if status else ""
    loss = (numbers or {}).get("max_loss")
    risk = f" Worst case at the stop: ${loss}." if loss and row["why"]["code"] == "approved" \
        else ""
    return f"{lead} {why}.{tail}{risk}"


def _record_status(record: dict | None) -> str | None:
    return ((record or {}).get("outcome") or {}).get("status")


def _not_reached(record: dict | None, row: dict | None) -> bool:
    """True when the decision stopped before the analysts (a skip, or a cap before them)."""
    if row is not None and row["outcome"] == "SKIPPED":
        return True
    return _record_status(record) in ("skipped", "not_started")


def _stopped_here(record: dict | None) -> bool:
    """The record says the decision stopped on an error or a cap after it started: a step
    whose data is missing is where it stopped (failed), never "not reached"."""
    return _record_status(record) in ("error", "stopped")


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
                     f"A run_day scan listed it at {_hhmm(found)}.", {"scan_at": found})
    return _step(n, key, title, actor, "not_recorded",
                 "No run_day scan listed it before this decision (a manual or test run), or "
                 "the scan is not recorded.")


def _event_step(record: dict | None, row: dict | None) -> dict:
    n, key, title, actor = STEPS[1]
    cov = (record or {}).get("coverage")
    if isinstance(cov, dict):
        if cov.get("covered") is True:
            last = cap(cov.get("last_item_202"), 40)
            filed = f" (last SEC 8-K item 2.02 filed {last})" if last else ""
            return _step(n, key, title, actor, "done",
                         f"Proven to report earnings{filed}. The earnings and halt check "
                         "itself is at step 7.",
                         {"coverage": {
                             "covered": True, "last_item_202": last,
                             "reason": cap(cov.get("reason"), SHORT_CAP),
                             "fetch_failed": cov.get("fetch_failed")}})
        reason = cap(cov.get("reason"), SHORT_CAP) or "quarterly reporting not proven"
        state = "failed" if cov.get("fetch_failed") else "blocked"
        return _step(n, key, title, actor, state,
                     f"Not proven to report earnings ({reason}), so no AI time was spent.")
    if row is not None and row["outcome"] == "SKIPPED":
        return _step(n, key, title, actor, "blocked",
                     f"Not proven to report earnings ({row['why']['detail'] or 'see why'}), so "
                     "no AI time was spent.")
    if row is not None and row["outcome"] in ("BUY", "PASS"):
        return _step(n, key, title, actor, "done",
                     "Passed: the decision went on to the analysts. The details are not "
                     "recorded.")
    return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)


def _analysts_step(record: dict | None, row: dict | None) -> dict:
    n, key, title, actor = STEPS[2]
    if _not_reached(record, row):
        return _step(n, key, title, actor, "not_reached", "Not reached.")
    if record is None:
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    if not record["analysts"]:
        if _stopped_here(record):
            return _step(n, key, title, actor, "failed",
                         "The decision stopped here: no analyst report was recorded.")
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    out, failed, missing = [], [], []
    for agent in ("technical", "sentiment", "fundamentals"):
        a = record["analysts"].get(agent)
        if not isinstance(a, dict):
            missing.append(AGENT_NAME[agent])
            out.append({"agent": agent, "name": AGENT_NAME[agent], "ok": None,
                        "reason_code": None, "stance": None, "confidence": None,
                        "evidence": []})
            continue
        rep = a.get("report") or {}
        run = a.get("run") or {}
        code = cap(run.get("reason_code"), 40)
        if run.get("ok") is False:
            ok: bool | None = False
            failed.append(f"{AGENT_NAME[agent]} ({code or 'failed'})")
        elif not a.get("report"):
            ok = None  # nothing recorded for this analyst: never a guessed "done"
            missing.append(AGENT_NAME[agent])
        else:
            ok = True
        out.append({
            "agent": agent, "name": AGENT_NAME[agent], "ok": ok,
            "reason_code": code,
            "stance": cap(rep.get("stance"), 20) if ok else None,
            "confidence": rep.get("confidence") if ok else None,
            "evidence": ([{"point": cap(e.get("point")), "source": cap(e.get("source"), 60)}
                          for e in (rep.get("evidence") or [])[:10]] if ok else []),
        })
    if failed:
        return _step(n, key, title, actor, "failed",
                     f"Failed: {', '.join(failed)}. A failed analyst forces a PASS.",
                     {"analysts": out})
    parts = [f"{a['name']} {a['stance']}" if a["ok"] else f"{a['name']} not recorded"
             for a in out]
    state = "not_recorded" if len(missing) == 3 else "done"
    return _step(n, key, title, actor, state, f"{', '.join(parts)}.", {"analysts": out})


def _plans_step(record: dict | None, row: dict | None) -> dict:
    n, key, title, actor = STEPS[3]
    if _not_reached(record, row):
        return _step(n, key, title, actor, "not_reached", "Not reached.")
    if record is None:
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    chosen = (record.get("trader") or {}).get("plan_id")
    plans = [{"plan_id": cap(p.get("plan_id"), 40), "setup": cap(p.get("setup"), 20),
              "entry": cap(p.get("entry"), 40), "stop": cap(p.get("stop"), 40),
              "target": cap(p.get("target"), 40),
              "reward_to_risk": cap(p.get("reward_to_risk"), 40),
              "chosen": chosen is not None and p.get("plan_id") == chosen}
             for p in record["exit_plans"]]
    if not plans:
        if _stopped_here(record) and not record["analysts"]:
            return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
        return _step(n, key, title, actor, "skipped", "No plan was made.", {"plans": []})
    word = "plan" if len(plans) == 1 else "plans"
    pick = f" The Trader picked {cap(chosen, 40)}." if chosen else ""
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
        if _stopped_here(record) and record["analysts"]:
            return _step(n, key, title, actor, "failed",
                         "The decision stopped here: no debate was recorded.")
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    if d.get("skipped"):
        code = str(d.get("skip_reason") or "").split(":", 1)[0]
        text = SKIP_TEXT.get(code, cap(code, 60) or "skipped")
        return _step(n, key, title, actor, "skipped", f"Skipped: {text}.",
                     {"skip_code": cap(code, 60)})
    bull = d.get("final_bull") or {}
    bear = d.get("final_bear") or {}
    turns, failed_sides = [], set()
    for t in (d.get("turns") or [])[:12]:
        case = t.get("case") or {}
        ok = (t.get("run") or {}).get("ok") is not False
        if not ok:
            failed_sides.add(t.get("side"))
        turns.append({
            "round": t.get("round"), "side": t.get("side"), "ok": ok,
            "argument": cap(case.get("argument")),
            "points": [cap(p, 300) for p in (case.get("key_points") or
                                              case.get("objections") or [])[:8]],
            "fatal": case.get("fatal") if t.get("side") == "bear" else None,
        })
    details = {
        "rounds_run": d.get("rounds_run"), "rounds_planned": d.get("rounds_planned"),
        "fatal": bool(d.get("fatal")), "stopped_early": bool(d.get("stopped_early")),
        "all_ok": d.get("all_ok"),
        "bull": cap(bull.get("argument"), 300),
        "bear": cap(((bear.get("objections") or [None])[0]), 300),
        "turns": turns,
    }
    run_n = d.get("rounds_run")
    rounds = ("Rounds not recorded" if run_n is None
              else f"{run_n} round{'s' if run_n != 1 else ''}")
    if "bear" in failed_sides:
        return _step(n, key, title, actor, "failed",
                     f"{rounds}. The Bear's call failed, so the debate fails closed and "
                     "forces a PASS.", details)
    if "bull" in failed_sides:
        return _step(n, key, title, actor, "failed",
                     f"{rounds}. The Bull's call failed.", details)
    if d.get("fatal"):
        return _step(n, key, title, actor, "blocked",
                     f"{rounds}. The Bear raised a fatal objection, which forces a PASS.",
                     details)
    return _step(n, key, title, actor, "done",
                 f"{rounds}. The Bear found no deal-breaker.", details)


def _forced(code: str, reasons: dict) -> tuple[str, str]:
    """(state, line) for a forced pass, from the fixed texts in reasons.json."""
    text = reasons["why"].get(f"pass:{code}") or cap(code, 40)
    if code in PRE_CALL:
        return "blocked", f"Forced PASS without a Trader call. {text}."
    return "failed", f"The Trader's call failed, so the decision passed. {text}."


def _trader_step(record: dict | None, row: dict | None, entry: dict, reasons: dict) -> dict:
    n, key, title, actor = STEPS[5]
    if _not_reached(record, row):
        return _step(n, key, title, actor, "not_reached", "Not reached.")
    t = (record or {}).get("trader")
    if isinstance(t, dict):
        forced = t.get("forced_pass")
        conf = t.get("confidence")
        details = {"action": t.get("action"), "plan_id": cap(t.get("plan_id"), 40),
                   "confidence": conf, "forced_pass": cap(forced, 40),
                   "reasoning": None if forced else cap(t.get("reasoning"), 1200)}
        if forced:
            state, line = _forced(forced, reasons)
            return _step(n, key, title, actor, state, line, details)
        sure = f", {conf} sure" if conf is not None else ""
        if t.get("action") == "BUY":
            return _step(n, key, title, actor, "done",
                         f"BUY {cap(t.get('plan_id'), 40)}{sure}.", details)
        return _step(n, key, title, actor, "done", f"PASS{sure}.", details)
    if record is not None and _stopped_here(record) and isinstance(record.get("debate"), dict):
        return _step(n, key, title, actor, "failed",
                     "The decision stopped here: no Trader decision was recorded.")
    # no Trader in the record: the journal still says what the Trader chose
    if entry.get("status") == "pass":
        forced = entry.get("forced_pass")
        if forced:
            state, line = _forced(str(forced), reasons)
            return _step(n, key, title, actor, state, f"{line} The reasoning is not recorded.")
        return _step(n, key, title, actor, "done", "PASS. The reasoning is not recorded.")
    if row is not None and row["outcome"] == "BUY" and isinstance(entry.get("plan_id"), str):
        return _step(n, key, title, actor, "done",
                     f"BUY {cap(entry['plan_id'], 40)}. The reasoning is not recorded.")
    return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)


def _ticket_step(record: dict | None, row: dict | None, entry: dict,
                 limit: str | None) -> dict:
    n, key, title, actor = STEPS[6]
    status = entry.get("status")
    if row is None:
        if _not_reached(record, None) or _record_status(record) == "pass":
            return _step(n, key, title, actor, "not_reached", "Not reached.")
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    if row["outcome"] in ("SKIPPED", "PASS"):
        return _step(n, key, title, actor, "not_reached", "Not reached: no plan was chosen.")
    verdict = entry.get("verdict") if isinstance(entry.get("verdict"), dict) else {}
    reasons = [cap(r, 300) for r in (verdict.get("reasons") or [])[:10]]
    details = {"gate": row["gate"], "risk": verdict.get("verdict"), "risk_reasons": reasons,
               "stage": entry.get("stage"), "limit_price": limit,
               "decided_by": _str(entry.get("decided_by")),
               # an approved entry holds the approval re-check's gate and verdict (#112)
               "from_recheck": status == APPROVED}
    if status == "no_order":
        state = "failed" if entry.get("stage") == "events" else "blocked"
        return _step(n, key, title, actor, state, f"No order: {row['why']['text']}.", details)
    if status in ("journal_locked", "journal_error"):
        return _step(n, key, title, actor, "done",
                     "The ticket was made and shown; its numbers are not in this entry.",
                     details)
    if status not in TICKET_STATUSES:
        return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
    order = entry.get("order") if isinstance(entry.get("order"), dict) else {}
    lim = limit or "—"
    risk = verdict.get("verdict") or "—"
    return _step(n, key, title, actor, "done",
                 f"Buy {order.get('qty')} shares with a limit of {lim}, stop "
                 f"{order.get('stop_loss')}, target {order.get('take_profit')}. Risk check: "
                 f"{risk}; earnings and halt: {row['gate']['status']}.", details)


def _approval_step(record: dict | None, row: dict | None, entry: dict,
                   timeout: dict | None, events: list[dict]) -> dict:
    n, key, title, actor = STEPS[7]
    status = entry.get("status")
    if row is None:
        if _not_reached(record, None) or _record_status(record) == "pass":
            return _step(n, key, title, actor, "not_reached", "Not reached.")
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
        gate = entry.get("gate")
        recheck = (" after the re-check (earnings and halt rechecked)"
                   if isinstance(gate, dict) else " after the re-check")
        tail = f" Order: {row['order_status']['text']}." if row.get("order_status") else ""
        return _step(n, key, title, actor, "done",
                     f"Approved at {_hhmm(entry.get('approved_at'))}{recheck}.{tail}", details)
    if status in ("journal_locked", "journal_error"):
        return _step(n, key, title, actor, "failed", f"{why}.", details)
    if row["why"]["code"] == "timeout_no_outcome":
        return _step(n, key, title, actor, "failed", f"{why}.", details)
    # A human answer other than yes, and no answer in time, are neither the code refusing
    # nor a failure: each has its own neutral state. Only run_day's timeout note tells them
    # apart (the journal's "declined" entry is the same for both); a decline without that
    # note is shown as a decline.
    if row["why"]["code"] == "declined:timeout":
        return _step(n, key, title, actor, "unanswered", f"{why}.", details)
    if status == "declined":
        return _step(n, key, title, actor, "declined", f"{why}.", details)
    # "blocked" is only the code refusing: the re-check, the gate, a stale plan
    if status in TICKET_STATUSES:
        return _step(n, key, title, actor, "blocked", f"{why}.", details)
    return _step(n, key, title, actor, "not_recorded", NOT_RECORDED)
