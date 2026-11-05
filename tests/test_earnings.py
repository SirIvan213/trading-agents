"""Tests for tools.earnings. Saved companyfacts fixture and fake openers; never the network.

Fixture tests/fixtures/sec_companyfacts_abcd.json (ABCD, cik 12345), quarterly revenue:
  Q1'25 1,000,000 (filed 2025-05-08)   Q2'25 1,100,000 (2025-08-07)   Q3'25 1,200,000 (2025-11-06)
  Q1'26 1,150,000 (2026-05-07)         Q2'26 1,250,000 (2026-08-05), amended by a 10-Q/A to
  1,265,000 (2026-08-20)               Q3'26 1,400,000 (filed LATE, 2026-11-10)
  FY2025 10-K 4,600,000 (364 days, filed 2026-02-25; no Q4 10-Q, so Q4'25 is absent)
Net income: 100k, -50k, 120k, 90k, 25k, 150k (Q3'26).  EPS: 0.10, -0.05, 0.12, 0.09, 0.03, 0.15.
Cash: 2026-03-31 520,000 / 2026-06-30 540,000 / 2026-09-30 600,000 (late).
Shares: 2026-04-30 10,000,000 / 2026-07-31 10,100,000 / 2026-10-31 10,200,000 (late).
"""
from datetime import date, datetime

import pytest

from tests.fakes import load_fixture
from tests.test_news import Opener, headers, limiter
from tools import earnings as E

UA = "Ivan Test research@acme-research.org"
NOV3 = date(2026, 11, 3)


@pytest.fixture
def cf():
    return load_fixture("sec_companyfacts_abcd.json")


def q(start, end, val, filed, form="10-Q", fy=2026, fp="Q2"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form,
            "fy": fy, "fp": fp}


def inst(end, val, filed, form="10-Q"):
    return {"end": end, "val": val, "filed": filed, "form": form, "fy": 2026, "fp": "Q2"}


def build(revenue=None, net_income=None, eps=None, cash=None, shares=None, rev_concept="Revenues"):
    us: dict = {}
    if revenue is not None:
        us[rev_concept] = {"units": {"USD": revenue}}
    if net_income is not None:
        us["NetIncomeLoss"] = {"units": {"USD": net_income}}
    if eps is not None:
        us["EarningsPerShareDiluted"] = {"units": {"USD/shares": eps}}
    if cash is not None:
        us["CashAndCashEquivalentsAtCarryingValue"] = {"units": {"USD": cash}}
    out: dict = {"cik": 12345, "entityName": "T Co", "facts": {"us-gaap": us}}
    if shares is not None:
        out["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": shares}}}
    return out


# Two quarters one year apart; ends 2025-06-30 and 2026-06-30 are 365 days apart.
OLD = ("2025-04-01", "2025-06-30")
NEW = ("2026-04-01", "2026-06-30")


def growth_of(new, old, metric="revenue"):
    rows_new, rows_old = [q(*NEW, new, "2026-08-05")], [q(*OLD, old, "2025-08-05")]
    out = E.earnings_facts(build(revenue=rows_old + rows_new), date(2026, 9, 1))
    assert out["ok"] is True
    return out["growth"][f"{metric}_yoy_pct"]


# ---------------- point in time ----------------

def test_as_of_after_late_filing_includes_it(cf):
    out = E.earnings_facts(cf, date(2026, 11, 10))  # filing day itself counts (filed <= as_of)
    top = out["quarters"][0]
    assert (top["end"], top["revenue"], top["form"], top["first_filed"], top["last_filed"]) == (
        "2026-09-30", "1400000", "10-Q", "2026-11-10", "2026-11-10")
    assert "filed" not in top
    assert out["latest_instant"]["cash"] == {
        "value": "600000", "end": "2026-09-30", "form": "10-Q",
        "first_filed": "2026-11-10", "last_filed": "2026-11-10"}
    assert out["latest_instant"]["shares_outstanding"]["value"] == "10200000"
    assert out["latest_filed"] == "2026-11-10"
    # Q3'26 vs Q3'25 (365 days): rev (1.4M-1.2M)/1.2M = 16.666..% -> 16.67
    # NI (150k-120k)/120k = 25.00 ; EPS (0.15-0.12)/0.12 = 25.00
    assert out["growth"] == {
        "revenue_yoy_pct": "16.67", "net_income_yoy_pct": "25.00", "eps_yoy_pct": "25.00"}


