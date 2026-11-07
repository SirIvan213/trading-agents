"""Tests for tools.config validation (issues #4 and #5). Dicts come from the shipped
example file, copied and mutated in memory; nothing is written under config/."""
import copy
import json
from datetime import datetime, time
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from tools.config import CONFIG_DIR, limits_from_dict, load_limits
from tools.risk_check import DayState, check_order
from tools.size import Plan, size_position

EXAMPLE = json.loads((CONFIG_DIR / "limits.example.json").read_text())
NY = ZoneInfo("America/New_York")
MON_1015 = datetime(2026, 10, 19, 10, 15, tzinfo=NY)

MONEY_FIELDS = [
    "account_cash",
    "max_loss_per_trade",
    "max_position_value",
    "daily_loss_limit",
    "min_reward_to_risk",
    "max_reward_multiple",
]


def cfg(**overrides):
    d = copy.deepcopy(EXAMPLE)
    d.update(overrides)
    return d


def rejects(d, *names):
    with pytest.raises(ValueError) as ei:
        limits_from_dict(d)
    for n in names:
        assert n in str(ei.value), (n, str(ei.value))
    return str(ei.value)


# ---------- the example config ----------

def test_example_config_loads_with_expected_values():
    lim = load_limits(CONFIG_DIR / "limits.example.json")
    assert lim.paper is True
    assert lim.account_cash == D("5000.00")
    assert lim.cash_buffer_pct == D("10")
    assert lim.max_loss_per_trade == D("50.00")
    assert lim.max_position_value == D("3000.00")
    assert lim.daily_loss_limit == D("150.00")
    assert lim.max_trades_per_day == 3
    assert lim.min_reward_to_risk == D("1.0")
    assert lim.max_reward_multiple == D("5")
    assert (lim.entry_start, lim.entry_end, lim.flat_by) == (
        time(9, 45), time(15, 30), time(15, 55))
    assert lim.blocked_symbols == frozenset()


def test_max_reward_multiple_defaults_to_five_when_absent():
    d = cfg()
    del d["max_reward_multiple"]
    assert limits_from_dict(d).max_reward_multiple == D("5")


# ---------- money and ratio fields ----------

@pytest.mark.parametrize("field", MONEY_FIELDS)
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "0", "0.00", "-1", "-0.01", 0, -5])
def test_money_field_rejects_nan_inf_zero_negative(field, bad):
    rejects(cfg(**{field: bad}), field)


@pytest.mark.parametrize("field", MONEY_FIELDS)
@pytest.mark.parametrize("bad", ["abc", "", None, "1,000"])
def test_money_field_rejects_unparseable_naming_field(field, bad):
    rejects(cfg(**{field: bad}), field)


@pytest.mark.parametrize("field", MONEY_FIELDS)
def test_money_field_accepts_small_positive(field):
    d = cfg(**{field: "0.01"})
    if field == "max_reward_multiple":
        d["min_reward_to_risk"] = "0.01"
        d["good_setup_multiple"] = "0.01"
    assert limits_from_dict(d)


def test_floats_in_json_are_accepted_as_decimals():
    assert limits_from_dict(cfg(account_cash=5000.5)).account_cash == D("5000.5")


# ---------- cash_buffer_pct ----------

def test_cash_buffer_zero_passes():
    assert limits_from_dict(cfg(cash_buffer_pct="0")).cash_buffer_pct == D("0")


def test_cash_buffer_just_under_100_passes():
    assert limits_from_dict(cfg(cash_buffer_pct="99.99")).cash_buffer_pct == D("99.99")


@pytest.mark.parametrize("bad", ["100", "100.01", "-1", "-0.01", "NaN", "Infinity", "abc"])
def test_cash_buffer_rejected(bad):
    rejects(cfg(cash_buffer_pct=bad), "cash_buffer_pct")


# ---------- max_trades_per_day ----------

@pytest.mark.parametrize("good,expected", [(3, 3), (3.0, 3), ("3", 3), (" 3 ", 3), (1, 1)])
def test_max_trades_accepts_whole_numbers(good, expected):
    lim = limits_from_dict(cfg(max_trades_per_day=good))
    assert lim.max_trades_per_day == expected
    assert isinstance(lim.max_trades_per_day, int)


