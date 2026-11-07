"""Scenario fixtures (clean-buy, red-flag, tsla) and pipeline/scenarios.py. Offline.

Part 1 checks the saved BRKO / VNTR data with the REAL deterministic tools. Part 2 covers
load_scenario, ScenarioData and compare. No network, no Claude API.
"""
import importlib.util
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import pipeline.scenarios as sc
from agents.tool_registry import ToolSession, price_levels
from pipeline.orders import build_order
from pipeline.scenarios import Expected, ScenarioData, compare, load_scenario, scenario_names
from tools.bars import parse_bars, regular_session, summarize
from tools.config import CONFIG_DIR, load_limits
from tools.earnings import earnings_facts
from tools.exit_plan import exit_plan
from tools.news import digest, parse_8k_filings
from tools.risk_check import DayState, check_order
from tools.scan import ScanCriteria
from tools.scan import scan as run_scan

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCEN = FIXTURES / "scenarios"


class Loaded:
    """One scenario's data run through the real tools (no model involved)."""

    def __init__(self, name: str) -> None:
        self.data = ScenarioData(load_scenario(name))
        self.symbol = self.data.symbol
        self.now = self.data.as_of()
        bars = regular_session(parse_bars(self.data.bars(self.symbol, self.now, self.now)),
                               day=self.now.date())
        self.bars = bars
        self.summary = summarize(bars)
        self.levels = price_levels(self.summary)
        self.atr = Decimal(self.summary["atr"])

    def level_plan(self, multiple: str = "2") -> dict:
        return exit_plan("level", Decimal(self.levels["last_close"]), LIMITS,
                         level=Decimal(self.levels["vwap"]), atr=self.atr,
                         reward_multiple=Decimal(multiple))

    def digest(self) -> dict:
        filings = parse_8k_filings(self.data.submissions(self.symbol),
                                   self.now - timedelta(hours=72))
        news = self.data.news([self.symbol], self.now - timedelta(hours=24), self.now)
        return digest(self.symbol, news, filings, self.now)


@pytest.fixture(scope="module")
def clean() -> Loaded:
    return Loaded("clean-buy")


@pytest.fixture(scope="module")
def flag() -> Loaded:
    return Loaded("red-flag")


# ---------------- clean-buy (BRKO) ----------------

def test_clean_buy_symbol_and_clock(clean):
    assert clean.symbol == "BRKO"
    assert clean.now.isoformat() == "2026-11-03T11:00:00-05:00"


def test_clean_buy_opening_range_is_complete(clean):
    orng = clean.summary["opening_range"]
    assert orng["complete"] is True and orng["missing_minutes"] == 0 and orng["bars"] == 15


def test_clean_buy_close_is_above_vwap_but_not_extended(clean):
    last, vwap = Decimal(clean.summary["last_close"]), Decimal(clean.summary["vwap"])
    assert vwap < last <= vwap + 2 * clean.atr


def test_clean_buy_level_plan_is_tight_and_has_room_for_2r(clean):
    plan = clean.level_plan("2")
    assert plan["ok"] is True
    assert plan["entry"] == clean.levels["last_close"] and plan["reward_to_risk"] == "2.00"
    risk = Decimal(plan["risk_per_share"])
    assert 1 * clean.atr <= risk <= 2 * clean.atr  # tight and structural, not a noise stop
    assert Decimal(plan["stop"]) < Decimal(plan["entry"]) < Decimal(plan["target"])
    assert Decimal(plan["target"]) < Decimal(clean.levels["session_high"])  # room for 2R


def test_clean_buy_news_is_positive_with_no_red_flags(clean):
    dg = clean.digest()
    assert dg["red_flags"] == []
    assert {"analyst", "guidance", "material_agreement"} <= set(dg["catalysts"])


def test_clean_buy_fundamentals_ok_and_scan_passes(clean):
    facts = earnings_facts(clean.data.company_facts(clean.symbol), clean.now.date())
    assert facts["ok"] is True
    snaps = clean.data.snapshots([clean.symbol])
    res = run_scan(snaps, ScanCriteria(), LIMITS, today=clean.now.date())
    assert [c["symbol"] for c in res["candidates"]] == ["BRKO"] and res["rejected"] == []


