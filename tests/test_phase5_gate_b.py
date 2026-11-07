"""Phase 5 gate B: #11 (NYSE calendar: holidays, early closes), #12 (stale trade/quote in
scan), #14 (gappy opening range refuses orb). Offline: fixtures, fakes, a fixed clock."""
import copy
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

import pipeline.market as market
from agents.tool_registry import ToolSession
from tests.fakes import FakeMarketData, load_fixture, merged_bars
from tools import bars as B
from tools import market_calendar as cal
from tools.config import CONFIG_DIR, limits_from_dict, load_limits
from tools.exit_plan import exit_plan
from tools.risk_check import DayState, check_order
from tools.scan import ScanCriteria, criteria_from_dict, scan
from tools.size import Plan, size_position

NY = ZoneInfo("America/New_York")
EXAMPLE_PATH = CONFIG_DIR / "limits.example.json"
EXAMPLE = json.loads(EXAMPLE_PATH.read_text())
LIMITS = load_limits(EXAMPLE_PATH)


def at(y, m, d, hh, mm, s=0):
    return datetime(y, m, d, hh, mm, s, tzinfo=NY)


def order(limits=LIMITS):
    sized = size_position(Plan("AAPL", D("20.00"), D("19.00"), D("21.00")), limits)
    assert sized["ok"], sized
    return sized["order"]


def check(now, limits=LIMITS):
    return check_order(order(limits), limits, DayState(), now)


# ============================ #11 calendar tables ============================

HOL_2026 = [(1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3), (9, 7), (11, 26),
            (12, 25)]
HOL_2027 = [(1, 1), (1, 18), (2, 15), (3, 26), (5, 31), (6, 18), (7, 5), (9, 6), (11, 25),
            (12, 24)]
HOLIDAYS = [date(2026, m, d) for m, d in HOL_2026] + [date(2027, m, d) for m, d in HOL_2027]
EARLY = [date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)]


def test_holiday_table_is_exactly_the_listed_dates():
    assert set(cal.FULL_HOLIDAYS) == set(HOLIDAYS)
    assert set(cal.EARLY_CLOSES) == set(EARLY)
    assert cal.COVERED_YEARS == {2026, 2027}


@pytest.mark.parametrize("day", HOLIDAYS, ids=str)
def test_every_listed_holiday_has_no_session(day):
    assert cal.session_hours(day) is None
    assert cal.closed_reason(day) == cal.FULL_HOLIDAYS[day]


@pytest.mark.parametrize("day", EARLY, ids=str)
def test_every_early_close_ends_at_13(day):
    assert cal.session_hours(day) == (time(9, 30), time(13, 0))


def test_normal_weekday_and_weekend():
    assert cal.session_hours(date(2026, 11, 30)) == (time(9, 30), time(16, 0))
    assert cal.session_hours(date(2026, 11, 28)) is None  # Saturday
    assert cal.session_hours(date(2026, 11, 29)) is None  # Sunday
    assert cal.closed_reason(date(2026, 11, 28)) == "weekend"


@pytest.mark.parametrize("day", [date(2025, 11, 3), date(2028, 3, 1)], ids=str)
def test_uncovered_year_raises(day):
    assert cal.covers(day) is False
    with pytest.raises(cal.CalendarError, match="does not cover"):
        cal.session_hours(day)
    with pytest.raises(cal.CalendarError):
        cal.day_times(day, LIMITS)


def test_day_times_normal_day_is_configured():
    assert cal.day_times(date(2026, 11, 30), LIMITS) == (time(9, 45), time(15, 30), time(15, 55))


@pytest.mark.parametrize("day", EARLY, ids=str)
def test_day_times_early_close_example_limits(day):
    assert cal.day_times(day, LIMITS) == (time(9, 45), time(12, 30), time(12, 55))


def test_day_times_flat_by_already_earlier_than_1255():
    lim = replace(LIMITS, entry_end=time(11, 0), flat_by=time(11, 30))
    # flat_by is not moved later; entry_end keeps its place
    assert cal.day_times(date(2026, 11, 27), lim) == (time(9, 45), time(11, 0), time(11, 30))


def test_day_times_entry_end_keeps_configured_gap_before_flat_by():
    lim = replace(LIMITS, entry_end=time(15, 0), flat_by=time(15, 55))  # 55 min gap
    assert cal.day_times(date(2026, 11, 27), lim) == (time(9, 45), time(12, 0), time(12, 55))


