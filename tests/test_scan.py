"""Tests for tools.scan. Fixtures plus fake openers; expectations computed from raw values."""
import json
import urllib.parse
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from tools import scan as S
from tools.config import CONFIG_DIR, load_limits

FIX = Path(__file__).parent / "fixtures"
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
CRIT = S.ScanCriteria()
CENT = D("0.01")


def snaps():
    return json.loads((FIX / "alpaca_snapshots.json").read_text(), parse_float=D)


def gap_of(s):
    o, pc = D(str(s["dailyBar"]["o"])), D(str(s["prevDailyBar"]["c"]))
    return ((o - pc) / pc * 100).quantize(CENT)


def spread_of(s):
    ask, bid = D(str(s["latestQuote"]["ap"])), D(str(s["latestQuote"]["bp"]))
    return ((ask - bid) / ((ask + bid) / 2) * 100).quantize(CENT)


def rvol_of(s, avg=None):
    den = avg if avg else s["prevDailyBar"]["v"]
    return (D(s["dailyBar"]["v"]) / D(den)).quantize(CENT)


def run(data=None, criteria=CRIT, limits=LIMITS, avg=None):
    return S.scan(snaps() if data is None else data, criteria, limits, avg)


def rejected(result):
    return {r["symbol"]: r["reasons"] for r in result["rejected"]}


def cands(result):
    return {c["symbol"]: c for c in result["candidates"]}


def mk(prev_c="100", o="102", v=500000, price="102", bid="101.99", ask="102.01", prev_v=250000):
    s = {
        "dailyBar": {"o": o, "c": price, "v": v},
        "prevDailyBar": {"c": prev_c, "v": prev_v},
        "latestTrade": {"p": price},
    }
    if bid is not None:
        s["latestQuote"] = {"bp": bid, "ap": ask}
    return s


# ---------- fixture-driven outcomes ----------

def test_fixture_candidates_and_sort_order():
    r = run()
    syms = [c["symbol"] for c in r["candidates"]]
    assert syms == ["GAPA", "GAPB"]  # rvol 3.00 then 1.50
    a, b = cands(r)["GAPA"], cands(r)["GAPB"]
    data = snaps()
    assert D(a["gap_pct"]) == gap_of(data["GAPA"]) == D("5.00")
    assert D(a["spread_pct"]) == spread_of(data["GAPA"]) == D("0.09")  # 0.01 / 10.595
    assert D(a["rvol"]) == rvol_of(data["GAPA"]) == D("3.00")
    assert a["rvol_note"] == S.RVOL_NOTE
    assert a["price"] == "10.60" and a["volume"] == 600000
    assert D(b["gap_pct"]) == gap_of(data["GAPB"]) == D("4.00")
    assert D(b["rvol"]) == rvol_of(data["GAPB"]) == D("1.50")
    assert D(b["spread_pct"]) == spread_of(data["GAPB"]) == D("0.10")  # 0.02 / 20.79
    assert a["spread_pct"] == "0.09" and b["spread_pct"] == "0.10"


def test_fixture_rejections_have_expected_reasons():
    rej = rejected(run())
    assert any("wrong direction" in x for x in rej["DOWN"])
    assert any("gap" in x and "below minimum" in x for x in rej["SMALL"])
    assert any("price" in x and "below minimum" in x for x in rej["PENNY"])
    assert any("above maximum" in x and "price" in x for x in rej["BIGP"])
    assert any("volume" in x for x in rej["LOWV"])
    assert any("spread" in x and "above maximum" in x for x in rej["WIDE"])
    assert any("spread unavailable" in x for x in rej["CROSQ"])
    assert any("spread unavailable" in x for x in rej["NOQTE"])
    assert rej["NULLS"] == ["null or malformed snapshot"]
    assert any("malformed snapshot" in x for x in rej["BADSN"])


def test_every_symbol_is_candidate_or_rejected_exactly_once():
    r = run()
    names = [c["symbol"] for c in r["candidates"]] + [x["symbol"] for x in r["rejected"]]
    assert sorted(names) == sorted(snaps())


def test_wide_spread_value_from_first_principles():
    assert spread_of(snaps()["WIDE"]) == D("2.00")
    assert any("2.00%" in x for x in rejected(run())["WIDE"])


