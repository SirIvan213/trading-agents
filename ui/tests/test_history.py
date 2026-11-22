"""GET /api/decisions: the History page (UI-3, #216), over a journal written by the journal's
own code with every outcome kind. The golden response ui/fixtures/api/decisions.json is what
the frontend's Vitest tests render; this test checks the real endpoint against it.
Regenerate after a deliberate change:
    UPDATE_GOLDEN=1 .venv/Scripts/python.exe -m pytest -q ui/tests/test_history.py
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from journal.log import Journal
from ui.api.app import create_app
from ui.api.history import load_reasons
from ui.tests.journal_fixtures import HISTORY_NOW, write_history

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "ui" / "fixtures" / "api" / "decisions.json"


@pytest.fixture
def hist(paths):
    write_history(Journal(paths.journal))
    return paths


def client(paths):
    return TestClient(create_app(paths, clock=lambda: HISTORY_NOW, web_dir=None),
                      base_url="http://127.0.0.1")


def get(paths, **params):
    r = client(paths).get("/api/decisions", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def by_run(body):
    return {i["run_id"]: i for i in body["items"]}


def test_the_endpoint_matches_the_golden_json(hist):
    body = get(hist, limit=200)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    assert body == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_every_outcome_kind_maps_to_buy_pass_skipped_or_failed(hist):
    rows = by_run(get(hist, limit=200))
    expected = {
        "r-skip": ("SKIPPED", "skipped"), "r-pass": ("PASS", "pass:trader"),
        "r-fatal": ("PASS", "pass:fatal_bear"), "r-stale": ("PASS", "pass:stale_before_trader"),
        "r-risk": ("BUY", "no_order:risk"), "r-quote": ("BUY", "no_order:quote"),
        "r-timeout": ("BUY", "declined:timeout"), "r-decl": ("BUY", "declined"),
        "r-refused": ("BUY", "refused_after_approval"),
        "r-changed": ("BUY", "changed_after_approval"), "r-live": ("BUY", "approved"),
        "r-expired": ("BUY", "approved"), "r-stop": ("BUY", "approved"),
        "r-target": ("BUY", "approved"), "r-rej": ("BUY", "approved"),
        "r-locked": ("FAILED", "journal_locked"), "r-odd": ("FAILED", "mystery_status"),
        "r-fri": ("PASS", "pass:trader"),
    }
    for run, (outcome, code) in expected.items():
        assert (rows[run]["outcome"], rows[run]["why"]["code"]) == (outcome, code), run
    failed = [r for r in rows.values() if r["symbol"] == "CVX"]
    assert len(failed) == 1 and failed[0]["outcome"] == "FAILED"
    assert failed[0]["run_id"].startswith("line-") and failed[0]["why"]["code"] == "failed"
    assert len(rows) == 19  # one row per decision, the day's notes are not rows


def test_failed_and_unknown_decisions_are_never_dropped(hist):
    body = get(hist, outcome="failed")
    assert {i["symbol"] for i in body["items"]} == {"XOM", "CVX", "DIS"}
    odd = by_run(body)["r-odd"]
    assert odd["why"]["text"] == "mystery_status"  # an unknown code is shown as itself


def test_why_is_a_fixed_string_for_a_known_code_with_the_recorded_detail(hist):
    rows = by_run(get(hist, limit=200))
    reasons = load_reasons()["why"]
    for row in rows.values():
        code = row["why"]["code"]
        assert row["why"]["text"] == reasons.get(code, code)
    assert rows["r-risk"]["why"]["detail"] == ("event gate: EVENT (earnings 2026-11-24 "
                                               "before the open)")
    assert rows["r-timeout"]["why"]["text"].startswith("No answer before the plan expired")


def test_the_event_check_is_recorded_or_not_reached_never_guessed(hist):
    rows = by_run(get(hist, limit=200))
    for run in ("r-skip", "r-pass", "r-fatal", "r-stale", "r-quote", "r-locked", "r-odd"):
        assert rows[run]["gate"] == {"status": "not reached", "reason": None}, run
    assert rows["r-risk"]["gate"] == {"status": "EVENT",
                                      "reason": "earnings 2026-11-24 before the open"}
    assert rows["r-live"]["gate"]["status"] == "CLEAR"


def test_money_is_shown_exactly_as_recorded(hist):
    rows = by_run(get(hist, limit=200))
    assert rows["r-stop"]["realized_pnl"] == "-31.65"
    assert rows["r-target"]["realized_pnl"] == "45.20"
    assert rows["r-live"]["realized_pnl"] is None and rows["r-live"]["max_loss"] == "49.84"
    assert rows["r-risk"]["entry"] == "101.20" and rows["r-risk"]["max_loss"] == "7.50"
    assert rows["r-quote"]["entry"] is None and rows["r-quote"]["qty"] is None


def test_order_statuses_come_from_the_order_lifecycle(hist):
    rows = by_run(get(hist, limit=200))
    status = {r: (rows[r]["order_status"] or {}).get("code") for r in rows}
    assert status["r-live"] == "approved_not_sent"
    assert status["r-expired"] == "approved_expired"  # its plan expired, never sent
    assert status["r-stop"] == "closed" and status["r-rej"] == "rejected"
    assert status["r-decl"] is None and status["r-pass"] is None


def test_tiles_and_the_realized_total(hist):
    body = get(hist)
    assert body["tiles"] == {"reviewed": 15, "buys": 11, "passed": 4, "skipped": 1,
                             "failed": 3, "realized_pnl": "13.55"}
    assert body["counts"] == {"all": 19, "buy": 11, "pass": 4, "skipped": 1, "failed": 3}


def test_the_realized_total_is_null_until_a_trade_closes(paths):
    j = Journal(paths.journal)
    j.append({"status": "pass", "run_id": "r1", "symbol": "KO", "forced_pass": None,
              "checked_at": HISTORY_NOW.isoformat()})
    assert get(paths)["tiles"]["realized_pnl"] is None


def test_the_outcome_filter_and_counts(hist):
    for outcome, n in (("all", 19), ("buy", 11), ("pass", 4), ("skipped", 1), ("failed", 3)):
        body = get(hist, outcome=outcome, limit=200)
        assert body["total"] == n and len(body["items"]) == n
        assert body["counts"]["all"] == 19  # counts ignore the outcome filter


def test_the_date_range(hist):
    friday = get(hist, **{"from": "2026-11-20", "to": "2026-11-20"})
    assert [i["symbol"] for i in friday["items"]] == ["BAC"]
    monday = get(hist, **{"from": "2026-11-23"}, limit=200)
    assert "BAC" not in {i["symbol"] for i in monday["items"]}
    assert monday["tiles"]["passed"] == 3


def test_newest_first_and_pagination(hist):
    first = get(hist, limit=5)
    assert len(first["items"]) == 5 and first["next_cursor"] == "5"
    ats = [i["at"] for i in first["items"]]
    assert ats == sorted(ats, reverse=True)
    rest = get(hist, limit=200, cursor=first["next_cursor"])
    assert rest["next_cursor"] is None
    ids = [i["run_id"] for i in first["items"]] + [i["run_id"] for i in rest["items"]]
    assert len(ids) == len(set(ids)) == 19


@pytest.mark.parametrize("params", [{"outcome": "maybe"}, {"limit": 0}, {"limit": 201},
                                    {"cursor": "-1"}, {"cursor": "x"}, {"from": "monday"}])
def test_bad_parameters_are_refused(hist, params):
    assert client(hist).get("/api/decisions", params=params).status_code == 422


def test_a_corrupt_line_is_a_problem_and_earlier_rows_still_show(hist):
    with hist.journal.open("ab") as fh:
        fh.write(b"this is not json\n")
    body = get(hist, limit=200)
    assert any("is not JSON" in p["text"] for p in body["problems"])
    assert body["total"] == 19  # everything written before it is still shown


def test_a_cut_off_last_line_is_a_problem(hist):
    with hist.journal.open("ab") as fh:
        fh.write(b'{"status": "pass", "run_id": "r-half"')
    body = get(hist, limit=200)
    assert any("last line is incomplete" in p["text"] for p in body["problems"])
    assert "r-half" not in by_run(body)


def test_an_impossible_order_history_is_a_problem_and_rows_still_show(hist):
    j = Journal(hist.journal)
    j.append({"status": "filled", "client_order_id": "ta-nope", "at": HISTORY_NOW.isoformat(),
              "filled_qty": 1, "avg_price": "1"})
    body = get(hist, limit=200)
    assert any("order statuses are not shown" in p["text"] for p in body["problems"])
    assert body["total"] == 19
    assert all(i["order_status"] is None for i in body["items"])


def test_reading_history_never_locks_or_writes(hist):
    Journal(hist.journal).lock_path.unlink()
    before = sorted(p.name for p in hist.journal.parent.iterdir())
    get(hist)
    assert sorted(p.name for p in hist.journal.parent.iterdir()) == before


# ---- the reasons content covers every code the pipeline records ----

def test_every_forced_pass_code_has_a_plain_english_reason():
    trader = (REPO / "agents" / "trader.py").read_text(encoding="utf-8")
    decide = (REPO / "pipeline" / "decide.py").read_text(encoding="utf-8")
    schemas = (REPO / "agents" / "schemas.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'code = "([a-z_]+)"', trader))
    codes |= set(re.findall(r'_forced\(symbol, "([a-z_]+)"\)', trader))
    codes |= set(re.findall(r'forced_pass\([^,]+, "([a-z_]+)"', decide))
    rc = re.search(r"ReasonCode = Literal\[(.*?)\]", schemas, re.S)
    assert rc is not None
    codes |= {c for c in re.findall(r'"([a-z_]+)"', rc.group(1))
              if c in ("api_error", "cancelled", "exception", "invalid_output")}
    assert {"fatal_bear", "analyst_failed", "no_plans", "stale_before_trader"} <= codes
    why = load_reasons()["why"]
    assert {c for c in codes if f"pass:{c}" not in why} == set()


def test_every_no_order_stage_has_a_plain_english_reason():
    flow = (REPO / "pipeline" / "order_flow.py").read_text(encoding="utf-8")
    stages = set(re.findall(r'refuse\("([a-z_]+)"', flow))
    assert {"plan", "quote", "size", "risk", "journal", "events", "limits"} <= stages
    why = load_reasons()["why"]
    assert {s for s in stages if f"no_order:{s}" not in why} == set()


def test_every_order_status_has_a_plain_english_name():
    from journal.orders import APPROVED, ORDER_EVENTS

    names = load_reasons()["order_status"]
    assert {*ORDER_EVENTS, APPROVED, "approved_expired"} <= set(names)
