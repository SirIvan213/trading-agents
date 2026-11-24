"""pipeline/universe_check.py: the candidate check and the universe writer. Fixtures only; the
repo's network guard (tests/conftest.py) is in force, and the fakes below stand in for the
Alpaca asset endpoint, SEC EDGAR and Alpaca daily bars."""
from __future__ import annotations

import copy
import json
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import pipeline.universe_check as uc
from pipeline.budget import config_from_dict
from tests.order_helpers import submissions_202

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 23, 10, 30, tzinfo=NY)  # a Monday; the fixture's last bar is Fri
FIX = Path(__file__).parent / "fixtures"
BARS = json.loads((FIX / "universe_check" / "daily_bars.json").read_text(encoding="utf-8"))
ASSET = json.loads((FIX / "alpaca_asset_tsla.json").read_text(encoding="utf-8"))
EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "run_day.example.json"


class FakeData:
    """listing() and submissions() as pipeline.data.LiveData has them. Per-symbol overrides:
    an exchange, an exception, a missing SEC filer, days since the last item 2.02."""

    def __init__(self, exchange=None, listing_error=None, sec_error=None, no_filer=(),
                 days_ago=None, not_active=()):
        self.exchange = exchange or {}
        self.listing_error = listing_error or {}
        self.sec_error = sec_error or {}
        self.no_filer = set(no_filer)
        self.days_ago = days_ago or {}
        self.not_active = set(not_active)
        self.calls: list[tuple] = []

    def listing(self, symbol):
        self.calls.append(("listing", symbol))
        if symbol in self.listing_error:
            raise self.listing_error[symbol]
        asset = {**ASSET, "symbol": symbol, "exchange": self.exchange.get(symbol, "NASDAQ")}
        if symbol in self.not_active:
            asset["status"] = "inactive"
        return asset

    def submissions(self, symbol):
        self.calls.append(("submissions", symbol))
        if symbol in self.sec_error:
            raise self.sec_error[symbol]
        if symbol in self.no_filer:
            return None
        return submissions_202(NOW, self.days_ago.get(symbol, 30), symbol=symbol)


def bars_for(close=None, volume=None, extra=()):
    """The fixture's 30 daily bars (last 20 completed ones average 3,019,500 shares, last
    close 130), with the last close and every volume optionally replaced."""
    out = copy.deepcopy(BARS["bars"]) + list(extra)
    for b in out[:30]:
        if volume is not None:
            b["v"] = volume
    if close is not None:
        out[29]["c"] = close
    return {"bars": out, "symbol": "X", "next_page_token": None}


def daily(by_symbol=None, error=None):
    by_symbol = by_symbol or {}
    seen: list[tuple] = []

    def fetch(symbol, start, end, feed):
        seen.append((symbol, start, end, feed))
        if error and symbol in error:
            raise error[symbol]
        return by_symbol.get(symbol, bars_for())

    fetch.seen = seen
    return fetch


def row(symbol="AAA", data=None, fetch=None, feed="sip"):
    return uc.check_symbol(data or FakeData(), fetch or daily(), symbol, NOW, feed)


# ---- the candidates -------------------------------------------------------------------------

def test_the_candidate_list_is_the_one_given():
    """Ivan called it 60; counted, it is 62 distinct tickers (19 + 15 + 16 + 12)."""
    assert len(uc.CANDIDATES) == 62 and len(set(uc.CANDIDATES)) == 62
    assert uc.CANDIDATES[0] == "AMD" and uc.CANDIDATES[-1] == "META"
    assert {"XYZ", "U", "AI", "W", "RUN", "ON"} <= set(uc.CANDIDATES)


# ---- each criterion ---------------------------------------------------------------------------

def test_a_symbol_that_meets_every_criterion_passes():
    r = row()
    assert r["status"] == uc.PASS and r["reasons"] == []
    assert r["exchange"] == "NASDAQ" and r["close"] == D("130")
    assert r["avg_volume"] == D("3019500") and r["last_item_202"] is not None


@pytest.mark.parametrize("exchange, ok", [("NASDAQ", True), ("NYSE", True), ("AMEX", True),
                                          ("ARCA", False), ("BATS", False), ("OTC", False)])
def test_the_listing_must_be_nasdaq_nyse_or_amex(exchange, ok):
    r = row(data=FakeData(exchange={"AAA": exchange}))
    assert (r["status"] == uc.PASS) is ok
    if not ok:
        assert any(f"primary listing is {exchange}" in x for x in r["reasons"])


