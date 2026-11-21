"""run_day's config and daily budget (#157, #156): the four CLAUDE.md rule 6 caps, enforced in
code, shared across processes on disk, failing closed. Offline: tmp_path only."""
from __future__ import annotations

import copy
import json
import threading
from datetime import date

import pytest

from pipeline.budget import (
    CEILINGS,
    CapReached,
    Caps,
    DailyBudget,
    RunDayConfigError,
    config_from_dict,
    load_run_day_config,
)
from tools.config import CONFIG_DIR

DAY = date(2026, 11, 23)
EXAMPLE = json.loads((CONFIG_DIR / "run_day.example.json").read_text(encoding="utf-8"))
CAPS = Caps(max_decisions=10, max_tokens=600_000, max_fmp_requests=3, max_av_calls=5)


def cfg(**over):
    d = copy.deepcopy(EXAMPLE)
    for k, v in over.items():
        if k.startswith("caps."):
            name = k.split(".", 1)[1]
            if v is ...:
                d["caps"].pop(name)
            else:
                d["caps"][name] = v
        elif v is ...:
            d.pop(k)
        else:
            d[k] = v
    return d


def budget(tmp_path, caps=CAPS, *, started_today=False):
    b = DailyBudget(caps, DAY, tmp_path)
    b.open_day(started_today=started_today)
    return b


# ======================= config =======================

def test_the_example_config_matches_claude_md_rule_6():
    c = config_from_dict(EXAMPLE)
    assert c.caps == CAPS
    assert CEILINGS == {"max_decisions": 10, "max_tokens": 600_000, "max_fmp_requests": 3,
                        "max_av_calls": 5}
    assert len(c.universe) == 20 and c.candidates_per_cycle == 2 and c.cycle_minutes == 5
    assert c.scan_time.isoformat() == "09:40:00"  # #131: the scan runs at 09:40 ET


def test_a_lower_cap_is_allowed():
    c = config_from_dict(cfg(**{"caps.max_decisions": 3, "caps.max_tokens": 100_000}))
    assert c.caps.max_decisions == 3 and c.caps.max_tokens == 100_000


@pytest.mark.parametrize("name", list(CEILINGS))
def test_blocked_a_missing_cap_refuses_to_start(name):
    with pytest.raises(RunDayConfigError, match=f"caps.{name} is missing"):
        config_from_dict(cfg(**{f"caps.{name}": ...}))


@pytest.mark.parametrize("name", list(CEILINGS))
@pytest.mark.parametrize("bad", [0, -1, "10", 1.5, True, None])
def test_blocked_an_invalid_cap_refuses_to_start(name, bad):
    with pytest.raises(RunDayConfigError, match=f"caps.{name} must be a whole number"):
        config_from_dict(cfg(**{f"caps.{name}": bad}))


@pytest.mark.parametrize("name", list(CEILINGS))
def test_blocked_a_cap_above_claude_md_refuses_to_start(name):
    """Raising a cap needs Ivan's explicit approval: the ceilings are fixed in code."""
    with pytest.raises(RunDayConfigError, match=f"caps.{name}"):
        config_from_dict(cfg(**{f"caps.{name}": CEILINGS[name] + 1}))


@pytest.mark.parametrize("key", ["cycle_minute", "candidate_per_cycle", "unvierse"])
def test_blocked_a_misspelled_top_level_key_refuses(key):
    with pytest.raises(RunDayConfigError, match="unknown key"):
        config_from_dict({**EXAMPLE, key: 5})


@pytest.mark.parametrize("scan_time", ["09:00", "09:30", "16:00", "17:15", "9", "noon"])
def test_blocked_a_scan_time_outside_the_session_refuses(scan_time):
    with pytest.raises(RunDayConfigError, match="scan_time"):
        config_from_dict(cfg(scan_time=scan_time))


def test_blocked_more_than_2_candidates_per_cycle_refuses():
    """Ivan's decision on #156: at most 2 candidates per cycle."""
    with pytest.raises(RunDayConfigError, match="candidates_per_cycle"):
        config_from_dict(cfg(candidates_per_cycle=3))
    assert config_from_dict(cfg(candidates_per_cycle=1)).candidates_per_cycle == 1


@pytest.mark.parametrize("name", list(CEILINGS))
def test_blocked_a_hand_built_cap_above_claude_md_is_refused_by_the_budget(tmp_path, name):
    caps = Caps(**{**vars(CAPS), name: CEILINGS[name] + 1})
    with pytest.raises(RunDayConfigError, match=f"caps.{name}"):
        DailyBudget(caps, DAY, tmp_path)


def test_a_snapshot_never_writes_the_budget_file(tmp_path):
    b = budget(tmp_path)
    before = b.path.stat().st_mtime_ns
    b.path.chmod(0o444)  # read-only: a write would fail
    try:
        for _ in range(3):
            assert b.snapshot()["decisions"] == 0
        assert b.decided("AAPL") is False
    finally:
        b.path.chmod(0o644)
    assert b.path.stat().st_mtime_ns == before


