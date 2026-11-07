"""Phase 5 gate A: #30 (paper is a JSON bool), #31 (check_order fails closed on clock and
state), #32 (trading_enabled kill switch), #107 (journal.exclusive around approval).

Offline: example limits, tmp_path journals, a fake clock. No network, no model.
"""
import copy
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

import pipeline.order_flow as of
from journal.log import APPROVED, Journal, JournalError
from pipeline.order_flow import approve, propose
from tests.order_helpers import T0, raw_lines, sealed_ledger
from tools.config import CONFIG_DIR, Limits, limits_from_dict, load_limits, validate_limits
from tools.risk_check import DayState, check_order
from tools.size import Plan, size_position

NY = ZoneInfo("America/New_York")
EXAMPLE_PATH = CONFIG_DIR / "limits.example.json"
EXAMPLE = json.loads(EXAMPLE_PATH.read_text())
LIMITS = load_limits(EXAMPLE_PATH)
MON_1015 = datetime(2026, 10, 19, 10, 15, tzinfo=NY)
KILL = "trading is disabled (kill switch: trading_enabled is false)"
LIVE = "live trading is disabled in this phase (paper must be true)"


def cfg(**over):
    d = copy.deepcopy(EXAMPLE)
    d.update(over)
    return d


def order(limits=LIMITS):
    sized = size_position(Plan("AAPL", D("20.00"), D("19.00"), D("21.00")), limits)
    assert sized["ok"], sized
    return sized["order"]


_DEFAULT = object()


def check(limits=LIMITS, state=_DEFAULT, now=MON_1015):
    return check_order(order(), limits, DayState() if state is _DEFAULT else state, now)


# ============================ #30 paper ============================

@pytest.mark.parametrize("raw", ["false", "true", "no", 0, 1, "", None], ids=repr)
def test_30_non_bool_paper_rejected(raw):
    with pytest.raises(ValueError, match="paper"):
        limits_from_dict(cfg(paper=raw))


def test_30_missing_paper_still_raises():
    d = cfg()
    del d["paper"]
    with pytest.raises(KeyError):
        limits_from_dict(d)


def test_30_true_passes_false_blocks():
    assert check(limits_from_dict(cfg(paper=True))) == {"verdict": "PASS", "reasons": []}
    r = check(limits_from_dict(cfg(paper=False)))
    assert r == {"verdict": "BLOCK", "reasons": [LIVE]}


@pytest.mark.parametrize("bad", [0, 1, "false", None])
def test_30_hand_built_paper_non_bool_fails_validation_and_blocks(bad):
    lim = replace(LIMITS, paper=bad)
    with pytest.raises(ValueError, match="paper must be a boolean"):
        validate_limits(lim)
    assert LIVE in check(lim)["reasons"]


# ============================ #32 kill switch ============================

def test_32_example_config_has_switch_on():
    assert EXAMPLE["trading_enabled"] is True
    assert LIMITS.trading_enabled is True


def test_32_missing_key_defaults_true():
    d = cfg()
    del d["trading_enabled"]
    assert limits_from_dict(d).trading_enabled is True


@pytest.mark.parametrize("raw", ["false", "true", 0, 1, None, ""], ids=repr)
def test_32_non_bool_rejected(raw):
    with pytest.raises(ValueError, match="trading_enabled"):
        limits_from_dict(cfg(trading_enabled=raw))


def test_32_enabled_true_passes():
    lim = limits_from_dict(cfg(trading_enabled=True))
    assert lim.trading_enabled is True
    assert check(lim) == {"verdict": "PASS", "reasons": []}


def test_32_disabled_false_blocks_with_only_the_kill_reason():
    lim = limits_from_dict(cfg(trading_enabled=False))
    assert lim.trading_enabled is False
    assert check(lim) == {"verdict": "BLOCK", "reasons": [KILL]}


@pytest.mark.parametrize("bad", [0, "false", 1, "true", None])
def test_32_hand_built_non_bool_blocks(bad):
    lim = replace(LIMITS, trading_enabled=bad)
    r = check(lim)
    assert r["verdict"] == "BLOCK" and KILL in r["reasons"]
    with pytest.raises(ValueError, match="trading_enabled must be a boolean"):
        validate_limits(lim)


