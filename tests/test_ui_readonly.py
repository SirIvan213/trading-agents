"""The UI track's read-only rules, checked statically in the required CI job (stdlib only: no
FastAPI or Node needed). CLAUDE.md, "UI track".

1. Nothing ui/api/ imports, directly OR through the project modules it imports (followed
   transitively through agents/, journal/, pipeline/, tools/ and ui/), is on the deny-list:
   code that can place, change or approve an order, run a decision, call Claude, or reach
   the network. So `from tools.listing import ...` is refused here: tools.listing imports
   tools.http.
   pipeline.budget is the one exception, and a narrow one: ui/api may import only
   load_run_day_config and CEILINGS from it (`from pipeline.budget import ...`), never the
   module itself, and may never name DailyBudget, the budget's file lock or a function's
   __globals__. So the UI cannot construct a DailyBudget or take its lock.
   Every import form counts: `import x`, `from x import y`, `from pkg import module`, and
   relative imports. ui/api itself may not use dynamic code at all: importlib, runpy,
   subprocess, exec, eval, compile or __import__, and no star import from the project.
2. Record and model text are rendered as plain text only: no frontend source under ui/web/
   uses raw-HTML sinks (dangerouslySetInnerHTML/__html, innerHTML, outerHTML,
   insertAdjacentHTML, setHTMLUnsafe, createContextualFragment, srcdoc, document.write,
   new Function), and ui/web/package.json depends on no Markdown or HTML renderer.

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
    # project modules that load a network client (tools.scan -> tools.http)
    "tools.scan", "tools.http",
    # network clients
    "urllib.request", "http.client", "socket", "ssl", "smtplib", "ftplib", "imaplib",
    "poplib", "telnetlib", "nntplib", "xmlrpc", "requests", "httpx", "httpcore", "urllib3",
    "aiohttp", "websockets",
)
# ui/api itself: no dynamic imports or code, which a static check cannot follow
DENY_IN_UI_API = ("importlib", "runpy", "subprocess", "multiprocessing", "ctypes.util",
                  "pipeline.filelock")
# pipeline.budget: only these names, and only by `from pipeline.budget import ...`
BUDGET_NAMES = frozenset({"load_run_day_config", "CEILINGS"})
# names ui/api may never use: what writes or locks the budget, and the way around an import
BUDGET_FORBIDDEN = frozenset({"DailyBudget", "exclusive_file_lock", "try_lock", "lock_path",
                              "LockTimeout", "__globals__", "__dict__"})
# ways around a static name check: refused in ui/api outright
EVASION_ATTRS = frozenset({"modules", "budget", "__getattribute__"})  # sys.modules, pkg.budget
# builtins called by name (re.compile and the like are attribute calls and stay allowed)
DYNAMIC_BUILTINS = ("exec", "eval", "compile", "__import__")
DYNAMIC_FUNCTIONS = ("import_module", "run_module", "run_path", "__import__")
# attribute calls that run code or start a process (os.system, builtins.exec, os.spawnv ...);
# `compile` only off `re` (re.compile is allowed)
PROCESS_ATTRS = ("system", "popen", "startfile", "exec", "eval", "compile", "fork",
                 "forkpty", "posix_spawn", "posix_spawnp")
PROCESS_PREFIXES = ("spawn", "exec")
PROJECT = ("agents", "journal", "pipeline", "tools", "ui")

DANGEROUS_WEB = re.compile(
    r"dangerouslySetInnerHTML|__html\b|\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML"
    r"|setHTMLUnsafe|createContextualFragment|\bsrcdoc\b|document\.write|new\s+Function\b")
RENDERERS = ("markdown", "marked", "remark", "remarkable", "rehype", "showdown", "micromark",
             "mdx", "commonmark", "snarkdown", "html-react-parser", "react-html-parser",
             "dompurify", "sanitize-html")
_RENDERER = re.compile(r"(^|[/@-])(" + "|".join(map(re.escape, RENDERERS)) + r")([/-]|$)")
WEB_SOURCES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".html", ".vue",
               ".svelte", ".css", ".scss", ".sass", ".less")
# an absolute URL to another origin in a frontend source: the page talks to its own API only
# any host (dotted, numeric or bare) except 127.0.0.1 or localhost; "// a comment" is no URL
EXTERNAL_URL = re.compile(
    r"(?:https?:)?//(?!(?:127\.0\.0\.1|localhost)(?:[:/\"'\s)]|$))[a-z0-9\[]",
    re.IGNORECASE)
DEP_SECTIONS = ("dependencies", "devDependencies", "peerDependencies",
                "optionalDependencies")


def _matches(module: str, names: tuple[str, ...]) -> bool:
    return any(module == d or module.startswith(d + ".") for d in names)


def _package_of(path: Path, root: Path = REPO) -> str:
    """The package a relative import in `path` resolves against (for an __init__.py, the
    package itself; for a module, its package: the same parts either way)."""
    return ".".join(path.relative_to(root).with_suffix("").parts[:-1])


def _module_file(module: str, root: Path = REPO) -> Path | None:
    """The file for a project module (`a.b` -> a/b.py or a/b/__init__.py), or None."""
    base = root.joinpath(*module.split("."))
    for f in (base.with_suffix(".py"), base / "__init__.py"):
        if f.is_file():
            return f
    return None


def _process_call(f: ast.Attribute) -> bool:
    if f.attr == "compile":
        return not (isinstance(f.value, ast.Name) and f.value.id == "re")
    return f.attr in PROCESS_ATTRS or f.attr.startswith(PROCESS_PREFIXES)


def _imports(source: str, package: str,
             root: Path = REPO) -> tuple[list[str], list[str]]:
    """(modules imported by `source`, dynamic-code problems) for a module in `package`.
    `from x import y` yields x, and x.y too when x.y is a module of the project."""
    mods: list[str] = []
    dynamic: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: resolve inside the package
                parts = package.split(".")
                base = parts[: len(parts) - node.level + 1]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            mods.append(mod)
            for a in node.names:
                if a.name == "*":
                    if mod.split(".")[0] in PROJECT:
                        dynamic.append(f"{mod}.* (star import from the project)")
                elif mod.split(".")[0] in PROJECT and _module_file(f"{mod}.{a.name}",
                                                                     root):
                    mods.append(f"{mod}.{a.name}")
                elif _matches(f"{mod}.{a.name}", DENY):
                    mods.append(f"{mod}.{a.name}")
        elif isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id in DYNAMIC_BUILTINS + DYNAMIC_FUNCTIONS) \
                    or (isinstance(f, ast.Attribute) and f.attr in DYNAMIC_FUNCTIONS):
                name = f.id if isinstance(f, ast.Name) else f.attr
                dynamic.append(f"{name}() (dynamic code)")
            elif isinstance(f, ast.Attribute) and _process_call(f):
                dynamic.append(f"{f.attr}() (runs code or a process)")
    return mods, dynamic


def _with_parents(module: str) -> list[str]:
    """`a.b.c` -> [a, a.b, a.b.c]: importing a module runs its packages' __init__ too."""
    parts = module.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


