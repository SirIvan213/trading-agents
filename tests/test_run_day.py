"""run_day (Phase 5 PR 6, #156, #157, #131, #141): the day's loop, built without a broker.

Offline: a fake clock whose sleep advances it, fake market data that is always fresh, a
stand-in run_decision, tmp_path journal and state. No network, no billed call, no broker.
"""
from __future__ import annotations

import json
import sys
import threading
import types
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import pipeline.run_day as rd
from journal.log import Journal
from pipeline.budget import CapReached, DailyBudget, config_from_dict
from pipeline.decide import SymbolSkipped
from pipeline.filelock import exclusive_file_lock
from pipeline.terminal import WatchedTerminal
from tests.fakes import asset_reply
from tests.order_helpers import sent_order, submissions_202
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tools.config import CONFIG_DIR, load_limits

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
LIMITS = load_limits(CONFIG_DIR / "limits.example.json")
MONDAY = date(2026, 11, 23)
EXAMPLE = json.loads((CONFIG_DIR / "run_day.example.json").read_text(encoding="utf-8"))


class Clock:
    """The fake clock: sleep() advances it, so a day runs in milliseconds."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept = 0.0

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept += s
        self.now += timedelta(seconds=s)

    def advance(self, s: float) -> None:
        self.now += timedelta(seconds=s)


class Data:
    """Market data that is always fresh at the clock's time: every symbol gaps up 8.7% on
    2M shares, is listed on Nasdaq and filed an 8-K item 2.02 30 days ago."""

    def __init__(self, clock: Clock, *, no_reporting: tuple[str, ...] = ()) -> None:
        self.clock = clock
        self.snapshot_times: list[datetime] = []
        self.no_reporting = no_reporting

    def snapshots(self, symbols):
        now = self.clock()
        self.snapshot_times.append(now)
        t = (now - timedelta(seconds=10)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        bar = datetime.combine(now.astimezone(NY).date(), datetime.min.time(), tzinfo=NY)
        bar_t = bar.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {s: {"latestTrade": {"p": 25.0, "t": t},
                    "latestQuote": {"ap": 25.01, "bp": 24.99, "t": t},
                    "dailyBar": {"o": 25.0, "c": 25.0, "v": 2_000_000, "t": bar_t},
                    "prevDailyBar": {"c": 23.0, "t": bar_t}} for s in symbols}

    def listing(self, symbol):
        return asset_reply(symbol)

    def submissions(self, symbol):
        if symbol in self.no_reporting:
            return None
        return submissions_202(self.clock(), 30, symbol=symbol)


class Decisions:
    """A stand-in for run_decision: counts the decision through the real hook, takes
    `minutes` of clock time, optionally spends tokens, skips, crashes or asks."""

    def __init__(self, clock: Clock, *, minutes: float = 1, tokens: int = 0,
                 skip: tuple[str, ...] = (), crash: tuple[str, ...] = (),
                 ask_after_s: float | None = None) -> None:
        self.clock = clock
        self.minutes = minutes
        self.tokens = tokens
        self.skip, self.crash = skip, crash
        self.ask_after_s = ask_after_s
        self.calls: list[tuple[str, datetime]] = []
        self.brokers: list[object] = []
        self.budget: DailyBudget | None = None

    def __call__(self, symbol, *, before_analysts, ask, broker=None, **kw):
        if symbol in self.skip:
            raise SymbolSkipped(symbol, "no 8-K item 2.02 in the last 100 days")
        before_analysts()  # the real budget counts it (CapReached propagates)
        self.calls.append((symbol, self.clock()))
        self.brokers.append(broker)
        if self.tokens and self.budget is not None:
            self.budget.settle_tokens(self.budget.reserve_tokens(self.tokens), self.tokens)
        if symbol in self.crash:
            raise RuntimeError("boom")
        self.clock.advance(self.minutes * 60)
        if self.ask_after_s is not None:
            self.clock.advance(self.ask_after_s)
            ask("ORDER TICKET ...")
        return SimpleNamespace(outcome=SimpleNamespace(status="pass", reason=None),
                               record_error=None, ledger_run_id=f"run-{symbol}")


class Silent:
    """A console where nobody types."""

    def __init__(self) -> None:
        self.ev = threading.Event()
        self.out: list[str] = []

    def readline(self):
        self.ev.wait()
        return ""

    def write(self, text):
        self.out.append(text)


def make_day(tmp_path, monkeypatch, *, start=None, universe=None, caps=None,
             decisions=None, data=None, broker=None, **dec_kw):
    clock = Clock(start or datetime(2026, 11, 23, 9, 30, tzinfo=NY))
    monkeypatch.setattr(rd, "HEARTBEAT_S", 900.0)  # fewer beats: a fake day runs fast
    d = dict(EXAMPLE)
    if universe is not None:
        d["universe"] = universe
    if caps is not None:
        d["caps"] = {**EXAMPLE["caps"], **caps}
    config = config_from_dict(d)
    dec = decisions or Decisions(clock, **dec_kw)
    monkeypatch.setattr(rd, "run_decision", dec)
    out: list[str] = []
    console = Silent()
    day = rd.Day(config=config, data=data or Data(clock), clients=object(),
                 journal=Journal(tmp_path / "j.jsonl"), clock=clock, sleep=clock.sleep,
                 terminal=WatchedTerminal(readline=console.readline, write=console.write),
                 state_dir=tmp_path / "state", broker=broker, write=out.append)
    original_open = DailyBudget.open_day

    def capture(self, **k):
        dec.budget = self
        return original_open(self, **k)

    monkeypatch.setattr(DailyBudget, "open_day", capture)
    return day, clock, dec, out


def statuses(day):
    return [e["status"] for e in day.journal.entries()]


def entries(day, status):
    return [e for e in day.journal.entries() if e["status"] == status]


SYMS = [f"S{c}" for c in "ABCDEFGHIJKL"]  # 12 symbols


# ======================= the day =======================

def test_a_full_day_scans_at_0940_then_decides_until_the_decision_cap(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=SYMS)
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    first_scan = day.data.snapshot_times[0]
    assert first_scan == datetime(2026, 11, 23, 9, 40, tzinfo=NY)  # #131: the 09:40 scan
    assert [s for s, _ in dec.calls] == SYMS[:10]  # 10 decisions, each symbol once
    assert all(t >= datetime(2026, 11, 23, 9, 45, tzinfo=NY) for _, t in dec.calls)
    per_cycle: dict = {}
    for _, t in dec.calls:
        slot = t.replace(minute=t.minute - t.minute % 5, second=0)
        per_cycle[slot] = per_cycle.get(slot, 0) + 1
    assert max(per_cycle.values()) <= 2  # at most 2 candidates per cycle
    end = entries(day, "run_day_end")[-1]
    assert end["reason"] == "decision cap reached (10 today)" and end["decisions"] == 10
    assert statuses(day)[0] == "run_day_start"
    beat = json.loads(day.heartbeat_path.read_text(encoding="utf-8"))
    assert beat["phase"] == "ended" and beat["decisions"] == 10


def test_blocked_no_decision_starts_after_entry_end(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=SYMS,
                                    start=datetime(2026, 11, 23, 15, 26, tzinfo=NY))
    rd.run_day(day, limits=LIMITS)
    assert all(t < datetime(2026, 11, 23, 15, 30, tzinfo=NY) for _, t in dec.calls)
    assert len(dec.calls) <= 2
    assert entries(day, "run_day_end")[-1]["reason"] == "entry window closed"


def test_an_early_close_ends_entries_at_1230(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=SYMS, minutes=40,
                                    start=datetime(2026, 11, 27, 9, 30, tzinfo=NY))
    rd.run_day(day, limits=LIMITS)
    assert dec.calls and all(t < datetime(2026, 11, 27, 12, 30, tzinfo=NY)
                             for _, t in dec.calls)


@pytest.mark.parametrize("day_,why", [(date(2026, 11, 21), "weekend"),
                                      (date(2026, 11, 26), "Thanksgiving Day")])
def test_no_session_no_decisions(tmp_path, monkeypatch, day_, why):
    day, clock, dec, out = make_day(tmp_path, monkeypatch,
                                    start=datetime.combine(day_, datetime.min.time(),
                                                           tzinfo=NY) + timedelta(hours=9))
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert dec.calls == [] and day.data.snapshot_times == []
    assert entries(day, "run_day_end")[-1]["reason"] == f"no session ({why})"


def test_a_skipped_symbol_does_not_count_as_a_decision(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=SYMS, skip=("SA", "SB"))
    rd.run_day(day, limits=LIMITS)
    assert [s for s, _ in dec.calls] == SYMS[2:12]  # SA, SB skipped; 10 others decided
    assert entries(day, "run_day_end")[-1]["decisions"] == 10


def test_a_symbol_without_reporting_is_rejected_by_the_scan(tmp_path, monkeypatch):
    clock = Clock(datetime(2026, 11, 23, 9, 30, tzinfo=NY))
    data = Data(clock, no_reporting=("SA",))
    day, clock2, dec, out = make_day(tmp_path, monkeypatch, universe=["SA", "SB"], data=data)
    day.clock = data.clock = clock2  # one clock
    rd.run_day(day, limits=LIMITS)
    assert [s for s, _ in dec.calls] == ["SB"]


def test_a_restart_does_not_recheck_symbols_skipped_earlier_today(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA", "SB"])
    day.journal.append({"status": "skipped", "symbol": "SA", "reason": "no 8-K",
                        "checked_at": "2026-11-23T09:46:00-05:00"})
    rd.run_day(day, limits=LIMITS)
    assert [s for s, _ in dec.calls] == ["SB"]


def test_a_crashed_decision_alerts_and_the_day_goes_on(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA", "SB", "SC"],
                                    crash=("SA",))
    rd.run_day(day, limits=LIMITS)
    assert [s for s, _ in dec.calls] == ["SA", "SB", "SC"]
    assert any("decision for SA failed" in e["message"] for e in entries(day, "run_day_alert"))
    assert any(line.startswith("\aALERT:") for line in out)


def test_no_answer_before_the_plan_expires_is_declined_and_journaled(tmp_path, monkeypatch):
    """The ticket is asked after the plan's age budget has run out: declined at once."""
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"],
                                    ask_after_s=LIMITS.max_plan_age_s)
    rd.run_day(day, limits=LIMITS)
    (note,) = entries(day, "run_day_approval_timeout")
    assert note["symbol"] == "SA" and note["run_id"] == "run-SA"