def test_an_inactive_asset_fails():
    r = row(data=FakeData(not_active={"AAA"}))
    assert r["status"] == uc.FAIL and any("not an active asset" in x for x in r["reasons"])


def test_an_unknown_symbol_is_a_fail_not_an_error():
    r = row(data=FakeData(listing_error={"AAA": RuntimeError("HTTP 404 from /v2/assets/AAA")}))
    assert r["status"] == uc.FAIL and "not an Alpaca asset (HTTP 404)" in r["reasons"]


@pytest.mark.parametrize("days, ok", [(30, True), (99, True), (101, False), (400, False)])
def test_it_must_have_reported_in_the_last_100_days(days, ok):
    r = row(data=FakeData(days_ago={"AAA": days}))
    assert (r["status"] == uc.PASS) is ok
    if not ok:
        assert any("no 8-K item 2.02 in the last 100 days" in x for x in r["reasons"])


def test_a_symbol_with_no_sec_filer_fails():
    r = row(data=FakeData(no_filer=["AAA"]))
    assert r["status"] == uc.FAIL and any("2.02" in x for x in r["reasons"])


@pytest.mark.parametrize("close, ok", [("10", True), ("9.99", False), ("500", True),
                                       ("500.01", False), ("130", True)])
def test_the_last_close_must_be_between_10_and_500(close, ok):
    r = row(fetch=daily({"AAA": bars_for(close=close)}))
    assert (r["status"] == uc.PASS) is ok
    if not ok:
        assert any("outside $10-$500" in x for x in r["reasons"])


@pytest.mark.parametrize("volume, ok", [(2_000_000, True), (1_999_999, False),
                                        (5_000_000, True)])
def test_the_20_day_average_volume_must_be_at_least_2m(volume, ok):
    r = row(fetch=daily({"AAA": bars_for(volume=volume)}))
    assert (r["status"] == uc.PASS) is ok
    if not ok:
        assert any("below 2,000,000" in x for x in r["reasons"])


def test_only_the_last_20_completed_sessions_count():
    """Ten sessions of tiny volume before the last 20 do not matter."""
    bars = bars_for()
    for b in bars["bars"][:10]:
        b["v"] = 1
    assert row(fetch=daily({"AAA": bars})) ["status"] == uc.PASS
    bars = bars_for()
    for b in bars["bars"][-10:]:  # inside the window: the average drops under 2M
        b["v"] = 1
    assert row(fetch=daily({"AAA": bars}))["status"] == uc.FAIL


def test_todays_partial_bar_is_never_used():
    today = {"t": "2026-11-23T05:00:00Z", "o": 5, "h": 5, "l": 5, "c": 5, "v": 10}
    r = row(fetch=daily({"AAA": bars_for(extra=[today])}))
    assert r["status"] == uc.PASS and r["close"] == D("130")  # not 5, not diluted


def test_fewer_than_20_completed_sessions_fails():
    bars = bars_for()
    bars["bars"] = bars["bars"][-10:]
    r = row(fetch=daily({"AAA": bars}))
    assert r["status"] == uc.FAIL and "only 10 completed daily bars (need 20)" in r["reasons"]


def test_every_failing_reason_is_listed():
    r = row(data=FakeData(exchange={"AAA": "OTC"}, days_ago={"AAA": 300}),
            fetch=daily({"AAA": bars_for(close="5", volume=100)}))
    assert r["status"] == uc.FAIL and len(r["reasons"]) == 4


# ---- a failed request is an ERROR, never a pass or a fail -----------------------------------

@pytest.mark.parametrize("kwargs, text", [
    (dict(data=FakeData(listing_error={"AAA": RuntimeError("network error for /v2/assets")})),
     "listing request failed"),
    (dict(data=FakeData(sec_error={"AAA": RuntimeError("HTTP 503 from /submissions")})),
     "SEC request failed"),
    (dict(fetch=daily(error={"AAA": RuntimeError("HTTP 403 from /v2/stocks/AAA/bars")})),
     "daily bars request failed"),
])
def test_a_failed_request_is_an_error(kwargs, text):
    r = row(**kwargs)
    assert r["status"] == uc.ERROR and any(text in x for x in r["reasons"])


def test_a_malformed_bar_is_an_error():
    r = row(fetch=daily({"AAA": {"bars": [{"t": "2026-11-20T04:00:00Z", "c": "x", "v": 1}]}}))
    assert r["status"] == uc.ERROR


