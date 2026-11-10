"""#168: trade only symbols listed on Nasdaq, NYSE or NYSE American until the halt feed is seen
to cover NYSE Arca and Cboe. The listing comes from Alpaca's asset endpoint (fake openers and
fixtures here; no network). The scan rejects other listings with a reason; the event gate is
the order path's backstop (UNKNOWN, so check_order blocks)."""
import io
import json
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from pipeline.data import LiveData
from pipeline.events import events_from, listing_input
from pipeline.scenarios import ScenarioData, load_scenario
from pipeline.smoke_events import run_live
from tests.fakes import FakeMarketData, asset_reply, av_csv, fresh_halt_feed, load_fixture
from tests.order_helpers import NY, gate_for, listing_ok, submissions_202, verdict_for
from tests.test_tool_registry import make
from tools.config import load_limits
from tools.listing import (
    ALLOWED_LISTINGS,
    ASSETS_URL,
    fetch_asset,
    listing_problem,
    parse_asset,
)
from tools.scan import ScanCriteria, scan

KEY, SECRET = "SENTINEL-ALPACA-ID-1", "SENTINEL-ALPACA-SECRET-2"
NOW = datetime(2026, 11, 9, 10, 30, tzinfo=NY)
OTHER_VENUES = ["ARCA", "NYSEARCA", "BATS", "OTC", "IEX", "CBOE"]


@pytest.fixture(autouse=True)
def alpaca_keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", KEY)
    monkeypatch.setenv("ALPACA_SECRET_KEY", SECRET)


class Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()
        self.headers = {}

    def read(self, n=None):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(body=None, exc=None):
    seen = []

    def op(req, timeout=None):
        seen.append(req)
        if exc is not None:
            raise exc
        return Resp(body)

    op.seen = seen
    return op


# ======================= fetch_asset =======================

def test_the_request_goes_to_the_paper_host_with_the_key_headers():
    op = opener(asset_reply("AAPL"))
    assert fetch_asset("AAPL", opener=op)["exchange"] == "NASDAQ"
    req = op.seen[0]
    assert req.full_url == f"{ASSETS_URL}/AAPL"
    assert urllib.parse.urlsplit(req.full_url).netloc == "paper-api.alpaca.markets"
    assert req.get_header("Apca-api-key-id") == KEY
    assert req.get_header("Apca-api-secret-key") == SECRET


def test_a_share_class_symbol_keeps_its_dot_in_the_path():
    op = opener(asset_reply("BRK.B", "NYSE"))
    fetch_asset("BRK.B", opener=op)
    assert op.seen[0].full_url == f"{ASSETS_URL}/BRK.B"


