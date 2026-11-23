"""GET /api/live: the Live run page (UI-5, #218).

Golden responses live in ui/fixtures/api/live-*.json (the Vitest tests render them).
Regenerate them after an intended change with:
    UPDATE_GOLDEN=1 .venv/Scripts/python.exe -m pytest -q ui/tests/test_live.py
"""
from __future__ import annotations

import json
import os
import types
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from journal.log import Journal
from journal.orders import client_order_id
from ui.api import live as lv
from ui.api.app import create_app
from ui.api.readers import shared_open
from ui.tests.live_fixtures import (
    DAY,
    KINDS,
    LIVE_NOW,
    budget,
    caches,
    configs,
    heartbeat,
    morning,
    write_live,
)

GOLDEN = Path(__file__).resolve().parent.parent / "fixtures" / "api"
GOLDEN_CASES = ("normal", "not_running_closed", "calendar_down", "stale_heartbeat")
EXPECTED = {
    "normal": "normal", "before_scan": "idle", "not_running": "not_running",
    "not_running_closed": "not_running", "ended": "idle", "stale_heartbeat": "blocked",
    "future_heartbeat": "blocked", "corrupt_journal": "blocked",
    "unreadable_budget": "blocked", "budget_exhausted": "blocked", "kill_switch": "blocked",
    "held": "blocked", "calendar_down": "blocked", "run_end_cap": "blocked",
}
BLOCKED_CODE = {
    "stale_heartbeat": "heartbeat_stale", "future_heartbeat": "heartbeat_future",
    "corrupt_journal": "journal", "unreadable_budget": "budget_unreadable",
    "budget_exhausted": "budget_exhausted", "kill_switch": "kill_switch", "held": "held",
    "calendar_down": "event_calendar", "run_end_cap": "run_day_end",
}


def client(paths, now):
    return TestClient(create_app(paths, clock=lambda: now, web_dir=None),
                      base_url="http://127.0.0.1")


def get(paths, kind):
    now = write_live(paths, kind)
    r = client(paths, now).get("/api/live")
    assert r.status_code == 200
    return r.json()


def test_every_case_is_listed():
    assert set(EXPECTED) == set(KINDS)


@pytest.mark.parametrize("kind", KINDS)
def test_every_status_kind(paths, kind):
    body = get(paths, kind)
    st = body["status"]
    assert st["kind"] == EXPECTED[kind]
    assert st["kind"] != "order_waiting" and body["ticket"] is None  # needs #212
    if st["kind"] == "blocked":
        assert st["sentence"].startswith("New trades are blocked: ")
        assert BLOCKED_CODE[kind] in [r["code"] for r in st["reasons"]]
    else:
        assert st["reasons"] == []


def test_the_sentences_are_fixed_templates(paths):
    assert get(paths, "normal")["status"]["sentence"] == "All normal · nothing needs you"


@pytest.mark.parametrize(("kind", "sentence"), [
    ("before_scan", "Idle · waiting for the 9:40 AM ET scan"),
    ("ended", "Done for today · the entry window closed"),
    ("not_running", "Not running today · run_day has not started"),
    ("not_running_closed", "Not running today · market closed"),
])
def test_idle_and_not_running_sentences(paths, kind, sentence):
    assert get(paths, kind)["status"]["sentence"] == sentence


def test_not_running_outside_market_hours_is_calm(paths):
    body = get(paths, "not_running_closed")
    assert body["status"]["kind"] == "not_running"
    assert body["market"]["state"] == "closed"
    assert not [p for p in body["problems"] if p["level"] == "error"]
    assert body["current"] is None and body["today"] == []


@pytest.mark.parametrize("seconds", [91, 600])
def test_a_heartbeat_more_than_90_s_old_is_blocked(paths, seconds):
    now = write_live(paths, "normal")
    heartbeat(paths, now - timedelta(seconds=seconds), "deciding AMD")
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "blocked"
    assert body["run_day"]["stale"] is True
    assert body["status"]["reasons"][0]["code"] == "heartbeat_stale"


