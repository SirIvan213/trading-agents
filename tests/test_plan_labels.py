"""plan_labels, atr_5m_seeded and the three label thresholds in Limits. Offline, pure functions."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from tools import bars as B
from tools.bars import Bar, atr_5m, atr_5m_seeded, summarize
from tools.config import CONFIG_DIR, limits_from_dict, load_limits
from tools.plan_labels import STRUCTURAL_LEVELS, min_multiple, plan_labels

NY = ZoneInfo("America/New_York")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
EXAMPLE = json.loads((CONFIG_DIR / "limits.example.json").read_text())
ATR = D("0.2000")
VWAP = D("100.00")


def labels(**over):
    kw = dict(entry=D("100.10"), risk_per_share=D("0.30"), vwap=VWAP, atr_5m=ATR,
              atr_5m_provisional=False, structure_level="vwap", limits=LIMITS)
    kw.update(over)
    return plan_labels(**kw)


def test_example_limits_are_the_documented_thresholds():
    assert LIMITS.max_extension_atr_5m == D("2.0")
    assert LIMITS.max_stop_atr_5m == D("2.5")
    assert LIMITS.good_setup_multiple == D("2")
    assert min_multiple(LIMITS) == D("1")


# ---------------- extension boundary (strict) ----------------

def test_extension_exactly_at_the_threshold_is_not_extended():
    # 2.0 x 0.2000 = 0.40 above VWAP
    assert labels(entry=D("100.40"))["extended"] is False


def test_extension_one_cent_past_the_threshold_is_extended():
    assert labels(entry=D("100.41"))["extended"] is True


def test_entry_below_vwap_is_not_extended():
    assert labels(entry=D("99.00"))["extended"] is False


# ---------------- tight stop boundary (inclusive) ----------------

def test_stop_exactly_at_the_threshold_is_tight():
    # 2.5 x 0.2000 = 0.50
    assert labels(risk_per_share=D("0.50"))["stop_within_max_width"] is True


def test_stop_one_cent_past_the_threshold_is_not_tight():
    assert labels(risk_per_share=D("0.51"))["stop_within_max_width"] is False


# ---------------- structural stop ----------------

@pytest.mark.parametrize("name", sorted(STRUCTURAL_LEVELS))
def test_structural_levels_earn_the_good_multiple(name):
    out = labels(structure_level=name)
    assert out["extended"] is False and out["stop_within_max_width"] is True
    assert out["suggested_multiple"] == "2"


@pytest.mark.parametrize("name", ["last_close", "session_high", "", "bogus", None])
def test_non_structural_levels_get_the_minimum(name):
    out = labels(structure_level=name)
    assert out["suggested_multiple"] == "1"


def test_structural_levels_are_exactly_the_four_documented():
    assert STRUCTURAL_LEVELS == {"or_low", "or_high", "vwap", "session_low"}


# ---------------- suggested multiple: all three conditions needed ----------------

def test_each_failed_condition_alone_drops_the_suggestion_to_the_minimum():
    assert labels()["suggested_multiple"] == "2"
    assert labels(entry=D("100.41"))["suggested_multiple"] == "1"      # extended
    assert labels(risk_per_share=D("0.51"))["suggested_multiple"] == "1"  # loose stop
    assert labels(structure_level="last_close")["suggested_multiple"] == "1"


def test_labels_are_json_serializable_with_the_documented_keys():
    out = labels()
    assert json.loads(json.dumps(out)) == out
    assert set(out) == {"extended", "stop_within_max_width", "suggested_multiple",
                        "atr_5m_provisional"}


# ---------------- unknowns fail closed ----------------

@pytest.mark.parametrize("bad_atr", [None, D("0"), D("0.0000"), D("-0.2")])
def test_unusable_atr_makes_extended_and_tight_unknown_and_suggests_the_minimum(bad_atr):
    out = labels(atr_5m=bad_atr, atr_5m_provisional=True)
    assert out["extended"] is None and out["stop_within_max_width"] is None
    assert out["suggested_multiple"] == "1"
    assert out["atr_5m_provisional"] is False  # nothing to be provisional about


def test_missing_vwap_makes_only_extended_unknown():
    out = labels(vwap=None)
    assert out["extended"] is None and out["stop_within_max_width"] is True
    assert out["suggested_multiple"] == "1"  # an unknown label never earns the higher multiple


def test_provisional_atr_never_earns_more_than_the_minimum():
    """Early-session ATR tends to be too large (extended less likely, tight more likely), so
    a provisional ATR fails closed: the suggestion stays at the minimum."""
    good = labels(atr_5m_provisional=False)
    early = labels(atr_5m_provisional=True)
    assert good["suggested_multiple"] == "2"
    assert (early["extended"], early["stop_within_max_width"]) == (
        good["extended"], good["stop_within_max_width"])
    assert early["suggested_multiple"] == "1"


def test_provisional_flag_is_carried_when_the_atr_is_usable():
    assert labels(atr_5m_provisional=True)["atr_5m_provisional"] is True
    assert labels(atr_5m_provisional=False)["atr_5m_provisional"] is False


# ---------------- custom thresholds ----------------

def test_custom_thresholds_move_the_boundaries():
    lim = replace(LIMITS, max_extension_atr_5m=D("1"), max_stop_atr_5m=D("1.5"))
    assert labels(entry=D("100.20"), limits=lim)["extended"] is False   # exactly 1.0 ATR
    assert labels(entry=D("100.21"), limits=lim)["extended"] is True
    # 1.5 x 0.2 = 0.30 is the boundary
    assert labels(risk_per_share=D("0.30"), limits=lim)["stop_within_max_width"] is True
    assert labels(risk_per_share=D("0.31"), limits=lim)["stop_within_max_width"] is False


def test_min_reward_to_risk_above_one_raises_the_minimum_multiple():
    lim = replace(LIMITS, min_reward_to_risk=D("1.5"))
    assert min_multiple(lim) == D("1.5")
    assert labels(entry=D("100.41"), limits=lim)["suggested_multiple"] == "1.5"
    assert labels(limits=lim)["suggested_multiple"] == "2"


def test_min_reward_to_risk_below_one_still_gives_a_one_r_minimum():
    lim = replace(LIMITS, min_reward_to_risk=D("0.5"))
    assert min_multiple(lim) == D("1")
    assert labels(entry=D("100.41"), limits=lim)["suggested_multiple"] == "1"


@pytest.mark.parametrize("good,text", [("1", "1"), ("1.5", "1.5"), ("2.5", "2.5"), ("3", "3"),
                                       ("2.50", "2.5"), ("3.0", "3")])
def test_good_setup_multiple_is_spelled_like_the_menu(good, text):
    lim = replace(LIMITS, good_setup_multiple=D(good))
    assert labels(limits=lim)["suggested_multiple"] == text


@pytest.mark.parametrize("minimum", ["1", "1.5", "2"])
@pytest.mark.parametrize("good", ["2", "2.5", "3", "5"])
@pytest.mark.parametrize("case", [
    {}, {"entry": D("100.41")}, {"risk_per_share": D("0.51")}, {"structure_level": "x"},
    {"atr_5m": None}, {"vwap": None},
])
def test_suggestion_never_exceeds_good_setup_multiple(minimum, good, case):
    lim = replace(LIMITS, min_reward_to_risk=D(minimum), good_setup_multiple=D(good))
    assert D(labels(limits=lim, **case)["suggested_multiple"]) <= D(good)


# ---------------- config ----------------

def cfg(**over):
    d = copy.deepcopy(EXAMPLE)
    d.update(over)
    return d


def test_example_json_has_the_three_keys():
    for key in ("max_extension_atr_5m", "max_stop_atr_5m", "good_setup_multiple"):
        assert key in EXAMPLE, key


def test_defaults_apply_when_the_keys_are_absent():
    d = cfg()
    for key in ("max_extension_atr_5m", "max_stop_atr_5m", "good_setup_multiple"):
        del d[key]
    lim = limits_from_dict(d)
    assert (lim.max_extension_atr_5m, lim.max_stop_atr_5m, lim.good_setup_multiple) == (
        D("2.0"), D("2.5"), D("2"))


def test_values_are_loaded_from_json():
    lim = limits_from_dict(cfg(max_extension_atr_5m="1.5", max_stop_atr_5m="3",
                               good_setup_multiple="2.5"))
    assert (lim.max_extension_atr_5m, lim.max_stop_atr_5m, lim.good_setup_multiple) == (
        D("1.5"), D("3"), D("2.5"))


@pytest.mark.parametrize("field", ["max_extension_atr_5m", "max_stop_atr_5m",
                                   "good_setup_multiple"])
@pytest.mark.parametrize("bad", ["0", "0.0", 0, "-1", -2.5, "NaN", "Infinity", "abc", None])
def test_bad_threshold_values_are_rejected_naming_the_field(field, bad):
    with pytest.raises(ValueError, match=field):
        limits_from_dict(cfg(**{field: bad}))


def test_good_setup_multiple_below_min_reward_to_risk_is_rejected():
    with pytest.raises(ValueError, match="good_setup_multiple"):
        limits_from_dict(cfg(min_reward_to_risk="1.5", good_setup_multiple="1.4"))


def test_good_setup_multiple_above_max_reward_multiple_is_rejected():
    with pytest.raises(ValueError, match="good_setup_multiple"):
        limits_from_dict(cfg(max_reward_multiple="5", good_setup_multiple="5.01"))


def test_good_setup_multiple_exactly_at_either_bound_is_accepted():
    assert limits_from_dict(cfg(min_reward_to_risk="1.5", good_setup_multiple="1.5"))
    assert limits_from_dict(cfg(max_reward_multiple="3", good_setup_multiple="3"))


@pytest.mark.parametrize("good", ["1.75", "4", "5", "0.5"])
def test_good_setup_multiple_must_be_on_the_menu(good):
    d = cfg(good_setup_multiple=good, min_reward_to_risk="0.5")
    with pytest.raises(ValueError, match="good_setup_multiple must be one of"):
        limits_from_dict(d)


@pytest.mark.parametrize("good", ["1", "1.5", "2", "2.5", "3", "2.0", "3.00"])
def test_every_menu_spelling_of_good_setup_multiple_is_accepted(good):
    assert limits_from_dict(cfg(good_setup_multiple=good)).good_setup_multiple == D(good)


# ---------------- atr_5m_seeded ----------------

def five_min_series(shapes, day=3):
    """One 5-minute bucket per (high, low, close): five identical 1-minute bars each."""
    out = []
    for k, (h, lo, c) in enumerate(shapes):
        for m in range(5):
            t = datetime(2026, 11, day, 9, 30, tzinfo=NY) + timedelta(minutes=5 * k + m)
            out.append(Bar(ts=t, open=D(c), high=D(h), low=D(lo), close=D(c), volume=100))
    return out


THREE = [("11", "9", "10"), ("14", "11", "13"), ("13", "12", "12.5")]
# true ranges: 2; max(3, |14-10|, |11-10|) = 4; max(1, |13-13|, |12-13|) = 1 -> mean 7/3


def test_two_finished_five_minute_bars_give_none():
    assert atr_5m_seeded(five_min_series(THREE[:2])) == (None, False)
    assert atr_5m_seeded([]) == (None, False)


def test_three_finished_bars_give_the_provisional_mean_of_true_ranges():
    assert atr_5m_seeded(five_min_series(THREE)) == (D("2.3333"), True)


def test_provisional_mean_for_every_count_between_the_minimum_and_n():
    shapes = [(str(11 + i % 3), str(9 + i % 2), "10") for i in range(13)]
    for count in range(3, 14):
        bars = five_min_series(shapes[:count])
        value, provisional = atr_5m_seeded(bars)
        assert provisional is True and value is not None
        fives = B.resample(bars, 5)
        trs = [fives[0].high - fives[0].low] + [
            max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
            for p, b in zip(fives, fives[1:], strict=False)]
        assert value == (sum(trs) / len(trs)).quantize(D("0.0001"))


def test_fourteen_bars_switch_to_wilder_and_equal_atr_5m():
    shapes = [(str(11 + i % 4), str(9 + i % 3), str(10 + i % 2)) for i in range(14)]
    bars = five_min_series(shapes)
    value, provisional = atr_5m_seeded(bars)
    assert provisional is False and value == atr_5m(bars) and value is not None
    assert atr_5m(five_min_series(shapes[:13])) is None  # atr_5m itself is unchanged


def test_unfinished_trailing_bucket_is_ignored():
    bars = five_min_series(THREE)
    partial = five_min_series(THREE + [("30", "1", "15")])[-5:-1]  # 4 of the 5 minutes
    assert atr_5m_seeded(bars + partial) == (D("2.3333"), True)  # the wild bucket not counted
    # two finished buckets plus a partial third are still below the minimum
    assert atr_5m_seeded(five_min_series(THREE[:2]) + five_min_series(THREE)[10:14]) == (
        None, False)


def test_custom_n_and_min_bars():
    bars = five_min_series(THREE)
    value, provisional = atr_5m_seeded(bars, n=3)
    assert provisional is False and value == atr_5m(bars, n=3)
    assert atr_5m_seeded(bars, n=14, min_bars=4) == (None, False)
    with pytest.raises(ValueError):
        atr_5m_seeded(bars, n=3, min_bars=4)
    with pytest.raises(ValueError):
        atr_5m_seeded(bars, n=3, min_bars=0)


def test_summarize_reports_the_seeded_atr_and_the_provisional_flag():
    s = summarize(five_min_series(THREE))
    assert s["atr_5m"] == "2.3333" and s["atr_5m_provisional"] is True
    assert summarize(five_min_series(THREE[:2]))["atr_5m"] is None
    assert summarize(five_min_series(THREE[:2]))["atr_5m_provisional"] is False
    long = summarize(five_min_series([("11", "9", "10")] * 14))
    assert long["atr_5m_provisional"] is False and long["atr_5m"] == "2.0000"
    assert summarize([])["atr_5m_provisional"] is False


def test_seeded_atr_is_never_what_atr_5m_returns_before_n_bars():
    bars = five_min_series(THREE)
    assert atr_5m(bars) is None and atr_5m_seeded(bars)[0] is not None
