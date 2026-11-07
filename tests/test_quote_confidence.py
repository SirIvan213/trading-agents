"""Debaters may quote an ok analyst report's confidence as written ("capped at 0.6").

Offline: fakes only, no network.
"""
import json

import pytest

from agents.bull import run_bull
from agents.debate_common import build_payload, make_validator
from agents.llm import AgentConfig
from agents.money_guard import allowed_values, money_violations, quoted_values
from agents.schemas import BearCase, BullCase
from tests.debate_helpers import bull_json, reply, user_json
from tests.fakes import FakeClient
from tests.test_issue87 import PKT, run_record

# Exact text from the debug smoke run (the Bull quoting the technical confidence).
SMOKE_TEXTS = ["capped at 0.6", "trade at 0.6"]


@pytest.mark.parametrize("text", SMOKE_TEXTS)
def test_smoke_run_confidence_quotes_pass(text):
    assert money_violations(text, allowed_values(PKT)) != []  # rejected before this change
    assert money_violations(text, allowed_values(PKT), quoted_values(PKT)) == []


def test_bull_case_with_smoke_text_passes_first_try():
    case = bull_json(argument="technical conviction is capped at 0.6",
                     key_points=["even the strongest analyst would only trade at 0.6"])
    c = FakeClient([reply(case)])
    r = run_bull(PKT, client=c, config=AgentConfig())
    assert r.ok and len(c.calls) == 1 and r.retries == []


def test_confidence_is_quoted_exactly_as_written_in_the_payload():
    payload = user_json({"messages": [{"content": build_payload(PKT, None, 1)}]})
    written = json.dumps(payload["reports"]["technical"]["confidence"])
    assert written == "0.6" and written in quoted_values(PKT)


@pytest.mark.parametrize("text", ["capped at 0.60", "capped at 0.7",
                                  "trade at 0.0", "buy 0.65"])
def test_other_forms_and_values_still_rejected(text):
    # "0.0" is the failed fundamentals report's confidence: failed runs contribute nothing
    assert money_violations(text, allowed_values(PKT), quoted_values(PKT)), text


def test_failed_report_confidence_is_not_quotable():
    rep = PKT.reports["technical"].model_copy(update={"confidence": 0.85})
    pkt = PKT.model_copy(update={"reports": {"technical": rep},
                                 "runs": {"technical": run_record("technical", ok=False)}})
    assert "0.85" not in quoted_values(pkt)


def test_validator_accepts_confidence_quote_for_both_sides():
    assert make_validator(PKT, BullCase)(BullCase(**bull_json(argument="trade at 0.6"))) is None
    bear = BearCase(symbol="AAPL", plan_id=None, fatal=False,
                    objections=["conviction capped at 0.6", "thin volume", "late move"])
    assert make_validator(PKT, BearCase)(bear) is None


def test_prompts_say_sizing_is_not_theirs_and_allow_confidence():
    from agents.bear import PROMPT as BEAR
    from agents.bull import PROMPT as BULL
    for p in (BULL, BEAR):
        assert "Position size is not yours to discuss" in p
        assert "sizing is fixed by deterministic code" in p
        assert "`confidence` exactly as written" in p
