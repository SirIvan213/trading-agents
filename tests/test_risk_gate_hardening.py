"""Risk-gate hardening: #33 exact reward cap, #34 one ticker rule, #62 hand-built limits in
run_technical, #75 / #76 / #140 config bounds, #77 percent-of-entry stop floor, #78 build_order
refuses noise stops before sizing. Each rule has a case that passes and a case that is blocked.
Offline: fixtures and fakes only."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime
from decimal import ROUND_CEILING
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

import pipeline.orders as orders
from agents.tool_registry import price_levels
from pipeline.orders import build_order
from tests.fakes import NOW, FakeClient, FakeMarketData
from tests.order_helpers import T0, fresh_quote, gate_for_order, plan
from tests.test_scenario_review import session
from tools.config import CONFIG_DIR, Limits, limits_from_dict, load_limits, validate_limits
from tools.exit_plan import exit_plan
from tools.risk_check import DayState, check_order
from tools.scan import validate_symbols
from tools.size import Plan, size_position
from tools.slippage import minimum_allowance
from tools.symbols import SYMBOL_PATTERN, SYMBOL_RE, normalize_symbol

NY = ZoneInfo("America/New_York")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
EXAMPLE = json.loads((CONFIG_DIR / "limits.example.json").read_text())
MON_1015 = datetime(2026, 10, 19, 10, 15, tzinfo=NY)
CENT = D("0.01")


def cfg(**over):
    d = copy.deepcopy(EXAMPLE)
    d.update(over)
    return d


def mk_order(entry="100.00", stop="99.00", target="102.00", qty=10, symbol="AAPL", limits=LIMITS):
    return {
        "symbol": symbol, "side": "buy", "qty": qty, "type": "market",
        "order_class": "bracket", "entry": entry, "stop_loss": stop, "take_profit": target,
        "slippage": str(minimum_allowance(D(entry), limits)),
    }


def check(order, limits=LIMITS):
    return check_order(order, limits, DayState(), MON_1015,
                       gate=gate_for_order(order, MON_1015))


def cap_reasons(r):
    return [x for x in r["reasons"] if "over cap" in x]


# ============================== #33 exact reward cap ==============================

def test_33_reward_exactly_at_the_cap_passes():
    r = check(mk_order(stop="99.50", target="102.50"))  # risk 0.50 x 5 = 2.50
    assert r == {"verdict": "PASS", "reasons": []}


def test_33_one_cent_over_the_cap_blocks_with_exact_reason():
    r = check(mk_order(stop="99.50", target="102.51"))
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == ["reward:risk 5.02 over cap 5 (reward 2.51 > 2.50)"]


def test_33_cap_5_risk_one_cent_reward_six_cents_blocks():
    # 6.0R on a one-cent risk used to slip through the flat +0.01 tolerance
    r = check(mk_order(stop="99.99", target="100.06", qty=1))
    assert cap_reasons(r) == ["reward:risk 6.00 over cap 5 (reward 0.06 > 0.05)"]
    assert r["verdict"] == "BLOCK"
    # 5.0R on the same one-cent risk is not over the cap (blocked only by the risk floors)
    ok = check(mk_order(stop="99.99", target="100.05", qty=1))
    assert cap_reasons(ok) == []


def test_33_non_exact_product_rounds_up_to_the_cent():
    lim = replace(LIMITS, max_reward_multiple=D("2.5"))
    # risk 0.33 x 2.5 = 0.825, which exit_plan would round up to 0.83
    assert (D("2.5") * D("0.33")).quantize(CENT, rounding=ROUND_CEILING) == D("0.83")
    at = check(mk_order(stop="99.67", target="100.83", limits=lim), lim)
    assert cap_reasons(at) == []
    over = check(mk_order(stop="99.67", target="100.84", limits=lim), lim)
    assert cap_reasons(over) == ["reward:risk 2.55 over cap 2.5 (reward 0.84 > 0.83)"]


GRID_ENTRIES = ["10.00", "33.33", "52.17", "101.01", "364.18"]
GRID_OFFSETS = [D("0.40"), D("1.33"), D("2.01"), D("3.07"), D("5.50")]
MENU = ["1", "1.5", "2", "2.5", "3"]


def test_33_exit_plan_size_risk_check_grid_at_the_cap_passes():
    """Every plan exit_plan makes at R = a menu multiple or R = max_reward_multiple passes the
    cap rule, and the whole chain passes risk_check. Plans the stop floors refuse (counted, and
    asserted to be refused for a floor) or sizing cannot fit are skipped."""
    multiples = [D(m) for m in MENU] + [LIMITS.max_reward_multiple]
    passed = skipped = 0
    for entry in GRID_ENTRIES:
        for off in GRID_OFFSETS:
            for mult in multiples:
                p = exit_plan("level", D(entry), LIMITS, level=D(entry) - off,
                              reward_multiple=mult)
                if not p["ok"]:
                    assert "below" in p["reason"], p
                    skipped += 1
                    continue
                reward = D(p["target"]) - D(p["entry"])
                risk = D(p["risk_per_share"])
                assert reward <= (mult * risk).quantize(CENT, rounding=ROUND_CEILING)
                plan_obj = Plan("AAPL", D(p["entry"]), D(p["stop"]), D(p["target"]))
                allowance = minimum_allowance(D(p["entry"]), LIMITS)
                sized = size_position(plan_obj, LIMITS, slippage=allowance)
                if not sized["ok"]:
                    skipped += 1
                    continue
                r = check(sized["order"])
                assert r == {"verdict": "PASS", "reasons": []}, (entry, off, mult, p, r)
                passed += 1
    assert passed > 80 and skipped > 0


# ============================== #34 one ticker rule ==============================

def test_34_pattern_constants():
    assert SYMBOL_PATTERN == r"^[A-Z]{1,5}(?:\.[A-Z]{1,2})?\Z"
    assert SYMBOL_RE.fullmatch("BRK.B")
    assert not SYMBOL_RE.fullmatch("brk.b")


GOOD_SYMBOLS = [("AAPL", "AAPL"), ("BRK.B", "BRK.B"), ("BF.A", "BF.A"), ("A", "A"),
                ("GOOGL", "GOOGL"), (" tsla ", "TSLA"), ("aapl\n", "AAPL"),
                ("brk.bc", "BRK.BC")]
BAD_SYMBOLS = ["G ME", "AB​C", "АAAPL", "ABCDEF", "A..", "AB.", ".AB", "BRK.BCD",
               "BRK.B.C", "", "  ", "ß", "AAPı", "ＡAPL", "A\tB", "AB-C", "A1"]
NON_EMPTY_BAD = [s for s in BAD_SYMBOLS if s.strip()]


@pytest.mark.parametrize("raw,want", GOOD_SYMBOLS, ids=repr)
def test_34_normalize_accepts_and_canonicalises(raw, want):
    assert normalize_symbol(raw) == want


@pytest.mark.parametrize("raw", [*BAD_SYMBOLS, None, 5, b"AAPL", ["AAPL"]], ids=repr)
def test_34_normalize_rejects(raw):
    assert normalize_symbol(raw) is None


def test_34_sharp_s_does_not_uppercase_into_a_valid_symbol():
    assert "ß".upper() == "SS"  # why the ASCII check must come before upper-casing
    assert normalize_symbol("ß") is None
    assert normalize_symbol("ßS") is None


@pytest.mark.parametrize("raw,want", GOOD_SYMBOLS, ids=repr)
def test_34_validate_symbols_accepts(raw, want):
    assert validate_symbols([raw]) == [want]


@pytest.mark.parametrize("raw", BAD_SYMBOLS, ids=repr)
def test_34_validate_symbols_rejects(raw):
    with pytest.raises(ValueError, match="invalid symbol"):
        validate_symbols([raw])


CANONICAL_SYMBOLS = [s for s in GOOD_SYMBOLS if s[0] == s[1]]
NON_CANONICAL_SYMBOLS = [s for s in GOOD_SYMBOLS if s[0] != s[1]]


@pytest.mark.parametrize("raw,want", CANONICAL_SYMBOLS, ids=repr)
def test_34_check_order_passes_a_valid_symbol(raw, want):
    assert check(mk_order(symbol=raw)) == {"verdict": "PASS", "reasons": []}


@pytest.mark.parametrize("raw,want", NON_CANONICAL_SYMBOLS, ids=repr)
def test_34_check_order_blocks_a_valid_but_non_canonical_symbol(raw, want):
    # validate_symbols / size_position accept these and canonicalise them; the final
    # gate wants the canonical form already on the order.
    assert check(mk_order(symbol=raw)) == {
        "verdict": "BLOCK",
        "reasons": [f"order symbol {raw!r} is not canonical (expected {want!r})"]}


@pytest.mark.parametrize("raw", NON_EMPTY_BAD, ids=repr)
def test_34_check_order_blocks_an_invalid_symbol(raw):
    r = check(mk_order(symbol=raw))
    assert r["verdict"] == "BLOCK"
    assert any("is not a valid ticker" in x for x in r["reasons"]), r


@pytest.mark.parametrize("raw", ["", "  "], ids=repr)
def test_34_check_order_blocks_an_empty_symbol(raw):
    assert check(mk_order(symbol=raw)) == {"verdict": "BLOCK", "reasons": ["order has no symbol"]}


@pytest.mark.parametrize("raw,want", GOOD_SYMBOLS, ids=repr)
def test_34_size_position_accepts_a_valid_symbol(raw, want):
    out = size_position(Plan(raw, D("100.00"), D("99.00"), D("102.00")), LIMITS,
                        slippage=D("0.10"))
    assert out["ok"] is True and out["order"]["symbol"].strip().upper() == want


@pytest.mark.parametrize("raw", NON_EMPTY_BAD, ids=repr)
def test_34_size_position_refuses_an_invalid_symbol(raw):
    out = size_position(Plan(raw, D("100.00"), D("99.00"), D("102.00")), LIMITS,
                        slippage=D("0.10"))
    assert out == {"ok": False, "reason": "symbol is not a valid ticker"}


def test_34_size_position_empty_symbol_still_says_required():
    out = size_position(Plan("  ", D("100.00"), D("99.00"), D("102.00")), LIMITS, slippage=D("0"))
    assert out == {"ok": False, "reason": "symbol is required"}


@pytest.mark.parametrize("lookalike", ["АAPL", "AАPL", "AAPL​", "​AAPL",
                                       "AA PL", "AAPL.", "ＡＡＰＬ"], ids=repr)
def test_34_lookalike_of_a_blocked_symbol_is_blocked_as_invalid_not_matched(lookalike):
    lim = replace(LIMITS, blocked_symbols=frozenset({"AAPL"}))
    r = check(mk_order(symbol=lookalike, limits=lim), lim)
    assert r["verdict"] == "BLOCK"
    assert any("is not a valid ticker" in x for x in r["reasons"]), r
    assert not any("blocked list" in x for x in r["reasons"]), r  # invalid, not "matched"
    # the real symbol is still stopped by the list itself
    real = check(mk_order(symbol="AAPL", limits=lim), lim)
    assert real == {"verdict": "BLOCK", "reasons": ["AAPL is on the blocked list"]}
    padded = check(mk_order(symbol="aapl ", limits=lim), lim)
    assert padded == {"verdict": "BLOCK", "reasons": [
        "order symbol 'aapl ' is not canonical (expected 'AAPL')",
        "AAPL is on the blocked list"]}


def test_34_blocked_class_share_symbol_matches_exactly():
    lim = replace(LIMITS, blocked_symbols=frozenset({"BRK.B"}))
    assert check(mk_order(symbol="BRK.B", limits=lim), lim)["reasons"] == [
        "BRK.B is on the blocked list"]
    assert check(mk_order(symbol="brk.b", limits=lim), lim)["reasons"] == [
        "order symbol 'brk.b' is not canonical (expected 'BRK.B')",
        "BRK.B is on the blocked list"]
    assert check(mk_order(symbol="BRK.A", limits=lim), lim)["verdict"] == "PASS"


# ============================== #62 run_technical limits ==============================

def _run(limits, queue=()):
    from agents.technical import run_technical
    client = FakeClient(list(queue))
    data = FakeMarketData()
    return client, lambda: run_technical("AAPL", NOW, data, client=client, limits=limits)


@pytest.mark.parametrize("bad", [
    replace(LIMITS, max_reward_multiple=D("NaN")),
    replace(LIMITS, min_risk_atr_multiple=D("0")),
    replace(LIMITS, min_risk_pct=D("0")),
    replace(LIMITS, max_plan_age_s=901),
    replace(LIMITS, paper="yes"),
], ids=["nan-cap", "zero-atr-multiple", "zero-pct", "plan-age-901", "paper-str"])
def test_62_run_technical_rejects_invalid_hand_built_limits_before_any_call(bad):
    client, go = _run(bad)
    with pytest.raises(ValueError, match="limits config is invalid") as exc:
        go()
    assert "NaN" not in str(exc.value)
    assert client.calls == []


def test_62_run_technical_accepts_valid_hand_built_limits():
    from tests.test_technical_agent import good
    client, go = _run(replace(LIMITS, max_loss_per_trade=D("40.00")), [good()])
    out = go()
    assert out.report.symbol == "AAPL"
    assert len(client.calls) >= 1


# ===================== #75 min_risk_per_share / max_plan_age_s =====================

@pytest.mark.parametrize("bad", ["0.001", "0.015", "0.105"])
def test_75_min_risk_per_share_must_be_whole_cents(bad):
    with pytest.raises(ValueError, match="min_risk_per_share must be whole cents"):
        limits_from_dict(cfg(min_risk_per_share=bad))
    with pytest.raises(ValueError, match="whole cents"):
        validate_limits(replace(LIMITS, min_risk_per_share=D(bad)))


@pytest.mark.parametrize("good", ["0.01", "0.10", "1", "49.99"])
def test_75_whole_cent_min_risk_per_share_passes(good):
    assert limits_from_dict(cfg(min_risk_per_share=good)).min_risk_per_share == D(good)


@pytest.mark.parametrize("bad", ["50.00", "50.01", "500"])
def test_75_min_risk_per_share_at_or_above_max_loss_is_refused(bad):
    with pytest.raises(ValueError, match="min_risk_per_share must be below max_loss_per_trade"):
        limits_from_dict(cfg(min_risk_per_share=bad))  # max_loss_per_trade is 50.00


@pytest.mark.parametrize("age", [1, 300, 900])
def test_75_plan_age_in_range_passes(age):
    assert limits_from_dict(cfg(max_plan_age_s=age)).max_plan_age_s == age


@pytest.mark.parametrize("age", [0, -1, 901, 3600])
def test_75_plan_age_out_of_range_is_refused(age):
    with pytest.raises(ValueError, match="max_plan_age_s must be an integer from 1 to 900"):
        limits_from_dict(cfg(max_plan_age_s=age))
    r = check(mk_order(), replace(LIMITS, max_plan_age_s=age))
    assert r["verdict"] == "BLOCK" and r["reasons"][0].startswith("limits invalid:")


# ============================== #76 min_risk_atr_multiple > 0 ==============================

@pytest.mark.parametrize("bad", ["0", "0.0", "0.00", "-1"])
def test_76_zero_or_negative_atr_multiple_is_refused(bad):
    with pytest.raises(ValueError, match="min_risk_atr_multiple"):
        limits_from_dict(cfg(min_risk_atr_multiple=bad))


def test_76_zero_message_and_hand_built():
    with pytest.raises(ValueError, match="must be greater than zero"):
        limits_from_dict(cfg(min_risk_atr_multiple="0"))
    with pytest.raises(ValueError, match="must be greater than zero"):
        validate_limits(replace(LIMITS, min_risk_atr_multiple=D("0")))


def test_76_check_order_blocks_with_limits_invalid():
    r = check(mk_order(), replace(LIMITS, min_risk_atr_multiple=D("0")))
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == ["limits invalid: min_risk_atr_multiple must be greater than zero, "
                            "got 0"]


def test_76_smallest_cent_multiple_passes():
    lim = limits_from_dict(cfg(min_risk_atr_multiple="0.01"))
    assert lim.min_risk_atr_multiple == D("0.01")
    assert check(mk_order(), lim)["verdict"] == "PASS"


# ========================= #77 percent-of-entry stop floor =========================

def plan_at(level, entry="100.00", limits=LIMITS, **kw):
    return exit_plan("level", D(entry), limits, level=D(level), **kw)


def test_77_exit_plan_at_exactly_the_floor_passes():
    r = plan_at("99.75")  # 0.25% of 100.00 = 0.25
    assert r["ok"] is True and r["risk_per_share"] == "0.25" and r["stop"] == "99.75"


def test_77_exit_plan_one_cent_below_the_floor_is_refused():
    r = plan_at("99.76")
    assert r == {"ok": False,
                 "reason": "risk per share 0.24 is below 0.25% of entry (0.25)"}


def test_77_floor_checked_after_min_risk_per_share():
    # risk 0.05 is under both floors; the absolute one is reported first
    r = plan_at("99.95")
    assert r["reason"] == "risk per share 0.05 is below min_risk_per_share 0.10"


def test_77_floor_follows_the_limit():
    wide = replace(LIMITS, min_risk_pct=D("1"))
    assert plan_at("99.00", limits=wide)["ok"] is True
    assert plan_at("99.01", limits=wide)["reason"] == (
        "risk per share 0.99 is below 1% of entry (1)")


def test_77_risk_check_at_exactly_the_floor_passes():
    r = check(mk_order(stop="99.75", target="100.25", qty=10))
    assert r == {"verdict": "PASS", "reasons": []}


def test_77_risk_check_one_cent_below_the_floor_blocks_with_the_reason():
    r = check(mk_order(stop="99.76", target="100.24", qty=10))
    assert r == {"verdict": "BLOCK",
                 "reasons": ["risk per share 0.24 below 0.25% of entry (0.25)"]}


def test_77_risk_check_uses_planned_prices_not_slippage():
    # a big allowance does not lift the planned risk over the floor
    o = mk_order(stop="99.76", target="100.24", qty=1)
    o["slippage"] = "5.00"
    r = check(o)
    assert "risk per share 0.24 below 0.25% of entry (0.25)" in r["reasons"]


@pytest.mark.parametrize("name", ["clean-buy", "red-flag", "extended-chase", "tsla"])
def test_77_smoke_scenario_plans_are_still_accepted_at_the_default(name):
    assert LIMITS.min_risk_pct == D("0.25")
    s = session(name)
    lv = price_levels(s.summary)
    for level in ("vwap", "or_high"):
        res, err = s.execute("plan_exit", {"setup": "level", "entry": lv["last_close"],
                                           "level": lv[level]})
        assert err is False and res["ok"] is True, (name, level, res)
        floor = D(res["entry"]) * LIMITS.min_risk_pct / 100
        assert D(res["risk_per_share"]) >= floor


def test_77_missing_atr_is_noted():
    note = "no atr given; the ATR floor (min_risk_atr_multiple) was not applied"
    r = plan_at("99.00")
    assert r["ok"] is True and note in r["notes"]
    with_atr = plan_at("99.00", atr=D("0.50"))
    assert with_atr["ok"] is True and note not in with_atr["notes"]
    orb = exit_plan("orb", D("100.00"), LIMITS, opening_range={
        "high": "101.00", "low": "99.00", "complete": True, "missing_minutes": 0})
    assert orb["ok"] is True and note in orb["notes"]


def test_77_config_default_example_and_bounds():
    assert Limits.__dataclass_fields__["min_risk_pct"].default == D("0.25")
    assert EXAMPLE["min_risk_pct"] == "0.25" and LIMITS.min_risk_pct == D("0.25")
    d = cfg()
    del d["min_risk_pct"]
    assert limits_from_dict(d).min_risk_pct == D("0.25")
    assert limits_from_dict(cfg(min_risk_pct="9.99")).min_risk_pct == D("9.99")
    assert limits_from_dict(cfg(min_risk_pct="0.01")).min_risk_pct == D("0.01")


@pytest.mark.parametrize("bad", ["0", "10", "-1", "NaN", "Infinity", "10.5", "abc", ""])
def test_77_config_bounds_refuse(bad):
    with pytest.raises(ValueError, match="min_risk_pct"):
        limits_from_dict(cfg(min_risk_pct=bad))


@pytest.mark.parametrize("bad", [D("0"), D("10"), D("-1"), D("NaN"), 0.25])
def test_77_hand_built_bounds_refuse(bad):
    with pytest.raises(ValueError, match="min_risk_pct"):
        validate_limits(replace(LIMITS, min_risk_pct=bad))
    assert check(mk_order(), replace(LIMITS, min_risk_pct=bad))["reasons"][0].startswith(
        "limits invalid:")


# ========================= #78 build_order noise stops =========================

@pytest.fixture
def sized_spy(monkeypatch):
    calls = []
    real = orders.size_position

    def spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(orders, "size_position", spy)
    return calls


def forged(entry, stop, target, risk, **over):
    return plan(entry=entry, stop=stop, target=target, risk_per_share=risk,
                reward_to_risk="1.00", last_close=entry, **over)


def build(p, limits=LIMITS):
    return build_order(p, limits, T0, fresh_quote(p.entry, T0))


def test_78_risk_below_min_risk_per_share_is_refused_before_sizing(sized_spy):
    p = forged("364.18", "364.10", "364.26", "0.08")  # self-consistent, risk 0.08 < 0.10
    out = build(p)
    assert out == {"ok": False,
                   "reason": "risk per share 0.08 is below min_risk_per_share 0.10"}
    assert sized_spy == []


def test_78_risk_below_percent_of_entry_is_refused_before_sizing(sized_spy):
    # 0.50 clears the 0.10 floor but not 0.25% of 364.18 (0.9105)
    p = forged("364.18", "363.68", "364.68", "0.50")
    out = build(p)
    assert out == {"ok": False,
                   "reason": "risk per share 0.50 is below 0.25% of entry (0.91045)"}
    assert sized_spy == []


def test_78_risk_just_above_both_floors_is_sized(sized_spy):
    p = forged("364.18", "363.26", "365.10", "0.92")  # 0.92 > 0.9105
    out = build(p)
    assert out["ok"] is True and len(sized_spy) == 1
    assert check(out["order"])["verdict"] == "PASS"


def test_78_boundary_exactly_at_the_percent_floor_is_sized(sized_spy):
    p = forged("100.00", "99.75", "100.25", "0.25")
    assert build(p)["ok"] is True and len(sized_spy) == 1


def test_78_one_cent_below_the_percent_floor_is_refused(sized_spy):
    p = forged("100.00", "99.76", "100.24", "0.24")
    out = build(p)
    assert out == {"ok": False,
                   "reason": "risk per share 0.24 is below 0.25% of entry (0.25)"}
    assert sized_spy == []


def test_78_exactly_min_risk_per_share_passes_that_floor(sized_spy):
    lim = replace(LIMITS, min_risk_pct=D("0.01"))  # isolate the absolute floor
    p = forged("100.00", "99.90", "100.10", "0.10")
    assert build(p, lim)["ok"] is True and len(sized_spy) == 1


# ======================== #140 max_order_quote_age_s ========================

@pytest.mark.parametrize("age", [1, 15, 60])
def test_140_quote_age_in_range_passes(age):
    assert limits_from_dict(cfg(max_order_quote_age_s=age)).max_order_quote_age_s == age
    assert check(mk_order(), replace(LIMITS, max_order_quote_age_s=age))["verdict"] == "PASS"


@pytest.mark.parametrize("age", [0, -1, 61, 3600])
def test_140_quote_age_out_of_range_is_refused(age):
    with pytest.raises(ValueError, match="max_order_quote_age_s must be an integer from 1 to 60"):
        limits_from_dict(cfg(max_order_quote_age_s=age))
    r = check(mk_order(), replace(LIMITS, max_order_quote_age_s=age))
    assert r["verdict"] == "BLOCK" and r["reasons"][0].startswith("limits invalid:")


# ================= review fixes: scan keys, anchored pattern, blocklist, floors =================

def _scan_rows(key):
    from tests.test_scan import CRIT, mk
    from tools.scan import scan
    return scan({key: mk()}, CRIT, LIMITS)


@pytest.mark.parametrize("key", ["ß", "ﬁ", "ı", "АAPL"], ids=ascii)
def test_scan_rejects_non_ascii_snapshot_keys_as_invalid_symbol(key):
    r = _scan_rows(key)
    assert r["candidates"] == []
    assert r["rejected"] == [{"symbol": key, "reasons": ["invalid symbol"]}]


def test_scan_accepts_a_padded_lowercase_key_as_canonical():
    r = _scan_rows(" aapl ")
    assert r["rejected"] == []
    assert [c["symbol"] for c in r["candidates"]] == ["AAPL"]


def test_symbol_re_match_is_anchored_at_both_ends():
    assert SYMBOL_RE.match("AAPL")
    assert SYMBOL_RE.match("BRK.B")
    assert SYMBOL_RE.match("AAPLXYZ") is None
    assert SYMBOL_RE.match("AAPL\n") is None  # \Z, not $
    assert SYMBOL_RE.match("xAAPL") is None


@pytest.mark.parametrize("bad", [["BRK-B"], ["GME!!"], ["ＧＭＥ"], ["G ME"]], ids=ascii)
def test_blocked_symbols_loader_refuses_an_invalid_ticker(bad):
    with pytest.raises(ValueError, match="not a valid ticker"):
        limits_from_dict(cfg(blocked_symbols=bad))


def test_blocked_symbols_loader_normalises_and_skips_blanks():
    lim = limits_from_dict(cfg(blocked_symbols=[" gme ", "BRK.B", ""]))
    assert lim.blocked_symbols == frozenset({"GME", "BRK.B"})
    assert validate_limits(lim) is None


@pytest.mark.parametrize("bad", [frozenset({"gme"}), frozenset({"G ME"}),
                                 frozenset({" GME"}), frozenset({"GME", "brk.b"})], ids=repr)
def test_validate_limits_reports_non_canonical_blocked_symbols(bad):
    with pytest.raises(ValueError, match="blocked_symbols must be a set of valid upper-case"):
        validate_limits(replace(LIMITS, blocked_symbols=bad))


@pytest.mark.parametrize("good", [frozenset({"GME"}), frozenset({"GME", "BRK.B"}), frozenset()],
                         ids=repr)
def test_validate_limits_accepts_canonical_blocked_symbols(good):
    assert validate_limits(replace(LIMITS, blocked_symbols=good)) is None


def test_check_order_canonical_symbol_passes():
    assert check(mk_order(symbol="TSLA")) == {"verdict": "PASS", "reasons": []}


@pytest.mark.parametrize("raw", [" TSLA", "tsla", "TSLA\n", "TSLA "], ids=repr)
def test_check_order_blocks_a_non_canonical_symbol(raw):
    assert check(mk_order(symbol=raw)) == {
        "verdict": "BLOCK",
        "reasons": [f"order symbol {raw!r} is not canonical (expected 'TSLA')"]}


def test_check_order_padded_blocked_symbol_gets_both_reasons():
    lim = replace(LIMITS, blocked_symbols=frozenset({"GME"}))
    assert check(mk_order(symbol="gme", limits=lim), lim) == {
        "verdict": "BLOCK",
        "reasons": ["order symbol 'gme' is not canonical (expected 'GME')",
                    "GME is on the blocked list"]}
    assert check(mk_order(symbol="GME", limits=lim), lim) == {
        "verdict": "BLOCK", "reasons": ["GME is on the blocked list"]}


# ---- sub-cent percent floor: 0.25% of 40.01 is 0.100025, printed exactly ----

def test_percent_floor_subcent_refused_by_exit_plan_build_order_and_risk_check(sized_spy):
    lim = LIMITS  # min_risk_per_share 0.10, min_risk_pct 0.25, no atr given
    assert lim.min_risk_per_share == D("0.10") and lim.min_risk_pct == D("0.25")
    # exit_plan
    ep = exit_plan("level", D("40.01"), lim, level=D("39.91"))
    assert ep["ok"] is False
    assert ep["reason"] == "risk per share 0.10 is below 0.25% of entry (0.100025)"
    # build_order: refused before size_position is called
    p = forged("40.01", "39.91", "40.21", "0.10")
    out = build(p, lim)
    assert out == {"ok": False,
                   "reason": "risk per share 0.10 is below 0.25% of entry (0.100025)"}
    assert sized_spy == []
    # risk_check
    r = check(mk_order(entry="40.01", stop="39.91", target="40.21", qty=10))
    assert r["verdict"] == "BLOCK"
    assert "risk per share 0.10 below 0.25% of entry (0.100025)" in r["reasons"]


def test_percent_floor_subcent_boundary_accepted_by_all_three_gates(sized_spy):
    ep = exit_plan("level", D("40.00"), LIMITS, level=D("39.90"))  # floor 0.1, risk 0.10
    assert ep["ok"] is True and ep["risk_per_share"] == "0.10"
    out = build(forged("40.00", "39.90", "40.20", "0.10"))
    assert out["ok"] is True and len(sized_spy) == 1
    assert check(out["order"]) == {"verdict": "PASS", "reasons": []}
    r = check(mk_order(entry="40.00", stop="39.90", target="40.20", qty=10))
    assert r == {"verdict": "PASS", "reasons": []}


@pytest.mark.parametrize("cap", ["4.5", "3.3"])
def test_non_integer_reward_cap_grid_every_accepted_plan_passes_risk_check(cap):
    lim = replace(LIMITS, max_reward_multiple=D(cap))
    assert validate_limits(lim) is None
    multiples = [D(m) for m in MENU if D(m) <= D(cap)] + [D(cap)]
    passed = 0
    for entry in GRID_ENTRIES:
        for off in GRID_OFFSETS:
            for mult in multiples:
                p = exit_plan("level", D(entry), lim, level=D(entry) - off,
                              reward_multiple=mult)
                if not p["ok"]:
                    assert "below" in p["reason"], p
                    continue
                reward = D(p["target"]) - D(p["entry"])
                risk = D(p["risk_per_share"])
                assert reward <= (D(cap) * risk).quantize(CENT, rounding=ROUND_CEILING)
                sized = size_position(
                    Plan("AAPL", D(p["entry"]), D(p["stop"]), D(p["target"])), lim,
                    slippage=minimum_allowance(D(p["entry"]), lim))
                if not sized["ok"]:
                    continue
                r = check(sized["order"], lim)
                assert r == {"verdict": "PASS", "reasons": []}, (cap, entry, off, mult, p, r)
                passed += 1
    assert passed > 40


@pytest.mark.parametrize("cap", ["4.5", "3.3"])
def test_non_integer_reward_cap_one_cent_over_exact_cap_is_blocked(cap):
    lim = replace(LIMITS, max_reward_multiple=D(cap))
    at = (D(cap) * D("1.00")).quantize(CENT, rounding=ROUND_CEILING)

    def order(reward):
        return mk_order(stop="99.00", target=str(D("100.00") + reward), limits=lim)

    assert cap_reasons(check(order(at), lim)) == []
    assert cap_reasons(check(order(at + CENT), lim)) != []