def test_already_decided_is_its_own_kind_of_refusal(tmp_path):
    from pipeline.budget import AlreadyDecided
    b = budget(tmp_path)
    b.start_decision("AAPL")
    with pytest.raises(AlreadyDecided):
        b.start_decision("AAPL")


def test_blocked_a_missing_caps_object_or_an_unknown_cap_refuses():
    with pytest.raises(RunDayConfigError, match="caps is missing"):
        config_from_dict(cfg(caps=...))
    with pytest.raises(RunDayConfigError, match="unknown cap"):
        config_from_dict(cfg(**{"caps.max_orders": 3}))


@pytest.mark.parametrize("universe", [[], None, "AAPL", ["AAPL", "not a symbol!"],
                                      [f"A{i:03d}" for i in range(201)]],
                         ids=["empty", "none", "text", "invalid", "too-many"])
def test_blocked_a_bad_universe_refuses_to_start(universe):
    with pytest.raises(RunDayConfigError, match="universe"):
        config_from_dict(cfg(universe=universe))


def test_the_universe_is_normalized_and_deduplicated():
    c = config_from_dict(cfg(universe=["aapl", "AAPL", "msft"]))
    assert c.universe == ("AAPL", "MSFT")


def test_blocked_a_missing_config_file_refuses_and_never_falls_back(tmp_path):
    with pytest.raises(RunDayConfigError, match="not found"):
        load_run_day_config(tmp_path / "run_day.json")
    (tmp_path / "run_day.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RunDayConfigError, match="not JSON"):
        load_run_day_config(tmp_path / "run_day.json")


def test_a_config_error_never_echoes_the_value():
    with pytest.raises(RunDayConfigError) as exc:
        config_from_dict(cfg(**{"caps.max_tokens": "sk-ant-SENTINEL"}))
    assert "SENTINEL" not in str(exc.value)


# ======================= the daily budget =======================

def test_counts_are_shared_across_processes_through_the_file(tmp_path):
    a = budget(tmp_path)
    a.start_decision("AAPL")
    a.count_request("fmp")
    rid = a.reserve_tokens(1000)
    a.settle_tokens(rid, 800)
    b = DailyBudget(CAPS, DAY, tmp_path)  # a restarted run_day
    b.open_day(started_today=True)
    s = b.snapshot()
    assert (s["decisions"], s["fmp_requests"], s["tokens_used"]) == (1, 1, 800)
    assert s["reservations"] == {} and s["symbols"] == ["AAPL"]


def test_blocked_the_eleventh_decision_is_refused(tmp_path):
    b = budget(tmp_path)
    for i in range(10):
        assert b.start_decision(f"S{chr(65 + i)}") == i + 1
    with pytest.raises(CapReached, match="decision cap reached"):
        b.start_decision("ZZZ")
    assert b.snapshot()["decisions"] == 10


def test_blocked_a_symbol_is_decided_once_a_day(tmp_path):
    b = budget(tmp_path)
    b.start_decision("aapl")
    with pytest.raises(CapReached, match="AAPL was already decided today"):
        b.start_decision("AAPL")
    assert b.decided("AAPL") and not b.decided("MSFT")


def test_blocked_no_decision_starts_once_the_tokens_are_used_up(tmp_path):
    b = budget(tmp_path, Caps(10, 1000, 3, 5))
    b.settle_tokens(b.reserve_tokens(1000), 1000)
    with pytest.raises(CapReached, match="token cap reached"):
        b.start_decision("AAPL")


def test_blocked_a_reservation_that_would_pass_the_cap_is_refused(tmp_path):
    b = budget(tmp_path, Caps(10, 1000, 3, 5))
    r1 = b.reserve_tokens(600)
    with pytest.raises(CapReached, match="token cap"):
        b.reserve_tokens(401)  # 600 reserved + 401 > 1000
    r2 = b.reserve_tokens(400)  # exactly the cap
    b.settle_tokens(r1, 100)  # the call used less: frees the rest
    assert b.reserve_tokens(500)  # 100 used + 400 reserved + 500 = 1000
    b.settle_tokens(r2, 0)


def test_an_unsettled_reservation_stays_counted(tmp_path):
    """A call cut off by a timeout or a crash never settles: its worst case keeps counting."""
    b = budget(tmp_path, Caps(10, 1000, 3, 5))
    b.reserve_tokens(900)
    c = DailyBudget(Caps(10, 1000, 3, 5), DAY, tmp_path)  # after a restart
    c.open_day(started_today=True)
    with pytest.raises(CapReached):
        c.reserve_tokens(101)


def test_an_actual_above_the_reservation_is_recorded_in_full(tmp_path):
    b = budget(tmp_path)
    b.settle_tokens(b.reserve_tokens(100), 250)
    assert b.snapshot()["tokens_used"] == 250


@pytest.mark.parametrize("source,cap", [("fmp", 3), ("av", 5)])
def test_blocked_requests_past_their_cap_are_refused(tmp_path, source, cap):
    b = budget(tmp_path)
    for i in range(cap):
        assert b.count_request(source) == i + 1
    with pytest.raises(CapReached, match=f"{source} request cap reached"):
        b.count_request(source)


def test_blocked_concurrent_reservations_never_pass_the_cap(tmp_path):
    b = budget(tmp_path, Caps(10, 1000, 3, 5))
    others = [DailyBudget(Caps(10, 1000, 3, 5), DAY, tmp_path) for _ in range(4)]
    for o in others:
        o.open_day(started_today=True)
    won = []

    def grab(x):
        for _ in range(10):
            try:
                won.append(x.reserve_tokens(60))
            except CapReached:
                pass

    threads = [threading.Thread(target=grab, args=(x,)) for x in [b, *others]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 16  # 16 x 60 = 960 <= 1000; a 17th would pass it
    assert b.tokens_committed(b.snapshot()) == 960


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({"day": "2026-11-23"}),
                                     json.dumps({"day": "2026-11-22", "decisions": 0})],
                         ids=["not-json", "list", "fields-missing", "another-day"])
