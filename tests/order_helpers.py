"""Shared offline helpers for the Phase 4B order-path tests (no network, no Claude API)."""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime, timedelta
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
                 age_s: float = 1, quiet_gate: bool = True) -> None:
        # quiet_gate: the event gate (#112) reads CLEAR from the wrapped data, so a test of the
        # order flow is not decided by the fixture's halt headline; False keeps the raw data
        self._inner = QuietGateData(inner) if quiet_gate else inner
        self._clock = clock
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


# ---- event gate inputs (#112): real tools.event_gate results built from small, honest inputs ----

def fmp_row(symbol: str, day: str, time: str | None = "bmo", confirmed: bool = True,
            **over) -> dict:
    """One FMP /stable/earnings-calendar row as returned with includeReportTimes=true (same
    shape as tests/fixtures/scenarios/build.py fmp_row and tests/fixtures/fmp/)."""
    row = {"symbol": symbol, "date": day, "epsActual": None, "epsEstimated": 1.12,
           "revenueActual": None, "revenueEstimated": 412000000, "time": time,
           "periodEnding": "2026-09-30", "fiscalPeriod": "Q3", "fiscalYear": 2026,
           "confirmed": confirmed, "lastUpdated": "2026-11-02"}
    row.update(over)
    return row


def calendar_ok(now: datetime, rows=(), *, fetched_at: datetime | None = None,
                start: date | None = None, end: date | None = None,
                background: bool = True) -> dict:
    """A parse_fmp_calendar result fetched a minute before `now`, covering [today, today+14].
    Other companies' background rows across the range are added unless background=False (a
    real whole-market reply is never empty or cut short; the gate treats those as UNKNOWN)."""
    from tests.fakes import background_rows
    from tools.earnings_calendar import parse_fmp_calendar
    today = now.astimezone(NY).date()
    start, end = start or today, end or today + timedelta(days=14)
    extra = background_rows(start, end) if background else []
    return parse_fmp_calendar([*rows, *extra], fetched_at or now - timedelta(minutes=1),
                              start, end)


def submissions_202(now: datetime, days_ago: float = 30, items: str = "2.02,9.01",
                    symbol: str = "TSLA") -> dict:
    """EDGAR submissions with one 8-K carrying `items`, accepted `days_ago` days before `now`."""
    accepted = (now - timedelta(days=days_ago)).astimezone(ZoneInfo("UTC"))
    stamp = accepted.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "cik": "0000012345", "name": f"{symbol} (fixture)", "tickers": [symbol],
        "filings": {"recent": {
            "accessionNumber": ["0000012345-26-000001"], "filingDate": [stamp[:10]],
            "acceptanceDateTime": [stamp], "form": ["8-K"], "items": [items],
            "primaryDocument": ["8k.htm"]}, "files": []},
    }


def filings_ok(now: datetime, days_ago: float = 30, items: str = "2.02,9.01",
               symbol: str = "TSLA") -> dict:
    return {"ok": True, "submissions": submissions_202(now, days_ago, items, symbol)}


def news_item(symbol: str, headline: str, created_at: datetime) -> dict:
    t = created_at.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": 1, "headline": headline, "summary": "", "author": "fixture",
            "source": "fixture", "url": "https://example.invalid/n", "symbols": [symbol],
            "created_at": t, "updated_at": t}


def news_ok(items=()) -> dict:
    return {"ok": True, "news": {"news": list(items), "next_page_token": None}}


def window_end_for(now: datetime) -> datetime:
    return now.astimezone(NY).replace(hour=15, minute=55, second=0, microsecond=0)


def gate_for(symbol: str, now: datetime, *, calendar=None, filings=None, news=None,
             window_end: datetime | None = None, **kw) -> dict:
    """A real event_gate result; every input defaults to its clear form."""
    from tools.event_gate import event_gate
    return event_gate(
        symbol, now, window_end or window_end_for(now),
        calendar=calendar if calendar is not None else calendar_ok(now),
        filings=filings if filings is not None else filings_ok(now, symbol=symbol),
        news=news if news is not None else news_ok(), **kw)