def test_as_of_before_late_filing_has_no_look_ahead(cf):
    out = E.earnings_facts(cf, NOV3)
    assert out["ok"] is True and out["as_of"] == "2026-11-03"
    ends = [r["end"] for r in out["quarters"]]
    assert "2026-09-30" not in ends
    assert ends == ["2026-06-30", "2026-03-31", "2025-09-30", "2025-06-30"]
    assert out["latest_instant"]["cash"]["value"] == "540000"
    assert out["latest_instant"]["shares_outstanding"]["value"] == "10100000"
    # latest_filed is the max FIRST filing date: the 08-20 amendment is not new information
    assert out["latest_filed"] == "2026-08-05"
    assert "2026-11-10" not in str(out)


def test_filed_one_day_after_as_of_is_excluded(cf):
    out = E.earnings_facts(cf, date(2026, 11, 9))
    assert out["quarters"][0]["end"] == "2026-06-30"
    assert out["latest_filed"] == "2026-08-05"


def test_no_data_before_first_filing(cf):
    out = E.earnings_facts(cf, date(2025, 5, 7))  # first 10-Q filed 2025-05-08
    assert out == {"ok": False, "reason": "no quarterly data filed on or before 2025-05-07"}
    assert E.earnings_facts(cf, date(2025, 5, 8))["quarters"][0]["end"] == "2025-03-31"


def test_result_is_json_serialisable(cf):
    import json
    json.dumps(E.earnings_facts(cf, NOV3))


# ---------------- amendments and 10-K ----------------

def test_amendment_wins_after_it_is_filed(cf):
    out = E.earnings_facts(cf, NOV3)
    row = out["quarters"][0]
    # 10-Q/A (1,265,000, filed 2026-08-20) supersedes the 10-Q (1,250,000, 2026-08-05)
    # fy/fp/form come from the EARLIEST filing (the original 10-Q); the value from the latest
    assert (row["revenue"], row["form"], row["first_filed"], row["last_filed"]) == (
        "1265000", "10-Q", "2026-08-05", "2026-08-20")
    assert (row["fy"], row["fp"]) == (2026, "Q2")
    assert out["catalysts"] == ["restatement"]
    # (1,265,000 - 1,100,000) / 1,100,000 * 100 = 15.00
    assert out["growth"]["revenue_yoy_pct"] == "15.00"


def test_original_wins_before_the_amendment_is_filed(cf):
    out = E.earnings_facts(cf, date(2026, 8, 10))
    row = out["quarters"][0]
    assert (row["revenue"], row["form"], row["first_filed"], row["last_filed"]) == (
        "1250000", "10-Q", "2026-08-05", "2026-08-05")
    assert out["catalysts"] == ["recent_10q"]  # first filed 5 days before as_of
    # (1,250,000 - 1,100,000) / 1,100,000 * 100 = 13.636..% -> 13.64
    assert out["growth"]["revenue_yoy_pct"] == "13.64"


def test_ten_k_full_year_is_skipped_and_noted(cf):
    out = E.earnings_facts(cf, NOV3)
    assert all(r["start"] != "2025-01-01" or r["end"] != "2025-12-31" for r in out["quarters"])
    wide = E.earnings_facts(cf, NOV3, quarters=20)["quarters"]
    assert "2025-12-31" not in [r["end"] for r in wide]
    # three concepts carry a 10-K full-year entry filed 2026-02-25
    assert out["notes"] == [
        "skipped 3 non-quarterly duration entries (full-year/year-to-date; "
        "Q4 not derived by subtraction)"
    ]


def test_ten_k_not_noted_before_it_was_filed(cf):
    out = E.earnings_facts(cf, date(2026, 1, 15))
    assert out["notes"] == ["missing concept: cash", "missing concept: shares_outstanding"] or \
        not any("non-quarterly" in n for n in out["notes"])


def test_quarters_limit(cf):
    assert [r["end"] for r in E.earnings_facts(cf, NOV3, quarters=2)["quarters"]] == [
        "2026-06-30", "2026-03-31"]
    assert len(E.earnings_facts(cf, NOV3, quarters=50)["quarters"]) == 5  # only 5 visible
    assert len(E.earnings_facts(cf, NOV3, quarters=1)["quarters"]) == 1


def test_entity_and_cik(cf):
    out = E.earnings_facts(cf, NOV3)
    assert out["entity"] == "ABCD Holdings Inc" and out["cik"] == "0000012345"


