import pytest
from pydantic import ValidationError

from agents.schemas import AnalystReport, BearCase, TraderDecision


def test_bear_needs_three_objections():
    with pytest.raises(ValidationError):
        BearCase(symbol="TSLA", objections=["one", "two"], fatal=False)
    BearCase(symbol="TSLA", objections=["a", "b", "c"], fatal=False)


def test_confidence_bounded():
    with pytest.raises(ValidationError):
        TraderDecision(symbol="TSLA", action="BUY", confidence=1.5, reasoning="x")


def test_analyst_report_needs_evidence():
    with pytest.raises(ValidationError):
        AnalystReport(
            agent="technical", symbol="TSLA", stance="bullish", confidence=0.6, evidence=[]
        )
