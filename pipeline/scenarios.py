"""Saved smoke scenarios: fixture data for one symbol plus the decision we expect.

Each scenario lives in tests/fixtures/scenarios/<name>/scenario.json:
    {"name", "symbol", "description",
     "files": {"bars", "snapshot", "news", "submissions", "companyfacts",
               "earnings_calendar", "av_earnings_calendar", "halts"},
               # relative paths; earnings_calendar = an FMP reply list,
               # av_earnings_calendar = an Alpha Vantage CSV, halts = the Nasdaq feed XML,
               # asset = the Alpaca asset (listing exchange, #168)
     "expected": {"action": "BUY" | "PASS" | null, "fatal_bear": true | false | null, "why"}}
File paths are relative to the scenario folder and must stay inside tests/fixtures/.
`ScenarioData` serves them through the same methods the live data classes have (bars,
snapshots, news, submissions, company_facts, earnings_calendar, av_earnings_calendar,
halt_feed, listing); it makes no network calls.

The expected outcome is a label for humans reading the smoke output. It never feeds an
agent, a prompt, sizing or the risk check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from pipeline.market import NY
from tools.bars import parse_bars
from tools.scan import validate_symbols

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
SCENARIOS_DIR = FIXTURES / "scenarios"
FILE_KEYS = ("bars", "snapshot", "news", "submissions", "companyfacts", "earnings_calendar",
             "av_earnings_calendar", "halts", "asset")
# raw files, not JSON: the halt feed is XML, the Alpha Vantage calendar CSV
TEXT_KEYS = frozenset({"halts", "av_earnings_calendar"})
HALT_FEED_LAG = timedelta(seconds=30)  # the scenario's feed fetch, just before its clock
CALENDAR_FETCH_TIME = time(8, 30)  # the scenario day's once-a-day calendar fetches, pre-open
DEFAULT = "tsla"


@dataclass(frozen=True)
class Expected:
    """What a scenario should produce. None means no expectation."""

    action: str | None
    fatal_bear: bool | None
    why: str


@dataclass(frozen=True)
class Scenario:
    name: str
    symbol: str
    description: str
    files: dict[str, Path]
    expected: Expected


def scenario_names() -> list[str]:
    """Names of the saved scenarios (folders with a scenario.json), sorted."""
    if not SCENARIOS_DIR.is_dir():
        return []
    return sorted(p.parent.name for p in SCENARIOS_DIR.glob("*/scenario.json"))


def load_scenario(name: str) -> Scenario:
    """Load and validate one scenario. Raises ValueError for an unknown or malformed one."""
    if name not in scenario_names():
        raise ValueError(f"unknown scenario {name!r} (have: {', '.join(scenario_names())})")
    folder = SCENARIOS_DIR / name
    try:
        meta = json.loads((folder / "scenario.json").read_text(encoding="utf-8"))
        symbol = str(meta["symbol"]).strip().upper()
        validate_symbols([symbol])
        files: dict[str, Path] = {}
        for key in FILE_KEYS:
            path = (folder / str(meta["files"][key])).resolve()
            if not path.is_relative_to(FIXTURES.resolve()) or not path.is_file():
                raise ValueError(f"{key} file must be an existing file under tests/fixtures")
            files[key] = path
        exp = meta.get("expected") or {}
        if not isinstance(exp, dict):
            raise ValueError("expected must be an object")
        action = exp.get("action")
        if action not in (None, "BUY", "PASS"):
            raise ValueError("expected.action must be BUY, PASS or null")
        fatal = exp.get("fatal_bear")
        if fatal is not None and not isinstance(fatal, bool):  # 1 == True, so no `in`
            raise ValueError("expected.fatal_bear must be true, false or null")
        expected = Expected(action, fatal, str(exp.get("why", "")))
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"scenario {name!r} is malformed ({type(exc).__name__})") from None
    return Scenario(name, symbol, str(meta.get("description", "")), files, expected)


class ScenarioData:
    """Market, news and filings data for one scenario, from its fixture files."""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.symbol = scenario.symbol
        load = {k: (p.read_bytes() if k in TEXT_KEYS else json.loads(p.read_text(encoding="utf-8")))
                for k, p in scenario.files.items()}
        self._bars, self._snapshots = load["bars"], load["snapshot"]
        self._news, self._subs = load["news"], load["submissions"]
        self._facts = load["companyfacts"]
        self._calendar = load["earnings_calendar"]
        self._av_calendar = load["av_earnings_calendar"].decode("utf-8")
        self._halts = load["halts"]
        self._asset = load["asset"]

    def bars(self, symbol: str, start: datetime, end: datetime) -> dict:
        if symbol.upper() != self.symbol:
            return {"bars": [], "symbol": symbol.upper(), "next_page_token": None}
        return self._bars

    def snapshots(self, symbols: list[str]) -> dict:
        return {s.upper(): self._snapshots.get(s.upper()) for s in symbols}

    def news(self, symbols: list[str], start: datetime, end: datetime) -> dict:
        wanted = {s.upper() for s in symbols}
        items = [n for n in self._news["news"] if wanted & {s.upper() for s in n["symbols"]}]
        return {"news": items, "next_page_token": None}

    def submissions(self, symbol: str) -> dict | None:
        return self._subs if symbol.upper() == self.symbol else None

    def company_facts(self, symbol: str) -> dict | None:
        return self._facts if symbol.upper() == self.symbol else None

    def earnings_calendar(self, start: date, end: date) -> dict:
        """The saved FMP reply, as LiveData returns it: fetched at 08:30 ET on the scenario day,
        holding the fixture rows dated inside [start, end] (as FMP would)."""
        day = self.as_of().date()
        fetched = datetime.combine(day, CALENDAR_FETCH_TIME, tzinfo=NY)
        # Like FMP, only rows inside the requested range (the fixture may hold more)
        lo, hi = start.isoformat(), end.isoformat()
        rows = [r for r in self._calendar
                if isinstance(r, dict) and lo <= str(r.get("date", "")) <= hi]
        return {"fetched_at": fetched.isoformat(), "from": lo, "to": hi, "reply": rows}

    def listing(self, symbol: str) -> dict:
        """The saved Alpaca asset for the scenario symbol, as LiveData returns it. Any other
        symbol raises, like Alpaca's HTTP 404."""
        if symbol.strip().upper() != self.symbol:
            raise RuntimeError(f"HTTP 404 from /v2/assets/{symbol.strip().upper()[:16]}")
        return self._asset

    def av_earnings_calendar(self, start: date, end: date) -> dict:
        """The saved (synthetic) Alpha Vantage CSV, as LiveData returns it: fetched at 08:30 ET
        on the scenario day. The whole file is the reply (Alpha Vantage returns 3 months)."""
        fetched = datetime.combine(self.as_of().date(), CALENDAR_FETCH_TIME, tzinfo=NY)
        return {"fetched_at": fetched.isoformat(), "from": start.isoformat(),
                "to": end.isoformat(), "reply": self._av_calendar}

    def halt_feed(self) -> dict:
        """The saved (synthetic) Nasdaq halt feed, as LiveData returns it: fetched 30 s before
        the scenario clock. Its pubDate is 10:59 ET on the scenario day."""
        fetched = self.as_of() - HALT_FEED_LAG
        return {"fetched_at": fetched.isoformat(), "body": self._halts}

    def as_of(self) -> datetime:
        """One minute after the last fixture bar, in New York time."""
        last = max(b.ts for b in parse_bars(self._bars))
        return (last + timedelta(minutes=1)).astimezone(NY)


def compare(expected: Expected, action: str, fatal_bear: bool) -> tuple[str, bool | None]:
    """A one-line expected-vs-actual verdict, and whether the action matched (None when the
    scenario has no expectation). The fatal-Bear part is reported, not required."""
    actual = f"actual {action} (fatal Bear: {'yes' if fatal_bear else 'no'})"
    if expected.action is None:
        return f"expected: none (baseline) | {actual}", None
    want = f"expected {expected.action}"
    if expected.fatal_bear is not None:
        want += f" (ideally fatal Bear: {'yes' if expected.fatal_bear else 'no'})"
    match = action == expected.action
    verdict = "MATCH" if match else "MISMATCH"
    if match and expected.fatal_bear is not None and fatal_bear != expected.fatal_bear:
        verdict += " (decision matches; fatal Bear differs)"
    return f"{want} | {actual} -> {verdict}", match