def clear_gate(symbol: str, now: datetime) -> dict:
    """A real CLEAR event-gate result for `symbol` at `now` (asserted, so a broken helper cannot
    silently turn a risk test into something else)."""
    gate = gate_for(symbol, now)
    assert gate["status"] == "CLEAR", gate["reason"]
    return gate


def events_clear():
    """An EventSource that reports CLEAR for whatever (symbol, now, window_end) it is asked."""
    calls: list[tuple] = []

    def source(symbol: str, now: datetime, window_end: datetime) -> dict:
        calls.append((symbol, now, window_end))
        return gate_for(symbol, now, window_end=window_end)

    source.calls = calls  # type: ignore[attr-defined]
    return source


def events_of(**inputs):
    """An EventSource built from gate_for inputs (calendar=, filings=, news=...)."""
    def source(symbol: str, now: datetime, window_end: datetime) -> dict:
        return gate_for(symbol, now, window_end=window_end, **inputs)
    return source


class QuietGateData:
    """Wraps a fake data object so the event gate reads CLEAR from it: an FMP calendar with
    only other companies' rows, an item 2.02 filing 30 days ago, and the news without halt or
    earnings
    headlines. Every other attribute is the wrapped object's. Used where a test is about
    something else (the order flow) and runs through pipeline.decide.run_decision (#112)."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def earnings_calendar(self, start: date, end: date) -> dict:
        fetched = datetime.combine(start, datetime.min.time().replace(hour=8, minute=30),
                                   tzinfo=NY)
        from tests.fakes import background_rows
        return {"fetched_at": fetched.isoformat(), "from": start.isoformat(),
                "to": end.isoformat(), "reply": background_rows(start, end)}

    def submissions(self, symbol: str):
        return submissions_202(datetime(2026, 11, 3, 12, 0, tzinfo=NY), 30, symbol=symbol)

    def news(self, symbols, start, end):
        out = self._inner.news(symbols, start, end)
        words = ("halt", "earnings", "quarterly results")
        # Quiet = no halt or earnings headline AND nothing unreadable: the gate treats an
        # item it cannot read (no valid time or headline) as a possible halt (UNKNOWN).
        out["news"] = [n for n in out["news"]
                       if isinstance(n, dict) and isinstance(n.get("headline"), str)
                       and n["headline"].strip() and _readable_time(n.get("created_at"))
                       and not any(w in n["headline"].lower() for w in words)]
        return out


def _readable_time(value) -> bool:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt.tzinfo is not None


def gate_for_order(order, now):
    """A real CLEAR gate for the order's symbol at `now`, for tests of other risk rules that
    call check_order directly. None when `now` is not a usable clock (check_order blocks that
    before it reads the gate). An order with no symbol gets a gate with an empty symbol."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return None
    raw = order.get("symbol") if isinstance(order, dict) else None
    sym = raw.strip().upper() if isinstance(raw, str) else ""
    try:
        gate = clear_gate(sym or "TSLA", now)
    except (OverflowError, ValueError):  # a clock at the edge of the date range
        return None
    if not sym:
        gate["symbol"] = ""
    return gate


# ---- check_order on a real sized order, judged only by the gate (#112) ----

def _example_limits():
    from tools.config import CONFIG_DIR, load_limits
    return load_limits(CONFIG_DIR / "limits.example.json")


def sized_order(symbol: str = "TSLA") -> dict:
    """The TSLA test order (8 shares, 0.37 allowance) from size_position, under example limits."""
    from tools.size import Plan, size_position
    return size_position(Plan(symbol, Decimal("364.18"), Decimal("358.32"), Decimal("370.04")),
                         _example_limits(), slippage=Decimal("0.37"))["order"]


def verdict_for(gate, now: datetime, symbol: str = "TSLA") -> dict:
    """check_order for the sized order at `now` with this gate result and nothing else wrong."""
    from tools.risk_check import DayState, check_order
    return check_order(sized_order(symbol), _example_limits(), DayState(), now, gate=gate)
