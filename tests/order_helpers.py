"""Shared offline helpers for the Phase 4B order-path tests (no network, no Claude API)."""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
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
        "structure_level": "vwap", "as_of": AS_OF.isoformat(), "last_close": "364.18",
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


# ---- live quotes and slippage (#68) ----

def fresh_quote(entry: str | Decimal, now: datetime, spread: str = "0.02",
                age_s: float = 1) -> dict:
    """An Alpaca latestQuote whose ask equals `entry` and whose bid is `spread` below it,
    stamped `age_s` seconds before `now` (a naive `now` gives a quote at the same wall time)."""
    ask = Decimal(str(entry))
    bid = ask - Decimal(spread)
    t = now - timedelta(seconds=age_s)
    return {"ap": float(ask), "bp": float(bid), "t": t.isoformat()}


class QuoteSource:
    """A `Quotes` callable (symbol -> latestQuote) that counts its reads and remembers symbols.
    `quote` may be a dict, None, or a callable(symbol) -> object (to raise or change per read)."""

    def __init__(self, quote: object) -> None:
        self.quote = quote
        self.calls = 0
        self.symbols: list[str] = []

    def __call__(self, symbol: str) -> object:
        self.calls += 1
        self.symbols.append(symbol)
        return self.quote(symbol) if callable(self.quote) else self.quote


def quotes_at(now: datetime, entry: str | Decimal = "364.18", spread: str = "0.02",
              age_s: float = 1) -> QuoteSource:
    """A quote source for the TSLA test plan, fresh at `now` (ask == entry)."""
    return QuoteSource(fresh_quote(entry, now, spread, age_s))


def with_slippage(order: dict, limits, slippage: str | Decimal | None = None) -> dict:
    """A copy of a hand-built order with a valid slippage allowance (default: the minimum
    allowance for its entry), so risk_check judges the rule under test and not the missing field."""
    from tools.slippage import minimum_allowance
    out = dict(order)
    out["slippage"] = str(minimum_allowance(Decimal(str(order["entry"])), limits)
                          if slippage is None else slippage)
    return out


class QuotingData:
    """Wraps a fake market-data object (FakeAllData) so snapshots() also returns a live
    latestQuote for `symbol`, stamped `age_s` before the clock's latest reading (read without
    advancing a StepClock). Lets run_decision's `quotes_from(data)` find a fresh quote at the
    time propose() and approve() read it (#68). Every other attribute is the wrapped object's."""

    def __init__(self, inner, clock, symbol: str, entry: str, spread: str = "0.02",
                 age_s: float = 1) -> None:
        self._inner, self._clock = inner, clock
        self.symbol, self.entry, self.spread, self.age_s = symbol, entry, spread, age_s
        self.quote_reads = 0

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def _last_reading(self) -> datetime:
        times = getattr(self._clock, "times", None)
        if times:
            return times[max(0, min(self._clock.calls - 1, len(times) - 1))]
        return self._clock()

    def snapshots(self, symbols: list[str]) -> dict:
        out = self._inner.snapshots(symbols)
        if self.symbol in symbols and out.get(self.symbol) is None:
            self.quote_reads += 1
            q = fresh_quote(self.entry, self._last_reading(), self.spread, self.age_s)
            out[self.symbol] = {"latestQuote": q}
        return out