# ======================= the caps =======================

def test_blocked_the_day_stops_when_the_tokens_run_low(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=SYMS, tokens=200_000)
    rd.run_day(day, limits=LIMITS)
    assert len(dec.calls) == 3  # 600,000 used: fewer than one call's worth left
    assert entries(day, "run_day_end")[-1]["reason"].startswith("token cap")


def test_a_restart_continues_the_days_counts(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=SYMS)
    day.journal.append({"status": "run_day_start", "day": "2026-11-23"})
    b = DailyBudget(day.config.caps, MONDAY, day.state_dir)
    b.open_day(started_today=False)
    for s in SYMS[:9]:
        b.start_decision(s)  # 9 decisions before the restart
    rd.run_day(day, limits=LIMITS)
    assert [s for s, _ in dec.calls] == ["SJ"]  # the 10th, never a repeat
    assert entries(day, "run_day_end")[-1]["decisions"] == 10


def test_blocked_a_lost_budget_file_after_a_start_today_means_no_decisions(tmp_path,
                                                                          monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=SYMS)
    day.journal.append({"status": "run_day_start", "day": "2026-11-23"})  # file never written
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert dec.calls == []
    assert any("daily budget closed" in e["message"] for e in entries(day, "run_day_alert"))


def test_blocked_a_cap_reached_inside_the_hook_ends_the_day(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=SYMS)
    real = DailyBudget.start_decision
    n = {"k": 0}

    def flaky(self, symbol):
        n["k"] += 1
        if n["k"] == 2:
            raise CapReached("daily budget file is unreadable")
        return real(self, symbol)

    monkeypatch.setattr(DailyBudget, "start_decision", flaky)
    rd.run_day(day, limits=LIMITS)
    assert len(dec.calls) == 1
    assert entries(day, "run_day_end")[-1]["reason"] == "daily budget file is unreadable"


