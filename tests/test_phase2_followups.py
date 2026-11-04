"""Review follow-ups: SDK blocks, other-day bars, AlpacaMarketData, AST guard, new checks."""
import ast
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from anthropic.types.beta import BetaMessage

import agents.technical as tech
import agents.tool_registry as reg
import pipeline.data as pdata
from agents.tool_registry import ToolSession
from tests.fakes import NOW, FakeClient, FakeMarketData, report_json, response, text
from tools.config import load_limits
from tools.exit_plan import exit_plan as real_exit_plan
from tools.scan import ScanCriteria

ROOT = Path(__file__).resolve().parent.parent
NY = ZoneInfo("America/New_York")


def make(now=NOW, data=None, symbol="AAPL") -> ToolSession:
    return ToolSession(
        symbol=symbol, now=now, data=data or FakeMarketData(),
        limits=load_limits(), criteria=ScanCriteria(),
    )


@pytest.fixture
def spy(monkeypatch):
    calls = []

    def _spy(*a, **k):
        calls.append((a, k))
        return real_exit_plan(*a, **k)

    monkeypatch.setattr(reg, "exit_plan", _spy)
    return calls


# ---- 1. thinking / fallback blocks preserved ----

def test_thinking_fallback_and_tool_use_blocks_round_trip_unmodified():
    msg = BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [
            {"type": "thinking", "thinking": "plan the call", "signature": "SIG-abc123"},
            {
                "type": "fallback",
                "from": {"model": "claude-opus-5-5"},
                "to": {"model": "claude-sonnet-5-5"},
                "trigger": {"type": "refusal", "category": None},
            },
            {"type": "tool_use", "id": "toolu_1", "name": "get_bars", "input": {"symbol": "AAPL"}},
        ],
    })
    original = [b.model_dump() for b in msg.content]
    types = [b.type for b in msg.content]
    assert types == ["thinking", "fallback", "tool_use"]
    client = FakeClient([msg, response([text(report_json())], "end_turn")])
    report = tech.run_technical_analyst("AAPL", NOW, FakeMarketData(), client=client)
    assert report.stance == "bullish"
    assistant = client.calls[1]["messages"][1]
    assert assistant["role"] == "assistant"
    assert [b.type for b in assistant["content"]] == types
    assert [b.model_dump() for b in assistant["content"]] == original
    assert assistant["content"][0].signature == "SIG-abc123"
    (res,) = client.calls[1]["messages"][2]["content"]
    assert res["tool_use_id"] == "toolu_1" and res["is_error"] is False


# ---- 2/3. other-day bars, get_bars refusal ----

def test_bars_from_another_day_yield_no_bars_and_orb_refused(spy):
    s = make(now=datetime(2026, 11, 4, 10, 30, tzinfo=NY))
    summary, err = s.execute("get_bars", {"symbol": "AAPL"})
    assert err is False
    assert summary["bars"] == 0 and summary["opening_range"] is None
    _, err = s.execute("plan_exit", {"setup": "orb", "entry": "102.50"})
    assert err is True  # no session range, so the entry cannot be validated
    assert spy == []
    _, err = s.execute(
        "plan_exit",
        {"setup": "orb", "entry": "102.50", "opening_range": {"low": "1", "complete": True}},
    )
    assert err is True and spy == []


def test_plan_exit_after_refused_get_bars_still_needs_get_bars():
    s = make()
    _, err = s.execute("get_bars", {"symbol": "MSFT"})
    assert err is True
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "102.50"})
    assert (res, err) == ({"error": "call get_bars first"}, True)


# ---- 4. AlpacaMarketData passthrough ----

def test_alpaca_market_data_passes_arguments_through(monkeypatch):
    seen = {}

    def fb(symbol, start, end):
        seen["bars"] = (symbol, start, end)
        return {"bars": ["B"]}

    def fs(symbols):
        seen["snaps"] = symbols
        return {"X": None}

    monkeypatch.setattr(pdata, "fetch_bars", fb)
    monkeypatch.setattr(pdata, "fetch_snapshots", fs)
    md = pdata.AlpacaMarketData()
    start = datetime(2026, 11, 3, 4, 0, tzinfo=NY)
    assert md.bars("AAPL", start, NOW) == {"bars": ["B"]}
    assert md.snapshots(["X", "Y"]) == {"X": None}
    assert seen == {"bars": ("AAPL", start, NOW), "snaps": ["X", "Y"]}


def test_session_window():
    start, end = pdata.session_window(NOW)
    assert start.isoformat() == "2026-11-03T04:00:00-05:00" and end == NOW
    with pytest.raises(ValueError):
        pdata.session_window(datetime(2026, 11, 3, 10, 30))


# ---- 5. AST guard for scan ----