def test_clean_buy_order_passes_the_risk_check(clean):
    plan = clean.level_plan("2")
    session = ToolSession(clean.symbol, clean.now, clean.data, LIMITS, ScanCriteria())
    session.execute("get_bars", {"symbol": clean.symbol})
    res, err = session.execute("plan_exit", {
        "setup": "level", "entry": clean.levels["last_close"], "level": clean.levels["vwap"],
        "reward_multiple": "2"})
    assert err is False and res["ok"] is True and res["target"] == plan["target"]
    assert res["target_vs_session_high"] == "below"
    sized = build_order(session.exit_plans[0], LIMITS, clean.now)
    assert sized["ok"] is True
    verdict = check_order(sized["order"], LIMITS, DayState(), clean.now)
    assert verdict == {"verdict": "PASS", "reasons": []}


# ---------------- red-flag (VNTR) ----------------

def test_red_flag_chart_gives_the_same_ok_level_plan(flag):
    assert flag.symbol == "VNTR"
    assert flag.summary["opening_range"]["complete"] is True
    plan = flag.level_plan("2")
    assert plan["ok"] is True and plan["reward_to_risk"] == "2.00"
    assert Decimal(plan["target"]) < Decimal(flag.levels["session_high"])


def test_red_flag_digest_has_halt_and_earnings(flag):
    dg = flag.digest()
    assert "halt" in dg["red_flags"]
    assert "earnings" in dg["catalysts"]


def test_red_flag_earnings_is_scheduled_inside_the_trade_window(flag):
    dg = flag.digest()
    (head,) = [h for h in dg["headlines"] if "earnings" in h["flags"]]
    m = re.search(r"(\d{1,2}):(\d{2}) p\.m\. ET today", head["headline"])
    assert m, head["headline"]
    release = flag.now.replace(hour=int(m[1]) % 12 + 12, minute=int(m[2]), second=0)
    flat_by = LIMITS.flat_by
    assert flag.now < release
    assert release.time() < flat_by
    assert datetime.fromisoformat(head["time"]).date() == flag.now.date()  # announced today


def test_red_flag_bars_have_the_halt_gap(flag, clean):
    def minutes(loaded):
        return {b.ts.astimezone(sc.NY).strftime("%H:%M") for b in loaded.bars}
    halted = {f"10:0{m}" for m in range(5, 10)}
    assert not halted & minutes(flag)
    assert {"10:04", "10:10"} <= minutes(flag)
    assert halted <= minutes(clean)  # the gap is what differs


# ---------------- determinism of build.py ----------------

