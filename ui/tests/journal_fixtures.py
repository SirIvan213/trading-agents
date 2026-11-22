"""Journal fixtures written by the journal's own code (Journal.append and
Journal.record_order_event check every lifecycle step), so they can never drift from what the
pipeline writes."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from journal.log import Journal
from journal.orders import APPROVED, client_order_id

NY = ZoneInfo("America/New_York")
MONDAY = datetime(2026, 11, 23, 10, 0, tzinfo=NY)


def approved_entry(run_id: str, plan_id: str, at: datetime, *, symbol: str = "TSLA",
                   qty: int = 8, max_loss: str = "49.84",
                   expires_in_s: int = 300) -> dict:
    """An approved_not_sent entry in the shape pipeline.order_flow.approve writes."""
    return {
        "status": APPROVED, "reason": None, "run_id": run_id, "plan_id": plan_id,
        "symbol": symbol,
        "order": {"symbol": symbol, "side": "buy", "qty": qty, "entry": "250.00",
                  "stop_loss": "243.77", "take_profit": "262.46", "slippage": "0.25"},
        "max_loss": max_loss, "decided_by": "max_loss_per_trade", "quote": None,
        "gate": {"status": "CLEAR", "reason": "no event"}, "verdict": {"verdict": "PASS",
                                                                     "reasons": []},
        "checked_at": at.isoformat(), "approved_at": at.isoformat(), "plan_age_s": 40,
        "client_order_id": client_order_id(run_id, plan_id),
        "expires_at": (at + timedelta(seconds=expires_in_s)).isoformat(),
    }


def write_day(journal: Journal, day: datetime = MONDAY) -> None:
    """A realistic day, through Journal.append and Journal.record_order_event (which check
    every lifecycle step): a skip, a PASS, a decline, an order filled and closed at the stop
    yesterday, an order sent today and filled, and an approval still waiting to be sent."""
    y = day - timedelta(days=3)  # the Friday before
    journal.append({"status": "skipped", "symbol": "SAP", "run_id": "r-skip",
                    "reason": "no 8-K item 2.02", "checked_at": day.isoformat()})
    journal.append({"status": "pass", "run_id": "r-pass", "symbol": "AAPL",
                    "forced_pass": None, "checked_at": day.isoformat()})
    journal.append({**approved_entry("r-dec", "p-dec", day), "status": "declined",
                    "reason": "not approved"})
    journal.append(approved_entry("r-old", "p-old", y, symbol="NVDA", qty=5, max_loss="30.00"))
    cid_old = client_order_id("r-old", "p-old")
    journal.record_order_event(cid_old, "submitting", y + timedelta(seconds=5),
                               limit_price="250.25")
    journal.record_order_event(cid_old, "submitted", y + timedelta(seconds=6))
    journal.record_order_event(cid_old, "filled", y + timedelta(minutes=2), filled_qty=5,
                               avg_price="250.10")
    journal.record_order_event(cid_old, "closed", day - timedelta(minutes=5),
                               closed_by="stop", realized_pnl="-31.65")
    journal.append(approved_entry("r-new", "p-new", day, symbol="TSLA"))
    cid_new = client_order_id("r-new", "p-new")
    journal.record_order_event(cid_new, "submitting", day + timedelta(seconds=5),
                               limit_price="250.25")
    journal.record_order_event(cid_new, "submitted", day + timedelta(seconds=6))
    journal.record_order_event(cid_new, "filled", day + timedelta(minutes=1), filled_qty=8,
                               avg_price="250.05")
    journal.append(approved_entry("r-wait", "p-wait", day + timedelta(minutes=10),
                                  symbol="AMD", qty=4, max_loss="20.00"))


def outcome_entry(status: str, run_id: str, plan_id: str | None, at: datetime, *,
                  symbol: str, reason: str | None = None, order: bool = True,
                  gate: dict | None = None, **extra: object) -> dict:
    """An outcome entry in the shape pipeline.order_flow._entry writes (no_order, declined,
    refused_after_approval, changed_after_approval): the order fields only when it had one."""
    o = {"symbol": symbol, "side": "buy", "qty": 5, "entry": "101.20", "stop_loss": "99.80",
         "take_profit": "104.00", "slippage": "0.10"} if order else None
    return {
        "status": status, "reason": reason, "run_id": run_id, "plan_id": plan_id,
        "symbol": symbol, "order": o, "max_loss": "7.50" if order else None,
        "decided_by": "max_loss_per_trade" if order else None, "quote": None, "gate": gate,
        "verdict": None, "checked_at": at.isoformat(), **extra,
    }


HISTORY_NOW = MONDAY + timedelta(hours=2)  # the clock the history tests read at


def write_history(journal: Journal, day: datetime = MONDAY) -> None:
    """Every outcome kind History must show, written by Journal.append and
    Journal.record_order_event (which validate every order lifecycle step)."""
    t = lambda minutes: day + timedelta(minutes=minutes)  # noqa: E731
    a = journal.append
    a({"status": "run_day_start", "day": day.date().isoformat(), "at": t(-20).isoformat(),
       "pid": 1, "caps": {}, "universe": []})
    a({"status": "run_day_scan", "at": t(-19).isoformat(), "ok": True, "candidates": [],
       "rejected": 0})
    # a decision from the Friday before (the date filter)
    a({"status": "pass", "run_id": "r-fri", "symbol": "BAC", "forced_pass": None,
       "checked_at": (day - timedelta(days=3)).isoformat()})
    a({"status": "skipped", "symbol": "SAP", "run_id": "r-skip",
       "reason": "no 8-K item 2.02 in the last 100 days", "checked_at": t(1).isoformat()})
    a({"status": "pass", "run_id": "r-pass", "symbol": "AAPL", "forced_pass": None,
       "checked_at": t(2).isoformat()})
    a({"status": "pass", "run_id": "r-fatal", "symbol": "VNTR", "forced_pass": "fatal_bear",
       "checked_at": t(3).isoformat()})
    a({"status": "pass", "run_id": "r-stale", "symbol": "KO",
       "forced_pass": "stale_before_trader", "checked_at": t(4).isoformat()})
    a(outcome_entry("no_order", "r-risk", "p-risk", t(5), symbol="NVDA", stage="risk",
                    reason="event gate: EVENT (earnings 2026-11-24 before the open)",
                    gate={"status": "EVENT", "reason": "earnings 2026-11-24 before the open"}))
    a(outcome_entry("no_order", "r-quote", "p-quote", t(6), symbol="AMD", stage="quote",
                    reason="live quote unavailable (TimeoutError)", order=False))
    a(outcome_entry("declined", "r-timeout", "p-to", t(7), symbol="MSFT",
                    reason="not approved", gate={"status": "CLEAR", "reason": "no event"}))
    a({"status": "run_day_approval_timeout", "at": t(8).isoformat(), "symbol": "MSFT",
       "run_id": "r-timeout"})
    a(outcome_entry("declined", "r-decl", "p-decl", t(9), symbol="META",
                    reason="not approved", gate={"status": "CLEAR", "reason": "no event"}))
    a(outcome_entry("refused_after_approval", "r-refused", "p-ref", t(10), symbol="INTC",
                    reason="re-check failed at size: quote is stale",
                    gate={"status": "CLEAR", "reason": "no event"}, recheck_at=t(11).isoformat()))
    a(outcome_entry("changed_after_approval", "r-changed", "p-chg", t(12), symbol="UBER",
                    reason="the re-built order differs from the approved ticket",
                    gate={"status": "CLEAR", "reason": "no event"}, recheck_at=t(12).isoformat()))
    # approvals: one still live, one expired unsent, one closed at the stop, one at the target,
    # one rejected by the broker
    a(approved_entry("r-live", "p-live", t(13), symbol="AMZN", expires_in_s=86_400))
    a(approved_entry("r-expired", "p-exp", t(14), symbol="GOOGL", expires_in_s=300))
    a(approved_entry("r-stop", "p-stop", t(15), symbol="TSLA", qty=5, max_loss="31.65"))
    cid = client_order_id("r-stop", "p-stop")
    journal.record_order_event(cid, "submitting", t(15.1), limit_price="250.25")
    journal.record_order_event(cid, "submitted", t(15.2))
    journal.record_order_event(cid, "filled", t(16), filled_qty=5, avg_price="250.10")
    journal.record_order_event(cid, "closed", t(40), closed_by="stop", realized_pnl="-31.65")
    a(approved_entry("r-target", "p-tgt", t(17), symbol="JPM", qty=4, max_loss="20.00"))
    cid = client_order_id("r-target", "p-tgt")
    journal.record_order_event(cid, "submitting", t(17.1), limit_price="250.25")
    journal.record_order_event(cid, "submitted", t(17.2))
    journal.record_order_event(cid, "filled", t(18), filled_qty=4, avg_price="250.00")
    journal.record_order_event(cid, "closed", t(50), closed_by="target", realized_pnl="45.20")
    a(approved_entry("r-rej", "p-rej", t(19), symbol="PLTR", qty=3, max_loss="9.00"))
    cid = client_order_id("r-rej", "p-rej")
    journal.record_order_event(cid, "submitting", t(19.1), limit_price="250.25")
    journal.record_order_event(cid, "rejected", t(19.2), broker_reason="insufficient buying power")
    # failures: never dropped
    a({"status": "journal_locked", "reason": "journal unavailable after a yes, nothing "
       "approved: journal lock timed out", "run_id": "r-locked", "plan_id": "p-lock",
       "symbol": "XOM", "proposal_checked_at": t(20).isoformat(),
       "checked_at": t(21).isoformat(), "answer": "yes"})
    a({"status": "run_day_alert", "at": t(22).isoformat(),
       "message": "decision for CVX failed (RuntimeError)", "symbol": "CVX"})
    a({"status": "mystery_status", "run_id": "r-odd", "symbol": "DIS",
       "checked_at": t(23).isoformat()})
    a({"status": "run_day_end", "at": t(330).isoformat(), "day": day.date().isoformat(),
       "reason": "entry window closed"})
