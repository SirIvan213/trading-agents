"""run_day's state files and the configs, read as the pipeline writes them, without locks."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import pipeline.run_day as rd
from pipeline.budget import STATE_DIR, Caps, DailyBudget
from ui.api.paths import STATE_DIR as UI_STATE_DIR
from ui.api.readers.config import read_limits
from ui.api.readers.shared_open import open_shared
from ui.api.readers.state import (
    HEARTBEAT_STALE_S,
    budget_exists,
    budget_path,
    heartbeat_age_s,
    heartbeat_path,
    read_state_file,
)
from ui.tests.journal_fixtures import MONDAY

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAPS = Caps(10, 600_000, 3, 5)


def test_paths_match_the_pipelines_own(tmp_path):
    assert UI_STATE_DIR == STATE_DIR
    b = DailyBudget(CAPS, date(2026, 11, 23), tmp_path)
    assert budget_path(tmp_path, date(2026, 11, 23)) == b.path
    day = SimpleNamespace(state_dir=tmp_path)
    assert heartbeat_path(tmp_path) == rd.Day.heartbeat_path.fget(day)
    assert HEARTBEAT_STALE_S >= 3 * rd.HEARTBEAT_S


def test_the_budget_reads_as_daily_budget_wrote_it(tmp_path):
    b = DailyBudget(CAPS, date(2026, 11, 23), tmp_path)
    b.open_day(started_today=False)
    b.start_decision("TSLA")
    got = read_state_file(budget_path(tmp_path, date(2026, 11, 23)))
    assert got.exists and got.error is None and got.data == b.snapshot()


@pytest.mark.xfail(os.name == "nt", strict=True, raises=Exception,
                   reason="#213: os.replace cannot replace an open file on Windows")
def test_daily_budget_can_write_while_the_ui_reads(tmp_path):
    """Holding the budget file open must never turn a write into CapReached. On Windows this
    fails until #213 (strict: it flips to a failure when #213 lands, so the app may then start
    reading the budget)."""
    b = DailyBudget(CAPS, date(2026, 11, 23), tmp_path)
    b.open_day(started_today=False)
    with open_shared(b.path):
        assert b.start_decision("TSLA") == 1
        rid = b.reserve_tokens(1000)
        b.settle_tokens(rid, 400)
    assert read_state_file(b.path).data["tokens_used"] == 400


def test_budget_exists_comes_from_the_listing(tmp_path):
    day = date(2026, 11, 23)
    assert not budget_exists(tmp_path / "missing", day)
    assert not budget_exists(tmp_path, day)
    DailyBudget(CAPS, day, tmp_path).open_day(started_today=False)
    assert budget_exists(tmp_path, day) and not budget_exists(tmp_path, date(2026, 11, 24))


def test_reading_the_budget_never_takes_its_lock_or_creates_files(tmp_path):
    b = DailyBudget(CAPS, date(2026, 11, 23), tmp_path)
    b.open_day(started_today=False)
    b.lock_path.unlink()
    before = sorted(p.name for p in tmp_path.iterdir())
    read_state_file(b.path)
    read_state_file(heartbeat_path(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_heartbeat_reads_as_run_day_wrote_it(tmp_path):
    clock = lambda: MONDAY  # noqa: E731
    day = rd.Day(config=None, data=None, clients=None, journal=None, clock=clock,
                 sleep=lambda s: None, terminal=None, state_dir=tmp_path, phase="scan")
    day.beat()
    hb = read_state_file(heartbeat_path(tmp_path))
    assert hb.data["phase"] == "scan" and hb.data["pid"] == os.getpid()
    assert heartbeat_age_s(hb, MONDAY + timedelta(seconds=42)) == 42.0


@pytest.mark.parametrize(("raw", "error"), [
    (b"{not json", "not JSON"), (b"[1]", "not a JSON object"), (b"\xff", "not JSON"),
])
def test_a_bad_state_file_is_an_error_that_never_echoes_it(tmp_path, raw, error):
    f = tmp_path / "budget-2026-11-23.json"
    f.write_bytes(raw)
    got = read_state_file(f)
    assert got.exists and got.data is None and got.error == error


def test_a_missing_state_file_is_not_an_error(tmp_path):
    got = read_state_file(tmp_path / "run_day.heartbeat.json")
    assert not got.exists and got.error is None and got.data is None


@pytest.mark.parametrize("at", [None, "garbage", "2026-11-23T10:00:00"])
def test_a_heartbeat_without_an_aware_time_has_no_age(at):
    from ui.api.readers.state import StateFile

    assert heartbeat_age_s(StateFile("hb", True, {"at": at}), MONDAY) is None


def test_limits_use_limits_json_or_fall_back_to_the_example(tmp_path):
    example = json.loads(open(os.path.join(REPO, "config", "limits.example.json")).read())
    (tmp_path / "limits.example.json").write_text(json.dumps(example))
    got = read_limits(tmp_path)
    assert got.error is None and got.source == "limits.example.json" and got.value.paper
    (tmp_path / "limits.json").write_text(json.dumps({**example, "max_trades_per_day": 2}))
    got = read_limits(tmp_path)
    assert got.source == "limits.json" and got.value.max_trades_per_day == 2


def test_invalid_limits_are_an_error_that_never_echoes_the_file(tmp_path):
    (tmp_path / "limits.json").write_text('{"paper": true, "api_key": "sk-SECRET-123"}')
    got = read_limits(tmp_path)
    assert got.value is None and got.error == "limits config is invalid"
    assert "SECRET" not in json.dumps(got.view())


def test_no_limits_file_at_all(tmp_path):
    assert read_limits(tmp_path).error == "no limits config"

