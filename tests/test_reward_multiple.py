"""plan_exit reward_multiple: a fixed menu the model picks from; exit_plan computes the target.
Offline: fixture bars through FakeMarketData, no network, no Claude API."""
import json
from dataclasses import replace
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import pytest

import agents.tool_registry as reg
from agents.tool_registry import REWARD_MULTIPLES, TOOLS, ToolSession
from pipeline.ledger import PlanLedger
from tests.fakes import NOW, FakeMarketData
from tools.config import CONFIG_DIR, load_limits
from tools.exit_plan import exit_plan as real_exit_plan
from tools.scan import ScanCriteria

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
# The AAPL fixture's stops are wider than max_stop_atr_5m x ATR, so its plans' suggestion (the
# ceiling on reward_multiple) is the minimum. These label thresholds, not risk limits, are
# widened so every menu value is a legal choice; max_reward_multiple is unchanged.
WIDE = replace(LIMITS, max_stop_atr_5m=Decimal("10"), good_setup_multiple=Decimal("3"))
MENU = ["1", "1.5", "2", "2.5", "3"]
ORB = {"setup": "orb", "entry": "103.10"}
LEVEL = {"setup": "level", "entry": "103.10", "level": "99.70"}
SETUPS = [pytest.param(ORB, id="orb"), pytest.param(LEVEL, id="level")]
PROMPT = (Path(__file__).resolve().parent.parent / "agents" / "prompts" / "technical.md"
          ).read_text(encoding="utf-8")


def make(limits=WIDE, ledger=None) -> ToolSession:
    s = ToolSession(symbol="AAPL", now=NOW, data=FakeMarketData(), limits=limits,
                    criteria=ScanCriteria(), ledger=ledger)
    s.execute("get_bars", {"symbol": "AAPL"})
    # The fixture is early in the session, so its 5-minute ATR is provisional, which caps the
    # suggestion at the minimum (tests/test_plan_labels.py covers that rule). These tests are
    # about the menu, so the summary is marked settled; no price, stop or limit changes.
    s.summary["atr_5m_provisional"] = False
    return s


@pytest.fixture
def spy(monkeypatch):
    calls = []

    def _spy(*a, **k):
        calls.append((a, k))
        return real_exit_plan(*a, **k)

    monkeypatch.setattr(reg, "exit_plan", _spy)
    return calls


def expected_plan(inp: dict, multiple: str | None, limits=LIMITS) -> dict:
    """What exit_plan itself says for the same structure (ATR from the fixture bars)."""
    s = make(limits)
    atr = Decimal(s.summary["atr"])
    kw = {} if multiple is None else {"reward_multiple": Decimal(multiple)}
    if inp["setup"] == "orb":
        return real_exit_plan("orb", Decimal(inp["entry"]), limits,
                              opening_range=s.summary["opening_range"], atr=atr, **kw)
    return real_exit_plan("level", Decimal(inp["entry"]), limits, level=Decimal(inp["level"]),
                          atr=atr, **kw)


# ---- every menu value works, for both setups ----

def test_menu_is_the_documented_one():
    assert REWARD_MULTIPLES == ("1", "1.5", "2", "2.5", "3")


@pytest.mark.parametrize("inp", SETUPS)
@pytest.mark.parametrize("m", MENU)
def test_menu_value_sets_the_reward_to_risk(inp, m):
    res, err = make().execute("plan_exit", {**inp, "reward_multiple": m})
    assert err is False and res["ok"] is True
    entry, risk = Decimal(res["entry"]), Decimal(res["risk_per_share"])
    # the tool computed the target: entry + m * risk, rounded up to the cent
    assert Decimal(res["target"]) == (entry + Decimal(m) * risk).quantize(
        Decimal("0.01"), rounding="ROUND_CEILING")
    assert Decimal(res["reward_to_risk"]) >= Decimal(m).quantize(Decimal("0.01"), ROUND_FLOOR)
    assert Decimal(res["reward_to_risk"]) == Decimal(m)  # exact on this fixture
    want = expected_plan(inp, m)
    assert {k: res[k] for k in want if k != "notes"} == {
        k: v for k, v in want.items() if k != "notes"}