def test_32_kill_and_paper_both_reported():
    r = check(replace(LIMITS, paper=False, trading_enabled=False))
    assert r["verdict"] == "BLOCK" and LIVE in r["reasons"] and KILL in r["reasons"]


# ---- 32 in the order flow ----

def set_limits(monkeypatch, lim: Limits):
    monkeypatch.setattr(of, "load_checked_limits", lambda *a, **k: lim)


def clock_at(t):
    return lambda: t


def yes(_):
    return "yes"


def statuses(j):
    return [e["status"] for e in raw_lines(j)]


def test_32_flow_propose_with_switch_off_is_refused_at_risk(monkeypatch, tmp_path):
    set_limits(monkeypatch, replace(LIMITS, trading_enabled=False))
    p = propose(sealed_ledger(), "plan-1", journal=Journal(tmp_path / "j.jsonl"),
                clock=clock_at(T0))
    assert p.ok is False and p.stage == "risk" and "kill switch" in p.reason


def test_32_flow_switch_on_throughout_is_approved(monkeypatch, tmp_path):
    set_limits(monkeypatch, LIMITS)
    led, j = sealed_ledger(), Journal(tmp_path / "j.jsonl")
    p = propose(led, "plan-1", journal=j, clock=clock_at(T0))
    assert p.ok
    out = approve(p, led, journal=j, ask=yes, clock=clock_at(T0))
    assert out.status == APPROVED and statuses(j) == [APPROVED]


def test_32_flow_flipped_between_ticket_and_yes_refuses(monkeypatch, tmp_path):
    set_limits(monkeypatch, LIMITS)
    led, j = sealed_ledger(), Journal(tmp_path / "j.jsonl")
    p = propose(led, "plan-1", journal=j, clock=clock_at(T0))
    assert p.ok

    def flip_then_yes(_):
        set_limits(monkeypatch, replace(LIMITS, trading_enabled=False))
        return "yes"

    out = approve(p, led, journal=j, ask=flip_then_yes, clock=clock_at(T0))
    assert out.status == "refused_after_approval" and out.recheck.stage == "risk"
    assert "kill switch" in out.reason
    assert APPROVED not in statuses(j) and statuses(j) == ["refused_after_approval"]


# ============================ #31 fail closed ============================

def blocked_without_raising(**kw):
    r = check(**kw)
    assert r["verdict"] == "BLOCK"
    return r


def test_31_naive_now_blocks():
    r = blocked_without_raising(now=datetime(2026, 10, 19, 10, 15))
    assert "now must be a timezone-aware datetime" in r["reasons"]


@pytest.mark.parametrize("bad", [None, "2026-10-19T10:15:00-04:00", 1.0, 0])
def test_31_non_datetime_now_blocks(bad):
    r = blocked_without_raising(now=bad)
    assert "now must be a timezone-aware datetime" in r["reasons"]


def test_31_aware_now_passes():
    assert check() == {"verdict": "PASS", "reasons": []}
    # an aware non-NY zone is still fine (converted to New York time)
    utc = MON_1015.astimezone(ZoneInfo("UTC"))
    assert check(now=utc) == {"verdict": "PASS", "reasons": []}


@pytest.mark.parametrize("bad", [True, -1, 1.0, None, "1"], ids=repr)
def test_31_bad_trades_today_blocks(bad):
    r = blocked_without_raising(state=DayState(trades_today=bad))
    assert any("trades_today" in x for x in r["reasons"])


@pytest.mark.parametrize(
    "bad", [1.0, -1, D("-1"), D("NaN"), D("Infinity"), None], ids=repr)
def test_31_bad_risk_used_today_blocks(bad):
    r = blocked_without_raising(state=DayState(risk_used_today=bad))
    assert any("risk_used_today" in x for x in r["reasons"])


@pytest.mark.parametrize("bad", [None, {"trades_today": 0, "risk_used_today": D("0")}, (0, D(0))],
                         ids=repr)