def test_day_times_entry_end_clamped_at_entry_start():
    lim = replace(LIMITS, entry_start=time(12, 40), entry_end=time(15, 30), flat_by=time(15, 55))
    assert cal.day_times(date(2026, 11, 27), lim) == (time(12, 40), time(12, 40), time(12, 55))


def test_day_times_wide_gap_keeps_an_early_configured_entry_end():
    """entry_end 10:30 with flat_by 15:55 (gap 5h25): keeping the gap would close the window
    before it opens, but 10:30 is already well before the 12:55 flat-by, so it stays."""
    lim = replace(LIMITS, entry_end=time(10, 30), flat_by=time(15, 55))
    assert cal.day_times(date(2026, 11, 27), lim) == (time(9, 45), time(10, 30), time(12, 55))
    assert cal.day_times(date(2026, 11, 30), lim) == (time(9, 45), time(10, 30), time(15, 55))
    assert check(at(2026, 11, 27, 10, 30), limits=lim)["verdict"] == "PASS"
    blocked = check(at(2026, 11, 27, 10, 31), limits=lim)
    assert blocked["verdict"] == "BLOCK"
    assert "outside entry window 09:45-10:30 ET (early close)" in blocked["reasons"]


# ============================ #11 risk_check ============================

@pytest.mark.parametrize("day", [(2026, 11, 27), (2026, 12, 24)], ids=str)
def test_early_close_day_pass_in_window(day):
    assert check(at(*day, 11, 0)) == {"verdict": "PASS", "reasons": []}
    assert check(at(*day, 12, 30))["verdict"] == "PASS"  # boundary is inclusive


@pytest.mark.parametrize("day", [(2026, 11, 27), (2026, 12, 24)], ids=str)
@pytest.mark.parametrize("hh,mm", [(12, 31), (12, 45)])
def test_early_close_day_blocked_after_entry_end(day, hh, mm):
    r = check(at(*day, hh, mm))
    assert r == {"verdict": "BLOCK",
                 "reasons": ["outside entry window 09:45-12:30 ET (early close)"]}


@pytest.mark.parametrize("day", [(2026, 11, 27), (2026, 12, 24)], ids=str)
def test_early_close_day_blocked_after_close(day):
    assert check(at(*day, 13, 0))["reasons"] == ["market is closed"]
    assert check(at(*day, 13, 30))["reasons"] == ["market is closed"]


@pytest.mark.parametrize("day,name", [
    ((2026, 11, 26), "Thanksgiving Day"),
    ((2026, 12, 25), "Christmas Day"),
    ((2027, 12, 24), "Christmas Day (observed)"),
])
def test_full_holiday_blocked_at_any_time(day, name):
    for hh, mm in [(11, 0), (10, 15)]:
        r = check(at(*day, hh, mm))
        assert r == {"verdict": "BLOCK", "reasons": [f"market is closed ({name})"]}


def test_weekend_blocked():
    r = check(at(2026, 11, 28, 11, 0))
    assert r == {"verdict": "BLOCK", "reasons": ["market is closed (weekend)"]}


def test_normal_day_entry_window_still_1530():
    assert check(at(2026, 11, 30, 15, 0)) == {"verdict": "PASS", "reasons": []}
    assert check(at(2026, 11, 30, 15, 30))["verdict"] == "PASS"
    r = check(at(2026, 11, 30, 15, 31))
    assert r["reasons"] == ["outside entry window 09:45-15:30 ET"]  # no early-close suffix


@pytest.mark.parametrize("day", [(2025, 11, 3), (2028, 3, 1)])
def test_uncovered_year_blocked(day):
    r = check(at(*day, 11, 0))
    assert r == {"verdict": "BLOCK", "reasons": ["market calendar does not cover this date"]}


# ============================ #11 bars ============================

def minute_bars(day: date, start=(9, 30), end=(15, 59)):
    out, t = [], datetime(day.year, day.month, day.day, *start, tzinfo=NY)
    stop = datetime(day.year, day.month, day.day, *end, tzinfo=NY)
    while t <= stop:
        out.append({"t": t.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "o": 10, "h": 10.2, "l": 9.9, "c": 10.1, "v": 100})
        t += timedelta(minutes=1)
    return {"bars": out}


def test_regular_session_early_close_keeps_before_13():
    kept = B.regular_session(B.parse_bars(minute_bars(date(2026, 11, 27))))
    assert kept[0].ts.astimezone(NY).time() == time(9, 30)
    assert kept[-1].ts.astimezone(NY).time() == time(12, 59)
    assert len(kept) == 210


