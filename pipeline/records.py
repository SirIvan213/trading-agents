"""Decision records (P5-UI-1, #211): one JSON file per decision, for display only.

When run_decision ends, however it ends (BUY, PASS, a forced pass, a skip, an early stop or a
failure), it writes journal/records/YYYY-MM-DD/<run_id>.json (the New York date the decision
started) through tools.atomic_write. The record holds what the journal deliberately does not:
the analysts' reports and runs, the plans, the debate and the Trader's reasoning. It feeds the
UI's Decision detail page (UI-4) and nothing else.

Rules:
  - display only: no money-path module (tools/, pipeline/order_flow.py, orders.py, send.py,
    budget.py, journal/log.py) reads journal/records/ (tests/test_decision_records.py checks
    it), so model text can never reach sizing, the risk check or an order;
  - best effort with no effect: a record that cannot be built or written (disk full, a
    permission error, an unserializable field) is logged by exception type and dropped; the
    decision's outcome, journal entries, timing and approval flow are exactly the same;
  - errors are recorded by exception type and reason_code only, never by message (CLAUDE.md
    rule 4): an agent run's `reason` and its retries' `reason` are left out for that reason.

The record stores model text (analyst evidence, the Bull's and Bear's cases, the Trader's
reasoning). Whoever displays it treats every string as untrusted plain text.

`checked_at` is the SEC check's clock reading (the moment the decision began; the plan-age
clock starts a little later). `outcome.status` says how it ended; `outcome.approval` keeps the
approval's own status even when a later error ended the decision; "not_started" means a cap
stopped it in before_analysts, so it was never counted and no billed call was made.
`time_budget_s` holds the plan-age limits and the ages measured against them.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from journal.orders import APPROVED
from tools.atomic_write import atomic_write_text

log = logging.getLogger(__name__)
SCHEMA = 1  # bump on any change to the record's shape: UI-4 checks it
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# an agent run without its free-text `reason` (it can hold an error message): reason_code stays
RUN_EXCLUDE: dict[str, Any] = {"reason": True, "retries": {"__all__": {"reason"}}}


def records_dir(journal_path: Path) -> Path:
    """journal/records next to the journal file (journal/decisions.jsonl by default)."""
    return Path(journal_path).parent / "records"


def _run(run: Any) -> dict | None:
    return None if run is None else run.model_dump(mode="json", exclude=RUN_EXCLUDE)


def _reason_code(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "AlreadyDecided":
        return "already_decided"
    if name == "CapReached":
        return "cap_reached"
    if name == "SymbolSkipped":
        return "skipped"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "exception"


def _outcome(result: Any, trader: Any, error: BaseException | None, skipped: bool,
             started: bool) -> dict:
    sent = getattr(result, "sent", None) if result is not None else None
    approval = getattr(result, "outcome", None) if result is not None else None
    out: dict[str, Any] = {"status": None, "record_error": False,
                           "approval": approval.status if approval is not None else None,
                           "sent": getattr(sent, "status", None) if sent is not None else None}
    if skipped:
        out["status"] = "skipped"
    elif error is not None and not started:
        out["status"] = "not_started"  # stopped in before_analysts: never counted, no call
    elif error is not None:
        out["status"] = "stopped" if _reason_code(error) in ("cap_reached",
                                                             "already_decided") else "error"
    elif result is not None and result.outcome is not None:
        out["status"] = result.outcome.status
    elif trader is not None and trader.decision.action == "PASS":
        out["status"] = "pass"
    elif result is not None and result.proposal is not None:
        out["status"] = "not_asked"  # ask=None: the ticket was made, no approval asked
    if result is not None:
        out["record_error"] = bool(getattr(result, "record_error", None))
    return out


def _proposal(result: Any) -> dict | None:
    p = getattr(result, "proposal", None) if result is not None else None
    if p is None:
        return None
    from pipeline.order_flow import limit_price  # the ticket's limit price, from the tool

    outcome = getattr(result, "outcome", None)
    recheck = getattr(outcome, "recheck", None) if outcome is not None else None
    return {"ok": p.ok, "stage": p.stage, "plan_id": p.plan_id, "checked_at": p.checked_at,
            "expires_at": p.expires_at, "limit_price": limit_price(p),
            # the order actually approved: the re-check's (it must equal the ticket's)
            "approved_limit_price": (limit_price(recheck)
                                     if recheck is not None and outcome.status == APPROVED
                                     else None),
            "gate_status": (p.gate or {}).get("status") if isinstance(p.gate, dict) else None}


def build_record(*, run_id: str, symbol: str, checked_at: datetime,
                 coverage: dict | None, packet: Any = None, debate: Any = None,
                 trader: Any = None, result: Any = None,
                 error: BaseException | None = None, started: bool = True) -> dict:
    """The record as written: JSON types only (pydantic's mode="json")."""
    skipped = error is not None and type(error).__name__ == "SymbolSkipped"
    analysts: dict[str, Any] = {}
    if packet is not None:
        for agent in sorted(set(packet.runs) | set(packet.reports)):
            report = packet.reports.get(agent)
            analysts[agent] = {
                "report": report.model_dump(mode="json") if report is not None else None,
                "run": _run(packet.runs.get(agent)),
            }
    debate_out = None
    if debate is not None:
        debate_out = debate.model_dump(mode="json", exclude={"turns": {"__all__": {"run"}}})
        debate_out["turns"] = [
            {**t.model_dump(mode="json", exclude={"run"}), "run": _run(t.run)}
            for t in debate.turns]
    trader_out = None
    if trader is not None:
        trader_out = {**trader.decision.model_dump(mode="json"), "ok": trader.ok,
                      "forced_pass": trader.forced_pass, "run": _run(trader.run)}
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "symbol": symbol,
        "checked_at": checked_at.isoformat(),
        "coverage": ({**{k: coverage.get(k) for k in ("covered", "reason", "last_item_202")},
                      "fetch_failed": bool(coverage.get("fetch_failed"))}
                     if isinstance(coverage, dict) else None),
        "analysts": analysts,
        "packet_ok": packet.all_ok if packet is not None else None,
        "exit_plans": ([p.model_dump(mode="json") for p in packet.exit_plans]
                       if packet is not None else []),
        "debate": debate_out,
        "trader": trader_out,
        "proposal": _proposal(result),
        "outcome": _outcome(result, trader, None if skipped else error, skipped, started),
        "timings_ms": dict(result.timings_ms) if result is not None else {},
        "time_budget_s": dict(result.budget) if result is not None else {},
        "error": (None if error is None or skipped
                  else {"type": type(error).__name__, "reason_code": _reason_code(error)}),
    }


def write_record(directory: Path, day: str, run_id: str, build: Any) -> Path | None:
    """Build (`build()`) and write one record, best effort: any failure is logged by its
    exception type and swallowed. Returns the path written, or None."""
    try:
        if not RUN_ID.match(run_id) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("unsafe record path")
        record = build()
        text = json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False)
        folder = Path(directory) / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{run_id}.json"
        atomic_write_text(path, text + "\n")
        return path
    except Exception as exc:  # noqa: BLE001 - a record never changes the decision
        log.warning("decision record for run %s not written (%s)", run_id[:64],
                    type(exc).__name__)
        return None