def test_blocked_an_untrustworthy_budget_file_counts_as_every_cap_reached(tmp_path, content):
    b = budget(tmp_path)
    b.path.write_text(content, encoding="utf-8")
    for act in (lambda: b.start_decision("AAPL"), lambda: b.reserve_tokens(1),
                lambda: b.count_request("fmp"), lambda: b.count_request("av"), b.snapshot):
        with pytest.raises(CapReached):
            act()


def test_blocked_a_negative_count_is_untrustworthy(tmp_path):
    b = budget(tmp_path)
    s = b.snapshot()
    s["decisions"] = -5  # someone "reset" it
    b.path.write_text(json.dumps(s), encoding="utf-8")
    with pytest.raises(CapReached, match="invalid"):
        b.start_decision("AAPL")


def test_blocked_a_budget_file_deleted_mid_day_is_every_cap_reached(tmp_path):
    b = budget(tmp_path)
    b.start_decision("AAPL")
    b.path.unlink()
    with pytest.raises(CapReached, match="missing after the day started"):
        b.start_decision("MSFT")
    assert not b.path.exists()  # never silently recreated at zero


def test_blocked_a_missing_file_after_a_start_today_is_closed_for_the_day(tmp_path):
    """The journal shows run_day already started today, but the budget file is gone: the
    counts are lost, so the day is closed, never restarted at zero."""
    b = DailyBudget(CAPS, DAY, tmp_path)
    with pytest.raises(CapReached, match="missing after the day started"):
        b.open_day(started_today=True)
    c = DailyBudget(CAPS, DAY, tmp_path)  # and a later restart stays closed
    with pytest.raises(CapReached, match="closed"):
        c.open_day(started_today=False)


def test_a_new_day_starts_at_zero_and_nothing_carries_over(tmp_path):
    b = budget(tmp_path)
    b.start_decision("AAPL")
    nxt = DailyBudget(CAPS, date(2026, 11, 24), tmp_path)
    nxt.open_day(started_today=False)
    s = nxt.snapshot()
    assert s["decisions"] == 0 and s["symbols"] == [] and s["day"] == "2026-11-24"


def test_blocked_a_held_budget_lock_counts_as_cap_reached(tmp_path, monkeypatch):
    import pipeline.budget as pb
    from pipeline.filelock import exclusive_file_lock
    b = budget(tmp_path)
    monkeypatch.setattr(pb, "LOCK_TIMEOUT_S", 0.2)
    held, release = threading.Event(), threading.Event()

    def hold():
        with exclusive_file_lock(b.lock_path):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(CapReached, match="lock timed out"):
            b.count_request("av")
    finally:
        release.set()
        t.join()
    assert b.snapshot()["av_calls"] == 0


def test_two_real_processes_share_the_cap(tmp_path):
    """A second Python process takes tokens from the same budget file: between them they
    never pass the cap, and each sees the other's counts."""
    import subprocess
    import sys
    from pathlib import Path as _P
    b = budget(tmp_path, Caps(10, 1000, 3, 5))
    b.reserve_tokens(700)
    child = (
        "from datetime import date\n"
        "from pipeline.budget import CapReached, Caps, DailyBudget\n"
        f"b = DailyBudget(Caps(10, 1000, 3, 5), date(2026, 11, 23), r'{tmp_path}')\n"
        "b.open_day(started_today=True)\n"
        "ok = 0\n"
        "for _ in range(5):\n"
        "    try:\n"
        "        b.reserve_tokens(100)\n"
        "        ok += 1\n"
        "    except CapReached:\n"
        "        pass\n"
        "b.count_request('fmp')\n"
        "print(ok)\n"
    )
    out = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True,
                         cwd=_P(__file__).resolve().parent.parent, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "3"  # 700 + 3 x 100 = 1000: a 4th would pass the cap
    s = b.snapshot()
    assert b.tokens_committed(s) == 1000 and s["fmp_requests"] == 1