def test_a_heartbeat_90_s_old_is_not_stale(paths):
    now = write_live(paths, "normal")
    heartbeat(paths, now - timedelta(seconds=90), "deciding AMD")
    body = client(paths, now).get("/api/live").json()
    assert body["run_day"]["stale"] is False and body["status"]["kind"] == "normal"


def test_a_heartbeat_a_second_ahead_is_not_the_future(paths):
    """The clock is read before the heartbeat: a write in between is not a clock problem."""
    now = write_live(paths, "normal")
    heartbeat(paths, now + timedelta(seconds=1), "deciding AMD")
    body = client(paths, now).get("/api/live").json()
    assert body["run_day"]["future"] is False and body["status"]["kind"] == "normal"


@pytest.mark.parametrize("seconds", [5, 300])
def test_a_heartbeat_dated_in_the_future_is_stale_and_blocked(paths, seconds):
    now = write_live(paths, "normal")
    heartbeat(paths, now + timedelta(seconds=seconds), "deciding AMD")
    body = client(paths, now).get("/api/live").json()
    assert body["run_day"]["future"] is True and body["run_day"]["stale"] is True
    assert body["status"]["kind"] == "blocked"
    assert body["status"]["reasons"][0]["code"] == "heartbeat_future"
    assert body["current"] is None  # a future heartbeat is not today's run


def test_a_corrupt_journal_is_blocked_and_shown(paths):
    body = get(paths, "corrupt_journal")
    assert body["status"]["kind"] == "blocked"
    assert any(p["level"] == "error" and "not JSON" in p["text"] for p in body["problems"])
    # totals that cannot be trusted are not shown
    shown = {x["key"]: x for x in body["key_limits"]}
    assert shown["money_at_risk"]["used"] is None and shown["open_positions"]["used"] is None
    assert body["positions"] is None


def test_an_impossible_order_history_is_blocked(paths):
    now = write_live(paths, "normal")
    j = Journal(paths.journal)
    # a second approved entry for the same order: the replay refuses it
    first = next(e for e in j.entries() if e.get("status") == "approved_not_sent")
    with open(paths.journal, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(first) + "\n")
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "blocked"
    assert body["status"]["reasons"][0]["code"] == "journal"
    assert body["positions"] is None


@pytest.mark.parametrize("content", [b"{not json", b"[]", b'{"day": "2026-11-23"}',
                                     b'{"day": "2026-11-20", "decisions": 0}'])
def test_an_unreadable_or_invalid_budget_is_blocked(paths, content):
    now = write_live(paths, "normal")
    (paths.state_dir / f"budget-{DAY}.json").write_bytes(content)
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "blocked"
    assert "budget_unreadable" in [r["code"] for r in body["status"]["reasons"]]
    caps = {x["key"]: x for x in body["caps"]}
    assert caps["tokens"]["used"] is None  # never a guessed zero


def test_a_missing_budget_after_the_day_started_is_blocked(paths):
    now = write_live(paths, "normal")
    (paths.state_dir / f"budget-{DAY}.json").unlink()
    body = client(paths, now).get("/api/live").json()
    assert "budget_missing" in [r["code"] for r in body["status"]["reasons"]]


def test_the_token_cap_counts_reservations(paths):
    now = write_live(paths, "normal")
    budget(paths, tokens_used=590000, reservations={"x": 10000})
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "blocked"
    tokens = {x["key"]: x for x in body["caps"]}["tokens"]
    assert tokens["used"] == "590000" and tokens["reached"] is True
    assert tokens["note"] == "10000 more reserved for calls in progress"


def test_a_closed_market_day_with_a_no_session_end_is_idle(paths):
    configs(paths)
    sat = LIVE_NOW.replace(day=28)
    Journal(paths.journal).append({"status": "run_day_end", "day": "2026-11-28",
                                   "at": sat.isoformat(), "reason": "no session (weekend)"})
    body = client(paths, sat).get("/api/live").json()
    assert body["status"]["kind"] == "idle"
    assert body["status"]["sentence"] == "Done for today · no session today"
    assert body["market"]["text"] == "Market closed today (weekend)"