def test_regular_session_normal_day_keeps_to_1559():
    kept = B.regular_session(B.parse_bars(minute_bars(date(2026, 11, 30))))
    assert kept[-1].ts.astimezone(NY).time() == time(15, 59) and len(kept) == 390


@pytest.mark.parametrize("day", [date(2026, 11, 26), date(2026, 11, 28)], ids=str)
def test_regular_session_holiday_or_weekend_has_no_bars(day):
    assert B.regular_session(B.parse_bars(minute_bars(day))) == []


def test_regular_session_uncovered_year_falls_back_to_1600():
    kept = B.regular_session(B.parse_bars(minute_bars(date(2028, 3, 1))))
    assert len(kept) == 390


def test_summarize_on_early_close_stops_at_1259():
    s = B.summarize(B.regular_session(B.parse_bars(minute_bars(date(2026, 11, 27)))))
    assert s["bars"] == 210 and s["last"].endswith("12:59:00-05:00")


def test_summarize_on_holiday_is_empty():
    s = B.summarize(B.regular_session(B.parse_bars(minute_bars(date(2026, 11, 26)))))
    assert s["bars"] == 0 and s["opening_range"] is None


# ============================ #11 exit_plan / plan_exit ============================

GOOD_OR = {"high": "102.00", "low": "99.70", "complete": True, "missing_minutes": 0}


def test_exit_plan_without_session_day_uses_configured_flat_by():
    r = exit_plan("orb", D("102.50"), LIMITS, opening_range=GOOD_OR, atr=D("0.80"))
    assert r["ok"] and r["time_exit"] == "15:55"


def test_exit_plan_session_day_normal_and_early():
    n = exit_plan("orb", D("102.50"), LIMITS, opening_range=GOOD_OR, atr=D("0.80"),
                  session_day=date(2026, 11, 30))
    e = exit_plan("orb", D("102.50"), LIMITS, opening_range=GOOD_OR, atr=D("0.80"),
                  session_day=date(2026, 11, 27))
    assert n["ok"] and n["time_exit"] == "15:55"
    assert e["ok"] and e["time_exit"] == "12:55"
    assert {k: v for k, v in n.items() if k != "time_exit"} == \
           {k: v for k, v in e.items() if k != "time_exit"}


@pytest.mark.parametrize("day", [date(2026, 11, 26), date(2026, 11, 28), date(2027, 12, 24)])
def test_exit_plan_holiday_or_weekend_refused(day):
    r = exit_plan("orb", D("102.50"), LIMITS, opening_range=GOOD_OR, atr=D("0.80"),
                  session_day=day)
    assert r == {"ok": False, "reason": f"market is closed on {day}"}


def test_exit_plan_uncovered_year_refused():
    r = exit_plan("orb", D("102.50"), LIMITS, opening_range=GOOD_OR, atr=D("0.80"),
                  session_day=date(2028, 3, 1))
    assert r == {"ok": False, "reason": "market calendar does not cover this date"}


class ShiftedData(FakeMarketData):
    """FakeMarketData whose saved bars are moved to another date (and optionally thinned)."""

    def __init__(self, day: str, drop: tuple[str, ...] = (), **kw):
        super().__init__(**kw)
        self.day, self.drop = day, drop

    def bars(self, symbol, start, end):
        raw = copy.deepcopy(merged_bars())
        out = []
        for b in raw["bars"]:
            if b["t"][11:16] in self.drop:
                continue
            b["t"] = self.day + b["t"][10:]
            out.append(b)
        return {"bars": out}


def session(day: str, now: datetime, drop=()) -> ToolSession:
    s = ToolSession(symbol="AAPL", now=now, data=ShiftedData(day, drop), limits=LIMITS,
                    criteria=ScanCriteria())
    _, err = s.execute("get_bars", {"symbol": "AAPL"})
    assert err is False
    return s


def level_plan(s):
    entry = s.summary["price_levels"]["last_close"]
    return s.execute("plan_exit", {"setup": "level", "entry": entry,
                                   "level": s.summary["price_levels"]["or_low"]})


def test_plan_exit_through_session_early_close_time_exit_1255():
    s = session("2026-11-27", at(2026, 11, 27, 11, 0))
    res, err = level_plan(s)
    assert err is False and res["ok"] is True, res
    assert res["time_exit"] == "12:55"
    assert s.exit_plans[0].time_exit == "12:55"


