"""Issue #87: debate may quote analyst evidence verbatim; retries are recorded; fallback
iteration models; 8-K item 9.01 is not a catalyst. Offline: fakes only, no network."""
import json
from datetime import datetime

import pytest

from agents.bear import run_bear
from agents.bull import run_bull
from agents.debate import run_debate
from agents.debate_common import make_validator
from agents.llm import AgentConfig
from agents.money_guard import allowed_values, money_violations, quoted_values
from agents.runner import call_record
from agents.schemas import (
    AgentRunRecord,
    AnalystReport,
    BullCase,
    DecisionPacket,
    Evidence,
    RetryRecord,
)
from tests.debate_helpers import AS_OF, bear_json, bull_json, make_plan, reply
from tests.fakes import FakeClient, load_fixture
from tests.test_runner import OPUS, SONNET, call_news, end, go, sdk_message
from tools import news as N
from tools.news import NY

EVIDENCE = [
    "price is 2.54% above VWAP and relative volume is 1.64",
    "the stock is up 7.47% on the day",
    "EPS was 0.46 last quarter on 3,225,000,000 shares outstanding",
]
FAILED_EVIDENCE = "fundamentals analysis unavailable: 9.99 target 123.45"


def run_record(agent: str, ok: bool = True) -> AgentRunRecord:
    return AgentRunRecord(agent=agent, requested_model="m", effort="low", ok=ok, duration_ms=1)


def packet() -> DecisionPacket:
    tech = AnalystReport(
        agent="technical", symbol="AAPL", stance="bullish", confidence=0.6,
        evidence=[Evidence(point=p, source="get_bars") for p in EVIDENCE],
    )
    fund = AnalystReport(  # a failed run: its evidence must not be quotable
        agent="fundamentals", symbol="AAPL", stance="neutral", confidence=0.0,
        evidence=[Evidence(point=FAILED_EVIDENCE, source="analyst_unavailable")],
    )
    return DecisionPacket(
        symbol="AAPL", as_of=AS_OF, reports={"technical": tech, "fundamentals": fund},
        runs={"technical": run_record("technical"),
              "fundamentals": run_record("fundamentals", ok=False)},
        all_ok=False, exit_plans=[make_plan()],
    )


PKT = packet()
ALLOWED = allowed_values(PKT)
QUOTED = quoted_values(PKT)


# ---------------- quoted_values ----------------

def test_quoted_values_are_verbatim_numbers_from_ok_evidence_only():
    assert QUOTED == {"2.54", "1.64", "7.47", "0.46", "3,225,000,000"}
    assert "9.99" not in QUOTED and "123.45" not in QUOTED  # failed run's evidence


def test_quoted_values_need_a_run_record():
    pkt = PKT.model_copy(update={"runs": {}})
    assert quoted_values(pkt) == frozenset()


def test_quoted_values_never_include_plan_text_or_catalysts():
    # plan values are allowed separately (Decimal-equal, prices only), never as quoted text
    assert "102.00" not in QUOTED and "15:45" not in QUOTED


# ---------------- money_violations with quoted values ----------------

@pytest.mark.parametrize("text", [
    "the stop is 2.54% below",            # (c) strong keyword, quoted figure
    "price 1.64 relative volume",          # (c)
    "risk of a reversal after 7.47% up",   # weak keyword
    "EPS of 0.46 is soft",                 # bare 2-decimal (d)
    "3,225,000,000 shares outstanding",    # (b) size, quoted from evidence
    "$0.46 EPS",                           # (a)
    "entry 102, stop 99.61",               # plan values still allowed
])
def test_quoted_evidence_numbers_pass(text):
    assert money_violations(text, ALLOWED, QUOTED) == [], text


