"""GET /api/health over fixture files: read-only, and problems are never hidden."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from pipeline.budget import Caps, DailyBudget
from ui.api.app import create_app
from ui.tests.journal_fixtures import MONDAY

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def client(paths, now=MONDAY):
    return TestClient(create_app(paths, clock=lambda: now), base_url="http://127.0.0.1")


def configs(paths):
    for name in ("limits.example.json",):
        src = open(os.path.join(REPO, "config", name), encoding="utf-8").read()
        (paths.config_dir / name.replace(".example", "")).write_text(src, encoding="utf-8")


def heartbeat(paths, at):
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.state_dir / "run_day.heartbeat.json").write_text(
        json.dumps({"pid": 1, "at": at.isoformat(), "phase": "scan"}))


def tree(folder):
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


def test_health_on_a_healthy_day(paths, journal):
    configs(paths)
    DailyBudget(Caps(10, 600_000, 3, 5), date(2026, 11, 23), paths.state_dir).open_day(
        started_today=False)
    heartbeat(paths, MONDAY - timedelta(seconds=10))
    body = client(paths).get("/api/health").json()
    assert body["ok"] and body["read_only"] and body["problems"] == []
    assert body["journal"] == {"file": "decisions.jsonl", "exists": True,
                               "entries": len(journal.entries()), "tail_incomplete": False,
                               "error": None, "orders_ok": True}
    assert body["limits"] == {"source": "limits.json", "ok": True, "error": None}
    assert body["state"]["heartbeat"]["age_s"] == 10.0
    assert not body["state"]["heartbeat"]["stale"]
    assert body["state"]["budget"] == {"file": "budget-2026-11-23.json", "exists": True,
                                       "error": None}


def test_health_never_opens_the_budget_file(paths, journal, monkeypatch):
    """Until #213, opening the budget file could make run_day's write fail on Windows. Every
    way to open a file is recorded: the readers' helpers, open/io.open, os.open, and (on
    Windows) CreateFileW itself."""
    import builtins
    import io

    import ui.api.readers.config as cfg
    import ui.api.readers.journal_snapshot as js
    import ui.api.readers.shared_open as so
    import ui.api.readers.state as st

    configs(paths)
    DailyBudget(Caps(10, 600_000, 3, 5), date(2026, 11, 23), paths.state_dir).open_day(
        started_today=False)
    opened = []
    for mod, name in ((st, "read_bytes_shared"), (cfg, "read_bytes_shared"),
                      (js, "open_shared")):
        real = getattr(mod, name)
        monkeypatch.setattr(mod, name, lambda p, real=real: (opened.append(str(p)),
                                                              real(p))[1])

    def recording(real):
        def wrapper(file, *a, **k):
            opened.append(str(file))
            return real(file, *a, **k)
        return wrapper

    monkeypatch.setattr(builtins, "open", recording(builtins.open))
    monkeypatch.setattr(io, "open", recording(io.open))
    monkeypatch.setattr(os, "open", recording(os.open))
    if os.name == "nt":
        monkeypatch.setattr(so, "_create_file", recording(so._create_file))
    assert client(paths).get("/api/health").status_code == 200
    assert opened and not any("budget-" in p for p in opened)


def test_health_never_writes_under_the_journal_or_config(paths, journal):
    configs(paths)
    journal.lock_path.unlink()
    root = paths.journal.parent.parent
    before = tree(root)
    c = client(paths)
    for _ in range(3):
        assert c.get("/api/health").status_code == 200
    assert tree(root) == before


def test_problems_are_reported_never_hidden(paths, journal):
    with paths.journal.open("ab") as fh:
        fh.write(b"garbage\n")
    paths.state_dir.mkdir(parents=True)
    (paths.state_dir / "run_day.heartbeat.json").write_text("{bad")
    body = client(paths).get("/api/health").json()
    texts = " | ".join(p["text"] for p in body["problems"])
    assert not body["ok"]
    assert "journal line" in texts and "run_day.heartbeat.json is not JSON" in texts
    assert "no limits config" in texts
    assert body["state"]["heartbeat"]["stale"]  # no heartbeat: not running


def test_an_impossible_order_history_is_a_problem(paths, journal):
    configs(paths)
    journal.append({"status": "closed", "client_order_id": "ta-nope",
                    "at": MONDAY.isoformat()})
    body = client(paths).get("/api/health").json()
    assert body["journal"]["orders_ok"] is False and not body["ok"]


def test_a_stale_heartbeat_reads_as_stale(paths, journal):
    configs(paths)
    heartbeat(paths, MONDAY - timedelta(seconds=91))
    assert client(paths).get("/api/health").json()["state"]["heartbeat"]["stale"]


def test_a_heartbeat_from_the_future_is_stale_and_a_problem(paths, journal):
    configs(paths)
    heartbeat(paths, MONDAY + timedelta(minutes=5))
    body = client(paths).get("/api/health").json()
    assert body["state"]["heartbeat"]["stale"] and not body["ok"]
    assert any("in the future" in p["text"] for p in body["problems"])


def test_an_unlistable_state_folder_is_a_problem(paths, journal, monkeypatch):
    import ui.api.readers.state as st

    configs(paths)

    def denied(path):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(st.os, "scandir", denied)
    body = client(paths).get("/api/health").json()
    assert body["state"]["budget"]["exists"] is None and not body["ok"]


def test_no_journal_yet_is_healthy_and_creates_nothing(tmp_path):
    from ui.api.paths import Paths

    p = Paths(tmp_path / "j" / "decisions.jsonl", tmp_path / "j" / "state", tmp_path / "c")
    body = client(p).get("/api/health").json()
    assert body["journal"]["exists"] is False and body["journal"]["orders_ok"] is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("host", ["testserver", "evil.example", "127.0.0.1.evil.example"])
def test_a_foreign_host_header_is_refused(paths, host):
    c = TestClient(create_app(paths, clock=lambda: MONDAY), base_url=f"http://{host}")
    assert c.get("/api/health").status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.1:8765"])
def test_local_host_headers_are_accepted(paths, host):
    c = TestClient(create_app(paths, clock=lambda: MONDAY), base_url=f"http://{host}")
    assert c.get("/api/health").status_code == 200


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_writes_are_refused(paths, method):
    r = getattr(client(paths), method)("/api/health")
    assert r.status_code == 405


def test_there_are_no_docs_or_openapi_routes(paths):
    c = client(paths)
    for url in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(url).status_code == 404