def budget_violations(source: str) -> list[str]:
    """Why `source` (ui/api) uses pipeline.budget beyond load_run_day_config and CEILINGS."""
    bad: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            bad += [f"import {a.name} (only `from pipeline.budget import ...`)"
                    for a in node.names if _matches(a.name, ("pipeline.budget",))]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if node.module == "pipeline" and any(a.name == "budget" for a in node.names):
                bad.append("from pipeline import budget (only its two names)")
            elif node.module == "pipeline.budget":
                bad += [f"pipeline.budget.{a.name} (only {sorted(BUDGET_NAMES)})"
                        for a in node.names if a.name not in BUDGET_NAMES]
        elif isinstance(node, ast.Name) and node.id in BUDGET_FORBIDDEN:
            bad.append(f"{node.id} (the UI never writes or locks the budget)")
        elif isinstance(node, ast.Attribute) and node.attr in BUDGET_FORBIDDEN:
            bad.append(f".{node.attr} (the UI never writes or locks the budget)")
        elif isinstance(node, ast.Constant) and node.value in BUDGET_FORBIDDEN:
            bad.append(f"{node.value!r} (the UI never writes or locks the budget)")
        if isinstance(node, ast.Import) and any(a.name == "pipeline" for a in node.names):
            bad.append("import pipeline (reach pipeline.budget only by its two names)")
        if isinstance(node, ast.Attribute) and node.attr in EVASION_ATTRS:
            bad.append(f".{node.attr} (cannot be checked statically)")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "vars":
                bad.append("vars() (cannot be checked statically)")
            elif node.func.id in ("getattr", "setattr", "hasattr") and not (
                    len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)):
                bad.append(f"{node.func.id}() with a computed name (cannot be checked)")
    return bad


