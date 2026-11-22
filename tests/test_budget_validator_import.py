"""#221: pipeline.budget's validator loads no network module, so the read-only UI may import
it. validate_symbols lives in tools.symbols (pure) and tools.scan re-exports it unchanged."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import tools.scan
import tools.symbols

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def loaded_after_importing(module: str) -> set[str]:
    code = f"import json, sys, {module}; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,
                         text=True, check=True, timeout=120)
    return set(json.loads(out.stdout))


def test_importing_pipeline_budget_loads_no_network_module():
    mods = loaded_after_importing("pipeline.budget")
    for bad in ("tools.http", "tools.scan", "tools.listing", "urllib.request", "http.client",
                "socket", "anthropic"):
        assert bad not in mods, bad
    project = {m for m in mods if m.split(".")[0] in ("agents", "journal", "pipeline", "tools")}
    assert project == {"pipeline", "pipeline.budget", "pipeline.filelock", "tools",
                       "tools.config", "tools.symbols"}


def test_tools_scan_re_exports_the_same_validator():
    assert tools.scan.validate_symbols is tools.symbols.validate_symbols
    assert "validate_symbols" in tools.scan.__all__


def test_the_validator_behaves_as_before():
    assert tools.symbols.validate_symbols([" brk.b", "AAPL", "aapl"]) == ["BRK.B", "AAPL"]
    for bad in (["A..B"], ["ß"], [7]):
        try:
            tools.symbols.validate_symbols(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