# ---------------- growth math ----------------

@pytest.mark.parametrize(("new", "old", "expected"), [
    # (new-old)/|old|*100, quantized to 0.01, ROUND_HALF_EVEN
    (115, 100, "15.00"),
    (1, 3, "-66.67"),          # -66.666..
    (2, 3, "-33.33"),          # -33.333..
    (8010, 8000, "0.12"),      # exactly 0.125 -> half-even gives 0.12 (half-up would be 0.13)
    (8030, 8000, "0.38"),      # exactly 0.375 -> half-even gives 0.38
    (100, 100, "0.00"),
    (0, 100, "-100.00"),
    (25, -50, "150.00"),       # negative base uses |old|: (25+50)/50*100
    (-25, -50, "50.00"),       # (-25+50)/50*100
    (-50, 50, "-200.00"),
])
def test_yoy_growth_values(new, old, expected):
    assert growth_of(new, old) == expected


def test_growth_none_when_old_is_zero():
    assert growth_of(100, 0) is None


def test_growth_none_when_old_missing():
    # no prior-year quarter at all
    out = E.earnings_facts(build(revenue=[q(*NEW, 100, "2026-08-05")]), date(2026, 9, 1))
    assert out["growth"] == {
        "revenue_yoy_pct": None, "net_income_yoy_pct": None, "eps_yoy_pct": None}
    assert (
        "no quarter ending 350-380 days before the newest quarter; growth unavailable"
        in out["notes"]
    )


def test_growth_none_when_metric_missing_in_old_quarter_only():
    rev = [q(*OLD, 100, "2025-08-05"), q(*NEW, 110, "2026-08-05")]
    ni = [q(*NEW, 5, "2026-08-05")]  # net income absent a year ago
    out = E.earnings_facts(build(revenue=rev, net_income=ni), date(2026, 9, 1))
    assert out["growth"]["revenue_yoy_pct"] == "10.00"
    assert out["growth"]["net_income_yoy_pct"] is None


def test_growth_none_when_new_metric_missing():
    rev = [q(*OLD, 100, "2025-08-05"), q(*NEW, 110, "2026-08-05")]
    ni = [q(*OLD, 5, "2025-08-05")]
    out = E.earnings_facts(build(revenue=rev, net_income=ni), date(2026, 9, 1))
    assert out["growth"]["net_income_yoy_pct"] is None


@pytest.mark.parametrize(("old_end", "has_prior"), [
    ("2025-06-30", True),    # 365 days
    ("2025-07-15", True),    # 350 days: boundary included (2026-06-30 - 350d)
    ("2025-07-16", False),   # 349 days
    ("2025-06-15", True),    # 380 days: boundary included
    ("2025-06-14", False),   # 381 days
])
def test_yoy_window_boundaries(old_end, has_prior):
    from datetime import timedelta
    end = date.fromisoformat(old_end)
    old = q((end - timedelta(days=90)).isoformat(), old_end, 100, "2025-08-05")
    out = E.earnings_facts(build(revenue=[old, q(*NEW, 110, "2026-08-05")]), date(2026, 9, 1))
    assert (out["growth"]["revenue_yoy_pct"] == "10.00") is has_prior


def test_fixture_growth_values(cf):
    g = E.earnings_facts(cf, NOV3)["growth"]
    # revenue 15.00 ; NI (25,000 - (-50,000)) / 50,000 * 100 = 150.00
    # EPS (0.03 + 0.05) / 0.05 * 100 = 160.00
    assert g == {"revenue_yoy_pct": "15.00", "net_income_yoy_pct": "150.00",
                 "eps_yoy_pct": "160.00"}


def test_money_values_are_strings_not_floats(cf):
    out = E.earnings_facts(cf, NOV3)
    assert out["quarters"][0]["eps_diluted"] == "0.03"
    assert all(isinstance(r["revenue"], str) for r in out["quarters"])
    assert isinstance(out["growth"]["revenue_yoy_pct"], str)


def test_float_values_do_not_leak_binary_noise():
    rows = [q(*NEW, 0.1, "2026-08-05")]
    out = E.earnings_facts(build(eps=rows), date(2026, 9, 1))
    assert out["quarters"][0]["eps_diluted"] == "0.1"


def test_same_filed_date_tie_breaks_on_larger_value():
    rev = [q(*NEW, 100, "2026-08-05"), q(*NEW, 120, "2026-08-05")]
    out = E.earnings_facts(build(revenue=rev), date(2026, 9, 1))
    assert out["quarters"][0]["revenue"] == "120"


