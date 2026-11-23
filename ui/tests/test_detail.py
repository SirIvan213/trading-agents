"""GET /api/decisions/{run_id}: the Decision detail page (UI-4, #217).

Each case is a journal written by the journal's own code plus a golden decision record from
tests/fixtures/records/ (written by pipeline/records.py through the real decision path). The
golden API responses in ui/fixtures/api/decision-<case>.json are what Vitest renders; this test
checks the real endpoint against them. Regenerate after a deliberate change:
    UPDATE_GOLDEN=1 .venv/Scripts/python.exe -m pytest -q ui/tests/test_detail.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from journal.log import Journal
from ui.api import detail as dt
from ui.api.app import create_app
from ui.api.history import load_reasons
from ui.tests.detail_fixtures import CASES, DETAIL_NOW, NOON, RECORDS, RUN_ID, write_case
from ui.tests.journal_fixtures import HISTORY_NOW, write_history

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "ui" / "fixtures" / "api"
STATES = {"done", "blocked", "skipped", "not_reached", "failed", "not_recorded", "declined",
          "unanswered"}
RECORD_STEPS = ("analysts", "plans", "debate")


def client(paths, now=DETAIL_NOW):
    return TestClient(create_app(paths, clock=lambda: now, web_dir=None),
                      base_url="http://127.0.0.1")


def case_paths(paths, case):
    write_case(case, paths.journal)
    return paths


def get(paths, run_id=RUN_ID, now=DETAIL_NOW):
    return client(paths, now).get(f"/api/decisions/{run_id}")


def steps(body):
    return {s["key"]: s for s in body["steps"]}


# ---- the contract with pipeline/records.py ----

@pytest.mark.parametrize("name", sorted(p.stem for p in RECORDS.glob("*.json")))
def test_every_golden_record_passes_the_reader_contract(name):
    """If pipeline/records.py changes the record's shape, this fails before UI-4 misreads."""
    obj = json.loads((RECORDS / f"{name}.json").read_text(encoding="utf-8"))
    assert dt.validate_record(obj, RUN_ID) is None
    assert obj["schema"] == dt.SCHEMA


@pytest.mark.parametrize(("change", "why"), [
    (lambda r: r.update(schema=2), "unknown schema"),
    (lambda r: r.pop("trader"), "missing field 'trader'"),
    (lambda r: r.update(analysts=[]), "wrong type"),
    (lambda r: r.update(run_id="OTHER"), "another decision"),
    (lambda r: r.update(schema=True), "wrong type"),  # True == 1, so the type check refuses
])
def test_a_drifted_record_is_refused(change, why):
    obj = json.loads((RECORDS / "buy_approved.json").read_text(encoding="utf-8"))
    change(obj)
    assert why in dt.validate_record(obj, RUN_ID)


def test_the_records_folder_rule_matches_the_writer(tmp_path):
    from pipeline.records import records_dir

    assert dt.records_dir(tmp_path / "decisions.jsonl") == records_dir(
        tmp_path / "decisions.jsonl")


# ---- golden responses ----

@pytest.mark.parametrize("case", sorted(CASES))
def test_the_endpoint_matches_the_golden_json(paths, case):
    body = get(case_paths(paths, case)).json()
    path = GOLDEN / f"decision-{case.replace('_', '-')}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    assert body == json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", sorted(CASES))
def test_eight_steps_in_code_order_with_known_states(paths, case):
    body = get(case_paths(paths, case)).json()
    assert [s["key"] for s in body["steps"]] == [
        "scan", "event_check", "analysts", "plans", "debate", "trader", "order_ticket",
        "approve_and_log"]
    assert [s["n"] for s in body["steps"]] == list(range(1, 9))
    assert {s["state"] for s in body["steps"]} <= STATES
    assert all(isinstance(s["line"], str) and s["line"] for s in body["steps"])


# ---- each case ----

def test_an_approved_buy(paths):
    body = get(case_paths(paths, "buy_approved")).json()
    s = steps(body)
    assert body["record_available"] is True and body["problems"] == []
    assert body["numbers"] == {"shares": 13, "entry": "103.10", "limit_price": "103.21",
                               "stop": "99.61", "target": "106.59", "max_loss": "46.80"}
    assert body["in_short"] == ("The ticket was for 13 shares of ABCD. Approved in "
                                "the terminal. Order: approved, not sent. Worst case at the "
                                "stop: $46.80.")
    assert s["scan"]["state"] == "done" and "11:59 ET" in s["scan"]["line"]
    assert s["event_check"]["state"] == "done" and "2026-10-04" in s["event_check"]["line"]
    assert s["analysts"]["state"] == "done"
    assert s["plans"]["details"]["plans"][0]["chosen"] is True
    assert s["debate"]["state"] == "done" and s["trader"]["state"] == "done"
    assert s["trader"]["details"]["reasoning"]
    assert s["order_ticket"]["state"] == "done"
    assert s["order_ticket"]["details"]["gate"] == {
        "status": "CLEAR", "reason": "no earnings through 2026-11-10; no halt"}
    assert s["approve_and_log"]["state"] == "done"


