"""Offline test doubles: FakeMarketData and FakeClient (no network, no Claude API)."""
from __future__ import annotations

import copy
import json
from datetime import datetime
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
