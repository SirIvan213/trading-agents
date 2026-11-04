"""Issue #10: plan_exit only ever sees the opening range / ATR computed from bars."""
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import agents.tool_registry as reg
from agents.tool_registry import TOOLS, ToolSession
from tests.fakes import NOW, FakeMarketData
from tools.config import load_limits
from tools.exit_plan import exit_plan as real_exit_plan
from tools.scan import ScanCriteria

REFUSAL = "opening_range and atr come from get_bars, not from the model"
SENTINEL = "sk-ant-SENTINEL-1234567890"


def make(data=None, symbol="AAPL") -> ToolSession:
    return ToolSession(
        symbol=symbol, now=NOW, data=data or FakeMarketData(),
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


# ---- plan_exit refusal of model-supplied numbers ----

@pytest.mark.parametrize("extra", [
    {"opening_range": {"low": "1.00", "high": "2.00", "complete": True}},
    {"atr": "0.01"},
    {"bogus": "x"},
])
def test_plan_exit_refuses_extra_keys(spy, extra):
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})  # even with bars loaded
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "102.50", **extra})
    assert err is True
    assert res == {"error": REFUSAL}
    assert spy == []  # exit_plan never called


def test_plan_exit_refuses_extra_keys_before_get_bars(spy):
    res, err = make().execute(
        "plan_exit", {"setup": "orb", "entry": "5", "opening_range": {"low": "1"}}
    )
    assert (res, err) == ({"error": REFUSAL}, True)
    assert spy == []


def test_plan_exit_uses_fixture_opening_range(spy):
    s = make()
    summary, err = s.execute("get_bars", {"symbol": "AAPL"})
    assert err is False
    assert summary["opening_range"]["low"] == "99.7"
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "102.50"})
    assert err is False
    assert len(spy) == 1
    args, kwargs = spy[0]
    assert args[0] == "orb" and args[1] == Decimal("102.50")
    assert kwargs["opening_range"] == summary["opening_range"]
    assert kwargs["atr"] == Decimal("0.8151")
    # stop = floor(99.7 - 0.10 * 0.8151) = 99.61
    assert res["ok"] is True
    assert res["stop"] == "99.61"
    assert res["risk_per_share"] == "2.89"


def test_plan_exit_before_get_bars(spy):
    res, err = make().execute("plan_exit", {"setup": "orb", "entry": "102.50"})
    assert (res, err) == ({"error": "call get_bars first"}, True)
    assert spy == []


def test_level_inside_range_passes(spy):
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "102.50", "level": "100.00"})
    assert err is False and res["ok"] is True
    assert res["stop"] == "99.91"  # floor(100 - 0.08151)
    assert len(spy) == 1


@pytest.mark.parametrize("level", ["99.70", "103.20"])  # exactly at session low / high
def test_level_at_session_boundary_passes(level):
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    _, err = s.execute("plan_exit", {"setup": "level", "entry": "103.20", "level": level})
    assert err is False


@pytest.mark.parametrize("level", ["99.69", "103.21", "1.00", "500"])
def test_level_outside_range_refused(spy, level):
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "102.50", "level": level})
    assert (res, err) == ({"error": "level must be within today's session range"}, True)
    assert spy == []


@pytest.mark.parametrize("bad", ["abc", "", "NaN", "Infinity", 100, None, True])
def test_junk_entry_refused(spy, bad):
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": bad})
    assert err is True and "error" in res
    assert spy == []


@pytest.mark.parametrize("bad", ["abc", "NaN", "-Infinity", 100])
def test_junk_level_refused(spy, bad):
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "102.50", "level": bad})
    assert err is True and "error" in res
    assert spy == []


def test_level_setup_without_level_refused():
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    res, err = s.execute("plan_exit", {"setup": "level", "entry": "102.50"})
    assert (res, err) == ({"error": "level setup needs a level"}, True)


def test_bad_setup_refused():
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    _, err = s.execute("plan_exit", {"setup": "momentum", "entry": "102.50"})
    assert err is True


def test_entry_at_structure_is_exit_plan_refusal_not_tool_error():
    s = make()
    s.execute("get_bars", {"symbol": "AAPL"})
    res, err = s.execute("plan_exit", {"setup": "orb", "entry": "99.70"})
    assert err is False and res["ok"] is False


# ---- get_bars ----

def test_get_bars_summary_matches_fixture():
    data = FakeMarketData()
    res, err = make(data).execute("get_bars", {"symbol": "AAPL"})
    assert err is False
    assert res["bars"] == 26 and res["last_close"] == "103.1"
    assert res["session_high"] == "103.2" and res["session_low"] == "99.7"
    assert res["opening_range"]["complete"] is True
    (_, sym, start, end) = data.calls[0]
    assert sym == "AAPL"
    assert start.isoformat() == "2026-11-03T04:00:00-05:00" and end == NOW