def test_31_state_not_a_daystate_blocks(bad):
    r = blocked_without_raising(state=bad)
    assert "day state must be a DayState" in r["reasons"]


def test_31_valid_state_passes():
    assert check(state=DayState(trades_today=2, risk_used_today=D("10.00")))["verdict"] == "PASS"
    assert check(state=DayState(trades_today=0, risk_used_today=D("0")))["verdict"] == "PASS"


def test_31_paper_and_kill_reasons_survive_the_early_return():
    r = check(replace(LIMITS, paper=False, trading_enabled=False),
              now=datetime(2026, 10, 19, 10, 15))
    assert r["verdict"] == "BLOCK"
    assert LIVE in r["reasons"] and KILL in r["reasons"]
    assert "now must be a timezone-aware datetime" in r["reasons"]


# ============================ #107 journal lock ============================

_REAL_EXCLUSIVE = Journal.exclusive


def _with_default_timeout(timeout):
    def exclusive(self, timeout_s=timeout):
        return _REAL_EXCLUSIVE(self, timeout_s)
    return exclusive


@contextmanager
def _no_lock(self, timeout_s=0.0):
    yield


def test_107_single_approver_approved_and_lock_released(monkeypatch, tmp_path):
    set_limits(monkeypatch, LIMITS)
    led, j = sealed_ledger(), Journal(tmp_path / "j.jsonl")
    p = propose(led, "plan-1", journal=j, clock=clock_at(T0))
    out = approve(p, led, journal=j, ask=yes, clock=clock_at(T0))
    assert out.status == APPROVED and statuses(j) == [APPROVED]
    with j.exclusive(timeout_s=0.5):  # same object again
        pass
    with Journal(j.path).exclusive(timeout_s=0.5):  # a different object, same file
        pass


def test_107_second_object_blocked_while_held_then_free(tmp_path):
    a, b = Journal(tmp_path / "j.jsonl"), Journal(tmp_path / "j.jsonl")
    with a.exclusive():
        with pytest.raises(JournalError, match="timed out"):
            with b.exclusive(timeout_s=0.05):
                pytest.fail("must not get the lock")
    with b.exclusive(timeout_s=0.5):
        pass


def test_107_same_object_other_thread_times_out(tmp_path):
    """Another thread using the same Journal object waits for the holder and times out."""
    import threading
    a = Journal(tmp_path / "j.jsonl")
    errors: list[BaseException] = []

    def other() -> None:
        try:
            with a.exclusive(timeout_s=0.05):
                errors.append(AssertionError("second thread got the lock"))
        except JournalError as exc:
            errors.append(exc)

    with a.exclusive():
        t = threading.Thread(target=other)
        t.start()
        t.join(5)
    assert len(errors) == 1 and isinstance(errors[0], JournalError)
    assert "timed out" in str(errors[0])


def test_107_same_thread_same_object_is_reentrant_and_appends(tmp_path):
    """append() inside an approval's exclusive block re-enters the lock (no self-deadlock),
    and the lock is released only when the outer block ends."""
    a = Journal(tmp_path / "j.jsonl")
    b = Journal(tmp_path / "j.jsonl")
    with a.exclusive():
        with a.exclusive(timeout_s=0.05):
            a.append({"status": "x"})
        with pytest.raises(JournalError, match="timed out"):
            with b.exclusive(timeout_s=0.05):
                pass  # still held by the outer block
    with b.exclusive(timeout_s=0.05):
        pass
    assert [e["status"] for e in a.entries()] == ["x"]


def test_107_append_waits_for_the_lock_held_by_another_object(tmp_path, monkeypatch):
    """A plain append (e.g. a "declined" entry) from another writer cannot interleave with
    an approval holding the lock: it times out instead of writing."""
    import journal.log as jl
    monkeypatch.setattr(jl, "LOCK_TIMEOUT_S", 0.05)
    a = Journal(tmp_path / "j.jsonl")
    b = Journal(tmp_path / "j.jsonl")
    with a.exclusive():
        with pytest.raises(JournalError, match="timed out"):
            b.append({"status": "declined"})
    b.append({"status": "declined"})
    assert [e["status"] for e in a.entries()] == ["declined"]