def test_a_stale_heartbeat_after_the_close_with_no_end_is_still_blocked(paths):
    now = write_live(paths, "normal").replace(hour=17)
    heartbeat(paths, now.replace(hour=11), "deciding AMD")
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "blocked"


def test_after_a_normal_end_and_the_close_the_reasons_are_problems(paths):
    now = write_live(paths, "ended").replace(hour=17)
    configs(paths, trading_enabled=False)
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "idle"
    assert any("kill switch" in p["text"] for p in body["problems"])


def test_a_reason_is_a_problem_when_not_running(paths):
    now = write_live(paths, "not_running")
    configs(paths, trading_enabled=False)
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "not_running"
    assert any("kill switch" in p["text"] for p in body["problems"])


# ---- the budget read: one short read, never held open ------------------------------------

def test_the_budget_is_read_once_and_closed_before_the_response_is_built(paths, monkeypatch):
    now = write_live(paths, "normal")
    opened: list = []
    real = shared_open.open_shared

    def spy(path):
        fh = real(path)
        opened.append((Path(path).name, fh))
        return fh

    monkeypatch.setattr(shared_open, "open_shared", spy)
    checked = []
    real_limits = lv.build_limits

    def at_build(*args, **kwargs):
        # the budget's data is in hand: every handle must already be closed
        assert [n for n, _ in opened].count(f"budget-{DAY}.json") == 1
        assert all(fh.closed for _, fh in opened)
        checked.append(True)
        return real_limits(*args, **kwargs)

    monkeypatch.setattr(lv, "build_limits", at_build)
    c = client(paths, now)
    for _ in range(3):  # each request reads it once
        opened.clear()
        assert c.get("/api/live").status_code == 200
        assert [n for n, _ in opened].count(f"budget-{DAY}.json") == 1
        assert all(fh.closed for _, fh in opened)
    assert len(checked) == 3


def test_a_budget_write_succeeds_between_requests(paths):
    """The writer's replace (tools/atomic_write.py, what DailyBudget uses) never meets an
    open UI handle."""
    from tools.atomic_write import atomic_write_text

    now = write_live(paths, "normal")
    c = client(paths, now)
    p = paths.state_dir / f"budget-{DAY}.json"
    for n in range(4, 7):
        assert c.get("/api/live").status_code == 200
        s = json.loads(p.read_text(encoding="utf-8"))
        s["decisions"] = n
        atomic_write_text(p, json.dumps(s))
    assert {x["key"]: x for x in c.get("/api/live").json()["key_limits"]}[
        "decisions"]["used"] == "6"


def test_a_real_budget_and_heartbeat_read_as_valid(paths):
    """Written by pipeline.budget.DailyBudget and pipeline.run_day.Day.beat themselves."""
    from pipeline.budget import DailyBudget, load_run_day_config
    from pipeline.run_day import Day

    configs(paths)
    morning(Journal(paths.journal))
    caches(paths, "av-earnings")
    cfg = load_run_day_config(paths.config_dir / "run_day.json")
    b = DailyBudget(cfg.caps, LIVE_NOW.date(), paths.state_dir)
    b.open_day(started_today=False)
    for sym in ("NVDA", "KO", "AMD"):
        b.start_decision(sym)
    b.settle_tokens(b.reserve_tokens(5000), 4200)
    b.count_request("av")
    fake = types.SimpleNamespace(phase="starting", budget=b, clock=lambda: LIVE_NOW,
                                 state_dir=paths.state_dir,
                                 heartbeat_path=paths.state_dir / "run_day.heartbeat.json")
    Day.beat(fake, "deciding AMD")
    body = client(paths, LIVE_NOW).get("/api/live").json()
    assert body["status"]["kind"] == "normal", body["status"]
    assert body["problems"] == []
    shown = {x["key"]: x["used"] for x in body["key_limits"] + body["caps"]}
    assert shown["decisions"] == "3" and shown["tokens"] == "4200" and shown["av_calls"] == "1"
    assert body["current"]["symbol"] == "AMD" and body["current"]["live"] is True


# ---- limits ------------------------------------------------------------------------------

