"""Rule-2 isolation: the order path and the deterministic tools never touch debate output."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {"BullCase", "BearCase", "DebateRecord", "DebateTurn"}
FORBIDDEN_MODULES = {
    "agents.debate", "agents.bull", "agents.bear", "agents.debate_common",
    "pipeline.debate",
}
FILES = [ROOT / "pipeline" / "orders.py", *sorted((ROOT / "tools").glob("*.py"))]


def violations(source: str) -> list[str]:
    """Imports, names, attributes, aliases and string literals naming debate types/modules."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in FORBIDDEN_MODULES:
                out.append(f"from {mod}")
            for a in node.names:
                if a.name in FORBIDDEN or (a.asname in FORBIDDEN) or a.name == "*" and mod in (
                        "agents.schemas",):
                    out.append(f"import {a.name}")
                if f"{mod}.{a.name}" in FORBIDDEN_MODULES:
                    out.append(f"import {mod}.{a.name}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in FORBIDDEN_MODULES or a.name.split(".")[-1] in FORBIDDEN:
                    out.append(f"import {a.name}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN:
            out.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
            out.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(f in node.value for f in FORBIDDEN):
                out.append(f"string {node.value[:30]!r}")
    return out


def test_files_found():
    assert (ROOT / "pipeline" / "orders.py").is_file()
    assert len(FILES) > 3


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_debate_types_in_order_path_or_tools(path):
    assert violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("src", [
    "from agents.schemas import BullCase\n",
    "from agents.schemas import DebateRecord as R\n",
    "from agents.schemas import ExitPlanRecord, BearCase\n",
    "import agents.schemas as s\nx = s.DebateTurn\n",
    "from agents import debate\n",
    "from agents.debate import run_debate\n",
    "import agents.bull\n",
    "def f(x: BearCase): ...\n",
    "x = isinstance(y, DebateRecord)\n",
    "n = 'BullCase'\n",
])
def test_guard_detects_violations(src):
    assert violations(src)


@pytest.mark.parametrize("src", [
    "from agents.schemas import ExitPlanRecord\n",
    "import json\nx = 1\n",
])
def test_guard_allows_clean_code(src):
    assert violations(src) == []