@pytest.mark.parametrize("bad", [0, True, False, 3.7, 3.5, -1, "3.7", "abc", "", None, "-1", [3]])
def test_max_trades_rejected(bad):
    rejects(cfg(max_trades_per_day=bad), "max_trades_per_day")


def test_max_trades_float_nan_and_inf_rejected():
    rejects(cfg(max_trades_per_day=float("nan")), "max_trades_per_day")
    rejects(cfg(max_trades_per_day=float("inf")), "max_trades_per_day")


# ---------- reward cap vs floor ----------

def test_max_reward_below_min_reward_rejected():
    msg = rejects(cfg(min_reward_to_risk="2", max_reward_multiple="1.5"), "max_reward_multiple")
    assert "min_reward_to_risk" in msg


def test_max_reward_equal_to_min_reward_passes():
    lim = limits_from_dict(cfg(min_reward_to_risk="2", max_reward_multiple="2"))
    assert lim.min_reward_to_risk == lim.max_reward_multiple == D("2")


def test_max_reward_just_below_min_rejected():
    rejects(cfg(min_reward_to_risk="2.00", max_reward_multiple="1.99"), "max_reward_multiple")


# ---------- entry window ----------

def test_entry_window_start_after_end_rejected():
    rejects(cfg(entry_window=["15:00", "10:00"]), "entry_window")


def test_entry_window_start_equal_end_passes():
    lim = limits_from_dict(cfg(entry_window=["10:00", "10:00"]))
    assert lim.entry_start == lim.entry_end == time(10, 0)


def test_entry_window_start_at_open_passes_and_before_open_rejected():
    assert limits_from_dict(cfg(entry_window=["09:30", "15:30"])).entry_start == time(9, 30)
    rejects(cfg(entry_window=["09:29", "15:30"]), "entry_window start")


def test_entry_window_end_at_close_passes_with_flat_at_close():
    lim = limits_from_dict(cfg(entry_window=["09:45", "16:00"], flat_by="16:00"))
    assert lim.entry_end == time(16, 0)


def test_entry_window_end_after_close_rejected():
    rejects(cfg(entry_window=["09:45", "16:01"], flat_by="16:00"), "entry_window end")


@pytest.mark.parametrize("window", [None, [], ["10:00"], ["09:45", "15:30", "16:00"], 5, "09:45"])
def test_entry_window_wrong_shape_rejected(window):
    rejects(cfg(entry_window=window), "entry_window")


# ---------- flat_by ----------

@pytest.mark.parametrize("good", ["15:55", "16:00"])
def test_flat_by_inside_hours_passes(good):
    assert limits_from_dict(cfg(flat_by=good)).flat_by == time(*map(int, good.split(":")))


@pytest.mark.parametrize("bad", ["09:29", "16:01", "00:00", "23:59"])
def test_flat_by_outside_hours_rejected(bad):
    rejects(cfg(flat_by=bad), "flat_by")


def test_entry_end_after_flat_by_rejected():
    msg = rejects(cfg(entry_window=["09:45", "15:50"], flat_by="15:45"), "flat_by")
    assert "entry_window end" in msg


def test_entry_end_equal_flat_by_passes():
    lim = limits_from_dict(cfg(entry_window=["09:45", "15:45"], flat_by="15:45"))
    assert lim.entry_end == lim.flat_by == time(15, 45)


# ---------- aggregated and unparseable ----------

def test_multiple_problems_reported_together():
    msg = rejects(
        cfg(account_cash="0", max_loss_per_trade="-1", flat_by="16:01", cash_buffer_pct="100"),
        "account_cash", "max_loss_per_trade", "flat_by", "cash_buffer_pct",
    )
    assert msg.count(";") >= 3  # one error, several problems


@pytest.mark.parametrize(
    "d,name",
    [
        (cfg(flat_by="abc"), "flat_by"),
        (cfg(flat_by="9am"), "flat_by"),
        (cfg(flat_by="25:00"), "flat_by"),
        (cfg(flat_by=None), "flat_by"),
        (cfg(flat_by=1555), "flat_by"),
        (cfg(entry_window=["abc", "15:30"]), "entry_window start"),
        (cfg(entry_window=["09:45", "9am"]), "entry_window end"),
        (cfg(entry_window=["09:45", "15:61"]), "entry_window end"),
    ],
)
def test_unparseable_times_name_the_field(d, name):
    rejects(d, name)