def test_result_json_serializable_and_criteria_echoed():
    r = run()
    json.dumps(r)
    assert r["criteria"]["min_price"] == "2.00" and r["criteria"]["direction"] == "up"


def test_direction_both_keeps_gap_down():
    r = run(criteria=S.ScanCriteria(direction="both"))
    assert [c["symbol"] for c in r["candidates"]] == ["GAPA", "DOWN", "GAPB"]  # rvol 3, 2, 1.5
    down = cands(r)["DOWN"]
    assert D(down["gap_pct"]) == gap_of(snaps()["DOWN"]) == D("-5.00")
    assert D(down["rvol"]) == D("2.00")


def test_direction_up_rejects_gap_down():
    assert "DOWN" in rejected(run())
    assert "DOWN" not in cands(run())


# ---------- boundaries ----------

def ok_for(snap, criteria=CRIT):
    r = S.scan({"TEST": snap}, criteria, LIMITS)
    return bool(r["candidates"]), rejected(r).get("TEST", [])


@pytest.mark.parametrize("price", ["2.00", "500.00", "250"])
def test_price_inside_inclusive_bounds_passes(price):
    p = D(price)
    ok, why = ok_for(mk(prev_c="1", o="1.5", price=price, bid=str(p - D("0.005")),
                        ask=str(p + D("0.005"))))
    assert ok, why


@pytest.mark.parametrize("price,word", [("1.99", "below minimum"), ("500.01", "above maximum")])
def test_price_outside_bounds_rejected(price, word):
    p = D(price)
    ok, why = ok_for(mk(prev_c="1", o="1.5", price=price, bid=str(p - D("0.005")),
                        ask=str(p + D("0.005"))))
    assert not ok and any(word in x for x in why)


def test_volume_exactly_at_min_passes_and_one_below_rejected():
    assert ok_for(mk(v=100000))[0]
    ok, why = ok_for(mk(v=99999))
    assert not ok and any("volume" in x for x in why)


def test_gap_exactly_at_min_passes():
    s = mk(prev_c="100", o="102")
    assert gap_of(s) == D("2.00")
    assert ok_for(s)[0]


def test_gap_just_below_min_rejected():
    s = mk(prev_c="100", o="101.99", price="102")
    assert gap_of(s) == D("1.99")
    ok, why = ok_for(s)
    assert not ok and any("below minimum" in x for x in why)


def test_gap_zero_in_up_mode_rejected_as_below_minimum():
    ok, why = ok_for(mk(prev_c="100", o="100", price="100", bid="99.99", ask="100.01"))
    assert not ok and any("below minimum" in x for x in why)


def test_negative_gap_exactly_at_min_in_both_mode_passes():
    s = mk(prev_c="100", o="98", price="98", bid="97.99", ask="98.01")
    assert ok_for(s, S.ScanCriteria(direction="both"))[0]
    assert not ok_for(s)[0]


def test_spread_exactly_at_max_passes():
    s = mk(bid="99.75", ask="100.25", price="100", o="102")
    assert spread_of(s) == D("0.50")
    assert ok_for(s)[0]


def test_spread_just_above_max_rejected():
    s = mk(bid="99.74", ask="100.26", price="100", o="102")
    assert spread_of(s) == D("0.52")
    ok, why = ok_for(s)
    assert not ok and any("spread" in x and "above maximum" in x for x in why)


def test_zero_spread_locked_quote_passes():
    assert ok_for(mk(bid="102.00", ask="102.00"))[0]


def test_crossed_quote_rejected():
    ok, why = ok_for(mk(bid="102.05", ask="102.00"))
    assert not ok and any("spread unavailable" in x for x in why)


def test_zero_quote_values_treated_as_missing():
    ok, why = ok_for(mk(bid="0", ask="102.00"))
    assert not ok and any("spread unavailable" in x for x in why)


# ---------- blocked symbols ----------

def test_blocked_symbol_rejected():
    limits = replace(LIMITS, blocked_symbols=frozenset({"GAPA"}))
    r = run(limits=limits)
    assert "blocked symbol" in rejected(r)["GAPA"]
    assert [c["symbol"] for c in r["candidates"]] == ["GAPB"]