def _lf(path):
    """File bytes with CRLF normalised: git may check the JSON out with CRLF on Windows."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def test_build_reproduces_the_committed_files_byte_for_byte(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scenario_build", SCEN / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    monkeypatch.setattr(build, "HERE", tmp_path)  # FIXTURES stays: it reads the TSLA facts
    build.build()
    names = ("bars.json", "snapshot.json", "news.json", "submissions.json", "companyfacts.json")
    for folder in ("clean-buy", "red-flag"):
        for name in names:
            assert _lf(tmp_path / folder / name) == _lf(SCEN / folder / name), \
                f"{folder}/{name} differs from build.py output"


# ---------------- scenario_names / load_scenario ----------------

def test_scenario_names_are_sorted():
    assert scenario_names() == ["clean-buy", "red-flag", "tsla"]


@pytest.mark.parametrize("name,symbol,action,fatal", [
    ("clean-buy", "BRKO", "BUY", None),
    ("red-flag", "VNTR", "PASS", True),
    ("tsla", "TSLA", None, None),
])
def test_load_scenario_expected_values(name, symbol, action, fatal):
    s = load_scenario(name)
    assert (s.name, s.symbol) == (name, symbol)
    assert (s.expected.action, s.expected.fatal_bear) == (action, fatal)
    assert s.expected.why and s.description
    assert set(s.files) == set(sc.FILE_KEYS)
    assert all(p.is_file() and p.is_relative_to(FIXTURES) for p in s.files.values())


@pytest.fixture
def tmp_scen(tmp_path, monkeypatch):
    """Point the module at a temp fixtures tree. make(name, **over) writes a scenario."""
    fx = tmp_path / "fx"
    (fx / "scenarios").mkdir(parents=True)
    monkeypatch.setattr(sc, "FIXTURES", fx)
    monkeypatch.setattr(sc, "SCENARIOS_DIR", fx / "scenarios")

    def make(name="t", *, symbol="ABCD", expected=None, files=None, raw=None):
        folder = fx / "scenarios" / name
        folder.mkdir(parents=True, exist_ok=True)
        for key in sc.FILE_KEYS:
            (folder / f"{key}.json").write_text("{}", encoding="utf-8")
        meta = {"name": name, "symbol": symbol,
                "files": files or {k: f"{k}.json" for k in sc.FILE_KEYS},
                "expected": {"action": "BUY", "fatal_bear": None, "why": "w"}
                if expected is None else expected}
        text = raw if raw is not None else json.dumps(meta)
        (folder / "scenario.json").write_text(text, encoding="utf-8")
        return folder

    make.root = tmp_path
    return make


def test_load_scenario_accepts_a_well_formed_temp_scenario(tmp_scen):
    tmp_scen("ok", symbol=" abcd ")
    s = load_scenario("ok")
    assert s.symbol == "ABCD" and s.expected == Expected("BUY", None, "w")


def test_unknown_scenario_raises(tmp_scen):
    with pytest.raises(ValueError, match="unknown scenario"):
        load_scenario("nope")
    with pytest.raises(ValueError):
        load_scenario("../scenarios/ok")


def test_unknown_scenario_raises_against_the_real_tree():
    with pytest.raises(ValueError, match="clean-buy, red-flag, tsla"):
        load_scenario("nope")


@pytest.mark.parametrize("raw", ["{not json", "[]", '{"symbol": "ABCD"}', "null"])
def test_malformed_scenario_json_raises(tmp_scen, raw):
    tmp_scen("bad", raw=raw)
    with pytest.raises(ValueError, match="malformed"):
        load_scenario("bad")


def test_file_path_escaping_fixtures_raises(tmp_scen):
    outside = tmp_scen.root / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    for evil in ("../../../outside.json", str(outside),
                 "../../../../pyproject.toml"):
        files = {k: f"{k}.json" for k in sc.FILE_KEYS} | {"news": evil}
        tmp_scen("esc", files=files)
        with pytest.raises(ValueError, match="under tests/fixtures"):
            load_scenario("esc")


def test_real_repo_file_outside_fixtures_is_refused(tmp_scen, monkeypatch):
    """The real FIXTURES root with a path to the repo's pyproject.toml (it exists)."""
    monkeypatch.setattr(sc, "FIXTURES", FIXTURES)
    pyproject = FIXTURES.parent.parent / "pyproject.toml"
    assert pyproject.is_file()
    files = {k: f"{k}.json" for k in sc.FILE_KEYS} | {"bars": str(pyproject)}
    tmp_scen("esc", files=files)
    with pytest.raises(ValueError, match="under tests/fixtures"):
        load_scenario("esc")


def test_missing_file_raises(tmp_scen):
    folder = tmp_scen("miss")
    (folder / "bars.json").unlink()
    with pytest.raises(ValueError, match="existing file"):
        load_scenario("miss")


def test_missing_file_key_raises(tmp_scen):
    files = {k: f"{k}.json" for k in sc.FILE_KEYS if k != "news"}
    tmp_scen("nokey", files=files)
    with pytest.raises(ValueError, match="malformed"):
        load_scenario("nokey")


@pytest.mark.parametrize("expected", [
    {"action": "SELL", "fatal_bear": None},
    {"action": "buy", "fatal_bear": None},
    {"action": 1, "fatal_bear": None},
    {"action": "BUY", "fatal_bear": "yes"},
    {"action": "PASS", "fatal_bear": []},
])
def test_bad_expected_values_raise(tmp_scen, expected):
    tmp_scen("badexp", expected=expected)
    with pytest.raises(ValueError):
        load_scenario("badexp")


@pytest.mark.parametrize("fatal", [1, 0])
def test_integer_fatal_bear_is_refused(tmp_scen, fatal):
    tmp_scen("badexp", expected={"action": "BUY", "fatal_bear": fatal})
    with pytest.raises(ValueError):
        load_scenario("badexp")


@pytest.mark.parametrize("symbol", ["", "BR$K", "12345", "TOOLONGSYMBOL", "A B", "../x"])
def test_bad_symbol_raises(tmp_scen, symbol):
    tmp_scen("badsym", symbol=symbol)
    with pytest.raises(ValueError):
        load_scenario("badsym")


def test_null_expected_means_no_expectation(tmp_scen):
    tmp_scen("noexp", raw=json.dumps({
        "symbol": "ABCD", "files": {k: f"{k}.json" for k in sc.FILE_KEYS}, "expected": None}))
    assert load_scenario("noexp").expected == Expected(None, None, "")


# ---------------- ScenarioData ----------------