def test_plan_exit_through_session_normal_day_time_exit_1555():
    s = session("2026-11-03", at(2026, 11, 3, 10, 30))
    res, _ = level_plan(s)
    assert res["ok"] is True and res["time_exit"] == "15:55"


def test_get_bars_on_holiday_returns_no_bars():
    s = ToolSession(symbol="AAPL", now=at(2026, 11, 26, 11, 0),
                    data=ShiftedData("2026-11-26"), limits=LIMITS, criteria=ScanCriteria())
    res, _ = s.execute("get_bars", {"symbol": "AAPL"})
    assert res["bars"] == 0
    assert s.exit_plans == []


def test_plan_exit_holiday_with_summary_present_refused():
    s = session("2026-11-27", at(2026, 11, 27, 11, 0))
    s.now = at(2026, 11, 26, 11, 0)  # same summary, but the session clock is Thanksgiving
    res, _ = level_plan(s)
    assert res == {"ok": False, "reason": "market is closed on 2026-11-26"}
    assert s.exit_plans == []


# ============================ #12 scan staleness ============================

SNAP_T = datetime(2026, 11, 3, 14, 40, tzinfo=UTC)  # trade and quote time in the fixture


def snaps(trade_t="2026-11-03T14:40:00Z", quote_t="2026-11-03T14:40:00Z"):
    s = load_fixture("alpaca_snapshots.json")["GAPA"]
    for key, t in (("latestTrade", trade_t), ("latestQuote", quote_t)):
        if t is None:
            del s[key]["t"]
        else:
            s[key]["t"] = t
    return {"GAPA": s}


def run(snapshots, now, criteria=None):
    return scan(snapshots, criteria or ScanCriteria(), LIMITS,
                today=date(2026, 11, 3), now=now)


def reasons(out):
    assert out["candidates"] == []
    return out["rejected"][0]["reasons"]


def test_scan_fresh_trade_and_quote_pass():
    out = run(snaps(), SNAP_T + timedelta(seconds=30))
    assert [c["symbol"] for c in out["candidates"]] == ["GAPA"]


def test_scan_age_exactly_at_max_passes_one_second_over_blocks():
    assert run(snaps(), SNAP_T + timedelta(seconds=120))["candidates"]
    r = reasons(run(snaps(), SNAP_T + timedelta(seconds=121)))
    assert r == ["stale trade (121s old, max 120s)", "stale quote (121s old, max 120s)"]


def test_scan_only_trade_stale():
    r = reasons(run(snaps(trade_t="2026-11-03T14:30:00Z"), SNAP_T + timedelta(seconds=10)))
    assert r == ["stale trade (610s old, max 120s)"]


def test_scan_only_quote_stale():
    r = reasons(run(snaps(quote_t="2026-11-03T14:30:00Z"), SNAP_T + timedelta(seconds=10)))
    assert r == ["stale quote (610s old, max 120s)"]


@pytest.mark.parametrize("bad", [None, "", "yesterday", "2026-11-03T14:40:00"])
def test_scan_missing_or_invalid_timestamps(bad):
    now = SNAP_T + timedelta(seconds=5)
    assert reasons(run(snaps(trade_t=bad), now)) == ["stale trade (missing or invalid timestamp)"]
    assert reasons(run(snaps(quote_t=bad), now)) == ["stale quote (missing or invalid timestamp)"]


def test_scan_future_timestamp_counts_as_fresh():
    assert run(snaps(), SNAP_T - timedelta(seconds=600))["candidates"]


def test_scan_custom_max_age():
    crit = criteria_from_dict({"max_quote_age_s": 600})
    now = SNAP_T + timedelta(seconds=500)
    assert run(snaps(), now, crit)["candidates"]
    r = reasons(run(snaps(), SNAP_T + timedelta(seconds=601), crit))
    assert r[0] == "stale trade (601s old, max 600s)"
    assert run(snaps(), now)["candidates"] == []  # default 120 s blocks the same data


def test_scan_criteria_dict_includes_max_age():
    assert run(snaps(), SNAP_T)["criteria"]["max_quote_age_s"] == 120


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "abc"])
def test_scan_invalid_max_age_rejected(bad):
    with pytest.raises(ValueError):
        criteria_from_dict({"max_quote_age_s": bad})


def test_scan_dataclass_rejects_bad_ages_directly():
    for bad in (0, -5, True, 1.5):
        with pytest.raises(ValueError, match="max_quote_age_s"):
            ScanCriteria(max_quote_age_s=bad)