# ======================= starting up =======================

def test_blocked_a_broker_is_refused_until_the_reconciler_and_flatten_are_merged(tmp_path,
                                                                               monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, broker=object())
    assert rd.run_day(day, limits=LIMITS) == rd.REFUSED
    assert "no broker until the reconciler and flatten job are merged" in out[-1]
    assert dec.calls == [] and not (tmp_path / "state").exists()
    assert not (tmp_path / "j.jsonl").exists()


@pytest.mark.parametrize("ready", [(False, False), (True, False), (False, True)])
def test_blocked_a_broker_needs_both_modules_ready(monkeypatch, ready):
    for name, ok in zip(rd.SENDING_PREREQUISITES, ready, strict=True):
        mod = types.ModuleType(name)
        if ok:
            mod.RUN_DAY_READY = True
        monkeypatch.setitem(sys.modules, name, mod)
    allowed, why = rd.broker_allowed()
    assert allowed is False and "RUN_DAY_READY" in why


def test_a_broker_is_accepted_once_both_modules_declare_ready(tmp_path, monkeypatch):
    for name in rd.SENDING_PREREQUISITES:
        mod = types.ModuleType(name)
        mod.RUN_DAY_READY = True
        monkeypatch.setitem(sys.modules, name, mod)
    broker = QuietBroker()
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"], broker=broker)
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    # the decision sends through the guard, which wraps this broker (#155)
    assert len(dec.brokers) == 1 and isinstance(dec.brokers[0], rd.GuardedBroker)
    assert dec.brokers[0]._day.broker is broker