def test_key_limits_and_caps_as_recorded(paths):
    body = get(paths, "normal")
    assert [x["key"] for x in body["key_limits"]] == ["decisions", "money_at_risk",
                                                       "open_positions"]
    assert [x["key"] for x in body["caps"]] == ["tokens", "av_calls", "fmp_requests", "trades"]
    k = {x["key"]: x for x in body["key_limits"] + body["caps"]}
    assert (k["decisions"]["used"], k["decisions"]["limit"]) == ("3", "10")
    assert (k["money_at_risk"]["used"], k["money_at_risk"]["limit"]) == ("15.91", "150.00")
    assert (k["open_positions"]["used"], k["open_positions"]["limit"]) == ("1", "2")
    assert k["open_positions"]["reached"] is False
    assert (k["trades"]["used"], k["trades"]["limit"]) == ("1", "3")
    assert k["money_at_risk"]["unit"] == "usd" and k["money_at_risk"]["reached"] is False


def test_money_at_risk_and_open_positions_match_risk_checks_day_state(paths):
    """The same numbers as the journal's own day_state, which risk_check reads."""
    now = write_live(paths, "normal")
    ds = Journal(paths.journal).day_state(now)
    k = {x["key"]: x for x in client(paths, now).get("/api/live").json()["key_limits"]}
    assert k["money_at_risk"]["used"] == str(ds.risk_used_today)
    assert k["open_positions"]["used"] == str(ds.open_positions)


def test_without_configs_the_limits_are_unknown_not_guessed(paths):
    now = write_live(paths, "normal")
    for f in paths.config_dir.iterdir():
        f.unlink()
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "blocked"
    for x in body["key_limits"] + body["caps"]:
        assert x["limit"] is None and x["reached"] is False and x["pct"] is None


# ---- today's stocks, the current decision, the position ---------------------------------

def test_today_groups_like_history_with_the_deciding_symbol_first(paths):
    rows = get(paths, "normal")["today"]
    assert [(r["symbol"], r["outcome"]) for r in rows] == [
        ("AMD", "DECIDING"), ("DIS", "SKIPPED"), ("KO", "PASS"), ("NVDA", "BUY")]
    assert rows[3]["order_status"]["text"] == "sent, fill not recorded"


def test_a_counted_symbol_with_no_outcome_is_not_deciding_when_run_day_stopped(paths):
    rows = get(paths, "stale_heartbeat")["today"]
    assert rows[0]["symbol"] == "AMD" and rows[0]["outcome"] == "NO_OUTCOME"


def test_yesterdays_decisions_are_not_today(paths):
    now = write_live(paths, "normal")
    Journal(paths.journal).append({"status": "pass", "run_id": "r-old", "symbol": "BAC",
                                   "forced_pass": None,
                                   "checked_at": (now - timedelta(days=3)).isoformat()})
    rows = client(paths, now).get("/api/live").json()["today"]
    assert "BAC" not in [r["symbol"] for r in rows]


def test_the_current_symbol_comes_from_the_phase_text_only(paths):
    now = write_live(paths, "normal")
    for phase, symbol in (("deciding AMD", "AMD"), ("waiting for the next cycle", None),
                          ("deciding 10 shares now", None), ("cycle scan", None)):
        heartbeat(paths, now - timedelta(seconds=5), phase)
        cur = client(paths, now).get("/api/live").json()["current"]
        assert cur["line"] == phase and cur["symbol"] == symbol
        assert cur["step"] is None and cur["analysts"] is None


def test_the_phase_text_is_plain_and_capped(paths):
    now = write_live(paths, "normal")
    heartbeat(paths, now - timedelta(seconds=5), "deciding ‮AMD" + "x" * 400)
    cur = client(paths, now).get("/api/live").json()["current"]
    assert "‮" not in cur["line"] and "⟨U+202E⟩" in cur["line"]
    assert len(cur["line"]) <= 120 and cur["symbol"] is None


