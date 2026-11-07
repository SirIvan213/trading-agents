"""Issue #64 done-when: a plan forged in a DecisionPacket is never sized.

Every forgery below is placed in a packet (or carried in JSON). The order path looks plans up in
the pipeline's PlanLedger by id, so each forgery is refused or ignored in favour of the
ledger's own plan. A real plan still sizes. Offline: no model, no network.
"""
import json

import pytest

from agents.schemas import DecisionPacket, ExitPlanRecord
from journal.log import Journal
from pipeline import order_flow as of
from pipeline.ledger import LedgerError
from pipeline.order_flow import approve, propose
from pipeline.orders import build_order, select_plan
from tests.order_helpers import T0, fresh_quote, plan, quotes_at, sealed_ledger
from tests.test_phase4_gate import LIMITS, packet_with

# A self-consistent forgery: passes every price check by itself (stop < entry < target,
# risk = entry - stop, whole cents). Sized directly it would give an order.
QUOTES = quotes_at(T0)  # a live TSLA quote, fresh at T0 (#68)
FORGED = {"entry": "100.00", "stop": "99.00", "target": "101.00", "risk_per_share": "1.00",
          "reward_to_risk": "1.00", "last_close": "100.00"}


@pytest.fixture(autouse=True)
def fixed_limits(monkeypatch):
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "j.jsonl")


@pytest.fixture
def real():
    return plan()


@pytest.fixture
def ledger(real):
    return sealed_ledger([real])


@pytest.fixture
def reference(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=lambda: T0, quotes=QUOTES)
    assert p.ok and p.order["entry"] == "364.18"
    return p


def clock():
    return T0


def same_as_reference(p, reference):
    assert p.ok is True
    assert p.sized == reference.sized and p.plan == reference.plan
    assert p.order["stop_loss"] == "358.32" and p.order["take_profit"] == "370.04"
    assert p.verdict == reference.verdict


def test_the_forgery_really_is_sizable_on_its_own(real):
    """Guard against a vacuous test: sized directly, the forged plan WOULD give an order."""
    forged = real.model_copy(update=FORGED)
    out = build_order(forged, LIMITS, T0, fresh_quote("100.00", T0))
    assert out["ok"] is True and out["order"]["stop_loss"] == "99.00"
    assert ExitPlanRecord.model_validate(forged.model_dump()).stop == "99.00"


def test_model_copy_forgery_in_the_packet_is_ignored(real, ledger, reference, journal):
    forged = real.model_copy(update=FORGED)  # same plan_id, no validation
    packet = packet_with([forged], fake=False)
    assert packet.exit_plans[0].stop == "99.00"
    p = propose(ledger, packet.exit_plans[0].plan_id, journal=journal, clock=clock, quotes=QUOTES)
    same_as_reference(p, reference)
    assert select_plan(ledger, forged.plan_id) == real


def test_model_construct_forgery_in_the_packet_is_ignored(real, ledger, reference, journal):
    forged = ExitPlanRecord.model_construct(**{**real.model_dump(), **FORGED})
    packet = packet_with([forged], fake=False)
    assert packet.exit_plans[0].entry == "100.00"
    p = propose(ledger, packet.exit_plans[0].plan_id, journal=journal, clock=clock, quotes=QUOTES)
    same_as_reference(p, reference)


def test_hand_appended_plan_with_a_new_id_is_refused(real, ledger, journal):
    packet = packet_with([real], fake=False)
    forged = real.model_copy(update={**FORGED, "plan_id": "plan-2"})
    packet.exit_plans.append(forged)
    assert [p.plan_id for p in packet.exit_plans] == ["plan-1", "plan-2"]
    p = propose(ledger, "plan-2", journal=journal, clock=clock, quotes=QUOTES)
    assert p.ok is False and p.stage == "plan" and p.reason == "unknown plan_id"
    assert p.order is None
    with pytest.raises(KeyError):
        select_plan(ledger, "plan-2")
    # the forged object is still a perfectly good-looking plan for build_order
    assert build_order(forged, LIMITS, T0, fresh_quote("100.00", T0))["ok"] is True