# ---------------- concept fallback ----------------

def test_revenue_concept_fallback_order():
    def rev(val):
        return {"units": {"USD": [q(*NEW, val, "2026-08-05")]}}

    concepts = {
        "Revenues": rev(1),
        "RevenueFromContractWithCustomerExcludingAssessedTax": rev(2),
        "SalesRevenueNet": rev(3),
    }

    def top():
        out = E.earnings_facts({"facts": {"us-gaap": concepts}}, date(2026, 9, 1))
        return out["quarters"][0]["revenue"]

    assert top() == "1"
    del concepts["Revenues"]
    assert top() == "2"
    del concepts["RevenueFromContractWithCustomerExcludingAssessedTax"]
    assert top() == "3"


def test_fallback_when_first_concept_has_no_usable_entries():
    facts = {"facts": {"us-gaap": {
        # Revenues only has a filing after as_of and a full-year entry -> nothing usable
        "Revenues": {"units": {"USD": [
            q(*NEW, 1, "2026-12-01"),
            q("2026-01-01", "2026-12-31", 9, "2026-02-01", form="10-K"),
        ]}},
        "SalesRevenueNet": {"units": {"USD": [q(*NEW, 3, "2026-08-05")]}},
    }}}
    out = E.earnings_facts(facts, date(2026, 9, 1))
    assert out["quarters"][0]["revenue"] == "3"


def test_missing_concepts_are_noted():
    out = E.earnings_facts(build(revenue=[q(*NEW, 5, "2026-08-05")]), date(2026, 9, 1))
    assert out["quarters"][0]["net_income"] is None and out["quarters"][0]["eps_diluted"] is None
    assert out["notes"] == [
        "no quarter ending 350-380 days before the newest quarter; growth unavailable",
        "missing concept: net_income", "missing concept: eps_diluted",
        "missing concept: cash", "missing concept: shares_outstanding",
    ]
    assert out["latest_instant"] == {"cash": None, "shares_outstanding": None}


# ---------------- instants ----------------

def test_instant_picks_latest_end_then_latest_filed():
    cash = [
        inst("2026-03-31", 10, "2026-05-07"),
        inst("2026-06-30", 20, "2026-08-05"),
        inst("2026-06-30", 25, "2026-08-20", form="10-Q/A"),
    ]
    out = E.earnings_facts(build(revenue=[q(*NEW, 5, "2026-08-05")], cash=cash), date(2026, 9, 1))
    assert out["latest_instant"]["cash"] == {
        "value": "25", "end": "2026-06-30", "form": "10-Q",
        "first_filed": "2026-08-05", "last_filed": "2026-08-20"}
    out = E.earnings_facts(build(revenue=[q(*NEW, 5, "2026-08-05")], cash=cash), date(2026, 8, 10))
    assert out["latest_instant"]["cash"]["value"] == "20"


def test_instant_ignores_duration_values_and_bad_forms():
    cash = [
        q("2026-04-01", "2026-06-30", 999, "2026-08-05"),            # has a start: not an instant
        inst("2026-06-30", 111, "2026-08-05", form="8-K"),           # form not allowed
        inst("2026-03-31", 7, "2026-05-07"),
    ]
    out = E.earnings_facts(build(revenue=[q(*NEW, 5, "2026-08-05")], cash=cash), date(2026, 9, 1))
    assert out["latest_instant"]["cash"]["value"] == "7"


def test_instant_future_filed_excluded():
    cash = [inst("2026-09-30", 600, "2026-11-10"), inst("2026-06-30", 540, "2026-08-05")]
    out = E.earnings_facts(build(revenue=[q(*NEW, 5, "2026-08-05")], cash=cash), NOV3)
    assert out["latest_instant"]["cash"]["value"] == "540"


# ---------------- refusals ----------------

@pytest.mark.parametrize("bad", [{}, {"facts": None}, {"facts": []}, None, [], "x", 5])
def test_no_facts_refused(bad):
    assert E.earnings_facts(bad, NOV3) == {
        "ok": False, "reason": "companyfacts has no 'facts' object"}


def test_empty_facts_object_has_no_quarterly_data():
    out = E.earnings_facts({"facts": {}}, NOV3)
    assert out == {"ok": False, "reason": "no quarterly data filed on or before 2026-11-03"}