def test_get_bars_other_symbol_refused():
    data = FakeMarketData()
    res, err = make(data).execute("get_bars", {"symbol": "MSFT"})
    assert (res, err) == ({"error": "symbol must be AAPL"}, True)
    assert data.calls == []


@pytest.mark.parametrize("sym", ["aapl", "  AAPL ", " aapl"])
def test_get_bars_case_and_padding_accepted(sym):
    _, err = make().execute("get_bars", {"symbol": sym})
    assert err is False


@pytest.mark.parametrize("sym", [None, 5, ["AAPL"]])
def test_get_bars_non_string_symbol_refused(sym):
    _, err = make().execute("get_bars", {"symbol": sym})
    assert err is True


# ---- scan ----

def test_scan_over_50_refused():
    data = FakeMarketData()
    syms = [f"A{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(51)]
    res, err = make(data).execute("scan", {"symbols": syms})
    assert (res, err) == ({"error": "at most 50 symbols"}, True)
    assert data.calls == []


def test_scan_exactly_50_allowed():
    syms = ["GAPA"] + [f"A{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(49)]
    assert len(syms) == 50
    data = FakeMarketData()
    _, err = make(data).execute("scan", {"symbols": syms})
    assert err is False
    assert len(data.calls[0][1]) == 50


def test_scan_goes_through_run_scan_with_ny_date(monkeypatch):
    seen = {}

    def fake_run_scan(snaps, criteria, limits, now, avg_volume=None):
        seen.update(snaps=snaps, now=now)
        return {"candidates": [], "rejected": [], "criteria": {}}

    monkeypatch.setattr(reg, "run_scan", fake_run_scan)
    data = FakeMarketData()
    _, err = make(data).execute("scan", {"symbols": ["gapa", "GAPA", "gapb"]})
    assert err is False
    assert data.calls == [("snapshots", ["GAPA", "GAPB"])]
    assert seen["now"] == NOW and set(seen["snaps"]) == {"GAPA", "GAPB"}


def test_scan_real_pipeline_rejects_stale_bars_premarket():
    s = make()
    s.now = datetime(2026, 11, 4, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    res, err = s.execute("scan", {"symbols": ["GAPA"]})
    assert err is False and res["candidates"] == []
    assert any("stale daily bar" in r for r in res["rejected"][0]["reasons"])


def test_scan_fresh_bar_passes():
    res, err = make().execute("scan", {"symbols": ["GAPA"]})
    assert err is False
    assert [c["symbol"] for c in res["candidates"]] == ["GAPA"]


@pytest.mark.parametrize("bad", [[], "GAPA", None, ["bad sym!"], [1]])
def test_scan_bad_symbols_refused(bad):
    _, err = make().execute("scan", {"symbols": bad})
    assert err is True


# ---- executors never raise ----

def test_unknown_tool_and_non_dict_input():
    s = make()
    assert s.execute("nope", {})[1] is True
    assert s.execute("get_bars", "AAPL")[1] is True
    assert reg.execute(s, "get_bars", None)[1] is True


@pytest.mark.parametrize("call", [
    ("get_bars", {"symbol": "AAPL"}),
    ("scan", {"symbols": ["GAPA"]}),
])
def test_data_failure_returns_error_without_secret(monkeypatch, call):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    monkeypatch.setenv("ALPACA_SECRET_KEY", SENTINEL)
    res, err = make(FakeMarketData(raises=RuntimeError("upstream failed"))).execute(*call)
    assert err is True
    assert SENTINEL not in json.dumps(res)


def test_unexpected_exception_type_is_contained():
    res, err = make(FakeMarketData(raises=KeyError(SENTINEL))).execute(
        "get_bars", {"symbol": "AAPL"}
    )
    assert err is True
    assert SENTINEL not in json.dumps(res)


def test_runtime_error_message_containing_secret_is_not_echoed():
    # Regression: execute() used to return str(exc)[:200] for RuntimeError/ValueError,
    # so a secret inside an upstream exception message reached the model.
    res, err = make(FakeMarketData(raises=RuntimeError(f"auth failed key={SENTINEL}"))).execute(
        "get_bars", {"symbol": "AAPL"}
    )
    assert err is True
    assert SENTINEL not in json.dumps(res)


# ---- schemas ----

def test_tools_strict_and_closed():
    assert {t["name"] for t in TOOLS} == {"get_bars", "plan_exit", "scan"}
    for t in TOOLS:
        assert t["strict"] is True, t["name"]
        assert t["input_schema"]["additionalProperties"] is False, t["name"]
        assert t["input_schema"]["type"] == "object"


def test_plan_exit_schema_has_no_model_numbers():
    (t,) = [t for t in TOOLS if t["name"] == "plan_exit"]
    props = t["input_schema"]["properties"]
    assert "opening_range" not in props and "atr" not in props
    assert set(props) == {"setup", "entry", "level"}
    assert t["input_schema"]["required"] == ["setup", "entry"]