def test_scan_criteria_accepts_whole_number_forms():
    assert criteria_from_dict({"max_quote_age_s": "90"}).max_quote_age_s == 90
    assert criteria_from_dict({"max_quote_age_s": 90.0}).max_quote_age_s == 90


def test_scan_without_now_has_no_staleness_check():
    out = scan(snaps(trade_t="2000-01-01T00:00:00Z", quote_t=None), ScanCriteria(), LIMITS,
               today=date(2026, 11, 3))
    assert [c["symbol"] for c in out["candidates"]] == ["GAPA"]


def test_scan_naive_now_raises():
    with pytest.raises(ValueError):
        scan(snaps(), ScanCriteria(), LIMITS, now=datetime(2026, 11, 3, 14, 41))


def test_run_scan_halted_looking_snapshot_rejected_both_reasons():
    """Today's dailyBar, but the last trade and quote are 7 minutes old (a halt)."""
    halted = snaps(trade_t="2026-11-03T14:34:00Z", quote_t="2026-11-03T14:34:00Z")
    now = datetime(2026, 11, 3, 14, 41, tzinfo=UTC)
    out = market.run_scan(halted, ScanCriteria(), LIMITS, now)
    assert out["candidates"] == []
    assert out["rejected"][0]["reasons"] == [
        "stale trade (420s old, max 120s)", "stale quote (420s old, max 120s)"]


def test_run_scan_live_snapshot_accepted():
    now = datetime(2026, 11, 3, 14, 41, tzinfo=UTC)
    out = market.run_scan(snaps(), ScanCriteria(), LIMITS, now)
    assert [c["symbol"] for c in out["candidates"]] == ["GAPA"]


# ============================ #14 missing opening-range minutes ============================

def or_bars(skip: tuple[int, ...] = (), bar_minutes: int = 1, upto: int = 20):
    """Bars from 09:30 every bar_minutes, skipping the given minute offsets."""
    items = []
    for off in range(0, upto, bar_minutes):
        if off in skip:
            continue
        t = datetime(2026, 11, 3, 9, 30, tzinfo=NY) + timedelta(minutes=off)
        items.append({"t": t.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                      "o": 10, "h": 10.5, "l": 9.8, "c": 10.1, "v": 100})
    return B.parse_bars({"bars": items})


@pytest.mark.parametrize("skip,expected", [((), 0), ((3, 4, 5), 3), ((3, 4, 5, 6), 4)])
def test_opening_range_missing_minutes_1m(skip, expected):
    o = B.opening_range(or_bars(skip))
    assert o["missing_minutes"] == expected and o["complete"] is True


def test_opening_range_missing_minutes_5m():
    full = B.opening_range(or_bars(bar_minutes=5), minutes=15, bar_minutes=5)
    assert full["missing_minutes"] == 0
    gap = B.opening_range(or_bars(skip=(5,), bar_minutes=5), minutes=15, bar_minutes=5)
    assert gap["missing_minutes"] == 5


def test_opening_range_late_first_bar_counts_leading_slots():
    o = B.opening_range(or_bars(skip=(0, 1)))
    assert o["missing_minutes"] == 2


@pytest.mark.parametrize("minutes,bar", [(15, 4), (10, 3), (0, 1), (-5, 5), (15, 0)])
def test_opening_range_invalid_minutes_raises(minutes, bar):
    with pytest.raises(ValueError):
        B.opening_range(or_bars(), minutes=minutes, bar_minutes=bar)


def orb(missing, limits=LIMITS):
    orr = {"high": "102.00", "low": "99.70", "complete": True}
    if missing is not ...:
        orr["missing_minutes"] = missing
    return exit_plan("orb", D("102.50"), limits, opening_range=orr, atr=D("0.80"))


def test_orb_passes_at_exactly_max_missing():
    assert orb(3)["ok"] is True
    assert orb(0)["ok"] is True


def test_orb_refused_above_max_missing():
    assert orb(4) == {"ok": False, "reason": "opening range is missing 4 minutes of bars (max 3)"}


def test_orb_max_is_configurable_both_ways():
    assert orb(4, replace(LIMITS, max_or_missing_minutes=4))["ok"] is True
    r = orb(1, replace(LIMITS, max_or_missing_minutes=0))
    assert r["ok"] is False and "max 0" in r["reason"]
    assert orb(0, replace(LIMITS, max_or_missing_minutes=0))["ok"] is True