def import_violations(source: str, package: str = "ui.api", root: Path = REPO) -> list[str]:
    """Why `source` (a module in ui/api's `package`) breaks rule 1, or []. A transitive
    violation reads as a chain: "pipeline.budget -> tools.scan -> tools.http". `root` is the
    folder project modules are looked up in (the repo; a fake tree in the checker's tests)."""
    mods, bad = _imports(source, package, root)
    bad += [m for m in mods if _matches(m, DENY_IN_UI_API)]
    bad += budget_violations(source)
    seen: dict[str, str | None] = {}
    denied: set[str] = set()
    queue: list[tuple[str, str | None]] = [(m, None) for m in mods]
    while queue:
        mod, via = queue.pop(0)
        for m in _with_parents(mod):
            if m in seen:
                if m in denied:
                    break  # already reported through this package
                continue
            seen[m] = via
            if _matches(m, DENY):
                denied.add(m)
                chain = [m]
                while seen.get(chain[-1]):
                    chain.append(seen[chain[-1]])  # type: ignore[arg-type]
                bad.append(" -> ".join(reversed(chain)))
                break  # its submodules are part of the same violation
            if m.split(".")[0] not in PROJECT:
                continue
            f = _module_file(m, root)
            if f is not None:
                sub, _ = _imports(f.read_text(encoding="utf-8"), _package_of(f, root), root)
                queue += [(s, m) for s in sub]
    return bad


def web_violations(source: str) -> list[str]:
    return DANGEROUS_WEB.findall(source)


def renderer_dependencies(package_json: dict) -> list[str]:
    deps: set[str] = set()
    for section in DEP_SECTIONS:
        deps |= set(package_json.get(section, {}) or {})
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


def web_sources() -> list[Path]:
    if not WEB.exists():
        return []
    return [f for f in WEB.rglob("*") if f.suffix in WEB_SOURCES and f.is_file()
            and "node_modules" not in f.parts and "dist" not in f.parts]


def test_the_frontend_loads_nothing_from_another_origin():
    """No CDN fonts, analytics or telemetry: no source names another origin's URL."""
    found = {str(f.relative_to(REPO)): u for f in web_sources()
             if (u := EXTERNAL_URL.findall(f.read_text(encoding="utf-8")))}
    assert found == {}


def test_the_external_url_checker():
    for src in ('<link href="https://fonts.googleapis.com/css2">', "fetch('https://x.io/a')",
                "@import url(//cdn.example.com/a.css);", "src: url(http://evil.test/f.woff2)",
                "http://10.0.0.5/", "http://127.0.0.1.nip.io/", "https://intranet/",
                "http://localhost.evil.test/", "fetch('http://[::1]:80/')"):
        assert EXTERNAL_URL.findall(src), src
    for src in ('proxy: { "/api": "http://127.0.0.1:8765" }', "fetch('/api/explainer')",
                "http://localhost:5173/", "// a comment", "a // b"):
        assert not EXTERNAL_URL.findall(src), src


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
        "from agents.llm import make_client": ["agents"],
        "import anthropic": ["anthropic"],
        "from urllib.request import urlopen": ["urllib.request"],
        "import socket": ["socket"],
        "import smtplib": ["smtplib"],
        "import urllib3": ["urllib3"],
        "from tools import *": ["tools.* (star import from the project)"],
    }
    for src, expected in cases.items():
        assert import_violations(src) == expected, src


