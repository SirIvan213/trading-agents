"""Phase 5 gate C, issue #68: slippage on market entries.

An order is a market order, so it fills near the live ask, not at the plan's entry. The allowance
max(ask - bid, slippage_pct% x entry, 0.01), rounded up to the cent, is added to the per-share
risk and to the per-share price in sizing and in risk_check; an order whose ask is already past
entry + allowance, or whose quote is missing, stale or crossed, is refused. Offline: saved
quote shapes, fake clocks, a fake quote source. No network, no model.
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from itertools import product

import pytest

import pipeline.order_flow as of
import pipeline.smoke_debate as S
from journal.log import APPROVED, Journal
from pipeline.decide import run_decision
from pipeline.order_flow import approve, propose, quotes_from, render_ticket
from pipeline.orders import build_order
from pipeline.scenarios import ScenarioData, load_scenario
from tests.fakes import NOON, FakeAllData
from tests.order_helpers import (
    AS_OF,
    T0,
    QuoteSource,
    QuotingData,
    StepClock,
    events_clear,
    fresh_quote,
    gate_for_order,
    plan,
    quotes_at,
    raw_lines,
    sealed_ledger,
    with_slippage,
)
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tests.test_phase4b_proofs import SYMBOL, Router, isolated_agent_env, scripts  # noqa: F401
from tools.config import CONFIG_DIR, Limits, limits_from_dict, load_limits, validate_limits
from tools.risk_check import DayState, check_order
from tools.size import Plan, size_position
from tools.slippage import minimum_allowance, slippage_from_quote

EXAMPLE_PATH = CONFIG_DIR / "limits.example.json"
EXAMPLE = json.loads(EXAMPLE_PATH.read_text())
LIMITS = load_limits(EXAMPLE_PATH)
NOW = T0  # Monday 10:15:30 ET, 30 s after the TSLA test plan was made
NO_PCT = replace(LIMITS, slippage_pct=D("0"))  # only the spread and the 0.01 floor remain


@pytest.fixture(autouse=True)
def fixed_limits(monkeypatch):
    """The example limits for the order flow, whatever config/limits.json holds here."""
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)


def quote(bid: str, ask: str, t: datetime = NOW, age_s: float = 1) -> dict:
    return {"ap": float(ask), "bp": float(bid), "t": (t - timedelta(seconds=age_s)).isoformat()}


def slip(entry: str, q: object, limits: Limits = LIMITS, now: datetime = NOW) -> dict:
    return slippage_from_quote(D(entry), q, limits, now)


# ======================= allowance math =======================

@pytest.mark.parametrize(
    ("entry", "bid", "ask", "limits", "allowance", "why"),
    [
        ("20.00", "19.90", "20.00", LIMITS, "0.10", "spread 0.10 beats 0.02% pct"),
        ("100.00", "99.99", "100.00", LIMITS, "0.10", "pct 0.10 beats spread 0.01"),
        ("100.00", "99.99", "100.00", NO_PCT, "0.01", "0.01 floor (spread 0.01 = floor)"),
        ("100.00", "100.00", "100.00", NO_PCT, "0.01", "bid == ask, no pct: the 0.01 floor"),
        ("1.00", "1.00", "1.00", LIMITS, "0.01", "pct 0.001 is below the 0.01 floor"),
        ("80.70", "80.69", "80.70", LIMITS, "0.09", "pct 0.0807 is rounded UP to 0.09"),
        ("364.18", "364.13", "364.18", LIMITS, "0.37", "TSLA: pct 0.36418 up to 0.37"),
        ("20.00", "19.995", "20.00", NO_PCT, "0.01", "spread 0.005 rounds up to 0.01"),
        ("50.00", "49.00", "50.00", LIMITS, "1.00", "wide spread 1.00 beats pct 0.05"),
    ],
)
def test_allowance_is_the_largest_of_spread_pct_and_floor_rounded_up(
        entry, bid, ask, limits, allowance, why):
    r = slip(entry, quote(bid, ask), limits)
    assert r["ok"] is True, (r, why)
    assert r["allowance"] == allowance, why
    assert D(r["allowance"]) == D(r["allowance"]).quantize(D("0.01"))


def test_ok_result_shape_and_json_serializable():
    r = slip("364.18", quote("364.13", "364.18"))
    assert set(r) == {"ok", "allowance", "bid", "ask", "spread", "quote_t", "age_s"}
    assert (r["bid"], r["ask"], r["spread"], r["age_s"]) == ("364.13", "364.18", "0.05", 1)
    assert r["quote_t"] == (NOW - timedelta(seconds=1)).isoformat()
    assert json.loads(json.dumps(r)) == r


@pytest.mark.parametrize(
    ("entry", "pct", "expected"),
    [("364.18", "0.10", "0.37"), ("80.70", "0.10", "0.09"), ("100.00", "0.10", "0.10"),
     ("1.00", "0.10", "0.01"), ("364.18", "0", "0.01"), ("364.18", "0.5", "1.83"),
     ("20.00", "0.10", "0.02")],
)
def test_minimum_allowance(entry, pct, expected):
    limits = replace(LIMITS, slippage_pct=D(pct))
    assert minimum_allowance(D(entry), limits) == D(expected)


def test_allowance_follows_the_configured_percent():
    q = quote("364.17", "364.18")
    assert slip("364.18", q, replace(LIMITS, slippage_pct=D("0.5")))["allowance"] == "1.83"
    assert slip("364.18", q, replace(LIMITS, slippage_pct=D("0.01")))["allowance"] == "0.04"


# ---- age: 15 s fresh, 15.001 s stale ----

def aged(seconds: float) -> dict:
    return quote("364.16", "364.18", age_s=seconds)


@pytest.mark.parametrize("age", [0, 1, 14.999, 15])
def test_quote_up_to_the_max_age_is_fresh(age):
    assert slip("364.18", aged(age))["ok"] is True


@pytest.mark.parametrize(("age", "shown"), [(15.001, 15), (16, 16), (3600, 3600)])
def test_quote_older_than_the_max_age_is_refused(age, shown):
    assert slip("364.18", aged(age)) == {
        "ok": False, "reason": f"live quote is {shown}s old (max 15s)"}


def test_max_age_follows_the_limit():
    lim = replace(LIMITS, max_order_quote_age_s=30)
    assert slip("364.18", aged(30), lim)["ok"] is True
    assert slip("364.18", aged(30.001), lim)["ok"] is False
    short = replace(LIMITS, max_order_quote_age_s=1)
    assert slip("364.18", aged(1), short)["ok"] is True
    assert slip("364.18", aged(1.001), short)["ok"] is False


# ---- future: 5 s ok, 5.001 refused ----

@pytest.mark.parametrize("ahead", [0.5, 5])
def test_quote_up_to_five_seconds_in_the_future_is_accepted(ahead):
    r = slip("364.18", aged(-ahead))
    assert r["ok"] is True and r["age_s"] in (0, -0, int(-ahead))


@pytest.mark.parametrize("ahead", [5.001, 6, 3600])
def test_quote_more_than_five_seconds_in_the_future_is_refused(ahead):
    assert slip("364.18", aged(-ahead)) == {
        "ok": False, "reason": "live quote is dated in the future"}


# ---- price moved: ask == entry + allowance ok, one cent more refused ----

def test_ask_exactly_entry_plus_allowance_is_accepted():
    # entry 364.18, pct allowance 0.37 -> ask up to 364.55
    r = slip("364.18", quote("364.54", "364.55"))
    assert r["ok"] is True and r["allowance"] == "0.37" and r["ask"] == "364.55"


def test_ask_one_cent_over_entry_plus_allowance_is_refused():
    r = slip("364.18", quote("364.55", "364.56"))
    assert r == {"ok": False, "reason": (
        "price moved: ask 364.56 is above entry 364.18 + slippage allowance 0.37")}


def test_wide_spread_raises_the_allowance_so_the_same_ask_is_accepted():
    # ask 100.10 / entry 100.00: with a 1-cent spread the allowance is the 0.10 pct floor (ok);
    # with ask 100.11 it is refused; a 0.30 spread (allowance 0.30) lets ask 100.25 through
    assert slip("100.00", quote("100.09", "100.10"))["ok"] is True
    assert slip("100.00", quote("100.10", "100.11"))["ok"] is False
    assert slip("100.00", quote("99.95", "100.25"))["allowance"] == "0.30"
    assert slip("100.00", quote("99.95", "100.25"))["ok"] is True


def test_ask_below_entry_is_accepted():
    r = slip("364.18", quote("363.00", "363.05"))
    assert r["ok"] is True and r["allowance"] == "0.37"  # the price fell: no move to cover


def test_bid_equal_to_ask_is_not_crossed():
    r = slip("364.18", quote("364.18", "364.18"))
    assert r["ok"] is True and r["spread"] == "0.00" and r["allowance"] == "0.37"


def test_crossed_quote_is_refused_with_both_prices():
    assert slip("364.18", quote("364.19", "364.18")) == {
        "ok": False, "reason": "live quote is crossed (bid 364.19 > ask 364.18)"}


# ---- malformed quotes ----

NO_QUOTE = "no live quote"
NO_BID_ASK = "live quote has no valid bid and ask"
NO_TS = "live quote has no valid timestamp"
GOOD_T = (NOW - timedelta(seconds=1)).isoformat()


@pytest.mark.parametrize("bad", [None, [], "quote", 364.18, ("ap", 1), True], ids=repr)
def test_not_a_dict_is_no_live_quote(bad):
    assert slip("364.18", bad) == {"ok": False, "reason": NO_QUOTE}


@pytest.mark.parametrize(
    "q",
    [{}, {"t": GOOD_T}, {"ap": 364.18, "t": GOOD_T}, {"bp": 364.16, "t": GOOD_T},
     {"ap": 0, "bp": 364.16, "t": GOOD_T}, {"ap": 364.18, "bp": 0, "t": GOOD_T},
     {"ap": -1, "bp": 364.16, "t": GOOD_T}, {"ap": 364.18, "bp": -0.01, "t": GOOD_T},
     {"ap": None, "bp": 364.16, "t": GOOD_T}, {"ap": True, "bp": 364.16, "t": GOOD_T},
     {"ap": 364.18, "bp": False, "t": GOOD_T}, {"ap": "abc", "bp": 364.16, "t": GOOD_T},
     {"ap": "NaN", "bp": 364.16, "t": GOOD_T}, {"ap": float("nan"), "bp": 1, "t": GOOD_T},
     {"ap": 364.18, "bp": float("inf"), "t": GOOD_T},
     {"ap": "Infinity", "bp": 364.16, "t": GOOD_T}, {"ap": [364.18], "bp": 364.16, "t": GOOD_T}],
    ids=repr,
)
def test_missing_or_non_positive_or_non_finite_bid_ask_is_refused(q):
    assert slip("364.18", q) == {"ok": False, "reason": NO_BID_ASK}


@pytest.mark.parametrize(
    "t",
    [None, "", "yesterday", "2026-10-19", 12345, ["2026-10-19T10:15:29-04:00"], True,
     "2026-10-19T10:15:29",  # naive: no offset, so no instant
     "2026-13-45T10:15:29-04:00"],
    ids=repr,
)
def test_missing_or_invalid_or_naive_timestamp_is_refused(t):
    q = {"ap": 364.18, "bp": 364.16, "t": t}
    assert slip("364.18", q) == {"ok": False, "reason": NO_TS}
    assert slip("364.18", {k: v for k, v in q.items() if k != "t"}) == {
        "ok": False, "reason": NO_TS}


def test_alpaca_z_suffix_timestamp_is_accepted():
    zulu = (NOW - timedelta(seconds=3)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    assert zulu.endswith("Z")
    r = slip("364.18", {"ap": 364.18, "bp": 364.16, "t": zulu})
    assert r["ok"] is True and r["age_s"] == 3


def test_naive_now_is_refused_before_anything_else():
    r = slip("364.18", quote("364.16", "364.18"), now=NOW.replace(tzinfo=None))
    assert r == {"ok": False, "reason": "now must be timezone-aware"}


def test_crossed_is_reported_before_a_bad_timestamp_and_bad_prices_before_crossed():
    assert "crossed" in slip("364.18", {"ap": 364.18, "bp": 364.19, "t": "nope"})["reason"]
    assert slip("364.18", {"ap": 364.18, "bp": 0, "t": "nope"})["reason"] == NO_BID_ASK


def test_stale_is_reported_before_price_moved():
    q = quote("999.00", "1000.00", age_s=60)
    assert slip("364.18", q)["reason"] == "live quote is 60s old (max 15s)"


# ======================= sizing with the allowance =======================

def test_size_position_default_slippage_is_zero_and_unchanged():
    r = size_position(Plan("TSLA", D("364.18"), D("358.32"), D("370.04")), LIMITS)
    assert r["shares"] == 8 and r["max_loss"] == "46.88" and r["slippage"] == "0"
    assert r["order"]["slippage"] == "0"


def test_example_20_19_spread_2c_is_49_shares_not_50():
    entry, stop = D("20.00"), D("19.00")
    flat = size_position(Plan("AAPL", entry, stop, D("21.00")), LIMITS)
    assert flat["shares"] == 50 and flat["max_loss"] == "50.00"  # the old, optimistic size
    allowance = slip("20.00", quote("19.98", "20.00"))["allowance"]
    assert allowance == "0.02"
    r = size_position(Plan("AAPL", entry, stop, D("21.00")), LIMITS, slippage=D(allowance))
    assert r["ok"] and r["shares"] == 49
    assert r["max_loss"] == "49.98"  # 49 x (1.00 + 0.02): the worst case, within 50.00
    assert r["risk_per_share"] == "1.00" and r["slippage"] == "0.02"
    assert r["order"]["slippage"] == "0.02" and r["order"]["qty"] == 49


def test_tsla_spread_5c_gives_allowance_37c_8_shares_max_loss_49_84():
    plan_ = plan()
    out = build_order(plan_, LIMITS, NOW, quote("364.13", "364.18"))
    assert out["ok"] is True
    assert out["slippage"] == "0.37" and out["shares"] == 8 and out["max_loss"] == "49.84"
    assert out["order"]["slippage"] == "0.37" and out["risk_per_share"] == "5.86"
    assert out["quote"] == {"bid": "364.13", "ask": "364.18", "spread": "0.05",
                            "quote_t": (NOW - timedelta(seconds=1)).isoformat(), "age_s": 1}
    assert check_order(out["order"], LIMITS, DayState(), NOW,
        gate=gate_for_order(out["order"], NOW)) == {"verdict": "PASS", "reasons": []}


def test_position_cap_uses_entry_plus_slippage():
    lim = replace(LIMITS, max_position_value=D("1000"), max_loss_per_trade=D("1000"))
    p = Plan("AAPL", D("20.00"), D("19.00"), D("21.00"))
    assert size_position(p, lim)["shares"] == 50  # 1000 / 20
    r = size_position(p, lim, slippage=D("0.50"))
    assert r["shares"] == 48 and r["decided_by"] == "max_position_value"  # 1000 / 20.50
    assert 48 * D("20.50") <= 1000 < 49 * D("20.50")


def test_cash_cap_uses_entry_plus_slippage():
    lim = replace(LIMITS, account_cash=D("1000"), cash_buffer_pct=D("0"),
                  max_position_value=D("100000"), max_loss_per_trade=D("1000"))
    p = Plan("AAPL", D("20.00"), D("19.00"), D("21.00"))
    assert size_position(p, lim)["shares"] == 50
    r = size_position(p, lim, slippage=D("0.50"))
    assert r["shares"] == 48 and r["decided_by"] == "cash_after_buffer"


def test_stop_wider_than_the_budget_after_slippage_is_refused():
    # risk 49.90 fits one share under 50.00; with a 0.20 allowance the worst loss is 50.10
    p = Plan("AAPL", D("100.00"), D("50.10"), D("160.00"))
    assert size_position(p, LIMITS)["shares"] == 1
    r = size_position(p, LIMITS, slippage=D("0.20"))
    assert r["ok"] is False and "max_loss_per_trade" in r["reason"]


@pytest.mark.parametrize(
    "bad", [-D("0.01"), D("NaN"), D("Infinity"), D("-Infinity"), 0.37, "0.37", 1, None, True],
    ids=repr)
def test_size_position_refuses_a_bad_slippage(bad):
    r = size_position(Plan("AAPL", D("20.00"), D("19.00"), D("21.00")), LIMITS, slippage=bad)
    assert r == {"ok": False, "reason": "slippage must be a finite, non-negative Decimal"}


def test_size_position_accepts_zero_and_positive_slippage():
    p = Plan("AAPL", D("20.00"), D("19.00"), D("21.00"))
    assert size_position(p, LIMITS, slippage=D("0"))["ok"] is True
    assert size_position(p, LIMITS, slippage=D("0.00"))["slippage"] == "0.00"


# ---- the worst-case guarantee over a grid ----

ENTRIES = ["3.50", "20.00", "80.42", "364.18", "999.99"]
RISKS = ["0.10", "0.50", "1.00", "5.86", "12.00"]
SPREADS = ["0.00", "0.01", "0.02", "0.05", "0.25"]
GRID_LIMITS = [
    LIMITS, NO_PCT, replace(LIMITS, slippage_pct=D("0.5")),
    replace(LIMITS, max_loss_per_trade=D("20.00"), max_position_value=D("1500.00")),
    replace(LIMITS, account_cash=D("2000"), cash_buffer_pct=D("20")),
]
ASK_OFFSETS = ["0", "0.01", "spread", "edge", "over"]


def grid_plan(entry: str, risk: str):
    e, r = D(entry), D(risk)
    if e - r <= 0:
        return None
    return plan("grid", entry=entry, stop=str(e - r), target=str(e + r), risk_per_share=risk,
                reward_to_risk="1.00", last_close=entry)  # a 1.00R plan


def test_every_accepted_quote_keeps_every_fill_within_every_limit():
    accepted = refused_price = unsizable = 0
    for entry, risk, spread, limits, off in product(
            ENTRIES, RISKS, SPREADS, GRID_LIMITS, ASK_OFFSETS):
        p = grid_plan(entry, risk)
        if p is None:
            continue
        edge = max(D(spread), minimum_allowance(D(entry), limits)).quantize(
            D("0.01"), "ROUND_CEILING")  # the allowance for this quote
        offsets = {"spread": D(spread), "edge": edge, "over": edge + D("0.01")}
        offset = offsets[off] if off in offsets else D(off)
        ask = D(entry) + offset
        q = {"ap": float(ask), "bp": float(ask - D(spread)),
             "t": (NOW - timedelta(seconds=2)).isoformat()}
        out = build_order(p, limits, NOW, q)
        if out["ok"] is not True:
            if out["reason"].startswith("price moved"):
                refused_price += 1
                continue
            assert "max_loss_per_trade" in out["reason"] or "shares" in out["reason"], out
            unsizable += 1
            continue
        accepted += 1
        allowance, shares = D(out["slippage"]), out["shares"]
        e, stop = D(entry), D(p.stop)
        assert shares >= 1 and allowance >= minimum_allowance(e, limits)
        assert ask <= e + allowance
        usable_cash = limits.account_cash * (D(100) - limits.cash_buffer_pct) / 100
        for fill in (e, e + allowance / 2, ask, e + allowance):
            if fill > e + allowance:
                continue
            assert shares * (fill - stop) <= limits.max_loss_per_trade, (entry, risk, spread)
            assert shares * fill <= limits.max_position_value, (entry, risk, spread)
            assert shares * fill <= usable_cash, (entry, risk, spread)
        # tight: one more share would break a limit at the worst allowed fill
        worst = e + allowance
        assert ((shares + 1) * (worst - stop) > limits.max_loss_per_trade
                or (shares + 1) * worst > limits.max_position_value
                or (shares + 1) * worst > usable_cash)
        assert D(out["max_loss"]) == ((e - stop + allowance) * shares).quantize(D("0.01"))
        assert D(out["max_loss"]) <= limits.max_loss_per_trade
        # and risk_check, which recomputes it, agrees (1.00R plans pass the reward:risk rule)
        verdict = check_order(out["order"], limits, DayState(), NOW,
            gate=gate_for_order(out["order"], NOW))
        assert verdict["verdict"] == "PASS", (entry, risk, spread, verdict)
    assert accepted > 500 and refused_price > 200 and unsizable >= 0  # both paths ran


# ======================= risk_check =======================

ORDER = size_position(Plan("TSLA", D("364.18"), D("358.32"), D("370.04")), LIMITS,
                      slippage=D("0.37"))["order"]
MON_1015 = AS_OF


def check(order=ORDER, limits=LIMITS, state=None):
    return check_order(order, limits, state or DayState(), MON_1015,
        gate=gate_for_order(order, MON_1015))


def reasons(order=ORDER, limits=LIMITS, state=None):
    r = check(order, limits, state)
    return r["verdict"], r["reasons"]


def test_order_fields_include_slippage():
    from tools.risk_check import ORDER_FIELDS
    assert "slippage" in ORDER_FIELDS and len(ORDER_FIELDS) == 9
    assert ORDER_FIELDS == frozenset(ORDER)


def test_order_with_a_valid_slippage_passes():
    assert reasons() == ("PASS", [])


NEEDS = "order needs a finite, non-negative slippage allowance"


def test_order_without_a_slippage_field_is_blocked():
    no_slip = {k: v for k, v in ORDER.items() if k != "slippage"}
    assert reasons(no_slip) == ("BLOCK", [NEEDS])


@pytest.mark.parametrize(
    "bad",
    [None, True, False, "abc", "", "NaN", "sNaN", "Infinity", "-Infinity", "-0.01", -1,
     D("-0.01"), float("nan"), float("inf"), [0.37], {"a": 1}],
    ids=repr,
)
def test_missing_invalid_non_finite_or_negative_slippage_is_blocked(bad):
    assert reasons({**ORDER, "slippage": bad}) == ("BLOCK", [NEEDS])


@pytest.mark.parametrize("ok", ["0.37", "0.370", D("0.37"), "3.7e-1"], ids=repr)
def test_slippage_given_as_string_or_decimal_at_the_minimum_passes(ok):
    assert reasons({**ORDER, "slippage": ok}) == ("PASS", [])


@pytest.mark.parametrize("big", [1, "5"], ids=repr)
def test_a_large_slippage_is_a_valid_number_but_blocks_on_the_loss(big):
    # 8 x (5.86 + 1) = 54.88 and 8 x 10.86 = 86.88 are over the 50.00 per-trade limit
    verdict, rs = reasons({**ORDER, "slippage": big})
    assert verdict == "BLOCK"
    assert any("exceeds max loss per trade" in x for x in rs)
    assert not any("below minimum" in x or "slippage allowance" in x for x in rs)


def test_slippage_exactly_the_minimum_passes_and_one_cent_below_blocks():
    assert D(minimum_allowance(D("364.18"), LIMITS)) == D("0.37")
    assert reasons({**ORDER, "slippage": "0.37"}) == ("PASS", [])
    assert reasons({**ORDER, "slippage": "0.36"}) == (
        "BLOCK", ["slippage allowance 0.36 below minimum 0.37"])


def test_zero_slippage_is_blocked_while_the_floor_is_positive():
    assert reasons({**ORDER, "slippage": "0"}) == (
        "BLOCK", ["slippage allowance 0 below minimum 0.37"])
    lim = NO_PCT  # slippage_pct 0: the 0.01 floor still applies
    base = size_position(Plan("TSLA", D("364.18"), D("358.32"), D("370.04")), lim,
                         slippage=D("0.01"))["order"]
    assert reasons(base, lim) == ("PASS", [])
    assert reasons({**base, "slippage": "0"}, lim) == (
        "BLOCK", ["slippage allowance 0 below minimum 0.01"])


def test_minimum_follows_the_configured_percent():
    lim = replace(LIMITS, slippage_pct=D("0.5"))  # 364.18 x 0.5% = 1.8209 -> 1.83
    v, rs = reasons(ORDER, lim)
    assert v == "BLOCK" and "slippage allowance 0.37 below minimum 1.83" in rs


def test_loss_at_stop_includes_slippage():
    # NO_PCT: qty 8 x (5.86 + slippage); 0.39 -> 50.00 is exactly the limit
    def mk(slippage):
        return {**ORDER, "slippage": slippage}
    assert reasons(mk("0.01"), NO_PCT) == ("PASS", [])  # 8 x 5.87 = 46.96
    assert reasons(mk("0.39"), NO_PCT) == ("PASS", [])  # 8 x 6.25 = 50.00: boundary passes
    v, rs = reasons(mk("0.40"), NO_PCT)  # 8 x 6.26 = 50.08
    assert v == "BLOCK"
    assert rs[0] == "loss at stop 50.08 exceeds max loss per trade 50.00"
    # the planned loss alone (8 x 5.86 = 46.88) would have passed: slippage is what blocks it
    assert 8 * D("5.86") <= LIMITS.max_loss_per_trade < 8 * D("6.26")


def test_daily_loss_limit_counts_slippage():
    # NO_PCT, state with 103.04 used: slippage 0.01 -> 46.96 -> exactly 150.00 (pass);
    # slippage 0.39 -> 50.00 -> 153.04 (BLOCK on the daily limit only)
    used = DayState(trades_today=1, risk_used_today=D("103.04"))
    assert reasons({**ORDER, "slippage": "0.01"}, NO_PCT, used) == ("PASS", [])
    v, rs = reasons({**ORDER, "slippage": "0.39"}, NO_PCT, used)
    assert v == "BLOCK" and len(rs) == 1
    assert rs[0] == "today's risk would reach 153.04, over the daily limit 150.00"


def test_position_value_counts_slippage():
    # (364.18 + 0.37) x 8 = 2916.40
    at_cap = replace(LIMITS, max_position_value=D("2916.40"))
    assert reasons(ORDER, at_cap) == ("PASS", [])
    just_under = replace(LIMITS, max_position_value=D("2916.39"))
    v, rs = reasons(ORDER, just_under)
    assert v == "BLOCK" and rs == ["position value 2916.40 over cap 2916.39"]
    assert 364.18 * 8 < 2916.39  # the planned entry alone would have fit


# ---- PIN: reward:risk, the reward cap and min risk are on the PLANNED prices ----

def one_r(slippage: str, target="101.00", stop="99.00", entry="100.00", qty=10) -> dict:
    return {"symbol": "AAPL", "side": "buy", "qty": qty, "type": "market",
            "order_class": "bracket", "entry": entry, "stop_loss": stop, "take_profit": target,
            "slippage": slippage}


@pytest.mark.parametrize("slippage", ["0.10", "0.25", "0.50", "1.00"])
def test_a_1_00r_plan_with_positive_slippage_still_passes_the_min_reward_to_risk(slippage):
    order = one_r(slippage)  # target - entry == entry - stop == 1.00
    r = check(order)
    assert not any("reward:risk" in x for x in r["reasons"]), r
    assert r == {"verdict": "PASS", "reasons": []}
    assert LIMITS.min_reward_to_risk == D("1.0")


def test_reward_to_risk_is_judged_without_slippage_both_ways():
    # passes at exactly 1.00R; blocks at 0.99R no matter the slippage
    assert check(one_r("0.10"))["verdict"] == "PASS"
    r = check(one_r("0.10", target="100.99"))
    assert r["verdict"] == "BLOCK"
    assert r["reasons"] == ["reward:risk 0.99 below minimum 1.0"]
    # a bigger allowance does not change that number
    assert check(one_r("0.50", target="100.99"))["reasons"] == [
        "reward:risk 0.99 below minimum 1.0"]


def test_reward_cap_and_min_risk_use_planned_prices_not_slippage():
    cap5 = one_r("0.50", target="105.00")  # exactly 5R on the planned prices
    assert check(cap5) == {"verdict": "PASS", "reasons": []}
    over = check(one_r("0.50", target="105.02"))
    assert over["verdict"] == "BLOCK" and "over cap 5" in over["reasons"][0]
    # risk exactly at the 0.10 minimum passes however big the allowance; 0.09 blocks
    assert check(one_r("0.10", stop="99.90", target="100.10"))["verdict"] == "PASS"
    assert check(one_r("0.10", stop="99.91", target="100.09"))["reasons"] == [
        "risk per share 0.09 below minimum 0.10"]


def test_with_slippage_helper_adds_the_minimum_allowance():
    order = {k: v for k, v in one_r("0").items() if k != "slippage"}
    fixed = with_slippage(order, LIMITS)
    assert fixed["slippage"] == "0.10" and check(fixed)["verdict"] == "PASS"
    assert with_slippage(order, LIMITS, "0.50")["slippage"] == "0.50"


# ======================= order flow =======================

@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "decisions.jsonl")


@pytest.fixture
def ledger():
    return sealed_ledger()


def yes(_prompt: str) -> str:
    return "yes"


def at(t: datetime):
    return lambda: t


def run(ledger, journal, quotes, t: datetime = NOW):
    return propose(ledger, "plan-1", journal=journal, clock=at(t), quotes=quotes,
        events=events_clear())


def test_propose_without_a_quote_source_is_refused_at_quote(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=at(NOW), events=events_clear())
    assert (p.ok, p.stage, p.reason) == (False, "quote", "no live quote source")
    assert p.order is None and p.plan is not None


def test_propose_with_a_failing_quote_source_is_refused_at_quote_and_never_raises(
        ledger, journal):
    def boom(symbol):
        raise ConnectionError("down")
    p = run(ledger, journal, boom)
    assert (p.ok, p.stage, p.reason) == (False, "quote", "live quote unavailable (ConnectionError)")
    p = run(ledger, journal, QuoteSource(lambda s: 1 / 0))
    assert p.stage == "quote" and p.reason == "live quote unavailable (ZeroDivisionError)"


@pytest.mark.parametrize(
    ("q", "reason"),
    [
        (None, "no live quote"),
        ({}, NO_BID_ASK),
        (quote("364.16", "364.18", age_s=16), "live quote is 16s old (max 15s)"),
        (quote("364.19", "364.18"), "live quote is crossed (bid 364.19 > ask 364.18)"),
        (quote("364.55", "364.56"),
         "price moved: ask 364.56 is above entry 364.18 + slippage allowance 0.37"),
        ({"ap": 364.18, "bp": 364.16, "t": "junk"}, NO_TS),
    ],
    ids=["none", "empty", "stale", "crossed", "moved", "bad-t"],
)
def test_unusable_quote_is_refused_at_size_with_the_reason(ledger, journal, q, reason):
    p = run(ledger, journal, QuoteSource(q))
    assert p.ok is False and p.stage == "size" and p.reason == reason
    assert p.order is None and p.verdict is None


def test_quote_at_the_boundaries_is_accepted(ledger, journal):
    assert run(ledger, journal, QuoteSource(quote("364.16", "364.18", age_s=15))).ok
    assert run(ledger, journal, QuoteSource(quote("364.54", "364.55"))).ok  # ask = entry + 0.37
    assert run(ledger, journal, QuoteSource(quote("364.16", "364.18", age_s=-5))).ok


def test_happy_path_sizes_with_the_allowance_and_reads_the_plan_symbol(ledger, journal):
    src = quotes_at(NOW, spread="0.05")
    p = run(ledger, journal, src)
    assert p.ok and p.stage is None
    assert src.calls == 1 and src.symbols == ["TSLA"]
    assert p.sized["slippage"] == "0.37" and p.sized["max_loss"] == "49.84"
    assert p.order["qty"] == 8 and p.order["slippage"] == "0.37"
    assert p.sized["quote"]["spread"] == "0.05" and p.sized["quote"]["bid"] == "364.13"
    assert p.verdict == {"verdict": "PASS", "reasons": []}


def test_quote_is_read_after_the_clock_and_plan_refusals_never_read_it(ledger, journal):
    src = quotes_at(NOW)
    assert propose(ledger, "plan-9", journal=journal, clock=at(NOW), quotes=src,
        events=events_clear()).stage == "plan"
    assert src.calls == 0  # an unknown plan never triggers a quote read
    old = NOW + timedelta(seconds=400)
    p = propose(ledger, "plan-1", journal=journal, clock=at(old), quotes=quotes_at(old),
        events=events_clear())
    assert p.stage == "size" and "plan is" in p.reason  # plan age is judged before the quote


def test_ticket_names_the_allowance_the_live_ask_and_the_worst_case_loss(ledger, journal):
    t = render_ticket(run(ledger, journal, quotes_at(NOW, spread="0.05")))
    assert "slippage allowance 0.37 per share" in t
    assert "(live ask 364.18)" in t
    assert "Worst-case loss at the stop 49.84" in t
    assert "Buy 8 shares of TSLA" in t


def test_approve_reads_the_quote_again_after_yes(ledger, journal):
    src = quotes_at(NOW)
    p = run(ledger, journal, src)
    assert src.calls == 1  # propose
    out = approve(p, ledger, journal=journal, ask=yes, clock=at(NOW), quotes=src,
        events=events_clear())
    assert out.status == APPROVED and src.calls == 2  # exactly one re-read for the re-check
    assert src.symbols == ["TSLA", "TSLA"]


def test_a_decline_does_not_read_the_quote_again(ledger, journal):
    src = quotes_at(NOW)
    p = run(ledger, journal, src)
    out = approve(p, ledger, journal=journal, ask=lambda _: "no", clock=at(NOW), quotes=src,
        events=events_clear())
    assert out.status == "declined" and src.calls == 1


def test_approve_without_a_quote_source_refuses_after_approval(ledger, journal):
    p = run(ledger, journal, quotes_at(NOW))
    out = approve(p, ledger, journal=journal, ask=yes, clock=at(NOW),
        events=events_clear())  # no quotes=
    assert out.status == "refused_after_approval"
    assert out.recheck.stage == "quote" and "no live quote source" in out.reason
    assert [e["status"] for e in raw_lines(journal)] == ["refused_after_approval"]


def test_quote_failing_at_the_recheck_refuses_after_approval(ledger, journal):
    state = {"n": 0}

    def flaky(symbol):
        state["n"] += 1
        if state["n"] > 1:
            raise TimeoutError
        return fresh_quote("364.18", NOW)
    p = run(ledger, journal, flaky)
    out = approve(p, ledger, journal=journal, ask=yes, clock=at(NOW), quotes=flaky,
        events=events_clear())
    assert out.status == "refused_after_approval" and out.recheck.stage == "quote"
    assert "live quote unavailable (TimeoutError)" in out.reason


def test_price_moving_past_the_allowance_between_ticket_and_yes_refuses(ledger, journal):
    box = {"q": fresh_quote("364.18", NOW)}
    src = QuoteSource(lambda s: box["q"])
    p = run(ledger, journal, src)
    assert p.ok

    def move_then_yes(_):
        box["q"] = quote("364.55", "364.56")  # one cent past entry + 0.37
        return "yes"

    out = approve(p, ledger, journal=journal, ask=move_then_yes, clock=at(NOW), quotes=src,
        events=events_clear())
    assert out.status == "refused_after_approval"
    assert out.recheck.stage == "size"
    assert "price moved: ask 364.56 is above entry 364.18 + slippage allowance 0.37" in out.reason
    assert [e["status"] for e in raw_lines(journal)] == ["refused_after_approval"]
    assert journal.day_state(NOW).trades_today == 0


def test_quote_going_stale_between_ticket_and_yes_refuses(ledger, journal):
    box = {"q": fresh_quote("364.18", NOW)}
    src = QuoteSource(lambda s: box["q"])
    p = run(ledger, journal, src)

    def freeze_then_yes(_):
        box["q"] = fresh_quote("364.18", NOW, age_s=16)  # the feed stopped
        return "yes"

    out = approve(p, ledger, journal=journal, ask=freeze_then_yes, clock=at(NOW), quotes=src,
        events=events_clear())
    assert out.status == "refused_after_approval"
    assert "live quote is 16s old (max 15s)" in out.reason


def test_wider_spread_at_the_recheck_changes_allowance_and_shares(ledger, journal):
    box = {"q": fresh_quote("364.18", NOW)}  # spread 0.02 -> allowance 0.37 -> 8 shares
    src = QuoteSource(lambda s: box["q"])
    p = run(ledger, journal, src)
    assert p.order["qty"] == 8 and p.sized["slippage"] == "0.37"

    def widen_then_yes(_):
        box["q"] = fresh_quote("364.18", NOW, spread="0.60")  # allowance 0.60 -> 7 shares
        return "yes"

    out = approve(p, ledger, journal=journal, ask=widen_then_yes, clock=at(NOW), quotes=src,
        events=events_clear())
    assert out.status == "changed_after_approval"
    assert out.recheck.ok and out.recheck.sized["slippage"] == "0.60"
    assert out.recheck.order["qty"] == 7  # 50 / (5.86 + 0.60) = 7.7
    assert out.reason == "the re-built order differs from the approved ticket"
    assert [e["status"] for e in raw_lines(journal)] == ["changed_after_approval"]
    assert journal.day_state(NOW).trades_today == 0


def test_tighter_spread_cannot_lower_the_allowance_below_the_pct_floor(ledger, journal):
    box = {"q": fresh_quote("364.18", NOW, spread="0.05")}
    src = QuoteSource(lambda s: box["q"])
    p = run(ledger, journal, src)

    def tighten_then_yes(_):
        box["q"] = fresh_quote("364.18", NOW, spread="0.01")
        return "yes"

    out = approve(p, ledger, journal=journal, ask=tighten_then_yes, clock=at(NOW), quotes=src,
        events=events_clear())
    assert out.status == APPROVED  # allowance is 0.37 either way (0.10% of the entry)


def test_time_and_bid_changes_within_the_same_allowance_are_still_approved(ledger, journal):
    clock = StepClock(NOW, NOW + timedelta(seconds=12))
    box = {"q": fresh_quote("364.18", NOW)}
    src = QuoteSource(lambda s: box["q"])
    p = propose(ledger, "plan-1", journal=journal, clock=clock, quotes=src, events=events_clear())
    first = p.sized["quote"]

    def move_then_yes(_):
        # 12 s later; bid 3 cents lower (spread 0.05 <= 0.37 allowance); ask unchanged
        box["q"] = fresh_quote("364.18", NOW + timedelta(seconds=12), spread="0.05", age_s=3)
        return "yes"

    out = approve(p, ledger, journal=journal, ask=move_then_yes, clock=clock, quotes=src,
        events=events_clear())
    assert out.status == APPROVED, out.reason
    assert out.recheck.sized["quote"]["bid"] == "364.13" != first["bid"]
    assert out.recheck.sized["quote"]["quote_t"] != first["quote_t"]
    assert out.recheck.sized["slippage"] == p.sized["slippage"] == "0.37"
    assert out.recheck.order == p.order
    (entry,) = raw_lines(journal)
    assert entry["status"] == APPROVED
    assert entry["quote"] == out.recheck.sized["quote"]  # the re-check's quote is journaled


def test_journal_entries_carry_the_quote(ledger, journal):
    src = quotes_at(NOW)
    p = run(ledger, journal, src)
    approve(p, ledger, journal=journal, ask=lambda _: "no", clock=at(NOW), quotes=src,
        events=events_clear())
    approve(run(ledger, journal, src), ledger, journal=journal, ask=yes, clock=at(NOW), quotes=src,
        events=events_clear())
    declined, approved = raw_lines(journal)
    assert declined["status"] == "declined" and approved["status"] == APPROVED
    for e in (declined, approved):
        assert set(e["quote"]) == {"bid", "ask", "spread", "quote_t", "age_s"}
        assert e["quote"]["ask"] == "364.18"
        assert e["order"]["slippage"] == "0.37"
    # a refused proposal has no order and no quote
    bad = run(ledger, journal, QuoteSource(None))
    approve(bad, ledger, journal=journal, ask=yes, clock=at(NOW), quotes=src, events=events_clear())
    last = raw_lines(journal)[-1]
    assert last["status"] == "no_order" and last["quote"] is None and last["order"] is None


def test_approved_entry_counts_the_worst_case_loss_toward_the_day(ledger, journal):
    src = quotes_at(NOW, spread="0.05")
    out = approve(run(ledger, journal, src), ledger, journal=journal, ask=yes,
                  clock=at(NOW), quotes=src, events=events_clear())
    assert out.status == APPROVED
    assert journal.day_state(NOW).risk_used_today == D("49.84")  # not the planned 46.88


# ======================= decide and smoke wiring =======================

def go(tmp_path, data, clock=None):
    clock = clock or StepClock(NOON, NOON + timedelta(seconds=20), NOON + timedelta(seconds=40))
    journal = Journal(tmp_path / "j.jsonl")
    result = run_decision(SYMBOL, data=data, clients=Router(scripts()), journal=journal,
                          clock=clock, ask=yes, rounds=1, timeout_s=10)
    return result, journal


def test_run_decision_reads_the_quote_from_data_for_propose_and_again_for_approve(tmp_path):
    clock = StepClock(NOON, NOON + timedelta(seconds=20), NOON + timedelta(seconds=40))
    data = QuotingData(FakeAllData(), clock, SYMBOL, "103.10")
    result, journal = go(tmp_path, data, clock)
    assert result.outcome.status == APPROVED
    assert data.quote_reads == 2  # once for the ticket, once for the re-check
    (entry,) = raw_lines(journal)
    # 0.10% of 103.10 = 0.1031, rounded up to the cent, beats the 2-cent spread
    assert D(entry["quote"]["ask"]) == D("103.10") and entry["order"]["slippage"] == "0.11"
    assert result.proposal.sized["slippage"] == "0.11"
    assert "live ask 103.1" in result.ticket


def test_run_decision_without_a_quote_in_the_snapshots_gets_no_order(tmp_path):
    result, journal = go(tmp_path, FakeAllData())  # the fixture has no ABCD snapshot
    assert result.proposal.ok is False and result.proposal.stage == "size"
    assert result.proposal.reason == "no live quote"
    assert result.outcome.status == "no_order"
    assert APPROVED not in [e["status"] for e in raw_lines(journal)]


def test_run_decision_with_a_stale_live_quote_gets_no_order(tmp_path):
    clock = StepClock(NOON, NOON + timedelta(seconds=20), NOON + timedelta(seconds=40))
    data = QuotingData(FakeAllData(), clock, SYMBOL, "103.10", age_s=16)
    result, _ = go(tmp_path, data, clock)
    assert result.proposal.stage == "size"
    assert result.proposal.reason == "live quote is 16s old (max 15s)"


def test_quotes_from_reads_latest_quote_of_the_symbol():
    class Data:
        def __init__(self, snaps):
            self.snaps, self.asked = snaps, []

        def snapshots(self, symbols):
            self.asked.append(list(symbols))
            return {s: self.snaps.get(s) for s in symbols}

    q = {"ap": 1.0, "bp": 0.9, "t": "x"}
    d = Data({"AAA": {"latestQuote": q, "latestTrade": {"p": 5}}, "BBB": None, "CCC": {}})
    read = quotes_from(d)
    assert read("AAA") == q and d.asked == [["AAA"]]
    assert read("BBB") is None and read("CCC") is None and read("ZZZ") is None


def test_smoke_fixture_quotes_restamp_the_saved_quote_at_the_run_clock():
    data = ScenarioData(load_scenario("tsla"))
    now = data.as_of()
    saved = data.snapshots(["TSLA"])["TSLA"]["latestQuote"]
    assert saved["t"] == "2026-11-03T15:59:00Z"
    got = S.fixture_quotes(data, now)("TSLA")
    assert got["t"] == now.isoformat()
    assert (got["ap"], got["bp"]) == (saved["ap"], saved["bp"])  # prices untouched
    assert data.snapshots(["TSLA"])["TSLA"]["latestQuote"]["t"] == "2026-11-03T15:59:00Z"
    # without the re-stamp the saved quote is past the 15 s limit at the fixture clock
    stale = slippage_from_quote(D("248.65"), saved, LIMITS, now)
    assert stale["ok"] is False and "old (max 15s)" in stale["reason"]
    fresh = slippage_from_quote(D("248.65"), got, LIMITS, now)
    assert fresh["ok"] is True and fresh["age_s"] == 0


def test_smoke_fixture_quotes_passes_a_missing_quote_through():
    class Data:
        def snapshots(self, symbols):
            return {symbols[0]: None}
    assert S.fixture_quotes(Data(), NOW)("TSLA") is None


# ======================= config =======================

def cfg(**over) -> dict:
    d = copy.deepcopy(EXAMPLE)
    d.update(over)
    return d


def test_dataclass_defaults():
    f = Limits.__dataclass_fields__
    assert f["slippage_pct"].default == D("0.10")
    assert f["max_order_quote_age_s"].default == 15


def test_example_json_ships_both_values():
    assert EXAMPLE["slippage_pct"] == "0.10" and EXAMPLE["max_order_quote_age_s"] == 15
    assert LIMITS.slippage_pct == D("0.10") and LIMITS.max_order_quote_age_s == 15
    assert LIMITS.paper is True


def test_missing_keys_use_the_defaults():
    d = cfg()
    del d["slippage_pct"], d["max_order_quote_age_s"]
    lim = limits_from_dict(d)
    assert lim.slippage_pct == D("0.10") and lim.max_order_quote_age_s == 15


@pytest.mark.parametrize("bad", ["-0.01", "-1", "100", "100.00", "250", "NaN", "Infinity",
                                 "-Infinity", "x", "", None, "0,5"], ids=repr)
def test_invalid_slippage_pct_is_rejected(bad):
    with pytest.raises(ValueError, match="slippage_pct"):
        limits_from_dict(cfg(slippage_pct=bad))


@pytest.mark.parametrize("ok", ["0", "0.00", "0.25", "0.10", "1", "99.99", 0.5, 1], ids=repr)
def test_valid_slippage_pct_is_accepted(ok):
    assert limits_from_dict(cfg(slippage_pct=ok)).slippage_pct == D(str(ok))


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "1.5", "x", "", "-5", None, [15]],
                         ids=repr)
def test_invalid_max_order_quote_age_is_rejected(bad):
    with pytest.raises(ValueError, match="max_order_quote_age_s"):
        limits_from_dict(cfg(max_order_quote_age_s=bad))


@pytest.mark.parametrize(("ok", "val"), [(1, 1), (15, 15), (30, 30), ("30", 30), (30.0, 30)],
                         ids=repr)
def test_valid_max_order_quote_age_is_accepted(ok, val):
    assert limits_from_dict(cfg(max_order_quote_age_s=ok)).max_order_quote_age_s == val


@pytest.mark.parametrize(
    "bad",
    [D("-0.01"), D("100"), D("NaN"), D("Infinity"), 0.1, "0.10", None],
    ids=repr,
)
def test_hand_built_limits_with_a_bad_slippage_pct_fail_validation_and_block_every_order(bad):
    lim = replace(LIMITS, slippage_pct=bad)
    with pytest.raises(ValueError, match="slippage_pct"):
        validate_limits(lim)
    r = check_order(ORDER, lim, DayState(), MON_1015, gate=gate_for_order(ORDER, MON_1015))
    assert r["verdict"] == "BLOCK" and r["reasons"][0].startswith("limits invalid:")


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "15", None])
def test_hand_built_limits_with_a_bad_quote_age_fail_validation_and_block(bad):
    lim = replace(LIMITS, max_order_quote_age_s=bad)
    with pytest.raises(ValueError, match="max_order_quote_age_s"):
        validate_limits(lim)
    assert check_order(ORDER, lim, DayState(), MON_1015,
        gate=gate_for_order(ORDER, MON_1015))["verdict"] == "BLOCK"


def test_valid_boundary_values_pass_validation():
    validate_limits(replace(LIMITS, slippage_pct=D("0")))
    validate_limits(replace(LIMITS, slippage_pct=D("99.99")))
    validate_limits(replace(LIMITS, max_order_quote_age_s=1))
    assert math.isfinite(float(LIMITS.slippage_pct))


# ============================ code-review additions ============================

def test_absurd_quote_is_a_refusal_not_an_exception():
    """A 1e26 spread overflows the cent quantize: refused, never raised (fail closed)."""
    q = {"ap": 1e26, "bp": 1, "t": T0.isoformat()}
    res = slippage_from_quote(D("100.00"), q, LIMITS, T0)
    assert res == {"ok": False, "reason": "live quote values are out of range"}


def test_absurd_quote_through_propose_and_approve_never_raises(tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    bad = lambda sym: {"ap": 1e26, "bp": 1, "t": T0.isoformat()}  # noqa: E731
    p = propose(sealed_ledger(), "plan-1", journal=journal, clock=lambda: T0, quotes=bad,
        events=events_clear())
    assert p.ok is False and p.stage == "size" and "out of range" in p.reason
    good = propose(sealed_ledger(), "plan-1", journal=journal, clock=lambda: T0,
                   quotes=quotes_at(T0), events=events_clear())
    assert good.ok, good.reason
    led = sealed_ledger()
    good = propose(led, "plan-1", journal=journal, clock=lambda: T0, quotes=quotes_at(T0),
        events=events_clear())
    out = approve(good, led, journal=journal, ask=lambda _: "yes", clock=lambda: T0,
                  quotes=bad, events=events_clear())
    assert out.status == "refused_after_approval" and "out of range" in out.reason
    assert APPROVED not in [e["status"] for e in journal.entries()]


def test_risk_check_huge_entry_blocks_instead_of_raising():
    """minimum_allowance on an absurd entry overflows: BLOCK, not an exception."""
    order = {"symbol": "TSLA", "side": "buy", "qty": 1, "type": "market",
             "order_class": "bracket", "entry": "1e30", "stop_loss": "1",
             "take_profit": "2e30", "slippage": "0.01"}
    res = check_order(order, LIMITS, DayState(), MON_1015, gate=gate_for_order(order, MON_1015))
    assert res["verdict"] == "BLOCK" and "order arithmetic failed" in res["reasons"]


@pytest.mark.parametrize("t", [
    "2026-10-19T14:15:29.123456789Z",       # Alpaca's nanosecond form, 0.9 s old
    "2026-10-19T14:15:29.999999999+00:00",
])
def test_alpaca_nanosecond_quote_timestamps_parse(t):
    now = datetime(2026, 10, 19, 14, 15, 30, tzinfo=UTC)
    res = slippage_from_quote(D("364.18"), {"ap": 364.18, "bp": 364.16, "t": t}, LIMITS, now)
    assert res["ok"] is True, res
    assert res["ask"] == "364.18" and res["bid"] == "364.16"  # whole cents, two decimals