def test_blocked_symbol_not_in_list_still_passes():
    limits = replace(LIMITS, blocked_symbols=frozenset({"ZZZZ"}))
    assert "GAPA" in cands(run(limits=limits))


def test_blocked_matches_case_insensitively():
    limits = replace(LIMITS, blocked_symbols=frozenset({"gapa"}))
    assert "blocked symbol" in rejected(run(limits=limits))["GAPA"]


# ---------- rvol ----------

def test_rvol_from_avg_volume_has_no_note():
    r = run(avg={"GAPA": 300000})
    a = cands(r)["GAPA"]
    assert D(a["rvol"]) == rvol_of(snaps()["GAPA"], 300000) == D("2.00")
    assert a["rvol_note"] is None
    assert cands(r)["GAPB"]["rvol_note"] == S.RVOL_NOTE  # only GAPA had an average


def test_avg_volume_keys_case_insensitive():
    assert cands(run(avg={"gapa": 300000}))["GAPA"]["rvol_note"] is None


@pytest.mark.parametrize("bad", [0, -5, None, True, "300000"])
def test_bad_avg_volume_falls_back_to_previous_day(bad):
    a = cands(run(avg={"GAPA": bad}))["GAPA"]
    assert D(a["rvol"]) == D("3.00") and a["rvol_note"] == S.RVOL_NOTE


def test_avg_volume_changes_sort_order():
    r = run(avg={"GAPB": 50000})  # GAPB rvol 6.00 beats GAPA 3.00
    assert [c["symbol"] for c in r["candidates"]] == ["GAPB", "GAPA"]


def test_tie_on_rvol_sorts_by_gap_desc_then_symbol():
    data = {
        "ZZ": mk(prev_c="100", o="103", price="103", bid="102.99", ask="103.01"),  # gap 3
        "BB": mk(prev_c="100", o="105", price="105", bid="104.99", ask="105.01"),  # gap 5
        "AA": mk(prev_c="100", o="105", price="105", bid="104.99", ask="105.01"),  # gap 5
    }
    r = run(data)
    assert [c["symbol"] for c in r["candidates"]] == ["AA", "BB", "ZZ"]


def test_missing_prev_volume_gives_none_rvol_sorted_last():
    no_prev_v = mk(prev_c="100", o="110", price="110", bid="109.99", ask="110.01")
    del no_prev_v["prevDailyBar"]["v"]
    data = {"NOV": no_prev_v, "HAS": mk()}
    r = run(data)
    assert [c["symbol"] for c in r["candidates"]] == ["HAS", "NOV"]
    assert cands(r)["NOV"]["rvol"] is None


# ---------- malformed input never raises ----------

@pytest.mark.parametrize(
    "snap",
    [
        None,
        "string",
        42,
        [],
        {},
        {"dailyBar": None, "prevDailyBar": None},
        {"dailyBar": {"o": 10, "v": 1000}},  # no prevDailyBar
        {"dailyBar": {"o": 0, "c": 1, "v": 1000}, "prevDailyBar": {"c": 10}},  # zero open
        {"dailyBar": {"o": -1, "c": 1, "v": 1000}, "prevDailyBar": {"c": 10}},
        {"dailyBar": {"o": 10, "c": 10, "v": -5}, "prevDailyBar": {"c": 10}},
        {"dailyBar": {"o": 10, "c": 10, "v": 1.5}, "prevDailyBar": {"c": 10}},
        {"dailyBar": {"o": "NaN", "c": 10, "v": 5}, "prevDailyBar": {"c": 10}},
        {"dailyBar": {"o": "abc", "c": 10, "v": 5}, "prevDailyBar": {"c": 10}},
        {"dailyBar": {"o": True, "c": 10, "v": 5}, "prevDailyBar": {"c": 10}},
        {"dailyBar": {"o": 10, "v": 5}, "prevDailyBar": {"c": 10}},  # no trade, no close
    ],
    ids=repr,
)
def test_malformed_snapshot_rejected_not_raised(snap):
    r = S.scan({"BAD": snap}, CRIT, LIMITS)
    assert r["candidates"] == []
    assert r["rejected"][0]["symbol"] == "BAD" and r["rejected"][0]["reasons"]