class QuietBroker:
    """A paper broker with nothing in it: the reconciler's reads find no order, no position."""

    def verify_paper_account(self):
        return {"account_number": "PA-FAKE", "status": "ACTIVE"}

    def list_open_orders_checked(self):
        return {"orders": [], "unreadable": []}

    def list_positions(self):
        return []

    def get_order_by_client_id(self, cid):
        return None


def test_today_the_flatten_job_is_not_merged():
    """PR 4 (#155) merged the reconciler; run_day still refuses a broker until PR 5's flatten
    job (#135) declares RUN_DAY_READY."""
    import pipeline.reconcile as rc
    assert rc.RUN_DAY_READY is True
    allowed, why = rd.broker_allowed()
    assert allowed is False and "pipeline.flatten does not declare RUN_DAY_READY" in why


def test_blocked_paper_false_is_refused(tmp_path, monkeypatch):
    from dataclasses import replace
    day, clock, dec, out = make_day(tmp_path, monkeypatch)
    monkeypatch.setattr(rd, "load_checked_limits", lambda limits=None: replace(LIMITS,
                                                                                paper=False))
    assert rd.run_day(day) == rd.REFUSED
    assert "paper must be true" in out[-1]


def test_blocked_a_second_instance_exits(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch)
    held, release = threading.Event(), threading.Event()

    def hold():
        with exclusive_file_lock(tmp_path / "state" / "run_day.lock"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        assert rd.run_day(day, limits=LIMITS) == rd.ALREADY_RUNNING
    finally:
        release.set()
        t.join()
    assert dec.calls == [] and "already running" in out[-1]


def test_the_coverage_warning_is_shown_on_the_real_path(tmp_path, monkeypatch):
    """#141: run_day shows tools.market_calendar.coverage_warning() at every start."""
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])
    monkeypatch.setattr(rd, "coverage_warning",
                        lambda d: "market calendar ends on December 31, 2026")
    rd.run_day(day, limits=LIMITS)
    assert any("calendar: market calendar ends" in line for line in out)
    assert any("calendar: market calendar ends" in e["message"]
               for e in entries(day, "run_day_alert"))


