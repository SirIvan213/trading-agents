"""Offline test doubles: FakeMarketData and FakeClient (no network, no Claude API)."""
from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, time, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from anthropic import APIConnectionError

try:  # the SDK's HTTP layer is httpx2 in newer releases, httpx in older ones
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

FIX = Path(__file__).parent / "fixtures"
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 3, 10, 30, tzinfo=NY)


def load_fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def merged_bars() -> dict:
    items = load_fixture("alpaca_bars_page1.json")["bars"] + load_fixture(
        "alpaca_bars_page2.json"
    )["bars"]
    return {"bars": items}


class FakeMarketData:
    """MarketData over saved fixtures. Records every call. Optionally raises."""

    def __init__(self, raises: Exception | None = None, snapshots: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self.raises = raises
        self._snaps = snapshots

    def bars(self, symbol: str, start: datetime, end: datetime) -> dict:
        self.calls.append(("bars", symbol, start, end))
        if self.raises:
            raise self.raises
        return merged_bars()

    def snapshots(self, symbols: list[str]) -> dict:
        self.calls.append(("snapshots", list(symbols)))
        if self.raises:
            raise self.raises
        snaps = self._snaps if self._snaps is not None else load_fixture("alpaca_snapshots.json")
        return {s: snaps.get(s) for s in symbols}


def text(s: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=s)


def tool_use(id_: str, name: str, inp: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=inp)


def response(content: list, stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def report_json(**over: Any) -> str:
    d = {
        "agent": "technical", "symbol": "AAPL", "stance": "bullish", "confidence": 0.7,
        "evidence": [{"point": "above vwap", "source": "get_bars"}], "catalysts": [],
    }
    d.update(over)
    return json.dumps(d)


def api_error(msg: str = "boom") -> APIConnectionError:
    """A real anthropic.APIError subclass built with the SDK constructor."""
    return APIConnectionError(message=msg, request=httpx.Request("POST", "https://example.invalid"))


class _Messages:
    def __init__(self, owner: FakeClient) -> None:
        self._o = owner

    def create(self, **kwargs: Any) -> Any:
        rec = dict(kwargs)
        rec["messages"] = copy.deepcopy(kwargs["messages"])
        self._o.calls.append(rec)
        if not self._o.queue:
            raise AssertionError("FakeClient queue exhausted (unexpected extra API call)")
        item = self._o.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    """Exposes .beta.messages.create(**kwargs); queued responses; records deep-copied kwargs."""

    def __init__(self, queue: list | None = None) -> None:
        self.queue: list = list(queue or [])
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=_Messages(self))


NOON = datetime(2026, 11, 3, 12, 0, tzinfo=NY)  # 17:00Z; the clock the news fixtures were built for


def merged_news() -> dict:
    items = load_fixture("alpaca_news_page1.json")["news"] + load_fixture(
        "alpaca_news_page2.json"
    )["news"]
    return {"news": items}



def background_rows(start: date, end: date) -> list[dict]:
    """Other companies reporting on every day of [start, end] (#112 review): a real
    whole-market FMP calendar is never empty and always has rows near its end, and the gate
    now treats an empty or cut-short reply as UNKNOWN. None of these symbols is a test symbol."""
    rows, day, n = [], start, 0
    while day <= end:
        n += 1
        rows.append({"symbol": f"ZBG{n:02d}", "date": day.isoformat(), "epsActual": None,
                     "epsEstimated": 0.5, "revenueActual": None, "revenueEstimated": 1000000,
                     "time": "amc", "periodEnding": "2026-09-30", "fiscalPeriod": "Q3",
                     "fiscalYear": 2026, "confirmed": True, "lastUpdated": "2026-11-02"})
        day += timedelta(days=1)
    return rows


def halt_item_xml(symbol: str, code: str = "LUDP", *, halt_date: str = "11/03/2026",
                  halt_time: str = "10:05:00.214", resumed: tuple[str, str] | None = None,
                  market: str = "NASDAQ") -> str:
    """One synthetic <item> in the Nasdaq trade-halt feed's shape (NOT a copy of real data).
    `resumed` is (MM/DD/YYYY, HH:MM:SS) for the resumption date and trade time."""
    rd, rt = resumed or ("", "")
    return (
        f"<item><title>{symbol}</title><ndaq:HaltDate>{halt_date}</ndaq:HaltDate>"
        f"<ndaq:HaltTime>{halt_time}</ndaq:HaltTime><ndaq:IssueSymbol>{symbol}</ndaq:IssueSymbol>"
        f"<ndaq:IssueName>Synthetic {symbol} Inc</ndaq:IssueName>"
        f"<ndaq:Market>{market}</ndaq:Market>"
        f"<ndaq:ReasonCode>{code}</ndaq:ReasonCode><ndaq:PauseThresholdPrice />"
        f"<ndaq:ResumptionDate>{rd}</ndaq:ResumptionDate><ndaq:ResumptionQuoteTime />"
        f"<ndaq:ResumptionTradeTime>{rt}</ndaq:ResumptionTradeTime></item>")


def halt_feed_xml(pub: datetime, items: tuple | list = (), *, num_items: int | str | None = None,
                  ) -> bytes:
    """A synthetic trade-halt RSS document published at `pub`. `items` are item XML strings
    (halt_item_xml). `num_items` overrides ndaq:numItems (default: the item count)."""
    n = len(items) if num_items is None else num_items
    pub_s = format_datetime(pub.astimezone(UTC), usegmt=True)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>'
        "<title>Synthetic Trade Halts</title>"
        f"<pubDate>{pub_s}</pubDate><ttl>1</ttl><ndaq:numItems>{n}</ndaq:numItems>"
        + "".join(items) + "</channel></rss>").encode("utf-8")


