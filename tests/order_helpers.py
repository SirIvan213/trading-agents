"""Shared offline helpers for the Phase 4B order-path tests (no network, no Claude API)."""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agents.schemas import ExitPlanRecord
from journal.log import APPROVED, Journal
from pipeline.ledger import PlanLedger

NY = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 10, 19, 10, 15, tzinfo=NY)  # a Monday
T0 = AS_OF + timedelta(seconds=30)  # "just after" the plan was made


def plan(plan_id: str = "plan-1", **over) -> ExitPlanRecord:
    """TSLA plan: entry 364.18, stop 358.32, target 370.04 (risk 5.86). Sizes to 8 shares."""
    d = {
        "plan_id": plan_id, "symbol": "TSLA", "setup": "level", "entry": "364.18",
        "stop": "358.32", "target": "370.04", "risk_per_share": "5.86",
        "reward_to_risk": "1.00", "time_exit": "15:55", "entry_level": "last_close",
        "structure_level": "vwap", "as_of": AS_OF.isoformat(),
    }
    d.update(over)
    return ExitPlanRecord(**d)


def sealed_ledger(plans: Iterable[ExitPlanRecord] | None = None, symbol: str = "TSLA",
                  accept: bool = True) -> PlanLedger:
    led = PlanLedger(symbol)
    for p in [plan()] if plans is None else plans:
        led.record(p)
    led.seal(accept=accept)
    return led


class StepClock:
    """A clock that returns the given times in order, then repeats the last. Counts calls."""

    def __init__(self, *times: datetime) -> None:
        self.times = list(times)
        self.calls = 0

    def __call__(self) -> datetime:
        i = min(self.calls, len(self.times) - 1)
        self.calls += 1
        return self.times[i]


def approved_entry(when: datetime, max_loss: str = "1.00") -> dict:
    """A journal entry as order_flow writes it (only the fields day_state reads)."""
    return {"status": APPROVED, "approved_at": when.isoformat(), "max_loss": max_loss}


def journal_with(path: Path, entries: Iterable[dict]) -> Journal:
    j = Journal(path)
    for e in entries:
        j.append(e)
    return j


def raw_lines(j: Journal) -> list[dict]:
    if not j.path.exists():
        return []
    return [json.loads(x) for x in j.path.read_text(encoding="utf-8").splitlines() if x.strip()]
