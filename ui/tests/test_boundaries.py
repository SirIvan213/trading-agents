"""The UI's hard boundaries, checked at run time (the static deny-list check is
tests/test_ui_readonly.py, in the required CI job):
  - importing the API loads only allow-listed project modules: nothing that can place, change
    or approve an order, no agent or Claude client, no network data source;
  - every route is GET;
  - the server binds 127.0.0.1 only, with no way to choose another host.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from fastapi.routing import APIRoute

from ui.api import __main__ as launcher
from ui.api.app import create_app

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT = ("agents", "journal", "pipeline", "tools", "ui")
# Every project module the API may load (directly or through these). Adding one means
# checking it can never place, change or approve an order, call Claude or reach the network.
ALLOWED = {
    "journal", "journal.log", "journal.orders",
    "tools", "tools.config", "tools.risk_check", "tools.symbols", "tools.market_calendar",
    "tools.slippage",
    # pipeline.budget for load_run_day_config and CEILINGS only (#215; the static check
    # refuses every other name), and what it imports: none reaches the network (#221)
    "pipeline", "pipeline.budget", "pipeline.filelock", "tools.atomic_write",
}
DENIED = ("tools.broker", "pipeline.send", "pipeline.orders", "pipeline.order_flow",
          "pipeline.decide", "pipeline.run_day", "pipeline.metering", "pipeline.probe_broker",
          "agents", "anthropic", "tools.http", "tools.scan")
# Stdlib network modules are not listed: the framework loads some (starlette imports
# http.client for its status codes). ui/api's own code may not import them (the static check,
# tests/test_ui_readonly.py), and the network guard (conftest) fails any call.


def loaded_modules() -> list[str]:
    code = ("import json, sys; import ui.api.app, ui.api.__main__; "
            "print(json.dumps(sorted(sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,
                         text=True, check=True, timeout=120)
    return json.loads(out.stdout)


def test_importing_the_api_loads_only_allow_listed_project_modules():
    mods = loaded_modules()
    project = {m for m in mods if m.split(".")[0] in PROJECT and m.split(".")[0] != "ui"}
    assert project <= ALLOWED, sorted(project - ALLOWED)
    for bad in DENIED:
        assert not any(m == bad or m.startswith(bad + ".") for m in mods), bad
    # fastapi depends on opentelemetry-api, which is inert without an SDK and an exporter
    assert not any(m.startswith(("opentelemetry.sdk", "opentelemetry.exporter")) for m in mods)


def test_every_route_is_get(tmp_path):
    from ui.api.paths import Paths

    app = create_app(Paths(tmp_path / "j.jsonl", tmp_path, tmp_path))
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert routes
    for r in routes:
        assert r.methods <= {"GET", "HEAD"}, (r.path, r.methods)


def test_the_server_binds_127_0_0_1_only():
    calls = []
    assert launcher.main(["--port", "9001"], run=lambda app, **kw: calls.append(kw)) == 0
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 9001
    assert launcher.HOST == "127.0.0.1"


@pytest.mark.parametrize("argv", [["--host", "0.0.0.0"], ["--port", "80"], ["--port", "x"]])
def test_there_is_no_way_to_choose_another_host_or_a_bad_port(argv):
    with pytest.raises(SystemExit):
        launcher.main(argv, run=lambda app, **kw: None)