@pytest.mark.parametrize("sym", ["", "123", "bad sym", "TOOLONGSYMBOL", "A-B"])
def test_invalid_symbol_in_snapshots_rejected(sym):
    r = S.scan({sym: mk()}, CRIT, LIMITS)
    assert r["candidates"] == [] and r["rejected"][0]["reasons"] == ["invalid symbol"]


@pytest.mark.parametrize("bad", [None, [], "x", 5])
def test_non_dict_snapshots_yield_empty_result(bad):
    r = S.scan(bad, CRIT, LIMITS)
    assert r["candidates"] == [] and r["rejected"] == []


def test_lowercase_symbol_key_is_normalized():
    assert "ABC" in cands(S.scan({"abc": mk()}, CRIT, LIMITS))


def test_falls_back_to_daily_close_when_no_trade():
    s = mk()
    del s["latestTrade"]
    s["dailyBar"]["c"] = "102"
    assert cands(S.scan({"X": s}, CRIT, LIMITS))["X"]["price"] == "102"


# ---------- criteria ----------

def test_criteria_defaults():
    c = S.criteria_from_dict({})
    assert c == S.ScanCriteria()
    assert (c.min_price, c.max_price, c.min_volume) == (D("2.00"), D("500.00"), 100_000)
    assert (c.max_spread_pct, c.min_gap_pct, c.direction) == (D("0.50"), D("2.0"), "up")


def test_criteria_overrides_and_types():
    c = S.criteria_from_dict(
        {"min_price": 5, "max_price": "100.5", "min_volume": "250000",
         "max_spread_pct": 0.25, "min_gap_pct": "3", "direction": "both"}
    )
    assert c.min_price == D("5") and c.max_price == D("100.5")
    assert c.min_volume == 250000 and isinstance(c.min_volume, int)
    assert c.max_spread_pct == D("0.25") and c.min_gap_pct == D("3")
    assert c.direction == "both"


def test_criteria_custom_values_change_outcome():
    r = run(criteria=S.criteria_from_dict({"min_gap_pct": 4.5}))
    assert [c["symbol"] for c in r["candidates"]] == ["GAPA"]  # GAPB gap 4.00 now below


@pytest.mark.parametrize(
    "bad",
    [
        {"nope": 1},
        {"min_price": "abc"},
        {"min_price": None},
        {"min_volume": "x"},
        {"min_volume": None},
        {"direction": "sideways"},
        {"direction": None},
        "not a dict",
        None,
        [],
    ],
    ids=repr,
)
def test_criteria_bad_input_raises_value_error(bad):
    with pytest.raises(ValueError):
        S.criteria_from_dict(bad)


def test_criteria_constructor_rejects_bad_direction():
    with pytest.raises(ValueError):
        S.ScanCriteria(direction="down")


# ---------- validate_symbols ----------

def test_validate_symbols_normalizes_and_dedupes():
    assert S.validate_symbols([" aapl ", "AAPL", "brk.b", "msft"]) == ["AAPL", "BRK.B", "MSFT"]


@pytest.mark.parametrize("bad", ["", "1ABC", "A B", "A,B", "TOOLONGSYMBOL", "A;B", "../x", None, 5])
def test_validate_symbols_rejects(bad):
    with pytest.raises(ValueError):
        S.validate_symbols([bad])


# ---------- fetch_snapshots ----------

class Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SnapOpener:
    """Serves {symbol: snapshot} for the symbols in each request's query string."""

    def __init__(self, wrap=False):
        self.requests = []
        self.wrap = wrap

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        body = {s: {"dailyBar": {"o": 1}} for s in q["symbols"][0].split(",")}
        return Resp({"snapshots": body} if self.wrap else body)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "dummy-key-id")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "dummy-secret")


def many(n):
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return [a + b for a in letters for b in letters][:n]


def req_symbols(req):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
    return q["symbols"][0].split(",")


def test_fetch_chunks_at_100(keys):
    syms = many(150)
    op = SnapOpener()
    out = S.fetch_snapshots(syms, opener=op)
    assert len(op.requests) == 2
    assert [len(req_symbols(r)) for r in op.requests] == [100, 50]
    assert req_symbols(op.requests[0]) + req_symbols(op.requests[1]) == syms
    assert set(out) == set(syms)