def test_a_sent_order_is_an_open_position_with_no_fill_recorded(paths):
    pos = get(paths, "normal")["positions"]
    assert len(pos) == 1
    p = pos[0]
    assert (p["symbol"], p["qty"], p["filled_qty"], p["avg_price"]) == ("NVDA", 6, 0, None)
    assert (p["stop"], p["target"], p["max_loss"]) == ("243.77", "262.46", "15.91")
    assert p["status"]["text"] == "sent, fill not recorded" and p["unrealized"] is None


def test_a_filled_order_shows_its_recorded_fill_and_a_closed_one_is_gone(paths):
    now = write_live(paths, "normal")
    j = Journal(paths.journal)
    cid = client_order_id("r-nvda", "p-nvda")
    j.record_order_event(cid, "filled", now - timedelta(minutes=20), filled_qty=6,
                         avg_price="250.20")
    p = client(paths, now).get("/api/live").json()["positions"][0]
    assert (p["filled_qty"], p["avg_price"], p["status"]["text"]) == (6, "250.20", "filled")
    assert p["unrealized"] is None
    j.record_order_event(cid, "closed", now - timedelta(minutes=1), closed_by="target",
                         realized_pnl="73.50")
    assert client(paths, now).get("/api/live").json()["positions"] == []


def test_an_approval_not_sent_is_not_a_position(paths):
    from ui.tests.journal_fixtures import approved_entry

    now = write_live(paths, "normal")
    Journal(paths.journal).append(approved_entry("r-w", "p-w", now - timedelta(seconds=30),
                                                 symbol="AMD"))
    assert [p["symbol"] for p in client(paths, now).get("/api/live").json()["positions"]] == [
        "NVDA"]


# ---- data sources ------------------------------------------------------------------------

def test_sources_collapse_when_all_are_ok(paths):
    src = get(paths, "normal")["sources"]
    assert src == {**src, "ok": True, "expanded": False, "summary": "All 3 sources OK"}


def test_a_failed_source_expands_the_line_by_itself(paths):
    src = get(paths, "calendar_down")["sources"]
    assert src["ok"] is False and src["expanded"] is True
    assert src["summary"] == ("Not working: Earnings calendar (Alpha Vantage). Details "
                              "below.")
    av = src["items"][1]
    assert av["ok"] is False
    assert av["detail"].startswith("no valid calendar today, and all 5 calls are used")


def test_a_failed_scan_is_a_failed_source(paths):
    now = write_live(paths, "normal")
    Journal(paths.journal).append({"status": "run_day_scan", "at": now.isoformat(),
                                   "ok": False, "reason": "snapshots failed (TimeoutError)"})
    src = client(paths, now).get("/api/live").json()["sources"]
    assert src["expanded"] is True
    assert src["items"][0] == {"name": "Prices (Alpaca)", "ok": False,
                               "detail": "last scan failed: snapshots failed (TimeoutError)"}


def test_fmp_used_up_with_no_calendar_clears_nothing(paths):
    now = write_live(paths, "normal")
    os.remove(paths.journal.parent / "cache" / f"fmp-earnings-{DAY}.json")
    budget(paths, fmp_requests=3)
    body = client(paths, now).get("/api/live").json()
    fmp = body["sources"]["items"][2]
    assert fmp["ok"] is False and "its absence clears nothing" in fmp["detail"]
    assert body["status"]["kind"] == "normal"  # block-only: its absence blocks nothing


def test_the_caches_are_listed_never_opened(paths, monkeypatch):
    now = write_live(paths, "normal")
    opened = []
    real = shared_open.open_shared
    monkeypatch.setattr(shared_open, "open_shared",
                        lambda p: opened.append(Path(p)) or real(p))
    import builtins
    real_open = builtins.open

    def no_cache_open(file, *a, **k):
        assert "cache" not in Path(str(file)).parts, file
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", no_cache_open)
    assert client(paths, now).get("/api/live").status_code == 200
    assert not [p for p in opened if "cache" in p.parts]


# ---- ETag, read-only ---------------------------------------------------------------------