def test_validation_does_not_relax_paper_flag():
    assert limits_from_dict(cfg()).paper is True


# ---------- blocked_symbols (issue #5) ----------

def test_blocked_symbols_stripped_uppercased_empty_dropped():
    lim = limits_from_dict(cfg(blocked_symbols=[" gme ", "", "   ", "tsla", "\tamc\n"]))
    assert lim.blocked_symbols == frozenset({"GME", "TSLA", "AMC"})


def test_blocked_symbols_duplicates_collapse():
    lim = limits_from_dict(cfg(blocked_symbols=["gme", "GME", " Gme "]))
    assert lim.blocked_symbols == frozenset({"GME"})


def test_blocked_symbols_missing_key_and_empty_list():
    d = cfg()
    del d["blocked_symbols"]
    assert limits_from_dict(d).blocked_symbols == frozenset()
    assert limits_from_dict(cfg(blocked_symbols=[])).blocked_symbols == frozenset()
    assert limits_from_dict(cfg(blocked_symbols=["", " "])).blocked_symbols == frozenset()


@pytest.mark.parametrize("bad", ["GME", None, {"GME": 1}, 5, {"GME"}])
def test_blocked_symbols_non_list_rejected(bad):
    rejects(cfg(blocked_symbols=bad), "blocked_symbols")


@pytest.mark.parametrize("bad", [[5], [None], [["GME"]], ["GME", 7], [b"GME"]])
def test_blocked_symbols_non_string_entry_rejected(bad):
    rejects(cfg(blocked_symbols=bad), "blocked_symbols")


def order_for(symbol, limits):
    plan = Plan(symbol, D("20.00"), D("19.00"), D("21.00"))
    sized = size_position(plan, limits)
    assert sized["ok"], sized
    return sized["order"]


def test_padded_lowercase_entry_blocks_gme_but_not_others():
    lim = limits_from_dict(cfg(blocked_symbols=[" gme "]))
    blocked = check_order(order_for("GME", lim), lim, DayState(), MON_1015)
    assert blocked == {"verdict": "BLOCK", "reasons": ["GME is on the blocked list"]}
    other = check_order(order_for("AAPL", lim), lim, DayState(), MON_1015)
    assert other == {"verdict": "PASS", "reasons": []}


def test_blocked_symbol_check_ignores_order_symbol_case():
    lim = limits_from_dict(cfg(blocked_symbols=[" gme "]))
    order = {**order_for("GME", lim), "symbol": "gme"}
    r = check_order(order, lim, DayState(), MON_1015)
    assert r["verdict"] == "BLOCK" and "GME is on the blocked list" in r["reasons"]


# ---------- how `paper` parses today (documented, not endorsed) ----------
# limits_from_dict uses bool(value). Only a falsy value turns paper off, and
# risk_check then blocks every order. Any non-empty string is truthy, which is
# the safe side: the string "false" does NOT switch to live.

@pytest.mark.parametrize(
    "raw,expected",
    [(True, True), ("true", True), ("false", True), ("no", True), (1, True),
     (False, False), (0, False), ("", False), (None, False)],
    ids=repr,
)
def test_paper_parsing_is_plain_bool(raw, expected):
    assert limits_from_dict(cfg(paper=raw)).paper is expected


def test_paper_missing_key_raises():
    d = cfg()
    del d["paper"]
    with pytest.raises(KeyError):
        limits_from_dict(d)


def test_paper_zero_end_to_end_blocks_with_live_disabled_reason():
    lim = limits_from_dict(cfg(paper=0))
    assert lim.paper is False
    r = check_order(order_for("AAPL", lim), lim, DayState(), MON_1015)
    assert r == {
        "verdict": "BLOCK",
        "reasons": ["live trading is disabled in this phase (paper must be true)"],
    }


def test_paper_string_false_stays_paper_and_passes_end_to_end():
    lim = limits_from_dict(cfg(paper="false"))
    assert lim.paper is True
    assert check_order(order_for("AAPL", lim), lim, DayState(), MON_1015)["verdict"] == "PASS"