def scan_violations(source: str) -> list[str]:
    """Return ways a module reaches tools.scan.scan other than pipeline.market.run_scan.

    Flags: `from tools.scan import scan [as x]` / `import *`; `import tools.scan as x` then
    x.scan(...); `from tools import scan [as m]` then m.scan(...); `import tools[.scan]
    [as t]` then t.scan.scan(...).
    """
    tree = ast.parse(source)
    mod_aliases: set[str] = set()  # names bound to the tools.scan module
    pkg_aliases: set[str] = set()  # names bound to the tools package
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for a in node.names:
                if node.module == "tools.scan" and a.name in ("scan", "*"):
                    out.append(f"from tools.scan import {a.name}")
                if node.module == "tools" and a.name == "scan":
                    mod_aliases.add(a.asname or "scan")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "tools.scan" and a.asname:
                    mod_aliases.add(a.asname)
                elif a.name == "tools.scan":
                    pkg_aliases.add("tools")
                elif a.name == "tools":
                    pkg_aliases.add(a.asname or "tools")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "scan":
            v = node.value
            if isinstance(v, ast.Name) and v.id in mod_aliases:
                out.append(f"{v.id}.scan")
            if (
                isinstance(v, ast.Attribute) and v.attr == "scan"
                and isinstance(v.value, ast.Name) and v.value.id in pkg_aliases
            ):
                out.append(f"{v.value.id}.scan.scan")
    return out


@pytest.mark.parametrize("src", [
    "from tools.scan import scan\n",
    "from tools.scan import scan as s\nx = s({})",
    "from tools.scan import ScanCriteria, scan\n",
    "from tools.scan import *\n",
    "import tools.scan as x\nx.scan({})",
    "import tools.scan\ntools.scan.scan({})",
    "import tools\ntools.scan.scan({})",
    "import tools as t\nt.scan.scan({})",
    "from tools import scan as m\nm.scan({})",
    "from tools import scan\nscan.scan({})",
])
def test_ast_checker_catches_evasions(src):
    assert scan_violations(src) != []


@pytest.mark.parametrize("src", [
    "from tools.scan import ScanCriteria, fetch_snapshots, validate_symbols\n",
    "import tools.scan as x\nx.ScanCriteria()",
    "from pipeline.market import run_scan\nrun_scan({})",
    "def scan(): pass\nscan()",
])
def test_ast_checker_allows_legit_code(src):
    assert scan_violations(src) == []


def test_only_pipeline_market_reaches_scan():
    offenders = {}
    checked = 0
    for folder in ("agents", "pipeline"):
        for path in (ROOT / folder).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel == "pipeline/market.py":
                continue
            checked += 1
            v = scan_violations(path.read_text(encoding="utf-8"))
            if v:
                offenders[rel] = v
    assert checked >= 5
    assert offenders == {}
    # and market.py itself does use the sanctioned path
    assert scan_violations((ROOT / "pipeline" / "market.py").read_text(encoding="utf-8")) != []


# ---- 6. entry within session range, symbol validation ----

SETUPS = [
    ("orb", {}),
    ("level", {"level": "100.00"}),
]


@pytest.mark.parametrize(("setup", "extra"), SETUPS)
@pytest.mark.parametrize("entry", ["99.69", "103.21", "0.01", "500.00"])
def test_entry_outside_session_range_refused(spy, setup, extra, entry):
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    res, err = s.execute("plan_exit", {"setup": setup, "entry": entry, **extra})
    assert (res, err) == ({"error": "entry must be within today's session range"}, True)
    assert spy == []


@pytest.mark.parametrize(("setup", "extra"), SETUPS)
@pytest.mark.parametrize("entry", ["99.70", "103.20"])
def test_entry_at_range_edges_reaches_exit_plan(spy, setup, extra, entry):
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    res, err = s.execute("plan_exit", {"setup": setup, "entry": entry, **extra})
    assert err is False  # not the range error; exit_plan decided
    assert len(spy) == 1
    if entry == "103.20":
        assert res["ok"] is True


@pytest.mark.parametrize(("setup", "extra"), SETUPS)
def test_entry_inside_range_passes(setup, extra):
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    res, err = s.execute("plan_exit", {"setup": setup, "entry": "102.00", **extra})
    assert err is False and res["ok"] is True


@pytest.mark.parametrize("bad", ["", "   ", "G ME", 5, None, "TOOLONGSYMBOL", "A1", "-X"])
def test_invalid_symbol_raises(bad):
    client = FakeClient([response([text(report_json())], "end_turn")])
    with pytest.raises(ValueError, match="symbol must be a valid ticker"):
        tech.run_technical_analyst(bad, NOW, FakeMarketData(), client=client)
    assert client.calls == []


def test_padded_lowercase_symbol_accepted():
    client = FakeClient([response([text(report_json())], "end_turn")])
    report = tech.run_technical_analyst(" aapl ", NOW, FakeMarketData(), client=client)
    assert report.symbol == "AAPL" and report.stance == "bullish"


def test_tool_results_json_serializable():
    res, _ = make().execute("get_bars", {"symbol": "AAPL"})
    json.dumps(res)
