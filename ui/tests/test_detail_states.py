"""Decision detail: honest step states for every kind of row, records that are malformed in
their nested fields (never a 500), records that say the decision stopped, and untrusted
text made safe to show (UI-4, #217; the review's blockers B1-B3)."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from journal.log import Journal
from ui.api import detail as dt
from ui.api.app import create_app
from ui.api.history import load_reasons
from ui.api.readers.journal_snapshot import JournalSnapshot
from ui.tests.detail_fixtures import DETAIL_NOW, NOON, RECORDS, RUN_ID, write_case
from ui.tests.journal_fixtures import HISTORY_NOW, write_history

RECORD_STEPS = ("analysts", "plans", "debate")


def get(paths, run_id=RUN_ID, now=DETAIL_NOW):
    c = TestClient(create_app(paths, clock=lambda: now, web_dir=None),
                   base_url="http://127.0.0.1")
    r = c.get(f"/api/decisions/{run_id}")
    assert r.status_code == 200, r.text
    return r.json()


def steps(body):
    return {s["key"]: s for s in body["steps"]}


def record_file(paths):
    return paths.journal.parent / "records" / NOON.date().isoformat() / f"{RUN_ID}.json"


def rewrite_record(paths, change):
    path = record_file(paths)
    obj = json.loads(path.read_text(encoding="utf-8"))
    change(obj)
    path.write_text(json.dumps(obj), encoding="utf-8")


# ---- B1: FAILED rows never get a made-up ticket or risk verdict ----

@pytest.fixture
def history_rows(paths):
    write_history(Journal(paths.journal))
    c = TestClient(create_app(paths, clock=lambda: HISTORY_NOW, web_dir=None),
                   base_url="http://127.0.0.1")
    rows = {r["run_id"]: r for r in c.get("/api/decisions", params={"limit": 200}).json()["items"]}
    return paths, rows


@pytest.mark.parametrize("kind", ["r-lost", "r-odd", "alert-CVX", "alert-HD"])
def test_failed_rows_show_no_ticket_and_no_risk_verdict(history_rows, kind):
    paths, rows = history_rows
    if kind.startswith("alert-"):
        run_id = next(k for k, r in rows.items() if r["symbol"] == kind.split("-")[1])
    else:
        run_id = kind
    s = steps(get(paths, run_id, HISTORY_NOW))
    assert s["order_ticket"]["state"] == "not_recorded"
    assert "Risk check" not in s["order_ticket"]["line"]
    assert "PASS" not in s["order_ticket"]["line"]
    if kind == "r-lost":
        assert s["approve_and_log"]["state"] == "failed"
        assert "decline could not be recorded" in s["approve_and_log"]["line"]
    else:
        assert s["approve_and_log"]["state"] == "not_recorded"


def test_every_history_row_gets_states_its_sources_support(history_rows):
    paths, rows = history_rows
    for run_id, row in rows.items():
        s = steps(get(paths, run_id, HISTORY_NOW))
        if row["outcome"] in ("PASS", "SKIPPED"):
            assert s["order_ticket"]["state"] == "not_reached", run_id
        if row["outcome"] == "FAILED":
            assert s["order_ticket"]["state"] in ("not_recorded", "done"), run_id
            assert s["order_ticket"]["state"] != "done" or row["why"]["code"] in (
                "journal_locked", "journal_error"), run_id


# ---- B2: a record malformed in its nested fields is a problem, never a 500 ----

MALFORMED = {
    "analyst is a string": lambda r: r["analysts"].update(technical="oops"),
    "report is a list": lambda r: r["analysts"]["technical"].update(report=[1]),
    "evidence is a dict": lambda r: r["analysts"]["technical"]["report"].update(evidence={}),
    "evidence item is a string": lambda r: r["analysts"]["technical"]["report"].update(
        evidence=["x"]),
    "confidence is a string": lambda r: r["analysts"]["technical"]["report"].update(
        confidence="high"),
    "confidence is a bool": lambda r: r["trader"].update(confidence=True),
    "turn is a string": lambda r: r["debate"].update(turns=["x"]),
    "case is a list": lambda r: r["debate"]["turns"][0].update(case=[1]),
    "side is a number": lambda r: r["debate"]["turns"][0].update(side=3),
    "final_bull is a string": lambda r: r["debate"].update(final_bull="x"),
    "key_points is an int": lambda r: r["debate"]["turns"][0]["case"].update(key_points=5),
    "forced_pass is a list": lambda r: r["trader"].update(forced_pass=["x"]),
    "action is unknown": lambda r: r["trader"].update(action="SELL"),
    "plan is a string": lambda r: r.update(exit_plans=["x"]),
    "plan entry is a number": lambda r: r["exit_plans"][0].update(entry=103.1),
    "limit_price is a number": lambda r: r["proposal"].update(limit_price=103.21),
    "error type is a dict": lambda r: r.update(error={"type": {}, "reason_code": "x"}),
    "evidence is an int": lambda r: r["analysts"]["technical"]["report"].update(evidence=5),
    "evidence is a bool": lambda r: r["analysts"]["technical"]["report"].update(
        evidence=True),
    "turns is an int": lambda r: r["debate"].update(turns=5),
    "confidence is not finite": lambda r: r["trader"].update(confidence=float("nan")),
}


@pytest.mark.parametrize("change", sorted(MALFORMED))
def test_a_record_malformed_inside_is_a_problem_not_a_500(paths, change):
    write_case("buy_approved", paths.journal)
    rewrite_record(paths, MALFORMED[change])
    body = get(paths)
    assert body["record_available"] is False
    assert any("unusable" in p["text"] for p in body["problems"]), change
    for key in RECORD_STEPS:
        assert steps(body)[key]["state"] == "not_recorded", (change, key)
    assert steps(body)["order_ticket"]["state"] == "done"  # the journal still shows


def test_the_crash_guard_falls_back_to_the_journal(paths, monkeypatch):
    """A shape the contract let through but a step can't use: a problem, and the page is
    built from the journal alone (never a 500)."""
    write_case("buy_approved", paths.journal)
    real = dt._plans_step

    def breaks_on_a_record(record, row):
        if record is not None:
            raise AttributeError("an unexpected shape")
        return real(record, row)

    monkeypatch.setattr(dt, "_plans_step", breaks_on_a_record)
    body = get(paths)
    assert any("unexpected shape" in p["text"] for p in body["problems"])
    assert body["record_available"] is False
    assert steps(body)["plans"]["state"] == "not_recorded"
    assert steps(body)["order_ticket"]["state"] == "done"


# ---- B3: a record that says the decision stopped is shown, never hidden ----

def test_an_error_record_is_a_problem_and_marks_where_it_stopped(paths):
    write_case("pass_fatal_bear", paths.journal)

    def crashed_in_debate(r):
        r["debate"] = None
        r["trader"] = None
        r["outcome"] = {"status": "error", "record_error": False, "approval": None,
                        "sent": None}
        r["error"] = {"type": "RuntimeError", "reason_code": "exception"}

    rewrite_record(paths, crashed_in_debate)
    body = get(paths)
    s = steps(body)
    assert any("stopped on an error (RuntimeError, exception)" in p["text"]
               for p in body["problems"])
    assert s["analysts"]["state"] == "done"
    assert s["debate"]["state"] == "failed"  # it was reached and failed: never "not reached"


def test_a_not_started_record_without_a_journal_row(paths):
    write_case("skipped", paths.journal)
    paths.journal.write_text("", encoding="utf-8")  # the journal lost the skip

    def not_started(r):
        r["outcome"]["status"] = "not_started"
        r["error"] = {"type": "CapReached", "reason_code": "cap_reached"}

    rewrite_record(paths, not_started)
    body = get(paths)
    s = steps(body)
    assert "it stopped before the analysts" in body["in_short"]
    for key in ("analysts", "plans", "debate", "trader", "order_ticket", "approve_and_log"):
        assert s[key]["state"] == "not_reached", key


def test_a_trader_call_that_failed_is_failed_not_a_pre_call_pass(paths):
    write_case("pass_fatal_bear", paths.journal)

    def api_error(r):
        r["trader"]["forced_pass"] = "api_error"
        r["debate"]["fatal"] = False

    rewrite_record(paths, api_error)
    s = steps(get(paths))
    assert s["trader"]["state"] == "failed"
    assert "without a Trader call" not in s["trader"]["line"]
    assert load_reasons()["why"]["pass:api_error"] in s["trader"]["line"]


def test_a_failed_bear_call_fails_closed(paths):
    write_case("pass_fatal_bear", paths.journal)

    def bear_failed(r):
        for t in r["debate"]["turns"]:
            if t["side"] == "bear":
                t["run"]["ok"] = False

    rewrite_record(paths, bear_failed)
    s = steps(get(paths))
    assert s["debate"]["state"] == "failed" and "fails closed" in s["debate"]["line"]


def test_a_missing_analyst_is_shown_as_not_recorded(paths):
    write_case("buy_approved", paths.journal)
    rewrite_record(paths, lambda r: r["analysts"].pop("sentiment"))
    s = steps(get(paths))
    assert "News not recorded" in s["analysts"]["line"] and "None" not in s["analysts"]["line"]


# ---- finding the record, run_id, untrusted text ----

def test_a_record_is_found_in_another_day_folder_and_odd_folders_are_ignored(paths):
    write_case("pass_fatal_bear", paths.journal)
    root = paths.journal.parent / "records"
    src = record_file(paths)
    other = root / "2026-10-01"
    other.mkdir()
    src.rename(other / src.name)
    (root / "not-a-day").mkdir()
    (root / "not-a-day" / src.name).write_text("{}", encoding="utf-8")
    paths.journal.write_text("", encoding="utf-8")  # no row: only the folder listing finds it
    body = get(paths)
    assert body["record_available"] is True


@pytest.mark.parametrize("bad", ["abc\n", "../x", "a.b", "", "x" * 65, "٣٣"])
def test_the_reader_refuses_a_bad_run_id_itself(tmp_path, bad):
    snap = JournalSnapshot((), exists=False)
    assert dt.decision_detail(snap, DETAIL_NOW, bad, tmp_path, load_reasons()) is None


def test_control_and_bidi_characters_are_made_visible():
    text = "buy‮evil⁦x\u0007\ttab\nline"
    out = dt.cap(text)
    assert "‮" not in out and "⟨U+202E⟩" in out and "⟨U+2066⟩" in out
    assert "⟨U+0007⟩" in out and "\t" in out and "\n" in out


def test_record_text_with_bidi_reaches_the_page_made_visible(paths):
    write_case("buy_approved", paths.journal)
    rewrite_record(paths, lambda r: r["trader"].update(reasoning="ok‮KCAB"))
    reasoning = steps(get(paths))["trader"]["details"]["reasoning"]
    assert reasoning == "ok⟨U+202E⟩KCAB"


def test_every_golden_record_still_passes_the_full_contract():
    for path in RECORDS.glob("*.json"):
        assert dt.validate_record(json.loads(path.read_text(encoding="utf-8")), RUN_ID) is None


def test_an_approved_entry_without_a_gate_does_not_claim_a_recheck(paths):
    write_case("buy_approved", paths.journal)
    lines = paths.journal.read_text(encoding="utf-8").splitlines()
    approved = json.loads(lines[1])
    approved["gate"] = None
    paths.journal.write_text(lines[0] + "\n" + json.dumps(approved) + "\n", encoding="utf-8")
    s = steps(get(paths, now=NOON + timedelta(minutes=1)))
    assert "earnings and halt rechecked" not in s["approve_and_log"]["line"]


def test_the_validator_never_raises_out_of_the_endpoint(paths, monkeypatch):
    write_case("buy_approved", paths.journal)

    def boom(obj, run_id):
        raise TypeError("unexpected")

    monkeypatch.setattr(dt, "validate_record", boom)
    body = get(paths)
    assert body["record_available"] is False
    assert any("unexpected shape" in p["text"] for p in body["problems"])


def test_an_error_status_without_its_error_is_still_a_problem(paths):
    write_case("pass_fatal_bear", paths.journal)

    def no_error(r):
        r["outcome"]["status"] = "error"
        r["error"] = None

    rewrite_record(paths, no_error)
    body = get(paths)
    assert any("stopped on an error (type not recorded)" in p["text"]
               for p in body["problems"])


def test_an_unknown_forced_pass_code_is_capped_and_made_visible(paths):
    write_case("pass_fatal_bear", paths.journal)
    rewrite_record(paths, lambda r: r["trader"].update(forced_pass="\u202e" + "z" * 500))
    line = steps(get(paths))["trader"]["line"]
    assert "\u202e" not in line and "⟨U+202E⟩" in line and len(line) < 200


# ---- round 3: inputs JSON allows but the page cannot send, sizes, code strings ----

def test_an_unpaired_surrogate_is_a_problem_not_a_500(paths):
    write_case("buy_approved", paths.journal)
    # json.dumps writes the lone surrogate as the JSON escape \ud800, which JSON allows
    lone = chr(0xD800)
    rewrite_record(paths, lambda r: r["analysts"]["technical"]["report"]["evidence"][0]
                   .update(point=lone + " breakout"))
    body = get(paths)
    assert body["record_available"] is False
    assert any("unpaired surrogate" in p["text"] for p in body["problems"])


def test_an_integer_too_long_to_parse_is_a_problem_not_a_500(paths):
    write_case("buy_approved", paths.journal)
    record_file(paths).write_text('{"schema": 1, "x": ' + "9" * 5000 + "}", encoding="utf-8")
    body = get(paths)
    assert any("not valid JSON" in p["text"] for p in body["problems"])


def test_a_record_over_the_size_limit_is_not_read(paths, monkeypatch):
    write_case("buy_approved", paths.journal)
    monkeypatch.setattr(dt, "MAX_RECORD_BYTES", 100)
    body = get(paths)
    assert body["record_available"] is False
    assert any("too large" in p["text"] for p in body["problems"])


def test_code_written_record_strings_are_capped_and_made_visible(paths):
    write_case("pass_analyst_failed", paths.journal)
    long_rlo = "‮" + "q" * 5000

    def odd(r):
        r["analysts"]["technical"]["run"]["reason_code"] = long_rlo
        r["outcome"]["status"] = "error"
        r["error"] = {"type": long_rlo, "reason_code": long_rlo}
        r["coverage"]["extra_key"] = {"nested": ["x" * 100000]}

    rewrite_record(paths, odd)
    body = get(paths)
    s = steps(body)
    assert len(s["analysts"]["line"]) < 300 and "‮" not in s["analysts"]["line"]
    assert all(len(p["text"]) < 300 and "‮" not in p["text"] for p in body["problems"])
    assert set(s["event_check"]["details"]["coverage"]) == {
        "covered", "last_item_202", "reason", "fetch_failed"}


@pytest.mark.parametrize("analyst", [{"report": None, "run": None},
                                     {"report": None, "run": {"ok": True,
                                                              "reason_code": None}}])
def test_an_analyst_with_nothing_recorded_is_not_a_guessed_done(paths, analyst):
    write_case("buy_approved", paths.journal)
    rewrite_record(paths, lambda r: r["analysts"].update(technical=analyst))
    s = steps(get(paths))
    assert "Chart not recorded" in s["analysts"]["line"]
    assert "None" not in s["analysts"]["line"]
    assert s["analysts"]["details"]["analysts"][0]["ok"] is None


def test_a_journal_entry_with_an_odd_shape_is_a_problem_not_a_500(paths):
    write_case("buy_approved", paths.journal)
    lines = paths.journal.read_text(encoding="utf-8").splitlines()
    approved = json.loads(lines[1])
    approved["verdict"] = {"verdict": "PASS", "reasons": {"not": "a list"}}
    paths.journal.write_text(lines[0] + "\n" + json.dumps(approved) + "\n", encoding="utf-8")
    body = get(paths)
    assert any("journal entry has an unexpected shape" in p["text"] for p in body["problems"])
    assert all(x["state"] == "not_recorded" for x in body["steps"])


def test_a_not_started_record_names_its_error(paths):
    write_case("buy_approved", paths.journal)

    def not_started(r):
        r["outcome"]["status"] = "not_started"
        r["error"] = {"type": "ValueError", "reason_code": "exception"}

    rewrite_record(paths, not_started)
    assert any("did not start (ValueError, exception)" in p["text"]
               for p in get(paths)["problems"])


def test_the_detail_module_source_has_no_invisible_characters():
    src = Path(dt.__file__).read_text(encoding="utf-8")
    assert not dt._INVISIBLE.search(src)


def test_a_record_only_symbol_is_capped_and_made_visible(paths):
    write_case("pass_fatal_bear", paths.journal)
    paths.journal.write_text("", encoding="utf-8")  # no journal row: the record's symbol is used
    rewrite_record(paths, lambda r: r.update(symbol="‮" + "S" * 500))
    body = get(paths)
    assert len(body["symbol"]) <= 20 and "‮" not in body["symbol"]
    assert "‮" not in body["in_short"] and len(body["in_short"]) < 200