def test_datetime_as_of_refused(cf):
    for bad in (datetime(2026, 11, 3, 12, 0), "2026-11-03", None, 20261103):
        out = E.earnings_facts(cf, bad)
        assert out["ok"] is False and out["reason"].startswith("as_of must be a datetime.date")


@pytest.mark.parametrize("bad", [0, -1, True, False, "4", 2.0, None])
def test_bad_quarters_refused(cf, bad):
    assert E.earnings_facts(cf, NOV3, quarters=bad) == {
        "ok": False, "reason": "quarters must be an integer >= 1"}


# ---------------- malformed entries ----------------

def test_malformed_entries_skipped_and_counted():
    good = q(*NEW, 5, "2026-08-05")
    rev = [
        good,
        "not a dict",
        q(*NEW, "abc", "2026-08-05"),                    # bad val
        q(*NEW, True, "2026-08-05"),                     # bool val
        q(*NEW, float("inf"), "2026-08-05"),             # non-finite
        {**q(*NEW, 5, "2026-08-05"), "end": "nope"},     # bad end
        {**q(*NEW, 5, "2026-08-05"), "filed": None},     # missing filed
        {**q(*NEW, 5, "2026-08-05"), "form": 10},        # non-str form
        {k: v for k, v in q(*NEW, 5, "2026-08-05").items() if k != "start"},  # duration w/o start
        {**q(*NEW, 5, "2026-08-05"), "start": "garbage"},
    ]
    out = E.earnings_facts(build(revenue=rev), date(2026, 9, 1))
    assert out["ok"] is True
    assert [r["revenue"] for r in out["quarters"]] == ["5"]
    assert "skipped 9 malformed entries" in out["notes"]


def test_malformed_instant_entries_counted():
    cash = [{"end": "x", "val": 1, "filed": "2026-08-05", "form": "10-Q"},
            inst("2026-06-30", 4, "2026-08-05")]
    out = E.earnings_facts(build(revenue=[q(*NEW, 5, "2026-08-05")], cash=cash), date(2026, 9, 1))
    assert out["latest_instant"]["cash"]["value"] == "4"
    assert "skipped 1 malformed entries" in out["notes"]


def test_unusable_concept_shapes_do_not_raise():
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": "oops"}},
        "NetIncomeLoss": "oops",
        "EarningsPerShareDiluted": {"units": []},
        "CashAndCashEquivalentsAtCarryingValue": None,
    }, "dei": 5}}
    assert E.earnings_facts(facts, NOV3)["ok"] is False


def test_bad_cik_noted_not_raised():
    cfx = build(revenue=[q(*NEW, 5, "2026-08-05")])
    cfx["cik"] = "abc"
    del cfx["entityName"]
    out = E.earnings_facts(cfx, date(2026, 9, 1))
    assert out["cik"] is None and out["entity"] is None
    assert "companyfacts has no valid cik" in out["notes"]


# ---------------- cik_for_symbol ----------------

MAP = {"ABCD": "0000012345", "AAPL": "0000320193"}


@pytest.mark.parametrize("sym", ["ABCD", "abcd", "  abcd  "])
def test_cik_for_symbol_found(sym):
    assert E.cik_for_symbol(sym, MAP) == "0000012345"


@pytest.mark.parametrize(("sym", "m"), [
    ("ZZZZ", MAP), ("", MAP), (None, MAP), (5, MAP), ("ABCD", None), ("ABCD", []),
    ("ABCD", {"ABCD": 12345}), ("ABCD", {}),
])
def test_cik_for_symbol_none(sym, m):
    assert E.cik_for_symbol(sym, m) is None


# ---------------- fetch_company_facts ----------------

