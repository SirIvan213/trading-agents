"""The UI track's read-only rules, checked statically in the required CI job (stdlib only: no
FastAPI or Node needed). CLAUDE.md, "UI track".

1. No module under ui/api/ imports anything on the deny-list: code that can place, change or
   approve an order, the agents or the Claude client, or a network client. Every import form
   counts: `import x`, `from x import y`, `from pkg import module`, star imports from the
   project, and importlib.import_module / __import__ with a literal (a non-literal dynamic
   import is refused outright, since it cannot be checked).
2. Record and model text are rendered as plain text only: no frontend source under ui/web/
   uses dangerouslySetInnerHTML, innerHTML, outerHTML, insertAdjacentHTML or document.write,
   and ui/web/package.json depends on no Markdown or HTML renderer.

ui/tests/test_boundaries.py checks the same boundary at run time (what importing the API
actually loads) in the non-required ui job.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API = REPO / "ui" / "api"
WEB = REPO / "ui" / "web"

DENY = (
    # can place, change or approve an order, or run a decision
    "tools.broker", "pipeline.send", "pipeline.orders", "pipeline.order_flow",
    "pipeline.decide", "pipeline.run_day", "pipeline.probe_broker",
    # billed calls: the agents, the Claude client, the metered client
    "agents", "anthropic", "pipeline.metering",
    # network clients
    "tools.http", "urllib.request", "http.client", "socket", "ssl", "requests", "httpx",
    "aiohttp", "websockets",
)
PROJECT = ("agents", "journal", "pipeline", "tools")
DANGEROUS_WEB = re.compile(
    r"dangerouslySetInnerHTML|\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|document\.write")
RENDERERS = ("markdown", "marked", "remark", "rehype", "showdown", "micromark", "mdx",
             "html-react-parser", "react-html-parser", "dompurify", "sanitize-html")
_RENDERER = re.compile(r"(^|[/@-])(" + "|".join(map(re.escape, RENDERERS)) + r")([/-]|$)")
WEB_SOURCES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html")


def _denied(module: str) -> bool:
    return any(module == d or module.startswith(d + ".") for d in DENY)


def _package_of(path: Path) -> str:
    """The package a relative import in `path` resolves against (for an __init__.py, the
    package itself; for a module, its package: the same parts either way)."""
    return ".".join(path.relative_to(REPO).with_suffix("").parts[:-1])


def import_violations(source: str, package: str = "ui.api") -> list[str]:
    """Deny-listed (or uncheckable) imports in `source`, a module in `package`."""
    bad: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if _denied(a.name)]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: resolve inside the package
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if _denied(mod):
                bad.append(mod)
                continue
            for a in node.names:
                if a.name == "*":
                    if mod.split(".")[0] in PROJECT:
                        bad.append(f"{mod}.* (star import from the project)")
                elif _denied(f"{mod}.{a.name}"):
                    bad.append(f"{mod}.{a.name}")
        elif isinstance(node, ast.Call):
            f = node.func
            name = (f.attr if isinstance(f, ast.Attribute)
                    else f.id if isinstance(f, ast.Name) else "")
            if name in ("import_module", "__import__"):
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if _denied(arg.value):
                        bad.append(arg.value)
                else:
                    bad.append(f"{name}(<not a literal>)")
    return bad


def web_violations(source: str) -> list[str]:
    return DANGEROUS_WEB.findall(source)


def renderer_dependencies(package_json: dict) -> list[str]:
    deps = {**package_json.get("dependencies", {}), **package_json.get("devDependencies", {})}
    return sorted(d for d in deps if _RENDERER.search(d))


# ---- the rules, over the real tree ----

def test_the_ui_api_imports_nothing_on_the_deny_list():
    files = sorted(API.rglob("*.py"))
    assert files, "ui/api has no Python files"
    found = {str(f.relative_to(REPO)): v for f in files
             if (v := import_violations(f.read_text(encoding="utf-8"), _package_of(f)))}
    assert found == {}


def test_the_frontend_renders_text_as_plain_text_only():
    sources = [f for f in WEB.rglob("*") if f.suffix in WEB_SOURCES and f.is_file()
               and "node_modules" not in f.parts and "dist" not in f.parts] \
        if WEB.exists() else []
    found = {str(f.relative_to(REPO)): v for f in sources
             if (v := web_violations(f.read_text(encoding="utf-8")))}
    assert found == {}


def test_the_frontend_depends_on_no_markdown_or_html_renderer():
    pkg = WEB / "package.json"
    if not pkg.exists():
        return  # no frontend yet (UI-2 adds it)
    assert renderer_dependencies(json.loads(pkg.read_text(encoding="utf-8"))) == []


# ---- the checkers catch what they must (so a passing rule is not a vacuous one) ----

def test_the_import_checker_catches_every_form():
    cases = {
        "import tools.broker": ["tools.broker"],
        "import pipeline.send as s": ["pipeline.send"],
        "from pipeline import order_flow": ["pipeline.order_flow"],
        "from pipeline.orders import build_order": ["pipeline.orders"],
        "from agents.llm import make_client": ["agents.llm"],
        "import anthropic": ["anthropic"],
        "from urllib.request import urlopen": ["urllib.request"],
        "import socket": ["socket"],
        "from tools import *": ["tools.* (star import from the project)"],
        "import importlib\nimportlib.import_module('pipeline.decide')": ["pipeline.decide"],
        "__import__('tools.broker')": ["tools.broker"],
        "import importlib\nname = 'x'\nimportlib.import_module(name)":
            ["import_module(<not a literal>)"],
    }
    for src, expected in cases.items():
        assert import_violations(src) == expected, src


def test_the_import_checker_allows_the_read_only_modules():
    ok = ("from journal.log import Journal\nfrom journal.orders import replay\n"
          "from pipeline.budget import config_from_dict\nfrom tools.config import Limits\n"
          "from tools import config\nimport json, os, urllib.parse\n"
          "from .readers import state\nfrom fastapi import FastAPI\n")
    assert import_violations(ok, "ui.api") == []


def test_a_relative_import_resolves_inside_the_package():
    assert import_violations("from ..x import y", "ui.api.readers") == []
    assert import_violations("from . import shared_open", "ui.api.readers") == []


def test_the_web_checker_catches_raw_html():
    for src in ("<div dangerouslySetInnerHTML={{__html: t}} />", "el.innerHTML = text",
                "el.outerHTML = t", "el.insertAdjacentHTML('beforeend', t)",
                "document.write(t)"):
        assert web_violations(src), src
    assert web_violations("<p>{record.text}</p>") == []


def test_the_dependency_checker_catches_renderers():
    pkg = {"dependencies": {"react": "18.3.1", "react-dom": "18.3.1", "react-markdown": "9",
                            "markdown-to-jsx": "7", "remark-gfm": "4"},
           "devDependencies": {"marked": "1", "@mdx-js/react": "3", "vitest": "2",
                               "typescript": "5", "vite": "5"}}
    assert renderer_dependencies(pkg) == ["@mdx-js/react", "markdown-to-jsx", "marked",
                                          "react-markdown", "remark-gfm"]
