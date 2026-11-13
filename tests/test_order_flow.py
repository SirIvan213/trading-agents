"""Order flow (issues #64, #65, #85): plan_id -> ticket -> typed yes -> re-check -> journal.

Offline: a hand-built sealed ledger, a tmp_path journal and a fake clock. No model, no network.
"""
import dataclasses
import inspect
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

import pipeline.order_flow as of
from journal.log import APPROVED, Journal, JournalError
from journal.log import APPROVED as APPROVED_STATUS
from pipeline.ledger import PlanLedger
from pipeline.order_flow import (
    Proposal,
    approval_prompt,
    approve,
    propose,
    render_ticket,
    wall_clock,
)
from tests.order_helpers import (
    AS_OF,
    T0,
    StepClock,
    approved_entry,
    closed_trade,
    events_clear,
    journal_with,
    plan,
    quotes_at,
    raw_lines,
    sealed_ledger,
    sent_order,
)
from tools.config import CONFIG_DIR, load_limits

LIMITS = load_limits(CONFIG_DIR / "limits.example.json")


@pytest.fixture(autouse=True)
def fixed_limits(monkeypatch):
    """The example limits, whatever config/limits.json holds on this machine."""
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: LIMITS)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "decisions.jsonl")


@pytest.fixture
def ledger():
    return sealed_ledger()


def clock_at(t):
    return lambda: t


def yes(_prompt: str) -> str:
    return "yes"


def never_ask(_prompt: str) -> str:
    raise AssertionError("the human must not be asked")


def good(ledger, journal, t=T0) -> Proposal:
    p = propose(ledger, "plan-1", journal=journal, clock=clock_at(t), quotes=quotes_at(t),
        events=events_clear())
    assert p.ok, p.reason
    return p


# ===================== propose: the happy path =====================

def test_propose_sizes_and_passes(ledger, journal):
    p = good(ledger, journal)
    assert p.ok and p.stage is None and p.reason is None
    assert p.plan_id == "plan-1" and p.run_id == ledger.run_id
    assert p.order == {
        "symbol": "TSLA", "side": "buy", "qty": 8, "type": "market", "order_class": "bracket",
        "entry": "364.18", "stop_loss": "358.32", "take_profit": "370.04", "slippage": "0.37"}
    # #68: allowance 0.37 (0.10% of entry beats the 0.02 spread): (5.86 + 0.37) x 8 = 49.84
    assert p.sized["max_loss"] == "49.84" and p.verdict == {"verdict": "PASS", "reasons": []}
    assert p.checked_at == T0.isoformat()


def test_propose_reads_the_clock_once_and_never_the_plan_date(ledger, journal):
    clock = StepClock(T0)
    propose(ledger, "plan-1", journal=journal, clock=clock, quotes=quotes_at(T0),
        events=events_clear())
    assert clock.calls == 1


def test_propose_does_not_write_the_journal(ledger, journal):
    good(ledger, journal)
    assert not journal.path.exists()


def test_proposal_is_frozen(ledger, journal):
    p = good(ledger, journal)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.ok = False  # type: ignore[misc]


def test_wall_clock_is_timezone_aware_utc():
    now = wall_clock()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


# ===================== propose: every refusal stage =====================

def refused(p: Proposal, stage: str, reason_part: str):
    assert p.ok is False and p.stage == stage, (p.stage, p.reason)
    assert reason_part in (p.reason or "")
    assert p.order is None


@pytest.mark.parametrize("bad", ["plan-9", "", "PLAN-1", "plan-1 ", None, 1, ["plan-1"]])
def test_unknown_plan_id(ledger, journal, bad):
    p = propose(ledger, bad, journal=journal, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())  # type: ignore[arg-type]
    refused(p, "plan", "unknown plan_id")
    assert p.plan is None and p.sized is None and p.verdict is None


def test_unsealed_ledger(journal):
    led = PlanLedger("TSLA")
    led.record(plan())
    refused(propose(led, "plan-1", journal=journal, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear()), "plan", "unknown")


def test_sealed_but_not_accepted_ledger(journal):
    refused(propose(sealed_ledger(accept=False), "plan-1", journal=journal, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear()),
            "plan", "unknown")