def test_fetch_company_facts_url_headers_and_limiter(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", f"  {UA}  ")
    lim, _ = limiter()
    op = Opener([load_fixture("sec_companyfacts_abcd.json")])
    out = E.fetch_company_facts(12345, op, limiter=lim)
    assert out["entityName"] == "ABCD Holdings Inc"
    (req,) = op.requests
    assert req.full_url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000012345.json"
    assert headers(req)["user-agent"] == UA
    assert len(lim._calls) == 1


def test_fetch_company_facts_uses_module_limiter_by_default(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    lim, _ = limiter()
    monkeypatch.setattr(E, "SEC_LIMITER", lim)
    E.fetch_company_facts("0000012345", Opener([{"facts": {}}]))
    assert len(lim._calls) == 1


@pytest.mark.parametrize("value", [None, "", "   ", "Your Name you@example.com"])
def test_fetch_company_facts_ua_rules(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    else:
        monkeypatch.setenv("SEC_USER_AGENT", value)
    op = Opener([])
    lim, _ = limiter()
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        E.fetch_company_facts("12345", op, limiter=lim)
    assert op.requests == [] and len(lim._calls) == 0


@pytest.mark.parametrize("cik", ["abc", 0, "-5", "", None, 12345678901, "../1"])
def test_fetch_company_facts_invalid_cik(monkeypatch, cik):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    op = Opener([])
    with pytest.raises(ValueError):
        E.fetch_company_facts(cik, op, limiter=limiter()[0])
    assert op.requests == []


@pytest.mark.parametrize("body", [[1], {"message": "nope"}, {"error": "x"}])
def test_fetch_company_facts_bad_bodies(monkeypatch, body):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    with pytest.raises(RuntimeError):
        E.fetch_company_facts("12345", Opener([body]), limiter=limiter()[0])


def test_fetch_company_facts_without_opener_hits_guard(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    with pytest.raises(RuntimeError, match="network access in tests"):
        E.fetch_company_facts("12345", limiter=limiter()[0])


def test_fetch_company_facts_body_without_facts_is_returned_and_then_refused(monkeypatch):
    # get_json only rejects error payloads; a body lacking "facts" passes through and
    # earnings_facts is the layer that refuses it.
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    out = E.fetch_company_facts("12345", Opener([{"other": 1}]), limiter=limiter()[0])
    assert E.earnings_facts(out, NOV3)["ok"] is False


# ---------------- realistic fixture: comparatives, YTD, stale concept ----------------
# sec_companyfacts_abcd_realistic.json. Revenue is under RevenueFromContract... (current);
# "Revenues" has only a 2017 quarter. Each later 10-Q repeats the prior-year quarter as a
# comparative with the LATER filing's fy/fp. 6- and 9-month YTD durations are present.
# Q1 2025 comparative was restated 1,000,000 -> 1,010,000 in the 2026-05-07 10-Q; the Q3 2025
# comparative 1,200,000 -> 1,210,000 in the 2026-11-03 10-Q.

@pytest.fixture
def real():
    return load_fixture("sec_companyfacts_abcd_realistic.json")


def by_end(out):
    return {r["end"]: r for r in out["quarters"]}


def test_b1_current_concept_beats_stale_first_concept(real):
    out = E.earnings_facts(real, NOV3, quarters=20)
    assert out["concepts"] == {
        "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "net_income": "NetIncomeLoss",
        "eps_diluted": "EarningsPerShareDiluted",
        "cash": "CashAndCashEquivalentsAtCarryingValue",
        "shares_outstanding": "EntityCommonStockSharesOutstanding",
    }
    ends = [r["end"] for r in out["quarters"]]
    assert "2017-03-31" not in ends
    assert ends[0] == "2026-09-30" and out["quarters"][0]["revenue"] == "1400000"


def test_b1_concepts_are_never_mixed(real):
    out = E.earnings_facts(real, NOV3, quarters=20)
    assert all(r["revenue"] != "500000" for r in out["quarters"])  # the 2017 value


def test_b1_stale_concept_alone_is_still_used_when_nothing_newer():
    facts = build(revenue=[q("2017-01-01", "2017-03-31", 500000, "2017-05-04")])
    out = E.earnings_facts(facts, date(2017, 6, 1))
    assert out["concepts"]["revenue"] == "Revenues"
    assert out["quarters"][0]["revenue"] == "500000"


def test_b1_tie_goes_to_listed_priority():
    both = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [q(*NEW, 1, "2026-08-05")]}},
        "SalesRevenueNet": {"units": {"USD": [q(*NEW, 3, "2026-08-05")]}},
    }}}
    out = E.earnings_facts(both, date(2026, 9, 1))
    assert out["concepts"]["revenue"] == "Revenues" and out["quarters"][0]["revenue"] == "1"


def test_b1_newer_lower_priority_concept_wins_over_older_higher_priority():
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [q("2025-04-01", "2025-06-30", 1, "2025-08-05")]}},
        "SalesRevenueNet": {"units": {"USD": [q(*NEW, 3, "2026-08-05")]}},
    }}}
    out = E.earnings_facts(facts, date(2026, 9, 1))
    assert out["concepts"]["revenue"] == "SalesRevenueNet"
    assert [r["revenue"] for r in out["quarters"]] == ["3"]


