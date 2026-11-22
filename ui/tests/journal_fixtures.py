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