def test_not_a_ledger_at_all(journal):
    p = propose(object(), "plan-1", journal=journal, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())  # type: ignore[arg-type]
    refused(p, "plan", "unknown plan_id")
    assert p.run_id == ""


def test_stale_plan(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=clock_at(AS_OF + timedelta(seconds=301)),
            quotes=quotes_at(AS_OF + timedelta(seconds=301)), events=events_clear())
    refused(p, "size", "301s old")
    assert p.plan is not None and p.verdict is None


def test_plan_age_boundary_exactly_max_plan_age_s_passes(ledger, journal):
    at_limit = propose(ledger, "plan-1", journal=journal,
                       clock=clock_at(AS_OF + timedelta(seconds=LIMITS.max_plan_age_s)),
                               quotes=quotes_at(AS_OF + timedelta(seconds=LIMITS.max_plan_age_s)),
                                   events=events_clear())
    assert at_limit.ok
    over = propose(ledger, "plan-1", journal=journal,
                   clock=clock_at(AS_OF + timedelta(seconds=LIMITS.max_plan_age_s + 1)),
                           quotes=quotes_at(AS_OF + timedelta(seconds=LIMITS.max_plan_age_s + 1)),
                               events=events_clear())
    refused(over, "size", "old")


def test_plan_dated_in_the_future(ledger, journal):
    t = AS_OF - timedelta(seconds=60)
    refused(propose(ledger, "plan-1", journal=journal, clock=clock_at(t), quotes=quotes_at(t),
        events=events_clear()),
            "size", "future")


def test_plan_from_another_session_day(ledger, journal):
    next_day = AS_OF + timedelta(days=1)
    refused(propose(ledger, "plan-1", journal=journal, clock=clock_at(next_day),
            quotes=quotes_at(next_day), events=events_clear()),
            "size", "another session day")


def test_risk_block_outside_entry_hours(journal):
    early = AS_OF.replace(hour=9, minute=31)
    led = sealed_ledger([plan(as_of=early.isoformat())])
    p = propose(led, "plan-1", journal=journal, clock=clock_at(early + timedelta(seconds=10)),
            quotes=quotes_at(early + timedelta(seconds=10)), events=events_clear())
    refused(p, "risk", "entry")
    assert p.sized is not None and p.verdict["verdict"] == "BLOCK"


def test_entry_window_start_boundary_passes(journal):
    start = AS_OF.replace(hour=9, minute=45)
    led = sealed_ledger([plan(as_of=start.isoformat())])
    assert propose(led, "plan-1", journal=journal, clock=clock_at(start),
            quotes=quotes_at(start), events=events_clear()).ok
    before = start - timedelta(minutes=1)
    led2 = sealed_ledger([plan(as_of=before.isoformat())])
    refused(propose(led2, "plan-1", journal=journal, clock=clock_at(before),
            quotes=quotes_at(before), events=events_clear()), "risk", "entry")


def test_market_closed_weekend_is_blocked(journal):
    sat = AS_OF.replace(day=24)  # Saturday 2026-10-24
    led = sealed_ledger([plan(as_of=sat.isoformat())])
    p = propose(led, "plan-1", journal=journal, clock=clock_at(sat + timedelta(seconds=5)),
            quotes=quotes_at(sat + timedelta(seconds=5)), events=events_clear())
    assert p.ok is False and p.stage == "risk"