def test_107_approve_with_lock_held_elsewhere_raises_and_approves_nothing(monkeypatch, tmp_path):
    import journal.log as jl
    set_limits(monkeypatch, LIMITS)
    assert jl.LOCK_TIMEOUT_S == 10.0  # the real default, before the test shortens it
    monkeypatch.setattr(Journal, "exclusive", _with_default_timeout(0.05))
    led = sealed_ledger()
    j = Journal(tmp_path / "j.jsonl")
    p = propose(led, "plan-1", journal=j, clock=clock_at(T0))
    holder = Journal(j.path)
    with holder.exclusive(timeout_s=1):
        with pytest.raises(JournalError, match="timed out"):
            approve(p, led, journal=j, ask=yes, clock=clock_at(T0))
    assert APPROVED not in statuses(j)
    # once released, the same ticket can be approved
    assert approve(p, led, journal=j, ask=yes, clock=clock_at(T0)).status == APPROVED


def race(monkeypatch, tmp_path, lim: Limits, *, lock: bool):
    """Two approvers, two Journal objects, one file; both say "yes" at the same instant."""
    set_limits(monkeypatch, lim)
    if not lock:
        monkeypatch.setattr(Journal, "exclusive", _no_lock)
    real_day_state = Journal.day_state
    read_barrier = threading.Barrier(2, timeout=5)
    armed = threading.Event()  # set after the initial proposes, so only approve() is affected

    def slow_day_state(self, now):  # widen the read-check-append window
        s = real_day_state(self, now)
        if armed.is_set():
            if lock:
                time.sleep(0.05)
            else:
                read_barrier.wait()  # control: both read before either appends, no timing luck
        return s

    monkeypatch.setattr(Journal, "day_state", slow_day_state)
    path = tmp_path / "j.jsonl"
    led = sealed_ledger()
    barrier = threading.Barrier(2, timeout=5)

    def ask(_):
        barrier.wait()
        return "yes"

    props = [propose(led, "plan-1", journal=Journal(path), clock=clock_at(T0))
             for _ in range(2)]
    assert all(p.ok for p in props)
    armed.set()
    outs: list = [None, None]
    errs: list = []

    def worker(i):
        try:
            outs[i] = approve(props[i], led, journal=Journal(path), ask=ask, clock=clock_at(T0))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errs.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert not errs, errs
    return outs, Journal(path)


MAX_TRADES_1 = replace(LIMITS, max_trades_per_day=1)
LOSS_ONE_FITS = replace(LIMITS, daily_loss_limit=D("60.00"))  # 46.88 fits once, 93.76 does not


@pytest.mark.parametrize("lim", [MAX_TRADES_1, LOSS_ONE_FITS], ids=["max_trades", "daily_loss"])
def test_107_race_exactly_one_approval(monkeypatch, tmp_path, lim):
    outs, j = race(monkeypatch, tmp_path, lim, lock=True)
    assert sorted(o.status for o in outs) == sorted([APPROVED, "refused_after_approval"])
    loser = next(o for o in outs if o.status != APPROVED)
    assert loser.recheck.stage == "risk"
    assert statuses(j).count(APPROVED) == 1
    assert j.day_state(T0).trades_today == 1


@pytest.mark.parametrize("lim", [MAX_TRADES_1, LOSS_ONE_FITS], ids=["max_trades", "daily_loss"])
def test_107_CONTROL_without_the_lock_both_approve(monkeypatch, tmp_path, lim):
    """CONTROL: proves the race test is meaningful. With exclusive() a no-op, the same race
    approves twice and overshoots the limit."""
    outs, j = race(monkeypatch, tmp_path, lim, lock=False)
    assert [o.status for o in outs] == [APPROVED, APPROVED]
    # Not asserted: the line count. Two unlocked appends from two Journal objects can overwrite
    # each other on Windows (seen ~1 run in 10), which is itself a symptom of no locking.
    assert statuses(j).count(APPROVED) >= 1