def test_the_etag_answers_304_until_something_changes(paths):
    now = write_live(paths, "normal")
    clock = {"now": now}
    c = TestClient(create_app(paths, clock=lambda: clock["now"], web_dir=None),
                   base_url="http://127.0.0.1")
    r = c.get("/api/live")
    tag = r.headers["etag"]
    assert r.headers["cache-control"] == "no-cache"
    clock["now"] = now + timedelta(seconds=2)  # only the time and the heartbeat's age moved
    r2 = c.get("/api/live", headers={"If-None-Match": tag})
    assert r2.status_code == 304 and r2.headers["etag"] == tag and r2.content == b""
    clock["now"] = now + timedelta(seconds=100)  # the heartbeat turned stale
    r3 = c.get("/api/live", headers={"If-None-Match": tag})
    assert r3.status_code == 200 and r3.headers["etag"] != tag
    assert r3.json()["status"]["kind"] == "blocked"


def test_the_live_route_never_writes_or_locks(paths):
    now = write_live(paths, "normal")

    def tree():
        return sorted((str(p.relative_to(paths.journal.parent)), p.stat().st_size,
                       p.stat().st_mtime_ns) for p in paths.journal.parent.rglob("*"))

    before = tree()
    c = client(paths, now)
    for _ in range(3):
        assert c.get("/api/live").status_code == 200
    assert tree() == before
    assert not list(paths.state_dir.glob("*.lock"))


@pytest.mark.parametrize("case", GOLDEN_CASES)
def test_golden_responses(paths, case):
    body = get(paths, case)
    path = GOLDEN / f"live-{case.replace('_', '-')}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    assert body == json.loads(path.read_text(encoding="utf-8"))


# ---- review round 1 ----------------------------------------------------------------------

@pytest.mark.parametrize("at", ["9999-12-31T23:00:00-05:00", "0001-01-01T00:00:00+05:00"])
def test_a_time_at_the_date_limits_is_never_a_500(paths, at):
    now = write_live(paths, "normal")
    Journal(paths.journal).append({"status": "run_day_scan", "at": at, "ok": True,
                                   "candidates": [], "rejected": 0})
    assert client(paths, now).get("/api/live").status_code == 200
    body = json.loads((paths.state_dir / "run_day.heartbeat.json").read_text("utf-8"))
    body["at"] = at
    (paths.state_dir / "run_day.heartbeat.json").write_text(json.dumps(body), "utf-8")
    r = client(paths, now).get("/api/live")
    assert r.status_code == 200 and r.json()["status"]["kind"] == "blocked"
    # History reads the same times
    assert client(paths, now).get("/api/decisions").status_code == 200


@pytest.mark.parametrize("body", [
    {"at": 12, "pid": "x", "phase": ["a"]}, {}, {"at": None}, {"phase": 3, "pid": True},
])
def test_a_heartbeat_with_odd_types_is_shown_not_a_500(paths, body):
    now = write_live(paths, "normal")
    (paths.state_dir / "run_day.heartbeat.json").write_text(json.dumps(body), "utf-8")
    r = client(paths, now).get("/api/live")
    assert r.status_code == 200
    rd = r.json()["run_day"]
    assert rd["stale"] is True and rd["pid"] is None and rd["phase"] is None


def test_a_held_day_is_not_held_after_a_restart_that_found_nothing(paths):
    now = write_live(paths, "held")
    Journal(paths.journal).append({"status": "run_day_start", "day": DAY,
                                   "at": (now - timedelta(minutes=30)).isoformat(),
                                   "pid": 4243, "caps": {}, "universe": []})
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "normal", body["status"]


def test_an_end_whose_reason_is_not_text_is_blocked(paths):
    now = write_live(paths, "normal")
    Journal(paths.journal).append({"status": "run_day_end", "day": DAY,
                                   "at": now.isoformat(), "reason": ["x"]})
    body = client(paths, now).get("/api/live").json()
    assert body["status"]["kind"] == "blocked"
    assert body["status"]["reasons"][0]["text"] == "run_day ended with no reason recorded"


def test_after_a_normal_end_with_the_market_open_the_reasons_are_problems(paths):
    now = write_live(paths, "ended")
    configs(paths, trading_enabled=False)
    body = client(paths, now).get("/api/live").json()
    assert body["market"]["state"] == "open"
    assert body["status"]["kind"] == "idle"
    assert any("kill switch" in p["text"] for p in body["problems"])