def test_no_coverage_warning_on_an_ordinary_day(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])
    rd.run_day(day, limits=LIMITS)
    assert not any("calendar:" in line for line in out)


def test_blocked_a_position_from_an_earlier_day_holds_the_day(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=SYMS)
    friday = datetime(2026, 11, 20, 10, 0, tzinfo=NY)
    for e in sent_order(friday):
        day.journal.append(e)
    assert rd.run_day(day, limits=LIMITS) == rd.HELD
    assert dec.calls == [] and day.data.snapshot_times == []
    assert any("earlier day" in e["message"] for e in entries(day, "run_day_alert"))


def test_an_unsent_approval_from_an_earlier_day_does_not_hold_the_day(tmp_path, monkeypatch):
    from tests.order_helpers import approved_entry
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])
    day.journal.append(approved_entry(datetime(2026, 11, 20, 10, 0, tzinfo=NY)))
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert [s for s, _ in dec.calls] == ["SA"]


def test_the_heartbeat_is_rewritten_while_waiting(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])
    monkeypatch.setattr(rd, "HEARTBEAT_S", 30.0)  # the real interval
    beats = []
    real = rd.Day.beat

    def spy(self, phase=None):
        real(self, phase)
        beats.append(self.clock())

    monkeypatch.setattr(rd.Day, "beat", spy)
    rd.run_day(day, limits=LIMITS)
    gaps = [(b - a).total_seconds() for a, b in zip(beats, beats[1:], strict=False)]
    assert gaps and max(gaps) <= rd.HEARTBEAT_S + 60  # waits beat every 30 s; a decision 60 s


# ======================= the CLI =======================

def test_main_without_scheduled_is_a_dry_run_that_calls_nothing(tmp_path, monkeypatch,
                                                                 capsys):
    import pipeline.budget as pb
    (tmp_path / "run_day.json").write_text(json.dumps(EXAMPLE), encoding="utf-8")
    monkeypatch.setattr(pb, "CONFIG_PATH", tmp_path / "run_day.json")
    built = []
    assert rd.main([], build=lambda *a, **k: built.append(1)) == rd.NOT_SCHEDULED
    assert built == [] and "Nothing was called" in capsys.readouterr().out


def test_blocked_main_refuses_a_missing_config(tmp_path, monkeypatch, capsys):
    import pipeline.budget as pb
    monkeypatch.setattr(pb, "CONFIG_PATH", tmp_path / "run_day.json")
    assert rd.main(["--scheduled"], build=lambda *a, **k: pytest.fail("built")) == rd.REFUSED
    assert "not found" in capsys.readouterr().err


def test_blocked_main_refuses_send(tmp_path, monkeypatch, capsys):
    import pipeline.budget as pb
    (tmp_path / "run_day.json").write_text(json.dumps(EXAMPLE), encoding="utf-8")
    monkeypatch.setattr(pb, "CONFIG_PATH", tmp_path / "run_day.json")
    assert rd.main(["--scheduled", "--send"],
                   build=lambda *a, **k: pytest.fail("built")) == rd.REFUSED
    assert "--send needs the reconciler" in capsys.readouterr().err


def test_main_scheduled_builds_and_runs_the_day(tmp_path, monkeypatch):
    import pipeline.budget as pb
    (tmp_path / "run_day.json").write_text(json.dumps({**EXAMPLE, "universe": ["SA"]}),
                                           encoding="utf-8")
    monkeypatch.setattr(pb, "CONFIG_PATH", tmp_path / "run_day.json")

    def build(config, *, clock, send):
        assert send is False
        day, *_ = make_day(tmp_path, monkeypatch, universe=list(config.universe))
        return day

    monkeypatch.setattr(rd, "load_checked_limits", lambda limits=None: LIMITS)
    assert rd.main(["--scheduled"], build=build) == rd.OK


# ======================= the decision hook in run_decision =======================