def fresh_halt_feed(now: datetime, items: tuple | list = ()) -> dict:
    """What LiveData.halt_feed() returns: fetched 5 s before `now`, feed published 60 s before."""
    return {"fetched_at": (now - timedelta(seconds=5)).astimezone(NY).isoformat(),
            "body": halt_feed_xml(now - timedelta(seconds=60), items)}


class HaltFeedFake:
    """Mixin giving a fake data object a clean, fresh synthetic halt feed (#144). It is fresh
    relative to the latest `end` of a news() read, which the event gate makes at its `now`
    just before it reads the feed (pipeline.events.gate_inputs)."""

    halt_items: tuple = ()
    _halt_now: datetime | None = None

    def _note_now(self, end: Any) -> None:
        if isinstance(end, datetime) and end.tzinfo is not None:
            self._halt_now = end

    def halt_feed(self) -> dict:
        return fresh_halt_feed(self._halt_now or NOW, self.halt_items)


class FakeFilingsData(HaltFeedFake):
    """NewsData + FilingsData over saved fixtures (ABCD, CIK 12345). Records every call."""

    def __init__(
        self,
        *,
        news_raises: Exception | None = None,
        subs_raises: Exception | None = None,
        facts_raises: Exception | None = None,
        no_cik: bool = False,
        facts_fixture: str = "sec_companyfacts_abcd.json",
        calendar_rows: list | None = None,
        calendar_raises: Exception | None = None,
    ) -> None:
        self.calendar_rows = calendar_rows or []
        self.calendar_raises = calendar_raises
        self.facts_fixture = facts_fixture
        self.calls: list[tuple] = []
        self.news_raises = news_raises
        self.subs_raises = subs_raises
        self.facts_raises = facts_raises
        self.no_cik = no_cik

    def news(self, symbols: list[str], start: datetime, end: datetime) -> dict:
        self.calls.append(("news", list(symbols), start, end))
        self._note_now(end)
        if self.news_raises:
            raise self.news_raises
        return merged_news()

    def submissions(self, symbol: str) -> dict | None:
        self.calls.append(("submissions", symbol))
        if self.subs_raises:
            raise self.subs_raises
        return None if self.no_cik else load_fixture("sec_submissions_8k.json")

    def earnings_calendar(self, start: date, end: date) -> dict:
        """The FMP calendar as LiveData returns it: fetched 08:30 ET on `start`, with the given
        rows plus other companies' background rows across the range (so no test symbol is on
        it by default, and the reply is neither empty nor cut short) (#112)."""
        self.calls.append(("earnings_calendar", start, end))
        if self.calendar_raises:
            raise self.calendar_raises
        fetched = datetime.combine(start, time(8, 30), tzinfo=NY)
        return {"fetched_at": fetched.isoformat(), "from": start.isoformat(),
                "to": end.isoformat(),
                "reply": [*self.calendar_rows, *background_rows(start, end)]}

    def company_facts(self, symbol: str) -> dict | None:
        self.calls.append(("company_facts", symbol))
        if self.facts_raises:
            raise self.facts_raises
        return None if self.no_cik else load_fixture(self.facts_fixture)


class FakeAllData(FakeMarketData, FakeFilingsData):
    """Everything the three analysts read, offline."""

    def __init__(self, **kwargs: Any) -> None:
        FakeMarketData.__init__(self)
        calls = self.calls
        FakeFilingsData.__init__(self, **kwargs)
        self.calls = calls