def test_an_error_never_leaks_a_key(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "SENTINEL-KEY-123456")
    r = row(fetch=daily(error={"AAA": ValueError("SENTINEL-KEY-123456 in a message")}))
    assert "SENTINEL-KEY" not in json.dumps(r["reasons"])  # only the type name is shown


# ---- the bars request: free source, feed, window ------------------------------------------------

def test_daily_bars_are_requested_over_completed_sessions_on_the_sip_feed():
    fetch = daily()
    row(fetch=fetch)
    symbol, start, end, feed = fetch.seen[0]
    assert (symbol, feed) == ("AAA", "sip")
    assert end < NOW and (NOW - end).total_seconds() >= 15 * 60  # older than the free delay
    assert (NOW - start).days == 50


def test_the_live_fetch_asks_for_1day_bars(monkeypatch):
    seen = {}
    monkeypatch.setattr(uc, "fetch_bars", lambda *a, **k: seen.update(a=a, k=k) or {})
    uc.live_fetch_daily("AAA", NOW, NOW, "sip")
    assert seen["k"] == {"timeframe": "1Day", "feed": "sip"}


# ---- the table ----------------------------------------------------------------------------------

def test_the_table_shows_each_symbols_result_and_reason():
    rows = [row("AAA"), row("BBB", data=FakeData(exchange={"BBB": "OTC"}))]
    text = uc.render(rows, "sip", NOW)
    assert "daily bars feed: sip" in text
    lines = {ln.split()[0]: ln for ln in text.splitlines() if ln[:3] in ("AAA", "BBB")}
    assert "PASS" in lines["AAA"] and "130.00" in lines["AAA"] and "3,019,500" in lines["AAA"]
    assert "FAIL" in lines["BBB"] and "primary listing is OTC" in lines["BBB"]
    assert "1 pass, 1 fail, 0 error (of 2)" in text


# ---- the writer ---------------------------------------------------------------------------------

def make_config(tmp_path, **extra):
    cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    cfg.update(extra)
    path = tmp_path / "config" / "run_day.json"
    path.parent.mkdir()
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path, cfg


def test_write_replaces_only_the_universe_and_backs_up_first(tmp_path):
    path, cfg = make_config(tmp_path, scan_time="09:45", cycle_minutes=7)
    before = path.read_bytes()
    backup = uc.write_universe(path, ["AMD", "MU"])
    assert backup.name == "run_day.json.bak" and backup.read_bytes() == before
    new = json.loads(path.read_text(encoding="utf-8"))
    assert new["universe"] == ["AMD", "MU"]
    assert {k: v for k, v in new.items() if k != "universe"} == {
        k: v for k, v in cfg.items() if k != "universe"}  # caps and times kept
    assert list(new) == list(cfg)  # key order kept
    config_from_dict(new)  # still a valid run_day config


@pytest.mark.parametrize("content", [None, "{not json", "[]"])
def test_write_refuses_a_missing_or_bad_config_and_changes_nothing(tmp_path, content):
    path = tmp_path / "run_day.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(uc.RunDayConfigError):
        uc.write_universe(path, ["AMD"])
    assert not (tmp_path / "run_day.json.bak").exists()
    assert (path.read_text(encoding="utf-8") if path.exists() else None) == content


def test_write_refuses_a_universe_run_day_would_refuse(tmp_path):
    path, _ = make_config(tmp_path)
    before = path.read_bytes()
    with pytest.raises(uc.RunDayConfigError):
        uc.write_universe(path, [])
    assert path.read_bytes() == before and not path.with_name("run_day.json.bak").exists()


def test_write_never_raises_a_cap(tmp_path):
    path, cfg = make_config(tmp_path)
    uc.write_universe(path, ["AMD"])
    assert json.loads(path.read_text(encoding="utf-8"))["caps"] == cfg["caps"]


# ---- main ---------------------------------------------------------------------------------------

def run(tmp_path, *argv, data=None, fetch=None, config=True):
    path = None
    if config:
        path, _ = make_config(tmp_path)
    code = uc.main(["--no-dotenv", *argv], data=data or FakeData(), fetch_daily=fetch or daily(),
                   now=NOW, config_path=path or tmp_path / "config" / "run_day.json")
    return code, path


def test_without_write_nothing_changes(tmp_path, capsys):
    path, _ = make_config(tmp_path)
    before = path.read_bytes()
    code = uc.main(["--no-dotenv", "--symbols", "AMD", "MU"], data=FakeData(),
                   fetch_daily=daily(), now=NOW, config_path=path)
    assert code == 0
    assert path.read_bytes() == before and not path.with_name("run_day.json.bak").exists()
    assert "dry run: nothing was changed" in capsys.readouterr().out