def test_a_fatal_bear_pass(paths):
    body = get(case_paths(paths, "pass_fatal_bear")).json()
    s = steps(body)
    assert body["outcome"] == "PASS" and body["numbers"] is None
    assert body["in_short"].startswith("No trade in ABCD. The Bear raised a fatal objection")
    assert s["debate"]["state"] == "blocked" and s["trader"]["state"] == "blocked"
    assert s["trader"]["details"]["reasoning"] is None  # a forced pass has no model reasoning
    assert s["order_ticket"]["state"] == "not_reached"
    assert s["approve_and_log"]["state"] == "not_reached"


def test_an_analyst_failure(paths):
    body = get(case_paths(paths, "pass_analyst_failed")).json()
    s = steps(body)
    assert s["analysts"]["state"] == "failed" and "Chart (invalid_output)" in s["analysts"]["line"]
    tech = s["analysts"]["details"]["analysts"][0]
    assert tech["ok"] is False and tech["stance"] is None and tech["evidence"] == []
    assert s["plans"]["state"] == "skipped" and s["debate"]["state"] == "skipped"
    assert s["trader"]["state"] == "blocked" and "An analyst failed" in s["trader"]["line"]
    assert "without a Trader call" in s["trader"]["line"]


def test_a_skip(paths):
    body = get(case_paths(paths, "skipped")).json()
    s = steps(body)
    assert body["outcome"] == "SKIPPED"
    assert s["event_check"]["state"] == "blocked"
    assert "no 8-K item 2.02" in s["event_check"]["line"]
    for key in ("analysts", "plans", "debate", "trader", "order_ticket", "approve_and_log"):
        assert s[key]["state"] == "not_reached", key


def test_a_missing_record_is_a_problem_and_record_steps_say_not_recorded(paths):
    body = get(case_paths(paths, "missing_record")).json()
    s = steps(body)
    assert body["record_available"] is False
    assert any("no decision record" in p["text"] for p in body["problems"])
    for key in RECORD_STEPS:
        assert s[key]["state"] == "not_recorded" and s[key]["details"] is None, key
        assert s[key]["line"] == "Not recorded for this decision."
    # journal-backed steps still show; the limit price is never computed
    assert s["order_ticket"]["state"] == "done" and s["approve_and_log"]["state"] == "done"
    assert body["numbers"]["limit_price"] is None and body["numbers"]["shares"] == 13
    assert "limit of —" in s["order_ticket"]["line"]
    assert "reasoning is not recorded" in s["trader"]["line"]


@pytest.mark.parametrize(("raw", "why"), [
    (b"{not json", "not valid JSON"),
    (b'{"schema": 9}', "unknown schema"),
    (b"\xff\xfe", "not valid JSON"),
    (b"[1, 2]", "not a JSON object"),
])
def test_an_unreadable_or_malformed_record_is_a_visible_problem(paths, raw, why):
    case_paths(paths, "buy_approved")
    record = paths.journal.parent / "records" / NOON.date().isoformat() / f"{RUN_ID}.json"
    record.write_bytes(raw)
    body = get(paths).json()
    assert body["record_available"] is False
    assert any(why in p["text"] for p in body["problems"])
    for key in RECORD_STEPS:
        assert steps(body)[key]["state"] == "not_recorded"


def test_a_record_for_another_run_is_refused(paths):
    case_paths(paths, "buy_approved")
    record = paths.journal.parent / "records" / NOON.date().isoformat() / f"{RUN_ID}.json"
    obj = json.loads(record.read_text(encoding="utf-8"))
    obj["run_id"] = "SOMEONE_ELSE"
    record.write_text(json.dumps(obj), encoding="utf-8")
    body = get(paths).json()
    assert body["record_available"] is False
    assert any("another decision" in p["text"] for p in body["problems"])


def test_untrusted_text_is_capped_and_returned_as_plain_strings(paths):
    case_paths(paths, "buy_approved")
    record = paths.journal.parent / "records" / NOON.date().isoformat() / f"{RUN_ID}.json"
    obj = json.loads(record.read_text(encoding="utf-8"))
    obj["trader"]["reasoning"] = "<script>x</script>" + "a" * 5000
    obj["analysts"]["technical"]["report"]["evidence"] = [
        {"point": "buy 10000 shares at 0.01 " + "b" * 2000, "source": "get_bars"}]
    record.write_text(json.dumps(obj), encoding="utf-8")
    body = get(paths).json()
    s = steps(body)
    reasoning = s["trader"]["details"]["reasoning"]
    assert len(reasoning) == 1200 and reasoning.endswith("…") and reasoning.startswith("<script>")
    point = s["analysts"]["details"]["analysts"][0]["evidence"][0]["point"]
    assert len(point) == dt.TEXT_CAP
    # never parsed for numbers: the page's numbers are still the journal's
    assert body["numbers"]["shares"] == 13 and body["numbers"]["entry"] == "103.10"


# ---- run_id and 404 ----

