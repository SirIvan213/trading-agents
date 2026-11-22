"""GET /api/explainer and the built frontend on the same origin (UI-2, #215).

The golden response ui/fixtures/api/explainer.json is what the frontend's Vitest tests render.
This test checks the real endpoint against it, over the example configs, so the two can never
drift. Regenerate it after a deliberate change with:
    UPDATE_GOLDEN=1 .venv/Scripts/python.exe -m pytest -q ui/tests/test_explainer.py
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ui.api import explainer as ex
from ui.api.app import CSP, create_app
from ui.tests.journal_fixtures import MONDAY

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "ui" / "fixtures" / "api" / "explainer.json"


@pytest.fixture
def cfg(paths):
    for name in ("limits.example.json", "run_day.example.json"):
        shutil.copy(REPO / "config" / name, paths.config_dir / name.replace(".example", ""))
    return paths


def get(paths, url="/api/explainer", web_dir=None):
    c = TestClient(create_app(paths, clock=lambda: MONDAY, web_dir=web_dir),
                   base_url="http://127.0.0.1")
    return c.get(url)


def test_the_endpoint_matches_the_golden_json(cfg):
    body = get(cfg).json()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    assert body == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_limits_are_shown_as_recorded_with_their_source(cfg):
    body = get(cfg).json()
    by_key = {x["key"]: x for x in body["limits"]}
    assert by_key["max_loss_per_trade"] == {"key": "max_loss_per_trade", "unit": "usd",
                                            "label": "most a single trade can lose",
                                            "value": "50.00"}
    assert by_key["flat_by"]["value"] == "15:55" and by_key["max_trades_per_day"]["value"] == "3"
    assert body["limits_source"] == {"source": "limits.json", "ok": True, "error": None}
    assert body["paper"] is True and body["problems"] == []


def test_the_example_limits_are_used_and_named_when_there_is_no_limits_json(cfg):
    (cfg.config_dir / "limits.json").rename(cfg.config_dir / "limits.example.json")
    assert get(cfg).json()["limits_source"]["source"] == "limits.example.json"


def test_caps_come_from_run_day_json_beside_the_hard_ceilings(cfg):
    caps = {c["key"]: c for c in get(cfg).json()["caps"]}
    assert caps["max_decisions"] == {"key": "max_decisions", "value": 10, "ceiling": 10,
                                     "label": "AI decisions per day, at most"}
    assert caps["max_tokens"]["value"] == 600_000 and caps["max_av_calls"]["ceiling"] == 5


def test_a_lower_cap_is_shown_as_configured(cfg):
    raw = json.loads((cfg.config_dir / "run_day.json").read_text())
    raw["caps"]["max_decisions"] = 4
    (cfg.config_dir / "run_day.json").write_text(json.dumps(raw))
    caps = {c["key"]: c for c in get(cfg).json()["caps"]}
    assert caps["max_decisions"]["value"] == 4 and caps["max_decisions"]["ceiling"] == 10


def test_a_cap_above_its_ceiling_is_a_problem_never_a_value(cfg):
    raw = json.loads((cfg.config_dir / "run_day.json").read_text())
    raw["caps"]["max_decisions"] = 11
    (cfg.config_dir / "run_day.json").write_text(json.dumps(raw))
    body = get(cfg).json()
    assert all(c["value"] is None for c in body["caps"])
    assert [c["ceiling"] for c in body["caps"]] == [10, 600_000, 3, 5]
    assert body["caps_source"]["ok"] is False
    assert any("refuses to start" in p["text"] for p in body["problems"])
    assert "11" not in json.dumps(body["problems"])


def test_missing_run_day_json_is_a_problem(cfg):
    (cfg.config_dir / "run_day.json").unlink()
    body = get(cfg).json()
    assert body["caps_source"] == {"source": None, "ok": False,
                                   "error": "config/run_day.json is missing: run_day refuses "
                                            "to start"}
    assert all(c["value"] is None for c in body["caps"])


def test_invalid_limits_are_a_problem_and_never_echoed(cfg):
    (cfg.config_dir / "limits.json").write_text('{"paper": true, "key": "sk-SECRET-1"}')
    body = get(cfg).json()
    assert all(x["value"] is None for x in body["limits"]) and body["paper"] is None
    assert body["limits_source"]["error"] == "limits config is invalid"
    assert "SECRET" not in json.dumps(body)


def test_the_explainer_never_constructs_a_budget_or_takes_its_lock(cfg, monkeypatch):
    import pipeline.budget as pb
    import pipeline.filelock as fl

    def refused(*a, **k):
        raise AssertionError("the UI touched the daily budget")

    monkeypatch.setattr(pb.DailyBudget, "__init__", refused)
    monkeypatch.setattr(pb, "exclusive_file_lock", refused)
    monkeypatch.setattr(fl, "exclusive_file_lock", refused)
    assert get(cfg).status_code == 200
    assert get(cfg, "/api/health").status_code == 200
    assert not (cfg.state_dir / "budget.lock").exists()


def test_the_ceilings_given_out_are_a_copy(cfg):
    import pipeline.budget as pb

    before = dict(pb.CEILINGS)
    ex.ceilings()["max_decisions"] = 99
    assert pb.CEILINGS == before


@pytest.mark.parametrize(("change", "message"), [
    (lambda d: d["steps"].pop(), "8 steps"),
    (lambda d: d["steps"][0].update(actor="robot"), "actor"),
    (lambda d: d["phases"][0].update(state="soon"), "state"),
    (lambda d: d.pop("glossary"), "glossary"),
])
def test_the_content_shape_is_checked(tmp_path, change, message):
    data = json.loads(ex.CONTENT.read_text(encoding="utf-8"))
    change(data)
    bad = tmp_path / "explainer.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        ex.load_content(bad)


def test_the_content_has_the_eight_tagged_steps():
    data = ex.load_content()
    assert [s["title"] for s in data["steps"]] == [
        "Scan", "Event check", "Analysts", "Plans", "Debate", "Trader", "Order ticket",
        "Approve and log"]
    assert [s["actor"] for s in data["steps"]] == [
        "code", "code", "ai", "code", "ai", "ai", "code", "you"]


# ---- the frontend on the same origin ----

@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>t</title><div id=root></div>")
    (d / "assets" / "app.js").write_text("console.log(1)")
    return d


def test_the_built_frontend_is_served_at_the_root(cfg, dist):
    r = get(cfg, "/", web_dir=dist)
    assert r.status_code == 200 and "<div id=root>" in r.text
    assert r.headers["content-security-policy"] == CSP
    assert get(cfg, "/assets/app.js", web_dir=dist).text == "console.log(1)"
    assert get(cfg, "/api/explainer", web_dir=dist).json()["steps"]  # not shadowed


def test_the_static_files_are_read_only_and_stay_inside_dist(cfg, dist):
    c = TestClient(create_app(cfg, clock=lambda: MONDAY, web_dir=dist),
                   base_url="http://127.0.0.1")
    for method in ("post", "put", "delete"):
        assert getattr(c, method)("/index.html").status_code == 405
    (dist.parent / "secret.txt").write_text("no")
    for url in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%5csecret.txt",
                "/%2e%2e%5csecret.txt", "/assets/../../secret.txt", "/assets/..%5c..%5csecret.txt"):
        r = c.get(url)
        assert r.status_code == 404 and r.text != "no", url


def test_without_a_build_the_root_says_how_to_build(cfg, tmp_path):
    r = get(cfg, "/", web_dir=tmp_path / "missing")
    assert r.status_code == 200 and "npm run build" in r.text


@pytest.mark.parametrize("url", ["/api/explainer", "/api/health", "/"])
def test_every_response_carries_the_security_headers(cfg, url):
    h = get(cfg, url).headers
    assert h["content-security-policy"] == CSP and h["x-content-type-options"] == "nosniff"
    assert "connect-src 'self'" in CSP and "font-src 'self'" in CSP


def test_the_csp_allows_nothing_from_another_origin():
    for directive in CSP.split(";"):
        sources = directive.split()[1:]
        assert all(s in ("'self'", "'none'", "data:") for s in sources), directive


def test_paper_false_is_always_a_problem(cfg):
    raw = json.loads((cfg.config_dir / "limits.json").read_text())
    raw["paper"] = False
    (cfg.config_dir / "limits.json").write_text(json.dumps(raw))
    body = get(cfg).json()
    assert body["problems"], "paper: false must never read as healthy"
    assert body["paper"] in (False, None)


def test_the_kill_switch_is_shown_and_a_problem(cfg):
    raw = json.loads((cfg.config_dir / "limits.json").read_text())
    raw["trading_enabled"] = False
    (cfg.config_dir / "limits.json").write_text(json.dumps(raw))
    body = get(cfg).json()
    assert body["trading_enabled"] is False
    assert any("kill switch" in p["text"] for p in body["problems"])
    health = get(cfg, "/api/health").json()
    assert health["trading_enabled"] is False and health["paper"] is True


def test_health_carries_paper_for_the_header(cfg):
    body = get(cfg, "/api/health").json()
    assert body["paper"] is True and body["trading_enabled"] is True
    (cfg.config_dir / "limits.json").write_text("{bad")
    body = get(cfg, "/api/health").json()
    assert body["paper"] is None and body["trading_enabled"] is None


HEALTH_GOLDEN = REPO / "ui" / "fixtures" / "api" / "health.json"


def test_health_matches_the_golden_json(cfg):
    """The header's mode badges render from this golden response in Vitest."""
    body = get(cfg, "/api/health").json()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        HEALTH_GOLDEN.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
    assert body == json.loads(HEALTH_GOLDEN.read_text(encoding="utf-8"))