def test_b1_concepts_none_when_missing():
    out = E.earnings_facts(build(revenue=[q(*NEW, 5, "2026-08-05")]), date(2026, 9, 1))
    assert out["concepts"]["net_income"] is None and out["concepts"]["cash"] is None
    assert out["concepts"]["revenue"] == "Revenues"


def test_b2_labels_come_from_the_earliest_filing(real):
    rows = by_end(E.earnings_facts(real, NOV3, quarters=20))
    q3_25 = rows["2025-09-30"]
    # first reported in the 2025-11-06 10-Q (fy 2025 Q3); repeated in the 2026-11-03 10-Q
    assert (q3_25["fy"], q3_25["fp"], q3_25["form"]) == (2025, "Q3", "10-Q")
    assert (q3_25["first_filed"], q3_25["last_filed"]) == ("2025-11-06", "2026-11-03")
    q1_25 = rows["2025-03-31"]
    assert (q1_25["fy"], q1_25["fp"]) == (2025, "Q1")  # not the 2026 Q1 label of the re-filing
    assert (q1_25["first_filed"], q1_25["last_filed"]) == ("2025-05-08", "2026-05-07")
    q2_24 = rows["2024-06-30"]
    assert (q2_24["fy"], q2_24["fp"]) == (2024, "Q2")
    assert (q2_24["first_filed"], q2_24["last_filed"]) == ("2024-08-08", "2025-08-07")
    assert "filed" not in q3_25


def test_b2_value_comes_from_the_latest_filing_known_at_as_of(real):
    after = by_end(E.earnings_facts(real, NOV3, quarters=20))
    assert after["2025-09-30"]["revenue"] == "1210000"      # restated by the 2026-11-03 10-Q
    assert after["2025-03-31"]["revenue"] == "1010000"      # restated by the 2026-05-07 10-Q
    assert after["2025-06-30"]["revenue"] == "1100000"      # comparative unchanged
    before = by_end(E.earnings_facts(real, date(2026, 11, 2), quarters=20))
    assert before["2025-09-30"]["revenue"] == "1200000"     # restatement not yet public
    assert before["2025-09-30"]["last_filed"] == "2025-11-06"
    assert "2026-09-30" not in before


def test_restatement_catalyst_only_when_visible_and_returned(real):
    # Q3 2025 is among the 4 returned quarters at Nov 3 and its value changed
    assert "restatement" in E.earnings_facts(real, NOV3)["catalysts"]
    # a day earlier the restated comparative is not public: no restatement
    assert "restatement" not in E.earnings_facts(real, date(2026, 11, 2))["catalysts"]
    # Q1 2025 (restated 2026-05-07) is returned only with a wider window
    oct1 = date(2026, 10, 1)
    assert E.earnings_facts(real, oct1)["catalysts"] == []
    assert E.earnings_facts(real, oct1, quarters=10)["catalysts"] == ["restatement"]


def test_restatement_catalyst_for_amendment_form(cf):
    assert E.earnings_facts(cf, NOV3)["catalysts"] == ["restatement"]  # 10-Q/A supplied the value


def test_growth_uses_restated_comparative(real):
    # Q3'26 1,400,000 vs restated Q3'25 1,210,000: 190,000 / 1,210,000 * 100 = 15.7024.. -> 15.70
    g = E.earnings_facts(real, NOV3)["growth"]
    assert g == {"revenue_yoy_pct": "15.70", "net_income_yoy_pct": "25.00",
                 "eps_yoy_pct": "25.00"}
    # a day earlier: Q2'26 1,250,000 vs Q2'25 1,100,000 = 13.636.. -> 13.64 ; NI 20/110 = 18.18
    g = E.earnings_facts(real, date(2026, 11, 2))["growth"]
    assert g == {"revenue_yoy_pct": "13.64", "net_income_yoy_pct": "18.18",
                 "eps_yoy_pct": "18.18"}


@pytest.mark.parametrize(("as_of", "expected"), [
    (date(2026, 11, 3), ["recent_10q", "restatement"]),   # 0 days after the 10-Q
    (date(2026, 11, 8), ["recent_10q", "restatement"]),   # 5 days: still recent
    (date(2026, 11, 9), ["restatement"]),                 # 6 days: no longer recent
    (date(2026, 11, 2), []),                              # before the filing
])
def test_recent_10q_window_is_five_days(real, as_of, expected):
    assert E.earnings_facts(real, as_of)["catalysts"] == expected