def test_the_hook_runs_after_the_sec_check_and_before_any_analyst(monkeypatch, tmp_path):
    from tests.test_phase5_gate_d import run_scenario
    seen = []
    result, *_ = run_scenario(monkeypatch, tmp_path, "clean-buy",
                              before_analysts=lambda: seen.append("counted"))
    assert seen == ["counted"] and result.proposal is not None


def test_blocked_a_skipped_symbol_never_reaches_the_hook(monkeypatch, tmp_path):
    from tests.test_phase5_gate_d import run_scenario
    seen = []
    with pytest.raises(SymbolSkipped):
        run_scenario(monkeypatch, tmp_path, "red-flag",
                     before_analysts=lambda: seen.append("counted"))
    assert seen == []


def test_blocked_a_cap_in_the_hook_stops_before_any_analyst(monkeypatch, tmp_path):
    import pipeline.decide as decide
    from tests.test_phase5_gate_d import run_scenario

    def no_analysts(*a, **k):
        raise AssertionError("an analyst ran")

    def cap():
        raise CapReached("decision cap reached (10 today)")

    monkeypatch.setattr(decide, "run_analysts", no_analysts)
    with pytest.raises(CapReached):
        run_scenario(monkeypatch, tmp_path, "clean-buy", stub_analysts=False,
                     before_analysts=cap)


# ======================= review fixes =======================

class AskOnly(Decisions):
    """Asks the ticket only for the symbols in `ask_for`, after `ask_after_s`."""

    def __init__(self, clock, ask_for, **kw):
        super().__init__(clock, **kw)
        self.ask_for = ask_for

    def __call__(self, symbol, *, before_analysts, ask, broker=None, **kw):
        before_analysts()
        self.calls.append((symbol, self.clock()))
        self.clock.advance(60)
        if symbol in self.ask_for:
            self.clock.advance(LIMITS.max_plan_age_s)
            ask("ORDER TICKET ...")
        return SimpleNamespace(outcome=SimpleNamespace(status="pass", reason=None),
                               record_error=None, ledger_run_id=f"run-{symbol}")


def test_s4_a_timeout_is_never_carried_into_the_next_decision(tmp_path, monkeypatch):
    clock = Clock(datetime(2026, 11, 23, 9, 30, tzinfo=NY))
    dec = AskOnly(clock, ask_for=("SA",))
    day, clock2, _, out = make_day(tmp_path, monkeypatch, universe=["SA", "SB"],
                                   decisions=dec)
    day.clock = day.data.clock = dec.clock = clock2
    rd.run_day(day, limits=LIMITS)
    assert [e["symbol"] for e in entries(day, "run_day_approval_timeout")] == ["SA"]


def test_the_ticket_deadline_counts_from_when_the_decision_counts(tmp_path, monkeypatch):
    """A slow SEC check (100 s) before the decision counts does not eat the human's time."""
    clock = Clock(datetime(2026, 11, 23, 9, 30, tzinfo=NY))

    class SlowSec(Decisions):
        def __call__(self, symbol, *, before_analysts, ask, broker=None, **kw):
            self.clock.advance(100)  # the free SEC check
            before_analysts()
            counted = self.clock()
            self.calls.append((symbol, counted))
            ask("ORDER TICKET ...")
            self.deadline_seen = counted + timedelta(seconds=LIMITS.max_plan_age_s)
            return SimpleNamespace(outcome=None, record_error=None, ledger_run_id="r")

    dec = SlowSec(clock)
    day, clock2, _, out = make_day(tmp_path, monkeypatch, universe=["SA"], decisions=dec)
    day.clock = day.data.clock = dec.clock = clock2
    console_out = []
    day.terminal = WatchedTerminal(readline=lambda: threading.Event().wait() or "",
                                   write=console_out.append)
    monkeypatch.setattr(day.terminal, "ask",
                        lambda prompt, timeout_s, deadline_label="": console_out.append(
                            (timeout_s, deadline_label)) or "")
    rd.run_day(day, limits=LIMITS)
    (left, label) = next(x for x in console_out if isinstance(x, tuple))
    assert left == LIMITS.max_plan_age_s  # the full plan age, from the count
    assert label == dec.deadline_seen.strftime("%H:%M:%S ET")