def test_with_write_the_passing_symbols_become_the_universe_in_candidate_order(tmp_path,
                                                                               capsys):
    data = FakeData(exchange={"MU": "OTC"})
    code, path = run(tmp_path, "--write", "--symbols", "AMD", "MU", "INTC", data=data)
    assert code == 0
    assert json.loads(path.read_text(encoding="utf-8"))["universe"] == ["AMD", "INTC"]
    assert path.with_name("run_day.json.bak").exists()
    assert "wrote 2 symbols" in capsys.readouterr().out


def test_the_whole_candidate_list_is_checked_by_default(tmp_path):
    data = FakeData()
    code, path = run(tmp_path, "--write", data=data)
    assert code == 0
    assert [c[1] for c in data.calls if c[0] == "listing"] == list(uc.CANDIDATES)
    assert json.loads(path.read_text(encoding="utf-8"))["universe"] == list(uc.CANDIDATES)


def test_an_error_refuses_the_write_and_changes_nothing(tmp_path, capsys):
    path, _ = make_config(tmp_path)
    before = path.read_bytes()
    code = uc.main(["--no-dotenv", "--write", "--symbols", "AMD", "MU"], data=FakeData(),
                   fetch_daily=daily(error={"MU": RuntimeError("HTTP 500 from /bars")}),
                   now=NOW, config_path=path)
    assert code == uc.INCOMPLETE
    assert path.read_bytes() == before and not path.with_name("run_day.json.bak").exists()
    err = capsys.readouterr().err
    assert "MU" in err and "Nothing was written" in err


def test_an_error_without_write_is_reported_but_writes_nothing(tmp_path, capsys):
    code, _ = run(tmp_path, "--symbols", "AMD",
                  fetch=daily(error={"AMD": RuntimeError("HTTP 500 from /bars")}))
    assert code == uc.INCOMPLETE and "ERROR" in capsys.readouterr().out


def test_nothing_passing_leaves_the_universe_alone(tmp_path, capsys):
    path, _ = make_config(tmp_path)
    before = path.read_bytes()
    code = uc.main(["--no-dotenv", "--write", "--symbols", "AMD"],
                   data=FakeData(exchange={"AMD": "OTC"}), fetch_daily=daily(), now=NOW,
                   config_path=path)
    assert code == uc.NOTHING_PASSED and path.read_bytes() == before
    assert "no symbol passed" in capsys.readouterr().err


def test_write_without_a_config_is_refused_before_any_request(tmp_path, capsys):
    data = FakeData()
    code = uc.main(["--no-dotenv", "--write", "--symbols", "AMD"], data=data,
                   fetch_daily=daily(), now=NOW, config_path=tmp_path / "missing.json")
    assert code == uc.REFUSED and data.calls == []
    assert "not found" in capsys.readouterr().err


@pytest.mark.parametrize("symbols", [["amd!"], ["TOOLONGSYM"], [""]])
def test_a_bad_symbol_is_refused_before_any_request(tmp_path, symbols):
    data = FakeData()
    code = uc.main(["--no-dotenv", "--symbols", *symbols], data=data, fetch_daily=daily(),
                   now=NOW, config_path=tmp_path / "x.json")
    assert code == uc.REFUSED and data.calls == []


def test_feed_iex_is_passed_through(tmp_path):
    fetch = daily()
    run(tmp_path, "--feed", "iex", "--symbols", "AMD", fetch=fetch)
    assert fetch.seen[0][3] == "iex"


# ---- free sources only, no scan changes -----------------------------------------------

def test_it_imports_no_billed_source_no_agent_and_no_broker():
    import ast
    tree = ast.parse(Path(uc.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    banned = ("anthropic", "agents", "tools.broker", "pipeline.run_day", "pipeline.send",
              "pipeline.metering", "tools.earnings", "tools.scan", "pipeline.market")
    assert not [n for n in names if any(n == b or n.startswith(b + ".") for b in banned)]


def test_the_live_data_object_is_never_asked_for_a_billed_source(tmp_path):
    """The check only calls listing() and submissions() on its data object (plus the bars
    function): no earnings_calendar, no av_earnings_calendar, no news."""
    class Strict(FakeData):
        def __getattr__(self, name):
            raise AssertionError(f"universe_check used data.{name}")

    code, _ = run(tmp_path, "--symbols", "AMD", data=Strict())
    assert code == 0
