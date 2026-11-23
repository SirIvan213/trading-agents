"""GET /api/decisions: the History page (UI-3, #216).

The journal is read lock-free (JournalReader) and its entries are grouped by run_id, one row
per decision. Every outcome kind maps to BUY, PASS, SKIPPED or FAILED; a failed, incomplete
or unrecognized entry is always a row, never dropped:

  skipped                                                   -> SKIPPED
  pass (a Trader PASS, or a forced pass with its code)      -> PASS
  no_order, declined, refused_after_approval,
  changed_after_approval, approved_not_sent (+ order events) -> BUY (the Trader chose a plan;
                                                               the order column says what
                                                               became of it)
  journal_locked, journal_error                             -> FAILED (nothing was approved)
  run_day_alert naming a symbol (a decision that crashed,
  or a PASS that could not be journaled: the two cannot be
  told apart yet, so the why is neutral)                    -> FAILED (keyed "entry-<seq>")
  run_day_approval_timeout with no outcome entry for its run
  (both the decline and its retried record failed)          -> FAILED
  any other entry with a run_id, or an unknown status       -> FAILED, its status as the code

"Why" is a recorded code (the status, the forced-pass code, the no_order stage) mapped to a
fixed plain-English string in ui/api/content/reasons.json; an unknown code is shown as itself.
The journal never holds model text (journal/log.py); a recorded, code-generated reason (a
risk-check reason, a skip reason) is passed along as `detail`.

The event check is the recorded gate {status, reason} of a decision that reached the order
ticket; "check failed" when the gate itself could not run (no_order at stage "events"); else
"not reached": never a guessed CLEAR.

`seq` is an entry's position among the journal's parsed entries (1-based; blank lines are not
counted), so it is not a file line number. A row without a run_id is keyed "entry-<seq>". A
run_day_alert carries no run_id, so a crash after a decision's outcome was written shows as
its own FAILED row next to the decision (a follow-up adds run_id to alerts).

Money values are passed through exactly as recorded (strings). The realized P&L total is the
Decimal sum of the recorded realized_pnl of closed trades, for display only, and null until a
closed event exists.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from journal.log import JournalError
from journal.orders import APPROVED, CLOSED, ORDER_EVENTS, OrderState
from ui.api.readers.journal_snapshot import JournalSnapshot, SnapshotJournal

NY = ZoneInfo("America/New_York")
REASONS = Path(__file__).resolve().parent / "content" / "reasons.json"
OUTCOMES = ("BUY", "PASS", "SKIPPED", "FAILED")
FILTERS = {"all": None, "buy": "BUY", "pass": "PASS", "skipped": "SKIPPED", "failed": "FAILED"}
BUY_STATUSES = frozenset({"no_order", "declined", "refused_after_approval",
                          "changed_after_approval", APPROVED})
FAILED_STATUSES = frozenset({"journal_locked", "journal_error"})
NOT_REACHED = {"status": "not reached", "reason": None}


def load_reasons(path: Path = REASONS) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("why"), dict) or not isinstance(data.get("order_status"), dict):
        raise ValueError("reasons content needs 'why' and 'order_status' maps")
    return data


def _as_recorded(value: object) -> str | None:
    """A recorded value as a string, exactly as written (never re-rounded)."""
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def _when(entry: dict) -> datetime | None:
    for key in ("approved_at", "checked_at", "at", "proposal_checked_at"):
        text = entry.get(key)
        if isinstance(text, str):
            try:
                when = datetime.fromisoformat(text)
                if when.tzinfo is not None:
                    return when.astimezone(NY)
            except (ValueError, OverflowError):  # OverflowError: a time at the date limits
                continue
    return None


@dataclass
class _Row:
    key: str
    seq: int
    entry: dict
    outcome: str
    code: str
    detail: str | None = None
    notes: list[str] = field(default_factory=list)


def _classify(entry: dict) -> tuple[str, str, str | None]:
    """(outcome, why code, detail) for one outcome entry."""
    status = entry.get("status")
    reason = entry.get("reason") if isinstance(entry.get("reason"), str) else None
    if status == "skipped":
        return "SKIPPED", "skipped", reason
    if status == "pass":
        forced = entry.get("forced_pass")
        return "PASS", f"pass:{forced}" if forced else "pass:trader", None
    if status == "no_order":
        return "BUY", f"no_order:{entry.get('stage')}", reason
    if status == APPROVED:
        return "BUY", "approved", None
    if status in BUY_STATUSES:
        return "BUY", str(status), reason
    if status in FAILED_STATUSES:
        return "FAILED", str(status), reason
    return "FAILED", str(status), reason


def _gate(entry: dict) -> dict:
    if entry.get("status") == "no_order" and entry.get("stage") == "events":
        return {"status": "check failed", "reason": _as_recorded(entry.get("reason"))}
    gate = entry.get("gate")
    if isinstance(gate, dict) and gate.get("status"):
        return {"status": str(gate.get("status")), "reason": _as_recorded(gate.get("reason"))}
    return dict(NOT_REACHED)


def decisions(snap: JournalSnapshot, now: datetime, *, outcome: str = "all",
              day_from: date | None = None, day_to: date | None = None, limit: int = 50,
              cursor: int = 0, reasons: dict | None = None) -> dict:
    reasons = reasons or load_reasons()
    problems = snap.problems()
    if snap.tail_bytes and not snap.tail_stuck:
        problems.append({"level": "warn", "text": "the journal's last line is incomplete (a "
                         "write in progress, or cut off): it is not shown yet"})

    try:
        orders: dict[str, OrderState] | None = SnapshotJournal(snap.entries).orders()
    except JournalError as exc:
        orders = None
        problems.append({"level": "error", "text": f"{exc}: order statuses are not shown"})

    rows: dict[str, _Row] = {}
    timeouts: set[str] = set()
    closed_pnl: dict[str, str] = {}  # client_order_id -> realized_pnl as recorded
    timeout_entries: dict[str, tuple[int, dict]] = {}
    for seq, entry in enumerate(snap.entries, 1):
        status = entry.get("status")
        run_id = entry.get("run_id")
        if status in ORDER_EVENTS:
            if status == CLOSED and entry.get("realized_pnl") is not None:
                closed_pnl[str(entry.get("client_order_id"))] = _as_recorded(
                    entry["realized_pnl"])  # type: ignore[assignment]
            continue
        if isinstance(status, str) and status.startswith("run_day_"):
            if status == "run_day_approval_timeout" and isinstance(run_id, str):
                timeouts.add(run_id)
                timeout_entries[run_id] = (seq, entry)
            elif status == "run_day_alert" and isinstance(entry.get("symbol"), str):
                rows[f"entry-{seq}"] = _Row(f"entry-{seq}", seq, entry, "FAILED", "alert",
                                            _as_recorded(entry.get("message")))
            continue  # the day's notes (start, scan, cap, end) are not decisions
        key = run_id if isinstance(run_id, str) and run_id else f"entry-{seq}"
        outcome_, code, detail = _classify(entry)
        if key in rows:  # a second outcome for one run: keep the last, say so
            rows[key].notes.append(f"replaces an earlier {rows[key].entry.get('status')} entry")
            problems.append({"level": "warn", "text": f"run {key} has more than one outcome "
                             "entry; the last is shown"})
        rows[key] = _Row(key, seq, entry, outcome_, code, detail, rows[key].notes
                         if key in rows else [])

    # a timeout whose run left no outcome entry is still a decision: never dropped
    for run_id, (seq, entry) in timeout_entries.items():
        if run_id not in rows:
            rows[run_id] = _Row(run_id, seq, entry, "FAILED", "timeout_no_outcome", None)

    items: list[dict] = []
    for row in rows.values():
        if row.code == "declined" and row.key in timeouts:
            row.code = "declined:timeout"
        items.append(_item(row, orders, closed_pnl, reasons, now))

    in_range = [i for i in items if _in_range(i, day_from, day_to)]
    in_range.sort(key=_newest_first, reverse=True)
    counts = {o: sum(1 for i in in_range if i["outcome"] == o) for o in OUTCOMES}
    pnl = [i["realized_pnl"] for i in in_range if i["realized_pnl"] is not None]
    wanted = FILTERS[outcome]
    shown = [i for i in in_range if wanted is None or i["outcome"] == wanted]
    page = shown[cursor:cursor + limit]
    more = cursor + limit < len(shown)
    return {
        "items": page,
        "tiles": {
            "reviewed": counts["BUY"] + counts["PASS"],
            "buys": counts["BUY"], "passed": counts["PASS"], "skipped": counts["SKIPPED"],
            "failed": counts["FAILED"],
            "realized_pnl": _sum(pnl, problems) if pnl else None,
        },
        "counts": {"all": len(in_range), **{k: counts[v] for k, v in FILTERS.items() if v}},
        "total": len(shown),
        "next_cursor": str(cursor + limit) if more else None,
        "problems": problems,
    }


def _newest_first(item: dict) -> tuple:
    """Rows with a time first, newest first (by instant, not by text: offsets change with
    daylight time); then by position in the journal."""
    at = item["at"]
    return (at is not None, datetime.fromisoformat(at).timestamp() if at else 0.0, item["seq"])


def _sum(values: list[str], problems: list[dict]) -> str | None:
    """The Decimal sum of recorded realized_pnl strings, for display only."""
    total = Decimal("0")
    for v in values:
        try:
            total += Decimal(v)
        except (InvalidOperation, ValueError):
            problems.append({"level": "error", "text": "a recorded realized_pnl is not a "
                             "number: the total is not shown"})
            return None
    return str(total)


def _in_range(item: dict, day_from: date | None, day_to: date | None) -> bool:
    if item["day"] is None:
        return True  # a row whose time can't be read is never filtered away
    d = date.fromisoformat(item["day"])
    return (day_from is None or d >= day_from) and (day_to is None or d <= day_to)


def _item(row: _Row, orders: dict[str, OrderState] | None, closed_pnl: dict[str, str],
          reasons: dict, now: datetime) -> dict[str, Any]:
    e = row.entry
    when = _when(e)
    order = e.get("order") if isinstance(e.get("order"), dict) else {}
    cid = e.get("client_order_id") if e.get("status") == APPROVED else None
    status_code: str | None = None
    if cid is not None and orders is not None and cid in orders:
        state = orders[cid]
        status_code = state.status
        if state.status == APPROVED and not state.live_unsent(now):
            status_code = "approved_expired"
    order_status = ({"code": status_code,
                     "text": reasons["order_status"].get(status_code, status_code)}
                    if status_code else None)
    symbol = e.get("symbol") or order.get("symbol")
    return {
        "run_id": row.key,
        "seq": row.seq,
        "at": when.isoformat() if when else None,
        "day": when.date().isoformat() if when else None,
        "symbol": symbol if isinstance(symbol, str) else None,
        "outcome": row.outcome,
        "why": {"code": row.code, "text": reasons["why"].get(row.code, row.code),
                "detail": row.detail},
        "forced_pass": e.get("forced_pass") if e.get("status") == "pass" else None,
        "order_status": order_status,
        "qty": order.get("qty") if isinstance(order.get("qty"), int) else None,
        "entry": _as_recorded(order.get("entry")),
        "max_loss": _as_recorded(e.get("max_loss")),
        # shown only when the order history replays: never P&L from a history called invalid
        "realized_pnl": closed_pnl.get(str(cid)) if cid and orders is not None else None,
        "plan_id": e.get("plan_id") if isinstance(e.get("plan_id"), str) else None,
        "decided_by": _as_recorded(e.get("decided_by")),
        "gate": _gate(e),
        "notes": row.notes,
    }