@pytest.mark.parametrize("text", [
    "stop 2.540",                          # not verbatim (extra digit)
    "price 2.5",                           # not verbatim (rounded)
    "3225000000 shares",                   # not verbatim (commas dropped)
    "target 105.00",                       # invented price
    "stop 9.99",                           # only in a FAILED run's evidence
    "buy 100 shares",                      # invented size
    "104.39 shares",                       # plan value never allows a size
    "EPS of 0.47",                         # near but not equal
])
def test_numbers_not_in_the_packet_are_flagged(text):
    assert money_violations(text, ALLOWED, QUOTED), text


# Every known bypass and flagged example still fails with a realistic quoted set present.
@pytest.mark.parametrize("text", [
    "buy 100", "size of 500", "go long 100", "sell short 50", "98 dollars", "trade 1000 USD",
    "position of 100 units", "entry 102.50", "stop 101", "target 105", "price 99", "$99",
    "shares: 100", "qty 50", "limit 99.5", "sell at $250", "CEO sold 50,000 shares",
    "2.40 per share", "stop item 98.50", "buy at 101:50",
])
def test_bypasses_still_flagged_with_quoted_values(text):
    assert money_violations(text, ALLOWED, QUOTED), text


def test_trailing_comma_is_not_part_of_the_number():
    assert money_violations("risk 0.46, then more", ALLOWED, QUOTED) == []
    assert money_violations("risk 0.46,", ALLOWED, frozenset({"0.46"})) == []


def test_default_quoted_is_empty_and_old_behaviour_unchanged():
    assert money_violations("EPS of 0.46", ALLOWED)
    assert money_violations("entry 102.00", ALLOWED) == []


# ---------------- validator and agents ----------------

def test_validator_accepts_evidence_quotes_and_rejects_count_only():
    v = make_validator(PKT, BullCase)
    ok = BullCase(**bull_json(argument="up 7.47% with relative volume 1.64",
                              key_points=["EPS of 0.46"]))
    assert v(ok) is None
    bad = BullCase(**bull_json(argument="target 110.25"))
    stored, hint = v(bad)
    assert stored == "case states 1 money value(s) not in the decision packet"
    assert "110.25" not in stored and "110.25" in hint


def test_retry_is_recorded_with_count_only_reason():
    c = FakeClient([reply(bull_json(argument="target 110.25")), reply(bull_json())])
    r = run_bull(PKT, client=c, config=AgentConfig())
    assert r.ok and len(c.calls) == 2
    assert r.retries == [RetryRecord(
        attempt=1, reason="case states 1 money value(s) not in the decision packet",
        reason_code="invalid_output")]
    assert "110.25" not in json.dumps([x.model_dump() for x in r.retries])


def test_no_retry_when_first_attempt_passes():
    c = FakeClient([reply(bull_json(argument="up 7.47% on the day"))])
    r = run_bull(PKT, client=c, config=AgentConfig())
    assert r.ok and r.retries == [] and len(c.calls) == 1


def test_failed_bear_keeps_retry_record():
    bad = bear_json(objections=["stop 1.11", "b", "c"])
    r = run_bear(PKT, client=FakeClient([reply(bad), reply(bad)]), config=AgentConfig())
    assert not r.ok and [x.attempt for x in r.retries] == [1]


def test_debate_turn_records_carry_retries():
    bad, good = bull_json(argument="target 110.25"), bull_json()
    rec = run_debate(PKT, clients=FakeClient([reply(bad), reply(good), reply(bear_json())]),
                     configs={"bull": AgentConfig(), "bear": AgentConfig()}, rounds=1)
    bull_turn, bear_turn = rec.turns
    assert [r.attempt for r in bull_turn.run.retries] == [1]
    assert bear_turn.run.retries == []


def test_analyst_retry_is_recorded():
    run, _ = go([call_news(), end(symbol="ZZZZ"), end()])
    assert run.ok
    assert [(r.attempt, r.reason_code, r.reason) for r in run.retries] == [
        (1, "invalid_output", "symbol must be ABCD")]


def test_prompts_allow_verbatim_evidence_quotes():
    from agents.bear import PROMPT as BEAR
    from agents.bull import PROMPT as BULL
    for p in (BULL, BEAR):
        assert "analyst report's `evidence`" in p and "never round, reformat or derive" in p