def test_the_normal_end_reasons_are_run_days_own():
    """NORMAL_ENDS are pipeline/run_day.py's literal reasons: a reworded one fails here."""
    src = (Path(__file__).resolve().parent.parent.parent / "pipeline" / "run_day.py"
           ).read_text(encoding="utf-8")
    assert '"entry window closed"' in src
    assert 'f"no session ({closed_reason(today)})"' in src
    assert lv.NORMAL_ENDS == ("entry window closed", "no session (")


def test_a_phase_4_approval_without_a_client_order_id_shows_its_stop_and_target(paths):
    now = write_live(paths, "normal")
    lines = paths.journal.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        e = json.loads(line)
        if e.get("status") == "approved_not_sent":
            e.pop("client_order_id")
        out.append(json.dumps(e))
    paths.journal.write_text("\n".join(out) + "\n", encoding="utf-8")
    p = client(paths, now).get("/api/live").json()["positions"][0]
    assert (p["stop"], p["target"]) == ("243.77", "262.46")


@pytest.mark.parametrize(("change", "words"), [
    ({"max_open_positions": 1}, "1 position is open (at most 1 at once)"),
    ({"max_trades_per_day": 1}, "all 1 of today's trades are used"),
    ({"daily_loss_limit": "15.00"}, "today's money-at-risk limit ($15.00) is reached"),
])
def test_a_reached_risk_limit_is_said_never_all_normal(paths, change, words):
    now = write_live(paths, "normal")
    configs(paths, **{"max_open_positions": 2, **change})
    st = client(paths, now).get("/api/live").json()["status"]
    assert st["kind"] == "normal"
    assert st["sentence"] == f"Running · no new entries while {words}"


def test_a_calendar_not_in_yet_with_calls_left_is_blocked(paths):
    now = write_live(paths, "normal")
    os.remove(paths.journal.parent / "cache" / f"av-earnings-{DAY}.json")
    budget(paths, av_calls=3)
    st = client(paths, now).get("/api/live").json()["status"]
    assert st["kind"] == "blocked"
    assert st["reasons"][0]["code"] == "event_calendar"
    assert "3 of 5 calls used" in st["reasons"][0]["text"]


def test_a_stale_heartbeat_still_answers_304_between_polls(paths):
    now = write_live(paths, "stale_heartbeat")
    clock = {"now": now}
    c = TestClient(create_app(paths, clock=lambda: clock["now"], web_dir=None),
                   base_url="http://127.0.0.1")
    r = c.get("/api/live")
    assert r.json()["status"]["reasons"][0]["text"].startswith(
        "run_day's last heartbeat was at 10:10:25 ET, more than 90 s ago")
    clock["now"] = now + timedelta(seconds=2)
    assert c.get("/api/live", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


# ---- review round 2 ----------------------------------------------------------------------

@pytest.mark.parametrize(("hour", "minute"), [(9, 27), (17, 0)])
def test_a_held_day_is_blocked_outside_the_session_too(paths, hour, minute):
    now = write_live(paths, "held").replace(hour=hour, minute=minute)
    heartbeat(paths, now - timedelta(seconds=5), "ended")
    body = client(paths, now).get("/api/live").json()
    assert body["market"]["state"] == "closed"
    assert body["status"]["kind"] == "blocked"
    assert body["status"]["reasons"][0]["code"] == "held"
    assert body["status"]["sentence"].startswith(
        "New trades are blocked: a position from an earlier day may still be open")


def test_a_stale_heartbeat_from_an_earlier_day_names_its_date(paths):
    now = write_live(paths, "normal")
    heartbeat(paths, now - timedelta(days=1), "deciding AMD")
    st = client(paths, now).get("/api/live").json()["status"]
    assert st["kind"] == "blocked"  # run_day started today and its heartbeat is old
    assert "last heartbeat was at 2026-11-22 10:12:00 ET" in st["reasons"][0]["text"]