@pytest.mark.parametrize("inp", SETUPS)
def test_higher_multiple_gives_a_further_target_and_the_same_stop(inp):
    plans = [make().execute("plan_exit", {**inp, "reward_multiple": m})[0] for m in MENU]
    targets = [Decimal(p["target"]) for p in plans]
    assert targets == sorted(set(targets))
    assert len({p["stop"] for p in plans}) == 1 and len({p["risk_per_share"] for p in plans}) == 1


# ---- refused before exit_plan ----

@pytest.mark.parametrize("bad", ["4", "2.0", "0.5", 2, 2.0, True, "", "1e1", "-1", "0", " 2",
                                 "2 ", "02", "1.50", "5", "NaN", "Infinity", [], ["2"], {}])
@pytest.mark.parametrize("inp", SETUPS)
def test_value_outside_the_menu_is_refused_before_exit_plan(spy, inp, bad):
    s = make()
    res, err = s.execute("plan_exit", {**inp, "reward_multiple": bad})
    assert err is True
    assert res == {"error": "reward_multiple must be one of 1, 1.5, 2, 2.5, 3"}
    assert spy == [] and s.exit_plans == []


def test_refused_multiple_records_nothing_in_the_ledger(spy):
    led = PlanLedger("AAPL")
    make(ledger=led).execute("plan_exit", {**ORB, "reward_multiple": "4"})
    led.seal(accept=True)
    assert led.plan_ids() == []


# ---- the configured cap still applies ----

@pytest.mark.parametrize("inp", SETUPS)
def test_multiple_above_max_reward_multiple_is_refused_by_exit_plan(spy, inp):
    # replace() skips validation: good_setup_multiple 3 above the cap 2 leaves the cap itself
    # (exit_plan's own check) as the only thing refusing 2.5
    capped = replace(WIDE, max_reward_multiple=Decimal("2"))
    s = make(capped)
    res, err = s.execute("plan_exit", {**inp, "reward_multiple": "2.5"})
    assert err is False  # a plan refusal, not a tool error
    assert res["ok"] is False and "exceeds max_reward_multiple 2" in res["reason"]
    assert spy[-1][1]["reward_multiple"] == Decimal("2.5")
    assert s.exit_plans == []


@pytest.mark.parametrize("inp", SETUPS)
def test_multiple_exactly_at_the_cap_is_allowed(inp):
    capped = replace(WIDE, max_reward_multiple=Decimal("2"))
    res, err = make(capped).execute("plan_exit", {**inp, "reward_multiple": "2"})
    assert err is False and res["ok"] is True and res["reward_to_risk"] == "2.00"


# ---- omitted: the plan's suggested multiple ----

@pytest.mark.parametrize("inp", SETUPS)
def test_omitting_it_uses_the_suggested_multiple_passed_explicitly(spy, inp):
    res, err = make().execute("plan_exit", inp)
    assert err is False and res["ok"] is True
    assert res["labels"]["suggested_multiple"] == "3"
    assert spy[0][1]["reward_multiple"] is None  # the labelling call, at the minimum
    assert spy[-1][1]["reward_multiple"] == Decimal("3")
    assert res["reward_to_risk"] == "3.00"
    want = expected_plan(inp, "3")
    assert {k: res[k] for k in ("stop", "target", "risk_per_share")} == {
        k: want[k] for k in ("stop", "target", "risk_per_share")}


@pytest.mark.parametrize("inp", SETUPS)
def test_omitting_it_with_a_minimum_suggestion_gives_the_minimum(spy, inp):
    res, err = make(LIMITS).execute("plan_exit", inp)  # wide stops: suggestion is "1"
    assert err is False and res["ok"] is True
    assert res["labels"]["suggested_multiple"] == "1"
    assert res["reward_to_risk"] == "1.00"  # limits.min_reward_to_risk is 1.0
    assert spy[-1][1]["reward_multiple"] == Decimal("1")


