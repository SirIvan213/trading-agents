"""Shared safety plumbing for the smoke scripts (CLAUDE.md rule 6).

A smoke script makes real, billed Claude API calls only when run with ``--billed``. Without
it, the script loads its config, prints what it WOULD run (agents, models, effort, an
estimate of the calls) and exits with NOT_BILLED, without constructing a client or reading
the API key. A caller that injects its own client (tests, offline dry runs) is never billed,
so the flag isn't needed then.

The .env file is loaded only when asked for: by default from the repo root, from
``--env-file PATH`` instead, or not at all with ``--no-dotenv``. That makes the missing-key
path testable without a real key in the repo's .env.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOT_BILLED = 3  # exit code: --billed not given, nothing was sent


def _parse_value(raw: str) -> str:
    """Value part of KEY=VALUE: quoted values keep everything inside the quotes; unquoted
    values drop a trailing ' # comment'."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end != -1:
            return raw[1:end]
    if raw.startswith("#"):
        return ""
    return re.split(r"\s#", raw, maxsplit=1)[0].strip()


def load_dotenv(path: Path) -> None:
    """Minimal .env reader. Handles a UTF-8 BOM, `export `, quotes and inline comments.

    Never overrides a non-empty exported variable. Raises ValueError for an unreadable file
    (e.g. UTF-16, which PowerShell's `>` writes). Never includes values in messages.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} is not UTF-8 (save it as UTF-8)") from exc
    except OSError as exc:
        raise ValueError(f"cannot read {path.name} ({type(exc).__name__})") from exc
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if key and not os.environ.get(key, "").strip():
            os.environ[key] = _parse_value(value)


def add_safety_flags(parser: argparse.ArgumentParser) -> None:
    """Add --billed, --env-file and --no-dotenv to a smoke script's parser."""
    parser.add_argument("--billed", action="store_true",
                        help="actually call the Claude API (billed). Without it the script "
                             "only prints what it would run and exits 3")
    env = parser.add_mutually_exclusive_group()
    env.add_argument("--env-file", type=Path, default=None, metavar="PATH",
                     help="load settings and the key from PATH instead of the repo's .env")
    env.add_argument("--no-dotenv", action="store_true",
                     help="don't read any .env file; use only the exported environment")


def load_env(args: argparse.Namespace, root: Path = ROOT) -> int | None:
    """Load the .env chosen by the flags. Return an exit code on error, else None."""
    if args.no_dotenv:
        return None
    path = args.env_file if args.env_file is not None else root / ".env"
    if args.env_file is not None and not path.is_file():
        print(f"error: --env-file {path.name} not found", file=sys.stderr)
        return 2
    try:
        load_dotenv(path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return None


def refuse_unbilled(script: str, plan: list[str]) -> int:
    """Print what would run without --billed, and return NOT_BILLED. Sends nothing."""
    print(f"== {script}: dry plan only, NOTHING was sent ==")
    for line in plan:
        print(line)
    print("\nThis run would make real, billed Claude API calls. Re-run with --billed to do so.")
    return NOT_BILLED