# ---------------- iteration models on fallback calls ----------------

def usage(*iters):
    return {"input_tokens": 300, "output_tokens": 70, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "iterations": list(iters)}


def it(type_, model=None):
    d = {"type": type_, "input_tokens": 1, "output_tokens": 1,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    if model:
        d["model"] = model
    return d


FB_BLOCK = {"type": "fallback", "from": {"model": OPUS}, "to": {"model": SONNET},
            "trigger": {"type": "refusal", "category": "cyber"}}


def test_fallback_call_fills_declined_hop_model_from_fallback_block():
    # The API leaves `model` off the declined hop's `message` entry (seen live: model=None).
    msg = sdk_message(content=[FB_BLOCK, {"type": "text", "text": "{}"}],
                      usage=usage(it("message"), it("fallback_message", SONNET)))
    rec = call_record(msg, OPUS)
    assert [(i.type, i.model) for i in rec.iterations] == [
        ("message", OPUS), ("fallback_message", SONNET)]
    assert rec.serving_model == SONNET and rec.fallback_ran and rec.fallback_from == OPUS


def test_two_hop_fallback_maps_blocks_in_order():
    haiku = "claude-haiku-5-5"
    blocks = [FB_BLOCK, {"type": "fallback", "from": {"model": SONNET}, "to": {"model": haiku},
                         "trigger": {"type": "refusal", "category": "cyber"}}]
    msg = sdk_message(content=[*blocks, {"type": "text", "text": "{}"}],
                      usage=usage(it("message"), it("message"), it("fallback_message", haiku)))
    rec = call_record(msg, OPUS)
    assert [i.model for i in rec.iterations] == [OPUS, SONNET, haiku]
    assert rec.serving_model == haiku


def test_plain_call_iteration_gets_response_model():
    msg = sdk_message(model=SONNET, usage=usage(it("message")))
    assert [i.model for i in call_record(msg, SONNET).iterations] == [SONNET]


def test_explicit_iteration_model_is_kept():
    msg = sdk_message(content=[FB_BLOCK, {"type": "text", "text": "{}"}],
                      usage=usage(it("message", "claude-x"), it("fallback_message", SONNET)))
    assert [i.model for i in call_record(msg, OPUS).iterations] == ["claude-x", SONNET]


def test_unmatched_hop_count_leaves_blank_rather_than_guess():
    msg = sdk_message(content=[FB_BLOCK, {"type": "text", "text": "{}"}],
                      usage=usage(it("message"), it("message"), it("fallback_message", SONNET)))
    assert [i.model for i in call_record(msg, OPUS).iterations] == [None, None, SONNET]


# ---------------- 8-K item 9.01 ----------------

def test_item_9_01_is_never_a_catalyst_but_stays_in_items():
    subs = load_fixture("sec_submissions_tsla.json")
    rows = N.parse_8k_filings(subs, datetime(2000, 1, 1, tzinfo=NY))
    with_901 = [r for r in rows if "9.01" in r["items"]]
    assert with_901, "fixture should contain 9.01 filings"
    for r in rows:
        assert "item_9.01" not in r["catalysts"]
    assert any(r["catalysts"] == ["earnings"] for r in with_901 if "2.02" in r["items"])


def test_filing_with_only_item_9_01_has_no_catalysts():
    subs = load_fixture("sec_submissions_tsla.json")
    recent = subs["filings"]["recent"]
    recent["items"] = ["9.01"] * len(recent["items"])
    rows = N.parse_8k_filings(subs, datetime(2000, 1, 1, tzinfo=NY))
    assert rows and all(r["catalysts"] == [] and r["red_flags"] == [] for r in rows)


def test_9_01_is_listed_as_a_non_catalyst_item():
    assert "9.01" in N.NON_CATALYST_ITEMS and "9.01" not in N.ITEM_CATALYSTS