def test_s5_a_budget_for_another_day_refuses_to_start(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])
    day.budget = DailyBudget(day.config.caps, date(2026, 11, 22), day.state_dir)
    assert rd.run_day(day, limits=LIMITS) == rd.REFUSED
    assert dec.calls == [] and "day changed during start-up" in out[-1]


def test_s6_the_start_marker_is_required(tmp_path, monkeypatch):
    from journal.log import JournalError
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])
    real = Journal.append

    def refuse_start(self, entry):
        if entry.get("status") == "run_day_start":
            raise JournalError("disk full")
        return real(self, entry)

    monkeypatch.setattr(Journal, "append", refuse_start)
    assert rd.run_day(day, limits=LIMITS) == rd.REFUSED
    assert dec.calls == [] and "run_day_start entry could not be journaled" in out[-1]


def test_s8_a_failed_sec_lookup_is_asked_again_at_the_next_scan(tmp_path, monkeypatch):
    clock = Clock(datetime(2026, 11, 23, 9, 30, tzinfo=NY))

    class Flaky(Data):
        def __init__(self, clock):
            super().__init__(clock)
            self.sec_calls = 0

        def submissions(self, symbol):
            self.sec_calls += 1
            if self.sec_calls == 1:
                raise TimeoutError  # SEC down at 09:40
            return super().submissions(symbol)

    data = Flaky(clock)
    day, clock2, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"], data=data)
    day.clock = data.clock = dec.clock = clock2
    rd.run_day(day, limits=LIMITS)
    assert [s for s, _ in dec.calls] == ["SA"]  # decided once SEC answered
    assert data.sec_calls == 2  # then cached: not asked again all day


def test_reporting_check_marks_a_failed_sec_request():
    from pipeline.events import reporting_check

    class Down:
        def submissions(self, symbol):
            raise TimeoutError

    class Foreign:
        def submissions(self, symbol):
            return None

    now = datetime(2026, 11, 23, 10, 0, tzinfo=NY)
    assert reporting_check(Down(), "SA", now)["fetch_failed"] is True
    assert "fetch_failed" not in reporting_check(Foreign(), "SA", now)  # a real answer


def test_s9_a_budget_doubt_inside_a_cycle_still_ends_the_day(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA", "SB"])

    def doubt(self, symbol):
        raise CapReached("daily budget lock timed out")

    monkeypatch.setattr(DailyBudget, "decided", doubt)
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert entries(day, "run_day_end")[-1]["reason"] == "daily budget lock timed out"


def test_s9_an_unexpected_error_alerts_and_still_ends_the_day(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])

    def boom(*a, **k):
        raise KeyError("bug")

    monkeypatch.setattr(rd, "scan_universe", boom)
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    assert entries(day, "run_day_end")[-1]["reason"] == "error: KeyError"
    assert any("unexpected error" in e["message"] for e in entries(day, "run_day_alert"))


def test_s10_the_heartbeat_keeps_beating_during_a_decision(tmp_path, monkeypatch):
    import time as _time
    clock = Clock(datetime(2026, 11, 23, 9, 30, tzinfo=NY))

    class Slow(Decisions):
        def __call__(self, symbol, *, before_analysts, ask, broker=None, **kw):
            before_analysts()
            self.calls.append((symbol, self.clock()))
            _time.sleep(0.4)  # a long decision, in real time
            return SimpleNamespace(outcome=None, record_error=None, ledger_run_id="r")

    dec = Slow(clock)
    day, clock2, _, out = make_day(tmp_path, monkeypatch, universe=["SA"], decisions=dec)
    day.clock = day.data.clock = dec.clock = clock2
    day.pulse_s = 0.05
    beats_during = []
    real = rd.Day.beat

    def spy(self, phase=None):
        real(self, phase)
        if dec.calls and self.phase.startswith("deciding"):
            beats_during.append(1)

    monkeypatch.setattr(rd.Day, "beat", spy)
    rd.run_day(day, limits=LIMITS)
    assert len(beats_during) >= 3  # the pulse beat while the decision ran