def test_fetch_exactly_100_is_one_request(keys):
    op = SnapOpener()
    S.fetch_snapshots(many(100), opener=op)
    assert len(op.requests) == 1


def test_fetch_101_is_two_requests(keys):
    op = SnapOpener()
    S.fetch_snapshots(many(101), opener=op)
    assert [len(req_symbols(r)) for r in op.requests] == [100, 1]


def test_fetch_unwraps_snapshots_key(keys):
    out = S.fetch_snapshots(["AAPL", "MSFT"], opener=SnapOpener(wrap=True))
    assert set(out) == {"AAPL", "MSFT"}


def test_fetch_dedupes_and_uppercases(keys):
    op = SnapOpener()
    S.fetch_snapshots(["aapl", "AAPL", "msft"], opener=op)
    assert req_symbols(op.requests[0]) == ["AAPL", "MSFT"]


def test_fetch_sends_auth_headers_and_feed(keys):
    op = SnapOpener()
    S.fetch_snapshots(["AAPL"], feed="sip", opener=op)
    h = {k.lower(): v for k, v in op.requests[0].header_items()}
    assert h["apca-api-key-id"] == "dummy-key-id"
    assert h["apca-api-secret-key"] == "dummy-secret"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(op.requests[0].full_url).query)
    assert q["feed"] == ["sip"]


def test_fetch_missing_keys_raises(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    op = SnapOpener()
    with pytest.raises(RuntimeError):
        S.fetch_snapshots(["AAPL"], opener=op)
    assert op.requests == []


def test_fetch_one_key_missing_raises(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "dummy-key-id")
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        S.fetch_snapshots(["AAPL"], opener=SnapOpener())


@pytest.mark.parametrize("bad", ["", "A B", "A,B", "123", "AAPL&feed=sip", "TOOLONGSYMBOL"])
def test_fetch_invalid_symbol_raises_before_any_request(keys, bad):
    op = SnapOpener()
    with pytest.raises(ValueError):
        S.fetch_snapshots(["AAPL", bad], opener=op)
    assert op.requests == []


def test_fetch_error_payload_raises(keys):
    def op(req, timeout=None):
        return Resp({"message": "forbidden"})

    with pytest.raises(RuntimeError, match="error payload"):
        S.fetch_snapshots(["AAPL"], opener=op)


def test_fetch_list_body_rejected(keys):
    def op(req, timeout=None):
        return Resp([1, 2])

    with pytest.raises(RuntimeError, match="unexpected"):
        S.fetch_snapshots(["AAPL"], opener=op)


def test_fetch_without_opener_hits_guard(keys):
    with pytest.raises(RuntimeError, match="network access in tests"):
        S.fetch_snapshots(["AAPL"])


def test_fetch_then_scan_roundtrip(keys):
    data = json.loads((FIX / "alpaca_snapshots.json").read_text())

    def op(req, timeout=None):
        return Resp({k: v for k, v in data.items() if k in req_symbols(req)})

    got = S.fetch_snapshots(["GAPA", "GAPB", "DOWN"], opener=op)
    r = S.scan(got, CRIT, LIMITS)
    assert [c["symbol"] for c in r["candidates"]] == ["GAPA", "GAPB"]


# ---------- rounding at the limits: raw values decide, not the 0.01-rounded ones ----------

def gap_snap(open_):
    return mk(prev_c="100", o=open_, price="100", bid="99.99", ask="100.01")


@pytest.mark.parametrize("open_", ["101.995", "101.9999", "101.99"])
def test_gap_just_under_min_is_rejected_even_if_it_rounds_up(open_):
    # 1.995 and 1.9999 would round to 2.00 but are below the 2.0 minimum.
    ok, why = ok_for(gap_snap(open_))
    assert not ok and any("below minimum" in x for x in why)


@pytest.mark.parametrize("open_", ["102", "102.0001"])
def test_gap_at_or_above_min_passes(open_):
    ok, why = ok_for(gap_snap(open_))
    assert ok, why


def test_negative_gap_rounding_in_both_mode():
    both = S.ScanCriteria(direction="both")
    assert not ok_for(gap_snap("98.005"), both)[0]  # -1.995%
    assert ok_for(gap_snap("98"), both)[0]  # -2.00%


def test_rounded_gap_still_reported_to_two_places():
    r = S.scan({"X": gap_snap("102.004")}, CRIT, LIMITS)
    assert r["candidates"][0]["gap_pct"] == "2.00"


def spread_snap(bid, ask):
    return mk(prev_c="100", o="102", price="100", bid=bid, ask=ask)


def test_spread_just_over_max_rejected_even_if_it_rounds_to_max():
    ok, why = ok_for(spread_snap("99.748", "100.252"))  # 0.504% -> would print 0.50
    assert not ok and any("above maximum" in x for x in why)


def test_spread_at_max_passes_and_just_over_rejected():
    assert ok_for(spread_snap("99.75", "100.25"))[0]  # exactly 0.50%
    assert not ok_for(spread_snap("99.7499", "100.2501"))[0]  # 0.502%


def test_spread_just_under_max_passes():
    assert ok_for(spread_snap("99.752", "100.248"))[0]  # 0.496%


# ---------- criteria validation ----------

MONEY = ["min_price", "max_price", "max_spread_pct", "min_gap_pct"]


@pytest.mark.parametrize("field", MONEY)
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "-0.01", "-1"])
def test_criteria_rejects_nan_inf_negative(field, bad):
    with pytest.raises(ValueError):
        S.criteria_from_dict({field: bad})