def test_the_import_checker_follows_project_imports_transitively(tmp_path):
    """On a fake tree, so the test does not depend on what real modules import today."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "__init__.py").write_text("")
    (tmp_path / "tools" / "nice.py").write_text("from tools import middle\n")
    (tmp_path / "tools" / "middle.py").write_text(
        "def later():\n    from .http import get_json\n")  # inside a function, relative
    (tmp_path / "tools" / "clean.py").write_text("import json\n")
    assert import_violations("from tools.nice import x", root=tmp_path) == [
        "tools.nice -> tools.middle -> tools.http"]
    assert import_violations("from tools import nice", root=tmp_path) == [
        "tools.nice -> tools.middle -> tools.http"]
    assert import_violations("from tools.clean import x", root=tmp_path) == []
    assert import_violations("import tools.scan") == ["tools.scan"]
    assert import_violations("from pipeline import budget") == [
        "from pipeline import budget (only its two names)"]


def test_pipeline_budget_allows_only_its_two_names():
    ok = "from pipeline.budget import CEILINGS, load_run_day_config\n"
    assert import_violations(ok) == []
    for src in ("from pipeline.budget import DailyBudget",
                "from pipeline.budget import config_from_dict",
                "from pipeline.budget import *",
                "import pipeline.budget",
                "import pipeline.budget as b",
                "from pipeline import budget",
                "from pipeline.filelock import exclusive_file_lock",
                ok + "load_run_day_config.__globals__['DailyBudget']",
                ok + "getattr(load_run_day_config, '__globals__')",
                ok + "x = DailyBudget",
                ok + "y.lock_path",
                "import pipeline\npipeline.budget.config_from_dict({})",
                "import pipeline.filelock",
                "import sys\nsys.modules['pipeline.budget']",
                ok + "getattr(load_run_day_config, '__glo' + 'bals__')",
                "import x\nvars(x)"):
        assert import_violations(src), src


def test_the_import_checker_refuses_dynamic_code_in_ui_api():
    for src in ("import importlib\nimportlib.import_module('pipeline.decide')",
                "from importlib import import_module as im\nim('tools.broker')",
                "__import__('tools.broker')", "exec('import tools.broker')",
                "eval('1')", "import runpy\nrunpy.run_module('pipeline.send')",
                "import subprocess", "compile('x', 'f', 'exec')",
                "import os\nos.system('x')", "import os\nos.popen('x')",
                "import os\nos.spawnv(0, 'x', [])", "import os\nos.execv('x', [])",
                "import os\nos.startfile('x')", "import builtins\nbuiltins.exec('x')",
                "import builtins\nbuiltins.eval('1')", "import regex\nregex.compile('x')"):
        assert import_violations(src), src


def test_the_import_checker_allows_the_read_only_modules():
    ok = ("from journal.log import Journal\nfrom journal.orders import replay\n"
          "from tools.config import Limits\nfrom tools import config\n"
          "from tools.risk_check import DayState\nimport json, os, urllib.parse, ctypes\n"
          "from .readers import state\nfrom fastapi import FastAPI\n"
          "import re\nPATTERN = re.compile('x')\n")
    assert import_violations(ok, "ui.api") == []


def test_a_relative_import_resolves_inside_the_package():
    assert import_violations("from . import shared_open", "ui.api.readers") == []
    assert import_violations("from .shared_open import open_shared", "ui.api.readers") == []


def test_the_web_checker_catches_raw_html():
    for src in ("<div dangerouslySetInnerHTML={{__html: t}} />", "el.innerHTML = text",
                'el["innerHTML"] = t', "el.outerHTML = t",
                "el.insertAdjacentHTML('beforeend', t)", "el.setHTMLUnsafe(t)",
                "range.createContextualFragment(t)", "<iframe srcdoc={t} />",
                "document.write(t)", "new Function(t)"):
        assert web_violations(src), src
    assert web_violations("<p>{record.text}</p>") == []
    assert web_violations("const re = /x/; el.textContent = t") == []


def test_the_dependency_checker_catches_renderers():
    pkg = {"dependencies": {"react": "18.3.1", "react-dom": "18.3.1", "react-markdown": "9",
                            "markdown-to-jsx": "7", "remark-gfm": "4"},
           "devDependencies": {"marked": "1", "@mdx-js/react": "3", "vitest": "2",
                               "typescript": "5", "vite": "5"},
           "peerDependencies": {"remarkable": "2"},
           "optionalDependencies": {"snarkdown": "2", "commonmark": "0.31"}}
    assert renderer_dependencies(pkg) == [
        "@mdx-js/react", "commonmark", "markdown-to-jsx", "marked", "react-markdown",
        "remark-gfm", "remarkable", "snarkdown"]