def test_build_live_wires_one_metered_budget_and_no_broker(tmp_path, monkeypatch):
    """_build_live, offline: the client is metered with the SDK's retries off, the FMP and
    Alpha Vantage caches count every request, there is no broker, and the client, the data
    and the loop share ONE budget for today."""
    import agents.llm as llm
    import pipeline.smoke_common as sc
    from pipeline.metering import MeteredClient

    made = []

    class FakeAnthropic:
        beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: None))

    def fake_make_client(**kw):
        made.append(kw)
        return FakeAnthropic()

    monkeypatch.setattr(llm, "make_client", fake_make_client)
    monkeypatch.setattr(sc, "load_dotenv", lambda path: None)  # never the real .env
    monkeypatch.setattr(rd, "STATE_DIR", tmp_path / "state")
    config = config_from_dict({**EXAMPLE, "universe": ["SA"]})
    now = datetime(2026, 11, 23, 9, 30, tzinfo=NY)
    day = rd._build_live(config, clock=lambda: now, send=False)
    assert made == [{"max_retries": 0}]
    assert isinstance(day.clients, MeteredClient) and day.broker is None
    assert day.budget is not None and day.budget.day == MONDAY
    assert day.clients._budget is day.budget
    assert day.data._calendar.fetch.__name__ == "counted"  # FMP metered
    assert day.data._av_calendar.fetch.__name__ == "counted"  # Alpha Vantage metered
    assert day.state_dir == tmp_path / "state"



def test_a_budget_lock_that_cannot_be_opened_is_a_cap_reached(tmp_path, monkeypatch):
    import pipeline.budget as pb
    from pipeline.budget import Caps
    b = DailyBudget(Caps(10, 600_000, 3, 5), MONDAY, tmp_path)
    b.open_day(started_today=False)

    def denied(*a, **k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(pb, "exclusive_file_lock", denied)
    with pytest.raises(CapReached, match="could not be opened"):
        b.snapshot()


def test_run_day_end_is_written_even_when_the_budget_breaks_mid_day(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])

    def broken(self):
        raise PermissionError(13, "denied")

    real_scan = rd.scan_universe

    def scan_then_break(d, limits):
        out_ = real_scan(d, limits)
        monkeypatch.setattr(DailyBudget, "snapshot", broken)
        return out_

    monkeypatch.setattr(rd, "scan_universe", scan_then_break)
    assert rd.run_day(day, limits=LIMITS) == rd.OK
    end = entries(day, "run_day_end")[-1]
    assert end["budget"] == "unreadable (PermissionError)"


def test_a_scan_stops_sec_lookups_after_the_first_failure(tmp_path, monkeypatch):
    clock = Clock(datetime(2026, 11, 23, 9, 30, tzinfo=NY))

    class SecDown(Data):
        def __init__(self, clock):
            super().__init__(clock)
            self.sec_calls = 0

        def submissions(self, symbol):
            self.sec_calls += 1
            raise TimeoutError

    data = SecDown(clock)
    day, clock2, dec, out = make_day(tmp_path, monkeypatch, universe=["SA", "SB", "SC"],
                                     data=data,
                                     start=datetime(2026, 11, 23, 15, 25, tzinfo=NY))
    day.clock = data.clock = dec.clock = clock2
    rd.run_day(day, limits=LIMITS)
    scans = len(entries(day, "run_day_scan"))
    assert dec.calls == [] and data.sec_calls == scans  # one SEC request per scan, not three


def test_blocked_a_budget_in_another_state_folder_refuses(tmp_path, monkeypatch):
    day, clock, dec, out = make_day(tmp_path, monkeypatch, universe=["SA"])
    day.budget = DailyBudget(day.config.caps, MONDAY, tmp_path / "elsewhere")
    assert rd.run_day(day, limits=LIMITS) == rd.REFUSED
    assert "different state folders" in out[-1]