@pytest.mark.parametrize("unset", ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"])
def test_a_missing_key_makes_no_request(monkeypatch, unset):
    monkeypatch.setenv(unset, "  ")
    op = opener(asset_reply("AAPL"))
    with pytest.raises(RuntimeError, match="must be set"):
        fetch_asset("AAPL", opener=op)
    assert op.seen == []


def test_an_unknown_symbol_is_a_safe_runtime_error():
    err = urllib.error.HTTPError(f"{ASSETS_URL}/ZZZZ", 404, "x", {}, io.BytesIO(b""))
    with pytest.raises(RuntimeError) as exc:
        fetch_asset("ZZZZ", opener=opener(exc=err))
    assert str(exc.value) == "HTTP 404 from /v2/assets/ZZZZ"
    assert KEY not in str(exc.value) and SECRET not in str(exc.value)


@pytest.mark.parametrize("bad", ["..", "A/B", "", "TOOLONGX"], ids=repr)
def test_an_invalid_symbol_makes_no_request(bad):
    op = opener(asset_reply("AAPL"))
    with pytest.raises(ValueError, match="invalid symbol"):
        fetch_asset(bad, opener=op)
    assert op.seen == []


def test_a_reply_that_is_not_an_object_is_refused():
    with pytest.raises(RuntimeError, match="unexpected asset response shape"):
        fetch_asset("AAPL", opener=opener([asset_reply("AAPL")]))


# ======================= parse_asset =======================

def test_a_good_asset_parses_to_its_exchange():
    assert parse_asset(asset_reply("aapl", "NYSE"), "AAPL") == {
        "ok": True, "symbol": "AAPL", "exchange": "NYSE"}


@pytest.mark.parametrize("reply,why", [
    (None, "not an object"),
    ([], "not an object"),
    (asset_reply("MSFT"), "is for 'MSFT', not AAPL"),
    ({**asset_reply("AAPL"), "exchange": ""}, "has no exchange"),
    ({**asset_reply("AAPL"), "exchange": None}, "has no exchange"),
    ({k: v for k, v in asset_reply("AAPL").items() if k != "exchange"}, "has no exchange"),
    (asset_reply("AAPL", status="inactive"), "not an active asset"),
], ids=["none", "list", "other-symbol", "empty-exchange", "null-exchange", "no-exchange",
        "inactive"])
def test_a_bad_asset_reply_is_not_ok(reply, why):
    out = parse_asset(reply, "AAPL")
    assert out["ok"] is False and why in out["reason"]


def test_a_lowercase_padded_exchange_is_normalised_and_allowed():
    out = parse_asset({**asset_reply("AAPL"), "exchange": " nasdaq "}, "AAPL")
    assert out == {"ok": True, "symbol": "AAPL", "exchange": "NASDAQ"}
    assert listing_problem(out, "AAPL") is None


def test_an_invalid_symbol_is_not_ok():
    assert parse_asset(asset_reply("AAPL"), "AA PL!")["ok"] is False


# ======================= listing_problem =======================

@pytest.mark.parametrize("code", ["NASDAQ", "NYSE", "AMEX"])
def test_nasdaq_nyse_and_nyse_american_are_allowed(code):
    assert code in ALLOWED_LISTINGS
    assert listing_problem(listing_ok("AAPL", code), "AAPL") is None


@pytest.mark.parametrize("code", OTHER_VENUES)
def test_blocked_any_other_venue_is_a_problem_naming_it(code):
    why = listing_problem(listing_ok("SPY", code), "SPY")
    assert why == (f"listed on {code}: only Nasdaq, NYSE and NYSE American listings until the "
                   "halt feed is seen to cover NYSE Arca and Cboe (#168)")


@pytest.mark.parametrize("listing", [None, {}, {"ok": False, "reason": "HTTP 503 from /x"},
                                     "NASDAQ", {"ok": "yes", "symbol": "AAPL", "exchange": "NYSE"}],
                         ids=["none", "empty", "failed", "string", "ok-not-true"])
def test_blocked_a_missing_or_failed_listing_is_a_problem(listing):
    assert listing_problem(listing, "AAPL").startswith("listing exchange unknown")


def test_blocked_a_listing_for_another_symbol_is_a_problem():
    assert listing_problem(listing_ok("MSFT"), "AAPL") == (
        "listing exchange unknown (listing is for another symbol)")


def test_share_class_spellings_match():
    assert listing_problem(listing_ok("BRK.B", "NYSE"), "BRK B") is None


# ======================= the scan =======================

def snaps():
    return load_fixture("alpaca_snapshots.json")


def test_without_listings_the_pure_scan_is_unchanged():
    out = scan(snaps(), ScanCriteria(), load_limits())
    assert [c["symbol"] for c in out["candidates"]] == ["GAPA", "GAPB"]


def test_blocked_the_scan_rejects_an_arca_listing_with_a_reason():
    listings = {"GAPA": listing_ok("GAPA", "ARCA"), "GAPB": listing_ok("GAPB", "NYSE")}
    out = scan(snaps(), ScanCriteria(), load_limits(), listings=listings)
    assert [c["symbol"] for c in out["candidates"]] == ["GAPB"]
    (gapa,) = [r for r in out["rejected"] if r["symbol"] == "GAPA"]
    assert gapa["reasons"] == [listing_problem(listings["GAPA"], "GAPA")]


def test_nyse_american_passes_the_scan():
    listings = {s: listing_ok(s, "AMEX") for s in ("GAPA", "GAPB")}
    out = scan(snaps(), ScanCriteria(), load_limits(), listings=listings)
    assert [c["symbol"] for c in out["candidates"]] == ["GAPA", "GAPB"]


def test_blocked_a_symbol_missing_from_the_listings_is_rejected():
    out = scan(snaps(), ScanCriteria(), load_limits(), listings={"GAPB": listing_ok("GAPB")})
    (gapa,) = [r for r in out["rejected"] if r["symbol"] == "GAPA"]
    assert gapa["reasons"] == ["listing exchange unknown (no listing)"]


def test_a_listing_problem_is_added_to_other_reasons():
    listings = {"DOWN": listing_ok("DOWN", "OTC")}
    out = scan(snaps(), ScanCriteria(), load_limits(), listings=listings)
    (down,) = [r for r in out["rejected"] if r["symbol"] == "DOWN"]
    assert down["reasons"][0].startswith("listed on OTC:") and len(down["reasons"]) > 1


def test_blocked_the_scan_tool_rejects_an_arca_listing():
    data = FakeMarketData(exchanges={"GAPA": "ARCA"})
    out, err = make(data).execute("scan", {"symbols": ["GAPA", "GAPB"]})
    assert err is False and out["candidates"] == []  # the fixture snapshots are stale too
    reasons = {r["symbol"]: r["reasons"] for r in out["rejected"]}
    assert reasons["GAPA"][0].startswith("listed on ARCA:")
    assert not any("listing" in x or "listed on" in x for x in reasons["GAPB"])


def test_blocked_the_scan_tool_rejects_every_symbol_when_the_listing_lookup_fails():
    data = FakeMarketData()
    data.listing_raises = RuntimeError("HTTP 503 from /v2/assets/GAPA")
    out, err = make(data).execute("scan", {"symbols": ["GAPA", "GAPB"]})
    assert err is False and out["candidates"] == []
    reasons = {r["symbol"]: r["reasons"][0] for r in out["rejected"]}
    assert reasons["GAPA"] == "listing exchange unknown (HTTP 503 from /v2/assets/GAPA)"
    # the lookups stop after the first transport failure (a hung host costs one timeout)
    assert reasons["GAPB"] == ("listing exchange unknown (lookup skipped after an earlier "
                               "failure)")
    assert [c for c in data.calls if c[0] == "listing"] == [("listing", "GAPA")]


def test_an_unknown_symbol_404_does_not_stop_the_other_lookups():
    class Partly(FakeMarketData):
        def listing(self, symbol):
            self.calls.append(("listing", symbol))
            if symbol == "GAPA":
                raise RuntimeError("HTTP 404 from /v2/assets/GAPA")
            return asset_reply(symbol)
    data = Partly()
    out, _ = make(data).execute("scan", {"symbols": ["GAPA", "GAPB"]})
    reasons = {r["symbol"]: r["reasons"] for r in out["rejected"]}
    assert reasons["GAPA"][0] == "listing exchange unknown (HTTP 404 from /v2/assets/GAPA)"
    assert not any("listing" in x for x in reasons["GAPB"])
    assert [c for c in data.calls if c[0] == "listing"] == [("listing", "GAPA"),
                                                           ("listing", "GAPB")]


def test_a_bad_reply_does_not_stop_the_other_lookups():
    data = FakeMarketData(exchanges={"GAPA": "ARCA"})
    make(data).execute("scan", {"symbols": ["GAPA", "GAPB"]})
    assert [c for c in data.calls if c[0] == "listing"] == [("listing", "GAPA"),
                                                           ("listing", "GAPB")]


def test_blocked_the_scan_tool_rejects_everything_without_a_listing_source():
    class NoListing:
        def snapshots(self, symbols):
            return FakeMarketData().snapshots(symbols)
    out, _ = make(NoListing()).execute("scan", {"symbols": ["GAPA"]})
    assert out["candidates"] == []
    assert out["rejected"][0]["reasons"][0] == "listing exchange unknown (no listing source)"


# ======================= the gate: the order path's backstop =======================

@pytest.mark.parametrize("code", OTHER_VENUES)
def test_blocked_the_gate_is_unknown_for_another_venue_and_check_order_blocks(code):
    gate = gate_for("TSLA", NOW, listing=listing_ok("TSLA", code))
    assert gate["status"] == "UNKNOWN" and gate["halt"]["status"] == "UNKNOWN"
    assert f"UNKNOWN (listing): listed on {code}: only Nasdaq" in gate["reason"]
    assert "cannot rule out a halt" in gate["reason"]
    assert verdict_for(gate, NOW)["verdict"] == "BLOCK"


@pytest.mark.parametrize("code", ["NASDAQ", "NYSE", "AMEX"])
def test_the_gate_is_clear_for_an_allowed_listing(code):
    gate = gate_for("TSLA", NOW, listing=listing_ok("TSLA", code))
    assert gate["status"] == "CLEAR", gate["reason"]


def test_blocked_a_gate_without_a_listing_is_unknown():
    """Every other input clean; the listing is left out (None): UNKNOWN, never CLEAR."""
    from tests.order_helpers import calendar_ok, filings_ok, halts_ok, news_ok
    from tools.event_gate import event_gate
    gate = event_gate("TSLA", NOW, NOW.replace(hour=15, minute=55), calendar=calendar_ok(NOW),
                      filings=filings_ok(NOW), news=news_ok(), halts=halts_ok(NOW))
    assert gate["status"] == "UNKNOWN"
    assert gate["reason"] == ("UNKNOWN (listing): listing exchange unknown (no listing): "
                              "cannot rule out a halt")


def test_blocked_a_listing_for_another_symbol_makes_the_gate_unknown():
    gate = gate_for("AAPL", NOW, listing=listing_ok("MSFT"))
    assert gate["status"] == "UNKNOWN"
    assert "listing exchange unknown (listing is for another symbol)" in gate["reason"]


def test_an_event_still_wins_over_a_listing_problem():
    from tests.order_helpers import calendar_ok, fmp_row
    cal = calendar_ok(NOW, [fmp_row("TSLA", NOW.date().isoformat(), "amc", True)])
    gate = gate_for("TSLA", NOW, calendar=cal, listing=listing_ok("TSLA", "ARCA"))
    assert gate["status"] == "EVENT"
    assert "UNKNOWN (listing)" in gate["reason"]  # both kinds stay visible


def data_for(listing):
    return SimpleNamespace(
        av_earnings_calendar=lambda s, e: {
            "fetched_at": (NOW - timedelta(minutes=1)).isoformat(), "from": s.isoformat(),
            "to": e.isoformat(), "reply": av_csv(start=s, end=e)},
        submissions=lambda sym: submissions_202(NOW, 30, symbol=sym),
        news=lambda syms, a, b: {"news": [], "next_page_token": None},
        halt_feed=lambda: fresh_halt_feed(NOW), listing=listing)


def test_events_from_reads_the_listing_and_is_clear_for_nasdaq():
    seen = []
    gate = events_from(data_for(lambda s: seen.append(s) or asset_reply(s)))(
        "TSLA", NOW, NOW.replace(hour=15, minute=55))
    assert gate["status"] == "CLEAR", gate["reason"] and seen == ["TSLA"]


def test_blocked_events_from_with_a_failing_listing_lookup_is_unknown():
    def boom(sym):
        raise RuntimeError(f"HTTP 503 from /v2/assets/{sym} {KEY}")
    gate = events_from(data_for(boom))("TSLA", NOW, NOW.replace(hour=15, minute=55))
    assert gate["status"] == "UNKNOWN"
    assert "UNKNOWN (listing): listing exchange unknown (HTTP 503 from /v2/assets/TSLA" \
        in gate["reason"]


def test_blocked_events_from_with_an_arca_listing_is_unknown():
    gate = events_from(data_for(lambda s: asset_reply(s, "ARCA")))(
        "SPY", NOW, NOW.replace(hour=15, minute=55))
    assert gate["status"] == "UNKNOWN" and "listed on ARCA" in gate["reason"]


def test_listing_input_without_a_source_is_not_ok():
    assert listing_input(SimpleNamespace(), "TSLA") == {
        "ok": False, "reason": "no listing source", "fetch_failed": True}


# ======================= LiveData, scenarios, smoke_events =======================

@pytest.mark.parametrize("bad", [asset_reply("AAPL", status="inactive"), asset_reply("MSFT")],
                         ids=["inactive", "other-symbol"])
def test_live_data_does_not_keep_a_reply_that_does_not_parse(monkeypatch, bad):
    calls = []
    monkeypatch.setattr("pipeline.data.fetch_asset",
                        lambda sym, opener=None: calls.append(sym) or bad)
    data = LiveData()
    data.listing("AAPL")
    data.listing("AAPL")
    assert calls == ["AAPL", "AAPL"]


def test_live_data_keeps_a_good_listing_and_retries_a_failure(monkeypatch):
    calls = []

    def fake(sym, opener=None):
        calls.append(sym)
        if len(calls) == 1:
            raise RuntimeError("HTTP 503 from /v2/assets/AAPL")
        return asset_reply(sym)
    monkeypatch.setattr("pipeline.data.fetch_asset", fake)
    data = LiveData()
    with pytest.raises(RuntimeError):
        data.listing("AAPL")
    assert data.listing("aapl")["exchange"] == "NASDAQ"
    assert data.listing("AAPL")["exchange"] == "NASDAQ"
    assert calls == ["AAPL", "AAPL"]


@pytest.mark.parametrize("name,code", [("clean-buy", "NASDAQ"), ("red-flag", "NASDAQ"),
                                       ("extended-chase", "NYSE"), ("tsla", "NASDAQ")])
def test_every_scenario_has_an_allowed_listing(name, code):
    data = ScenarioData(load_scenario(name))
    listing = listing_input(data, data.symbol)
    assert listing == {"ok": True, "symbol": data.symbol, "exchange": code}


def test_a_scenario_has_no_listing_for_another_symbol():
    data = ScenarioData(load_scenario("clean-buy"))
    assert listing_input(data, "AAPL") == {
        "ok": False, "reason": "HTTP 404 from /v2/assets/AAPL", "fetch_failed": True}


def test_smoke_events_plan_names_the_asset_lookups():
    from pipeline.smoke_events import plan_lines
    assert "Alpaca assets (listing exchange, #168): 1 call per symbol (2)" in plan_lines(
        ["AAPL", "TSLA"])


def test_smoke_events_prints_the_listing():
    data = ScenarioData(load_scenario("extended-chase"))
    out = "\n".join(run_live(["CHSW", "AAPL"], data, data.as_of()))
    assert "  listing: NYSE" in out
    assert "  listing: NOT OK: HTTP 404 from /v2/assets/AAPL" in out