@pytest.mark.parametrize("bad", [..., None, True, False, -1, "2", 2.0])
def test_orb_refused_on_invalid_missing_minutes(bad):
    r = orb(bad)
    assert r == {"ok": False, "reason": "opening range has no valid missing_minutes count"}


def test_level_plan_ignores_missing_minutes():
    r = exit_plan("level", D("102.50"), LIMITS, level=D("101.00"), atr=D("0.80"))
    assert r["ok"] is True


def test_config_default_and_example():
    assert EXAMPLE["max_or_missing_minutes"] == 3
    assert LIMITS.max_or_missing_minutes == 3
    d = copy.deepcopy(EXAMPLE)
    del d["max_or_missing_minutes"]
    assert limits_from_dict(d).max_or_missing_minutes == 3


@pytest.mark.parametrize("bad", [-1, True, 1.5, "x"])
def test_config_rejects_bad_missing_minutes(bad):
    d = copy.deepcopy(EXAMPLE)
    d["max_or_missing_minutes"] = bad
    with pytest.raises(ValueError):
        limits_from_dict(d)


@pytest.mark.parametrize("good", ["4", 4.0, 4, 0])
def test_config_accepts_whole_numbers(good):
    d = copy.deepcopy(EXAMPLE)
    d["max_or_missing_minutes"] = good
    assert limits_from_dict(d).max_or_missing_minutes == int(good)


def test_gappy_opening_fixture_refuses_orb_and_or_levels_but_allows_vwap_level():
    now = at(2026, 11, 3, 10, 30)
    gappy = session("2026-11-03", now, drop=("14:35", "14:36", "14:37", "14:38"))
    assert gappy.summary["opening_range"]["missing_minutes"] == 4
    entry = gappy.summary["price_levels"]["last_close"]
    res, err = gappy.execute("plan_exit", {"setup": "orb", "entry": entry})
    assert err is False
    assert res["ok"] is False and "missing 4 minutes of bars (max 3)" in res["reason"]
    assert gappy.exit_plans == []
    # code review: a level plan on or_low / or_high must not bypass the gap gate
    for name in ("or_low", "or_high"):
        lvl, err = gappy.execute("plan_exit", {
            "setup": "level", "entry": entry, "level": gappy.summary["price_levels"][name]})
        assert err is False
        assert lvl["ok"] is False and "missing 4 minutes of bars (max 3)" in lvl["reason"], lvl
    assert gappy.exit_plans == []
    vwap, _ = gappy.execute("plan_exit", {
        "setup": "level", "entry": entry, "level": gappy.summary["price_levels"]["vwap"]})
    assert "opening range is missing" not in vwap.get("reason", "")


def test_three_missing_fixture_passes_the_gap_gate():
    s = session("2026-11-03", at(2026, 11, 3, 10, 30), drop=("14:35", "14:36", "14:37"))
    assert s.summary["opening_range"]["missing_minutes"] == 3
    entry = s.summary["price_levels"]["last_close"]
    res, _ = s.execute("plan_exit", {"setup": "orb", "entry": entry})
    assert res["ok"] is True, res
    lvl, _ = level_plan(s)  # or_low level from a 3-missing range is allowed too
    assert lvl["ok"] is True, lvl


# ============================ code-review additions (#12) ============================

NOW_UTC = datetime(2026, 11, 3, 14, 40, 0, tzinfo=UTC)


@pytest.mark.parametrize("t, expected", [
    ("2026-11-03T14:39:59.876543219Z", None),     # Alpaca nanoseconds, 0.12 s old
    ("2026-11-03T14:38:00Z", None),                 # exactly 120 s: fresh
    ("2026-11-03T14:37:59.500000000Z", "stale trade (121s old, max 120s)"),  # 120.5 s: stale
], ids=["nanoseconds", "120s", "120.5s"])
def test_staleness_parses_nanoseconds_and_compares_exactly(t, expected):
    from tools.scan import _staleness
    assert _staleness("trade", t, NOW_UTC, 120) == expected


def test_weekend_bars_are_dropped_even_in_an_uncovered_year():
    from tools.bars import Bar, regular_session
    sat = datetime(2028, 3, 4, 10, 0, tzinfo=NY)  # a Saturday, year not in the calendar
    mon = datetime(2028, 3, 6, 10, 0, tzinfo=NY)
    bars = [Bar(ts=t, open=D("1"), high=D("1"), low=D("1"), close=D("1"), volume=1)
            for t in (sat, mon)]
    assert [b.ts for b in regular_session(bars)] == [mon]