def test_criteria_accepts_zero_boundaries():
    c = S.criteria_from_dict({"min_price": "0", "max_spread_pct": "0", "min_gap_pct": "0"})
    assert (c.min_price, c.max_spread_pct, c.min_gap_pct) == (D("0"), D("0"), D("0"))
    assert S.criteria_from_dict({"min_price": "0", "max_price": "0"}).max_price == D("0")


def test_criteria_min_price_above_max_price_rejected():
    with pytest.raises(ValueError, match="min_price"):
        S.criteria_from_dict({"min_price": "600"})  # default max 500
    with pytest.raises(ValueError):
        S.criteria_from_dict({"min_price": "10", "max_price": "9.99"})


def test_criteria_min_equals_max_price_allowed():
    c = S.criteria_from_dict({"min_price": "10", "max_price": "10"})
    assert c.min_price == c.max_price == D("10")


@pytest.mark.parametrize("bad", [1.9, "1.9", True, False, -1, "-1", "NaN", "Infinity", None])
def test_criteria_min_volume_must_be_non_negative_whole_number(bad):
    with pytest.raises(ValueError):
        S.criteria_from_dict({"min_volume": bad})


@pytest.mark.parametrize("good,expected", [(0, 0), (5, 5), ("100000", 100000), (1000.0, 1000)])
def test_criteria_min_volume_whole_numbers_accepted(good, expected):
    assert S.criteria_from_dict({"min_volume": good}).min_volume == expected


def test_constructor_validates_directly():
    for kwargs in (
        {"min_price": D("NaN")},
        {"max_price": D("Infinity")},
        {"min_gap_pct": D("-1")},
        {"max_spread_pct": D("-0.5")},
        {"min_price": D("600")},
        {"min_volume": 1.9},
        {"min_volume": True},
        {"min_volume": -1},
        {"min_price": 2.0},  # not a Decimal
    ):
        with pytest.raises(ValueError):
            S.ScanCriteria(**kwargs)
    S.ScanCriteria(min_volume=0, min_gap_pct=D("0"))  # boundary values are fine


# ---------- numeric overflow ----------

def test_overflow_snapshot_rejected_not_raised():
    r = S.scan({"X": mk(prev_c="1E-30", o="1E+10")}, CRIT, LIMITS)
    assert r["candidates"] == []
    assert "numeric overflow" in r["rejected"][0]["reasons"][0]


def test_overflow_in_spread_rejected_not_raised():
    r = S.scan({"X": mk(bid="1E-30", ask="1E+999999")}, CRIT, LIMITS)
    assert r["candidates"] == [] and r["rejected"][0]["symbol"] == "X"