def test_risk_block_when_journal_is_at_max_trades_per_day(ledger, journal):
    n = LIMITS.max_trades_per_day
    for i in range(n - 1):  # one below the limit: still passes (each trade is closed)
        for e in closed_trade(T0 - timedelta(minutes=10 * (i + 1))):
            journal.append(e)
    assert propose(ledger, "plan-1", journal=journal, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear()).ok
    for e in closed_trade(T0 - timedelta(minutes=10 * (n + 1))):
        journal.append(e)
    p = propose(ledger, "plan-1", journal=journal, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    refused(p, "risk", f"already {n} trades today")


def test_risk_block_on_daily_loss_limit_boundary(ledger, tmp_path):
    # worst-case loss of this order is 49.84 (#68); 100.16 used makes exactly 150.00: allowed
    # (an earlier trade today in another symbol, stopped out: its realized loss counts, #188)
    j_ok = journal_with(tmp_path / "a.jsonl", closed_trade(
        T0 - timedelta(hours=1), "100.16", pnl="-100.16", closed_by="stop", symbol="AAPL"))
    assert propose(ledger, "plan-1", journal=j_ok, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear()).ok
    j_bad = journal_with(tmp_path / "b.jsonl", closed_trade(
        T0 - timedelta(hours=1), "100.17", pnl="-100.17", closed_by="stop", symbol="AAPL"))
    refused(propose(ledger, "plan-1", journal=j_bad, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear()), "risk", "daily limit")


def test_yesterdays_approvals_do_not_block_today(ledger, journal):
    for i in range(5):
        journal.append(approved_entry(T0 - timedelta(days=1, minutes=i), "100.00"))
    assert propose(ledger, "plan-1", journal=journal, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear()).ok


def test_invalid_limits(ledger, journal, monkeypatch):
    def boom(*a, **k):
        raise ValueError("limits config is invalid")

    monkeypatch.setattr(of, "load_checked_limits", boom)
    p = propose(ledger, "plan-1", journal=journal, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    refused(p, "limits", "limits config is invalid")
    assert p.plan is None and p.sized is None


def test_paper_false_limits_block(ledger, journal, monkeypatch):
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: replace(LIMITS, paper=False))
    p = propose(ledger, "plan-1", journal=journal, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    refused(p, "risk", "paper")


@pytest.mark.parametrize("junk", ["not json\n", "[1]\n", '{"status": "approved_not_sent"}\n'])
def test_corrupt_or_malformed_journal(ledger, journal, junk):
    journal.path.write_text(junk, encoding="utf-8")
    p = propose(ledger, "plan-1", journal=journal, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    refused(p, "journal", "journal")
    assert p.sized is not None and p.verdict is None  # sized, but never checked or ordered


def test_naive_clock(ledger, journal):
    p = propose(ledger, "plan-1", journal=journal, clock=clock_at(T0.replace(tzinfo=None)),
            quotes=quotes_at(T0.replace(tzinfo=None)), events=events_clear())
    refused(p, "plan", "timezone-aware")


def test_zero_negative_and_inverted_plans_never_size(journal):
    bad = [
        plan(entry="0.00", stop="-5.86", risk_per_share="5.86"),
        plan(stop="364.18", risk_per_share="0.00"),  # stop at entry
        plan(stop="365.00", risk_per_share="-0.82"),  # stop above entry
        plan(target="364.18"),  # target at entry
        plan(risk_per_share="1.00"),  # risk does not match entry - stop
    ]
    for p in bad:
        out = propose(sealed_ledger([p]), "plan-1", journal=journal, clock=clock_at(T0),
                quotes=quotes_at(T0), events=events_clear())
        assert out.ok is False and out.stage == "size" and out.order is None


# ===================== ticket =====================

def test_ticket_names_everything_a_human_must_check(ledger, journal):
    p = good(ledger, journal)
    t = render_ticket(p)
    for part in ("Buy 8 shares of TSLA", "358.32", "370.04", "364.18", "49.84", "0.37",
                 "Worst-case loss", "plan-1",
                 ledger.run_id, "NOT sent to a broker", "paper account", "15:55"):
        assert part in t, part
    assert "max_loss_per_trade" in t  # which limit decided the size


def test_ticket_for_a_refusal_says_no_order(ledger, journal):
    p = propose(ledger, "plan-9", journal=journal, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    t = render_ticket(p)
    assert t.startswith("NO ORDER for plan plan-9") and "unknown plan_id" in t
    assert "Buy" not in t
    none = propose(ledger, None, journal=journal,  # type: ignore[arg-type]
                   clock=clock_at(T0), quotes=quotes_at(T0), events=events_clear())
    assert render_ticket(none).startswith("NO ORDER for plan (none)")


def test_approval_prompt_names_the_exact_order(ledger, journal):
    q = approval_prompt(good(ledger, journal))
    assert "BUY 8 TSLA" in q and "stop 358.32" in q and "target 370.04" in q and "yes" in q


def test_ask_gets_the_ticket_and_the_prompt(ledger, journal):
    p = good(ledger, journal)
    seen = []
    approve(p, ledger, journal=journal, ask=lambda q: seen.append(q) or "no",
            clock=clock_at(T0), quotes=quotes_at(T0), events=events_clear())
    assert seen == [render_ticket(p) + "\n" + approval_prompt(p)]


# ===================== approve: what counts as yes =====================

@pytest.mark.parametrize("answer", ["yes", " yes \n", "yes\n", "\tyes", "  yes  "])
def test_only_exact_yes_approves(ledger, journal, answer):
    out = approve(good(ledger, journal), ledger, journal=journal, ask=lambda _: answer,
                  clock=clock_at(T0 + timedelta(seconds=10)),
                          quotes=quotes_at(T0 + timedelta(seconds=10)), events=events_clear())
    assert out.status == APPROVED and out.reason is None


@pytest.mark.parametrize("answer", [
    "Yes", "YES", "y", "Y", "yes please", "yes!", "yess", "no", "", "   ", "ye", "yes yes",
    "yes\nno", "oui", "1", "true", "ok", "yes.", "уes",  # last: Cyrillic u
    None, 1, True, b"yes", ["yes"], ("yes",), {"yes"}, object(),
])
def test_everything_else_declines(ledger, journal, answer):
    out = approve(good(ledger, journal), ledger, journal=journal, ask=lambda _: answer,
                  clock=clock_at(T0), quotes=quotes_at(T0), events=events_clear())
    assert out.status == "declined" and out.reason == "not approved"
    assert out.recheck is None
    entries = raw_lines(journal)
    assert [e["status"] for e in entries] == ["declined"]
    assert journal.day_state(T0).trades_today == 0


def test_eof_declines(ledger, journal):
    def eof(_):
        raise EOFError

    out = approve(good(ledger, journal), ledger, journal=journal, ask=eof, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())
    assert out.status == "declined"
    assert [e["status"] for e in raw_lines(journal)] == ["declined"]


def test_a_decline_does_not_re_read_the_clock(ledger, journal):
    clock = StepClock(T0)
    approve(good(ledger, journal), ledger, journal=journal, ask=lambda _: "no", clock=clock,
            quotes=quotes_at(T0), events=events_clear())
    assert clock.calls == 0


def test_a_not_ok_proposal_never_asks(ledger, journal):
    p = propose(ledger, "plan-9", journal=journal, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    out = approve(p, ledger, journal=journal, ask=never_ask, clock=StepClock(T0),
                  quotes=quotes_at(T0), events=events_clear())
    assert out.status == "no_order" and out.reason == "unknown plan_id"
    assert out.proposal is p and out.recheck is None
    # audited as "no_order" with its stage; never approved
    (entry,) = journal.entries()
    assert (entry["status"], entry["stage"], entry["order"]) == ("no_order", "plan", None)


@pytest.mark.parametrize("stage_case", ["stale", "risk", "limits", "journal"])
def test_every_refused_proposal_never_asks(ledger, journal, monkeypatch, stage_case):
    t = T0
    if stage_case == "stale":
        t = AS_OF + timedelta(seconds=999)
    elif stage_case == "risk":
        for _ in range(LIMITS.max_trades_per_day):
            journal.append(approved_entry(T0, "1.00"))
    elif stage_case == "limits":
        monkeypatch.setattr(of, "load_checked_limits",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    elif stage_case == "journal":
        journal.path.write_text("garbage\n", encoding="utf-8")
    p = propose(ledger, "plan-1", journal=journal, clock=clock_at(t), quotes=quotes_at(t),
        events=events_clear())
    assert p.ok is False
    before = journal.path.read_text(encoding="utf-8") if journal.path.exists() else ""
    out = approve(p, ledger, journal=journal, ask=never_ask, clock=clock_at(t), quotes=quotes_at(t),
        events=events_clear())
    assert out.status == "no_order"
    after = journal.path.read_text(encoding="utf-8") if journal.path.exists() else ""
    added = after[len(before):].splitlines()
    assert after.startswith(before) and len(added) == 1  # one audit line, nothing else
    assert '"status": "no_order"' in added[0] and APPROVED_STATUS not in added[0]
    assert f'"stage": "{p.stage}"' in added[0]


# ===================== approve: the re-check =====================

def test_approval_re_reads_the_clock_and_records_the_new_time(ledger, journal):
    p = good(ledger, journal)
    clock = StepClock(T0 + timedelta(seconds=45))
    out = approve(p, ledger, journal=journal, ask=yes, clock=clock,
                  quotes=quotes_at(T0 + timedelta(seconds=45)), events=events_clear())
    assert clock.calls == 1
    assert out.status == APPROVED
    assert out.approved_at == (T0 + timedelta(seconds=45)).isoformat() != p.checked_at
    assert out.plan_age_s == 75  # 30 s at propose + 45 s more
    assert out.recheck.ok and out.proposal is p


def test_clock_past_max_plan_age_between_propose_and_approve_refuses(ledger, journal):
    p = good(ledger, journal)  # age 30 s: fresh
    late = AS_OF + timedelta(seconds=LIMITS.max_plan_age_s + 1)
    out = approve(p, ledger, journal=journal, ask=yes, clock=clock_at(late), quotes=quotes_at(late),
        events=events_clear())
    assert out.status == "refused_after_approval"
    assert "re-check failed at size" in out.reason and "old" in out.reason
    assert out.recheck.ok is False and out.recheck.stage == "size"
    statuses = [e["status"] for e in raw_lines(journal)]
    assert statuses == ["refused_after_approval"] and APPROVED not in statuses
    assert journal.day_state(late).trades_today == 0


def test_answer_slow_but_inside_the_age_limit_still_approves(ledger, journal):
    p = good(ledger, journal)
    edge = AS_OF + timedelta(seconds=LIMITS.max_plan_age_s)
    out = approve(p, ledger, journal=journal, ask=yes, clock=clock_at(edge), quotes=quotes_at(edge),
        events=events_clear())
    assert out.status == APPROVED and out.plan_age_s == LIMITS.max_plan_age_s


def test_session_day_rolls_over_during_the_prompt(ledger, journal):
    p = good(ledger, journal)
    out = approve(p, ledger, journal=journal, ask=yes, clock=clock_at(AS_OF + timedelta(days=1)),
            quotes=quotes_at(AS_OF + timedelta(days=1)), events=events_clear())
    assert out.status == "refused_after_approval" and "another session day" in out.reason


def test_re_check_naive_clock_refuses(ledger, journal):
    p = good(ledger, journal)
    out = approve(p, ledger, journal=journal, ask=yes,
                  clock=clock_at(T0.replace(tzinfo=None)),
                          quotes=quotes_at(T0.replace(tzinfo=None)), events=events_clear())
    assert out.status == "refused_after_approval"
    assert APPROVED not in [e["status"] for e in raw_lines(journal)]


def test_re_check_block_when_max_trades_reached_while_waiting(ledger, journal):
    p = good(ledger, journal)

    def ask_and_race(_):
        for i in range(LIMITS.max_trades_per_day):  # three other orders are sent meanwhile
            for e in sent_order(T0 + timedelta(seconds=i), "1.00"):
                journal.append(e)
        return "yes"

    out = approve(p, ledger, journal=journal, ask=ask_and_race, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())
    assert out.status == "refused_after_approval" and out.recheck.stage == "risk"
    assert "already 3 trades today" in out.reason
    statuses = [e["status"] for e in raw_lines(journal)]
    assert statuses.count(APPROVED) == 3 and statuses[-1] == "refused_after_approval"
    assert journal.day_state(T0).trades_today == 3  # unchanged: ours was not counted


def test_re_check_block_when_daily_loss_reached_while_waiting(ledger, journal):
    p = good(ledger, journal)

    def ask_and_race(_):
        for e in sent_order(T0, "110.00"):  # 110 + 49.84 > 150
            journal.append(e)
        return "yes"

    out = approve(p, ledger, journal=journal, ask=ask_and_race, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())
    assert out.status == "refused_after_approval" and "daily limit" in out.reason
    assert journal.day_state(T0).trades_today == 1


def test_re_check_corrupt_journal_refuses(ledger, journal):
    p = good(ledger, journal)

    def ask_and_corrupt(_):
        journal.path.write_text("garbage\n", encoding="utf-8")
        return "yes"

    out = approve(p, ledger, journal=journal, ask=ask_and_corrupt, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())
    assert out.status == "refused_after_approval" and out.recheck.stage == "journal"
    assert journal.path.read_text(encoding="utf-8").splitlines()[0] == "garbage"
    with pytest.raises(JournalError):  # the journal stays unreadable; nothing counted as approved
        journal.entries()


def test_re_check_invalid_limits_refuses(ledger, journal, monkeypatch):
    p = good(ledger, journal)

    def ask_and_break(_):
        def boom(*a, **k):
            raise ValueError("limits config is invalid")
        monkeypatch.setattr(of, "load_checked_limits", boom)
        return "yes"

    out = approve(p, ledger, journal=journal, ask=ask_and_break, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())
    assert out.status == "refused_after_approval" and out.recheck.stage == "limits"


def test_order_changed_between_ticket_and_re_check(ledger, journal, monkeypatch):
    p = good(ledger, journal)
    assert p.order["qty"] == 8
    tighter = replace(LIMITS, max_loss_per_trade=D("25.00"))  # qty becomes 4
    seen = []

    def second_time_different(*a, **k):
        seen.append(1)
        return tighter

    monkeypatch.setattr(of, "load_checked_limits", second_time_different)
    out = approve(p, ledger, journal=journal, ask=yes, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    assert out.status == "changed_after_approval"
    assert out.reason == "the re-built order differs from the approved ticket"
    assert out.recheck.ok and out.recheck.order["qty"] == 4 and out.proposal.order["qty"] == 8
    statuses = [e["status"] for e in raw_lines(journal)]
    assert statuses == ["changed_after_approval"]
    assert journal.day_state(T0).trades_today == 0


def test_order_changed_when_limits_differ_on_the_second_load_only(journal, monkeypatch):
    """The scenario from the brief: load_checked_limits returns different limits on call 2."""
    led = sealed_ledger()
    calls = []

    def flip(*a, **k):
        calls.append(1)
        return LIMITS if len(calls) == 1 else replace(LIMITS, max_position_value=D("1000.00"))

    monkeypatch.setattr(of, "load_checked_limits", flip)
    p = propose(led, "plan-1", journal=journal, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    assert p.ok and p.order["qty"] == 8
    out = approve(p, led, journal=journal, ask=yes, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    assert len(calls) == 2
    assert out.status == "changed_after_approval" and out.recheck.order["qty"] == 2


def test_plan_changed_between_ticket_and_re_check(ledger, journal):
    p = good(ledger, journal)
    other = sealed_ledger([plan(entry="364.00", stop="358.14", risk_per_share="5.86",
                                target="369.86", last_close="364.00")])
    # ^ a different plan under the same id
    out = approve(p, other, journal=journal, ask=yes, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    assert out.status == "changed_after_approval"
    assert APPROVED not in [e["status"] for e in raw_lines(journal)]


def test_re_check_against_a_ledger_without_the_plan_refuses(ledger, journal):
    p = good(ledger, journal)
    out = approve(p, sealed_ledger([]), journal=journal, ask=yes, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())
    assert out.status == "refused_after_approval" and out.recheck.stage == "plan"


def test_ask_cannot_alter_the_order_it_approves(ledger, journal):
    """A human's answer is a string; whatever the ask function does to p, p is frozen."""
    p = good(ledger, journal)
    order_before = json.dumps(p.order, sort_keys=True)

    def tamper(_):
        p.order["qty"] = 8000  # the dict is shared; the re-check must not trust it
        return "yes"

    out = approve(p, ledger, journal=journal, ask=tamper, clock=clock_at(T0), quotes=quotes_at(T0),
        events=events_clear())
    assert out.status == "changed_after_approval"  # re-built order differs from tampered ticket
    assert APPROVED not in [e["status"] for e in raw_lines(journal)]
    p.order["qty"] = 8
    assert json.dumps(p.order, sort_keys=True) == order_before


# ===================== the approved journal entry =====================

def approved(ledger, journal, t=T0 + timedelta(seconds=20)):
    out = approve(good(ledger, journal), ledger, journal=journal, ask=yes, clock=clock_at(t),
            quotes=quotes_at(t), events=events_clear())
    assert out.status == APPROVED
    (entry,) = raw_lines(journal)
    return out, entry


def test_approved_entry_holds_order_max_loss_age_and_run_id(ledger, journal):
    out, e = approved(ledger, journal)
    assert e["status"] == "approved_not_sent" and e["reason"] is None
    assert e["order"] == out.recheck.order and e["order"]["qty"] == 8
    assert e["max_loss"] == "49.84" and e["plan_age_s"] == 50
    assert e["run_id"] == ledger.run_id and e["plan_id"] == "plan-1" and e["symbol"] == "TSLA"
    assert e["decided_by"] == "max_loss_per_trade"
    assert e["verdict"] == {"verdict": "PASS", "reasons": []}
    assert e["approved_at"] == e["checked_at"] == (T0 + timedelta(seconds=20)).isoformat()


def test_approved_entry_has_exactly_these_keys(ledger, journal):
    _, e = approved(ledger, journal)
    assert set(e) == {"status", "reason", "run_id", "plan_id", "symbol", "order", "max_loss",
                      "decided_by", "quote", "verdict", "checked_at", "approved_at",
                      "plan_age_s", "gate", "client_order_id", "expires_at"}


def test_approved_entry_carries_the_deterministic_client_order_id(ledger, journal):
    from journal.orders import client_order_id
    _, e = approved(ledger, journal)
    assert e["client_order_id"] == client_order_id(ledger.run_id, "plan-1")


def test_no_journal_entry_ever_holds_confidence_reasoning_or_evidence(ledger, journal):
    approve(good(ledger, journal), ledger, journal=journal, ask=lambda _: "no", clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())
    approve(good(ledger, journal), ledger, journal=journal, ask=yes,
            clock=clock_at(AS_OF + timedelta(days=1)),
                    quotes=quotes_at(AS_OF + timedelta(days=1)),
                        events=events_clear())  # refused_after_approval

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)

    banned = ("confidence", "reasoning", "evidence", "argument", "objection", "stance",
              "catalyst", "point", "report", "debate", "decision")
    seen = {k.lower() for e in raw_lines(journal) for k in keys(e)}
    assert seen and not [k for k in seen if any(b in k for b in banned)]
    text = journal.path.read_text(encoding="utf-8").lower()
    assert "confidence" not in text and "reasoning" not in text


def test_an_unsent_approval_holds_its_risk_and_a_position_but_no_trade_slot(ledger, journal):
    """Ivan's decision (#109): until its plan expires, an approval not yet sent counts as an
    open position with its worst-case risk; only sending takes a trade slot."""
    approved(ledger, journal)
    s = journal.day_state(T0 + timedelta(seconds=20))
    assert s.trades_today == 0 and s.risk_used_today == D("49.84") and s.open_positions == 1


def test_journal_write_failure_after_approval_raises_and_counts_nothing(ledger, tmp_path):
    class Failing(Journal):
        def append(self, entry):
            if entry["status"] == APPROVED:
                raise JournalError("journal write failed (OSError)")
            super().append(entry)

    j = Failing(tmp_path / "f.jsonl")
    p = good(ledger, j)
    with pytest.raises(JournalError):
        approve(p, ledger, journal=j, ask=yes, clock=clock_at(T0), quotes=quotes_at(T0),
            events=events_clear())
    assert j.day_state(T0).trades_today == 0


def test_third_approval_of_the_day_blocks_the_fourth(journal):
    """End to end through the journal: approvals consume the day's trade budget. Each trade
    is a new run (a plan gets one order) and is closed before the next (one position at a
    time, #109)."""
    from journal.orders import client_order_id
    for i in range(LIMITS.max_trades_per_day):  # 3 x 49.84 = 149.52, inside the 150 limit
        led, t = sealed_ledger(), T0 + timedelta(seconds=i)
        p = propose(led, "plan-1", journal=journal, clock=clock_at(t), quotes=quotes_at(t),
            events=events_clear())
        assert p.ok, p.reason
        assert approve(p, led, journal=journal, ask=yes, clock=clock_at(t),
                quotes=quotes_at(t), events=events_clear()).status == APPROVED
        cid = client_order_id(led.run_id, "plan-1")
        journal.record_order_event(cid, "submitting", t)
        journal.record_order_event(cid, "submitted", t)
        journal.record_order_event(cid, "filled", t, filled_qty=8, avg_price="364.18")
        journal.record_order_event(cid, "closed", t, closed_by="target", realized_pnl="5.00")
    t = T0 + timedelta(seconds=9)
    p = propose(sealed_ledger(), "plan-1", journal=journal, clock=clock_at(t),
                quotes=quotes_at(t), events=events_clear())
    refused(p, "risk", f"already {LIMITS.max_trades_per_day} trades today")


def test_a_plan_is_approved_at_most_once(ledger, journal):
    """#109: the second approval of the same run and plan would reuse its client_order_id."""
    approved(ledger, journal)
    refused(propose(ledger, "plan-1", journal=journal, clock=clock_at(T0),
                    quotes=quotes_at(T0), events=events_clear()),
            "journal", "this plan already has an order (one order per plan)")


# ===================== no model data can reach the order flow =====================

FORBIDDEN_PARAM_PARTS = ("packet", "decision", "confidence", "debate", "trader", "report",
                         "evidence", "reasoning", "stance", "price", "stop", "target", "qty",
                         "size", "limits")


def params(fn):
    sig = inspect.signature(fn)
    kinds = {p.kind for p in sig.parameters.values()}
    assert not kinds & {inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL}
    return list(sig.parameters)


def test_propose_signature_pinned():
    assert params(propose) == ["ledger", "plan_id", "journal", "clock", "quotes", "events"]
    kw_only = [n for n, p in inspect.signature(propose).parameters.items()
               if p.kind is inspect.Parameter.KEYWORD_ONLY]
    assert kw_only == ["journal", "clock", "quotes", "events"]


def test_approve_signature_pinned():
    assert params(approve) == ["p", "ledger", "journal", "ask", "clock", "quotes", "events"]
    kw_only = [n for n, p in inspect.signature(approve).parameters.items()
               if p.kind is inspect.Parameter.KEYWORD_ONLY]
    assert kw_only == ["journal", "ask", "clock", "quotes", "events"]


def test_ticket_functions_take_only_a_proposal():
    assert params(render_ticket) == ["p"] and params(approval_prompt) == ["p"]


@pytest.mark.parametrize("fn", [propose, approve, render_ticket, approval_prompt])
def test_no_parameter_can_carry_model_output_or_money(fn):
    for name in params(fn):
        assert not any(part in name.lower() for part in FORBIDDEN_PARAM_PARTS), name


@pytest.mark.parametrize("extra", ["packet", "decision", "confidence", "debate", "reasoning"])
def test_extra_arguments_are_a_type_error(ledger, journal, extra):
    with pytest.raises(TypeError):
        propose(ledger, "plan-1", journal=journal, clock=clock_at(T0),
                quotes=quotes_at(T0), **{extra: 0.9}, events=events_clear())
    p = good(ledger, journal)
    with pytest.raises(TypeError):
        approve(p, ledger, journal=journal, ask=yes, clock=clock_at(T0),
                quotes=quotes_at(T0), **{extra: 0.9}, events=events_clear())
    assert not journal.path.exists()


def test_proposal_and_outcome_fields_pinned():
    assert [f.name for f in dataclasses.fields(Proposal)] == [
        "ok", "stage", "reason", "run_id", "plan_id", "plan", "sized", "verdict", "checked_at",
        "expires_at", "gate"]
    assert [f.name for f in dataclasses.fields(of.Outcome)] == [
        "status", "reason", "proposal", "recheck", "approved_at", "plan_age_s"]


def test_a_packet_passed_as_the_ledger_yields_no_order(journal):
    from tests.debate_helpers import make_packet
    p = propose(make_packet(), "p1", journal=journal, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear())  # type: ignore[arg-type]
    assert p.ok is False and p.stage == "plan"


def test_default_ask_is_input_and_default_clock_is_the_wall_clock():
    assert inspect.signature(approve).parameters["ask"].default is input
    assert inspect.signature(approve).parameters["clock"].default is wall_clock
    assert inspect.signature(propose).parameters["clock"].default is wall_clock


def test_plan_age_uses_the_ledger_plan_date_in_new_york_time(journal):
    """The plan's as_of can carry any UTC offset; age is computed on instants."""
    utc_made = AS_OF.astimezone(ZoneInfo("UTC"))
    led = sealed_ledger([plan(as_of=utc_made.isoformat())])
    out = approve(propose(led, "plan-1", journal=journal, clock=clock_at(T0),
            quotes=quotes_at(T0), events=events_clear()), led,
                  journal=journal, ask=yes, clock=clock_at(T0), quotes=quotes_at(T0),
                      events=events_clear())
    assert out.status == APPROVED and out.plan_age_s == 30