def test_menu_multiple_is_forwarded_to_exit_plan_as_a_decimal(spy):
    make().execute("plan_exit", {**LEVEL, "reward_multiple": "1.5"})
    assert spy[-1][1]["reward_multiple"] == Decimal("1.5")
    assert isinstance(spy[-1][1]["reward_multiple"], Decimal)


# ---- target_vs_session_high ----

def test_target_vs_session_high_at_or_above_on_real_numbers():
    res, _ = make().execute("plan_exit", {**ORB, "reward_multiple": "1"})
    assert Decimal(res["target"]) > Decimal("103.20")  # session high
    assert res["target_vs_session_high"] == "at_or_above"


@pytest.mark.parametrize("target,verdict", [
    ("103.19", "below"), ("103.20", "at_or_above"), ("103.21", "at_or_above"),
])
def test_target_vs_session_high_boundary(monkeypatch, target, verdict):
    def stub(*a, **k):
        return {**real_exit_plan(*a, **k), "target": target}

    monkeypatch.setattr(reg, "exit_plan", stub)
    res, err = make().execute("plan_exit", {**ORB, "reward_multiple": "1"})
    assert err is False and res["target"] == target
    assert res["target_vs_session_high"] == verdict


def test_target_vs_session_high_unknown_without_a_session_high():
    s = make()
    del s.summary["price_levels"]["session_high"]
    res, err = s.execute("plan_exit", ORB)
    assert err is False and res["ok"] is True
    assert res["target_vs_session_high"] == "unknown"


def test_vs_session_high_compares_decimals_not_strings():
    assert reg._vs_session_high("99.50", {"session_high": "100.00"}) == "below"
    assert reg._vs_session_high("100.00", {"session_high": "99.50"}) == "at_or_above"
    assert reg._vs_session_high("1000.00", {"session_high": "999.99"}) == "at_or_above"
    assert reg._vs_session_high("9.99", {"session_high": "10.00"}) == "below"


def test_refused_plan_has_no_target_vs_session_high():
    res, _ = make().execute("plan_exit", {"setup": "orb", "entry": "99.70",
                                           "reward_multiple": "2"})
    assert res["ok"] is False and "target_vs_session_high" not in res


# ---- ledger and record carry the multiple's reward_to_risk ----

@pytest.mark.parametrize("m", MENU)
def test_ledger_and_plan_record_carry_the_multiple(m):
    led = PlanLedger("AAPL")
    s = make(ledger=led)
    res, err = s.execute("plan_exit", {**LEVEL, "reward_multiple": m})
    assert err is False
    rec = s.exit_plans[0]
    assert rec.reward_to_risk == res["reward_to_risk"] == f"{Decimal(m):.2f}"
    assert rec.target == res["target"] and rec.plan_id == res["plan_id"]
    led.seal(accept=True)
    assert led.get(rec.plan_id) == rec


# ---- schema and prompt ----

def test_schema_has_no_free_numeric_field():
    (t,) = [t for t in TOOLS if t["name"] == "plan_exit"]
    props = t["input_schema"]["properties"]
    assert set(props) == {"setup", "entry", "level", "reward_multiple"}
    assert props["reward_multiple"]["enum"] == MENU
    for name, spec in props.items():
        assert spec["type"] in ("string",), name  # nothing numeric
        assert "number" not in json.dumps(spec) and "integer" not in json.dumps(spec)
    assert t["input_schema"]["required"] == ["setup", "entry"]
    assert t["input_schema"]["additionalProperties"] is False and t["strict"] is True


def test_plan_exit_keys_match_the_schema():
    (t,) = [t for t in TOOLS if t["name"] == "plan_exit"]
    assert reg.PLAN_EXIT_KEYS == frozenset(t["input_schema"]["properties"])


def test_technical_prompt_teaches_the_menu_and_the_signals():
    for word in ("reward_multiple", "VWAP", "ATR", "target_vs_session_high"):
        assert word in PROMPT, word
    for value in MENU:
        assert f'"{value}"' in PROMPT, value
    assert "carries no signal" not in PROMPT
    assert "never write stop or target numbers yourself" in PROMPT