def test_large_but_valid_values_do_not_trip_overflow():
    r = S.scan({"X": mk(prev_c="100", o="102", v=10**12)}, CRIT, LIMITS)
    assert [c["symbol"] for c in r["candidates"]] == ["X"]


# ---------- freshness (today) ----------

TODAY = date(2026, 11, 3)


def with_t(t, **kw):
    s = mk(**kw)
    if t is not None:
        s["dailyBar"]["t"] = t
    return s


def fresh_scan(snap, today=TODAY):
    r = S.scan({"X": snap}, CRIT, LIMITS, today=today)
    return bool(r["candidates"]), rejected(r).get("X", [])


def test_today_matching_et_date_passes():
    assert fresh_scan(with_t("2026-11-03T05:00:00Z"))[0]


@pytest.mark.parametrize("t", ["2026-11-02T05:00:00Z", None, "garbage", "", 12345,
                               "2026-11-03T05:00:00"])
def test_today_stale_or_unusable_bar_rejected(t):
    ok, why = fresh_scan(with_t(t))
    assert not ok and any("stale daily bar" in x for x in why)


def test_today_uses_et_date_not_utc_date_late_evening():
    # 03:30Z on Nov 4 is 22:30 EST on Nov 3: same ET day as today, different UTC day.
    assert fresh_scan(with_t("2026-11-04T03:30:00Z"))[0]


def test_today_uses_et_date_not_utc_date_early_morning():
    # 03:30Z on Nov 3 is 22:30 EST on Nov 2: UTC date matches today but the ET date is stale.
    ok, why = fresh_scan(with_t("2026-11-03T03:30:00Z"))
    assert not ok and any("stale daily bar" in x for x in why)


def test_today_edt_midnight_bar_matches():
    assert fresh_scan(with_t("2026-07-06T04:00:00Z"), today=date(2026, 7, 6))[0]  # 00:00 EDT


def test_without_today_no_freshness_check():
    for t in ("2020-01-01T05:00:00Z", None, "garbage"):
        r = S.scan({"X": with_t(t)}, CRIT, LIMITS)
        assert len(r["candidates"]) == 1


def test_today_with_fixture():
    r = S.scan(snaps(), CRIT, LIMITS, today=date(2026, 11, 3))
    assert [c["symbol"] for c in r["candidates"]] == ["GAPA", "GAPB"]
    r2 = S.scan(snaps(), CRIT, LIMITS, today=date(2026, 11, 4))
    assert r2["candidates"] == []
    assert any("stale daily bar" in x for x in rejected(r2)["GAPA"])


# ---------- duplicate symbols and rejected ordering ----------

def test_duplicate_symbol_first_evaluated_rest_rejected():
    r = S.scan({"abc": mk(), "ABC": mk(o="150", price="150", bid="149.99", ask="150.01")},
               CRIT, LIMITS)
    assert [c["symbol"] for c in r["candidates"]] == ["ABC"]
    assert r["candidates"][0]["price"] == "102"  # the first entry, "abc", was the one evaluated
    assert r["rejected"] == [{"symbol": "ABC", "reasons": ["duplicate symbol"]}]


def test_duplicate_symbol_when_first_is_bad_still_rejects_second():
    r = S.scan({"ABC": None, " abc ": mk()}, CRIT, LIMITS)
    assert r["candidates"] == []
    assert rejected(r)["ABC"] in (["null or malformed snapshot"], ["duplicate symbol"])
    assert len(r["rejected"]) == 2
    reasons = sorted(x["reasons"][0] for x in r["rejected"])
    assert reasons == ["duplicate symbol", "null or malformed snapshot"]


def test_rejected_sorted_by_symbol():
    data = {"ZZ": None, "9": mk(), "AA": None, "MM": None}
    r = S.scan(data, CRIT, LIMITS)
    assert [x["symbol"] for x in r["rejected"]] == ["9", "AA", "MM", "ZZ"]
    assert r["rejected"][0]["reasons"] == ["invalid symbol"]


def test_no_duplicates_in_fixture():
    r = run()
    syms = [x["symbol"] for x in r["rejected"]]
    assert syms == sorted(syms)