def test_recent_10q_boundaries_on_synthetic_data():
    from datetime import timedelta
    rows = [q(*NEW, 5, "2026-08-05")]
    for days, present in [(0, True), (5, True), (6, False)]:
        out = E.earnings_facts(build(revenue=rows), date(2026, 8, 5) + timedelta(days=days))
        assert ("recent_10q" in out["catalysts"]) is present, days


def test_recent_10k_from_a_recent_10k_instant(real):
    out = E.earnings_facts(real, date(2026, 2, 27))
    assert out["catalysts"] == ["recent_10k"]   # cash at 2025-12-31 came from the 10-K filed 2-25
    assert out["latest_instant"]["cash"]["form"] == "10-K"
    assert E.earnings_facts(real, date(2026, 3, 3))["catalysts"] == []  # 6 days later


def test_recent_10k_from_newest_quarter_form():
    # a 91-day period filed on a 10-K is treated as a quarter by the duration rule
    rows = [q("2025-10-01", "2025-12-31", 5, "2026-02-25", form="10-K")]
    out = E.earnings_facts(build(revenue=rows), date(2026, 2, 26))
    assert out["catalysts"] == ["recent_10k"]


def test_catalysts_are_sorted_and_from_the_allowed_set(real):
    for as_of in (NOV3, date(2026, 11, 8), date(2026, 2, 27), date(2026, 10, 1)):
        c = E.earnings_facts(real, as_of, quarters=10)["catalysts"]
        assert c == sorted(c) and set(c) <= {"recent_10q", "recent_10k", "restatement"}


def test_ytd_durations_are_skipped(real):
    out = E.earnings_facts(real, NOV3, quarters=50)
    assert all(
        (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days <= 100
        for r in out["quarters"]
    )
    starts = {r["start"] for r in out["quarters"]}
    assert "2025-01-01" in starts                       # the Q1 2025 quarter, 89 days
    assert "2026-01-01" in starts                       # the Q1 2026 quarter
    revenues = {r["revenue"] for r in out["quarters"]}
    assert not revenues & {"2100000", "3300000", "4600000", "2400000", "3800000"}
    assert "220000" not in {r["net_income"] for r in out["quarters"]}
    # revenue: 6mo 2025, 9mo 2025, FY 2025 10-K, 6mo 2026, 9mo 2026 ; net income: 6mo 2026 => 6
    assert out["notes"] == [
        "skipped 6 non-quarterly duration entries (full-year/year-to-date; "
        "Q4 not derived by subtraction)"
    ]


def test_ytd_count_depends_on_as_of(real):
    # at 2026-10-01 the 9-month 2026 YTD (filed 2026-11-03) is not public: 5 skipped
    out = E.earnings_facts(real, date(2026, 10, 1))
    assert out["notes"] == [
        "skipped 5 non-quarterly duration entries (full-year/year-to-date; "
        "Q4 not derived by subtraction)"
    ]


def test_latest_filed_is_max_first_filed(real):
    assert E.earnings_facts(real, NOV3)["latest_filed"] == "2026-11-03"
    # the restated 2025 comparatives (last_filed 2026-*) do not move it
    assert E.earnings_facts(real, date(2026, 11, 2))["latest_filed"] == "2026-08-05"


def test_instants_report_form_and_first_last_filed(real):
    out = E.earnings_facts(real, NOV3)
    assert out["latest_instant"]["cash"] == {
        "value": "600000", "end": "2026-09-30", "form": "10-Q",
        "first_filed": "2026-11-03", "last_filed": "2026-11-03"}
    assert out["latest_instant"]["shares_outstanding"]["value"] == "10200000"


def test_realistic_result_is_json_serialisable(real):
    import json
    json.dumps(E.earnings_facts(real, NOV3, quarters=20))


def test_extreme_growth_ratio_is_none_with_note():
    # (1E+999999 - 1E-999999) / 1E-999999 overflows decimal's exponent range
    rows = [q(*OLD, "1E-999999", "2025-08-05"), q(*NEW, "1E+999999", "2026-08-05")]
    out = E.earnings_facts(build(revenue=rows), date(2026, 9, 1))
    assert out["ok"] is True
    assert out["growth"]["revenue_yoy_pct"] is None
    assert "revenue_yoy_pct not computable (extreme ratio)" in out["notes"]