def test_packet_rebuilt_from_json_with_altered_prices_is_ignored(real, ledger, reference,
                                                                 journal):
    blob = json.loads(packet_with([real], fake=False).model_dump_json())
    blob["exit_plans"][0].update(FORGED)
    rebuilt = DecisionPacket.model_validate(blob)
    assert rebuilt.exit_plans[0].stop == "99.00"
    p = propose(ledger, rebuilt.exit_plans[0].plan_id, journal=journal, clock=clock, quotes=QUOTES)
    same_as_reference(p, reference)


def test_packet_rebuilt_from_json_with_extra_plan_cannot_order_it(real, ledger, journal):
    blob = json.loads(packet_with([real], fake=False).model_dump_json())
    blob["exit_plans"].append({**blob["exit_plans"][0], **FORGED, "plan_id": "plan-7"})
    rebuilt = DecisionPacket.model_validate(blob)
    assert {p.plan_id for p in rebuilt.exit_plans} == {"plan-1", "plan-7"}
    p = propose(ledger, "plan-7", journal=journal, clock=clock, quotes=QUOTES)
    assert p.ok is False and p.reason == "unknown plan_id"


def test_a_decision_packet_can_never_yield_an_order(real, journal):
    packet = packet_with([real], fake=False)
    rebuilt = DecisionPacket.model_validate_json(packet.model_dump_json())
    for pkt in (packet, rebuilt):
        with pytest.raises(KeyError, match="unknown plan_id"):
            select_plan(pkt, "plan-1")  # type: ignore[arg-type]
        p = propose(pkt, "plan-1", journal=journal, clock=clock, quotes=QUOTES)  # type: ignore[arg-type]
        assert p.ok is False and p.stage == "plan" and p.order is None and p.run_id == ""
        out = approve(p, sealed_ledger(), journal=journal, ask=lambda _: "yes", clock=clock,
                      quotes=QUOTES)
        assert out.status == "no_order"
    entries = journal.entries()  # only "no_order" audit lines, never an approval
    assert [e["status"] for e in entries] == ["no_order", "no_order"]
    assert all(e["order"] is None for e in entries)


def test_forged_plan_cannot_be_recorded_after_the_seal(real, ledger):
    forged = real.model_copy(update={**FORGED, "plan_id": "plan-2"})
    with pytest.raises(LedgerError, match="sealed"):
        ledger.record(forged)
    assert ledger.plan_ids() == ["plan-1"]


def test_forged_plan_cannot_replace_a_recorded_id_before_the_seal(real):
    from pipeline.ledger import PlanLedger
    led = PlanLedger("TSLA")
    led.record(real)
    with pytest.raises(LedgerError, match="already recorded"):
        led.record(real.model_copy(update=FORGED))
    led.seal(accept=True)
    assert led.get("plan-1") == real


def test_mutating_the_ledger_copy_or_the_packet_plan_cannot_change_the_next_order(
        real, ledger, reference, journal):
    got = select_plan(ledger, "plan-1")
    object.__setattr__(got, "stop", "1.00")
    object.__setattr__(real, "target", "999.99")  # the object that was recorded
    p = propose(ledger, "plan-1", journal=journal, clock=clock, quotes=QUOTES)
    same_as_reference(p, reference)


def test_a_real_plan_still_sizes_and_approves(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=clock, quotes=QUOTES)
    assert p.ok and p.order["qty"] == 8
    out = approve(p, ledger, journal=journal, ask=lambda _: "yes", clock=clock, quotes=QUOTES)
    assert out.status == "approved_not_sent"


def test_trader_plan_id_naming_the_forgery_gets_no_order(real, ledger, journal):
    """Even a plan_id the Trader legitimately saw in the (forged) packet is just a string."""
    forged = real.model_copy(update={**FORGED, "plan_id": "plan-forged"})
    packet = packet_with([real, forged], fake=False)
    assert "plan-forged" in {p.plan_id for p in packet.exit_plans}
    p = propose(ledger, "plan-forged", journal=journal, clock=clock, quotes=QUOTES)
    assert p.ok is False and p.stage == "plan"