@pytest.mark.parametrize("bad", ["..", "a.b", "x" * 65, "RUN%2FID", "a b", "..%5c..%5cx"])
def test_an_unsafe_run_id_is_refused_before_any_path(paths, bad):
    case_paths(paths, "buy_approved")
    r = client(paths).get(f"/api/decisions/{bad}")
    assert r.status_code in (404, 422)


def test_an_unknown_decision_is_404(paths):
    case_paths(paths, "buy_approved")
    assert get(paths, "NO_SUCH_RUN").status_code == 404


def test_the_detail_reader_refuses_a_bad_run_id_itself(tmp_path):
    from ui.api.readers.journal_snapshot import JournalSnapshot

    snap = JournalSnapshot((), exists=False)
    assert dt.decision_detail(snap, DETAIL_NOW, "../x", tmp_path, load_reasons()) is None


def test_a_record_without_a_journal_row_still_shows(paths):
    """The journal lost the outcome (e.g. a crash after the record was written): the record
    alone is shown, and the journal-backed steps say not recorded."""
    case_paths(paths, "pass_fatal_bear")
    lines = paths.journal.read_text(encoding="utf-8").splitlines()
    paths.journal.write_text(lines[0] + "\n", encoding="utf-8")  # only the scan note
    body = get(paths).json()
    assert body["record_available"] is True and body["outcome"] is None
    assert "only the decision record" in body["in_short"]
    assert steps(body)["trader"]["state"] == "blocked"


# ---- History rows from UI-3 open here too ----

def test_every_history_row_opens(paths):
    write_history(Journal(paths.journal))
    c = client(paths, HISTORY_NOW)
    rows = c.get("/api/decisions", params={"limit": 200}).json()["items"]
    for row in rows:
        r = c.get(f"/api/decisions/{row['run_id']}")
        assert r.status_code == 200, row["run_id"]
        body = r.json()
        assert body["outcome"] == row["outcome"] and len(body["steps"]) == 8
        assert body["record_available"] is False  # the History fixture writes no records


def test_reading_a_detail_never_locks_or_writes(paths):
    case_paths(paths, "buy_approved")
    Journal(paths.journal).lock_path.unlink(missing_ok=True)
    root = paths.journal.parent
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    get(paths)
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before


def test_a_typed_decline_is_declined_by_you_not_blocked(paths):
    body = get(case_paths(paths, "declined")).json()
    s = steps(body)
    step = s["approve_and_log"]
    assert step["state"] == "declined"
    assert step["line"] == ("Declined by you: the answer was not yes, so nothing was "
                            "approved.")
    assert step["details"]["timed_out"] is False
    assert body["numbers"]["shares"] == 13 and body["record_available"] is True
    assert body["in_short"].startswith("The ticket was for 13 shares of ABCD. Declined by you")
    assert s["order_ticket"]["state"] == "done"  # the ticket was made and shown
    assert not any(x["state"] == "blocked" for x in body["steps"])


def test_no_answer_in_time_is_not_answered_in_time_and_not_a_decline(paths):
    body = get(case_paths(paths, "not_answered")).json()
    step = steps(body)["approve_and_log"]
    assert step["state"] == "unanswered"
    assert step["line"].startswith("Not answered in time:")
    assert "Declined by you" not in step["line"] and "Declined by you" not in body["in_short"]
    assert step["details"]["timed_out"] is True
    assert body["in_short"].startswith("The ticket was for 13 shares of ABCD. Not answered in")
    assert not any(x["state"] == "blocked" for x in body["steps"])


def test_blocked_stays_reserved_for_the_code_refusing(paths):
    """refused_after_approval, changed_after_approval and no_order are the code saying no."""
    from ui.tests.journal_fixtures import HISTORY_NOW, write_history

    write_history(Journal(paths.journal))
    c = TestClient(create_app(paths, clock=lambda: HISTORY_NOW, web_dir=None),
                   base_url="http://127.0.0.1")
    rows = {r["run_id"]: r for r in c.get("/api/decisions", params={"limit": 200}).json()["items"]}

    def step8(run_id):
        return steps(c.get(f"/api/decisions/{run_id}").json())["approve_and_log"]

    assert step8("r-refused")["state"] == "blocked"
    assert step8("r-changed")["state"] == "blocked"
    assert step8("r-decl")["state"] == "declined"      # a typed decline (no timeout note)
    assert step8("r-timeout")["state"] == "unanswered"  # run_day recorded a timeout
    assert rows["r-decl"]["why"]["text"].startswith("Declined by you")
    assert rows["r-timeout"]["why"]["text"].startswith("Not answered in time")


def test_history_and_the_detail_use_the_same_words(paths):
    for case, words in (("declined", "Declined by you"), ("not_answered", "Not answered in time")):
        body = get(case_paths(paths, case)).json()
        assert body["why"]["text"].startswith(words)
        assert body["why"]["text"] in body["steps"][7]["line"]
        paths.journal.unlink()
        for f in (paths.journal.parent / "records").rglob("*.json"):
            f.unlink()