def test_scenario_data_serves_its_own_symbol_only():
    d = ScenarioData(load_scenario("clean-buy"))
    t = datetime(2026, 11, 3, 11, tzinfo=sc.NY)
    assert d.bars("brko", t, t)["bars"]
    other = d.bars("tsla", t, t)
    assert other["bars"] == [] and other["next_page_token"] is None and other["symbol"] == "TSLA"
    assert d.snapshots(["BRKO", "zzzz"])["ZZZZ"] is None
    assert d.snapshots(["brko"])["BRKO"]["latestTrade"]
    assert d.news(["TSLA"], t, t) == {"news": [], "next_page_token": None}
    assert len(d.news(["brko"], t, t)["news"]) == 3
    assert d.submissions("TSLA") is None and d.submissions("brko")["tickers"] == ["BRKO"]
    assert d.company_facts("TSLA") is None and d.company_facts("brko")["entityName"]


@pytest.mark.parametrize("name", ["clean-buy", "red-flag", "tsla"])
def test_as_of_is_11_00_eastern(name):
    now = ScenarioData(load_scenario(name)).as_of()
    assert now.isoformat() == "2026-11-03T11:00:00-05:00"
    assert now.utcoffset() == timedelta(hours=-5)


def test_tsla_scenario_data_equals_the_old_fixture_data():
    from pipeline import smoke_debate as S
    old = {
        "bars": json.loads((FIXTURES / "alpaca_bars_tsla.json").read_text(encoding="utf-8")),
        "snap": json.loads((FIXTURES / "alpaca_snapshot_tsla.json").read_text(encoding="utf-8")),
        "news": json.loads((FIXTURES / "alpaca_news_tsla.json").read_text(encoding="utf-8")),
        "subs": json.loads((FIXTURES / "sec_submissions_tsla.json").read_text(encoding="utf-8")),
        "facts": json.loads((FIXTURES / "sec_companyfacts_tsla.json").read_text(encoding="utf-8")),
    }
    for d in (ScenarioData(load_scenario("tsla")), S.FixtureData()):
        t = datetime(2026, 11, 3, 11, tzinfo=sc.NY)
        assert d.symbol == "TSLA"
        assert d.bars("TSLA", t, t) == old["bars"]
        assert d.snapshots(["TSLA"]) == {"TSLA": old["snap"]["TSLA"]}
        assert d.news(["TSLA"], t, t)["news"] == [
            n for n in old["news"]["news"] if "TSLA" in n["symbols"]]
        assert d.submissions("TSLA") == old["subs"]
        assert d.company_facts("TSLA") == old["facts"]
    assert isinstance(S.FixtureData(), ScenarioData)


# ---------------- compare ----------------

BUY = Expected("BUY", None, "w")
PASS_FATAL = Expected("PASS", True, "w")
BUY_NOFATAL = Expected("BUY", False, "w")
BASELINE = Expected(None, None, "w")


def test_compare_match_without_fatal_expectation():
    line, ok = compare(BUY, "BUY", False)
    assert ok is True and line.endswith("-> MATCH") and "ideally" not in line
    assert "expected BUY" in line and "actual BUY (fatal Bear: no)" in line


def test_compare_mismatch():
    line, ok = compare(BUY, "PASS", True)
    assert ok is False and line.endswith("-> MISMATCH")
    assert "actual PASS (fatal Bear: yes)" in line


def test_compare_match_with_expected_fatal_bear():
    line, ok = compare(PASS_FATAL, "PASS", True)
    assert ok is True and line.endswith("-> MATCH")
    assert "(ideally fatal Bear: yes)" in line and "differs" not in line


def test_compare_fatal_bear_difference_on_a_match_is_reported_not_required():
    line, ok = compare(PASS_FATAL, "PASS", False)
    assert ok is True
    assert line.endswith("-> MATCH (decision matches; fatal Bear differs)")
    line, ok = compare(BUY_NOFATAL, "BUY", True)
    assert ok is True and "fatal Bear differs" in line


@pytest.mark.parametrize("fatal", [True, False])
def test_compare_mismatch_with_expected_fatal_bear(fatal):
    line, ok = compare(PASS_FATAL, "BUY", fatal)
    assert ok is False and line.endswith("-> MISMATCH") and "differs" not in line


@pytest.mark.parametrize("action", ["BUY", "PASS"])
@pytest.mark.parametrize("fatal", [True, False])
def test_compare_baseline_has_no_verdict(action, fatal):
    line, ok = compare(BASELINE, action, fatal)
    assert ok is None
    assert line.startswith("expected: none (baseline)") and "MATCH" not in line
    assert f"actual {action}" in line


def test_compare_baseline_ignores_a_fatal_bear_expectation_without_an_action():
    assert compare(Expected(None, True, "w"), "BUY", False)[1] is None
